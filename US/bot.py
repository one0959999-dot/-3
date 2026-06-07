"""
bots/us_bot.py — 미국장 실전 매매 봇 (KIS 해외주식 API)
──────────────────────────────────────────────────────────
KRBotController 아키텍처 기반으로 완전 재구축.
- KIS 해외주식 OpenAPI 실주문 (NASDAQ / NYSE)
- KIS 잔고 역추적으로 원금 자동 감지 (KR 봇 동일 패턴)
- 주문 후 즉시 KIS 잔고 재조회 → 포지션/현금 동기화
- yfinance: 위성 스크리닝 + 가격 보조 캐시
- 미국 동부 시간(ET) 장 운영 시간 체크 (09:30~16:00)
"""

import threading
import time
import logging
import collections
import json
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

import yfinance as yf

from base.telegram_bot import TelegramNotifier
from US.screener import (
    scan_us_satellites, scan_us_satellites_kis, scan_us_cores,
    get_us_prices_batch, generate_us_daily_report,
    get_futures_snapshot, get_sector_trends,
)
from US.kis.overseas_api import KisOverseasApi
from base.database import (
    update_bot_status,
    save_portfolio_state,
    load_portfolio_state,
    get_user_initial_cash,
    set_user_initial_cash,
    add_user_initial_cash,
    get_sector_guide,
    log_trade_journal,
)
from KR.strategy import (calculate_entry_score, get_entry_threshold, get_budget_ratio_from_score,
                      get_bull_momentum_score, calc_rsi, _calc_adx, _get_up_streak,
                      check_theme_overextension_exit, check_rsi_progressive_exit,
                      get_composite_signal, calculate_core_entry_score, get_core_entry_threshold)
# bot_manager는 순환 임포트 방지를 위해 런타임에 참조
import importlib

logger = logging.getLogger('lassi_bot')

# ── US 방어 자산 포트폴리오 (BEAR 국면 자동 편입) ────────────────────────
# KR 봇의 DEFENSIVE_ASSETS와 동일 구조 — 총 배분 40%
# PSQ 20% (나스닥 1x 인버스) + GLD 13% (금 ETF) + UUP 7% (달러 강세 ETF)
US_DEFENSIVE_ASSETS = [
    {
        "ticker": "PSQ",
        "name":   "ProShares Short QQQ",
        "ratio":  0.20,    # 총자산의 20% — 나스닥 1배 인버스
        "emoji":  "📉",
    },
    {
        "ticker": "GLD",
        "name":   "SPDR Gold Shares",
        "ratio":  0.13,    # 총자산의 13% — 금 안전자산
        "emoji":  "🥇",
    },
    {
        "ticker": "UUP",
        "name":   "Invesco DB USD Bull",
        "ratio":  0.07,    # 총자산의 7% — 달러 강세 헤지
        "emoji":  "💵",
    },
]

# ── 미국 동부 시간 (America/New_York — EST/EDT 자동 전환) ────────────
_ET = ZoneInfo("America/New_York")

def _now_et() -> datetime:
    return datetime.now(_ET)

def _is_us_market_open() -> bool:
    """미국 정규장 여부 (ET 09:30~16:00, 평일만)"""
    now = _now_et()
    if now.weekday() >= 5:
        return False
    t_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    t_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return t_open <= now < t_close

def _is_us_sell_hours() -> bool:
    """매도 허용 시간 — 정규장 + 시간외(프리마켓 04:00~09:30, 애프터마켓 16:00~20:00).
    매수는 정규장(_is_us_market_open)만, 매도(손절)는 이 함수로 판단."""
    now = _now_et()
    if now.weekday() >= 5:
        return False
    t_pre_open  = now.replace(hour=4,  minute=0,  second=0, microsecond=0)
    t_after_end = now.replace(hour=20, minute=0,  second=0, microsecond=0)
    return t_pre_open <= now < t_after_end

# ── USD/KRW 환율 캐시 (60초) ────────────────────────────────────────
_fx_cache: dict = {"rate": 1400.0, "ts": 0.0}
_fx_lock  = threading.Lock()

def _get_fx_rate() -> float:
    with _fx_lock:
        if time.time() - _fx_cache["ts"] < 60:
            return _fx_cache["rate"]
    try:
        hist = yf.Ticker("USDKRW=X").history(period="5d").dropna(subset=["Close"])
        if not hist.empty:
            rate = float(hist["Close"].iloc[-1])
            with _fx_lock:
                _fx_cache["rate"] = rate
                _fx_cache["ts"]   = time.time()
            return rate
    except Exception:
        pass
    return _fx_cache["rate"]

# ── 매도 수수료 (KIS 해외주식 기준 약 0.25%) ────────────────────────
_US_FEE = 0.0025

def _net_profit_usd(sell_p: float, avg_p: float, shares: float) -> float:
    return (sell_p * (1 - _US_FEE) - avg_p) * shares


# ── 포지션 ──────────────────────────────────────────────────────────
@dataclass
class USPosition:
    ticker:         str
    name:           str
    shares:         float = 0.0
    floor_shares:   float = 0.0   # 최소 보유 주식 수 — 이 이하로 절대 안 팜 (주식 수 축적)
    avg_price_usd:  float = 0.0
    budget_usd:     float = 0.0
    partial_sold:   bool  = False
    partial_sold_2: bool  = False
    status:         str   = "감시 중 👀"
    last_order_time:float = 0.0
    max_price_usd:  float = 0.0
    ai_exit_pending:     bool  = False
    ai_exit_decision:    str   = None   # 'SELL_PARTIAL' / 'SELL_ALL' / 'HOLD' / None
    ai_exit_asked_price: float = 0.0    # 마지막 AI 문의 시점 가격 (새 고점 갱신 시 재요청)
    second_buy_price:        float = 0.0    # 2차 매수 발동가 (1차 진입가 × 0.98)
    second_buy_cash:         float = 0.0    # 2차 매수 유보 예산 (USD)
    second_buy_done:         bool  = False  # 2차 매수 완료 여부
    third_buy_price:         float = 0.0    # 3차 매수 발동가 (1차 진입가 × 0.96)
    third_buy_cash:          float = 0.0    # 3차 매수 유보 예산 (USD)
    third_buy_done:          bool  = False  # 3차 매수 완료 여부
    initial_shares_for_exit: float = 0.0   # 첫 매도 트리거 시 원금 주수 고정


# ══════════════════════════════════════════════════════════════════════
class USBotController:
    """미국장 실전 매매 봇 — KIS 해외주식 API (KRBotController 아키텍처 기반)"""

    # ── 전략 상수 ─────────────────────────────────────────────────────
    CORE_RATIO     = 0.50    # 코어 50% — 목표 풀매수(100%) / AI 거절 시 자연 현금 보유
    SAT_RATIO      = 0.50    # 위성 50% — 목표 풀매수(100%) / 진입 점수 미달 시 현금 유지
    ORDER_COOLDOWN = 300     # 연속 주문 방지 (초)
    # ATR 기반 손절 (KR 동일 방식)
    CORE_HARD_MULT  = {"BULL": 3.0, "NEUTRAL": 2.5, "BEAR": 1.8}
    SAT_TRAIL_MULT  = {"BULL": 1.5, "NEUTRAL": 1.5, "BEAR": 1.2}
    SAT_TRAIL_TRIG  = {"BULL": 1.2, "NEUTRAL": 1.0, "BEAR": 0.8}
    SAT_HARD_MULT   = {"BULL": 3.0, "NEUTRAL": 2.5, "BEAR": 1.8}
    PARTIAL1_PCT    = 10.0   # 1차 익절 기준 (%) — KR 동일
    PARTIAL1_QTY    = 0.50   # 1차 익절 비율 — KR 동일 (50%)
    PARTIAL2_PCT    = 20.0   # 2차 익절 기준 (%)
    PARTIAL2_QTY    = 1.00   # 2차: 나머지 전량

    def __init__(self, user_id, kis_config=None, telegram_config=None, core_stocks=None, satellite_stocks=None):
        self.user_id    = user_id
        self.is_running = False
        self.thread     = None
        self.logs: collections.deque = collections.deque(maxlen=100)
        self.num_satellites = 3   # 최대 3개

        # DB 슬롯: US 봇은 is_mock=True 슬롯 재사용
        self._is_mock  = True
        self.mode_name = "US실전"
        self.alert_icon = "🇺🇸"

        # ── KIS 해외주식 API ──────────────────────────────────────────
        self.kis_overseas: KisOverseasApi | None = None
        self._init_api(kis_config)

        # ── 사용자 지정 종목 (AI 선정 대신 고정) ────────────────────────
        try:
            self.user_core_stocks = json.loads(core_stocks) if core_stocks else []
        except Exception:
            self.user_core_stocks = []
        try:
            self.user_satellite_stocks = json.loads(satellite_stocks) if satellite_stocks else []
        except Exception:
            self.user_satellite_stocks = []

        # ── 포트폴리오 ────────────────────────────────────────────────
        self.core_positions:      dict[str, USPosition] = {}  # 코어 40%
        self.core_info:           list = []     # AI 선정 코어 종목 메타
        self.num_cores            = 2   # 상한 — AI가 좋은 종목만 채움
        self.last_core_screen_date = None       # 코어 스크리닝 날짜 (주 1회)
        self.satellite_positions: dict[str, USPosition] = {}
        self.satellite_info:      list = []
        self.hot_sectors:         list = []
        self.daily_pnl:           dict = {}
        self.daily_report              = None
        # ── 주말 사전 분석 플랜 ────────────────────────────────────────
        self._monday_swap_plan: dict = {}   # {ticker_to_sell: {new_ticker, new_name, reason}}
        self._weekend_scan_done: str = ""   # 마지막 주말 스캔 날짜 (중복 방지)
        self._ai_market_entry_bonus = 0     # AI 시장판단 진입 보너스 (-2~+2)

        # ── 현금 / 원금 추적 ──────────────────────────────────────────
        self.cash_usd        = 0.0
        self._last_trade_ts  = 0.0   # 마지막 체결 타임스탬프 (잔고 재동기화 시점 판단)

        # ── KR 봇 동일 패턴: 원금 자동 감지 ─────────────────────────
        self.initial_capital_captured = False
        self.last_asset_cost          = None   # USD 기준
        self.pnl_this_turn            = 0.0

        # ── 스크리닝 ──────────────────────────────────────────────────
        self.last_screen_date  = None
        self.market_regime     = "NEUTRAL"
        self.futures_snapshot: dict = {}     # 야간선물 스냅샷 (NQ=F / ES=F / EWY)
        self.sector_trends:    list = []     # NASDAQ 섹터 추세 리스트

        # ── 방어 자산 ─────────────────────────────────────────────────
        self._last_defensive_check = 0.0          # 5분 캐시
        self._defensive_sold_ts:   dict = {}      # 종목별 청산 타임스탬프 (24h 쿨다운)
        self._defensive_shares:    dict = {}      # 종목별 보유 수량 캐시

        # ── AI 스윙 재진입 큐 ─────────────────────────────────────────
        # {ticker: {'sell_price': float, 'target_rsi': 35, 'target_price': float, 'ts': float}}
        self._swing_rebuy_queue:    dict = {}
        self._swing_accumulate_cnt: dict = {}  # {ticker: int} 누적 횟수 (최대 2회)

        # ── 거절 쿨다운 (영구 블랙리스트 대신 15분/20분 쿨다운) ─────────
        self._bl_date              = ""
        self._satellite_rejects:   dict = {}   # {ticker: float(ts)}
        self._satellite_reject_rsn:dict = {}   # {ticker: str}
        self._core_reject_ts:      dict = {}   # {ticker: float(ts)} — 현재 미사용
        self._SAT_REJECT_COOLDOWN  = 300       # 위성 5분 (KR봇 동일)

        # ── AI 채팅으로 동적 조정 가능한 파라미터 ────────────────────
        self.entry_thresholds: dict = {}    # {'BULL': 4, 'NEUTRAL': 5, ...}

        # ── 가격 캐시 ─────────────────────────────────────────────────
        self._price_cache:   dict  = {}
        self._last_price_ts: float = 0.0

        # ── 텔레그램 ─────────────────────────────────────────────────
        self.telegram = None
        if telegram_config and telegram_config.get("token"):
            try:
                self.telegram = TelegramNotifier(
                    token   = telegram_config["token"].strip(),
                    chat_id = (telegram_config.get("chat_id") or "").strip(),
                )
            except Exception:
                pass

        # ── BaseBot 호환 필드 (app.py 분기용) ────────────────────────
        self.kis             = None    # KR KIS 없음
        self.real_kis        = None
        self.cached_balance  = None
        self.live_prices     = {}
        self.claude          = None
        self.sector_guide    = get_sector_guide(user_id) or ""
        self.fundamental_cache: dict = {}

        self.lock = threading.RLock()

        # 상태 복원
        self._restore_state()

        # 백그라운드 가격 갱신 (30초 주기)
        self._sync_thread = threading.Thread(
            target=self._perpetual_price_sync, daemon=True
        )
        self._sync_thread.start()

        # 백그라운드 잔고 동기화 (60초 주기 — 봇 정지 상태에서도 실행)
        threading.Thread(target=self._perpetual_balance_sync, daemon=True).start()

        has_api = "✅ KIS API 연결됨" if self.kis_overseas else "⚠️ KIS API 미설정 (설정 필요)"
        self.add_log(f"🇺🇸 US 실전 매매 봇 초기화 완료 — {has_api}")

    # ─────────────────────────────────────────────────────────────────
    # API 초기화 (KRBotController._init_api 패턴)
    # ─────────────────────────────────────────────────────────────────

    def _init_api(self, kis_config):
        """KIS 해외주식 API 초기화"""
        if kis_config and kis_config.get("app_key") and kis_config.get("app_secret"):
            try:
                self.kis_overseas = KisOverseasApi(
                    app_key    = kis_config["app_key"].strip(),
                    app_secret = kis_config["app_secret"].strip(),
                    account_no = (kis_config.get("account_no") or "").strip(),
                )
            except Exception as e:
                logger.warning(f"[US봇] KIS 해외주식 API 초기화 실패: {e}")
                self.kis_overseas = None
        else:
            self.kis_overseas = None

    # ─────────────────────────────────────────────────────────────────
    # 로그 / 텔레그램
    # ─────────────────────────────────────────────────────────────────

    def add_log(self, msg: str):
        t = _now_et().strftime("%H:%M:%S")
        self.logs.append({"time": t, "message": msg})
        logger.info(f"[US봇] {msg}")

    def _tg(self, msg: str):
        if self.telegram:
            try:
                self.telegram.send_message(msg)
            except Exception:
                pass

    def _run_threaded(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    # ─────────────────────────────────────────────────────────────────
    # 잔고 동기화 — KRBotController._sync_internal_balances 패턴 이식
    # ─────────────────────────────────────────────────────────────────

    def _sync_balance_from_kis(self):
        """
        KIS 잔고 재조회 → 현금·포지션·원금 동기화.
        KR 봇의 _sync_internal_balances 패턴을 USD 기준으로 이식.
        """
        if not self.kis_overseas:
            return
        try:
            bal      = self.kis_overseas.get_balance()
            cash_usd = bal["cash_usd"]
            stocks   = bal["stocks"]   # [{ticker, name, shares, avg_price, current_price, value}]

            total_usd = cash_usd + sum(float(s.get("value", 0)) for s in stocks)

            with self.lock:
                # ── 현금 재동기화 ──────────────────────────────────────
                # 첫 조회 or 마지막 체결 2분 경과 시 KIS 값으로 재동기화
                if self.cash_usd == 0.0 or (time.time() - self._last_trade_ts >= 120):
                    # T+2 보정: 매수가능금액조회(TTTS3007R) → ovrs_ord_psbl_amt
                    # 당일 매도 직후에도 재사용 가능 금액이 포함됨
                    buyable_usd = cash_usd
                    try:
                        _bc = self.kis_overseas.get_buyable_cash_usd()
                        if _bc > 0:
                            buyable_usd = _bc
                    except Exception:
                        pass
                    self.cash_usd = buyable_usd

                # ── 원금 자동 감지 (KR 봇 동일 패턴) ─────────────────
                if not self.initial_capital_captured and total_usd > 0:
                    fx        = _get_fx_rate()
                    total_krw = round(total_usd * fx)
                    db_cash   = get_user_initial_cash(self.user_id, self._is_mock)
                    if db_cash == 10_000_000:
                        set_user_initial_cash(self.user_id, total_krw, self._is_mock)
                        self.add_log(
                            f"💰 [US 원금 셋업] ${total_usd:,.2f} (≈₩{total_krw:,.0f}) 확정 (첫 실행 감지)"
                        )
                    self.initial_capital_captured = True

                # ── 입출금 감지 ────────────────────────────────────────
                if self.last_asset_cost is not None:
                    expected  = self.last_asset_cost + self.pnl_this_turn
                    self.pnl_this_turn = 0.0
                    delta_usd = total_usd - expected
                    fx        = _get_fx_rate()
                    if abs(delta_usd * fx) > 10_000:   # 1만원 이상 변동
                        delta_krw = round(delta_usd * fx)
                        add_user_initial_cash(self.user_id, delta_krw, self._is_mock)
                        if delta_usd > 0:
                            self.add_log(f"💰 US 계좌 외부 입금 감지: +${delta_usd:,.2f}")
                        else:
                            self.add_log(f"💸 US 계좌 외부 출금 감지: ${delta_usd:,.2f}")
                self.last_asset_cost = total_usd

                # ── 포지션 KIS 동기화 (핵심 버그 수정) ──────────────
                # stocks=빈목록인데 total_usd > 0 → KIS API 오류, 포지션 초기화 건너뜀
                if not stocks and total_usd > 10:
                    logger.warning(
                        f"[US봇] KIS stocks 빈 응답 (total_usd=${total_usd:,.2f}) — 포지션 동기화 건너뜀"
                    )
                    return
                kis_map = {s["ticker"]: s for s in stocks}
                for ticker, pos in self.satellite_positions.items():
                    if ticker in kis_map:
                        kis_shares = float(kis_map[ticker].get("shares", 0))
                        kis_avg    = float(kis_map[ticker].get("avg_price", 0))
                        # 주수 불일치 → KIS 값으로 교정
                        if abs(pos.shares - kis_shares) > 0.5:
                            logger.info(
                                f"[US봇] 포지션 동기화 {ticker}: {pos.shares:.0f}→{kis_shares:.0f}주"
                            )
                            pos.shares = kis_shares
                        # 평균단가 불일치 → KIS 값으로 교정
                        if kis_avg > 0 and abs(pos.avg_price_usd - kis_avg) > 0.01:
                            pos.avg_price_usd = kis_avg
                    elif pos.shares > 0:
                        # 내부적으로는 보유 중인데 KIS에 없음 → 청산된 것
                        logger.warning(f"[US봇] {ticker} KIS 미보유 → 포지션 초기화")
                        pos.shares                  = 0.0
                        pos.floor_shares            = 0.0
                        pos.second_buy_price        = 0.0
                        pos.second_buy_cash         = 0.0
                        pos.second_buy_done         = False
                        pos.third_buy_price         = 0.0
                        pos.third_buy_cash          = 0.0
                        pos.third_buy_done          = False
                        pos.bull_pyramid_done       = False
                        pos.partial_sold            = False
                        pos.partial_sold_2          = False
                        pos.max_price_usd           = 0.0
                        pos.stop_news_checked       = False
                        pos.overext_sell_count      = 0
                        pos.initial_shares_for_exit = 0.0
                        pos.status                  = "청산됨 (KIS 동기화)"

                # ── KR봇 동일: 계좌에 있는데 대시보드에 없는 종목 강제 편입 ──
                tracked = set(self.satellite_positions.keys()) | set(self.core_positions.keys())
                for s in stocks:
                    t = s.get("ticker", "")
                    if not t or t in tracked:
                        continue
                    kis_shares = float(s.get("shares", 0))
                    if kis_shares <= 0:
                        continue
                    kis_avg  = float(s.get("avg_price", 0))
                    kis_name = s.get("name", t)
                    self.add_log(f"🌟 [US봇] 계좌 미등록 종목 '{kis_name}'({t}) 위성으로 강제 편입!")
                    new_pos = USPosition(
                        ticker        = t,
                        name          = kis_name,
                        shares        = kis_shares,
                        avg_price_usd = kis_avg,
                        status        = "계좌편입 ✅",
                    )
                    self.satellite_positions[t] = new_pos
                    if not any(x.get("ticker") == t for x in self.satellite_info):
                        self.satellite_info.append({
                            "ticker": t, "name": kis_name,
                            "sector": "계좌편입", "score": 0, "ai_reason": "계좌 보유 종목"
                        })
                    tracked.add(t)

            logger.debug(f"[US봇] 잔고 동기화 완료: 현금 ${cash_usd:,.2f} / 총 ${total_usd:,.2f}")

        except Exception as e:
            logger.debug(f"[US봇] KIS 잔고 동기화 실패: {e}")

    # ─────────────────────────────────────────────────────────────────
    # 가격 조회
    # ─────────────────────────────────────────────────────────────────

    def _refresh_prices(self, tickers=None):
        """보유 종목(+ 지정 종목) 가격 일괄 갱신. KIS 우선, yfinance 폴백."""
        if tickers is None:
            tickers = set()
        tickers = set(tickers)
        for t in self.core_positions:
            tickers.add(t)
        for info in self.core_info:
            tickers.add(info["ticker"])
        for t in self.satellite_positions:
            tickers.add(t)
        for info in self.satellite_info:
            tickers.add(info["ticker"])
        if not tickers:
            return {}

        new_prices: dict = {}

        # 1순위: KIS 해외주식 실시간
        if self.kis_overseas:
            try:
                new_prices = self.kis_overseas.get_prices_batch(list(tickers))
            except Exception as e:
                logger.debug(f"[US봇] KIS 가격 조회 실패, yfinance 폴백: {e}")

        # 2순위: yfinance 보조
        missing = tickers - set(new_prices.keys())
        if missing:
            yf_prices = get_us_prices_batch(missing)
            new_prices.update(yf_prices)

        with self.lock:
            self._price_cache.update(new_prices)
            self._last_price_ts = time.time()
        return new_prices

    def _perpetual_price_sync(self):
        """백그라운드 가격 갱신 루프 — 30초마다 (KR봇 동일)."""
        while True:
            try:
                self._refresh_prices()
            except Exception as e:
                logger.debug(f"[US봇] 가격 동기화 오류: {e}")
            time.sleep(30)

    def _perpetual_balance_sync(self):
        """백그라운드 잔고 동기화 루프 — 60초마다, 봇 정지 상태에서도 실행."""
        while True:
            try:
                if self.kis_overseas:
                    self._sync_balance_from_kis()
            except Exception as e:
                logger.debug(f"[US봇] 잔고 동기화 오류: {e}")
            time.sleep(60)

    def _price(self, ticker: str) -> float:
        return self._price_cache.get(ticker, 0.0)

    def _ai_swing_check(self, pos, ticker: str, price: float, reason: str) -> str:
        """ATR 손절/트레일링 발동 시 AI 전권 판단 — SELL_REBUY / ACCUMULATE / EXIT"""
        if not self.claude:
            return 'EXIT'
        avg = pos.avg_price_usd
        if avg <= 0:
            return 'EXIT'
        acc_cnt = self._swing_accumulate_cnt.get(ticker, 0)
        if acc_cnt >= 2:
            return 'EXIT'

        pnl_pct = (price / avg - 1) * 100
        roe_bonus, roe_reason = self._roe_turnaround_bonus(ticker)
        news         = self._fetch_us_news([ticker])
        fundamental  = self._fetch_fundamental(ticker)

        decision = self.claude.ai_swing_trade_check(
            ticker         = ticker,
            name           = pos.name,
            price_usd      = price,
            avg_usd        = avg,
            pnl_pct        = pnl_pct,
            regime         = self.market_regime,
            exit_reason    = reason,
            roe_reason     = roe_reason,
            news           = news,
            fundamental    = fundamental,
            hot_sectors    = self.hot_sectors or [],
            accumulate_count = acc_cnt,
        )
        return decision

    def _reinvest_to_cores(self, profit_usd: float, source: str = ""):
        """위성·단타 수익 전액을 코어 budget에 명시적 배분 (KR봇 REINVEST_RATIO=1.0 동일).
        실제 cash는 _sell()에서 이미 증가 — 여기서는 코어별 budget_usd를 즉시 늘려
        다음 매수 사이클에서 바로 활용 가능하게 함."""
        if profit_usd <= 0 or not self.core_positions:
            return
        n = max(1, len(self.core_positions))
        per_core = profit_usd / n
        with self.lock:
            for pos in self.core_positions.values():
                pos.budget_usd = getattr(pos, 'budget_usd', 0.0) + per_core
        self.add_log(f"♻️ 수익 재투자: ${profit_usd:,.0f} → 코어 {n}개 배분 (개당 ${per_core:,.0f}) [{source}]")

    # ─────────────────────────────────────────────────────────────────
    # OHLCV 캐시 — 인터벌별 TTL 분리
    #   일봉(1d): 1시간 캐시 — 종가는 장 마감 후 확정, 장중 변화 없음
    #   5분봉(5m): 5분 캐시  — 장중 실시간 흐름 반영
    # ─────────────────────────────────────────────────────────────────
    _ohlcv_cache_1d: dict = {}   # {ticker: (ts, df)}
    _ohlcv_cache_5m: dict = {}   # {ticker: (ts, df)}
    _OHLCV_TTL_1D = 3600         # 일봉 1시간 캐시
    _OHLCV_TTL_5M = 300          # 5분봉 5분 캐시

    def _get_cached_ohlcv(self, ticker: str, period: str = "60d") -> "pd.DataFrame":
        """일봉(1d) OHLCV — 추세/MA/RSI/ATR/MACD 계산용. 1시간 캐시."""
        import yfinance as yf
        cached = self._ohlcv_cache_1d.get(ticker)
        if cached and time.time() - cached[0] < self._OHLCV_TTL_1D:
            return cached[1]
        try:
            df = yf.download(ticker, period=period, interval="1d",
                             progress=False, auto_adjust=True)
            if hasattr(df.columns, "get_level_values"):
                df.columns = df.columns.get_level_values(0)
            df = df.dropna(subset=["Close"])
            df.columns = [c.lower() for c in df.columns]
            self._ohlcv_cache_1d[ticker] = (time.time(), df)
            return df
        except Exception:
            return pd.DataFrame()

    def _get_cached_ohlcv_5m(self, ticker: str, period: str = "5d") -> "pd.DataFrame":
        """5분봉 OHLCV — 장중 진입 타이밍/모멘텀/거래량 서지 확인용. 5분 캐시."""
        import yfinance as yf
        cached = self._ohlcv_cache_5m.get(ticker)
        if cached and time.time() - cached[0] < self._OHLCV_TTL_5M:
            return cached[1]
        try:
            df = yf.download(ticker, period=period, interval="5m",
                             progress=False, auto_adjust=True)
            if hasattr(df.columns, "get_level_values"):
                df.columns = df.columns.get_level_values(0)
            df = df.dropna(subset=["Close"])
            df.columns = [c.lower() for c in df.columns]
            self._ohlcv_cache_5m[ticker] = (time.time(), df)
            return df
        except Exception:
            return pd.DataFrame()

    # ─────────────────────────────────────────────────────────────────
    # ROE 턴어라운드 보너스 (분기별 ROE 개선 추세 감지)
    # ─────────────────────────────────────────────────────────────────
    _roe_cache: dict = {}  # {ticker: (ts, score, reason)}

    def _roe_turnaround_bonus(self, ticker: str) -> tuple:
        """분기별 ROE 음→양 전환 추세 → 진입 점수 보너스. 1시간 캐시."""
        cached = self._roe_cache.get(ticker)
        if cached and time.time() - cached[0] < 3600:
            return cached[1], cached[2]
        score, reason = 0.0, ""
        try:
            import yfinance as yf
            stk = yf.Ticker(ticker)
            fi  = stk.quarterly_financials
            bs  = stk.quarterly_balance_sheet
            if fi is None or fi.empty or bs is None or bs.empty:
                return 0.0, ""
            ni_key = next((k for k in fi.index if 'Net Income' in str(k)), None)
            eq_key = next((k for k in bs.index
                           if 'Stockholders Equity' in str(k) or 'Total Equity' in str(k)), None)
            if not ni_key or not eq_key:
                return 0.0, ""
            ni  = fi.loc[ni_key].dropna().sort_index()
            eq  = bs.loc[eq_key].dropna().sort_index().abs()
            common = ni.index.intersection(eq.index)
            if len(common) < 3:
                return 0.0, ""
            roe = (ni.loc[common] / (eq.loc[common] + 1)).sort_index()
            vals = list(roe.values[-4:]) if len(roe) >= 4 else list(roe.values)
            # 최신 분기 ROE 가 음수여야 턴어라운드 후보
            if vals[-1] >= 0:
                return 0.0, ""
            n = len(vals)
            improving = sum(1 for i in range(1, n) if vals[i] > vals[i-1])
            if improving == n - 1:          # 모든 분기 지속 개선
                if vals[-1] > -0.02:
                    score, reason = 10.0, f"ROE 흑자전환 임박({vals[-1]*100:.1f}%→0%) +10"
                elif vals[-1] > -0.08:
                    score, reason = 6.0,  f"ROE 빠른개선({vals[0]*100:.1f}%→{vals[-1]*100:.1f}%) +6"
                else:
                    score, reason = 3.0,  f"ROE 턴어라운드 추세 +3"
            elif improving >= max(1, n // 2):
                score, reason = 2.0, f"ROE 부분개선 +2"
        except Exception:
            pass
        self._roe_cache[ticker] = (time.time(), score, reason)
        return score, reason

    # ─────────────────────────────────────────────────────────────────
    # 주문 — KIS 실주문 후 즉시 잔고 재동기화
    # ─────────────────────────────────────────────────────────────────

    def _buy(self, ticker: str, name: str, budget_usd: float, price: float = 0,
             strategy: str = "", ai_reason: str = "") -> int:
        """실전 매수. KIS 시장가 주문 → 즉시 잔고 재동기화. 체결 주수 반환 (0=실패)"""
        if not self.kis_overseas:
            self.add_log(f"⚠️ BUY 실패: KIS API 미설정 ({ticker})")
            return 0
        if self.cash_usd is None:
            self.add_log(f"⏳ [{name}] 매수 보류 — KIS 잔고 초기화 대기 중")
            return 0
        price = price or self._price(ticker)
        if price <= 0 or budget_usd <= 0:
            return 0
        with self.lock:
            avail = min(budget_usd, self.cash_usd)
            # 정규장(09:30~16:00 ET): 소수점 주 허용 / 시간외: 정수 주만
            if _is_us_market_open():
                qty_f = round(avail / price, 3)   # 소수점 3자리
                qty   = int(qty_f) if abs(qty_f - round(qty_f)) < 1e-6 else qty_f
            else:
                qty_f = None
                qty   = int(avail / price)         # 정수 주만
            if (qty_f or qty) <= 0:
                return 0

        # 소수점 주문 (정규장 + 0.001 이상 분수)
        _use_fractional = (qty_f is not None and abs(qty_f - round(qty_f)) >= 1e-6 and qty_f >= 0.001
                           and hasattr(self.kis_overseas, 'buy_fractional_order'))
        if _use_fractional:
            ok   = self.kis_overseas.buy_fractional_order(ticker, qty_f)
            qty  = qty_f   # 반환값도 소수점
        else:
            qty  = int(qty_f) if qty_f is not None else qty
            ok   = self.kis_overseas.buy_market_order(ticker, qty)

        if ok:
            cost = qty * price
            with self.lock:
                self.cash_usd        = max(0.0, self.cash_usd - cost)
                self._last_trade_ts  = time.time()
                self.pnl_this_turn  -= cost
            _qty_str = f"{qty:.3f}" if isinstance(qty, float) and qty != int(qty) else str(int(qty))
            self.add_log(f"📥 BUY  {name}({ticker}) {_qty_str}주 @ ${price:.2f} 추정 (${cost:,.0f})")
            try:
                log_trade_journal(self.user_id, ticker, name, 'BUY', price,
                                  strategy=strategy, ai_reason=ai_reason[:120], shares=qty, mode='US')
            except Exception:
                pass
            # 주문 후 5초 대기 후 즉시 잔고 재조회
            self._run_threaded(lambda: (time.sleep(5), self._sync_balance_from_kis()))
            return qty
        else:
            self.add_log(f"❌ BUY 주문 실패: {name}({ticker}) — KIS 응답 확인 필요")
            return 0

    def _sell(self, ticker: str, name: str, shares: float, price: float = 0,
              strategy: str = "", ai_reason: str = "", profit: float = 0) -> float:
        """실전 매도. KIS 시장가 주문 → 즉시 잔고 재동기화. 체결 대금(USD) 추정값 반환"""
        if not self.kis_overseas:
            self.add_log(f"⚠️ SELL 실패: KIS API 미설정 ({ticker})")
            return 0.0
        price = price or self._price(ticker)
        qty_frac = round(shares, 3)
        qty_int  = int(qty_frac)
        _use_frac_sell = (abs(qty_frac - qty_int) >= 1e-6
                          and hasattr(self.kis_overseas, 'sell_fractional_order'))
        qty = qty_frac if _use_frac_sell else qty_int
        if price <= 0 or qty <= 0:
            return 0.0

        ok = (self.kis_overseas.sell_fractional_order(ticker, qty) if _use_frac_sell
              else self.kis_overseas.sell_market_order(ticker, qty_int))
        if ok:
            proceeds = qty * price * (1 - _US_FEE)
            with self.lock:
                if self.cash_usd is None:
                    self.cash_usd = 0.0
                self.cash_usd       += proceeds
                self._last_trade_ts  = time.time()
                self.pnl_this_turn  += proceeds   # 원금 추적용
            _qty_str = f"{qty:.3f}" if isinstance(qty, float) and abs(qty - round(qty)) >= 1e-6 else str(int(qty))
            self.add_log(f"📤 SELL {name}({ticker}) {_qty_str}주 @ ${price:.2f} 추정 (${proceeds:,.0f})")
            try:
                log_trade_journal(self.user_id, ticker, name, 'SELL', price,
                                  strategy=strategy, ai_reason=ai_reason[:120],
                                  shares=qty, profit=profit, mode='US')
            except Exception:
                pass
            # 주문 후 5초 대기 후 즉시 잔고 재조회
            self._run_threaded(lambda: (time.sleep(5), self._sync_balance_from_kis()))
            return proceeds
        else:
            self.add_log(f"❌ SELL 주문 실패: {name}({ticker}) — KIS 응답 확인 필요")
            return 0.0

    # ─────────────────────────────────────────────────────────────────
    # 손익 기록
    # ─────────────────────────────────────────────────────────────────

    def _record_pnl(self, usd_pnl: float):
        today = _now_et().strftime("%Y-%m-%d")
        self.daily_pnl[today] = self.daily_pnl.get(today, 0.0) + usd_pnl

    def _get_total_assets_usd(self) -> float:
        """
        현재 총 자산 USD = 현금 + 전체 포지션 평가액.
        KR 봇의 '위성 수익 → 코어 재투자' 대신,
        수익금이 cash_usd에 환원된 뒤 이 값 기준으로 예산을 재산정함으로써
        코어·위성 40/40 고정 비율 복리 효과를 구현합니다.
        """
        pos_value = 0.0
        for t, p in self.core_positions.items():
            if p.shares > 0:
                price = self._price(t)
                if price > 0:
                    pos_value += p.shares * price
        for t, p in self.satellite_positions.items():
            if p.shares > 0:
                price = self._price(t)
                if price > 0:
                    pos_value += p.shares * price
        return self.cash_usd + pos_value

    # ─────────────────────────────────────────────────────────────────
    # 중앙 포지션 재구성 — KR봇 _init_dummy_cores() + initialize_portfolio() 통합
    # 모든 포지션 변경은 이 함수를 통해야 info↔positions 동기화 보장
    # ─────────────────────────────────────────────────────────────────

    def _rebuild_positions(self):
        """
        core_info / satellite_info 기준으로 positions를 완전 동기화.
        - info에 있고 positions에 없으면 → 즉시 생성
        - info에 없고 positions에 있고 0주면 → 즉시 제거
        - 보유 중(shares>0)인 종목은 info에 없어도 유지 (청산 대기)
        - 코어 종목이 위성에 중복이면 위성에서 제거

        reload_api_keys, _restore_state, _screen_cores, _screen_satellites
        어디서든 호출 가능 — 항상 일관된 상태 보장.
        """
        with self.lock:
            _before_core = set(self.core_positions.keys())
            _before_sat  = set(self.satellite_positions.keys())

            # ── 코어: info → positions 동기화 ────────────────────────
            core_info_tickers = {c["ticker"] for c in self.core_info}
            for t in list(self.core_positions.keys()):
                if t not in core_info_tickers and self.core_positions[t].shares == 0:
                    del self.core_positions[t]
            for c in self.core_info:
                t = c["ticker"]
                if t not in self.core_positions:
                    self.core_positions[t] = USPosition(
                        ticker=t, name=c.get("name", t), status="감시 중 👀"
                    )

            # ── 코어 중복 제거 (위성에서) ────────────────────────────
            core_t = set(self.core_positions.keys())
            self.satellite_info = [s for s in self.satellite_info
                                   if s.get("ticker") not in core_t]
            for t in list(self.satellite_positions.keys()):
                if t in core_t and self.satellite_positions[t].shares == 0:
                    del self.satellite_positions[t]

            # ── 위성: info → positions 동기화 ────────────────────────
            sat_info_tickers = {s.get("ticker") for s in self.satellite_info}
            for t in list(self.satellite_positions.keys()):
                if t not in sat_info_tickers and self.satellite_positions[t].shares == 0:
                    del self.satellite_positions[t]
            for s in self.satellite_info:
                t = s.get("ticker")
                if t and t not in self.satellite_positions:
                    self.satellite_positions[t] = USPosition(
                        ticker=t, name=s.get("name", t), status="감시 중 👀"
                    )

            # ── 변경 로그 ──────────────────────────────────────────────
            _after_core = set(self.core_positions.keys())
            _after_sat  = set(self.satellite_positions.keys())
            _removed = (_before_core | _before_sat) - (_after_core | _after_sat)
            _added   = (_after_core | _after_sat) - (_before_core | _before_sat)
            if _removed or _added:
                self.add_log(f"🔄 [rebuild] 제거: {_removed or '없음'} | 추가: {_added or '없음'}")
                self.add_log(f"   코어: {list(_after_core)} | 위성: {list(_after_sat)}")
            self._save_state()

    # ─────────────────────────────────────────────────────────────────
    # 코어 스크리닝 (매일 미보유 슬롯 재스캔)
    # ─────────────────────────────────────────────────────────────────

    def _inject_user_cores(self):
        """user_core_stocks를 core_info 앞 슬롯에 고정 (KR 봇 패턴 동일)."""
        if not self.user_core_stocks:
            return
        user_tickers = {s['ticker'] for s in self.user_core_stocks if s.get('ticker')}
        filtered = [c for c in self.core_info if c['ticker'] not in user_tickers]
        pinned = [
            {'ticker': s['ticker'], 'name': s.get('name', s['ticker']),
             'ai_reason': '사용자지정', 'score': 999}
            for s in self.user_core_stocks if s.get('ticker')
        ]
        self.core_info = (pinned + filtered)[:self.num_cores]

    def _inject_user_satellites(self):
        """user_satellite_stocks를 satellite_info 앞 슬롯에 고정 (KR 봇 패턴 동일)."""
        if not self.user_satellite_stocks:
            return
        user_tickers = {s['ticker'] for s in self.user_satellite_stocks if s.get('ticker')}
        filtered = [c for c in self.satellite_info if c['ticker'] not in user_tickers]
        pinned = [
            {'ticker': s['ticker'], 'name': s.get('name', s['ticker']),
             'sector': '', 'score': 999, 'ai_reason': '사용자지정'}
            for s in self.user_satellite_stocks if s.get('ticker')
        ]
        self.satellite_info = (pinned + filtered)[:self.num_satellites]

    def _screen_cores(self):
        """
        코어 종목 선정 — 매일 미보유 슬롯 대상 재스캔 (KR봇 동일).
        - 보유 중인 슬롯: 유지 (강제 교체 없음)
        - 미보유 슬롯: 더 좋은 후보 있으면 즉시 교체
        - 오늘 이미 스캔했으면 스킵 (중복 방지)
        """
        now = _now_et()
        today = now.strftime("%Y-%m-%d")
        if self.last_core_screen_date == today:
            return

        # 미보유 슬롯이 없으면 스캔 불필요
        holding = {t for t, p in self.core_positions.items() if p.shares > 0}
        empty_slots = self.num_cores - len(holding)
        if self.core_info and empty_slots <= 0:
            self.last_core_screen_date = today
            return
        self.add_log("🔍 US 코어 종목 스캔 시작 (KIS 실시간 랭킹)…")

        try:
            candidates = []

            # ① KIS 실시간 랭킹 우선 (거래량·신고가·모멘텀으로 시장이 증명한 종목)
            if self.kis_overseas:
                try:
                    candidates = scan_us_satellites_kis(
                        self.kis_overseas, n=self.num_cores * 4, exclude=holding
                    )
                    if candidates:
                        self.add_log(f"📡 KIS 랭킹 {len(candidates)}개 후보 수집")
                except Exception as e:
                    logger.warning(f"[US봇] KIS 코어 스캔 오류: {e}")

            # ② KIS 미연결 또는 결과 없으면 하드코딩 유니버스 폴백
            if not candidates:
                self.add_log("⚠️ KIS 랭킹 미사용 → 퀀트 유니버스 폴백")
                candidates = scan_us_cores(n=self.num_cores * 3, exclude=holding)

            if not candidates:
                self.add_log("⚠️ 코어 후보 없음 — 기존 유지")
                return

            # ③ AI 최종 선정 (장기 보유 적합성 판단)
            if self.claude:
                ai_result = self.claude.ai_select_us_core_stocks(
                    candidates=candidates, n=self.num_cores
                )
                if ai_result:
                    self.core_info = ai_result
                    names = [f"{c['ticker']}({c.get('ai_reason','')[:15]})" for c in self.core_info]
                    self.add_log(f"🤖 AI 코어 선정: {', '.join(names)}")
                else:
                    self.core_info = candidates[:self.num_cores]
                    self.add_log("⚠️ AI 코어 선정 실패 → 퀀트 상위 유지")
            else:
                self.core_info = candidates[:self.num_cores]
                self.add_log(f"✅ 코어 종목(퀀트): {[c['ticker'] for c in self.core_info]}")

            # 보유 중인 슬롯은 유지하고 미보유 슬롯만 새 후보로 교체
            holding_info = [c for c in self.core_info
                            if self.core_positions.get(c['ticker']) and
                            self.core_positions[c['ticker']].shares > 0]
            new_tickers   = {c['ticker'] for c in holding_info}
            new_info = list(holding_info)
            for c in (ai_result if self.claude and ai_result else candidates):
                if len(new_info) >= self.num_cores:
                    break
                if c['ticker'] not in new_tickers and c['ticker'] not in holding:
                    new_info.append(c)
                    new_tickers.add(c['ticker'])
            self.core_info = new_info[:self.num_cores]

            changed = [c['ticker'] for c in self.core_info if c['ticker'] not in {x['ticker'] for x in holding_info}]
            if changed:
                self.add_log(f"🔄 코어 슬롯 교체 (미보유): {changed}")
            self._inject_user_cores()

            self._rebuild_positions()
            self.last_core_screen_date = today
        except Exception as e:
            logger.warning(f"[US봇] 코어 스캔 오류: {e}")

    # ─────────────────────────────────────────────────────────────────
    # 코어 관리 (KR 코어와 동일 로직: RSI + ATR 손절 + 통합 점수)
    # ─────────────────────────────────────────────────────────────────

    def _manage_cores(self, buy_allowed: bool = True):
        """코어 포지션 매수/손절 — KR 코어와 동일 전략.
        buy_allowed=False 시 매수 로직 건너뜀 (시간외 손절 전용 모드)."""
        if not self.kis_overseas or not self.core_info:
            return

        import pandas as pd
        # 총자산 기준 예산 산정 (수익 복리 효과: 수익금 → cash_usd → 총자산 증가 → 예산 자동 증가)
        total_usd      = self._get_total_assets_usd()

        # ── 코어 풀 내 동적 균등 배분 ────────────────────────────────────
        # 코어 풀(CORE_RATIO) 안에서 활성 코어 수로 균등 분배
        # 1개: 풀 100% | 2개: 각 50% | 3개: 각 33%
        _core_pool      = total_usd * self.CORE_RATIO
        _active_cores   = max(1, len([t for t, p in self.core_positions.items() if p is not None]))
        core_budget_per = _core_pool / _active_cores

        for info in self.core_info:
            ticker = info["ticker"]
            pos    = self.core_positions.get(ticker)
            price  = self._price(ticker)
            if price <= 0:
                continue
            if pos is None:
                pos = USPosition(ticker=ticker, name=info["name"],
                                 budget_usd=core_budget_per)
                with self.lock:
                    self.core_positions[ticker] = pos

            # OHLCV 조회 (캐시 1시간 — 매 루프 재다운로드 방지)
            df_raw = self._get_cached_ohlcv(ticker, period="180d")
            if df_raw.empty:
                df_raw = None

            avg    = pos.avg_price_usd
            regime = self.market_regime

            # ── ATR 계산 ───────────────────────────────────────────────
            c_atr = avg * 0.02 if avg > 0 else price * 0.02
            if df_raw is not None and not df_raw.empty and all(
                    c in df_raw.columns for c in ['high', 'low', 'close']):
                try:
                    tr    = pd.concat([
                        df_raw['high'] - df_raw['low'],
                        (df_raw['high'] - df_raw['close'].shift(1)).abs(),
                        (df_raw['low']  - df_raw['close'].shift(1)).abs(),
                    ], axis=1).max(axis=1)
                    c_atr = float(tr.rolling(14, min_periods=1).mean().iloc[-1])
                except Exception:
                    pass

            hard_mult = self.CORE_HARD_MULT.get(regime, 2.5)
            is_cd     = time.time() - pos.last_order_time > self.ORDER_COOLDOWN

            # ── ATR 하드 손절 (전량) ────────────────────────────────────
            if pos.shares > 0 and avg > 0 and is_cd and price <= avg - (hard_mult * c_atr):
                # 손절 전 뉴스 확인 — 호재면 일시 노이즈일 수 있어 1회 유예
                _stop_news = self._fetch_us_news([ticker])
                _stop_skip = False
                if _stop_news and not getattr(pos, 'stop_news_checked', False):
                    _positive_kw = ['beat', 'upgrade', 'buy', 'bullish', 'record', 'contract', 'deal', 'win']
                    if any(kw in _stop_news.lower() for kw in _positive_kw):
                        pos.stop_news_checked = True
                        _stop_skip = True
                        self.add_log(f"⚠️ [코어] {pos.name} ATR 손절 터치 but 호재 뉴스 감지 → 1회 유예\n{_stop_news[:120]}")
                if not _stop_skip:
                    pos.stop_news_checked = False
                    _atr_reason = f"ATR×{hard_mult:.1f} 손절"

                    # ── AI 스윙 판단 우선 (코어도 동일) ───────────────
                    swing = self._ai_swing_check(pos, ticker, price, _atr_reason)

                    if swing == 'SELL_REBUY':
                        # 매도 후 재진입 큐
                        self._swing_rebuy_queue[ticker] = {
                            'sell_price':   price,
                            'target_price': price * 0.95,
                            'target_rsi':   35,
                            'name':         pos.name,
                            'ts':           time.time(),
                            'budget':       core_budget_per,
                            'is_core':      True,
                        }
                        self.add_log(f"🔄 [스윙 코어] {pos.name}({ticker}) SELL_REBUY — 재진입 큐 등록")
                        self._tg(f"🔄 [US 코어 스윙] {pos.name}\n매도 후 재진입 대기 | 조건: RSI≤35 or ${price*0.95:.2f}")
                        # 아래 매도 실행

                    elif swing == 'ACCUMULATE':
                        acc_cnt = self._swing_accumulate_cnt.get(ticker, 0)
                        _acc_budget = min(core_budget_per * 0.30, self.cash_usd * 0.15)
                        acc_qty = self._buy(ticker, pos.name, _acc_budget, price)
                        if acc_qty > 0:
                            with self.lock:
                                new_sh = pos.shares + acc_qty
                                pos.avg_price_usd = (pos.avg_price_usd * pos.shares + price * acc_qty) / new_sh
                                pos.shares = new_sh
                                pos.floor_shares = max(pos.floor_shares, pos.shares * 0.5)
                                pos.last_order_time = time.time()
                                pos.status = f"코어 스윙 누적 {acc_cnt+1}차 📥"
                                self._swing_accumulate_cnt[ticker] = acc_cnt + 1
                            self.add_log(f"📥 [스윙 코어] {pos.name}({ticker}) ACCUMULATE {acc_cnt+1}차 | {acc_qty}주 @ ${price:.2f}")
                            self._tg(f"📥 [US 코어 스윙 누적] {pos.name}\n{acc_cnt+1}차 추가매수 | 평단 ${pos.avg_price_usd:.2f}")
                        continue  # 청산 없이 다음 루프

                    # EXIT 또는 SELL_REBUY → 공통 매도 실행
                    proceeds = self._sell(ticker, pos.name, pos.shares, price)
                    pnl      = _net_profit_usd(price, avg, pos.shares)
                    with self.lock:
                        pos.shares                  = 0.0
                        pos.floor_shares            = 0.0
                        pos.partial_sold            = False
                        pos.partial_sold_2          = False
                        pos.second_buy_price        = 0.0
                        pos.second_buy_cash         = 0.0
                        pos.second_buy_done         = False
                        pos.third_buy_price         = 0.0
                        pos.third_buy_cash          = 0.0
                        pos.third_buy_done          = False
                        pos.initial_shares_for_exit = 0.0
                        pos.bull_pyramid_done       = False
                        pos.status                  = "코어 손절 🚨" if swing == 'EXIT' else "코어 스윙매도 🔄"
                    self._record_pnl(pnl)
                    self._reinvest_to_cores(pnl, _atr_reason)
                    self.add_log(f"🚨 코어 손절 {pos.name}({ticker}) | {_atr_reason} [{swing}] | PnL ${pnl:+.0f}")
                    self._tg(f"🚨 [US 코어 손절] {pos.name}\n{_atr_reason} | {swing} | ${pnl:+,.0f}")
                    if self.claude:
                        self.claude.record_trade_event(f"코어 손절 {pos.name}({ticker}) | {_atr_reason} | {swing} | ${pnl:+.0f}")
                continue

            # ── 통합 진입 점수 + RSI 매수 신호 (정규장만 매수) ────────────
            if not buy_allowed:
                continue   # 시간외 → 매수 건너뜀 (손절은 위에서 이미 처리됨)

            if pos.shares == 0 and is_cd:
                available_cash = self.cash_usd
                budget = min(core_budget_per, available_cash)
                if budget < price * 0.1:
                    continue

                # ── 코어 전용 진입 점수 (RSI 저평가 + 120MA/60MA만 판단) ──────
                # 모멘텀·거래량·MACD 완전 무시 — 장기 프로젝트 원칙
                if df_raw is not None and not df_raw.empty:
                    c_score, c_reasons = calculate_core_entry_score(df_raw, price, regime)
                else:
                    c_score, c_reasons = 0, []

                c_threshold = self.entry_thresholds.get(f'core_{regime}', get_core_entry_threshold(regime))

                if c_score < c_threshold:
                    with self.lock:
                        pos.status = f"코어 진입 대기 ({c_score}/{c_threshold}pt) ⏳"
                    continue

                budget_ratio = max(0.5, c_score / max(c_threshold, 1) * 0.75)
                # 3트랜치 분할: 1차=ratio, 2차=min(ratio,남은), 3차=나머지
                first_usd    = budget * budget_ratio
                _c_remain1   = max(0.0, budget - first_usd)
                reserve_usd  = min(budget * budget_ratio, _c_remain1)
                third_usd    = max(0.0, budget - first_usd - reserve_usd)
                qty = int(first_usd // price)
                if qty > 0:
                    # AI 승인 (위성과 동일)
                    approved, ai_reason = True, "AI 미설정"
                    if self.claude:
                        with self.lock:
                            pos.status = "AI 심사 중 🤖"
                        momentum_20d = 0.0
                        try:
                            c = df_raw['close'].dropna()
                            if len(c) >= 21:
                                momentum_20d = float((c.iloc[-1] / c.iloc[-21] - 1) * 100)
                        except Exception:
                            pass
                        _core_news = self._fetch_us_news([ticker])
                        _core_fundamental = self._fetch_fundamental(ticker)
                        _core_ai_reason = info.get("ai_reason", "")
                        if _core_fundamental:
                            _core_ai_reason = f"{_core_ai_reason} | [{_core_fundamental}]".strip(" |")
                        approved, ai_reason = self.claude.ai_approve_us_trade(
                            signal         = 'BUY',
                            stock_name     = pos.name,
                            ticker         = ticker,
                            price_usd      = price,
                            sector         = info.get("sector", ""),
                            hot_sectors    = self.hot_sectors,
                            momentum_20d   = momentum_20d,
                            rsi            = info.get("rsi", 50.0),
                            ai_reason      = _core_ai_reason,
                            news_headlines = _core_news,
                        )
                    if not approved:
                        with self.lock:
                            pos.status = f"코어 AI 거절 🛑 ({c_score}pt)"
                        self.add_log(f"🛑 코어 AI 거절 {pos.name}({ticker}): {ai_reason[:60]}")
                        if self.claude:
                            self.claude.record_trade_event(
                                f"코어 매수 거절 🛑 {pos.name}({ticker}) @ ${price:.2f} | "
                                f"거절이유: {ai_reason[:80]}"
                            )
                        self._tg(
                            f"🛑 <b>[US 코어 매수 거절]</b>  {self.alert_icon}\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"📌 <b>{pos.name}</b>  {ticker}\n"
                            f"🤖 {ai_reason[:100]}\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"⏰ {_now_et().strftime('%H:%M ET')}"
                        )
                    else:
                        # ── 코어 분봉 확인 (BULL 제외, 5분봉 하락 추세면 대기) ──
                        if not self._check_minute_trend_up_us(ticker):
                            with self.lock:
                                pos.status = "분봉 하락 📉"
                            self.add_log(f"⏸ 코어 분봉 하락 보류: {pos.name}({ticker}) — 다음 턴 재시도")
                            continue
                        bought_qty = self._buy(ticker, pos.name, first_usd, price)
                        if bought_qty > 0:
                            with self.lock:
                                pos.shares          = float(bought_qty)
                                # floor_shares: 첫 매수 수량의 50% — 이 이하로 절대 매도 안 함
                                pos.floor_shares    = max(pos.floor_shares, float(bought_qty) * 0.5)
                                pos.avg_price_usd   = price
                                pos.max_price_usd   = price
                                pos.partial_sold    = False
                                pos.partial_sold_2  = False
                                pos.last_order_time = time.time()
                                pos.second_buy_price       = price * 0.98
                                pos.second_buy_cash        = reserve_usd
                                pos.second_buy_done        = False
                                pos.third_buy_price        = price * 0.96
                                pos.third_buy_cash         = third_usd
                                pos.third_buy_done         = False
                                pos.initial_shares_for_exit= 0
                                pos.status          = f"코어 보유 💎 ({c_score}pt)"
                            score_str = " | ".join(c_reasons[:3])
                            self.add_log(f"💎 코어 1차 매수 {pos.name}({ticker}) {bought_qty}주 @ ${price:.2f} | {c_score}pt [{score_str}] | 2차 예약 ${price*0.98:.2f} | AI: {ai_reason[:40]}")
                            self._tg(f"💎 [US 코어 1차 매수] {pos.name} ({ticker})\n@ ${price:.2f}  점수 {c_score}pt\n2차 예약: ${price*0.98:.2f} (-2%)\n3차 예약: ${price*0.96:.2f} (-4%)")
                            if self.claude:
                                self.claude.record_trade_event(
                                    f"코어 매수 ✅ {pos.name}({ticker}) {bought_qty}주 @ ${price:.2f} | "
                                    f"진입점수 {c_score}pt | 근거: {score_str} | AI승인: {ai_reason[:60]}"
                                )

            # ── 코어 2차 분할 매수: 1차 진입가 -2% 눌림목 ──────────────
            if (pos.shares > 0 and avg > 0 and is_cd
                    and not getattr(pos, 'second_buy_done', True)
                    and getattr(pos, 'second_buy_price', 0) > 0
                    and price <= pos.second_buy_price
                    and getattr(pos, 'second_buy_cash', 0) >= price):
                sq = self._buy(ticker, pos.name, pos.second_buy_cash, price)
                if sq > 0:
                    with self.lock:
                        new_shares = pos.shares + sq
                        pos.avg_price_usd  = (pos.avg_price_usd * pos.shares + price * sq) / new_shares if new_shares > 0 else price
                        pos.shares         = new_shares
                        # floor_shares는 오직 증가 방향으로만 (주식 수 축적 원칙)
                        pos.floor_shares   = max(pos.floor_shares, pos.shares * 0.5)
                        pos.second_buy_done= True
                        pos.second_buy_cash= 0.0
                        pos.last_order_time= time.time()
                        pos.status         = "2차 매수 ✅"
                    self.add_log(f"💎 코어 2차 매수 {pos.name}({ticker}) {sq}주 @ ${price:.2f} | 눌림목 -2%")
                    self._tg(f"💎 [US 코어 2차 매수] {pos.name}\n@ ${price:.2f}  눌림목 -2% 포착")

            # ── 코어 3차 분할 매수: 1차 진입가 -4% 눌림목 ──────────────
            if (pos.shares > 0 and avg > 0 and is_cd
                    and getattr(pos, 'second_buy_done', False)
                    and not getattr(pos, 'third_buy_done', True)
                    and getattr(pos, 'third_buy_price', 0) > 0
                    and price <= pos.third_buy_price
                    and getattr(pos, 'third_buy_cash', 0) >= price):
                sq3 = self._buy(ticker, pos.name, pos.third_buy_cash, price)
                if sq3 > 0:
                    with self.lock:
                        new_sh3 = pos.shares + sq3
                        pos.avg_price_usd  = (pos.avg_price_usd * pos.shares + price * sq3) / new_sh3 if new_sh3 > 0 else price
                        pos.shares         = new_sh3
                        pos.floor_shares   = max(pos.floor_shares, pos.shares * 0.5)
                        pos.third_buy_done = True
                        pos.third_buy_cash = 0.0
                        pos.last_order_time= time.time()
                        pos.status         = "3차 매수 ✅"
                    self.add_log(f"💎 코어 3차 매수 {pos.name}({ticker}) {sq3}주 @ ${price:.2f} | 눌림목 -4%")
                    self._tg(f"💎 [US 코어 3차 매수] {pos.name}\n@ ${price:.2f}  눌림목 -4% 포착")

            # ── BULL 불타기 (코어 피라미딩) — +3% 돌파 + MA5 정배열 ──
            # BULL 장 + 보유 중 +3% 이상 + 정배열 확인 시 잔여현금 30% 추가 매수
            if (regime == "BULL" and pos.shares > 0 and avg > 0 and is_cd
                    and not getattr(pos, 'bull_pyramid_done', False)
                    and price >= avg * 1.03):
                try:
                    _py_ok = False
                    if df_raw is not None and not df_raw.empty and len(df_raw['close'].dropna()) >= 22:
                        _cl_py  = df_raw['close'].dropna()
                        _py_ok  = float(_cl_py.rolling(5).mean().iloc[-1]) > float(_cl_py.rolling(20).mean().iloc[-1])
                    if _py_ok:
                        _py_budget = min(core_budget_per * 0.30, self.cash_usd * 0.15)
                        _py_qty = self._buy(ticker, pos.name, _py_budget, price)
                        if _py_qty > 0:
                            _py_pct = (price / avg - 1) * 100
                            with self.lock:
                                new_sh = pos.shares + _py_qty
                                pos.avg_price_usd  = (pos.avg_price_usd * pos.shares + price * _py_qty) / new_sh if new_sh > 0 else price
                                pos.shares         = new_sh
                                # floor_shares 갱신 (항상 증가 방향)
                                pos.floor_shares   = max(pos.floor_shares, pos.shares * 0.5)
                                pos.bull_pyramid_done = True
                                pos.last_order_time= time.time()
                                pos.status         = f"불타기 🔥 (+{_py_pct:.1f}%)"
                            self.add_log(f"🔥 [BULL 불타기] {pos.name}({ticker}) +{_py_pct:.1f}% | {_py_qty}주 @ ${price:.2f} 추가")
                            self._tg(f"🔥 [US BULL 불타기] {pos.name}\n+{_py_pct:.1f}% 추세 추종 | {_py_qty}주 @ ${price:.2f}")
                except Exception as _pye:
                    logger.debug(f"[US봇] BULL 불타기(코어) 오류: {_pye}")
            # ─────────────────────────────────────────────────────────────

            # ── 코어 복합 SELL 신호 → 전량 청산 (floor_shares 무시) ──────────
            # KR봇: 'RSI 데드크로스 → 전량 매도 (floor_shares 제거)' 동일 원칙
            # 추세 붕괴 확인 시 floor를 포함한 전량 청산 후 재진입 타점 탐색
            if pos.shares > 0 and avg > 0 and is_cd and df_raw is not None and not df_raw.empty:
                try:
                    _core_sig, _core_buy_sc, _core_sell_sc, _core_sig_reasons = get_composite_signal(df_raw)
                    if _core_sig == 'SELL' and _core_sell_sc >= 2:
                        # 뉴스 유예 (호재 뉴스 있으면 1회 건너뜀)
                        _core_sell_news = self._fetch_us_news([ticker])
                        _core_sell_skip = False
                        if _core_sell_news and not getattr(pos, 'sell_news_checked', False):
                            _positive_kw = ['beat', 'upgrade', 'buy', 'bullish', 'record', 'contract', 'deal', 'win']
                            if any(kw in _core_sell_news.lower() for kw in _positive_kw):
                                pos.sell_news_checked = True
                                _core_sell_skip = True
                                self.add_log(
                                    f"⚠️ [코어 SELL신호] {pos.name} 복합신호 SELL but 호재 뉴스 → 1회 유예\n"
                                    f"{_core_sell_news[:120]}"
                                )
                        if not _core_sell_skip:
                            pos.sell_news_checked = False
                            q   = pos.shares
                            self._sell(ticker, pos.name, q, price)
                            pnl = _net_profit_usd(price, avg, q)
                            with self.lock:
                                pos.shares                  = 0.0
                                pos.floor_shares            = 0.0
                                pos.partial_sold            = False
                                pos.partial_sold_2          = False
                                pos.second_buy_price        = 0.0
                                pos.second_buy_cash         = 0.0
                                pos.second_buy_done         = False
                                pos.third_buy_price         = 0.0
                                pos.third_buy_cash          = 0.0
                                pos.third_buy_done          = False
                                pos.initial_shares_for_exit = 0.0
                                pos.bull_pyramid_done       = False
                                pos.ai_exit_decision        = None
                                pos.status                  = "코어 추세청산 📉"
                            self._record_pnl(pnl)
                            self.add_log(
                                f"📉 코어 복합신호 전량청산 {pos.name}({ticker}) "
                                f"| SELL {_core_sell_sc}pt [{' '.join(_core_sig_reasons[:2])}] "
                                f"| PnL ${pnl:+.0f}"
                            )
                            self._tg(
                                f"📉 [US 코어 추세청산] {pos.name}\n"
                                f"복합신호 SELL {_core_sell_sc}pt — 재진입 타점 탐색\n"
                                f"손익: ${pnl:+,.0f}"
                            )
                            continue
                except Exception as _csig_err:
                    logger.debug(f"[US봇] 코어 복합신호 체크 오류 ({ticker}): {_csig_err}")

            # ── 코어 BEAR 조기 익절: +5% 도달 시 즉시 전량 청산 (floor_shares 무시) ──────
            # 하락장 반등은 짧고 강함 — 수익이 난 순간 전량 회수 (KR봇 동일 원칙)
            if pos.shares > 0 and avg > 0 and is_cd and regime == "BEAR":
                pnl_pct_bear = (price / avg - 1) * 100
                if pnl_pct_bear >= 5.0:
                    q   = pos.shares
                    self._sell(ticker, pos.name, q, price)
                    pnl = _net_profit_usd(price, avg, q)
                    with self.lock:
                        pos.shares            = 0.0
                        pos.floor_shares      = 0.0   # floor 리셋 — 재진입 시 새로 설정
                        pos.second_buy_price  = 0.0
                        pos.second_buy_cash   = 0.0
                        pos.second_buy_done   = False
                        pos.partial_sold      = False
                        pos.partial_sold_2    = False
                        pos.bull_pyramid_done = False
                        pos.status            = "BEAR 조기익절 🐻"
                    self._record_pnl(pnl)
                    self.add_log(f"🐻 코어 BEAR 조기익절 {pos.name}({ticker}) +{pnl_pct_bear:.1f}% | PnL ${pnl:+.0f}")
                    self._tg(f"🐻 [US 코어 BEAR 익절] {pos.name}\n+{pnl_pct_bear:.1f}% 하락장 반등 수확\nPnL ${pnl:+,.0f}")
                    continue

            # ── 코어 부분 익절 (AI 판단) ──────────────────────────────
            elif pos.shares > 0 and avg > 0:
                pnl_pct = (price / avg - 1) * 100
                decision = getattr(pos, 'ai_exit_decision', None)
                # BULL 장에서는 추세가 강하므로 익절 기준 상향 (+15%/+30%)
                _core_partial1 = 15.0 if regime == "BULL" else self.PARTIAL1_PCT
                _core_partial2 = 30.0 if regime == "BULL" else self.PARTIAL2_PCT

                # 1차: +10%(일반) / +15%(BULL) 도달 → AI에 익절 여부 문의
                if not pos.partial_sold and pnl_pct >= _core_partial1 and pos.shares > 1:
                    if decision is None:
                        if self.claude:
                            self._trigger_ai_partial_exit(pos, ticker, pos.name, price, avg, pnl_pct, regime)
                            with self.lock: pos.status = f"AI 익절 검토 중 ({pnl_pct:+.1f}%) 🤖"
                        else:
                            with self.lock: pos.ai_exit_decision = "SELL_PARTIAL"
                    elif decision == "HOLD":
                        with self.lock:
                            pos.status = f"AI 홀드 ({pnl_pct:+.1f}%) ⏳"
                    else:
                        # 원금 주수 고정 (복리 방지 — KR봇 동일)
                        if not getattr(pos, 'initial_shares_for_exit', 0):
                            with self.lock: pos.initial_shares_for_exit = pos.shares
                        _c_init1 = getattr(pos, 'initial_shares_for_exit', 0) or pos.shares
                        sellable_p1 = max(0.0, pos.shares - pos.floor_shares)
                        if sellable_p1 > 0:
                            q   = max(1.0, min(_c_init1 * self.PARTIAL1_QTY, sellable_p1))
                            self._sell(ticker, pos.name, q, price)
                            pnl = _net_profit_usd(price, avg, q)
                            with self.lock:
                                pos.shares           -= q
                                pos.ai_exit_decision  = None
                                pos.status            = f"코어 1차익절({pnl_pct:+.1f}%) ✂️"
                            self._record_pnl(pnl)
                            self.add_log(f"✂️  코어 1차익절 {pos.name} ({q:.0f}주 / 원금 {_c_init1:.0f}주 기준 50%) | PnL ${pnl:+.0f}")
                        with self.lock:
                            pos.partial_sold = True
                            if sellable_p1 <= 0:
                                pos.ai_exit_decision = None
                                pos.status = f"코어 floor 보호 ({pnl_pct:+.1f}%) 🛡️"

                # 2차: +20%(일반) / +30%(BULL) → 원금 기준 50% 부분 매도 (KR봇 동일)
                elif pos.partial_sold and not pos.partial_sold_2 and pnl_pct >= _core_partial2 and pos.shares > 0:
                    if decision is None:
                        if self.claude:
                            self._trigger_ai_partial_exit(pos, ticker, pos.name, price, avg, pnl_pct, regime)
                            with self.lock: pos.status = f"AI 2차익절 검토 ({pnl_pct:+.1f}%) 🤖"
                        else:
                            with self.lock: pos.ai_exit_decision = "SELL_PARTIAL"
                    elif decision == "HOLD":
                        with self.lock:
                            pos.status = f"AI 홀드 ({pnl_pct:+.1f}%) ⏳"
                    else:
                        _c_init2 = getattr(pos, 'initial_shares_for_exit', 0) or pos.shares
                        sellable_p2 = max(0.0, pos.shares - pos.floor_shares)
                        if sellable_p2 > 0:
                            q2  = max(1.0, min(_c_init2 * 0.50, sellable_p2))
                            self._sell(ticker, pos.name, q2, price)
                            pnl = _net_profit_usd(price, avg, q2)
                            self._record_pnl(pnl)
                            _thr2c = "30%(BULL)" if regime == "BULL" else "20%"
                            self.add_log(f"✅ 코어 2차익절 {pos.name} +{_thr2c} ({q2:.0f}주 / 원금 {_c_init2:.0f}주 기준 50%) | PnL ${pnl:+.0f}")
                            self._tg(f"✅ [US 코어 2차익절] {pos.name}\n+{_thr2c} | 잔여 {pos.shares - q2:.0f}주 ATR 대기 | ${pnl:+,.0f}")
                            with self.lock:
                                pos.shares           = max(0.0, pos.shares - q2)
                                pos.partial_sold_2   = True
                                pos.ai_exit_decision = None
                                pos.status           = f"코어 2차익절({pnl_pct:+.1f}%) ✅"

                else:
                    with self.lock:
                        pos.status = f"코어 보유 💎 ({pnl_pct:+.1f}%)"

    # ─────────────────────────────────────────────────────────────────
    # AI 익절 판단 헬퍼 (백그라운드 비차단)
    # ─────────────────────────────────────────────────────────────────

    def _trigger_ai_partial_exit(self, pos, ticker: str, name: str,
                                  price: float, avg: float,
                                  pnl_pct: float, regime: str):
        """AI 익절 판단을 백그라운드 스레드로 요청 (메인 루프 비차단).

        HOLD 후에는 시간 대신 가격 기준: 마지막 문의가격 대비 +1% 이상 올랐을 때만 재요청.
        """
        if getattr(pos, 'ai_exit_pending', False):
            return
        asked = getattr(pos, 'ai_exit_asked_price', 0.0)
        # HOLD 후 재요청 조건: +1% 상승 OR -2% 하락 (가격 반납 감지)
        if getattr(pos, 'ai_exit_decision', None) == "HOLD" and asked > 0:
            risen  = price >= asked * 1.01
            fallen = price <= asked * 0.98
            if not risen and not fallen:
                return
        pos.ai_exit_pending     = True
        pos.ai_exit_asked_price = price  # 현재 문의 가격 기록

        def _worker():
            try:
                _news = self._fetch_us_news([ticker])
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

    # ─────────────────────────────────────────────────────────────────
    # 위성 스크리닝 (하루 1회)
    # ─────────────────────────────────────────────────────────────────

    _GROWTH_KEEP_PCT = 3.0   # +3% 이상 = 성장세 양호 → 교체 없이 강제 유지

    # ── 주말 사전 분석 ────────────────────────────────────────────────────
    def _weekend_satellite_scan_us(self):
        """주말(토·일) 10:00 ET — 위성 후보 분석 후 월요일 교체 계획 수립."""
        now = _now_et()
        today_str = now.strftime('%Y-%m-%d')
        if self._weekend_scan_done == today_str:
            return
        if now.weekday() < 5:
            return  # 평일 실행 안 함

        self.add_log("📅 [US 주말 사전분석] 위성 후보 스캔 시작...")
        try:
            from US.screener import scan_us_satellites
            with self.lock:
                current_sat = {t: p for t, p in self.satellite_positions.items() if p.shares > 0}
            current_tickers = set(current_sat.keys())

            # 교체 후보 파악: 수익률 낮은 보유 종목
            swap_plan = {}
            for ticker, pos in current_sat.items():
                price = self._price(ticker)
                if price > 0 and pos.avg_price_usd > 0:
                    pnl_pct = (price / pos.avg_price_usd - 1) * 100
                    if pnl_pct < -2.0:  # -2% 이하면 교체 후보
                        swap_plan[ticker] = {"pnl_pct": pnl_pct, "name": pos.name}

            if not swap_plan:
                self.add_log("📅 [US 주말분석] 교체 대상 없음 — 현 포지션 유지")
                self._weekend_scan_done = today_str
                self._save_state()
                return

            # 신규 후보 스캔
            candidates = scan_us_satellites(
                kis=self.kis_overseas, n=len(swap_plan) * 3,
                exclude=current_tickers
            )

            new_plan = {}
            cand_iter = iter(candidates)
            for old_ticker, old_info in swap_plan.items():
                try:
                    cand = next(cand_iter)
                    new_plan[old_ticker] = {
                        "new_ticker": cand["ticker"],
                        "new_name":   cand.get("name", cand["ticker"]),
                        "reason":     f"수익률 {old_info['pnl_pct']:+.1f}% 부진"
                    }
                    self.add_log(
                        f"📋 [US 주말분석] {old_info['name']}({old_ticker}) → "
                        f"{cand.get('name',cand['ticker'])}({cand['ticker']}) 교체 예정"
                    )
                except StopIteration:
                    break

            self._monday_swap_plan  = new_plan
            self._weekend_scan_done = today_str
            self._save_state()

            if new_plan:
                self._tg(
                    f"📅 <b>US 주말 사전분석 완료</b>  {self.alert_icon}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    + "\n".join([f"· {swap_plan[o]['name']}({o}) → {v['new_name']}({v['new_ticker']})" for o, v in new_plan.items()])
                    + f"\n⏰ 월요일 9:30 ET 장 시작 시 자동 실행"
                )
        except Exception as e:
            logger.error(f"[US봇] 주말 사전분석 오류: {e}", exc_info=True)

    def _execute_monday_swap_us(self):
        """월요일 9:30 ET 장 시작 — 주말 교체 계획 실행."""
        if not self._monday_swap_plan:
            return
        now = _now_et()
        if now.weekday() != 0:
            return

        self.add_log(f"🚀 [US 월요일 교체] {len(self._monday_swap_plan)}건 실행")
        executed = []
        for old_ticker, plan in list(self._monday_swap_plan.items()):
            try:
                with self.lock:
                    pos = self.satellite_positions.get(old_ticker)
                if not pos or pos.shares <= 0:
                    executed.append(old_ticker)
                    continue
                price = self._price(old_ticker)
                if not price:
                    continue
                qty = pos.shares
                if self._sell_order(old_ticker, qty, reason="주말계획교체"):
                    self.add_log(f"📤 [US] {pos.name}({old_ticker}) 매도 완료")
                    with self.lock:
                        self.satellite_info = [s for s in self.satellite_info if s.get('ticker') != old_ticker]
                        self.satellite_info.insert(0, {
                            "ticker": plan['new_ticker'], "name": plan['new_name'],
                            "sector": "-", "return_pct": 0
                        })
                    executed.append(old_ticker)
            except Exception as e:
                logger.error(f"[US봇] 월요일 교체 실행 오류({old_ticker}): {e}")

        for t in executed:
            self._monday_swap_plan.pop(t, None)
        if executed:
            self._save_state()
            self.add_log(f"✅ [US 월요일 교체] {len(executed)}건 완료")

    def _screen_satellites(self):
        today = _now_et().strftime("%Y-%m-%d")
        if self.last_screen_date == today:
            return
        self.last_screen_date = today  # 즉시 선점 — 동시 호출 중복 실행 방지

        # ── 성장세 양호 종목 파악 — 교체 슬롯에서 제외 ──────────────
        strong_keep_info: list = []
        strong_keep_tickers: set = set()
        for t, p in list(self.satellite_positions.items()):
            if p.shares > 0 and p.avg_price_usd > 0:
                price = self._price(t)
                if price > 0:
                    pnl_pct = (price / p.avg_price_usd - 1) * 100
                    if pnl_pct >= self._GROWTH_KEEP_PCT:
                        existing = next((i for i in self.satellite_info if i["ticker"] == t), None)
                        strong_keep_info.append(existing or {"ticker": t, "name": p.name,
                                                             "sector": "", "score": 0, "ai_reason": ""})
                        strong_keep_tickers.add(t)
                        self.add_log(f"🌱 {p.name}({t}) 성장세 양호 ({pnl_pct:+.1f}%) — 교체 없이 유지")

        slots_needed = self.num_satellites - len(strong_keep_tickers)
        if slots_needed <= 0:
            self.add_log(f"✅ 위성 {len(strong_keep_tickers)}개 성장세 양호 — 재스크리닝 스킵")
            self.satellite_info   = strong_keep_info
            self.last_screen_date = today
            return

        # ── 빈 슬롯만 새로 채움 ──────────────────────────────────────
        # 코어 positions + core_info 모두 제외 (코어 교체 직후에도 중복 방지)
        core_tickers = set(self.core_positions.keys()) | {c["ticker"] for c in self.core_info}
        holding = strong_keep_tickers | {t for t, p in self.satellite_positions.items() if p.shares > 0} | core_tickers
        self.add_log(f"🔍 미국 위성 종목 스캔 시작… (빈 슬롯 {slots_needed}개)")

        candidates: list = []

        # ① AI 테마 발굴 → yfinance 퀀트 검증 (제2의 엔비디아 발굴)
        if self.claude:
            try:
                self.add_log("🤖 AI 테마 발굴 시작 (제2의 엔비디아·로켓랩 후보 탐색)…")
                themes = self.claude.ai_discover_satellite_themes()
                if themes:
                    theme_tickers = []
                    for theme in themes:
                        t_list = [t for t in theme.get("tickers", []) if t not in holding]
                        self.add_log(f"  💡 테마: {theme.get('theme','')} → {t_list}")
                        theme_tickers.extend(t_list)

                    # yfinance로 퀀트 검증 (실제 존재 + 지표 계산)
                    if theme_tickers:
                        from US.screener import _scan_universe, _satellite_score, SATELLITE_UNIVERSE
                        theme_universe = {"AI발굴": list(set(theme_tickers) - holding)}
                        quant_results = _scan_universe(
                            theme_universe,
                            n=slots_needed * 3,
                            exclude=holding,
                            score_fn=_satellite_score,
                        )
                        # 테마 정보 보강
                        ticker_theme_map = {}
                        for theme in themes:
                            for t in theme.get("tickers", []):
                                ticker_theme_map[t] = theme.get("theme", "AI발굴")
                        for c in quant_results:
                            c["sector"] = ticker_theme_map.get(c["ticker"], "AI발굴")
                            c["ai_theme"] = ticker_theme_map.get(c["ticker"], "")

                        candidates = quant_results
                        self.add_log(f"✅ AI 테마 발굴 + 퀀트 검증: {len(candidates)}개 후보")
            except Exception as _e:
                logger.warning(f"[US봇] AI 테마 발굴 오류: {_e}")
                candidates = []

        # ② AI 실패 또는 결과 부족 → 하드코딩 유니버스 yfinance 스캔 폴백
        if not candidates:
            self.add_log("📈 yfinance 위성 유니버스 폴백…")
            candidates = scan_us_satellites(n=slots_needed * 2 + 2, exclude=holding)

        if not candidates:
            self.add_log("⚠️ 스캔 결과 없음 — 기존 위성 유지")
            self.satellite_info   = strong_keep_info + [i for i in self.satellite_info
                                                        if i["ticker"] not in strong_keep_tickers]
            self.last_screen_date = today
            return

        # 이미 보유 중인 종목은 중복 선정 방지
        holding_tickers = {t for t, p in self.satellite_positions.items() if p.shares > 0}
        candidates = [c for c in candidates if c["ticker"] not in holding_tickers]

        # ── ROE 턴어라운드 보너스 반영 → 선정 점수에 가산 후 재정렬 ──
        for c in candidates:
            _rb, _rr = self._roe_turnaround_bonus(c["ticker"])
            if _rb > 0:
                c["score"] = c.get("score", 0) + _rb
                c["ai_reason"] = (c.get("ai_reason", "") + f" | {_rr}").strip(" |")
        candidates.sort(key=lambda x: x.get("score", 0), reverse=True)

        # 섹터 다양성: 같은 섹터 최대 2개
        seen_sec: dict = {}
        filtered: list = []
        for c in candidates:
            s = c["sector"]
            seen_sec[s] = seen_sec.get(s, 0) + 1
            if seen_sec[s] <= 2:
                filtered.append(c)

        # ── AI 위성 선정 (빈 슬롯 대상만) ───────────────────────────
        new_info: list = []
        if self.claude and filtered:
            try:
                ai_result = self.claude.ai_select_us_satellites(
                    candidates  = filtered,
                    hot_sectors = self.hot_sectors or [],
                    n           = slots_needed,
                    sector_guide= self.sector_guide,
                )
                if ai_result:
                    new_info = ai_result
                    names = [f"{c['ticker']}(AI:{c.get('ai_reason','')[:20]})" for c in new_info]
                    self.add_log(f"🤖 AI 위성 선정 (신규): {', '.join(names)}")
                else:
                    new_info = filtered[:slots_needed]
                    self.add_log("⚠️ AI 선정 실패 → 퀀트 상위 종목")
            except Exception as e:
                logger.warning(f"[US봇] AI 위성 선정 오류: {e}")
                new_info = filtered[:slots_needed]
        else:
            new_info = filtered[:slots_needed]
            names = [f"{c['ticker']}(점수:{c['score']:.0f})" for c in new_info]
            self.add_log(f"✅ 위성 종목 선정 (신규): {', '.join(names)}")

        self.satellite_info   = strong_keep_info + new_info
        # US봇은 100% AI 자율 운영 — 사용자 지정 위성 무시
        # self._inject_user_satellites()
        self.add_log(f"📋 위성 info 확정: {[s.get('ticker') for s in self.satellite_info]}")

        _new_hot = list({c["sector"] for c in self.satellite_info if c.get("sector")})
        if _new_hot:
            self.hot_sectors = _new_hot
        self.last_screen_date = today

        # 중앙 재구성: info↔positions 완전 동기화 (중복 제거 포함)
        try:
            self.add_log("🔧 위성 rebuild 호출...")
            self._rebuild_positions()
            self.add_log(f"✅ 위성 rebuild 완료 — positions: {list(self.satellite_positions.keys())}")
        except Exception as _rb_err:
            logger.error(f"[US봇] 위성 rebuild 오류: {_rb_err}", exc_info=True)

        # 신규 종목 선정 시 텔레그램 알림 (KR봇 initialize_portfolio 동일)
        if new_info:
            _lines = "\n".join([
                f"• <b>{c.get('name', c['ticker'])}</b>  <code>{c['ticker']}</code>  [{c.get('sector','')}]"
                for c in self.satellite_info
            ])
            self._tg(
                f"🔍 <b>US 위성 종목 선정{'(AI 검토)' if self.claude else ''}</b>  {self.alert_icon}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{_lines}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⏰ {_now_et().strftime('%H:%M ET')}"
            )

    # ─────────────────────────────────────────────────────────────────
    # 위성 관리 (매수 + 청산 조건)
    # ─────────────────────────────────────────────────────────────────

    def _manage_satellites(self, buy_allowed: bool = True):
        """위성 포지션 매수/손절.
        buy_allowed=False 시 매수 로직 건너뜀 (시간외 손절 전용 모드)."""
        if not self.kis_overseas:
            return

        # 중앙 재구성: info↔positions 동기화 (매 루프마다 일관성 보장)
        self._rebuild_positions()

        import pandas as pd
        # ── 위성 풀 내 동적 균등 배분 ────────────────────────────────────
        # 위성 풀(SAT_RATIO) 안에서 활성 위성 수로 균등 분배
        # 1개: 풀 100% | 2개: 각 50% | 3개: 각 33%
        total_usd      = self._get_total_assets_usd()
        _sat_pool      = total_usd * self.SAT_RATIO
        _active_sats   = max(1, len(self.satellite_info))
        sat_budget_per = _sat_pool / _active_sats
        regime         = self.market_regime

        # ── 미보유 후보 매수 (정규장만) ──────────────────────────────
        for info in (self.satellite_info if buy_allowed else []):
            ticker = info["ticker"]
            pos    = self.satellite_positions.get(ticker)
            if pos and pos.shares > 0:
                continue
            # 블랙리스트 없음 — satellite_info에서 이미 제거됐으므로 여기까지 오지 않음
            if pos and (time.time() - pos.last_order_time < self.ORDER_COOLDOWN):
                continue
            price = self._price(ticker)
            if price <= 0 or self.cash_usd < sat_budget_per * 0.3:
                continue

            # 일봉 OHLCV (추세/MA/RSI/ATR, 1시간 캐시)
            df_raw = self._get_cached_ohlcv(ticker, period="120d")
            if df_raw.empty:
                df_raw = None

            # 5분봉 OHLCV (장중 모멘텀/거래량 서지, 5분 캐시)
            df_5m = self._get_cached_ohlcv_5m(ticker, period="2d")
            # 5분봉으로 장중 거래량 서지 확인 → momentum_20d 보정
            _intraday_vol_surge = False
            if not df_5m.empty and 'volume' in df_5m.columns and len(df_5m) >= 12:
                _v5m = df_5m['volume'].dropna()
                _recent_vol = float(_v5m.iloc[-3:].mean())   # 최근 15분 평균
                _base_vol   = float(_v5m.iloc[-78:-3].mean()) if len(_v5m) > 78 else float(_v5m.mean())
                if _base_vol > 0 and _recent_vol >= _base_vol * 1.5:
                    _intraday_vol_surge = True

            # ── 통합 진입 점수 체크 ────────────────────────────────
            momentum_20d = info.get("momentum_20d", 0.0)
            # 5분봉 거래량 서지 시 모멘텀 점수 보정 (진입 우선도 상승)
            if _intraday_vol_surge:
                momentum_20d = max(momentum_20d, 4.0)
            if df_raw is not None and not df_raw.empty:
                entry_score, entry_reasons = calculate_entry_score(
                    df_raw, price, regime, momentum_20d=momentum_20d
                )
            else:
                entry_score, entry_reasons = 0, []

            # AI 시장판단 보너스 반영 (NQ/ES·VIX·섹터 종합 판단)
            _ai_bonus = getattr(self, '_ai_market_entry_bonus', 0)
            if _ai_bonus != 0:
                entry_score += _ai_bonus
                entry_reasons.append(f"AI시장판단 {_ai_bonus:+d}pt")

            entry_threshold = self.entry_thresholds.get(f'sat_{regime}', self.entry_thresholds.get(regime, get_entry_threshold(regime, 'satellite')))

            # ── 진입 점수 게이트 (RSI 필수 아님 — 10개 지표 합산으로 판단) ──────────
            # composite_signal 게이트 제거 → entry_score >= threshold 면 AI 심사로 진행
            budget_ratio = max(0.6, get_budget_ratio_from_score(entry_score, entry_threshold))

            if entry_score < entry_threshold:
                _timing_status = f"점수 대기 ({entry_score}/{entry_threshold}pt) ⏳"
                if pos is None:
                    self.satellite_positions[ticker] = USPosition(
                        ticker=ticker, name=info["name"], budget_usd=sat_budget_per,
                        status=_timing_status
                    )
                else:
                    with self.lock:
                        pos.status = _timing_status
                continue
            # ────────────────────────────────────────────────────────────────
            # ── BEAR 국면: 포지션 크기 50% 제한 (리스크 관리) ──
            if regime == "BEAR":
                budget_ratio = min(budget_ratio * 0.50, 0.50)

            # ── 실적 발표 D-7 이내 → 보유 중이면 30% 축소, 미보유면 진입 차단 (KR봇 동일) ─
            try:
                _cal = yf.Ticker(ticker).calendar
                if _cal is not None and not _cal.empty:
                    _earn_col = next((c for c in _cal.columns if 'Earnings' in str(c)), None)
                    if _earn_col:
                        import datetime as _dt
                        _earn_date = _cal[_earn_col].dropna()
                        if len(_earn_date) > 0:
                            _days_to_earn = (_earn_date.iloc[0].date() - _dt.date.today()).days
                            _earn_date_str = str(_earn_date.iloc[0].date())
                            if 0 <= _days_to_earn <= 7:
                                if pos and pos.shares > 1:
                                    # 이미 보유 중 + D-7 이내 → 30% 축소 (1회만)
                                    _earn_key = f"{ticker}_{_earn_date_str}"
                                    with self.lock:
                                        if not hasattr(self, '_earnings_notified_us'):
                                            self._earnings_notified_us = {}
                                        _earn_already = self._earnings_notified_us.get(_earn_key, False)
                                    if not _earn_already:
                                        _reduce = max(1.0, pos.shares * 0.30)
                                        _ep = self._price(ticker)
                                        self._sell(ticker, pos.name, _reduce, _ep)
                                        _pnl_e = _net_profit_usd(_ep, pos.avg_price_usd, _reduce)
                                        with self.lock:
                                            pos.shares = max(0.0, pos.shares - _reduce)
                                            pos.status = f"실적전 축소 📊 (D-{_days_to_earn})"
                                            self._earnings_notified_us[_earn_key] = True
                                        self._record_pnl(_pnl_e)
                                        self.add_log(f"📊 [{ticker}] 실적발표 D-{_days_to_earn} → 30% 축소 ({_earn_date_str}) | PnL ${_pnl_e:+.0f}")
                                        self._tg(f"📊 [US 실적 전 축소] {pos.name}\nD-{_days_to_earn} ({_earn_date_str}) → 30% 선매도 | ${_pnl_e:+,.0f}")
                                elif pos is None or pos.shares == 0:
                                    # 미보유 + D-7 이내 → 신규 진입 차단
                                    _msg = f"실적발표 D-{_days_to_earn} 진입 차단 ({_earn_date_str})"
                                    if pos:
                                        with self.lock: pos.status = f"⚠️ {_msg}"
                                    self.add_log(f"⚠️ [{ticker}] {_msg}")
                                    continue
            except Exception:
                pass

            # ── 52주 신고가 근접 체크 → AI 프롬프트에 전달용 플래그 ──
            _near_52w_high = False
            _52w_note = ""
            try:
                if df_raw is not None and not df_raw.empty and len(df_raw) >= 50:
                    _52w_high = float(df_raw['high'].rolling(252, min_periods=50).max().iloc[-1])
                    _52w_pct  = (price / _52w_high - 1) * 100
                    if _52w_pct >= -3.0:   # 52주 고가 3% 이내 = 돌파 시도
                        _near_52w_high = True
                        _52w_note = f"52주 신고가 근접 ({_52w_pct:+.1f}%) — 돌파 시 강세 신호"
                    elif _52w_pct <= -40.0:  # 52주 고가 대비 -40% 이하 = 추세 붕괴
                        _52w_note = f"52주 고가 대비 {_52w_pct:.0f}% — 추세 붕괴 주의"
            except Exception:
                pass

            # 3트랜치 분할: 1차=ratio, 2차=min(ratio,남은), 3차=나머지
            _sat_1st     = sat_budget_per * budget_ratio
            _sat_remain1 = max(0.0, sat_budget_per - _sat_1st)
            _sat_2nd     = min(sat_budget_per * budget_ratio, _sat_remain1)
            _sat_3rd     = max(0.0, sat_budget_per - _sat_1st - _sat_2nd)
            actual_budget = min(_sat_1st, self.cash_usd)

            # ── AI 심사 전: 대시보드에 즉시 표시 (심사 중 상태) ──────
            if ticker not in self.satellite_positions:
                with self.lock:
                    self.satellite_positions[ticker] = USPosition(
                        ticker        = ticker,
                        name          = info["name"],
                        shares        = 0.0,
                        avg_price_usd = price,
                        status        = "AI 심사 중 🤖",
                    )
            else:
                with self.lock:
                    self.satellite_positions[ticker].status = "AI 심사 중 🤖"

            # ── AI 매수 승인 심사 ────────────────────────────────────
            if self.claude:
                try:
                    # 종목 뉴스 헤드라인 + 재무지표 fetch
                    _news_str = self._fetch_us_news([ticker])
                    _fundamental = self._fetch_fundamental(ticker)
                    # 52주 신고가 + 재무지표 정보 ai_reason에 포함
                    _full_ai_reason = info.get("ai_reason", "")
                    if _52w_note:
                        _full_ai_reason = f"{_full_ai_reason} | {_52w_note}".strip(" |")
                    if _fundamental:
                        _full_ai_reason = f"{_full_ai_reason} | [{_fundamental}]".strip(" |")
                    approved, ai_reason = self.claude.ai_approve_us_trade(
                        signal         = 'BUY',
                        stock_name     = info["name"],
                        ticker         = ticker,
                        price_usd      = price,
                        sector         = info.get("sector", ""),
                        hot_sectors    = self.hot_sectors,
                        momentum_20d   = momentum_20d,
                        rsi            = info.get("rsi", 50.0),
                        ai_reason      = _full_ai_reason,
                        news_headlines = _news_str,
                    )
                    if not approved:
                        # 거절 즉시 제거 → 재스캔에서 더 좋은 종목으로 교체 (블랙리스트 불필요)
                        self.satellite_info = [s for s in self.satellite_info if s.get("ticker") != ticker]
                        if ticker in self.satellite_positions and self.satellite_positions[ticker].shares == 0:
                            del self.satellite_positions[ticker]
                        self.add_log(f"🤖 AI 거절 → 제거 후 교체 탐색: {info['name']}({ticker}) — {ai_reason[:80]}")
                        if self.claude:
                            self.claude.record_trade_event(
                                f"위성 매수 거절 🛑 {info['name']}({ticker}) @ ${price:.2f} | 거절이유: {ai_reason[:80]}"
                            )
                        self._tg(
                            f"🛑 <b>[US 위성 매수 거절]</b>  {self.alert_icon}\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"📌 <b>{info['name']}</b>  {ticker}\n"
                            f"🤖 {ai_reason[:100]}\n"
                            f"➡️ 당일 블랙리스트 등록 후 대체 종목 탐색\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"⏰ {_now_et().strftime('%H:%M ET')}"
                        )
                        # 즉시 대체 종목 탐색 (KR봇 동일)
                        self._run_threaded(self._rescreen_satellites)
                        continue
                    self.add_log(f"🤖 AI 매수 승인: {info['name']}({ticker}) | 점수 {entry_score}pt")
                except Exception as e:
                    logger.warning(f"[US봇] AI 승인 심사 오류 ({ticker}): {e} — 알고리즘 신호 허용")

            # ── 분봉 추세 확인 (NEUTRAL/BEAR 국면: 5분봉 하락 추세면 대기) ──
            if not self._check_minute_trend_up_us(ticker):
                with self.lock:
                    pos_obj = self.satellite_positions.get(ticker)
                    if pos_obj:
                        pos_obj.status = "분봉 하락 📉"
                        pos_obj.status_msg = "5분봉 하락 추세 — 다음 턴 재시도"
                self.add_log(f"⏸ 분봉 하락 추세 보류: {info['name']}({ticker}) — 다음 턴 재시도")
                continue

            qty = self._buy(ticker, info["name"], actual_budget, price)
            if qty > 0:
                score_str = " | ".join(entry_reasons[:3])
                with self.lock:
                    self.satellite_positions[ticker] = USPosition(
                        ticker                  = ticker,
                        name                    = info["name"],
                        shares                  = float(qty),
                        floor_shares            = float(qty) * 0.5,
                        avg_price_usd           = price,
                        budget_usd              = sat_budget_per,
                        status                  = f"보유 중 🛰️ ({entry_score}pt)",
                        last_order_time         = time.time(),
                        max_price_usd           = price,
                        second_buy_price        = price * 0.98,
                        second_buy_cash         = _sat_2nd,
                        second_buy_done         = False,
                        third_buy_price         = price * 0.96,
                        third_buy_cash          = _sat_3rd,
                        third_buy_done          = False,
                        initial_shares_for_exit = 0.0,
                    )
                self.add_log(f"🛰️ 위성 1차매수 {info['name']}({ticker}) {qty}주 @ ${price:.2f} | {entry_score}pt [{score_str}]")
                self._tg(
                    f"🛰️ [US 위성 1차매수] {info['name']} ({ticker})\n"
                    f"@ ${price:.2f}  점수 {entry_score}pt  섹터: {info.get('sector','')}\n"
                    f"2차 예약: ${price*0.98:.2f} (-2%)  3차: ${price*0.96:.2f} (-4%)"
                )
                if self.claude:
                    self.claude.record_trade_event(
                        f"위성 매수 ✅ {info['name']}({ticker}) {qty}주 @ ${price:.2f} | "
                        f"점수 {entry_score}pt | 근거: {score_str}"
                    )

        # ── 위성 2차/3차 분할 매수 체크 ─────────────────────────────
        for ticker, pos in list(self.satellite_positions.items()):
            if pos.shares <= 0:
                continue
            price = self._price(ticker)
            if not price:
                continue
            is_cd_sat = time.time() - pos.last_order_time > self.ORDER_COOLDOWN

            # 2차 분할 매수: 진입가 -2% 눌림목
            if (is_cd_sat
                    and not getattr(pos, 'second_buy_done', True)
                    and getattr(pos, 'second_buy_price', 0) > 0
                    and price <= pos.second_buy_price
                    and getattr(pos, 'second_buy_cash', 0) >= price):
                _s2_ok = self.claude.ai_approve_split_buy(
                    ticker, pos.name, price, pos.avg_price_usd, 2,
                    self.market_regime, self._fetch_us_news([ticker])
                ) if self.claude else True
                if _s2_ok:
                    sq2 = self._buy(ticker, pos.name, pos.second_buy_cash, price)
                    if sq2 > 0:
                        with self.lock:
                            new_sh2 = pos.shares + sq2
                            pos.avg_price_usd  = (pos.avg_price_usd * pos.shares + price * sq2) / new_sh2
                            pos.shares         = new_sh2
                            pos.second_buy_done= True
                            pos.second_buy_cash= 0.0
                            pos.last_order_time= time.time()
                            pos.status         = "2차 매수 ✅"
                        self.add_log(f"🛰️ 위성 2차매수 {pos.name}({ticker}) {sq2}주 @ ${price:.2f} | -2% 눌림목")
                        self._tg(f"🛰️ [US 위성 2차매수] {pos.name}\n@ ${price:.2f}  눌림목 -2% 포착")
                else:
                    self.add_log(f"🛑 2차 분할매수 AI 중단: {pos.name}({ticker}) — 시장 악화")

            # 3차 분할 매수: 진입가 -4% 눌림목
            elif (is_cd_sat
                    and getattr(pos, 'second_buy_done', False)
                    and not getattr(pos, 'third_buy_done', True)
                    and getattr(pos, 'third_buy_price', 0) > 0
                    and price <= pos.third_buy_price
                    and getattr(pos, 'third_buy_cash', 0) >= price):
                _s3_ok = self.claude.ai_approve_split_buy(
                    ticker, pos.name, price, pos.avg_price_usd, 3,
                    self.market_regime, self._fetch_us_news([ticker])
                ) if self.claude else True
                if not _s3_ok:
                    self.add_log(f"🛑 3차 분할매수 AI 중단: {pos.name}({ticker}) — 시장 악화")
                    continue  # elif 블록 넘어가기 위해 continue 대신 아래 sq3 실행 방지용
                sq3 = self._buy(ticker, pos.name, pos.third_buy_cash, price)
                if sq3 > 0:
                    with self.lock:
                        new_sh3 = pos.shares + sq3
                        pos.avg_price_usd  = (pos.avg_price_usd * pos.shares + price * sq3) / new_sh3
                        pos.shares         = new_sh3
                        pos.third_buy_done = True
                        pos.third_buy_cash = 0.0
                        pos.last_order_time= time.time()
                        pos.status         = "3차 매수 ✅"
                    self.add_log(f"🛰️ 위성 3차매수 {pos.name}({ticker}) {sq3}주 @ ${price:.2f} | -4% 눌림목")
                    self._tg(f"🛰️ [US 위성 3차매수] {pos.name}\n@ ${price:.2f}  눌림목 -4% 포착")

        # ── 보유 중 청산 조건 체크 (ATR 기반 — KR 동일) ─────────────
        for ticker, pos in list(self.satellite_positions.items()):
            if pos.shares <= 0:
                continue
            price = self._price(ticker)
            if price <= 0:
                continue

            # OHLCV 조회 (캐시 1시간)
            df_raw = self._get_cached_ohlcv(ticker, period="60d")
            if df_raw.empty:
                df_raw = None

            avg = pos.avg_price_usd
            if avg <= 0:
                continue

            # ATR 계산
            s_atr = avg * 0.02
            if df_raw is not None and not df_raw.empty and all(
                    c in df_raw.columns for c in ['high', 'low', 'close']):
                try:
                    tr    = pd.concat([
                        df_raw['high'] - df_raw['low'],
                        (df_raw['high'] - df_raw['close'].shift(1)).abs(),
                        (df_raw['low']  - df_raw['close'].shift(1)).abs(),
                    ], axis=1).max(axis=1)
                    s_atr = float(tr.rolling(14, min_periods=1).mean().iloc[-1])
                except Exception:
                    pass

            trail_mult  = self.SAT_TRAIL_MULT.get(regime, 1.5)
            trail_trig  = self.SAT_TRAIL_TRIG.get(regime, 1.0)
            hard_mult   = self.SAT_HARD_MULT.get(regime, 2.5)
            pnl_pct     = (price / avg - 1) * 100
            is_cd       = time.time() - pos.last_order_time > self.ORDER_COOLDOWN

            # 고점 갱신
            if price > pos.max_price_usd:
                with self.lock:
                    pos.max_price_usd = price

            p_max = pos.max_price_usd

            # ① BEAR 국면 하드 익절: +5% 도달 시 즉시 전량 청산
            if regime == "BEAR" and is_cd and price >= avg * 1.05:
                self._close_sat(ticker, pos, price,
                                f"BEAR +5% 하드 익절 (평단${avg:.2f}→${price:.2f})")
                continue

            # ② ATR 트레일링 익절 — KR과 동일
            if (p_max >= avg + (trail_trig * s_atr)
                    and price <= p_max - (trail_mult * s_atr)):
                self._close_sat(ticker, pos, price,
                                f"ATR 트레일링 익절 (고점${p_max:.2f}→${price:.2f})")
                continue

            # ② ATR 하드 손절 (전량) — 손절 전 뉴스 체크 (호재면 1회 유예)
            if price <= avg - (hard_mult * s_atr):
                _stop_news_s = self._fetch_us_news([ticker])
                _stop_skip_s = False
                if _stop_news_s and not getattr(pos, 'stop_news_checked', False):
                    _positive_kw = ['beat', 'upgrade', 'buy', 'bullish', 'record', 'contract', 'deal', 'win']
                    if any(kw in _stop_news_s.lower() for kw in _positive_kw):
                        pos.stop_news_checked = True
                        _stop_skip_s = True
                        self.add_log(f"⚠️ [위성] {pos.name} ATR 손절 but 호재 뉴스 → 1회 유예\n{_stop_news_s[:120]}")
                if not _stop_skip_s:
                    pos.stop_news_checked = False
                    self._close_sat(ticker, pos, price,
                                    f"ATR 손절 (평단${avg:.2f}  ATR×{hard_mult:.1f})")
                continue

            # ③ 테마주 과열 청산 & RSI 점진적 익절 (KR봇 동일 전략)
            # 60일선 이격 과열 + RSI 베어다이버전스 + 거래량 소멸 → 급락 전 자동 청산
            if pos.shares > 0 and avg > 0 and is_cd:
                if df_raw is not None and not df_raw.empty:
                    try:
                        _sat_info_us   = next((s for s in self.satellite_info if s["ticker"] == ticker), None)
                        _sector_us     = _sat_info_us.get("sector", "") if _sat_info_us else ""
                        _sector_bonus_us = 10 if (_sector_us and _sector_us in (self.hot_sectors or [])) else 0

                        _oe_sig, _oe_sc, _oe_reason  = check_theme_overextension_exit(df_raw, price, _sector_bonus_us)
                        _rs_sig, _rs_val, _rs_reason = check_rsi_progressive_exit(df_raw, price, avg)

                        _sig_rank = {'HOLD': 0, 'PARTIAL_EXIT_30': 1, 'PARTIAL_EXIT_60': 2, 'FULL_EXIT': 3}
                        _fe_sig, _fe_reason = ((_oe_sig, _oe_reason) if _sig_rank.get(_oe_sig, 0) >= _sig_rank.get(_rs_sig, 0)
                                               else (_rs_sig, _rs_reason))

                        if _fe_sig == 'FULL_EXIT':
                            self._close_sat(ticker, pos, price, f"과열 전량청산 [{_fe_reason[:50]}]")
                            continue
                        elif _fe_sig == 'PARTIAL_EXIT_60' and pos.shares > 1:
                            _q60 = max(1.0, pos.shares * 0.60)
                            self._sell(ticker, pos.name, _q60, price)
                            _pnl60 = _net_profit_usd(price, avg, _q60)
                            with self.lock:
                                pos.shares -= _q60
                                pos.status  = f"과열 선익절 60% ✂️"
                            self._record_pnl(_pnl60)
                            self.add_log(f"✂️ [US 과열청산 60%] {pos.name} | {_fe_reason[:50]} | ${_pnl60:+.0f}")
                        elif _fe_sig == 'PARTIAL_EXIT_30' and pos.shares > 1:
                            _oe_cnt = getattr(pos, 'overext_sell_count', 0)
                            if _oe_cnt < 3:
                                # 1차 트리거 시 원금 주수 고정 (복리 방지 — KR봇 동일)
                                if _oe_cnt == 0 and not getattr(pos, 'initial_shares_for_exit', 0):
                                    with self.lock: pos.initial_shares_for_exit = pos.shares
                                _init_sh_oe = getattr(pos, 'initial_shares_for_exit', 0) or pos.shares
                                _q30 = max(1.0, min(_init_sh_oe * 0.30, pos.shares))
                                self._sell(ticker, pos.name, _q30, price)
                                _pnl30 = _net_profit_usd(price, avg, _q30)
                                with self.lock:
                                    pos.shares = max(0.0, pos.shares - _q30)
                                    pos.overext_sell_count = _oe_cnt + 1
                                    pos.status = f"과열 선익절 {_oe_cnt+1}차 30% ✂️"
                                self._record_pnl(_pnl30)
                                self.add_log(f"✂️ [US 과열청산 30% {_oe_cnt+1}차] {pos.name} | {_fe_reason[:50]} | ${_pnl30:+.0f}")
                    except Exception as _oe_err:
                        logger.debug(f"[US봇] 과열청산 체크 오류 ({ticker}): {_oe_err}")

            # ④ 1차 부분 익절 (+10%(일반) / +15%(BULL)) — AI 판단
            _sat_partial1 = 15.0 if regime == "BULL" else self.PARTIAL1_PCT
            _sat_partial2 = 30.0 if regime == "BULL" else self.PARTIAL2_PCT
            if not pos.partial_sold and pnl_pct >= _sat_partial1 and pos.shares > 1:
                decision = getattr(pos, 'ai_exit_decision', None)
                if decision is None:
                    if self.claude:
                        self._trigger_ai_partial_exit(pos, ticker, pos.name, price, avg, pnl_pct, regime)
                        with self.lock: pos.status = f"AI 익절 검토 중 ({pnl_pct:+.1f}%) 🤖"
                    else:
                        with self.lock: pos.ai_exit_decision = "SELL_PARTIAL"
                    continue
                if decision == "HOLD":
                    with self.lock:
                        pos.ai_exit_hold_until = time.time() + 300
                        pos.ai_exit_decision   = None
                        pos.status = f"AI 홀드 ({pnl_pct:+.1f}%) ⏳"
                    continue
                # SELL_PARTIAL 또는 SELL_ALL → 1차 익절 실행 (floor_shares 보호)
                # 원금 주수 고정 (복리 방지 — KR봇 동일)
                if not getattr(pos, 'initial_shares_for_exit', 0):
                    with self.lock: pos.initial_shares_for_exit = pos.shares
                _init_sh1 = getattr(pos, 'initial_shares_for_exit', 0) or pos.shares
                sellable_s1 = max(0.0, pos.shares - pos.floor_shares)
                if sellable_s1 > 0:
                    q   = max(1.0, min(_init_sh1 * self.PARTIAL1_QTY, sellable_s1))
                    self._sell(ticker, pos.name, q, price)
                    pnl = _net_profit_usd(price, avg, q)
                    with self.lock:
                        pos.shares           -= q
                        pos.ai_exit_decision  = None
                        pos.status            = f"1차익절({pnl_pct:+.1f}%) ✂️"
                    self._record_pnl(pnl)
                    self.add_log(f"✂️  1차익절 {pos.name} ({q:.0f}주 / 원금 {_init_sh1:.0f}주 기준 50%) | PnL ${pnl:+.0f}")
                    self._tg(f"✂️ [US 위성 1차익절] {pos.name}\n@ ${price:.2f}  +{pnl_pct:.1f}% | 원금 {_init_sh1:.0f}주 기준 50%")
                with self.lock:
                    pos.partial_sold = True
                    if sellable_s1 <= 0:
                        pos.ai_exit_decision = None
                        pos.status = f"floor 보호 ({pnl_pct:+.1f}%) 🛡️"
                continue

            # ④-2. 2차 부분 익절 (+20%(일반) / +30%(BULL)) — 원금 기준 50% (KR봇 동일)
            if (pos.partial_sold and not pos.partial_sold_2
                    and pnl_pct >= _sat_partial2 and pos.shares > 1 and is_cd):
                decision2 = getattr(pos, 'ai_exit_decision', None)
                if decision2 is None:
                    if self.claude:
                        self._trigger_ai_partial_exit(pos, ticker, pos.name, price, avg, pnl_pct, regime)
                        with self.lock: pos.status = f"AI 2차익절 검토 ({pnl_pct:+.1f}%) 🤖"
                    else:
                        with self.lock: pos.ai_exit_decision = "SELL_PARTIAL"
                    continue
                if decision2 == "HOLD":
                    with self.lock:
                        pos.ai_exit_decision = None
                        pos.status = f"AI 홀드 ({pnl_pct:+.1f}%) ⏳"
                    continue
                _init_sh2 = getattr(pos, 'initial_shares_for_exit', 0) or pos.shares
                q2 = max(1.0, min(_init_sh2 * 0.50, pos.shares))
                self._sell(ticker, pos.name, q2, price)
                pnl2 = _net_profit_usd(price, avg, q2)
                with self.lock:
                    pos.shares           = max(0.0, pos.shares - q2)
                    pos.partial_sold_2   = True
                    pos.ai_exit_decision = None
                    pos.status           = f"2차익절({pnl_pct:+.1f}%) ✅"
                self._record_pnl(pnl2)
                self._reinvest_to_cores(pnl2, f"위성 2차익절 {ticker}")
                _thr2 = "30%(BULL)" if regime == "BULL" else "20%"
                self.add_log(f"✅ 2차익절 {pos.name} +{_thr2} ({q2:.0f}주 / 원금 {_init_sh2:.0f}주 기준 50%) | PnL ${pnl2:+.0f}")
                self._tg(f"✅ [US 위성 2차익절] {pos.name}\n@ ${price:.2f}  +{_thr2} | 잔여 {pos.shares:.0f}주 ATR 트레일링 대기")
                continue

            # ⑤ BULL 불타기 (위성 피라미딩) — +3% + MA5 정배열 ────────
            if (regime == "BULL" and not pos.partial_sold
                    and not getattr(pos, 'bull_pyramid_done', False)
                    and pnl_pct >= 3.0 and is_cd and self.cash_usd > price):
                try:
                    _py_ok = False
                    if df_raw is not None and not df_raw.empty and len(df_raw['close'].dropna()) >= 22:
                        _cl = df_raw['close'].dropna()
                        _py_ok = float(_cl.rolling(5).mean().iloc[-1]) > float(_cl.rolling(20).mean().iloc[-1])
                    if _py_ok:
                        _py_budget = min(sat_budget_per * 0.30, self.cash_usd * 0.15)
                        _py_qty = self._buy(ticker, pos.name, _py_budget, price)
                        if _py_qty > 0:
                            with self.lock:
                                new_sh = pos.shares + _py_qty
                                pos.avg_price_usd  = (pos.avg_price_usd * pos.shares + price * _py_qty) / new_sh if new_sh > 0 else price
                                pos.shares         = new_sh
                                # floor_shares 갱신 (항상 증가 방향 — 주식 수 축적 원칙)
                                pos.floor_shares   = max(pos.floor_shares, pos.shares * 0.5)
                                pos.bull_pyramid_done = True
                                pos.max_price_usd  = max(pos.max_price_usd, price)
                                pos.last_order_time= time.time()
                                pos.status         = f"불타기 🔥 (+{pnl_pct:.1f}%)"
                            self.add_log(f"🔥 [BULL 위성 불타기] {pos.name}({ticker}) +{pnl_pct:.1f}% | {_py_qty}주 @ ${price:.2f} 추가")
                            self._tg(f"🔥 [US BULL 위성 불타기] {pos.name}\n+{pnl_pct:.1f}% 추세 추종 | {_py_qty}주 @ ${price:.2f}")
                except Exception as _pye:
                    logger.debug(f"[US봇] BULL 불타기(위성) 오류: {_pye}")

            # ⑥ 스크리너 제외 + 수익권 → 청산
            # 성장세 양호(+3% 이상)면 스크리너 제외여도 유지 — 자연 익절/손절 대기
            in_info = {i["ticker"] for i in self.satellite_info}
            if ticker not in in_info and pnl_pct > 0 and pnl_pct < self._GROWTH_KEEP_PCT:
                self._close_sat(ticker, pos, price, f"스크리너 제외 (수익 {pnl_pct:.1f}%)")
                continue

            with self.lock:
                pos.status = f"보유 중 🛰️ ({pnl_pct:+.1f}%)"

    def _check_swing_rebuy_queue(self):
        """스윙 재진입 큐 모니터링 — RSI≤35 or 추가 -5% 도달 시 AI 승인 후 재매수."""
        _QUEUE_TTL = 3 * 86400   # 3 거래일 유효 (장 마감에도 만료 안 됨)
        now_et = _now_et()
        expired = []

        for ticker, rebuy in list(self._swing_rebuy_queue.items()):
            # 3일 초과 시 만료
            if time.time() - rebuy.get('ts', 0) > _QUEUE_TTL:
                self.add_log(f"⏰ [스윙큐 만료] {rebuy.get('name', ticker)} — 3일 미실현")
                expired.append(ticker)
                continue

            price = self._price(ticker)
            if price <= 0:
                continue

            # 트리거 체크: RSI≤35 or 추가 -5%
            triggered = False
            trigger_reason = ""
            df_5m = self._get_cached_ohlcv_5m(ticker)
            if not df_5m.empty and 'close' in df_5m.columns and len(df_5m) >= 15:
                _c = df_5m['close'].dropna()
                _d = _c.diff()
                _g = _d.clip(lower=0).rolling(14).mean()
                _l = (-_d.clip(upper=0)).rolling(14).mean()
                _rsi = float((100 - 100 / (1 + _g / (_l + 1e-10))).iloc[-1])
                if _rsi <= rebuy['target_rsi']:
                    triggered = True
                    trigger_reason = f"RSI {_rsi:.0f}≤35"

            if not triggered and price <= rebuy['target_price']:
                triggered = True
                trigger_reason = f"추가 -5% (${price:.2f}≤${rebuy['target_price']:.2f})"

            if not triggered:
                continue

            # AI 재진입 승인
            name   = rebuy.get('name', ticker)
            budget = rebuy.get('budget', self.cash_usd * 0.10)
            is_core = rebuy.get('is_core', False)

            approved, ai_reason = True, "AI 미설정"
            if self.claude:
                news = self._fetch_us_news([ticker])
                approved, ai_reason = self.claude.ai_approve_us_trade(
                    signal='BUY', stock_name=name, ticker=ticker,
                    price_usd=price, sector='',
                    hot_sectors=self.hot_sectors or [],
                    ai_reason=f"스윙 재진입 | 트리거: {trigger_reason} | 원매도가: ${rebuy['sell_price']:.2f}",
                    news_headlines=news,
                )

            if approved:
                qty = self._buy(ticker, name, min(budget, self.cash_usd * 0.95), price)
                if qty > 0:
                    label = "코어 스윙 재진입" if is_core else "위성 스윙 재진입"
                    self.add_log(f"🎯 [{label}] {name}({ticker}) {qty}주 @ ${price:.2f} | {trigger_reason} | AI: {ai_reason[:50]}")
                    self._tg(f"🎯 [US {label}] {name}\n{trigger_reason} | {qty}주 @ ${price:.2f}")
                    if self.claude:
                        self.claude.record_trade_event(f"US {label}: {name}({ticker}) {qty}주 @ ${price:.2f} | {trigger_reason}")
                    # 스윙 누적 횟수 초기화 (재진입 성공 시)
                    self._swing_accumulate_cnt.pop(ticker, None)
                expired.append(ticker)
            else:
                self.add_log(f"🛑 스윙 재진입 AI 거절: {name}({ticker}) — {ai_reason[:60]}")
                expired.append(ticker)

        for t in expired:
            self._swing_rebuy_queue.pop(t, None)

    def _close_sat(self, ticker: str, pos: USPosition, price: float, reason: str):
        """위성 전량 청산 — ATR 손절·트레일링 발동 시 AI 스윙 판단 우선.
        과열/스크리너제외 등 비ATR 청산은 AI 판단 없이 즉시 실행."""
        # ── AI 스윙 판단 (ATR 손절/트레일링 발동 시만) ────────────────
        _is_atr_exit = any(kw in reason for kw in ["ATR", "손절", "트레일링"])
        if _is_atr_exit and self.claude and pos.shares > 0 and pos.avg_price_usd > 0:
            with self.lock: pos.status = "AI 스윙 판단 중 🤖"
            swing = self._ai_swing_check(pos, ticker, price, reason)

            if swing == 'SELL_REBUY':
                # 매도 후 재진입 큐 등록
                sell_price = price
                self._swing_rebuy_queue[ticker] = {
                    'sell_price':   sell_price,
                    'target_price': sell_price * 0.95,   # 추가 -5%
                    'target_rsi':   35,
                    'name':         pos.name,
                    'ts':           time.time(),
                    'budget':       pos.budget_usd,
                }
                self.add_log(f"🔄 [스윙] {pos.name}({ticker}) SELL_REBUY — 매도 후 RSI≤35 or -5% 재매수 큐")
                self._tg(f"🔄 [US 스윙매매] {pos.name}\n매도 후 재진입 대기\n재매수 조건: RSI≤35 or ${sell_price*0.95:.2f}(-5%)")
                # 매도는 아래 공통 로직으로 실행됨

            elif swing == 'ACCUMULATE':
                # 매도 없이 추가매수
                acc_cnt = self._swing_accumulate_cnt.get(ticker, 0)
                _acc_budget = min(pos.budget_usd * 0.30, self.cash_usd * 0.15)
                acc_qty = self._buy(ticker, pos.name, _acc_budget, price)
                if acc_qty > 0:
                    with self.lock:
                        new_sh = pos.shares + acc_qty
                        pos.avg_price_usd = (pos.avg_price_usd * pos.shares + price * acc_qty) / new_sh
                        pos.shares        = new_sh
                        pos.last_order_time = time.time()
                        pos.status        = f"스윙 누적 {acc_cnt+1}차 📥"
                        self._swing_accumulate_cnt[ticker] = acc_cnt + 1
                    self.add_log(f"📥 [스윙] {pos.name}({ticker}) ACCUMULATE {acc_cnt+1}차 | {acc_qty}주 @ ${price:.2f} | 평단 ${pos.avg_price_usd:.2f}")
                    self._tg(f"📥 [US 스윙 누적] {pos.name}\n{acc_cnt+1}차 추가매수 | 평단 ${pos.avg_price_usd:.2f}")
                    if self.claude:
                        self.claude.record_trade_event(f"US 스윙 누적 {acc_cnt+1}차: {pos.name}({ticker}) {acc_qty}주 @ ${price:.2f}")
                return  # 청산 없이 리턴

            else:  # EXIT
                self.add_log(f"📤 [스윙] {pos.name}({ticker}) EXIT — AI: 추세 붕괴 판단")

        # ── 공통 청산 로직 ────────────────────────────────────────────
        shares   = pos.shares
        proceeds = self._sell(ticker, pos.name, shares, price)
        pnl      = _net_profit_usd(price, pos.avg_price_usd, shares)
        with self.lock:
            pos.shares                  = 0.0
            pos.floor_shares            = 0.0
            pos.second_buy_price        = 0.0
            pos.second_buy_cash         = 0.0
            pos.second_buy_done         = False
            pos.third_buy_price         = 0.0
            pos.third_buy_cash          = 0.0
            pos.third_buy_done          = False
            pos.bull_pyramid_done       = False
            pos.partial_sold            = False
            pos.partial_sold_2          = False
            pos.max_price_usd           = 0.0
            pos.stop_news_checked       = False
            pos.overext_sell_count      = 0
            pos.initial_shares_for_exit = 0.0
            pos.status                  = f"청산: {reason}"
        self._record_pnl(pnl)
        self._reinvest_to_cores(pnl, reason[:30])
        icon = "🔴" if pnl < 0 else "🟢"
        self.add_log(f"{icon} 청산 {pos.name}({ticker}) | {reason} | PnL ${pnl:+.0f}")
        self._tg(
            f"{icon} [US 위성 청산] {pos.name}\n"
            f"사유: {reason}\n손익: ${pnl:+,.0f}"
        )
        if self.claude:
            self.claude.record_trade_event(
                f"위성 청산 {icon} {pos.name}({ticker}) | {reason} | 손익 ${pnl:+.0f}"
            )

    # ─────────────────────────────────────────────────────────────────
    # 메인 루프
    # ─────────────────────────────────────────────────────────────────
    # 위성 즉시 재스크리닝 (KR봇 _rescreen_satellites 동일 패턴)
    # ─────────────────────────────────────────────────────────────────

    _last_rescreen_actual_ts: float = 0.0   # 연속 재스캔 방지 쿨다운

    def _rescreen_satellites(self):
        """위성 빈 슬롯 발생 / 주기적 교체 탐색 (KR봇 동일 패턴).
        last_screen_date를 초기화해 _screen_satellites()를 강제 재실행.
        AI 거절 연속 호출 방지: 5분 쿨다운."""
        if not _is_us_market_open():
            return
        # 연속 재스캔 방지 — 1분 쿨다운 (거절 즉시 교체 허용)
        now = time.time()
        if now - self._last_rescreen_actual_ts < 60:
            return
        self._last_rescreen_actual_ts = now
        self.add_log("🦅 [US] 위성 실시간 교체 탐색 중...")
        # 성장세 양호(+3%) 종목 유지 여부 사전 체크
        strong_keep: set = set()
        for t, p in list(self.satellite_positions.items()):
            if p.shares > 0 and p.avg_price_usd > 0:
                price = self._price(t)
                if price > 0 and (price / p.avg_price_usd - 1) * 100 >= self._GROWTH_KEEP_PCT:
                    strong_keep.add(t)
                    self.add_log(f"🌱 {p.name}({t}) 성장세 양호 — 교체 없이 유지")
        # 전 슬롯이 성장세 양호하면 재스크리닝 불필요
        if len(strong_keep) >= self.num_satellites:
            self.add_log(f"✅ 위성 {len(strong_keep)}개 성장세 양호 — 재스크리닝 스킵")
            return
        # last_screen_date 초기화 → _screen_satellites() 강제 재실행
        self.last_screen_date = None
        self._screen_satellites()

    # ─────────────────────────────────────────────────────────────────

    def _run_loop(self, total_cash: float):
        self.add_log("🚀 US 실전 봇 루프 시작")
        if not self.kis_overseas:
            self.add_log("⚠️ KIS API 미설정 — 계좌 설정에서 API 키를 입력하세요")

        # 초기 자금: KIS 잔고 우선 동기화
        if self.kis_overseas:
            self._sync_balance_from_kis()

        # ── 초기 종목 스크리닝 (KR봇 initialize_portfolio 동일 패턴) ──
        # satellite_info / core_info 가 비어 있으면 즉시 스크리닝 후 텔레그램 알림
        if not self.satellite_info or not self.core_info:
            self.add_log("🔍 US 초기 종목 스크리닝 시작...")
            self._screen_cores()
            self._screen_satellites()
            if self.core_info:
                _c_lines = "\n".join([
                    f"• <b>{c.get('name', c['ticker'])}</b>  <code>{c['ticker']}</code>"
                    for c in self.core_info
                ])
                self._tg(
                    f"💎 <b>US 코어 종목 선정 완료</b>  {self.alert_icon}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"{_c_lines}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"⏰ {_now_et().strftime('%H:%M ET')}"
                )
            if self.satellite_info:
                _s_lines = "\n".join([
                    f"• <b>{c.get('name', c['ticker'])}</b>  <code>{c['ticker']}</code>"
                    f"  [{c.get('sector', '')}]"
                    for c in self.satellite_info
                ])
                self._tg(
                    f"🔍 <b>US 위성 종목 선정 완료</b>  {self.alert_icon}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"{_s_lines}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"⏰ {_now_et().strftime('%H:%M ET')}"
                )

        _save_interval     = 300
        _bal_interval      = 30    # 30초마다 (KR봇 동일)
        _regime_interval   = 3600   # 1시간마다 시장 국면 갱신
        _rescreen_interval = 3600   # 1시간마다 위성 재스크리닝 (KR봇 동일)
        _REPORT_SLOT       = "16:10"
        _last_save_ts      = 0.0
        _last_bal_ts       = 0.0
        _last_regime_ts    = 0.0
        _last_rescreen_ts  = 0.0

        while self.is_running:
            try:
                now          = _now_et()
                cur_time_str = now.strftime("%H:%M")
                today_str    = now.strftime("%Y-%m-%d")

                # ── 장 상태 판단 ─────────────────────────────────────
                _mkt_open   = _is_us_market_open()
                _sell_ok    = _is_us_sell_hours()   # 프리/애프터 포함
                api_hint    = "" if self.kis_overseas else " (⚠️ KIS 미연결)"

                if not _sell_ok:
                    # 완전 장외 / 주말 — 매매 없음, 가격·잔고·스크리닝은 유지
                    self._refresh_prices()
                    if time.time() - _last_bal_ts >= 60:
                        self._sync_balance_from_kis()
                        _last_bal_ts = time.time()
                    # 주말에도 종목 스크리닝 — 순차 실행 (rebuild 경합 방지)
                    try:
                        self._screen_cores()
                        self._screen_satellites()
                    except Exception as _scr_err:
                        logger.error(f"[US봇] 장외 스크리닝 오류: {_scr_err}", exc_info=True)
                    # 주말 10:00 ET — 사전 분석
                    if now.weekday() >= 5 and cur_time_str == "14:00":
                        threading.Thread(target=self._weekend_satellite_scan_us, daemon=True).start()
                    if time.time() - _last_save_ts >= _save_interval:
                        self._save_state()
                        _last_save_ts = time.time()
                    time.sleep(60)
                    continue

                if not _mkt_open:
                    h, m = now.hour, now.minute
                    _is_premarket   = h < 9 or (h == 9 and m < 30)
                    _is_aftermarket = h >= 16
                    session = "프리마켓" if _is_premarket else "애프터마켓"
                    # 애프터마켓(16:00~20:00): 매수 허용 / 프리마켓: 매도(손절)만
                    _buy_allowed_ext = _is_aftermarket
                    self.add_log(
                        f"{'🌆' if _is_aftermarket else '🌙'} {session} ({now.strftime('%a %H:%M ET')})"
                        f" — {'매수+매도' if _buy_allowed_ext else '손절 감시 중'}{api_hint}"
                    )
                    # 장 시작 전 사전 스캔
                    if _is_premarket:
                        self._screen_satellites()
                        self._screen_cores()
                    # 가격 갱신 후 매매 실행
                    self._refresh_prices()
                    if self.kis_overseas:
                        self._manage_cores(buy_allowed=_buy_allowed_ext)
                        self._manage_satellites(buy_allowed=_buy_allowed_ext)
                    time.sleep(60)
                    continue

                # ── 월요일 9:30 ET — 주말 교체 계획 실행 ────────────────
                if now.weekday() == 0 and cur_time_str == "09:30" and self._monday_swap_plan:
                    threading.Thread(target=self._execute_monday_swap_us, daemon=True).start()

                # ── 가격 갱신 ─────────────────────────────────────────
                self._refresh_prices()

                # ── KIS 잔고 동기화 (5분마다) ─────────────────────────
                if time.time() - _last_bal_ts >= _bal_interval:
                    self._sync_balance_from_kis()
                    _last_bal_ts = time.time()

                # ── 시장 국면 갱신 (1시간마다) ───────────────────────
                if time.time() - _last_regime_ts >= _regime_interval:
                    self._run_threaded(self._update_market_regime)
                    _last_regime_ts = time.time()

                # ── 일일 리포트 (16:10 ET) ────────────────────────────
                if cur_time_str == _REPORT_SLOT:
                    already = (
                        isinstance(self.daily_report, dict)
                        and self.daily_report.get("date") == today_str
                        and self.daily_report.get(_REPORT_SLOT) is not None
                    )
                    if not already and self.claude:
                        self._run_threaded(lambda: self.generate_daily_report(_REPORT_SLOT))

                # ── 종목 스크리닝 (코어: 주 1회 / 위성: 하루 1회) ────
                self._screen_cores()
                self._screen_satellites()

                # ── 위성 재스크리닝 (1시간마다 — 빈 슬롯 있을 때만) ──────
                if time.time() - _last_rescreen_ts >= _rescreen_interval:
                    if _is_us_market_open():
                        with self.lock:
                            _has_empty = any(
                                p.shares == 0
                                for p in self.satellite_positions.values()
                            )
                        if _has_empty:
                            self._run_threaded(self._rescreen_satellites)
                    _last_rescreen_ts = time.time()

                # ── 스윙 재진입 큐 체크 ──────────────────────────────
                if self.kis_overseas and _mkt_open:
                    self._check_swing_rebuy_queue()

                # ── 코어·위성·방어자산 매매 (KIS 연결 시에만) ──────────
                if self.kis_overseas:
                    self._handle_defensive_assets(self.market_regime)
                    self._manage_cores()
                    self._manage_satellites()
                else:
                    self.add_log("🔍 스캔 완료 — KIS API 미연결로 매매 건너뜀")

                # ── 상태 저장 (5분마다) ───────────────────────────────
                if time.time() - _last_save_ts >= _save_interval:
                    self._save_state()
                    _last_save_ts = time.time()

                time.sleep(60)   # 60초 루프 (KR봇 동일)

            except Exception as e:
                logger.error(f"[US봇] 루프 오류: {e}", exc_info=True)
                time.sleep(30)

        self._save_state()
        self.add_log("⏹️ US 봇 루프 종료")

    # ─────────────────────────────────────────────────────────────────
    # 방어 자산 관리 (KR 봇 _handle_defensive_assets 동일 패턴)
    # ─────────────────────────────────────────────────────────────────

    def _handle_defensive_assets(self, regime: str):
        """
        BEAR 국면: US_DEFENSIVE_ASSETS 3종 자동 매수 (PSQ 20%, GLD 13%, UUP 7%).
        BULL/NEUTRAL 국면: 보유 중이면 각 자산 전량 청산.
        종목별 독립 24h 재매수 쿨다운 (휩쏘 방지).
        5분마다 한 번만 실행.
        """
        if not self.kis_overseas:
            return
        if time.time() - self._last_defensive_check < 300:
            return
        self._last_defensive_check = time.time()

        try:
            bal = self.kis_overseas.get_balance()
            if not bal:
                return

            cash_usd     = float(bal.get("cash_usd", 0))
            stocks       = bal.get("stocks", [])
            total_val_usd = cash_usd + sum(float(s.get("value", 0)) for s in stocks)
            stocks_map   = {s["ticker"]: s for s in stocks}

            # 방어자산 보유 수량 캐시 갱신
            for asset in US_DEFENSIVE_ASSETS:
                t = asset["ticker"]
                s = stocks_map.get(t)
                self._defensive_shares[t] = int(s.get("shares", 0)) if s else 0

            for asset in US_DEFENSIVE_ASSETS:
                ticker = asset["ticker"]
                name   = asset["name"]
                ratio  = asset["ratio"]
                emoji  = asset["emoji"]
                shares_held = self._defensive_shares.get(ticker, 0)
                has_pos     = shares_held > 0

                if regime == "BEAR" and not has_pos:
                    # 휩쏘 방지: 청산 후 24h 이내 재매수 금지
                    sold_ts = self._defensive_sold_ts.get(ticker, 0.0)
                    cooldown = 86400 - (time.time() - sold_ts)
                    if sold_ts > 0 and cooldown > 0:
                        self.add_log(f"⏳ [US방어] {name} 재매수 쿨다운 ({cooldown/3600:.1f}h) — 휩쏘 방지")
                        continue

                    budget_usd = total_val_usd * ratio
                    price_usd  = self._price_cache.get(ticker, 0.0)
                    if price_usd <= 0:
                        try:
                            import yfinance as yf
                            hist = yf.Ticker(ticker).history(period="2d")
                            if not hist.empty:
                                price_usd = float(hist["Close"].iloc[-1])
                                self._price_cache[ticker] = price_usd
                        except Exception:
                            pass
                    if price_usd <= 0:
                        continue

                    qty = int(budget_usd // price_usd)
                    if qty > 0 and cash_usd >= qty * price_usd * 1.003:
                        if self.kis_overseas.buy_market_order(ticker, qty):
                            cash_usd -= qty * price_usd
                            self._defensive_shares[ticker] = qty
                            fx = _get_fx_rate()
                            self.add_log(
                                f"🐻 [US방어 매수] {emoji} {name}({ticker}) "
                                f"{qty}주 @ ${price_usd:.2f} | 총자산 {ratio*100:.0f}% 헤지"
                            )
                            self._tg(
                                f"🐻 <b>US 방어 자산 매수</b>  {self.alert_icon}\n"
                                f"━━━━━━━━━━━━━━━━━━━━\n"
                                f"{emoji} <b>{name}</b>  <code>{ticker}</code>\n"
                                f"💰 <b>${price_usd:.2f}</b> × <b>{qty}주</b>"
                                f"  =  <b>₩{round(qty*price_usd*fx):,}</b>\n"
                                f"📋 BEAR 국면  ·  총자산 {ratio*100:.0f}% 헤지\n"
                                f"━━━━━━━━━━━━━━━━━━━━\n"
                                f"⏰ {_now_et().strftime('%H:%M ET')}"
                            )

                elif regime != "BEAR" and has_pos:
                    if self.kis_overseas.sell_market_order(ticker, shares_held):
                        self._defensive_sold_ts[ticker] = time.time()
                        self._defensive_shares[ticker]  = 0
                        price_usd = self._price_cache.get(ticker, 0.0)
                        self.add_log(
                            f"🐂 [US방어 청산] 국면→{regime} | {emoji} {name}({ticker}) "
                            f"{shares_held}주 전량 (24h 재매수 대기)"
                        )
                        self._tg(
                            f"🐂 <b>US 방어 자산 청산</b>  {self.alert_icon}\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"{emoji} <b>{name}</b>  <code>{ticker}</code>\n"
                            f"💰 <b>{shares_held}주</b> 전량 청산\n"
                            f"📋 국면 전환: BEAR → <b>{regime}</b>  ·  헤지 해제\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"⏰ {_now_et().strftime('%H:%M ET')}"
                        )

        except Exception as e:
            logger.error(f"[US봇] 방어 자산 처리 오류: {e}", exc_info=True)

    # ─────────────────────────────────────────────────────────────────
    # 시장 국면 판단 (SPY/QQQ 기반)
    # ─────────────────────────────────────────────────────────────────

    def _update_market_regime(self):
        """
        미국 시장 국면 + 야간선물 스냅샷 + NASDAQ 섹터 추세 갱신.

        ① SPY SMA20/50 → BULL / BEAR / NEUTRAL
        ② NQ=F / ES=F / EWY 선물 스냅샷 (선행지표)
        ③ NASDAQ 섹터 추세 분석 → hot_sectors 업데이트
        """
        # ── ① SPY 시장 국면 (KR과 동일한 7신호 + ADX 과열 필터) ─────
        try:
            import pandas as pd
            df = yf.download("SPY", period="120d", interval="1d",
                             progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.dropna(subset=["Close"])
            df.columns = [c.lower() for c in df.columns]
            if len(df) >= 60:
                close = df["close"]
                cur      = float(close.iloc[-1])
                sma5     = float(close.rolling(5).mean().iloc[-1])
                sma5_3ago= float(close.rolling(5).mean().iloc[-4])
                sma20    = float(close.rolling(20).mean().iloc[-1])
                sma20_5ago=float(close.rolling(20).mean().iloc[-6])
                sma50    = float(close.rolling(50).mean().iloc[-1])
                p22ago   = float(close.iloc[-23]) if len(close) >= 23 else float(close.iloc[0])

                # RSI(14)
                d  = close.diff()
                g  = d.clip(lower=0).rolling(14).mean()
                lo = (-d.clip(upper=0)).rolling(14).mean()
                rsi= float((100 - 100 / (1 + g / (lo + 1e-10))).iloc[-1])

                # 7신호 점수 (SPY 기준 — KR과 동일 구조)
                score = 0
                score += 1 if cur > sma5       else -1   # S1
                score += 1 if sma5 > sma5_3ago else -1   # S2
                score += 1 if cur > sma20      else -1   # M1
                score += 1 if sma20 > sma20_5ago else -1 # M2
                if rsi > 55:   score += 1                 # M3
                elif rsi < 45: score -= 1
                score += 1 if sma20 > sma50    else -1   # L1
                ret22 = (cur / p22ago - 1) * 100 if p22ago > 0 else 0
                if ret22 > 3.0:    score += 1             # L2
                elif ret22 < -3.0: score -= 1

                # ADX 과열 필터
                adx, plus_di, minus_di = _calc_adx(df)
                up_streak = _get_up_streak(close)
                downgrade_reason = ''

                if score >= 5:
                    base = "BULL"
                    if adx >= 40:
                        base = "NEUTRAL"
                        downgrade_reason = f"BULL→NEUTRAL: ADX {adx:.1f}≥40 (추세 막바지)"
                    elif up_streak >= 8:
                        base = "NEUTRAL"
                        downgrade_reason = f"BULL→NEUTRAL: {up_streak}일 연속 상승 (단기 과열)"
                elif score <= -4:
                    base = "BEAR"
                    if adx < 20:
                        base = "NEUTRAL"
                        downgrade_reason = f"BEAR→NEUTRAL: ADX {adx:.1f}<20 (추세 미확인)"
                    elif adx >= 50 and minus_di > 40:
                        base = "NEUTRAL"
                        downgrade_reason = f"BEAR→NEUTRAL: ADX {adx:.1f}≥50 패닉 저점 (낙폭 과대)"
                else:
                    base = "NEUTRAL"

                regime = base
                diag = (f"점수{score:+d} | ADX={adx:.1f} | +DI={plus_di:.1f}/-DI={minus_di:.1f} | "
                        f"연속{up_streak}일 | RSI={rsi:.1f} | 22일{ret22:+.1f}%")

                if downgrade_reason:
                    self.add_log(f"⚠️ [US] {downgrade_reason}")
                if regime != self.market_regime:
                    icons = {"BULL": "🐂", "BEAR": "🐻", "NEUTRAL": "😐"}
                    self.add_log(
                        f"{icons.get(regime,'📊')} US 시장 국면 변경: "
                        f"{self.market_regime} → {regime} | {diag}"
                    )
                    self._tg(
                        f"{icons.get(regime,'📊')} <b>US 시장 국면 변경</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"📊 <b>{self.market_regime}</b>  →  <b>{regime}</b>\n"
                        + (f"⚠️ {downgrade_reason}\n" if downgrade_reason else "")
                        + f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"📈 {diag}"
                    )
                # ── AI 하이브리드 판단 ────────────────────────────────
                if self.claude:
                    try:
                        # VIX 수집
                        vix_val = 20.0
                        try:
                            df_vix = yf.download("^VIX", period="3d", interval="1d",
                                                  progress=False, auto_adjust=True)
                            if isinstance(df_vix.columns, pd.MultiIndex):
                                df_vix.columns = df_vix.columns.get_level_values(0)
                            if not df_vix.empty:
                                vix_val = float(df_vix["Close"].iloc[-1])
                        except Exception:
                            pass
                        # NQ/ES 변화율
                        nq_chg = es_chg = 0.0
                        for sym, ref in [("NQ=F", "nq_chg"), ("ES=F", "es_chg")]:
                            try:
                                df_f = yf.download(sym, period="3d", interval="1d",
                                                   progress=False, auto_adjust=True)
                                if isinstance(df_f.columns, pd.MultiIndex):
                                    df_f.columns = df_f.columns.get_level_values(0)
                                if not df_f.empty and len(df_f) >= 2:
                                    c0 = float(df_f["Close"].iloc[-2])
                                    c1 = float(df_f["Close"].iloc[-1])
                                    val = (c1/c0 - 1) * 100
                                    if ref == "nq_chg": nq_chg = val
                                    else: es_chg = val
                            except Exception:
                                pass

                        ai_result = self.claude.ai_us_market_context(
                            rule_score  = score,
                            spy_regime  = regime,
                            nq_change   = nq_chg,
                            es_change   = es_chg,
                            vix         = vix_val,
                            spy_rsi     = rsi,
                            hot_sectors = self.hot_sectors[:5],
                        )
                        regime = ai_result['regime']
                        self._ai_market_entry_bonus = ai_result.get('entry_bonus', 0)
                        self.add_log(
                            f"🤖 [AI US시장] {base}(규칙) → {regime}(AI) "
                            f"| NQ{nq_chg:+.1f}% ES{es_chg:+.1f}% VIX{vix_val:.1f} "
                            f"| 진입보너스 {ai_result['entry_bonus']:+d}pt | {ai_result['reason']}"
                        )
                    except Exception as ai_err:
                        logger.debug(f"[US봇] AI 시장판단 실패: {ai_err}")
                else:
                    self._ai_market_entry_bonus = 0

                self.market_regime = regime
        except Exception as e:
            logger.debug(f"[US봇] 시장 국면 판단 실패: {e}")

        # ── ② 야간선물 스냅샷 ────────────────────────────────────────
        try:
            snap = get_futures_snapshot()
            self.futures_snapshot = snap
            summary = snap.get("summary", "N/A")
            self.add_log(f"📈 선물 스냅샷: {summary}")
        except Exception as e:
            logger.debug(f"[US봇] 선물 스냅샷 실패: {e}")

        # ── ③ NASDAQ 섹터 추세 ───────────────────────────────────────
        try:
            sector_result = get_sector_trends()
            self.sector_trends = sector_result.get("sectors", [])
            trend_hot  = sector_result.get("hot_sectors", [])
            trend_cold = sector_result.get("cold_sectors", [])

            # 스크리닝 선정 종목의 섹터와 병합 (순서 유지, 중복 제거)
            screen_hot = list({c["sector"] for c in self.satellite_info})
            merged = list(dict.fromkeys(trend_hot + screen_hot))
            if merged:
                self.hot_sectors = merged

            up_str   = ", ".join(trend_hot)  or "없음"
            down_str = ", ".join(trend_cold) or "없음"
            self.add_log(f"🏭 섹터 추세 — 상승: [{up_str}]  하락: [{down_str}]")
        except Exception as e:
            logger.debug(f"[US봇] 섹터 추세 분석 실패: {e}")

    # ─────────────────────────────────────────────────────────────────
    # US 분봉 진입 타이밍 (yfinance 5분봉, BULL 국면 제외 게이트)
    # ─────────────────────────────────────────────────────────────────

    def _check_minute_trend_up_us(self, ticker: str) -> bool:
        """최근 5개 5분봉 종가 기울기가 양수(상승 추세)이면 True.
        BULL 국면에서는 항상 True 반환 (상승 중 진입 기회 놓치지 않기 위해).
        데이터 조회 실패 시에도 True 반환 (차단하지 않음).
        5분봉 캐시(5분 TTL) 활용 — 매 호출마다 다운로드 방지.
        """
        if self.market_regime == "BULL":
            return True
        try:
            df = self._get_cached_ohlcv_5m(ticker, period="1d")
            if df.empty or len(df) < 3:
                return True
            closes = df["close"].iloc[-5:].tolist()
            return float(closes[-1]) >= float(closes[0])
        except Exception:
            return True

    # ─────────────────────────────────────────────────────────────────
    # US 기본적 분석 (PER·PBR·ROE — yfinance .info, 일 1회 캐싱)
    # ─────────────────────────────────────────────────────────────────

    def _fetch_fundamental(self, ticker: str) -> str:
        """yfinance .info로 PER·PBR·ROE를 조회하고 오늘 날짜 키로 캐싱 (일 1회).
        반환: "PER 25.3x | PBR 8.1x | ROE 42.5%" 형태 문자열, 실패 시 ""
        """
        import datetime as _dt
        today_str = _dt.date.today().strftime('%Y-%m-%d')
        cache_key = f"{ticker}_{today_str}"
        if cache_key in self.fundamental_cache:
            return self.fundamental_cache[cache_key]
        try:
            import yfinance as yf
            info = yf.Ticker(ticker).info
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

    # ─────────────────────────────────────────────────────────────────
    # US 뉴스 수집 (yfinance 무료 — API 키 불필요)
    # ─────────────────────────────────────────────────────────────────

    def _fetch_us_news(self, tickers: list = None) -> str:
        """보유/후보 종목의 최신 뉴스 헤드라인 수집 (Yahoo Finance, 무료)"""
        if not tickers:
            tickers = (
                [t for t, p in self.satellite_positions.items() if p.shares > 0]
                + [i["ticker"] for i in self.satellite_info]
            )
        tickers = list(dict.fromkeys(tickers))[:5]  # 중복 제거, 최대 5개
        lines = []
        for ticker in tickers:
            try:
                news_items = yf.Ticker(ticker).news or []
                for item in news_items[:2]:
                    title = item.get("title", "")
                    if title:
                        lines.append(f"- [{ticker}] {title}")
            except Exception:
                pass
        return "\n".join(lines) if lines else ""

    # ─────────────────────────────────────────────────────────────────
    # 일일 리포트
    # ─────────────────────────────────────────────────────────────────

    def generate_daily_report(self, time_slot: str = "16:10"):
        """ET 16:10 US 일일 리포트 생성 — KR 컨텍스트 + 뉴스 포함"""
        try:
            today_str = _now_et().strftime("%Y-%m-%d")
            if (isinstance(self.daily_report, dict)
                    and self.daily_report.get("date") == today_str
                    and self.daily_report.get(time_slot) is not None):
                return

            self.add_log(f"📝 US 일일 리포트 생성 중… ({time_slot} ET)")

            # ── 시장 국면 최신화 ──────────────────────────────────────
            self._update_market_regime()

            # ── US 뉴스 수집 ──────────────────────────────────────────
            news_context = self._fetch_us_news()

            # ── KR 봇 컨텍스트 (KR 시장이 오늘 어땠는지) ─────────────
            kr_context = ""
            try:
                bm = importlib.import_module("bots.bot_manager")
                kr_ctx = bm.manager.get_peer_context(self.user_id, want_us=False)
                if kr_ctx:
                    kr_context = (
                        f"한국 시장 국면: {kr_ctx['market_regime']}"
                        + (f" / 주도 섹터: {', '.join(kr_ctx['hot_sectors'])}" if kr_ctx['hot_sectors'] else "")
                        + (f" / KR봇 {'운행 중' if kr_ctx['is_running'] else '정지'}")
                    )
            except Exception:
                pass

            result = generate_us_daily_report(
                claude_client    = self.claude,
                positions        = dict(self.satellite_positions),
                satellite_info   = list(self.satellite_info),
                news_context     = news_context,
                kr_context       = kr_context,
                market_regime    = self.market_regime,
                futures_snapshot = self.futures_snapshot,
                sector_trends    = self.sector_trends,
            )
            if result:
                if not isinstance(self.daily_report, dict) or self.daily_report.get("date") != today_str:
                    self.daily_report = {"date": today_str, "16:10": None}
                self.daily_report[time_slot] = result.get("report_markdown", "")
                self._save_state()
                self.add_log(f"✅ US 리포트 발간 완료 ({time_slot} ET) — 국면: {self.market_regime}")
                self._tg(f"📝 [US 리포트 {time_slot} ET]\n\n{self.daily_report[time_slot][:3000]}")
        except Exception as e:
            logger.error(f"[US봇] 리포트 생성 오류: {e}", exc_info=True)

    # ─────────────────────────────────────────────────────────────────
    # 상태 저장 / 복원
    # ─────────────────────────────────────────────────────────────────

    def _save_state(self):
        try:
            state = {
                "cash_usd":              self.cash_usd,
                "last_asset_cost":       self.last_asset_cost,
                "initial_capital_captured": self.initial_capital_captured,
                "core_info":             self.core_info,
                "last_core_screen_date": self.last_core_screen_date,
                "satellite_info":        self.satellite_info,
                "hot_sectors":           self.hot_sectors,
                "daily_pnl":             self.daily_pnl,
                "daily_report":          self.daily_report,
                "last_screen_date":      self.last_screen_date,
                "futures_snapshot":      self.futures_snapshot,
                "sector_trends":         self.sector_trends,
                # 당일 AI 거절 블랙리스트 — 재시작 후에도 유지
                "bl_date":               self._bl_date,
                "satellite_rejects":     dict(self._satellite_rejects),
                "monday_swap_plan":      self._monday_swap_plan,
                "weekend_scan_done":     self._weekend_scan_done,
                "cores": {
                    t: {
                        "name":             p.name,
                        "shares":           p.shares,
                        "floor_shares":     p.floor_shares,
                        "avg_price_usd":    p.avg_price_usd,
                        "budget_usd":       p.budget_usd,
                        "partial_sold":     p.partial_sold,
                        "partial_sold_2":   p.partial_sold_2,
                        "max_price_usd":    p.max_price_usd,
                        "status":                  p.status,
                        "second_buy_price":        p.second_buy_price,
                        "second_buy_cash":         p.second_buy_cash,
                        "second_buy_done":         p.second_buy_done,
                        "third_buy_price":         p.third_buy_price,
                        "third_buy_cash":          p.third_buy_cash,
                        "third_buy_done":          p.third_buy_done,
                        "bull_pyramid_done":       getattr(p, 'bull_pyramid_done', False),
                        "stop_news_checked":       getattr(p, 'stop_news_checked', False),
                        "initial_shares_for_exit": p.initial_shares_for_exit,
                    }
                    for t, p in self.core_positions.items()
                },
                "satellites": {
                    t: {
                        "name":                    p.name,
                        "shares":                  p.shares,
                        "floor_shares":            p.floor_shares,
                        "avg_price_usd":           p.avg_price_usd,
                        "budget_usd":              p.budget_usd,
                        "partial_sold":            p.partial_sold,
                        "partial_sold_2":          p.partial_sold_2,
                        "max_price_usd":           p.max_price_usd,
                        "status":                  p.status,
                        "second_buy_price":        p.second_buy_price,
                        "second_buy_cash":         p.second_buy_cash,
                        "second_buy_done":         p.second_buy_done,
                        "third_buy_price":         p.third_buy_price,
                        "third_buy_cash":          p.third_buy_cash,
                        "third_buy_done":          p.third_buy_done,
                        "bull_pyramid_done":       getattr(p, 'bull_pyramid_done', False),
                        "stop_news_checked":       getattr(p, 'stop_news_checked', False),
                        "overext_sell_count":      getattr(p, 'overext_sell_count', 0),
                        "initial_shares_for_exit": p.initial_shares_for_exit,
                    }
                    for t, p in self.satellite_positions.items()
                },
            }
            save_portfolio_state(self.user_id, state, is_mock=True)
        except Exception as e:
            logger.warning(f"[US봇] 상태 저장 실패: {e}")

    def _restore_state(self):
        try:
            state = load_portfolio_state(self.user_id, is_mock=True)
            if not state:
                return
            self.cash_usd               = float(state.get("cash_usd", 0))
            self.last_asset_cost        = state.get("last_asset_cost")
            self.initial_capital_captured = bool(state.get("initial_capital_captured", False))
            self.core_info              = state.get("core_info", [])[:self.num_cores]
            self.last_core_screen_date  = None   # 재시작 시 항상 재스캔 (교체 즉시 반영)
            self.satellite_info         = state.get("satellite_info", [])
            self.hot_sectors            = state.get("hot_sectors", [])
            self.daily_pnl              = state.get("daily_pnl", {})
            self.daily_report           = state.get("daily_report", None)
            self.last_screen_date       = None   # 재시작 시 항상 재스캔
            self.futures_snapshot       = state.get("futures_snapshot", {})
            self.sector_trends          = state.get("sector_trends", [])
            # 당일 블랙리스트 복원 (같은 날 재시작 시에만)
            saved_bl_date = state.get("bl_date", "")
            today_str_us  = _now_et().strftime("%Y-%m-%d")
            if saved_bl_date == today_str_us:
                self._bl_date           = saved_bl_date
                self._satellite_rejects = state.get("satellite_rejects", {})
            # 주말 교체 계획 복원
            self._monday_swap_plan  = state.get("monday_swap_plan", {})
            self._weekend_scan_done = state.get("weekend_scan_done", "")
            if self._monday_swap_plan:
                self.add_log(f"📅 [US] 주말 교체 계획 복원: {len(self._monday_swap_plan)}건 대기")
                n_rej = len(self._satellite_rejects)
                if n_rej:
                    self.add_log(f"🚫 [US] 당일 AI 거절 블랙리스트 복원: {n_rej}개 종목 재심사 제외")
            for t, s in state.get("cores", {}).items():
                pos = USPosition(
                    ticker         = t,
                    name           = s.get("name", t),
                    shares         = float(s.get("shares", 0)),
                    floor_shares   = float(s.get("floor_shares", 0.0)),
                    avg_price_usd  = float(s.get("avg_price_usd", 0)),
                    budget_usd     = float(s.get("budget_usd", 0)),
                    partial_sold   = bool(s.get("partial_sold", False)),
                    partial_sold_2 = bool(s.get("partial_sold_2", False)),
                    max_price_usd  = float(s.get("max_price_usd", 0)),
                    status         = s.get("status", "코어 보유 💎"),
                )
                pos.second_buy_price        = float(s.get("second_buy_price", 0.0))
                pos.second_buy_cash         = float(s.get("second_buy_cash",  0.0))
                pos.second_buy_done         = bool(s.get("second_buy_done",   False))
                pos.third_buy_price         = float(s.get("third_buy_price",  0.0))
                pos.third_buy_cash          = float(s.get("third_buy_cash",   0.0))
                pos.third_buy_done          = bool(s.get("third_buy_done",    False))
                pos.bull_pyramid_done       = bool(s.get("bull_pyramid_done", False))
                pos.stop_news_checked       = bool(s.get("stop_news_checked", False))
                pos.initial_shares_for_exit = float(s.get("initial_shares_for_exit", 0.0))
                self.core_positions[t] = pos
            for t, s in state.get("satellites", {}).items():
                _sat_pos = USPosition(
                    ticker         = t,
                    name           = s.get("name", t),
                    shares         = float(s.get("shares", 0)),
                    floor_shares   = float(s.get("floor_shares", 0.0)),
                    avg_price_usd  = float(s.get("avg_price_usd", 0)),
                    budget_usd     = float(s.get("budget_usd", 0)),
                    partial_sold   = bool(s.get("partial_sold", False)),
                    partial_sold_2 = bool(s.get("partial_sold_2", False)),
                    max_price_usd  = float(s.get("max_price_usd", 0)),
                    status         = s.get("status", "보유 중 🛰️"),
                )
                _sat_pos.second_buy_price        = float(s.get("second_buy_price", 0.0))
                _sat_pos.second_buy_cash         = float(s.get("second_buy_cash",  0.0))
                _sat_pos.second_buy_done         = bool(s.get("second_buy_done",   False))
                _sat_pos.third_buy_price         = float(s.get("third_buy_price",  0.0))
                _sat_pos.third_buy_cash          = float(s.get("third_buy_cash",   0.0))
                _sat_pos.third_buy_done          = bool(s.get("third_buy_done",    False))
                _sat_pos.bull_pyramid_done       = bool(s.get("bull_pyramid_done", False))
                _sat_pos.stop_news_checked       = bool(s.get("stop_news_checked", False))
                _sat_pos.overext_sell_count      = int(s.get("overext_sell_count",  0))
                _sat_pos.initial_shares_for_exit = float(s.get("initial_shares_for_exit", 0.0))
                self.satellite_positions[t] = _sat_pos
            # satellite_info에 선정된 종목 중 positions에 없는 것 → 빈 포지션 생성 (대시보드 표시용)
            _existing_us = set(self.satellite_positions.keys())
            for _sat in self.satellite_info:
                _t = _sat.get("ticker")
                if _t and _t not in _existing_us:
                    self.satellite_positions[_t] = USPosition(
                        ticker=_t, name=_sat.get("name", _t),
                        status="감시 중 👀"
                    )
                    _existing_us.add(_t)

            # 중앙 재구성: 복원된 info↔positions 완전 동기화
            self._rebuild_positions()
            # 재시작 후 KIS 실계좌 잔고로 shares 검증 (state.json과 불일치 방지)
            if self.kis:
                try:
                    _bal = self.kis.get_account_balance()
                    if _bal and _bal.get('stocks'):
                        _ks = {s['ticker']: float(s.get('shares', 0)) for s in _bal['stocks'] if s.get('ticker')}
                        with self.lock:
                            for _pos in list(self.core_positions.values()) + list(self.satellite_positions.values()):
                                if _pos.ticker in _ks:
                                    _pos.shares = _ks[_pos.ticker]
                                elif _pos.shares > 0:
                                    _pos.shares = 0.0  # KIS에 없는 포지션 초기화
                        self.add_log("✅ [US복원] KIS 실계좌 잔고로 shares 검증 완료")
                except Exception as _ve:
                    logger.warning(f"[US복원] KIS 잔고 검증 실패: {_ve}")
            self.add_log("📂 이전 상태 복원 완료")
        except Exception as e:
            logger.warning(f"[US봇] 상태 복원 실패: {e}")

    # ─────────────────────────────────────────────────────────────────
    # 공개 인터페이스 (KRBotController 호환)
    # ─────────────────────────────────────────────────────────────────

    def reload_api_keys(self, kis_config, telegram_config, gemini_config, core_stocks):
        """API 키 및 코어 설정 갱신 — KR봇과 동일하게 즉시 반영"""
        self._init_api(kis_config)
        if self.kis_overseas:
            self.add_log("🔑 KIS 해외주식 API 갱신 완료")
        else:
            self.add_log("⚠️ KIS 해외주식 API 미설정")

        if telegram_config and telegram_config.get("token"):
            try:
                self.telegram = TelegramNotifier(
                    token   = telegram_config["token"].strip(),
                    chat_id = (telegram_config.get("chat_id") or "").strip(),
                )
            except Exception:
                self.telegram = None
        else:
            self.telegram = None

        # ── 코어/위성 설정 즉시 반영 (KR봇과 동일) ──────────────────
        try:
            self.user_core_stocks = json.loads(core_stocks) if core_stocks else []
        except Exception:
            self.user_core_stocks = []

        # 코어 포지션 즉시 재구성 — 기존 보유 데이터 보존
        existing = dict(self.core_positions)
        self.core_positions = {}
        for uc in self.user_core_stocks:
            t = uc.get('ticker')
            if not t:
                continue
            pos = existing.get(t) or USPosition(ticker=t, name=uc.get('name', t))
            pos.ticker = t
            pos.name   = uc.get('name', t)
            self.core_positions[t] = pos

        # user_core_stocks를 core_info에도 반영
        self._inject_user_cores()

        # 중앙 재구성 + 재스캔 강제
        self._rebuild_positions()
        self.last_screen_date      = None
        self.last_core_screen_date = None

        self._save_state()
        self.add_log(f"🔑 [US봇] 설정 갱신 완료 — 코어·위성 재스캔 즉시 예약")

    def reload_news_monitor(self, dart_key: str, naver_id: str, naver_secret: str):
        """BaseBot 호환 — US 봇은 한국 뉴스 모니터 미사용"""
        pass

    def start(self, total_cash: float = 10_000_000) -> bool:
        if self.is_running:
            return False
        self.is_running = True
        self.initial_capital_captured = False  # 재시작 시 원금 재감지
        self.thread = threading.Thread(
            target=self._run_loop, args=(total_cash,), daemon=True
        )
        self.thread.start()
        update_bot_status(self.user_id, True, is_mock=True)
        self.add_log("▶️ US 실전 매매 봇 시작")
        return True

    def stop(self):
        if self.is_running:
            self.is_running = False
            update_bot_status(self.user_id, False, is_mock=True)
            if self.thread:
                self.thread.join(timeout=5)
            self._save_state()

    def get_pnl_data(self) -> dict:
        """일/주/월/년 손익 집계 (KRW 환산)"""
        fx      = _get_fx_rate()
        krw_pnl = {d: round(v * fx) for d, v in self.daily_pnl.items()}
        sorted_days = sorted(krw_pnl.keys())

        daily_labels = sorted_days[-30:]
        daily_values = [krw_pnl[d] for d in daily_labels]

        weekly: dict = defaultdict(float)
        for d in sorted_days:
            try:
                dt = datetime.strptime(d, "%Y-%m-%d")
                weekly[dt.strftime("%Y-W%W")] += krw_pnl.get(d, 0)
            except Exception:
                pass
        wl = sorted(weekly.keys())[-26:]

        monthly: dict = defaultdict(float)
        for d in sorted_days:
            monthly[d[:7]] += krw_pnl.get(d, 0)
        ml = sorted(monthly.keys())[-24:]

        yearly: dict = defaultdict(float)
        for d in sorted_days:
            yearly[d[:4]] += krw_pnl.get(d, 0)
        yl = sorted(yearly.keys())

        return {
            "daily":   {"labels": daily_labels, "values": daily_values},
            "weekly":  {"labels": wl, "values": [round(weekly[w]) for w in wl]},
            "monthly": {"labels": ml, "values": [round(monthly[m]) for m in ml]},
            "yearly":  {"labels": yl, "values": [round(yearly[y]) for y in yl]},
            "labels":  daily_labels,
            "values":  daily_values,
        }

    def get_status(self) -> dict:
        """KRBotController.get_status()와 동일한 JSON 형식 반환 (KRW 환산)"""
        try:
            fx = _get_fx_rate()

            # ── 코어 포지션 ───────────────────────────────────────────
            total_core_usd = 0.0
            cores_data = []
            with self.lock:
                core_items = list(self.core_positions.items()) if isinstance(self.core_positions, dict) else []
            for t, pos in core_items:
                sp_usd  = self._price_cache.get(t, pos.avg_price_usd)
                val_usd = pos.shares * sp_usd
                total_core_usd += val_usd
                avg_p   = pos.avg_price_usd
                pnl_pct = ((sp_usd / avg_p) - 1) * 100 if avg_p > 0 else 0.0
                cores_data.append({
                    "name":       pos.name,
                    "ticker":     t,
                    "shares":     pos.shares,
                    "floor_shares": pos.floor_shares,
                    "price":      round(sp_usd * fx),
                    "value":      round(val_usd * fx),
                    "avg_price":  round(avg_p * fx),
                    "budget":     round(pos.budget_usd * fx),
                    "floor":      round(pos.floor_shares * avg_p * fx) if avg_p > 0 else 0,
                    "status":     pos.status,
                    "status_msg": (
                        f"{pos.shares:.0f}주 보유 | 평단 {avg_p * fx:,.0f}원 (${avg_p:.2f}) | "
                        f"수익 {pnl_pct:+.1f}% | 현재가 {sp_usd * fx:,.0f}원 | 시장: {self.market_regime}"
                        if pos.shares > 0 and avg_p > 0
                        else f"현재가 {sp_usd * fx:,.0f}원 (${sp_usd:.2f}) | "
                             f"진입점수 확인 중 | 시장: {self.market_regime}"
                    ),
                    "dca_mode":   False,
                })

            # ── 위성 포지션 ───────────────────────────────────────────
            total_sat_usd = 0.0
            satellites    = []
            holding_items = [(t, p) for t, p in self.satellite_positions.items() if p.shares > 0]
            empty_items   = [(t, p) for t, p in self.satellite_positions.items() if p.shares == 0]
            display_items = (holding_items + empty_items)[:self.num_satellites]

            for t, pos in display_items:
                sp_usd  = self._price_cache.get(t, pos.avg_price_usd)
                val_usd = pos.shares * sp_usd
                total_sat_usd += val_usd
                avg_p   = pos.avg_price_usd
                pnl_pct = ((sp_usd / avg_p) - 1) * 100 if avg_p > 0 else 0.0
                satellites.append({
                    "name":       pos.name,
                    "ticker":     t,
                    "shares":     int(pos.shares),
                    "price":      round(sp_usd * fx),
                    "value":      round(val_usd * fx),
                    "avg_price":  round(avg_p * fx),
                    "budget":     round(pos.budget_usd * fx),
                    "status":     pos.status,
                    "status_msg": (
                        f"{int(pos.shares)}주 보유 | 평단 {avg_p * fx:,.0f}원 (${avg_p:.2f}) | "
                        f"수익 {pnl_pct:+.1f}% | 현재가 {sp_usd * fx:,.0f}원 | 시장: {self.market_regime}"
                        if pos.shares > 0 and avg_p > 0
                        else f"현재가 {sp_usd * fx:,.0f}원 (${sp_usd:.2f}) | "
                             f"진입 대기 | 시장: {self.market_regime}"
                    ),
                })

            # 표시 외 보유 종목도 총액에 합산
            for t, pos in self.satellite_positions.items():
                if t not in {s["ticker"] for s in satellites} and pos.shares > 0:
                    sp_usd = self._price_cache.get(t, pos.avg_price_usd)
                    total_sat_usd += pos.shares * sp_usd

            # ── 방어 자산 상태 ─────────────────────────────────────────
            is_bear = (self.market_regime == "BEAR")
            defensive_list = []
            # 방어자산 가격 캐시 미스 시 KIS API fallback
            _def_miss = [a["ticker"] for a in US_DEFENSIVE_ASSETS if not self._price_cache.get(a["ticker"])]
            if _def_miss and self.kis_overseas:
                try:
                    fetched = self.kis_overseas.get_prices_batch(_def_miss)
                    self._price_cache.update(fetched)
                except Exception:
                    pass
            for asset in US_DEFENSIVE_ASSETS:
                t        = asset["ticker"]
                sp_usd   = self._price_cache.get(t, 0.0)
                d_shares = self._defensive_shares.get(t, 0)
                defensive_list.append({
                    "ticker":     t,
                    "name":       asset["name"],
                    "emoji":      asset["emoji"],
                    "ratio":      asset["ratio"],
                    "price":      round(sp_usd * fx),
                    "shares":     d_shares,
                    "value":      round(d_shares * sp_usd * fx),
                    "active":     is_bear,
                    "change_pct": 0.0,
                })

            # ── 총 평가금액 (코어 + 위성 + 방어 + 현금) ──────────────
            total_def_usd = sum(
                self._defensive_shares.get(a["ticker"], 0) * self._price_cache.get(a["ticker"], 0.0)
                for a in US_DEFENSIVE_ASSETS
            )
            total_usd   = self.cash_usd + total_core_usd + total_sat_usd + total_def_usd
            total_krw   = round(total_usd * fx)
            initial_krw = get_user_initial_cash(self.user_id, self._is_mock)
            pnl_krw     = total_krw - initial_krw
            pnl_rt      = (pnl_krw / initial_krw * 100) if initial_krw > 0 else 0.0

            return {
                "is_running":       self.is_running,
                "is_mock":          True,
                "has_keys":         self.kis_overseas is not None,
                "logs":             list(self.logs)[-30:],
                "hot_sectors":      self.hot_sectors,
                "num_satellites":   self.num_satellites,
                "cores":            cores_data,
                "satellites":       satellites,
                "momentum_list":    [],
                "defensive_list":   defensive_list,
                "market_regime":    self.market_regime,
                "us_total_asset": total_krw,
                "us_pnl":         pnl_krw,
                "us_pnl_rt":      round(pnl_rt, 2),
                "initial_cash":     initial_krw,
                "available_cash":   round(self.cash_usd * fx),
            }

        except Exception as e:
            logger.error(f"[US봇] get_status 오류: {e}", exc_info=True)
            return {
                "is_running": self.is_running, "is_mock": True, "has_keys": False,
                "logs": list(self.logs)[-30:], "hot_sectors": [], "num_satellites": self.num_satellites,
                "cores": [], "satellites": [], "momentum_list": [], "defensive_list": [],
                "market_regime": "NEUTRAL", "us_total_asset": 0, "us_pnl": 0,
                "us_pnl_rt": 0, "initial_cash": 10_000_000, "available_cash": 0,
            }
