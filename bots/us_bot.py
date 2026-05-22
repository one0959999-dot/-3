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
    scan_us_satellites, get_us_prices_batch, generate_us_daily_report,
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


# ══════════════════════════════════════════════════════════════════════
class USBotController:
    """미국장 실전 매매 봇 — KIS 해외주식 API (KRBotController 아키텍처 기반)"""

    # ── 전략 상수 ─────────────────────────────────────────────────────
    SAT_RATIO      = 0.80    # 위성 80% (나머지 20% = 현금 버퍼)
    ORDER_COOLDOWN = 300     # 연속 주문 방지 (초)
    STOP_LOSS_PCT  = -12.0   # 하드 손절 (%)
    TRAIL_DROP_PCT = -8.0    # 트레일링 스탑: 고점 대비 (%)
    PARTIAL1_PCT   = 15.0    # 1차 익절 기준 (%)
    PARTIAL1_QTY   = 0.30    # 1차 익절 비율
    PARTIAL2_PCT   = 30.0    # 2차 익절 기준 (%)
    PARTIAL2_QTY   = 0.50    # 2차 익절 비율

    def __init__(self, user_id, kis_config=None, telegram_config=None, core_stocks=None):
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

        # ── 포트폴리오 ────────────────────────────────────────────────
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
                    self.cash_usd = cash_usd

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

    # ─────────────────────────────────────────────────────────────────
    # 위성 스크리닝 (하루 1회)
    # ─────────────────────────────────────────────────────────────────

    def _screen_satellites(self):
        today = _now_et().strftime("%Y-%m-%d")
        if self.last_screen_date == today:
            return
        holding = {t for t, p in self.satellite_positions.items() if p.shares > 0}
        self.add_log("🔍 미국 위성 종목 스캔 시작…")
        candidates = scan_us_satellites(n=self.num_satellites * 2, exclude=holding)
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

        self.satellite_info   = filtered[:self.num_satellites]
        self.hot_sectors      = list({c["sector"] for c in self.satellite_info})
        self.last_screen_date = today
        names = [f"{c['ticker']}(점수:{c['score']:.0f})" for c in self.satellite_info]
        self.add_log(f"✅ 위성 종목 선정: {', '.join(names)}")

    # ─────────────────────────────────────────────────────────────────
    # 위성 관리 (매수 + 청산 조건)
    # ─────────────────────────────────────────────────────────────────

    def _manage_satellites(self):
        if not self.kis_overseas:
            return

        initial_krw = get_user_initial_cash(self.user_id, self._is_mock)
        fx          = _get_fx_rate()
        initial_usd = initial_krw / fx if fx > 0 else 0.0
        sat_budget_per = (initial_usd * self.SAT_RATIO) / max(1, self.num_satellites)

        # ── 미보유 후보 매수 ─────────────────────────────────────────
        for info in self.satellite_info:
            ticker = info["ticker"]
            pos    = self.satellite_positions.get(ticker)
            if pos and pos.shares > 0:
                continue
            # 블랙리스트 체크
            if ticker in self._satellite_rejects:
                continue
            # 쿨다운 체크
            if pos and (time.time() - pos.last_order_time < self.ORDER_COOLDOWN):
                continue
            price = self._price(ticker)
            if price <= 0 or self.cash_usd < sat_budget_per * 0.5:
                continue

            qty = self._buy(ticker, info["name"], sat_budget_per, price)
            if qty > 0:
                with self.lock:
                    self.satellite_positions[ticker] = USPosition(
                        ticker         = ticker,
                        name           = info["name"],
                        shares         = float(qty),
                        avg_price_usd  = price,
                        budget_usd     = sat_budget_per,
                        status         = "보유 중 🛰️",
                        last_order_time= time.time(),
                        max_price_usd  = price,
                    )
                self._tg(
                    f"🛰️ [US 위성 매수] {info['name']} ({ticker})\n"
                    f"@ ${price:.2f}  섹터: {info['sector']}"
                )

        # ── 보유 중 청산 조건 체크 ───────────────────────────────────
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
            trail   = (price - pos.max_price_usd) / pos.max_price_usd * 100 if pos.max_price_usd > 0 else 0.0

            # ① 하드 손절
            if pnl_pct <= self.STOP_LOSS_PCT:
                self._close_sat(ticker, pos, price, f"손절 {pnl_pct:.1f}%")
                continue

            # ② 트레일링 스탑 (고점 대비 -8%)
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
                    pos.status       = f"1차익절({pnl_pct:+.1f}%) ✂️"
                self._record_pnl(pnl)
                self.add_log(f"✂️  1차익절 {pos.name} | PnL ${pnl:+.0f}")
                continue

            # ④ 2차 부분 익절 (+30% → 추가 50% 매도)
            if not pos.partial_sold_2 and pnl_pct >= self.PARTIAL2_PCT and pos.shares > 1:
                q = max(1.0, pos.shares * self.PARTIAL2_QTY)
                self._sell(ticker, pos.name, q, price)
                pnl = _net_profit_usd(price, avg, q)
                with self.lock:
                    pos.shares        -= q
                    pos.partial_sold_2 = True
                    pos.status         = f"2차익절({pnl_pct:+.1f}%) ✂️✂️"
                self._record_pnl(pnl)
                self.add_log(f"✂️✂️ 2차익절 {pos.name} | PnL ${pnl:+.0f}")
                continue

            # ⑤ 스크리너 제외 종목 + 수익권 → 청산
            in_info = {i["ticker"] for i in self.satellite_info}
            if ticker not in in_info and pnl_pct > 0:
                self._close_sat(ticker, pos, price, f"스크리너 제외 (수익 {pnl_pct:.1f}%)")
                continue

            # 상태 업데이트
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

                # ── 위성 스크리닝 (하루 1회, KIS 미연결도 허용) ───────
                self._screen_satellites()

                # ── 위성 매매 (KIS 연결 시에만) ──────────────────────
                if self.kis_overseas:
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
        # ── ① SPY 시장 국면 ──────────────────────────────────────────
        try:
            import pandas as pd
            df = yf.download("SPY", period="60d", interval="1d",
                             progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.dropna(subset=["Close"])
            if len(df) >= 50:
                close = df["Close"]
                cur   = float(close.iloc[-1])
                sma20 = float(close.rolling(20).mean().iloc[-1])
                sma50 = float(close.rolling(50).mean().iloc[-1])
                if cur > sma20 > sma50:
                    regime = "BULL"
                elif cur < sma20 < sma50:
                    regime = "BEAR"
                else:
                    regime = "NEUTRAL"
                if regime != self.market_regime:
                    self.add_log(f"📊 US 시장 국면 변경: {self.market_regime} → {regime}")
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
                "satellite_info":        self.satellite_info,
                "hot_sectors":           self.hot_sectors,
                "daily_pnl":             self.daily_pnl,
                "daily_report":          self.daily_report,
                "last_screen_date":      self.last_screen_date,
                "futures_snapshot":      self.futures_snapshot,
                "sector_trends":         self.sector_trends,
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
            self.satellite_info         = state.get("satellite_info", [])
            self.hot_sectors            = state.get("hot_sectors", [])
            self.daily_pnl              = state.get("daily_pnl", {})
            self.daily_report           = state.get("daily_report", None)
            self.last_screen_date       = state.get("last_screen_date")
            self.futures_snapshot       = state.get("futures_snapshot", {})
            self.sector_trends          = state.get("sector_trends", [])
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
                "mock_total_asset": total_krw,
                "mock_pnl":         pnl_krw,
                "mock_pnl_rt":      round(pnl_rt, 2),
                "initial_cash":     initial_krw,
                "available_cash":   round(self.cash_usd * fx),
            }

        except Exception as e:
            logger.error(f"[US봇] get_status 오류: {e}", exc_info=True)
            return {
                "is_running": self.is_running, "is_mock": True, "has_keys": False,
                "logs": list(self.logs)[-30:], "hot_sectors": [], "num_satellites": self.num_satellites,
                "cores": [], "satellites": [], "momentum_list": [], "defensive_list": [],
                "market_regime": "NEUTRAL", "mock_total_asset": 0, "mock_pnl": 0,
                "mock_pnl_rt": 0, "initial_cash": 10_000_000, "available_cash": 0,
            }
