"""
bot_controller.py
코어-위성 전략 자동 매매 봇 컨트롤러 (Wall Street Quant 1.0)
- 시작 시 위성 종목 자동 스크리닝 (KOSPI 상위 50 종목)
- 종목마다 13가지 전략 백테스트 후 최고 수익 전략 개별 적용
- 보령: RSI(9) + floor 보호 / 위성: 종목별 최적 전략
- 수익 발생 시 수익금 50% 보령 자동 재투자
- 매월 1회 위성 종목 & 전략 재선정
- 🚨 [알파 로직 적용]: NLP 뉴스 분석, 리스크 패리티 비중 조절, MDD 위기 대응 모드 탑재
"""

import threading
import time
import schedule
import json
import pandas as pd
import requests
from bs4 import BeautifulSoup
import urllib.parse
from datetime import datetime

from kis_api import KisApi
from telegram_bot import TelegramNotifier
from gemini_api import GeminiApi
from kis_websocket import KisWebSocket  # ◀ 추가됨
from strategy import CorePosition, Position, get_rsi_signal, get_signal_by_strategy, REINVEST_RATIO
from stock_screener import select_satellites, generate_daily_market_report
from main import load_config
from database import update_bot_status, save_portfolio_state, load_portfolio_state, log_trade_journal, get_recent_trades, save_ai_rules, load_ai_rules

CORE_TICKER    = "003850"
CORE_NAME      = "보령"
CORE_RATIO     = 0.30
SATELLITE_RATIO = 0.70
CORE_MIN_FLOOR_RATIO = 0.5 # 바닥 보호 물량 비율 (50%)

def fetch_recent_news(stock_name):
    """[NLP 연동용] 네이버 실시간 뉴스 헤드라인 3개 크롤링"""
    try:
        encoded_name = urllib.parse.quote(stock_name.encode('utf-8'))
        url = f"https://search.naver.com/search.naver?where=news&query={encoded_name}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        res = requests.get(url, headers=headers, timeout=3)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, 'html.parser')
            titles = [a.get_text() for a in soup.select('.news_tit')[:3]]
            return " | ".join(titles) if titles else "최근 주요 뉴스 없음"
    except Exception:
        pass
    return "뉴스 조회 실패"

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
        self.ohlcv_cache = {}  
        
        # 🔒 [스레드 안전성] 딕셔너리 동시 접근으로 인한 런타임 에러 방지용 락
        self.lock = threading.Lock()
        
        # 🚨 [신규 추가] 월급 및 외부 입금 자동 추적용 독립 장부 변수
        self.last_asset_cost = None
        self.pnl_this_turn = 0.0
            
        # 봇 정지 상태에서도 코어 종목 UI를 표시하기 위해 미리 로드
        self._init_dummy_cores()
        self._restore_state()
        
        # 🟢 [웹소켓 실시간 가격 수신용 메모리 딕셔너리]
        self.live_prices = {}
        self.ws_client = None

        # 증권사 키가 정상적으로 있으면 웹소켓 가동 시작
        if self.kis:
            app_key = self.kis.get_approval_key()
            if app_key:
                # 콜백 함수: 웹소켓으로 가격이 들어올 때마다 live_prices 갱신 (지연시간 0.01초)
                def on_price_update(ticker, price):
                    self.live_prices[ticker] = price
                self.ws_client = KisWebSocket(app_key, is_mock=self._is_mock, price_callback=on_price_update)
                self.ws_client.start()

        # 💎 [구조 개선] 잔고를 수집하고 웹소켓 구독을 관리하는 영속 스레드 가동
        self.perpetual_thread = threading.Thread(target=self._perpetual_sync_loop, daemon=True)
        self.perpetual_thread.start()
        
        self.add_log(f"User {user_id} Bot Controller 및 실시간 웹소켓 통신망 가동 완료.")

    def _perpetual_sync_loop(self):
        """봇 가동과 상관없이 백그라운드에서 '잔고'만 캐싱합니다. (가격은 웹소켓이 실시간으로 꽂아줌 API 차단 안 당함)"""
        while True:
            try:
                if self.kis:
                    # 1. 증권사 실제 잔고 비동기 캐싱 및 내부 장부 동기화 (조회 제한 여유로움)
                    real_balance = self.kis.get_account_balance()
                    if real_balance:
                        self.cached_balance = real_balance
                        self._sync_internal_balances(real_balance)
                    
                    # 2. 웹소켓 감시망에 현재 포트폴리오 종목들이 제대로 등록되어 있는지 주기적 확인 및 갱신
                    if self.ws_client:
                        with self.lock:
                            # 코어 종목 + 위성 종목 + KOSPI 지수 대용(KODEX 200)을 모두 웹소켓 구독 목록에 담음
                            current_tickers = [c.ticker for c in self.core_positions] + list(self.satellite_positions.keys())
                            if "069500" not in current_tickers:
                                current_tickers.append("069500")

                        # 새로 감시해야 할 종목 구독
                        for t in current_tickers:
                            if t not in self.ws_client.subscribed_tickers:
                                self.ws_client.subscribe(t)
                        
                        # 팔아서 더 이상 감시 안 해도 되는 종목 구독 해제
                        for t in list(self.ws_client.subscribed_tickers):
                            if t not in current_tickers:
                                self.ws_client.unsubscribe(t)

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
                real_purchase = float(real_balance.get('total_purchase', 0))
                total_equity = real_cash + real_stock_value
                
                # 🚨 [원금 자율 역추적 엔진 개조] 10000000원 기본값 검사를 완전 폐기합니다.
                # 이제 봇을 켜는 순간(오늘 장 시작 시점) 계좌에 들어있는 총 평가자산을 오늘 하루의 "원금 기준점"으로 무조건 강제 고정합니다.
                if not getattr(self, 'initial_capital_captured', False) and total_equity > 0:
                    from database import get_db_connection
                    conn = get_db_connection()
                    # 🟢 각 봇의 신분에 맞는 전용 장부 칸을 찾아냅니다.
                    cash_col = "mock_initial_cash" if self._is_mock else "real_initial_cash"
                    conn.execute(f'UPDATE users SET {cash_col} = ? WHERE id = ?', (total_equity, self.user_id))
                    conn.commit()
                    conn.close()
                    self.initial_capital_captured = True  # 오늘 장 가동 중 중복 스냅샷 방지 플래그 선언
                    self.add_log(f"💰 [원금 자율 역추적 성공] 구동 시점의 실시간 계좌 총자산 {total_equity:,.0f}원을 시스템 원금 기준선으로 자동 동기화했습니다.")
                
                # 🚨 [신규 추가] 외부 입금(월급 등) 자동 추적 및 원금 실시간 보정 알고리즘
                current_asset_cost = real_cash + real_purchase # 주가 변동성이 제거된 순수 자산 원가
                if self.last_asset_cost is not None:
                    # KIS 서버 지연 방어: 봇이 매도를 했는데 아직 증권사 잔고가 갱신 안 됐다면 환각을 막기 위해 정산을 다음으로 보류함
                    if self.pnl_this_turn != 0 and abs(current_asset_cost - self.last_asset_cost) < 100:
                        pass 
                    else:
                        expected_asset_cost = self.last_asset_cost + self.pnl_this_turn
                        self.pnl_this_turn = 0.0 # 정산 완료 후 대기 비움
                        
                        deposit_delta = current_asset_cost - expected_asset_cost
                        if deposit_delta > 10000 or deposit_delta < -10000: # 1만원 이상 변동 시 입출금으로 간주 (수수료 오차 제외)
                            from database import get_db_connection
                            conn = get_db_connection()
                            # 🟢 입출금 추적도 각자의 장부에서만 덧셈 뺄셈을 합니다.
                            cash_col = "mock_initial_cash" if self._is_mock else "real_initial_cash"
                            conn.execute(f'UPDATE users SET {cash_col} = {cash_col} + ? WHERE id = ?', (deposit_delta, self.user_id))
                            conn.commit()
                            conn.close()
                            
                            if deposit_delta > 0:
                                self.add_log(f"💰 [자율 원금 감지] 외부 입금 포착: +{deposit_delta:,.0f}원 -> 투자 원금 자동 상향.")
                            else:
                                self.add_log(f"💸 [자율 원금 감지] 외부 출금 포착: {deposit_delta:,.0f}원 -> 투자 원금 자동 하향.")
                        
                        self.last_asset_cost = current_asset_cost
                else:
                    self.last_asset_cost = current_asset_cost
                
                if total_equity >= 0:
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

                for core in self.core_positions: 
                    core.shares = 0
                    # ❌ (맹목적 락 해제 코드 삭제 완료)
                for sat in self.satellite_positions.values(): 
                    sat.shares = 0
                    # ❌ (맹목적 락 해제 코드 삭제 완료)

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
        """정지 상태에서도 코어 종목을 화면에 표시하기 위해 초기 세팅 및 KIS 잔고 동기화"""
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
            threading.Thread(target=_async_init_balance, daemon=True).start()

    def _get_cached_base_ohlcv(self, ticker):
        today_str = datetime.now().strftime('%Y-%m-%d')
        
        with self.lock:
            if ticker in self.ohlcv_cache and self.ohlcv_cache[ticker]['date'] == today_str:
                return self.ohlcv_cache[ticker]['df'].copy()
        
        if self.kis:
            df = self.kis.get_ohlcv(ticker, "D")
            if df is not None and not df.empty and 'date' in df.columns:
                df['date'] = pd.to_datetime(df['date'])
                df = df[df['date'].dt.date < datetime.now().date()].reset_index(drop=True)
                
                with self.lock:
                    self.ohlcv_cache[ticker] = {"date": today_str, "df": df}
                return df.copy()
        return pd.DataFrame()

    def _get_extended_ohlcv(self, ticker, current_price):
        """당일 시/고/저/종가를 KIS API로 실시간으로 가져와 합성 (일봉 왜곡 버그 해결 완료)"""
        base_df = self._get_cached_base_ohlcv(ticker)
        if base_df.empty:
            if self.kis:
                return self.kis.get_ohlcv(ticker, "D")
            return pd.DataFrame()
            
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

    def add_log(self, msg):
        t = datetime.now().strftime("%H:%M:%S")
        entry = {"time": t, "message": msg}
        self.logs.append(entry)
        print(f"[{t}] {msg}")
        if len(self.logs) > 100:
            self.logs.pop(0)

    def _send_telegram(self, message):
        if not self.telegram:
            return
        mode_prefix = "🟢[모의]" if self._is_mock else "🔴[실전]"
        final_msg = f"{mode_prefix} {message}"
        threading.Thread(target=self.telegram.send_message, args=(final_msg,), daemon=True).start()

    def reload_api_keys(self, kis_config, telegram_config, gemini_config, core_stocks):
        self.cached_balance = None
        try:
            self.user_core_stocks = json.loads(core_stocks) if core_stocks else []
        except:
            self.user_core_stocks = []

        if kis_config and kis_config.get('app_key'):
            self.kis = KisApi(
                app_key=kis_config.get('app_key', '').strip(),
                app_secret=kis_config.get('app_secret', '').strip(),
                account_no=kis_config.get('account_no', '').strip(),
                is_mock=kis_config.get('is_mock', True)
            )
        else:
            self.kis = None
        
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
        mode_name = "모의투자" if is_mock else "실전투자"
        self.add_log(f"ℹ️ UI 모드가 {mode_name} 화면으로 전환되었습니다.")

    def initialize_portfolio(self, total_cash):
        self.add_log("포트폴리오 초기화 중...")

        self.add_log("📡 위성 종목 & 최적 전략 자동 스크리닝 중... (1~2분 소요)")
        self.satellite_info, self.hot_sectors = select_satellites(kis=self.kis, n=self.num_satellites, verbose=False, gemini_client=self.gemini)
        
        from stock_screener import select_ai_core_stock
        self.satellite_strategies = {c['ticker']: c['strategy_name'] for c in self.satellite_info}
        log_lines = []
        for i, c in enumerate(self.satellite_info):
            line = f"  {i+1}. {c['name']} ({c['ticker']}) → [{c['strategy_name']}] {c['return_pct']:+.1f}%"
            log_lines.append(line)
            self.add_log(f"✅ {line.strip()}")
            
        self._send_telegram("🔍 위성 종목 & 전략 선정!\n" + "\n".join(log_lines))

        core_budget = total_cash * self.core_ratio
        sat_budget  = total_cash * self.satellite_ratio
        per_sat     = sat_budget / self.num_satellites if self.num_satellites > 0 else 0

        self.core_positions = []
        
        if self.user_core_stocks:
            per_core_budget = core_budget / len(self.user_core_stocks)
            for c in self.user_core_stocks:
                core_pos = CorePosition(c['ticker'], c['name'], initial_cash=per_core_budget)
                self.core_positions.append(core_pos)
        else:
            half_core_budget = core_budget / 2
            boryung_core = CorePosition(CORE_TICKER, CORE_NAME, initial_cash=half_core_budget)
            self.core_positions.append(boryung_core)
            
            ai_core_info = select_ai_core_stock(verbose=False)
            if ai_core_info:
                ai_core = CorePosition(ai_core_info['ticker'], ai_core_info['name'], initial_cash=half_core_budget)
                self.core_positions.append(ai_core)

        self.satellite_positions = {}
        for c in self.satellite_info:
            self.satellite_positions[c['ticker']] = Position(c['ticker'], c['name'], per_sat)
        self.add_log(f"위성 자금 예산 배정: 종목당 {per_sat:,.0f}원")
        
        if self.kis:
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
                            core.floor_shares = max(1, int(q * CORE_MIN_FLOOR_RATIO)) if q > 0 else 0
                            self.add_log(f"✅ 기존 보유 동기화: 코어 {core.name} {q}주")
                            break
                            
                    if t in self.satellite_positions:
                        sat = self.satellite_positions[t]
                        sat.shares = q
                        sat.avg_price = p
                        self.add_log(f"✅ 기존 보유 동기화: 위성 {sat.name} {q}주")
        
        self.last_screen_month = datetime.now().month
        self._save_state()

    def _save_state(self):
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
        try:
            state = load_portfolio_state(self.user_id, self._is_mock)
            if not state or not state.get("cores"):
                return False

            self.add_log("🔄 이전 포트폴리오 상태를 복구하는 중...")

            self.core_positions = []
            for c in state["cores"]:
                pos = CorePosition(c["ticker"], c["name"], initial_cash=c.get("initial_cash", 3000000))
                pos.shares = c["shares"]
                pos.floor_shares = c["floor_shares"]
                pos.cash = c["cash"]
                pos.avg_price = c.get("avg_price", 0)
                self.core_positions.append(pos)
                self.add_log(f"💎 {c['name']} 복구: {c['shares']}주")

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
                self.daily_report = restored_report
                if restored_report.get('date') == today_str:
                    pass
                elif datetime.now().weekday() >= 5:
                    self.add_log(f"📋 장 휴무일(주말)이므로 직전 거래일 분석 리포트({restored_report.get('date')})를 화면에 유지합니다.")
                else:
                    self.add_log(f"📋 오늘 자 리포트 생성 전이므로, 전날 분석 리포트({restored_report.get('date')})를 임시 노출합니다.")
            else:
                self.daily_report = None
            
            self.add_log(f"✅ 복구 완료: 코어 {len(self.core_positions)}개, 위성 {len(self.satellite_positions)}개")
            return True
        except Exception as e:
            print(f"⚠️ 상태 복구 실패 (새로 초기화): {e}")
            return False

    def trading_job(self):
        if not self.core_positions:
            self.add_log("⚠️ 포트폴리오가 초기화되지 않았습니다.")
            return

        now = datetime.now()
        
        if now.weekday() >= 5:
            if now.minute % 30 == 0:
                self.add_log(f"💤 오늘은 주말 휴무일({now.strftime('%A')})입니다. 매매 감시를 중단하고 휴식합니다.")
            return

        current_time_str = now.strftime('%H:%M')
        # 🟢 [족쇄 파괴 1] 11시~15시 사이의 휴식 제한을 없애고 09:01 ~ 15:20 장중 내내 풀가동!
        is_golden_hours = ("09:01" <= current_time_str <= "15:20")
        
        if not is_golden_hours:
            if now.minute % 30 == 0:
                self.add_log(f"🕒 현재 시간({current_time_str}) 장 마감 또는 휴식 구간입니다.")
        else:
            self.add_log(f"--- 🎯 실시간 매수/매도 전면 점검 ({current_time_str}) ---")

        # 🦅 [신규 알파 로직] 관망 및 저점 탐색 모드 (Crisis Mode) 체크
        if getattr(self, 'is_crisis_mode', False):
            if now.minute % 10 == 0:
                self.add_log("🦅 [관망 모드 유지 중] 시장의 진정이 확인될 때까지 현금을 보유하고 저점을 탐색합니다.")
            
            # KOSPI 지수 대용인 KODEX 200(069500) ETF를 통해 바닥 반등 여부 확인
            if self.kis:
                # 💡 실시간 웹소켓 메모리에 들어온 0.1초 전 가격을 즉시 사용 (딜레이 0, 차단 위험 0)
                kospi_cp = self.kis.get_current_price("069500")
                if kospi_cp:
                    extended_df = self._get_extended_ohlcv("069500", kospi_cp)
                    if not extended_df.empty and len(extended_df) >= 5:
                        c = extended_df['close']
                        # 단기 5일 이평선 강돌파를 '저점 반등' 시그널로 판단
                        ema_5 = c.ewm(span=5, adjust=False).mean().iloc[-1]
                        
                        if kospi_cp > ema_5:
                            msg = "🚀 [저점 반등 확인!] KOSPI 지수가 단기 이평선을 회복했습니다. 관망 모드를 해제하고 딥(Dip) 매수를 재개합니다."
                            self.add_log(msg)
                            self._send_telegram(msg)
                            self.is_crisis_mode = False  # 관망 모드 해제
                            self.peak_total_asset = 0    # MDD 고점 초기화
            return  # 위기 모드 중에는 아래의 개별 종목 매매(BUY/SELL) 로직을 실행하지 않음

        if self.kis:
            try:
                real_balance = self.kis.get_account_balance()
                if real_balance and 'stocks' in real_balance:
                    self._sync_internal_balances(real_balance)
                    self.add_log("🔄 [잔고 동기화 완료] 실제 계좌의 실시간 자산 데이터 연동 완료.")
                    
                    # 🚨 [신규 알파 로직] 계좌 단위 서킷브레이커 (-10% 하락 시 전량 매도 후 관망)
                    current_total_asset = float(real_balance.get('total_cash', 0)) + float(real_balance.get('total_value', 0))
                    
                    if not hasattr(self, 'peak_total_asset'):
                        self.peak_total_asset = current_total_asset
                    elif current_total_asset > self.peak_total_asset:
                        self.peak_total_asset = current_total_asset
                        
                    if getattr(self, 'peak_total_asset', 0) > 0:
                        mdd = ((current_total_asset / self.peak_total_asset) - 1) * 100
                        if mdd <= -10.0:
                            # 🎯 로그 메시지 문구 변경 (순수 시장가 청산 명시)
                            msg = f"💥 [서킷브레이커 발동] 계좌 MDD {mdd:.2f}% 폭락! 자산 수호를 위해 보유 주식을 순수 시장가로 즉시 청산합니다."
                            self.add_log(msg)
                            self._send_telegram(msg)
                            
                            with self.lock:
                                safe_core_positions = list(self.core_positions)
                            for core in safe_core_positions:
                                if core.shares > 0:
                                    # 🚨 변경: 하한가로 밀려서라도 무조건 즉시 체결되는 순수 시장가(01) 메서드 호출
                                    self.kis.sell_panic_market_order(core.ticker, core.shares)
                                    self.add_log(f"   🔥 [긴급 강제 청산] 코어 종목 {core.name} {core.shares}주 순수 시장가 매도 완료")
                                    
                            with self.lock:
                                safe_satellite_items = list(self.satellite_positions.items())
                            for ticker, pos in safe_satellite_items:
                                if pos.shares > 0:
                                    # 🚨 변경: 하한가로 밀려서라도 무조건 즉시 체결되는 순수 시장가(01) 메서드 호출
                                    self.kis.sell_panic_market_order(ticker, pos.shares)
                                    self.add_log(f"   🔥 [긴급 강제 청산] 위성 종목 {pos.name} {pos.shares}주 순수 시장가 매도 완료")
                            
                            self._send_telegram("🚨 [청산 완료] 전 자산 100% 현금화 완료. 안전 장부 모드로 전환합니다.")
                            self.is_crisis_mode = True 
                            return
            except Exception as e:
                self.add_log(f"⚠️ [잔고 동기화 실패] 증권사 잔고 로드 실패: {e}")

        with self.lock:
            safe_core_positions = list(self.core_positions)
            
        for core in safe_core_positions:
            # 💡 증권사에 묻지 않고, 웹소켓이 메모리에 밀어넣어둔 가격을 즉시 꺼내 씀!
            cp = self.live_prices.get(core.ticker)
            if not cp or cp <= 0: 
                continue
                
            with self.lock:
                core._last_price = cp
                core_shares = core.shares
                core_floor_shares = core.floor_shares
                core_cash = core.cash
                core_name = core.name
                core_ticker = core.ticker
            
                core_val = core_shares * cp
                self.add_log(f"💎 {core_name} 현황: {core_shares}주 (floor: {core_floor_shares}주) × {cp:,}원 = {core_val:,}원")

                try:
                    from strategy import get_rsi_signal
                    extended_df = self._get_extended_ohlcv(core_ticker, cp)
                    core_signal, _, core_rsi = get_rsi_signal(core_ticker, kis_api=self.kis, df=extended_df)

                    # 🟢 [최종보완] 코어 매수: 10분(600초) 쿨타임 적용으로 미체결 중복 주문 완벽 차단
                    if core_signal == 'BUY' and core_cash >= cp and (time.time() - getattr(core, 'last_order_time', 0) > 600):
                        qty = int((core_cash * 0.98) // cp)
                        if qty > 0:
                            if self.kis:
                                order_res = self.kis.buy_market_order(core_ticker, qty)
                                if order_res:
                                    with self.lock:
                                        core.last_order_time = time.time()  # 🟢 현재 시각 타임스탬프 탁본
                                        
                                    msg = f"💎 {core_name} 매수 주문 전송 완료 (10분 쿨타임 가동) | {qty}주 @ {cp:,}원 (RSI:{core_rsi:.1f})"
                                    self.add_log(msg)
                                    self._send_telegram(msg)

                    # 🟢 [최종보완] 코어 매도: 10분(600초) 쿨타임 적용
                    elif core_signal == 'SELL' and core_shares > core_floor_shares and (time.time() - getattr(core, 'last_order_time', 0) > 600):
                        sellable = core_shares - core_floor_shares
                        if sellable > 0:
                            if self.kis:
                                order_res = self.kis.sell_market_order(core_ticker, sellable)
                                if order_res:
                                    with self.lock:
                                        core.last_order_time = time.time()  # 🟢 현재 시각 타임스탬프 탁본
                                        
                                    # (대략적인 손익 기록용)
                                    profit = (cp - core.avg_price) * sellable
                                    msg = f"💎 {core_name} 익절 매도 주문 전송 완료 | {sellable}주 @ {cp:,}원 (RSI:{core_rsi:.1f}) | 예상 이익 {profit:,.0f}원"
                                    self.add_log(msg)
                                    self._send_telegram(msg)
                                    
                                    with self.lock:
                                        # 🚨 [신규 추가] 실현손익을 기록하여 자동 입금 감지 노이즈를 제거합니다.
                                        self.pnl_this_turn += profit
                                        today_str = now.strftime('%Y-%m-%d')
                                        self.daily_pnl[today_str] = self.daily_pnl.get(today_str, 0) + profit
                    else:
                        self.add_log(f"  [{core_name}] HOLD (RSI:{core_rsi:.1f}, floor:{core_floor_shares}주 보호)")
                except Exception as e:
                    self.add_log(f"  [{core_name}] 점검 중 오류: {str(e)}")


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

                # 💡 증권사에 묻지 않고, 웹소켓이 메모리에 밀어넣어둔 가격을 즉시 꺼내 씀!
                price = self.live_prices.get(ticker)
                if not price or price <= 0: continue
                    
                with self.lock: pos._last_price = price
                    
                from strategy import get_signal_by_strategy
                extended_df = self._get_extended_ohlcv(ticker, price)
                signal, price, ind_val = get_signal_by_strategy(ticker, strat_name, kis_api=self.kis, df=extended_df)
                if price <= 0: continue

                high_low = extended_df['high'] - extended_df['low']
                high_close = (extended_df['high'] - extended_df['close'].shift(1)).abs()
                low_close = (extended_df['low'] - extended_df['close'].shift(1)).abs()
                tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
                atr_14 = tr.rolling(window=14, min_periods=1).mean().iloc[-1] if not tr.empty else 0
                
                if atr_14 <= 0: atr_14 = pos_avg_price * 0.02

                today_str = datetime.now().strftime('%Y-%m-%d')
                cache_key = f"{ticker}_{today_str}"
                
                if not hasattr(self, 'fundamental_cache'): self.fundamental_cache = {}
                with self.lock: has_cache = cache_key in self.fundamental_cache
                    
                if has_cache:
                    with self.lock: financial_data = self.fundamental_cache[cache_key]
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
                            with self.lock: self.fundamental_cache[cache_key] = financial_data
                    except Exception:
                        pass

                macro_context = self.kis.get_macro_context() if self.kis else "시황 정보 없음"
                extended_strategy = f"{strat_name} | 실시간 재무상태: {financial_data} | 현재 거시 시황: {macro_context} | 최근 14일 ATR 변동폭: {atr_14:.1f}원"

                # 🟢 [최종보완] 위성 종목 10분 절대 쿨타임 변수 캐싱
                is_cooldown_passed = (time.time() - getattr(pos, 'last_order_time', 0) > 600)

                if pos_shares > 0 and price > 0 and is_cooldown_passed:
                    if price > pos_max_price:
                        with self.lock: pos.max_price = price
                        pos_max_price = price
                    
                    if pos_max_price >= pos_avg_price + (1.0 * atr_14):
                        dynamic_trailing_stop = pos_max_price - (1.5 * atr_14)
                        if price <= dynamic_trailing_stop:
                            reason = f"ATR 트레일링 스탑 (최고점 대비 1.5*ATR: {int(dynamic_trailing_stop):,}원 이탈)"
                            self.add_log(f"🎯 [{pos_name}] 변동성 추적 익절선 이탈! 수익 확정을 위해 전량 매도합니다.")
                            
                            if self.kis: 
                                order_res = self.kis.sell_market_order(ticker, pos_shares)
                                if order_res:
                                    with self.lock: 
                                        pos.last_order_time = time.time()  # 🟢 쿨타임 타임스탬프 기록
                                        pos.max_price = 0  
                                        
                                    profit = (price - pos_avg_price) * pos_shares
                                    
                                    log_trade_journal(self.user_id, ticker, pos_name, 'SELL', price, strat_name, reason, profit=profit)
                                    self._send_telegram(f"🎯 [{pos_name}] ATR 익절 전송 완료 (10분 쿨타임 가동)! 예상 손익: {profit:+,.0f}원")
                                    
                                    with self.lock:
                                        # 🚨 [신규 추가] 실현손익 기록 연동
                                        self.pnl_this_turn += profit
                                        today = datetime.now().strftime('%Y-%m-%d')
                                        self.daily_pnl[today] = self.daily_pnl.get(today, 0) + profit
                            continue

                if pos_shares > 0 and pos_avg_price > 0 and is_cooldown_passed:
                    dynamic_hard_stop = pos_avg_price - (2.5 * atr_14)
                    if price <= dynamic_hard_stop:
                        reason = f"ATR 변동성 하드 손절 (방어선 {int(dynamic_hard_stop):,}원 이탈)"
                        self.add_log(f"🚨 [{pos_name}] 변동성 위험 한계점 돌파! 추세 하락으로 판단 전량 시장가 매도.")
                        
                        if self.kis: 
                            order_res = self.kis.sell_market_order(ticker, pos_shares)
                            if order_res:
                                with self.lock: 
                                    pos.last_order_time = time.time()  # 🟢 쿨타임 타임스탬프 기록
                                
                                profit = (price - pos_avg_price) * pos_shares
                                
                                msg = f"💥 [{pos_name}] ATR 손절 전송 완료 (10분 쿨타임 가동) | 예상 손익: {profit:+,.0f}원"
                                self.add_log(msg)
                                log_trade_journal(self.user_id, ticker, pos_name, 'SELL', price, strat_name, reason, profit=profit)
                                self._send_telegram(msg)
                                
                                with self.lock:
                                    today = datetime.now().strftime('%Y-%m-%d')
                                    self.daily_pnl[today] = self.daily_pnl.get(today, 0) + profit
                        continue

                # ⚡ [매매 집행 파트]
                # 만약 AI 비서가 탑재되어 있다면 AI 승인 단계를 거치고, 없다면 즉각(Fast Track) 매매를 진행합니다.
                if signal == 'BUY' and pos_shares == 0 and is_cooldown_passed:
                    if not is_golden_hours: continue
                    
                    if self.gemini:
                        # 🤖 [AI 매수 승인] 알고리즘 매수 신호 발생 시 AI 최종 판단
                        self.add_log(f"🤔 [{pos_name}] 매수 신호 포착! AI의 판단을 요청합니다...")
                        
                        # DB에서 과거 매매 기록과 AI 자가 규칙 불러오기
                        recent_trades = get_recent_trades(self.user_id, ticker)
                        custom_rules = load_ai_rules(self.user_id)
                        
                        # 개발자님이 만드신 ai_approve_trade 호출!
                        decision, ai_reason = self.gemini.ai_approve_trade(
                            signal, pos_name, ticker, price, strat_name, ind_val, self.hot_sectors, recent_trades, custom_rules
                        )
                        
                        if decision:
                            reason = f"AI 승인 (사유: {ai_reason})"
                            qty = int((pos_cash * 0.98) // price)
                            
                            if qty > 0:
                                if self.kis: 
                                    order_res = self.kis.buy_market_order(ticker, qty)
                                    if order_res:
                                        with self.lock:
                                            pos.last_order_time = time.time()  # 🟢 쿨타임 타임스탬프 기록
                                        
                                        msg = f"📈 [{pos_name}] AI 승인 매수 완료 (10분 쿨타임)\n👉 {ai_reason}"
                                        self.add_log(msg)
                                        log_trade_journal(self.user_id, ticker, pos_name, 'BUY', price, strat_name, reason)
                                        self._send_telegram(msg)
                        else:
                            msg = f"🛑 [{pos_name}] AI 매수 거절 (REJECT)\n👉 {ai_reason}"
                            self.add_log(msg)
                            self._send_telegram(msg)
                    else:
                        # ⚡ [Fast Track] AI 비서가 없을 경우 알고리즘 즉각 매수
                        reason = "기술적 지표 조건 충족 (Fast Track 자동 매수)"
                        qty = int((pos_cash * 0.98) // price)
                        
                        if qty > 0:
                            if self.kis: 
                                order_res = self.kis.buy_market_order(ticker, qty)
                                if order_res:
                                    with self.lock:
                                        pos.last_order_time = time.time()  # 🟢 쿨타임 타임스탬프 기록
                                    
                                    msg = f"📈 [{pos_name}] 알고리즘 즉각 매수 전송 완료 (10분 쿨타임 가동)"
                                    self.add_log(msg)
                                    log_trade_journal(self.user_id, ticker, pos_name, 'BUY', price, strat_name, reason)
                                    self._send_telegram(msg)

                elif signal == 'SELL' and pos_shares > 0 and is_cooldown_passed:
                    if self.gemini:
                        # 🤖 [AI 매도 승인] 알고리즘 매도 신호 발생 시 AI 최종 판단
                        self.add_log(f"🤔 [{pos_name}] 매도 신호 포착! AI의 판단을 요청합니다...")
                        
                        recent_trades = get_recent_trades(self.user_id, ticker)
                        custom_rules = load_ai_rules(self.user_id)
                        
                        decision, ai_reason = self.gemini.ai_approve_trade(
                            signal, pos_name, ticker, price, strat_name, ind_val, self.hot_sectors, recent_trades, custom_rules
                        )
                        
                        if decision:
                            reason = f"AI 승인 (사유: {ai_reason})"
                            
                            if self.kis: 
                                order_res = self.kis.sell_market_order(ticker, pos_shares)
                                if order_res:
                                    with self.lock:
                                        pos.last_order_time = time.time()  # 🟢 쿨타임 타임스탬프 기록 
                                    
                                    profit = (price - pos_avg_price) * pos_shares 
                                    
                                    msg = f"📉 [{pos_name}] AI 승인 전량 매도 완료 | 예상 손익: {profit:+,.0f}원\n👉 {ai_reason}"
                                    self.add_log(msg)
                                    log_trade_journal(self.user_id, ticker, pos_name, 'SELL', price, strat_name, reason, profit=profit)
                                    self._send_telegram(msg)

                                    with self.lock:
                                        self.pnl_this_turn += profit
                                        today = datetime.now().strftime('%Y-%m-%d')
                                        self.daily_pnl[today] = self.daily_pnl.get(today, 0) + profit
                                    
                                    # 수익 재투자 로직 (실제 잔고 바탕)
                                    if profit > 0:
                                        with self.lock:
                                            if self.core_positions and pos.cash >= profit * REINVEST_RATIO:
                                                reinvest = profit * REINVEST_RATIO
                                                pos.cash -= reinvest
                                                split_amount = reinvest / len(self.core_positions)
                                                for core in self.core_positions: core.cash += split_amount
                                                msg_dist = f"🔄 위성 수익 중 {reinvest:,.0f}원 코어 매수자금 편입 완료"
                                                self.add_log(msg_dist)
                                                self._send_telegram(msg_dist)
                                                
                                                total_asset_now = float(self.cached_balance.get('total_cash', 0)) + float(self.cached_balance.get('total_value', 0)) if self.cached_balance else 0
                                                if total_asset_now > 0:
                                                    new_core_target = (total_asset_now * self.core_ratio) + reinvest
                                                    self.core_ratio = new_core_target / total_asset_now
                                                    self.satellite_ratio = 1.0 - self.core_ratio
                                                    self.add_log(f"📊 [복리 엔진 가동] 코어 포트폴리오 목표 비중이 {self.core_ratio*100:.2f}% 로 상향되었습니다.")
                        else:
                            msg = f"🛡️ [{pos_name}] AI 매도 보류 (HOLD)\n👉 {ai_reason}"
                            self.add_log(msg)
                            self._send_telegram(msg)
                    else:
                        # ⚡ [Fast Track] AI 비서가 없을 경우 알고리즘 즉각 매도
                        reason = "기술적 지표 조건 충족 (Fast Track 자동 매도)"
                        
                        if self.kis: 
                            order_res = self.kis.sell_market_order(ticker, pos_shares)
                            if order_res:
                                with self.lock:
                                    pos.last_order_time = time.time()  # 🟢 쿨타임 타임스탬프 기록 
                                
                                profit = (price - pos_avg_price) * pos_shares 
                                
                                msg = f"📉 [{pos_name}] 알고리즘 전량 매도 전송 완료 | 예상 손익: {profit:+,.0f}원"
                                self.add_log(msg)
                                log_trade_journal(self.user_id, ticker, pos_name, 'SELL', price, strat_name, reason, profit=profit)
                                self._send_telegram(msg)

                                with self.lock:
                                    self.pnl_this_turn += profit
                                    today = datetime.now().strftime('%Y-%m-%d')
                                    self.daily_pnl[today] = self.daily_pnl.get(today, 0) + profit
                                
                                # 수익 재투자 로직 (실제 잔고 바탕)
                                if profit > 0:
                                    with self.lock:
                                        if self.core_positions and pos.cash >= profit * REINVEST_RATIO:
                                            reinvest = profit * REINVEST_RATIO
                                            pos.cash -= reinvest
                                            split_amount = reinvest / len(self.core_positions)
                                            for core in self.core_positions: core.cash += split_amount
                                            msg_dist = f"🔄 위성 수익 중 {reinvest:,.0f}원 코어 매수자금 편입 완료"
                                            self.add_log(msg_dist)
                                            self._send_telegram(msg_dist)
                                            
                                            total_asset_now = float(self.cached_balance.get('total_cash', 0)) + float(self.cached_balance.get('total_value', 0)) if self.cached_balance else 0
                                            if total_asset_now > 0:
                                                new_core_target = (total_asset_now * self.core_ratio) + reinvest
                                                self.core_ratio = new_core_target / total_asset_now
                                                self.satellite_ratio = 1.0 - self.core_ratio
                                                self.add_log(f"📊 [복리 엔진 가동] 코어 포트폴리오 목표 비중이 {self.core_ratio*100:.2f}% 로 상향되었습니다.")

            except Exception as e:
                self.add_log(f"⚠️ [{ticker}] 오류: {e}")

        self._save_state()

    def _rescreen_satellites(self):
        try:
            now = datetime.now()
            
            # 🟢 [수정 포인트 1] 하루 한 번만 실행되도록 막아둔 족쇄를 파괴하고, 장중 언제라도 실행되도록 변경합니다.
            now_time_str = now.strftime('%H:%M')
            if not ("09:00" <= now_time_str <= "15:20") or now.weekday() >= 5:
                return # 장 시간이 아니면 스캔 종료
                
            self.add_log("🦅 [AI 실시간 종목 교체] 부진한 종목 퇴출 및 놀고 있는 빈자리에 즉각 주도주를 발굴하여 채웁니다...")
            keep_tickers = set()
            freed_cash = 0
            from pykrx import stock as krx_stock
            from datetime import timedelta
            
            with self.lock: sat_items = list(self.satellite_positions.items())
            
            for ticker, pos in sat_items:
                if pos.shares == 0:
                    freed_cash += pos.cash
                    with self.lock:
                        if ticker in self.satellite_positions: del self.satellite_positions[ticker]
                        if ticker in self.satellite_strategies: del self.satellite_strategies[ticker]
                    self.add_log(f"🔄 위성 교체 (미매수 대기 제거): {pos.name}")
                    continue
                    
                price = self.kis.get_current_price(ticker) if self.kis else 0
                is_uptrend = False
                try:
                    end_dt = now; start_dt = end_dt - timedelta(days=40)
                    df = krx_stock.get_market_ohlcv_by_date(start_dt.strftime("%Y%m%d"), end_dt.strftime("%Y%m%d"), ticker)
                    if not df.empty and len(df) >= 20:
                        c = df['종가']; ema5 = c.ewm(span=5, adjust=False).mean().iloc[-1]; ema20 = c.ewm(span=20, adjust=False).mean().iloc[-1]
                        is_uptrend = ema5 > ema20
                except: pass

                is_hot_sector = False
                for s_info in self.satellite_info:
                    if s_info['ticker'] == ticker and s_info.get('sector') in self.hot_sectors:
                        is_hot_sector = True; break

                if price and pos.avg_price > 0:
                    profit_rt = (price / pos.avg_price - 1) * 100
                    if is_uptrend or is_hot_sector:
                        keep_tickers.add(ticker)
                        reason = "추세 우상향" if is_uptrend else "주도 섹터"
                        self.add_log(f"🛡️ 위성 보존 ({reason}): {pos.name} ({profit_rt:+.2f}%)")
                    else:
                        if self.kis: self.kis.sell_market_order(ticker, pos.shares)
                        with self.lock: qty, profit = pos.sell(price)
                        if profit > 0 and self.core_positions:
                            reinvest = profit * REINVEST_RATIO
                            with self.lock:
                                if pos.cash >= reinvest:
                                    pos.cash -= reinvest
                                    split = reinvest / len(self.core_positions)
                                    for core in self.core_positions: core.cash += split
                                    self.add_log(f"🔄 위성 수익 {profit:,.0f}원 중 {reinvest:,.0f}원 코어 편입")
                                    
                                    # 🚨 [버그 수정] 스크리닝 교체 시에도 코어 비율 상향 업데이트 적용
                                    total_asset_now = float(self.cached_balance.get('total_cash', 0)) + float(self.cached_balance.get('total_value', 0)) if self.cached_balance else 0
                                    if total_asset_now > 0:
                                        new_core_target = (total_asset_now * self.core_ratio) + reinvest
                                        self.core_ratio = new_core_target / total_asset_now
                                        self.satellite_ratio = 1.0 - self.core_ratio
                        freed_cash += pos.cash
                        with self.lock:
                            if ticker in self.satellite_positions: del self.satellite_positions[ticker]
                            if ticker in self.satellite_strategies: del self.satellite_strategies[ticker]
                        self.add_log(f"🔄 위성 매도 및 교체: {pos.name} ({profit_rt:+.2f}%)")

            n_needed = self.num_satellites - len(keep_tickers)
            if n_needed <= 0:
                self.add_log("✅ 데일리 리밸런싱 완료: 전 종목 상승 추세 유지됨.")
                self.last_screen_date = now.date(); self._save_state()
                return

            is_bull_market = True
            try:
                if self.kis:
                    kospi_df = self.kis.get_ohlcv("0001", "D"); kosdaq_df = self.kis.get_ohlcv("2001", "D")
                    kospi_bull = kospi_df['close'].iloc[-1] >= kospi_df['close'].rolling(20).mean().iloc[-1] if kospi_df is not None and not kospi_df.empty else True
                    kosdaq_bull = kosdaq_df['close'].iloc[-1] >= kosdaq_df['close'].rolling(20).mean().iloc[-1] if kosdaq_df is not None and not kosdaq_df.empty else True
                    is_bull_market = kospi_bull and kosdaq_bull
            except Exception as e:
                self.add_log(f"⚠️ 시장 지수 판별 중 오류 발생: {e}")

            if not is_bull_market:
                self.core_ratio = 0.60; self.satellite_ratio = 0.40  
                self.add_log(f"📊 [동적 자산배분] 약세장 방어 모드 가동: 코어 {self.core_ratio*100}% / 위성 {self.satellite_ratio*100}% 변환")
            else:
                self.core_ratio = 0.30; self.satellite_ratio = 0.70
                self.add_log(f"📊 [동적 자산배분] 강세장 공격 모드 가동: 코어 {self.core_ratio*100}% / 위성 {self.satellite_ratio*100}% 변환")

            new_info = []
            if not is_bull_market:
                self.add_log("🚨 [하락장 감지] '4방패 + 1창' 인버스 헷지 체계로 전환합니다.")
                defensive_etfs = [
                    {'ticker': '261240', 'name': 'KODEX 미국달러선물', 'strategy_name': 'EMA 5/20', 'return_pct': 0.0},
                    {'ticker': '411060', 'name': 'ACE KRX금현물', 'strategy_name': 'EMA 5/20', 'return_pct': 0.0},
                    {'ticker': '114800', 'name': 'KODEX 인버스', 'strategy_name': 'EMA 5/20', 'return_pct': 0.0},
                    {'ticker': '251340', 'name': 'KODEX 코스닥150선물인버스', 'strategy_name': 'EMA 5/20', 'return_pct': 0.0},
                    {'ticker': '329750', 'name': 'TIGER 미국채10년선물', 'strategy_name': 'EMA 5/20', 'return_pct': 0.0} 
                ]
                for etf in defensive_etfs:
                    if etf['ticker'] not in keep_tickers and etf['ticker'] not in self.satellite_positions and len(new_info) < n_needed:
                        new_info.append(etf)
                remaining_slots = n_needed - len(new_info)
                if remaining_slots > 0:
                    raw_info, self.hot_sectors = select_satellites(kis=self.kis, n=remaining_slots * 3, verbose=False, gemini_client=self.gemini)
                    for c in raw_info:
                        if c['ticker'] not in keep_tickers and c['ticker'] not in [x['ticker'] for x in defensive_etfs]:
                            new_info.append(c)
                            if len(new_info) == n_needed: break
            else:
                raw_info, self.hot_sectors = select_satellites(kis=self.kis, n=self.num_satellites + n_needed, verbose=False, gemini_client=self.gemini)
                for c in raw_info:
                    if c['ticker'] not in keep_tickers:
                        new_info.append(c)
                        if len(new_info) == n_needed: break

            # 🟢 [신규 알파 로직] 리스크 패리티 비중 배분 적용
            total_available_cash = freed_cash
            for ticker in keep_tickers:
                total_available_cash += self.satellite_positions[ticker].cash
                self.satellite_positions[ticker].cash = 0
                
            target_tickers = [t for t in keep_tickers if self.satellite_positions[t].shares == 0]
            target_tickers.extend([c['ticker'] for c in new_info])
            
            if total_available_cash > 0 and target_tickers:
                inverse_vols = {}
                total_inv_vol = 0.0
                
                for t in target_tickers:
                    df = self._get_cached_base_ohlcv(t)
                    if not df.empty and len(df) > 14:
                        high_low = df['high'] - df['low']
                        high_close = (df['high'] - df['close'].shift(1)).abs()
                        low_close = (df['low'] - df['close'].shift(1)).abs()
                        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
                        atr = tr.rolling(14).mean().iloc[-1]
                        
                        if pd.isna(atr) or atr <= 0: atr = df['close'].iloc[-1] * 0.02
                        atr_pct = atr / df['close'].iloc[-1]
                        inv_vol = 1.0 / atr_pct
                    else:
                        inv_vol = 1.0 
                        
                    inverse_vols[t] = inv_vol
                    total_inv_vol += inv_vol

                added_lines = []
                with self.lock:
                    for t in keep_tickers:
                        if self.satellite_positions[t].shares == 0:
                            weight = inverse_vols[t] / total_inv_vol
                            allocated_budget = total_available_cash * weight
                            self.satellite_positions[t].cash = allocated_budget
                            self.add_log(f"⚖️ [리스크 패리티] {self.satellite_positions[t].name} 변동성 역비례 자금 배정: {allocated_budget:,.0f}원")

                    for c in new_info:
                        t = c['ticker']
                        weight = inverse_vols[t] / total_inv_vol
                        allocated_budget = total_available_cash * weight
                        self.satellite_positions[t] = Position(t, c['name'], allocated_budget)
                        self.satellite_strategies[t] = c['strategy_name']
                        self.add_log(f"✨ 새 위성 편입 (리스크 패리티 가중 {weight*100:.1f}%): {c['name']} → 예산 {allocated_budget:,.0f}원")
                        added_lines.append(f"  {c['name']} → [{c['strategy_name']}]")
                        
                keep_info = [c for c in self.satellite_info if c['ticker'] in keep_tickers]
                self.satellite_info = keep_info + new_info

            msg = f"📅 데일리 위성 리밸런싱 완료! (유지: {len(keep_tickers)} / 교체: {n_needed})\n" + "\n".join(added_lines)
            self._send_telegram(msg)
                
            self.last_screen_date = now.date()
            self._save_state()
            
        except Exception as e:
            self.add_log(f"🚨 위성 리밸런싱 중 오류 발생: {e}")

    def generate_daily_report(self):
        try:
            self.add_log("📝 11시 시장 분석 리포트 생성을 시작합니다...")
            
            # 🚨 [신규 추가] 현재 관리 중인 코어 및 위성 전 종목의 실시간 뉴스 헤드라인 병렬 스캔 추출
            news_lines = []
            with self.lock:
                target_stocks = [(c.name, c.ticker) for c in self.core_positions] + [(pos.name, t) for t, pos in self.satellite_positions.items()]
            
            # 중복 종목 제거
            target_stocks = list(dict.fromkeys(target_stocks))
            
            for name, ticker in target_stocks:
                news_headline = fetch_recent_news(name)
                news_lines.append(f"- {name}({ticker}): {news_headline}")
                time.sleep(0.1) # 네이버 디도스 차단 방어선 우회 미세 버퍼
            
            news_context = "\n".join(news_lines) if news_lines else "스캔된 주요 포트폴리오 뉴스 없음"
            
            # 취합된 뉴스 컨텍스트를 장중 보고서 양식으로 주입
            report_data = generate_daily_market_report(gemini_client=self.gemini, verbose=False, news_context=news_context)
            if report_data:
                self.daily_report = report_data
                self.add_log("✅ 일일 시장 분석 및 실시간 NLP 뉴스 통합 리포트 생성 완료")
                self._save_state()
                msg = "📝 [🎯 11시 장중 시장 분석 리포트 알림]\n\n"
                if isinstance(report_data, dict):
                    msg += report_data.get('summary', report_data.get('content', '11시 시장 분석이 완료되었습니다. 자세한 정보는 대시보드 팝업창을 확인하세요!'))
                else: msg += str(report_data)
                self._send_telegram(msg)
        except Exception as e:
            self.add_log(f"⚠️ 일일 리포트 생성 중 오류: {e}")

    def _weekly_self_reflection(self):
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

    def _run_threaded(self, job_func):
        threading.Thread(target=job_func, daemon=True).start()

    def _run_loop(self, total_cash):
        self.scheduler = schedule.Scheduler()
        restored = self._restore_state()
        if not restored: self.initialize_portfolio(total_cash)
        else: self.add_log("📊 기존 포트폴리오로 매매를 재개합니다.")

        self.scheduler.every(5).minutes.do(self.trading_job)
        self.scheduler.every().day.at("11:00").do(lambda: self._run_threaded(self.generate_daily_report))
        self.scheduler.every().day.at("09:05").do(lambda: self._run_threaded(self._rescreen_satellites))
        
        # 🟢 [수정 포인트 2] 매 1시간마다 수시로 포트폴리오를 스캔해서 성과가 꺾였거나 빈자리가 난 곳을 가차 없이 갈아치웁니다.
        self.scheduler.every(1).hours.do(lambda: self._run_threaded(self._rescreen_satellites))
        
        self.scheduler.every().friday.at("16:00").do(lambda: self._run_threaded(self._weekly_self_reflection))
        
        # 🟢 [개선 1 반영] 매일 오전 08:00 정각에 24시간 만료되는 웹소켓 접속키를 연장하고 소켓을 재부팅합니다.
        self.scheduler.every().day.at("08:00").do(lambda: self._run_threaded(self.refresh_websocket))
        
        # 🟢 [개선 2 반영] 매주 토요일 새벽 02:00 정각에 200대 주도주 기반 딥러닝 훈련을 자율 실행하고 모델을 교체합니다.
        self.scheduler.every().saturday.at("02:00").do(lambda: self._run_threaded(self.run_lstm_training))

        self.trading_job()  
        
        # 🟢 [족쇄 파괴 2] 오늘 이미 스캔을 했더라도, 봇을 켰을 때 현금이 놀고 있는 빈자리가 있다면 즉시 스캔해서 채워 넣습니다!
        needs_rescreen = len(self.satellite_positions) < self.num_satellites or any(p.shares == 0 for p in self.satellite_positions.values())
        if getattr(self, 'last_screen_date', None) != datetime.now().date() or needs_rescreen:
            now_time_str = datetime.now().strftime('%H:%M')
            if "09:00" <= now_time_str <= "15:30": self._rescreen_satellites()

        today = datetime.today().strftime('%Y-%m-%d')
        if not self.daily_report or self.daily_report.get('date') != today:
            now = datetime.now()
            if now.weekday() < 5 and now.strftime('%H:%M') >= "11:00":
                self.daily_report = None
                self.generate_daily_report()

        while self.is_running:
            self.scheduler.run_pending()
            time.sleep(1)
    
    def refresh_websocket(self):
        """매일 새벽 호출되어 24시간 만료되는 웹소켓 암호키(Approval Key)를 자동으로 재발급하고 연결을 연장합니다."""
        self.add_log("🔄 [웹소켓 키 연장] KIS 규정에 따른 24시간 만료 대비 실시간 웹소켓 재시작 루틴 가동...")
        try:
            if self.kis:
                # 1. 기존 가동 중인 웹소켓 클라이언트 채널 안전하게 파괴
                if self.ws_client and self.ws_client.ws:
                    try:
                        self.ws_client.ws.close()
                    except Exception:
                        pass
                
                # 2. 증권사로부터 새로운 24시간짜리 무적 접속 권한키 발급
                app_key = self.kis.get_approval_key()
                if app_key:
                    def on_price_update(ticker, price):
                        self.live_prices[ticker] = price
                    
                    # 기존에 실시간 감시하고 있던 종목 임시 백업
                    old_subscribed = list(self.ws_client.subscribed_tickers) if self.ws_client else []
                    
                    from kis_websocket import KisWebSocket
                    self.ws_client = KisWebSocket(app_key, is_mock=self._is_mock, price_callback=on_price_update)
                    self.ws_client.start()
                    
                    # 네트워크 안정화 딜레이 부여 후 백업된 종목 실시간 채널 재구독 등록
                    time.sleep(3.0)
                    for t in old_subscribed:
                        self.ws_client.subscribe(t)
                        
                    self.add_log("✅ [웹소켓 키 연장 완료] 새로운 암호키 갱신 및 기존 실시간 감시 채널 복구 전원 성공!")
                    self._send_telegram("📡 실시간 웹소켓 전용 접속 키(Approval Key) 24시간 만료 전 자동 연장 및 재구독 성공!")
                else:
                    self.add_log("❌ [웹소켓 키 연장 실패] 증권사 공용 방화벽 인증 실패 (키 발급 거절).")
        except Exception as e:
            self.add_log(f"❌ [웹소켓 키 연장 오류] 자동 재연결 제어 장치 장애: {e}")

    def run_lstm_training(self):
        """매주 토요일 새벽 2시, 메인 프로세스 간섭 없이 독립된 서브 백그라운드로 딥러닝 모델 훈련 실행"""
        self.add_log("🧠 [AI 자율 진화] 주말 자동화 스케줄러에 의해 시장 상위 200대 주도주 패턴 LSTM 재학습을 가동합니다.")
        self._send_telegram("🧠 주말 AI 자율 진화 모드 시작: 코스피/코스닥 거래대금 최상위 200대 주도주 차트 빅데이터 딥러닝 훈련에 돌입합니다.")
        
        try:
            import os
            import sys
            import subprocess
            
            # 현재 가동 중인 가상환경(venv) 내부의 진짜 파이썬 실행 파일 경로 추적
            python_executable = sys.executable
            base_dir = os.path.dirname(os.path.abspath(__file__))
            script_path = os.path.join(base_dir, "train_lstm.py")
            
            # 💡 [메모리 수호] 훈련 연산 중 램 부족으로 메인 봇이 같이 기절하는 것을 원천 차단코자 독립 서브프로세스로 분리 격리 가동
            result = subprocess.run([python_executable, script_path], capture_output=True, text=True)
            
            if result.returncode == 0:
                self.add_log("✅ [AI 자율 진화 완료] 이번 주 주도주 200개 기반 LSTM 가중치 모델 파일(.pth) 자동 갱신 완료!")
                self._send_telegram("🎉 [AI 자율 진화 완료] 이번 주 시장을 지배한 상위 200개 주식의 파동 패턴 완전 마스터 및 AI 매매 신경망 교체 성공!")
            else:
                self.add_log(f"❌ [AI 자율 진화 실패] train_lstm.py 학습 도중 오류가 검출되었습니다:\n{result.stderr}")
                self._send_telegram("⚠️ [AI 자율 진화 실패] 주말 딥러닝 자율 학습 도중 에러가 발견되었습니다. (로그 확인 필요)")
        except Exception as e:
            self.add_log(f"❌ [AI 자율 진화 오류] 서브 프로세스 통제 장치 장애: {e}")

    def start(self, total_cash=10_000_000):
        if not self.kis:
            self.add_log("❌ API 키가 설정되지 않았습니다.")
            return False

        if not self.is_running:
            self.is_running = True
            
            # 🚨 [원금 자율 역추적 시스템] 봇 구동 시점의 계좌 총자산을 새 원금 기준으로 삼도록 플래그 세팅
            self.initial_capital_captured = False
            
            self.thread = threading.Thread(target=self._run_loop, args=(total_cash,), daemon=True)
            self.thread.start()
            update_bot_status(self.user_id, True)
            
            mode_str = "모의투자" if self._is_mock else "실전투자"
            self.add_log(f"▶️ [{mode_str}] 매매 봇이 시작되었습니다.")
            self._send_telegram("▶️ 봇 감시를 시작합니다.")
            return True
        return False

    def stop(self):
        if self.is_running:
            self.is_running = False
            update_bot_status(self.user_id, False)
            if self.thread: self.thread.join(timeout=3)
            self.add_log("⏸️ 매매 봇이 일시 정지되었습니다.")
            self._send_telegram("⏸️ 봇 감시가 일시 정지되었습니다.")

    def get_pnl_data(self):
        sorted_days = sorted(self.daily_pnl.keys())
        return {"labels": sorted_days, "values": [round(self.daily_pnl[d]) for d in sorted_days]}

    def get_status(self):
        with self.lock:
            safe_core_positions = list(self.core_positions)
            safe_satellite_items = list(self.satellite_positions.items())

        cores_data = []
        for core in safe_core_positions:
            cp = getattr(core, '_last_price', 0) or 0
            cores_data.append({
                "name": core.name, "ticker": core.ticker, "shares": core.shares,
                "floor": core.floor_shares, "price": cp, "value": core.shares * cp,
                "budget": core.initial_cash, "strategy": "장기 우상향" if core.ticker != CORE_TICKER else "RSI + floor 보호"
            })

        satellites = []
        for ticker, pos in safe_satellite_items:
            sp = getattr(pos, '_last_price', 0) or 0
            satellites.append({
                "name": pos.name, "ticker": ticker, "strategy": self.satellite_strategies.get(ticker, '-'),
                "shares": pos.shares, "price": sp, "value": pos.shares * sp,
                "budget": getattr(pos, 'initial_cash', getattr(pos, 'budget', 0))
            })

        if self.cached_balance:
            api_cash = float(self.cached_balance.get('total_cash', 0))
            api_stock_val = float(self.cached_balance.get('total_value', 0))
            api_purchase = float(self.cached_balance.get('total_purchase', 0))
            mock_total_asset = api_cash + api_stock_val
            mock_pnl = api_stock_val - api_purchase
            mock_pnl_rt = (mock_pnl / api_purchase * 100) if api_purchase > 0 else 0
        else:
            mock_total_asset = 0; mock_pnl = 0; mock_pnl_rt = 0

        # 🚨 [UI 덮어쓰기 방지] DB에서 가장 최신화된 원금을 직접 꺼내서 웹 화면으로 쏴줍니다.
        from database import get_db_connection
        conn = get_db_connection()
        # 🟢 현재 웹 화면에 켜진 모드에 맞춰서 올바른 장부 데이터를 꺼내옵니다.
        cash_col = "mock_initial_cash" if self._is_mock else "real_initial_cash"
        row = conn.execute(f'SELECT {cash_col} FROM users WHERE id = ?', (self.user_id,)).fetchone()
        conn.close()
        current_initial_cash = float(row[cash_col]) if row and row[cash_col] is not None else 10000000

        return {
            "is_running": self.is_running, "is_mock": self._is_mock, "has_keys": self.kis is not None,
            "logs": self.logs[-30:], "hot_sectors": self.hot_sectors, "num_satellites": self.num_satellites,
            "cores": cores_data, "satellites": satellites, "mock_total_asset": mock_total_asset,
            "mock_pnl": mock_pnl, "mock_pnl_rt": mock_pnl_rt, "initial_cash": current_initial_cash
        }

    def refresh_websocket(self):
        """매일 새벽 호출되어 24시간 만료되는 웹소켓 암호키(Approval Key)를 자동으로 재발급하고 연결을 연장합니다."""
        self.add_log("🔄 [웹소켓 키 연장] KIS 규정에 따른 24시간 만료 대비 실시간 웹소켓 재시작 루틴 가동...")
        try:
            if self.kis:
                # 1. 기존 가동 중인 웹소켓 클라이언트 채널 안전하게 파괴
                if self.ws_client:
                    try:
                        self.ws_client.stop()  # 🚨 [버그 수정] ws.close() 대신 stop()을 호출하여 좀비 스레드 무한 증식을 완벽 차단
                    except Exception:
                        pass
                
                # 2. 증권사로부터 새로운 24시간짜리 무적 접속 권한키 발급
                app_key = self.kis.get_approval_key()
                if app_key:
                    def on_price_update(ticker, price):
                        self.live_prices[ticker] = price
                    
                    # 기존에 실시간 감시하고 있던 종목 임시 백업
                    old_subscribed = list(self.ws_client.subscribed_tickers) if self.ws_client else []
                    
                    from kis_websocket import KisWebSocket
                    self.ws_client = KisWebSocket(app_key, is_mock=self._is_mock, price_callback=on_price_update)
                    self.ws_client.start()
                    
                    # 네트워크 안정화 딜레이 부여 후 백업된 종목 실시간 채널 재구독 등록
                    time.sleep(3.0)
                    for t in old_subscribed:
                        self.ws_client.subscribe(t)
                        
                    self.add_log("✅ [웹소켓 키 연장 완료] 새로운 암호키 갱신 및 기존 실시간 감시 채널 복구 전원 성공!")
                    self._send_telegram("📡 실시간 웹소켓 전용 접속 키(Approval Key) 24시간 만료 전 자동 연장 및 재구독 성공!")
                else:
                    self.add_log("❌ [웹소켓 키 연장 실패] 증권사 공용 방화벽 인증 실패 (키 발급 거절).")
        except Exception as e:
            self.add_log(f"❌ [웹소켓 키 연장 오류] 자동 재연결 제어 장치 장애: {e}")

    def run_lstm_training(self):
        """매주 토요일 새벽 2시, 메인 프로세스 간섭 없이 독립된 서브 백그라운드로 딥러닝 모델 훈련 실행"""
        self.add_log("🧠 [AI 자율 진화] 주말 자동화 스케줄러에 의해 시장 상위 200대 주도주 패턴 LSTM 재학습을 가동합니다.")
        self._send_telegram("🧠 주말 AI 자율 진화 모드 시작: 코스피/코스닥 거래대금 최상위 200대 주도주 차트 빅데이터 딥러닝 훈련에 돌입합니다.")
        
        try:
            import os
            import sys
            import subprocess
            
            # 현재 가동 중인 가상환경(venv) 내부의 진짜 파이썬 실행 파일 경로 추적
            python_executable = sys.executable
            base_dir = os.path.dirname(os.path.abspath(__file__))
            script_path = os.path.join(base_dir, "train_lstm.py")
            
            # 💡 [메모리 수호] 훈련 연산 중 램 부족으로 메인 봇이 같이 기절하는 것을 원천 차단코자 독립 서브프로세스로 분리 격리 가동
            result = subprocess.run([python_executable, script_path], capture_output=True, text=True)
            
            if result.returncode == 0:
                self.add_log("✅ [AI 자율 진화 완료] 이번 주 주도주 200개 기반 LSTM 가중치 모델 파일(.pth) 자동 갱신 완료!")
                self._send_telegram("🎉 [AI 자율 진화 완료] 이번 주 시장을 지배한 상위 200개 주식의 파동 패턴 완전 마스터 및 AI 매매 신경망 교체 성공!")
            else:
                self.add_log(f"❌ [AI 자율 진화 실패] train_lstm.py 학습 도중 오류가 검출되었습니다:\n{result.stderr}")
                self._send_telegram("⚠️ [AI 자율 진화 실패] 주말 딥러닝 자율 학습 도중 에러가 발견되었습니다. (로그 확인 필요)")
        except Exception as e:
            self.add_log(f"❌ [AI 자율 진화 오류] 서브 프로세스 통제 장치 장애: {e}")

class BotManager:
    def __init__(self):
        self.last_assets = None
        self.bots = {}        
        self.ai_clients = {}  

    def get_bot(self, user_id, user_data=None):
        if not user_data: return self.bots.get((user_id, True))
            
        is_mock = bool(user_data.get('is_mock', 1))
        bot_key = (user_id, is_mock)
        
        if user_data.get('gemini_api_key'):
            api_key_clean = user_data.get('gemini_api_key').strip()
            if user_id not in self.ai_clients or getattr(self.ai_clients[user_id], '_current_key', '') != api_key_clean:
                from gemini_api import GeminiApi
                new_ai = GeminiApi(api_key=api_key_clean)
                new_ai._current_key = api_key_clean  
                self.ai_clients[user_id] = new_ai

        if bot_key not in self.bots:
            prefix = 'mock_' if is_mock else 'real_'
            kis_config = {
                "app_key": user_data.get(f'{prefix}app_key'), "app_secret": user_data.get(f'{prefix}app_secret'),
                "account_no": user_data.get(f'{prefix}account_no'), "is_mock": is_mock
            }
            tele_config = {"token": user_data.get('telegram_token'), "chat_id": user_data.get('telegram_chat_id')}
            self.bots[bot_key] = BotController(user_id, kis_config, tele_config, gemini_config=None, core_stocks=user_data.get('core_stocks'), is_mock=is_mock)
            
        if user_id in self.ai_clients: self.bots[bot_key].gemini = self.ai_clients[user_id]
            
        return self.bots.get(bot_key)

    def stop_all(self):
        for bot in self.bots.values(): bot.stop()

manager = BotManager()