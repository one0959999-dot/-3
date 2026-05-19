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
from strategy import CorePosition, Position, get_rsi_signal, get_signal_by_strategy, REINVEST_RATIO
from stock_screener import select_satellites, generate_daily_market_report
from database import update_bot_status, save_portfolio_state, load_portfolio_state, log_trade_journal, get_recent_trades, save_ai_rules, load_ai_rules, get_user_initial_cash, set_user_initial_cash, add_user_initial_cash

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
        self.num_satellites = 5
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
        self.fundamental_cache = {}

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
            
        self._init_dummy_cores()
        self._restore_state()
        
        self.live_prices = {}
        self.ws_client = None

        def _async_network_connect():
            if self.kis:
                try:
                    app_key_token = self.kis.get_approval_key()
                    if app_key_token:
                        def on_price_update(ticker, price):
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
                    real_balance = self.kis.get_account_balance()
                    if real_balance:
                        self.cached_balance = real_balance
                        self._sync_internal_balances(real_balance)
                    
                    if self.ws_client:
                        with self.lock:
                            current_tickers = [c.ticker for c in self.core_positions] + list(self.satellite_positions.keys())
                            for idx_ticker, _ in self.market_indices:
                                if idx_ticker not in current_tickers:
                                    current_tickers.append(idx_ticker)

                        for t in current_tickers:
                            if t not in self.ws_client.subscribed_tickers:
                                self.ws_client.subscribe(t)
                        for t in list(self.ws_client.subscribed_tickers):
                            if t not in current_tickers:
                                self.ws_client.unsubscribe(t)
            except Exception as e:
                print(f"[{self.mode_name} _perpetual_sync_loop 에러] {e}")
            time.sleep(10)

    def _sync_internal_balances(self, real_balance):
        with self.lock:
            try:
                if not real_balance or 'stocks' not in real_balance: return
                real_cash = float(real_balance.get('total_cash', 0))
                real_stock_value = float(real_balance.get('total_value', 0))
                real_purchase = float(real_balance.get('total_purchase', 0))
                total_equity = real_cash + real_stock_value
                
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
                    if self.pnl_this_turn != 0 and abs(current_asset_cost - self.last_asset_cost) < 100:
                        pass 
                    else:
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
                            self.add_log(f"🌟 {self.mode_name} 계좌 미등록 종목 '{stock_name}'을 위성으로 강제 편입합니다!")
                            new_sat = Position(t, stock_name, 0.0)
                            new_sat.shares = q; new_sat.avg_price = p; new_sat.kis_current_price = c_p 
                            self.satellite_positions[t] = new_sat
                            self.satellite_strategies[t] = 'RSI(9) 30/70'
                            if not any(x['ticker'] == t for x in self.satellite_info):
                                self.satellite_info.append({'ticker': t, 'name': stock_name, 'strategy_name': 'RSI(9) 30/70', 'return_pct': 0.0, 'sector': '-'})
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
        full_msg = f"{self.alert_icon}[{self.mode_name}] {message}"
        threading.Thread(target=self.telegram.send_message, args=(full_msg,), daemon=True).start()

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

    def initialize_portfolio(self, total_cash):
        self.add_log("포트폴리오 초기화 중...")
        self.satellite_info, self.hot_sectors = select_satellites(kis=self.kis, n=self.num_satellites, verbose=False, gemini_client=self.gemini)
        from stock_screener import select_ai_core_stock
        self.satellite_strategies = {c['ticker']: c['strategy_name'] for c in self.satellite_info}
        log_lines = [f"  {i+1}. {c['name']} ({c['ticker']}) → [{c['strategy_name']}] {c['return_pct']:+.1f}%" for i, c in enumerate(self.satellite_info)]
        for line in log_lines: self.add_log(f"✅ {line.strip()}")
        self._send_telegram(f"🔍 {self.mode_name} 위성 종목 선정!\n" + "\n".join(log_lines))

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
            self.num_satellites = state.get("num_satellites", 5)
            self.last_screen_month = state.get("last_screen_month")
            lsd_str = state.get("last_screen_date")
            self.last_screen_date = datetime.strptime(lsd_str, '%Y-%m-%d').date() if lsd_str else None
            self.daily_pnl = state.get("daily_pnl", {})
            self.daily_report = state.get("daily_report", None)
            return True
        except Exception as e:
            logger.error(f"[{self.mode_name}] 상태 복구 실패: {e}", exc_info=True)
            return False

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

                if getattr(self, 'is_crisis_mode', False):
                    if self.kis:
                        main_idx_ticker = self.market_indices[0][0]
                        idx_cp = self.kis.get_current_price(main_idx_ticker)
                        if idx_cp:
                            extended_df = self._get_extended_ohlcv(main_idx_ticker, idx_cp)
                            if not extended_df.empty and len(extended_df) >= 5:
                                if idx_cp > extended_df['close'].ewm(span=5, adjust=False).mean().iloc[-1]:
                                    msg = f"🚀 {self.mode_name} 저점 반등 확인! 관망 모드 해제."
                                    self.add_log(msg); self._send_telegram(msg)
                                    self.is_crisis_mode = False; self.peak_total_asset = 0
                    return

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

                if c_sig == 'BUY' and c_cash >= cp and (time.time() - getattr(core, 'last_order_time', 0) > 60):
                    qty = int((c_cash * 0.98) // cp)
                    if qty > 0 and self.kis and self.kis.buy_market_order(c_tk, qty):
                        with self.lock: core.last_order_time = time.time(); core.status = "체결 대기 ⏳"
                        self.add_log(f"💎 {c_nm} 매수 완료 | {qty}주 @ {cp:,}원"); self._send_telegram(f"💎 {c_nm} 매수")
                elif c_sig == 'SELL' and c_sh > c_fl and (time.time() - getattr(core, 'last_order_time', 0) > 60):
                    sellable = c_sh - c_fl
                    if sellable > 0 and self.kis and self.kis.sell_market_order(c_tk, sellable):
                        with self.lock: core.last_order_time = time.time(); core.status = "체결 대기 ⏳"; self.pnl_this_turn += (cp - core.avg_price)*sellable
                        self.add_log(f"💎 {c_nm} 매도 완료 | {sellable}주 @ {cp:,}원"); self._send_telegram(f"💎 {c_nm} 매도")
            except Exception as e:
                logger.error(f"[{self.mode_name}] 코어 매매 오류 ({c_tk}): {e}", exc_info=True)
            time.sleep(0.2)

        with self.lock: trading_sat_items = list(self.satellite_positions.items())
        shared_macro_context = self.kis.get_macro_context() if self.kis else "시황 정보 없음"
        
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

                cache_key = f"{ticker}_{_now_kst().strftime('%Y-%m-%d')}"
                with self.lock: has_cache = cache_key in self.fundamental_cache
                if has_cache: 
                    with self.lock: fin_data = self.fundamental_cache[cache_key]
                else: fin_data = "재무 조회 불가"

                is_cd_passed = (time.time() - getattr(pos, 'last_order_time', 0) > 60)

                if p_sh > 0 and price > 0 and is_cd_passed:
                    if price > p_max: 
                        with self.lock: pos.max_price = price; p_max = price
                    if p_max >= p_avg + (1.0 * atr_14) and price <= p_max - (1.5 * atr_14):
                        if self.kis and self.kis.sell_market_order(ticker, p_sh):
                            with self.lock: pos.last_order_time = time.time(); pos.max_price = 0; pos.status = "체결 대기 ⏳"
                            profit = (price - p_avg) * p_sh
                            log_trade_journal(self.user_id, ticker, p_nm, 'SELL', price, st_nm, "ATR 트레일링 익절", profit=profit)
                            self._send_telegram(f"🎯 [{p_nm}] ATR 익절 완료! 손익: {profit:+,.0f}원")
                            with self.lock: self.pnl_this_turn += profit
                        continue

                if p_sh > 0 and p_avg > 0 and is_cd_passed:
                    if price <= p_avg - (2.5 * atr_14):
                        if self.kis and self.kis.sell_market_order(ticker, p_sh):
                            with self.lock: pos.last_order_time = time.time(); pos.status = "체결 대기 ⏳"
                            profit = (price - p_avg) * p_sh
                            log_trade_journal(self.user_id, ticker, p_nm, 'SELL', price, st_nm, "ATR 하드 손절", profit=profit)
                            self._send_telegram(f"💥 [{p_nm}] ATR 손절 완료! 손익: {profit:+,.0f}원")
                            with self.lock: self.pnl_this_turn += profit
                        continue

                if sig == 'BUY' and p_sh == 0 and is_cd_passed and is_golden_hours:
                    if self.gemini:
                        pos.status = "AI 심사 중 🤖"
                        decision, ai_reason = self.gemini.ai_approve_trade(sig, p_nm, ticker, price, st_nm, ind_val, self.hot_sectors, get_recent_trades(self.user_id, ticker), load_ai_rules(self.user_id) + "\n" + getattr(self, 'current_ai_market_view', ''))
                        if decision:
                            qty = int((p_cash * 0.98) // price)
                            if qty > 0 and self.kis and self.kis.buy_market_order(ticker, qty):
                                with self.lock: pos.last_order_time = time.time(); pos.status = "체결 대기 ⏳"
                                log_trade_journal(self.user_id, ticker, p_nm, 'BUY', price, st_nm, f"AI 승인 ({ai_reason})")
                                self._send_telegram(f"📈 [{p_nm}] AI 매수 완료\n👉 {ai_reason}")
                        else:
                            pos.status = "AI 거절 🛑"
                            self._send_telegram(f"🛑 [{p_nm}] 매수 거절 ➡️ 즉시 대체 종목 탐색\n👉 {ai_reason}")
                            threading.Thread(target=self._rescreen_satellites, daemon=True).start()
                    else:
                        qty = int((p_cash * 0.98) // price)
                        if qty > 0 and self.kis and self.kis.buy_market_order(ticker, qty):
                            with self.lock: pos.last_order_time = time.time(); pos.status = "체결 대기 ⏳"
                            log_trade_journal(self.user_id, ticker, p_nm, 'BUY', price, st_nm, "알고리즘 직통")
                            self._send_telegram(f"📈 [{p_nm}] 알고리즘 매수")

                elif sig == 'SELL' and p_sh > 0 and is_cd_passed:
                    if self.gemini:
                        pos.status = "AI 심사 중 🤖"
                        decision, ai_reason = self.gemini.ai_approve_trade(sig, p_nm, ticker, price, st_nm, ind_val, self.hot_sectors, get_recent_trades(self.user_id, ticker), load_ai_rules(self.user_id) + "\n" + getattr(self, 'current_ai_market_view', ''))
                        if decision:
                            if self.kis and self.kis.sell_market_order(ticker, p_sh):
                                with self.lock: pos.last_order_time = time.time(); pos.status = "체결 대기 ⏳"
                                profit = (price - p_avg) * p_sh 
                                log_trade_journal(self.user_id, ticker, p_nm, 'SELL', price, st_nm, f"AI 승인 ({ai_reason})", profit=profit)
                                self._send_telegram(f"📉 [{p_nm}] AI 매도 완료 | 이익: {profit:+,.0f}원\n👉 {ai_reason}")
                                with self.lock:
                                    self.pnl_this_turn += profit
                                    if profit > 0 and self.core_positions and pos.cash >= profit * REINVEST_RATIO:
                                        pos.cash -= profit * REINVEST_RATIO
                                        for core in self.core_positions: core.cash += (profit * REINVEST_RATIO) / len(self.core_positions)
                        else:
                            pos.status = "AI 거절(보유) 🛑"
                    else:
                        if self.kis and self.kis.sell_market_order(ticker, p_sh):
                            with self.lock: pos.last_order_time = time.time(); pos.status = "체결 대기 ⏳"
                            profit = (price - p_avg) * p_sh 
                            log_trade_journal(self.user_id, ticker, p_nm, 'SELL', price, st_nm, "알고리즘 직통", profit=profit)
                            self._send_telegram(f"📉 [{p_nm}] 알고리즘 매도 | 이익: {profit:+,.0f}원")
                            with self.lock: self.pnl_this_turn += profit
            except Exception as e:
                logger.error(f"[{self.mode_name}] 위성 매매 오류 ({ticker}): {e}", exc_info=True)
            time.sleep(0.2)
        self._save_state()

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
                        if self.kis: self.kis.sell_market_order(ticker, pos.shares, price=int(price))
                        with self.lock: qty, profit = pos.sell(price)
                        freed_cash += pos.cash
                        with self.lock:
                            if ticker in self.satellite_positions: del self.satellite_positions[ticker]

            n_needed = self.num_satellites - len(keep_tickers)
            if n_needed <= 0: return

            raw_info, self.hot_sectors = select_satellites(kis=self.kis, n=self.num_satellites + n_needed, verbose=False, gemini_client=self.gemini)
            new_info = [c for c in raw_info if c['ticker'] not in keep_tickers][:n_needed]

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
            
            report_data = generate_daily_market_report(gemini_client=self.gemini, verbose=False, news_context=combined_context)
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
        try:
            conn = get_db_connection()
            rows = conn.execute('SELECT date(created_at) as date, stock_name, action, price, ai_reason, profit FROM trade_journal WHERE user_id = ? ORDER BY created_at DESC LIMIT 30', (self.user_id,)).fetchall()
        except Exception as e:
            logger.warning(f"[{self.mode_name}] 주간 반성 데이터 조회 실패: {e}")
            rows = []
        finally: conn.close()
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

        # initialize_portfolio 실패 시 스레드가 죽지 않도록 보호
        try:
            if not self._restore_state():
                self.initialize_portfolio(total_cash)
        except Exception as e:
            logger.error(f"[{self.mode_name}] 포트폴리오 초기화 실패 (기본 코어로 계속 진행): {e}", exc_info=True)

        # schedule 라이브러리는 시스템 시계(UTC)를 사용하므로 모든 시간을 UTC로 지정
        # KST = UTC+9 → UTC = KST - 9h
        self.scheduler.every(5).minutes.do(self.trading_job)
        self.scheduler.every(30).minutes.do(lambda: self._run_threaded(self.analyze_continuous_market_flow))
        self.scheduler.every().day.at("02:00").do(lambda: self._run_threaded(lambda: self.generate_daily_report("11:00")))  # 11:00 KST
        self.scheduler.every().day.at("06:30").do(lambda: self._run_threaded(lambda: self.generate_daily_report("15:30")))  # 15:30 KST
        self.scheduler.every().day.at("11:00").do(lambda: self._run_threaded(lambda: self.generate_daily_report("20:00")))  # 20:00 KST
        self.scheduler.every().day.at("00:05").do(lambda: self._run_threaded(self._rescreen_satellites))                    # 09:05 KST
        self.scheduler.every(1).hours.do(lambda: self._run_threaded(self._rescreen_satellites))
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
                    self.ws_client = self._create_websocket(app_key, lambda t, p: self.live_prices.update({t: p}))
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
        sorted_days = sorted(self.daily_pnl.keys())
        return {"labels": sorted_days, "values": [round(self.daily_pnl[d]) for d in sorted_days]}

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
            for ticker, pos in safe_satellite_items:
                sp = float(getattr(pos, '_last_price', 0) or self.live_prices.get(ticker, 0) or getattr(pos, 'kis_current_price', 0) or pos.avg_price or 0)
                sat_val = float(pos.shares) * sp
                total_realtime_stock_val += sat_val
                satellites.append({"name": pos.name, "ticker": ticker, "strategy": self.satellite_strategies.get(ticker, '-'), "shares": pos.shares, "price": sp, "value": sat_val, "budget": getattr(pos, 'initial_cash', getattr(pos, 'budget', 0)), "status": getattr(pos, 'status', '감시 중 👀'), "status_msg": getattr(pos, 'status_msg', '지표 점검 중...')})

            try:
                current_initial_cash = get_user_initial_cash(self.user_id, self._is_mock)
            except Exception: current_initial_cash = 10000000.0

            if self.cached_balance:
                api_cash = float(self.cached_balance.get('total_cash', 0))
                mock_total_asset = api_cash + total_realtime_stock_val
                mock_pnl = mock_total_asset - current_initial_cash
                mock_pnl_rt = (mock_pnl / current_initial_cash * 100) if current_initial_cash > 0 else 0
            else:
                mock_total_asset = 0.0; mock_pnl = 0.0; mock_pnl_rt = 0.0

            return {"is_running": self.is_running, "is_mock": self._is_mock, "has_keys": self.kis is not None, "logs": self.logs[-30:], "hot_sectors": self.hot_sectors, "num_satellites": self.num_satellites, "cores": cores_data, "satellites": satellites, "mock_total_asset": mock_total_asset, "mock_pnl": mock_pnl, "mock_pnl_rt": mock_pnl_rt, "initial_cash": current_initial_cash}
        except Exception as critical_e:
            return {"is_running": False, "is_mock": self._is_mock, "has_keys": False, "logs": [{"time": "Error", "message": f"오류: {str(critical_e)}"}], "hot_sectors": [], "num_satellites": 5, "cores": [], "satellites": [], "mock_total_asset": 0, "mock_pnl": 0, "mock_pnl_rt": 0, "initial_cash": 10000000}