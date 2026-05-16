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
from database import update_bot_status, save_portfolio_state, load_portfolio_state

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
            
        # 봇 정지 상태에서도 코어 종목 UI를 표시하기 위해 미리 로드
        self._init_dummy_cores()
        self._restore_state()
        
        self.add_log(f"User {user_id} Bot Controller 초기화 완료.")

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
        
        # 제미나이 AI 객체 갱신
        if gemini_config and gemini_config.get('api_key'):
            self.gemini = GeminiApi(api_key=gemini_config.get('api_key', '').strip())
        else:
            self.gemini = None
            
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

            # 복구된 리포트가 오늘 날짜가 아니면 버림 (재생성 유도)
            restored_report = state.get("daily_report", None)
            today_str = datetime.now().strftime('%Y-%m-%d')
            if restored_report and restored_report.get('date') == today_str:
                self.daily_report = restored_report
            else:
                self.daily_report = None  # 어제 리포트는 쓰지 않음
            
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
        current_time_str = now.strftime('%H:%M')
        
        # [수정] 한국 시장 통계상 수급과 거래량이 집중되어 승률이 가장 높은 '골든 타임'을 정의합니다.
        # - 장 초반 주도주 수급 타임 (09:01 ~ 11:00)
        # - 장 마감 직전 종가 형성 타임 (15:00 ~ 15:20)
        is_golden_hours = ("09:01" <= current_time_str <= "11:00") or ("15:00" <= current_time_str <= "15:20")
        
        # 골든 타임이 아닐 때는 매매 연산을 건너뛰어 가짜 신호로 인한 뇌동매매와 손실을 원천 차단합니다.
        if not is_golden_hours:
            # 5분마다 로그가 너무 많이 쌓이는 것을 방지하기 위해 매 정시와 30분에만 안내 로그를 출력합니다.
            if now.minute % 30 == 0:
                self.add_log(f"💤 현재 시간({current_time_str})은 횡보 가능성이 높은 구간입니다. 오신호 손절 방지를 위해 봇 매매 대기 중...")
            return

        # 골든 타임 진입 시 정상적으로 5분 주기 신호 탐색을 수행합니다.
        self.add_log(f"--- 🎯 골든 타임 매매 신호 점검 ({current_time_str}) ---")

        # [핵심 리팩토링] 매 사이클마다 한투증권(KIS)의 실제 예수금과 보유 주식 데이터를 가져와 시스템 장부를 강제 동기화합니다.
        # ... (이하 기존 코드 동일) ...
        # 이로 인해 수수료, 세금, 미체결 등으로 인한 장부 왜곡 및 예수금 부족으로 인한 주문 에러를 원천 차단합니다.
        if self.kis:
            try:
                real_balance = self.kis.get_account_balance()
                if real_balance and 'stocks' in real_balance:
                    # 1. [구조 개혁] 전체 자산 평가액(현금+주식) 기준 정밀 타겟 배분 방식으로 교체합니다.
                    real_cash = float(real_balance.get('total_cash', 0))
                    real_stock_value = float(real_balance.get('total_value', 0))
                    total_equity = real_cash + real_stock_value  # 계좌의 순수 총 자산 가치
                    
                    if total_equity > 0:
                        # CORE_RATIO(0.3)와 SATELLITE_RATIO(0.7)에 따른 이상적인 방별 목표 자산 정의
                        target_core_pool = total_equity * CORE_RATIO
                        target_sat_pool = total_equity * SATELLITE_RATIO
                        
                        # 1-A. 실제 증권사 데이터로부터 코어 주식들의 평가 금액 총합 계산
                        current_core_stock_val = 0
                        for real_stock in real_balance['stocks']:
                            t = real_stock['ticker']
                            for core in self.core_positions:
                                if core.ticker == t:
                                    current_core_stock_val += float(real_stock['value'])
                                    break
                                    
                        # 코어 방에 배정되어야 할 정확한 잔여 현금 계산 (목표 자산 - 현재 주식 가치)
                        # 코어 종목 수에 맞춰 균등 분배하되, 최소 0원 이하로 떨어지지 않도록 방어합니다.
                        per_core_cash = max(0.0, (target_core_pool - current_core_stock_val) / max(1, len(self.core_positions)))
                        for core in self.core_positions:
                            core.cash = round(per_core_cash, 2)
                            
                        # 1-B. 실제 증권사 데이터로부터 위성 주식들의 평가 금액 총합 계산
                        current_sat_stock_val = 0
                        for real_stock in real_balance['stocks']:
                            t = real_stock['ticker']
                            if t in self.satellite_positions:
                                current_sat_stock_val += float(real_stock['value'])
                                
                        # 위성 전체 방에 남아야 할 잔여 현금 계산
                        total_sat_cash = max(0.0, target_sat_pool - current_sat_stock_val)
                        
                        # 각 위성 슬롯별 가용 예산 분배 (기존에 주식을 안 들고 있는 슬롯에 현금 집중)
                        # 주식을 들고 있는 위성은 예산이 주식에 묶여 있으므로 가용 현금을 0에 가깝게 리셋하여 중복 매수를 막고,
                        # 주식이 없는 빈 슬롯은 다음 매수를 원활히 할 수 있도록 남은 현금을 정교하게 채워넣습니다.
                        empty_sat_count = sum(1 for sat in self.satellite_positions.values() if int(sat.shares) == 0)
                        
                        for t, sat in self.satellite_positions.items():
                            if int(sat.shares) > 0:
                                sat.cash = 0.0  # 이미 주식으로 들고 있으므로 가용 현금 비움
                            else:
                                # 주식이 없는 빈 슬롯들이 남은 위성 예수금을 나눠 갖도록 매칭
                                sat.cash = round(total_sat_cash / max(1, empty_sat_count), 2)

                    # 2. 실제 보유 주식 수(shares) 및 평균 매입단가(avg_price) 동기화
                    # 장부를 먼저 0으로 클리어한 후 실제 증권사 데이터만 매칭 주입합니다.
                    for core in self.core_positions:
                        core.shares = 0
                    for sat in self.satellite_positions.values():
                        sat.shares = 0

                    for real_stock in real_balance['stocks']:
                        t = real_stock['ticker']
                        q = int(real_stock['shares'])
                        p = float(real_stock['purchase_price'])

                        # 코어 종목 실제 수량 매칭
                        for core in self.core_positions:
                            if core.ticker == t:
                                core.shares = q
                                core.avg_price = p
                                if core.floor_shares == 0 and q > 0:
                                    core.floor_shares = max(1, int(q * CORE_MIN_FLOOR_RATIO))
                                break

                        # 위성 종목 실제 수량 매칭
                        if t in self.satellite_positions:
                            sat = self.satellite_positions[t]
                            sat.shares = q
                            sat.avg_price = p
                            
                    self.add_log("🔄 [잔고 동기화 완료] 실제 계좌의 실시간 자산 데이터가 가상 장부에 연동되었습니다.")
            except Exception as e:
                self.add_log(f"⚠️ [잔고 동기화 실패] 증권사 잔고 로드 실패 (안전을 위해 기존 데이터로 대치): {e}")

        # ── 코어 현재가 및 신호 점검 ──
        for core in self.core_positions:
            cp = self.kis.get_current_price(core.ticker) if self.kis else None
            if not cp: continue
            core._last_price = cp  # [추가] 대시보드 웹 화면에 실시간 현재가가 정상 출력되도록 바인딩합니다.
            
            core_val = core.shares * cp
            self.add_log(
                f"💎 {core.name} 현황: {core.shares}주 "
                f"(floor: {core.floor_shares}주) "
                f"× {cp:,}원 = {core_val:,}원"
            )

            # 코어 매매 로직 (RSI)
            try:
                core_signal, _, core_rsi = get_rsi_signal(core.ticker)

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
        for ticker, pos in self.satellite_positions.items():
            try:
                strat_name = self.satellite_strategies.get(ticker, 'RSI(9) 30/70')
                signal, price, ind_val = get_signal_by_strategy(ticker, strat_name)
                if price > 0:
                    pos._last_price = price  # [추가] 대시보드 웹 화면에 위성 종목 현재가가 정상 출력되도록 바인딩합니다.

                if signal == 'BUY' and pos.shares == 0:
                    # 1. AI에게 최종 승인 요청 (Gemini 활용)
                    if self.gemini:
                        is_approved, reason = self.gemini.ai_approve_trade(
                            signal='BUY', 
                            stock_name=pos.name, 
                            ticker=ticker, 
                            price=price, 
                            strategy=strat_name, 
                            indicator_val=ind_val, 
                            hot_sectors=self.hot_sectors
                        )
                        
                        # AI가 거절하면 매수하지 않고 로그를 남긴 후 다음 종목으로 넘어감
                        if not is_approved:
                            self.add_log(f"🚫 AI 매수 거부: [{pos.name}] - {reason}")
                            continue 

                    # 2. AI가 승인했을 때만 실제 주문 실행
                    if self.kis:
                        qty = int(pos.cash // price)
                        if qty > 0:
                            self.kis.buy_market_order(ticker, qty)
                    
                    qty = pos.buy(price)
                    if qty > 0:
                        msg = f"📈 [{pos.name}] 매수 {qty}주 @ {price:,}원 [{strat_name} → {ind_val:.1f}]"
                        self.add_log(msg)
                        if self.telegram:
                            self.telegram.send_message(msg)

                elif signal == 'SELL' and pos.shares > 0:
                    if self.kis:
                        self.kis.sell_market_order(ticker, pos.shares)
                    qty, profit = pos.sell(price)
                    msg = (f"📉 [{pos.name}] 매도 {qty}주 @ {price:,}원 "
                           f"| 손익: {profit:+,.0f}원 [{strat_name} → {ind_val:.1f}]")
                    self.add_log(msg)
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
                    self.add_log(f"  [{pos.name}] HOLD [{strat_name} → {ind_val:.1f}]")


            except Exception as e:
                self.add_log(f"⚠️ [{ticker}] 오류: {e}")

        # 매 사이클 후 상태 저장 (재시작 시 복구용)
        self._save_state()

    def _rescreen_satellites(self):
        """위성 종목 데일리 리밸런싱 (B+C 혼합형)"""
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

        # 2. 빈 자리 개수만큼 새 종목 스크리닝
        raw_info, self.hot_sectors = select_satellites(kis=self.kis, n=self.num_satellites + n_needed, verbose=False, gemini_client=self.gemini)
        new_info = []
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

    def generate_daily_report(self):
        """매일 아침 시장 분석 리포트를 생성하고 상태에 저장합니다."""
        try:
            self.add_log("📝 일일 시장 분석 리포트 생성을 시작합니다...")
            report_data = generate_daily_market_report(gemini_client=self.gemini, verbose=False)
            if report_data:
                self.daily_report = report_data
                self.add_log("✅ 일일 시장 분석 리포트 생성 완료")
                self._save_state()
        except Exception as e:
            self.add_log(f"⚠️ 일일 리포트 생성 중 오류: {e}")

    # ─── 봇 시작/정지 ───
    def _run_loop(self, total_cash):
        schedule.clear()

        # 서버 재시작 시 기존 상태 복구 시도
        restored = self._restore_state()
        if not restored:
            # 복구 실패 시에만 새로 초기화 (스크리닝 실행)
            self.initialize_portfolio(total_cash)
        else:
            # 복구 성공 시 schedule만 재등록
            self.add_log("📊 기존 포트폴리오로 매매를 재개합니다.")

        # 장중 매매: 5분마다
        schedule.every(5).minutes.do(self.trading_job)
        # 일일 시장 분석 리포트 (08:00)
        schedule.every().day.at("08:00").do(self.generate_daily_report)
        # 데일리 위성 리밸런싱 (08:50) - 오타 수정됨 (monthly_rescreen -> _rescreen_satellites)
        schedule.every().day.at("08:50").do(self._rescreen_satellites)

        self.trading_job()  # 즉시 1회 실행
        
        # 오늘 날짜의 위성 스크리닝이 안 되어 있으면 즉시 실행
        if getattr(self, 'last_screen_date', None) != datetime.now().date():
            self.add_log("오늘 날짜의 위성 리밸런싱 기록이 없어 즉시 실행합니다...")
            self._rescreen_satellites()

        # 오늘 날짜의 리포트가 없거나 어제 날짜면 즉시 재생성
        today = datetime.today().strftime('%Y-%m-%d')
        if not self.daily_report or self.daily_report.get('date') != today:
            self.add_log("과거 리포트 감지. 오늘 날짜로 리포트를 재생성합니다...")
            self.daily_report = None
            self.generate_daily_report()

        while self.is_running:
            schedule.run_pending()
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
            return True
        return False

    def stop(self):
        if self.is_running:
            self.is_running = False
            update_bot_status(self.user_id, False)
            if self.thread:
                self.thread.join(timeout=3)
            self.add_log("봇이 정지되었습니다.")

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
        for core in self.core_positions:
            cp = getattr(core, '_last_price', 0) or 0
            core_value = core.shares * cp
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
        for ticker, pos in self.satellite_positions.items():
            sp = getattr(pos, '_last_price', 0) or 0
            sat_value = pos.shares * sp
            satellites.append({
                "name":     pos.name,
                "ticker":   ticker,
                "strategy": self.satellite_strategies.get(ticker, '-'),
                "shares":   pos.shares,
                "price":    sp,
                "value":    sat_value,
                "budget":   getattr(pos, 'initial_cash', getattr(pos, 'budget', 0))
            })

        return {
            "is_running":    self.is_running,
            "is_mock":       self._is_mock,
            "has_keys":      self.kis is not None,
            "logs":          self.logs[-30:],
            "hot_sectors":   self.hot_sectors,
            "num_satellites": self.num_satellites,
            "cores":         cores_data,
            "satellites":    satellites,
        }

class BotManager:
    """모든 사용자의 봇 인스턴스를 관리합니다."""
    def __init__(self):
        self.bots = {}        # { (user_id, is_mock): BotController } -> 쌍둥이 봇 바디 관리
        self.ai_clients = {}  # { user_id: GeminiApi }               -> 공유형 단일 AI 엔진 관리

    def get_bot(self, user_id, user_data=None):
        if not user_data:
            return self.bots.get((user_id, True))
            
        is_mock = bool(user_data.get('is_mock', 1))
        bot_key = (user_id, is_mock)
        
        # 1. [AI 엔진 싱글톤화] 해당 유저의 공유형 AI 엔진이 메모리에 없다면 최초 1회만 생성
        if user_id not in self.ai_clients and user_data.get('gemini_api_key'):
            from gemini_api import GeminiApi
            self.ai_clients[user_id] = GeminiApi(api_key=user_data.get('gemini_api_key').strip())
            print(f"🤖 [AI 공유 엔진] User {user_id}를 위한 단일 AI 엔진이 생성되었습니다. (모든 모드에서 공유)")

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
            
            # BotController 인스턴스(바디) 생성 (Gemini 키는 일단 제외하고 생성)
            self.bots[bot_key] = BotController(
                user_id, kis_config, tele_config, gemini_config=None,
                core_stocks=user_data.get('core_stocks'),
                is_mock=is_mock
            )
            
            # 3. [핵심] 생성된 봇 바디에 중앙에서 관리하는 '공유형 AI 엔진'을 똑같이 주입합니다.
            self.bots[bot_key].gemini = self.ai_clients.get(user_id)
            
        return self.bots.get(bot_key)

    def stop_all(self):
        for bot in self.bots.values():
            bot.stop()
# 글로벌 매니저 인스턴스
manager = BotManager()