import threading
import time
import schedule
import json
import logging
import os
import tempfile
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
from strategy import CorePosition, Position, get_rsi_signal, get_signal_by_strategy, REINVEST_RATIO, get_market_regime, get_bear_bounce_signal, get_bear_bottom_score, get_bull_momentum_score, get_neutral_range_score, INVERSE_ETF_TICKER, INVERSE_ETF_NAME, INVERSE_BUDGET_RATIO, check_giveback_stop, check_early_drop_stop
from stock_screener import select_satellites, generate_daily_market_report
from hot_momentum_scanner import scan_hot_momentum, clear_expired_cache
from database import update_bot_status, save_portfolio_state, load_portfolio_state, log_trade_journal, get_recent_trades, save_ai_rules, load_ai_rules, get_user_initial_cash, set_user_initial_cash, add_user_initial_cash

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


class BaseBot:
    """실전/모의투자의 공통 매매 및 AI 판단 로직을 품은 부모 클래스"""
    def __init__(self, user_id, kis_config=None, telegram_config=None, core_stocks=None, is_mock=False):
        self.user_id = user_id
        self.is_running = False
        self.thread = None
        self.logs = []
        self.num_satellites = 3  # 위성 3개 고정
        self._is_mock = is_mock
        
        self.mode_name = "모의" if is_mock else "실전"
        self.alert_icon = "🟢" if is_mock else "🔴"

        self.core_ticker = "003850"
        self.core_name = "보령"
        self.core_ratio = 0.30
        self.satellite_ratio = 0.70
        self.core_min_floor_ratio = 0.5
        self.market_indices = [("069500", "KOSPI"), ("229200", "KOSDAQ")]

        try:
            self.user_core_stocks = json.loads(core_stocks) if core_stocks else []
        except Exception:
            self.user_core_stocks = []

        self.core_positions = []
        self.satellite_positions = {}
        self.satellite_info = []
        self.satellite_strategies = {}
        self.daily_pnl = {}
        self.last_screen_month = None
        self.last_screen_date = None
        self.hot_sectors = []
        self.daily_report = None

        # 예수금 즉시 반영용 내부 현금 추적기
        # KIS 모의 API는 체결 후 1~3분 지연이 있어 캐시 API 값 대신 내부 추적값 사용
        self.internal_cash = None          # 최초 KIS API 값으로 초기화 후 매수/매도마다 즉각 갱신
        self._last_trade_ts = 0.0          # 마지막 체결 타임스탬프 (KIS API 재동기화 시점 판단)
        self.fundamental_cache = {}

        # ── 당일 블랙리스트 (날짜가 바뀌면 자동 초기화) ──────────────────
        # momentum_exits  : 모멘텀 슬롯에서 오늘 청산된 종목 (재진입 금지)
        # satellite_rejects: 오늘 AI 거절된 위성 종목 {ticker: reason}
        self._bl_date           = ""          # 마지막 초기화 날짜 (YYYY-MM-DD)
        self._momentum_exits    : set  = set()
        self._satellite_rejects : dict = {}

        # 시장 국면 (BULL / BEAR / NEUTRAL)
        self.market_regime = "NEUTRAL"
        self.last_regime_check = 0.0
        self._regime_check_interval = 3600  # 1시간마다 재판단
        self._last_inverse_check = 0.0      # 인버스 ETF 체크 캐시 (5분)

        # ── 🚀 테마·급등주 모멘텀 전용 슬롯 ──────────────────────────
        # 위성 5개와 완전히 별개의 단일 포지션.
        # 초고속 진입·이탈이 핵심이므로 AI 심사 없이 즉시 주문.
        self.momentum_positions = [None, None, None]  # 모멘텀 슬롯 3개 독립 관리
        self.momentum_budget_ratio = 0.05    # 총자산의 5% 를 모멘텀 슬롯에 배정
        self._last_momentum_scan = 0.0       # 마지막 스캔 타임스탬프
        self._momentum_scan_interval = 60    # 1분마다 스캔

        self.kis = None
        self.telegram = None
        self.gemini = None

        self._init_api(kis_config)
        
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
        raise NotImplementedError("자식 클래스에서 KIS API 객체를 초기화해야 합니다.")

    def _create_websocket(self, app_key, callback):
        raise NotImplementedError("자식 클래스에서 웹소켓 객체를 반환해야 합니다.")

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

                        for t2 in current_tickers:
                            if t2 not in self.ws_client.subscribed_tickers:
                                self.ws_client.subscribe(t2)
                        for t2 in list(self.ws_client.subscribed_tickers):
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
                    cash_col = "mock_initial_cash" if self._is_mock else "real_initial_cash"
                    # EC2 읽기 전용 디렉토리 대응: /tmp 아래에 플래그 파일 생성
                    _flag_dir = os.path.join(tempfile.gettempdir(), 'lassi_bot')
                    os.makedirs(_flag_dir, exist_ok=True)
                    flag_file = os.path.join(_flag_dir, f"{cash_col}_{self.user_id}_locked.flag")

                    if not os.path.exists(flag_file) and pure_principal > 0:
                        set_user_initial_cash(self.user_id, pure_principal, self._is_mock)
                        with open(flag_file, 'w') as f: f.write("Locked")
                        self.add_log(f"💰 [{self.mode_name} 원금 셋업] 실시간 계좌 진짜 원금 {pure_principal:,.0f}원으로 영구 고정 완료.")
                    else:
                        db_cash = get_user_initial_cash(self.user_id, self._is_mock)
                        if db_cash == 10000000.0 and pure_principal > 0:
                            set_user_initial_cash(self.user_id, pure_principal, self._is_mock)
                            self.add_log(f"💰 [{self.mode_name} 최초 원금 계산] 투자 원금 {pure_principal:,.0f}원 셋업 완료.")
                            with open(flag_file, 'w') as f: f.write("Locked")
                    self.initial_capital_captured = True
                
                current_asset_cost = real_cash + real_purchase 
                if self.last_asset_cost is not None:
                    # W-10: pnl_this_turn != 0이어도 항상 처리해야 누적 방지
                    # (이전의 `pass` 분기는 pnl_this_turn을 0으로 리셋하지 않아 누산 버그 유발)
                    expected_asset_cost = self.last_asset_cost + self.pnl_this_turn
                    self.pnl_this_turn = 0.0
                    deposit_delta = current_asset_cost - expected_asset_cost
                    if deposit_delta > 10000 or deposit_delta < -10000:
                        add_user_initial_cash(self.user_id, deposit_delta, self._is_mock)
                        if deposit_delta > 0: self.add_log(f"💰 {self.mode_name} 계좌 외부 입금 포착: +{deposit_delta:,.0f}원")
                        else: self.add_log(f"💸 {self.mode_name} 계좌 외부 출금 포착: {deposit_delta:,.0f}원")
                    self.last_asset_cost = current_asset_cost
                else:
                    self.last_asset_cost = current_asset_cost
                
                if total_equity >= 0:
                    target_core_pool = total_equity * self.core_ratio
                    target_sat_pool = total_equity * self.satellite_ratio
                    
                    current_core_stock_val = sum([float(s['value']) for s in real_balance['stocks'] if any(c.ticker == s['ticker'] for c in self.core_positions)])
                    per_core_cash = max(0.0, (target_core_pool - current_core_stock_val) / max(1, len(self.core_positions)))
                    for core in self.core_positions: core.cash = round(per_core_cash, 2)
                        
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
                    c_p = float(real_stock.get('current_price', p)) 
                    stock_name = real_stock.get('name', t)

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
            except Exception as e:
                logger.error(f"[{self.mode_name}] 장부 동기화 중 오류: {e}", exc_info=True)

    def _init_dummy_cores(self):
        self.core_positions = []
        if self.user_core_stocks:
            for c in self.user_core_stocks:
                self.core_positions.append(CorePosition(c['ticker'], c['name'], initial_cash=0))
        else:
            self.core_positions.append(CorePosition(self.core_ticker, self.core_name, initial_cash=0))
            self.core_positions.append(CorePosition("047040", "대우건설", initial_cash=0))
            
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
        self.logs.append({"time": t, "message": msg})
        print(f"[{t}] {msg}")
        if len(self.logs) > 100: self.logs.pop(0)

    def _send_telegram(self, message):
        if not self.telegram: return
        # 메시지에 이미 모드 정보가 포함되어 있으므로 그대로 전달
        threading.Thread(target=self.telegram.send_message, args=(message,), daemon=True).start()

    def _buy_order(self, ticker: str, qty: int, pos, name: str) -> bool:
        """매수 주문 실행 + KIS 응답 체크. 성공 True, 실패 False (봇 로그에 에러 기록)."""
        if not self.kis:
            return False
        result = self.kis.buy_market_order(ticker, qty)
        if result:
            # 내부 현금 즉시 차감 — KIS 모의 API 반영 지연 보정
            est_price = self.live_prices.get(ticker, 0) or getattr(pos, 'avg_price', 0) or 0
            if est_price > 0:
                with self.lock:
                    if self.internal_cash is not None:
                        self.internal_cash = max(0.0, self.internal_cash - est_price * qty * 1.00015)
                    self._last_trade_ts = time.time()
            return True
        err = f"⚠️ [{self.mode_name}] {name}({ticker}) {qty}주 매수 주문 실패 — KIS API 오류"
        self.add_log(err)
        logger.warning(err)
        with self.lock:
            pos.status = "주문 실패 ❌"
            pos.status_msg = "KIS API 오류 — 서버 로그 확인 필요"
        return False

    def _sell_order(self, ticker: str, qty: int, pos, name: str, price: int = 0) -> bool:
        """매도 주문 실행 + KIS 응답 체크. 성공 True, 실패 False (봇 로그에 에러 기록)."""
        if not self.kis:
            return False
        result = self.kis.sell_market_order(ticker, qty, price=price)
        if result:
            # 내부 현금 즉시 증가 — KIS 모의 API 반영 지연 보정
            est_price = price or self.live_prices.get(ticker, 0) or getattr(pos, 'avg_price', 0) or 0
            if est_price > 0:
                with self.lock:
                    if self.internal_cash is not None:
                        self.internal_cash += est_price * qty * (1 - 0.00015)
                    self._last_trade_ts = time.time()
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

    def _refresh_blacklist(self):
        """날짜가 바뀌면 당일 블랙리스트를 초기화합니다."""
        today = _now_kst().strftime('%Y-%m-%d')
        if self._bl_date != today:
            self._bl_date           = today
            self._momentum_exits    = set()
            self._satellite_rejects = {}

    def _add_momentum_exit(self, ticker: str):
        """모멘텀 청산 종목을 당일 재진입 금지 목록에 추가합니다."""
        self._refresh_blacklist()
        self._momentum_exits.add(ticker)

    def _add_satellite_reject(self, ticker: str, reason: str):
        """AI 거절 위성 종목을 당일 재편입 금지 목록에 추가합니다."""
        self._refresh_blacklist()
        self._satellite_rejects[ticker] = reason

    def _is_momentum_blacklisted(self, ticker: str) -> bool:
        self._refresh_blacklist()
        return ticker in self._momentum_exits

    def _is_satellite_blacklisted(self, ticker: str) -> bool:
        self._refresh_blacklist()
        return ticker in self._satellite_rejects

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
            reviewed = self.gemini.review_satellite_candidates(candidates, self.hot_sectors)
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
        raw_info, self.hot_sectors = select_satellites(kis=self.kis, n=self.num_satellites * 2, verbose=False, gemini_client=self.gemini)
        # AI 검토: 부적합 종목 제거 후 num_satellites 개수만 사용
        filtered_info = self._ai_filter_satellites(raw_info)
        self.satellite_info = filtered_info[:self.num_satellites]
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
        per_sat     = sat_budget / self.num_satellites if self.num_satellites > 0 else 0

        self.core_positions = []
        if self.user_core_stocks:
            per_core_budget = core_budget / len(self.user_core_stocks)
            for c in self.user_core_stocks: self.core_positions.append(CorePosition(c['ticker'], c['name'], initial_cash=per_core_budget))
        else:
            half_core_budget = core_budget / 2
            self.core_positions.append(CorePosition(self.core_ticker, self.core_name, initial_cash=half_core_budget))
            ai_core_info = select_ai_core_stock(verbose=False)
            if ai_core_info: self.core_positions.append(CorePosition(ai_core_info['ticker'], ai_core_info['name'], initial_cash=half_core_budget))

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
                "cores": [{"ticker": c.ticker, "name": c.name, "shares": int(c.shares), "floor_shares": int(c.floor_shares), "cash": float(c.cash), "initial_cash": float(c.initial_cash), "avg_price": float(c.avg_price)} for c in self.core_positions],
                "satellites": {ticker: {"name": pos.name, "shares": int(pos.shares), "cash": float(pos.cash), "initial_cash": float(pos.initial_cash), "avg_price": float(pos.avg_price)} for ticker, pos in self.satellite_positions.items()},
                "satellite_info": self.satellite_info, "satellite_strategies": self.satellite_strategies, "hot_sectors": self.hot_sectors, "num_satellites": self.num_satellites,
                "last_screen_month": getattr(self, 'last_screen_month', None), "last_screen_date": self.last_screen_date.strftime('%Y-%m-%d') if getattr(self, 'last_screen_date', None) else None,
                "daily_pnl": self.daily_pnl, "daily_report": self.daily_report,
                "momentum_positions": [self._serialize_one_momentum(mp) for mp in self.momentum_positions],
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
                self.core_positions.append(pos)
            self.satellite_positions = {}
            for ticker, s in state["satellites"].items():
                pos = Position(ticker, s["name"], s.get("initial_cash", 1400000))
                pos.shares = s["shares"]; pos.cash = s["cash"]; pos.avg_price = s.get("avg_price", 0)
                self.satellite_positions[ticker] = pos

            self.satellite_info = state.get("satellite_info", [])
            self.satellite_strategies = state.get("satellite_strategies", {})
            self.hot_sectors = state.get("hot_sectors", [])
            self.num_satellites = 3  # 위성 3개 고정 (저장값 무시)
            self.last_screen_month = state.get("last_screen_month")
            lsd_str = state.get("last_screen_date")
            self.last_screen_date = datetime.strptime(lsd_str, '%Y-%m-%d').date() if lsd_str else None
            self.daily_pnl = state.get("daily_pnl", {})
            self.daily_report = state.get("daily_report", None)
            # 모멘텀 슬롯 복원 (구버전 단일 포지션 호환)
            saved_slots = state.get("momentum_positions")
            if saved_slots is not None:
                self.momentum_positions = [self._deserialize_one_momentum(mp) for mp in saved_slots]
                while len(self.momentum_positions) < 3:
                    self.momentum_positions.append(None)
            else:
                old_single = state.get("momentum_position")
                self.momentum_positions = [self._deserialize_one_momentum(old_single), None, None]
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
            prev = self.market_regime
            self.market_regime = get_market_regime(self.kis)
            self.last_regime_check = time.time()
            if self.market_regime != prev:
                icons = {"BULL": "🐂", "BEAR": "🐻", "NEUTRAL": "😐"}
                regime_desc = {'BEAR': '📉 위성 신규 매수 중단, 인버스 ETF 진입', 'BULL': '📈 정상 매매 재개', 'NEUTRAL': '📊 혼조 — 기존 전략 유지'}
                msg = (f"{icons.get(self.market_regime,'📊')} [{self.mode_name}] "
                       f"시장 국면 변경: {prev} → {self.market_regime}  {regime_desc.get(self.market_regime,'')}")
                self.add_log(msg)
                icons = {"BULL": "🐂", "BEAR": "🐻", "NEUTRAL": "😐"}
                regime_desc = {'BEAR': '위성 신규 매수 중단\n인버스 ETF 자동 진입', 'BULL': '정상 매매 모드 재개', 'NEUTRAL': '혼조장 — 기존 전략 유지'}
                self._send_telegram(
                    f"{icons.get(self.market_regime,'📊')} <b>시장 국면 변경</b>  ·  {self.alert_icon} {self.mode_name}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📊 <b>{prev}</b>  →  <b>{self.market_regime}</b>\n"
                    f"📋 {regime_desc.get(self.market_regime,'')}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"⏰ {_now_kst().strftime('%H:%M KST')}"
                )
        except Exception as e:
            logger.error(f"[{self.mode_name}] 시장 국면 판단 오류: {e}", exc_info=True)
        return self.market_regime

    def _handle_inverse_etf(self, regime: str):
        """
        BEAR 국면: KODEX 인버스(114800) 총자산의 20% 자동 매수.
        BULL/NEUTRAL 국면: 보유 중이면 전량 청산.
        모의투자도 동일하게 동작.
        5분마다 한 번만 실행 (매분 API 호출 방지).
        """
        if not self.kis:
            return
        if time.time() - self._last_inverse_check < 300:  # 5분 캐시
            return
        self._last_inverse_check = time.time()
        try:
            balance = self.kis.get_account_balance()
            if not balance:
                return

            total_cash  = float(balance.get('total_cash', 0))
            total_value = float(balance.get('total_value', 0))
            total_assets = total_cash + total_value

            stocks = balance.get('stocks', [])
            inv_holding = next((s for s in stocks if s.get('ticker') == INVERSE_ETF_TICKER), None)
            has_inverse  = inv_holding and int(inv_holding.get('shares', 0)) > 0
            inv_shares   = int(inv_holding.get('shares', 0)) if inv_holding else 0

            if regime == "BEAR" and not has_inverse:
                budget = int(total_assets * INVERSE_BUDGET_RATIO)
                price  = self.kis.get_current_price(INVERSE_ETF_TICKER)
                if price and price > 0:
                    qty = int(budget // price)
                    if qty > 0 and total_cash >= qty * price * 1.002:
                        self.kis.buy_market_order(INVERSE_ETF_TICKER, qty)
                        self.add_log(f"🐻 하락장 인버스 매수 | {INVERSE_ETF_NAME} {qty}주 @ {price:,.0f}원")
                        self._send_telegram(
                            f"🐻 <b>인버스 ETF 매수</b>  ·  {self.alert_icon} {self.mode_name}\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"📌 <b>{INVERSE_ETF_NAME}</b>  <code>{INVERSE_ETF_TICKER}</code>\n"
                            f"💰 <b>{price:,.0f}원</b> × <b>{qty}주</b>  =  <b>{qty*price:,.0f}원</b>\n"
                            f"📋 BEAR 국면  ·  총자산 {INVERSE_BUDGET_RATIO*100:.0f}% 헤지\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"⏰ {_now_kst().strftime('%H:%M KST')}"
                        )

            elif regime != "BEAR" and has_inverse and inv_shares > 0:
                self.kis.sell_market_order(INVERSE_ETF_TICKER, inv_shares)
                price = self.kis.get_current_price(INVERSE_ETF_TICKER) or 0
                self.add_log(f"🐂 국면 전환({regime}) → {INVERSE_ETF_NAME} {inv_shares}주 전량 청산")
                self._send_telegram(
                    f"🐂 <b>인버스 ETF 청산</b>  ·  {self.alert_icon} {self.mode_name}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📌 <b>{INVERSE_ETF_NAME}</b>  <code>{INVERSE_ETF_TICKER}</code>\n"
                    f"💰 <b>{inv_shares}주</b> 전량 청산\n"
                    f"📋 국면 전환: BEAR → <b>{regime}</b>  ·  헤지 해제\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"⏰ {_now_kst().strftime('%H:%M KST')}"
                )

        except Exception as e:
            logger.error(f"[{self.mode_name}] 인버스 ETF 처리 오류: {e}", exc_info=True)

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

        # ── 1. 뉴스 ──────────────────────────────────────────────
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
                    vol_str = f"평소 대비 {vol_ratio:.0f}% ({'급증↑↑' if vol_ratio > 200 else '증가↑' if vol_ratio > 130 else '보통' if vol_ratio > 70 else '감소↓'})"
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

        # ── 5. 시장 국면 & 전략 ──────────────────────────────────
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
        if not self.core_positions: return
        now = _now_kst()  # EC2(UTC) 환경에서도 KST 기준으로 장 시간 판단
        if now.weekday() >= 5: return
        current_time_str = now.strftime('%H:%M')
        is_golden_hours = ("09:01" <= current_time_str <= "19:50")
        
        if not is_golden_hours:
            with self.lock:
                for core in self.core_positions: core.status = "휴식 중 💤"; core.status_msg = "정규 장 및 대체거래소 마감"
                for sat in self.satellite_positions.values(): sat.status = "휴식 중 💤"; sat.status_msg = "정규 장 및 대체거래소 마감"
        else:
            self.add_log(f"--- 🎯 {self.mode_name} 실시간 점검 ({current_time_str}) ---")
            with self.lock:
                for core in self.core_positions:
                    if "대기" not in core.status and "심사" not in core.status: # 대기/심사 중이 아닐 때만 텍스트 초기화
                        core.status = "감시 중 👀"
                        core.status_msg = "최적 타이밍 스캔 중"

                for sat in self.satellite_positions.values():
                    if "대기" not in sat.status and "심사" not in sat.status:
                        sat.status = "감시 중 👀"
                        sat.status_msg = "최적 타이밍 스캔 중"

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

        # 시장 국면 갱신 (1시간 캐시) + 인버스 ETF 자동 관리
        regime = self._update_market_regime()
        if is_golden_hours:
            self._handle_inverse_etf(regime)

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
                        with self.lock: safe_core_positions = list(self.core_positions); safe_satellite_items = list(self.satellite_positions.items())
                        for core in safe_core_positions:
                            if core.shares > 0: self.kis.sell_market_order(core.ticker, core.shares); self.add_log(f"🔥 {self.mode_name} 코어 {core.name} 청산")
                        for ticker, pos in safe_satellite_items:
                            if pos.shares > 0: self.kis.sell_market_order(ticker, pos.shares); self.add_log(f"🔥 {self.mode_name} 위성 {pos.name} 청산")
                        self.is_crisis_mode = True; return
            except Exception as e:
                logger.error(f"[{self.mode_name}] 서킷브레이커 잔고 조회 오류: {e}", exc_info=True)

        with self.lock: safe_core_positions = list(self.core_positions)
        for core in safe_core_positions:
            cp = self.live_prices.get(core.ticker) or getattr(core, 'kis_current_price', 0) or (self.kis.get_current_price(core.ticker) if self.kis else 0)
            if not cp or cp <= 0: continue
            with self.lock: core._last_price = cp; c_sh = core.shares; c_fl = core.floor_shares; c_cash = core.cash; c_nm = core.name; c_tk = core.ticker
            try:
                from strategy import get_rsi_signal
                ex_df = self._get_extended_ohlcv(c_tk, cp)
                c_sig, _, c_rsi = get_rsi_signal(c_tk, kis_api=self.kis, df=ex_df)

                if c_sig == 'BUY' and c_cash >= cp and (time.time() - getattr(core, 'last_order_time', 0) > 300):
                    qty = int((c_cash * 0.98) // cp)
                    if qty > 0 and self._buy_order(c_tk, qty, core, c_nm):
                        # W-02: 체결 확인 전 임시로 shares 갱신 → 다음 턴에 중복 매수 방지
                        with self.lock:
                            core.last_order_time = time.time()
                            core.status = "체결 대기 ⏳"
                            core.shares += qty
                        self.add_log(f"💎 {c_nm} 매수 | {qty}주 @ {cp:,}원")
                        self._send_telegram(self._fmt_trade_msg("💎", "코어 매수", c_tk, c_nm, cp, qty, strategy="RSI 코어 장기보유"))
                elif c_sig == 'SELL' and c_sh > c_fl and (time.time() - getattr(core, 'last_order_time', 0) > 300):
                    sellable = c_sh - c_fl
                    # W-03: avg_price가 0이면 수익 계산이 무의미하므로 매도 건너뜀
                    if sellable > 0 and core.avg_price > 0 and self._sell_order(c_tk, sellable, core, c_nm):
                        core_profit = _net_profit(cp, core.avg_price, sellable)
                        with self.lock: core.last_order_time = time.time(); core.status = "체결 대기 ⏳"; self.pnl_this_turn += core_profit
                        self._record_daily_pnl(core_profit)
                        self.add_log(f"💎 {c_nm} 매도 | {sellable}주 @ {cp:,}원 | 손익: {core_profit:+,.0f}원")
                        self._send_telegram(self._fmt_trade_msg("💎", "코어 매도", c_tk, c_nm, cp, sellable, profit=core_profit, strategy="RSI 코어 장기보유"))
            except Exception as e:
                logger.error(f"[{self.mode_name}] 코어 매매 오류 ({c_tk}): {e}", exc_info=True)
            time.sleep(0.2)

        with self.lock: trading_sat_items = list(self.satellite_positions.items())

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

                if p_sh > 0 and price > 0 and is_cd_passed:
                    if price > p_max:
                        with self.lock: pos.max_price = price; p_max = price
                    if p_max >= p_avg + (trail_trigger * atr_14) and price <= p_max - (trail_mult * atr_14):
                        if self._sell_order(ticker, p_sh, pos, p_nm):
                            with self.lock: pos.last_order_time = time.time(); pos.max_price = 0; pos.status = "체결 대기 ⏳"
                            profit = _net_profit(price, p_avg, p_sh)
                            log_trade_journal(self.user_id, ticker, p_nm, 'SELL', price, st_nm, "ATR 트레일링 익절", profit=profit)
                            self._send_telegram(self._fmt_trade_msg("🎯", "트레일링 익절", ticker, p_nm, price, p_sh, profit=profit, strategy=st_nm, note="ATR 트레일링 스탑 발동"))
                            with self.lock: self.pnl_this_turn += profit
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
                            profit = _net_profit(price, p_avg, stop_qty)
                            log_trade_journal(self.user_id, ticker, p_nm, 'SELL', price, st_nm, f"장초 급락 손절 {_es_pct*100:.0f}% [{_es_reason}]", profit=profit)
                            self._send_telegram(self._fmt_trade_msg("🚨", "장초 급락 손절", ticker, p_nm, price, stop_qty, profit=profit, strategy=st_nm, note=_es_reason))
                            with self.lock: self.pnl_this_turn += profit
                            self._record_daily_pnl(profit)
                        continue

                if p_sh > 0 and p_avg > 0 and is_cd_passed:
                    if price <= p_avg - (hard_mult * atr_14):
                        if self._sell_order(ticker, p_sh, pos, p_nm):
                            with self.lock:
                                pos.last_order_time = time.time(); pos.status = "체결 대기 ⏳"
                                pos.second_buy_done = True; pos.pyramid_done = True; pos.partial_sold = False
                                pos.second_buy_price = 0; pos.second_buy_cash = 0
                            profit = _net_profit(price, p_avg, p_sh)
                            log_trade_journal(self.user_id, ticker, p_nm, 'SELL', price, st_nm, "ATR 하드 손절", profit=profit)
                            self._send_telegram(self._fmt_trade_msg("💥", "손절 체결", ticker, p_nm, price, p_sh, profit=profit, strategy=st_nm, note="ATR 하드 손절선 이탈"))
                            with self.lock: self.pnl_this_turn += profit
                            self._record_daily_pnl(profit)
                        continue

                # ── 분할 익절: +5% 도달 시 보유량 50% 선익절, 나머지는 ATR 트레일링 ──
                if (p_sh > 0 and p_avg > 0 and is_cd_passed
                        and not getattr(pos, 'partial_sold', False)
                        and price >= p_avg * 1.05):
                    sell_qty = max(1, p_sh // 2)
                    if self._sell_order(ticker, sell_qty, pos, p_nm):
                        with self.lock:
                            pos.last_order_time = time.time(); pos.partial_sold = True; pos.status = "1차익절 ✅"
                        profit = _net_profit(price, p_avg, sell_qty)
                        log_trade_journal(self.user_id, ticker, p_nm, 'SELL', price, st_nm, f"1차 분할 익절 +5% ({sell_qty}주)", profit=profit)
                        self._send_telegram(self._fmt_trade_msg("🎯", "1차 분할 익절", ticker, p_nm, price, sell_qty, profit=profit, strategy=st_nm, note=f"나머지 {p_sh - sell_qty}주는 ATR 트레일링 계속 보유"))
                        with self.lock: self.pnl_this_turn += profit
                        self._record_daily_pnl(profit)

                # ── 피라미딩: +3% 수익 중 & 상승 추세 지속 → 추가 20% 매수 ──
                if (p_sh > 0 and p_avg > 0 and is_cd_passed
                        and not getattr(pos, 'pyramid_done', False)
                        and price >= p_avg * 1.03
                        and p_cash > price
                        and sig != 'SELL'
                        and regime != "BEAR"):
                    pyramid_cash = p_cash * 0.20
                    pyramid_qty = int((pyramid_cash * 0.98) // price)
                    if pyramid_qty > 0 and self._buy_order(ticker, pyramid_qty, pos, p_nm):
                        with self.lock:
                            pos.last_order_time = time.time(); pos.pyramid_done = True; pos.status = "피라미딩 📈"
                        log_trade_journal(self.user_id, ticker, p_nm, 'BUY', price, st_nm, f"피라미딩 +3% 추세 지속 ({pyramid_qty}주)")
                        self._send_telegram(self._fmt_trade_msg("📈", "피라미딩 추가 매수", ticker, p_nm, price, pyramid_qty, strategy=st_nm, note="+3% 돌파 · 상승 추세 지속 확인"))

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
                        log_trade_journal(self.user_id, ticker, p_nm, 'BUY', price, st_nm, f"2차 분할 매수 눌림목 ({sq}주)")
                        self._send_telegram(self._fmt_trade_msg("🛒", "2차 분할 매수", ticker, p_nm, price, sq, strategy=st_nm, note="-2% 눌림목 포착"))

                # 당일 AI 거절 블랙리스트 종목은 매수 시도 자체를 차단
                if sig == 'BUY' and p_sh == 0 and self._is_satellite_blacklisted(ticker):
                    pos.status = "당일 블랙리스트 🚫"
                    pos.status_msg = f"오늘 거절됨: {self._satellite_rejects.get(ticker, '')[:30]}"
                    continue

                if sig == 'BUY' and p_sh == 0 and is_cd_passed and is_golden_hours:
                    # ── BEAR 국면: 10개 저점 전략 스코어 기반 차등 진입 + AI 최종 심사 ──
                    if regime == "BEAR":
                        bear_score, bear_reasons = get_bear_bottom_score(ex_df)
                        if bear_score == 0:
                            pos.status = "하락장 매수 보류 🐻"
                            pos.status_msg = "BEAR 국면 — 저점 신호 없음, 매수 차단"
                            continue
                        # 신호 강도에 따른 차등 포지션 사이징
                        if bear_score >= 3:
                            bear_ratio, bear_label = 0.40, f"저점 강신호({bear_score}개)"
                        elif bear_score == 2:
                            bear_ratio, bear_label = 0.30, f"저점 중신호({bear_score}개)"
                        else:
                            bear_ratio, bear_label = 0.20, f"저점 약신호({bear_score}개)"
                        bear_reason_str = " | ".join(bear_reasons)
                        bounce_cash = p_cash * bear_ratio
                        qty = int((bounce_cash * 0.98) // price)
                        if qty > 0:
                            # 하락장은 더 신중해야 하므로 AI 심사 필수
                            if self.gemini:
                                pos.status = "AI 심사 중 🤖"
                                trade_ctx = self._build_trade_context(ticker, p_nm, price, ex_df, st_nm, regime)
                                decision, ai_reason = self.gemini.ai_approve_trade(sig, p_nm, ticker, price, st_nm, ind_val, self.hot_sectors, get_recent_trades(self.user_id, ticker), load_ai_rules(self.user_id) + "\n" + getattr(self, 'current_ai_market_view', ''), context=trade_ctx)
                                if decision:
                                    if self._buy_order(ticker, qty, pos, p_nm):
                                        with self.lock: pos.last_order_time = time.time(); pos.status = "체결 대기 ⏳"
                                        log_trade_journal(self.user_id, ticker, p_nm, 'BUY', price, st_nm, f"하락장 저점포착 AI승인 [{bear_reason_str}]")
                                        self._send_telegram(self._fmt_trade_msg("🎣", f"하락장 저점 매수 ({bear_label})", ticker, p_nm, price, qty, strategy=st_nm, ai_reason=ai_reason, note=bear_reason_str))
                                else:
                                    pos.status = "AI 거절(하락장) 🛑"
                                    self._add_satellite_reject(ticker, ai_reason)
                                    self._send_telegram(
                                        f"🛑 <b>매수 거절</b>  ·  {self.alert_icon} {self.mode_name}\n"
                                        f"━━━━━━━━━━━━━━━━━━━━\n"
                                        f"📌 <b>{p_nm}</b>  <code>{ticker}</code>\n"
                                        f"🤖 {ai_reason}\n"
                                        f"📋 하락장 저점 — 근거 불충분 (당일 블랙리스트 등록)"
                                    )
                                    threading.Thread(target=self._rescreen_satellites, daemon=True).start()
                            elif self._buy_order(ticker, qty, pos, p_nm):
                                with self.lock: pos.last_order_time = time.time(); pos.status = "체결 대기 ⏳"
                                log_trade_journal(self.user_id, ticker, p_nm, 'BUY', price, st_nm, f"하락장 저점포착 [{bear_reason_str}]")
                                self._send_telegram(self._fmt_trade_msg("🎣", f"하락장 저점 매수 ({bear_label})", ticker, p_nm, price, qty, strategy=st_nm, note=bear_reason_str))
                        continue

                    if not self._check_etf_market_positive():
                        pos.status = "시장 약세 ⏸"
                        pos.status_msg = "ETF 지수 -1% 이하, 매수 보류"
                        continue
                    if not self._check_minute_trend_up(ticker):
                        pos.status = "추세 하락 📉"
                        pos.status_msg = "최근 5분봉 하락 추세, 매수 보류"
                        continue

                    # ── 국면별 포지션 사이징 ──────────────────────────────
                    if regime == "BULL":
                        bull_score, bull_reasons = get_bull_momentum_score(ex_df)
                        if bull_score >= 3:
                            entry_ratio, regime_label = 0.80, f"상승강신호({bull_score}개)"
                        elif bull_score >= 1:
                            entry_ratio, regime_label = 0.70, f"상승중신호({bull_score}개)"
                        else:
                            entry_ratio, regime_label = 0.60, "상승장기본진입"
                        regime_reason_str = " | ".join(bull_reasons) if bull_reasons else "상승 추세 추종"
                    else:  # NEUTRAL
                        neutral_score, neutral_reasons = get_neutral_range_score(ex_df)
                        if neutral_score == 0:
                            pos.status = "횡보 관망 ⏸"
                            pos.status_msg = "NEUTRAL 국면 — 레인지 신호 없음, 매수 차단"
                            continue
                        if neutral_score >= 3:
                            entry_ratio, regime_label = 0.55, f"횡보강신호({neutral_score}개)"
                        elif neutral_score == 2:
                            entry_ratio, regime_label = 0.45, f"횡보중신호({neutral_score}개)"
                        else:
                            entry_ratio, regime_label = 0.30, f"횡보약신호({neutral_score}개)"
                        regime_reason_str = " | ".join(neutral_reasons)

                    # 1차 매수: entry_ratio 의 75%, 나머지 25%는 2차 분할 매수용 유보
                    first_ratio  = entry_ratio * 0.75
                    reserve_ratio = entry_ratio * 0.25
                    entry_cash   = p_cash * first_ratio
                    reserve_cash = p_cash * reserve_ratio

                    if self.gemini:
                        pos.status = "AI 심사 중 🤖"
                        trade_ctx = self._build_trade_context(ticker, p_nm, price, ex_df, st_nm, regime)
                        decision, ai_reason = self.gemini.ai_approve_trade(sig, p_nm, ticker, price, st_nm, ind_val, self.hot_sectors, get_recent_trades(self.user_id, ticker), load_ai_rules(self.user_id) + "\n" + getattr(self, 'current_ai_market_view', ''), context=trade_ctx)
                        if decision:
                            qty = int((entry_cash * 0.98) // price)
                            if qty > 0 and self._buy_order(ticker, qty, pos, p_nm):
                                with self.lock:
                                    pos.last_order_time = time.time(); pos.status = "체결 대기 ⏳"
                                    pos.second_buy_price = price * 0.98   # -2% 눌림목 발동가
                                    pos.second_buy_cash  = reserve_cash
                                    pos.second_buy_done  = False
                                    pos.pyramid_done     = False
                                    pos.partial_sold     = False
                                log_trade_journal(self.user_id, ticker, p_nm, 'BUY', price, st_nm, f"AI 승인 [{regime_label}] 1차({int(first_ratio*100)}%) ({ai_reason})")
                                self._send_telegram(self._fmt_trade_msg("📈", f"AI 매수 승인  ({int(first_ratio*100)}% 1차)", ticker, p_nm, price, qty, strategy=f"{st_nm}  ·  {regime_label}", ai_reason=ai_reason, note=regime_reason_str))
                        else:
                            pos.status = "AI 거절 🛑"
                            # 당일 블랙리스트 등록 — 같은 이유로 재편입 금지
                            self._add_satellite_reject(ticker, ai_reason)
                            self._send_telegram(
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
                            log_trade_journal(self.user_id, ticker, p_nm, 'BUY', price, st_nm, f"알고리즘 [{regime_label}] 1차({int(first_ratio*100)}%): {regime_reason_str}")
                            self._send_telegram(self._fmt_trade_msg("📈", f"알고리즘 매수  ({int(first_ratio*100)}% 1차)", ticker, p_nm, price, qty, strategy=f"{st_nm}  ·  {regime_label}", note=regime_reason_str))

                elif sig == 'SELL' and p_sh > 0 and is_cd_passed:
                    if self.gemini:
                        pos.status = "AI 심사 중 🤖"
                        trade_ctx = self._build_trade_context(ticker, p_nm, price, ex_df, st_nm, regime)
                        decision, ai_reason = self.gemini.ai_approve_trade(sig, p_nm, ticker, price, st_nm, ind_val, self.hot_sectors, get_recent_trades(self.user_id, ticker), load_ai_rules(self.user_id) + "\n" + getattr(self, 'current_ai_market_view', ''), context=trade_ctx)
                        if decision:
                            if self._sell_order(ticker, p_sh, pos, p_nm):
                                with self.lock: pos.last_order_time = time.time(); pos.status = "체결 대기 ⏳"
                                profit = _net_profit(price, p_avg, p_sh)
                                log_trade_journal(self.user_id, ticker, p_nm, 'SELL', price, st_nm, f"AI 승인 ({ai_reason})", profit=profit)
                                self._send_telegram(self._fmt_trade_msg("📉", "AI 매도 승인", ticker, p_nm, price, p_sh, profit=profit, strategy=st_nm, ai_reason=ai_reason))
                                with self.lock:
                                    self.pnl_this_turn += profit
                                    if profit > 0 and self.core_positions and pos.cash >= profit * REINVEST_RATIO:
                                        pos.cash -= profit * REINVEST_RATIO
                                        for core in self.core_positions: core.cash += (profit * REINVEST_RATIO) / len(self.core_positions)
                                self._record_daily_pnl(profit)
                        else:
                            pos.status = "AI 거절(보유) 🛑"
                    else:
                        if self._sell_order(ticker, p_sh, pos, p_nm):
                            with self.lock: pos.last_order_time = time.time(); pos.status = "체결 대기 ⏳"
                            profit = _net_profit(price, p_avg, p_sh)
                            log_trade_journal(self.user_id, ticker, p_nm, 'SELL', price, st_nm, "알고리즘 직통", profit=profit)
                            self._send_telegram(self._fmt_trade_msg("📉", "알고리즘 매도", ticker, p_nm, price, p_sh, profit=profit, strategy=st_nm))
                            with self.lock: self.pnl_this_turn += profit
                            self._record_daily_pnl(profit)
            except Exception as e:
                logger.error(f"[{self.mode_name}] 위성 매매 오류 ({ticker}): {e}", exc_info=True)
            time.sleep(0.2)

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

    def _check_momentum_exit_one(self, slot_idx: int, mp: dict, now) -> bool:
        """슬롯 idx의 모멘텀 포지션 청산 조건 체크. 청산 시 True 반환."""
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
            candles = self.kis.get_minute_candles(ticker, count=10)
            if candles and len(candles) >= 3:
                peak_vol   = mp.get('peak_volume', 0)
                recent_vol = float(candles[-1].get('volume', 0))
                if recent_vol > peak_vol:
                    mp['peak_volume'] = recent_vol
                    peak_vol = recent_vol
                if peak_vol > 0:
                    if is_upper_limit:
                        pass  # 상한가 구간: 페이드 체크 스킵
                    elif is_post_upper:
                        if recent_vol < peak_vol * 0.30:
                            vol_fade = True
                    else:
                        if recent_vol < peak_vol * 0.5:
                            vol_fade = True
                is_ride = (peak_p / avg_p - 1) * 100 >= 10 if avg_p > 0 else False
                giveback_signal, _gpct, giveback_reason = check_giveback_stop(
                    candles, avg_p, peak_p, is_momentum_ride=is_ride
                )
        except Exception:
            pass

        time_over = enter_t and (now - enter_t).total_seconds() / 60 > 60

        sell_reason = None
        if vol_fade:
            sell_reason = "거래량 페이드(고점 대비 50%↓)"
        elif avg_p > 0 and price >= avg_p * 1.05:
            sell_reason = f"+5% 목표 달성 ({avg_p:,.0f}→{price:,.0f})"
        elif giveback_signal == 'FULL_EXIT':
            sell_reason = f"5분봉 반납률 전량 이탈: {giveback_reason}"
        elif avg_p > 0 and price <= avg_p - atr:
            sell_reason = f"ATR 손절 ({avg_p:,.0f}→{price:,.0f})"
        elif time_over:
            sell_reason = "보유 60분 초과 강제 청산"

        if sell_reason:
            if not self.kis.sell_market_order(ticker, shares):
                self.add_log(f"⚠️ 모멘텀#{slot_idx+1} 청산 주문 실패: {name}({ticker})")
                return False
            profit = _net_profit(price, avg_p, shares)
            with self.lock:
                if self.internal_cash is not None:
                    self.internal_cash += price * shares * (1 - 0.00015)
                self._last_trade_ts = time.time()
            log_trade_journal(self.user_id, ticker, name, 'SELL', price, "모멘텀슬롯", sell_reason, profit=profit)
            self.add_log(f"🏁 모멘텀#{slot_idx+1} 청산 | {name}({ticker}) {shares}주 @ {price:,.0f}원 | {sell_reason} | 손익: {profit:+,.0f}원")
            self._send_telegram(self._fmt_trade_msg("🏁", f"모멘텀#{slot_idx+1} 청산", ticker, name, price, shares, profit=profit, strategy="모멘텀슬롯", note=sell_reason))
            with self.lock:
                self.pnl_this_turn += profit
            self._record_daily_pnl(profit)
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
                    self._check_momentum_exit_one(i, mp, now)
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
                if ct not in held and ct not in used_tickers and not self._is_momentum_blacklisted(ct):
                    best = candidate
                    break
            if best is None:
                continue

            b_ticker = best['ticker']
            b_name   = best['name']
            b_price  = best['price']

            budget = total_assets * self.momentum_budget_ratio  # 슬롯당 5%
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

            # AI 심사
            if self.gemini:
                trade_ctx = (
                    f"모멘텀 슬롯#{slot_idx+1} 진입 요청.\n"
                    f"트리거: {best['trigger_reason']}\n"
                    f"모멘텀 점수: {best['momentum_score']:.1f}점\n"
                    f"현재가: {b_price:,.0f}원  ATR: {atr_val:,.0f}원"
                )
                m_decision, m_ai_reason = self.gemini.ai_approve_trade(
                    'BUY', b_name, b_ticker, b_price, "모멘텀슬롯",
                    {"momentum_score": best['momentum_score']}, self.hot_sectors,
                    get_recent_trades(self.user_id, b_ticker),
                    load_ai_rules(self.user_id), context=trade_ctx
                )
                if not m_decision:
                    self.add_log(f"🛑 모멘텀#{slot_idx+1} AI 거절: {b_name} — {m_ai_reason}")
                    self._add_momentum_exit(b_ticker)
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

            self.momentum_positions[slot_idx] = {
                'ticker': b_ticker, 'name': b_name, 'shares': qty,
                'avg_price': b_price, 'atr': atr_val, 'peak_price': b_price,
                'peak_volume': 0, 'enter_time': now,
                'score': best['momentum_score'], 'reason': best['trigger_reason'],
                'slot_idx': slot_idx,
            }
            log_trade_journal(self.user_id, b_ticker, b_name, 'BUY', b_price, "모멘텀슬롯", m_buy_note)
            self.add_log(f"{buy_label} | {b_name}({b_ticker}) {qty}주 @ {b_price:,.0f}원 | {best['trigger_reason']}")
            self._send_telegram(
                f"{buy_label} 진입!  ·  {self.alert_icon} {self.mode_name}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>{b_name}</b>  <code>{b_ticker}</code>\n"
                f"💰 <b>{b_price:,.0f}원</b> × <b>{qty}주</b> = <b>{b_price*qty:,.0f}원</b>\n"
                f"🔥 {best['trigger_reason']}\n"
                f"📊 모멘텀 점수 <b>{best['momentum_score']:.1f}점</b>\n"
                f"🤖 {m_ai_reason}\n"
                f"🛡️ 손절: ATR <b>{atr_val:,.0f}원</b>  ·  🎯 익절: <b>+5%</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⏰ {now.strftime('%H:%M KST')}"
            )
            used_tickers.add(b_ticker)
            held.add(b_ticker)

    def _rescreen_satellites(self):
        try:
            now = _now_kst()
            if not ("09:00" <= now.strftime('%H:%M') <= "19:50") or now.weekday() >= 5: return
            self.add_log(f"🦅 {self.mode_name} 위성 실시간 교체 탐색 중...")
            keep_tickers = set(); freed_cash = 0
            with self.lock: sat_items = list(self.satellite_positions.items())
            
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
                    if profit_rt > -5: keep_tickers.add(ticker)
                    else:
                        # I-05: trading_job과의 이중 매도 방지 — 락 안에서 shares 확인 후 주문
                        with self.lock:
                            shares_now = pos.shares
                        if shares_now > 0:
                            if self.kis: self.kis.sell_market_order(ticker, shares_now, price=int(price))
                            with self.lock:
                                # trading_job이 먼저 매도했을 경우 재진입 차단
                                if pos.shares > 0:
                                    qty, profit = pos.sell(price)
                                    freed_cash += pos.cash  # C-05: lock 내부에서 접근
                                if ticker in self.satellite_positions: del self.satellite_positions[ticker]
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
                        if shares_now > 0:
                            price_e = self.live_prices.get(t) or (self.kis.get_current_price(t) if self.kis else 0) or pos.avg_price
                            if self.kis and price_e:
                                self.kis.sell_market_order(t, shares_now, price=int(price_e))
                            with self.lock:
                                if pos.shares > 0:
                                    freed_cash += pos.cash
                        with self.lock:
                            if t in self.satellite_positions: del self.satellite_positions[t]
                            if t in self.satellite_strategies: del self.satellite_strategies[t]
                        keep_tickers.discard(t)
                        self.add_log(f"✂️ 위성 초과({self.num_satellites}개 한도) 정리: {pos.name}({t}) 청산")

            n_needed = self.num_satellites - len(keep_tickers)
            if n_needed <= 0: return

            # 당일 블랙리스트 종목을 충분히 걸러낼 수 있도록 여유 있게 조회
            self._refresh_blacklist()
            raw_info, self.hot_sectors = select_satellites(
                kis=self.kis, n=self.num_satellites + n_needed + len(self._satellite_rejects) + 3,
                verbose=False, gemini_client=self.gemini, bear_mode=(self.market_regime == "BEAR")
            )
            # 이미 보유 중인 종목 + 당일 AI 거절 블랙리스트 종목 모두 제외
            pre_filter = [
                c for c in raw_info
                if c['ticker'] not in keep_tickers
                and not self._is_satellite_blacklisted(c['ticker'])
            ]
            # AI 종목·전략 검토 (여유분 포함해서 검토 후 필요 개수만큼 잘라냄)
            ai_filtered = self._ai_filter_satellites(pre_filter)
            new_info = ai_filtered[:n_needed]
            if len(new_info) < n_needed:
                self.add_log(f"⚠️ 당일 블랙리스트/AI 퇴출로 인해 {n_needed - len(new_info)}개 위성 슬롯 공석 유지")

            for ticker in keep_tickers: freed_cash += self.satellite_positions[ticker].cash; self.satellite_positions[ticker].cash = 0
            
            if freed_cash > 0 and (len(keep_tickers) + len(new_info)) > 0:
                with self.lock:
                    alloc = freed_cash / (len(keep_tickers) + len(new_info))
                    for t in keep_tickers:
                        if self.satellite_positions[t].shares == 0: self.satellite_positions[t].cash = alloc
                    for c in new_info:
                        self.satellite_positions[c['ticker']] = Position(c['ticker'], c['name'], alloc)
                        self.satellite_strategies[c['ticker']] = c['strategy_name']
                self.satellite_info = [c for c in self.satellite_info if c['ticker'] in keep_tickers] + new_info

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
            for name, ticker in target_stocks: news_lines.append(f"- {name}: {fetch_recent_news(name)}"); time.sleep(0.1)
            news_context = "\n".join(news_lines) if news_lines else "뉴스 없음"
            
            flow_context = "\n\n".join(getattr(self, 'market_flow_history', []))
            combined_context = f"[뉴스]\n{news_context}\n\n[실시간 AI 추적]\n{flow_context}"
            
            report_data = generate_daily_market_report(gemini_client=self.gemini, verbose=False, news_context=combined_context, kis=self.kis)
            if report_data:
                today_str = _now_kst().strftime('%Y-%m-%d')
                if not isinstance(self.daily_report, dict) or self.daily_report.get('date') != today_str: self.daily_report = {'date': today_str, '11:00': None, '15:30': None, '20:00': None}
                content = report_data.get('report_markdown') if isinstance(report_data, dict) else str(report_data)
                self.daily_report[time_slot] = content
                self._save_state()
                self._send_telegram(f"📝 [리포트 발간]\n\n{content[:4000]}")
        except Exception as e:
            logger.error(f"[{self.mode_name}] 일일 리포트 생성 오류: {e}", exc_info=True)

    def _weekly_self_reflection(self):
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
        if self.gemini:
            new_rules = self.gemini.generate_weekly_reflection(history_text)
            if new_rules:
                save_ai_rules(self.user_id, new_rules)
                self._send_telegram(f"🧠 [봇 자가 학습 완료]\n\n{new_rules}")

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

        # schedule 라이브러리는 시스템 시계(UTC)를 사용하므로 모든 시간을 UTC로 지정
        # KST = UTC+9 → UTC = KST - 9h
        self.scheduler.every(1).minutes.do(self.trading_job)
        self.scheduler.every(30).minutes.do(lambda: self._run_threaded(self.analyze_continuous_market_flow))
        self.scheduler.every().day.at("02:00").do(lambda: self._run_threaded(lambda: self.generate_daily_report("11:00")))  # 11:00 KST
        self.scheduler.every().day.at("06:30").do(lambda: self._run_threaded(lambda: self.generate_daily_report("15:30")))  # 15:30 KST
        self.scheduler.every().day.at("11:00").do(lambda: self._run_threaded(lambda: self.generate_daily_report("20:00")))  # 20:00 KST
        self.scheduler.every().day.at("00:05").do(lambda: self._run_threaded(self._rescreen_satellites))                    # 09:05 KST
        self.scheduler.every(1).hours.do(lambda: self._run_threaded(self._rescreen_satellites))
        self.scheduler.every(30).minutes.do(clear_expired_cache)  # 모멘텀 캐시 만료 항목 정리
        self.scheduler.every().friday.at("07:00").do(lambda: self._run_threaded(self._weekly_self_reflection))              # 금요일 16:00 KST
        self.scheduler.every().day.at("23:00").do(lambda: self._run_threaded(self.refresh_websocket))                       # 08:00 KST
        self.scheduler.every().friday.at("17:00").do(lambda: self._run_threaded(self.run_lstm_training))                    # 토요일 02:00 KST → 금요일 17:00 UTC

        try:
            self.trading_job()
        except Exception as e:
            logger.error(f"[{self.mode_name}] 초기 trading_job 오류: {e}", exc_info=True)

        while self.is_running:
            try:
                self.scheduler.run_pending()
            except Exception as e:
                logger.error(f"[{self.mode_name}] 스케줄러 오류: {e}", exc_info=True)
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
            update_bot_status(self.user_id, True)
            self.add_log(f"▶️ [{self.mode_name}투자] 매매 봇이 시작되었습니다.")
            return True
        return False

    def stop(self):
        if self.is_running:
            self.is_running = False
            update_bot_status(self.user_id, False)
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
            cores_data = []
            for core in safe_core_positions:
                cp = float(getattr(core, '_last_price', 0) or self.live_prices.get(core.ticker, 0) or getattr(core, 'kis_current_price', 0) or core.avg_price or 0)
                core_val = float(core.shares) * cp
                total_realtime_stock_val += core_val
                cores_data.append({"name": core.name, "ticker": core.ticker, "shares": core.shares, "floor": core.floor_shares, "price": cp, "value": core_val, "budget": getattr(core, 'initial_cash', 0), "strategy": "장기 우상향" if core.ticker != self.core_ticker else "RSI + floor 보호", "status": getattr(core, 'status', '감시 중 👀'), "status_msg": getattr(core, 'status_msg', '지표 점검 중...')})

            satellites = []
            # num_satellites 한도만큼만 UI에 표시 (보유 중인 종목 우선)
            holding_items = [(t, p) for t, p in safe_satellite_items if p.shares > 0]
            empty_items   = [(t, p) for t, p in safe_satellite_items if p.shares == 0]
            capped_items  = (holding_items + empty_items)[:self.num_satellites]
            for ticker, pos in capped_items:
                sp = float(getattr(pos, '_last_price', 0) or self.live_prices.get(ticker, 0) or getattr(pos, 'kis_current_price', 0) or pos.avg_price or 0)
                sat_val = float(pos.shares) * sp
                total_realtime_stock_val += sat_val
                satellites.append({"name": pos.name, "ticker": ticker, "strategy": self.satellite_strategies.get(ticker, '-'), "shares": pos.shares, "price": sp, "value": sat_val, "budget": getattr(pos, 'initial_cash', getattr(pos, 'budget', 0)), "status": getattr(pos, 'status', '감시 중 👀'), "status_msg": getattr(pos, 'status_msg', '지표 점검 중...')})

            try:
                current_initial_cash = get_user_initial_cash(self.user_id, self._is_mock)
            except Exception: current_initial_cash = 10000000.0

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

            # 모멘텀 슬롯 3개 상태
            momentum_list = []
            for mp in self.momentum_positions:
                if mp:
                    mp_ticker = mp.get('ticker', '')
                    mp_price  = (self.live_prices.get(mp_ticker)
                                 or (self.kis.get_current_price(mp_ticker) if self.kis else 0)
                                 or mp.get('avg_price', 0))
                    mp_val  = float(mp.get('shares', 0)) * float(mp_price or 0)
                    total_realtime_stock_val += mp_val
                    avg_p   = mp.get('avg_price', 0)
                    pnl_pct = ((mp_price / avg_p) - 1) * 100 if avg_p > 0 and mp_price else 0
                    elapsed = ""
                    et = mp.get('enter_time')
                    if et:
                        try:
                            elapsed = f"{(datetime.now() - et).total_seconds() / 60:.0f}분 보유"
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
                else:
                    momentum_list.append(None)

            return {"is_running": self.is_running, "is_mock": self._is_mock, "has_keys": self.kis is not None, "logs": self.logs[-30:], "hot_sectors": self.hot_sectors, "num_satellites": self.num_satellites, "cores": cores_data, "satellites": satellites, "momentum_list": momentum_list, "mock_total_asset": mock_total_asset, "mock_pnl": mock_pnl, "mock_pnl_rt": mock_pnl_rt, "initial_cash": current_initial_cash}
        except Exception as critical_e:
            return {"is_running": False, "is_mock": self._is_mock, "has_keys": False, "logs": [{"time": "Error", "message": f"오류: {str(critical_e)}"}], "hot_sectors": [], "num_satellites": 3, "cores": [], "satellites": [], "momentum_list": [None, None, None], "mock_total_asset": 0, "mock_pnl": 0, "mock_pnl_rt": 0, "initial_cash": 10000000}