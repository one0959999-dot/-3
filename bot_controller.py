"""
bot_controller.py
코어-위성 전략 자동 매매 봇 컨트롤러
- 시작 시 위성 종목 자동 스크리닝 (KOSPI 상위 50 종목)
- 종목마다 13가지 전략 백테스트 후 최고 수익 전략 개별 적용
- 보령: RSI(9) + floor 보호 / 위성: 종목별 최적 전략
- 수익 발생 시 수익금 50% 보령 자동 재투자
- 매월 1회 위성 종목 & 전략 재선정
"""

import threading
import time
import schedule
import json
from datetime import datetime

from kis_api import KisApi
from telegram_bot import TelegramNotifier
from gemini_api import GeminiApi
from strategy import CorePosition, Position, get_rsi_signal, get_signal_by_strategy, REINVEST_RATIO
from stock_screener import select_satellites, generate_daily_market_report
from main import load_config
from database import update_bot_status, save_portfolio_state, load_portfolio_state, log_trade_journal, get_recent_trades, save_ai_rules, load_ai_rules

CORE_TICKER    = "003850"
CORE_NAME      = "보령"
CORE_RATIO     = 0.30
SATELLITE_RATIO = 0.70
CORE_MIN_FLOOR_RATIO = 0.5 # 바닥 보호 물량 비율 (50%)

class BotController:
    def __init__(self, user_id, kis_config=None, telegram_config=None, gemini_config=None, core_stocks=None, is_mock=True):
        self.user_id      = user_id
        self.is_running   = False
        self.thread       = None
        self.logs         = []
        self.num_satellites = 5
        self._is_mock     = is_mock   # DB에서 읽은 사용자 설정값
        
        # 💡 [독립 자산배분 격리] 전역 변수 공유로 인한 실전/모의 간섭 버그를 원천 차단하기 위해 인스턴스 변수로 격리합니다.
        self.core_ratio      = CORE_RATIO
        self.satellite_ratio = SATELLITE_RATIO

        # 코어 종목 리스트 (JSON 파싱)
        try:
            self.user_core_stocks = json.loads(core_stocks) if core_stocks else []
        except:
            self.user_core_stocks = []

        # 포지션
        self.core_positions      = []   # list of CorePosition
        self.satellite_positions = {}   # {ticker: Position}
        self.satellite_info      = []   # [{'ticker','name','strategy_name','return_pct'}]
        self.satellite_strategies = {}  # {ticker: strategy_name}

        # 일별 수익 추적 {date_str: profit}
        self.daily_pnl = {}

        # 월별 재선정 카운터
        self.last_screen_month = None
        self.last_screen_date = None

        # 현재 강세 섹터 (대시보드 표시용)
        self.hot_sectors = []
        
        # 일일 시장 분석 리포트 (대시보드 팝업용)
        self.daily_report = None
        
        # pykrx 펀더멘털 데이터 1일 1회 캐싱용 딕셔너리
        self.fundamental_cache = {}

        # API 연동 객체
        self.kis = None
        self.telegram = None
        self.gemini = None

        if kis_config and kis_config.get('app_key'):
            self.kis = KisApi(
                app_key=kis_config.get('app_key', '').strip(),
                app_secret=kis_config.get('app_secret', '').strip(),
                account_no=kis_config.get('account_no', '').strip(),
                is_mock=kis_config.get('is_mock', True)
            )
        
        if telegram_config and telegram_config.get('token'):
            self.telegram = TelegramNotifier(
                token=telegram_config.get('token', '').strip(),
                chat_id=telegram_config.get('chat_id', '').strip()
            )
        
        if gemini_config and gemini_config.get('api_key'):
            self.gemini = GeminiApi(api_key=gemini_config.get('api_key', '').strip())
            
        # 캐시 컨테이너 정의
        self.cached_balance = None
        self.ohlcv_cache = {}  # 💎 [신규 추가] 대량 트래픽 폭주 및 IP 차단 완벽 방어용 일봉 캐시 주머니
        
        # 🔒 [스레드 안전성] 딕셔너리 동시 접근으로 인한 런타임 에러 방지용 락
        self.lock = threading.Lock()
            
        # 봇 정지 상태에서도 코어 종목 UI를 표시하기 위해 미리 로드
        self._init_dummy_cores()
        self._restore_state()
        
        # 💎 [구조 개선] 봇 가동 여부와 무관하게 24시간 잔고를 수집하는 독립 영속 스레드 가동
        self.perpetual_thread = threading.Thread(target=self._perpetual_sync_loop, daemon=True)
        self.perpetual_thread.start()
        
        self.add_log(f"User {user_id} Bot Controller 및 실시간 영속 동기화 스레드 가동 완료.")

    def _perpetual_sync_loop(self):
        """봇 가동과 상관없이 10초마다 백그라운드에서 한투 API를 찔러 최신 주가와 잔고를 캐싱합니다."""
        while True:
            try:
                if self.kis:
                    # 1. 증권사 실제 잔고 비동기 캐싱 및 내부 장부 동기화
                    real_balance = self.kis.get_account_balance()
                    if real_balance:
                        self.cached_balance = real_balance
                        self._sync_internal_balances(real_balance)
                    
                    # 2. 실전/모의 활성화된 전 종목 실시간 현재가 비동기 갱신
                    for core in self.core_positions:
                        cp = self.kis.get_current_price(core.ticker)
                        if cp: 
                            with self.lock:
                                core._last_price = cp
                        time.sleep(0.05) # 💡 트래픽 폭주 방지 딜레이
                            
                    # 🔒 [스레드 보호 및 트래픽 방지] 락을 사용하여 에러를 막고, API 한도를 넘지 않게 대기시간 상향
                    with self.lock:
                        sat_keys = list(self.satellite_positions.keys())

                    for ticker in sat_keys:
                        sp = self.kis.get_current_price(ticker)
                        if sp: 
                            with self.lock:
                                # 리스트를 도는 동안 메인 스레드에서 종목이 삭제되었을 수 있으므로 재검증
                                if ticker in self.satellite_positions:
                                    self.satellite_positions[ticker]._last_price = sp
                        # 💡 0.05초 -> 0.2초로 상향하여 KIS 서버 IP 차단(Rate Limit) 원천 방지
                        time.sleep(0.2) 
            except Exception as e:
                print(f"[_perpetual_sync_loop 에러] {e}")
            time.sleep(10)

    def _sync_internal_balances(self, real_balance):
        """한투증권 실제 데이터셋을 가상 포지션 자산 장부에 완벽히 이식하는 공통 로직"""
        with self.lock:
            try:
                if not real_balance or 'stocks' not in real_balance: return
                real_cash = float(real_balance.get('total_cash', 0))
                real_stock_value = float(real_balance.get('total_value', 0))
                total_equity = real_cash + real_stock_value
                
                # 💡 [버그 해결] 가짜 장부를 버리고 모의/실전 무관하게 한투증권 앱의 '진짜 잔고'를 그대로 가져옵니다.
                if total_equity >= 0:
                    # 💡 전역 변수 대신 인스턴스 격리 변수인 self.core_ratio 와 self.satellite_ratio 를 연산에 적용합니다.
                    target_core_pool = total_equity * self.core_ratio
                    target_sat_pool = total_equity * self.satellite_ratio
                    
                    current_core_stock_val = sum([float(s['value']) for s in real_balance['stocks'] if any(c.ticker == s['ticker'] for c in self.core_positions)])
                    per_core_cash = max(0.0, (target_core_pool - current_core_stock_val) / max(1, len(self.core_positions)))
                    for core in self.core_positions:
                        core.cash = round(per_core_cash, 2)
                        
                    current_sat_stock_val = sum([float(s['value']) for s in real_balance['stocks'] if s['ticker'] in self.satellite_positions])
                    total_sat_cash = max(0.0, target_sat_pool - current_sat_stock_val)
                    empty_sat_count = sum(1 for sat in self.satellite_positions.values() if int(sat.shares) == 0)
                    
                    for t, sat in self.satellite_positions.items():
                        if int(sat.shares) > 0: sat.cash = 0.0
                        else: sat.cash = round(total_sat_cash / max(1, empty_sat_count), 2)

                for core in self.core_positions: core.shares = 0
                for sat in self.satellite_positions.values(): sat.shares = 0

                for real_stock in real_balance['stocks']:
                    t = real_stock['ticker']
                    q = int(real_stock['shares'])
                    p = float(real_stock['purchase_price'])

                    for core in self.core_positions:
                        if core.ticker == t:
                            core.shares = q
                            core.avg_price = p
                            if core.floor_shares == 0 and q > 0:
                                core.floor_shares = max(1, int(q * CORE_MIN_FLOOR_RATIO))
                            break

                    if t in self.satellite_positions:
                        sat = self.satellite_positions[t]
                        sat.shares = q
                        sat.avg_price = p
            except Exception as e:
                print(f"⚠️ 내부 장부 동기화 중 오류 발생: {e}")

    def _init_dummy_cores(self):
        """정지 상태에서도 코어 종목을 화면에 표시하기 위해 초기 세팅 및 KIS 잔고 동기화 (비동기 처리로 딜레이 제거)"""
        self.core_positions = []
        if self.user_core_stocks:
            for c in self.user_core_stocks:
                self.core_positions.append(CorePosition(c['ticker'], c['name'], initial_cash=0))
        else:
            self.core_positions.append(CorePosition(CORE_TICKER, CORE_NAME, initial_cash=0))
            self.core_positions.append(CorePosition("047040", "대우건설", initial_cash=0))
            
        if self.kis:
            def _async_init_balance():
                try:
                    real_balance = self.kis.get_account_balance()
                    if real_balance and 'stocks' in real_balance:
                        for real_stock in real_balance['stocks']:
                            t = real_stock['ticker']
                            q = int(real_stock['shares'])
                            p = float(real_stock['purchase_price'])
                            for core in self.core_positions:
                                if core.ticker == t:
                                    core.shares = q
                                    core.avg_price = p
                                    break
                except Exception as e:
                    print(f"초기 잔고 동기화 실패: {e}")
            
            # API 응답 대기로 인한 화면 멈춤(딜레이)을 방지하기 위해 비동기 스레드로 실행합니다.
            threading.Thread(target=_async_init_balance, daemon=True).start()

    # 💎 [신규 고도화] 하루 한 번만 KIS 과거 차트를 로드하여 메모리에 고정하는 캐시 엔진
    def _get_cached_base_ohlcv(self, ticker):
        import pandas as pd
        today_str = datetime.now().strftime('%Y-%m-%d')
        
        with self.lock:
            if ticker in self.ohlcv_cache and self.ohlcv_cache[ticker]['date'] == today_str:
                return self.ohlcv_cache[ticker]['df'].copy()
        
        if self.kis:
            df = self.kis.get_ohlcv(ticker, "D")
            if df is not None and not df.empty and 'date' in df.columns:
                df['date'] = pd.to_datetime(df['date'])
                # 실시간 가격 합성 연산의 왜곡을 방지하기 위해 오늘 장중 미완성 봉 데이터 행은 완벽하게 필터링 후 캐싱
                df = df[df['date'].dt.date < datetime.now().date()].reset_index(drop=True)
                
                with self.lock:
                    self.ohlcv_cache[ticker] = {"date": today_str, "df": df}
                return df.copy()
        return pd.DataFrame()

    # 💎 [신규 고도화] 캐싱된 어제까지의 일봉 정보에 당일 실시간 시/고/저/종가를 단기 인덱스로 병합하는 무적의 데이터 합성 기능
    def _get_extended_ohlcv(self, ticker, current_price):
        import pandas as pd
        base_df = self._get_cached_base_ohlcv(ticker)
        if base_df.empty:
            if self.kis:
                return self.kis.get_ohlcv(ticker, "D")
            return pd.DataFrame()
            
        # 🟢 [버그 해결] 현재가 하나로 도배하지 않고 실시간 KIS API로 오늘 하루의 진짜 O/H/L/C를 가져와 합성합니다.
        realtime_data = self.kis.get_realtime_price_data(ticker) if self.kis else None
        
        if realtime_data:
            today_row = pd.DataFrame([{
                'date': pd.to_datetime(datetime.now().date()),
                'open': realtime_data['open'],
                'high': realtime_data['high'],
                'low': realtime_data['low'],
                'close': realtime_data['close'],
                'volume': realtime_data['volume']
            }])
        else:
            today_row = pd.DataFrame([{
                'date': pd.to_datetime(datetime.now().date()),
                'open': float(current_price),
                'high': float(current_price),
                'low': float(current_price),
                'close': float(current_price),
                'volume': 0.0
            }])
            
        extended_df = pd.concat([base_df, today_row], ignore_index=True)
        return extended_df

    # ─── 로그 ───
    def add_log(self, msg):
        t = datetime.now().strftime("%H:%M:%S")
        entry = {"time": t, "message": msg}
        self.logs.append(entry)
        print(f"[{t}] {msg}")
        if len(self.logs) > 100:
            self.logs.pop(0)

    def _send_telegram(self, message):
        """텔레그램 발송을 백그라운드 비동기로 처리하고 실전/모의 모드를 명확히 표시합니다."""
        if not self.telegram:
            return
        
        mode_prefix = "🟢[모의]" if self._is_mock else "🔴[실전]"
        final_msg = f"{mode_prefix} {message}"
        
        # 텔레그램 API 대기 시간(딜레이)으로 인한 메인 봇 병목 방지
        threading.Thread(target=self.telegram.send_message, args=(final_msg,), daemon=True).start()

    def reload_api_keys(self, kis_config, telegram_config, gemini_config, core_stocks):
        """사용자가 웹에서 API 키를 수정했을 때, 실행 중인 봇 객체들에 실시간으로 새 키를 반영합니다."""
        
        # 🚨 [잔고 딜레이 & 꼬임 방지] 계좌 키가 바뀌었으니 캐시된 잔고를 즉시 파기합니다.
        self.cached_balance = None
        
        try:
            self.user_core_stocks = json.loads(core_stocks) if core_stocks else []
        except:
            self.user_core_stocks = []

        # KIS API 객체 갱신
        if kis_config and kis_config.get('app_key'):
            self.kis = KisApi(
                app_key=kis_config.get('app_key', '').strip(),
                app_secret=kis_config.get('app_secret', '').strip(),
                account_no=kis_config.get('account_no', '').strip(),
                is_mock=kis_config.get('is_mock', True)
            )
        else:
            self.kis = None
        
        # 텔레그램 알림 객체 갱신
        if telegram_config and telegram_config.get('token'):
            self.telegram = TelegramNotifier(
                token=telegram_config.get('token', '').strip(),
                chat_id=telegram_config.get('chat_id', '').strip()
            )
        else:
            self.telegram = None
            
        self._init_dummy_cores()
        self.add_log("🔑 변경된 API 키 및 계좌 설정이 시스템에 실시간 반영되었습니다.")

    def update_mode(self, is_mock, total_cash=10000000):
        """
        봇을 멈추지 않고 실전/모의 모드를 즉시 전환하며, 해당 모드의 독립 장부를 새로 로드합니다.
        """
        mode_name = "모의투자" if is_mock else "실전투자"
        self.add_log(f"ℹ️ UI 모드가 {mode_name} 화면으로 전환되었습니다. (현재 모니터링은 독립적으로 유지됩니다.)")
        pass

    # ─── 초기화 ───
    def initialize_portfolio(self, total_cash):
        """포트폴리오 초기 구성"""
        self.add_log("포트폴리오 초기화 중...")

        # 1. 위성 종목 & 전략 스크리닝
        self.add_log("📡 위성 종목 & 최적 전략 자동 스크리닝 중... (1~2분 소요)")
        self.satellite_info, self.hot_sectors = select_satellites(kis=self.kis, n=self.num_satellites, verbose=False, gemini_client=self.gemini)
        
        from stock_screener import select_ai_core_stock
        # 결과 로그
        self.satellite_strategies = {c['ticker']: c['strategy_name'] for c in self.satellite_info}
        log_lines = []
        for i, c in enumerate(self.satellite_info):
            line = f"  {i+1}. {c['name']} ({c['ticker']}) → [{c['strategy_name']}] {c['return_pct']:+.1f}%"
            log_lines.append(line)
            self.add_log(f"✅ {line.strip()}")
            
        self._send_telegram("🔍 위성 종목 & 전략 선정!\n" + "\n".join(log_lines))

        # 2. 자금 배분
        core_budget = total_cash * self.core_ratio
        sat_budget  = total_cash * self.satellite_ratio
        per_sat     = sat_budget / self.num_satellites if self.num_satellites > 0 else 0

        # 3. 코어 포지션 초기화
        self.core_positions = []
        
        if self.user_core_stocks:
            # 사용자가 직접 설정한 코어 종목들
            per_core_budget = core_budget / len(self.user_core_stocks)
            for c in self.user_core_stocks:
                core_pos = CorePosition(c['ticker'], c['name'], initial_cash=per_core_budget)
                self.core_positions.append(core_pos)
        else:
            # 설정이 없는 경우 기본값: 보령 + AI 코어
            half_core_budget = core_budget / 2
            
            # 1. 보령
            boryung_core = CorePosition(CORE_TICKER, CORE_NAME, initial_cash=half_core_budget)
            self.core_positions.append(boryung_core)
            
            # 2. AI 코어
            ai_core_info = select_ai_core_stock(verbose=False)
            if ai_core_info:
                ai_core = CorePosition(ai_core_info['ticker'], ai_core_info['name'], initial_cash=half_core_budget)
                self.core_positions.append(ai_core)

        # 4. 위성 포지션 초기화 (가상 매수 없음)
        self.satellite_positions = {}
        for c in self.satellite_info:
            self.satellite_positions[c['ticker']] = Position(c['ticker'], c['name'], per_sat)
        self.add_log(f"위성 자금 예산 배정: 종목당 {per_sat:,.0f}원")
        
        # 5. 실제 KIS 잔고와 동기화
        if self.kis:
            real_balance = self.kis.get_account_balance()
            if real_balance and 'stocks' in real_balance:
                for real_stock in real_balance['stocks']:
                    t = real_stock['ticker']
                    q = int(real_stock['shares'])
                    p = float(real_stock['purchase_price'])
                    
                    # 코어 잔고 동기화
                    for core in self.core_positions:
                        if core.ticker == t:
                            core.shares = q
                            core.avg_price = p
                            core.floor_shares = max(1, int(q * CORE_MIN_FLOOR_RATIO)) if q > 0 else 0
                            self.add_log(f"✅ 기존 보유 동기화: 코어 {core.name} {q}주")
                            break
                            
                    # 위성 잔고 동기화
                    if t in self.satellite_positions:
                        sat = self.satellite_positions[t]
                        sat.shares = q
                        sat.avg_price = p
                        self.add_log(f"✅ 기존 보유 동기화: 위성 {sat.name} {q}주")
        
        self.last_screen_month = datetime.now().month

        # 6. 초기화 완료 후 DB에 상태 저장
        self._save_state()

    def _save_state(self):
        """현재 포트폴리오 상태를 DB에 저장 (JSON 에러 방지 수정본)"""
        try:
            state = {
                "cores": [
                     {"ticker": c.ticker, "name": c.name,
                      "shares": int(c.shares), "floor_shares": int(c.floor_shares),
                      "cash": float(c.cash), "initial_cash": float(c.initial_cash),
                      "avg_price": float(c.avg_price)}
                    for c in self.core_positions
                ],
                "satellites": {
                    ticker: {"name": pos.name, "shares": int(pos.shares),
                             "cash": float(pos.cash), "initial_cash": float(pos.initial_cash),
                             "avg_price": float(pos.avg_price)}
                    for ticker, pos in self.satellite_positions.items()
                },
                "satellite_info": self.satellite_info,
                "satellite_strategies": self.satellite_strategies,
                "hot_sectors": self.hot_sectors,
                "num_satellites": self.num_satellites,
                "last_screen_month": getattr(self, 'last_screen_month', None),
                "last_screen_date": self.last_screen_date.strftime('%Y-%m-%d') if getattr(self, 'last_screen_date', None) else None,
                "daily_pnl": self.daily_pnl,
                "daily_report": self.daily_report,
            }
            save_portfolio_state(self.user_id, state, self._is_mock)
        except Exception as e:
            print(f"⚠️ 상태 저장 실패: {e}")

    def _restore_state(self):
        """DB에서 포트폴리오 상태 복구. 성공하면 True 반환."""
        try:
            state = load_portfolio_state(self.user_id, self._is_mock)
            if not state or not state.get("cores"):
                return False

            self.add_log("🔄 이전 포트폴리오 상태를 복구하는 중...")

            # 코어 포지션 복구
            self.core_positions = []
            for c in state["cores"]:
                pos = CorePosition(c["ticker"], c["name"], initial_cash=c.get("initial_cash", 3000000))
                pos.shares = c["shares"]
                pos.floor_shares = c["floor_shares"]
                pos.cash = c["cash"]
                pos.avg_price = c.get("avg_price", 0)
                self.core_positions.append(pos)
                self.add_log(f"💎 {c['name']} 복구: {c['shares']}주")

            # 위성 포지션 복구
            self.satellite_positions = {}
            for ticker, s in state["satellites"].items():
                pos = Position(ticker, s["name"], s.get("initial_cash", 1400000))
                pos.shares = s["shares"]
                pos.cash = s["cash"]
                pos.avg_price = s.get("avg_price", 0)
                self.satellite_positions[ticker] = pos

            self.satellite_info       = state.get("satellite_info", [])
            self.satellite_strategies = state.get("satellite_strategies", {})
            self.hot_sectors          = state.get("hot_sectors", [])
            self.num_satellites       = state.get("num_satellites", 5)
            self.last_screen_month    = state.get("last_screen_month")
            lsd_str = state.get("last_screen_date")
            self.last_screen_date     = datetime.strptime(lsd_str, '%Y-%m-%d').date() if lsd_str else None
            self.daily_pnl            = state.get("daily_pnl", {})

            restored_report = state.get("daily_report", None)
            today_str = datetime.now().strftime('%Y-%m-%d')
            
            if restored_report:
                # 💡 [기능 수정] 평일 오전 11시 전 오늘 자 리포트가 생성되기 전이라도 데이터가 None으로 초기화되는 현상을 방지합니다.
                # 모의/실전 구분 없이 전날(직전 거래일) 리포트를 안전하게 보존하여 화면에 연동합니다.
                self.daily_report = restored_report
                if restored_report.get('date') == today_str:
                    pass
                elif datetime.now().weekday() >= 5:
                    self.add_log(f"📋 장 휴무일(주말)이므로 직전 거래일 분석 리포트({restored_report.get('date')})를 화면에 유지합니다.")
                else:
                    self.add_log(f"📋 오늘 자 리포트 생성 전이므로, 전날(이전 거래일) 분석 리포트({restored_report.get('date')})를 화면에 임시 노출합니다.")
            else:
                self.daily_report = None
            
            self.add_log(f"✅ 복구 완료: 코어 {len(self.core_positions)}개, 위성 {len(self.satellite_positions)}개")
            return True
        except Exception as e:
            print(f"⚠️ 상태 복구 실패 (새로 초기화): {e}")
            return False

    # ─── 트레이딩 잡 ───
    def trading_job(self):
        if not self.core_positions:
            self.add_log("⚠️ 포트폴리오가 초기화되지 않았습니다.")
            return

        now = datetime.now()
        
        # 🟢 주말 휴무장 체크
        if now.weekday() >= 5:
            if now.minute % 30 == 0:
                self.add_log(f"💤 오늘은 주말 휴무일({now.strftime('%A')})입니다. 가짜 신호 방지를 위해 매매 감시를 중단하고 휴식합니다.")
            return

        current_time_str = now.strftime('%H:%M')
        
        # 🎯 한국 주식시장 맞춤형 변동성 수급 골든타임 판단
        is_golden_hours = ("09:01" <= current_time_str <= "11:00") or ("15:00" <= current_time_str <= "15:20")
        
        if not is_golden_hours:
            if now.minute % 30 == 0:
                self.add_log(f"🕒 현재 시간({current_time_str})은 횡보 구간입니다. 신규 매수(BUY)는 중지하되 보유 종목 리스크 관리(SELL)는 유지합니다.")
        else:
            self.add_log(f"--- 🎯 골든 타임 매수/매도 전면 점검 ({current_time_str}) ---")

        # 🦅 [신규 알파 로직] 관망 및 저점 탐색 모드 (Crisis Mode) 체크
        if getattr(self, 'is_crisis_mode', False):
            if now.minute % 10 == 0:
                self.add_log("🦅 [관망 모드 유지 중] 시장의 진정이 확인될 때까지 현금을 보유하고 저점을 탐색합니다.")
            
            # KOSPI 지수(0001)를 통해 바닥 반등 여부 확인
            if self.kis:
                kospi_cp = self.kis.get_current_price("0001")
                if kospi_cp:
                    extended_df = self._get_extended_ohlcv("0001", kospi_cp)
                    if not extended_df.empty and len(extended_df) >= 5:
                        c = extended_df['close']
                        # 단기 5일 이평선 강돌파를 '저점 반등' 시그널로 판단
                        ema_5 = c.ewm(span=5, adjust=False).mean().iloc[-1]
                        
                        if kospi_cp > ema_5:
                            msg = "🚀 [저점 반등 확인!] KOSPI 지수가 단기 이평선을 회복했습니다. 관망 모드를 해제하고 확보된 현금으로 딥(Dip) 매수를 재개합니다."
                            self.add_log(msg)
                            self._send_telegram(msg)
                            self.is_crisis_mode = False  # 관망 모드 해제, 정상 매매 루프 재진입
                            self.peak_total_asset = 0    # MDD 고점 초기화
            return  # 위기 모드 중에는 아래의 개별 종목 매매(BUY/SELL) 로직을 실행하지 않음

        # 증권사 계좌 실제 현금 잔고 동기화 (영속 스레드와 상호 간섭 최소화)
        if self.kis:
            try:
                real_balance = self.kis.get_account_balance()
                if real_balance and 'stocks' in real_balance:
                    self._sync_internal_balances(real_balance)
                    self.add_log("🔄 [잔고 동기화 완료] 실제 계좌의 실시간 자산 데이터가 가상 장부에 연동되었습니다.")
                    
                    # 🚨 [신규 알파 로직] 계좌 단위 서킷브레이커 (MDD -10% 하락 시 전량 매도 후 관망 모드 진입)
                    current_total_asset = float(real_balance.get('total_cash', 0)) + float(real_balance.get('total_value', 0))
                    
                    if not hasattr(self, 'peak_total_asset'):
                        self.peak_total_asset = current_total_asset
                    elif current_total_asset > self.peak_total_asset:
                        self.peak_total_asset = current_total_asset
                        
                    if getattr(self, 'peak_total_asset', 0) > 0:
                        mdd = ((current_total_asset / self.peak_total_asset) - 1) * 100
                        if mdd <= -10.0:
                            msg = f"💥 [서킷브레이커 발동] 계좌 MDD {mdd:.2f}% 폭락! 봇을 끄지 않고, 전량 현금화 후 '관망(저점 탐색) 모드'로 전환합니다."
                            self.add_log(msg)
                            self._send_telegram(msg)
                            
                            # 🛡️ 1. 코어 종목 전량 긴급 매도
                            with self.lock:
                                safe_core_positions = list(self.core_positions)
                            for core in safe_core_positions:
                                if core.shares > 0:
                                    self.kis.sell_market_order(core.ticker, core.shares)
                                    self.add_log(f"   🔥 [긴급 청산] 코어 종목 {core.name} {core.shares}주 매도 접수 완료")
                                    
                            # 🛡️ 2. 위성 종목 전량 긴급 매도
                            with self.lock:
                                safe_satellite_items = list(self.satellite_positions.items())
                            for ticker, pos in safe_satellite_items:
                                if pos.shares > 0:
                                    self.kis.sell_market_order(ticker, pos.shares)
                                    self.add_log(f"   🔥 [긴급 청산] 위성 종목 {pos.name} {pos.shares}주 매도 접수 완료")
                            
                            # 🛡️ 3. 봇을 끄지 않고 위기 모드 플래그 ON
                            self._send_telegram("🚨 [청산 및 관망 시작] 모든 주식을 매도하여 100% 현금을 확보했습니다. 시장이 바닥을 칠 때까지 매수를 멈추고 대기합니다.")
                            self.is_crisis_mode = True 
                            return
            except Exception as e:
                self.add_log(f"⚠️ [잔고 동기화 실패] 증권사 잔고 로드 실패 (안전을 위해 기존 데이터로 대치): {e}")

        # ── 코어 현재가 및 신호 점검 ──
        with self.lock:
            safe_core_positions = list(self.core_positions)
            
        for core in safe_core_positions:
            cp = self.kis.get_current_price(core.ticker) if self.kis else None
            if not cp or cp <= 0: 
                continue # 💡 0원 또는 무효가(오류 패킷) 수신 시 무조건 패스하여 폭사 예방
                
            with self.lock:
                core._last_price = cp  # 웹 대시보드 출력 동기화
                core_shares = core.shares
                core_floor_shares = core.floor_shares
                core_cash = core.cash
                core_name = core.name
                core_ticker = core.ticker
            
                core_val = core_shares * cp
                self.add_log(
                    f"💎 {core_name} 현황: {core_shares}주 "
                    f"(floor: {core_floor_shares}주) "
                    f"× {cp:,}원 = {core_val:,}원"
                )

                # 코어 매매 로직 (RSI) - 로컬 메모리 합성 차트 강제 바인딩 (Rate Limit 제로화)
                try:
                    from strategy import get_rsi_signal
                    extended_df = self._get_extended_ohlcv(core_ticker, cp)
                    core_signal, _, core_rsi = get_rsi_signal(core_ticker, kis_api=self.kis, df=extended_df)

                    if core_signal == 'BUY' and core_cash >= cp:
                        qty = int(core_cash // cp)
                        if qty > 0:
                            if self.kis:
                                self.kis.buy_market_order(core_ticker, qty)
                            actual_qty = core.buy(cp)
                            msg = f"💎 {core_name} 매수 {actual_qty}주 @ {cp:,}원 (RSI:{core_rsi:.1f}) → 총 {core_shares + actual_qty}주"
                            self.add_log(msg)
                            self._send_telegram(msg)

                    elif core_signal == 'SELL' and core_shares > core_floor_shares:
                        sellable = core_shares - core_floor_shares
                        if sellable > 0:
                            if self.kis:
                                self.kis.sell_market_order(core_ticker, sellable)
                            qty, profit = core.sell(cp)
                            if qty > 0:
                                msg = f"💎 {core_name} 익절 매도 {qty}주 @ {cp:,}원 (RSI:{core_rsi:.1f}) | 이익 {profit:,.0f}원"
                                self.add_log(msg)
                                today_str = now.strftime('%Y-%m-%d')
                                self.daily_pnl[today_str] = self.daily_pnl.get(today_str, 0) + profit
                                self._send_telegram(msg)
                    else:
                        self.add_log(f"  [{core_name}] HOLD (RSI:{core_rsi:.1f}, floor:{core_floor_shares}주 보호)")
                except Exception as e:
                    self.add_log(f"  [{core_name}] 점검 중 오류: {str(e)}")


        # ── 위성 신호 점검 (종목별 최적 전략 적용) ──
        with self.lock:
            trading_sat_items = list(self.satellite_positions.items())

        for ticker, pos in trading_sat_items:
            try:
                with self.lock:
                    strat_name = self.satellite_strategies.get(ticker, 'RSI(9) 30/70')
                    pos_shares = pos.shares
                    pos_avg_price = pos.avg_price
                    pos_max_price = pos.max_price
                    pos_cash = pos.cash
                    pos_name = pos.name

                price = self.kis.get_current_price(ticker) if self.kis else 0
                if price <= 0: 
                    continue # 💡 0원 반환 시 ZeroDivisionError 발생 원천 차단 방패막 추가
                    
                with self.lock:
                    pos._last_price = price  # 대시보드 연동
                    
                # 💎 [핵심 리팩토링] KIS 서버 원격 조회를 배제하고 로컬 메모리 합성 데이터를 주입
                from strategy import get_signal_by_strategy
                extended_df = self._get_extended_ohlcv(ticker, price)
                signal, price, ind_val = get_signal_by_strategy(ticker, strat_name, kis_api=self.kis, df=extended_df)
                if price <= 0:
                    continue

                # 📈 [신규 연산 구조] 판다스를 이용하여 최근 14일 기준 ATR 실질 변동폭을 완벽하게 동적 산출합니다.
                import pandas as pd
                high_low = extended_df['high'] - extended_df['low']
                high_close = (extended_df['high'] - extended_df['close'].shift(1)).abs()
                low_close = (extended_df['low'] - extended_df['close'].shift(1)).abs()
                tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
                atr_14 = tr.rolling(window=14, min_periods=1).mean().iloc[-1] if not tr.empty else 0
                
                # 상장 초기 혹은 무효 패킷 예외 조건 방어코드 구축: 변동폭이 잡히지 않으면 평단가의 2%를 최소 단위 마진 폭으로 자동 우회 적용합니다.
                if atr_14 <= 0:
                    atr_14 = pos_avg_price * 0.02

                # pykrx 펀더멘털 데이터 1일 1회 캐시 연동 구조 (영속 스레드 안전화)
                today_str = datetime.now().strftime('%Y-%m-%d')
                cache_key = f"{ticker}_{today_str}"
                
                if not hasattr(self, 'fundamental_cache'):
                    self.fundamental_cache = {}
                    
                with self.lock:
                    has_cache = cache_key in self.fundamental_cache
                    
                if has_cache:
                    with self.lock:
                        financial_data = self.fundamental_cache[cache_key]
                else:
                    financial_data = "재무 데이터 조회 불가"
                    try:
                        from pykrx import stock as krx_stock
                        from datetime import timedelta
                        latest_date = krx_stock.get_business_days_dates(datetime.now() - timedelta(days=7), datetime.now())[-1]
                        fund_df = krx_stock.get_market_fundamental_by_ticker(latest_date.strftime("%Y%m%d"), latest_date.strftime("%Y%m%d"), ticker)
                        if not fund_df.empty:
                            per = fund_df.loc[ticker, 'PER']
                            pbr = fund_df.loc[ticker, 'PBR']
                            financial_data = f"PER: {per:.2f}배, PBR: {pbr:.2f}배"
                            with self.lock:
                                self.fundamental_cache[cache_key] = financial_data
                    except Exception:
                        pass

                macro_context = self.kis.get_macro_context() if self.kis else "시황 정보 없음"
                
                # 🤖 AI 판단 컨텍스트 장부에 최신 14일 ATR 실시간 변동폭 정보까지 추가 결합하여 두뇌 인지 능력을 대폭 업그레이드합니다.
                extended_strategy = f"{strat_name} | 실시간 재무상태: {financial_data} | 현재 거시 시황: {macro_context} | 최근 14일 ATR 변동폭: {atr_14:.1f}원"

                # 🟢 변동성 적응형 트레일링 스탑 (수익 보전 매도 메커니즘)
                if pos_shares > 0 and price > 0:
                    if price > pos_max_price:
                        with self.lock:
                            pos.max_price = price
                        pos_max_price = price
                    
                    # 💡 [고도화 패치] 매입 평단가 대비 최소 1.0 * ATR 수준 이상의 확실한 추세적 수익권에 들어섰을 때만 시동을 겁니다.
                    # 찰나의 노이즈가 아닌 확실한 꼭대기 고점 대비 1.5 * ATR 폭을 실시간 침범 이탈할 시 자금을 완벽하게 익절 회수합니다.
                    if pos_max_price >= pos_avg_price + (1.0 * atr_14):
                        dynamic_trailing_stop = pos_max_price - (1.5 * atr_14)
                        if price <= dynamic_trailing_stop:
                            reason = f"ATR 트레일링 스탑 (최고점 대비 1.5*ATR: {int(dynamic_trailing_stop):,}원 이탈)"
                            self.add_log(f"🎯 [{pos_name}] 변동성 추적 익절선 이탈! 수익 확정을 위해 전량 매도합니다.")
                            if self.kis:
                                self.kis.sell_market_order(ticker, pos_shares)
                            with self.lock:
                                qty, profit = pos.sell(price)
                            log_trade_journal(self.user_id, ticker, pos_name, 'SELL', price, strat_name, reason, profit=profit)
                            self._send_telegram(f"🎯 [{pos_name}] ATR 변동성 익절 완료! 손익: {profit:+,.0f}원")
                            with self.lock:
                                pos.max_price = 0  
                            continue

                # 🔴 변동성 적응형 하드 손절선 (최종 위험 한계 청산 메커니즘)
                if pos_shares > 0 and pos_avg_price > 0:
                    # 💡 [고도화 패치] 단순 기계적 일률 -5% 규격을 버리고, 해당 주식 고유 하루 변동폭의 2.5배 폭(`2.5 * ATR`)을 평단가에서 완전 하향 차감 차단합니다.
                    dynamic_hard_stop = pos_avg_price - (2.5 * atr_14)
                    if price <= dynamic_hard_stop:
                        reason = f"ATR 변동성 하드 손절 (방어선 {int(dynamic_hard_stop):,}원 이탈)"
                        self.add_log(f"🚨 [{pos_name}] 변동성 위험 한계점 돌파! 노이즈가 아닌 추세 하락으로 판단하여 전량 시장가 매도합니다.")
                        if self.kis:
                            self.kis.sell_market_order(ticker, pos_shares)
                        with self.lock:
                            qty, profit = pos.sell(price)
                        msg = f"💥 [{pos_name}] ATR 변동성 손절 완료: {qty}주 @ {price:,}원 | 손익: {profit:+,.0f}원"
                        self.add_log(msg)
                        log_trade_journal(self.user_id, ticker, pos_name, 'SELL', price, strat_name, reason, profit=profit)
                        self._send_telegram(msg)
                        with self.lock:
                            today = datetime.now().strftime('%Y-%m-%d')
                            self.daily_pnl[today] = self.daily_pnl.get(today, 0) + profit
                        continue

                # 🔵 AI 매수 조율 파트
                if signal == 'BUY' and pos_shares == 0:
                    if not is_golden_hours:
                        continue 

                    reason = "조건 충족 자동 매수"
                    if self.gemini:
                        recent_logs = get_recent_trades(self.user_id, ticker, limit=5)
                        custom_rules = load_ai_rules(self.user_id)
                        
                        is_approved, reason = self.gemini.ai_approve_trade(
                            signal='BUY', 
                            stock_name=pos_name, 
                            ticker=ticker, 
                            price=price, 
                            strategy=extended_strategy, 
                            indicator_val=ind_val, 
                            hot_sectors=self.hot_sectors,
                            recent_trades=recent_logs,
                            custom_rules=custom_rules
                        )
                        
                        if not is_approved:
                            self.add_log(f"🚫 AI 매수 거절 (재무/차트/학습 융합 판단): [{pos_name}] - {reason}")
                            continue 

                    qty = int(pos_cash // price)
                    if qty > 0:
                        if self.kis:
                            self.kis.buy_market_order(ticker, qty)
                        with self.lock:
                            actual_qty = pos.buy(price)
                        if actual_qty > 0:
                            msg = f"📈 [{pos_name}] AI 매수 승인: {actual_qty}주 @ {price:,}원 [{strat_name} → {ind_val:.1f}]"
                            self.add_log(msg)
                            log_trade_journal(self.user_id, ticker, pos_name, 'BUY', price, strat_name, reason)
                            self._send_telegram(msg)

                # 🔵 AI 매도 조율 파트
                elif signal == 'SELL' and pos_shares > 0:
                    reason = "조건 충족 자동 매도"
                    if self.gemini:
                        recent_logs = get_recent_trades(self.user_id, ticker, limit=5)
                        custom_rules = load_ai_rules(self.user_id)
                        
                        is_approved, reason = self.gemini.ai_approve_trade(
                            signal='SELL', 
                            stock_name=pos_name, 
                            ticker=ticker, 
                            price=price, 
                            strategy=f"{strat_name} | 실시간 재무상태: {financial_data}", 
                            indicator_val=ind_val, 
                            hot_sectors=self.hot_sectors,
                            recent_trades=recent_logs,
                            custom_rules=custom_rules
                        )
                        
                        if not is_approved:
                            self.add_log(f"✋ AI 매도 보류 (수익 극대화 홀딩 판단): [{pos_name}] - {reason}")
                            continue

                    if self.kis:
                        self.kis.sell_market_order(ticker, pos_shares)
                    with self.lock:
                        qty, profit = pos.sell(price)
                    msg = (f"📉 [{pos_name}] AI 매도 승인: {qty}주 @ {price:,}원 "
                           f"| 손익: {profit:+,.0f}원 [{strat_name} → {ind_val:.1f}]")
                    self.add_log(msg)
                    log_trade_journal(self.user_id, ticker, pos_name, 'SELL', price, strat_name, reason, profit=profit)
                    self._send_telegram(msg)

                    with self.lock:
                        today = datetime.now().strftime('%Y-%m-%d')
                        self.daily_pnl[today] = self.daily_pnl.get(today, 0) + profit

                    # 위성 익절 수익금 50% 코어 재투자 원자적 처리 안정화
                    if profit > 0:
                        with self.lock:
                            if self.core_positions and pos.cash >= profit * REINVEST_RATIO:
                                reinvest = profit * REINVEST_RATIO
                                pos.cash -= reinvest
                                split_amount = reinvest / len(self.core_positions)
                                for core in self.core_positions:
                                    core.cash += split_amount
                                msg_dist = f"🔄 위성 수익 {profit:,.0f}원 중 {reinvest:,.0f}원을 코어({len(self.core_positions)}개) 매수자금으로 균등 편입"
                                self.add_log(msg_dist)
                                self._send_telegram(msg_dist)
                else:
                    pass

            except Exception as e:
                self.add_log(f"⚠️ [{ticker}] 오류: {e}")

        # 매 사이클 후 상태 저장
        self._save_state()

    def _rescreen_satellites(self):
        """위성 종목 데일리 리밸런싱 (B+C 혼합형)"""
        try:
            now = datetime.now()
            if getattr(self, 'last_screen_date', None) == now.date():
                return
                
            self.add_log("📅 데일리 위성 리밸런싱 (추세 및 모멘텀 기반) 실행...")
            keep_tickers = set()
            freed_cash = 0
            
            from pykrx import stock as krx_stock
            from datetime import timedelta
            
            # 1. 기존 종목 점검 (안전한 스레드 순회를 위해 복사 장부 생성)
            with self.lock:
                sat_items = list(self.satellite_positions.items())
            
            for ticker, pos in sat_items:
                if pos.shares == 0:
                    freed_cash += pos.cash
                    with self.lock:
                        if ticker in self.satellite_positions:
                            del self.satellite_positions[ticker]
                        if ticker in self.satellite_strategies:
                            del self.satellite_strategies[ticker]
                    self.add_log(f"🔄 위성 교체 (미매수 대기 제거): {pos.name}")
                    continue
                    
                price = self.kis.get_current_price(ticker) if self.kis else 0
                
                is_uptrend = False
                try:
                    end_dt = now
                    start_dt = end_dt - timedelta(days=40)
                    df = krx_stock.get_market_ohlcv_by_date(start_dt.strftime("%Y%m%d"), end_dt.strftime("%Y%m%d"), ticker)
                    if not df.empty and len(df) >= 20:
                        c = df['종가']
                        ema5 = c.ewm(span=5, adjust=False).mean().iloc[-1]
                        ema20 = c.ewm(span=20, adjust=False).mean().iloc[-1]
                        is_uptrend = ema5 > ema20
                except:
                    is_uptrend = False

                is_hot_sector = False
                for s_info in self.satellite_info:
                    if s_info['ticker'] == ticker and s_info.get('sector') in self.hot_sectors:
                        is_hot_sector = True
                        break

                if price and pos.avg_price > 0:
                    profit_rt = (price / pos.avg_price - 1) * 100
                    
                    if is_uptrend or is_hot_sector:
                        keep_tickers.add(ticker)
                        reason = "추세 우상향" if is_uptrend else "주도 섹터"
                        self.add_log(f"🛡️ 위성 보존 ({reason}): {pos.name} ({profit_rt:+.2f}%)")
                    else:
                        if self.kis:
                            self.kis.sell_market_order(ticker, pos.shares)
                        with self.lock:
                            qty, profit = pos.sell(price)
                        
                        if profit > 0 and self.core_positions:
                            reinvest = profit * REINVEST_RATIO
                            with self.lock:
                                if pos.cash >= reinvest:
                                    pos.cash -= reinvest
                                    split = reinvest / len(self.core_positions)
                                    for core in self.core_positions:
                                        core.cash += split
                                    self.add_log(f"🔄 위성 수익 {profit:,.0f}원 중 {reinvest:,.0f}원 코어 편입")
                                
                        freed_cash += pos.cash
                        with self.lock:
                            if ticker in self.satellite_positions:
                                del self.satellite_positions[ticker]
                            if ticker in self.satellite_strategies:
                                del self.satellite_strategies[ticker]
                        self.add_log(f"🔄 위성 매도 및 교체: {pos.name} ({profit_rt:+.2f}%)")

            n_needed = self.num_satellites - len(keep_tickers)
            if n_needed <= 0:
                self.add_log("✅ 데일리 리밸런싱 완료: 전 종목 상승 추세 유지됨.")
                self.last_screen_date = now.date()
                self._save_state()
                return

            # 📉 [신규 퀀트 로직] 시장 국면(상승/하락) 판별 및 하이브리드 포트폴리오 스위칭
            is_bull_market = True
            try:
                if self.kis:
                    kospi_df = self.kis.get_ohlcv("0001", "D")
                    kosdaq_df = self.kis.get_ohlcv("2001", "D")
                    
                    kospi_bull = True
                    if kospi_df is not None and not kospi_df.empty and len(kospi_df) >= 20 and 'close' in kospi_df.columns:
                        kospi_bull = kospi_df['close'].iloc[-1] >= kospi_df['close'].rolling(20).mean().iloc[-1]
                        
                    kosdaq_bull = True
                    if kosdaq_df is not None and not kosdaq_df.empty and len(kosdaq_df) >= 20 and 'close' in kosdaq_df.columns:
                        kosdaq_bull = kosdaq_df['close'].iloc[-1] >= kosdaq_df['close'].rolling(20).mean().iloc[-1]
                    
                    is_bull_market = kospi_bull and kosdaq_bull
            except Exception as e:
                self.add_log(f"⚠️ 시장 지수 판별 중 오류 발생(기본 상승장 간주): {e}")

            # 🟢 거시 시장 국면에 따른 동적 자산 배분 비율 조정
            if not is_bull_market:
                self.core_ratio = 0.60       
                self.satellite_ratio = 0.40  
                self.add_log(f"📊 [동적 자산배분] 약세장 방어 모드 가동: 코어 {self.core_ratio*100}% / 위성 {self.satellite_ratio*100}% 변환")
            else:
                self.core_ratio = 0.30       
                self.satellite_ratio = 0.70
                self.add_log(f"📊 [동적 자산배분] 강세장 공격 모드 가동: 코어 {self.core_ratio*100}% / 위성 {self.satellite_ratio*100}% 변환")

            new_info = []
            if not is_bull_market:
                self.add_log("🚨 [하락장 감지] 코스피 또는 코스닥 지수가 20일선 밑으로 깨졌습니다. '4방패(헷지) + 1창(알파)' 양방향 인버스 체계로 포지션을 전환합니다.")
                
                # 💡 코스닥 인버스만 편입되던 한계를 극복하기 위해 코스피 인버스(114800) 종목을 헷지 자산 풀에 추가 바인딩합니다.
                defensive_etfs = [
                    {'ticker': '261240', 'name': 'KODEX 미국달러선물', 'strategy_name': 'EMA 5/20', 'return_pct': 0.0},
                    {'ticker': '411060', 'name': 'ACE KRX금현물', 'strategy_name': 'EMA 5/20', 'return_pct': 0.0},
                    {'ticker': '114800', 'name': 'KODEX 인버스', 'strategy_name': 'EMA 5/20', 'return_pct': 0.0},
                    {'ticker': '251340', 'name': 'KODEX 코스닥150선물인버스', 'strategy_name': 'EMA 5/20', 'return_pct': 0.0},
                    {'ticker': '329750', 'name': 'TIGER 미국채10년선물', 'strategy_name': 'EMA 5/20', 'return_pct': 0.0} # 🌟 기관급 방어구 추가
                ]
                
                for etf in defensive_etfs:
                    if etf['ticker'] not in keep_tickers and etf['ticker'] not in self.satellite_positions and len(new_info) < n_needed:
                        new_info.append(etf)
                        
                remaining_slots = n_needed - len(new_info)
                if remaining_slots > 0:
                    self.add_log(f"⚔️ 하락장을 역행하는 알파 헌팅을 위해 {remaining_slots}개 슬롯을 개별 주식으로 탐색합니다.")
                    raw_info, self.hot_sectors = select_satellites(kis=self.kis, n=remaining_slots * 3, verbose=False, gemini_client=self.gemini)
                    for c in raw_info:
                        if c['ticker'] not in keep_tickers and c['ticker'] not in [x['ticker'] for x in defensive_etfs]:
                            new_info.append(c)
                            if len(new_info) == n_needed:
                                break
            else:
                self.add_log("📈 [상승장 확인] 코스피와 코스닥 모두 20일선 위에서 순항 중입니다. 주도주/모멘텀 기반 개별 주식 위성 스크리닝을 진행합니다.")
                raw_info, self.hot_sectors = select_satellites(kis=self.kis, n=self.num_satellites + n_needed, verbose=False, gemini_client=self.gemini)
                for c in raw_info:
                    if c['ticker'] not in keep_tickers:
                        new_info.append(c)
                        if len(new_info) == n_needed:
                            break

            # 3. 새 종목 편입 및 예산 할당
            total_available_cash = freed_cash
            for ticker in keep_tickers:
                total_available_cash += self.satellite_positions[ticker].cash
                self.satellite_positions[ticker].cash = 0
                
            total_slots_for_cash = n_needed + sum(1 for t in keep_tickers if self.satellite_positions[t].shares == 0)
            per_budget = total_available_cash / total_slots_for_cash if total_slots_for_cash > 0 else 0
            
            for ticker in keep_tickers:
                if self.satellite_positions[ticker].shares == 0:
                    self.satellite_positions[ticker].cash = per_budget
                    
            added_lines = []
            with self.lock:
                for c in new_info:
                    self.satellite_positions[c['ticker']] = Position(c['ticker'], c['name'], per_budget)
                    self.satellite_strategies[c['ticker']] = c['strategy_name']
                    self.add_log(f"✨ 새 위성 편입: {c['name']} → [{c['strategy_name']}] {c['return_pct']:+.1f}%")
                    added_lines.append(f"  {c['name']} → [{c['strategy_name']}]")
                    
                keep_info = [c for c in self.satellite_info if c['ticker'] in keep_tickers]
                self.satellite_info = keep_info + new_info

            msg = f"📅 데일리 위성 리밸런싱 완료! (유지: {len(keep_tickers)} / 교체: {n_needed})\n" + "\n".join(added_lines)
            self._send_telegram(msg)
                
            self.last_screen_date = now.date()
            self._save_state()
            
        except Exception as e:
            self.add_log(f"🚨 위성 리밸런싱 중 오류 발생 (스케줄러 보호됨): {e}")

    def generate_daily_report(self):
        """11시 1차 매매 종료 후 시장 분석 리포트를 생성하고 텔레그램으로 실시간 알림을 보냅니다."""
        try:
            self.add_log("📝 11시 시장 분석 리포트 생성을 시작합니다...")
            report_data = generate_daily_market_report(gemini_client=self.gemini, verbose=False)
            if report_data:
                self.daily_report = report_data
                self.add_log("✅ 일일 시장 분석 리포트 생성 완료")
                self._save_state()
                
                msg = "📝 [🎯 11시 장중 시장 분석 리포트 알림]\n\n"
                if isinstance(report_data, dict):
                    msg += report_data.get('summary', report_data.get('content', '11시 시장 분석이 완료되었습니다. 자세한 정보는 대시보드 팝업창을 확인하세요!'))
                else:
                    msg += str(report_data)
                self._send_telegram(msg)
        except Exception as e:
            self.add_log(f"⚠️ 일일 리포트 생성 중 오류: {e}")

    def _weekly_self_reflection(self):
        """[AI 자가 학습] 일주일간의 매매 기록을 바탕으로 오답 노트를 작성하고 룰을 업데이트합니다."""
        self.add_log("🧠 [AI 자아성찰] 한 주간의 매매 결과를 분석하여 새로운 투자 원칙을 수립합니다...")
        
        from database import get_db_connection
        try:
            conn = get_db_connection()
            rows = conn.execute('''
                SELECT date(created_at) as date, stock_name, action, price, ai_reason, profit 
                FROM trade_journal WHERE user_id = ? ORDER BY created_at DESC LIMIT 30
            ''', (self.user_id,)).fetchall()
        except Exception as e:
            self.add_log(f"⚠️ 자아성찰 DB 조회 중 오류: {e}")
            rows = []
        finally:
            conn.close()

        if not rows:
            self.add_log("ℹ️ 이번 주 매매 기록이 없어 자아성찰을 건너뜁니다.")
            return

        history_lines = []
        for r in rows:
            p_str = f" | 손익: {r['profit']:,.0f}원" if r['action'] == 'SELL' else ""
            history_lines.append(f"- {r['date']} | {r['stock_name']} | {r['action']} | 승인이유: {r['ai_reason']}{p_str}")
        
        history_text = "\n".join(history_lines)
        
        if self.gemini:
            new_rules = self.gemini.generate_weekly_reflection(history_text)
            if new_rules:
                save_ai_rules(self.user_id, new_rules)
                self.add_log(f"✨ [AI 진화 완료] 새로운 투자 원칙이 두뇌에 각인되었습니다:\n{new_rules}")
                self._send_telegram(f"🧠 [라씨 AI 자가 학습 완료]\n\n이번 주 오답노트를 바탕으로 새로운 매매 원칙을 세웠습니다:\n\n{new_rules}")

    # ─── 봇 시작/정지 ───
    def _run_threaded(self, job_func):
        job_thread = threading.Thread(target=job_func, daemon=True)
        job_thread.start()

    def _run_loop(self, total_cash):
        self.scheduler = schedule.Scheduler()

        restored = self._restore_state()
        if not restored:
            self.initialize_portfolio(total_cash)
        else:
            self.add_log("📊 기존 포트폴리오로 매매를 재개합니다.")

        self.scheduler.every(5).minutes.do(self.trading_job)
        self.scheduler.every().day.at("11:00").do(lambda: self._run_threaded(self.generate_daily_report))
        self.scheduler.every().day.at("09:05").do(lambda: self._run_threaded(self._rescreen_satellites))
        self.scheduler.every().friday.at("16:00").do(lambda: self._run_threaded(self._weekly_self_reflection))

        self.trading_job()  
        
        if getattr(self, 'last_screen_date', None) != datetime.now().date():
            now_time_str = datetime.now().strftime('%H:%M')
            if "09:00" <= now_time_str <= "15:30":
                self.add_log("오늘 날짜의 위성 리밸런싱 기록이 없어 즉시 실행합니다...")
                self._rescreen_satellites()
            else:
                self.add_log("오늘 날짜의 위성 리밸런싱 기록이 없으나 정규장 시간이 아니므로 정규 스케줄러(09:05)까지 대기합니다.")

        today = datetime.today().strftime('%Y-%m-%d')
        if not self.daily_report or self.daily_report.get('date') != today:
            now = datetime.now()
            if now.weekday() < 5 and now.strftime('%H:%M') >= "11:00":
                self.add_log("오전 11시가 지났으나 오늘 자 시장 분석 리포트가 없어 즉시 재생성합니다...")
                self.daily_report = None
                self.generate_daily_report()
            elif now.weekday() >= 5:
                if self.daily_report:
                    self.add_log(f"📋 주말 장 휴무 모드: 이전 거래일 리포트({self.daily_report.get('date')})를 대시보드 화면에 노출합니다.")
                else:
                    self.add_log("오늘 자 시장 분석 리포트가 없고 시스템에 저장된 이전 과거 리포트도 존재하지 않습니다.")
            else:
                self.add_log("오늘 자 시장 분석 리포트가 아직 없으나, 평일 11시 이전이므로 정규 분석 스케줄을 대기합니다.")

        while self.is_running:
            self.scheduler.run_pending()
            time.sleep(1)

    def start(self, total_cash=10_000_000):
        if not self.kis:
            self.add_log("❌ API 키가 설정되지 않았습니다. [계좌 설정]에서 먼저 키를 입력해주세요.")
            return False

        if not self.is_running:
            self.is_running = True
            self.thread = threading.Thread(
                target=self._run_loop,
                args=(total_cash,),
                daemon=True
            )
            self.thread.start()
            update_bot_status(self.user_id, True)
            
            mode_str = "모의투자" if self._is_mock else "실전투자"
            self.add_log(f"▶️ [{mode_str}] 매매 봇이 시작되었습니다.")
            self._send_telegram("▶️ 봇 감시를 시작합니다.\n- 현재 모드에 맞춰 종목 감시 및 자동 매매가 활성화되었습니다.")
                
            return True
        return False

    def stop(self):
        if self.is_running:
            self.is_running = False
            update_bot_status(self.user_id, False)
            if self.thread:
                self.thread.join(timeout=3)
                
            self.add_log("⏸️ 매매 봇이 일시 정지되었습니다.")
            self._send_telegram("⏸️ 봇 감시가 일시 정지되었습니다.\n- 대기 상태로 전환되어 매수/매도가 중단됩니다.")

    # ─── 대시보드 상태 반환 ───
    def get_pnl_data(self):
        sorted_days = sorted(self.daily_pnl.keys())
        return {
            "labels": sorted_days,
            "values": [round(self.daily_pnl[d]) for d in sorted_days],
        }

    def get_status(self):
        with self.lock:
            safe_core_positions = list(self.core_positions)
            safe_satellite_items = list(self.satellite_positions.items())

        cores_data = []
        total_core_stock_val = 0
        total_core_cash_val = 0
        for core in safe_core_positions:
            cp = getattr(core, '_last_price', 0) or 0
            core_value = core.shares * cp
            total_core_stock_val += core_value
            total_core_cash_val += getattr(core, 'cash', 0)
            cores_data.append({
                "name":        core.name,
                "ticker":      core.ticker,
                "shares":      core.shares,
                "floor":       core.floor_shares,
                "price":       cp,
                "value":       core_value,
                "budget":      core.initial_cash,
                "strategy":    "장기 우상향/RSI(9)" if core.ticker != CORE_TICKER else "RSI(9) 30/70 + floor 보호"
            })

        satellites = []
        total_sat_stock_val = 0
        total_sat_cash_val = 0
        for ticker, pos in safe_satellite_items:
            sp = getattr(pos, '_last_price', 0) or 0
            sat_value = pos.shares * sp
            total_sat_stock_val += sat_value
            total_sat_cash_val += getattr(pos, 'cash', 0)
            satellites.append({
                "name":     pos.name,
                "ticker":   ticker,
                "strategy": self.satellite_strategies.get(ticker, '-'),
                "shares":   pos.shares,
                "price":    sp,
                "value":    sat_value,
                "budget":   getattr(pos, 'initial_cash', getattr(pos, 'budget', 0))
            })

        if self.cached_balance:
            api_cash = float(self.cached_balance.get('total_cash', 0))
            api_stock_val = float(self.cached_balance.get('total_value', 0))
            api_purchase = float(self.cached_balance.get('total_purchase', 0))
            
            mock_total_asset = api_cash + api_stock_val
            mock_pnl = api_stock_val - api_purchase
            mock_pnl_rt = (mock_pnl / api_purchase * 100) if api_purchase > 0 else 0
        else:
            mock_total_asset = 0
            mock_pnl = 0
            mock_pnl_rt = 0

        return {
            "is_running":    self.is_running,
            "is_mock":       self._is_mock,
            "has_keys":      self.kis is not None,
            "logs":          self.logs[-30:],
            "hot_sectors":   self.hot_sectors,
            "num_satellites": self.num_satellites,
            "cores":         cores_data,
            "satellites":    satellites,
            "mock_total_asset": mock_total_asset,
            "mock_pnl": mock_pnl,
            "mock_pnl_rt": mock_pnl_rt
        }

class BotManager:
    """모든 사용자의 봇 인스턴스를 관리합니다."""
    def __init__(self):
        self.last_assets = None
        self.bots = {}        
        self.ai_clients = {}  

    def get_bot(self, user_id, user_data=None):
        if not user_data:
            return self.bots.get((user_id, True))
            
        is_mock = bool(user_data.get('is_mock', 1))
        bot_key = (user_id, is_mock)
        
        if user_data.get('gemini_api_key'):
            api_key_clean = user_data.get('gemini_api_key').strip()
            
            if user_id not in self.ai_clients or getattr(self.ai_clients[user_id], '_current_key', '') != api_key_clean:
                from gemini_api import GeminiApi
                new_ai = GeminiApi(api_key=api_key_clean)
                new_ai._current_key = api_key_clean  
                self.ai_clients[user_id] = new_ai
                print(f"🤖 [AI 공유 엔진 활성화/갱신] User {user_id}의 최신 API 키로 공유형 AI 두뇌 세팅 완료.")

        if bot_key not in self.bots:
            prefix = 'mock_' if is_mock else 'real_'
            
            kis_config = {
                "app_key": user_data.get(f'{prefix}app_key'),
                "app_secret": user_data.get(f'{prefix}app_secret'),
                "account_no": user_data.get(f'{prefix}account_no'),
                "is_mock": is_mock
            }
            
            tele_config = {
                "token": user_data.get('telegram_token'),
                "chat_id": user_data.get('telegram_chat_id')
            }
            
            self.bots[bot_key] = BotController(
                user_id, kis_config, tele_config, gemini_config=None,
                core_stocks=user_data.get('core_stocks'),
                is_mock=is_mock
            )
            
        if user_id in self.ai_clients:
            self.bots[bot_key].gemini = self.ai_clients[user_id]
            
        return self.bots.get(bot_key)

    def stop_all(self):
        for bot in self.bots.values():
            bot.stop()

# 글로벌 매니저 인스턴스
manager = BotManager()