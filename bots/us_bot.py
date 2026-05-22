"""
bots/us_bot.py — 미국장 페이퍼 트레이딩 봇
──────────────────────────────────────────────
- yfinance 기반 실시간 가격 (1분 캐시)
- 가상 주문 체결 (페이퍼 트레이딩 — 브로커 API 불필요)
- 미국 동부 시간(ET) 장 운영 시간 체크 (09:30 ~ 16:00)
- 코어(SPY) + 위성(US 모멘텀 종목 N개) 구조
- BaseBot 과 동일한 공개 인터페이스: start / stop / get_status / get_pnl_data
"""

import threading
import time
import logging
import collections
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import yfinance as yf

from database import (
    update_bot_status,
    save_portfolio_state,
    load_portfolio_state,
    get_user_initial_cash,
    set_user_initial_cash,
    log_trade_journal,
    get_sector_guide,
)
from telegram_bot import TelegramNotifier
from us_screener import scan_us_satellites, get_us_prices_batch, get_us_ohlcv

logger = logging.getLogger('lassi_bot')

# ── 미국 동부 시간 (EDT = UTC-4, 서머타임 기준) ─────────────────────
_ET = timezone(timedelta(hours=-4))

def _now_et() -> datetime:
    return datetime.now(_ET)

def _is_us_market_open() -> bool:
    """미국 정규장 여부 (ET 09:30 ~ 16:00, 평일만)"""
    now = _now_et()
    if now.weekday() >= 5:          # 토/일
        return False
    t_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    t_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return t_open <= now < t_close

# ── USD/KRW 환율 캐시 (60초) ────────────────────────────────────────
_usd_krw_cache: dict = {"rate": 1400.0, "ts": 0.0}
_usd_krw_lock  = threading.Lock()

def _get_fx_rate() -> float:
    """USD/KRW 환율 (60초 서버 캐시)"""
    with _usd_krw_lock:
        if time.time() - _usd_krw_cache["ts"] < 60:
            return _usd_krw_cache["rate"]
    try:
        hist = yf.Ticker("USDKRW=X").history(period="5d").dropna(subset=["Close"])
        if not hist.empty:
            rate = float(hist["Close"].iloc[-1])
            with _usd_krw_lock:
                _usd_krw_cache["rate"] = rate
                _usd_krw_cache["ts"]   = time.time()
            return rate
    except Exception:
        pass
    return _usd_krw_cache["rate"]

# ── 포지션 데이터 ─────────────────────────────────────────────────────
@dataclass
class USPosition:
    ticker:        str
    name:          str
    shares:        float = 0.0
    avg_price_usd: float = 0.0   # 평균 단가 (USD)
    budget_usd:    float = 0.0   # 배정 예산 (USD)
    partial_sold:  bool  = False  # +15% 1차 익절 완료
    partial_sold_2:bool  = False  # +30% 2차 익절 완료
    status:        str   = "감시 중 👀"
    last_order_time: float = 0.0
    max_price_usd: float = 0.0   # 고점 추적 (trailing stop)

# ── 매도 수수료 ───────────────────────────────────────────────────────
_US_FEE = 0.0   # Alpaca 무수수료; Interactive Brokers 쓰면 0.0005 정도

def _net_profit_usd(sell_p: float, avg_p: float, shares: float) -> float:
    return (sell_p * (1 - _US_FEE) - avg_p) * shares


# ══════════════════════════════════════════════════════════════════════
class USBotController:
    """미국장 페이퍼 트레이딩 봇 (BaseBot 호환 인터페이스)"""

    US_CORE_TICKER = "SPY"
    US_CORE_NAME   = "S&P 500 ETF (SPY)"
    CORE_RATIO     = 0.40    # 코어 40%
    SAT_RATIO      = 0.40    # 위성 40%  (나머지 20% = 현금 버퍼)
    ORDER_COOLDOWN = 300     # 연속 주문 방지 (초)
    STOP_LOSS_PCT  = -12.0   # 하드 손절 (%)
    TRAIL_DROP_PCT = -8.0    # ATR trailing stop: 고점 대비 (%)
    PARTIAL1_PCT   = 15.0    # 1차 익절 기준 (%)
    PARTIAL1_QTY   = 0.30    # 1차 익절 비율
    PARTIAL2_PCT   = 30.0    # 2차 익절 기준 (%)
    PARTIAL2_QTY   = 0.50    # 2차 익절 비율

    def __init__(self, user_id, kis_config=None, telegram_config=None, core_stocks=None):
        self.user_id    = user_id
        self._is_mock   = True    # DB 호환: US 봇은 항상 is_mock=True
        self.is_running = False
        self.thread     = None
        self.lock       = threading.RLock()
        self.logs: collections.deque = collections.deque(maxlen=100)

        # ── 포트폴리오 ─────────────────────────────────────────────
        self.core_ticker      = self.US_CORE_TICKER
        self.core_name        = self.US_CORE_NAME
        self.num_satellites   = 3
        self.core_shares      = 0.0
        self.core_avg_usd     = 0.0
        self.core_cash_usd    = 0.0   # 코어에 배정된 현금 (아직 미매수분)
        self.core_budget_usd  = 0.0
        self.core_status      = "감시 중 👀"
        self.satellite_positions: dict[str, USPosition] = {}
        self.satellite_info: list = []
        self.hot_sectors:    list = []

        # ── 현금 ───────────────────────────────────────────────────
        self.cash_usd         = 0.0   # 가용 현금 (USD)
        self.initial_cash_usd = 0.0   # 투자 원금 (USD)

        # ── PnL ────────────────────────────────────────────────────
        self.daily_pnl: dict = {}   # {YYYY-MM-DD: usd_pnl}

        # ── 스크리닝 ───────────────────────────────────────────────
        self.last_screen_date = None
        self._last_screen_ts  = 0.0

        # ── 가격 캐시 ──────────────────────────────────────────────
        self._price_cache:    dict  = {}
        self._last_price_ts:  float = 0.0
        self._price_ttl:      int   = 60   # 1분

        # ── 텔레그램 ──────────────────────────────────────────────
        self.telegram = None
        if telegram_config and telegram_config.get("token"):
            try:
                self.telegram = TelegramNotifier(
                    token=telegram_config["token"].strip(),
                    chat_id=(telegram_config.get("chat_id") or "").strip(),
                )
            except Exception:
                pass

        # ── BaseBot 호환 필드 ─────────────────────────────────────
        self.kis             = True    # 페이퍼 트레이딩 = 항상 준비됨
        self.cached_balance  = None
        self.live_prices     = {}
        self.market_regime   = "NEUTRAL"
        self.gemini          = None
        self.sector_guide    = get_sector_guide(user_id) or ""

        # 상태 복원
        self._restore_state()
        self.add_log("🇺🇸 미국장 페이퍼 트레이딩 봇 초기화 완료")

        # ── 백그라운드 가격 갱신 루프 (UI 응답 속도 분리) ────────────
        # get_status()에서 yfinance를 직접 호출하지 않도록
        # 별도 데몬 스레드에서 60초마다 가격을 캐시에 미리 채워둠
        self._sync_thread = threading.Thread(
            target=self._perpetual_price_sync, daemon=True
        )
        self._sync_thread.start()

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

    # ─────────────────────────────────────────────────────────────────
    # 가격 조회
    # ─────────────────────────────────────────────────────────────────

    def _refresh_prices(self, tickers=None):
        """보유 종목(+ 지정 종목) 가격 일괄 갱신"""
        if tickers is None:
            tickers = set()
        tickers = set(tickers)
        tickers.add(self.core_ticker)
        for t in self.satellite_positions:
            tickers.add(t)
        for info in self.satellite_info:
            tickers.add(info["ticker"])

        new_prices = get_us_prices_batch(tickers)
        with self.lock:
            self._price_cache.update(new_prices)
            self._last_price_ts = time.time()
        return new_prices

    def _perpetual_price_sync(self):
        """백그라운드 가격 갱신 루프 — 60초마다 캐시를 채워둠."""
        while True:
            try:
                self._refresh_prices()
            except Exception as e:
                logger.debug(f"[US봇] 가격 동기화 오류: {e}")
            time.sleep(60)

    def _price(self, ticker: str) -> float:
        """캐시된 가격 반환 (캐시에 없으면 0 반환 — 블로킹 없음)"""
        return self._price_cache.get(ticker, 0.0)

    # ─────────────────────────────────────────────────────────────────
    # 페이퍼 주문 (즉시 시장가 체결 시뮬레이션)
    # ─────────────────────────────────────────────────────────────────

    def _buy(self, ticker: str, name: str, budget_usd: float, price: float = 0) -> int:
        """페이퍼 매수. 체결 주수 반환 (0 = 실패/현금 부족)"""
        price = price or self._price(ticker)
        if price <= 0 or budget_usd <= 0:
            return 0
        with self.lock:
            avail = min(budget_usd, self.cash_usd)
            qty   = int(avail / price)
            if qty <= 0:
                return 0
            cost = qty * price
            self.cash_usd -= cost
        self.add_log(f"📥 BUY  {name}({ticker}) {qty}주 @ ${price:.2f}  (${cost:,.0f})")
        return qty

    def _sell(self, ticker: str, name: str, shares: float, price: float = 0) -> float:
        """페이퍼 매도. 체결 대금(USD) 반환"""
        price = price or self._price(ticker)
        if price <= 0 or shares <= 0:
            return 0.0
        proceeds = shares * price * (1 - _US_FEE)
        with self.lock:
            self.cash_usd += proceeds
        self.add_log(f"📤 SELL {name}({ticker}) {shares:.0f}주 @ ${price:.2f}  (${proceeds:,.0f})")
        return proceeds

    # ─────────────────────────────────────────────────────────────────
    # 손익 기록
    # ─────────────────────────────────────────────────────────────────

    def _record_pnl(self, usd_pnl: float):
        today = _now_et().strftime("%Y-%m-%d")
        self.daily_pnl[today] = self.daily_pnl.get(today, 0.0) + usd_pnl
        try:
            fx = _get_fx_rate()
            log_trade_journal(self.user_id, today, round(usd_pnl * fx))
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────
    # 초기 자금 설정
    # ─────────────────────────────────────────────────────────────────

    def _init_cash_from_krw(self, total_krw: float):
        """KRW → USD 환산 후 포트폴리오 예산 배분"""
        fx = _get_fx_rate()
        total_usd = total_krw / fx
        self.initial_cash_usd = total_usd
        self.core_budget_usd  = total_usd * self.CORE_RATIO
        self.core_cash_usd    = self.core_budget_usd
        # 위성 예산 + 현금 버퍼
        self.cash_usd = total_usd * (1 - self.CORE_RATIO)
        self.add_log(
            f"💵 초기 자금 ${total_usd:,.0f}"
            f" (코어 ${self.core_budget_usd:,.0f}"
            f" / 위성+버퍼 ${self.cash_usd:,.0f})"
        )

    # ─────────────────────────────────────────────────────────────────
    # 위성 스크리닝 (하루 1회, 장 시작 후 최초 1회)
    # ─────────────────────────────────────────────────────────────────

    def _screen_satellites(self):
        today = _now_et().strftime("%Y-%m-%d")
        if self.last_screen_date == today:
            return
        # 이미 보유 중인 종목은 제외 (교체 방지)
        holding = {t for t, p in self.satellite_positions.items() if p.shares > 0}
        exclude = {self.core_ticker} | holding
        self.add_log("🔍 미국 위성 종목 스캔 시작…")
        candidates = scan_us_satellites(n=self.num_satellites * 2, exclude=exclude)
        if not candidates:
            self.add_log("⚠️ 스캔 결과 없음 — 기존 위성 유지")
            return

        # 섹터 다양성: 같은 섹터 최대 2개
        seen_sec: dict = {}
        filtered: list = []
        for c in candidates:
            s = c["sector"]
            seen_sec[s] = seen_sec.get(s, 0) + 1
            if seen_sec[s] <= 2:
                filtered.append(c)

        self.satellite_info = filtered[:self.num_satellites]
        self.hot_sectors    = list({c["sector"] for c in self.satellite_info})
        self.last_screen_date = today
        names = [f"{c['ticker']}(점수:{c['score']:.0f})" for c in self.satellite_info]
        self.add_log(f"✅ 위성 종목 선정: {', '.join(names)}")

    # ─────────────────────────────────────────────────────────────────
    # 코어 관리 (SPY 장기 보유)
    # ─────────────────────────────────────────────────────────────────

    def _manage_core(self):
        price = self._price(self.core_ticker)
        if price <= 0:
            return
        # 미보유 → 코어 예산으로 매수
        if self.core_shares <= 0 and self.core_cash_usd >= price:
            qty = self._buy(self.core_ticker, self.core_name, self.core_cash_usd, price)
            if qty > 0:
                cost = qty * price
                with self.lock:
                    self.core_avg_usd    = price
                    self.core_shares     = float(qty)
                    self.core_cash_usd   = max(0.0, self.core_cash_usd - cost)
                    self.core_status     = "코어 보유 중 💎"
                self._tg(f"🇺🇸 [코어 매수] {self.core_name} {qty}주 @ ${price:.2f}")

    # ─────────────────────────────────────────────────────────────────
    # 위성 관리 (매수 + 청산 조건 체크)
    # ─────────────────────────────────────────────────────────────────

    def _manage_satellites(self):
        sat_budget_per = (
            self.initial_cash_usd * self.SAT_RATIO
            / max(1, self.num_satellites)
        )

        # ── 미보유 후보 매수 ───────────────────────────────────────
        for info in self.satellite_info:
            ticker = info["ticker"]
            pos    = self.satellite_positions.get(ticker)
            if pos and pos.shares > 0:
                continue   # 이미 보유
            price = self._price(ticker)
            if price <= 0 or self.cash_usd < sat_budget_per * 0.5:
                continue
            qty = self._buy(ticker, info["name"], sat_budget_per, price)
            if qty > 0:
                with self.lock:
                    self.satellite_positions[ticker] = USPosition(
                        ticker=ticker,
                        name=info["name"],
                        shares=float(qty),
                        avg_price_usd=price,
                        budget_usd=sat_budget_per,
                        status="보유 중 🛰️",
                        last_order_time=time.time(),
                        max_price_usd=price,
                    )
                self._tg(
                    f"🛰️ [위성 매수] {info['name']} ({ticker})\n"
                    f"@ ${price:.2f}  섹터: {info['sector']}"
                )

        # ── 보유 중 청산 조건 ──────────────────────────────────────
        for ticker, pos in list(self.satellite_positions.items()):
            if pos.shares <= 0:
                continue
            price = self._price(ticker)
            if price <= 0:
                continue

            # 고점 갱신
            if price > pos.max_price_usd:
                with self.lock:
                    pos.max_price_usd = price

            avg     = pos.avg_price_usd
            pnl_pct = (price / avg - 1) * 100 if avg > 0 else 0.0
            trail   = (price - pos.max_price_usd) / pos.max_price_usd * 100

            # ① 하드 손절
            if pnl_pct <= self.STOP_LOSS_PCT:
                self._close_sat(ticker, pos, price, f"손절 {pnl_pct:.1f}%")
                continue

            # ② ATR Trailing Stop (고점 대비 -8%)
            if trail <= self.TRAIL_DROP_PCT:
                self._close_sat(ticker, pos, price, f"트레일링 손절 (고점-{abs(trail):.1f}%)")
                continue

            # ③ 1차 부분 익절 (+15% → 30% 매도)
            if not pos.partial_sold and pnl_pct >= self.PARTIAL1_PCT and pos.shares > 1:
                q = max(1.0, pos.shares * self.PARTIAL1_QTY)
                self._sell(ticker, pos.name, q, price)
                pnl = _net_profit_usd(price, avg, q)
                with self.lock:
                    pos.shares      -= q
                    pos.partial_sold = True
                    pos.status       = f"30%익절({pnl_pct:+.1f}%) ✂️"
                self._record_pnl(pnl)
                self.add_log(f"✂️  부분익절30% {pos.name} | PnL ${pnl:+.0f}")
                continue

            # ④ 2차 부분 익절 (+30% → 추가 50% 매도)
            if not pos.partial_sold_2 and pnl_pct >= self.PARTIAL2_PCT and pos.shares > 1:
                q = max(1.0, pos.shares * self.PARTIAL2_QTY)
                self._sell(ticker, pos.name, q, price)
                pnl = _net_profit_usd(price, avg, q)
                with self.lock:
                    pos.shares       -= q
                    pos.partial_sold_2 = True
                    pos.status         = f"50%추가익절({pnl_pct:+.1f}%) ✂️✂️"
                self._record_pnl(pnl)
                self.add_log(f"✂️✂️ 부분익절50% {pos.name} | PnL ${pnl:+.0f}")
                continue

            # ⑤ 스크리너에서 빠진 종목 + 수익권이면 청산
            in_info = {i["ticker"] for i in self.satellite_info}
            if ticker not in in_info and pnl_pct > 0:
                self._close_sat(ticker, pos, price, f"스크리너 제외 (수익 {pnl_pct:.1f}%)")

            # 상태 업데이트
            with self.lock:
                pos.status = f"보유 중 🛰️ ({pnl_pct:+.1f}%)"

    def _close_sat(self, ticker: str, pos: USPosition, price: float, reason: str):
        """위성 전량 청산"""
        shares  = pos.shares
        self._sell(ticker, pos.name, shares, price)
        pnl     = _net_profit_usd(price, pos.avg_price_usd, shares)
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
        self.add_log("🚀 미국장 봇 루프 시작")
        # 초기 자금 설정 (복원 데이터 없을 때만)
        if self.initial_cash_usd <= 0:
            self._init_cash_from_krw(total_cash)
            set_user_initial_cash(self.user_id, total_cash, is_mock=True)

        _save_interval  = 300   # 5분마다 상태 저장
        _last_save_ts   = 0.0

        while self.is_running:
            try:
                now = _now_et()
                # ── 장 밖이면 대기 ──────────────────────────────────
                if not _is_us_market_open():
                    h, m = now.hour, now.minute
                    self.add_log(
                        f"💤 장 외 시간 ({now.strftime('%a %H:%M ET')}) "
                        f"— 09:30 개장 대기 중"
                    )
                    # 장 시작 전이면 스크리닝 미리 실행
                    if h < 9 or (h == 9 and m < 30):
                        self._screen_satellites()
                    time.sleep(300)
                    continue

                # ── 가격 갱신 (트레이딩 루프: 최신 가격 확보) ────────
                # 백그라운드 sync와 타이밍이 겹칠 수 있지만 무해함
                self._refresh_prices()

                # ── 위성 스크리닝 (하루 1회) ────────────────────────
                self._screen_satellites()

                # ── 코어 관리 ───────────────────────────────────────
                self._manage_core()

                # ── 위성 관리 ───────────────────────────────────────
                self._manage_satellites()

                # ── 상태 저장 ───────────────────────────────────────
                if time.time() - _last_save_ts >= _save_interval:
                    self._save_state()
                    _last_save_ts = time.time()

                time.sleep(60)   # 1분 간격

            except Exception as e:
                logger.error(f"[US봇] 루프 오류: {e}", exc_info=True)
                time.sleep(30)

        self._save_state()
        self.add_log("⏹️ 미국장 봇 루프 종료")

    # ─────────────────────────────────────────────────────────────────
    # 상태 저장 / 복원
    # ─────────────────────────────────────────────────────────────────

    def _save_state(self):
        try:
            state = {
                "core_shares":     self.core_shares,
                "core_avg_usd":    self.core_avg_usd,
                "core_cash_usd":   self.core_cash_usd,
                "core_budget_usd": self.core_budget_usd,
                "cash_usd":        self.cash_usd,
                "initial_cash_usd":self.initial_cash_usd,
                "satellite_info":  self.satellite_info,
                "hot_sectors":     self.hot_sectors,
                "daily_pnl":       self.daily_pnl,
                "satellites": {
                    t: {
                        "name":          p.name,
                        "shares":        p.shares,
                        "avg_price_usd": p.avg_price_usd,
                        "budget_usd":    p.budget_usd,
                        "partial_sold":  p.partial_sold,
                        "partial_sold_2":p.partial_sold_2,
                        "max_price_usd": p.max_price_usd,
                        "status":        p.status,
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
            self.core_shares      = float(state.get("core_shares", 0))
            self.core_avg_usd     = float(state.get("core_avg_usd", 0))
            self.core_cash_usd    = float(state.get("core_cash_usd", 0))
            self.core_budget_usd  = float(state.get("core_budget_usd", 0))
            self.cash_usd         = float(state.get("cash_usd", 0))
            self.initial_cash_usd = float(state.get("initial_cash_usd", 0))
            self.satellite_info   = state.get("satellite_info", [])
            self.hot_sectors      = state.get("hot_sectors", [])
            self.daily_pnl        = state.get("daily_pnl", {})
            if self.core_shares > 0:
                self.core_status = "코어 보유 중 💎"
            for t, s in state.get("satellites", {}).items():
                self.satellite_positions[t] = USPosition(
                    ticker=t,
                    name=s.get("name", t),
                    shares=float(s.get("shares", 0)),
                    avg_price_usd=float(s.get("avg_price_usd", 0)),
                    budget_usd=float(s.get("budget_usd", 0)),
                    partial_sold=bool(s.get("partial_sold", False)),
                    partial_sold_2=bool(s.get("partial_sold_2", False)),
                    max_price_usd=float(s.get("max_price_usd", 0)),
                    status=s.get("status", "보유 중 🛰️"),
                )
            self.add_log("📂 이전 상태 복원 완료")
        except Exception as e:
            logger.warning(f"[US봇] 상태 복원 실패: {e}")

    # ─────────────────────────────────────────────────────────────────
    # 공개 인터페이스 (BaseBot 호환)
    # ─────────────────────────────────────────────────────────────────

    def start(self, total_cash: float = 10_000_000) -> bool:
        if self.is_running:
            return False
        self.is_running = True
        self.thread = threading.Thread(
            target=self._run_loop, args=(total_cash,), daemon=True
        )
        self.thread.start()
        update_bot_status(self.user_id, True, is_mock=True)
        self.add_log("▶️ [US] 미국장 매매 봇 시작")
        return True

    def stop(self):
        if self.is_running:
            self.is_running = False
            update_bot_status(self.user_id, False, is_mock=True)
            if self.thread:
                self.thread.join(timeout=5)
            self._save_state()

    def get_pnl_data(self) -> dict:
        """일/주/월/년 손익 집계 (KRW 환산 반환)"""
        fx = _get_fx_rate()
        krw_pnl = {d: round(v * fx) for d, v in self.daily_pnl.items()}
        sorted_days = sorted(krw_pnl.keys())

        def _agg(keys):
            return [round(sum(krw_pnl.get(d, 0) for d in sorted_days if d.startswith(k))) for k in keys]

        # 일별
        daily_labels = sorted_days[-30:]
        daily_values = [krw_pnl[d] for d in daily_labels]

        # 주별
        weekly: dict = defaultdict(float)
        for d in sorted_days:
            try:
                dt = datetime.strptime(d, "%Y-%m-%d")
                weekly[dt.strftime("%Y-W%W")] += krw_pnl.get(d, 0)
            except Exception:
                pass
        wl = sorted(weekly.keys())[-26:]

        # 월별
        monthly: dict = defaultdict(float)
        for d in sorted_days:
            monthly[d[:7]] += krw_pnl.get(d, 0)
        ml = sorted(monthly.keys())[-24:]

        # 연별
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
        """BaseBot.get_status()와 동일한 JSON 형식 반환 (KRW 환산)"""
        try:
            fx = _get_fx_rate()
            # 가격은 백그라운드 루프(_perpetual_price_sync)가 60초마다 갱신.
            # get_status()는 캐시를 그대로 읽음 → 블로킹 없이 즉시 반환

            # ── 코어 ─────────────────────────────────────────────
            cp_usd      = self._price_cache.get(self.core_ticker, 0.0)
            core_val_usd= self.core_shares * cp_usd
            cores_data  = [{
                "name":       self.core_name,
                "ticker":     self.core_ticker,
                "shares":     int(self.core_shares),
                "floor":      0,
                "price":      round(cp_usd * fx),
                "value":      round(core_val_usd * fx),
                "avg_price":  round(self.core_avg_usd * fx),
                "budget":     round(self.core_budget_usd * fx),
                "strategy":   "S&P 500 장기 보유",
                "status":     self.core_status,
                "status_msg": (
                    f"SPY ${cp_usd:.2f}"
                    f" | {int(self.core_shares)}주 보유"
                    f" | PnL {((cp_usd/self.core_avg_usd-1)*100) if self.core_avg_usd>0 else 0:+.1f}%"
                ),
            }]

            # ── 위성 ─────────────────────────────────────────────
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

            # 전체 sat 가치 (표시 외 종목도 포함)
            for t, pos in self.satellite_positions.items():
                if t not in {s["ticker"] for s in satellites} and pos.shares > 0:
                    sp_usd = self._price_cache.get(t, pos.avg_price_usd)
                    total_sat_usd += pos.shares * sp_usd

            # ── 총 평가금액 ───────────────────────────────────────
            total_usd = (
                self.cash_usd
                + self.core_cash_usd   # 아직 미매수 코어 예산
                + core_val_usd
                + total_sat_usd
            )

            try:
                initial_krw = get_user_initial_cash(self.user_id, is_mock=True)
            except Exception:
                initial_krw = round(self.initial_cash_usd * fx)

            total_krw = round(total_usd * fx)
            pnl_krw   = total_krw - initial_krw
            pnl_rt    = (pnl_krw / initial_krw * 100) if initial_krw > 0 else 0.0

            return {
                "is_running":       self.is_running,
                "is_mock":          True,
                "has_keys":         True,   # 페이퍼 트레이딩 = 항상 준비됨
                "logs":             list(self.logs)[-30:],
                "hot_sectors":      self.hot_sectors,
                "num_satellites":   self.num_satellites,
                "cores":            cores_data,
                "satellites":       satellites,
                "momentum_list":    [],   # US 모드 = 단타 모멘텀 없음
                "defensive_list":   [],
                "market_regime":    self.market_regime,
                "mock_total_asset": total_krw,
                "mock_pnl":         pnl_krw,
                "mock_pnl_rt":      round(pnl_rt, 2),
                "initial_cash":     initial_krw,
                "available_cash":   round((self.cash_usd + self.core_cash_usd) * fx),
            }

        except Exception as e:
            logger.error(f"[US봇] get_status 오류: {e}", exc_info=True)
            return {
                "is_running": self.is_running,
                "is_mock": True,
                "has_keys": True,
                "logs": list(self.logs)[-30:],
                "hot_sectors": [],
                "num_satellites": self.num_satellites,
                "cores": [],
                "satellites": [],
                "momentum_list": [],
                "defensive_list": [],
                "market_regime": "NEUTRAL",
                "mock_total_asset": 0,
                "mock_pnl": 0,
                "mock_pnl_rt": 0,
                "initial_cash": 10_000_000,
                "available_cash": 0,
            }
