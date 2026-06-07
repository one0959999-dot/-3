import threading
import time
import schedule
import json
import logging
import os
import collections
import pandas as pd
import requests
from bs4 import BeautifulSoup
import urllib.parse
from datetime import datetime, timezone, timedelta

logger = logging.getLogger('lassi_bot')

# EC2(UTC) 환경에서도 한국 장 시간을 정확히 계산하기 위해 KST(UTC+9) 고정
_KST = timezone(timedelta(hours=9))

def _now_kst():
    """현재 시각을 한국 표준시(KST)로 반환합니다 (EC2 UTC 환경 대응)."""
    return datetime.now(_KST).replace(tzinfo=None)

from base.telegram_bot import TelegramNotifier
from KR.strategy import CorePosition, Position, get_rsi_signal, get_composite_signal, REINVEST_RATIO, get_market_regime, get_market_regime_detail, get_bear_bounce_signal, get_bear_bottom_score, get_bull_momentum_score, get_neutral_range_score, INVERSE_ETF_TICKER, INVERSE_ETF_NAME, INVERSE_BUDGET_RATIO, DEFENSIVE_ASSETS, check_giveback_stop, check_early_drop_stop, check_theme_overextension_exit, check_rsi_progressive_exit, calculate_entry_score, get_entry_threshold, get_budget_ratio_from_score, calc_rsi, calculate_core_entry_score, get_core_entry_threshold
from KR.screener import select_satellites, generate_daily_market_report
from base.database import update_bot_status, save_portfolio_state, load_portfolio_state, log_trade_journal, get_recent_trades, save_ai_rules, load_ai_rules, get_ai_rules_history, get_user_initial_cash, set_user_initial_cash, add_user_initial_cash, get_news_api_keys, get_sector_guide
from ai.news_monitor import NewsMonitor
from KR.kis.real_api import KisRealApi
from KR.kis.real_websocket import KisRealWebSocket

_SELL_FEE = 0.00015   # 매도 수수료율 (0.015%)
_SELL_TAX = 0.0018    # 증권거래세율 (0.18%)

# ── KR 코어 ROE 턴어라운드 보너스 ────────────────────────────────────
_roe_kr_cache: dict = {}  # {ticker: (ts, score, reason)}

def _roe_turnaround_kr(ticker: str) -> tuple:
    """pykrx EPS/BPS로 분기별 ROE 개선 추세 계산 → 코어 진입 보너스. 1시간 캐시."""
    cached = _roe_kr_cache.get(ticker)
    if cached and time.time() - cached[0] < 3600:
        return cached[1], cached[2]
    score, reason = 0, ""
    try:
        from pykrx import stock as pykrx_stock
        from datetime import datetime, timedelta
        today = datetime.now()
        roe_vals = []
        for i in range(1, 5):  # 최근 4분기 근사 (90일 간격)
            d = (today - timedelta(days=90 * i)).strftime("%Y%m%d")
            try:
                for mkt in ("KOSPI", "KOSDAQ"):
                    df_f = pykrx_stock.get_market_fundamental_by_ticker(d, d, mkt)
                    if ticker in df_f.index:
                        eps = float(df_f.loc[ticker, 'EPS'])
                        bps = float(df_f.loc[ticker, 'BPS'])
                        if bps != 0:
                            roe_vals.append(eps / abs(bps))
                        break
            except Exception:
                pass
        roe_vals.reverse()  # 과거 → 최근 순
        if len(roe_vals) < 3:
            return 0, ""
        # 최신 ROE 음수여야 턴어라운드 후보
        if roe_vals[-1] >= 0:
            return 0, ""
        n = len(roe_vals)
        improving = sum(1 for i in range(1, n) if roe_vals[i] > roe_vals[i-1])
        if improving == n - 1:
            if roe_vals[-1] > -0.02:
                score, reason = 2, f"ROE 흑자전환 임박({roe_vals[-1]*100:.1f}%→0%) +2"
            else:
                score, reason = 1, f"ROE 개선 추세({roe_vals[0]*100:.1f}%→{roe_vals[-1]*100:.1f}%) +1"
        elif improving >= n // 2:
            score, reason = 1, f"ROE 부분개선 +1"
    except Exception:
        pass
    _roe_kr_cache[ticker] = (time.time(), score, reason)
    return score, reason

def _net_profit(sell_price: float, avg_price: float, shares: int) -> float:
    """수수료·세금 반영 실현 손익 계산."""
    net_revenue = sell_price * shares * (1 - _SELL_FEE - _SELL_TAX)
    cost_basis  = avg_price * shares
    return net_revenue - cost_basis


def fetch_recent_news(stock_name):
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


class KRBotController:
    """KR 실전 매매 봇 — KIS 국내주식 API"""
    def __init__(self, user_id, kis_config=None, telegram_config=None, core_stocks=None, satellite_stocks=None):
        self.user_id = user_id
        self.is_running = False
        self.thread = None
        self.logs = collections.deque(maxlen=100)   # 스레드 안전 + O(1) 순환 버퍼
        self.num_satellites = 3  # 위성 3개 고정
        self._is_mock = False     # KR 봇은 항상 실전
        self.mode_name = "KR"
        self.alert_icon = "🔴"

        self.core_ratio = 0.40        # 코어 40% — 중기 누적 매수
        self.satellite_ratio = 0.60   # 위성 60% — 중기 성장주
        self.core_min_floor_ratio = 0.5
        self.market_indices = [("069500", "KOSPI"), ("229200", "KOSDAQ")]

        try:
            self.user_core_stocks = json.loads(core_stocks) if core_stocks else []
        except Exception:
            self.user_core_stocks = []

        try:
            self.user_satellite_stocks = json.loads(satellite_stocks) if satellite_stocks else []
        except Exception:
            self.user_satellite_stocks = []

        # 사용자 지정 코어 종목 (첫 번째만 사용) — DB에서 동적으로 읽음
        _u = self.user_core_stocks[0] if self.user_core_stocks else None
        self.core_ticker = _u['ticker'] if _u else ""
        self.core_name   = _u['name']   if _u else ""

        self.core_positions = []
        self.satellite_positions = {}
        self.satellite_info = []
        self.daily_pnl = {}
        self.last_screen_month = None
        self.last_screen_date = None
        self.last_core_rebalance_date = None   # AI 코어 마지막 재선정 날짜 (매주 월요일 갱신)
        self.hot_sectors = []
        # ── 주말 사전 분석 플랜 ────────────────────────────────────────
        # 주말 스캔에서 판단한 월요일 교체 계획
        # {ticker_to_sell: {"new_ticker": str, "new_name": str, "score": float, "reason": str}}
        self._monday_swap_plan: dict = {}
        self._weekend_scan_done: str = ""  # 마지막 주말 스캔 날짜 (중복 방지)
        self.daily_report = None
        self.volume_surge_details = []   # 거래량 급증 종목 실제 리스트 [{ticker, name, ratio}]
        self._last_total_equity = 0.0    # 최근 총자산 스냅샷 (방어자산 예산 계산용)

        # 예수금 즉시 반영용 내부 현금 추적기
        # KIS 모의 API는 체결 후 1~3분 지연이 있어 캐시 API 값 대신 내부 추적값 사용
        self.internal_cash = None          # 최초 KIS API 값으로 초기화 후 매수/매도마다 즉각 갱신
        self._last_trade_ts = 0.0          # 마지막 체결 타임스탬프 (KIS API 재동기화 시점 판단)
        self._dca_prev_cash      = 0.0     # 전 턴 예수금 스냅샷 (입금 감지용)
        self._dca_deposit_trigger= False   # 이번 턴 입금 감지 플래그
        self._dca_deposit_amount = 0.0     # 감지된 입금액
        self.fundamental_cache = {}

        # ── 당일 블랙리스트 (날짜가 바뀌면 자동 초기화) ──────────────────
        # satellite_rejects    : 오늘 AI 거절된 위성 종목 {ticker: reason}
        self._bl_date               = ""       # 마지막 초기화 날짜 (YYYY-MM-DD)
        self._satellite_rejects     : dict = {}  # {ticker: float(ts)} — 5분 쿨다운
        self._satellite_reject_rsn  : dict = {}  # {ticker: str} — 거절 사유
        self._SAT_REJECT_COOLDOWN   = 300        # 위성 AI 거절 쿨다운 5분

        # ── 유휴 위성 예산 → 코어 임시 배분 추적 ──────────────────────────
        # 위성 슬롯 공석 시 freed_cash를 코어 매수대기 포지션에 임시 배분.
        # 다음 위성 선정 성공 시 해당 코어가 아직 매수 전이면 회수해서 위성에 배정.
        # _sat_cash_lent 제거 — 동적 균등 예산으로 대체됨

        # ── 종목당 당일 누적 손실 추적 (하루 최대 손실 캡) ──────────────
        # {ticker: cumulative_loss_krw}  — 손실(-) 누계, 날짜 바뀌면 초기화
        # 클래스 상수 _MAX_DAILY_LOSS_PER_TICKER 는 하단 클래스 본문에 정의됨
        self._daily_loss_by_ticker  : dict = {}

        # ── AI 채팅으로 동적 조정 가능한 파라미터 ──────────────────────
        # entry_thresholds: 진입점수 기준 오버라이드 {regime: threshold}
        #   설정 없으면 strategy.get_entry_threshold() 기본값 사용
        self.entry_thresholds: dict = {}    # {'BULL': 4, 'NEUTRAL': 5, 'BEAR': 6}

        # 시장 국면 (BULL / BEAR / NEUTRAL)
        self.market_regime = "NEUTRAL"
        self.last_regime_check = 0.0
        self._regime_check_interval = 3600  # 1시간마다 재판단
        self._ai_market_entry_bonus = 0    # AI 시장판단 진입 보너스 (-2~+2)
        self._last_defensive_check = 0.0     # 방어 자산 체크 캐시 (5분)
        self._defensive_sold_ts   = {}      # 방어 자산 종목별 청산 타임스탬프 {ticker: ts} (24h 쿨다운)

        # ── 🧠 자가학습 트리거 ───────────────────────────────────────
        self._trades_since_reflection = 0        # 누적 거래 수 (10건마다 반성)
        self._last_emergency_reflection_ts = 0.0  # 긴급 반성 마지막 실행 (4시간 쿨다운)
        self._EMERGENCY_LOSS_THRESHOLD = -80_000  # 8만원 이상 손실 시 긴급 반성 트리거
        self._EMERGENCY_COOLDOWN = 4 * 3600       # 긴급 반성 최소 간격 (4시간)

        self.kis = None
        self.real_kis = None   # 모의봇에서 외인/기관 데이터 조회용 실전 KIS 인스턴스 (주입 시 사용)
        self.telegram = None
        self.claude = None
        self.news_monitor: NewsMonitor | None = None   # DART + Naver 뉴스 모니터

        # 뉴스 모니터 주기 제어
        self._last_dart_check     = 0.0   # 마지막 악재 공시 체크 타임스탬프
        self._dart_check_interval = 600   # 10분마다 체크
        self._last_earnings_check = 0.0   # 마지막 실적 발표일 체크
        self._earnings_check_interval = 3600  # 1시간마다 체크
        self._news_check_lock      = threading.Lock()  # [BUG-C3] 중복 실행 방지
        self._notified_disclosures: set  = set()       # [BUG-M5] 중복 알림 방지 {ticker+rcept_no}
        self._earnings_notified:    dict = {}          # [BUG-C1] 실적 축소 재발동 방지 {ticker: exp_date}

        # 섹터 가이드: 사용자가 입력한 MD 형식 전략 메모 → Claude 매매 심사 시 context로 주입
        self.sector_guide: str = get_sector_guide(user_id) or ''

        self._init_api(kis_config)
        self._init_news_monitor()  # DB에서 뉴스 API 키 로드
        
        if telegram_config and telegram_config.get('token'):
            self.telegram = TelegramNotifier(
                token=telegram_config.get('token', '').strip(),
                chat_id=telegram_config.get('chat_id', '').strip()
            )
            
        self.cached_balance = None
        self.ohlcv_cache = {}
        self.lock = threading.RLock()
        self.last_asset_cost = None
        self.pnl_this_turn = 0.0
        self.initial_capital_captured = False  # W-09: __init__에서 명시 선언

        self._init_dummy_cores()
        self._init_state_restored = self._restore_state()  # W-06: 결과 저장해 이중 호출 방지
        
        self.live_prices = {}
        self.ws_client = None

        def _async_network_connect():
            if self.kis:
                try:
                    app_key_token = self.kis.get_approval_key()
                    if app_key_token:
                        def on_price_update(ticker, price):
                            # W-07: live_prices 쓰기를 lock으로 보호
                            with self.lock:
                                self.live_prices[ticker] = price
                        self.ws_client = self._create_websocket(app_key_token, on_price_update)
                        if self.ws_client:
                            self.ws_client.start()
                except Exception as net_err:
                    logger.warning(f"[{self.mode_name}] WebSocket 초기 연결 실패: {net_err}")

        threading.Thread(target=_async_network_connect, daemon=True).start()

        self.perpetual_thread = threading.Thread(target=self._perpetual_sync_loop, daemon=True)
        self.perpetual_thread.start()
        self.add_log(f"User {user_id} [{self.mode_name}] Bot Controller 가동 완료.")

    def _init_api(self, kis_config):
        """KIS 실전투자 API 초기화."""
        if kis_config and kis_config.get('app_key'):
            self.kis = KisRealApi(
                app_key    = kis_config.get('app_key', '').strip(),
                app_secret = kis_config.get('app_secret', '').strip(),
                account_no = kis_config.get('account_no', '').strip(),
            )
        else:
            self.kis = None

    def _create_websocket(self, app_key, callback):
        """KIS 실전투자 웹소켓 생성."""
        return KisRealWebSocket(app_key, price_callback=callback)

    def _init_news_monitor(self):
        """DB에 저장된 뉴스 API 키로 NewsMonitor 초기화."""
        try:
            keys = get_news_api_keys(self.user_id)
            dart  = keys.get('dart_api_key', '')
            n_id  = keys.get('naver_client_id', '')
            n_sec = keys.get('naver_client_secret', '')
            if dart and n_id and n_sec:
                self.news_monitor = NewsMonitor(dart, n_id, n_sec)
                self.add_log("📡 뉴스 모니터 초기화 완료 (DART + Naver)")
        except Exception as e:
            logger.warning(f"[{self.mode_name}] 뉴스 모니터 초기화 실패: {e}")

    def reload_news_monitor(self, dart_key: str, naver_id: str, naver_secret: str):
        """설정 변경 시 뉴스 모니터를 새 키로 즉시 재초기화."""
        if dart_key and naver_id and naver_secret:
            self.news_monitor = NewsMonitor(dart_key, naver_id, naver_secret)
            self.add_log("📡 뉴스 모니터 키 업데이트 완료")
        else:
            self.news_monitor = None

    def _perpetual_sync_loop(self):
        while True:
            try:
                if self.kis:
                    # 잔고 조회를 별도 스레드에서 실행해 메인 sync 루프 블록 방지
                    result_holder = [None]
                    def _fetch():
                        try:
                            result_holder[0] = self.kis.get_account_balance()
                        except Exception as fe:
                            logger.warning(f"[{self.mode_name}] 잔고 조회 오류: {fe}")
                    t = threading.Thread(target=_fetch, daemon=True)
                    t.start()
                    t.join(timeout=15)  # 최대 15초 대기 후 포기

                    real_balance = result_holder[0]
                    if real_balance:
                        self.cached_balance = real_balance
                        self._sync_internal_balances(real_balance)

                    if self.ws_client:
                        with self.lock:
                            current_tickers = [c.ticker for c in self.core_positions] + list(self.satellite_positions.keys())
                            for idx_ticker, _ in self.market_indices:
                                if idx_ticker not in current_tickers:
                                    current_tickers.append(idx_ticker)
                            # 방어자산 항상 구독 — 대기 상태에도 현재가 표시
                            for _da in DEFENSIVE_ASSETS:
                                if _da['ticker'] not in current_tickers:
                                    current_tickers.append(_da['ticker'])

                        # [BUG-FIX] subscribed_tickers는 ws_client 내부 set — 반복 중 수정 방지용 스냅샷
                        try:
                            current_subscribed = set(self.ws_client.subscribed_tickers)
                        except Exception:
                            current_subscribed = set()
                        for t2 in current_tickers:
                            if t2 not in current_subscribed:
                                self.ws_client.subscribe(t2)
                        for t2 in current_subscribed:
                            if t2 not in current_tickers:
                                self.ws_client.unsubscribe(t2)
            except Exception as e:
                logger.error(f"[{self.mode_name}] _perpetual_sync_loop 오류: {e}", exc_info=True)
            # 5분마다 상태 자동 저장 (systemctl restart 시 최근 상태 보존)
            if int(time.time()) % 300 < 30:
                try: self._save_state()
                except Exception: pass
            time.sleep(30)

    def _sync_internal_balances(self, real_balance):
        with self.lock:
            try:
                if not real_balance or 'stocks' not in real_balance:
                    if self.internal_cash is None:
                        self.internal_cash = 0.0   # KIS API 실패 지속 시 완전 봉쇄 방지
                    return
                real_cash = float(real_balance.get('total_cash', 0))
                real_stock_value = float(real_balance.get('total_value', 0))
                real_purchase = float(real_balance.get('total_purchase', 0))
                total_equity = real_cash + real_stock_value
                # 방어자산 예산 계산용으로 저장
                if total_equity > 0:
                    self._last_total_equity = total_equity

                # 내부 현금 동기화:
                # - 첫 조회 시 KIS 값으로 초기화
                # - 마지막 체결로부터 2분 이상 경과 시 KIS 값으로 재동기화 (드리프트 보정)
                if self.internal_cash is None or (time.time() - self._last_trade_ts >= 120):
                    self.internal_cash = real_cash

                pure_principal = real_cash + real_purchase

                if not getattr(self, 'initial_capital_captured', False):
                    # DB 값이 기본값(1000만)인 경우에만 실제 원금으로 교체.
                    # ─ 이전에는 /tmp/ 플래그 파일을 사용했으나, PC 재시작 시 /tmp/ 가 초기화되면서
                    #   봇이 재기동할 때마다 "현재 잔고"를 원금으로 덮어쓰는 버그가 있었음.
                    #   (예: 손실 발생 후 재시작 → 남은 잔고를 원금으로 저장 → 수익률 왜곡)
                    # ─ 수정: 파일 플래그 제거, DB 값이 기본값일 때만 업데이트 (재시작-덮어쓰기 방지).
                    # ─ BUG-FIX: KIS API 첫 응답이 0이면 pure_principal=0 → 조건 미충족 → 기본값 고착
                    #   → pure_principal > 0인 경우에만 initial_capital_captured=True 확정
                    if pure_principal > 0:
                        db_cash = get_user_initial_cash(self.user_id, self._is_mock)
                        if db_cash == 10000000.0:
                            set_user_initial_cash(self.user_id, pure_principal, self._is_mock)
                            self.add_log(f"💰 [{self.mode_name} 원금 셋업] 투자 원금 {pure_principal:,.0f}원 확정 (첫 실행 감지).")
                        self.initial_capital_captured = True  # 실제 잔고 확인 후에만 플래그 확정
                
                current_asset_cost = real_cash + real_purchase
                if self.last_asset_cost is not None:
                    # W-10: pnl_this_turn != 0이어도 항상 처리해야 누적 방지
                    # (이전의 `pass` 분기는 pnl_this_turn을 0으로 리셋하지 않아 누산 버그 유발)
                    expected_asset_cost = self.last_asset_cost + self.pnl_this_turn
                    self.pnl_this_turn = 0.0
                    deposit_delta = current_asset_cost - expected_asset_cost
                    # 모의투자에서는 deposit_delta 감지 비활성화:
                    # KIS 모의 API의 dnca_tot_amt / real_purchase 값이 T+2 정산 지연으로
                    # 30초마다 들쭉날쭉하여 "외부 입금"으로 오인 → initial_cash 누적 부풀기 버그.
                    # 실전 계좌에서만 실제 외부 입출금 감지가 의미 있음.
                    if not self._is_mock:
                        if deposit_delta > 10000 or deposit_delta < -10000:
                            add_user_initial_cash(self.user_id, deposit_delta, self._is_mock)
                            if deposit_delta > 0: self.add_log(f"💰 {self.mode_name} 계좌 외부 입금 포착: +{deposit_delta:,.0f}원")
                            else: self.add_log(f"💸 {self.mode_name} 계좌 외부 출금 포착: {deposit_delta:,.0f}원")
                    self.last_asset_cost = current_asset_cost
                else:
                    self.last_asset_cost = current_asset_cost
                
                if total_equity >= 0:
                    # ── 동적 균등 예산 배분 ──────────────────────────────────
                    # 코어/위성 균등 배분
                    # BEAR 국면: 방어자산 40% + 현금 60% 전략
                    #   → 코어/위성 예산 = 총자산 × 60% / n (저점매수 탄약 확보)
                    # BULL/NEUTRAL: 100%를 n개 종목으로 나눔
                    _active_cores = [c for c in self.core_positions if c.ticker != "TBD"]
                    _active_sats  = list(self.satellite_positions.values())
                    n_total = max(1, len(_active_cores) + len(_active_sats))
                    _regime_now = getattr(self, 'market_regime', 'NEUTRAL')
                    if _regime_now == "BEAR":
                        # BEAR: 방어자산 40% 제외 후 60% 현금을 저점매수 예산으로
                        _tradable = total_equity * 0.60
                    else:
                        _tradable = total_equity
                    budget_per = _tradable / n_total if total_equity > 0 else 0

                    # 코어 예산 sync (TBD = 0, 매수 완료 = 0, 미매수 = budget_per)
                    for core in self.core_positions:
                        if core.ticker == "TBD":
                            core.cash = 0.0
                            continue
                        api_val    = next((float(s.get('value', 0)) for s in real_balance['stocks']
                                           if s['ticker'] == core.ticker), 0.0)
                        bought_val = getattr(core, '_bought_val', 0.0)
                        if api_val > 0:
                            core._bought_val = 0.0
                            bought_val = 0.0
                        effective_val = max(api_val, bought_val)
                        new_cash = round(max(0.0, budget_per - effective_val), 2)
                        if abs(new_cash - core.cash) > 10000:
                            logger.info(f"[{self.mode_name}] 코어 예산 sync | {core.ticker} | "
                                        f"총자산={total_equity:,.0f} 1인당={budget_per:,.0f}(총{n_total}종목) "
                                        f"api_val={api_val:,.0f} → cash {core.cash:,.0f} → {new_cash:,.0f}")
                        core.cash = new_cash

                    # 위성 예산 sync (주문가능현금 캡 적용)
                    buyable_cash = real_cash
                    if self.kis and hasattr(self.kis, 'get_buyable_cash'):
                        try:
                            _bc = float(self.kis.get_buyable_cash() or 0)
                            if _bc > 0:
                                buyable_cash = _bc
                        except Exception:
                            pass
                    core_reserved = sum(getattr(c, 'cash', 0.0) for c in self.core_positions)
                    avail_for_sat = max(0.0, buyable_cash - core_reserved)
                    sat_cash_each = min(budget_per, avail_for_sat / max(1, len(_active_sats))) if _active_sats else 0
                    for t, sat in self.satellite_positions.items():
                        if int(sat.shares) > 0:
                            sat.cash = 0.0
                        else:
                            sat.cash = round(sat_cash_each, 2)

                # 원자적 교체: 먼저 새 값을 모두 수집한 뒤 한 번에 적용
                # (중간에 예외 발생 시 shares=0으로 남아 재매수 폭주하는 버그 방지)
                new_shares: dict = {}   # ticker → (shares, avg_price, current_price)
                for real_stock in real_balance['stocks']:
                    t = real_stock.get('ticker', '')
                    if not t:
                        continue
                    try:
                        q   = int(real_stock['shares'])
                        p   = float(real_stock['purchase_price'])
                        c_p = float(real_stock.get('current_price', p))
                    except (KeyError, ValueError, TypeError) as _e:
                        logger.warning(f"[{self.mode_name}] KIS 잔고 파싱 오류 ({t}): {_e} — 건너뜀")
                        continue
                    if q < 0 or p < 0 or c_p < 0:
                        logger.warning(f"[{self.mode_name}] KIS 비정상 값 ({t}) q={q} p={p} cp={c_p} — 건너뜀")
                        continue
                    stock_name = real_stock.get('name', t)
                    new_shares[t] = (q, p, c_p, stock_name)

                # API 조회 성공 후에만 shares 초기화 → 교체 (원자적)
                # stocks=빈목록인데 total_value > 0 → KIS API 오류로 판단, 초기화 건너뜀
                _reported_val = float(real_balance.get('total_value', 0))
                if not new_shares and _reported_val > 100_000:
                    logger.warning(
                        f"[{self.mode_name}] KIS stocks 빈 응답 (total_value={_reported_val:,.0f}원) — 포지션 초기화 건너뜀"
                    )
                    return
                for core in self.core_positions:
                    core.shares = 0
                    core.floor_shares = 0   # 외부 청산 시 stale floor 초기화 — sync 후 재설정됨
                for sat in self.satellite_positions.values(): sat.shares = 0

                for t, (q, p, c_p, stock_name) in new_shares.items():
                    is_core = False
                    for core in self.core_positions:
                        if core.ticker == t:
                            core.shares = q; core.avg_price = p; core.kis_current_price = c_p
                            if core.floor_shares == 0 and q > 0: core.floor_shares = max(1, int(q * self.core_min_floor_ratio))
                            is_core = True; break

                    if not is_core:
                        if t in self.satellite_positions:
                            sat = self.satellite_positions[t]
                            sat.shares = q; sat.avg_price = p; sat.kis_current_price = c_p
                        else:
                            # num_satellites 한도 초과 시 새 위성 자동 추가 차단
                            if len(self.satellite_positions) < self.num_satellites:
                                self.add_log(f"🌟 {self.mode_name} 계좌 미등록 종목 '{stock_name}'을 위성으로 강제 편입합니다!")
                                new_sat = Position(t, stock_name, 0.0)
                                new_sat.shares = q; new_sat.avg_price = p; new_sat.kis_current_price = c_p
                                self.satellite_positions[t] = new_sat
                                if not any(x['ticker'] == t for x in self.satellite_info):
                                    self.satellite_info.append({'ticker': t, 'name': stock_name, 'return_pct': 0.0, 'sector': '-'})
                            else:
                                logger.warning(f"[{self.mode_name}] 위성 한도({self.num_satellites}) 초과 — '{stock_name}'({t}) 자동 편입 생략")

                # ── 체결 확인: API 반영 후 "체결 대기 ⏳" 상태 자동 해제 ──────────
                # 트레이딩 루프에서는 "대기" 포함 상태를 갱신하지 않아 영구 고착되는 버그 수정.
                # API에서 shares > 0 이 확인되면 → "보유 중" 으로 전환.
                # 1분 경과 후에도 shares == 0 이면 → "미체결" 경고로 전환.
                _now = time.time()
                for core in self.core_positions:
                    if "대기" in getattr(core, 'status', ''):
                        if core.shares > 0:
                            core.status = "보유 중 💎"
                        elif _now - getattr(core, 'last_order_time', 0) > 60:
                            core.status = "미체결 ⚠️"
                for sat in self.satellite_positions.values():
                    if "대기" in getattr(sat, 'status', ''):
                        if sat.shares > 0:
                            sat.status = "보유 중 ✅"
                        elif _now - getattr(sat, 'last_order_time', 0) > 60:
                            sat.status = "미체결 ⚠️"

            except Exception as e:
                logger.error(f"[{self.mode_name}] 장부 동기화 중 오류: {e}", exc_info=True)

    def _init_dummy_cores(self):
        """
        봇 초기화 시 임시 코어 포지션 생성 (initialize_portfolio 전 플레이스홀더).
        구조: 사용자 지정 최대 3개 → 남은 자리만 TBD 플레이스홀더로 채움
        BUG-FIX: 기존 보유 데이터(shares, avg_price 등) 보존 — reload_api_keys 호출 시 정보 손실 방지
        """
        # 기존 포지션 데이터 스냅샷 (설정 변경 시 보유주/평단 보존용)
        _existing = {c.ticker: c for c in self.core_positions if c.ticker != "TBD"}

        self.core_positions = []
        user_tickers_seen: set = set()
        # 사용자 지정 종목 전부 사용 (최대 3개까지)
        for c in self.user_core_stocks[:2]:
            if c.get('ticker') and c['ticker'] not in user_tickers_seen:
                pos = CorePosition(c['ticker'], c['name'], initial_cash=0)
                if c.get('dca'):
                    pos.dca_mode           = True
                    pos.dca_amount         = float(c.get('dca_amount', 0))
                    pos.dca_interval_hours = int(c.get('dca_hours', 72))
                    pos.dca_dip_pct        = float(c.get('dca_dip_pct', 3.0))
                # 기존 보유 데이터 복원 (동일 티커가 이미 있으면)
                if c['ticker'] in _existing:
                    _old = _existing[c['ticker']]
                    pos.shares       = _old.shares
                    pos.floor_shares = _old.floor_shares
                    pos.avg_price    = _old.avg_price
                    pos.cash         = _old.cash
                    pos.initial_cash = _old.initial_cash
                    pos.second_buy_price = getattr(_old, 'second_buy_price', 0.0)
                    pos.second_buy_cash  = getattr(_old, 'second_buy_cash',  0.0)
                    pos.second_buy_done  = getattr(_old, 'second_buy_done',  False)
                    pos.last_dca_time    = getattr(_old, 'last_dca_time',    0.0)
                self.core_positions.append(pos)
                user_tickers_seen.add(c['ticker'])
        # 빈 슬롯은 플레이스홀더로 채움 (사용자가 ⚙️에서 종목 지정하도록 안내)
        for i in range(len(self.core_positions), 2):
            ph = CorePosition("TBD", f"종목 미지정 #{i+1}", initial_cash=0)
            ph.status = "⚙️ 종목 설정 필요"
            ph.status_msg = "상단 ⚙️ 버튼을 눌러 코어 종목을 지정해주세요."
            self.core_positions.append(ph)
            
        if self.kis:
            def _async_init_balance():
                try:
                    real_balance = self.kis.get_account_balance()
                    if real_balance and 'stocks' in real_balance:
                        for real_stock in real_balance['stocks']:
                            t = real_stock['ticker']; q = int(real_stock['shares']); p = float(real_stock['purchase_price'])
                            for core in self.core_positions:
                                if core.ticker == t:
                                    core.shares = q; core.avg_price = p; break
                except Exception as e:
                    logger.warning(f"[{self.mode_name}] 초기 잔고 조회 실패: {e}")
            threading.Thread(target=_async_init_balance, daemon=True).start()

    def _inject_user_satellites(self):
        """user_satellite_stocks를 satellite_info 앞 슬롯에 고정 (코어의 _init_dummy_cores와 동일 패턴).
        - 사용자 지정 종목은 screener 결과보다 우선 배치
        - 중복 제거 후 num_satellites 한도 적용
        - 기본 지표(RSI, 수익률, 거래량비율) 자동 채움 → AI 리뷰어 "데이터 미제공" 방지
        - 사용자 목록에서 제거됐고 보유주 없는 종목은 satellite_positions에서 즉시 정리
        """
        user_tickers = {s['ticker'] for s in self.user_satellite_stocks if s.get('ticker')}

        # BUG-FIX: 사용자 목록에서 제거됐고 보유주 없는 종목은 satellite_positions에서 정리
        # (치우기 명령 후에도 ghost 포지션이 남아 재시작 시 복귀하는 버그 방지)
        for _t in list(self.satellite_positions.keys()):
            if _t not in user_tickers and int(self.satellite_positions[_t].shares) == 0:
                # 스크리너 선정 or 이전에 user가 지정했다가 제거한 종목 → positions에서 제거
                self.satellite_positions.pop(_t, None)
                self.satellite_info = [c for c in self.satellite_info if c.get('ticker') != _t]

        if not self.user_satellite_stocks:
            return
        # screener 결과에서 사용자 종목 제거 (중복 방지)
        filtered = [c for c in self.satellite_info if c['ticker'] not in user_tickers]
        pinned = []
        for s in self.user_satellite_stocks:
            if not s.get('ticker') or not s.get('name'):
                continue
            t = s['ticker']
            # [BUG-FIX] KR 봇에 US 티커(알파벳) 주입 방지 — KR 티커는 6자리 숫자
            if not (t.isdigit() and len(t) == 6):
                logger.warning(f"[KR봇] 사용자지정 위성 무시: {t} (KR 형식 아님 — US 봇 전용 종목)")
                continue
            # 기본 지표 계산 (OHLCV 캐시 활용 — 없으면 KIS 호출)
            _ret = 0.0; _rsi = None; _vol_ratio = None
            try:
                df = self._get_cached_base_ohlcv(t)
                if df.empty and self.kis:
                    df = self.kis.get_ohlcv(t, "D")
                if df is not None and not df.empty and 'close' in df.columns:
                    c_s = df['close'].dropna()
                    if len(c_s) >= 20:
                        _ret = round((c_s.iloc[-1] / c_s.iloc[-min(22, len(c_s)-1)] - 1) * 100, 1)
                    if len(c_s) >= 11:
                        _d = c_s.diff()
                        _g = _d.clip(lower=0).rolling(9).mean()
                        _l = (-_d.clip(upper=0)).rolling(9).mean()
                        _rsi = round(float((100 - 100 / (1 + _g / (_l + 1e-10))).iloc[-1]), 1)
                    if 'volume' in df.columns and len(df) >= 21:
                        v_s = df['volume'].dropna()
                        avg20 = float(v_s.iloc[-21:-1].mean()) if len(v_s) > 20 else 1
                        _vol_ratio = round(float(v_s.iloc[-1]) / avg20, 2) if avg20 > 0 else 1.0
            except Exception:
                pass
            entry = {'ticker': t, 'name': s['name'], 'return_pct': _ret, 'sector': '사용자지정'}
            if _rsi is not None:
                entry['rsi'] = _rsi
            if _vol_ratio is not None:
                entry['vol_ratio'] = _vol_ratio
            pinned.append(entry)
        self.satellite_info = (pinned + filtered)[:self.num_satellites]

    def _get_cached_base_ohlcv(self, ticker):
        today_str = _now_kst().strftime('%Y-%m-%d')
        with self.lock:
            if ticker in self.ohlcv_cache and self.ohlcv_cache[ticker]['date'] == today_str:
                return self.ohlcv_cache[ticker]['df'].copy()
        if self.kis:
            df = self.kis.get_ohlcv(ticker, "D")
            if df is None or (not hasattr(df, 'columns')) or ('high' not in df.columns): return pd.DataFrame()
            if df is not None and not df.empty and 'date' in df.columns:
                df['date'] = pd.to_datetime(df['date'])
                df = df[df['date'].dt.date < _now_kst().date()].reset_index(drop=True)
                with self.lock: self.ohlcv_cache[ticker] = {"date": today_str, "df": df}
                return df.copy()
        return pd.DataFrame()

    def _get_extended_ohlcv(self, ticker, current_price):
        base_df = self._get_cached_base_ohlcv(ticker)
        if base_df.empty: return self.kis.get_ohlcv(ticker, "D") if self.kis else pd.DataFrame()
        realtime_data = self.kis.get_realtime_price_data(ticker) if self.kis else None
        if realtime_data:
            today_row = pd.DataFrame([{'date': pd.to_datetime(_now_kst().date()), 'open': realtime_data['open'], 'high': realtime_data['high'], 'low': realtime_data['low'], 'close': realtime_data['close'], 'volume': realtime_data['volume']}])
        else:
            today_row = pd.DataFrame([{'date': pd.to_datetime(_now_kst().date()), 'open': float(current_price), 'high': float(current_price), 'low': float(current_price), 'close': float(current_price), 'volume': 0.0}])
        return pd.concat([base_df, today_row], ignore_index=True)

    def add_log(self, msg):
        t = _now_kst().strftime("%H:%M:%S")
        self.logs.append({"time": t, "message": msg})   # deque(maxlen=100) — 자동 순환
        print(f"[{t}] {msg}")

    def _send_telegram(self, message, msg_type: str = 'misc'):
        if not self.telegram: return
        # 메시지에 이미 모드 정보가 포함되어 있으므로 그대로 전달
        threading.Thread(target=self.telegram.send_message, args=(message,), daemon=True).start()

    def _send_trade_telegram(self, message):
        """거래 체결 알림 전용 헬퍼."""
        self._send_telegram(message, msg_type='trade')

    def _send_reject_telegram(self, message):
        """거래 거절 알림 전용 헬퍼."""
        self._send_telegram(message, msg_type='reject')

    def _buy_order(self, ticker: str, qty: int, pos, name: str, limit_price: int = 0,
                   strategy: str = "", ai_reason: str = "") -> bool:
        """매수 주문 실행 + KIS 응답 체크. 성공 True, 실패 False (봇 로그에 에러 기록).
        limit_price = 0 → 현재가 +0.3% 지정가 자동 계산 (슬리피지 제한)
        limit_price = -1 → 강제 시장가"""
        if not self.kis:
            return False
        if self.internal_cash is None:
            self.add_log(f"⏳ [{name}] 매수 보류 — KIS 잔고 초기화 대기 중")
            return False
        if limit_price == 0:
            # 코어·위성: 현재가 +0.3% 지정가 → 빠른 체결 + 슬리피지 제한
            cp = self.live_prices.get(ticker, 0)
            if cp > 0:
                limit_price = int(cp * 1.003)
        elif limit_price == -1:
            limit_price = 0  # 시장가
        result = self.kis.buy_market_order(ticker, qty, price=limit_price)
        if result:
            # 내부 현금 즉시 차감 — KIS 모의 API 반영 지연 보정
            with self.lock:
                self._last_trade_ts = time.time()
            est_price = self.live_prices.get(ticker, 0) or getattr(pos, 'avg_price', 0) or 0
            if est_price > 0:
                with self.lock:
                    if self.internal_cash is not None:
                        self.internal_cash = max(0.0, self.internal_cash - est_price * qty * 1.00015)
            try:
                log_trade_journal(self.user_id, ticker, name, 'BUY', est_price or limit_price,
                                  strategy=strategy, ai_reason=ai_reason[:120], shares=qty, mode='KR')
            except Exception:
                pass
            return True
        err = f"⚠️ [{self.mode_name}] {name}({ticker}) {qty}주 매수 주문 실패 — KIS API 오류"
        self.add_log(err)
        logger.warning(err)
        with self.lock:
            pos.status = "주문 실패 ❌"
            pos.status_msg = "KIS API 오류 — 서버 로그 확인 필요"
            # [BUG-FIX] 주문 실패 시 pos.cash 즉시 초기화.
            # 초기화하지 않으면 다음 사이클에도 동일 금액으로 재시도 → 반복 실패.
            # _sync_internal_balances 가 30초 후 실제 잔고 기준으로 재배정함.
            pos.cash = 0.0
        return False

    def _sell_order(self, ticker: str, qty: int, pos, name: str, price: int = 0,
                    strategy: str = "", ai_reason: str = "", profit: float = 0) -> bool:
        """매도 주문 실행 + KIS 응답 체크. 성공 True, 실패 False (봇 로그에 에러 기록)."""
        if not self.kis:
            return False
        if qty <= 0:
            self.add_log(f"⚠️ SELL 건너뜀: {name}({ticker}) qty={qty} ≤ 0")
            return False
        result = self.kis.sell_market_order(ticker, qty, price=price)
        if result:
            # 내부 현금 즉시 증가 — KIS 모의 API 반영 지연 보정
            with self.lock:
                self._last_trade_ts = time.time()
            est_price = price or self.live_prices.get(ticker, 0) or getattr(pos, 'avg_price', 0) or 0
            if est_price > 0:
                with self.lock:
                    if self.internal_cash is not None:
                        self.internal_cash += est_price * qty * (1 - _SELL_FEE - _SELL_TAX)
            try:
                log_trade_journal(self.user_id, ticker, name, 'SELL', est_price or price,
                                  strategy=strategy, ai_reason=ai_reason[:120],
                                  shares=qty, profit=profit, mode='KR')
            except Exception:
                pass
            return True
        err = f"⚠️ [{self.mode_name}] {name}({ticker}) {qty}주 매도 주문 실패 — KIS API 오류"
        self.add_log(err)
        logger.warning(err)
        with self.lock:
            pos.status = "주문 실패 ❌"
        return False

    def _record_daily_pnl(self, profit: float):
        """일별 실현 손익을 기록합니다 (PnL 그래프용)."""
        if profit == 0:
            return
        today = _now_kst().strftime('%Y-%m-%d')
        with self.lock:
            self.daily_pnl[today] = self.daily_pnl.get(today, 0.0) + profit

    # ══════════════════════════════════════════════════════════════════
    # 📡 뉴스 모니터 — 악재 공시 감지 / 실적 발표 예정 포지션 축소
    # ══════════════════════════════════════════════════════════════════

    def _check_news_alerts(self):
        """
        보유 종목별 악재 공시·실적 발표 예정 체크 (10분/1시간 주기).
        - 악재 공시 발견  → 텔레그램 경보 + AI 손절 검토 (위성만 매도, 코어는 알림만)
        - 실적 발표 D-7내 → 텔레그램 알림 + 포지션 30% 축소 (1회만)
        """
        if not self.news_monitor:
            return

        # [BUG-C3] 중복 실행 방지 — 이미 실행 중이면 즉시 반환
        if not self._news_check_lock.acquire(blocking=False):
            return
        try:
            self._check_news_alerts_inner()
        finally:
            self._news_check_lock.release()

    def _check_news_alerts_inner(self):
        now_ts = time.time()

        # ── 1. 악재 공시 체크 (10분 주기) ─────────────────────────────
        # [BUG-C3] 타임스탬프 체크+갱신을 원자적으로 처리
        with self.lock:
            dart_due = (now_ts - self._last_dart_check >= self._dart_check_interval)
            if dart_due:
                self._last_dart_check = now_ts
                held_sat = [(t, p.name, p.shares, p.avg_price)
                            for t, p in self.satellite_positions.items() if p.shares > 0]
                held_core = [(c.ticker, c.name, c.shares, c.avg_price)
                             for c in self.core_positions if c.shares > 0]

        if dart_due:
            # [BUG-M4] API rate limit — 종목 간 0.5초 간격
            for ticker, name, shares, avg_price in held_sat + held_core:
                try:
                    time.sleep(0.5)
                    neg = self.news_monitor.check_negative_disclosure(ticker, days=2)
                    if not neg:
                        continue
                    for d in neg:
                        report_nm = d.get('report_nm', '')
                        rcept_dt  = d.get('rcept_dt', '')
                        rcept_no  = d.get('rcept_no', rcept_dt + report_nm)
                        disc_key  = f"{ticker}_{rcept_no}"

                        # [BUG-M5] 이미 알림한 공시는 건너뜀
                        with self.lock:
                            if disc_key in self._notified_disclosures:
                                continue
                            self._notified_disclosures.add(disc_key)

                        is_core = any(c.ticker == ticker for c in self.core_positions)
                        sell_note = "📌 코어 종목 — 플로어 보호로 자동 매도 없음" if is_core else "🤖 AI 손절 검토 중..."
                        msg = (
                            f"⚠️ <b>악재 공시 감지</b>  ·  {self.alert_icon} {self.mode_name}\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"📌 <b>{name}</b>  <code>{ticker}</code>\n"
                            f"📋 {report_nm}\n"
                            f"📅 공시일: {rcept_dt}\n"
                            f"💼 보유: {shares}주 @ {avg_price:,.0f}원\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"{sell_note}"
                        )
                        self._send_telegram(msg, 'news')
                        self.add_log(f"⚠️ {name}({ticker}) 악재 공시: {report_nm}")

                        # [BUG-N2] 코어 종목은 알림만, 매도 없음
                        if is_core or not self.claude:
                            continue

                        # 위성 포지션 AI 손절 검토
                        try:
                            context = f"악재 공시 발생: {report_nm} ({rcept_dt})\n보유: {shares}주 @ 평단 {avg_price:,.0f}원"
                            decision, ai_reason = self.claude.ai_approve_trade(
                                'SELL', name, ticker, avg_price, "공시감지",
                                {}, self.hot_sectors,
                                get_recent_trades(self.user_id, ticker),
                                load_ai_rules(self.user_id) + ("\n\n[📊 섹터 가이드]\n" + self.sector_guide if self.sector_guide else ''),
                                context=context
                            )
                            if decision:
                                pos = self.satellite_positions.get(ticker)
                                # [C-NEW-04] sell_shares 를 락 안에서 재확인 — trading_job 동시 매도 경합 방지
                                sell_shares = 0
                                with self.lock:
                                    if pos and pos.shares > 0:
                                        sell_shares = pos.shares
                                if sell_shares > 0:
                                    if self._sell_order(ticker, sell_shares, pos, name):
                                        with self.lock:
                                            price_now = self.live_prices.get(ticker) or avg_price
                                            self._sat_exit_reset(pos); pos.status = "악재공시 손절 🚨"
                                            self.pnl_this_turn += _net_profit(price_now, avg_price, sell_shares)
                                        profit = _net_profit(price_now, avg_price, sell_shares)
                                        self._log_trade(ticker, name, 'SELL', price_now, "공시감지", f"악재공시 AI 손절: {report_nm}", profit=profit)  # [BUG-C2]
                                        self._record_daily_pnl(profit)  # [BUG-C2]
                                        self.add_log(f"🚨 {name}({ticker}) 악재 공시 AI 손절 완료")
                                        if self.claude:
                                            self.claude.record_trade_event(f"KR 악재공시 손절: {name}({ticker}) {sell_shares}주 @ {price_now:,.0f}원 | 손익: {profit:+,.0f}원 | 공시: {report_nm}")
                                        self._send_telegram(
                                            f"🚨 <b>악재공시 손절 완료</b>  {self.alert_icon}\n"
                                            f"📌 <b>{name}</b> | 손익: {profit:+,.0f}원\n"
                                            f"🤖 {ai_reason[:100]}",
                                            'news'
                                        )
                        except Exception as ae:
                            logger.warning(f"[{self.mode_name}] 악재 공시 AI 판단 오류 ({ticker}): {ae}")
                except Exception as e:
                    logger.warning(f"[{self.mode_name}] DART 공시 체크 오류 ({ticker}): {e}")

        # ── 2. 실적 발표 예정 체크 (1시간 주기) ───────────────────────
        with self.lock:
            earnings_due = (now_ts - self._last_earnings_check >= self._earnings_check_interval)
            if earnings_due:
                self._last_earnings_check = now_ts
                sat_items = [(t, p.name, p.shares, p.avg_price)
                             for t, p in self.satellite_positions.items() if p.shares > 0]

        if earnings_due:
            for ticker, name, shares, avg_price in sat_items:
                try:
                    time.sleep(0.5)  # [BUG-M4] rate limit
                    earnings = self.news_monitor.get_upcoming_earnings(ticker)
                    if not earnings:
                        continue
                    days_until = earnings['days_until']
                    exp_date   = earnings['expected_date']

                    # [BUG-C1] 이미 이 예정일로 축소한 종목은 재발동 차단
                    if self._earnings_notified.get(ticker) == exp_date:
                        continue

                    if days_until <= 7 and shares > 1:
                        reduce_qty = max(1, int(shares * 0.30))
                        pos = self.satellite_positions.get(ticker)
                        if pos and pos.shares > 0:
                            if self._sell_order(ticker, reduce_qty, pos, name):
                                with self.lock:
                                    price_now = self.live_prices.get(ticker) or avg_price  # [BUG-M1] 락 안에서
                                    pos.shares = max(0, pos.shares - reduce_qty)
                                    pos.status = "실적전 축소 📊"
                                    self._earnings_notified[ticker] = exp_date  # [BUG-C1] 재발동 방지
                                profit = _net_profit(price_now, avg_price, reduce_qty)
                                self._log_trade(ticker, name, 'SELL', price_now, "실적공시대응", f"실적발표 D-{days_until} 30% 축소", profit=profit)
                                with self.lock:
                                    self.pnl_this_turn += profit  # [BUG-C1]
                                self._record_daily_pnl(profit)    # [BUG-C1]
                                msg = (
                                    f"📊 <b>실적 발표 전 포지션 축소</b>  ·  {self.alert_icon} {self.mode_name}\n"
                                    f"━━━━━━━━━━━━━━━━━━━━\n"
                                    f"📌 <b>{name}</b>  <code>{ticker}</code>\n"
                                    f"📅 실적 발표 예정: {exp_date} (D-{days_until})\n"
                                    f"✂️ {reduce_qty}주 (30%) 선익절  손익: {profit:+,.0f}원\n"
                                    f"💼 잔여: {pos.shares}주 계속 보유\n"
                                    f"━━━━━━━━━━━━━━━━━━━━\n"
                                    f"⏰ {_now_kst().strftime('%H:%M KST')}"
                                )
                                self._send_telegram(msg, 'news')
                                self.add_log(f"📊 {name}({ticker}) 실적 발표 D-{days_until} → 30% 축소")
                except Exception as e:
                    logger.warning(f"[{self.mode_name}] 실적 발표 체크 오류 ({ticker}): {e}")

    _MAX_DAILY_LOSS_PER_TICKER = -5_000   # 종목당 하루 최대 허용 손실 (원)

    @staticmethod
    def _sat_exit_reset(pos) -> None:
        """위성 포지션 전량 청산 후 모든 거래 상태 초기화 — 재진입 시 이전 플래그 오염 방지."""
        pos.shares             = 0
        pos.max_price          = 0
        pos.stop_news_checked  = False
        pos.swing_acc_count    = 0
        pos.overext_sell_count = 0
        pos.second_buy_done    = False
        pos.second_buy_price   = 0.0
        pos.second_buy_cash    = 0.0
        pos.pyramid_done       = False
        pos.partial_sold       = False
        pos.partial_sold_2     = False
        pos.cash               = 0.0   # 다음 sync 때 fresh 재할당 — stale 예산 재진입 방지

    def _refresh_blacklist(self):
        """날짜가 바뀌면 당일 블랙리스트를 초기화합니다. [BUG-M1] 락 내부에서 호출 전제."""
        today = _now_kst().strftime('%Y-%m-%d')
        if self._bl_date != today:
            self._bl_date              = today
            self._satellite_rejects    = {}
            self._satellite_reject_rsn = {}
            self._daily_loss_by_ticker = {}
            self._notified_disclosures = set()   # 날짜 바뀌면 공시 알림 캐시 초기화

    def _add_satellite_reject(self, ticker: str, reason: str):
        """AI 거절 위성 종목에 15분 쿨다운 적용 (영구 블랙리스트 아님)."""
        with self.lock:
            self._refresh_blacklist()
            self._satellite_rejects[ticker]    = time.time()   # 타임스탬프 저장
            self._satellite_reject_rsn[ticker] = reason
        try:
            self._save_state()
        except Exception:
            pass

    def _record_ticker_loss(self, ticker: str, profit: float):
        """손실 발생 시 종목별 당일 누계 손실을 기록합니다."""
        if profit >= 0:
            return
        with self.lock:
            self._refresh_blacklist()
            self._daily_loss_by_ticker[ticker] = (
                self._daily_loss_by_ticker.get(ticker, 0) + profit
            )

    def _is_satellite_blacklisted(self, ticker: str) -> bool:
        # 사용자 직접 지정 종목은 쿨다운 적용 안 함
        user_tickers = {s['ticker'] for s in self.user_satellite_stocks if s.get('ticker')}
        if ticker in user_tickers:
            return False
        with self.lock:
            # _refresh_blacklist 는 trading_job 시작 시 호출 — 스캔 중 자정 리셋 방지
            ts = self._satellite_rejects.get(ticker)
            if ts is None:
                return False
            if time.time() - ts < self._SAT_REJECT_COOLDOWN:
                return True   # 쿨다운 중
            # 쿨다운 만료 → 재심사 허용
            del self._satellite_rejects[ticker]
            self._satellite_reject_rsn.pop(ticker, None)
            return False

    def _fmt_scan_report(self, theme: str, candidates: list, regime: str, action_note: str) -> str:
        """친구 AI 스타일 매수 검토 리포트 포맷.
        candidates: [{'name', 'ticker', 'price', 'stats': {'고점대비', '저점반등', 'ma5_pos', 'extra'}}]
        """
        regime_label = {"BULL": "상승장 🚀", "BEAR": "하락장 🐻", "NEUTRAL": "횡보장 ➡️"}.get(regime, regime)
        now_str = _now_kst().strftime('%H:%M KST')
        lines = [
            f"[{theme}]",
            f"정규장 · {regime_label} | {len(candidates)}종목",
            "━━━━━━━━━━━━━━━━━━━━",
        ]
        for c in candidates:
            s = c.get('stats', {})
            parts = [f"<b>{c['name']}</b>({c['ticker']}) {c['price']:,.0f}원"]
            if '고점대비' in s: parts.append(f"고점대비 {s['고점대비']:+.1f}%")
            if '저점반등' in s: parts.append(f"저점반등 {s['저점반등']:+.1f}%")
            if 'ma5_pos' in s: parts.append(f"MA5 {'위 ✅' if s['ma5_pos'] else '아래 ⚠️'}")
            if 'extra'  in s: parts.append(s['extra'])
            lines.append("· " + " / ".join(parts))
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"행동: {action_note}")
        lines.append(f"⏰ {now_str}")
        return "\n".join(lines)

    @staticmethod
    def _calc_price_stats(df: 'pd.DataFrame', price: float) -> dict:
        """OHLCV DataFrame → 고점대비/저점반등/MA5 위치 계산."""
        stats = {}
        try:
            if df is None or df.empty or 'close' not in df.columns:
                return stats
            closes = df['close'].dropna()
            if len(closes) >= 5:
                ma5 = closes.tail(5).mean()
                stats['ma5_pos'] = price >= ma5
            high_col = 'high' if 'high' in df.columns else 'close'
            low_col  = 'low'  if 'low'  in df.columns else 'close'
            recent20_high = df[high_col].tail(20).max()
            recent5_low   = df[low_col].tail(5).min()
            if recent20_high > 0:
                stats['고점대비'] = (price - recent20_high) / recent20_high * 100
            if recent5_low > 0:
                stats['저점반등'] = (price - recent5_low)  / recent5_low  * 100
        except Exception:
            pass
        return stats

    def _fmt_trade_msg(self, action_emoji, action_name, ticker, name, price, qty,
                       profit=None, strategy=None, ai_reason=None, note=None):
        """HTML 포맷 매매 체결 알림 메시지를 생성합니다."""
        now_str = _now_kst().strftime('%H:%M KST')
        invest = price * qty
        lines = [
            f"{action_emoji} <b>{action_name}</b>  ·  {self.alert_icon} {self.mode_name}",
            "━━━━━━━━━━━━━━━━━━━━",
            f"📌 <b>{name}</b>  <code>{ticker}</code>",
            f"💰 <b>{price:,.0f}원</b> × <b>{qty}주</b>  =  <b>{invest:,.0f}원</b>",
        ]
        if profit is not None:
            emoji = "📈" if profit >= 0 else "📉"
            lines.append(f"{emoji} 손익  <b>{profit:+,.0f}원</b>")
        if strategy:
            lines.append(f"📊 {strategy}")
        if ai_reason:
            lines.append(f"🤖 {ai_reason}")
        if note:
            lines.append(f"📋 {note}")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"⏰ {now_str}")
        return "\n".join(lines)

    def reload_api_keys(self, kis_config, telegram_config, gemini_config, core_stocks):
        self.cached_balance = None
        try: self.user_core_stocks = json.loads(core_stocks) if core_stocks else []
        except Exception: self.user_core_stocks = []
        _u = self.user_core_stocks[0] if self.user_core_stocks else None
        self.core_ticker = _u['ticker'] if _u else ""
        self.core_name   = _u['name']   if _u else ""

        self._init_api(kis_config)

        if telegram_config and telegram_config.get('token'):
            self.telegram = TelegramNotifier(token=telegram_config.get('token', '').strip(), chat_id=telegram_config.get('chat_id', '').strip())
        else: self.telegram = None
        self._init_dummy_cores()
        self._save_state()   # 새 코어 설정 즉시 저장 → 재시작 후에도 유지
        self.add_log(f"🔑 [{self.mode_name}] API 키 및 계좌 설정이 시스템에 반영되었습니다.")

    def update_mode(self, is_mock, total_cash=10000000):
        pass

    def _ai_filter_satellites(self, candidates: list) -> list:
        """AI가 위성 후보 검토 — 부적합 종목 제거 + 전략 교체. AI 없으면 원본 반환."""
        if not self.claude or not candidates:
            return candidates
        try:
            # ── 심사 시작 텔레그램 알림 ──
            preview = ', '.join([f"{c['name']}({c['ticker']})" for c in candidates[:5]])
            if len(candidates) > 5:
                preview += f" 외 {len(candidates)-5}개"
            self.add_log(f"🤖 AI가 위성 후보 {len(candidates)}개 종목·전략 검토 중...")
            if self.telegram:
                self.telegram.send_message(
                    f"🔍 <b>위성 후보 AI 심사 시작</b>  {self.alert_icon} {self.mode_name}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📋 후보 <b>{len(candidates)}개</b> → AI 검토 중...\n"
                    f"📝 {preview}"
                )
            reviewed = self.claude.review_satellite_candidates(candidates, self.hot_sectors, sector_guide=self.sector_guide)
            approved = [c for c in reviewed if c.get('approved', True)]
            rejected = [c for c in reviewed if not c.get('approved', True)]
            for c in rejected:
                self.add_log(f"🛑 AI 위성 퇴출: {c['name']}({c['ticker']}) — {c.get('ai_reason','')}")
                self._add_satellite_reject(c['ticker'], c.get('ai_reason', 'AI 부적합 판정'))
            # ── 심사 결과 텔레그램 알림 ──
            if self.telegram:
                approve_lines = "\n".join([f"  ✅ {c['name']}({c['ticker']})" for c in approved[:6]])
                reject_lines  = "\n".join([f"  🛑 {c['name']}({c['ticker']}): {c.get('ai_reason','')[:25]}" for c in rejected[:4]])
                msg = (
                    f"🤖 <b>AI 위성 심사 완료</b>  {self.alert_icon} {self.mode_name}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"✅ 승인 <b>{len(approved)}개</b>  /  🛑 퇴출 <b>{len(rejected)}개</b>\n"
                )
                if approve_lines:
                    msg += f"\n<b>승인 종목:</b>\n{approve_lines}"
                if reject_lines:
                    msg += f"\n\n<b>퇴출 종목:</b>\n{reject_lines}"
                self.telegram.send_message(msg)
            return approved
        except Exception as e:
            logger.warning(f"[{self.mode_name}] _ai_filter_satellites 오류: {e}")
            return candidates

    def initialize_portfolio(self, total_cash):
        self.add_log("포트폴리오 초기화 중...")
        raw_info, _new_hot = select_satellites(kis=self.kis, n=self.num_satellites * 2, verbose=False, claude_client=self.claude, sector_guide=self.sector_guide, real_kis=self.real_kis)
        if _new_hot:
            self.hot_sectors = _new_hot
        if self.hot_sectors:
            self.add_log(
                f"🔥 전 섹터 스캔 완료 (총 {len(self.hot_sectors)}개) — "
                f"가산점 TOP4: {', '.join(self.hot_sectors[:4])}"
            )
        else:
            self.add_log("⚠️ 전 섹터 스캔 완료 — 강세 섹터 없음 (상대 강세 기준 후보 선정)")
        # AI 검토: 부적합 종목 제거 후 num_satellites 개수만 사용
        filtered_info = self._ai_filter_satellites(raw_info)
        self.satellite_info = filtered_info[:self.num_satellites]
        self._inject_user_satellites()  # 사용자 지정 종목 우선 고정
        log_lines = [f"  {i+1}. {c['name']} ({c['ticker']}) {c.get('momentum_20d', 0):+.1f}%" for i, c in enumerate(self.satellite_info)]
        for line in log_lines: self.add_log(f"✅ {line.strip()}")
        log_html = "\n".join([f"  · {c['name']} <code>{c['ticker']}</code>" for c in self.satellite_info])
        self._send_telegram(
            f"🔍 <b>위성 종목 선정 완료{'(AI 검토 반영)' if self.claude else ''}</b>  ·  {self.alert_icon} {self.mode_name}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{log_html}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ {_now_kst().strftime('%H:%M KST')}"
        )

        core_budget = total_cash * self.core_ratio
        sat_budget  = total_cash * self.satellite_ratio
        n_sat       = len(self.satellite_info) if self.satellite_info else self.num_satellites
        per_sat     = sat_budget / n_sat if n_sat > 0 else 0

        # ── 코어 구성: 사용자 지정 종목만 사용 (AI 자동 선정 없음) ──
        self.core_positions = []
        user_tickers: set = set()

        for user_pick in self.user_core_stocks[:2]:
            if user_pick.get('ticker') and user_pick['ticker'] not in user_tickers:
                self.core_positions.append(CorePosition(user_pick['ticker'], user_pick['name'], initial_cash=0))
                user_tickers.add(user_pick['ticker'])

        # 코어 예산 균등 배분
        n_cores = max(1, len(self.core_positions))
        per_core_budget = core_budget / n_cores
        for core in self.core_positions:
            core.initial_cash = per_core_budget
            core.cash = per_core_budget

        # 선정 결과 로그 + 텔레그램 알림
        core_lines_log = []
        core_lines_tg  = []
        for i, core in enumerate(self.core_positions):
            tag     = "👤 사용자" if core.ticker in user_tickers else "🤖 AI"
            tag_tg  = "👤사용자" if core.ticker in user_tickers else "🤖AI"
            self.add_log(f"  코어 슬롯 {i+1}: {core.name}({core.ticker}) [{tag}] 예산 {per_core_budget:,.0f}원")
            core_lines_tg.append(f"  · [{tag_tg}] {core.name} {core.ticker}  예산 {per_core_budget:,.0f}원")
        self._send_telegram(
            f"💎 코어 종목 선정 완료  ·  {self.alert_icon} {self.mode_name}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{chr(10).join(core_lines_tg)}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ {_now_kst().strftime('%H:%M KST')}"
        )
        self.last_core_rebalance_date = _now_kst().date()

        self.satellite_positions = {c['ticker']: Position(c['ticker'], c['name'], per_sat) for c in self.satellite_info}
        
        if self.kis:
            real_balance = self.kis.get_account_balance()
            if real_balance and 'stocks' in real_balance:
                for real_stock in real_balance['stocks']:
                    t = real_stock['ticker']; q = int(real_stock['shares']); p = float(real_stock['purchase_price'])
                    for core in self.core_positions:
                        if core.ticker == t:
                            core.shares = q; core.avg_price = p; core.floor_shares = max(1, int(q * self.core_min_floor_ratio)) if q > 0 else 0
                            break
                    if t in self.satellite_positions:
                        self.satellite_positions[t].shares = q; self.satellite_positions[t].avg_price = p
        
        self.last_screen_month = datetime.now().month
        self._save_state()

    def _save_state(self):
        try:
            state = {
                "cores": [{"ticker": c.ticker, "name": c.name, "shares": int(c.shares), "floor_shares": int(c.floor_shares), "cash": float(c.cash), "initial_cash": float(c.initial_cash), "avg_price": float(c.avg_price), "dca_mode": bool(getattr(c, 'dca_mode', False)), "dca_amount": float(getattr(c, 'dca_amount', 0)), "dca_interval_hours": int(getattr(c, 'dca_interval_hours', 72)), "dca_dip_pct": float(getattr(c, 'dca_dip_pct', 3.0)), "last_dca_time": float(getattr(c, 'last_dca_time', 0.0)), "last_order_time": float(getattr(c, 'last_order_time', 0.0)), "second_buy_price": float(getattr(c, 'second_buy_price', 0.0)), "second_buy_cash": float(getattr(c, 'second_buy_cash', 0.0)), "second_buy_done": bool(getattr(c, 'second_buy_done', False))} for c in self.core_positions],
                "satellites": {ticker: {"name": pos.name, "shares": int(pos.shares), "cash": float(pos.cash), "initial_cash": float(pos.initial_cash), "avg_price": float(pos.avg_price), "partial_sold": bool(getattr(pos, 'partial_sold', False)), "partial_sold_2": bool(getattr(pos, 'partial_sold_2', False)), "second_buy_done": bool(getattr(pos, 'second_buy_done', False)), "pyramid_done": bool(getattr(pos, 'pyramid_done', False)), "second_buy_price": float(getattr(pos, 'second_buy_price', 0)), "second_buy_cash": float(getattr(pos, 'second_buy_cash', 0)), "max_price": float(getattr(pos, 'max_price', 0)), "last_order_time": float(getattr(pos, 'last_order_time', 0.0)), "stop_news_checked": bool(getattr(pos, 'stop_news_checked', False)), "swing_acc_count": int(getattr(pos, 'swing_acc_count', 0)), "overext_sell_count": int(getattr(pos, 'overext_sell_count', 0))} for ticker, pos in self.satellite_positions.items()},
                "satellite_info": self.satellite_info, "hot_sectors": self.hot_sectors, "num_satellites": self.num_satellites,
                "last_screen_month": getattr(self, 'last_screen_month', None), "last_screen_date": self.last_screen_date.strftime('%Y-%m-%d') if getattr(self, 'last_screen_date', None) else None,
                "last_core_rebalance_date": self.last_core_rebalance_date.strftime('%Y-%m-%d') if getattr(self, 'last_core_rebalance_date', None) else None,
                "daily_pnl": self.daily_pnl, "daily_report": self.daily_report,
                # 당일 블랙리스트 — 재시작 후에도 AI 거절 종목이 재심사 요청되지 않도록 저장
                "bl_date":              self._bl_date,
                "satellite_rejects":    dict(self._satellite_rejects),
                "monday_swap_plan":     self._monday_swap_plan,
                "weekend_scan_done":    self._weekend_scan_done,
            }
            save_portfolio_state(self.user_id, state, self._is_mock)
        except Exception as e: logger.error(f"[{self.mode_name}] 상태 저장 실패: {e}", exc_info=True)

    def _restore_state(self):
        try:
            state = load_portfolio_state(self.user_id, self._is_mock)
            if not state or not state.get("cores"): return False
            self.add_log(f"🔄 {self.mode_name} 포트폴리오 상태 복구 중...")

            # [BUG-FIX] 복원 기준: user_core_stocks(설정) 우선 → 저장된 거래 데이터(주수/평단/현금) 덮어씌움
            # 이전 방식(저장 상태를 그대로 복원)은 설정 변경 후 재시작 시 변경사항이 사라지는 버그 유발
            saved_core_map = {c["ticker"]: c for c in state.get("cores", [])}
            # user_core_stocks 기준으로 슬롯 재구성 (reload_api_keys와 동일한 로직)
            self.core_positions = []
            user_tickers_seen: set = set()
            for uc in self.user_core_stocks[:2]:
                if not uc.get('ticker') or uc['ticker'] in user_tickers_seen:
                    continue
                t = uc['ticker']
                c = saved_core_map.get(t, {})
                pos = CorePosition(t, uc['name'], initial_cash=c.get("initial_cash", 0))
                pos.shares         = c.get("shares", 0)
                pos.floor_shares   = c.get("floor_shares", 0)
                pos.cash           = c.get("cash", 0)
                pos.avg_price      = c.get("avg_price", 0)
                pos.dca_mode           = bool(uc.get("dca") or c.get("dca_mode", False))
                pos.dca_amount         = float(uc.get("dca_amount") or c.get("dca_amount", 0))
                pos.dca_interval_hours = int(uc.get("dca_hours") or c.get("dca_interval_hours", 72))
                pos.dca_dip_pct        = float(uc.get("dca_dip_pct") or c.get("dca_dip_pct", 3.0))
                pos.last_dca_time      = float(c.get("last_dca_time", 0.0))
                pos.last_order_time    = float(c.get("last_order_time", 0.0))
                pos.second_buy_price   = float(c.get("second_buy_price", 0.0))
                pos.second_buy_cash    = float(c.get("second_buy_cash", 0.0))
                pos.second_buy_done    = bool(c.get("second_buy_done", False))
                self.core_positions.append(pos)
                user_tickers_seen.add(t)
            # 빈 슬롯: 저장된 AI 코어 복원 (user_core_stocks에 없는 것)
            for c in state.get("cores", []):
                t = c["ticker"]
                if t in user_tickers_seen or t == "TBD":
                    continue
                if len(self.core_positions) >= 2:
                    break
                pos = CorePosition(t, c["name"], initial_cash=c.get("initial_cash", 0))
                pos.shares         = c.get("shares", 0)
                pos.floor_shares   = c.get("floor_shares", 0)
                pos.cash           = c.get("cash", 0)
                pos.avg_price      = c.get("avg_price", 0)
                pos.dca_mode           = bool(c.get("dca_mode", False))
                pos.dca_amount         = float(c.get("dca_amount", 0))
                pos.dca_interval_hours = int(c.get("dca_interval_hours", 72))
                pos.dca_dip_pct        = float(c.get("dca_dip_pct", 3.0))
                pos.last_dca_time      = float(c.get("last_dca_time", 0.0))
                pos.last_order_time    = float(c.get("last_order_time", 0.0))
                pos.second_buy_price   = float(c.get("second_buy_price", 0.0))
                pos.second_buy_cash    = float(c.get("second_buy_cash", 0.0))
                pos.second_buy_done    = bool(c.get("second_buy_done", False))
                self.core_positions.append(pos)
                user_tickers_seen.add(t)
            # 나머지 빈 슬롯 TBD 채움
            while len(self.core_positions) < 2:
                ph = CorePosition("TBD", f"AI선정대기#{len(self.core_positions)+1}", initial_cash=0)
                ph.status = "AI 선정 대기 🤖"
                self.core_positions.append(ph)
            _user_sat_tickers = {s['ticker'] for s in self.user_satellite_stocks if s.get('ticker')}
            self.satellite_positions = {}
            for ticker, s in state["satellites"].items():
                # [BUG-FIX] KR 봇 상태 파일에 US 종목(알파벳 티커)이 섞이는 버그 방어
                # KR 주식 티커는 반드시 6자리 숫자 — 알파벳 티커(MRVL, ARM 등)는 US 봇 전용
                if not (ticker.isdigit() and len(ticker) == 6):
                    logger.warning(f"[KR봇] 상태 복구 중 비KR 티커 무시: {ticker} (US 봇 종목 혼입 방지)")
                    continue
                # [BUG-FIX] 보유주 없고 사용자지정도 아닌 종목(스크리너 선정) → ghost 복구 방지
                # 재시작 시 치웠던 위성 종목이 다시 딸려오는 버그 수정
                if int(s.get("shares", 0)) == 0 and ticker not in _user_sat_tickers:
                    continue
                pos = Position(ticker, s["name"], s.get("initial_cash", 1400000))
                pos.shares = s["shares"]; pos.cash = s["cash"]; pos.avg_price = s.get("avg_price", 0)
                pos.partial_sold     = bool(s.get("partial_sold",     False))
                pos.partial_sold_2   = bool(s.get("partial_sold_2",   False))
                pos.second_buy_done  = bool(s.get("second_buy_done",  False))
                pos.pyramid_done     = bool(s.get("pyramid_done",     False))
                pos.second_buy_price = float(s.get("second_buy_price", 0))
                pos.second_buy_cash  = float(s.get("second_buy_cash",  0))
                pos.max_price          = float(s.get("max_price",          0))  # W-04: 트레일링 스탑 기준가 복원
                pos.last_order_time    = float(s.get("last_order_time",   0.0))
                pos.stop_news_checked  = bool(s.get("stop_news_checked",  False))
                pos.swing_acc_count    = int(s.get("swing_acc_count",     0))
                pos.overext_sell_count = int(s.get("overext_sell_count",  0))
                self.satellite_positions[ticker] = pos

            # [BUG-FIX] satellite_info 비KR 티커 제거
            # + 사용자지정 or 보유 중인 종목만 복원 (스크리너 ghost 방지)
            _restored_sat_tickers = set(self.satellite_positions.keys())
            self.satellite_info = [c for c in state.get("satellite_info", [])
                                   if c.get('ticker','').isdigit() and len(c.get('ticker','')) == 6
                                   and (c.get('ticker') in _restored_sat_tickers
                                        or c.get('ticker') in _user_sat_tickers)]
            self.hot_sectors = state.get("hot_sectors", [])
            self.num_satellites = min(3, state.get("num_satellites", 3))  # 최대 3개 강제
            self.last_screen_month = state.get("last_screen_month")
            lsd_str = state.get("last_screen_date")
            self.last_screen_date = datetime.strptime(lsd_str, '%Y-%m-%d').date() if lsd_str else None
            lcr_str = state.get("last_core_rebalance_date")
            self.last_core_rebalance_date = datetime.strptime(lcr_str, '%Y-%m-%d').date() if lcr_str else None
            self.daily_pnl = state.get("daily_pnl", {})
            self.daily_report = state.get("daily_report", None)
            # 당일 블랙리스트 복원 — 저장된 날짜와 오늘이 같을 때만 적용 (자정 넘기면 무효)
            saved_bl_date = state.get("bl_date", "")
            today_str     = _now_kst().strftime('%Y-%m-%d')
            if saved_bl_date == today_str:
                self._bl_date             = saved_bl_date
                self._satellite_rejects   = state.get("satellite_rejects",   {})
                n_rej = len(self._satellite_rejects)
                if n_rej:
                    self.add_log(f"🚫 당일 AI 거절 블랙리스트 복원: 위성 {n_rej}개 재심사 제외")
            # 주말 스캔 계획 복원
            self._monday_swap_plan  = state.get("monday_swap_plan", {})
            self._weekend_scan_done = state.get("weekend_scan_done", "")
            if self._monday_swap_plan:
                self.add_log(f"📅 주말 교체 계획 복원: {len(self._monday_swap_plan)}건 대기 중")

            # satellite_info에 선정된 종목 중 positions에 없는 것 → 빈 포지션 생성
            # BUG-FIX: 사용자지정 종목만 추가 (스크리너 선정 ghost 방지)
            # 스크리너 종목은 다음 screener 실행 시 자연스럽게 재선정됨
            _existing_tickers = set(self.satellite_positions.keys())
            for _sat in self.satellite_info:
                _t = _sat.get('ticker')
                if _t and _t not in _existing_tickers and _t in _user_sat_tickers:
                    self.satellite_positions[_t] = Position(_t, _sat.get('name', _t), 0.0)
                    _existing_tickers.add(_t)

            return True
        except Exception as e:
            logger.error(f"[{self.mode_name}] 상태 복구 실패: {e}", exc_info=True)
            return False

    def _update_market_regime(self) -> str:
        """
        시장 국면을 1시간 간격으로 갱신.
        KOSPI200 ETF(069500) 이중 이동평균(20/60일) 배열로 판단.
        국면 변경 시 텔레그램 알림 발송.
        """
        if not self.kis:
            return self.market_regime
        if time.time() - self.last_regime_check < self._regime_check_interval:
            return self.market_regime
        try:
            prev   = self.market_regime
            detail = get_market_regime_detail(self.kis)
            self.last_regime_check = time.time()

            # ── 외부 선행 신호 수집 (EWY, NQ선물, USD/KRW) ──────────────────
            ewy_change = nq_change = usd_krw_change = 0.0
            try:
                import yfinance as yf
                for sym, attr in [("EWY","ewy_change"),("NQ=F","nq_change")]:
                    df = yf.download(sym, period="3d", interval="1d", progress=False, auto_adjust=True)
                    if not df.empty and len(df) >= 2:
                        c0, c1 = float(df["Close"].iloc[-2]), float(df["Close"].iloc[-1])
                        if attr == "ewy_change": ewy_change = (c1/c0-1)*100
                        else: nq_change = (c1/c0-1)*100
                # USD/KRW: 간단히 UUP ETF 사용 (달러인덱스 프록시)
                df_uup = yf.download("UUP", period="3d", interval="1d", progress=False, auto_adjust=True)
                if not df_uup.empty and len(df_uup) >= 2:
                    c0, c1 = float(df_uup["Close"].iloc[-2]), float(df_uup["Close"].iloc[-1])
                    usd_krw_change = (c1/c0-1)*100
            except Exception as fx_err:
                logger.debug(f"[{self.mode_name}] 외부 신호 수집 실패: {fx_err}")

            # ── AI 하이브리드 판단 (Claude 있을 때만) ────────────────────────
            ai_result = None
            if self.claude:
                try:
                    ai_result = self.claude.ai_kr_market_context(
                        rule_score      = detail['score'],
                        kospi_regime    = detail['regime'],
                        ewy_change      = ewy_change,
                        nq_change       = nq_change,
                        usd_krw_change  = usd_krw_change,
                        kospi_rsi       = detail['rsi'],
                    )
                    # AI 판단으로 국면 결정
                    self.market_regime = ai_result['regime']
                    # 진입 보너스 저장 (entry score에서 활용)
                    self._ai_market_entry_bonus = ai_result.get('entry_bonus', 0)
                    self.add_log(
                        f"🤖 [AI 시장판단] {detail['regime']}(규칙) → {ai_result['regime']}(AI) "
                        f"| EWY{ewy_change:+.1f}% NQ{nq_change:+.1f}% USD{usd_krw_change:+.1f}% "
                        f"| 진입보너스 {ai_result['entry_bonus']:+d}pt | {ai_result['reason']}"
                    )
                except Exception as ai_err:
                    logger.debug(f"[{self.mode_name}] AI 시장판단 실패: {ai_err}")
                    self.market_regime = detail['regime']
                    self._ai_market_entry_bonus = 0
            else:
                self.market_regime = detail['regime']
                self._ai_market_entry_bonus = 0

            # ── 매번 현재 국면 진단 로그 (ADX·연속일·점수 포함) ──────────────
            adx_str    = f"ADX={detail['adx']:.1f}"
            streak_str = f"연속상승{detail['up_streak']}일"
            rsi_str    = f"RSI={detail['rsi']:.1f}"
            score_str  = f"점수{detail['score']:+d}"
            diag_line  = f"{score_str} | {rsi_str} | {adx_str} | {streak_str} | 22일수익{detail['ret22']:+.1f}%"

            if detail['downgrade_reason']:
                self.add_log(f"⚠️ [{self.mode_name}] {detail['downgrade_reason']} | {diag_line}")

            if self.market_regime != prev:
                # BEAR 전환 시 보유 위성 30% 즉시 손절 (방치 방지)
                if self.market_regime == "BEAR" and self.kis:
                    with self.lock:
                        bear_sat_items = [(t, p) for t, p in self.satellite_positions.items() if p.shares > 0]
                    for _bt, _bp in bear_sat_items:
                        try:
                            _bprice = self.live_prices.get(_bt) or self.kis.get_current_price(_bt) or 0
                            _bqty   = max(1, int(_bp.shares * 0.30))
                            if _bprice > 0 and _bqty > 0 and self._sell_order(_bt, _bqty, _bp, _bp.name):
                                with self.lock:
                                    _bp.shares = max(0, _bp.shares - _bqty)
                                    _bp.status = "BEAR전환 30%손절 🐻"
                                _profit = _net_profit(_bprice, _bp.avg_price, _bqty)
                                with self.lock:
                                    self.pnl_this_turn += _profit
                                self._record_daily_pnl(_profit)
                                self.add_log(f"🐻 [BEAR전환] {_bp.name}({_bt}) 30% 손절 {_bqty}주 @ {_bprice:,.0f}원")
                        except Exception as _be:
                            logger.warning(f"[BEAR전환 손절] {_bt} 오류: {_be}")

                icons = {"BULL": "🐂", "BEAR": "🐻", "NEUTRAL": "😐"}
                log_regime_desc = {
                    'BEAR':    '📉 위성 30% 즉시 손절, 신규 매수 중단, 인버스 ETF 진입',
                    'BULL':    '📈 BULL 매매 모드 — 불타기·눌림목 전략 활성화',
                    'NEUTRAL': '📊 혼조 — 기존 전략 유지',
                }
                tg_regime_desc = {
                    'BEAR':    '위성 신규 매수 중단\n인버스 ETF 자동 진입',
                    'BULL':    'BULL 매매 모드 재개\n불타기 · 눌림목 전략 활성화',
                    'NEUTRAL': '혼조장 — 기존 전략 유지',
                }
                self.add_log(
                    f"{icons.get(self.market_regime,'📊')} [{self.mode_name}] "
                    f"시장 국면 변경: {prev} → {self.market_regime}  "
                    f"{log_regime_desc.get(self.market_regime,'')} | {diag_line}"
                )
                _dg = detail['downgrade_reason']
                self._send_telegram(
                    f"{icons.get(self.market_regime,'📊')} <b>시장 국면 변경</b>  ·  {self.alert_icon} {self.mode_name}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📊 <b>{prev}</b>  →  <b>{self.market_regime}</b>\n"
                    f"📋 {tg_regime_desc.get(self.market_regime,'')}\n"
                    + (f"⚠️ {_dg}\n" if _dg else "")
                    + f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📈 {score_str}  |  {adx_str}  |  {streak_str}\n"
                    f"📉 {rsi_str}  |  22일수익 {detail['ret22']:+.1f}%\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"⏰ {_now_kst().strftime('%H:%M KST')}"
                )
        except Exception as e:
            logger.error(f"[{self.mode_name}] 시장 국면 판단 오류: {e}", exc_info=True)
        return self.market_regime

    def _handle_defensive_assets(self, regime: str):
        """
        BEAR 국면: DEFENSIVE_ASSETS 3종 자동 매수 (인버스 20%, 달러 13%, 금 7% = 총 40%).
        BULL/NEUTRAL 국면: 보유 중이면 각 자산 전량 청산.
        종목별 독립 24h 재매수 쿨다운 (휩쏘 방지).
        5분마다 한 번만 실행 (매분 API 호출 방지).
        """
        if not self.kis:
            return
        if time.time() - self._last_defensive_check < 300:  # 5분 캐시
            return
        self._last_defensive_check = time.time()
        try:
            balance = self.kis.get_account_balance()
            if not balance:
                return

            total_cash   = float(balance.get('total_cash', 0))
            total_value  = float(balance.get('total_value', 0))
            total_assets = total_cash + total_value
            stocks       = balance.get('stocks', [])

            for asset in DEFENSIVE_ASSETS:
                ticker     = asset['ticker']
                name       = asset['name']
                ratio      = asset['ratio']
                emoji      = asset['emoji']
                cd_key     = f"_def_sold_{ticker}"   # 종목별 쿨다운 키

                holding    = next((s for s in stocks if s.get('ticker') == ticker), None)
                has_pos    = holding and int(holding.get('shares', 0)) > 0
                shares_held = int(holding.get('shares', 0)) if holding else 0

                if regime == "BEAR" and not has_pos:
                    # 휩쏘 방지: 청산 후 24h 이내 재매수 금지
                    sold_ts = self._defensive_sold_ts.get(ticker, 0.0)
                    cooldown_remaining = 86400 - (time.time() - sold_ts)
                    if sold_ts > 0 and cooldown_remaining > 0:
                        self.add_log(f"⏳ {name} 재매수 쿨다운 중 ({cooldown_remaining/3600:.1f}h 남음) — 휩쏘 방지")
                        continue

                    # BEAR 방어헤지: ratio 그대로 사용 (인버스 20% + 달러 13% + 금 7% = 40%)
                    # 나머지 60%는 현금 보유 → 저점매수(bear_bottom_score) 탄약
                    budget = int(total_assets * ratio)
                    price  = self.kis.get_current_price(ticker)
                    if price and price > 0:
                        qty = int(budget // price)
                        if qty > 0 and total_cash >= qty * price * 1.002:
                            if self.kis.buy_market_order(ticker, qty):  # [BUG-FIX] 반환값 확인
                                total_cash -= qty * price  # 현금 차감 (다음 종목 계산용)
                                self.add_log(f"🐻 하락장 방어 매수 | {emoji} {name} {qty}주 @ {price:,.0f}원")
                                self._log_trade(ticker, name, 'BUY', price, "방어자산", f"BEAR 국면 총자산 {ratio*100:.0f}% 헤지")
                                self._send_telegram(
                                    f"🐻 <b>방어 자산 매수</b>  ·  {self.alert_icon} {self.mode_name}\n"
                                    f"━━━━━━━━━━━━━━━━━━━━\n"
                                    f"{emoji} <b>{name}</b>  <code>{ticker}</code>\n"
                                    f"💰 <b>{price:,.0f}원</b> × <b>{qty}주</b>  =  <b>{qty*price:,.0f}원</b>\n"
                                    f"📋 BEAR 국면  ·  총자산 {ratio*100:.0f}% 헤지 (방어40% + 저점대기60%)\n"
                                    f"━━━━━━━━━━━━━━━━━━━━\n"
                                    f"⏰ {_now_kst().strftime('%H:%M KST')}",
                                    msg_type='trade'
                                )

                elif regime != "BEAR" and has_pos and shares_held > 0:
                    if self.kis.sell_market_order(ticker, shares_held):  # [BUG-FIX] 반환값 확인
                        self._defensive_sold_ts[ticker] = time.time()  # 종목별 24h 쿨다운 시작
                        price = self.kis.get_current_price(ticker) or 0
                        def_profit = _net_profit(price, float(holding.get('purchase_price', price)), shares_held) if holding else 0
                        # [C-05] 방어 자산 청산 손익을 장부에 반영
                        with self.lock:
                            self.pnl_this_turn += def_profit
                        self._record_daily_pnl(def_profit)
                        self.add_log(f"🐂 국면 전환({regime}) → {emoji} {name} {shares_held}주 전량 청산 (24h 재매수 대기)")
                        self._log_trade(ticker, name, 'SELL', price, "방어자산", f"국면 전환 BEAR→{regime}", profit=def_profit)
                        self._send_telegram(
                            f"🐂 <b>방어 자산 청산</b>  ·  {self.alert_icon} {self.mode_name}\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"{emoji} <b>{name}</b>  <code>{ticker}</code>\n"
                            f"💰 <b>{shares_held}주</b> 전량 청산\n"
                            f"📋 국면 전환: BEAR → <b>{regime}</b>  ·  헤지 해제\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"⏰ {_now_kst().strftime('%H:%M KST')}",
                            msg_type='trade'
                        )

        except Exception as e:
            logger.error(f"[{self.mode_name}] 방어 자산 처리 오류: {e}", exc_info=True)

    def _check_etf_market_positive(self) -> bool:
        """시장 대표 ETF(KOSPI200·KOSDAQ150) 전일 대비율이 모두 -1% 이상이면 매수 허용."""
        if not self.kis:
            return True
        # 모의투자는 ETF API 미지원 → 항상 허용
        if getattr(self, '_is_mock', False):
            return True
        try:
            threshold = -1.0
            for etf_code, _ in self.market_indices:
                info = self.kis.get_etf_price(etf_code)
                if info and info.get("prdy_ctrt", 0) < threshold:
                    return False
            return True
        except Exception:
            return True  # 조회 실패 시 매수 차단하지 않음

    def _build_trade_context(self, ticker: str, stock_name: str, price: float,
                              ex_df: 'pd.DataFrame', strategy: str, regime: str) -> str:
        """AI에게 전달할 종합 분석 컨텍스트를 빌드합니다 (뉴스·재무·기술적 지표·분봉)."""
        lines = []

        # ── 1. 뉴스 + 공시 (NewsMonitor 우선, 없으면 기존 크롤러 사용) ──
        if self.news_monitor:
            try:
                naver_news = self.news_monitor.get_news_summary(stock_name, display=5)
                dart_disc  = self.news_monitor.get_disclosure_summary(ticker, days=5)
                if naver_news:
                    lines.append(naver_news)
                if dart_disc:
                    lines.append(f"[DART 공시]\n{dart_disc}")
            except Exception as ne:
                logger.warning(f"[{self.mode_name}] NewsMonitor 컨텍스트 조회 실패: {ne}")
        else:
            try:
                news = fetch_recent_news(stock_name)
                # "뉴스 조회 실패" 텍스트는 AI에 전달 금지 — 악재 오인 방지
                if "조회 실패" in news:
                    news = ""
            except Exception:
                news = ""
            if news:
                lines.append(f"[최근 뉴스] {news}")

        # ── 2. 재무지표 (PER·PBR·ROE — yfinance .info, 일 1회 캐싱) ──
        fundamental = self._fetch_fundamental(ticker, stock_name)
        if fundamental:
            lines.append(f"[재무지표] {fundamental}")

        # ── 3. 기술적 지표 (ex_df 기반) ─────────────────────────
        if ex_df is not None and not ex_df.empty and 'close' in ex_df.columns:
            from KR.strategy import calc_rsi
            close = ex_df['close'].dropna()
            vol   = ex_df['volume'].dropna() if 'volume' in ex_df.columns else pd.Series(dtype=float)

            # RSI(14)
            rsi_val = None
            if len(close) >= 16:
                try:
                    rsi_val = round(float(calc_rsi(close, 14).iloc[-1]), 1)
                except Exception:
                    pass

            # MACD (12/26/9)
            macd_str = "N/A"
            if len(close) >= 30:
                try:
                    ema12 = close.ewm(span=12, adjust=False).mean()
                    ema26 = close.ewm(span=26, adjust=False).mean()
                    macd_line = ema12 - ema26
                    signal_line = macd_line.ewm(span=9, adjust=False).mean()
                    macd_hist = macd_line.iloc[-1] - signal_line.iloc[-1]
                    macd_str = f"MACD {macd_line.iloc[-1]:+.2f} / 시그널 {signal_line.iloc[-1]:+.2f} / 히스토그램 {macd_hist:+.2f} ({'골든크로스↑' if macd_hist > 0 else '데드크로스↓'})"
                except Exception:
                    pass

            # 볼린저밴드 (20일, 2σ)
            bb_str = "N/A"
            if len(close) >= 22:
                try:
                    sma20 = close.rolling(20).mean().iloc[-1]
                    std20 = close.rolling(20).std().iloc[-1]
                    bb_upper = sma20 + 2 * std20
                    bb_lower = sma20 - 2 * std20
                    bb_pct = (price - bb_lower) / (bb_upper - bb_lower + 1e-9) * 100
                    if bb_pct >= 95:
                        bb_pos = f"상단 돌파 (과열 {bb_pct:.0f}%)"
                    elif bb_pct <= 5:
                        bb_pos = f"하단 터치 (과매도 {bb_pct:.0f}%)"
                    else:
                        bb_pos = f"밴드 내 {bb_pct:.0f}% 위치"
                    bb_str = f"상단 {bb_upper:,.0f} / 중간 {sma20:,.0f} / 하단 {bb_lower:,.0f} → {bb_pos}"
                except Exception:
                    pass

            # 거래량 비율 (오늘 vs 20일 평균)
            vol_str = "N/A"
            if len(vol) >= 2:
                try:
                    vol_avg20 = float(vol.iloc[:-1].rolling(20, min_periods=5).mean().iloc[-1])
                    vol_today = float(vol.iloc[-1])
                    vol_ratio = vol_today / (vol_avg20 + 1) * 100
                    vol_str = f"평소 대비 {vol_ratio:.0f}% ({'급증↑↑' if vol_ratio > 200 else '증가↑' if vol_ratio > 130 else '정상✅' if vol_ratio >= 100 else '보통' if vol_ratio > 70 else '감소↓'})"
                except Exception:
                    pass

            # 최근 5일 종가 추이
            price_hist = ""
            if len(close) >= 5:
                try:
                    last5 = close.tail(5).tolist()
                    price_hist = " → ".join(f"{int(p):,}" for p in last5) + "원"
                except Exception:
                    pass

            # 전일 종가 & 당일 등락률
            prev_close_str = "N/A"
            day_chg_str    = "N/A"
            if len(close) >= 2:
                try:
                    prev_close  = float(close.iloc[-2])
                    day_chg_pct = (price / prev_close - 1) * 100
                    prev_close_str = f"{prev_close:,.0f}원"
                    recov_tag  = "✅ 전일 종가 위" if price >= prev_close else "❌ 전일 종가 미회복"
                    day_chg_str = f"{day_chg_pct:+.1f}% ({recov_tag})"
                except Exception:
                    pass

            # 5일선 위치
            sma5_str = "N/A"
            if len(close) >= 6:
                try:
                    sma5    = float(close.rolling(5).mean().iloc[-1])
                    rel_sma5 = (price / sma5 - 1) * 100
                    sma5_str = f"{sma5:,.0f}원 ({rel_sma5:+.1f}% {'위↑' if rel_sma5 >= 0 else '아래↓'})"
                except Exception:
                    pass

            # 20일선 위치
            sma20_str = "N/A"
            if len(close) >= 22:
                try:
                    sma20    = float(close.rolling(20).mean().iloc[-1])
                    rel_sma20 = (price / sma20 - 1) * 100
                    sma20_str = f"{sma20:,.0f}원 ({rel_sma20:+.1f}% {'위↑' if rel_sma20 >= 0 else '아래↓'})"
                except Exception:
                    pass

            # 120일선 위치
            sma120_str = "N/A"
            if len(close) >= 60:
                try:
                    sma120 = float(close.rolling(120, min_periods=60).mean().iloc[-1])
                    rel = (price / sma120 - 1) * 100
                    sma120_str = f"{sma120:,.0f}원 ({rel:+.1f}% {'위↑ 정배열' if rel >= 0 else '아래↓ 역배열'})"
                except Exception:
                    pass

            lines.append(
                f"[기술 지표] RSI(14): {rsi_val if rsi_val is not None else 'N/A'} | {macd_str} | "
                f"볼린저밴드: {bb_str} | 거래량: {vol_str} | 120일선: {sma120_str}"
            )
            lines.append(
                f"[이동평균] 5일선: {sma5_str} | 20일선: {sma20_str}"
            )
            lines.append(
                f"[전일종가] {prev_close_str} | 당일 등락: {day_chg_str}"
            )
            if price_hist:
                lines.append(f"[최근 5일 종가] {price_hist}")

        # ── 4. 분봉 추세 ─────────────────────────────────────────
        try:
            if self.kis:
                candles = self.kis.get_minute_candles(ticker, count=5)
                if candles and len(candles) >= 3:
                    c_prices = [c["close"] for c in candles if c["close"] > 0]
                    if c_prices:
                        trend = "상승 추세 ↑" if c_prices[-1] > c_prices[0] else "하락 추세 ↓"
                        lines.append(f"[분봉 추세] 최근 5분봉: {trend} (시작 {c_prices[0]:,} → 현재 {c_prices[-1]:,})")
        except Exception:
            pass

        # ── 5. 외국계 순매수 실시간 조회 [국내주식-164] ──────────────
        frgn_inst_str = "N/A"
        try:
            if self.kis and hasattr(self.kis, 'get_foreign_buy_by_ticker'):
                fi = self.kis.get_foreign_buy_by_ticker(ticker)
                if fi is not None:
                    net  = fi["frgn_net"]
                    buy  = fi["frgn_buy"]
                    sell = fi["frgn_sell"]
                    tag  = "✅ 순매수" if net > 0 else ("❌ 순매도" if net < 0 else "➖ 중립")
                    frgn_inst_str = (
                        f"{tag}  순매수 {net:+,}주  "
                        f"(매수 {buy:,}주 / 매도 {sell:,}주)"
                    )
        except Exception:
            pass
        lines.append(f"[외국계 수급] {frgn_inst_str}")

        # ── 6. KOSPI / KOSDAQ 대비 상대강도 ─────────────────────────
        market_rs_str = "N/A"
        try:
            if self.kis and ex_df is not None and not ex_df.empty and 'close' in ex_df.columns:
                close_s = ex_df['close'].dropna()
                if len(close_s) >= 2:
                    stock_chg = (float(price) / float(close_s.iloc[-2]) - 1) * 100
                    parts = []
                    for etf_code, idx_name in [("069500", "KOSPI"), ("229200", "KOSDAQ")]:
                        try:
                            _etf = self.kis.get_etf_price(etf_code)
                            if _etf and "prdy_ctrt" in _etf:
                                idx_chg = float(_etf["prdy_ctrt"])
                                rs = stock_chg - idx_chg
                                tag = "↑ 아웃퍼폼" if rs > 0 else "↓ 언더퍼폼"
                                parts.append(f"{idx_name} {idx_chg:+.1f}% (RS {rs:+.1f}% {tag})")
                        except Exception:
                            pass
                    if parts:
                        market_rs_str = f"종목 {stock_chg:+.1f}% | " + " / ".join(parts)
        except Exception:
            pass
        lines.append(f"[시장 상대강도] {market_rs_str}")

        # ── 7. 시장 국면 & 전략 ──────────────────────────────────
        lines.append(f"[시장 국면] {regime} | 적용 전략: {strategy}")
        if self.hot_sectors:
            lines.append(f"[강세 섹터] {', '.join(self.hot_sectors[:5])}")

        return "\n".join(lines)

    def _fetch_fundamental(self, ticker: str, stock_name: str) -> str:
        """yfinance .info로 PER·PBR·ROE를 조회하고 오늘 날짜 키로 캐싱 (일 1회).
        반환: "PER 15.3x | PBR 2.1x | ROE 12.5%" 형태 문자열, 실패 시 ""
        """
        today_str = _now_kst().strftime('%Y-%m-%d')
        cache_key = f"{ticker}_{today_str}"
        if cache_key in self.fundamental_cache:
            return self.fundamental_cache[cache_key]
        try:
            import yfinance as yf
            yfk = ticker + ".KS" if not ticker.endswith((".KS", ".KQ")) else ticker
            info = yf.Ticker(yfk).info
            parts = []
            pe  = info.get("trailingPE")
            pb  = info.get("priceToBook")
            roe = info.get("returnOnEquity")
            if pe  and pe  > 0:  parts.append(f"PER {pe:.1f}x")
            if pb  and pb  > 0:  parts.append(f"PBR {pb:.2f}x")
            if roe and roe != 0: parts.append(f"ROE {roe*100:.1f}%")
            result = " | ".join(parts) if parts else ""
            self.fundamental_cache[cache_key] = result
        except Exception:
            result = ""
            self.fundamental_cache[cache_key] = result
        return result

    def _check_minute_trend_up(self, ticker: str) -> bool:
        """최근 5개 분봉 종가 기울기가 양수(상승 추세)이면 True."""
        if not self.kis:
            return True
        try:
            candles = self.kis.get_minute_candles(ticker, count=5)
            if len(candles) < 3:
                return True  # 데이터 부족 시 차단하지 않음
            closes = [c["close"] for c in candles if c["close"] > 0]
            if len(closes) < 3:
                return True
            # 단순 선형 기울기: 마지막 값이 첫 값보다 높으면 상승
            return closes[-1] >= closes[0]
        except Exception:
            return True

    def trading_job(self):
        # ── 중복 실행 방지 ─────────────────────────────────────────────
        # trading_job이 60초 이상 걸리면 schedule 라이브러리가 "늦었다"고 판단해
        # run_pending() 호출마다 (1초마다) 즉시 재실행하는 버그 방지.
        if getattr(self, '_trading_job_running', False):
            return
        self._trading_job_running = True
        self._trading_job_start_ts = time.time()
        try:
            self._trading_job_impl()
        finally:
            self._trading_job_running = False
            self._trading_job_start_ts = 0

    def _trading_job_impl(self):
        if not self.core_positions: return
        now = _now_kst()  # EC2(UTC) 환경에서도 KST 기준으로 장 시간 판단
        if now.weekday() >= 5: return
        current_time_str = now.strftime('%H:%M')
        today_str        = now.strftime('%Y-%m-%d')
        # [BUG-N2] NXT 애프터마켓 종료(20:00)에 맞게 확장 — 15:30~20:00 구간도 매매 허용
        # 15:15~16:00: 장 마감 전후 변동성 구간 — 매매 정지 (모니터링만)
        _is_pause = ("15:15" <= current_time_str < "16:00")
        is_golden_hours = ("09:01" <= current_time_str <= "20:00") and not _is_pause

        # ── KST 기준 일일 리포트 발행 (시스템 타임존 무관) ──────────────
        # 리포트가 아직 생성 안 됐고 Claude API 설정 있을 때만 실행
        if self.claude:
            for slot_time in ['15:40']:
                if current_time_str == slot_time:
                    dr = self.daily_report
                    already = (isinstance(dr, dict) and dr.get('date') == today_str
                               and dr.get(slot_time) is not None)
                    if not already:
                        self._run_threaded(lambda t=slot_time: self.generate_daily_report(t))
                    break

        
        if not is_golden_hours:
            with self.lock:
                if _is_pause:
                    _pause_msg = "매매 정지 구간 (15:15~16:00) ⏸️"
                    _pause_detail = "장 마감 전후 변동성 구간 — 16:00 이후 재개"
                    for core in self.core_positions: core.status = _pause_msg; core.status_msg = _pause_detail
                    for sat in self.satellite_positions.values(): sat.status = _pause_msg; sat.status_msg = _pause_detail
                else:
                    for core in self.core_positions: core.status = "휴식 중 💤"; core.status_msg = "정규 장 및 대체거래소 마감"
                    for sat in self.satellite_positions.values(): sat.status = "휴식 중 💤"; sat.status_msg = "정규 장 및 대체거래소 마감"
            return  # W-08: 장외 시간엔 나머지 로직 스킵 (불필요한 API 호출 방지)
        else:
            self.add_log(f"--- 🎯 {self.mode_name} 실시간 점검 ({current_time_str}) ---")
            with self.lock:
                _regime_now = getattr(self, 'market_regime', 'NEUTRAL')
                _regime_label = {"BULL": "상승장 🚀", "BEAR": "하락장 🐻", "NEUTRAL": "횡보장 ➡️"}.get(_regime_now, "분석 중")
                for core in self.core_positions:
                    if "대기" not in core.status and "심사" not in core.status:
                        if core.shares > 0:
                            _cp = getattr(core, 'kis_current_price', 0) or self.live_prices.get(core.ticker, 0)
                            _pnl = ((_cp - core.avg_price) / core.avg_price * 100) if core.avg_price > 0 and _cp > 0 else 0
                            core.status = "보유 중 💎"
                            core.status_msg = f"{core.shares}주 보유 중 | 평단 {core.avg_price:,.0f}원 | 수익률 {_pnl:+.1f}% | {_regime_label}"
                        elif core.cash > 0:
                            core.status = "감시 중 👀"
                            core.status_msg = f"진입점수 확인 중 | 가용 예산 {core.cash:,.0f}원 | 시장: {_regime_label}"
                        else:
                            core.status = "감시 중 👀"
                            core.status_msg = f"예산 소진 — 다음 잔고 동기화 대기 중 | 시장: {_regime_label}"

                for sat in self.satellite_positions.values():
                    if "대기" not in sat.status and "심사" not in sat.status:
                        if sat.shares > 0:
                            _sp = getattr(sat, 'kis_current_price', 0) or self.live_prices.get(sat.ticker, 0)
                            _pnl = ((_sp - sat.avg_price) / sat.avg_price * 100) if sat.avg_price > 0 and _sp > 0 else 0
                            sat.status = "보유 중 ✅"
                            sat.status_msg = f"{sat.shares}주 보유 중 | 평단 {sat.avg_price:,.0f}원 | 수익률 {_pnl:+.1f}% | {_regime_label}"
                        elif sat.cash > 0:
                            sat.status = "감시 중 👀"
                            sat.status_msg = f"신호 대기 | 예산 {sat.cash:,.0f}원 | 시장: {_regime_label}"
                        else:
                            sat.status = "감시 중 👀"
                            sat.status_msg = f"예산 소진 — 다음 종목 교체 대기 | 시장: {_regime_label}"

        # ── 📡 뉴스 모니터: 악재 공시 감지 + 실적 발표 예정 체크 ───────────
        if self.news_monitor and is_golden_hours:
            self._run_threaded(self._check_news_alerts)

        # C-01: is_crisis_mode 체크를 else 블록 밖으로 이동
        # → 장중(golden hours)이 아닐 때도 위기 모드가 유지되며,
        #   장이 열리면 반등 여부를 체크하고, 그 전까지는 매매 전체 차단
        if getattr(self, 'is_crisis_mode', False):
            if is_golden_hours and self.kis:
                main_idx_ticker = self.market_indices[0][0]
                idx_cp = self.kis.get_current_price(main_idx_ticker)
                if idx_cp:
                    extended_df = self._get_extended_ohlcv(main_idx_ticker, idx_cp)
                    if not extended_df.empty and len(extended_df) >= 5:
                        if idx_cp > extended_df['close'].ewm(span=5, adjust=False).mean().iloc[-1]:
                            msg = f"🚀 {self.mode_name} 저점 반등 확인! 관망 모드 해제."
                            self.add_log(msg); self._send_telegram(msg)
                            self.is_crisis_mode = False; self.peak_total_asset = 0
            if getattr(self, 'is_crisis_mode', False):  # 해제 안 됐으면 조기 종료
                return

        # 시장 국면 갱신 (1시간 캐시) + 방어 자산(인버스·달러·금 ETF) 자동 관리
        regime = self._update_market_regime()
        if is_golden_hours:
            self._handle_defensive_assets(regime)

        if self.kis:
            try:
                real_balance = self.kis.get_account_balance()
                if real_balance and 'stocks' in real_balance:
                    self._sync_internal_balances(real_balance)
                    current_total_asset = float(real_balance.get('total_cash', 0)) + float(real_balance.get('total_value', 0))
                    if not hasattr(self, 'peak_total_asset'): self.peak_total_asset = current_total_asset
                    elif current_total_asset > self.peak_total_asset: self.peak_total_asset = current_total_asset
                        
                    if getattr(self, 'peak_total_asset', 0) > 0 and ((current_total_asset / self.peak_total_asset) - 1) * 100 <= -10.0:
                        msg = f"💥 [서킷브레이커] {self.mode_name} 계좌 MDD 10% 폭락! 전량 시장가 강제 청산."
                        self.add_log(msg); self._send_telegram(msg)
                        # trading_job과의 race condition 방지: 먼저 crisis_mode를 세운 뒤 청산
                        # (is_crisis_mode=True이면 trading_job이 즉시 return하여 중복 매도 방지)
                        self.is_crisis_mode = True
                        with self.lock:
                            safe_core_positions = list(self.core_positions)
                            safe_satellite_items = list(self.satellite_positions.items())
                        for core in safe_core_positions:
                            if core.shares > 0:
                                self.kis.sell_market_order(core.ticker, core.shares)
                                with self.lock:
                                    core.shares = 0  # [C-NEW-03] 서킷브레이커 청산 후 잔여주수 초기화
                                self.add_log(f"🔥 {self.mode_name} 코어 {core.name} 청산")
                        for ticker, pos in safe_satellite_items:
                            if pos.shares > 0:
                                self.kis.sell_market_order(ticker, pos.shares)
                                with self.lock:
                                    self._sat_exit_reset(pos)   # 서킷브레이커 전량 청산
                                self.add_log(f"🔥 {self.mode_name} 위성 {pos.name} 청산")
                        return
            except Exception as e:
                logger.error(f"[{self.mode_name}] 서킷브레이커 잔고 조회 오류: {e}", exc_info=True)

        # ── 예수금 입금 감지 → DCA 트리거 ──────────────────────────────
        # 매매가 없는 상황에서 예수금이 200,000원 이상 증가 시 입금으로 판단
        # (매도 후 15분 이내는 정상 매도 수익으로 간주, DCA 트리거 안 함)
        _cur_cash  = float(self.internal_cash or 0)
        _prev_cash = self._dca_prev_cash
        _since_trade = time.time() - self._last_trade_ts
        self._dca_deposit_trigger = False
        self._dca_deposit_amount  = 0.0
        if _prev_cash > 0 and (_cur_cash - _prev_cash) >= 200_000 and _since_trade > 900:
            self._dca_deposit_trigger = True
            self._dca_deposit_amount  = _cur_cash - _prev_cash
            self.add_log(f"💵 예수금 입금 감지: +{self._dca_deposit_amount:,.0f}원 → DCA 적립 실행")
        self._dca_prev_cash = _cur_cash
        # ─────────────────────────────────────────────────────────────

        with self.lock: safe_core_positions = list(self.core_positions)
        for core in safe_core_positions:
            if core.ticker == "TBD":  # AI 선정 대기 중 — 매매 스킵
                continue
            cp = self.live_prices.get(core.ticker) or getattr(core, 'kis_current_price', 0) or (self.kis.get_current_price(core.ticker) if self.kis else 0)
            if not cp or cp <= 0: continue
            with self.lock: core._last_price = cp; c_sh = core.shares; c_fl = core.floor_shares; c_avg = core.avg_price; c_cash = core.cash; c_nm = core.name; c_tk = core.ticker
            try:
                from KR.strategy import get_rsi_signal
                ex_df = self._get_extended_ohlcv(c_tk, cp)
                c_sig, _, c_rsi = get_rsi_signal(c_tk, kis_api=self.kis, df=ex_df)

                # ── BULL 국면 진입 신호 보완 ────────────────────────────────
                # RSI 30/70 전략은 BULL 장에서 RSI 50~70 구간이 대부분이라 BUY 신호가 거의 안 뜸.
                # 조건 A: RSI ≤ 65 + bull_score ≥ 1 (BULL에서 50~65도 매수 구간)
                # 조건 B: MA5 > MA20 정배열 + 현재가가 MA5 이하(눌림목 진입)
                if c_sig != 'BUY' and regime == "BULL" and c_sh == 0:
                    try:
                        if not ex_df.empty and 'close' in ex_df.columns:
                            _closes_b  = ex_df['close'].dropna()
                            _rsi_bull  = float(calc_rsi(_closes_b).iloc[-1])
                            _bull_sc, _bull_reasons = get_bull_momentum_score(ex_df)
                            # 조건 A: RSI ≤ 65 + bull_score ≥ 1
                            _bull_cond_a = (_rsi_bull <= 65) and (_bull_sc >= 1)
                            # 조건 B: MA5 > MA20 정배열 + 가격이 MA5 이내(2%) 눌림목
                            _bull_cond_b = False
                            if len(_closes_b) >= 22:
                                _ma5_b  = float(_closes_b.rolling(5).mean().iloc[-1])
                                _ma20_b = float(_closes_b.rolling(20).mean().iloc[-1])
                                _bull_cond_b = (_ma5_b > _ma20_b) and (cp <= _ma5_b * 1.02)
                            if _bull_cond_a or _bull_cond_b:
                                c_sig = 'BUY'
                                c_rsi = _rsi_bull
                                _bull_why = (f"RSI={_rsi_bull:.1f} bull_score={_bull_sc}" if _bull_cond_a
                                             else f"MA5눌림목(MA5={_closes_b.rolling(5).mean().iloc[-1]:,.0f})")
                                self.add_log(f"🚀 [BULL 코어 진입] {c_tk} {_bull_why} → BUY 오버라이드")
                    except Exception as _be:
                        logger.debug(f"BULL 코어 오버라이드 오류: {_be}")
                # ─────────────────────────────────────────────────────────────

                # ── BEAR 국면 조기 익절 오버라이드 ──────────────────────────
                # BEAR 반등은 RSI 55~65 수준에서 꺾임 → RSI 70 기다리면 수익 반납
                # 보유 중 + BEAR + RSI ≥ 60 → SELL 조기 트리거
                if c_sig != 'SELL' and regime == "BEAR" and c_sh > 0 and c_avg > 0:
                    if c_rsi >= 60:
                        c_sig = 'SELL'
                        self.add_log(f"🐻 [BEAR 코어 조기익절] {c_tk} RSI={c_rsi:.1f} ≥ 60 → SELL 오버라이드")
                # ─────────────────────────────────────────────────────────────

                # ── BULL 장에서 RSI 70 SELL 억제 → MA5 이탈 시만 매도 ────────
                # BULL 추세에서는 RSI가 오래 고공행진 → RSI 70 신호로 팔면 수익 조기 반납
                # 현재가가 MA5 * 0.99 이상이면 추세 유지 → SELL을 NEUTRAL로 되돌림
                if c_sig == 'SELL' and regime == "BULL" and c_sh > 0:
                    try:
                        _closes_bull = ex_df['close'].dropna()
                        if len(_closes_bull) >= 5:
                            _ma5_sell = float(_closes_bull.rolling(5).mean().iloc[-1])
                            if cp >= _ma5_sell * 0.99:
                                c_sig = 'NEUTRAL'
                                self.add_log(f"🐂 [BULL 코어] {c_tk} RSI SELL 억제 (RSI={c_rsi:.1f}) — MA5({_ma5_sell:,.0f}) 위 보유 유지")
                    except Exception:
                        pass
                # ─────────────────────────────────────────────────────────────

                # ── 코어 2차 분할 매수: 1차 진입가 -2% 눌림목 ─────────────────
                if (c_sh > 0 and is_core_cd
                        and not getattr(core, 'second_buy_done', True)
                        and getattr(core, 'second_buy_price', 0) > 0
                        and cp <= core.second_buy_price
                        and getattr(core, 'second_buy_cash', 0) >= cp
                        and c_sig != 'SELL'):
                    sq = int((core.second_buy_cash * 0.98) // cp)
                    if sq > 0 and self._buy_order(c_tk, sq, core, c_nm):
                        with self.lock:
                            core.last_order_time = time.time()
                            core.second_buy_done = True
                            core.second_buy_cash = 0.0
                            core.status          = "2차 매수 ✅"
                            new_shares = core.shares + sq
                            if new_shares > 0:
                                core.avg_price = round((core.avg_price * core.shares + cp * sq) / new_shares, 2)
                            core.shares = new_shares
                            core.floor_shares = max(core.floor_shares, int(core.shares * self.core_min_floor_ratio))
                        self.add_log(f"💎 {c_nm} 코어 2차 매수 | {sq}주 @ {cp:,}원 | 눌림목 -2%")
                        self._log_trade(c_tk, c_nm, 'BUY', cp, "RSI코어", f"코어 2차 분할 매수 눌림목 -2% ({sq}주)")
                        self._send_trade_telegram(self._fmt_trade_msg("💎", "코어 2차 매수", c_tk, c_nm, cp, sq, strategy="RSI코어", note="-2% 눌림목 포착 | 3차 -4% 대기"))

                # ── 코어 3차 분할 매수: 1차 진입가 -4% 눌림목 ─────────────────
                if (c_sh > 0 and is_core_cd
                        and getattr(core, 'second_buy_done', False)
                        and not getattr(core, 'third_buy_done', False)
                        and getattr(core, 'third_buy_price', 0) > 0
                        and cp <= core.third_buy_price
                        and getattr(core, 'third_buy_cash', 0) >= cp
                        and c_sig != 'SELL'):
                    sq3 = int((core.third_buy_cash * 0.98) // cp)
                    if sq3 > 0 and self._buy_order(c_tk, sq3, core, c_nm):
                        with self.lock:
                            core.last_order_time = time.time()
                            core.third_buy_done  = True
                            core.third_buy_cash  = 0.0
                            core.status          = "3차 매수 ✅"
                            new_shares = core.shares + sq3
                            if new_shares > 0:
                                core.avg_price = round((core.avg_price * core.shares + cp * sq3) / new_shares, 2)
                            core.shares = new_shares
                            core.floor_shares = max(core.floor_shares, int(core.shares * self.core_min_floor_ratio))
                        self.add_log(f"💎 {c_nm} 코어 3차 매수 | {sq3}주 @ {cp:,}원 | 눌림목 -4% | 예산 전액 투입 완료")
                        self._log_trade(c_tk, c_nm, 'BUY', cp, "RSI코어", f"코어 3차 분할 매수 눌림목 -4% ({sq3}주)")
                        self._send_trade_telegram(self._fmt_trade_msg("💎", "코어 3차 매수", c_tk, c_nm, cp, sq3, strategy="RSI코어", note="-4% 눌림목 포착 | 예산 전액 투입 완료"))
                # ─────────────────────────────────────────────────────────────

                # ── BULL 불타기 (코어 피라미딩) — +3% 돌파 + MA5 정배열 유지 ──
                # BULL 장에서 보유 포지션이 +3% 이상 수익 중 + 단기 정배열이면
                # 잔여 현금의 30%를 추가 매수해 수익을 극대화.  1회만 실행.
                with self.lock: c_cash = core.cash  # 최신 현금 재확인
                if (regime == "BULL" and c_sh > 0 and is_core_cd
                        and not getattr(core, 'bull_pyramid_done', False)
                        and c_avg > 0 and cp >= c_avg * 1.03
                        and c_sig != 'SELL' and c_cash > cp):
                    try:
                        _py_ok = False
                        if not ex_df.empty and len(ex_df['close'].dropna()) >= 22:
                            _cl_py  = ex_df['close'].dropna()
                            _ma5_py = float(_cl_py.rolling(5).mean().iloc[-1])
                            _ma20_py= float(_cl_py.rolling(20).mean().iloc[-1])
                            _py_ok  = _ma5_py > _ma20_py
                        if _py_ok:
                            _py_qty = max(1, int((c_cash * 0.30 * 0.98) // cp))
                            if _py_qty > 0 and self._buy_order(c_tk, _py_qty, core, c_nm):
                                with self.lock:
                                    core.last_order_time   = time.time()
                                    core.bull_pyramid_done = True
                                    _py_new_sh = core.shares + _py_qty
                                    if _py_new_sh > 0:
                                        core.avg_price = round((core.avg_price * core.shares + cp * _py_qty) / _py_new_sh, 2)
                                    core.shares    = _py_new_sh
                                    core._bought_val = getattr(core, '_bought_val', 0.0) + int(cp * _py_qty)
                                    core.cash      = max(0.0, core.cash - int(cp * _py_qty))
                                    _py_pct = (cp / c_avg - 1) * 100
                                    core.status    = f"불타기 🔥 (+{_py_pct:.1f}%)"
                                self.add_log(f"🔥 {c_nm} [BULL 불타기] +{_py_pct:.1f}% 상승 | {_py_qty}주 @ {cp:,}원 추가 (잔여현금 30%)")
                                self._log_trade(c_tk, c_nm, 'BUY', cp, "BULL불타기", f"BULL 피라미딩 | +{_py_pct:.1f}% 돌파 · MA5 정배열 확인")
                                self._send_trade_telegram(self._fmt_trade_msg("🔥", "BULL 불타기", c_tk, c_nm, cp, _py_qty,
                                    strategy=f"BULL피라미딩 +{_py_pct:.1f}%", note="잔여현금 30% 추가 진입"))
                    except Exception as _pye:
                        logger.debug(f"BULL 불타기(코어) 오류: {_pye}")
                # ─────────────────────────────────────────────────────────────

                # ── BEAR 국면 코어 조기 익절 (+5%) — 하락장 반등은 짧아 즉시 수확 ──
                # US봇 동일 전략: BEAR+5% 도달 시 트레일링/AI 없이 전량 청산
                if regime == "BEAR" and c_sh > 0 and c_avg > 0:
                    is_core_cd_bear = time.time() - getattr(core, 'last_order_time', 0) > 300
                    if is_core_cd_bear and cp >= c_avg * 1.05:
                        if self._sell_order(c_tk, c_sh, core, c_nm):
                            _bear_profit = _net_profit(cp, c_avg, c_sh)
                            _bear_pct    = (cp / c_avg - 1) * 100
                            with self.lock:
                                core.last_order_time   = time.time()
                                core.status            = "BEAR 조기익절 🐻"
                                core.shares            = 0
                                core._bought_val       = 0.0
                                core.partial_sold      = False
                                core.partial_sold_2    = False
                                core.second_buy_price  = 0.0
                                core.second_buy_cash   = 0.0
                                core.second_buy_done   = False
                                core.bull_pyramid_done = False
                                self.pnl_this_turn    += _bear_profit
                            self._record_daily_pnl(_bear_profit)
                            self.add_log(f"🐻 {c_nm} 코어 BEAR 조기익절 +{_bear_pct:.1f}% | {c_sh}주 @ {cp:,}원 | 손익: {_bear_profit:+,.0f}원")
                            self._log_trade(c_tk, c_nm, 'SELL', cp, "BEAR조기익절", f"BEAR 반등 +{_bear_pct:.1f}% 조기 수확", profit=_bear_profit)
                            self._send_trade_telegram(self._fmt_trade_msg("🐻", "코어 BEAR 조기익절", c_tk, c_nm, cp, c_sh, profit=_bear_profit, strategy="BEAR 반등 수확", note="하락장 +5% 반등 즉시 수확"))
                        continue
                # ─────────────────────────────────────────────────────────────

                # ── ATR(14) 코어 하드 손절 (전량 청산 — floor_shares 없음) ──
                c_atr = c_avg * 0.02
                if not ex_df.empty and all(col in ex_df.columns for col in ['high','low','close']):
                    try:
                        _tr = pd.concat([
                            ex_df['high'] - ex_df['low'],
                            (ex_df['high'] - ex_df['close'].shift(1)).abs(),
                            (ex_df['low']  - ex_df['close'].shift(1)).abs(),
                        ], axis=1).max(axis=1)
                        c_atr = float(_tr.rolling(14, min_periods=1).mean().iloc[-1])
                    except Exception:
                        pass

                core_hard_mult = 2.5 if regime == "NEUTRAL" else (3.0 if regime == "BULL" else 1.8)
                is_core_cd = time.time() - getattr(core, 'last_order_time', 0) > 300

                if c_sh > 0 and c_avg > 0 and is_core_cd and cp <= c_avg - (core_hard_mult * c_atr):
                    # 손절 전 뉴스 확인 — 호재면 일시 노이즈일 수 있어 1회 유예 (US봇 동일)
                    _stop_news_c = ""
                    if self.news_monitor:
                        try:
                            _stop_news_c = self.news_monitor.get_news_summary(c_nm, display=3)
                        except Exception:
                            pass
                    _stop_skip_c = False
                    if _stop_news_c and not getattr(core, 'stop_news_checked', False):
                        _pos_kw_c = ['계약', '수주', '호재', '신제품', '상향', '목표가', '매수', '기록', '최고', '상승']
                        if any(kw in _stop_news_c for kw in _pos_kw_c):
                            core.stop_news_checked = True
                            _stop_skip_c = True
                            self.add_log(f"⚠️ {c_nm} 코어 ATR 손절 터치 but 호재 뉴스 감지 → 1회 유예\n{_stop_news_c[:100]}")
                    if _stop_skip_c:
                        continue
                    core.stop_news_checked = False
                    _core_atr_reason = f"코어 ATR×{core_hard_mult} 손절"
                    _swing_core = self._ai_swing_check_kr(core, c_tk, cp, _core_atr_reason)
                    if _swing_core == 'ACCUMULATE':
                        acc_c = getattr(core, 'swing_acc_count', 0)
                        _acc_cash_c = core.cash * 0.30
                        _acc_qty_c  = int((_acc_cash_c * 0.98) // cp)
                        if _acc_qty_c > 0 and self._buy_order(c_tk, _acc_qty_c, core, c_nm):
                            with self.lock:
                                new_sh = core.shares + _acc_qty_c
                                if new_sh > 0: core.avg_price = round((core.avg_price * core.shares + cp * _acc_qty_c) / new_sh, 2)
                                core.shares = new_sh; core.swing_acc_count = acc_c + 1; core.status = f"코어 스윙 누적 {acc_c+1}차 📥"
                            self.add_log(f"📥 [스윙 KR코어] {c_nm}({c_tk}) ACCUMULATE {acc_c+1}차 | {_acc_qty_c}주 @ {cp:,.0f}원")
                        continue
                    if _swing_core == 'SELL_REBUY':
                        self.add_log(f"🔄 [스윙 KR코어] {c_nm}({c_tk}) SELL_REBUY — 손절 후 재진입 모니터링")
                    if self._sell_order(c_tk, c_sh, core, c_nm):
                        core_profit = _net_profit(cp, c_avg, c_sh)
                        with self.lock:
                            core.last_order_time  = time.time()
                            core.status           = "코어 손절 🚨" if _swing_core == 'EXIT' else "코어 스윙매도 🔄"
                            core.shares           = 0; core._bought_val = 0.0
                            core.partial_sold     = False; core.partial_sold_2 = False
                            core.second_buy_price = 0.0; core.second_buy_cash = 0.0
                            core.second_buy_done  = False; core.bull_pyramid_done = False
                            core.swing_acc_count  = 0
                            self.pnl_this_turn   += core_profit
                        self._record_daily_pnl(core_profit)
                        self.add_log(f"🚨 {c_nm} 코어 ATR 손절 [{_swing_core}] | {c_sh}주 @ {cp:,}원 | 손익: {core_profit:+,.0f}원")
                        if self.claude:
                            self.claude.record_trade_event(f"KR 코어 ATR 손절 [{_swing_core}]: {c_nm}({c_tk}) {c_sh}주 @ {cp:,}원 | 손익: {core_profit:+,.0f}원")
                        self._log_trade(c_tk, c_nm, 'SELL', cp, "코어 ATR 손절", f"평단 {c_avg:,.0f} ATR×{core_hard_mult} [{_swing_core}]", profit=core_profit)
                        self._send_trade_telegram(self._fmt_trade_msg("🚨", f"코어 손절 [{_swing_core}]", c_tk, c_nm, cp, c_sh, profit=core_profit, strategy="코어 ATR 손절"))
                    continue

                # ── 코어 부분 익절 (AI 판단) ─────────────────────────────
                if c_sh > 0 and c_avg > 0 and is_core_cd:
                    c_pnl_pct = (cp / c_avg - 1) * 100
                    c_decision = getattr(core, 'ai_exit_decision', None)
                    # BULL 장에서는 추세가 강하므로 익절 기준 상향 (+15%/+30%)
                    _core_partial1 = 15.0 if regime == "BULL" else 10.0
                    _core_partial2 = 30.0 if regime == "BULL" else 20.0

                    # 1차: +10%(일반) / +15%(BULL) 도달 → AI에 익절 여부 문의
                    if not core.partial_sold and c_pnl_pct >= _core_partial1 and c_sh > 1:
                        if c_decision is None:
                            if self.claude:
                                self._trigger_ai_partial_exit(core, c_tk, c_nm, cp, c_avg, c_pnl_pct, regime)
                                with self.lock: core.status = f"AI 익절 검토 중 ({c_pnl_pct:+.1f}%) 🤖"
                            else:
                                with self.lock: core.ai_exit_decision = "SELL_PARTIAL"
                        elif c_decision == "HOLD":
                            with self.lock:
                                core.status = f"AI 홀드 ({c_pnl_pct:+.1f}%) ⏳"
                        else:
                            # 1차 익절 시 원금 주수 고정
                            with self.lock:
                                if not getattr(core, 'initial_shares_for_exit', 0):
                                    core.initial_shares_for_exit = c_sh
                            _c_init_sh = getattr(core, 'initial_shares_for_exit', 0) or c_sh
                            partial_qty = max(1, min(int(_c_init_sh * 0.50), c_sh))
                            if self._sell_order(c_tk, partial_qty, core, c_nm):
                                core_profit = _net_profit(cp, c_avg, partial_qty)
                                with self.lock:
                                    core.last_order_time  = time.time()
                                    core.shares          -= partial_qty
                                    core.partial_sold     = True
                                    core.ai_exit_decision = None
                                    core.status           = f"코어 1차익절({c_pnl_pct:+.1f}%) ✂️"
                                    self.pnl_this_turn   += core_profit
                                self._record_daily_pnl(core_profit)
                                self.add_log(f"✂️  {c_nm} 코어 1차익절 | {partial_qty}주 @ {cp:,}원 (원금 {_c_init_sh}주 기준 50%) | 손익: {core_profit:+,.0f}원")
                                self._send_trade_telegram(self._fmt_trade_msg("✂️", "코어 1차익절(50%)", c_tk, c_nm, cp, partial_qty, profit=core_profit, strategy="코어 AI 익절"))
                        continue

                    # 2차: +20%(일반) / +30%(BULL) 도달 → AI에 전량 익절 여부 문의
                    elif core.partial_sold and not core.partial_sold_2 and c_pnl_pct >= _core_partial2:
                        if c_decision is None:
                            if self.claude:
                                self._trigger_ai_partial_exit(core, c_tk, c_nm, cp, c_avg, c_pnl_pct, regime)
                                with self.lock: core.status = f"AI 익절 검토 중 ({c_pnl_pct:+.1f}%) 🤖"
                            else:
                                with self.lock: core.ai_exit_decision = "SELL_ALL"
                        elif c_decision == "HOLD":
                            with self.lock:
                                core.status = f"AI 홀드 ({c_pnl_pct:+.1f}%) ⏳"
                        else:
                            # 2차: 원금 기준 50% (1차와 동일 기준)
                            _c_init_sh2 = getattr(core, 'initial_shares_for_exit', 0) or c_sh
                            sell_qty_c2 = max(1, min(int(_c_init_sh2 * 0.50), c_sh))
                            if self._sell_order(c_tk, sell_qty_c2, core, c_nm):
                                core_profit = _net_profit(cp, c_avg, sell_qty_c2)
                                with self.lock:
                                    core.last_order_time  = time.time()
                                    core.shares           = max(0, core.shares - sell_qty_c2)
                                    core._bought_val      = 0.0
                                    core.partial_sold_2   = True
                                    core.ai_exit_decision = None
                                    core.status           = f"코어 2차익절({c_pnl_pct:+.1f}%) ✅"
                                    self.pnl_this_turn   += core_profit
                                self._record_daily_pnl(core_profit)
                                self.add_log(f"✅ {c_nm} 코어 2차익절 | {sell_qty_c2}주 @ {cp:,}원 (원금 {_c_init_sh2}주 기준 50%) | 손익: {core_profit:+,.0f}원")
                                self._send_trade_telegram(self._fmt_trade_msg("✅", "코어 2차익절(50%)", c_tk, c_nm, cp, sell_qty_c2, profit=core_profit, strategy="코어 AI 익절"))
                        continue

                # c_cash를 락 안에서 최신값으로 재확인 (스냅샷 후 _sync_internal_balances가 변경 가능)
                with self.lock: c_cash = core.cash

                # ── 적립식(DCA) 매수 — 진입 점수 게이트 우회 ────────────────
                # dca_mode=True 코어: 진입 점수/RSI 신호 무관하게
                # ① 예수금 입금 감지 시 → 입금액을 DCA 코어 수로 균등 분배해 적립
                # ② 48시간 쿨다운 + 평단 대비 -dca_dip_pct% 하락 시 → 눌림목 추가 매수
                _dca_bought_this_turn = False
                if getattr(core, 'dca_mode', False) and c_cash >= cp and is_core_cd:
                    _now_ts  = time.time()
                    _elapsed = _now_ts - getattr(core, 'last_dca_time', 0.0)
                    _dca_dip = getattr(core, 'dca_dip_pct', 3.0)

                    _do_dca, _dca_reason, _dca_budget = False, "", 0.0

                    # ① 예수금 입금 감지 트리거
                    if self._dca_deposit_trigger and self._dca_deposit_amount > 0:
                        _n_dca = sum(1 for _c in self.core_positions if getattr(_c, 'dca_mode', False))
                        _dca_budget = self._dca_deposit_amount / max(1, _n_dca)
                        _do_dca     = True
                        _dca_reason = f"예수금 입금 ({self._dca_deposit_amount:,.0f}원 / {_n_dca}종목 분배)"

                    # ② 눌림목: 48시간 쿨다운 + 평단 대비 -X% 하락
                    elif _elapsed >= 48 * 3600 and c_sh > 0 and c_avg > 0 and cp <= c_avg * (1 - _dca_dip / 100):
                        _dca_budget = getattr(core, 'dca_amount', 0) or (c_cash * 0.10)
                        _do_dca     = True
                        _dca_reason = f"눌림목 추가 ({(cp/c_avg-1)*100:.1f}% 하락)"

                    _dca_budget = min(_dca_budget, c_cash)
                    if _do_dca and _dca_budget >= cp:
                        _dca_qty = int((_dca_budget * 0.98) // cp)
                        if _dca_qty > 0 and self._buy_order(c_tk, _dca_qty, core, c_nm):
                            with self.lock:
                                core.last_order_time = time.time()
                                core.last_dca_time   = _now_ts
                                _new_shares = core.shares + _dca_qty
                                if _new_shares > 0:
                                    core.avg_price = round((core.avg_price * core.shares + cp * _dca_qty) / _new_shares, 2)
                                core.shares         = _new_shares
                                core.floor_shares   = max(core.floor_shares, int(core.shares * self.core_min_floor_ratio))
                                core._bought_val    = getattr(core, '_bought_val', 0.0) + int(cp * _dca_qty)
                                core.cash           = max(0.0, core.cash - int(cp * _dca_qty))
                                core.status         = "DCA 적립 💰"
                            self.add_log(f"💰 {c_nm} DCA 적립 | {_dca_qty}주 @ {cp:,}원 | {_dca_reason}")
                            self._log_trade(c_tk, c_nm, 'BUY', cp, "DCA적립", _dca_reason)
                            self._send_trade_telegram(self._fmt_trade_msg("💰", f"DCA 적립", c_tk, c_nm, cp, _dca_qty, strategy="DCA적립", note=_dca_reason))
                            _dca_bought_this_turn = True
                    elif getattr(core, 'dca_mode', False) and not _do_dca:
                        with self.lock:
                            core.status     = "DCA 적립 대기 💰"
                            core.status_msg = f"입금 감지 대기 | 눌림목 트리거 -{_dca_dip:.0f}% (평단 {c_avg:,.0f}원)"
                # ─────────────────────────────────────────────────────────────

                if c_cash >= cp and is_core_cd and not _dca_bought_this_turn:
                    # ① 코어 전용 진입 점수 (RSI 저평가 + 120MA/60MA 위치만 판단)
                    # 모멘텀·거래량·MACD 무관 — 장기 프로젝트 원칙
                    c_score, c_score_reasons = calculate_core_entry_score(ex_df, cp, regime)
                    # ROE 턴어라운드 보너스 (분기별 음→양 개선 추세)
                    _roe_b, _roe_r = _roe_turnaround_kr(c_tk)
                    if _roe_b > 0:
                        c_score += _roe_b
                        c_score_reasons.append(_roe_r)
                    c_threshold = self.entry_thresholds.get(f'core_{regime}', get_core_entry_threshold(regime))
                    if c_score < c_threshold:
                        with self.lock:
                            core.status = "점수 대기 ⏳"
                            core.status_msg = f"진입점수 {c_score}/{c_threshold}pt | 충족: {', '.join(c_score_reasons[:3]) if c_score_reasons else '없음'}"
                    else:
                        budget_ratio  = get_budget_ratio_from_score(c_score, c_threshold)
                        # 점수 비율을 각 회차에 동일 적용 — 배정 예산 초과 시 남은 예산으로 캡
                        first_cash    = c_cash * budget_ratio
                        _c_remain1    = max(0.0, c_cash - first_cash)
                        reserve_cash  = min(c_cash * budget_ratio, _c_remain1)
                        c_third_cash  = max(0.0, c_cash - first_cash - reserve_cash)
                        qty = int((first_cash * 0.98) // cp)
                        if qty > 0:
                            # ② 코어 전용 AI 승인 — 단기 모멘텀 무관, 악재 리스크만 판단
                            approved, ai_reason = True, "AI 미설정"
                            if self.claude:
                                with self.lock:
                                    core.status     = "AI 심사 중 🤖"
                                    core.status_msg = f"RSI{c_rsi:.0f}+120MA 기준 충족 | 악재 리스크 확인 중..."
                                # 120MA, 60MA 값 추출
                                try:
                                    _c = ex_df['close'].dropna()
                                    _ma120 = float(_c.rolling(120).mean().iloc[-1]) if len(_c) >= 120 else 0
                                    _ma60  = float(_c.rolling(60).mean().iloc[-1])  if len(_c) >= 60  else 0
                                except Exception:
                                    _ma120 = _ma60 = 0
                                _news_raw = fetch_recent_news(c_nm)
                                # "뉴스 조회 실패" 텍스트는 AI에 전달 금지 — 악재 오인 방지
                                _news = _news_raw if _news_raw and "조회 실패" not in _news_raw else ""
                                approved, ai_reason = self.claude.ai_approve_core_trade(
                                    stock_name=c_nm, ticker=c_tk, price=cp,
                                    rsi=c_rsi, ma120=_ma120, ma60=_ma60,
                                    regime=regime, news_headlines=_news,
                                )
                            if not approved:
                                with self.lock:
                                    core.status     = "AI 거절 🛑"
                                    core.status_msg = f"악재 리스크 감지: {ai_reason}"
                                self.add_log(f"🛑 {c_nm} 코어 AI 거절(악재): {ai_reason}")
                                if self.claude:
                                    self.claude.record_trade_event(f"KR 코어 AI 거절: {c_nm}({c_tk}) @ {cp:,}원 | {ai_reason}")
                            elif self._buy_order(c_tk, qty, core, c_nm):
                                with self.lock:
                                    core.last_order_time  = time.time()
                                    core.status           = "체결 대기 ⏳"
                                    core.shares          += qty
                                    core._bought_val      = getattr(core, '_bought_val', 0.0) + int(cp * qty)
                                    core.cash             = max(0.0, core.cash - int(cp * qty))
                                    core.partial_sold            = False
                                    core.partial_sold_2          = False
                                    core.initial_shares_for_exit = 0
                                    core.second_buy_price        = cp * 0.98
                                    core.second_buy_cash         = reserve_cash
                                    core.second_buy_done         = False
                                    core.third_buy_price         = cp * 0.96
                                    core.third_buy_cash          = c_third_cash
                                    core.third_buy_done          = False
                                score_str = " | ".join(c_score_reasons[:3])
                                _c_ratio_pct = int(budget_ratio * 100)
                                self.add_log(f"💎 {c_nm} 코어 1차 매수({_c_ratio_pct}%) | {qty}주 @ {cp:,}원 | {c_score}pt [{score_str}] | 2차:{cp*0.98:,.0f}(-2%) 3차:{cp*0.96:,.0f}(-4%) | {ai_reason}")
                                if self.claude:
                                    self.claude.record_trade_event(f"KR 코어 1차 매수({_c_ratio_pct}%): {c_nm}({c_tk}) {qty}주 @ {cp:,}원 | {c_score}pt [{score_str}]")
                                self._log_trade(c_tk, c_nm, 'BUY', cp, "RSI코어", f"RSI저평가+120MA {c_score}pt [{score_str}] — 1차({_c_ratio_pct}%)")
                                self._send_trade_telegram(self._fmt_trade_msg("💎", f"코어 1차 매수 ({int(budget_ratio*75):.0f}%)", c_tk, c_nm, cp, qty, strategy=f"RSI코어 · {c_score}pt/{c_threshold}pt", ai_reason=ai_reason, note=f"2차 예약: {cp*0.98:,.0f}원 (-2%)"))

                elif c_sig == 'SELL' and c_sh > 0 and is_core_cd:
                    # RSI 데드크로스 → 전량 매도 (floor_shares 제거)
                    if c_avg > 0 and self._sell_order(c_tk, c_sh, core, c_nm):
                        core_profit = _net_profit(cp, c_avg, c_sh)
                        with self.lock:
                            core.last_order_time = time.time()
                            core.status         = "체결 대기 ⏳"
                            core.shares         = 0
                            core._bought_val     = 0.0
                            core.partial_sold            = False
                            core.partial_sold_2          = False
                            core.initial_shares_for_exit = 0
                            core.second_buy_price        = 0.0
                            core.second_buy_cash         = 0.0
                            core.second_buy_done         = False
                            core.third_buy_price         = 0.0
                            core.third_buy_cash          = 0.0
                            core.third_buy_done          = False
                            core.bull_pyramid_done       = False
                            self.pnl_this_turn          += core_profit
                        self._record_daily_pnl(core_profit)
                        self.add_log(f"💎 {c_nm} 코어 매도 전량 | {c_sh}주 @ {cp:,}원 | 손익: {core_profit:+,.0f}원")
                        if self.claude:
                            self.claude.record_trade_event(f"KR 코어 전량매도(RSI 데드크로스): {c_nm}({c_tk}) {c_sh}주 @ {cp:,}원 | 손익: {core_profit:+,.0f}원")
                        self._log_trade(c_tk, c_nm, 'SELL', cp, "RSI 코어 전량매도", "RSI 데드크로스 — 재진입 타점 탐색", profit=core_profit)
                        self._send_trade_telegram(self._fmt_trade_msg("💎", "코어 전량매도", c_tk, c_nm, cp, c_sh, profit=core_profit, strategy="RSI 데드크로스 → 재진입 대기"))
            except Exception as e:
                logger.error(f"[{self.mode_name}] 코어 매매 오류 ({c_tk}): {e}", exc_info=True)
            time.sleep(0.2)

        with self.lock:
            trading_sat_items = list(self.satellite_positions.items())
            # 위성 루프 전 보유 중인 슬롯 스냅샷 (루프 후 비교용)
            _sat_full_before = {t for t, p in trading_sat_items if p.shares > 0}

        for ticker, pos in trading_sat_items:
            try:
                with self.lock: p_sh = pos.shares; p_avg = pos.avg_price; p_max = pos.max_price; p_cash = pos.cash; p_nm = pos.name
                price = self.live_prices.get(ticker) or getattr(pos, 'kis_current_price', 0) or (self.kis.get_current_price(ticker) if self.kis else 0)
                if not price or price <= 0: continue
                with self.lock: pos._last_price = price

                ex_df = self._get_extended_ohlcv(ticker, price)
                sig, buy_sc, sell_sc, sig_reasons = get_composite_signal(ex_df)
                ind_val = {"buy": buy_sc, "sell": sell_sc, "signals": sig_reasons}
                if price <= 0: continue

                # ── 진입 점수 계산 (RSI 30은 더이상 필수 아님 — 점수제로 통합) ──
                _frgn_net = 0
                try:
                    if self.kis and hasattr(self.kis, 'get_foreign_buy_by_ticker'):
                        _fi = self.kis.get_foreign_buy_by_ticker(ticker)
                        if _fi:
                            _frgn_net = int(_fi.get("frgn_net", 0))
                except Exception:
                    pass
                entry_score, entry_reasons = calculate_entry_score(ex_df, price, regime, frgn_net=_frgn_net)
                # AI 시장판단 보너스 반영 (EWY·NQ·환율 종합 판단)
                _ai_bonus = getattr(self, '_ai_market_entry_bonus', 0)
                if _ai_bonus != 0:
                    entry_score += _ai_bonus
                    entry_reasons.append(f"AI시장판단 {_ai_bonus:+d}pt")
                entry_threshold = self.entry_thresholds.get(f'sat_{regime}', self.entry_thresholds.get(regime, get_entry_threshold(regime, 'satellite')))
                score_ratio = max(0.6, get_budget_ratio_from_score(entry_score, entry_threshold))
                st_nm = f"진입점수({entry_score}/{entry_threshold}pt)"
                # ────────────────────────────────────────────────────────────────

                if ex_df.empty or not all(c in ex_df.columns for c in ['high', 'low', 'close']):
                    atr_14 = p_avg * 0.02
                else:
                    tr = pd.concat([ex_df['high']-ex_df['low'], (ex_df['high']-ex_df['close'].shift(1)).abs(), (ex_df['low']-ex_df['close'].shift(1)).abs()], axis=1).max(axis=1)
                    atr_14 = tr.rolling(14, min_periods=1).mean().iloc[-1] if not tr.empty else p_avg * 0.02

                is_cd_passed = (time.time() - getattr(pos, 'last_order_time', 0) > 300)

                # 국면별 ATR 배수 조정
                # BEAR: 익절 빠르게(0.8x), 손절 빠르게(1.8x) → 손실 최소화
                # BULL: 익절 여유롭게(1.2x), 손절 넉넉히(3.0x) → 수익 극대화
                # NEUTRAL: 기본값(1.0x trailing, 2.5x hard)
                if regime == "BEAR":
                    trail_mult, trail_trigger, hard_mult = 1.2, 0.8, 1.8
                elif regime == "BULL":
                    trail_mult, trail_trigger, hard_mult = 1.5, 1.2, 3.0
                else:
                    trail_mult, trail_trigger, hard_mult = 1.5, 1.0, 2.5

                # ── BEAR 국면 위성 하드 익절 (+5%) ──────────────────────────
                # 하락장 반등은 짧고 강함 — 트레일링 발동 전에 +5% 도달 시 즉시 전량 청산
                if regime == "BEAR" and p_sh > 0 and p_avg > 0 and is_cd_passed:
                    if price >= p_avg * 1.05:
                        if self._sell_order(ticker, p_sh, pos, p_nm):
                            with self.lock:
                                pos.last_order_time = time.time(); pos.status = "체결 대기 ⏳"
                                self._sat_exit_reset(pos)
                            profit = _net_profit(price, p_avg, p_sh)
                            self._log_trade(ticker, p_nm, 'SELL', price, st_nm, "BEAR +5% 하드 익절", profit=profit)
                            self._send_trade_telegram(self._fmt_trade_msg("🐻", "BEAR 하드 익절 +5%", ticker, p_nm, price, p_sh, profit=profit, strategy=st_nm, note="하락장 반등 조기 수확"))
                            with self.lock:
                                self.pnl_this_turn += profit
                                if profit > 0 and self.core_positions:
                                    reinvest_bear = profit * REINVEST_RATIO
                                    for core in self.core_positions:
                                        core.cash += reinvest_bear / len(self.core_positions)
                            self._record_daily_pnl(profit)
                        continue
                # ─────────────────────────────────────────────────────────────

                if p_sh > 0 and price > 0 and is_cd_passed:
                    if price > p_max:
                        with self.lock: pos.max_price = price; p_max = price
                    if p_max >= p_avg + (trail_trigger * atr_14) and price <= p_max - (trail_mult * atr_14):
                        _trail_reason = "ATR 트레일링 익절"
                        _swing = self._ai_swing_check_kr(pos, ticker, price, _trail_reason)
                        if _swing == 'ACCUMULATE':
                            acc_c = getattr(pos, 'swing_acc_count', 0)
                            _acc_cash = pos.cash * 0.30
                            _acc_qty  = int((_acc_cash * 0.98) // price)
                            if _acc_qty > 0 and self._buy_order(ticker, _acc_qty, pos, p_nm):
                                with self.lock:
                                    new_sh = pos.shares + _acc_qty
                                    if new_sh > 0: pos.avg_price = round((pos.avg_price * pos.shares + price * _acc_qty) / new_sh, 2)
                                    pos.shares = new_sh; pos.swing_acc_count = acc_c + 1
                                    pos.status = f"스윙 누적 {acc_c+1}차 📥"
                                self.add_log(f"📥 [스윙 KR위성] {p_nm}({ticker}) ACCUMULATE {acc_c+1}차 | {_acc_qty}주 @ {price:,.0f}원")
                            continue
                        if _swing == 'SELL_REBUY':
                            self.add_log(f"🔄 [스윙 KR위성] {p_nm}({ticker}) SELL_REBUY — 트레일링 매도 후 재진입 모니터링")
                        if self._sell_order(ticker, p_sh, pos, p_nm):
                            with self.lock:
                                pos.last_order_time = time.time(); pos.status = "체결 대기 ⏳"
                                self._sat_exit_reset(pos)
                            profit = _net_profit(price, p_avg, p_sh)
                            self._log_trade(ticker, p_nm, 'SELL', price, st_nm, f"{_trail_reason} [{_swing}]", profit=profit)
                            self._send_trade_telegram(self._fmt_trade_msg("🎯", "트레일링 익절", ticker, p_nm, price, p_sh, profit=profit, strategy=st_nm, note=f"ATR 트레일링 [{_swing}]"))
                            with self.lock:
                                self.pnl_this_turn += profit
                                if profit > 0 and self.core_positions:
                                    reinvest_trail = profit * REINVEST_RATIO
                                    for core in self.core_positions:
                                        core.cash += reinvest_trail / len(self.core_positions)
                            self._record_daily_pnl(profit)
                        continue

                # I-01: 장 초반(09:00~09:30) 급락 단계별 손절 — check_early_drop_stop 실제 연결
                # check_early_drop_stop은 (stage, sell_pct, reason) 튜플을 반환
                if p_sh > 0 and p_avg > 0 and is_cd_passed and "09:00" <= current_time_str <= "09:30":
                    _es_stage, _es_pct, _es_reason = check_early_drop_stop(price, p_avg)
                    if _es_stage > 0 and _es_pct > 0:
                        stop_qty = max(1, int(p_sh * _es_pct))
                        if self._sell_order(ticker, stop_qty, pos, p_nm):
                            with self.lock:
                                pos.last_order_time = time.time(); pos.status = "장초 급락 손절 🚨"
                                # [BUG-M5 동일 패턴] 매도 후 잔여주수 반영 — stage2/3 전량, stage1 50%
                                pos.shares = max(0, pos.shares - stop_qty)
                            profit = _net_profit(price, p_avg, stop_qty)
                            self._log_trade(ticker, p_nm, 'SELL', price, st_nm, f"장초 급락 손절 {_es_pct*100:.0f}% [{_es_reason}]", profit=profit)
                            self._send_trade_telegram(self._fmt_trade_msg("🚨", "장초 급락 손절", ticker, p_nm, price, stop_qty, profit=profit, strategy=st_nm, note=_es_reason))
                            with self.lock: self.pnl_this_turn += profit
                            self._record_daily_pnl(profit)
                        continue

                if p_sh > 0 and p_avg > 0 and is_cd_passed:
                    if price <= p_avg - (hard_mult * atr_14):
                        # 손절 전 뉴스 확인 — 호재면 일시 노이즈일 수 있어 1회 유예 (US봇 동일)
                        _stop_news_kr = ""
                        if self.news_monitor:
                            try:
                                _stop_news_kr = self.news_monitor.get_news_summary(p_nm, display=3)
                            except Exception:
                                pass
                        _stop_skip_kr = False
                        if _stop_news_kr and not getattr(pos, 'stop_news_checked', False):
                            _pos_kw_kr = ['계약', '수주', '호재', '신제품', '상향', '목표가', '매수', '기록', '최고', '상승']
                            if any(kw in _stop_news_kr for kw in _pos_kw_kr):
                                pos.stop_news_checked = True
                                _stop_skip_kr = True
                                self.add_log(f"⚠️ {p_nm} ATR 손절 터치 but 호재 뉴스 감지 → 1회 유예\n{_stop_news_kr[:100]}")
                        if _stop_skip_kr:
                            continue
                        pos.stop_news_checked = False
                        _atr_reason_kr = "ATR 하드 손절"
                        _swing_kr = self._ai_swing_check_kr(pos, ticker, price, _atr_reason_kr)
                        if _swing_kr == 'ACCUMULATE':
                            acc_c = getattr(pos, 'swing_acc_count', 0)
                            _acc_cash = pos.cash * 0.30
                            _acc_qty  = int((_acc_cash * 0.98) // price)
                            if _acc_qty > 0 and self._buy_order(ticker, _acc_qty, pos, p_nm):
                                with self.lock:
                                    new_sh = pos.shares + _acc_qty
                                    if new_sh > 0: pos.avg_price = round((pos.avg_price * pos.shares + price * _acc_qty) / new_sh, 2)
                                    pos.shares = new_sh; pos.swing_acc_count = acc_c + 1; pos.status = f"스윙 누적 {acc_c+1}차 📥"
                                self.add_log(f"📥 [스윙 KR위성] {p_nm}({ticker}) ACCUMULATE {acc_c+1}차 | {_acc_qty}주 @ {price:,.0f}원")
                            continue
                        if _swing_kr == 'SELL_REBUY':
                            self.add_log(f"🔄 [스윙 KR위성] {p_nm}({ticker}) SELL_REBUY — 손절 후 재진입 모니터링")
                        if self._sell_order(ticker, p_sh, pos, p_nm):
                            with self.lock:
                                pos.last_order_time = time.time(); pos.status = "체결 대기 ⏳"
                                self._sat_exit_reset(pos)
                            profit = _net_profit(price, p_avg, p_sh)
                            self._log_trade(ticker, p_nm, 'SELL', price, st_nm, f"{_atr_reason_kr} [{_swing_kr}]", profit=profit)
                            self._send_trade_telegram(self._fmt_trade_msg("💥", "손절 체결", ticker, p_nm, price, p_sh, profit=profit, strategy=st_nm, note=f"ATR 하드 손절 [{_swing_kr}]"))
                            if self.claude:
                                self.claude.record_trade_event(f"KR 위성 ATR 손절 [{_swing_kr}]: {p_nm}({ticker}) {p_sh}주 @ {price:,.0f}원 | 손익: {profit:+,.0f}원")
                            with self.lock: self.pnl_this_turn += profit
                            self._record_daily_pnl(profit)
                        continue

                # ── 🔥 테마주 과열 청산 & RSI 점진적 익절 ─────────────────────────────
                # crash_pattern_analysis.py 분석 반영:
                # 60일선 이격 평균 +45%, RSI 베어다이버전스, 거래량 소멸 = 급락 전 공통 패턴
                # sector_bonus≥10(핫섹터) 종목은 1단계 완화 적용 (테마주 조기 청산 방지)
                if p_sh > 0 and p_avg > 0 and is_cd_passed and not ex_df.empty:
                    _sat_info_m   = next((s for s in self.satellite_info if s['ticker'] == ticker), None)
                    _sector       = _sat_info_m.get('sector', '') if _sat_info_m else ''
                    _sector_bonus = 10 if (_sector and _sector in self.hot_sectors) else 0

                    _oe_sig,  _oe_score,  _oe_reason  = check_theme_overextension_exit(ex_df, price, _sector_bonus)
                    _rsi_sig, _rsi_val,   _rsi_reason = check_rsi_progressive_exit(ex_df, price, p_avg)

                    # 두 신호 중 더 강한 것 우선 (FULL > PARTIAL_60 > PARTIAL_30 > HOLD)
                    _sig_rank = {'HOLD': 0, 'PARTIAL_EXIT_30': 1, 'PARTIAL_EXIT_60': 2, 'FULL_EXIT': 3}
                    if _sig_rank.get(_oe_sig, 0) >= _sig_rank.get(_rsi_sig, 0):
                        _fe_sig, _fe_reason = _oe_sig, _oe_reason
                    else:
                        _fe_sig, _fe_reason = _rsi_sig, _rsi_reason

                    if _fe_sig == 'FULL_EXIT':
                        _full_qty = p_sh   # 매도 전 주수 보존 (락 안에서 p_sh 갱신 전)
                        if _full_qty > 0 and self._sell_order(ticker, _full_qty, pos, p_nm):
                            with self.lock:
                                pos.last_order_time = time.time(); pos.status = "과열 전량청산 🚨"
                                self._sat_exit_reset(pos)
                                p_sh = 0   # 이후 로직에서 0주로 인식
                            profit = _net_profit(price, p_avg, _full_qty)
                            self._log_trade(ticker, p_nm, 'SELL', price, st_nm, f"과열 전량청산 [{_fe_reason}]", profit=profit)
                            self._send_trade_telegram(self._fmt_trade_msg("🚨", "과열 전량청산", ticker, p_nm, price, _full_qty, profit=profit, strategy=st_nm, note=_fe_reason))
                            with self.lock:
                                self.pnl_this_turn += profit
                                if profit > 0 and self.core_positions:
                                    reinvest_sat = profit * REINVEST_RATIO
                                    for core in self.core_positions:
                                        core.cash += reinvest_sat / len(self.core_positions)
                            self._record_daily_pnl(profit)
                        continue

                    elif _fe_sig == 'PARTIAL_EXIT_60' and p_sh > 1:
                        _q60 = max(1, int(p_sh * 0.60))
                        if self._sell_order(ticker, _q60, pos, p_nm):
                            with self.lock:
                                pos.last_order_time = time.time(); pos.status = "과열 선익절 60% ✂️"
                                pos.shares = max(0, pos.shares - _q60)
                                p_sh = pos.shares   # 로컬 스냅샷 갱신 → 이후 익절 로직 정합성 유지
                            profit = _net_profit(price, p_avg, _q60)
                            self._log_trade(ticker, p_nm, 'SELL', price, st_nm, f"과열청산 60% [{_fe_reason}]", profit=profit)
                            self._send_trade_telegram(self._fmt_trade_msg("✂️", "과열 선익절 60%", ticker, p_nm, price, _q60, profit=profit, strategy=st_nm, note=_fe_reason))
                            with self.lock: self.pnl_this_turn += profit
                            self._record_daily_pnl(profit)

                    elif (_fe_sig == 'PARTIAL_EXIT_30'
                            and p_sh > 0
                            and getattr(pos, 'overext_sell_count', 0) < 3):
                        _oe_cnt = getattr(pos, 'overext_sell_count', 0)
                        # 1차 트리거 시 원금 주수 고정 (이후 p_sh가 줄어도 기준 유지)
                        if _oe_cnt == 0:
                            with self.lock:
                                pos.initial_shares_for_exit = p_sh
                        _init_sh = getattr(pos, 'initial_shares_for_exit', 0) or p_sh
                        if _oe_cnt < 2 and p_sh > 1:
                            # 1차 / 2차: 원금 기준 30% (잔여 주수 기준 아님)
                            _q30 = max(1, min(int(_init_sh * 0.30), p_sh))
                            if self._sell_order(ticker, _q30, pos, p_nm):
                                with self.lock:
                                    pos.last_order_time = time.time()
                                    pos.overext_sell_count = _oe_cnt + 1
                                    pos.status = f"과열 선익절 {_oe_cnt+1}차 30% ✂️"
                                    pos.shares = max(0, pos.shares - _q30)
                                    p_sh = pos.shares
                                profit = _net_profit(price, p_avg, _q30)
                                self._log_trade(ticker, p_nm, 'SELL', price, st_nm, f"과열청산 {_oe_cnt+1}차 30% [{_fe_reason}]", profit=profit)
                                self._send_trade_telegram(self._fmt_trade_msg("✂️", f"과열 선익절 {_oe_cnt+1}차 30%", ticker, p_nm, price, _q30, profit=profit, strategy=st_nm, note=_fe_reason))
                                with self.lock: self.pnl_this_turn += profit
                                self._record_daily_pnl(profit)
                        else:
                            # 3차: 전량 매도
                            _q_all = p_sh
                            if _q_all > 0 and self._sell_order(ticker, _q_all, pos, p_nm):
                                with self.lock:
                                    pos.last_order_time = time.time()
                                    pos.status = "과열 선익절 3차 전량 ✅"
                                    self._sat_exit_reset(pos)
                                    p_sh = 0
                                profit = _net_profit(price, p_avg, _q_all)
                                self._log_trade(ticker, p_nm, 'SELL', price, st_nm, f"과열청산 3차 전량 [{_fe_reason}]", profit=profit)
                                self._send_trade_telegram(self._fmt_trade_msg("✅", "과열 선익절 3차 전량", ticker, p_nm, price, _q_all, profit=profit, strategy=st_nm, note=_fe_reason))
                                with self.lock:
                                    self.pnl_this_turn += profit
                                    if profit > 0 and self.core_positions:
                                        reinvest_sat = profit * REINVEST_RATIO
                                        for core in self.core_positions:
                                            core.cash += reinvest_sat / len(self.core_positions)
                                self._record_daily_pnl(profit)
                                continue

                # ── 부분 익절: +10%(일반) / +15%(BULL) 도달 시 AI 판단 ─────
                # BULL 장에서는 추세 지속 가능성이 높아 익절 기준 상향
                _sat_partial1_mult = 1.15 if regime == "BULL" else 1.10
                _sat_partial2_mult = 1.30 if regime == "BULL" else 1.20
                if (p_sh > 0 and p_avg > 0 and is_cd_passed
                        and not getattr(pos, 'partial_sold', False)
                        and price >= p_avg * _sat_partial1_mult):
                    s_decision = getattr(pos, 'ai_exit_decision', None)
                    if s_decision is None:
                        if self.claude:
                            pnl_pct_s = (price / p_avg - 1) * 100
                            self._trigger_ai_partial_exit(pos, ticker, p_nm, price, p_avg, pnl_pct_s, regime)
                            with self.lock: pos.status = f"AI 익절 검토 중 (+{pnl_pct_s:.1f}%) 🤖"
                        else:
                            with self.lock: pos.ai_exit_decision = "SELL_PARTIAL"
                    elif s_decision == "HOLD":
                        with self.lock:
                            pos.status = f"AI 홀드 ⏳"
                    else:
                        # 1차 익절 시 원금 주수 고정 (이후 기준으로 사용)
                        with self.lock:
                            if not getattr(pos, 'initial_shares_for_exit', 0):
                                pos.initial_shares_for_exit = p_sh
                        _init_sh = getattr(pos, 'initial_shares_for_exit', 0) or p_sh
                        sell_qty = max(1, min(int(_init_sh * 0.50), p_sh))
                        if self._sell_order(ticker, sell_qty, pos, p_nm):
                            with self.lock:
                                pos.last_order_time   = time.time()
                                pos.partial_sold      = True
                                pos.ai_exit_decision  = None
                                pos.status            = "1차익절 ✅"
                                pos.shares            = max(0, pos.shares - sell_qty)
                            profit = _net_profit(price, p_avg, sell_qty)
                            _pnl_s1 = (price / p_avg - 1) * 100
                            _thr_s1 = "15%(BULL)" if regime == "BULL" else "10%"
                            self._log_trade(ticker, p_nm, 'SELL', price, st_nm, f"1차 익절 +{_thr_s1} ({sell_qty}주 / 원금 {_init_sh}주 기준 50%)", profit=profit)
                            self._send_trade_telegram(self._fmt_trade_msg("🎯", f"1차 익절 +{_thr_s1}", ticker, p_nm, price, sell_qty, profit=profit, strategy=st_nm, note=f"원금 {_init_sh}주 기준 50% | 나머지 {p_sh - sell_qty}주 2차 대기"))
                            with self.lock: self.pnl_this_turn += profit
                            self._record_daily_pnl(profit)

                # ── 2차 분할 익절: +20%(일반) / +30%(BULL) 도달 시 AI 판단 ──
                if (p_sh > 0 and p_avg > 0 and is_cd_passed
                        and getattr(pos, 'partial_sold', False)
                        and not getattr(pos, 'partial_sold_2', False)
                        and price >= p_avg * _sat_partial2_mult):
                    s_decision = getattr(pos, 'ai_exit_decision', None)
                    if s_decision is None:
                        if self.claude:
                            pnl_pct_s = (price / p_avg - 1) * 100
                            self._trigger_ai_partial_exit(pos, ticker, p_nm, price, p_avg, pnl_pct_s, regime)
                            with self.lock: pos.status = f"AI 익절 검토 중 (+{pnl_pct_s:.1f}%) 🤖"
                        else:
                            with self.lock: pos.ai_exit_decision = "SELL_ALL"
                    elif s_decision == "HOLD":
                        with self.lock:
                            pos.status = f"AI 홀드 ⏳"
                    else:
                        # 2차: 원금 기준 50% (1차와 동일 기준 — 잔여 주수 기준 아님)
                        _init_sh2 = getattr(pos, 'initial_shares_for_exit', 0) or p_sh
                        sell_qty  = max(1, min(int(_init_sh2 * 0.50), p_sh))
                        if sell_qty > 0 and self._sell_order(ticker, sell_qty, pos, p_nm):
                            with self.lock:
                                pos.last_order_time   = time.time()
                                pos.partial_sold_2    = True
                                pos.ai_exit_decision  = None
                                pos.status            = "2차익절 ✅"
                                pos.shares            = max(0, pos.shares - sell_qty)
                            profit = _net_profit(price, p_avg, sell_qty)
                            _thr_s2 = "30%(BULL)" if regime == "BULL" else "20%"
                            self._log_trade(ticker, p_nm, 'SELL', price, st_nm, f"2차 익절 +{_thr_s2} ({sell_qty}주 / 원금 {_init_sh2}주 기준 50%)", profit=profit)
                            self._send_trade_telegram(self._fmt_trade_msg("🏆", f"2차 익절 +{_thr_s2}", ticker, p_nm, price, sell_qty, profit=profit, strategy=st_nm, note=f"원금 {_init_sh2}주 기준 50% | 나머지 {pos.shares - sell_qty if pos.shares > sell_qty else 0}주 보유 지속"))
                            with self.lock: self.pnl_this_turn += profit
                            self._record_daily_pnl(profit)

                # ── 피라미딩: +3% 수익 중 & 상승 추세 지속 → 추가 20% 매수 ──
                if (p_sh > 0 and p_avg > 0 and is_cd_passed
                        and not getattr(pos, 'pyramid_done', False)
                        and price >= p_avg * 1.03
                        and p_cash > price
                        and sig != 'SELL'
                        and regime != "BEAR"):
                    # BULL 장에서는 추세가 강하므로 30%, 그 외 20%
                    pyramid_cash = p_cash * (0.30 if regime == "BULL" else 0.20)
                    pyramid_qty = int((pyramid_cash * 0.98) // price)
                    if pyramid_qty > 0 and self._buy_order(ticker, pyramid_qty, pos, p_nm):
                        with self.lock:
                            pos.last_order_time = time.time(); pos.pyramid_done = True; pos.status = "피라미딩 📈"
                            # [BUG-C4] 피라미딩 후 평단가·보유주수 즉시 갱신 (KIS 동기화 전 손절 방지)
                            new_shares = pos.shares + pyramid_qty
                            if new_shares > 0:
                                pos.avg_price = round((pos.avg_price * pos.shares + price * pyramid_qty) / new_shares, 2)
                            pos.shares = new_shares
                        self._log_trade(ticker, p_nm, 'BUY', price, st_nm, f"피라미딩 +3% 추세 지속 ({pyramid_qty}주)")
                        self._send_trade_telegram(self._fmt_trade_msg("📈", "피라미딩 추가 매수", ticker, p_nm, price, pyramid_qty, strategy=st_nm, note="+3% 돌파 · 상승 추세 지속 확인"))

                # ── 2차 분할 매수: 1차 매수가 대비 -2% 눌림목 ──
                if (p_sh > 0 and is_cd_passed
                        and not getattr(pos, 'second_buy_done', False)
                        and getattr(pos, 'second_buy_price', 0) > 0
                        and price <= pos.second_buy_price
                        and getattr(pos, 'second_buy_cash', 0) > price
                        and sig != 'SELL'):
                    _split2_ok = True
                    if self.claude:
                        _sn2 = ""
                        if self.news_monitor:
                            try: _sn2 = self.news_monitor.get_news_summary(p_nm, display=2)
                            except Exception: pass
                        _split2_ok = self.claude.ai_approve_split_buy(ticker, p_nm, price, p_avg, 2, regime, _sn2)
                    if _split2_ok:
                        sq = int((pos.second_buy_cash * 0.98) // price)
                        if sq > 0 and self._buy_order(ticker, sq, pos, p_nm):
                            with self.lock:
                                pos.last_order_time = time.time(); pos.second_buy_done = True
                                pos.second_buy_cash = 0; pos.status = "2차매수 ✅"
                                new_shares = pos.shares + sq
                                if new_shares > 0:
                                    pos.avg_price = round((pos.avg_price * pos.shares + price * sq) / new_shares, 2)
                                pos.shares = new_shares
                            self._log_trade(ticker, p_nm, 'BUY', price, st_nm, f"2차 분할 매수 눌림목 -2% ({sq}주)")
                            self._send_trade_telegram(self._fmt_trade_msg("🛒", "2차 분할 매수 (30%)", ticker, p_nm, price, sq, strategy=st_nm, note="-2% 눌림목 포착 | 3차 -4% 대기"))
                    else:
                        self.add_log(f"🛑 2차 분할매수 AI 중단: {p_nm}({ticker}) — 시장 악화 감지")

                # ── 3차 분할 매수: 1차 진입가 대비 -4% 눌림목 ──
                if (p_sh > 0 and is_cd_passed
                        and getattr(pos, 'second_buy_done', False)
                        and not getattr(pos, 'third_buy_done', False)
                        and getattr(pos, 'third_buy_price', 0) > 0
                        and price <= pos.third_buy_price
                        and getattr(pos, 'third_buy_cash', 0) > price
                        and sig != 'SELL'):
                    _split3_ok = True
                    if self.claude:
                        _sn3 = ""
                        if self.news_monitor:
                            try: _sn3 = self.news_monitor.get_news_summary(p_nm, display=2)
                            except Exception: pass
                        _split3_ok = self.claude.ai_approve_split_buy(ticker, p_nm, price, p_avg, 3, regime, _sn3)
                    if _split3_ok:
                        sq3 = int((pos.third_buy_cash * 0.98) // price)
                        if sq3 > 0 and self._buy_order(ticker, sq3, pos, p_nm):
                            with self.lock:
                                pos.last_order_time = time.time()
                                pos.third_buy_done  = True; pos.third_buy_cash = 0; pos.status = "3차매수 ✅"
                                new_shares = pos.shares + sq3
                                if new_shares > 0:
                                    pos.avg_price = round((pos.avg_price * pos.shares + price * sq3) / new_shares, 2)
                                pos.shares = new_shares
                            self._log_trade(ticker, p_nm, 'BUY', price, st_nm, f"3차 분할 매수 눌림목 -4% ({sq3}주)")
                            self._send_trade_telegram(self._fmt_trade_msg("🛒", "3차 분할 매수 (40%)", ticker, p_nm, price, sq3, strategy=st_nm, note="-4% 눌림목 포착 | 예산 전액 투입 완료"))
                    else:
                        self.add_log(f"🛑 3차 분할매수 AI 중단: {p_nm}({ticker}) — 시장 악화 감지")

                # 당일 AI 거절 블랙리스트 종목은 매수 시도 자체를 차단
                if p_sh == 0 and self._is_satellite_blacklisted(ticker):
                    pos.status = "당일 블랙리스트 🚫"
                    pos.status_msg = f"오늘 거절됨: {self._satellite_rejects.get(ticker, '')}"
                    continue

                # 실적 발표 D-3 이내 → 신규 진입 차단 (깜짝 손실 방지) — US봇 동일
                if p_sh == 0 and is_cd_passed and is_golden_hours and self.news_monitor:
                    try:
                        _earn_kr = self.news_monitor.get_upcoming_earnings(ticker)
                        if _earn_kr and _earn_kr.get('days_until', 99) <= 3:
                            _dti_kr = _earn_kr['days_until']
                            pos.status = f"⚠️ 실적발표 D-{_dti_kr} 진입 차단"
                            pos.status_msg = f"실적 발표 예정: {_earn_kr.get('expected_date','')} — 발표 후 진입 검토"
                            self.add_log(f"⚠️ [{ticker}] {p_nm} 실적발표 D-{_dti_kr} — 신규 진입 차단")
                            continue
                    except Exception:
                        pass

                # ── 진입 점수 게이트 (RSI 필수 아님 — 10개 지표 합산으로 판단) ──────
                if p_sh == 0 and is_cd_passed and is_golden_hours and entry_score < entry_threshold:
                    pos.status = f"점수 대기 ({entry_score}/{entry_threshold}pt) ⏳"
                    pos.status_msg = f"진입 점수 미달 | 충족: {' | '.join(entry_reasons[:3]) if entry_reasons else '없음'}"
                    continue

                if p_sh == 0 and is_cd_passed and is_golden_hours and entry_score >= entry_threshold:

                    # ── BEAR 국면: 10개 저점 전략 스코어 기반 차등 진입 + AI 최종 심사 ──
                    # bear_score ≥ 3 이상만 진입 (반등 확신도 높을 때만 → 오진입 방지)
                    if regime == "BEAR":
                        bear_score, bear_reasons = get_bear_bottom_score(ex_df)
                        if bear_score < 3:
                            pos.status = f"하락장 매수 보류 🐻 (저점신호 {bear_score}/3)"
                            pos.status_msg = f"BEAR 국면 — 저점 신호 부족 ({bear_score}개), 최소 3개 필요"
                            continue
                        # 신호 강도에 따른 차등 포지션 사이징
                        # BEAR 시 위성 저점매수 = bear_score ≥ 3 확인 후 진입
                        # 하락장 특성상 포지션은 통상 50%로 제한 (리스크 관리)
                        # score 5개 → 보너스 20%, 3~4개 → 보너스 15%
                        bear_timing_bonus = 0.20 if bear_score >= 5 else 0.15
                        bear_ratio  = min(0.50, score_ratio * 0.5 + bear_timing_bonus)  # BEAR: 최대 50% 포지션
                        bear_label  = f"BEAR·점수{entry_score}pt+저점{bear_score}개"
                        bear_reason_str = " | ".join(bear_reasons)
                        bounce_cash = p_cash * bear_ratio
                        qty = int((bounce_cash * 0.98) // price)
                        if qty > 0:
                            # 하락장은 더 신중해야 하므로 AI 심사 필수
                            if self.claude:
                                pos.status     = "AI 심사 중 🤖"
                                pos.status_msg = f"하락장 저점 신호 | {bear_reason_str} — AI 최종 승인 대기 중..."
                                trade_ctx = self._build_trade_context(ticker, p_nm, price, ex_df, st_nm, regime)
                                decision, ai_reason = self.claude.ai_approve_trade(sig, p_nm, ticker, price, st_nm, ind_val, self.hot_sectors, get_recent_trades(self.user_id, ticker), load_ai_rules(self.user_id) + "\n" + getattr(self, 'current_ai_market_view', '') + ("\n\n[📊 섹터 가이드 / 커스텀 전략]\n" + self.sector_guide if self.sector_guide else ''), context=trade_ctx)
                                if decision:
                                    if self._buy_order(ticker, qty, pos, p_nm):
                                        with self.lock:
                                            pos.last_order_time = time.time(); pos.status = "체결 대기 ⏳"
                                            pos.status_msg      = f"AI 승인: {ai_reason}"
                                        self._log_trade(ticker, p_nm, 'BUY', price, st_nm, f"하락장 저점포착 AI승인 [{bear_reason_str}]")
                                        self._send_trade_telegram(self._fmt_trade_msg("🎣", f"하락장 저점 매수 ({bear_label})", ticker, p_nm, price, qty, strategy=st_nm, ai_reason=ai_reason, note=bear_reason_str))
                                        self.claude.record_trade_event(f"KR 위성 BEAR 저점매수: {p_nm}({ticker}) {qty}주 @ {price:,.0f}원 | {bear_label} | AI: {ai_reason[:60]}")
                                else:
                                    pos.status     = "AI 거절(하락장) 🛑"
                                    pos.status_msg = f"거절 이유: {ai_reason}"
                                    self._add_satellite_reject(ticker, ai_reason)
                                    self.claude.record_trade_event(f"KR 위성 AI 거절(BEAR): {p_nm}({ticker}) @ {price:,.0f}원 | 사유: {ai_reason[:80]}")
                                    self._send_reject_telegram(
                                        f"🛑 <b>매수 거절</b>  ·  {self.alert_icon} {self.mode_name}\n"
                                        f"━━━━━━━━━━━━━━━━━━━━\n"
                                        f"📌 <b>{p_nm}</b>  <code>{ticker}</code>\n"
                                        f"🤖 {ai_reason}\n"
                                        f"📋 하락장 저점 — 근거 불충분 (당일 블랙리스트 등록)"
                                    )
                                    threading.Thread(target=self._rescreen_satellites, daemon=True).start()
                            elif self._buy_order(ticker, qty, pos, p_nm):
                                with self.lock: pos.last_order_time = time.time(); pos.status = "체결 대기 ⏳"
                                self._log_trade(ticker, p_nm, 'BUY', price, st_nm, f"하락장 저점포착 [{bear_reason_str}]")
                                self._send_trade_telegram(self._fmt_trade_msg("🎣", f"하락장 저점 매수 ({bear_label})", ticker, p_nm, price, qty, strategy=st_nm, note=bear_reason_str))
                        continue

                    # BULL 국면에서 하락일은 저점 매수 기회 → ETF/분봉 게이트 우회
                    # NEUTRAL/BEAR는 기존대로 시장 강도 확인 필수
                    if regime != "BULL":
                        if not self._check_etf_market_positive():
                            pos.status = "시장 약세 ⏸"
                            pos.status_msg = "ETF 지수 -1% 이하, 매수 보류 (BULL 국면 제외)"
                            continue
                        if not self._check_minute_trend_up(ticker):
                            pos.status = "추세 하락 📉"
                            pos.status_msg = "최근 5분봉 하락 추세, 매수 보류 (BULL 국면 제외)"
                            continue

                    # ── 국면별 타이밍 신호 + 점수 기반 회차별 비율 결정 ──────
                    if regime == "BULL":
                        bull_score, bull_reasons = get_bull_momentum_score(ex_df)
                        regime_bonus = 0.10 if bull_score >= 3 else (0.05 if bull_score >= 1 else 0.0)
                        entry_ratio  = min(0.90, score_ratio + regime_bonus)
                        regime_label = f"BULL·점수{entry_score}pt+타이밍{bull_score}개"
                        regime_reason_str = " | ".join(bull_reasons) if bull_reasons else "상승 추세 추종"
                    else:  # NEUTRAL
                        neutral_score, neutral_reasons = get_neutral_range_score(ex_df)
                        if neutral_score == 0:
                            pos.status = "횡보 관망 ⏸"
                            pos.status_msg = "NEUTRAL 국면 — 레인지 신호 없음, 매수 차단"
                            continue
                        regime_bonus  = 0.10 if neutral_score >= 3 else (0.05 if neutral_score >= 2 else 0.0)
                        entry_ratio   = min(0.90, score_ratio + regime_bonus)
                        regime_label  = f"NEUTRAL·점수{entry_score}pt+타이밍{neutral_score}개"
                        regime_reason_str = " | ".join(neutral_reasons)

                    # 점수 비율을 각 회차에 동일하게 적용 — 배정 예산 초과 시 남은 예산으로 자동 캡
                    # 예) score=60%, budget=1000만 → 1차 600 / 2차 min(600,400)=400 / 3차 0
                    # 예) score=30%, budget=1000만 → 1차 300 / 2차 300 / 3차 400
                    entry_cash   = p_cash * entry_ratio
                    _remain1     = max(0.0, p_cash - entry_cash)
                    reserve_cash = min(p_cash * entry_ratio, _remain1)
                    third_cash   = max(0.0, p_cash - entry_cash - reserve_cash)

                    # ── 매수 검토 리포트 발송 (친구 AI 스타일) ──────────────
                    try:
                        _stats = self._calc_price_stats(ex_df, price)
                        _stats['extra'] = f"전략 [{st_nm}] / {regime_label}"
                        self._send_telegram(self._fmt_scan_report(
                            theme="📊 위성 매수 신호",
                            candidates=[{'name': p_nm, 'ticker': ticker, 'price': price, 'stats': _stats}],
                            regime=regime,
                            action_note="AI 심사 후 자동주문" if self.claude else "알고리즘 자동주문"
                        ), 'misc')
                    except Exception:
                        pass

                    # 52주 신고가 근접 체크 → AI 판단 맥락에 추가 (US봇 동일)
                    _52w_note_kr = ""
                    try:
                        if not ex_df.empty and 'high' in ex_df.columns and len(ex_df) >= 50:
                            _52w_high_kr = float(ex_df['high'].rolling(252, min_periods=50).max().iloc[-1])
                            _52w_pct_kr  = (price / _52w_high_kr - 1) * 100
                            if _52w_pct_kr >= -3.0:
                                _52w_note_kr = f"52주 신고가 근접 ({_52w_pct_kr:+.1f}%) — 돌파 시 강세 신호"
                            elif _52w_pct_kr <= -40.0:
                                _52w_note_kr = f"52주 고가 대비 {_52w_pct_kr:.0f}% — 추세 붕괴 주의"
                    except Exception:
                        pass

                    if self.claude:
                        pos.status     = "AI 심사 중 🤖"
                        pos.status_msg = f"매수 신호 발생 | {st_nm} — AI 최종 승인 대기 중..."
                        trade_ctx = self._build_trade_context(ticker, p_nm, price, ex_df, st_nm, regime)
                        if _52w_note_kr:
                            trade_ctx += f"\n[52주 신고가] {_52w_note_kr}"
                        decision, ai_reason = self.claude.ai_approve_trade(sig, p_nm, ticker, price, st_nm, ind_val, self.hot_sectors, get_recent_trades(self.user_id, ticker), load_ai_rules(self.user_id) + "\n" + getattr(self, 'current_ai_market_view', '') + ("\n\n[📊 섹터 가이드 / 커스텀 전략]\n" + self.sector_guide if self.sector_guide else ''), context=trade_ctx)
                        if decision:
                            qty = int((entry_cash * 0.98) // price)
                            if qty > 0 and self._buy_order(ticker, qty, pos, p_nm):
                                with self.lock:
                                    pos.last_order_time = time.time(); pos.status = "체결 대기 ⏳"
                                    pos.status_msg      = f"AI 승인: {ai_reason}"
                                    pos.second_buy_price         = price * 0.98   # -2% 눌림목
                                    pos.second_buy_cash          = reserve_cash    # 원금의 30%
                                    pos.second_buy_done          = False
                                    pos.third_buy_price          = price * 0.96   # -4% 눌림목
                                    pos.third_buy_cash           = third_cash      # 원금의 40%
                                    pos.third_buy_done           = False
                                    pos.pyramid_done             = False
                                    pos.partial_sold             = False
                                    pos.partial_sold_2           = False
                                    pos.initial_shares_for_exit  = 0
                                    # third_buy는 위 decision 블록에서 이미 저장됨
                                self._log_trade(ticker, p_nm, 'BUY', price, st_nm, f"AI 승인 [{regime_label}] 1차(30%) ({ai_reason})")
                                self._send_trade_telegram(self._fmt_trade_msg("📈", "AI 매수 승인 (1차 30%)", ticker, p_nm, price, qty, strategy=f"{st_nm}  ·  {regime_label}", ai_reason=ai_reason, note=f"2차 -2%({price*0.98:,.0f}원) / 3차 -4%({price*0.96:,.0f}원) 예약"))
                                self.claude.record_trade_event(f"KR 위성 매수: {p_nm}({ticker}) {qty}주 @ {price:,.0f}원 | {regime_label} | AI: {ai_reason[:60]}")
                        else:
                            pos.status     = "AI 거절 🛑"
                            pos.status_msg = f"거절 이유: {ai_reason}"
                            # 당일 블랙리스트 등록 — 같은 이유로 재편입 금지
                            self._add_satellite_reject(ticker, ai_reason)
                            self.claude.record_trade_event(f"KR 위성 AI 매수 거절: {p_nm}({ticker}) @ {price:,.0f}원 | 사유: {ai_reason[:80]}")
                            self._send_reject_telegram(
                                f"🛑 <b>매수 거절</b>  ·  {self.alert_icon} {self.mode_name}\n"
                                f"━━━━━━━━━━━━━━━━━━━━\n"
                                f"📌 <b>{p_nm}</b>  <code>{ticker}</code>\n"
                                f"🤖 {ai_reason}\n"
                                f"➡️ 당일 블랙리스트 등록 후 즉시 대체 종목 탐색"
                            )
                            threading.Thread(target=self._rescreen_satellites, daemon=True).start()
                    else:
                        qty = int((entry_cash * 0.98) // price)
                        if qty > 0 and self._buy_order(ticker, qty, pos, p_nm):
                            with self.lock:
                                pos.last_order_time = time.time(); pos.status = "체결 대기 ⏳"
                                pos.second_buy_price         = price * 0.98
                                pos.second_buy_cash          = reserve_cash
                                pos.second_buy_done          = False
                                pos.third_buy_price          = price * 0.96
                                pos.third_buy_cash           = third_cash
                                pos.third_buy_done           = False
                                pos.pyramid_done             = False
                                pos.partial_sold             = False
                                pos.partial_sold_2           = False
                                pos.initial_shares_for_exit  = 0
                            self._log_trade(ticker, p_nm, 'BUY', price, st_nm, f"알고리즘 [{regime_label}] 1차(30%): {regime_reason_str}")
                            self._send_trade_telegram(self._fmt_trade_msg("📈", "알고리즘 매수 (1차 30%)", ticker, p_nm, price, qty, strategy=f"{st_nm}  ·  {regime_label}", note=f"2차 -2%({price*0.98:,.0f}원) / 3차 -4%({price*0.96:,.0f}원) 예약 | {regime_reason_str}"))

                elif sig == 'SELL' and p_sh > 0 and is_cd_passed:
                    if self.claude:
                        pos.status     = "AI 심사 중 🤖"
                        pos.status_msg = f"매도 신호 발생 | {st_nm} — AI 최종 승인 대기 중..."
                        trade_ctx = self._build_trade_context(ticker, p_nm, price, ex_df, st_nm, regime)
                        decision, ai_reason = self.claude.ai_approve_trade(sig, p_nm, ticker, price, st_nm, ind_val, self.hot_sectors, get_recent_trades(self.user_id, ticker), load_ai_rules(self.user_id) + "\n" + getattr(self, 'current_ai_market_view', '') + ("\n\n[📊 섹터 가이드 / 커스텀 전략]\n" + self.sector_guide if self.sector_guide else ''), context=trade_ctx)
                        if decision:
                            if self._sell_order(ticker, p_sh, pos, p_nm):
                                with self.lock:
                                    pos.last_order_time          = time.time(); pos.status = "체결 대기 ⏳"
                                    pos.status_msg               = f"AI 승인: {ai_reason}"
                                    self._sat_exit_reset(pos)
                                    pos.initial_shares_for_exit  = 0
                                profit = _net_profit(price, p_avg, p_sh)
                                self._log_trade(ticker, p_nm, 'SELL', price, st_nm, f"AI 승인 ({ai_reason})", profit=profit)
                                self._send_trade_telegram(self._fmt_trade_msg("📉", "AI 매도 승인", ticker, p_nm, price, p_sh, profit=profit, strategy=st_nm, ai_reason=ai_reason))
                                with self.lock:
                                    self.pnl_this_turn += profit
                                    # [BUG-6] pos.cash는 매도 직후 ≈0 이므로 잔액 조건 제거.
                                    # 수익금의 REINVEST_RATIO(50%)를 코어 슬롯에 직접 배분.
                                    if profit > 0 and self.core_positions:
                                        reinvest_sat = profit * REINVEST_RATIO
                                        for core in self.core_positions:
                                            core.cash += reinvest_sat / len(self.core_positions)
                                self._record_daily_pnl(profit)
                        else:
                            pos.status     = "AI 거절(보유) 🛑"
                            pos.status_msg = f"거절 이유: {ai_reason}"
                    else:
                        if self._sell_order(ticker, p_sh, pos, p_nm):
                            with self.lock:
                                pos.last_order_time          = time.time(); pos.status = "체결 대기 ⏳"
                                self._sat_exit_reset(pos)
                                pos.initial_shares_for_exit  = 0
                            profit = _net_profit(price, p_avg, p_sh)
                            self._log_trade(ticker, p_nm, 'SELL', price, st_nm, "알고리즘 직통", profit=profit)
                            self._send_trade_telegram(self._fmt_trade_msg("📉", "알고리즘 매도", ticker, p_nm, price, p_sh, profit=profit, strategy=st_nm))
                            with self.lock:
                                self.pnl_this_turn += profit
                                # [W-NEW-01] AI 없는 경로에도 재투자 로직 동일 적용
                                if profit > 0 and self.core_positions:
                                    reinvest_sat = profit * REINVEST_RATIO
                                    for core in self.core_positions:
                                        core.cash += reinvest_sat / len(self.core_positions)
                            self._record_daily_pnl(profit)
            except Exception as e:
                logger.error(f"[{self.mode_name}] 위성 매매 오류 ({ticker}): {e}", exc_info=True)
            time.sleep(0.2)

        # ── 위성 매도 후 재스캔: 이번 턴에 빈 슬롯이 생겼으면 즉시 새 종목 탐색 ─
        # 쿨다운(120초): 잦은 스캔 방지 (AI 호출 비용, API 부하)
        with self.lock:
            _sat_full_after = {t for t, p in self.satellite_positions.items() if p.shares > 0}
        _just_sold = _sat_full_before - _sat_full_after
        if _just_sold:
            _rescreen_cd = time.time() - getattr(self, '_last_rescreen_trigger_ts', 0)
            if _rescreen_cd > 120:
                self._last_rescreen_trigger_ts = time.time()
                self.add_log(f"🔄 위성 전량 매도 감지 ({', '.join(_just_sold)}) → 즉시 재스캔")
                threading.Thread(target=self._rescreen_satellites, daemon=True).start()

        self._save_state()

    def _ai_swing_check_kr(self, pos, ticker: str, price: float, reason: str) -> str:
        """KR봇 ATR 손절/트레일링 발동 시 AI 전권 판단 — SELL_REBUY / ACCUMULATE / EXIT"""
        if not self.claude:
            return 'EXIT'
        avg = getattr(pos, 'avg_price', 0)
        if avg <= 0:
            return 'EXIT'
        acc_cnt = getattr(pos, 'swing_acc_count', 0)
        if acc_cnt >= 2:
            return 'EXIT'
        pnl_pct = (price / avg - 1) * 100
        news = ""
        if self.news_monitor:
            try:
                news = self.news_monitor.get_news_summary(getattr(pos, 'name', ticker), display=3)
            except Exception:
                pass
        return self.claude.ai_swing_trade_check(
            ticker=ticker, name=getattr(pos, 'name', ticker),
            price_usd=price, avg_usd=avg, pnl_pct=pnl_pct,
            regime=self.market_regime, exit_reason=reason,
            news=news, hot_sectors=getattr(self, 'hot_sectors', []) or [],
            accumulate_count=acc_cnt,
        )

    def _trigger_ai_partial_exit(self, pos, ticker: str, name: str,
                                  price: float, avg: float,
                                  pnl_pct: float, regime: str):
        """AI 익절 판단을 백그라운드 스레드로 요청 (메인 루프 비차단).

        HOLD 후에는 가격 기준:
        - +1% 이상 상승 시 재요청 (새 고점)
        - -2% 이상 하락 시도 재요청 (가격 반납 감지)
        """
        if getattr(pos, 'ai_exit_pending', False):
            return
        asked = getattr(pos, 'ai_exit_asked_price', 0.0)
        if getattr(pos, 'ai_exit_decision', None) == "HOLD" and asked > 0:
            risen  = price >= asked * 1.01   # +1% 상승
            fallen = price <= asked * 0.98   # -2% 하락
            if not risen and not fallen:
                return
        pos.ai_exit_pending     = True
        pos.ai_exit_asked_price = price  # 현재 문의 가격 기록

        def _worker():
            try:
                # 뉴스 fetch (NewsMonitor 우선, 없으면 빈 문자열)
                _news = ""
                if self.news_monitor:
                    try:
                        _news = self.news_monitor.get_news_summary(name, display=3)
                    except Exception:
                        pass
                decision = self.claude.ai_partial_exit(
                    ticker=ticker, stock_name=name, price=price,
                    avg_price=avg, pnl_pct=pnl_pct,
                    shares=int(getattr(pos, 'shares', 0)),
                    partial_sold=bool(getattr(pos, 'partial_sold', False)),
                    regime=regime,
                    news_headlines=_news,
                )
                with self.lock:
                    pos.ai_exit_decision = decision
                    pos.ai_exit_pending  = False
            except Exception:
                with self.lock:
                    pos.ai_exit_pending = False

        threading.Thread(target=_worker, daemon=True).start()

    # ── 주말 사전 분석 ────────────────────────────────────────────────────
    def _weekend_satellite_scan(self):
        """
        주말(토·일)에 실행 — 위성 후보 스캔 후 월요일 교체 계획 수립.
        실제 매매 없이 분석만 수행, _monday_swap_plan에 저장.
        """
        now = _now_kst()
        today_str = now.strftime('%Y-%m-%d')
        if self._weekend_scan_done == today_str:
            return  # 오늘 이미 완료
        if now.weekday() < 5:
            return  # 평일엔 실행 안 함

        self.add_log("📅 [주말 사전분석] 위성 후보 스캔 시작...")
        try:
            # 현재 보유 종목 점수 측정
            with self.lock:
                current_sat = {t: p for t, p in self.satellite_positions.items() if p.shares > 0}
            current_tickers = set(current_sat.keys())

            # 새 후보 스캔 (현재 보유 제외)
            from KR.strategy import calculate_entry_score, get_entry_threshold, get_market_regime
            raw_candidates, new_hot = select_satellites(
                kis=self.kis, n=self.num_satellites * 4,
                verbose=False, claude_client=self.claude,
                sector_guide=self.sector_guide, real_kis=self.real_kis,
                exclude=current_tickers
            )
            if new_hot:
                self.hot_sectors = new_hot

            swap_plan = {}
            # 보유 종목 중 교체 대상 파악 (손실 or 약세)
            for ticker, pos in current_sat.items():
                try:
                    ohlcv = self._get_cached_base_ohlcv(ticker)
                    if ohlcv.empty: continue
                    score, reasons, _ = calculate_entry_score(
                        ohlcv, ticker, self.market_regime,
                        sector_score=0, kis_score=0, dl_score=0, roe_bonus=0
                    )
                    threshold = get_entry_threshold(self.market_regime)
                    # 점수가 기준 미달이면 교체 후보
                    if score < threshold - 1:
                        swap_plan[ticker] = {"reason": f"진입점수 {score}/{threshold}pt 미달", "score": score}
                except Exception:
                    pass

            # 새 후보 중 점수 높은 순으로 교체 매핑
            new_plan = {}
            cand_iter = iter(raw_candidates)
            for old_ticker, old_info in swap_plan.items():
                try:
                    cand = next(cand_iter)
                    new_plan[old_ticker] = {
                        "new_ticker": cand["ticker"],
                        "new_name":   cand["name"],
                        "score":      cand.get("score", 0),
                        "reason":     old_info["reason"]
                    }
                    self.add_log(
                        f"📋 [주말분석] {self.satellite_positions[old_ticker].name}({old_ticker}) → "
                        f"{cand['name']}({cand['ticker']}) 교체 예정 | 사유: {old_info['reason']}"
                    )
                except StopIteration:
                    break

            self._monday_swap_plan = new_plan
            self._weekend_scan_done = today_str
            self._save_state()

            if new_plan:
                self._send_telegram(
                    f"📅 <b>주말 사전분석 완료</b>  ·  {self.alert_icon} {self.mode_name}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    + "\n".join([f"· {self.satellite_positions.get(o, type('', (), {'name': o})()).name}({o}) → {v['new_name']}({v['new_ticker']})" for o, v in new_plan.items()])
                    + (f"\n📌 교체 예정 없음 — 현 포지션 유지" if not new_plan else "")
                    + f"\n⏰ 월요일 장 시작 시 자동 실행"
                )
            else:
                self.add_log("📅 [주말분석] 교체 대상 없음 — 현 포지션 유지")

        except Exception as e:
            logger.error(f"[{self.mode_name}] 주말 사전분석 오류: {e}", exc_info=True)

    def _execute_monday_swap(self):
        """
        월요일 09:00~09:10 — 주말에 수립한 교체 계획 실행.
        """
        if not self._monday_swap_plan:
            return
        now = _now_kst()
        if now.weekday() != 0:  # 월요일 아님
            return

        self.add_log(f"🚀 [월요일 교체] 주말 계획 실행 — {len(self._monday_swap_plan)}건")
        executed = []
        for old_ticker, plan in list(self._monday_swap_plan.items()):
            try:
                with self.lock:
                    pos = self.satellite_positions.get(old_ticker)
                if not pos or pos.shares == 0:
                    continue  # 이미 청산됨
                price = self.live_prices.get(old_ticker) or (self.kis.get_current_price(old_ticker) if self.kis else 0)
                if not price:
                    continue
                # 기존 종목 매도
                if self._sell_order(old_ticker, pos.shares, pos, pos.name):
                    profit = (price - pos.avg_price) * pos.shares if pos.avg_price else 0
                    self._log_trade(old_ticker, pos.name, 'SELL', price, "주말계획교체", plan['reason'], profit=profit)
                    self.add_log(f"📤 [{old_ticker}] {pos.name} 매도 완료 (주말 계획)")
                    # 새 종목 편입 예약 (satellite_info 갱신)
                    with self.lock:
                        self.satellite_info = [s for s in self.satellite_info if s.get('ticker') != old_ticker]
                        self.satellite_info.insert(0, {"ticker": plan['new_ticker'], "name": plan['new_name'], "return_pct": plan['score'], "sector": "-"})
                    executed.append(old_ticker)
            except Exception as e:
                logger.error(f"[{self.mode_name}] 월요일 교체 실행 오류({old_ticker}): {e}")

        # 실행된 항목 제거
        for t in executed:
            self._monday_swap_plan.pop(t, None)
        if executed:
            self._save_state()
            self.add_log(f"✅ [월요일 교체] {len(executed)}건 완료 → 새 종목 매수 진행")

    def _rescreen_satellites(self):
        try:
            now = _now_kst()
            # 위성은 1-3개월 보유 스윙 포지션 — 시간 제한 없이 빈 슬롯 발생 시 즉시 보충.
            # (매시간 자동 재스크리닝은 스케줄러에서 제거 → 불필요한 AI 호출 방지)
            if not ("09:01" <= now.strftime('%H:%M') <= "15:20") or now.weekday() >= 5:
                return
            self.add_log(f"🦅 {self.mode_name} 위성 실시간 교체 탐색 중...")
            keep_tickers = set()      # 유지 티커 (교체 슬롯으로 계산하지 않음)
            strong_keeps = set()      # 성장세 양호 — 절대 교체 대상 제외
            freed_cash = 0
            with self.lock: sat_items = list(self.satellite_positions.items())

            # 예산은 동적 균등 배분(initial_cap / n_total)으로 매 사이클 처리.
            # 위성 공석 임시 배분/회수 로직 제거 (이중 배분 방지)

            _GROWTH_KEEP = 3.0    # +3% 이상 → 성장세 양호, 교체 없이 강제 유지
            _LOSS_CUT    = -3.0   # -3% 이하 → 손절 교체 (관망 구간: -3%~+3%)

            for ticker, pos in sat_items:
                if pos.shares == 0:
                    freed_cash += pos.cash
                    with self.lock:
                        if ticker in self.satellite_positions: del self.satellite_positions[ticker]
                    continue
                time.sleep(0.2)
                price = self.kis.get_current_price(ticker) if self.kis else 0
                if price and pos.avg_price > 0:
                    profit_rt = (price / pos.avg_price - 1) * 100
                    if profit_rt >= _GROWTH_KEEP:
                        # 성장세 양호 → 교체 대상에서 완전 제외
                        keep_tickers.add(ticker)
                        strong_keeps.add(ticker)
                        self.add_log(f"🌱 {pos.name}({ticker}) 성장세 양호 ({profit_rt:+.1f}%) — 교체 없이 유지")
                    elif profit_rt > _LOSS_CUT:
                        # 관망 구간 → 유지하되 빈 슬롯 생기면 교체 가능
                        keep_tickers.add(ticker)
                        self.add_log(f"⏸️ {pos.name}({ticker}) 관망 유지 ({profit_rt:+.1f}%)")
                    else:
                        # I-05: trading_job과의 이중 매도 방지 — 락 안에서 shares 확인 후 주문
                        with self.lock:
                            shares_now = pos.shares
                        if shares_now > 0:
                            if self.kis: self.kis.sell_market_order(ticker, shares_now, price=int(price))
                            sell_qty = 0; sell_profit = 0.0  # [BUG-M2] 초기화
                            with self.lock:
                                # trading_job이 먼저 매도했을 경우 재진입 차단
                                if pos.shares > 0:
                                    sell_qty, sell_profit = pos.sell(price)
                                    freed_cash += pos.cash  # C-05: lock 내부에서 접근
                                    self.pnl_this_turn += sell_profit  # [BUG-M2]
                                if ticker in self.satellite_positions: del self.satellite_positions[ticker]
                            if sell_qty > 0:  # [BUG-M2] 실제 매도 발생 시에만 기록
                                self._log_trade(ticker, pos.name, 'SELL', price, '위성교체', '재스크리닝 손절', profit=sell_profit)
                                self._record_daily_pnl(sell_profit)
                        else:
                            with self.lock:
                                if ticker in self.satellite_positions: del self.satellite_positions[ticker]

            # ── 초과 포지션 정리: keep_tickers가 num_satellites 초과 시 ───────────
            # 예) num_satellites=5 인데 이익 중인 포지션이 7개 → 최하위 2개 청산
            if len(keep_tickers) > self.num_satellites:
                # 수익률 순으로 정렬 (최저 수익 먼저 제거)
                profit_map = {}
                for t in list(keep_tickers):
                    pos = self.satellite_positions.get(t)
                    if pos and pos.avg_price > 0:
                        p = self.live_prices.get(t) or (self.kis.get_current_price(t) if self.kis else 0) or pos.avg_price
                        profit_map[t] = (p / pos.avg_price - 1) * 100
                    else:
                        profit_map[t] = 0.0
                sorted_keep = sorted(keep_tickers, key=lambda t: profit_map.get(t, 0))
                excess = sorted_keep[:len(keep_tickers) - self.num_satellites]
                for t in excess:
                    pos = self.satellite_positions.get(t)
                    if pos:
                        with self.lock:
                            shares_now = pos.shares
                        price_e = (self.live_prices.get(t)
                                   or (self.kis.get_current_price(t) if self.kis else 0)
                                   or pos.avg_price or 0)
                        sell_qty, excess_profit = 0, 0.0
                        if shares_now > 0 and price_e:
                            if self.kis and self.kis.sell_market_order(t, shares_now, price=int(price_e)):
                                with self.lock:
                                    if pos.shares > 0:
                                        # [C-03] pos.sell()로 내부 현금 갱신 + 손익 계산
                                        sell_qty, excess_profit = pos.sell(price_e)
                                        self.pnl_this_turn += excess_profit
                        with self.lock:
                            freed_cash += pos.cash  # sell() 후 cash = 매도 대금 포함 전액
                            if t in self.satellite_positions: del self.satellite_positions[t]
                        if sell_qty > 0:
                            # [C-03] 누락된 거래 로그 및 손익 통계 추가
                            self._log_trade(t, pos.name, 'SELL', price_e, '위성초과정리',
                                            f'초과({self.num_satellites}개 한도) 강제 청산',
                                            profit=excess_profit)
                            self._record_daily_pnl(excess_profit)
                        keep_tickers.discard(t)
                        self.add_log(f"✂️ 위성 초과({self.num_satellites}개 한도) 정리: {pos.name}({t}) 청산")

            # strong_keeps는 교체 후보 슬롯에서 제외 — 성장세 종목은 건드리지 않음
            replaceable_keeps = keep_tickers - strong_keeps
            n_needed = self.num_satellites - len(keep_tickers)
            if n_needed <= 0:
                if strong_keeps:
                    self.add_log(f"✅ 위성 {len(strong_keeps)}개 성장세 양호 — 전 슬롯 유지, 재스크리닝 스킵")
                return

            # 당일 블랙리스트 종목을 충분히 걸러낼 수 있도록 여유 있게 조회
            # [BUG-7] _refresh_blacklist 는 내부 딕셔너리를 수정하므로 락 필요
            with self.lock:
                self._refresh_blacklist()
            # [W-NEW-08] _satellite_rejects 를 락 안에서 스냅샷으로 읽어 경합 방지
            with self.lock:
                n_rejects       = len(self._satellite_rejects)
                bl_set          = set(self._satellite_rejects.keys())
            # ★ [BUG-FIX] exclude 집합을 select_satellites() 에 전달 →
            #    첫 번째 AI(ai_select_satellites) 단계부터 블랙리스트·보유 종목 제외.
            #    기존엔 후보풀이 블랙리스트 종목으로 꽉 차서 pre_filter 후 0개가 남는 문제 발생.
            exclude_set = keep_tickers | bl_set
            raw_info, _new_hot = select_satellites(
                kis=self.kis, n=self.num_satellites + n_needed + 3,
                verbose=False, claude_client=self.claude, bear_mode=(self.market_regime == "BEAR"),
                sector_guide=self.sector_guide, real_kis=self.real_kis,
                exclude=exclude_set,
            )
            if _new_hot:
                self.hot_sectors = _new_hot
            if self.hot_sectors:
                _total = len(self.hot_sectors)
                _top4  = self.hot_sectors[:4]
                self.add_log(
                    f"🔥 전 섹터 스캔 완료 (총 {_total}개) — "
                    f"가산점 TOP4: {', '.join(_top4)}"
                )
            else:
                self.add_log("⚠️ 전 섹터 스캔 완료 — 강세 섹터 없음 (상대 강세 기준 후보 선정)")
            # exclude_set 이미 적용됐으므로 pre_filter 는 keep_tickers 중복 체크만 하면 됨
            pre_filter = [
                c for c in raw_info
                if c['ticker'] not in keep_tickers
                and not self._is_satellite_blacklisted(c['ticker'])
            ]
            # AI 종목·전략 검토 (여유분 포함해서 검토 후 필요 개수만큼 잘라냄)
            ai_filtered = self._ai_filter_satellites(pre_filter)
            new_info = ai_filtered[:n_needed]
            if len(new_info) < n_needed:
                empty_count = n_needed - len(new_info)
                with self.lock:
                    bl_tickers = list(self._satellite_rejects.keys())
                self.add_log(f"⚠️ 당일 블랙리스트/AI 퇴출로 인해 {empty_count}개 위성 슬롯 공석 유지")
                if self.telegram:
                    bl_text = ', '.join(bl_tickers[:5]) + (f" 외 {len(bl_tickers)-5}개" if len(bl_tickers) > 5 else "") if bl_tickers else "없음"
                    self.telegram.send_message(
                        f"⚠️ <b>위성 슬롯 {empty_count}개 공석</b>  {self.alert_icon} {self.mode_name}\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"📭 승인된 신규 후보 부족\n"
                        f"🚫 당일 블랙리스트: {bl_text}\n"
                        f"💡 내일 자정 블랙리스트 초기화 후 재시도"
                    )
                # 공석 예산은 동적 균등 배분(initial_cap / n_total)이 매 사이클 자동 처리.
                # 위성 공석 freed_cash를 별도 재배분하지 않음 (이중 배분 방지)

            # [BUG-FIX] 재스크리닝 도중 삭제된 종목이 keep_tickers에 남아 있으면 KeyError 발생
            keep_tickers = {t for t in keep_tickers if t in self.satellite_positions}
            for ticker in keep_tickers: freed_cash += self.satellite_positions[ticker].cash; self.satellite_positions[ticker].cash = 0

            # [C-NEW-05] 배분 분모를 실제 현금이 필요한 슬롯(shares==0인 keep + new)으로만 계산
            # shares>0 인 keep 포지션에 cash를 주면 의도치 않은 추가 매수(피라미딩)가 발생함
            empty_keep = [t for t in keep_tickers
                          if t in self.satellite_positions
                          and self.satellite_positions[t].shares == 0]
            cash_receivers = len(empty_keep) + len(new_info)
            if freed_cash > 0 and cash_receivers > 0:
                with self.lock:
                    alloc = freed_cash / cash_receivers
                    for t in empty_keep:
                        if t in self.satellite_positions:
                            self.satellite_positions[t].cash = alloc
                    for c in new_info:
                        self.satellite_positions[c['ticker']] = Position(c['ticker'], c['name'], alloc)
                self.satellite_info = [c for c in self.satellite_info if c['ticker'] in keep_tickers] + new_info
                self._inject_user_satellites()  # 사용자 지정 종목 우선 고정

            self.last_screen_date = now.date()
            self._save_state()
        except Exception as e:
            logger.error(f"[{self.mode_name}] 위성 재스크리닝 오류: {e}", exc_info=True)

    def analyze_continuous_market_flow(self):
        if not hasattr(self, 'market_flow_history'): self.market_flow_history = []
        today = _now_kst().strftime('%Y-%m-%d')
        if getattr(self, 'flow_history_date', '') != today: self.market_flow_history = []; self.flow_history_date = today

        try:
            _now = _now_kst()
            now_time_str = _now.strftime('%H:%M')
            if not ("09:00" <= now_time_str <= "15:30") or _now.weekday() >= 5: return

            market_data = []
            if self.kis:
                for ticker, name in self.market_indices:
                    df = self._get_cached_base_ohlcv(ticker)
                    cp = self.live_prices.get(ticker) or self.kis.get_current_price(ticker)
                    if not df.empty and cp: market_data.append(f"{name}: {cp:,}원 ({((cp/df['close'].iloc[-1])-1)*100:+.2f}%)")
            
            prompt = f"시각 {now_time_str}. 지수: {' | '.join(market_data)}.강세: {', '.join(self.hot_sectors)}. 장중 분위기 짧게 2줄 요약."
            if self.claude:
                analysis = self.claude.chat(prompt, stock_analysis_context="마크다운 없이 평문 2줄로.")
                self.current_ai_market_view = analysis
                self.market_flow_history.append(f"[{now_time_str}] {analysis}")
        except Exception as e:
            logger.warning(f"[{self.mode_name}] 장중 시장 흐름 분석 오류: {e}")

    def generate_daily_report(self, time_slot="11:00"):
        try:
            news_lines = []
            with self.lock: target_stocks = list(dict.fromkeys([(c.name, c.ticker) for c in self.core_positions] + [(pos.name, t) for t, pos in self.satellite_positions.items()]))
            for name, ticker in target_stocks:
                try:
                    raw = fetch_recent_news(name)
                    # 조회 실패 메시지는 컨텍스트에서 제외 (AI가 "판단 불가" 섹션 만드는 것 방지)
                    _fail_keywords = ("실패", "오류", "없음", "error", "fail", "N/A")
                    if raw and not any(k in raw for k in _fail_keywords):
                        news_lines.append(f"- {name}: {raw}")
                except Exception:
                    pass
                time.sleep(0.1)
            news_context = "\n".join(news_lines) if news_lines else ""
            
            flow_context = "\n\n".join(getattr(self, 'market_flow_history', []))
            parts = []
            if news_context:
                parts.append(f"[포트폴리오 주요 뉴스]\n{news_context}")
            if flow_context:
                parts.append(f"[실시간 AI 추적]\n{flow_context}")
            combined_context = "\n\n".join(parts) if parts else ""
            
            report_data = generate_daily_market_report(claude_client=self.claude, verbose=False, news_context=combined_context, kis=self.kis)
            if report_data:
                today_str = _now_kst().strftime('%Y-%m-%d')
                if not isinstance(self.daily_report, dict) or self.daily_report.get('date') != today_str: self.daily_report = {'date': today_str, '15:40': None}
                content = report_data.get('report_markdown') if isinstance(report_data, dict) else str(report_data)
                self.daily_report[time_slot] = content
                # 거래량 급증 종목 실제 리스트 저장 (채팅 AI가 종목명 조회 가능하도록)
                _surge = report_data.get('volume_surge_details', [])
                if _surge:
                    self.volume_surge_details = _surge
                self._save_state()
                self._send_telegram(f"📝 [리포트 발간]\n\n{content[:4000]}")
        except Exception as e:
            logger.error(f"[{self.mode_name}] 일일 리포트 생성 오류: {e}", exc_info=True)

    # ── 🧠 자가학습 메서드 그룹 ──────────────────────────────────────

    def _log_trade(self, ticker: str, name: str, action: str, price: float,
                   strategy: str, reason: str, profit: float = 0):
        """log_trade_journal 래퍼 — 매매 기록 후 자가학습 트리거 체크.
        모든 self._log_trade(...) 호출 대신 이 메서드를 사용."""
        log_trade_journal(self.user_id, ticker, name, action, price, strategy, reason, profit)

        # ① 누적 거래 카운터: 10건마다 누적 반성
        self._trades_since_reflection += 1
        if self._trades_since_reflection >= 10:
            self._trades_since_reflection = 0
            self.add_log("📚 [누적 10건 달성] 학습 반성 트리거")
            self._run_threaded(self._incremental_reflection)
            return  # 긴급 반성과 중복 방지

        # ② 큰 손실 감지: SELL이고 손실이 임계값 초과하면 긴급 반성
        if action == 'SELL' and profit < self._EMERGENCY_LOSS_THRESHOLD:
            cooldown_remaining = self._EMERGENCY_COOLDOWN - (time.time() - self._last_emergency_reflection_ts)
            if cooldown_remaining <= 0:
                self.add_log(f"🚨 [큰 손실 감지] {name} {profit:,.0f}원 — 긴급 반성 시작")
                self._last_emergency_reflection_ts = time.time()
                self._run_threaded(lambda: self._emergency_reflection(ticker, name, profit, reason))

    def _weekly_self_reflection(self):
        """주간 반성 — 기존 규칙 보존하면서 학습 결과 병합 (덮어쓰기 금지)."""
        from base.database import get_db_connection
        conn = None
        try:
            conn = get_db_connection()
            rows = conn.execute('SELECT date(created_at) as date, stock_name, action, price, ai_reason, profit FROM trade_journal WHERE user_id = ? ORDER BY created_at DESC LIMIT 30', (self.user_id,)).fetchall()
        except Exception as e:
            logger.warning(f"[{self.mode_name}] 주간 반성 데이터 조회 실패: {e}")
            rows = []
        finally:
            if conn: conn.close()
        if not rows: return

        history_text = "\n".join([f"- {r['date']} | {r['stock_name']} | {r['action']} | {r['ai_reason']} | 손익:{r['profit']}" for r in rows])
        existing_rules = load_ai_rules(self.user_id)   # ← 기존 규칙 로드
        if self.claude:
            new_rules = self.claude.generate_weekly_reflection(history_text, existing_rules)
            if new_rules:
                save_ai_rules(self.user_id, new_rules, trigger_type='weekly')
                self._send_telegram(f"🧠 [주간 학습 완료]\n\n{new_rules[:2000]}")

    def _incremental_reflection(self):
        """누적 10건 반성 — 주간 반성과 동일 로직, 트리거만 다름."""
        from base.database import get_db_connection
        conn = None
        try:
            conn = get_db_connection()
            rows = conn.execute('SELECT date(created_at) as date, stock_name, action, price, ai_reason, profit FROM trade_journal WHERE user_id = ? ORDER BY created_at DESC LIMIT 10', (self.user_id,)).fetchall()
        except Exception as e:
            logger.warning(f"[{self.mode_name}] 누적 반성 데이터 조회 실패: {e}")
            rows = []
        finally:
            if conn: conn.close()
        if not rows: return

        history_text = "\n".join([f"- {r['date']} | {r['stock_name']} | {r['action']} | {r['ai_reason']} | 손익:{r['profit']}" for r in rows])
        existing_rules = load_ai_rules(self.user_id)
        if self.claude:
            new_rules = self.claude.generate_weekly_reflection(history_text, existing_rules)
            if new_rules:
                save_ai_rules(self.user_id, new_rules, trigger_type='incremental')
                self._send_telegram(f"📚 [누적 10건 학습 완료]\n\n{new_rules[:2000]}")

    def _emergency_reflection(self, ticker: str, stock_name: str,
                               profit: float, ai_reason: str):
        """큰 손실 직후 긴급 반성 — 관련 규칙 항목만 수정/강화, 나머지 보존."""
        existing_rules = load_ai_rules(self.user_id)
        if not self.claude:
            return
        new_rules = self.claude.generate_emergency_reflection(
            ticker, stock_name, profit, ai_reason, existing_rules
        )
        if new_rules:
            save_ai_rules(self.user_id, new_rules, trigger_type='emergency')
            self._send_telegram(
                f"🚨 <b>긴급 학습 완료</b>  ·  {self.alert_icon} {self.mode_name}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>{stock_name}</b>  <code>{ticker}</code>\n"
                f"💸 손실: <b>{profit:,.0f}원</b>\n"
                f"🧠 규칙 업데이트 완료 (기존 규칙 보존)\n"
                f"⏰ {_now_kst().strftime('%H:%M KST')}"
            )

    def _run_threaded(self, job_func): threading.Thread(target=job_func, daemon=True).start()

    def _run_loop(self, total_cash):
        self.scheduler = schedule.Scheduler()

        # W-06: __init__에서 이미 _restore_state()를 호출했으므로 중복 호출 방지
        # 복원된 상태가 없거나 포지션이 비어 있으면 새로 초기화
        try:
            already_restored = getattr(self, '_init_state_restored', False)
            if not already_restored or not self.core_positions:
                if not self._restore_state():
                    self.initialize_portfolio(total_cash)
        except Exception as e:
            logger.error(f"[{self.mode_name}] 포트폴리오 초기화 실패 (기본 코어로 계속 진행): {e}", exc_info=True)

        # schedule 라이브러리는 항상 KST 기준으로 설정 (_now_kst() 기반 체크 사용)
        # 리포트는 trading_job 안에서 _now_kst() 시간을 직접 체크해 발행 → 시스템 타임존 무관
        self.scheduler.every(1).minutes.do(self.trading_job)
        self.scheduler.every(30).minutes.do(lambda: self._run_threaded(self.analyze_continuous_market_flow))

        def _hourly_rescreen_if_empty():
            """1시간마다 — 빈 위성 슬롯 있을 때만 재스크리닝 (슬롯 다 채워지면 스킵)."""
            with self.lock:
                has_empty = any(p.shares == 0 for p in self.satellite_positions.values())
            if has_empty:
                self._run_threaded(self._rescreen_satellites)

        self.scheduler.every(1).hours.do(_hourly_rescreen_if_empty)

        # ⚠️ [BUG-FIX] schedule.at()은 시스템 로컬 시간 기준으로 발동 (UTC EC2 서버 대응).
        # datetime.now()가 아닌 _now_kst()로 KST 시각을 직접 확인하는 래퍼를 사용.
        # UTC 서버에서 ".at('00:05')"는 09:05 KST에 발동해 버려 의도와 9시간 오차가 생기므로,
        # 매분 실행되는 1분 스케줄러 안에서 KST 목표 시각과 일치할 때만 실행하는 방식으로 대체.
        def _kst_midnight_rescreen():
            """KST 00:05 자정에만 위성 재스크리닝 (UTC 서버 대응)."""
            kst_hm = _now_kst().strftime('%H:%M')
            if kst_hm == "00:05":
                self._run_threaded(self._rescreen_satellites)

        def _kst_friday_reflection():
            """KST 금요일 16:00에만 주간 반성 (UTC 서버 대응)."""
            now_kst = _now_kst()
            if now_kst.weekday() == 4 and now_kst.strftime('%H:%M') == "16:00":
                self._run_threaded(self._weekly_self_reflection)

        def _kst_morning_websocket():
            """KST 08:00에만 웹소켓 재연결 (UTC 서버 대응)."""
            if _now_kst().strftime('%H:%M') == "08:00":
                self._run_threaded(self.refresh_websocket)

        def _kst_morning_prescreen():
            """KST 08:50 — 9:05 첫 매매 전 위성 사전 스크리닝.
            스크리닝 소요 ~2분 → 9:05 이전 완료 보장.
            """
            if _now_kst().strftime('%H:%M') == "08:50":
                self.add_log("🔍 [08:50 사전 스크리닝] 9:05 첫 매매 대비 위성 종목 선정 시작")
                self._run_threaded(self._rescreen_satellites)

        def _kst_friday_lstm():
            """KST 금요일 02:00에만 LSTM 훈련 (UTC 서버 대응)."""
            now_kst = _now_kst()
            if now_kst.weekday() == 4 and now_kst.strftime('%H:%M') == "02:00":
                self._run_threaded(self.run_lstm_training)

        def _kst_weekend_scan():
            """토·일 10:00 — 주말 위성 사전 분석 (월요일 교체 계획 수립)."""
            now_kst = _now_kst()
            if now_kst.weekday() >= 5 and now_kst.strftime('%H:%M') == "14:00":
                self._run_threaded(self._weekend_satellite_scan)

        def _kst_monday_execute():
            """월요일 09:00 — 주말 계획 즉시 실행."""
            now_kst = _now_kst()
            if now_kst.weekday() == 0 and now_kst.strftime('%H:%M') == "09:00":
                if self._monday_swap_plan:
                    self._run_threaded(self._execute_monday_swap)

        self.scheduler.every(1).minutes.do(_kst_midnight_rescreen)
        self.scheduler.every(1).minutes.do(_kst_friday_reflection)
        self.scheduler.every(1).minutes.do(_kst_morning_websocket)
        self.scheduler.every(1).minutes.do(_kst_friday_lstm)
        self.scheduler.every(1).minutes.do(_kst_morning_prescreen)
        self.scheduler.every(1).minutes.do(_kst_weekend_scan)
        self.scheduler.every(1).minutes.do(_kst_monday_execute)

        try:
            self.trading_job()
        except Exception as e:
            logger.error(f"[{self.mode_name}] 초기 trading_job 오류: {e}", exc_info=True)

        while self.is_running:
            try:
                self.scheduler.run_pending()
            except Exception as e:
                logger.error(f"[{self.mode_name}] 스케줄러 오류: {e}", exc_info=True)
            # ── watchdog: _trading_job_running 고착 감지 ─────────────────
            # AI API 무응답 등으로 trading_job이 180초 이상 실행 중이면 강제 리셋
            if getattr(self, '_trading_job_running', False):
                _job_start = getattr(self, '_trading_job_start_ts', 0)
                if _job_start > 0 and (time.time() - _job_start) > 180:
                    logger.error(f"[{self.mode_name}] trading_job 180초 초과 — 강제 리셋 (watchdog)")
                    self.add_log("⚠️ [watchdog] trading_job 3분 초과 강제 리셋")
                    self._trading_job_running = False
                    self._trading_job_start_ts = 0
            time.sleep(1)
    
    def refresh_websocket(self):
        try:
            if self.kis:
                if self.ws_client and self.ws_client.ws:
                    try: self.ws_client.ws.close()
                    except Exception: pass
                app_key = self.kis.get_approval_key()
                if app_key:
                    old_subscribed = list(self.ws_client.subscribed_tickers) if self.ws_client else []
                    # W-07: live_prices 쓰기도 lock으로 보호
                    def _on_price(t, p):
                        with self.lock:
                            self.live_prices[t] = p
                    self.ws_client = self._create_websocket(app_key, _on_price)
                    if self.ws_client:
                        self.ws_client.start()
                        time.sleep(3.0)
                        for t in old_subscribed: self.ws_client.subscribe(t)
        except Exception as e:
            logger.error(f"[{self.mode_name}] WebSocket 재연결 오류: {e}", exc_info=True)

    def run_lstm_training(self):
        try:
            import os, sys, subprocess
            script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "train_lstm.py")
            subprocess.run([sys.executable, script_path], capture_output=True, text=True)
        except Exception: pass

    def start(self, total_cash=10_000_000):
        if not self.kis: return False
        if not self.is_running:
            self.is_running = True
            self.initial_capital_captured = False
            self.thread = threading.Thread(target=self._run_loop, args=(total_cash,), daemon=True)
            self.thread.start()
            update_bot_status(self.user_id, True, is_mock=self._is_mock)
            self.add_log(f"▶️ [{self.mode_name}] 매매 봇이 시작되었습니다.")
            return True
        return False

    def stop(self):
        if self.is_running:
            self.is_running = False
            self._save_state()  # 정지 시 상태 저장 — 재시작 후 복구 보장
            update_bot_status(self.user_id, False, is_mock=self._is_mock)
            if self.thread: self.thread.join(timeout=3)

    def get_pnl_data(self):
        """일/주/월/년 4종 손익 집계를 반환합니다."""
        from collections import defaultdict
        sorted_days = sorted(self.daily_pnl.keys())

        # 일별 (최근 30일)
        daily_labels = sorted_days[-30:]
        daily_values = [round(self.daily_pnl[d]) for d in daily_labels]

        # 주별 집계 (YYYY-Www)
        weekly: dict = defaultdict(float)
        for d in sorted_days:
            try:
                dt = datetime.strptime(d, '%Y-%m-%d')
                week_key = dt.strftime('%Y-W%W')
                weekly[week_key] += self.daily_pnl[d]
            except Exception:
                pass
        weekly_labels = sorted(weekly.keys())[-26:]  # 최근 26주
        weekly_values = [round(weekly[w]) for w in weekly_labels]

        # 월별 집계 (YYYY-MM)
        monthly: dict = defaultdict(float)
        for d in sorted_days:
            monthly[d[:7]] += self.daily_pnl[d]
        monthly_labels = sorted(monthly.keys())[-24:]  # 최근 24개월
        monthly_values = [round(monthly[m]) for m in monthly_labels]

        # 연별 집계 (YYYY)
        yearly: dict = defaultdict(float)
        for d in sorted_days:
            yearly[d[:4]] += self.daily_pnl[d]
        yearly_labels = sorted(yearly.keys())
        yearly_values = [round(yearly[y]) for y in yearly_labels]

        return {
            "daily":   {"labels": daily_labels,   "values": daily_values},
            "weekly":  {"labels": weekly_labels,   "values": weekly_values},
            "monthly": {"labels": monthly_labels,  "values": monthly_values},
            "yearly":  {"labels": yearly_labels,   "values": yearly_values},
            # 하위 호환: 기존 labels/values 필드도 유지
            "labels":  daily_labels,
            "values":  daily_values,
        }

    def get_status(self):
        try:
            with self.lock:
                safe_core_positions = list(self.core_positions)
                safe_satellite_items = list(self.satellite_positions.items())

            total_realtime_stock_val = 0.0
            tracked_tickers = set()   # 봇이 알고 있는 종목 — 나중에 미추적 종목 합산 시 제외용
            cores_data = []
            for core in safe_core_positions:
                cp = float(getattr(core, '_last_price', 0) or self.live_prices.get(core.ticker, 0) or getattr(core, 'kis_current_price', 0) or core.avg_price or 0)
                core_val = float(core.shares) * cp
                total_realtime_stock_val += core_val
                tracked_tickers.add(core.ticker)
                cores_data.append({"name": core.name, "ticker": core.ticker, "shares": core.shares, "floor": core.floor_shares, "price": cp, "value": core_val, "avg_price": float(getattr(core, 'avg_price', 0) or 0), "budget": float(getattr(core, 'cash', 0) or 0), "strategy": "장기 우상향" if core.ticker != self.core_ticker else "RSI + floor 보호", "status": getattr(core, 'status', '감시 중 👀'), "status_msg": getattr(core, 'status_msg', '지표 점검 중...'), "dca_mode": bool(getattr(core, 'dca_mode', False))})

            satellites = []
            # num_satellites 한도만큼만 UI에 표시 (보유 중인 종목 우선)
            holding_items = [(t, p) for t, p in safe_satellite_items if p.shares > 0]
            empty_items   = [(t, p) for t, p in safe_satellite_items if p.shares == 0]
            capped_items  = (holding_items + empty_items)[:self.num_satellites]

            # [BUG-FIX] tracked_tickers & 평가금액은 ALL 위성 기준으로 계산 (UI 표시 캡과 분리)
            # capped_items 기준으로만 tracked_tickers를 채우면, 캡 밖의 종목이
            # cached_balance 루프에서 이중 합산되는 버그 발생.
            _sat_price_cache: dict = {}
            for ticker, pos in safe_satellite_items:
                tracked_tickers.add(ticker)   # 이중 계산 방지 (캡 무관하게 전체 등록)
                if pos.shares > 0:
                    sp = float(getattr(pos, '_last_price', 0) or self.live_prices.get(ticker, 0) or getattr(pos, 'kis_current_price', 0) or pos.avg_price or 0)
                    _sat_price_cache[ticker] = sp
                    total_realtime_stock_val += float(pos.shares) * sp

            # UI 표시는 capped_items으로만
            for ticker, pos in capped_items:
                sp = _sat_price_cache.get(ticker) or float(getattr(pos, '_last_price', 0) or self.live_prices.get(ticker, 0) or getattr(pos, 'kis_current_price', 0) or pos.avg_price or 0)
                sat_val = float(pos.shares) * sp
                satellites.append({"name": pos.name, "ticker": ticker, "shares": pos.shares, "price": sp, "value": sat_val, "avg_price": float(getattr(pos, 'avg_price', 0) or 0), "budget": float(getattr(pos, 'cash', 0) or 0), "status": getattr(pos, 'status', '감시 중 👀'), "status_msg": getattr(pos, 'status_msg', '지표 점검 중...')})

            try:
                current_initial_cash = get_user_initial_cash(self.user_id, self._is_mock)
            except Exception: current_initial_cash = 10000000.0

            # 모멘텀 기능 제거됨 — UI 호환을 위해 빈 리스트 유지
            momentum_list = []

            # [BUG-FIX] 봇이 추적하지 않는 종목(위성 교체로 빠진 보유주, 수동 매수 등)도 평가금액에 포함.
            # cached_balance에 실계좌 전체 잔고가 있으므로, 추적 중인 종목을 제외한 나머지를 합산.
            if self.cached_balance:
                for _s in self.cached_balance.get('stocks', []):
                    _t = _s.get('ticker', '')
                    _sh = int(_s.get('shares', 0))
                    if _t and _t not in tracked_tickers and _sh > 0:
                        _p = self.live_prices.get(_t) or float(_s.get('current_price', 0))
                        total_realtime_stock_val += _sh * _p

            # mock_total_asset: 코어+위성+미추적 종목 전체 반영 후 계산
            if self.cached_balance or self.internal_cash is not None:
                # internal_cash 우선 사용 — KIS 모의 API 1~3분 반영 지연 보정
                if self.internal_cash is not None:
                    api_cash = self.internal_cash
                else:
                    api_cash = float(self.cached_balance.get('total_cash', 0))
                mock_total_asset = api_cash + total_realtime_stock_val
                mock_pnl = mock_total_asset - current_initial_cash
                mock_pnl_rt = (mock_pnl / current_initial_cash * 100) if current_initial_cash > 0 else 0
            else:
                mock_total_asset = 0.0; mock_pnl = 0.0; mock_pnl_rt = 0.0

            available_cash = self.internal_cash if self.internal_cash is not None else 0.0

            # ── 방어자산 상태 (고정 3종목 항상 표시) ──
            is_bear = (self.market_regime == "BEAR")
            defensive_list = []
            bal_stocks = {s['ticker']: int(s.get('shares', 0)) for s in (self.cached_balance or {}).get('stocks', [])} if self.cached_balance else {}
            for asset in DEFENSIVE_ASSETS:
                d_ticker = asset['ticker']
                d_price  = self.live_prices.get(d_ticker, 0)
                # WebSocket 미연결 시 KIS API fallback
                if not d_price and self.kis:
                    try:
                        d_price = self.kis.get_current_price(d_ticker) or 0
                        if d_price:
                            with self.lock:
                                self.live_prices[d_ticker] = d_price
                    except Exception:
                        pass
                d_shares = bal_stocks.get(d_ticker, 0)
                # 전일 종가 대비 등락률 — ohlcv_cache에서 조회 (API 추가 호출 없음)
                d_change_pct = 0.0
                if d_price > 0:
                    try:
                        _df = self._get_cached_base_ohlcv(d_ticker)
                        if not _df.empty and 'close' in _df.columns:
                            _prev = float(_df['close'].iloc[-1])
                            if _prev > 0:
                                d_change_pct = (d_price - _prev) / _prev * 100
                    except Exception:
                        pass
                defensive_list.append({
                    "ticker":     d_ticker,
                    "name":       asset['name'],
                    "emoji":      asset['emoji'],
                    "ratio":      asset['ratio'],
                    "price":      d_price,
                    "shares":     d_shares,
                    "value":      d_shares * d_price,
                    "active":     is_bear,
                    "change_pct": round(d_change_pct, 2),
                })

            # BUG-FIX: deque는 슬라이싱 불가 → list()로 변환 후 슬라이스 (TypeError 방지)
            recent_logs = list(self.logs)[-30:]
            sat_info_snapshot = [{"ticker": c.get("ticker",""), "name": c.get("name",""), "return_pct": float(c.get("return_pct", c.get("momentum_20d", 0))), "sector": c.get("sector", "-")} for c in self.satellite_info[:5]]
            return {"is_running": self.is_running, "is_mock": self._is_mock, "has_keys": self.kis is not None, "logs": recent_logs, "hot_sectors": self.hot_sectors, "num_satellites": self.num_satellites, "cores": cores_data, "satellites": satellites, "satellite_info": sat_info_snapshot, "momentum_list": momentum_list, "defensive_list": defensive_list, "market_regime": self.market_regime, "mock_total_asset": mock_total_asset, "mock_pnl": mock_pnl, "mock_pnl_rt": mock_pnl_rt, "initial_cash": current_initial_cash, "available_cash": available_cash}
        except Exception as critical_e:
            return {"is_running": False, "is_mock": self._is_mock, "has_keys": False, "logs": [{"time": "Error", "message": f"오류: {str(critical_e)}"}], "hot_sectors": [], "num_satellites": self.num_satellites, "cores": [], "satellites": [], "momentum_list": [], "mock_total_asset": 0, "mock_pnl": 0, "mock_pnl_rt": 0, "initial_cash": 10000000}