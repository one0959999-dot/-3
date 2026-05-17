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
            
        # 봇 정지 상태에서도 코어 종목 UI를 표시하기 위해 미리 로드
        # 봇 정지 상태에서도 코어 종목 UI를 표시하기 위해 미리 로드
        self._init_dummy_cores()
        self._restore_state()
        
        # 🔒 [스레드 안전성] 딕셔너리 동시 접근으로 인한 런타임 에러 방지용 락
        self.lock = threading.Lock()
        
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
                        if cp: core._last_price = cp
                        time.sleep(0.05) # 💡 트래픽 폭주 방지 딜레이
                            
                    # 🔒 [스레드 보호 및 트래픽 방지] 락을 사용하여 에러를 막고, API 한도를 넘지 않게 대기시간 상향
                    with self.lock:
                        sat_items = list(self.satellite_positions.items())

                    for ticker, pos in sat_items:
                        sp = self.kis.get_current_price(ticker)
                        if sp: pos._last_price = sp
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
                # 어플 잔고가 0원이면 봇의 예산도 0원으로 정확하게 동기화됩니다.
                if total_equity >= 0:
                    target_core_pool = total_equity * CORE_RATIO
                    target_sat_pool = total_equity * SATELLITE_RATIO
                    
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
                self.add_log(f"⚠️ 내부 장부 동기화 중 오류 발생: {e}")

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
                self.add_log(f"초기 잔고 동기화 실패: {e}")

    # ─── 로그 ───
    def add_log(self, msg):
        t = datetime.now().strftime("%H:%M:%S")
        entry = {"time": t, "message": msg}
        self.logs.append(entry)
        print(f"[{t}] {msg}")
        if len(self.logs) > 100:
            self.logs.pop(0)

    def reload_api_keys(self, kis_config, telegram_config, gemini_config, core_stocks):
        """사용자가 웹에서 API 키를 수정했을 때, 실행 중인 봇 객체들에 실시간으로 새 키를 반영합니다."""
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
        
        # [수정] 제미나이 AI 객체는 단일 두뇌(공유형) 체제를 엄격히 유지해야 하므로,
        # 개별 봇 몸체에서 인스턴스를 독단적으로 새로 생성하여 메모리 주소를 파괴하지 않도록 코드를 제거합니다.
        # 최신 두뇌의 실시간 주입 및 키 동기화는 이제 상단의 BotManager.get_bot에서 완벽하게 통합 전담 처리합니다.
        pass
            
        self._init_dummy_cores()
        self.add_log("🔑 변경된 API 키 및 계좌 설정이 시스템에 실시간 반영되었습니다.")

    def update_mode(self, is_mock, total_cash=10000000):
        """봇을 멈추지 않고 실전/모의 모드를 즉시 전환하며, 해당 모드의 독립 장부를 새로 로드합니다."""
       
        self._is_mock = is_mock
        if self.kis:
            # KIS API 객체의 모드와 URL 정보를 먼저 변경합니다.
            self.kis.set_mode(is_mock) 
            
        mode_name = "모의투자" if is_mock else "실전투자"
        self.add_log(f"🔄 모드 실시간 전환: {mode_name} 자산 장부 데이터로 전면 교체합니다.")
        
        # [핵심 추가] 봇이 가동 중(Running) 상태라면 바뀐 모드의 장부 데이터를 DB에서 불러옵니다.
        if self.is_running:
            restored = self._restore_state()
            if not restored:
                self.add_log(f"ℹ️ {mode_name}의 기존 저장 상태가 없어 새로 포트폴리오를 구성합니다.")
                self.initialize_portfolio(total_cash)
        else:
            # 봇이 정지(Stopped) 상태일 때도 대시보드 화면에 올바른 코어 종목 금액이 나오도록 동기화합니다.
            self._init_dummy_cores()
            self._restore_state()

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
        if self.telegram:
            self.telegram.send_message("🔍 위성 종목 & 전략 선정!\n" + "\n".join(log_lines))

        # 2. 자금 배분
        core_budget = total_cash * CORE_RATIO
        sat_budget  = total_cash * SATELLITE_RATIO
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
            # [수정본 코드] 객체들을 숫자나 문자열로 확실히 변환하여 저장합니다.
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
                # [핵심 추가] 파이썬 date 객체를 문자열로 안전하게 변환하여 저장합니다.
                # 이를 통해 서버가 불시에 재시작되더라도 오늘 이미 실행한 위성 리밸런싱이 중복 작동하는 것을 완벽히 방지합니다.
                "last_screen_date": self.last_screen_date.strftime('%Y-%m-%d') if getattr(self, 'last_screen_date', None) else None,
                "daily_pnl": self.daily_pnl,
                "daily_report": self.daily_report,
            }
            # 데이터베이스 장부 분리를 위해 인자값에 self._is_mock을 정확히 추가해 줍니다.
            save_portfolio_state(self.user_id, state, self._is_mock)
        except Exception as e:
            self.add_log(f"⚠️ 상태 저장 실패: {e}")

    def _restore_state(self):
        """DB에서 포트폴리오 상태 복구. 성공하면 True 반환."""
        try:
            # 실전/모의 모드에 맞는 독자 장부를 불러오도록 self._is_mock 인자를 추가합니다.
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

            # [핵심 수정] 장 휴무일(주말)에는 직전 거래일(금요일 등)의 리포트를 대시보드에서 볼 수 있도록 복구 정책을 바꿉니다.
            restored_report = state.get("daily_report", None)
            today_str = datetime.now().strftime('%Y-%m-%d')
            
            if restored_report:
                # 1) 오늘 날짜 리포트이거나, 2) 오늘이 장 휴무일인 주말(토=5, 일=6)이라면 파기하지 않고 그대로 유지합니다.
                if restored_report.get('date') == today_str or datetime.now().weekday() >= 5:
                    self.daily_report = restored_report
                    if datetime.now().weekday() >= 5:
                        self.add_log(f"📋 장 휴무일(주말)이므로 직전 거래일 분석 리포트({restored_report.get('date')})를 화면에 유지합니다.")
                else:
                    # 평일인데 오늘 자 리포트가 아니면 오전 11시에 최신 분석을 받아오기 위해 비워둡니다.
                    self.daily_report = None  
            else:
                self.daily_report = None
            
            self.add_log(f"✅ 복구 완료: 코어 {len(self.core_positions)}개, 위성 {len(self.satellite_positions)}개")
            return True
        except Exception as e:
            self.add_log(f"⚠️ 상태 복구 실패 (새로 초기화): {e}")
            return False

    # ─── 트레이딩 잡 ───
    def trading_job(self):
        if not self.core_positions:
            self.add_log("⚠️ 포트폴리오가 초기화되지 않았습니다.")
            return

        now = datetime.now()
        
        # 🟢 [버그 해결] 토요일(5) 또는 일요일(6) 등 주말일 때는 주식 장이 열리지 않으므로 즉시 연산을 종료합니다.
        # 이로 인해 주말에 서버를 몇 번을 껐다 켜도 유령 매수 신호나 텔레그램 오발송이 완벽하게 차단됩니다.
        if now.weekday() >= 5:
            if now.minute % 30 == 0:
                self.add_log(f"💤 오늘은 주말 휴무일({now.strftime('%A')})입니다. 가짜 신호 방지를 위해 매매 감시를 중단하고 휴식합니다.")
            return

        current_time_str = now.strftime('%H:%M')
        
       # [수정] 한국 시장 통계상 수급과 거래량이 집중되어 승률이 가장 높은 '골든 타임'을 정의합니다.
        # - 장 초반 주도주 수급 타임 (09:01 ~ 11:00)
        # - 장 마감 직전 종가 형성 타임 (15:00 ~ 15:20)
        is_golden_hours = ("09:01" <= current_time_str <= "11:00") or ("15:00" <= current_time_str <= "15:20")
        
        # 골든 타임이 아닐 때는 신규 매수(BUY)만 제한하고, 보유 종목에 대한 매도(SELL) 및 손절 관리는 계속 감시합니다.
        if not is_golden_hours:
            if now.minute % 30 == 0:
                self.add_log(f"🕒 현재 시간({current_time_str})은 횡보 구간입니다. 신규 매수(BUY)는 중지하되 보유 종목 리스크 관리(SELL)는 유지합니다.")
            # return으로 종료하지 않고 매도 감시를 위해 아래 루프를 계속 진행시킵니다.
        else:
            self.add_log(f"--- 🎯 골든 타임 매수/매도 전면 점검 ({current_time_str}) ---")

        # [핵심 리팩토링] 하드코딩된 중복 잔고 동기화 로직 제거 및 통합 함수 호출
        # 이미 완벽하게 구현된 _sync_internal_balances 함수를 재사용하여 트래픽 낭비를 줄입니다.
        if self.kis:
            try:
                real_balance = self.kis.get_account_balance()
                if real_balance and 'stocks' in real_balance:
                    self._sync_internal_balances(real_balance)
                    self.add_log("🔄 [잔고 동기화 완료] 실제 계좌의 실시간 자산 데이터가 가상 장부에 연동되었습니다.")
            except Exception as e:
                self.add_log(f"⚠️ [잔고 동기화 실패] 증권사 잔고 로드 실패 (안전을 위해 기존 데이터로 대치): {e}")

        # ── 코어 현재가 및 신호 점검 ──
        # 🔒 안전한 반복과 주문을 위해 락을 걸어 데이터를 보호합니다.
        with self.lock:
            safe_core_positions = list(self.core_positions)
            
        for core in safe_core_positions:
            cp = self.kis.get_current_price(core.ticker) if self.kis else None
            if not cp: continue
            core._last_price = cp  # [추가] 대시보드 웹 화면에 실시간 현재가가 정상 출력되도록 바인딩합니다.
            
            core_val = core.shares * cp
            self.add_log(
                f"💎 {core.name} 현황: {core.shares}주 "
                f"(floor: {core.floor_shares}주) "
                f"× {cp:,}원 = {core_val:,}원"
            )

            # 코어 매매 로직 (RSI) - KIS API 객체 전달
            try:
                core_signal, _, core_rsi = get_rsi_signal(core.ticker, kis_api=self.kis)

                if core_signal == 'BUY' and core.cash >= (cp or 1):
                    qty = core.buy(cp)
                    if qty > 0:
                        if self.kis:
                            self.kis.buy_market_order(core.ticker, qty)
                        msg = f"💎 {core.name} 매수 {qty}주 @ {cp:,}원 (RSI:{core_rsi:.1f}) → 총 {core.shares}주"
                        self.add_log(msg)
                        if self.telegram:
                            self.telegram.send_message(msg)

                elif core_signal == 'SELL' and core.shares > core.floor_shares:
                    if self.kis:
                        sellable = core.shares - core.floor_shares
                        self.kis.sell_market_order(core.ticker, sellable)
                    qty, profit = core.sell(cp)
                    if qty > 0:
                        msg = f"💎 {core.name} 익절 매도 {qty}주 @ {cp:,}원 (RSI:{core_rsi:.1f}) | 이익 {profit:,.0f}원"
                        self.add_log(msg)
                        self.daily_pnl[now.strftime('%Y-%m-%d')] = self.daily_pnl.get(now.strftime('%Y-%m-%d'), 0) + profit
                        if self.telegram:
                            self.telegram.send_message(msg)
                else:
                    self.add_log(f"  [{core.name}] HOLD (RSI:{core_rsi:.1f}, floor:{core.floor_shares}주 보호)")
            except Exception as e:
                self.add_log(f"  [{core.name}] 점검 중 오류: {str(e)}")


       # ── 위성 신호 점검 (종목별 최적 전략 적용) ──
        # 🔒 안전한 반복을 위해 락을 걸고 리스트를 복사합니다.
        with self.lock:
            trading_sat_items = list(self.satellite_positions.items())

        for ticker, pos in trading_sat_items:
            try:
                strat_name = self.satellite_strategies.get(ticker, 'RSI(9) 30/70')
                # 💡 pykrx 대신 한투 KIS API 객체를 전달하여 안전하게 과거 시세를 조회합니다.
                signal, price, ind_val = get_signal_by_strategy(ticker, strat_name, kis_api=self.kis)
                if price > 0:
                    pos._last_price = price  # 대시보드 웹 화면에 위성 종목 현재가가 정상 출력되도록 바인딩합니다.
                # 🟢 [신규 추가 코드] 거시경제 매크로 데이터 확보 및 전략 문장 결합
                macro_context = self.kis.get_macro_context() if self.kis else "시황 정보 없음"
                extended_strategy = f"{strat_name} | 실시간 재무상태: {financial_data} | 현재 거시 시황: {macro_context}"

                # 🔴 [필수 추가] 하드 손절선(Hard Stop-Loss) 로직 (-5% 하락 시 기계적 매도)
                # 💡 [AI 뇌 확장 & IP 차단 방지] pykrx 펀더멘털 조회는 하루 1회 캐싱된 데이터만 사용합니다.
                today_str = datetime.now().strftime('%Y-%m-%d')
                cache_key = f"{ticker}_{today_str}"
                
                # 안전하게 캐시 딕셔너리 초기화 확인
                if not hasattr(self, 'fundamental_cache'):
                    self.fundamental_cache = {}
                    
                if cache_key in self.fundamental_cache:
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
                            self.fundamental_cache[cache_key] = financial_data # 하루 1번만 가져오고 캐시 저장
                    except Exception:
                        pass

                    # 🟢 [신규 추가 코드] 트레일링 스탑 (고점 대비 -3% 추적 청산)
                if pos.shares > 0 and price > 0:
                    # 매수 이후 최고가 갱신
                    if price > pos.max_price:
                        pos.max_price = price
                    
                    # 평균 매수가 대비 5% 이상 수익권에 진입한 적이 있는 종목이
                    # 당기 최고점 대비 3% 이상 주가가 흘러내렸을 때 보수적 익절 단행
                    if pos.max_price >= pos.avg_price * 1.05:
                        if price <= pos.max_price * 0.97:
                            reason = "트레일링 스탑 (최고점 대비 -3% 이탈)"
                            self.add_log(f"🎯 [{pos.name}] 트레일링 스탑 발동! 수익 보전을 위해 전량 매도합니다.")
                            if self.kis:
                                self.kis.sell_market_order(ticker, pos.shares)
                            qty, profit = pos.sell(price)
                            log_trade_journal(self.user_id, ticker, pos.name, 'SELL', price, strat_name, reason, profit=profit)
                            if self.telegram:
                                self.telegram.send_message(f"🎯 [{pos.name}] 트레일링 스탑 익절 완료! 손익: {profit:+,.0f}원")
                            pos.max_price = 0  # 초기화
                            continue

                # 🔴 [필수 추가] 하드 손절선(Hard Stop-Loss) 로직 (-5% 하락 시 기계적 매도)
                if pos.shares > 0 and pos.avg_price > 0:
                    current_profit_rt = (price / pos.avg_price) - 1
                    if current_profit_rt <= -0.05:
                        reason = "기계적 손절 (-5% 도달)"
                        self.add_log(f"🚨 [{pos.name}] 하드 손절선(-5%) 이탈 감지! 보호를 위해 즉시 시장가 매도합니다.")
                        if self.kis:
                            self.kis.sell_market_order(ticker, pos.shares)
                        qty, profit = pos.sell(price)
                        msg = f"💥 [{pos.name}] 기계적 손절 완료: {qty}주 @ {price:,}원 | 손익: {profit:+,.0f}원"
                        self.add_log(msg)
                        log_trade_journal(self.user_id, ticker, pos.name, 'SELL', price, strat_name, reason, profit=profit)
                        if self.telegram:
                            self.telegram.send_message(msg)
                        today = datetime.now().strftime('%Y-%m-%d')
                        self.daily_pnl[today] = self.daily_pnl.get(today, 0) + profit
                        continue # 매도 후 아래의 일반 신호(BUY/SELL) 체크는 건너뜀

                if signal == 'BUY' and pos.shares == 0:
                    if not is_golden_hours:
                        continue # 골든타임이 아닐 때는 매수(BUY) 금지

                    reason = "조건 충족 자동 매수"
                    # 1. AI에게 매수 권한 위임 (차트 보조지표 + 재무 데이터 융합)
                    if self.gemini:
                        recent_logs = get_recent_trades(self.user_id, ticker, limit=5)
                        custom_rules = load_ai_rules(self.user_id)
                        
                        is_approved, reason = self.gemini.ai_approve_trade(
                            signal='BUY', 
                            stock_name=pos.name, 
                            ticker=ticker, 
                            price=price, 
                            # 🔄 [수정 변경]기존 strategy 대신 매크로 시황이 포함된 extended_strategy를 주입
                            strategy=extended_strategy, 
                            indicator_val=ind_val, 
                            hot_sectors=self.hot_sectors,
                            recent_trades=recent_logs,
                            custom_rules=custom_rules
                        )
                        
                        if not is_approved:
                            self.add_log(f"🚫 AI 매수 거절 (재무/차트/학습 융합 판단): [{pos.name}] - {reason}")
                            continue 

                    # 2. 승인 시 주문 실행
                    if self.kis:
                        qty = int(pos.cash // price)
                        if qty > 0:
                            self.kis.buy_market_order(ticker, qty)
                    
                    qty = pos.buy(price)
                    if qty > 0:
                        msg = f"📈 [{pos.name}] AI 매수 승인: {qty}주 @ {price:,}원 [{strat_name} → {ind_val:.1f}]"
                        self.add_log(msg)
                        log_trade_journal(self.user_id, ticker, pos.name, 'BUY', price, strat_name, reason)
                        if self.telegram:
                            self.telegram.send_message(msg)

                elif signal == 'SELL' and pos.shares > 0:
                    reason = "조건 충족 자동 매도"
                    # 1. AI에게 매도(SELL) 권한까지 완전히 이양하여 조기 청산을 막고 수익 극대화를 유도
                    if self.gemini:
                        recent_logs = get_recent_trades(self.user_id, ticker, limit=5)
                        custom_rules = load_ai_rules(self.user_id)
                        
                        is_approved, reason = self.gemini.ai_approve_trade(
                            signal='SELL', 
                            stock_name=pos.name, 
                            ticker=ticker, 
                            price=price, 
                            strategy=f"{strat_name} | 실시간 재무상태: {financial_data}", 
                            indicator_val=ind_val, 
                            hot_sectors=self.hot_sectors,
                            recent_trades=recent_logs,
                            custom_rules=custom_rules
                        )
                        
                        if not is_approved:
                            self.add_log(f"✋ AI 매도 보류 (수익 극대화 홀딩 판단): [{pos.name}] - {reason}")
                            continue

                    # 2. AI가 하락 위험을 감지하여 털고 나가라고 승인했을 때만 매도 실행
                    if self.kis:
                        self.kis.sell_market_order(ticker, pos.shares)
                    qty, profit = pos.sell(price)
                    msg = (f"📉 [{pos.name}] AI 매도 승인: {qty}주 @ {price:,}원 "
                           f"| 손익: {profit:+,.0f}원 [{strat_name} → {ind_val:.1f}]")
                    self.add_log(msg)
                    log_trade_journal(self.user_id, ticker, pos.name, 'SELL', price, strat_name, reason, profit=profit)
                    if self.telegram:
                        self.telegram.send_message(msg)

                    # 일별 수익 기록
                    today = datetime.now().strftime('%Y-%m-%d')
                    self.daily_pnl[today] = self.daily_pnl.get(today, 0) + profit

                    # 수익 발생 시 코어 재투자 (멀티 코어 균등 분배)
                    if profit > 0 and self.core_positions:
                        reinvest = profit * REINVEST_RATIO
                        if pos.cash >= reinvest:
                            pos.cash -= reinvest
                            split_amount = reinvest / len(self.core_positions)
                            
                            for core in self.core_positions:
                                core.cash += split_amount
                                
                            msg_dist = f"🔄 위성 수익 {profit:,.0f}원 중 {reinvest:,.0f}원을 코어({len(self.core_positions)}개) 매수자금으로 균등 편입"
                            self.add_log(msg_dist)
                            if self.telegram:
                                self.telegram.send_message(msg_dist)
                else:
                    # 매수/매도 조건이 아닐 때 HOLD 로그 생략
                    pass

            except Exception as e:
                self.add_log(f"⚠️ [{ticker}] 오류: {e}")

        # 매 사이클 후 상태 저장 (재시작 시 복구용)
        self._save_state()

    def _rescreen_satellites(self):
        """위성 종목 데일리 리밸런싱 (B+C 혼합형)"""
        try:
            now = datetime.now()
            # 동일 날짜에 중복 실행 방지
            if getattr(self, 'last_screen_date', None) == now.date():
                return
                
            self.add_log("📅 데일리 위성 리밸런싱 (추세 및 모멘텀 기반) 실행...")
            keep_tickers = set()
            freed_cash = 0
            
            from pykrx import stock as krx_stock
            from datetime import timedelta
            
            # 1. 기존 종목 점검 (추세 우상향 또는 주도 섹터면 유지, 아니면 매도)
            for ticker, pos in list(self.satellite_positions.items()):
                if pos.shares == 0:
                    # 미매수 상태면 교체
                    freed_cash += pos.cash
                    del self.satellite_positions[ticker]
                    if ticker in self.satellite_strategies:
                        del self.satellite_strategies[ticker]
                    self.add_log(f"🔄 위성 교체 (미매수 대기 제거): {pos.name}")
                    continue
                    
                price = self.kis.get_current_price(ticker) if self.kis else 0
                
                # 추세 및 모멘텀 확인
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
                        # 추세 꺾임 & 모멘텀 소진 시 매도
                        if self.kis:
                            self.kis.sell_market_order(ticker, pos.shares)
                        qty, profit = pos.sell(price)
                        
                        # 매도 후 수익 발생 시 50% 코어 재투자
                        if profit > 0 and self.core_positions:
                            reinvest = profit * REINVEST_RATIO
                            if pos.cash >= reinvest:
                                pos.cash -= reinvest
                                split = reinvest / len(self.core_positions)
                                for core in self.core_positions:
                                    core.cash += split
                                self.add_log(f"🔄 위성 수익 {profit:,.0f}원 중 {reinvest:,.0f}원 코어 편입")
                                
                        freed_cash += pos.cash
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

            # 📉 [신규 퀀트 로직] 시장 국면(상승/하락) 판별 및 하이브리드 포트폴리오 스위칭 (3방패 2창)
            is_bull_market = True
            try:
                # 안정적인 KIS API의 get_ohlcv 함수를 우회 활용 (업종코드 코스닥: '2001')
                if self.kis:
                    index_df = self.kis.get_ohlcv("2001", "D")
                    if not index_df.empty and len(index_df) >= 20:
                        index_close = index_df['close']
                        index_ma20 = index_close.rolling(20).mean().iloc[-1]
                        current_index = index_close.iloc[-1]
                        
                        if current_index < index_ma20:
                            is_bull_market = False
            except Exception as e:
                self.add_log(f"⚠️ 시장 지수 판별 중 오류 발생(기본 상승장 간주): {e}")

            # 🟢 [신규 추가 코드] 거시 시장 국면에 따른 동적 자산 배분(Dynamic Asset Allocation) 비율 조정
            global CORE_RATIO, SATELLITE_RATIO
            if not is_bull_market:
                CORE_RATIO = 0.60       # 하락장에서는 코어(보수적 자산) 비중 증대
                SATELLITE_RATIO = 0.40  # 위성 트레이딩 자금 축소 (현금 확보 및 헷지)
                self.add_log(f"📊 [동적 자산배분] 약세장 방어 모드 가동: 코어 {CORE_RATIO*100}% / 위성 {SATELLITE_RATIO*100}% 변환")
            else:
                CORE_RATIO = 0.30       # 상승장 기본 비율 유지
                SATELLITE_RATIO = 0.70
                self.add_log(f"📊 [동적 자산배분] 강세장 공격 모드 가동: 코어 {CORE_RATIO*100}% / 위성 {SATELLITE_RATIO*100}% 변환")

            new_info = []
            if not is_bull_market:
                self.add_log("🚨 [하락장 감지] 코스닥 지수가 20일선 밑으로 깨졌습니다. '3방패(헷지) + 2창(알파)' 전략으로 포지션을 전환합니다.")
                
                # 방패 역할을 할 ETF 리스트 (달러, 금, 곱버스)
                # 하락장 방어에 탁월한 EMA 5/20 교차 전략을 부여하여 단기 모멘텀으로만 진입/청산합니다.
                defensive_etfs = [
                    {'ticker': '261240', 'name': 'KODEX 미국달러선물', 'strategy_name': 'EMA 5/20', 'return_pct': 0.0},
                    {'ticker': '411060', 'name': 'ACE KRX금현물', 'strategy_name': 'EMA 5/20', 'return_pct': 0.0},
                    {'ticker': '251340', 'name': 'KODEX 코스닥150선물인버스', 'strategy_name': 'EMA 5/20', 'return_pct': 0.0}
                ]
                
                # 1. 빈 자리에 방어 ETF 3개를 우선적으로 할당
                for etf in defensive_etfs:
                    if etf['ticker'] not in keep_tickers and etf['ticker'] not in self.satellite_positions and len(new_info) < n_needed:
                        new_info.append(etf)
                        
                # 2. 남은 빈 자리(창)는 지수를 이기는 강력한 개별 주식으로 채움
                remaining_slots = n_needed - len(new_info)
                if remaining_slots > 0:
                    self.add_log(f"⚔️ 하락장을 역행하는 알파 헌팅을 위해 {remaining_slots}개 슬롯을 개별 주식으로 탐색합니다.")
                    # 겹치는 종목을 건너뛰기 위해 충분한 수( * 3 )를 스크리닝
                    raw_info, self.hot_sectors = select_satellites(kis=self.kis, n=remaining_slots * 3, verbose=False, gemini_client=self.gemini)
                    for c in raw_info:
                        if c['ticker'] not in keep_tickers and c['ticker'] not in [x['ticker'] for x in defensive_etfs]:
                            new_info.append(c)
                            if len(new_info) == n_needed:
                                break
            else:
                self.add_log("📈 [상승장 확인] 20일선 위에서 순항 중입니다. 주도주/모멘텀 기반 개별 주식 위성 스크리닝을 진행합니다.")
                raw_info, self.hot_sectors = select_satellites(kis=self.kis, n=self.num_satellites + n_needed, verbose=False, gemini_client=self.gemini)
                for c in raw_info:
                    if c['ticker'] not in keep_tickers:
                        new_info.append(c)
                        if len(new_info) == n_needed:
                            break

            # 3. 새 종목 편입 및 예산 할당
            per_budget = freed_cash / n_needed if n_needed > 0 else 0
            added_lines = []
            for c in new_info:
                self.satellite_positions[c['ticker']] = Position(c['ticker'], c['name'], per_budget)
                self.satellite_strategies[c['ticker']] = c['strategy_name']
                self.add_log(f"✨ 새 위성 편입: {c['name']} → [{c['strategy_name']}] {c['return_pct']:+.1f}%")
                added_lines.append(f"  {c['name']} → [{c['strategy_name']}]")
                
            # satellite_info 업데이트
            keep_info = [c for c in self.satellite_info if c['ticker'] in keep_tickers]
            self.satellite_info = keep_info + new_info

            msg = f"📅 데일리 위성 리밸런싱 완료! (유지: {len(keep_tickers)} / 교체: {n_needed})\n" + "\n".join(added_lines)
            if self.telegram:
                self.telegram.send_message(msg)
                
            self.last_screen_date = now.date()
            self._save_state()
            
        except Exception as e:
            # 💡 [핵심 수정] 외부 API 오류 발생 시 스레드가 죽지 않도록 예외 처리
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
                
                # [추가] 리포트가 정상 생성되면 설정된 텔레그램 채널로 요약본을 즉시 발송합니다.
                if self.telegram:
                    msg = "📝 [🎯 11시 장중 시장 분석 리포트 알림]\n\n"
                    if isinstance(report_data, dict):
                        # 리포트 딕셔너리 내부에서 요약(summary) 또는 본문(content)을 안전하게 추출합니다.
                        msg += report_data.get('summary', report_data.get('content', '11시 시장 분석이 완료되었습니다. 자세한 정보는 대시보드 팝업창을 확인하세요!'))
                    else:
                        msg += str(report_data)
                    self.telegram.send_message(msg)
        except Exception as e:
            self.add_log(f"⚠️ 일일 리포트 생성 중 오류: {e}")

    # 🟢 [여기에 새로 추가] 주간 자아성찰 함수 🟢
    def _weekly_self_reflection(self):
        """[AI 자가 학습] 일주일간의 매매 기록을 바탕으로 오답 노트를 작성하고 룰을 업데이트합니다."""
        self.add_log("🧠 [AI 자아성찰] 한 주간의 매매 결과를 분석하여 새로운 투자 원칙을 수립합니다...")
        
        from database import get_db_connection
        conn = get_db_connection()
        rows = conn.execute('''
            SELECT date(created_at) as date, stock_name, action, price, ai_reason, profit 
            FROM trade_journal WHERE user_id = ? ORDER BY created_at DESC LIMIT 30
        ''', (self.user_id,)).fetchall()
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
                if self.telegram:
                    self.telegram.send_message(f"🧠 [라씨 AI 자가 학습 완료]\n\n이번 주 오답노트를 바탕으로 새로운 매매 원칙을 세웠습니다:\n\n{new_rules}")

    # ─── 봇 시작/정지 ───
    def _run_threaded(self, job_func):
        """무거운 스케줄러 작업을 메인 매매 루프와 분리하여 백그라운드 스레드에서 실행합니다."""
        job_thread = threading.Thread(target=job_func, daemon=True)
        job_thread.start()

    def _run_loop(self, total_cash):
        import schedule
        self.scheduler = schedule.Scheduler()

        restored = self._restore_state()
        if not restored:
            self.initialize_portfolio(total_cash)
        else:
            self.add_log("📊 기존 포트폴리오로 매매를 재개합니다.")

        # 5분 매매 감시는 가볍고 즉각적이어야 하므로 그대로 실행하되, 나머지 무거운 작업들은 _run_threaded를 통해 비동기로 실행
        self.scheduler.every(5).minutes.do(self.trading_job)
        self.scheduler.every().day.at("11:00").do(lambda: self._run_threaded(self.generate_daily_report))
        self.scheduler.every().day.at("09:05").do(lambda: self._run_threaded(self._rescreen_satellites))
        
        # 매주 금요일 장 마감 후 성찰 스케줄 비동기 처리
        self.scheduler.every().friday.at("16:00").do(lambda: self._run_threaded(self._weekly_self_reflection))

        self.trading_job()  # 즉시 1회 실행
        
        if getattr(self, 'last_screen_date', None) != datetime.now().date():
            now_time_str = datetime.now().strftime('%H:%M')
            if "09:00" <= now_time_str <= "15:30":
                self.add_log("오늘 날짜의 위성 리밸런싱 기록이 없어 즉시 실행합니다...")
                self._rescreen_satellites()
            else:
                self.add_log("오늘 날짜의 위성 리밸런싱 기록이 없으나 정규장 시간이 아니므로 정규 스케줄러(09:05)까지 대기합니다.")

        # 오늘 날짜의 리포트가 없더라도, 현재 시간이 11시 이전이라면 미리 생성하지 않고 11시 정각 스케줄러를 기다립니다.
        today = datetime.today().strftime('%Y-%m-%d')
        if not self.daily_report or self.daily_report.get('date') != today:
            now = datetime.now()
            
            # 평일이고 오전 11시가 이미 지났는데 오늘 자 리포트가 누락된 경우에만 즉시 생성합니다.
            if now.weekday() < 5 and now.strftime('%H:%M') >= "11:00":
                self.add_log("오전 11시가 지났으나 오늘 자 시장 분석 리포트가 없어 즉시 재생성합니다...")
                self.daily_report = None
                self.generate_daily_report()
            elif now.weekday() >= 5:
                # 주말일 때는 과거 리포트 주소가 대시보드 메모리에 잘 안착해 있다면 소중히 유지합니다.
                if self.daily_report:
                    self.add_log(f"📋 주말 장 휴무 모드: 이전 거래일 리포트({self.daily_report.get('date')})를 대시보드 화면에 노출합니다.")
                else:
                    self.add_log("오늘 자 시장 분석 리포트가 없고 시스템에 저장된 이전 과거 리포트도 존재하지 않습니다.")
            else:
                self.add_log("오늘 자 시장 분석 리포트가 아직 없으나, 평일 11시 이전이므로 정규 분석 스케줄을 대기합니다.")

        while self.is_running:
            # 💡 전역 큐가 아닌 새로 만든 내 봇의 독립 스케줄러 작업만 실행하도록 변경합니다.
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
            
            # 🟢 [추가] 봇 시작 시 텔레그램으로 즉각 보고
            mode_str = "모의투자" if self._is_mock else "실전투자"
            self.add_log(f"▶️ [{mode_str}] 매매 봇이 시작되었습니다.")
            if self.telegram:
                self.telegram.send_message(f"▶️ [{mode_str}] 봇 감시를 시작합니다.\n- 현재 모드에 맞춰 종목 감시 및 자동 매매가 활성화되었습니다.")
                
            return True
        return False

    def stop(self):
        if self.is_running:
            self.is_running = False
            update_bot_status(self.user_id, False)
            if self.thread:
                self.thread.join(timeout=3)
                
            # 🟢 [추가] 봇 정지 시 텔레그램으로 즉각 보고
            self.add_log("⏸️ 매매 봇이 일시 정지되었습니다.")
            if self.telegram:
                self.telegram.send_message("⏸️ 봇 감시가 일시 정지되었습니다.\n- 대기 상태로 전환되어 매수/매도가 중단됩니다.")

    # ─── 대시보드 상태 반환 ───
    def get_pnl_data(self):
        """일별 수익률 데이터 반환 (Chart.js용)"""
        sorted_days = sorted(self.daily_pnl.keys())
        return {
            "labels": sorted_days,
            "values": [round(self.daily_pnl[d]) for d in sorted_days],
        }

    def get_status(self):
        cores_data = []
        total_core_stock_val = 0
        total_core_cash_val = 0
        for core in self.core_positions:
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
        for ticker, pos in self.satellite_positions.items():
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

       # 💡 [버그 해결] 가짜 계산식을 모두 버리고, 무조건 한투 API 서버에서 받아온 진짜 내역만 화면에 직결시킵니다.
        if self.cached_balance:
            api_cash = float(self.cached_balance.get('total_cash', 0))
            api_stock_val = float(self.cached_balance.get('total_value', 0))
            api_purchase = float(self.cached_balance.get('total_purchase', 0))
            
            # 총 자산 = 앱 기준 예수금 + 현재 주식 평가금 총액
            mock_total_asset = api_cash + api_stock_val
            
            # 수익금 = 현재 주식 평가금액 - 주식 매입 원금 (실시간 주식 손익)
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
            # 계산된 진짜 한투 자산을 프론트엔드로 전송
            "mock_total_asset": mock_total_asset,
            "mock_pnl": mock_pnl,
            "mock_pnl_rt": mock_pnl_rt
        }

class BotManager:
    """모든 사용자의 봇 인스턴스를 관리합니다."""
    def __init__(self):
        # 🟢 초기값을 None으로 지정하여 '서버가 방금 막 켜진 최초 상태'임을 명시합니다.
        self.last_assets = None
        self.bots = {}        # 🚨 [버그 해결] 봇 인스턴스들을 관리할 핵심 주머니 딕셔너리를 누락 없이 생성합니다.
        self.ai_clients = {}  # { user_id: GeminiApi }               -> 공유형 단일 AI 엔진 관리

    def get_bot(self, user_id, user_data=None):
        if not user_data:
            return self.bots.get((user_id, True))
            
        is_mock = bool(user_data.get('is_mock', 1))
        bot_key = (user_id, is_mock)
        
        # 1. [버그 수정] 단일 AI 엔진 싱글톤 및 실시간 키 갱신 구조화
        # 사용자가 키를 처음 등록하거나 변경했을 때, 기존 인스턴스가 주머니에 있더라도 최신 키 정보로 확실하게 자동 리로드합니다.
        if user_data.get('gemini_api_key'):
            api_key_clean = user_data.get('gemini_api_key').strip()
            
            # AI 객체가 아예 없거나, 기존 객체가 들고 있는 키값과 새로 전달받은 키값이 다를 때만 인스턴스를 동적으로 교체합니다.
            if user_id not in self.ai_clients or getattr(self.ai_clients[user_id], '_current_key', '') != api_key_clean:
                from gemini_api import GeminiApi
                # pyrefly: ignore [unexpected-keyword]
                new_ai = GeminiApi(api_key=api_key_clean)
                new_ai._current_key = api_key_clean  # 키 변경 추적용 임시 바인딩 속성
                self.ai_clients[user_id] = new_ai
                print(f"🤖 [AI 공유 엔진 활성화/갱신] User {user_id}의 최신 API 키로 공유형 AI 두뇌 세팅 완료.")

        # 2. 쌍둥이 봇 바디(실전 또는 모의)가 주머니에 없다면 생성
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
            
        # 3. [핵심] 최초 생성 시점뿐만 아니라,get_bot이 실행될 때마다(매 API 트래픽마다) 
        # 항상 중앙 매니저의 최신 AI 두뇌 인스턴스 주소를 봇 바디에 강제로 실시간 주입 및 리바인딩합니다.
        if user_id in self.ai_clients:
            self.bots[bot_key].gemini = self.ai_clients[user_id]
            
        return self.bots.get(bot_key)

    def stop_all(self):
        for bot in self.bots.values():
            bot.stop()
# 글로벌 매니저 인스턴스
manager = BotManager()