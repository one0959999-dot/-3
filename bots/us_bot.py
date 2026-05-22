"""
bots/us_bot.py — 미국장 실전 매매 봇 (KIS 해외주식 API)
──────────────────────────────────────────────────────────
- KIS 해외주식 OpenAPI 로 실제 주문 체결 (NASDAQ / NYSE)
- yfinance 는 위성 스크리닝 + 가격 캐시 보조용으로만 사용
- 미국 동부 시간(ET) 장 운영 시간 체크 (09:30 ~ 16:00)
- 위성(US 모멘텀 종목 N개) 전략
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
    get_sector_guide,
)
from telegram_bot import TelegramNotifier
from us_screener import scan_us_satellites, get_us_prices_batch, get_us_ohlcv
from kis_brokers.kis_overseas_api import KisOverseasApi

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
    """미국장 실전 매매 봇 — KIS 해외주식 API (BaseBot 호환 인터페이스)"""

    SAT_RATIO      = 0.80    # 위성 80%  (나머지 20% = 현금 버퍼)
    ORDER_COOLDOWN = 300     # 연속 주문 방지 (초)
    STOP_LOSS_PCT  = -12.0   # 하드 손절 (%)
    TRAIL_DROP_PCT = -8.0    # ATR trailing stop: 고점 대비 (%)
    PARTIAL1_PCT   = 15.0    # 1차 익절 기준 (%)
    PARTIAL1_QTY   = 0.30    # 1차 익절 비율
    PARTIAL2_PCT   = 30.0    # 2차 익절 기준 (%)
    PARTIAL2_QTY   = 0.50    # 2차 익절 비율

    def __init__(self, user_id, kis_config=None, telegram_config=None, core_stocks=None):
        self.user_id    = user_id
        self._is_mock   = True    # DB 호환: US 봇은 is_mock=True 슬롯 사용
        self.is_running = False
        self.thread     = None
        self.lock       = threading.RLock()
        self.logs: collections.deque = collections.deque(maxlen=100)

        # ── KIS 해외주식 API ───────────────────────────────────────
        self.kis_overseas: KisOverseasApi | None = None
        if (kis_config
                and kis_config.get("app_key")
                and kis_config.get("app_secret")):
            try:
                self.kis_overseas = KisOverseasApi(
                    app_key    = kis_config["app_key"].strip(),
                    app_secret = kis_config["app_secret"].strip(),
                    account_no = (kis_config.get("account_no") or "").strip(),
                )
            except Exception as e:
                logger.warning(f"[US봇] KIS 해외주식 API 초기화 실패: {e}")

        # ── 포트폴리오 ─────────────────────────────────────────────
        self.num_satellites   = 3
        self.satellite_positions: dict[str, USPosition] = {}
        self.satellite_info: list = []
        self.hot_sectors:    list = []

        # ── 현금 / 원금 추적 (KIS 잔고 기준, 로컬 캐시) ────────────
        self.cash_usd         = 0.0
        self.initial_cash_usd = 0.0

        # ── PnL ────────────────────────────────────────────────────
        self.daily_pnl: dict = {}

        # ── 스크리닝 ───────────────────────────────────────────────
        self.last_screen_date = None
        self._last_screen_ts  = 0.0

        # ── 가격 캐시 (yfinance 보조 + KIS 실시간 병행) ───────────
        self._price_cache:   dict  = {}
        self._last_price_ts: float = 0.0

        # ── 텔레그램 ──────────────────────────────────────────────
        self.telegram = None
        if telegram_config and telegram_config.get("token"):
            try:
                self.telegram = TelegramNotifier(
                    token    = telegram_config["token"].strip(),
                    chat_id  = (telegram_config.get("chat_id") or "").strip(),
                )
            except Exception:
                pass

        # ── BaseBot 호환 필드 ─────────────────────────────────────
        self.kis             = None   # KR KIS API 없음 (app.py 분기용)
        self.cached_balance  = None
        self.live_prices     = {}
        self.market_regime   = "NEUTRAL"
        self.gemini          = None
        self.sector_guide    = get_sector_guide(user_id) or ""
        self.daily_report    = None
        self.core_positions  = []
        self.fundamental_cache: dict = {}

        # 상태 복원
        self._restore_state()
        has_api = "✅ KIS 해외주식 API 연결됨" if self.kis_overseas else "⚠️ KIS API 미설정 (설정 후 재시작)"
        self.add_log(f"🇺🇸 미국장 실전 매매 봇 초기화 완료 — {has_api}")

        # ── 백그라운드 가격 갱신 (yfinance, 60초 주기) ───────────
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
        """보유 종목(+ 지정 종목) 가격 일괄 갱신.
        KIS 해외주식 API 우선, 실패 시 yfinance 폴백.
        """
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

        # 1순위: KIS 해외주식 실시간 가격
        if self.kis_overseas:
            try:
                new_prices = self.kis_overseas.get_prices_batch(list(tickers))
            except Exception as e:
                logger.debug(f"[US봇] KIS 가격 조회 실패, yfinance 폴백: {e}")

        # 2순위: yfinance 보조 (KIS 미조회 종목 보완)
        missing = tickers - set(new_prices.keys())
        if missing:
            yf_prices = get_us_prices_batch(missing)
            new_prices.update(yf_prices)

        with self.lock:
            self._price_cache.update(new_prices)
            self._last_price_ts = time.time()
        return new_prices

    def _sync_balance_from_kis(self):
        """KIS 잔고에서 실제 현금 잔액 동기화 (5분마다 호출)"""
        if not self.kis_overseas:
            return
        try:
            bal = self.kis_overseas.get_balance()
            with self.lock:
                self.cash_usd = bal["cash_usd"]
            logger.debug(f"[US봇] KIS 잔고 동기화: 현금 ${bal['cash_usd']:,.2f}")
        except Exception as e:
            logger.debug(f"[US봇] KIS 잔고 동기화 실패: {e}")

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
        """실전 매수. KIS 해외주식 시장가 주문. 체결 주수 반환 (0 = 실패)"""
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
            cost = qty * price  # 체결 추정가 (실제 체결가는 KIS 잔고에서 확인)
            with self.lock:
                self.cash_usd = max(0.0, self.cash_usd - cost)
            self.add_log(f"📥 BUY  {name}({ticker}) {qty}주 @ ${price:.2f} 추정  (${cost:,.0f})")
            return qty
        else:
            self.add_log(f"❌ BUY 주문 실패: {name}({ticker}) — KIS 응답 확인 필요")
            return 0

    def _sell(self, ticker: str, name: str, shares: float, price: float = 0) -> float:
        """실전 매도. KIS 해외주식 시장가 주문. 체결 대금(USD) 추정값 반환"""
        if not self.kis_overseas:
            self.add_log(f"⚠️ SELL 실패: KIS API 미설정 ({ticker})")
            return 0.0
        price  = price or self._price(ticker)
        qty    = int(shares)
        if price <= 0 or qty <= 0:
            return 0.0
        ok = self.kis_overseas.sell_market_order(ticker, qty)
        if ok:
            proceeds = qty * price * (1 - _US_FEE)
            with self.lock:
                self.cash_usd += proceeds
            self.add_log(f"📤 SELL {name}({ticker}) {qty}주 @ ${price:.2f} 추정  (${proceeds:,.0f})")
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
        # log_trade_journal 은 페이퍼 트레이딩에서 미사용 (US 봇은 DB 거래일지 불필요)

    # ─────────────────────────────────────────────────────────────────
    # 초기 자금 설정
    # ─────────────────────────────────────────────────────────────────

    def _init_cash_from_krw(self, total_krw: float):
        """KRW → USD 환산 후 초기 자금 설정 (KIS 잔고 미연결 시 fallback)"""
        fx = _get_fx_rate()
        total_usd = total_krw / fx
        self.initial_cash_usd = total_usd
        self.cash_usd = total_usd
        self.add_log(f"💵 초기 자금 설정 ${total_usd:,.0f} (≈ ₩{total_krw:,.0f})")

    # ─────────────────────────────────────────────────────────────────
    # 위성 스크리닝 (하루 1회, 장 시작 후 최초 1회)
    # ─────────────────────────────────────────────────────────────────

    def _screen_satellites(self):
        today = _now_et().strftime("%Y-%m-%d")
        if self.last_screen_date == today:
            return
        # 이미 보유 중인 종목은 제외 (교체 방지)
        holding = {t for t, p in self.satellite_positions.items() if p.shares > 0}
        exclude = holding
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
        self.add_log("🚀 미국장 실전 봇 루프 시작")
        if not self.kis_overseas:
            self.add_log("⚠️ KIS 해외주식 API 미설정 — 계좌 설정에서 API 키를 입력하세요")

        # 초기 자금: KIS 잔고 우선, 없으면 설정값 사용
        if self.kis_overseas and self.cash_usd <= 0:
            self._sync_balance_from_kis()
        if self.initial_cash_usd <= 0:
            self._init_cash_from_krw(total_cash)
            set_user_initial_cash(self.user_id, total_cash, is_mock=True)

        _save_interval  = 300   # 5분마다 상태 저장
        _bal_interval   = 300   # 5분마다 KIS 잔고 동기화
        _last_save_ts   = 0.0
        _last_bal_ts    = 0.0

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
                    if h < 9 or (h == 9 and m < 30):
                        self._screen_satellites()
                    time.sleep(300)
                    continue

                # ── 가격 갱신 ───────────────────────────────────────
                self._refresh_prices()

                # ── KIS 잔고 동기화 (5분마다) ───────────────────────
                if time.time() - _last_bal_ts >= _bal_interval:
                    self._sync_balance_from_kis()
                    _last_bal_ts = time.time()

                # ── 위성 스크리닝 (하루 1회) ────────────────────────
                self._screen_satellites()

                # ── 위성 관리 ───────────────────────────────────────
                self._manage_satellites()

                # ── 상태 저장 ───────────────────────────────────────
                if time.time() - _last_save_ts >= _save_interval:
                    self._save_state()
                    _last_save_ts = time.time()

                time.sleep(60)

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
                "cash_usd":         self.cash_usd,
                "initial_cash_usd": self.initial_cash_usd,
                "satellite_info":   self.satellite_info,
                "hot_sectors":      self.hot_sectors,
                "daily_pnl":        self.daily_pnl,
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
            self.cash_usd         = float(state.get("cash_usd", 0))
            self.initial_cash_usd = float(state.get("initial_cash_usd", 0))
            self.satellite_info   = state.get("satellite_info", [])
            self.hot_sectors      = state.get("hot_sectors", [])
            self.daily_pnl        = state.get("daily_pnl", {})
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

    def reload_api_keys(self, kis_config, telegram_config, gemini_config, core_stocks):
        """BaseBot 호환 인터페이스 — KIS 해외주식 API + 텔레그램 갱신."""
        # KIS 해외주식 API 갱신
        if (kis_config
                and kis_config.get("app_key")
                and kis_config.get("app_secret")):
            try:
                self.kis_overseas = KisOverseasApi(
                    app_key    = kis_config["app_key"].strip(),
                    app_secret = kis_config["app_secret"].strip(),
                    account_no = (kis_config.get("account_no") or "").strip(),
                )
                self.add_log("🔑 KIS 해외주식 API 갱신 완료")
            except Exception as e:
                self.kis_overseas = None
                self.add_log(f"⚠️ KIS 해외주식 API 갱신 실패: {e}")
        else:
            self.kis_overseas = None

        # 텔레그램 갱신
        if telegram_config and telegram_config.get("token"):
            try:
                self.telegram = TelegramNotifier(
                    token    = telegram_config["token"].strip(),
                    chat_id  = (telegram_config.get("chat_id") or "").strip(),
                )
            except Exception:
                self.telegram = None
        else:
            self.telegram = None

    def reload_news_monitor(self, dart_key: str, naver_id: str, naver_secret: str):
        """BaseBot 호환 인터페이스 — US 봇은 한국 뉴스 모니터 미사용."""
        pass

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
            total_usd = self.cash_usd + total_sat_usd

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
                "has_keys":         self.kis_overseas is not None,  # KIS API 설정 여부
                "logs":             list(self.logs)[-30:],
                "hot_sectors":      self.hot_sectors,
                "num_satellites":   self.num_satellites,
                "cores":            [],
                "satellites":       satellites,
                "momentum_list":    [],
                "defensive_list":   [],
                "market_regime":    self.market_regime,
                "mock_total_asset": total_krw,
                "mock_pnl":         pnl_krw,
                "mock_pnl_rt":      round(pnl_rt, 2),
                "initial_cash":     initial_krw,
                "available_cash":   round(self.cash_usd * fx),
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
