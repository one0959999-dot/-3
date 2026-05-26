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
from collections import defaultdict

import yfinance as yf

from telegram_bot import TelegramNotifier
from us_screener import (
    scan_us_satellites, scan_us_satellites_kis,
    get_us_prices_batch, generate_us_daily_report,
    get_futures_snapshot, get_sector_trends,
)
from kis_brokers.kis_overseas_api import KisOverseasApi
from database import (
    update_bot_status,
    save_portfolio_state,
    load_portfolio_state,
    get_user_initial_cash,
    set_user_initial_cash,
    add_user_initial_cash,
    get_sector_guide,
)
from strategy import (calculate_entry_score, get_entry_threshold, get_budget_ratio_from_score,
                      get_bull_momentum_score, calc_rsi, _calc_adx, _get_up_streak)
# bot_manager는 순환 임포트 방지를 위해 런타임에 참조
import importlib

logger = logging.getLogger('lassi_bot')

# ── 미국 동부 시간 (EDT = UTC-4, 서머타임 기준) ─────────────────────
_ET = timezone(timedelta(hours=-4))

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
    second_buy_price:    float = 0.0    # 2차 매수 발동가 (1차 진입가 × 0.98)
    second_buy_cash:     float = 0.0    # 2차 매수 유보 예산 (USD)
    second_buy_done:     bool  = False  # 2차 매수 완료 여부


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
        self.num_satellites = 3

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
        self.num_cores            = 3
        self.last_core_screen_date = None       # 코어 스크리닝 날짜 (주 1회)
        self.satellite_positions: dict[str, USPosition] = {}
        self.satellite_info:      list = []
        self.hot_sectors:         list = []
        self.daily_pnl:           dict = {}
        self.daily_report              = None

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

        # ── 블랙리스트 ────────────────────────────────────────────────
        self._bl_date           = ""
        self._satellite_rejects: dict = {}

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
        self.gemini          = None
        self.sector_guide    = get_sector_guide(user_id) or ""
        self.core_positions  = []
        self.fundamental_cache: dict = {}

        self.lock = threading.RLock()

        # 상태 복원
        self._restore_state()

        # 백그라운드 가격 갱신 (60초 주기)
        self._sync_thread = threading.Thread(
            target=self._perpetual_price_sync, daemon=True
        )
        self._sync_thread.start()

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
                        pos.shares = 0.0
                        pos.status = "청산됨 (KIS 동기화)"

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
        """백그라운드 가격 갱신 루프 — 60초마다."""
        while True:
            try:
                self._refresh_prices()
            except Exception as e:
                logger.debug(f"[US봇] 가격 동기화 오류: {e}")
            time.sleep(60)

    def _price(self, ticker: str) -> float:
        return self._price_cache.get(ticker, 0.0)

    # ─────────────────────────────────────────────────────────────────
    # 주문 — KIS 실주문 후 즉시 잔고 재동기화
    # ─────────────────────────────────────────────────────────────────

    def _buy(self, ticker: str, name: str, budget_usd: float, price: float = 0) -> int:
        """실전 매수. KIS 시장가 주문 → 즉시 잔고 재동기화. 체결 주수 반환 (0=실패)"""
        if not self.kis_overseas:
            self.add_log(f"⚠️ BUY 실패: KIS API 미설정 ({ticker})")
            return 0
        price = price or self._price(ticker)
        if price <= 0 or budget_usd <= 0:
            return 0
        with self.lock:
            avail = min(budget_usd, self.cash_usd)
            qty   = int(avail / price)
            if qty <= 0:
                return 0

        ok = self.kis_overseas.buy_market_order(ticker, qty)
        if ok:
            cost = qty * price
            with self.lock:
                self.cash_usd        = max(0.0, self.cash_usd - cost)
                self._last_trade_ts  = time.time()
                self.pnl_this_turn  -= cost   # 원금 추적용
            self.add_log(f"📥 BUY  {name}({ticker}) {qty}주 @ ${price:.2f} 추정 (${cost:,.0f})")
            # 주문 후 5초 대기 후 즉시 잔고 재조회
            self._run_threaded(lambda: (time.sleep(5), self._sync_balance_from_kis()))
            return qty
        else:
            self.add_log(f"❌ BUY 주문 실패: {name}({ticker}) — KIS 응답 확인 필요")
            return 0

    def _sell(self, ticker: str, name: str, shares: float, price: float = 0) -> float:
        """실전 매도. KIS 시장가 주문 → 즉시 잔고 재동기화. 체결 대금(USD) 추정값 반환"""
        if not self.kis_overseas:
            self.add_log(f"⚠️ SELL 실패: KIS API 미설정 ({ticker})")
            return 0.0
        price = price or self._price(ticker)
        qty   = int(shares)
        if price <= 0 or qty <= 0:
            return 0.0

        ok = self.kis_overseas.sell_market_order(ticker, qty)
        if ok:
            proceeds = qty * price * (1 - _US_FEE)
            with self.lock:
                self.cash_usd       += proceeds
                self._last_trade_ts  = time.time()
                self.pnl_this_turn  += proceeds   # 원금 추적용
            self.add_log(f"📤 SELL {name}({ticker}) {qty}주 @ ${price:.2f} 추정 (${proceeds:,.0f})")
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
    # 코어 스크리닝 (주 1회 — 월요일 KR 봇과 동일 패턴)
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
        """월요일 1회 AI가 코어 종목 선정 (장기 우량주)."""
        now = _now_et()
        today = now.strftime("%Y-%m-%d")
        # 주 1회 (월요일) 또는 코어가 비어있을 때만 실행
        if self.last_core_screen_date == today:
            return
        if now.weekday() != 0 and self.core_info:
            return

        holding = {t for t, p in self.core_positions.items() if p.shares > 0}
        self.add_log("🔍 US 코어 종목 스캔 시작…")

        try:
            from us_screener import scan_us_satellites
            # 코어는 더 넓게 스캔 후 AI에게 넘김
            candidates = scan_us_satellites(n=self.num_cores * 3, exclude=holding)
            if not candidates:
                self.add_log("⚠️ 코어 스캔 결과 없음 — 기존 유지")
                return

            if self.gemini:
                ai_result = self.gemini.ai_select_us_core_stocks(
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
                self.add_log(f"✅ 코어 종목: {[c['ticker'] for c in self.core_info]}")

            self._inject_user_cores()   # 사용자 지정 코어 우선 고정
            self.last_core_screen_date = today
        except Exception as e:
            logger.warning(f"[US봇] 코어 스캔 오류: {e}")

    # ─────────────────────────────────────────────────────────────────
    # 코어 관리 (KR 코어와 동일 로직: RSI + ATR 손절 + 통합 점수)
    # ─────────────────────────────────────────────────────────────────

    def _manage_cores(self):
        """코어 포지션 매수/손절 — KR 코어와 동일 전략."""
        if not self.kis_overseas or not self.core_info:
            return

        import pandas as pd
        # 총자산 기준 예산 산정 (수익 복리 효과: 수익금 → cash_usd → 총자산 증가 → 예산 자동 증가)
        total_usd      = self._get_total_assets_usd()
        core_budget_per = (total_usd * self.CORE_RATIO) / max(1, self.num_cores)

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

            # OHLCV 조회 (yfinance)
            try:
                import yfinance as yf
                df_raw = yf.download(ticker, period="180d", interval="1d",
                                     progress=False, auto_adjust=True)
                if hasattr(df_raw.columns, "get_level_values"):
                    df_raw.columns = df_raw.columns.get_level_values(0)
                df_raw = df_raw.dropna(subset=["Close"])
                df_raw.columns = [c.lower() for c in df_raw.columns]
            except Exception:
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
                    proceeds = self._sell(ticker, pos.name, pos.shares, price)
                    pnl      = _net_profit_usd(price, avg, pos.shares)
                    with self.lock:
                        pos.shares             = 0.0
                        pos.partial_sold       = False
                        pos.partial_sold_2     = False
                        pos.second_buy_price   = 0.0
                        pos.second_buy_cash    = 0.0
                        pos.second_buy_done    = False
                        pos.bull_pyramid_done  = False
                        pos.status             = "코어 손절 🚨"
                    self._record_pnl(pnl)
                    self.add_log(f"🚨 코어 손절 {pos.name}({ticker}) | ATR×{hard_mult:.1f} 이탈 | PnL ${pnl:+.0f}")
                    self._tg(f"🚨 [US 코어 손절] {pos.name}\nATR×{hard_mult:.1f} 이탈 | 재진입 타점 탐색 중\n손익: ${pnl:+,.0f}")
                continue

            # ── 통합 진입 점수 + RSI 매수 신호 ─────────────────────────
            if pos.shares == 0 and is_cd:
                available_cash = self.cash_usd
                budget = min(core_budget_per, available_cash)
                if budget < price * 0.1:
                    continue

                # 통합 점수 체크
                if df_raw is not None and not df_raw.empty:
                    momentum_20d = 0.0
                    try:
                        c = df_raw['close'].dropna()
                        if len(c) >= 21:
                            momentum_20d = float((c.iloc[-1] / c.iloc[-21] - 1) * 100)
                    except Exception:
                        pass
                    c_score, c_reasons = calculate_entry_score(
                        df_raw, price, regime, momentum_20d=momentum_20d
                    )
                else:
                    c_score, c_reasons = 0, []

                c_threshold = get_entry_threshold(regime, 'core')

                # ── BULL 국면 진입 완화 ─────────────────────────────────
                # 조건 A: RSI ≤ 65 + bull_score ≥ 1
                # 조건 B: MA5 > MA20 정배열 + 현재가 MA5 이내(2%) 눌림목
                if c_score < c_threshold and regime == "BULL" and df_raw is not None and not df_raw.empty:
                    try:
                        _closes_b = df_raw['close'].dropna()
                        _rsi_bull = float(calc_rsi(_closes_b).iloc[-1])
                        _bull_sc, _ = get_bull_momentum_score(df_raw)
                        _bull_cond_a = (_rsi_bull <= 65) and (_bull_sc >= 1)
                        _bull_cond_b = False
                        if len(_closes_b) >= 22:
                            _ma5_b  = float(_closes_b.rolling(5).mean().iloc[-1])
                            _ma20_b = float(_closes_b.rolling(20).mean().iloc[-1])
                            _bull_cond_b = (_ma5_b > _ma20_b) and (price <= _ma5_b * 1.02)
                        if _bull_cond_a or _bull_cond_b:
                            c_score = c_threshold
                            _why = (f"RSI={_rsi_bull:.1f} bull_score={_bull_sc}" if _bull_cond_a
                                    else f"MA5눌림목(MA5={_closes_b.rolling(5).mean().iloc[-1]:.2f})")
                            self.add_log(f"🚀 [BULL 코어 진입] {ticker} {_why} → 점수 완화 진입")
                    except Exception:
                        pass

                if c_score < c_threshold:
                    with self.lock:
                        pos.status = f"코어 진입 대기 ({c_score}/{c_threshold}pt) ⏳"
                    continue

                budget_ratio  = get_budget_ratio_from_score(c_score, c_threshold)
                # 75/25 분할: 1차 진입 후 나머지는 -2% 눌림목 예약
                first_ratio   = budget_ratio * 0.75
                reserve_usd   = budget * budget_ratio * 0.25
                qty = int((budget * first_ratio) // price)
                if qty > 0:
                    # AI 승인 (위성과 동일)
                    approved, ai_reason = True, "AI 미설정"
                    if self.gemini:
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
                        approved, ai_reason = self.gemini.ai_approve_us_trade(
                            signal         = 'BUY',
                            stock_name     = pos.name,
                            ticker         = ticker,
                            price_usd      = price,
                            sector         = info.get("sector", ""),
                            hot_sectors    = self.hot_sectors,
                            momentum_20d   = momentum_20d,
                            rsi            = info.get("rsi", 50.0),
                            ai_reason      = info.get("ai_reason", ""),
                            news_headlines = _core_news,
                        )
                    if not approved:
                        with self.lock:
                            pos.status = f"코어 AI 거절 🛑 ({c_score}pt)"
                        self.add_log(f"🛑 코어 AI 거절 {pos.name}({ticker}): {ai_reason[:60]}")
                        self._tg(
                            f"🛑 <b>[US 코어 매수 거절]</b>  {self.alert_icon}\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"📌 <b>{pos.name}</b>  {ticker}\n"
                            f"🤖 {ai_reason[:100]}\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"⏰ {_now_et().strftime('%H:%M ET')}"
                        )
                    else:
                        bought_qty = self._buy(ticker, pos.name, budget * first_ratio, price)
                        if bought_qty > 0:
                            with self.lock:
                                pos.shares          = float(bought_qty)
                                pos.avg_price_usd   = price
                                pos.max_price_usd   = price
                                pos.partial_sold    = False
                                pos.partial_sold_2  = False
                                pos.last_order_time = time.time()
                                pos.second_buy_price= price * 0.98
                                pos.second_buy_cash = reserve_usd
                                pos.second_buy_done = False
                                pos.status          = f"코어 보유 💎 ({c_score}pt)"
                            score_str = " | ".join(c_reasons[:3])
                            self.add_log(f"💎 코어 1차 매수 {pos.name}({ticker}) {bought_qty}주 @ ${price:.2f} | {c_score}pt [{score_str}] | 2차 예약 ${price*0.98:.2f} | AI: {ai_reason[:40]}")
                            self._tg(f"💎 [US 코어 1차 매수] {pos.name} ({ticker})\n@ ${price:.2f}  점수 {c_score}pt\n2차 예약: ${price*0.98:.2f} (-2%)")

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
                        pos.second_buy_done= True
                        pos.second_buy_cash= 0.0
                        pos.last_order_time= time.time()
                        pos.status         = "2차 매수 ✅"
                    self.add_log(f"💎 코어 2차 매수 {pos.name}({ticker}) {sq}주 @ ${price:.2f} | 눌림목 -2%")
                    self._tg(f"💎 [US 코어 2차 매수] {pos.name}\n@ ${price:.2f}  눌림목 -2% 포착")

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
                                pos.bull_pyramid_done = True
                                pos.last_order_time= time.time()
                                pos.status         = f"불타기 🔥 (+{_py_pct:.1f}%)"
                            self.add_log(f"🔥 [BULL 불타기] {pos.name}({ticker}) +{_py_pct:.1f}% | {_py_qty}주 @ ${price:.2f} 추가")
                            self._tg(f"🔥 [US BULL 불타기] {pos.name}\n+{_py_pct:.1f}% 추세 추종 | {_py_qty}주 @ ${price:.2f}")
                except Exception as _pye:
                    logger.debug(f"[US봇] BULL 불타기(코어) 오류: {_pye}")
            # ─────────────────────────────────────────────────────────────

            # ── 코어 BEAR 조기 익절: +5% 도달 시 즉시 전량 청산 ──────
            if pos.shares > 0 and avg > 0 and is_cd and regime == "BEAR":
                pnl_pct_bear = (price / avg - 1) * 100
                if pnl_pct_bear >= 5.0:
                    q   = pos.shares
                    self._sell(ticker, pos.name, q, price)
                    pnl = _net_profit_usd(price, avg, q)
                    with self.lock:
                        pos.shares            = 0.0
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
                        if self.gemini:
                            self._trigger_ai_partial_exit(pos, ticker, pos.name, price, avg, pnl_pct, regime)
                            with self.lock: pos.status = f"AI 익절 검토 중 ({pnl_pct:+.1f}%) 🤖"
                        else:
                            with self.lock: pos.ai_exit_decision = "SELL_PARTIAL"
                    elif decision == "HOLD":
                        with self.lock:
                            pos.status = f"AI 홀드 ({pnl_pct:+.1f}%) ⏳"
                    else:
                        q   = max(1.0, pos.shares * self.PARTIAL1_QTY)
                        self._sell(ticker, pos.name, q, price)
                        pnl = _net_profit_usd(price, avg, q)
                        with self.lock:
                            pos.shares           -= q
                            pos.partial_sold      = True
                            pos.ai_exit_decision  = None
                            pos.status            = f"코어 1차익절({pnl_pct:+.1f}%) ✂️"
                        self._record_pnl(pnl)
                        self.add_log(f"✂️  코어 1차익절 {pos.name} | PnL ${pnl:+.0f}")

                # 2차: +20%(일반) / +30%(BULL) 도달 → AI에 전량 익절 여부 문의
                elif pos.partial_sold and not pos.partial_sold_2 and pnl_pct >= _core_partial2 and pos.shares > 0:
                    if decision is None:
                        if self.gemini:
                            self._trigger_ai_partial_exit(pos, ticker, pos.name, price, avg, pnl_pct, regime)
                            with self.lock: pos.status = f"AI 익절 검토 중 ({pnl_pct:+.1f}%) 🤖"
                        else:
                            with self.lock: pos.ai_exit_decision = "SELL_ALL"
                    elif decision == "HOLD":
                        with self.lock:
                            pos.status = f"AI 홀드 ({pnl_pct:+.1f}%) ⏳"
                    else:
                        q   = pos.shares
                        self._sell(ticker, pos.name, q, price)
                        pnl = _net_profit_usd(price, avg, q)
                        with self.lock:
                            pos.shares            = 0.0
                            pos.partial_sold_2    = True
                            pos.ai_exit_decision  = None
                            pos.status            = f"코어 2차익절({pnl_pct:+.1f}%) ✅"
                        self._record_pnl(pnl)
                        self.add_log(f"✅ 코어 2차익절(전량) {pos.name} | PnL ${pnl:+.0f}")
                        self._tg(f"✅ [US 코어 전량익절] {pos.name} | +{pnl_pct:.1f}% | ${pnl:+,.0f}")

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
        # HOLD 상태일 때: 새 고점(+1%) 도달 전까지 재요청 안 함
        if getattr(pos, 'ai_exit_decision', None) == "HOLD" and asked > 0:
            if price < asked * 1.01:
                return
        pos.ai_exit_pending     = True
        pos.ai_exit_asked_price = price  # 현재 문의 가격 기록

        def _worker():
            try:
                _news = self._fetch_us_news([ticker])
                decision = self.gemini.ai_partial_exit(
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

    def _screen_satellites(self):
        today = _now_et().strftime("%Y-%m-%d")
        if self.last_screen_date == today:
            return

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
            self._inject_user_satellites()
            self.last_screen_date = today
            return

        # ── 빈 슬롯만 새로 채움 ──────────────────────────────────────
        holding = strong_keep_tickers | {t for t, p in self.satellite_positions.items() if p.shares > 0}
        self.add_log(f"🔍 미국 위성 종목 스캔 시작… (빈 슬롯 {slots_needed}개)")

        # KIS API 있으면 실시간 랭킹 스크리너 우선 시도
        candidates: list = []
        if self.kis_overseas:
            try:
                self.add_log("📡 KIS 랭킹 API 스크리너 시도…")
                candidates = scan_us_satellites_kis(
                    kis_api = self.kis_overseas,
                    n       = slots_needed * 2 + 2,
                    exclude = holding,
                )
                if candidates:
                    self.add_log(f"✅ KIS 스크리너: {len(candidates)}개 후보 수집")
            except Exception as _e:
                logger.warning(f"[US봇] KIS 스크리너 오류: {_e}")
                candidates = []

        # KIS 실패 or 결과 없으면 yfinance 폴백
        if not candidates:
            self.add_log("📈 yfinance 스크리너로 폴백…")
            candidates = scan_us_satellites(n=slots_needed * 2 + 2, exclude=holding)

        if not candidates:
            self.add_log("⚠️ 스캔 결과 없음 — 기존 위성 유지")
            self.satellite_info   = strong_keep_info + [i for i in self.satellite_info
                                                        if i["ticker"] not in strong_keep_tickers]
            self._inject_user_satellites()
            self.last_screen_date = today
            return

        # 블랙리스트(당일 AI 거절) 종목 제거
        candidates = [c for c in candidates if c["ticker"] not in self._satellite_rejects]

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
        if self.gemini and filtered:
            try:
                ai_result = self.gemini.ai_select_us_satellites(
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
        self._inject_user_satellites()
        self.hot_sectors      = list({c["sector"] for c in self.satellite_info if c.get("sector")})
        self.last_screen_date = today

    # ─────────────────────────────────────────────────────────────────
    # 위성 관리 (매수 + 청산 조건)
    # ─────────────────────────────────────────────────────────────────

    def _manage_satellites(self):
        if not self.kis_overseas:
            return

        import pandas as pd
        # 총자산 기준 예산 산정 — 코어와 동일하게 수익 복리 효과 적용
        total_usd      = self._get_total_assets_usd()
        sat_budget_per = (total_usd * self.SAT_RATIO) / max(1, self.num_satellites)
        regime         = self.market_regime

        # ── 미보유 후보 매수 ─────────────────────────────────────────
        for info in self.satellite_info:
            ticker = info["ticker"]
            pos    = self.satellite_positions.get(ticker)
            if pos and pos.shares > 0:
                continue
            if ticker in self._satellite_rejects:
                continue
            if pos and (time.time() - pos.last_order_time < self.ORDER_COOLDOWN):
                continue
            price = self._price(ticker)
            if price <= 0 or self.cash_usd < sat_budget_per * 0.3:
                continue

            # OHLCV 조회
            try:
                import yfinance as yf
                df_raw = yf.download(ticker, period="120d", interval="1d",
                                     progress=False, auto_adjust=True)
                if hasattr(df_raw.columns, "get_level_values"):
                    df_raw.columns = df_raw.columns.get_level_values(0)
                df_raw = df_raw.dropna(subset=["Close"])
                df_raw.columns = [c.lower() for c in df_raw.columns]
            except Exception:
                df_raw = None

            # ── 통합 진입 점수 체크 ────────────────────────────────
            momentum_20d = info.get("momentum_20d", 0.0)
            if df_raw is not None and not df_raw.empty:
                entry_score, entry_reasons = calculate_entry_score(
                    df_raw, price, regime, momentum_20d=momentum_20d
                )
            else:
                entry_score, entry_reasons = 0, []

            entry_threshold = get_entry_threshold(regime, 'satellite')

            # ── BULL 국면 진입 완화 ─────────────────────────────────
            # 조건 A: RSI ≤ 65 + bull_score ≥ 1
            # 조건 B: MA5 > MA20 정배열 + 현재가 MA5 이내(2%) 눌림목
            if entry_score < entry_threshold and regime == "BULL" and df_raw is not None and not df_raw.empty:
                try:
                    _closes_b = df_raw['close'].dropna()
                    _rsi_bull = float(calc_rsi(_closes_b).iloc[-1])
                    _bull_sc, _ = get_bull_momentum_score(df_raw)
                    _bull_cond_a = (_rsi_bull <= 65) and (_bull_sc >= 1)
                    _bull_cond_b = False
                    if len(_closes_b) >= 22:
                        _ma5_b  = float(_closes_b.rolling(5).mean().iloc[-1])
                        _ma20_b = float(_closes_b.rolling(20).mean().iloc[-1])
                        _bull_cond_b = (_ma5_b > _ma20_b) and (price <= _ma5_b * 1.02)
                    if _bull_cond_a or _bull_cond_b:
                        entry_score = entry_threshold
                        _why = (f"RSI={_rsi_bull:.1f} bull_score={_bull_sc}" if _bull_cond_a
                                else f"MA5눌림목(MA5={_closes_b.rolling(5).mean().iloc[-1]:.2f})")
                        self.add_log(f"🚀 [BULL 위성 진입] {ticker} {_why} → 점수 완화 진입")
                except Exception:
                    pass

            if entry_score < entry_threshold:
                if pos is None:
                    self.satellite_positions[ticker] = USPosition(
                        ticker=ticker, name=info["name"], budget_usd=sat_budget_per,
                        status=f"진입 점수 부족 ({entry_score}/{entry_threshold}pt) ⏳"
                    )
                else:
                    with self.lock:
                        pos.status = f"진입 점수 부족 ({entry_score}/{entry_threshold}pt) ⏳"
                continue

            budget_ratio = get_budget_ratio_from_score(entry_score, entry_threshold)
            # ── BEAR 국면: 진입 점수가 충분해도 포지션 크기 50% 제한 ──
            if regime == "BEAR":
                if entry_score < entry_threshold + 3:
                    with self.lock if pos else (lambda: None)():
                        if pos:
                            pos.status = f"BEAR 보류 — 점수 부족 ({entry_score}/{entry_threshold+3}pt) 🐻"
                    continue
                budget_ratio = min(budget_ratio * 0.50, 0.50)

            # ── 실적 발표 D-3 이내 → 진입 차단 (깜짝 손실 방지) ───────
            try:
                _cal = yf.Ticker(ticker).calendar
                if _cal is not None and not _cal.empty:
                    _earn_col = next((c for c in _cal.columns if 'Earnings' in str(c)), None)
                    if _earn_col:
                        import datetime as _dt
                        _earn_date = _cal[_earn_col].dropna()
                        if len(_earn_date) > 0:
                            _days_to_earn = (_earn_date.iloc[0].date() - _dt.date.today()).days
                            if 0 <= _days_to_earn <= 3:
                                _msg = f"실적발표 D-{_days_to_earn} 진입 차단 ({_earn_date.iloc[0].date()})"
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

            actual_budget = min(sat_budget_per * budget_ratio, self.cash_usd)

            # ── AI 매수 승인 심사 ────────────────────────────────────
            if self.gemini:
                try:
                    # 종목 뉴스 헤드라인 fetch (yfinance, 무료)
                    _news_str = self._fetch_us_news([ticker])
                    # 52주 신고가 정보 ai_reason에 포함
                    _full_ai_reason = info.get("ai_reason", "")
                    if _52w_note:
                        _full_ai_reason = f"{_full_ai_reason} | {_52w_note}".strip(" |")
                    approved, ai_reason = self.gemini.ai_approve_us_trade(
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
                        self._satellite_rejects[ticker] = ai_reason
                        self.add_log(f"🤖 AI 매수 거절: {info['name']}({ticker}) — {ai_reason[:80]}")
                        self._tg(
                            f"🛑 <b>[US 위성 매수 거절]</b>  {self.alert_icon}\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"📌 <b>{info['name']}</b>  {ticker}\n"
                            f"🤖 {ai_reason[:100]}\n"
                            f"➡️ 당일 블랙리스트 등록\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"⏰ {_now_et().strftime('%H:%M ET')}"
                        )
                        continue
                    self.add_log(f"🤖 AI 매수 승인: {info['name']}({ticker}) | 점수 {entry_score}pt")
                except Exception as e:
                    logger.warning(f"[US봇] AI 승인 심사 오류 ({ticker}): {e} — 알고리즘 신호 허용")

            qty = self._buy(ticker, info["name"], actual_budget, price)
            if qty > 0:
                score_str = " | ".join(entry_reasons[:3])
                with self.lock:
                    self.satellite_positions[ticker] = USPosition(
                        ticker         = ticker,
                        name           = info["name"],
                        shares         = float(qty),
                        avg_price_usd  = price,
                        budget_usd     = sat_budget_per,
                        status         = f"보유 중 🛰️ ({entry_score}pt)",
                        last_order_time= time.time(),
                        max_price_usd  = price,
                    )
                self.add_log(f"🛰️ 위성 매수 {info['name']}({ticker}) {qty}주 @ ${price:.2f} | {entry_score}pt [{score_str}]")
                self._tg(
                    f"🛰️ [US 위성 매수] {info['name']} ({ticker})\n"
                    f"@ ${price:.2f}  점수 {entry_score}pt  섹터: {info.get('sector','')}"
                )

        # ── 보유 중 청산 조건 체크 (ATR 기반 — KR 동일) ─────────────
        for ticker, pos in list(self.satellite_positions.items()):
            if pos.shares <= 0:
                continue
            price = self._price(ticker)
            if price <= 0:
                continue

            # OHLCV 조회
            try:
                import yfinance as yf
                df_raw = yf.download(ticker, period="60d", interval="1d",
                                     progress=False, auto_adjust=True)
                if hasattr(df_raw.columns, "get_level_values"):
                    df_raw.columns = df_raw.columns.get_level_values(0)
                df_raw = df_raw.dropna(subset=["Close"])
                df_raw.columns = [c.lower() for c in df_raw.columns]
            except Exception:
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

            # ③ 1차 부분 익절 (+10%(일반) / +15%(BULL)) — AI 판단
            # BULL 장에서는 추세 지속 가능성이 높아 익절 기준 상향
            _sat_partial1 = 15.0 if regime == "BULL" else self.PARTIAL1_PCT
            _sat_partial2 = 30.0 if regime == "BULL" else self.PARTIAL2_PCT
            if not pos.partial_sold and pnl_pct >= _sat_partial1 and pos.shares > 1:
                decision = getattr(pos, 'ai_exit_decision', None)
                if decision is None:
                    if self.gemini:
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
                # SELL_PARTIAL 또는 SELL_ALL → 1차 익절 실행
                q   = max(1.0, pos.shares * self.PARTIAL1_QTY)
                self._sell(ticker, pos.name, q, price)
                pnl = _net_profit_usd(price, avg, q)
                with self.lock:
                    pos.shares           -= q
                    pos.partial_sold      = True
                    pos.ai_exit_decision  = None
                    pos.status            = f"1차익절({pnl_pct:+.1f}%) ✂️"
                self._record_pnl(pnl)
                self.add_log(f"✂️  1차익절 {pos.name} | PnL ${pnl:+.0f}")
                continue

            # ④ 2차 전량 익절 (+20%(일반) / +30%(BULL)) — AI 판단
            if (pos.partial_sold and not pos.partial_sold_2
                    and pnl_pct >= _sat_partial2 and pos.shares > 0):
                decision = getattr(pos, 'ai_exit_decision', None)
                if decision is None:
                    if self.gemini:
                        self._trigger_ai_partial_exit(pos, ticker, pos.name, price, avg, pnl_pct, regime)
                        with self.lock: pos.status = f"AI 익절 검토 중 ({pnl_pct:+.1f}%) 🤖"
                    else:
                        with self.lock: pos.ai_exit_decision = "SELL_ALL"
                    continue
                if decision == "HOLD":
                    with self.lock:
                        pos.ai_exit_hold_until = time.time() + 300
                        pos.ai_exit_decision   = None
                        pos.status = f"AI 홀드 ({pnl_pct:+.1f}%) ⏳"
                    continue
                # SELL_PARTIAL 또는 SELL_ALL → 2차 전량 익절 실행
                q   = pos.shares
                self._sell(ticker, pos.name, q, price)
                pnl = _net_profit_usd(price, avg, q)
                with self.lock:
                    pos.shares            = 0.0
                    pos.partial_sold_2    = True
                    pos.ai_exit_decision  = None
                    pos.status            = f"2차익절({pnl_pct:+.1f}%) ✅"
                self._record_pnl(pnl)
                self.add_log(f"✅ 2차익절(전량) {pos.name} | PnL ${pnl:+.0f}")
                self._tg(f"✅ [US 위성 전량익절] {pos.name} | +{pnl_pct:.1f}% | ${pnl:+,.0f}")
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

    def _close_sat(self, ticker: str, pos: USPosition, price: float, reason: str):
        """위성 전량 청산"""
        shares   = pos.shares
        proceeds = self._sell(ticker, pos.name, shares, price)
        pnl      = _net_profit_usd(price, pos.avg_price_usd, shares)
        with self.lock:
            pos.shares = 0.0
            pos.status = f"청산: {reason}"
        self._record_pnl(pnl)
        icon = "🔴" if pnl < 0 else "🟢"
        self.add_log(f"{icon} 청산 {pos.name}({ticker}) | {reason} | PnL ${pnl:+.0f}")
        self._tg(
            f"{icon} [US 위성 청산] {pos.name}\n"
            f"사유: {reason}\n손익: ${pnl:+,.0f}"
        )

    # ─────────────────────────────────────────────────────────────────
    # 메인 루프
    # ─────────────────────────────────────────────────────────────────

    def _run_loop(self, total_cash: float):
        self.add_log("🚀 US 실전 봇 루프 시작")
        if not self.kis_overseas:
            self.add_log("⚠️ KIS API 미설정 — 계좌 설정에서 API 키를 입력하세요")

        # 초기 자금: KIS 잔고 우선 동기화
        if self.kis_overseas:
            self._sync_balance_from_kis()

        _save_interval    = 300
        _bal_interval     = 300
        _regime_interval  = 3600   # 1시간마다 시장 국면 갱신
        _REPORT_SLOT      = "16:10"
        _last_save_ts     = 0.0
        _last_bal_ts      = 0.0
        _last_regime_ts   = 0.0

        while self.is_running:
            try:
                now          = _now_et()
                cur_time_str = now.strftime("%H:%M")
                today_str    = now.strftime("%Y-%m-%d")

                # ── 장 밖이면 대기 ────────────────────────────────────
                if not _is_us_market_open():
                    h, m = now.hour, now.minute
                    api_hint = "" if self.kis_overseas else " (⚠️ KIS 미연결)"
                    self.add_log(
                        f"💤 장 외 시간 ({now.strftime('%a %H:%M ET')})"
                        f" — 09:30 개장 대기 중{api_hint}"
                    )
                    # 장 시작 전 → 종목 사전 스캔
                    if h < 9 or (h == 9 and m < 30):
                        self._screen_satellites()
                        self._screen_cores()
                    time.sleep(300)
                    continue

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
                    if not already and self.gemini:
                        self._run_threaded(lambda: self.generate_daily_report(_REPORT_SLOT))

                # ── 종목 스크리닝 (코어: 주 1회 / 위성: 하루 1회) ────
                self._screen_cores()
                self._screen_satellites()

                # ── 코어·위성 매매 (KIS 연결 시에만) ────────────────
                if self.kis_overseas:
                    self._manage_cores()
                    self._manage_satellites()
                else:
                    self.add_log("🔍 스캔 완료 — KIS API 미연결로 매매 건너뜀")

                # ── 상태 저장 (5분마다) ───────────────────────────────
                if time.time() - _last_save_ts >= _save_interval:
                    self._save_state()
                    _last_save_ts = time.time()

                time.sleep(60)

            except Exception as e:
                logger.error(f"[US봇] 루프 오류: {e}", exc_info=True)
                time.sleep(30)

        self._save_state()
        self.add_log("⏹️ US 봇 루프 종료")

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
            self.hot_sectors = merged

            up_str   = ", ".join(trend_hot)  or "없음"
            down_str = ", ".join(trend_cold) or "없음"
            self.add_log(f"🏭 섹터 추세 — 상승: [{up_str}]  하락: [{down_str}]")
        except Exception as e:
            logger.debug(f"[US봇] 섹터 추세 분석 실패: {e}")

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
                gemini_client    = self.gemini,
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
                "cores": {
                    t: {
                        "name":             p.name,
                        "shares":           p.shares,
                        "avg_price_usd":    p.avg_price_usd,
                        "budget_usd":       p.budget_usd,
                        "partial_sold":     p.partial_sold,
                        "partial_sold_2":   p.partial_sold_2,
                        "max_price_usd":    p.max_price_usd,
                        "status":           p.status,
                        "second_buy_price": p.second_buy_price,
                        "second_buy_cash":  p.second_buy_cash,
                        "second_buy_done":  p.second_buy_done,
                    }
                    for t, p in self.core_positions.items()
                },
                "satellites": {
                    t: {
                        "name":           p.name,
                        "shares":         p.shares,
                        "avg_price_usd":  p.avg_price_usd,
                        "budget_usd":     p.budget_usd,
                        "partial_sold":   p.partial_sold,
                        "partial_sold_2": p.partial_sold_2,
                        "max_price_usd":  p.max_price_usd,
                        "status":         p.status,
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
            self.core_info              = state.get("core_info", [])
            self.last_core_screen_date  = state.get("last_core_screen_date")
            self.satellite_info         = state.get("satellite_info", [])
            self.hot_sectors            = state.get("hot_sectors", [])
            self.daily_pnl              = state.get("daily_pnl", {})
            self.daily_report           = state.get("daily_report", None)
            self.last_screen_date       = state.get("last_screen_date")
            self.futures_snapshot       = state.get("futures_snapshot", {})
            self.sector_trends          = state.get("sector_trends", [])
            # 당일 블랙리스트 복원 (같은 날 재시작 시에만)
            saved_bl_date = state.get("bl_date", "")
            today_str_us  = _now_et().strftime("%Y-%m-%d")
            if saved_bl_date == today_str_us:
                self._bl_date           = saved_bl_date
                self._satellite_rejects = state.get("satellite_rejects", {})
                n_rej = len(self._satellite_rejects)
                if n_rej:
                    self.add_log(f"🚫 [US] 당일 AI 거절 블랙리스트 복원: {n_rej}개 종목 재심사 제외")
            for t, s in state.get("cores", {}).items():
                pos = USPosition(
                    ticker         = t,
                    name           = s.get("name", t),
                    shares         = float(s.get("shares", 0)),
                    avg_price_usd  = float(s.get("avg_price_usd", 0)),
                    budget_usd     = float(s.get("budget_usd", 0)),
                    partial_sold   = bool(s.get("partial_sold", False)),
                    partial_sold_2 = bool(s.get("partial_sold_2", False)),
                    max_price_usd  = float(s.get("max_price_usd", 0)),
                    status         = s.get("status", "코어 보유 💎"),
                )
                pos.second_buy_price = float(s.get("second_buy_price", 0.0))
                pos.second_buy_cash  = float(s.get("second_buy_cash",  0.0))
                pos.second_buy_done  = bool(s.get("second_buy_done",   False))
                self.core_positions[t] = pos
            for t, s in state.get("satellites", {}).items():
                self.satellite_positions[t] = USPosition(
                    ticker         = t,
                    name           = s.get("name", t),
                    shares         = float(s.get("shares", 0)),
                    avg_price_usd  = float(s.get("avg_price_usd", 0)),
                    budget_usd     = float(s.get("budget_usd", 0)),
                    partial_sold   = bool(s.get("partial_sold", False)),
                    partial_sold_2 = bool(s.get("partial_sold_2", False)),
                    max_price_usd  = float(s.get("max_price_usd", 0)),
                    status         = s.get("status", "보유 중 🛰️"),
                )
            self.add_log("📂 이전 상태 복원 완료")
        except Exception as e:
            logger.warning(f"[US봇] 상태 복원 실패: {e}")

    # ─────────────────────────────────────────────────────────────────
    # 공개 인터페이스 (KRBotController 호환)
    # ─────────────────────────────────────────────────────────────────

    def reload_api_keys(self, kis_config, telegram_config, gemini_config, core_stocks):
        """API 키 갱신 (설정 저장 시 호출)"""
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
                    "strategy":   next(
                        (i["sector"] for i in self.satellite_info if i["ticker"] == t),
                        "US 모멘텀",
                    ),
                    "shares":     int(pos.shares),
                    "price":      round(sp_usd * fx),
                    "value":      round(val_usd * fx),
                    "avg_price":  round(avg_p * fx),
                    "budget":     round(pos.budget_usd * fx),
                    "status":     pos.status,
                    "status_msg": f"${sp_usd:.2f} | {pnl_pct:+.1f}%",
                })

            # 표시 외 보유 종목도 총액에 합산
            for t, pos in self.satellite_positions.items():
                if t not in {s["ticker"] for s in satellites} and pos.shares > 0:
                    sp_usd = self._price_cache.get(t, pos.avg_price_usd)
                    total_sat_usd += pos.shares * sp_usd

            # ── 총 평가금액 ───────────────────────────────────────────
            total_usd   = self.cash_usd + total_sat_usd
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
                "cores":            [],
                "satellites":       satellites,
                "momentum_list":    [],
                "defensive_list":   [],
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
