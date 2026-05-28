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

from telegram_bot import TelegramNotifier
from strategy import CorePosition, Position, get_rsi_signal, get_signal_by_strategy, REINVEST_RATIO, get_market_regime, get_market_regime_detail, get_bear_bounce_signal, get_bear_bottom_score, get_bull_momentum_score, get_neutral_range_score, INVERSE_ETF_TICKER, INVERSE_ETF_NAME, INVERSE_BUDGET_RATIO, DEFENSIVE_ASSETS, check_giveback_stop, check_early_drop_stop, check_theme_overextension_exit, check_rsi_progressive_exit, calculate_entry_score, get_entry_threshold, get_budget_ratio_from_score, calc_rsi
from stock_screener import select_satellites, generate_daily_market_report
from hot_momentum_scanner import scan_hot_momentum, clear_expired_cache
from upper_limit_pattern_scanner import collect_and_save_pattern, scan_pattern_matches
from database import update_bot_status, save_portfolio_state, load_portfolio_state, log_trade_journal, get_recent_trades, save_ai_rules, load_ai_rules, get_ai_rules_history, get_user_initial_cash, set_user_initial_cash, add_user_initial_cash, get_news_api_keys, get_sector_guide
from news_monitor import NewsMonitor
from kis_brokers.kis_real_api import KisRealApi
from kis_brokers.kis_real_websocket import KisRealWebSocket

_SELL_FEE = 0.00015   # 매도 수수료율 (0.015%)
_SELL_TAX = 0.0018    # 증권거래세율 (0.18%)

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
        self.mode_name = "실전"
        self.alert_icon = "🔴"

        self.core_ratio = 0.40        # 코어 40% — 중기 누적 매수
        self.satellite_ratio = 0.40   # 위성 40% — 중기 성장주 (단타 아님)
        # 나머지 20%는 단타 or 현금 — AI 재량 보유
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
        self.satellite_strategies = {}
        self.daily_pnl = {}
        self.last_screen_month = None
        self.last_screen_date = None
        self.last_core_rebalance_date = None   # AI 코어 마지막 재선정 날짜 (매주 월요일 갱신)
        self.hot_sectors = []
        self.daily_report = None

        # 예수금 즉시 반영용 내부 현금 추적기
        # KIS 모의 API는 체결 후 1~3분 지연이 있어 캐시 API 값 대신 내부 추적값 사용
        self.internal_cash = None          # 최초 KIS API 값으로 초기화 후 매수/매도마다 즉각 갱신
        self._last_trade_ts = 0.0          # 마지막 체결 타임스탬프 (KIS API 재동기화 시점 판단)
        self._dca_prev_cash      = 0.0     # 전 턴 예수금 스냅샷 (입금 감지용)
        self._dca_deposit_trigger= False   # 이번 턴 입금 감지 플래그
        self._dca_deposit_amount = 0.0     # 감지된 입금액
        self.fundamental_cache = {}

        # ── 당일 블랙리스트 (날짜가 바뀌면 자동 초기화) ──────────────────
        # momentum_exit_times  : {ticker: exit_timestamp}  30분 재진입 금지
        # satellite_rejects    : 오늘 AI 거절된 위성 종목 {ticker: reason}
        # momentum_ai_rejects  : {ticker: 거절횟수}  3회 거절 시 당일 블랙리스트
        self._bl_date               = ""       # 마지막 초기화 날짜 (YYYY-MM-DD)
        self._momentum_exit_times   : dict = {}  # {ticker: float(epoch)}
        self._satellite_rejects     : dict = {}
        self._momentum_ai_rejects   : dict = {}  # {ticker: int}  당일 AI 거절 횟수

        # ── 종목당 당일 누적 손실 추적 (하루 최대 손실 캡) ──────────────
        # {ticker: cumulative_loss_krw}  — 손실(-) 누계, 날짜 바뀌면 초기화
        # 클래스 상수 _MAX_DAILY_LOSS_PER_TICKER 는 하단 클래스 본문에 정의됨
        self._daily_loss_by_ticker  : dict = {}

        # 시장 국면 (BULL / BEAR / NEUTRAL)
        self.market_regime = "NEUTRAL"
        self.last_regime_check = 0.0
        self._regime_check_interval = 3600  # 1시간마다 재판단
        self._last_defensive_check = 0.0     # 방어 자산 체크 캐시 (5분)
        self._defensive_sold_ts   = {}      # 방어 자산 종목별 청산 타임스탬프 {ticker: ts} (24h 쿨다운)

        # ── 🚀 테마·급등주 모멘텀 전용 슬롯 ──────────────────────────
        # 위성과 완전히 별개의 단일 포지션. AI 심사 후 진입, BEAR 시 현금 보유.
        # 코어 40% + 위성 40% + 이 슬롯 20% = 총 100% (AI 재량으로 현금 가능)
        self.momentum_positions = [None]      # 모멘텀 슬롯 1개 (총자산의 20%)
        self.momentum_budget_ratio = 0.20    # 총자산의 20% (단일 슬롯)
        self._last_momentum_scan = 0.0       # 마지막 스캔 타임스탬프
        self._momentum_scan_interval = 60    # 1분마다 스캔

        # ── 🧠 자가학습 트리거 ───────────────────────────────────────
        self._trades_since_reflection = 0        # 누적 거래 수 (10건마다 반성)
        self._last_emergency_reflection_ts = 0.0  # 긴급 반성 마지막 실행 (4시간 쿨다운)
        self._EMERGENCY_LOSS_THRESHOLD = -80_000  # 8만원 이상 손실 시 긴급 반성 트리거
        self._EMERGENCY_COOLDOWN = 4 * 3600       # 긴급 반성 최소 간격 (4시간)

        self.kis = None
        self.real_kis = None   # 모의봇에서 외인/기관 데이터 조회용 실전 KIS 인스턴스 (주입 시 사용)
        self.telegram = None
        self.gemini = None
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
        self.add_log(f"User {user_id} {self.mode_name}투자 전용 Bot Controller 가동 완료.")

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
            # 봇이 정지 상태일 때는 KIS API 호출 및 잔고 동기화 건너뜀
            # (정지 중에도 cached_balance/internal_cash가 변경되면 UI 혼선 유발)
            if not self.is_running:
                time.sleep(30)
                continue
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
            time.sleep(30)

    def _sync_internal_balances(self, real_balance):
        with self.lock:
            try:
                if not real_balance or 'stocks' not in real_balance: return
                real_cash = float(real_balance.get('total_cash', 0))
                real_stock_value = float(real_balance.get('total_value', 0))
                real_purchase = float(real_balance.get('total_purchase', 0))
                total_equity = real_cash + real_stock_value

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
                    db_cash = get_user_initial_cash(self.user_id, self._is_mock)
                    if db_cash == 10000000.0 and pure_principal > 0:
                        set_user_initial_cash(self.user_id, pure_principal, self._is_mock)
                        self.add_log(f"💰 [{self.mode_name} 원금 셋업] 투자 원금 {pure_principal:,.0f}원 확정 (첫 실행 감지).")
                    self.initial_capital_captured = True
                
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
                    # [BUG-FIX v2] 코어 예산: total_equity 기반 재계산 → 기하급수적 감소 버그
                    # ─ 구 로직: target_core_pool = total_equity * core_ratio
                    #   매수 후 현금이 줄면 total_equity도 감소 → 목표 풀도 축소 → 또 매수 반복
                    # ─ v1 수정(last_order_time 가드)의 한계:
                    #   core.shares가 30초마다 원자적 0 리셋 → 5분 후 가드 해제 시 mem_val=0 → 재발
                    # ─ v2 수정: core._bought_val 필드로 "매수 확약액" 영속 추적.
                    #   - 매수 시 += cp*qty, API 반영 확인 시 자동 해제
                    #   - shares 리셋과 독립적으로 유지 → T+2 랙에 완전 면역
                    initial_cap = get_user_initial_cash(self.user_id, self._is_mock)
                    target_core_per = (initial_cap * self.core_ratio) / max(1, len(self.core_positions))
                    for core in self.core_positions:
                        api_val = next(
                            (float(s.get('value', 0)) for s in real_balance['stocks'] if s['ticker'] == core.ticker),
                            0.0
                        )
                        bought_val = getattr(core, '_bought_val', 0.0)
                        # API가 보유 주식을 반영했으면 _bought_val 해제 (API 데이터로 전환)
                        if api_val > 0:
                            core._bought_val = 0.0
                            bought_val = 0.0
                        effective_val = max(api_val, bought_val)
                        new_cash = round(max(0.0, target_core_per - effective_val), 2)
                        # 진단 로그: cash가 크게 변할 때만 출력 (중복매수 방지 확인용)
                        if abs(new_cash - core.cash) > 10000:
                            logger.info(f"[{self.mode_name}] 코어 예산 sync | {core.ticker} | "
                                        f"원금={initial_cap:,.0f} 슬롯목표={target_core_per:,.0f} "
                                        f"api_val={api_val:,.0f} bought_val={bought_val:,.0f} "
                                        f"→ cash {core.cash:,.0f} → {new_cash:,.0f}")
                        core.cash = new_cash

                    target_sat_pool = total_equity * self.satellite_ratio

                    current_sat_stock_val = sum([float(s.get('value', 0)) for s in real_balance['stocks'] if s['ticker'] in self.satellite_positions])
                    # [BUG-FIX] 위성 예산 상한을 실제 주문가능현금으로 캡 적용.
                    # total_equity 기반 목표치가 코어 투자분 포함 총 자산에서 계산되므로
                    # 실제 현금 < 위성 예산 → "주문가능금액 초과" 주문 실패 방지.
                    core_reserved = sum(getattr(c, 'cash', 0.0) for c in self.core_positions)
                    # T+2 보정: 매수가능조회(007) → nrcvb_buy_amt 는 당일 매도대금 포함
                    # 잔고조회의 ord_psbl_cash는 T+2 정산 전 0으로 나올 수 있어 위성 매수 차단됨
                    buyable_cash = real_cash
                    if self.kis and hasattr(self.kis, 'get_buyable_cash'):
                        try:
                            _bc = float(self.kis.get_buyable_cash() or 0)
                            if _bc > 0:
                                buyable_cash = _bc
                        except Exception:
                            pass
                    avail_for_sat = max(0.0, buyable_cash - core_reserved)
                    total_sat_cash = min(
                        max(0.0, target_sat_pool - current_sat_stock_val),
                        avail_for_sat
                    )
                    empty_sat_count = sum(1 for sat in self.satellite_positions.values() if int(sat.shares) == 0)
                    for t, sat in self.satellite_positions.items():
                        if int(sat.shares) > 0: sat.cash = 0.0
                        else: sat.cash = round(total_sat_cash / max(1, empty_sat_count), 2)

                # 원자적 교체: 먼저 새 값을 모두 수집한 뒤 한 번에 적용
                # (중간에 예외 발생 시 shares=0으로 남아 재매수 폭주하는 버그 방지)
                new_shares: dict = {}   # ticker → (shares, avg_price, current_price)
                for real_stock in real_balance['stocks']:
                    t = real_stock['ticker']
                    q = int(real_stock['shares'])
                    p = float(real_stock['purchase_price'])
                    c_p = float(real_stock.get('current_price', p))
                    stock_name = real_stock.get('name', t)
                    new_shares[t] = (q, p, c_p, stock_name)

                # API 조회 성공 후에만 shares 초기화 → 교체 (원자적)
                for core in self.core_positions: core.shares = 0
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
                                self.satellite_strategies[t] = 'RSI(9) 30/70'
                                if not any(x['ticker'] == t for x in self.satellite_info):
                                    self.satellite_info.append({'ticker': t, 'name': stock_name, 'strategy_name': 'RSI(9) 30/70', 'return_pct': 0.0, 'sector': '-'})
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
        """
        self.core_positions = []
        user_tickers_seen: set = set()
        # 사용자 지정 종목 전부 사용 (최대 3개까지)
        for c in self.user_core_stocks[:3]:
            if c.get('ticker') and c['ticker'] not in user_tickers_seen:
                pos = CorePosition(c['ticker'], c['name'], initial_cash=0)
                if c.get('dca'):
                    pos.dca_mode           = True
                    pos.dca_amount         = float(c.get('dca_amount', 0))
                    pos.dca_interval_hours = int(c.get('dca_hours', 72))
                    pos.dca_dip_pct        = float(c.get('dca_dip_pct', 3.0))
                self.core_positions.append(pos)
                user_tickers_seen.add(c['ticker'])
        # 빈 슬롯만 TBD 플레이스홀더로 채움
        ai_needed = 3 - len(self.core_positions)
        for i in range(ai_needed):
            ph = CorePosition("TBD", f"AI선정대기#{i+1}", initial_cash=0)
            ph.status = "AI 선정 대기 🤖"
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
        """
        if not self.user_satellite_stocks:
            return
        user_tickers = {s['ticker'] for s in self.user_satellite_stocks if s.get('ticker')}
        # screener 결과에서 사용자 종목 제거 (중복 방지)
        filtered = [c for c in self.satellite_info if c['ticker'] not in user_tickers]
        pinned = [
            {'ticker': s['ticker'], 'name': s['name'],
             'strategy_name': '사용자지정', 'return_pct': 0.0, 'sector': '-'}
            for s in self.user_satellite_stocks if s.get('ticker') and s.get('name')
        ]
        self.satellite_info = (pinned + filtered)[:self.num_satellites]
        # satellite_strategies 동기화
        for s in pinned:
            self.satellite_strategies[s['ticker']] = '사용자지정'

    def _rebalance_ai_cores(self):
        """
        AI 코어 슬롯 재선정 (매주 월요일 자동 호출).
        - 사용자 지정 슬롯(user_core_stocks 전체)은 유지
        - AI 슬롯 중 보유 주식이 없는(shares==0) 슬롯만 교체
        - 보유 중인 AI 코어는 자연 청산 신호가 날 때까지 유지
        """
        from stock_screener import select_ai_core_stock
        now = _now_kst()

        # 마지막 재선정으로부터 7일 미만이면 스킵
        if self.last_core_rebalance_date:
            diff = (now.date() - self.last_core_rebalance_date).days
            if diff < 7:
                return

        self.add_log("📅 [주간 AI 코어 재선정] 월요일 — AI 코어 슬롯 재검토 시작")

        # 사용자 지정 티커 전체 파악 (1개가 아닌 최대 3개)
        user_tickers: set = {s['ticker'] for s in self.user_core_stocks if s.get('ticker')}

        # 현재 코어 중 AI 슬롯(사용자 아닌 것) 파악
        with self.lock:
            ai_cores_snapshot = [
                (i, c) for i, c in enumerate(self.core_positions)
                if c.ticker not in user_tickers
            ]

        # 교체 가능한 슬롯: AI 코어 중 보유 주식 없는 것
        empty_ai_slots = [(i, c) for i, c in ai_cores_snapshot if c.shares == 0]
        if not empty_ai_slots:
            self.add_log("   ℹ️ 모든 AI 코어 보유 중 — 교체 없음 (자연 청산 대기)")
            self.last_core_rebalance_date = now.date()
            self._save_state()
            return

        # 현재 모든 코어 티커 제외하고 새 AI 후보 선정
        all_current_tickers = {c.ticker for c in self.core_positions}
        n_needed = len(empty_ai_slots)
        new_ai_list = select_ai_core_stock(
            n=n_needed,
            exclude_tickers=all_current_tickers,
            verbose=False
        )

        if not new_ai_list:
            self.add_log("   ⚠️ AI 코어 신규 후보 없음 (정배열 조건 미충족) — 기존 유지")
            self.last_core_rebalance_date = now.date()
            self._save_state()
            return

        # 초기 원금 기준으로 예산 계산
        try:
            from database import get_user_initial_cash
            initial_cap = get_user_initial_cash(self.user_id, self._is_mock)
            per_core_budget = (initial_cap * self.core_ratio) / max(1, len(self.core_positions))
        except Exception:
            per_core_budget = 0

        changed = []
        with self.lock:
            for (slot_idx, old_core), new_core_info in zip(empty_ai_slots, new_ai_list):
                old_name = old_core.name
                new_pos = CorePosition(new_core_info['ticker'], new_core_info['name'], initial_cash=per_core_budget)
                new_pos.cash = per_core_budget
                self.core_positions[slot_idx] = new_pos
                changed.append(f"{old_name} → {new_core_info['name']}({new_core_info['ticker']}) [{new_core_info.get('sector','-')}]")

        self.last_core_rebalance_date = now.date()
        self._save_state()

        # 알림
        change_lines = "\n".join([f"   · {c}" for c in changed])
        self.add_log(f"✅ [AI 코어 재선정] 교체 완료:\n{change_lines}")
        self._send_telegram(
            f"🔄 AI 코어 주간 재선정  ·  {self.alert_icon} {self.mode_name}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{chr(10).join(['· ' + c for c in changed])}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ {now.strftime('%H:%M KST')}"
        )

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

    def _buy_order(self, ticker: str, qty: int, pos, name: str, limit_price: int = 0) -> bool:
        """매수 주문 실행 + KIS 응답 체크. 성공 True, 실패 False (봇 로그에 에러 기록).
        limit_price = 0 → 현재가 +0.3% 지정가 자동 계산 (슬리피지 제한)
        limit_price = -1 → 강제 시장가 (모멘텀 전용)"""
        if not self.kis:
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
            # _last_trade_ts는 est_price 여부와 무관하게 항상 갱신:
            # est_price=0(신규 위성종목 첫 매수)일 때도 _sync_internal_balances가
            # KIS API 값으로 덮어쓰지 않도록 타임스탬프를 찍어 둬야 함
            with self.lock:
                self._last_trade_ts = time.time()
            est_price = self.live_prices.get(ticker, 0) or getattr(pos, 'avg_price', 0) or 0
            if est_price > 0:
                with self.lock:
                    if self.internal_cash is not None:
                        self.internal_cash = max(0.0, self.internal_cash - est_price * qty * 1.00015)
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

    def _sell_order(self, ticker: str, qty: int, pos, name: str, price: int = 0) -> bool:
        """매도 주문 실행 + KIS 응답 체크. 성공 True, 실패 False (봇 로그에 에러 기록)."""
        if not self.kis:
            return False
        result = self.kis.sell_market_order(ticker, qty, price=price)
        if result:
            # 내부 현금 즉시 증가 — KIS 모의 API 반영 지연 보정
            # _last_trade_ts는 est_price 여부와 무관하게 항상 갱신
            with self.lock:
                self._last_trade_ts = time.time()
            est_price = price or self.live_prices.get(ticker, 0) or getattr(pos, 'avg_price', 0) or 0
            if est_price > 0:
                with self.lock:
                    if self.internal_cash is not None:
                        self.internal_cash += est_price * qty * (1 - _SELL_FEE - _SELL_TAX)
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
                        if is_core or not self.gemini:
                            continue

                        # 위성 포지션 AI 손절 검토
                        try:
                            context = f"악재 공시 발생: {report_nm} ({rcept_dt})\n보유: {shares}주 @ 평단 {avg_price:,.0f}원"
                            decision, ai_reason = self.gemini.ai_approve_trade(
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
                                            pos.shares = 0; pos.status = "악재공시 손절 🚨"
                                            self.pnl_this_turn += _net_profit(price_now, avg_price, sell_shares)
                                        profit = _net_profit(price_now, avg_price, sell_shares)
                                        self._log_trade(ticker, name, 'SELL', price_now, "공시감지", f"악재공시 AI 손절: {report_nm}", profit=profit)  # [BUG-C2]
                                        self._record_daily_pnl(profit)  # [BUG-C2]
                                        self.add_log(f"🚨 {name}({ticker}) 악재 공시 AI 손절 완료")
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
    _MOMENTUM_COOLDOWN_SEC    = 1_800     # 손절 후 재진입 금지 시간 (30분)

    def _refresh_blacklist(self):
        """날짜가 바뀌면 당일 블랙리스트를 초기화합니다. [BUG-M1] 락 내부에서 호출 전제."""
        today = _now_kst().strftime('%Y-%m-%d')
        if self._bl_date != today:
            self._bl_date              = today
            self._momentum_exit_times  = {}
            self._satellite_rejects    = {}
            self._momentum_ai_rejects  = {}
            self._daily_loss_by_ticker = {}

    def _add_momentum_exit(self, ticker: str):
        """모멘텀 청산 종목을 30분 재진입 금지 목록에 추가합니다."""
        with self.lock:
            self._refresh_blacklist()
            self._momentum_exit_times[ticker] = time.time()

    def _add_satellite_reject(self, ticker: str, reason: str):
        """AI 거절 위성 종목을 당일 재편입 금지 목록에 추가합니다."""
        with self.lock:
            self._refresh_blacklist()
            self._satellite_rejects[ticker] = reason
        # 재시작 후에도 블랙리스트가 유지되도록 즉시 상태 저장
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

    def _is_momentum_blacklisted(self, ticker: str) -> bool:
        """30분 쿨다운 또는 당일 손실 캡 초과 시 True."""
        with self.lock:
            self._refresh_blacklist()
            # 30분 쿨다운
            exit_ts = self._momentum_exit_times.get(ticker, 0)
            if time.time() - exit_ts < self._MOMENTUM_COOLDOWN_SEC:
                return True
            # 하루 최대 손실 캡 (누계 손실이 캡 미만[더 깊은 마이너스]이면 차단)
            if self._daily_loss_by_ticker.get(ticker, 0) < self._MAX_DAILY_LOSS_PER_TICKER:
                return True
            return False

    def _is_satellite_blacklisted(self, ticker: str) -> bool:
        with self.lock:
            self._refresh_blacklist()
            return ticker in self._satellite_rejects

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
        self.add_log(f"🔑 {self.mode_name}투자 API 키 및 계좌 설정이 시스템에 반영되었습니다.")

    def update_mode(self, is_mock, total_cash=10000000):
        pass

    def _ai_filter_satellites(self, candidates: list) -> list:
        """AI가 위성 후보 검토 — 부적합 종목 제거 + 전략 교체. AI 없으면 원본 반환."""
        if not self.gemini or not candidates:
            return candidates
        try:
            self.add_log("🤖 AI가 위성 후보 종목·전략 검토 중...")
            reviewed = self.gemini.review_satellite_candidates(candidates, self.hot_sectors, sector_guide=self.sector_guide)
            approved = [c for c in reviewed if c.get('approved', True)]
            rejected = [c for c in reviewed if not c.get('approved', True)]
            for c in rejected:
                self.add_log(f"🛑 AI 위성 퇴출: {c['name']}({c['ticker']}) — {c.get('ai_reason','')}")
                self._add_satellite_reject(c['ticker'], c.get('ai_reason', 'AI 부적합 판정'))
            for c in approved:
                old_st = candidates[[x['ticker'] for x in candidates].index(c['ticker'])].get('strategy_name','') if c['ticker'] in [x['ticker'] for x in candidates] else ''
                if old_st and old_st != c.get('strategy_name', old_st):
                    self.add_log(f"🔄 AI 전략 교체: {c['name']} [{old_st}] → [{c['strategy_name']}] | {c.get('ai_reason','')}")
            return approved
        except Exception as e:
            logger.warning(f"[{self.mode_name}] _ai_filter_satellites 오류: {e}")
            return candidates

    def initialize_portfolio(self, total_cash):
        self.add_log("포트폴리오 초기화 중...")
        raw_info, self.hot_sectors = select_satellites(kis=self.kis, n=self.num_satellites * 2, verbose=False, gemini_client=self.gemini, sector_guide=self.sector_guide, real_kis=self.real_kis)
        if self.hot_sectors:
            self.add_log(f"🔥 강세 섹터: {', '.join(self.hot_sectors[:4])}")
        else:
            self.add_log("⚠️ 강세 섹터 없음 — 상대 강세 기준 후보 선정")
        # 모멘텀 슬롯 종목은 위성 편입 금지
        momentum_tickers = {
            mp['ticker'] for mp in self.momentum_positions
            if mp is not None and isinstance(mp, dict) and mp.get('ticker')
        }
        raw_info = [c for c in raw_info if c['ticker'] not in momentum_tickers]
        # AI 검토: 부적합 종목 제거 후 num_satellites 개수만 사용
        filtered_info = self._ai_filter_satellites(raw_info)
        self.satellite_info = filtered_info[:self.num_satellites]
        self._inject_user_satellites()  # 사용자 지정 종목 우선 고정
        from stock_screener import select_ai_core_stock
        self.satellite_strategies = {c['ticker']: c['strategy_name'] for c in self.satellite_info}
        log_lines = [f"  {i+1}. {c['name']} ({c['ticker']}) → [{c['strategy_name']}] {c['return_pct']:+.1f}%" for i, c in enumerate(self.satellite_info)]
        for line in log_lines: self.add_log(f"✅ {line.strip()}")
        log_html = "\n".join([f"  · {c['name']} <code>{c['ticker']}</code>  [{c['strategy_name']}]" for c in self.satellite_info])
        self._send_telegram(
            f"🔍 <b>위성 종목 선정 완료{'(AI 검토 반영)' if self.gemini else ''}</b>  ·  {self.alert_icon} {self.mode_name}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{log_html}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ {_now_kst().strftime('%H:%M KST')}"
        )

        core_budget = total_cash * self.core_ratio
        sat_budget  = total_cash * self.satellite_ratio
        n_sat       = len(self.satellite_info) if self.satellite_info else self.num_satellites
        per_sat     = sat_budget / n_sat if n_sat > 0 else 0

        # ── 코어 구성: 사용자 지정 최대 3개 → 남은 자리만 AI 선정 ──
        self.core_positions = []
        user_tickers: set = set()

        # 사용자 지정 종목 전부 등록 (최대 3개)
        for user_pick in self.user_core_stocks[:3]:
            if user_pick.get('ticker') and user_pick['ticker'] not in user_tickers:
                self.core_positions.append(CorePosition(user_pick['ticker'], user_pick['name'], initial_cash=0))
                user_tickers.add(user_pick['ticker'])

        # AI 슬롯: 빈 자리만 채우기 (사용자가 3개 다 지정하면 AI 선정 없음)
        ai_needed = 3 - len(self.core_positions)
        if ai_needed > 0:
            self.add_log(f"🔍 AI 코어 종목 선정 중... (목표 {ai_needed}개, 섹터 분산 적용)")
            ai_core_list = select_ai_core_stock(n=ai_needed, exclude_tickers=user_tickers, verbose=False)
            for ai_core in ai_core_list:
                self.core_positions.append(CorePosition(ai_core['ticker'], ai_core['name'], initial_cash=0))

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
                "cores": [{"ticker": c.ticker, "name": c.name, "shares": int(c.shares), "floor_shares": int(c.floor_shares), "cash": float(c.cash), "initial_cash": float(c.initial_cash), "avg_price": float(c.avg_price), "dca_mode": bool(getattr(c, 'dca_mode', False)), "dca_amount": float(getattr(c, 'dca_amount', 0)), "dca_interval_hours": int(getattr(c, 'dca_interval_hours', 72)), "dca_dip_pct": float(getattr(c, 'dca_dip_pct', 3.0)), "last_dca_time": float(getattr(c, 'last_dca_time', 0.0)), "second_buy_price": float(getattr(c, 'second_buy_price', 0.0)), "second_buy_cash": float(getattr(c, 'second_buy_cash', 0.0)), "second_buy_done": bool(getattr(c, 'second_buy_done', False))} for c in self.core_positions],
                "satellites": {ticker: {"name": pos.name, "shares": int(pos.shares), "cash": float(pos.cash), "initial_cash": float(pos.initial_cash), "avg_price": float(pos.avg_price), "partial_sold": bool(getattr(pos, 'partial_sold', False)), "partial_sold_2": bool(getattr(pos, 'partial_sold_2', False)), "second_buy_done": bool(getattr(pos, 'second_buy_done', False)), "pyramid_done": bool(getattr(pos, 'pyramid_done', False)), "second_buy_price": float(getattr(pos, 'second_buy_price', 0)), "second_buy_cash": float(getattr(pos, 'second_buy_cash', 0)), "max_price": float(getattr(pos, 'max_price', 0))} for ticker, pos in self.satellite_positions.items()},
                "satellite_info": self.satellite_info, "satellite_strategies": self.satellite_strategies, "hot_sectors": self.hot_sectors, "num_satellites": self.num_satellites,
                "last_screen_month": getattr(self, 'last_screen_month', None), "last_screen_date": self.last_screen_date.strftime('%Y-%m-%d') if getattr(self, 'last_screen_date', None) else None,
                "last_core_rebalance_date": self.last_core_rebalance_date.strftime('%Y-%m-%d') if getattr(self, 'last_core_rebalance_date', None) else None,
                "daily_pnl": self.daily_pnl, "daily_report": self.daily_report,
                "momentum_positions": [self._serialize_one_momentum(mp) for mp in self.momentum_positions],
                # 당일 블랙리스트 — 재시작 후에도 AI 거절 종목이 재심사 요청되지 않도록 저장
                "bl_date":              self._bl_date,
                "satellite_rejects":    dict(self._satellite_rejects),
                "momentum_ai_rejects":  dict(self._momentum_ai_rejects),
                # 모멘텀 당일 차단 타임스탬프 — 재시작 후에도 3회 거절 차단이 해제되지 않도록
                "momentum_exit_times":  {k: float(v) for k, v in self._momentum_exit_times.items()},
            }
            save_portfolio_state(self.user_id, state, self._is_mock)
        except Exception as e: logger.error(f"[{self.mode_name}] 상태 저장 실패: {e}", exc_info=True)

    def _restore_state(self):
        try:
            state = load_portfolio_state(self.user_id, self._is_mock)
            if not state or not state.get("cores"): return False
            self.add_log(f"🔄 {self.mode_name} 포트폴리오 상태 복구 중...")
            self.core_positions = []
            for c in state["cores"]:
                pos = CorePosition(c["ticker"], c["name"], initial_cash=c.get("initial_cash", 3000000))
                pos.shares = c["shares"]; pos.floor_shares = c["floor_shares"]; pos.cash = c["cash"]; pos.avg_price = c.get("avg_price", 0)
                pos.dca_mode           = bool(c.get("dca_mode", False))
                pos.dca_amount         = float(c.get("dca_amount", 0))
                pos.dca_interval_hours = int(c.get("dca_interval_hours", 72))
                pos.dca_dip_pct        = float(c.get("dca_dip_pct", 3.0))
                pos.last_dca_time      = float(c.get("last_dca_time", 0.0))
                pos.second_buy_price   = float(c.get("second_buy_price", 0.0))
                pos.second_buy_cash    = float(c.get("second_buy_cash", 0.0))
                pos.second_buy_done    = bool(c.get("second_buy_done", False))
                self.core_positions.append(pos)
            self.satellite_positions = {}
            for ticker, s in state["satellites"].items():
                pos = Position(ticker, s["name"], s.get("initial_cash", 1400000))
                pos.shares = s["shares"]; pos.cash = s["cash"]; pos.avg_price = s.get("avg_price", 0)
                pos.partial_sold     = bool(s.get("partial_sold",     False))
                pos.partial_sold_2   = bool(s.get("partial_sold_2",   False))
                pos.second_buy_done  = bool(s.get("second_buy_done",  False))
                pos.pyramid_done     = bool(s.get("pyramid_done",     False))
                pos.second_buy_price = float(s.get("second_buy_price", 0))
                pos.second_buy_cash  = float(s.get("second_buy_cash",  0))
                pos.max_price        = float(s.get("max_price",        0))  # W-04: 트레일링 스탑 기준가 복원
                self.satellite_positions[ticker] = pos

            self.satellite_info = state.get("satellite_info", [])
            self.satellite_strategies = state.get("satellite_strategies", {})
            self.hot_sectors = state.get("hot_sectors", [])
            self.num_satellites = state.get("num_satellites", 3)  # 저장된 값 복원
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
                self._momentum_ai_rejects = state.get("momentum_ai_rejects", {})
                # 모멘텀 당일 차단 타임스탬프 복원 — 재시작 후에도 3회 거절 종목이 재심사받지 않도록
                self._momentum_exit_times = {k: float(v) for k, v in state.get("momentum_exit_times", {}).items()}
                n_rej = len(self._satellite_rejects)
                n_mom = len(self._momentum_ai_rejects)
                if n_rej or n_mom:
                    self.add_log(f"🚫 당일 AI 거절 블랙리스트 복원: 위성 {n_rej}개 / 모멘텀 {n_mom}개 재심사 제외")
            # 모멘텀 슬롯 복원 (구버전 단일 포지션 호환)
            # __init__ 에서 설정한 슬롯 수를 먼저 저장 (덮어쓰기 전)
            target_slots = len(self.momentum_positions)
            saved_slots = state.get("momentum_positions")
            if saved_slots is not None:
                restored = [self._deserialize_one_momentum(mp) for mp in saved_slots]
                # 현재 슬롯 수에 맞게 자르거나 None 으로 채움
                while len(restored) < target_slots:
                    restored.append(None)
                self.momentum_positions = restored[:target_slots]
            else:
                old_single = state.get("momentum_position")
                self.momentum_positions = [self._deserialize_one_momentum(old_single)] + [None] * (target_slots - 1)

            # satellite_info에 선정된 종목 중 positions에 없는 것 → 빈 포지션 생성
            # 대시보드에 "감시 중" 상태로 표시되고, 다음 매매 턴에 즉시 진입 시도 가능
            _existing_tickers = set(self.satellite_positions.keys())
            for _sat in self.satellite_info:
                _t = _sat.get('ticker')
                if _t and _t not in _existing_tickers:
                    self.satellite_positions[_t] = Position(_t, _sat.get('name', _t), 0.0)
                    self.satellite_strategies[_t] = _sat.get('strategy_name', 'RSI(9) 30/70')
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
            self.market_regime     = detail['regime']
            self.last_regime_check = time.time()

            # ── 매번 현재 국면 진단 로그 (ADX·연속일·점수 포함) ──────────────
            adx_str    = f"ADX={detail['adx']:.1f}"
            streak_str = f"연속상승{detail['up_streak']}일"
            rsi_str    = f"RSI={detail['rsi']:.1f}"
            score_str  = f"점수{detail['score']:+d}"
            diag_line  = f"{score_str} | {rsi_str} | {adx_str} | {streak_str} | 22일수익{detail['ret22']:+.1f}%"

            if detail['downgrade_reason']:
                self.add_log(f"⚠️ [{self.mode_name}] {detail['downgrade_reason']} | {diag_line}")

            if self.market_regime != prev:
                icons = {"BULL": "🐂", "BEAR": "🐻", "NEUTRAL": "😐"}
                log_regime_desc = {
                    'BEAR':    '📉 위성 신규 매수 중단, 인버스 ETF 진입',
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
        BEAR 국면: DEFENSIVE_ASSETS 3종 자동 매수 (인버스 15%, 달러 10%, 금 5%).
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

                    # BEAR 시 방어헤지 총합 50% (기존 30% → 50% 스케일업)
                    # 개별 비율을 ×(0.50/0.30) = ×5/3 적용 → 인버스 25%, 달러 16.7%, 금 8.3%
                    _total_def_ratio = sum(a['ratio'] for a in DEFENSIVE_ASSETS)  # 0.30
                    bear_ratio_scaled = ratio * (0.50 / _total_def_ratio)
                    budget = int(total_assets * bear_ratio_scaled)
                    price  = self.kis.get_current_price(ticker)
                    if price and price > 0:
                        qty = int(budget // price)
                        if qty > 0 and total_cash >= qty * price * 1.002:
                            if self.kis.buy_market_order(ticker, qty):  # [BUG-FIX] 반환값 확인
                                total_cash -= qty * price  # 현금 차감 (다음 종목 계산용)
                                self.add_log(f"🐻 하락장 방어 매수 | {emoji} {name} {qty}주 @ {price:,.0f}원")
                                self._log_trade(ticker, name, 'BUY', price, "방어자산", f"BEAR 국면 총자산 {bear_ratio_scaled*100:.0f}% 헤지")
                                self._send_telegram(
                                    f"🐻 <b>방어 자산 매수</b>  ·  {self.alert_icon} {self.mode_name}\n"
                                    f"━━━━━━━━━━━━━━━━━━━━\n"
                                    f"{emoji} <b>{name}</b>  <code>{ticker}</code>\n"
                                    f"💰 <b>{price:,.0f}원</b> × <b>{qty}주</b>  =  <b>{qty*price:,.0f}원</b>\n"
                                    f"📋 BEAR 국면  ·  총자산 {bear_ratio_scaled*100:.0f}% 헤지 (방어50% + 위성저점50%)\n"
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
            except Exception:
                news = "뉴스 조회 실패"
            lines.append(f"[최근 뉴스] {news}")

        # ── 2. 재무제표 (캐시에 있으면 사용) ─────────────────────
        today_str = _now_kst().strftime('%Y-%m-%d')
        fundamental = self.fundamental_cache.get(f"{ticker}_{today_str}", "")
        if fundamental:
            lines.append(f"[재무지표] {fundamental}")

        # ── 3. 기술적 지표 (ex_df 기반) ─────────────────────────
        if ex_df is not None and not ex_df.empty and 'close' in ex_df.columns:
            from strategy import calc_rsi
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
        if self.gemini:
            for slot_time in ['15:40']:
                if current_time_str == slot_time:
                    dr = self.daily_report
                    already = (isinstance(dr, dict) and dr.get('date') == today_str
                               and dr.get(slot_time) is not None)
                    if not already:
                        self._run_threaded(lambda t=slot_time: self.generate_daily_report(t))
                    break

        # ── 15:35 장 마감 직후: 오늘 급등/상한가 패턴 수집 → 내일 선제 매수용 ──
        if current_time_str == '15:35':
            if not getattr(self, '_pattern_collected_date', None) == today_str:
                self._pattern_collected_date = today_str
                def _collect_pattern():
                    try:
                        prof = collect_and_save_pattern(kis=self.kis, verbose=False)
                        if prof:
                            self.add_log(
                                f"📊 [급등패턴] 오늘 급등 {prof['sample_count']}종목 패턴 저장 완료 "
                                f"| RSI평균 {prof['rsi']:.0f} / BB {prof['bb_pct']:.0f}% "
                                f"| 내일 유사 종목 선제 매수 준비"
                            )
                            self._send_telegram(
                                f"📊 <b>급등패턴 수집 완료</b>\n"
                                f"━━━━━━━━━━━━━━━━━━━━\n"
                                f"오늘 급등 {prof['sample_count']}종목 전일 패턴 분석 완료\n"
                                f"📈 평균 RSI: {prof['rsi']:.0f} | BB: {prof['bb_pct']:.0f}%\n"
                                f"🔍 내일 장 시작 전 유사 종목 자동 스캔 예정", 'misc'
                            )
                    except Exception as e:
                        logger.warning(f"[{self.mode_name}] 급등패턴 수집 오류: {e}")
                threading.Thread(target=_collect_pattern, daemon=True).start()
        
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
                            _pnl = ((core.kis_current_price - core.avg_price) / core.avg_price * 100) if core.avg_price > 0 and core.kis_current_price > 0 else 0
                            core.status = "보유 중 💎"
                            core.status_msg = f"{core.shares}주 보유 중 | 평단 {core.avg_price:,.0f}원 | 수익률 {_pnl:+.1f}% | {_regime_label}"
                        elif core.cash > 0:
                            core.status = "감시 중 👀"
                            core.status_msg = f"매수 신호 대기 중 | 가용 예산 {core.cash:,.0f}원 | 시장: {_regime_label}"
                        else:
                            core.status = "감시 중 👀"
                            core.status_msg = f"예산 소진 — 다음 잔고 동기화 대기 중 | 시장: {_regime_label}"

                for sat in self.satellite_positions.values():
                    if "대기" not in sat.status and "심사" not in sat.status:
                        if sat.shares > 0:
                            _pnl = ((sat.kis_current_price - sat.avg_price) / sat.avg_price * 100) if sat.avg_price > 0 and sat.kis_current_price > 0 else 0
                            sat.status = "보유 중 ✅"
                            sat.status_msg = f"{sat.shares}주 보유 중 | 평단 {sat.avg_price:,.0f}원 | 수익률 {_pnl:+.1f}% | {_regime_label}"
                        elif sat.cash > 0:
                            _st = getattr(sat, 'strategy', self.satellite_strategies.get(sat.ticker, '-'))
                            sat.status = "감시 중 👀"
                            sat.status_msg = f"전략 [{_st}] 신호 대기 | 예산 {sat.cash:,.0f}원 | 시장: {_regime_label}"
                        else:
                            sat.status = "감시 중 👀"
                            sat.status_msg = f"예산 소진 — 다음 종목 교체 대기 | 시장: {_regime_label}"

        # ── 📅 월요일 AI 코어 재선정 (09:10~09:20 사이 1회) ──────────────
        if now.weekday() == 0 and "09:10" <= current_time_str <= "09:20":
            self._run_threaded(self._rebalance_ai_cores)

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
                                    pos.shares = 0  # [C-NEW-03] 서킷브레이커 청산 후 잔여주수 초기화
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
                from strategy import get_rsi_signal
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
                        self.add_log(f"💎 {c_nm} 코어 2차 매수 | {sq}주 @ {cp:,}원 | 눌림목 -2%")
                        self._log_trade(c_tk, c_nm, 'BUY', cp, "RSI코어", f"코어 2차 분할 매수 눌림목 ({sq}주)")
                        self._send_trade_telegram(self._fmt_trade_msg("💎", "코어 2차 매수", c_tk, c_nm, cp, sq, strategy="RSI코어", note="-2% 눌림목 포착"))
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
                    _tr = pd.concat([
                        ex_df['high'] - ex_df['low'],
                        (ex_df['high'] - ex_df['close'].shift(1)).abs(),
                        (ex_df['low']  - ex_df['close'].shift(1)).abs(),
                    ], axis=1).max(axis=1)
                    c_atr = float(_tr.rolling(14, min_periods=1).mean().iloc[-1])

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
                    if self._sell_order(c_tk, c_sh, core, c_nm):
                        core_profit = _net_profit(cp, c_avg, c_sh)
                        with self.lock:
                            core.last_order_time = time.time()
                            core.status           = "코어 손절 🚨"
                            core.shares           = 0
                            core._bought_val      = 0.0
                            core.partial_sold     = False
                            core.partial_sold_2   = False
                            core.second_buy_price = 0.0
                            core.second_buy_cash  = 0.0
                            core.second_buy_done  = False
                            core.bull_pyramid_done= False
                            self.pnl_this_turn   += core_profit
                        self._record_daily_pnl(core_profit)
                        self.add_log(f"🚨 {c_nm} 코어 ATR 손절 전량 | {c_sh}주 @ {cp:,}원 | 손익: {core_profit:+,.0f}원")
                        self._log_trade(c_tk, c_nm, 'SELL', cp, "코어 ATR 손절", f"평단 {c_avg:,.0f} 대비 ATR×{core_hard_mult} 이탈", profit=core_profit)
                        self._send_trade_telegram(self._fmt_trade_msg("🚨", "코어 손절 전량", c_tk, c_nm, cp, c_sh, profit=core_profit, strategy="코어 ATR 손절", note=f"재진입 대기 — 더 좋은 타점 탐색 중"))
                    continue  # 손절 후 이번 턴 추가 로직 스킵

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
                            if self.gemini:
                                self._trigger_ai_partial_exit(core, c_tk, c_nm, cp, c_avg, c_pnl_pct, regime)
                                with self.lock: core.status = f"AI 익절 검토 중 ({c_pnl_pct:+.1f}%) 🤖"
                            else:
                                with self.lock: core.ai_exit_decision = "SELL_PARTIAL"
                        elif c_decision == "HOLD":
                            with self.lock:
                                core.status = f"AI 홀드 ({c_pnl_pct:+.1f}%) ⏳"
                        else:
                            partial_qty = max(1, c_sh // 2)
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
                                self.add_log(f"✂️  {c_nm} 코어 1차익절 | {partial_qty}주 @ {cp:,}원 | 손익: {core_profit:+,.0f}원")
                                self._send_trade_telegram(self._fmt_trade_msg("✂️", "코어 1차익절(50%)", c_tk, c_nm, cp, partial_qty, profit=core_profit, strategy="코어 AI 익절"))
                        continue

                    # 2차: +20%(일반) / +30%(BULL) 도달 → AI에 전량 익절 여부 문의
                    elif core.partial_sold and not core.partial_sold_2 and c_pnl_pct >= _core_partial2:
                        if c_decision is None:
                            if self.gemini:
                                self._trigger_ai_partial_exit(core, c_tk, c_nm, cp, c_avg, c_pnl_pct, regime)
                                with self.lock: core.status = f"AI 익절 검토 중 ({c_pnl_pct:+.1f}%) 🤖"
                            else:
                                with self.lock: core.ai_exit_decision = "SELL_ALL"
                        elif c_decision == "HOLD":
                            with self.lock:
                                core.status = f"AI 홀드 ({c_pnl_pct:+.1f}%) ⏳"
                        else:
                            if self._sell_order(c_tk, c_sh, core, c_nm):
                                core_profit = _net_profit(cp, c_avg, c_sh)
                                with self.lock:
                                    core.last_order_time  = time.time()
                                    core.shares           = 0
                                    core._bought_val      = 0.0
                                    core.partial_sold_2   = True
                                    core.ai_exit_decision = None
                                    core.status           = f"코어 2차익절({c_pnl_pct:+.1f}%) ✅"
                                    self.pnl_this_turn   += core_profit
                                self._record_daily_pnl(core_profit)
                                self.add_log(f"✅ {c_nm} 코어 2차익절(전량) | {c_sh}주 @ {cp:,}원 | 손익: {core_profit:+,.0f}원")
                                self._send_trade_telegram(self._fmt_trade_msg("✅", "코어 2차익절(전량)", c_tk, c_nm, cp, c_sh, profit=core_profit, strategy="코어 AI 전량익절"))
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

                if c_sig == 'BUY' and c_cash >= cp and is_core_cd and not _dca_bought_this_turn:
                    # ① 통합 진입 점수 체크
                    c_score, c_score_reasons = calculate_entry_score(ex_df, cp, regime)
                    c_threshold = get_entry_threshold(regime, 'core')
                    if c_score < c_threshold:
                        with self.lock:
                            core.status = "감시 중 👀"
                            core.status_msg = f"코어 진입 점수 부족 ({c_score}/{c_threshold}) — 신호 강화 대기"
                    else:
                        budget_ratio  = get_budget_ratio_from_score(c_score, c_threshold)
                        # 위성과 동일하게 75/25 분할: 1차 진입 후 나머지는 -2% 눌림목 예약
                        first_ratio   = budget_ratio * 0.75
                        reserve_ratio = budget_ratio * 0.25
                        first_cash    = c_cash * first_ratio
                        reserve_cash  = c_cash * reserve_ratio
                        qty = int((first_cash * 0.98) // cp)
                        if qty > 0:
                            # ② AI 승인 (위성과 동일)
                            approved, ai_reason = True, "AI 미설정"
                            if self.gemini:
                                with self.lock:
                                    core.status     = "AI 심사 중 🤖"
                                    core.status_msg = f"매수 신호 발생 | {c_score}pt/{c_threshold}pt | RSI {c_rsi:.1f} — AI 최종 승인 대기 중..."
                                trade_ctx = self._build_trade_context(c_tk, c_nm, cp, ex_df, 'RSI코어', regime)
                                approved, ai_reason = self.gemini.ai_approve_trade(
                                    'BUY', c_nm, c_tk, cp, 'RSI코어', c_rsi,
                                    self.hot_sectors, get_recent_trades(self.user_id, c_tk),
                                    load_ai_rules(self.user_id),
                                    context=trade_ctx
                                )
                            if not approved:
                                with self.lock:
                                    core.status     = f"코어 AI 거절 🛑 ({c_score}pt)"
                                    core.status_msg = f"거절 이유: {ai_reason}"
                                self.add_log(f"🛑 {c_nm} 코어 AI 거절: {ai_reason[:60]}")
                            elif self._buy_order(c_tk, qty, core, c_nm):
                                with self.lock:
                                    core.last_order_time  = time.time()
                                    core.status           = "체결 대기 ⏳"
                                    core.shares          += qty
                                    core._bought_val      = getattr(core, '_bought_val', 0.0) + int(cp * qty)
                                    core.cash             = max(0.0, core.cash - int(cp * qty))
                                    core.partial_sold     = False
                                    core.partial_sold_2   = False
                                    core.second_buy_price = cp * 0.98   # 1차 진입가 -2%
                                    core.second_buy_cash  = reserve_cash
                                    core.second_buy_done  = False
                                score_str = " | ".join(c_score_reasons[:3])
                                self.add_log(f"💎 {c_nm} 코어 1차 매수 | {qty}주 @ {cp:,}원 | 점수 {c_score}점 ({int(first_ratio*100)}%) | 2차 예약 {cp*0.98:,.0f}원 | AI: {ai_reason[:40]}")
                                self._log_trade(c_tk, c_nm, 'BUY', cp, "RSI+멀티점수 코어", f"RSI골든크로스 + 점수{c_score}pt [{score_str}] AI승인 — 1차({int(first_ratio*100)}%)")
                                self._send_trade_telegram(self._fmt_trade_msg("💎", f"코어 1차 매수 ({int(first_ratio*100)}%)", c_tk, c_nm, cp, qty, strategy=f"RSI코어 · {c_score}pt/{c_threshold}pt", ai_reason=ai_reason, note=f"2차 예약: {cp*0.98:,.0f}원 (-2%)"))

                elif c_sig == 'SELL' and c_sh > 0 and is_core_cd:
                    # RSI 데드크로스 → 전량 매도 (floor_shares 제거)
                    if c_avg > 0 and self._sell_order(c_tk, c_sh, core, c_nm):
                        core_profit = _net_profit(cp, c_avg, c_sh)
                        with self.lock:
                            core.last_order_time = time.time()
                            core.status         = "체결 대기 ⏳"
                            core.shares         = 0
                            core._bought_val     = 0.0
                            core.partial_sold     = False
                            core.partial_sold_2   = False
                            core.second_buy_price = 0.0
                            core.second_buy_cash  = 0.0
                            core.second_buy_done  = False
                            core.bull_pyramid_done= False
                            self.pnl_this_turn   += core_profit
                        self._record_daily_pnl(core_profit)
                        self.add_log(f"💎 {c_nm} 코어 매도 전량 | {c_sh}주 @ {cp:,}원 | 손익: {core_profit:+,.0f}원")
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
                with self.lock: st_nm = self.satellite_strategies.get(ticker, 'RSI'); p_sh = pos.shares; p_avg = pos.avg_price; p_max = pos.max_price; p_cash = pos.cash; p_nm = pos.name
                price = self.live_prices.get(ticker) or getattr(pos, 'kis_current_price', 0) or (self.kis.get_current_price(ticker) if self.kis else 0)
                if not price or price <= 0: continue
                with self.lock: pos._last_price = price
                    
                from strategy import get_signal_by_strategy
                ex_df = self._get_extended_ohlcv(ticker, price)
                sig, _, ind_val = get_signal_by_strategy(ticker, st_nm, kis_api=self.kis, df=ex_df)
                if price <= 0: continue

                # ── BULL 국면 진입 신호 보완 ────────────────────────────────
                # RSI 30/70 전략은 BULL 장에서 RSI 50~70이 대부분이라 BUY 신호가 거의 안 뜸.
                # 조건 A: RSI ≤ 65 + bull_score ≥ 1 (BULL에서 50~65도 매수 구간)
                # 조건 B: MA5 > MA20 정배열 + 현재가가 MA5 이내(2%) 눌림목
                if sig != 'BUY' and regime == "BULL" and p_sh == 0:
                    try:
                        if not ex_df.empty and 'close' in ex_df.columns:
                            _closes_b  = ex_df['close'].dropna()
                            _rsi_bull  = float(calc_rsi(_closes_b).iloc[-1])
                            _bull_sc, _bull_reasons = get_bull_momentum_score(ex_df)
                            _bull_cond_a = (_rsi_bull <= 65) and (_bull_sc >= 1)
                            _bull_cond_b = False
                            if len(_closes_b) >= 22:
                                _ma5_b  = float(_closes_b.rolling(5).mean().iloc[-1])
                                _ma20_b = float(_closes_b.rolling(20).mean().iloc[-1])
                                _bull_cond_b = (_ma5_b > _ma20_b) and (price <= _ma5_b * 1.02)
                            if _bull_cond_a or _bull_cond_b:
                                sig = 'BUY'
                                ind_val = _rsi_bull
                                _bull_why = (f"RSI={_rsi_bull:.1f} bull_score={_bull_sc}" if _bull_cond_a
                                             else f"MA5눌림목(MA5={_closes_b.rolling(5).mean().iloc[-1]:,.0f})")
                                self.add_log(f"🚀 [BULL 위성 진입] {ticker} {_bull_why} → BUY 오버라이드")
                    except Exception as _be:
                        logger.debug(f"BULL 위성 오버라이드 오류: {_be}")
                # ─────────────────────────────────────────────────────────────

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
                                pos.last_order_time = time.time(); pos.max_price = 0; pos.status = "체결 대기 ⏳"
                                pos.shares = 0
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
                        if self._sell_order(ticker, p_sh, pos, p_nm):
                            with self.lock:
                                pos.last_order_time = time.time(); pos.max_price = 0; pos.status = "체결 대기 ⏳"
                                pos.shares = 0  # [BUG-C1] 트레일링 익절 전량 매도 후 잔여주수 초기화
                            profit = _net_profit(price, p_avg, p_sh)
                            self._log_trade(ticker, p_nm, 'SELL', price, st_nm, "ATR 트레일링 익절", profit=profit)
                            self._send_trade_telegram(self._fmt_trade_msg("🎯", "트레일링 익절", ticker, p_nm, price, p_sh, profit=profit, strategy=st_nm, note="ATR 트레일링 스탑 발동"))
                            with self.lock:
                                self.pnl_this_turn += profit
                                # [W-NEW-03] 트레일링 익절 수익도 코어 재투자 적용
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
                        if self._sell_order(ticker, p_sh, pos, p_nm):
                            with self.lock:
                                pos.last_order_time = time.time(); pos.status = "체결 대기 ⏳"
                                pos.second_buy_done = True; pos.pyramid_done = True
                                pos.partial_sold = False; pos.partial_sold_2 = False; pos.overext_sell_count = 0  # [C-NEW-01/W-NEW-02]
                                pos.second_buy_price = 0; pos.second_buy_cash = 0
                                pos.shares = 0  # [BUG-M5] 하드 손절 전량 매도 후 잔여주수 초기화
                            profit = _net_profit(price, p_avg, p_sh)
                            self._log_trade(ticker, p_nm, 'SELL', price, st_nm, "ATR 하드 손절", profit=profit)
                            self._send_trade_telegram(self._fmt_trade_msg("💥", "손절 체결", ticker, p_nm, price, p_sh, profit=profit, strategy=st_nm, note="ATR 하드 손절선 이탈"))
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
                                pos.shares = 0; pos.partial_sold = False; pos.partial_sold_2 = False; pos.overext_sell_count = 0
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
                        if _oe_cnt < 2 and p_sh > 1:
                            # 1차 / 2차: 30% 매도
                            _q30 = max(1, int(p_sh * 0.30))
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
                                    pos.overext_sell_count = 3
                                    pos.status = "과열 선익절 3차 전량 ✅"
                                    pos.shares = 0; pos.partial_sold = False; pos.partial_sold_2 = False
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
                        if self.gemini:
                            pnl_pct_s = (price / p_avg - 1) * 100
                            self._trigger_ai_partial_exit(pos, ticker, p_nm, price, p_avg, pnl_pct_s, regime)
                            with self.lock: pos.status = f"AI 익절 검토 중 (+{pnl_pct_s:.1f}%) 🤖"
                        else:
                            with self.lock: pos.ai_exit_decision = "SELL_PARTIAL"
                    elif s_decision == "HOLD":
                        with self.lock:
                            pos.status = f"AI 홀드 ⏳"
                    else:
                        sell_qty = max(1, p_sh // 2)
                        if self._sell_order(ticker, sell_qty, pos, p_nm):
                            with self.lock:
                                pos.last_order_time   = time.time()
                                pos.partial_sold      = True
                                pos.ai_exit_decision  = None
                                pos.status            = "부분익절 ✅"
                                pos.shares            = max(0, pos.shares - sell_qty)
                            profit = _net_profit(price, p_avg, sell_qty)
                            _pnl_s1 = (price / p_avg - 1) * 100
                            _thr_s1 = "15%(BULL)" if regime == "BULL" else "10%"
                            self._log_trade(ticker, p_nm, 'SELL', price, st_nm, f"부분 익절 +{_thr_s1} ({sell_qty}주)", profit=profit)
                            self._send_trade_telegram(self._fmt_trade_msg("🎯", f"부분 익절 +{_thr_s1}", ticker, p_nm, price, sell_qty, profit=profit, strategy=st_nm, note=f"나머지 {p_sh - sell_qty}주는 AI 판단 트레일링"))
                            with self.lock: self.pnl_this_turn += profit
                            self._record_daily_pnl(profit)

                # ── 2차 분할 익절: +20%(일반) / +30%(BULL) 도달 시 AI 판단 ──
                if (p_sh > 0 and p_avg > 0 and is_cd_passed
                        and getattr(pos, 'partial_sold', False)
                        and not getattr(pos, 'partial_sold_2', False)
                        and price >= p_avg * _sat_partial2_mult):
                    s_decision = getattr(pos, 'ai_exit_decision', None)
                    if s_decision is None:
                        if self.gemini:
                            pnl_pct_s = (price / p_avg - 1) * 100
                            self._trigger_ai_partial_exit(pos, ticker, p_nm, price, p_avg, pnl_pct_s, regime)
                            with self.lock: pos.status = f"AI 익절 검토 중 (+{pnl_pct_s:.1f}%) 🤖"
                        else:
                            with self.lock: pos.ai_exit_decision = "SELL_ALL"
                    elif s_decision == "HOLD":
                        with self.lock:
                            pos.status = f"AI 홀드 ⏳"
                    else:
                        sell_qty = pos.shares
                        if sell_qty > 0 and self._sell_order(ticker, sell_qty, pos, p_nm):
                            with self.lock:
                                pos.last_order_time   = time.time()
                                pos.partial_sold_2    = True
                                pos.ai_exit_decision  = None
                                pos.status            = "2차익절 ✅"
                                pos.shares            = 0
                            profit = _net_profit(price, p_avg, sell_qty)
                            _thr_s2 = "30%(BULL)" if regime == "BULL" else "20%"
                            self._log_trade(ticker, p_nm, 'SELL', price, st_nm, f"2차 전량 익절 +{_thr_s2} ({sell_qty}주)", profit=profit)
                            self._send_trade_telegram(self._fmt_trade_msg("🏆", f"2차 전량 익절 +{_thr_s2}", ticker, p_nm, price, sell_qty, profit=profit, strategy=st_nm, note="AI 판단 익절"))
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
                    sq = int((pos.second_buy_cash * 0.98) // price)
                    if sq > 0 and self._buy_order(ticker, sq, pos, p_nm):
                        with self.lock:
                            pos.last_order_time = time.time(); pos.second_buy_done = True
                            pos.second_buy_cash = 0; pos.status = "2차매수 ✅"
                            # [BUG-C5] 2차 매수 후 평단가·보유주수 즉시 갱신 (KIS 동기화 전 손절 방지)
                            new_shares = pos.shares + sq
                            if new_shares > 0:
                                pos.avg_price = round((pos.avg_price * pos.shares + price * sq) / new_shares, 2)
                            pos.shares = new_shares
                        self._log_trade(ticker, p_nm, 'BUY', price, st_nm, f"2차 분할 매수 눌림목 ({sq}주)")
                        self._send_trade_telegram(self._fmt_trade_msg("🛒", "2차 분할 매수", ticker, p_nm, price, sq, strategy=st_nm, note="-2% 눌림목 포착"))

                # 당일 AI 거절 블랙리스트 종목은 매수 시도 자체를 차단
                if sig == 'BUY' and p_sh == 0 and self._is_satellite_blacklisted(ticker):
                    pos.status = "당일 블랙리스트 🚫"
                    pos.status_msg = f"오늘 거절됨: {self._satellite_rejects.get(ticker, '')[:30]}"
                    continue

                # 실적 발표 D-3 이내 → 신규 진입 차단 (깜짝 손실 방지) — US봇 동일
                if sig == 'BUY' and p_sh == 0 and is_cd_passed and is_golden_hours and self.news_monitor:
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

                if sig == 'BUY' and p_sh == 0 and is_cd_passed and is_golden_hours:
                    # ── 통합 진입 점수 체크 (1차 게이트) ──
                    _frgn_net = 0
                    try:
                        if self.kis and hasattr(self.kis, 'get_foreign_buy_by_ticker'):
                            _fi = self.kis.get_foreign_buy_by_ticker(ticker)
                            if _fi:
                                _frgn_net = int(_fi.get("frgn_net", 0))
                    except Exception:
                        pass
                    entry_score, entry_reasons = calculate_entry_score(ex_df, price, regime, frgn_net=_frgn_net)
                    entry_threshold = get_entry_threshold(regime, 'satellite')
                    if entry_score < entry_threshold:
                        pos.status = f"진입 점수 부족 ({entry_score}/{entry_threshold}) ⏳"
                        pos.status_msg = f"현재 {entry_score}점 — {entry_threshold}점 이상 필요 | 부족 항목: {', '.join(set(['①20MA','②60MA','③정배열','④MACD','⑤RSI','⑥거래량','⑦전일종가','⑧BB위치','⑨수급','⑩120MA']) - set([r[:4] for r in entry_reasons]))}"
                        continue
                    score_ratio = get_budget_ratio_from_score(entry_score, entry_threshold)

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
                            if self.gemini:
                                pos.status     = "AI 심사 중 🤖"
                                pos.status_msg = f"하락장 저점 신호 | {bear_reason_str[:60]} — AI 최종 승인 대기 중..."
                                trade_ctx = self._build_trade_context(ticker, p_nm, price, ex_df, st_nm, regime)
                                decision, ai_reason = self.gemini.ai_approve_trade(sig, p_nm, ticker, price, st_nm, ind_val, self.hot_sectors, get_recent_trades(self.user_id, ticker), load_ai_rules(self.user_id) + "\n" + getattr(self, 'current_ai_market_view', '') + ("\n\n[📊 섹터 가이드 / 커스텀 전략]\n" + self.sector_guide if self.sector_guide else ''), context=trade_ctx)
                                if decision:
                                    if self._buy_order(ticker, qty, pos, p_nm):
                                        with self.lock:
                                            pos.last_order_time = time.time(); pos.status = "체결 대기 ⏳"
                                            pos.status_msg      = f"AI 승인: {ai_reason}"
                                        self._log_trade(ticker, p_nm, 'BUY', price, st_nm, f"하락장 저점포착 AI승인 [{bear_reason_str}]")
                                        self._send_trade_telegram(self._fmt_trade_msg("🎣", f"하락장 저점 매수 ({bear_label})", ticker, p_nm, price, qty, strategy=st_nm, ai_reason=ai_reason, note=bear_reason_str))
                                else:
                                    pos.status     = "AI 거절(하락장) 🛑"
                                    pos.status_msg = f"거절 이유: {ai_reason}"
                                    self._add_satellite_reject(ticker, ai_reason)
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

                    # ── 국면별 타이밍 신호 + 통합 점수 합산 포지션 사이징 ──────
                    if regime == "BULL":
                        bull_score, bull_reasons = get_bull_momentum_score(ex_df)
                        # 통합 점수(score_ratio)와 국면 타이밍 신호를 합산
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

                    # 1차 매수: entry_ratio 의 75%, 나머지 25%는 2차 분할 매수용 유보
                    first_ratio   = entry_ratio * 0.75
                    reserve_ratio = entry_ratio * 0.25
                    entry_cash    = p_cash * first_ratio
                    reserve_cash  = p_cash * reserve_ratio

                    # ── 매수 검토 리포트 발송 (친구 AI 스타일) ──────────────
                    try:
                        _stats = self._calc_price_stats(ex_df, price)
                        _stats['extra'] = f"전략 [{st_nm}] / {regime_label}"
                        self._send_telegram(self._fmt_scan_report(
                            theme="📊 위성 매수 신호",
                            candidates=[{'name': p_nm, 'ticker': ticker, 'price': price, 'stats': _stats}],
                            regime=regime,
                            action_note="AI 심사 후 자동주문" if self.gemini else "알고리즘 자동주문"
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

                    if self.gemini:
                        pos.status     = "AI 심사 중 🤖"
                        pos.status_msg = f"매수 신호 발생 | {st_nm} | RSI {ind_val:.1f} — AI 최종 승인 대기 중..."
                        trade_ctx = self._build_trade_context(ticker, p_nm, price, ex_df, st_nm, regime)
                        if _52w_note_kr:
                            trade_ctx += f"\n[52주 신고가] {_52w_note_kr}"
                        decision, ai_reason = self.gemini.ai_approve_trade(sig, p_nm, ticker, price, st_nm, ind_val, self.hot_sectors, get_recent_trades(self.user_id, ticker), load_ai_rules(self.user_id) + "\n" + getattr(self, 'current_ai_market_view', '') + ("\n\n[📊 섹터 가이드 / 커스텀 전략]\n" + self.sector_guide if self.sector_guide else ''), context=trade_ctx)
                        if decision:
                            qty = int((entry_cash * 0.98) // price)
                            if qty > 0 and self._buy_order(ticker, qty, pos, p_nm):
                                with self.lock:
                                    pos.last_order_time = time.time(); pos.status = "체결 대기 ⏳"
                                    pos.status_msg      = f"AI 승인: {ai_reason}"
                                    pos.second_buy_price = price * 0.98   # -2% 눌림목 발동가
                                    pos.second_buy_cash  = reserve_cash
                                    pos.second_buy_done  = False
                                    pos.pyramid_done     = False
                                    pos.partial_sold     = False
                                    pos.partial_sold_2   = False  # [C-NEW-01] 신규 진입 시 반드시 초기화
                                self._log_trade(ticker, p_nm, 'BUY', price, st_nm, f"AI 승인 [{regime_label}] 1차({int(first_ratio*100)}%) ({ai_reason})")
                                self._send_trade_telegram(self._fmt_trade_msg("📈", f"AI 매수 승인  ({int(first_ratio*100)}% 1차)", ticker, p_nm, price, qty, strategy=f"{st_nm}  ·  {regime_label}", ai_reason=ai_reason, note=regime_reason_str))
                        else:
                            pos.status     = "AI 거절 🛑"
                            pos.status_msg = f"거절 이유: {ai_reason}"
                            # 당일 블랙리스트 등록 — 같은 이유로 재편입 금지
                            self._add_satellite_reject(ticker, ai_reason)
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
                                pos.second_buy_price = price * 0.98
                                pos.second_buy_cash  = reserve_cash
                                pos.second_buy_done  = False
                                pos.pyramid_done     = False
                                pos.partial_sold     = False
                                pos.partial_sold_2   = False  # [C-NEW-01] 알고리즘 경로도 동일하게 초기화
                            self._log_trade(ticker, p_nm, 'BUY', price, st_nm, f"알고리즘 [{regime_label}] 1차({int(first_ratio*100)}%): {regime_reason_str}")
                            self._send_trade_telegram(self._fmt_trade_msg("📈", f"알고리즘 매수  ({int(first_ratio*100)}% 1차)", ticker, p_nm, price, qty, strategy=f"{st_nm}  ·  {regime_label}", note=regime_reason_str))

                elif sig == 'SELL' and p_sh > 0 and is_cd_passed:
                    if self.gemini:
                        pos.status     = "AI 심사 중 🤖"
                        pos.status_msg = f"매도 신호 발생 | RSI {ind_val:.1f} — AI 최종 승인 대기 중..."
                        trade_ctx = self._build_trade_context(ticker, p_nm, price, ex_df, st_nm, regime)
                        decision, ai_reason = self.gemini.ai_approve_trade(sig, p_nm, ticker, price, st_nm, ind_val, self.hot_sectors, get_recent_trades(self.user_id, ticker), load_ai_rules(self.user_id) + "\n" + getattr(self, 'current_ai_market_view', '') + ("\n\n[📊 섹터 가이드 / 커스텀 전략]\n" + self.sector_guide if self.sector_guide else ''), context=trade_ctx)
                        if decision:
                            if self._sell_order(ticker, p_sh, pos, p_nm):
                                with self.lock:
                                    pos.last_order_time = time.time(); pos.status = "체결 대기 ⏳"
                                    pos.status_msg      = f"AI 승인: {ai_reason}"
                                    pos.shares = 0  # [BUG-C2] AI 승인 매도 후 잔여주수 초기화
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
                                pos.last_order_time = time.time(); pos.status = "체결 대기 ⏳"
                                pos.shares = 0  # [BUG-C2] 알고리즘 직통 매도 후 잔여주수 초기화
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
        # 모멘텀은 매도 즉시 다음 분에 새 종목 스캔, 위성도 동일하게 대응.
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

        # ── 🚀 테마·급등주 모멘텀 슬롯 매매 ─────────────────────────────
        if is_golden_hours:
            self._run_momentum_slot(regime)

        self._save_state()

    def _serialize_one_momentum(self, mp):
        """단일 모멘텀 포지션 dict → JSON 직렬화 (datetime→str)."""
        if mp is None:
            return None
        mp = dict(mp)
        et = mp.get('enter_time')
        if isinstance(et, datetime):
            mp['enter_time'] = et.strftime('%Y-%m-%dT%H:%M:%S')
        return mp

    def _deserialize_one_momentum(self, mp):
        """JSON → 단일 모멘텀 포지션 dict 복원 (str→datetime)."""
        if mp is None:
            return None
        mp = dict(mp)
        et = mp.get('enter_time')
        if isinstance(et, str):
            try:
                mp['enter_time'] = datetime.strptime(et, '%Y-%m-%dT%H:%M:%S')
            except Exception:
                mp['enter_time'] = None
        return mp

    def _check_momentum_exit_one(self, slot_idx: int, mp: dict, now, regime: str = "NEUTRAL") -> bool:
        """슬롯 idx의 모멘텀 포지션 청산 조건 체크. 청산 시 True 반환."""
        # C-01: kis가 None이면 체크 불가 → 청산 없이 리턴 (멀티스레드 재시작 타이밍 안전)
        if not self.kis:
            return False

        ticker  = mp['ticker']
        name    = mp['name']
        shares  = mp.get('shares', 0)
        avg_p   = mp.get('avg_price', 0)
        atr     = mp.get('atr', avg_p * 0.02)
        enter_t = mp.get('enter_time')

        if shares <= 0:
            self.momentum_positions[slot_idx] = None
            return True

        price = self.live_prices.get(ticker) or self.kis.get_current_price(ticker)
        if not price or price <= 0:
            return False

        if price > mp.get('peak_price', avg_p):
            mp['peak_price'] = price
        peak_p = mp.get('peak_price', avg_p)

        # 상한가 여부
        is_upper_limit = price >= avg_p * 1.295
        is_post_upper  = (not is_upper_limit) and avg_p > 0 and (price / avg_p - 1) >= 0.20

        vol_fade = False
        giveback_signal = 'HOLD'
        giveback_reason = ''
        try:
            # 5분봉 집계: 1분봉 25개 조회 후 5개씩 묶어 5분봉 거래량 산출
            candles = self.kis.get_minute_candles(ticker, count=25)
            if candles and len(candles) >= 5:
                # 5분봉 단위 거래량 집계 (5개씩 묶음, 최신봉이 마지막)
                five_min_vols = []
                chunk_count = len(candles) // 5
                for i in range(chunk_count):
                    chunk = candles[i*5:(i+1)*5]
                    five_min_vols.append(sum(float(c.get('volume', 0)) for c in chunk))
                # 잔여 1분봉(최신 미완성 5분봉)은 제외 → 완성된 5분봉만 사용
                if len(five_min_vols) >= 2:
                    peak_vol   = mp.get('peak_volume', 0)
                    recent_vol = five_min_vols[-1]          # 가장 최근 완성 5분봉 거래량
                    if recent_vol > peak_vol:
                        mp['peak_volume'] = recent_vol
                        peak_vol = recent_vol
                    if peak_vol > 0:
                        if is_upper_limit:
                            pass  # 상한가 구간: 페이드 체크 스킵
                        elif is_post_upper:
                            if recent_vol <= peak_vol * 0.30:
                                vol_fade = True
                        else:
                            if recent_vol <= peak_vol * 0.5:   # 이하(≤)
                                vol_fade = True
                is_ride = (peak_p / avg_p - 1) * 100 >= 10 if avg_p > 0 else False
                giveback_signal, _gpct, giveback_reason = check_giveback_stop(
                    candles, avg_p, peak_p, is_momentum_ride=is_ride
                )
        except Exception:
            pass

        # W-06: enter_t가 None(역직렬화 실패)이면 timeout 체크 비활성화
        # 국면별 최대 보유 시간: BULL 추세 추종 → 90분, NEUTRAL → 60분
        _max_hold_min = 90 if regime == "BULL" else 60
        try:
            time_over = enter_t is not None and (now - enter_t).total_seconds() / 60 > _max_hold_min
        except Exception:
            time_over = False

        # ── PARTIAL_EXIT_30: MA5 이탈+고점미달 → 보유량 30% 축소 (슬롯 유지) ─────
        if giveback_signal == 'PARTIAL_EXIT_30' and shares > 1:
            partial_qty = max(1, int(shares * 0.30))
            if self.kis.sell_market_order(ticker, partial_qty):
                partial_profit = _net_profit(price, avg_p, partial_qty)
                with self.lock:
                    if self.internal_cash is not None:
                        self.internal_cash += price * partial_qty * (1 - _SELL_FEE - _SELL_TAX)
                    self._last_trade_ts = time.time()
                    self.pnl_this_turn += partial_profit
                    mp['shares'] = shares - partial_qty   # [BUG-C2] 락 내부로 이동 — 레이스 컨디션 방지
                self._log_trade(ticker, name, 'SELL', price, "모멘텀슬롯",
                                  f"giveback MA5 이탈 → 30% 축소 ({giveback_reason})", profit=partial_profit)
                self.add_log(f"✂️ 모멘텀#{slot_idx+1} 부분청산 30% | {name} {partial_qty}주 @ {price:,.0f}원 | {giveback_reason} | 손익: {partial_profit:+,.0f}원")
                self._send_trade_telegram(self._fmt_trade_msg("✂️", f"모멘텀#{slot_idx+1} 30% 축소",
                    ticker, name, price, partial_qty, profit=partial_profit,
                    strategy="모멘텀슬롯", note=f"MA5 이탈 30% 축소 — 잔여 {mp['shares']}주 홀딩"))
                self._record_daily_pnl(partial_profit)
                self._record_ticker_loss(ticker, partial_profit)  # [BUG-4] 부분 손실도 종목별 캡에 반영
            return False  # 슬롯 유지 (잔여 70% 포지션 계속 관리)

        # ── PARTIAL_EXIT_70: 30% 반납 신호 → 보유량 70% 선익절 (슬롯은 유지) ──────
        if giveback_signal == 'PARTIAL_EXIT_70' and shares > 1:
            partial_qty = max(1, int(shares * 0.70))
            if self.kis.sell_market_order(ticker, partial_qty):
                partial_profit = _net_profit(price, avg_p, partial_qty)
                with self.lock:
                    if self.internal_cash is not None:
                        self.internal_cash += price * partial_qty * (1 - _SELL_FEE - _SELL_TAX)
                    self._last_trade_ts = time.time()
                    self.pnl_this_turn += partial_profit
                    mp['shares'] = shares - partial_qty   # [BUG-C2] 락 내부로 이동 — 레이스 컨디션 방지
                self._log_trade(ticker, name, 'SELL', price, "모멘텀슬롯",
                                  f"giveback 30% 반납 → 70% 선익절 ({giveback_reason})", profit=partial_profit)
                self.add_log(f"✂️ 모멘텀#{slot_idx+1} 부분청산 70% | {name} {partial_qty}주 @ {price:,.0f}원 | {giveback_reason} | 손익: {partial_profit:+,.0f}원")
                self._send_trade_telegram(self._fmt_trade_msg("✂️", f"모멘텀#{slot_idx+1} 70% 부분청산",
                    ticker, name, price, partial_qty, profit=partial_profit,
                    strategy="모멘텀슬롯", note=f"giveback 30% 반납 — 잔여 {mp['shares']}주 홀딩"))
                self._record_daily_pnl(partial_profit)
                self._record_ticker_loss(ticker, partial_profit)  # [BUG-4] 부분 손실도 종목별 캡에 반영
            return False  # 슬롯 유지 (잔여 포지션 계속 관리)

        # 모멘텀 슬롯 출구 전략 (국면별 차등 적용):
        # BULL : 손절 -4% / 익절 +8% / 90분 → 추세 추종, 수익 극대화
        # NEUTRAL: 손절 -3% / 익절 +5% / 60분 → 빠른 수익 실현 (기존)
        if regime == "BULL":
            MOMENTUM_STOP_PCT   = 0.96   # -4%  (추세장 정상 눌림에 손절 방지)
            MOMENTUM_TARGET_PCT = 1.08   # +8%  (추세 올라타기)
        else:  # NEUTRAL
            MOMENTUM_STOP_PCT   = 0.97   # -3%
            MOMENTUM_TARGET_PCT = 1.05   # +5%

        sell_reason = None
        if vol_fade:
            sell_reason = "거래량 페이드(5분봉 고점 대비 50% 이하)"
        elif avg_p > 0 and price >= avg_p * MOMENTUM_TARGET_PCT:
            sell_reason = f"+5% 목표 달성 ({avg_p:,.0f}→{price:,.0f})"
        elif giveback_signal == 'FULL_EXIT':
            sell_reason = f"5분봉 반납률 전량 이탈: {giveback_reason}"
        elif avg_p > 0 and price <= avg_p * MOMENTUM_STOP_PCT:
            sell_reason = f"고정 -3% 손절 ({avg_p:,.0f}→{price:,.0f}) [손익비 1:1.7]"
        elif time_over:
            sell_reason = "보유 60분 초과 강제 청산"

        if sell_reason:
            if not self.kis.sell_market_order(ticker, shares):
                self.add_log(f"⚠️ 모멘텀#{slot_idx+1} 청산 주문 실패: {name}({ticker})")
                return False
            profit = _net_profit(price, avg_p, shares)
            with self.lock:
                if self.internal_cash is not None:
                    self.internal_cash += price * shares * (1 - _SELL_FEE - _SELL_TAX)
                self._last_trade_ts = time.time()
                self.pnl_this_turn += profit  # [BUG-9] 두 번의 락 취득 → 하나로 합침 (레이스 컨디션 방지)
            self._log_trade(ticker, name, 'SELL', price, "모멘텀슬롯", sell_reason, profit=profit)
            self.add_log(f"🏁 모멘텀#{slot_idx+1} 청산 | {name}({ticker}) {shares}주 @ {price:,.0f}원 | {sell_reason} | 손익: {profit:+,.0f}원")
            self._send_trade_telegram(self._fmt_trade_msg("🏁", f"모멘텀#{slot_idx+1} 청산", ticker, name, price, shares, profit=profit, strategy="모멘텀슬롯", note=sell_reason))
            self._record_daily_pnl(profit)
            self._record_ticker_loss(ticker, profit)   # 종목별 일일 손실 추적
            # 수익의 REINVEST_RATIO(50%) → 코어 슬롯 재투자  [I-NEW-05] 상수 통일
            if profit > 0 and self.core_positions:
                reinvest = profit * REINVEST_RATIO
                per_core = reinvest / len(self.core_positions)
                with self.lock:
                    for core in self.core_positions:
                        core.cash += per_core
                self.add_log(f"💰 모멘텀 수익 재투자: {reinvest:,.0f}원 → 코어 {len(self.core_positions)}종목 ({per_core:,.0f}원씩)")
            self._add_momentum_exit(ticker)
            self.momentum_positions[slot_idx] = None
            return True
        return False

    def _run_momentum_slot(self, regime: str):
        """모멘텀 슬롯 3개 독립 관리 — 진입/청산 각 슬롯 독립 운영."""
        if not self.kis:
            return
        now = _now_kst()

        # ── A. 보유 중인 슬롯 청산 체크 ────────────────────────────────
        for i, mp in enumerate(self.momentum_positions):
            if mp is not None:
                try:
                    self._check_momentum_exit_one(i, mp, now, regime=regime)
                except Exception as e:
                    logger.error(f"[{self.mode_name}] 모멘텀#{i+1} 청산 체크 오류: {e}", exc_info=True)

        # ── B. 빈 슬롯 진입 스캔 ────────────────────────────────────────
        empty_slots = [i for i, mp in enumerate(self.momentum_positions) if mp is None]
        if not empty_slots or regime == "BEAR":
            return

        if time.time() - self._last_momentum_scan < self._momentum_scan_interval:
            return
        self._last_momentum_scan = time.time()

        try:
            clear_expired_cache()
            hits = scan_hot_momentum(kis=self.kis, top_n=len(empty_slots) * 3, verbose=False)
        except Exception as e:
            logger.warning(f"[{self.mode_name}] 모멘텀 스캔 오류: {e}")
            return

        # ── 급등패턴 매칭 후보 합산 (보너스 점수 부여) ──────────────────
        # 장 시작 전(09:00~09:30) 또는 오전장(09:00~11:00)에 패턴 후보를 우선 추가
        try:
            with self.lock:
                _held_now = {mp['ticker'] for mp in self.momentum_positions if mp is not None}
                _held_now |= {t for t, p in self.satellite_positions.items() if p.shares > 0}
            pattern_hits = scan_pattern_matches(
                kis=self.kis, top_n=len(empty_slots) * 2,
                exclude_tickers=_held_now, verbose=False,
            )
            if pattern_hits:
                # 중복 제거: hot_momentum에 이미 있으면 패턴 보너스 +5점만 추가
                hot_tickers = {h['ticker'] for h in hits}
                for ph in pattern_hits:
                    if ph['ticker'] in hot_tickers:
                        for h in hits:
                            if h['ticker'] == ph['ticker']:
                                h['momentum_score'] += 5.0
                                h['trigger_reason'] += ' | 🎯패턴매칭보너스'
                    else:
                        hits.append(ph)
                self.add_log(f"🎯 [급등패턴] 유사종목 {len(pattern_hits)}개 모멘텀 후보 합산")
        except Exception as _pe:
            logger.debug(f"[{self.mode_name}] 패턴 매칭 스캔 오류(무시): {_pe}")

        if not hits:
            return

        # 이미 보유 중인 종목 전부 수집 (슬롯 + 위성)
        with self.lock:
            held = {mp['ticker'] for mp in self.momentum_positions if mp is not None}
            held |= {t for t, p in self.satellite_positions.items() if p.shares > 0}

        # 예산 산정 (1회 조회 공유)
        try:
            balance = self.kis.get_account_balance()
            if not balance:
                return
            total_assets   = float(balance.get('total_cash', 0)) + float(balance.get('total_value', 0))
            available_cash = float(balance.get('total_cash', 0))
        except Exception:
            return

        used_tickers: set = set()
        for slot_idx in empty_slots:
            # 이 슬롯용 후보 탐색
            best = None
            for candidate in hits:
                ct = candidate['ticker']
                if ct in held or ct in used_tickers or self._is_momentum_blacklisted(ct):
                    continue
                # ── 당일 +20% 초과 종목 진입 금지 (이미 고점, 손실 가능성 높음) ──
                # scan_hot_momentum 반환 키: 'price_chg_pct' (hot_momentum_scanner.py 참조)
                chg = candidate.get('price_chg_pct', 0)
                if chg > 20.0:
                    self.add_log(f"⛔ 모멘텀 진입 금지: {candidate.get('name','?')}({ct}) 당일 +{chg:.1f}% 고점 과열")
                    # C-02: 락 안에서 공유 dict 수정 (레이스컨디션 방지)
                    with self.lock:
                        self._momentum_exit_times[ct] = time.time() + self._MOMENTUM_COOLDOWN_SEC
                    continue
                best = candidate
                break
            if best is None:
                continue

            b_ticker = best['ticker']
            b_name   = best['name']
            b_price  = best['price']

            # BULL 국면은 모멘텀 예산 12%로 상향 (추세장 확신 → 더 크게 베팅)
            _mom_ratio = self.momentum_budget_ratio * 1.2 if regime == "BULL" else self.momentum_budget_ratio
            budget = total_assets * _mom_ratio
            if available_cash < budget * 0.5:
                break  # 현금 부족 → 나머지 슬롯도 포기

            qty = int((budget * 0.98) // b_price)
            if qty <= 0:
                continue

            # ATR 계산
            atr_val = b_price * 0.02
            try:
                df_m = self._get_cached_base_ohlcv(b_ticker)
                if not df_m.empty and all(c in df_m.columns for c in ['high', 'low', 'close']):
                    tr = pd.concat([
                        df_m['high'] - df_m['low'],
                        (df_m['high'] - df_m['close'].shift(1)).abs(),
                        (df_m['low']  - df_m['close'].shift(1)).abs(),
                    ], axis=1).max(axis=1)
                    atr_val = float(tr.rolling(14, min_periods=1).mean().iloc[-1])
            except Exception:
                pass

            # ── 매수 검토 리포트 (친구 AI 스타일) ──────────────────────────
            try:
                _df_stat = self._get_cached_base_ohlcv(b_ticker)
                _stats   = self._calc_price_stats(_df_stat, b_price)
                _stats['extra'] = f"거래량 {best.get('vol_ratio', 0):.1f}x↑"
                _report = self._fmt_scan_report(
                    theme=f"🚀 모멘텀 급등 포착 — 슬롯#{slot_idx+1}",
                    candidates=[{'name': b_name, 'ticker': b_ticker, 'price': b_price, 'stats': _stats}],
                    regime=regime,
                    action_note="AI 심사 후 자동주문"
                )
                self._send_telegram(_report, 'misc')
            except Exception:
                pass

            # AI 심사
            if self.gemini:
                # 풀 기술 지표 컨텍스트 + 모멘텀 전용 신호 정보 결합
                _ex_df_m = self._get_extended_ohlcv(b_ticker, b_price)
                trade_ctx = self._build_trade_context(
                    b_ticker, b_name, b_price, _ex_df_m, "모멘텀슬롯", regime
                )
                trade_ctx += (
                    f"\n[모멘텀 신호] 슬롯#{slot_idx+1} | "
                    f"트리거: {best['trigger_reason']} | "
                    f"점수: {best['momentum_score']:.1f}점 | "
                    f"ATR: {atr_val:,.0f}원"
                )
                m_decision, m_ai_reason = self.gemini.ai_approve_trade(
                    'BUY', b_name, b_ticker, b_price, "모멘텀슬롯",
                    {"momentum_score": best['momentum_score']}, self.hot_sectors,
                    get_recent_trades(self.user_id, b_ticker),
                    load_ai_rules(self.user_id) + ("\n\n[📊 섹터 가이드]\n" + self.sector_guide if self.sector_guide else ''),
                    context=trade_ctx
                )
                if not m_decision:
                    # 당일 AI 거절 횟수 카운트
                    with self.lock:
                        self._refresh_blacklist()
                        reject_count = self._momentum_ai_rejects.get(b_ticker, 0) + 1
                        self._momentum_ai_rejects[b_ticker] = reject_count

                    if reject_count >= 3:
                        # 3회 거절 → 당일 블랙리스트 (오늘 더 이상 심사 없음)
                        self.add_log(f"🚫 모멘텀#{slot_idx+1} 당일 블랙리스트: {b_name} (AI {reject_count}회 거절)")
                        self._send_reject_telegram(
                            f"🚫 <b>모멘텀#{slot_idx+1} 당일 차단</b>  ·  {self.alert_icon} {self.mode_name}\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"📌 <b>{b_name}</b>  <code>{b_ticker}</code>\n"
                            f"💰 {b_price:,.0f}원\n"
                            f"🔒 AI {reject_count}회 거절 — 오늘 하루 진입 차단"
                        )
                        with self.lock:
                            # exit_ts를 현재 시각으로 → 하루 종일 차단 (장 마감 후 _refresh_blacklist 초기화)
                            self._momentum_exit_times[b_ticker] = time.time() + 86400
                    else:
                        # 1~2회 거절 → 10분 쿨다운 후 재심사 (손절 30분보다 짧게, 모멘텀 창 유지)
                        # C-03: COOLDOWN_SEC=1800에서 600초 남김 → 실제 10분 후 재진입 가능
                        _AI_REJECT_COOLDOWN = 600  # 10분
                        self.add_log(f"🛑 모멘텀#{slot_idx+1} AI 거절({reject_count}/3): {b_name} — {m_ai_reason}")
                        with self.lock:
                            self._refresh_blacklist()
                            self._momentum_exit_times[b_ticker] = time.time() - (self._MOMENTUM_COOLDOWN_SEC - _AI_REJECT_COOLDOWN)
                    used_tickers.add(b_ticker)
                    continue
                buy_label  = f"🚀 AI승인 모멘텀#{slot_idx+1}"
                m_buy_note = f"[AI승인] {best['trigger_reason']} 점수:{best['momentum_score']:.1f} ({m_ai_reason})"
            else:
                m_ai_reason = "알고리즘 자동승인"
                buy_label   = f"🚀 모멘텀#{slot_idx+1}"
                m_buy_note  = f"[알고리즘] {best['trigger_reason']} 점수:{best['momentum_score']:.1f}"

            if not self.kis.buy_market_order(b_ticker, qty):
                self.add_log(f"⚠️ 모멘텀#{slot_idx+1} 매수 실패: {b_name}({b_ticker})")
                continue

            with self.lock:
                if self.internal_cash is not None:
                    self.internal_cash = max(0.0, self.internal_cash - b_price * qty * 1.00015)
                self._last_trade_ts = time.time()
            available_cash = max(0.0, available_cash - b_price * qty)  # 로컬 잔고 갱신

            # [C-NEW-06] 포지션 딕셔너리 저장 실패 시 실매수 후 미추적 상태 방지 — try-except 보호
            try:
                self.momentum_positions[slot_idx] = {
                    'ticker': b_ticker, 'name': b_name, 'shares': qty,
                    'avg_price': b_price, 'atr': atr_val, 'peak_price': b_price,
                    'peak_volume': 0, 'enter_time': now,
                    'score': best['momentum_score'], 'reason': best['trigger_reason'],
                    'slot_idx': slot_idx,
                }
            except Exception as dict_err:
                logger.error(f"[{self.mode_name}] 모멘텀#{slot_idx+1} 포지션 저장 실패 → 즉시 시장가 청산: {dict_err}")
                self.kis.sell_market_order(b_ticker, qty)
                continue
            self._log_trade(b_ticker, b_name, 'BUY', b_price, "모멘텀슬롯", m_buy_note)
            self.add_log(f"{buy_label} | {b_name}({b_ticker}) {qty}주 @ {b_price:,.0f}원 | {best['trigger_reason']}")
            self._send_trade_telegram(
                f"{buy_label} 진입!  ·  {self.alert_icon} {self.mode_name}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>{b_name}</b>  <code>{b_ticker}</code>\n"
                f"💰 <b>{b_price:,.0f}원</b> × <b>{qty}주</b> = <b>{b_price*qty:,.0f}원</b>\n"
                f"🔥 {best['trigger_reason']}\n"
                f"📊 모멘텀 점수 <b>{best['momentum_score']:.1f}점</b>\n"
                f"🤖 {m_ai_reason}\n"
                f"🛡️ 손절: ATR <b>{atr_val:,.0f}원</b>  ·  🎯 익절: <b>+10% 1차 / +20% 2차</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⏰ {now.strftime('%H:%M KST')}"
            )
            used_tickers.add(b_ticker)
            held.add(b_ticker)

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
                # 뉴스 fetch (NewsMonitor 우선, 없으면 빈 문자열)
                _news = ""
                if self.news_monitor:
                    try:
                        _news = self.news_monitor.get_news_summary(name, display=3)
                    except Exception:
                        pass
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

    def _rescreen_satellites(self):
        try:
            now = _now_kst()
            # 위성은 "오늘 하루 들고 갈 종목"을 장 초반에 결정 → 10:30 이후 신규 선정 금지.
            # 10:30 이후 빈 슬롯 발생 시 당일 캐시 보유, 다음 날 08:50 사전 스크리닝에서 보충.
            if not ("09:00" <= now.strftime('%H:%M') <= "10:30") or now.weekday() >= 5:
                return
            self.add_log(f"🦅 {self.mode_name} 위성 실시간 교체 탐색 중...")
            keep_tickers = set()      # 유지 티커 (교체 슬롯으로 계산하지 않음)
            strong_keeps = set()      # 성장세 양호 — 절대 교체 대상 제외
            freed_cash = 0
            with self.lock: sat_items = list(self.satellite_positions.items())

            _GROWTH_KEEP = 3.0    # +3% 이상 → 성장세 양호, 교체 없이 강제 유지
            _LOSS_CUT    = -3.0   # -3% 이하 → 손절 교체 (관망 구간: -3%~+3%)

            for ticker, pos in sat_items:
                if pos.shares == 0:
                    freed_cash += pos.cash
                    with self.lock:
                        if ticker in self.satellite_positions: del self.satellite_positions[ticker]
                        if ticker in self.satellite_strategies: del self.satellite_strategies[ticker]
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
                            if t in self.satellite_strategies: del self.satellite_strategies[t]
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
                n_rejects = len(self._satellite_rejects)
            raw_info, self.hot_sectors = select_satellites(
                kis=self.kis, n=self.num_satellites + n_needed + n_rejects + 3,
                verbose=False, gemini_client=self.gemini, bear_mode=(self.market_regime == "BEAR"),
                sector_guide=self.sector_guide, real_kis=self.real_kis
            )
            if self.hot_sectors:
                self.add_log(f"🔥 강세 섹터 감지: {', '.join(self.hot_sectors[:4])}")
            else:
                self.add_log("⚠️ 강세 섹터 없음 (전 섹터 하락 — 상대 강세 기준으로 후보 선정)")
            # 현재 모멘텀 슬롯에 있는 종목 — 성격/전략이 다르므로 위성 편입 금지
            with self.lock:
                momentum_tickers = {
                    mp['ticker'] for mp in self.momentum_positions
                    if mp is not None and isinstance(mp, dict) and mp.get('ticker')
                }
            # 이미 보유 중인 종목 + 당일 AI 거절 블랙리스트 + 모멘텀 슬롯 종목 모두 제외
            pre_filter = [
                c for c in raw_info
                if c['ticker'] not in keep_tickers
                and c['ticker'] not in momentum_tickers
                and not self._is_satellite_blacklisted(c['ticker'])
            ]
            # AI 종목·전략 검토 (여유분 포함해서 검토 후 필요 개수만큼 잘라냄)
            ai_filtered = self._ai_filter_satellites(pre_filter)
            new_info = ai_filtered[:n_needed]
            if len(new_info) < n_needed:
                self.add_log(f"⚠️ 당일 블랙리스트/AI 퇴출로 인해 {n_needed - len(new_info)}개 위성 슬롯 공석 유지")

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
                        self.satellite_strategies[c['ticker']] = c['strategy_name']
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
            if self.gemini:
                analysis = self.gemini.chat(prompt, stock_analysis_context="마크다운 없이 평문 2줄로.")
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
            
            report_data = generate_daily_market_report(gemini_client=self.gemini, verbose=False, news_context=combined_context, kis=self.kis)
            if report_data:
                today_str = _now_kst().strftime('%Y-%m-%d')
                if not isinstance(self.daily_report, dict) or self.daily_report.get('date') != today_str: self.daily_report = {'date': today_str, '15:40': None}
                content = report_data.get('report_markdown') if isinstance(report_data, dict) else str(report_data)
                self.daily_report[time_slot] = content
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
        from database import get_db_connection
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
        if self.gemini:
            new_rules = self.gemini.generate_weekly_reflection(history_text, existing_rules)
            if new_rules:
                save_ai_rules(self.user_id, new_rules, trigger_type='weekly')
                self._send_telegram(f"🧠 [주간 학습 완료]\n\n{new_rules[:2000]}")

    def _incremental_reflection(self):
        """누적 10건 반성 — 주간 반성과 동일 로직, 트리거만 다름."""
        from database import get_db_connection
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
        if self.gemini:
            new_rules = self.gemini.generate_weekly_reflection(history_text, existing_rules)
            if new_rules:
                save_ai_rules(self.user_id, new_rules, trigger_type='incremental')
                self._send_telegram(f"📚 [누적 10건 학습 완료]\n\n{new_rules[:2000]}")

    def _emergency_reflection(self, ticker: str, stock_name: str,
                               profit: float, ai_reason: str):
        """큰 손실 직후 긴급 반성 — 관련 규칙 항목만 수정/강화, 나머지 보존."""
        existing_rules = load_ai_rules(self.user_id)
        if not self.gemini:
            return
        new_rules = self.gemini.generate_emergency_reflection(
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
        # 위성 재스크리닝은 08:50 사전 스크리닝(아래 _kst_morning_prescreen)으로 커버.
        # 1시간마다 자동 재스크리닝은 제거 — 10:30 이후 새 위성 선정은 의미 없음.
        self.scheduler.every(30).minutes.do(clear_expired_cache)

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

        self.scheduler.every(1).minutes.do(_kst_midnight_rescreen)
        self.scheduler.every(1).minutes.do(_kst_friday_reflection)
        self.scheduler.every(1).minutes.do(_kst_morning_websocket)
        self.scheduler.every(1).minutes.do(_kst_friday_lstm)
        self.scheduler.every(1).minutes.do(_kst_morning_prescreen)

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
            self.add_log(f"▶️ [{self.mode_name}투자] 매매 봇이 시작되었습니다.")
            return True
        return False

    def stop(self):
        if self.is_running:
            self.is_running = False
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
                satellites.append({"name": pos.name, "ticker": ticker, "strategy": self.satellite_strategies.get(ticker, '-'), "shares": pos.shares, "price": sp, "value": sat_val, "avg_price": float(getattr(pos, 'avg_price', 0) or 0), "budget": float(getattr(pos, 'cash', 0) or 0), "status": getattr(pos, 'status', '감시 중 👀'), "status_msg": getattr(pos, 'status_msg', '지표 점검 중...')})

            try:
                current_initial_cash = get_user_initial_cash(self.user_id, self._is_mock)
            except Exception: current_initial_cash = 10000000.0

            # 모멘텀 슬롯 3개 상태
            momentum_list = []
            for mp in self.momentum_positions:
                if mp:
                    try:
                        mp_ticker = mp.get('ticker', '')
                        # live_prices 우선, 없으면 저장된 avg_price 사용 (KIS API 호출 제거 — get_status는 빠르게)
                        mp_price = (self.live_prices.get(mp_ticker)
                                    or float(mp.get('avg_price', 0)))
                        mp_val  = float(mp.get('shares', 0)) * float(mp_price or 0)
                        total_realtime_stock_val += mp_val
                        tracked_tickers.add(mp_ticker)
                        avg_p   = float(mp.get('avg_price', 0))
                        pnl_pct = ((mp_price / avg_p) - 1) * 100 if avg_p > 0 and mp_price else 0
                        elapsed = ""
                        et = mp.get('enter_time')
                        if et:
                            try:
                                elapsed = f"{(_now_kst() - et).total_seconds() / 60:.0f}분 보유"
                            except Exception:
                                pass
                        momentum_list.append({
                            "ticker":    mp_ticker,
                            "name":      mp.get('name', mp_ticker),
                            "shares":    mp.get('shares', 0),
                            "price":     mp_price,
                            "value":     mp_val,
                            "avg_price": avg_p,
                            "pnl_pct":   round(pnl_pct, 2),
                            "reason":    mp.get('reason', ''),
                            "elapsed":   elapsed,
                            "status":    "🚀 보유 중",
                        })
                    except Exception as slot_err:
                        logger.warning(f"[{self.mode_name}] 모멘텀 슬롯 status 오류: {slot_err}")
                        # 오류가 나도 슬롯은 보유 중으로 표시 (avg_price 폴백)
                        momentum_list.append({
                            "ticker": mp.get('ticker', '?'), "name": mp.get('name', '?'),
                            "shares": mp.get('shares', 0), "price": mp.get('avg_price', 0),
                            "value": 0, "avg_price": mp.get('avg_price', 0),
                            "pnl_pct": 0, "reason": mp.get('reason', ''),
                            "elapsed": "조회 중", "status": "🚀 보유 중",
                        })
                else:
                    momentum_list.append(None)

            # [BUG-FIX] 봇이 추적하지 않는 종목(위성 교체로 빠진 보유주, 수동 매수 등)도 평가금액에 포함.
            # cached_balance에 실계좌 전체 잔고가 있으므로, 추적 중인 종목을 제외한 나머지를 합산.
            if self.cached_balance:
                for _s in self.cached_balance.get('stocks', []):
                    _t = _s.get('ticker', '')
                    _sh = int(_s.get('shares', 0))
                    if _t and _t not in tracked_tickers and _sh > 0:
                        _p = self.live_prices.get(_t) or float(_s.get('current_price', 0))
                        total_realtime_stock_val += _sh * _p

            # mock_total_asset: 코어+위성+모멘텀+미추적 종목 전체 반영 후 계산
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
                d_shares = bal_stocks.get(d_ticker, 0)
                defensive_list.append({
                    "ticker": d_ticker,
                    "name":   asset['name'],
                    "emoji":  asset['emoji'],
                    "ratio":  asset['ratio'],
                    "price":  d_price,
                    "shares": d_shares,
                    "value":  d_shares * d_price,
                    "active": is_bear,
                })

            # BUG-FIX: deque는 슬라이싱 불가 → list()로 변환 후 슬라이스 (TypeError 방지)
            recent_logs = list(self.logs)[-30:]
            return {"is_running": self.is_running, "is_mock": self._is_mock, "has_keys": self.kis is not None, "logs": recent_logs, "hot_sectors": self.hot_sectors, "num_satellites": self.num_satellites, "cores": cores_data, "satellites": satellites, "momentum_list": momentum_list, "defensive_list": defensive_list, "market_regime": self.market_regime, "mock_total_asset": mock_total_asset, "mock_pnl": mock_pnl, "mock_pnl_rt": mock_pnl_rt, "initial_cash": current_initial_cash, "available_cash": available_cash}
        except Exception as critical_e:
            return {"is_running": False, "is_mock": self._is_mock, "has_keys": False, "logs": [{"time": "Error", "message": f"오류: {str(critical_e)}"}], "hot_sectors": [], "num_satellites": self.num_satellites, "cores": [], "satellites": [], "momentum_list": [None] * len(self.momentum_positions), "mock_total_asset": 0, "mock_pnl": 0, "mock_pnl_rt": 0, "initial_cash": 10000000}