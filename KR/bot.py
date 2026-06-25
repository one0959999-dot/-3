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

                                                   
_KST = timezone(timedelta(hours=9))

def _now_kst():
                                                    
    return datetime.now(_KST).replace(tzinfo=None)

from base.telegram_bot import TelegramNotifier
from KR.strategy import CorePosition, Position, get_rsi_signal, get_composite_signal, REINVEST_RATIO, get_market_regime, get_market_regime_detail, get_bear_bounce_signal, get_bear_bottom_score, get_bear_budget_ratio, get_bull_momentum_score, get_neutral_range_score, INVERSE_ETF_TICKER, INVERSE_ETF_NAME, INVERSE_BUDGET_RATIO, DEFENSIVE_ASSETS, check_giveback_stop, check_early_drop_stop, check_theme_overextension_exit, check_rsi_progressive_exit, calculate_entry_score, get_entry_threshold, get_budget_ratio_from_score, calc_rsi, calculate_core_entry_score, get_core_entry_threshold
from KR.screener import select_satellites, generate_daily_market_report
from base.database import update_bot_status, save_portfolio_state, load_portfolio_state, log_trade_journal, get_recent_trades, save_ai_rules, load_ai_rules, get_ai_rules_history, get_user_initial_cash, set_user_initial_cash, add_user_initial_cash, get_news_api_keys, get_sector_guide, log_ai_decision, update_ai_decision_outcome
from ai.news_monitor import NewsMonitor
from base.toss_api import TossInvestApi

_SELL_FEE = 0.00015                     
_SELL_TAX = 0.0018                    

                                                             
_roe_kr_cache: dict = {}                                 

def _roe_turnaround_kr(ticker: str) -> tuple:
                                                              
    cached = _roe_kr_cache.get(ticker)
    if cached and time.time() - cached[0] < 3600:
        return cached[1], cached[2]
    score, reason = 0, ""
    try:
        from pykrx import stock as pykrx_stock
        from datetime import datetime, timedelta
        today = datetime.now()
        roe_vals = []
        for i in range(1, 5):                      
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
        roe_vals.reverse()             
        if len(roe_vals) < 3:
            return 0, ""
                              
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
                                    
    def __init__(self, user_id, toss_config=None, telegram_config=None, core_stocks=None, satellite_stocks=None):
        self.user_id = user_id
        self.is_running = False
        self.thread = None
        self.logs = collections.deque(maxlen=100)                        
        self.num_satellites = 3            
        self._is_mock = False                  
        self.mode_name = "KR"
        self.alert_icon = "🔴"

        self.core_ratio = 0.40                           
        self.satellite_ratio = 0.60                    
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

                                                
        _u = self.user_core_stocks[0] if self.user_core_stocks else None
        self.core_ticker = _u['ticker'] if _u else ""
        self.core_name   = _u['name']   if _u else ""

        self.core_positions = []
        self.satellite_positions = {}
        self.satellite_info = []
        self.daily_pnl = {}
        self.last_screen_month = None
        self.last_screen_date = None
        self.last_core_rebalance_date = None                                 
        self.hot_sectors = []
                                                                 
                               
                                                                                               
        self._monday_swap_plan: dict = {}
        self._weekend_scan_done: str = ""                        
        self.daily_report = None
        self.volume_surge_details = []                                             
        self._last_total_equity = 0.0

        # ── 포트폴리오 2단계 killswitch (일중 고점대비 equity drawdown) ──
        self.KILLSWITCH_ENABLED   = True
        self.KILL_PAUSE_DD        = 0.10   # -10% → 신규매수 중단
        self.KILL_LIQUIDATE_DD    = 0.20   # -20% → 전포지션 청산 + 당일 거래중단
        self._equity_peak_today   = 0.0
        self._equity_peak_date    = None
        self._trading_halted_date = None   # 'YYYY-MM-DD' = 당일 killswitch L2 발동됨
        self._killswitch_last_warn= ''


                                                          
        self.internal_cash = None                                             
        self._last_trade_ts = 0.0                                            
        self._dca_prev_cash      = 0.0                           
        self._dca_deposit_trigger= False                   
        self._dca_deposit_amount = 0.0              
        self.fundamental_cache = {}

                                                         
                                                                 
        self._bl_date               = ""                                
        self._satellite_rejects     : dict = {}                                
        self._satellite_reject_rsn  : dict = {}                         
        self._SAT_REJECT_COOLDOWN   = 300                         

                                                              
                                                    
                                                     
                                            

                                                        
                                                               
                                                            
        self._daily_loss_by_ticker  : dict = {}

                                                          
                                                             
                                                        
        self.entry_thresholds: dict = {}                                          

                                       
        self.market_regime = "NEUTRAL"
        self.last_regime_check = 0.0
        self._regime_check_interval = 3600             
        self._bull_pending_days = 0                                      
        self._ai_market_entry_bonus = 0                            
        self._last_defensive_check = 0.0                       
        self._defensive_sold_ts   = {}                                                 

                                                               
        self._trades_since_reflection = 0                            
        self._last_emergency_reflection_ts = 0.0                          
        self._EMERGENCY_LOSS_THRESHOLD = -80_000                         
        self._EMERGENCY_COOLDOWN = 4 * 3600                          

        self.toss: TossInvestApi | None = None
        self.real_toss = None                  
        self.telegram = None
        self.claude = None
        self.news_monitor: NewsMonitor | None = None                        

                      
        self._last_dart_check     = 0.0                       
        self._dart_check_interval = 600             
        self._last_earnings_check = 0.0                  
        self._earnings_check_interval = 3600            
        self._news_check_lock      = threading.Lock()                     
        self._notified_disclosures: set  = set()                                            
        self._earnings_notified:    dict = {}                                                    

                                                                   
        self.sector_guide: str = get_sector_guide(user_id) or ''

        self._init_api(toss_config)
        self._init_news_monitor()                    
        
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
        self.initial_capital_captured = False                          

        self._init_dummy_cores()
        self._init_state_restored = self._restore_state()                         
        
        self.live_prices: dict = {}                                   
        self.ws_client = None                           

        self.perpetual_thread = threading.Thread(target=self._perpetual_sync_loop, daemon=True)
        self.perpetual_thread.start()
        self.add_log(f"User {user_id} [{self.mode_name}] Bot Controller 가동 완료.")

    def _init_api(self, toss_config):
                           
        client_id     = (toss_config or {}).get('client_id') or (toss_config or {}).get('app_key', '')
        client_secret = (toss_config or {}).get('client_secret') or (toss_config or {}).get('app_secret', '')
        account_seq   = (toss_config or {}).get('account_seq') or (toss_config or {}).get('account_no', '')
        if client_id and client_secret:
            try:
                self.toss = TossInvestApi(
                    client_id     = client_id.strip(),
                    client_secret = client_secret.strip(),
                    account_seq   = account_seq.strip(),
                )
                self.add_log(f"✅ [KR봇] 토스증권 API 연결됨")
            except Exception as e:
                logger.warning(f"[{self.mode_name}] 토스 API 초기화 실패: {e}")
                self.toss = None
        else:
            self.toss = None

    def _init_news_monitor(self):
                                                
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
                                           
        if dart_key and naver_id and naver_secret:
            self.news_monitor = NewsMonitor(dart_key, naver_id, naver_secret)
            self.add_log("📡 뉴스 모니터 키 업데이트 완료")
        else:
            self.news_monitor = None

    def _perpetual_sync_loop(self):
        while True:
            try:
                if self.toss:
                                                                 
                    result_holder = [None]
                    def _fetch():
                        try:
                            result_holder[0] = self.toss.get_account_balance()
                        except Exception as fe:
                            logger.warning(f"[{self.mode_name}] 잔고 조회 오류: {fe}")
                    t = threading.Thread(target=_fetch, daemon=True)
                    t.start()
                    t.join(timeout=15)

                    real_balance = result_holder[0]
                    if real_balance:
                        self.cached_balance = real_balance
                        self._sync_internal_balances(real_balance)
                        try:
                            self._killswitch_enforce()     # 포트폴리오 급락 감시(L2 자동청산)
                        except Exception as _ke:
                            logger.error(f"[{self.mode_name}] killswitch 오류: {_ke}")

                                                                       
                    try:
                        with self.lock:
                            poll_tickers = (
                                [c.ticker for c in self.core_positions]
                                + list(self.satellite_positions.keys())
                                + [t for t, _ in self.market_indices]
                                + [d['ticker'] for d in DEFENSIVE_ASSETS]
                            )
                        poll_tickers = list(dict.fromkeys(poll_tickers))         
                        if poll_tickers:
                            prices = self.toss.get_prices(poll_tickers)
                            with self.lock:
                                self.live_prices.update(prices)
                    except Exception as _pe:
                        logger.debug(f"[{self.mode_name}] 현재가 폴링 오류: {_pe}")

            except Exception as e:
                logger.error(f"[{self.mode_name}] _perpetual_sync_loop 오류: {e}", exc_info=True)
                           
            if int(time.time()) % 300 < 30:
                try: self._save_state()
                except Exception: pass
            time.sleep(30)

    def _sync_internal_balances(self, real_balance):
        with self.lock:
            try:
                if not real_balance or 'stocks' not in real_balance:
                    if self.internal_cash is None:
                        self.internal_cash = 0.0                            
                    return
                real_cash = float(real_balance.get('total_cash', 0))
                real_stock_value = float(real_balance.get('total_value', 0))
                real_purchase = float(real_balance.get('total_purchase', 0))
                total_equity = real_cash + real_stock_value
                                  
                if total_equity > 0:
                    self._last_total_equity = total_equity

                            
                                         
                                                                  
                if self.internal_cash is None or (time.time() - self._last_trade_ts >= 120):
                    self.internal_cash = real_cash

                pure_principal = real_cash + real_purchase

                if not getattr(self, 'initial_capital_captured', False):
                                                        
                                                                         
                                                               
                                                                  
                                                                        
                                                                                    
                                                                                   
                    if pure_principal > 0:
                        db_cash = get_user_initial_cash(self.user_id, self._is_mock)
                        if db_cash == 10000000.0:
                            set_user_initial_cash(self.user_id, pure_principal, self._is_mock)
                            self.add_log(f"💰 [{self.mode_name} 원금 셋업] 투자 원금 {pure_principal:,.0f}원 확정 (첫 실행 감지).")
                        self.initial_capital_captured = True                       
                
                current_asset_cost = real_cash + real_purchase
                if self.last_asset_cost is not None:
                                                               
                                                                          
                    expected_asset_cost = self.last_asset_cost + self.pnl_this_turn
                    self.pnl_this_turn = 0.0
                    deposit_delta = current_asset_cost - expected_asset_cost
                                                    
                                                                         
                                                                         
                                                   
                    if not self._is_mock:
                        if deposit_delta > 10000 or deposit_delta < -10000:
                            add_user_initial_cash(self.user_id, deposit_delta, self._is_mock)
                            if deposit_delta > 0: self.add_log(f"💰 {self.mode_name} 계좌 외부 입금 포착: +{deposit_delta:,.0f}원")
                            else: self.add_log(f"💸 {self.mode_name} 계좌 외부 출금 포착: {deposit_delta:,.0f}원")
                    self.last_asset_cost = current_asset_cost
                else:
                    self.last_asset_cost = current_asset_cost
                
                if total_equity >= 0:
                                                                       
                                 
                                                   
                                                               
                                                    
                    _active_cores = [c for c in self.core_positions if c.ticker != "TBD"]
                    _active_sats  = list(self.satellite_positions.values())
                    n_total = max(1, len(_active_cores) + len(_active_sats))
                    _regime_now = getattr(self, 'market_regime', 'NEUTRAL')
                    if _regime_now == "BEAR":
                                                               
                        _tradable = total_equity * 0.60
                    else:
                        _tradable = total_equity
                    budget_per = _tradable / n_total if total_equity > 0 else 0

                                                                       
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

                                              
                    buyable_cash = real_cash
                    if self.toss and hasattr(self.toss, 'get_buyable_cash'):
                        try:
                            _bc = float(self.toss.get_buyable_cash() or 0)
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

                                                  
                                                            
                new_shares: dict = {}                                                
                for real_stock in real_balance['stocks']:
                    t = real_stock.get('ticker', '')
                    if not t:
                        continue
                    try:
                        q   = int(real_stock['shares'])
                        p   = float(real_stock['purchase_price'])
                        c_p = float(real_stock.get('current_price', p))
                    except (KeyError, ValueError, TypeError) as _e:
                        logger.warning(f"[{self.mode_name}] 토스 잔고 파싱 오류 ({t}): {_e} — 건너뜀")
                        continue
                    if q < 0 or p < 0 or c_p < 0:
                        logger.warning(f"[{self.mode_name}] 토스 비정상 값 ({t}) q={q} p={p} cp={c_p} — 건너뜀")
                        continue
                    stock_name = real_stock.get('name', t)
                    new_shares[t] = (q, p, c_p, stock_name)

                                                     
                                                                       
                _reported_val = float(real_balance.get('total_value', 0))
                if not new_shares and _reported_val > 100_000:
                    logger.warning(
                        f"[{self.mode_name}] 토스 stocks 빈 응답 (total_value={_reported_val:,.0f}원) — 포지션 초기화 건너뜀"
                    )
                    return
                for core in self.core_positions:
                    core.shares = 0
                    core.floor_shares = 0                                          
                for sat in self.satellite_positions.values(): sat.shares = 0

                for t, (q, p, c_p, stock_name) in new_shares.items():
                    is_core = False
                    for core in self.core_positions:
                        if core.ticker == t:
                            core.shares = q; core.avg_price = p; core.toss_current_price = c_p
                            if core.floor_shares == 0 and q > 0: core.floor_shares = max(1, int(q * self.core_min_floor_ratio))
                            is_core = True; break

                    if not is_core:
                        if t in self.satellite_positions:
                            sat = self.satellite_positions[t]
                            sat.shares = q; sat.avg_price = p; sat.toss_current_price = c_p
                        else:
                                                                  
                            if len(self.satellite_positions) < self.num_satellites:
                                self.add_log(f"🌟 {self.mode_name} 계좌 미등록 종목 '{stock_name}'을 위성으로 강제 편입합니다!")
                                new_sat = Position(t, stock_name, 0.0)
                                new_sat.shares = q; new_sat.avg_price = p; new_sat.toss_current_price = c_p
                                new_sat.user_managed = True                                
                                self.satellite_positions[t] = new_sat
                                if not any(x['ticker'] == t for x in self.satellite_info):
                                    self.satellite_info.append({'ticker': t, 'name': stock_name, 'return_pct': 0.0, 'sector': '계좌편입'})
                            else:
                                logger.warning(f"[{self.mode_name}] 위성 한도({self.num_satellites}) 초과 — '{stock_name}'({t}) 자동 편입 생략")

                                                                  
                                                               
                                                         
                                                          
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
\
\
\
\
           
                                             
        _existing = {c.ticker: c for c in self.core_positions if c.ticker != "TBD"}

        self.core_positions = []
        user_tickers_seen: set = set()
                                   
        for c in self.user_core_stocks[:2]:
            if c.get('ticker') and c['ticker'] not in user_tickers_seen:
                pos = CorePosition(c['ticker'], c['name'], initial_cash=0)
                if c.get('dca'):
                    pos.dca_mode           = True
                    pos.dca_amount         = float(c.get('dca_amount', 0))
                    pos.dca_interval_hours = int(c.get('dca_hours', 72))
                    pos.dca_dip_pct        = float(c.get('dca_dip_pct', 3.0))
                                              
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
                                                  
        for i in range(len(self.core_positions), 2):
            ph = CorePosition("TBD", f"종목 미지정 #{i+1}", initial_cash=0)
            ph.status = "⚙️ 종목 설정 필요"
            ph.status_msg = "상단 ⚙️ 버튼을 눌러 코어 종목을 지정해주세요."
            self.core_positions.append(ph)
            
        if self.toss:
            def _async_init_balance():
                try:
                    real_balance = self.toss.get_account_balance()
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
\
\
\
\
\
           
        user_tickers = {s['ticker'] for s in self.user_satellite_stocks if s.get('ticker')}

                                                                    
                                                     
        for _t in list(self.satellite_positions.keys()):
            if _t not in user_tickers and int(self.satellite_positions[_t].shares) == 0:
                                                                    
                self.satellite_positions.pop(_t, None)
                self.satellite_info = [c for c in self.satellite_info if c.get('ticker') != _t]

        if not self.user_satellite_stocks:
            return
                                         
        filtered = [c for c in self.satellite_info if c['ticker'] not in user_tickers]
        pinned = []
        for s in self.user_satellite_stocks:
            if not s.get('ticker') or not s.get('name'):
                continue
            t = s['ticker']
                                                              
            if not (t.isdigit() and len(t) == 6):
                logger.warning(f"[KR봇] 사용자지정 위성 무시: {t} (KR 형식 아님 — US 봇 전용 종목)")
                continue
                                                    
            _ret = 0.0; _rsi = None; _vol_ratio = None
            try:
                df = self._get_cached_base_ohlcv(t)
                if df.empty and self.toss:
                    df = self.toss.get_ohlcv(t, "D")
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
        if self.toss:
            df = self.toss.get_ohlcv(ticker, "D")
            if df is None or (not hasattr(df, 'columns')) or ('high' not in df.columns): return pd.DataFrame()
            if df is not None and not df.empty and 'date' in df.columns:
                df['date'] = pd.to_datetime(df['date'])
                df = df[df['date'].dt.date < _now_kst().date()].reset_index(drop=True)
                with self.lock: self.ohlcv_cache[ticker] = {"date": today_str, "df": df}
                return df.copy()
        return pd.DataFrame()

    def _get_extended_ohlcv(self, ticker, current_price):
        base_df = self._get_cached_base_ohlcv(ticker)
        if base_df.empty: return self.toss.get_ohlcv(ticker, "D") if self.toss else pd.DataFrame()
        realtime_data = self.toss.get_realtime_price_data(ticker) if self.toss else None
        if realtime_data:
            today_row = pd.DataFrame([{'date': pd.to_datetime(_now_kst().date()), 'open': realtime_data['open'], 'high': realtime_data['high'], 'low': realtime_data['low'], 'close': realtime_data['close'], 'volume': realtime_data['volume']}])
        else:
            today_row = pd.DataFrame([{'date': pd.to_datetime(_now_kst().date()), 'open': float(current_price), 'high': float(current_price), 'low': float(current_price), 'close': float(current_price), 'volume': 0.0}])
        return pd.concat([base_df, today_row], ignore_index=True)

    def add_log(self, msg):
        t = _now_kst().strftime("%H:%M:%S")
        self.logs.append({"time": t, "message": msg})                              
        print(f"[{t}] {msg}")

    # 노이즈 차단: 자주 오지만 정보가치 낮은 알림 유형은 발송 안 함
    # ('news' 태그는 실제론 보유종목 악재공시/실적 축소 등 매매 알림이라 제외 안 함)
    _MUTED_TG_TYPES = {'ai_pending', 'backtest'}

    def _send_telegram(self, message, msg_type: str = 'misc'):
        if not self.telegram: return
        if msg_type in self._MUTED_TG_TYPES:
            return
        threading.Thread(target=self.telegram.send_message, args=(message,), daemon=True).start()

    def _killswitch_level(self) -> int:
        """일중 고점대비 equity drawdown으로 0/1/2 반환. 1=신규매수중단, 2=전량청산.
        백테스트 기반 안전판 — 라이브 자산(_last_total_equity) 기준."""
        if not getattr(self, 'KILLSWITCH_ENABLED', True):
            return 0
        eq = float(getattr(self, '_last_total_equity', 0) or 0)
        if eq <= 0:
            return 0
        today = _now_kst().strftime('%Y-%m-%d')
        if self._equity_peak_date != today:        # 날짜 바뀌면 일중 고점 리셋
            self._equity_peak_date  = today
            self._equity_peak_today = eq
        if eq > self._equity_peak_today:
            self._equity_peak_today = eq
        peak = self._equity_peak_today or eq
        dd = (peak - eq) / peak if peak > 0 else 0.0
        if dd >= self.KILL_LIQUIDATE_DD:
            return 2
        if dd >= self.KILL_PAUSE_DD:
            return 1
        return 0

    def _killswitch_blocked(self, name: str = '') -> bool:
        """매수 차단 여부(L1 이상 또는 당일 L2 발동). _buy_order에서 호출."""
        today = _now_kst().strftime('%Y-%m-%d')
        if self._trading_halted_date == today:
            self.add_log(f"🛑 [{name}] 매수차단 — killswitch 당일 거래중단(-{self.KILL_LIQUIDATE_DD*100:.0f}% 발동)")
            return True
        if self._killswitch_level() >= 1:
            self.add_log(f"⏸️ [{name}] 매수보류 — killswitch 일중 -{self.KILL_PAUSE_DD*100:.0f}% 도달(신규매수 중단)")
            return True
        return False

    def _killswitch_enforce(self):
        """사이클마다 호출. L1=경고, L2=전포지션 청산+당일 거래중단."""
        if not getattr(self, 'KILLSWITCH_ENABLED', True):
            return
        lvl = self._killswitch_level()
        if lvl == 0:
            return
        today = _now_kst().strftime('%Y-%m-%d')
        eq    = float(getattr(self, '_last_total_equity', 0) or 0)
        peak  = self._equity_peak_today or eq
        dd_pct = (peak - eq) / peak * 100 if peak > 0 else 0
        warn_key = f"{today}-L{lvl}"
        if self._killswitch_last_warn != warn_key:
            self._killswitch_last_warn = warn_key
            try:
                self._send_telegram(
                    f"⚠️ <b>[KR killswitch L{lvl}]</b> 일중 고점대비 -{dd_pct:.1f}%\n"
                    f"현재자산 {eq:,.0f}원 (고점 {peak:,.0f}원)\n"
                    + ("→ 신규매수 중단" if lvl == 1 else "→ <b>전포지션 청산 + 당일 거래중단</b>"),
                    msg_type='misc')
            except Exception:
                pass
        if lvl < 2:
            return
        # ── L2: 전포지션 청산 + 당일 거래중단 ──
        # halted_date는 매수만 차단. 청산은 매 사이클 재시도(매도실패/장마감 대비) — 0주는 자동 skip.
        self._trading_halted_date = today
        try:
            with self.lock:
                _targets = [(c.ticker, c.name, int(getattr(c, 'shares', 0)), c, getattr(c, 'avg_price', 0)) for c in self.core_positions]
                _targets += [(t, getattr(p, 'name', t), int(getattr(p, 'shares', 0)), p, getattr(p, 'avg_price', 0)) for t, p in self.satellite_positions.items()]
            for tk, nm, sh, pos, avg in _targets:
                if sh <= 0:
                    continue
                price = self.live_prices.get(tk, 0) or avg or 0
                if self._sell_order(tk, sh, pos, nm, ai_reason='killswitch L2 전량청산'):
                    try:
                        profit = _net_profit(price, avg, sh) if avg else 0
                        self._log_trade(tk, nm, 'SELL', price, 'killswitch', 'killswitch -20% 전량청산', profit=profit)
                    except Exception:
                        pass
                    with self.lock:
                        pos.shares = 0
        except Exception as e:
            logger.error(f"[{self.mode_name}] killswitch 청산 오류: {e}", exc_info=True)

    def _send_trade_telegram(self, message):

        self._send_telegram(message, msg_type='trade')

    def _send_reject_telegram(self, message):
                             
        self._send_telegram(message, msg_type='reject')

    def _build_portfolio_context(self) -> str:
                                             
        try:
            cash = self.internal_cash or 0
            lines = [f"가용현금: {cash:,.0f}원 | 시장국면: {getattr(self, 'market_regime', 'N/A')}"]
            total_eval = cash
            loss_cnt = profit_cnt = 0
            for cp in self.core_positions:
                price = self.live_prices.get(cp.ticker, 0) or cp.avg_price or 0
                pnl_rt = ((price - cp.avg_price) / cp.avg_price * 100) if cp.avg_price > 0 and cp.shares > 0 else 0
                eval_v = price * cp.shares
                total_eval += eval_v
                tag = "🔴손실" if pnl_rt < 0 else "🟢수익"
                if cp.shares > 0:
                    lines.append(f"  [코어] {cp.name}({cp.ticker}): {cp.shares}주 @ 평단{cp.avg_price:,.0f}원 | 현재{price:,.0f}원 | {pnl_rt:+.1f}% {tag}")
                    if pnl_rt < 0: loss_cnt += 1
                    else: profit_cnt += 1
            for ticker, sp in self.satellite_positions.items():
                price = self.live_prices.get(ticker, 0) or sp.avg_price or 0
                pnl_rt = ((price - sp.avg_price) / sp.avg_price * 100) if sp.avg_price > 0 and sp.shares > 0 else 0
                eval_v = price * sp.shares
                total_eval += eval_v
                tag = "🔴손실" if pnl_rt < 0 else "🟢수익"
                if sp.shares > 0:
                    lines.append(f"  [위성] {sp.name}({ticker}): {sp.shares}주 @ 평단{sp.avg_price:,.0f}원 | 현재{price:,.0f}원 | {pnl_rt:+.1f}% {tag}")
                    if pnl_rt < 0: loss_cnt += 1
                    else: profit_cnt += 1
            lines.append(f"총평가액: {total_eval:,.0f}원 | 손실포지션: {loss_cnt}개 | 수익포지션: {profit_cnt}개")
            return "\n".join(lines)
        except Exception:
            return "포트폴리오 정보 조회 실패"

    def _ai_gate(self, signal: str, ticker: str, name: str, price: float,
                 strategy: str, pos=None, ex_df=None) -> tuple[bool, str, int]:
\
                                                               
        if not self.claude:
            return True, "AI 미설정 — 자동 승인", 100
        action = "매수" if signal == 'BUY' else "매도"
        avg_price = getattr(pos, 'avg_price', 0) or 0
        shares = getattr(pos, 'shares', 0) or 0
        profit_rt = ((price - avg_price) / avg_price * 100) if avg_price > 0 else 0

                                                    
        if ex_df is not None and not ex_df.empty:
            try:
                rich_ctx = self._build_trade_context(ticker, name, price, ex_df, strategy or 'N/A',
                                                     getattr(self, 'market_regime', 'N/A'))
            except Exception:
                rich_ctx = None
        else:
            rich_ctx = None

        context = rich_ctx or (
            f"현재가: {price:,.0f}원"
            + (f" | 평단가: {avg_price:,.0f}원 | 보유: {shares}주 | 수익률: {profit_rt:+.2f}%" if avg_price > 0 else "")
            + f" | 시장국면: {getattr(self, 'market_regime', 'N/A')}"
        )
        portfolio_context = self._build_portfolio_context()
        self._send_telegram(
            f"🤔 <b>AI 심사 중</b>  {self.alert_icon} {self.mode_name}\n"
            f"📌 <b>{name}</b>({ticker})  |  {action}\n"
            f"🔍 전략: {strategy or 'N/A'}",
            'ai_pending'
        )
        self.add_log(f"🤔 AI 심사 중: {name}({ticker}) {action}")
        try:
            result = self.claude.ai_approve_trade(
                signal, name, ticker, price, strategy or 'N/A',
                {}, getattr(self, 'hot_sectors', []),
                get_recent_trades(self.user_id, ticker),
                load_ai_rules(self.user_id) + (f"\n\n[섹터 가이드]\n{self.sector_guide}" if getattr(self, 'sector_guide', '') else ''),
                context=context,
                portfolio_context=portfolio_context
            )
                                                    
            decision, reason = result[0], result[1]
            confidence = result[2] if len(result) > 2 else 75
        except Exception as e:
            logger.warning(f"[{self.mode_name}] AI 게이트 오류 ({ticker}): {e}")
            return True, f"AI 오류 — 자동 승인: {e}", 75

                                                                   
        try:
            import json as _json
            log_ai_decision(
                user_id=self.user_id,
                mode='KR',
                ticker=ticker,
                stock_name=name,
                signal=signal,
                ai_decision='CONFIRM' if decision else 'REJECT',
                confidence=confidence,
                ai_reason=reason[:500],
                input_context=context,
                portfolio_snapshot=portfolio_context[:2000],
                market_regime=getattr(self, 'market_regime', ''),
                strategy=strategy or '',
                price=price,
                session_type='live'
            )
        except Exception as le:
            logger.warning(f"[{self.mode_name}] AI 로그 기록 실패 ({ticker}): {le}")

        conf_tag = f" (확신도 {confidence}%)"
        if decision:
            self._send_telegram(
                f"✅ <b>AI 승인{conf_tag}</b>  {self.alert_icon} {self.mode_name}\n"
                f"📌 <b>{name}</b>({ticker})  |  {action}\n"
                f"🤖 {reason[:120]}",
                'trade'
            )
            self.add_log(f"✅ AI 승인{conf_tag}: {name}({ticker}) {action} — {reason[:80]}")
        else:
            self._send_telegram(
                f"🚫 <b>AI 거절{conf_tag}</b>  {self.alert_icon} {self.mode_name}\n"
                f"📌 <b>{name}</b>({ticker})  |  {action}\n"
                f"🤖 {reason[:120]}",
                'reject'
            )
            self.add_log(f"🚫 AI 거절{conf_tag}: {name}({ticker}) {action} — {reason[:80]}")
        return decision, reason, confidence

    def _buy_order(self, ticker: str, qty: int, pos, name: str, limit_price: int = 0,
                   strategy: str = "", ai_reason: str = "") -> bool:
\
\
                                    
        if not self.toss:
            return False
        if self._killswitch_blocked(name):          # 포트폴리오 killswitch 게이트
            return False
        if self.internal_cash is None:
            self.add_log(f"⏳ [{name}] 매수 보류 — 토스 잔고 초기화 대기 중")
            return False
                                                           
        if not ai_reason and self.claude:
            price_now = self.live_prices.get(ticker, 0) or getattr(pos, 'avg_price', 0) or 0
            approved, ai_reason, confidence = self._ai_gate('BUY', ticker, name, price_now, strategy, pos)
            if not approved:
                return False
                                       
            if confidence < 70 and qty > 1:
                scaled = max(1, qty // 2)
                self.add_log(f"⚠️ AI 확신도 {confidence}% → {qty}주 → {scaled}주로 축소 매수")
                qty = scaled
        if limit_price == 0:
                                                    
            cp = self.live_prices.get(ticker, 0)
            if cp > 0:
                limit_price = int(cp * 1.003)
        elif limit_price == -1:
            limit_price = 0       
        result = self.toss.buy_market_order(ticker, qty, price=limit_price)
        if result:
                                           
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
        err = f"⚠️ [{self.mode_name}] {name}({ticker}) {qty}주 매수 주문 실패 — 토스 API 오류"
        self.add_log(err)
        logger.warning(err)
        with self.lock:
            pos.status = "주문 실패 ❌"
            pos.status_msg = "토스 API 오류 — 서버 로그 확인 필요"
                                                
                                                     
                                                              
            pos.cash = 0.0
        return False

    def _sell_order(self, ticker: str, qty: int, pos, name: str, price: int = 0,
                    strategy: str = "", ai_reason: str = "", profit: float = 0) -> bool:
                                                                     
        if not self.toss:
            return False
        if qty <= 0:
            self.add_log(f"⚠️ SELL 건너뜀: {name}({ticker}) qty={qty} ≤ 0")
            return False
                                                           
        if not ai_reason and self.claude:
            price_now = price or self.live_prices.get(ticker, 0) or getattr(pos, 'avg_price', 0) or 0
            approved, ai_reason, _ = self._ai_gate('SELL', ticker, name, price_now, strategy, pos)
            if not approved:
                return False
        result = self.toss.sell_market_order(ticker, qty, price=price)
        if result:
                                           
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
        err = f"⚠️ [{self.mode_name}] {name}({ticker}) {qty}주 매도 주문 실패 — 토스 API 오류"
        self.add_log(err)
        logger.warning(err)
        with self.lock:
            pos.status = "주문 실패 ❌"
        return False

    def _record_daily_pnl(self, profit: float):
                                         
        if profit == 0:
            return
        today = _now_kst().strftime('%Y-%m-%d')
        with self.lock:
            self.daily_pnl[today] = self.daily_pnl.get(today, 0.0) + profit

                                                                        
                                           
                                                                        

    def _check_news_alerts(self):
\
\
\
\
           
        if not self.news_monitor:
            return

                                             
        if not self._news_check_lock.acquire(blocking=False):
            return
        try:
            self._check_news_alerts_inner()
        finally:
            self._news_check_lock.release()

    def _check_news_alerts_inner(self):
        now_ts = time.time()

                                                               
                                        
        with self.lock:
            dart_due = (now_ts - self._last_dart_check >= self._dart_check_interval)
            if dart_due:
                self._last_dart_check = now_ts
                held_sat = [(t, p.name, p.shares, p.avg_price)
                            for t, p in self.satellite_positions.items() if p.shares > 0]
                held_core = [(c.ticker, c.name, c.shares, c.avg_price)
                             for c in self.core_positions if c.shares > 0]

        if dart_due:
                                                    
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

                                                    
                        if is_core or not self.claude:
                            continue

                                         
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
                                        self._log_trade(ticker, name, 'SELL', price_now, "공시감지", f"악재공시 AI 손절: {report_nm}", profit=profit)            
                                        self._record_daily_pnl(profit)            
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

                                                            
        with self.lock:
            earnings_due = (now_ts - self._last_earnings_check >= self._earnings_check_interval)
            if earnings_due:
                self._last_earnings_check = now_ts
                sat_items = [(t, p.name, p.shares, p.avg_price)
                             for t, p in self.satellite_positions.items() if p.shares > 0]

        if earnings_due:
            for ticker, name, shares, avg_price in sat_items:
                try:
                    time.sleep(0.5)                       
                    earnings = self.news_monitor.get_upcoming_earnings(ticker)
                    if not earnings:
                        continue
                    days_until = earnings['days_until']
                    exp_date   = earnings['expected_date']

                                                       
                    if self._earnings_notified.get(ticker) == exp_date:
                        continue

                    if days_until <= 7 and shares > 1:
                        reduce_qty = max(1, int(shares * 0.30))
                        pos = self.satellite_positions.get(ticker)
                        if pos and pos.shares > 0:
                            if self._sell_order(ticker, reduce_qty, pos, name):
                                with self.lock:
                                    price_now = self.live_prices.get(ticker) or avg_price                  
                                    pos.shares = max(0, pos.shares - reduce_qty)
                                    pos.status = "실적전 축소 📊"
                                    self._earnings_notified[ticker] = exp_date                   
                                profit = _net_profit(price_now, avg_price, reduce_qty)
                                self._log_trade(ticker, name, 'SELL', price_now, "실적공시대응", f"실적발표 D-{days_until} 30% 축소", profit=profit)
                                with self.lock:
                                    self.pnl_this_turn += profit            
                                self._record_daily_pnl(profit)              
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

    _MAX_DAILY_LOSS_PER_TICKER = -5_000                        

    @staticmethod
    def _sat_exit_reset(pos) -> None:
                                                               
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
        pos.cash               = 0.0                                          

    def _refresh_blacklist(self):
                                                              
        today = _now_kst().strftime('%Y-%m-%d')
        if self._bl_date != today:
            self._bl_date              = today
            self._satellite_rejects    = {}
            self._satellite_reject_rsn = {}
            self._daily_loss_by_ticker = {}
            self._notified_disclosures = set()                        

    def _add_satellite_reject(self, ticker: str, reason: str):
                                                    
        with self.lock:
            self._refresh_blacklist()
            self._satellite_rejects[ticker]    = time.time()             
            self._satellite_reject_rsn[ticker] = reason
        try:
            self._save_state()
        except Exception:
            pass

    def _record_ticker_loss(self, ticker: str, profit: float):
                                          
        if profit >= 0:
            return
        with self.lock:
            self._refresh_blacklist()
            self._daily_loss_by_ticker[ticker] = (
                self._daily_loss_by_ticker.get(ticker, 0) + profit
            )

    def _is_satellite_blacklisted(self, ticker: str) -> bool:
                                  
        user_tickers = {s['ticker'] for s in self.user_satellite_stocks if s.get('ticker')}
        if ticker in user_tickers:
            return False
        with self.lock:
                                                                      
            ts = self._satellite_rejects.get(ticker)
            if ts is None:
                return False
            if time.time() - ts < self._SAT_REJECT_COOLDOWN:
                return True          
                             
            del self._satellite_rejects[ticker]
            self._satellite_reject_rsn.pop(ticker, None)
            return False

    def _fmt_scan_report(self, theme: str, candidates: list, regime: str, action_note: str) -> str:
\
\
           
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

    def reload_api_keys(self, toss_config, telegram_config, gemini_config, core_stocks):
        self.cached_balance = None
        try: self.user_core_stocks = json.loads(core_stocks) if core_stocks else []
        except Exception: self.user_core_stocks = []
        _u = self.user_core_stocks[0] if self.user_core_stocks else None
        self.core_ticker = _u['ticker'] if _u else ""
        self.core_name   = _u['name']   if _u else ""

        self._init_api(toss_config)

        if telegram_config and telegram_config.get('token'):
            self.telegram = TelegramNotifier(token=telegram_config.get('token', '').strip(), chat_id=telegram_config.get('chat_id', '').strip())
        else: self.telegram = None
        self._init_dummy_cores()
        self._save_state()                               
        self.add_log(f"🔑 [{self.mode_name}] API 키 및 계좌 설정이 시스템에 반영되었습니다.")

    def update_mode(self, is_mock, total_cash=10000000):
        pass

    def _ai_filter_satellites(self, candidates: list) -> list:
                                                             
        if not self.claude or not candidates:
            return candidates
        try:
                                 
            preview = ', '.join([f"{c['name']}({c['ticker']})" for c in candidates[:5]])
            if len(candidates) > 5:
                preview += f" 외 {len(candidates)-5}개"
            self.add_log(f"🤖 AI가 위성 후보 {len(candidates)}개 종목·전략 검토 중...")
            if self.telegram:
                self.telegram.send_message(
                    f"🤔 <b>위성 후보 AI 심사 시작</b>  {self.alert_icon} {self.mode_name}\n"
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
        raw_info, _new_hot = select_satellites(toss=self.toss, n=self.num_satellites * 2, verbose=False, claude_client=self.claude, sector_guide=self.sector_guide)
        if _new_hot:
            self.hot_sectors = _new_hot
        if self.hot_sectors:
            self.add_log(
                f"🔥 전 섹터 스캔 완료 (총 {len(self.hot_sectors)}개) — "
                f"가산점 TOP4: {', '.join(self.hot_sectors[:4])}"
            )
        else:
            self.add_log("⚠️ 전 섹터 스캔 완료 — 강세 섹터 없음 (상대 강세 기준 후보 선정)")
                                                  
        filtered_info = self._ai_filter_satellites(raw_info)
        _now_str = _now_kst().strftime('%Y-%m-%d %H:%M')
        for _c in filtered_info:
            _c.setdefault('screened_at', _now_str)
        self.satellite_info = filtered_info[:self.num_satellites]
        self._inject_user_satellites()                   
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

                                                  
        self.core_positions = []
        user_tickers: set = set()

        for user_pick in self.user_core_stocks[:2]:
            if user_pick.get('ticker') and user_pick['ticker'] not in user_tickers:
                self.core_positions.append(CorePosition(user_pick['ticker'], user_pick['name'], initial_cash=0))
                user_tickers.add(user_pick['ticker'])

                     
        n_cores = max(1, len(self.core_positions))
        per_core_budget = core_budget / n_cores
        for core in self.core_positions:
            core.initial_cash = per_core_budget
            core.cash = per_core_budget

                            
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
        
        if self.toss:
            real_balance = self.toss.get_account_balance()
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
                "satellites": {ticker: {"name": pos.name, "shares": int(pos.shares), "cash": float(pos.cash), "initial_cash": float(pos.initial_cash), "avg_price": float(pos.avg_price), "partial_sold": bool(getattr(pos, 'partial_sold', False)), "partial_sold_2": bool(getattr(pos, 'partial_sold_2', False)), "second_buy_done": bool(getattr(pos, 'second_buy_done', False)), "pyramid_done": bool(getattr(pos, 'pyramid_done', False)), "second_buy_price": float(getattr(pos, 'second_buy_price', 0)), "second_buy_cash": float(getattr(pos, 'second_buy_cash', 0)), "max_price": float(getattr(pos, 'max_price', 0)), "last_order_time": float(getattr(pos, 'last_order_time', 0.0)), "stop_news_checked": bool(getattr(pos, 'stop_news_checked', False)), "swing_acc_count": int(getattr(pos, 'swing_acc_count', 0)), "overext_sell_count": int(getattr(pos, 'overext_sell_count', 0)), "bt_stop_pct": float(getattr(pos, 'bt_stop_pct', 0)), "bt_target_pct": float(getattr(pos, 'bt_target_pct', 0)), "bt_hold_days": float(getattr(pos, 'bt_hold_days', 0))} for ticker, pos in self.satellite_positions.items()},
                "satellite_info": self.satellite_info, "hot_sectors": self.hot_sectors, "num_satellites": self.num_satellites,
                "last_screen_month": getattr(self, 'last_screen_month', None), "last_screen_date": self.last_screen_date.strftime('%Y-%m-%d') if getattr(self, 'last_screen_date', None) else None,
                "last_core_rebalance_date": self.last_core_rebalance_date.strftime('%Y-%m-%d') if getattr(self, 'last_core_rebalance_date', None) else None,
                "daily_pnl": self.daily_pnl, "daily_report": self.daily_report,
                                                              
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

                                                                                  
                                                                  
            saved_core_map = {c["ticker"]: c for c in state.get("cores", [])}
                                                                    
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
                             
            while len(self.core_positions) < 2:
                ph = CorePosition("TBD", f"AI선정대기#{len(self.core_positions)+1}", initial_cash=0)
                ph.status = "AI 선정 대기 🤖"
                self.core_positions.append(ph)
            _user_sat_tickers = {s['ticker'] for s in self.user_satellite_stocks if s.get('ticker')}
            self.satellite_positions = {}
            for ticker, s in state["satellites"].items():
                                                                
                                                                     
                if not (ticker.isdigit() and len(ticker) == 6):
                    logger.warning(f"[KR봇] 상태 복구 중 비KR 티커 무시: {ticker} (US 봇 종목 혼입 방지)")
                    continue
                                                                      
                                                
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
                pos.max_price          = float(s.get("max_price",          0))                        
                pos.last_order_time    = float(s.get("last_order_time",   0.0))
                pos.stop_news_checked  = bool(s.get("stop_news_checked",  False))
                pos.swing_acc_count    = int(s.get("swing_acc_count",     0))
                pos.overext_sell_count = int(s.get("overext_sell_count",  0))
                pos.bt_stop_pct        = float(s.get("bt_stop_pct",   0))   # D 청산정렬 영속
                pos.bt_target_pct      = float(s.get("bt_target_pct", 0))
                pos.bt_hold_days       = float(s.get("bt_hold_days",  0))
                self.satellite_positions[ticker] = pos

                                                
                                                     
            _restored_sat_tickers = set(self.satellite_positions.keys())
            self.satellite_info = [c for c in state.get("satellite_info", [])
                                   if c.get('ticker','').isdigit() and len(c.get('ticker','')) == 6
                                   and (c.get('ticker') in _restored_sat_tickers
                                        or c.get('ticker') in _user_sat_tickers)]
            self.hot_sectors = state.get("hot_sectors", [])
            self.num_satellites = min(3, state.get("num_satellites", 3))            
            self.last_screen_month = state.get("last_screen_month")
            lsd_str = state.get("last_screen_date")
            self.last_screen_date = datetime.strptime(lsd_str, '%Y-%m-%d').date() if lsd_str else None
            lcr_str = state.get("last_core_rebalance_date")
            self.last_core_rebalance_date = datetime.strptime(lcr_str, '%Y-%m-%d').date() if lcr_str else None
            self.daily_pnl = state.get("daily_pnl", {})
            self.daily_report = state.get("daily_report", None)
                                                            
            saved_bl_date = state.get("bl_date", "")
            today_str     = _now_kst().strftime('%Y-%m-%d')
            if saved_bl_date == today_str:
                self._bl_date             = saved_bl_date
                self._satellite_rejects   = state.get("satellite_rejects",   {})
                n_rej = len(self._satellite_rejects)
                if n_rej:
                    self.add_log(f"🚫 당일 AI 거절 블랙리스트 복원: 위성 {n_rej}개 재심사 제외")
                         
            self._monday_swap_plan  = state.get("monday_swap_plan", {})
            self._weekend_scan_done = state.get("weekend_scan_done", "")
            if self._monday_swap_plan:
                self.add_log(f"📅 주말 교체 계획 복원: {len(self._monday_swap_plan)}건 대기 중")

                                                                 
                                                      
                                                  
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
\
\
\
\
           
        if not self.toss:
            return self.market_regime
        if time.time() - self.last_regime_check < self._regime_check_interval:
            return self.market_regime
        try:
            prev   = self.market_regime
            detail = get_market_regime_detail(self.toss)
            self.last_regime_check = time.time()

                                                                    
            ewy_change = nq_change = usd_krw_change = 0.0
            try:
                import yfinance as yf
                for sym, attr in [("EWY","ewy_change"),("NQ=F","nq_change")]:
                    df = yf.download(sym, period="3d", interval="1d", progress=False, auto_adjust=True)
                    if not df.empty and len(df) >= 2:
                        c0, c1 = float(df["Close"].iloc[-2]), float(df["Close"].iloc[-1])
                        if attr == "ewy_change": ewy_change = (c1/c0-1)*100
                        else: nq_change = (c1/c0-1)*100
                                                     
                df_uup = yf.download("UUP", period="3d", interval="1d", progress=False, auto_adjust=True)
                if not df_uup.empty and len(df_uup) >= 2:
                    c0, c1 = float(df_uup["Close"].iloc[-2]), float(df_uup["Close"].iloc[-1])
                    usd_krw_change = (c1/c0-1)*100
            except Exception as fx_err:
                logger.debug(f"[{self.mode_name}] 외부 신호 수집 실패: {fx_err}")

                                                                    
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
                                   
                    self.market_regime = ai_result['regime']
                                                  
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

                                                                  
                                                       
                                                          
            _proposed = self.market_regime
            if _proposed == "BULL" and prev != "BULL":
                self._bull_pending_days += 1
                if self._bull_pending_days < 2:
                    self.market_regime = prev                       
                    self.add_log(
                        f"⏳ [{self.mode_name}] BULL 확인 대기 {self._bull_pending_days}/2회 — "
                        f"{prev} 유지 (단기 급등 필터)"
                    )
                else:
                    self._bull_pending_days = 0                       
            elif _proposed != "BULL":
                self._bull_pending_days = 0                                

                                                              
            adx_str    = f"ADX={detail['adx']:.1f}"
            streak_str = f"연속상승{detail['up_streak']}일"
            rsi_str    = f"RSI={detail['rsi']:.1f}"
            score_str  = f"점수{detail['score']:+d}"
            diag_line  = f"{score_str} | {rsi_str} | {adx_str} | {streak_str} | 22일수익{detail['ret22']:+.1f}%"

            if detail['downgrade_reason']:
                self.add_log(f"⚠️ [{self.mode_name}] {detail['downgrade_reason']} | {diag_line}")

            if self.market_regime != prev:
                                                   
                if self.market_regime == "BEAR" and self.toss:
                    with self.lock:
                        bear_sat_items = [(t, p) for t, p in self.satellite_positions.items() if p.shares > 0]
                    for _bt, _bp in bear_sat_items:
                        try:
                            _bprice = self.live_prices.get(_bt) or self.toss.get_current_price(_bt) or 0
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
\
\
\
\
\
           
        if not self.toss:
            return
        if time.time() - self._last_defensive_check < 300:         
            return
        self._last_defensive_check = time.time()
        try:
            balance = self.toss.get_account_balance()
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
                cd_key     = f"_def_sold_{ticker}"              

                holding    = next((s for s in stocks if s.get('ticker') == ticker), None)
                has_pos    = holding and int(holding.get('shares', 0)) > 0
                shares_held = int(holding.get('shares', 0)) if holding else 0

                if regime == "BEAR" and not has_pos:
                                               
                    sold_ts = self._defensive_sold_ts.get(ticker, 0.0)
                    cooldown_remaining = 86400 - (time.time() - sold_ts)
                    if sold_ts > 0 and cooldown_remaining > 0:
                        self.add_log(f"⏳ {name} 재매수 쿨다운 중 ({cooldown_remaining/3600:.1f}h 남음) — 휩쏘 방지")
                        continue

                                                                             
                                                                 
                    if self._trading_halted_date == _now_kst().strftime('%Y-%m-%d'):
                        self.add_log(f"🛑 방어매수 차단 — killswitch 당일 거래중단(-20% 발동): {name}")
                        continue
                    budget = int(total_assets * ratio)
                    price  = self.toss.get_current_price(ticker)
                    if price and price > 0:
                        qty = int(budget // price)
                        if qty > 0 and total_cash >= qty * price * 1.002:
                            if self.toss.buy_market_order(ticker, qty):
                                total_cash -= qty * price                     
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
                    if self.toss.sell_market_order(ticker, shares_held):                    
                        self._defensive_sold_ts[ticker] = time.time()                  
                        price = self.toss.get_current_price(ticker) or 0
                        def_profit = _net_profit(price, float(holding.get('purchase_price', price)), shares_held) if holding else 0
                                                    
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
                                                                      
        if not self.toss:
            return True
                                   
        if getattr(self, '_is_mock', False):
            return True
        try:
            threshold = -1.0
            for etf_code, _ in self.market_indices:
                info = self.toss.get_etf_price(etf_code)
                if info and info.get("prdy_ctrt", 0) < threshold:
                    return False
            return True
        except Exception:
            return True                      

    def _build_trade_context(self, ticker: str, stock_name: str, price: float,
                              ex_df: 'pd.DataFrame', strategy: str, regime: str) -> str:
                                                           
        lines = []

                                                                 
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
                if "조회 실패" in news:
                    news = ""
            except Exception:
                news = ""
            if news:
                lines.append(f"[최근 뉴스] {news}")

                                                               
        fundamental = self._fetch_fundamental(ticker, stock_name)
        if fundamental:
            lines.append(f"[재무지표] {fundamental}")

                                                           
        if ex_df is not None and not ex_df.empty and 'close' in ex_df.columns:
            from KR.strategy import calc_rsi
            close = ex_df['close'].dropna()
            vol   = ex_df['volume'].dropna() if 'volume' in ex_df.columns else pd.Series(dtype=float)

                     
            rsi_val = None
            if len(close) >= 16:
                try:
                    rsi_val = round(float(calc_rsi(close, 14).iloc[-1]), 1)
                except Exception:
                    pass

                            
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

                                   
            vol_str = "N/A"
            if len(vol) >= 2:
                try:
                    vol_avg20 = float(vol.iloc[:-1].rolling(20, min_periods=5).mean().iloc[-1])
                    vol_today = float(vol.iloc[-1])
                    vol_ratio = vol_today / (vol_avg20 + 1) * 100
                    vol_str = f"평소 대비 {vol_ratio:.0f}% ({'급증↑↑' if vol_ratio > 200 else '증가↑' if vol_ratio > 130 else '정상✅' if vol_ratio >= 100 else '보통' if vol_ratio > 70 else '감소↓'})"
                except Exception:
                    pass

                         
            price_hist = ""
            if len(close) >= 5:
                try:
                    last5 = close.tail(5).tolist()
                    price_hist = " → ".join(f"{int(p):,}" for p in last5) + "원"
                except Exception:
                    pass

                            
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

                    
            sma5_str = "N/A"
            if len(close) >= 6:
                try:
                    sma5    = float(close.rolling(5).mean().iloc[-1])
                    rel_sma5 = (price / sma5 - 1) * 100
                    sma5_str = f"{sma5:,.0f}원 ({rel_sma5:+.1f}% {'위↑' if rel_sma5 >= 0 else '아래↓'})"
                except Exception:
                    pass

                     
            sma20_str = "N/A"
            if len(close) >= 22:
                try:
                    sma20    = float(close.rolling(20).mean().iloc[-1])
                    rel_sma20 = (price / sma20 - 1) * 100
                    sma20_str = f"{sma20:,.0f}원 ({rel_sma20:+.1f}% {'위↑' if rel_sma20 >= 0 else '아래↓'})"
                except Exception:
                    pass

                      
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

                                                               
        try:
            if self.toss:
                candles = self.toss.get_minute_candles(ticker, count=5)
                if candles and len(candles) >= 3:
                    c_prices = [c["close"] for c in candles if c["close"] > 0]
                    if c_prices:
                        trend = "상승 추세 ↑" if c_prices[-1] > c_prices[0] else "하락 추세 ↓"
                        lines.append(f"[분봉 추세] 최근 5분봉: {trend} (시작 {c_prices[0]:,} → 현재 {c_prices[-1]:,})")
        except Exception:
            pass

                                                        
        frgn_inst_str = "N/A"
        try:
            if self.toss and hasattr(self.toss, 'get_foreign_buy_by_ticker'):
                fi = self.toss.get_foreign_buy_by_ticker(ticker)
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

                                                                
        market_rs_str = "N/A"
        try:
            if self.toss and ex_df is not None and not ex_df.empty and 'close' in ex_df.columns:
                close_s = ex_df['close'].dropna()
                if len(close_s) >= 2:
                    stock_chg = (float(price) / float(close_s.iloc[-2]) - 1) * 100
                    parts = []
                    for etf_code, idx_name in [("069500", "KOSPI"), ("229200", "KOSDAQ")]:
                        try:
                            _etf = self.toss.get_etf_price(etf_code)
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

                                                             
        try:
            from base.market_phase import get_phase_for_date, build_phase_context_str
            from base.database import get_phase_strategy_stats
            today_str = _now_kst().strftime('%Y-%m-%d')
            phase_info = get_phase_for_date('KR', today_str)
            phase_ctx  = build_phase_context_str(phase_info)
            if phase_ctx:
                lines.append(phase_ctx)
            ph = phase_info.get('phase', '')
            if ph:
                ph_stats = get_phase_strategy_stats('KR', ph)
                if ph_stats:
                    top3 = ph_stats[:3]
                    stat_str = ' | '.join(
                        f"{s['signal_type']} 승률{s['win_rate']}%(n={s['total']})" for s in top3
                    )
                    lines.append(f"[이 국면 역대 승률 TOP] {stat_str}")
        except Exception:
            lines.append(f"[시장 국면] {regime} | 적용 전략: {strategy}")

        if self.hot_sectors:
            lines.append(f"[강세 섹터] {', '.join(self.hot_sectors[:5])}")

        return "\n".join(lines)

    def _build_indicators_dict(self, ticker: str, price: float,
                                ex_df: 'pd.DataFrame', signal_types: list = None,
                                sector: str = '기타') -> dict:
        """파인튜닝 형식 판단에 필요한 지표 dict 구성."""
        # 백테스트와 동일한 신호 라벨을 단일기준(base.signals)으로 생성 → 용어 통일
        bt_signals = []
        try:
            from base.signals import detect_latest_signals
            bt_signals = detect_latest_signals(ex_df)
        except Exception:
            pass
        # 백테스트 크로스 신호 우선, 없으면 호출부가 넘긴 라벨(CORE_BUY 등) 폴백
        ind = {'sector': sector, 'signal_types': bt_signals or (signal_types or [])}
        if ex_df is None or ex_df.empty or 'close' not in ex_df.columns:
            return ind
        try:
            from KR.strategy import calc_rsi
            close = ex_df['close'].dropna()
            vol   = ex_df['volume'].dropna() if 'volume' in ex_df.columns else pd.Series(dtype=float)

            if len(close) >= 16:
                ind['rsi'] = round(float(calc_rsi(close, 14).iloc[-1]), 2)
            if len(close) >= 30:
                ema12 = close.ewm(span=12, adjust=False).mean()
                ema26 = close.ewm(span=26, adjust=False).mean()
                macd_line   = ema12 - ema26
                signal_line = macd_line.ewm(span=9, adjust=False).mean()
                ind['macd']        = round(float(macd_line.iloc[-1]), 6)
                ind['macd_signal'] = round(float(signal_line.iloc[-1]), 6)
            if len(close) >= 22:
                sma20 = float(close.rolling(20).mean().iloc[-1])
                std20 = float(close.rolling(20).std().iloc[-1])
                ind['bb_upper'] = round(sma20 + 2 * std20, 2)
                ind['bb_mid']   = round(sma20, 2)
                ind['bb_lower'] = round(sma20 - 2 * std20, 2)
                ind['sma20']    = round(sma20, 2)
            if len(close) >= 6:
                ind['sma5'] = round(float(close.rolling(5).mean().iloc[-1]), 2)
            if len(close) >= 60:
                ind['sma60']  = round(float(close.rolling(60).mean().iloc[-1]), 2)
            if len(close) >= 120:
                ind['sma120'] = round(float(close.rolling(120).mean().iloc[-1]), 2)
            if len(vol) >= 21:
                vol_avg20 = float(vol.iloc[:-1].rolling(20, min_periods=5).mean().iloc[-1])
                vol_today = float(vol.iloc[-1])
                ind['vol_ratio'] = round(vol_today / (vol_avg20 + 1) * 100, 1)

            # 뉴스/공시
            if self.news_monitor:
                try:
                    news_parts = []
                    naver = self.news_monitor.get_news_summary(ticker, display=3)
                    dart  = self.news_monitor.get_disclosure_summary(ticker, days=5)
                    if naver: news_parts.append(naver)
                    if dart:  news_parts.append(dart)
                    ind['news_summary'] = '\n'.join(news_parts)
                except Exception:
                    pass

            # 오답노트: 최근 매매 이력
            try:
                ind['recent_trades'] = get_recent_trades(self.user_id, ticker, limit=5)
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"[{self.mode_name}] indicators_dict 오류 ({ticker}): {e}")
        return ind

    def _forecast_block(self, name: str, ticker: str, price: float, ex_df) -> str:
        """매수 시 백테스트 통계 기반 예측 4줄(예상 보유/목표/손절). 없으면 빈 문자열."""
        try:
            from base.signals import detect_latest_signals
            from base.signal_forecast import get_forecast, format_forecast_msg
            sigs = detect_latest_signals(ex_df)
            if not sigs:
                return ''
            mkt = self._build_market_info_dict()
            phase = mkt.get('market_phase')
            fc = get_forecast('KR', phase, sigs)
            if not fc:
                return ''
            return '\n' + format_forecast_msg(name, ticker, price,
                                              mkt.get('market_phase_kr') or phase, sigs, fc)
        except Exception:
            return ''

    def _capture_bt_levels(self, pos, ex_df):
        """매수 직후 백테스트 통계 손절/익절/보유선을 포지션에 저장 → 청산이 '표시값=실제'로 동작.
        실패해도 무해(기존 ATR/RSI 청산이 계속 작동)."""
        try:
            from base.signals import detect_latest_signals
            from base.signal_forecast import get_forecast
            sigs = [s for s in detect_latest_signals(ex_df) if 'BUY' in s]
            if not sigs:
                return
            phase = self._build_market_info_dict().get('market_phase')
            fc = get_forecast('KR', phase, sigs)
            if not fc:
                return
            with self.lock:
                pos.bt_stop_pct   = float(fc['stop'])      # 평균 max_drawdown (음수 %)
                pos.bt_target_pct = float(fc['target'])    # 평균 max_gain (%)
                pos.bt_hold_days  = float(fc['hold_days'])
        except Exception:
            pass

    def _build_market_info_dict(self) -> dict:
        """파인튜닝 형식 판단에 필요한 시장 국면/매크로 dict — 일별 캐시."""
        today_str = _now_kst().strftime('%Y-%m-%d')
        cache_key = f'_mkt_cache_{today_str}'
        cached = getattr(self, cache_key, None)
        if cached:
            return cached
        mkt = {}
        try:
            from base.market_phase import get_phase_for_date
            from base.macro_collector import get_macro_for_date, build_macro_context_str
            phase_info = get_phase_for_date('KR', today_str)
            mkt['market_phase']    = phase_info.get('phase')
            mkt['market_phase_kr'] = phase_info.get('phase_kr')
            mkt['phase_confidence']= phase_info.get('confidence')
            macro = get_macro_for_date(today_str)
            mkt['macro_str'] = build_macro_context_str(macro)
        except Exception:
            pass
        setattr(self, cache_key, mkt)
        return mkt

    def _fetch_fundamental(self, ticker: str, stock_name: str) -> str:
\
\
           
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
                                                
        if not self.toss:
            return True
        try:
            candles = self.toss.get_minute_candles(ticker, count=5)
            if len(candles) < 3:
                return True                    
            closes = [c["close"] for c in candles if c["close"] > 0]
            if len(closes) < 3:
                return True
                                            
            return closes[-1] >= closes[0]
        except Exception:
            return True

    def trading_job(self):
                                                                   
                                                            
                                                   
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
        now = _now_kst()                                   
        if now.weekday() >= 5: return
        current_time_str = now.strftime('%H:%M')
        today_str        = now.strftime('%Y-%m-%d')
                                                                     
                                                     
        _is_pause = ("15:15" <= current_time_str < "16:00")
        is_golden_hours = ("09:01" <= current_time_str <= "20:00") and not _is_pause

                                                         
                                                
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
            return                                           
        else:
            self.add_log(f"--- 🎯 {self.mode_name} 실시간 점검 ({current_time_str}) ---")

                                                             
            _ai_portfolio_key = f"_ai_portfolio_done_{today_str}_{current_time_str[:2]}"
            if self.claude and not getattr(self, _ai_portfolio_key, False):
                setattr(self, _ai_portfolio_key, True)
                def _run_portfolio_decision():
                    try:
                        market_ctx = (
                            f"시장국면: {getattr(self, 'market_regime', 'N/A')}\n"
                            f"강세섹터: {', '.join(getattr(self, 'hot_sectors', [])[:5]) or '없음'}\n"
                            + getattr(self, 'current_ai_market_view', '')
                        )
                        port_ctx = self._build_portfolio_context()
                        positions_detail = "\n".join(
                            [f"{cp.name}({cp.ticker}): 코어 {cp.shares}주 @ {cp.avg_price:,.0f}원"
                             for cp in self.core_positions if cp.shares > 0]
                            + [f"{sp.name}({tk}): 위성 {sp.shares}주 @ {sp.avg_price:,.0f}원"
                               for tk, sp in self.satellite_positions.items() if sp.shares > 0]
                        ) or "보유 포지션 없음"
                        result = self.claude.ai_portfolio_decision(port_ctx, market_ctx, positions_detail, 'KR')
                        with self.lock:
                            self._ai_portfolio_guidance = result
                        self.add_log(
                            f"🧠 AI 포트폴리오 판단: {result['overall_stance']} | "
                            f"국면: {result['regime']} | 현금목표: {result['cash_target_pct']}% | "
                            f"{result['notes'][:60]}"
                        )
                                                
                        if result['regime'] in ('BULL', 'BEAR', 'NEUTRAL'):
                            with self.lock:
                                self.market_regime = result['regime']
                    except Exception as e:
                        logger.warning(f"[{self.mode_name}] AI 포트폴리오 판단 오류: {e}")
                self._run_threaded(_run_portfolio_decision)

            with self.lock:
                _regime_now = getattr(self, 'market_regime', 'NEUTRAL')
                _regime_label = {"BULL": "상승장 🚀", "BEAR": "하락장 🐻", "NEUTRAL": "횡보장 ➡️"}.get(_regime_now, "분석 중")
                for core in self.core_positions:
                    if "대기" not in core.status and "심사" not in core.status:
                        if core.shares > 0:
                            _cp = getattr(core, 'toss_current_price', 0) or self.live_prices.get(core.ticker, 0)
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
                            _sp = getattr(sat, 'toss_current_price', 0) or self.live_prices.get(sat.ticker, 0)
                            _pnl = ((_sp - sat.avg_price) / sat.avg_price * 100) if sat.avg_price > 0 and _sp > 0 else 0
                            sat.status = "보유 중 ✅"
                            sat.status_msg = f"{sat.shares}주 보유 중 | 평단 {sat.avg_price:,.0f}원 | 수익률 {_pnl:+.1f}% | {_regime_label}"
                        elif sat.cash > 0:
                            sat.status = "감시 중 👀"
                            sat.status_msg = f"신호 대기 | 예산 {sat.cash:,.0f}원 | 시장: {_regime_label}"
                        else:
                            sat.status = "감시 중 👀"
                            sat.status_msg = f"예산 소진 — 다음 종목 교체 대기 | 시장: {_regime_label}"

                                                         
        if self.news_monitor and is_golden_hours:
            self._run_threaded(self._check_news_alerts)

                                                 
                                                
                                               
        if getattr(self, 'is_crisis_mode', False):
            if is_golden_hours and self.toss:
                main_idx_ticker = self.market_indices[0][0]
                idx_cp = self.toss.get_current_price(main_idx_ticker)
                if idx_cp:
                    extended_df = self._get_extended_ohlcv(main_idx_ticker, idx_cp)
                    if not extended_df.empty and len(extended_df) >= 5:
                        if idx_cp > extended_df['close'].ewm(span=5, adjust=False).mean().iloc[-1]:
                            msg = f"🚀 {self.mode_name} 저점 반등 확인! 관망 모드 해제."
                            self.add_log(msg); self._send_telegram(msg)
                            self.is_crisis_mode = False; self.peak_total_asset = 0
            if getattr(self, 'is_crisis_mode', False):                  
                return

                                                       
        regime = self._update_market_regime()
        if is_golden_hours:
            self._handle_defensive_assets(regime)

        if self.toss:
            try:
                real_balance = self.toss.get_account_balance()
                if real_balance and 'stocks' in real_balance:
                    self._sync_internal_balances(real_balance)
                    current_total_asset = float(real_balance.get('total_cash', 0)) + float(real_balance.get('total_value', 0))
                    if not hasattr(self, 'peak_total_asset'): self.peak_total_asset = current_total_asset
                    elif current_total_asset > self.peak_total_asset: self.peak_total_asset = current_total_asset
                        
                    if getattr(self, 'peak_total_asset', 0) > 0 and ((current_total_asset / self.peak_total_asset) - 1) * 100 <= -10.0:
                        msg = f"💥 [서킷브레이커] {self.mode_name} 계좌 MDD 10% 폭락! 전량 시장가 강제 청산."
                        self.add_log(msg); self._send_telegram(msg)
                                                                                  
                                                                                   
                        self.is_crisis_mode = True
                        with self.lock:
                            safe_core_positions = list(self.core_positions)
                            safe_satellite_items = list(self.satellite_positions.items())
                        for core in safe_core_positions:
                            if core.shares > 0:
                                self.toss.sell_market_order(core.ticker, core.shares)
                                with self.lock:
                                    core.shares = 0                                   
                                self.add_log(f"🔥 {self.mode_name} 코어 {core.name} 청산")
                        for ticker, pos in safe_satellite_items:
                            if pos.shares > 0:
                                self.toss.sell_market_order(ticker, pos.shares)
                                with self.lock:
                                    self._sat_exit_reset(pos)                 
                                self.add_log(f"🔥 {self.mode_name} 위성 {pos.name} 청산")
                        return
            except Exception as e:
                logger.error(f"[{self.mode_name}] 서킷브레이커 잔고 조회 오류: {e}", exc_info=True)

                                                               
                                                   
                                                   
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
                                                                       

        with self.lock: safe_core_positions = list(self.core_positions)
        for core in safe_core_positions:
            if core.ticker == "TBD":                      
                continue
            cp = self.live_prices.get(core.ticker) or getattr(core, 'toss_current_price', 0) or (self.toss.get_current_price(core.ticker) if self.toss else 0)
            if not cp or cp <= 0: continue
            with self.lock: core._last_price = cp; c_sh = core.shares; c_fl = core.floor_shares; c_avg = core.avg_price; c_cash = core.cash; c_nm = core.name; c_tk = core.ticker
            try:
                from KR.strategy import get_rsi_signal
                ex_df = self._get_extended_ohlcv(c_tk, cp)
                c_sig, _, c_rsi = get_rsi_signal(c_tk, toss_api=self.toss, df=ex_df)

                                                                      
                                                                            
                                                                       
                                                            
                if c_sig != 'BUY' and regime == "BULL" and c_sh == 0:
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
                                _bull_cond_b = (_ma5_b > _ma20_b) and (cp <= _ma5_b * 1.02)
                            if _bull_cond_a or _bull_cond_b:
                                c_sig = 'BUY'
                                c_rsi = _rsi_bull
                                _bull_why = (f"RSI={_rsi_bull:.1f} bull_score={_bull_sc}" if _bull_cond_a
                                             else f"MA5눌림목(MA5={_closes_b.rolling(5).mean().iloc[-1]:,.0f})")
                                self.add_log(f"🚀 [BULL 코어 진입] {c_tk} {_bull_why} → BUY 오버라이드")
                    except Exception as _be:
                        logger.debug(f"BULL 코어 오버라이드 오류: {_be}")
                                                                               

                                                                   
                                                                
                                                      
                if c_sig != 'SELL' and regime == "BEAR" and c_sh > 0 and c_avg > 0:
                    if c_rsi >= 60:
                        c_sig = 'SELL'
                        self.add_log(f"🐻 [BEAR 코어 조기익절] {c_tk} RSI={c_rsi:.1f} ≥ 60 → SELL 오버라이드")
                                                                               

                                                                    
                                                                  
                                                                 
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
                                                                               

                                                                
                                                         
                                                      
                with self.lock: c_cash = core.cash             
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

                                                                   
                if c_sh > 0 and c_avg > 0 and is_core_cd:
                    c_pnl_pct = (cp / c_avg - 1) * 100
                    c_decision = getattr(core, 'ai_exit_decision', None)
                                                             
                    _core_partial1 = 15.0 if regime == "BULL" else 10.0
                    _core_partial2 = 30.0 if regime == "BULL" else 20.0

                                                                 
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

                                                                                
                with self.lock: c_cash = core.cash

                                                                
                                                     
                                                          
                                                                   
                _dca_bought_this_turn = False
                if getattr(core, 'dca_mode', False) and c_cash >= cp and is_core_cd:
                    _now_ts  = time.time()
                    _elapsed = _now_ts - getattr(core, 'last_dca_time', 0.0)
                    _dca_dip = getattr(core, 'dca_dip_pct', 3.0)

                    _do_dca, _dca_reason, _dca_budget = False, "", 0.0

                                     
                    if self._dca_deposit_trigger and self._dca_deposit_amount > 0:
                        _n_dca = sum(1 for _c in self.core_positions if getattr(_c, 'dca_mode', False))
                        _dca_budget = self._dca_deposit_amount / max(1, _n_dca)
                        _do_dca     = True
                        _dca_reason = f"예수금 입금 ({self._dca_deposit_amount:,.0f}원 / {_n_dca}종목 분배)"

                                                    
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
                                                                               

                if c_cash >= cp and is_core_cd and not _dca_bought_this_turn:
                                                                 
                                                  
                    c_score, c_score_reasons = calculate_core_entry_score(ex_df, cp, regime)
                                                   
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
                                                                   
                        first_cash    = c_cash * budget_ratio
                        _c_remain1    = max(0.0, c_cash - first_cash)
                        reserve_cash  = min(c_cash * budget_ratio, _c_remain1)
                        c_third_cash  = max(0.0, c_cash - first_cash - reserve_cash)
                        qty = int((first_cash * 0.98) // cp)
                        if qty > 0:
                                                                   
                            approved, ai_reason = True, "AI 미설정"
                            if self.claude:
                                with self.lock:
                                    core.status     = "🤔 AI 심사 중"
                                    core.status_msg = f"RSI{c_rsi:.0f}+120MA 기준 충족 | 악재 리스크 확인 중..."
                                                  
                                try:
                                    _c = ex_df['close'].dropna()
                                    _ma120 = float(_c.rolling(120).mean().iloc[-1]) if len(_c) >= 120 else 0
                                    _ma60  = float(_c.rolling(60).mean().iloc[-1])  if len(_c) >= 60  else 0
                                except Exception:
                                    _ma120 = _ma60 = 0
                                _news_raw = fetch_recent_news(c_nm)
                                _news = _news_raw if _news_raw and "조회 실패" not in _news_raw else ""
                                _c_ind = self._build_indicators_dict(c_tk, cp, ex_df, signal_types=['CORE_BUY'])
                                _c_mkt = self._build_market_info_dict()
                                approved, ai_reason = self.claude.ai_approve_core_trade(
                                    stock_name=c_nm, ticker=c_tk, price=cp,
                                    rsi=c_rsi, ma120=_ma120, ma60=_ma60,
                                    regime=regime, news_headlines=_news,
                                    indicators=_c_ind, market_info=_c_mkt,
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
                                             
            _sat_full_before = {t for t, p in trading_sat_items if p.shares > 0}

        for ticker, pos in trading_sat_items:
            try:
                with self.lock: p_sh = pos.shares; p_avg = pos.avg_price; p_max = pos.max_price; p_cash = pos.cash; p_nm = pos.name
                price = self.live_prices.get(ticker) or getattr(pos, 'toss_current_price', 0) or (self.toss.get_current_price(ticker) if self.toss else 0)
                if not price or price <= 0: continue
                with self.lock: pos._last_price = price

                ex_df = self._get_extended_ohlcv(ticker, price)
                sig, buy_sc, sell_sc, sig_reasons = get_composite_signal(ex_df)
                ind_val = {"buy": buy_sc, "sell": sell_sc, "signals": sig_reasons}
                if price <= 0: continue

                                                              
                _frgn_net = 0
                try:
                    if self.toss and hasattr(self.toss, 'get_foreign_buy_by_ticker'):
                        _fi = self.toss.get_foreign_buy_by_ticker(ticker)
                        if _fi:
                            _frgn_net = int(_fi.get("frgn_net", 0))
                except Exception:
                    pass
                entry_score, entry_reasons = calculate_entry_score(ex_df, price, regime, frgn_net=_frgn_net)
                                                  
                _ai_bonus = getattr(self, '_ai_market_entry_bonus', 0)
                if _ai_bonus != 0:
                    entry_score += _ai_bonus
                    entry_reasons.append(f"AI시장판단 {_ai_bonus:+d}pt")
                entry_threshold = self.entry_thresholds.get(f'sat_{regime}', self.entry_thresholds.get(regime, get_entry_threshold(regime, 'satellite')))
                score_ratio = max(0.6, get_budget_ratio_from_score(entry_score, entry_threshold))
                st_nm = f"진입점수({entry_score}/{entry_threshold}pt)"
                                                                                  

                if ex_df.empty or not all(c in ex_df.columns for c in ['high', 'low', 'close']):
                    atr_14 = p_avg * 0.02
                else:
                    tr = pd.concat([ex_df['high']-ex_df['low'], (ex_df['high']-ex_df['close'].shift(1)).abs(), (ex_df['low']-ex_df['close'].shift(1)).abs()], axis=1).max(axis=1)
                    atr_14 = tr.rolling(14, min_periods=1).mean().iloc[-1] if not tr.empty else p_avg * 0.02

                is_cd_passed = (time.time() - getattr(pos, 'last_order_time', 0) > 300)

                               
                                                           
                                                            
                                                        
                if regime == "BEAR":
                    trail_mult, trail_trigger, hard_mult = 1.2, 0.8, 1.8
                elif regime == "BULL":
                    trail_mult, trail_trigger, hard_mult = 1.5, 1.2, 3.0
                else:
                    trail_mult, trail_trigger, hard_mult = 1.5, 1.0, 2.5

                                                                      
                                                              
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

                                                                                 
                                                                         
                # ── 백테스트 통계 손절/익절선 (표시=실제, D정렬) — 매수메시지에 띄운 값을 실제로 강제 ──
                if p_sh > 0 and p_avg > 0 and is_cd_passed:
                    _bt_stop = getattr(pos, 'bt_stop_pct', 0) or 0
                    _bt_tgt  = getattr(pos, 'bt_target_pct', 0) or 0
                    _pnl_pct = (price / p_avg - 1) * 100
                    _bt_hit  = None
                    if _bt_stop and _pnl_pct <= _bt_stop:
                        _bt_hit = ("백테스트 손절선 🚨", f"통계 손절 {_bt_stop:.0f}% 도달")
                    elif _bt_tgt and _pnl_pct >= _bt_tgt:
                        _bt_hit = ("백테스트 목표달성 🎯", f"통계 목표 +{_bt_tgt:.0f}% 도달")
                    if _bt_hit:
                        _bt_qty = p_sh
                        if self._sell_order(ticker, _bt_qty, pos, p_nm):
                            with self.lock:
                                pos.last_order_time = time.time(); pos.status = _bt_hit[0]
                                self._sat_exit_reset(pos); pos.shares = 0; p_sh = 0
                            profit = _net_profit(price, p_avg, _bt_qty)
                            self._log_trade(ticker, p_nm, 'SELL', price, st_nm, _bt_hit[1], profit=profit)
                            self._send_trade_telegram(self._fmt_trade_msg("🎯", _bt_hit[0], ticker, p_nm, price, _bt_qty, profit=profit, strategy=st_nm, note=_bt_hit[1]))
                            with self.lock: self.pnl_this_turn += profit
                            self._record_daily_pnl(profit)
                        continue

                if p_sh > 0 and p_avg > 0 and is_cd_passed and "09:00" <= current_time_str <= "09:30":
                    _es_stage, _es_pct, _es_reason = check_early_drop_stop(price, p_avg)
                    if _es_stage > 0 and _es_pct > 0:
                        stop_qty = max(1, int(p_sh * _es_pct))
                        if self._sell_order(ticker, stop_qty, pos, p_nm):
                            with self.lock:
                                pos.last_order_time = time.time(); pos.status = "장초 급락 손절 🚨"
                                                                                       
                                pos.shares = max(0, pos.shares - stop_qty)
                            profit = _net_profit(price, p_avg, stop_qty)
                            self._log_trade(ticker, p_nm, 'SELL', price, st_nm, f"장초 급락 손절 {_es_pct*100:.0f}% [{_es_reason}]", profit=profit)
                            self._send_trade_telegram(self._fmt_trade_msg("🚨", "장초 급락 손절", ticker, p_nm, price, stop_qty, profit=profit, strategy=st_nm, note=_es_reason))
                            with self.lock: self.pnl_this_turn += profit
                            self._record_daily_pnl(profit)
                        continue

                if p_sh > 0 and p_avg > 0 and is_cd_passed:
                    if price <= p_avg - (hard_mult * atr_14):
                                                                      
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

                                                                           
                                                  
                                                                   
                                                                   
                if p_sh > 0 and p_avg > 0 and is_cd_passed and not ex_df.empty:
                    _sat_info_m   = next((s for s in self.satellite_info if s['ticker'] == ticker), None)
                    _sector       = _sat_info_m.get('sector', '') if _sat_info_m else ''
                    _sector_bonus = 10 if (_sector and _sector in self.hot_sectors) else 0

                    _oe_sig,  _oe_score,  _oe_reason  = check_theme_overextension_exit(ex_df, price, _sector_bonus)
                    _rsi_sig, _rsi_val,   _rsi_reason = check_rsi_progressive_exit(ex_df, price, p_avg)

                                                                              
                    _sig_rank = {'HOLD': 0, 'PARTIAL_EXIT_30': 1, 'PARTIAL_EXIT_60': 2, 'FULL_EXIT': 3}
                    if _sig_rank.get(_oe_sig, 0) >= _sig_rank.get(_rsi_sig, 0):
                        _fe_sig, _fe_reason = _oe_sig, _oe_reason
                    else:
                        _fe_sig, _fe_reason = _rsi_sig, _rsi_reason

                    if _fe_sig == 'FULL_EXIT':
                        _full_qty = p_sh                                 
                        if _full_qty > 0 and self._sell_order(ticker, _full_qty, pos, p_nm):
                            with self.lock:
                                pos.last_order_time = time.time(); pos.status = "과열 전량청산 🚨"
                                self._sat_exit_reset(pos)
                                p_sh = 0                   
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
                                p_sh = pos.shares                                
                            profit = _net_profit(price, p_avg, _q60)
                            self._log_trade(ticker, p_nm, 'SELL', price, st_nm, f"과열청산 60% [{_fe_reason}]", profit=profit)
                            self._send_trade_telegram(self._fmt_trade_msg("✂️", "과열 선익절 60%", ticker, p_nm, price, _q60, profit=profit, strategy=st_nm, note=_fe_reason))
                            with self.lock: self.pnl_this_turn += profit
                            self._record_daily_pnl(profit)

                    elif (_fe_sig == 'PARTIAL_EXIT_30'
                            and p_sh > 0
                            and getattr(pos, 'overext_sell_count', 0) < 3):
                        _oe_cnt = getattr(pos, 'overext_sell_count', 0)
                                                                
                        if _oe_cnt == 0:
                            with self.lock:
                                pos.initial_shares_for_exit = p_sh
                        _init_sh = getattr(pos, 'initial_shares_for_exit', 0) or p_sh
                        if _oe_cnt < 2 and p_sh > 1:
                                                              
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

                                                             
                if (p_sh > 0 and p_avg > 0 and is_cd_passed
                        and not getattr(pos, 'pyramid_done', False)
                        and price >= p_avg * 1.03
                        and p_cash > price
                        and sig != 'SELL'
                        and regime != "BEAR"):
                                                     
                    pyramid_cash = p_cash * (0.30 if regime == "BULL" else 0.20)
                    pyramid_qty = int((pyramid_cash * 0.98) // price)
                    if pyramid_qty > 0 and self._buy_order(ticker, pyramid_qty, pos, p_nm):
                        with self.lock:
                            pos.last_order_time = time.time(); pos.pyramid_done = True; pos.status = "피라미딩 📈"
                                                                             
                            new_shares = pos.shares + pyramid_qty
                            if new_shares > 0:
                                pos.avg_price = round((pos.avg_price * pos.shares + price * pyramid_qty) / new_shares, 2)
                            pos.shares = new_shares
                        self._log_trade(ticker, p_nm, 'BUY', price, st_nm, f"피라미딩 +3% 추세 지속 ({pyramid_qty}주)")
                        self._send_trade_telegram(self._fmt_trade_msg("📈", "피라미딩 추가 매수", ticker, p_nm, price, pyramid_qty, strategy=st_nm, note="+3% 돌파 · 상승 추세 지속 확인"))

                                                   
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

                                                 
                if p_sh == 0 and self._is_satellite_blacklisted(ticker):
                    pos.status = "당일 블랙리스트 🚫"
                    pos.status_msg = f"오늘 거절됨: {self._satellite_rejects.get(ticker, '')}"
                    continue

                                                             
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

                                                                  
                if p_sh == 0 and is_cd_passed and is_golden_hours and entry_score < entry_threshold:
                    pos.status = f"점수 대기 ({entry_score}/{entry_threshold}pt) ⏳"
                    pos.status_msg = f"진입 점수 미달 | 충족: {' | '.join(entry_reasons[:3]) if entry_reasons else '없음'}"
                    continue

                # ── 앙상블 게이트: 점수제 통과 + 백테스트 엔진(신호+통계)도 동의해야 매수 ──
                # (대결 결과 앙상블이 평균수익 최고 — 신호·점수 둘 다 동의할 때만 선별 진입)
                if p_sh == 0 and is_cd_passed and is_golden_hours and entry_score >= entry_threshold:
                    try:
                        from base.entry_engine import evaluate_ensemble
                        _mkt_e = self._build_market_info_dict()
                        _ens = evaluate_ensemble('KR', ex_df, _mkt_e.get('market_phase'),
                                                 score_agrees=True)
                        if not _ens['engine_buy']:
                            pos.status = "엔진 미동의 ⏸"
                            pos.status_msg = f"점수 통과했으나 백테스트 신호 미발생/저승률 — 앙상블 보류"
                            continue
                    except Exception:
                        pass

                if p_sh == 0 and is_cd_passed and is_golden_hours and entry_score >= entry_threshold:

                                                                    
                                         
                                                              
                    if regime == "BEAR":
                        bear_score, bear_reasons = get_bear_bottom_score(ex_df)
                        bear_ratio = get_bear_budget_ratio(bear_score)
                        if bear_ratio <= 0:
                            _grade = "0–4pt" if bear_score < 5 else "계산오류"
                            pos.status = f"하락장 매수 보류 🐻 (가중점수 {bear_score}/{_grade})"
                            pos.status_msg = (
                                f"BEAR 국면 — 저점 가중점수 {bear_score}pt | "
                                f"5pt 미만 차단 (최대21pt) | 활성신호: {len(bear_reasons)}개"
                            )
                            continue
                                              
                        _grade_label = (
                            "약한저점(15%)" if bear_score < 8 else
                            "중간저점(30%)" if bear_score < 12 else
                            "강한저점(50%)"
                        )
                        bear_label  = f"BEAR·가중{bear_score}pt·{_grade_label}+진입{entry_score}pt"
                        bear_reason_str = " | ".join(bear_reasons)
                        bounce_cash = p_cash * bear_ratio
                        qty = int((bounce_cash * 0.98) // price)
                        if qty > 0:
                                                      
                            if self.claude:
                                pos.status     = "🤔 AI 심사 중"
                                pos.status_msg = f"하락장 저점 신호 | {bear_reason_str} — AI 최종 승인 대기 중..."
                                trade_ctx = self._build_trade_context(ticker, p_nm, price, ex_df, st_nm, regime)
                                _ind = self._build_indicators_dict(ticker, price, ex_df, signal_types=[st_nm])
                                _mkt = self._build_market_info_dict()
                                decision, ai_reason = self.claude.ai_approve_trade(sig, p_nm, ticker, price, st_nm, ind_val, self.hot_sectors, context=trade_ctx, indicators=_ind, market_info=_mkt)
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

                                                             
                                                    
                    if regime != "BULL":
                        if not self._check_etf_market_positive():
                            pos.status = "시장 약세 ⏸"
                            pos.status_msg = "ETF 지수 -1% 이하, 매수 보류 (BULL 국면 제외)"
                            continue
                        if not self._check_minute_trend_up(ticker):
                            pos.status = "추세 하락 📉"
                            pos.status_msg = "최근 5분봉 하락 추세, 매수 보류 (BULL 국면 제외)"
                            continue

                                                            
                    if regime == "BULL":
                        bull_score, bull_reasons = get_bull_momentum_score(ex_df)
                        regime_bonus = 0.10 if bull_score >= 3 else (0.05 if bull_score >= 1 else 0.0)
                        entry_ratio  = min(0.90, score_ratio + regime_bonus)
                        regime_label = f"BULL·점수{entry_score}pt+타이밍{bull_score}개"
                        regime_reason_str = " | ".join(bull_reasons) if bull_reasons else "상승 추세 추종"
                    else:           
                        neutral_score, neutral_reasons = get_neutral_range_score(ex_df)
                        if neutral_score == 0:
                            pos.status = "횡보 관망 ⏸"
                            pos.status_msg = "NEUTRAL 국면 — 레인지 신호 없음, 매수 차단"
                            continue
                        regime_bonus  = 0.10 if neutral_score >= 3 else (0.05 if neutral_score >= 2 else 0.0)
                        entry_ratio   = min(0.90, score_ratio + regime_bonus)
                        regime_label  = f"NEUTRAL·점수{entry_score}pt+타이밍{neutral_score}개"
                        regime_reason_str = " | ".join(neutral_reasons)

                                                                    
                                                                                      
                                                                           
                    entry_cash   = p_cash * entry_ratio
                    _remain1     = max(0.0, p_cash - entry_cash)
                    reserve_cash = min(p_cash * entry_ratio, _remain1)
                    third_cash   = max(0.0, p_cash - entry_cash - reserve_cash)

                                                                
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
                        pos.status     = "🤔 AI 심사 중"
                        pos.status_msg = f"매수 신호 발생 | {st_nm} — AI 최종 승인 대기 중..."
                        trade_ctx = self._build_trade_context(ticker, p_nm, price, ex_df, st_nm, regime)
                        if _52w_note_kr:
                            trade_ctx += f"\n[52주 신고가] {_52w_note_kr}"
                        _ind = self._build_indicators_dict(ticker, price, ex_df, signal_types=[st_nm])
                        _mkt = self._build_market_info_dict()
                        decision, ai_reason = self.claude.ai_approve_trade(sig, p_nm, ticker, price, st_nm, ind_val, self.hot_sectors, context=trade_ctx, indicators=_ind, market_info=_mkt)
                        if decision:
                            qty = int((entry_cash * 0.98) // price)
                            if qty > 0 and self._buy_order(ticker, qty, pos, p_nm):
                                with self.lock:
                                    pos.last_order_time = time.time(); pos.status = "체결 대기 ⏳"
                                    pos.status_msg      = f"AI 승인: {ai_reason}"
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
                                                                       
                                self._capture_bt_levels(pos, ex_df)
                                self._log_trade(ticker, p_nm, 'BUY', price, st_nm, f"AI 승인 [{regime_label}] 1차(30%) ({ai_reason})")
                                _fc_block = self._forecast_block(p_nm, ticker, price, ex_df)
                                self._send_trade_telegram(self._fmt_trade_msg("📈", "AI 매수 승인 (1차 30%)", ticker, p_nm, price, qty, strategy=f"{st_nm}  ·  {regime_label}", ai_reason=ai_reason, note=f"2차 -2%({price*0.98:,.0f}원) / 3차 -4%({price*0.96:,.0f}원) 예약") + _fc_block)
                                self.claude.record_trade_event(f"KR 위성 매수: {p_nm}({ticker}) {qty}주 @ {price:,.0f}원 | {regime_label} | AI: {ai_reason[:60]}")
                        else:
                            pos.status     = "AI 거절 🛑"
                            pos.status_msg = f"거절 이유: {ai_reason}"
                                                         
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
                            self._capture_bt_levels(pos, ex_df)
                            self._log_trade(ticker, p_nm, 'BUY', price, st_nm, f"알고리즘 [{regime_label}] 1차(30%): {regime_reason_str}")
                            _fc_block = self._forecast_block(p_nm, ticker, price, ex_df)
                            self._send_trade_telegram(self._fmt_trade_msg("📈", "알고리즘 매수 (1차 30%)", ticker, p_nm, price, qty, strategy=f"{st_nm}  ·  {regime_label}", note=f"2차 -2%({price*0.98:,.0f}원) / 3차 -4%({price*0.96:,.0f}원) 예약 | {regime_reason_str}") + _fc_block)

                elif sig == 'SELL' and p_sh > 0 and is_cd_passed:
                    if self.claude:
                        pos.status     = "🤔 AI 심사 중"
                        pos.status_msg = f"매도 신호 발생 | {st_nm} — AI 최종 승인 대기 중..."
                        trade_ctx = self._build_trade_context(ticker, p_nm, price, ex_df, st_nm, regime)
                        _ind = self._build_indicators_dict(ticker, price, ex_df, signal_types=[st_nm])
                        _mkt = self._build_market_info_dict()
                        decision, ai_reason = self.claude.ai_approve_trade(sig, p_nm, ticker, price, st_nm, ind_val, self.hot_sectors, context=trade_ctx, indicators=_ind, market_info=_mkt)
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
                                                                    
                                if profit > 0 and self.core_positions:
                                    reinvest_sat = profit * REINVEST_RATIO
                                    for core in self.core_positions:
                                        core.cash += reinvest_sat / len(self.core_positions)
                            self._record_daily_pnl(profit)
            except Exception as e:
                logger.error(f"[{self.mode_name}] 위성 매매 오류 ({ticker}): {e}", exc_info=True)
            time.sleep(0.2)

                                                       
                                                
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
\
\
\
\
\
           
        if getattr(pos, 'ai_exit_pending', False):
            return
        asked = getattr(pos, 'ai_exit_asked_price', 0.0)
        if getattr(pos, 'ai_exit_decision', None) == "HOLD" and asked > 0:
            risen  = price >= asked * 1.01           
            fallen = price <= asked * 0.98           
            if not risen and not fallen:
                return
        pos.ai_exit_pending     = True
        pos.ai_exit_asked_price = price               

        def _worker():
            try:
                                                      
                _news = ""
                if self.news_monitor:
                    try:
                        _news = self.news_monitor.get_news_summary(name, display=3)
                    except Exception:
                        pass
                _exit_df  = self._get_extended_ohlcv(ticker, price) if self.toss else None
                _exit_ind = self._build_indicators_dict(ticker, price, _exit_df, signal_types=['PARTIAL_EXIT'])
                _exit_mkt = self._build_market_info_dict()
                decision = self.claude.ai_partial_exit(
                    ticker=ticker, stock_name=name, price=price,
                    avg_price=avg, pnl_pct=pnl_pct,
                    shares=int(getattr(pos, 'shares', 0)),
                    partial_sold=bool(getattr(pos, 'partial_sold', False)),
                    regime=regime,
                    news_headlines=_news,
                    indicators=_exit_ind, market_info=_exit_mkt,
                )
                with self.lock:
                    pos.ai_exit_decision = decision
                    pos.ai_exit_pending  = False
            except Exception:
                with self.lock:
                    pos.ai_exit_pending = False

        threading.Thread(target=_worker, daemon=True).start()

                                                                      
    def _weekend_satellite_scan(self):
\
\
\
           
        now = _now_kst()
        today_str = now.strftime('%Y-%m-%d')
        if self._weekend_scan_done == today_str:
            return            
        if now.weekday() < 5:
            return              

        self.add_log("📅 [주말 사전분석] 위성 후보 스캔 시작...")
        try:
                            
            with self.lock:
                current_sat = {t: p for t, p in self.satellite_positions.items() if p.shares > 0}
            current_tickers = set(current_sat.keys())

                                
            from KR.strategy import calculate_entry_score, get_entry_threshold, get_market_regime
            raw_candidates, new_hot = select_satellites(
                toss=self.toss, n=self.num_satellites * 4,
                verbose=False, claude_client=self.claude,
                sector_guide=self.sector_guide,
                exclude=current_tickers
            )
            if new_hot:
                self.hot_sectors = new_hot

            swap_plan = {}
                                         
            for ticker, pos in current_sat.items():
                try:
                    ohlcv = self._get_cached_base_ohlcv(ticker)
                    if ohlcv.empty: continue
                    score, reasons, _ = calculate_entry_score(
                        ohlcv, ticker, self.market_regime,
                        sector_score=0, kis_score=0, dl_score=0, roe_bonus=0
                    )
                    threshold = get_entry_threshold(self.market_regime)
                                       
                    if score < threshold - 1:
                        swap_plan[ticker] = {"reason": f"진입점수 {score}/{threshold}pt 미달", "score": score}
                except Exception:
                    pass

                                    
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
\
\
           
        if not self._monday_swap_plan:
            return
        now = _now_kst()
        if now.weekday() != 0:          
            return

        self.add_log(f"🚀 [월요일 교체] 주말 계획 실행 — {len(self._monday_swap_plan)}건")
        executed = []
        for old_ticker, plan in list(self._monday_swap_plan.items()):
            try:
                with self.lock:
                    pos = self.satellite_positions.get(old_ticker)
                if not pos or pos.shares == 0:
                    continue          
                price = self.live_prices.get(old_ticker) or (self.toss.get_current_price(old_ticker) if self.toss else 0)
                if not price:
                    continue
                          
                if self._sell_order(old_ticker, pos.shares, pos, pos.name):
                    profit = (price - pos.avg_price) * pos.shares if pos.avg_price else 0
                    self._log_trade(old_ticker, pos.name, 'SELL', price, "주말계획교체", plan['reason'], profit=profit)
                    self.add_log(f"📤 [{old_ticker}] {pos.name} 매도 완료 (주말 계획)")
                                                    
                    with self.lock:
                        self.satellite_info = [s for s in self.satellite_info if s.get('ticker') != old_ticker]
                        self.satellite_info.insert(0, {"ticker": plan['new_ticker'], "name": plan['new_name'], "return_pct": plan['score'], "sector": "-"})
                    executed.append(old_ticker)
            except Exception as e:
                logger.error(f"[{self.mode_name}] 월요일 교체 실행 오류({old_ticker}): {e}")

                   
        for t in executed:
            self._monday_swap_plan.pop(t, None)
        if executed:
            self._save_state()
            self.add_log(f"✅ [월요일 교체] {len(executed)}건 완료 → 새 종목 매수 진행")

    def _rescreen_satellites(self, force: bool = False):
        try:
            now = _now_kst()
                                                      
            if not force:
                if not ("09:01" <= now.strftime('%H:%M') <= "15:20") or now.weekday() >= 5:
                    return
            self.add_log(f"🦅 {self.mode_name} 위성 실시간 교체 탐색 중...")
            keep_tickers = set()                               
            strong_keeps = set()                            
            freed_cash = 0
            with self.lock: sat_items = list(self.satellite_positions.items())

                                                             
                                             

            _GROWTH_KEEP = 3.0                                  
            _LOSS_CUT    = -3.0                                    

            for ticker, pos in sat_items:
                if pos.shares == 0:
                    freed_cash += pos.cash
                    with self.lock:
                        if ticker in self.satellite_positions: del self.satellite_positions[ticker]
                    continue
                                                   
                _is_user_mgd = getattr(pos, 'user_managed', False)
                _info_e = next((i for i in self.satellite_info if i.get('ticker') == ticker), None)
                _is_acct = _info_e and _info_e.get('sector') == '계좌편입'
                if _is_user_mgd or _is_acct:
                    keep_tickers.add(ticker)
                    strong_keeps.add(ticker)
                    self.add_log(f"🔒 {pos.name}({ticker}) 수동편입 보호 — 재스크리닝 제외")
                    continue
                time.sleep(0.2)
                price = self.toss.get_current_price(ticker) if self.toss else 0
                if price and pos.avg_price > 0:
                    profit_rt = (price / pos.avg_price - 1) * 100
                    if profit_rt >= _GROWTH_KEEP:
                                                
                        keep_tickers.add(ticker)
                        strong_keeps.add(ticker)
                        self.add_log(f"🌱 {pos.name}({ticker}) 성장세 양호 ({profit_rt:+.1f}%) — 교체 없이 유지")
                    elif profit_rt > _LOSS_CUT:
                                                             
                        try:
                            from KR.strategy import calculate_entry_score, get_entry_threshold
                            _ohlcv = self._get_cached_base_ohlcv(ticker)
                            if not _ohlcv.empty:
                                _score, _, _ = calculate_entry_score(
                                    _ohlcv, ticker, self.market_regime,
                                    sector_score=0, kis_score=0, dl_score=0, roe_bonus=0
                                )
                                _threshold = get_entry_threshold(self.market_regime)
                                if _score < _threshold - 1:
                                                                                  
                                    keep_tickers.add(ticker)
                                    self.add_log(f"📉 {pos.name}({ticker}) 관망이나 점수 미달 ({_score}/{_threshold}pt) — 교체 후보")
                                else:
                                    keep_tickers.add(ticker)
                                    strong_keeps.add(ticker)
                                    self.add_log(f"⏸️ {pos.name}({ticker}) 관망 유지 ({profit_rt:+.1f}%, 점수 {_score}pt)")
                            else:
                                keep_tickers.add(ticker)
                                self.add_log(f"⏸️ {pos.name}({ticker}) 관망 유지 ({profit_rt:+.1f}%)")
                        except Exception:
                            keep_tickers.add(ticker)
                            self.add_log(f"⏸️ {pos.name}({ticker}) 관망 유지 ({profit_rt:+.1f}%)")
                    else:
                                                                             
                        with self.lock:
                            shares_now = pos.shares
                        if shares_now > 0:
                            if self.toss: self.toss.sell_market_order(ticker, shares_now, price=int(price))
                            sell_qty = 0; sell_profit = 0.0                
                            with self.lock:
                                                                
                                if pos.shares > 0:
                                    sell_qty, sell_profit = pos.sell(price)
                                    freed_cash += pos.cash                      
                                    self.pnl_this_turn += sell_profit            
                                if ticker in self.satellite_positions: del self.satellite_positions[ticker]
                            if sell_qty > 0:                            
                                self._log_trade(ticker, pos.name, 'SELL', price, '위성교체', '재스크리닝 손절', profit=sell_profit)
                                self._record_daily_pnl(sell_profit)
                        else:
                            with self.lock:
                                if ticker in self.satellite_positions: del self.satellite_positions[ticker]

                                                                         
                                                              
            if len(keep_tickers) > self.num_satellites:
                                          
                profit_map = {}
                for t in list(keep_tickers):
                    pos = self.satellite_positions.get(t)
                    if pos and pos.avg_price > 0:
                        p = self.live_prices.get(t) or (self.toss.get_current_price(t) if self.toss else 0) or pos.avg_price
                        profit_map[t] = (p / pos.avg_price - 1) * 100
                    else:
                        profit_map[t] = 0.0
                sorted_keep = sorted(keep_tickers, key=lambda t: profit_map.get(t, 0))
                                         
                sorted_keep = [t for t in sorted_keep
                               if not getattr(self.satellite_positions.get(t), 'user_managed', False)
                               and not (next((i for i in self.satellite_info if i.get('ticker') == t), {}).get('sector') == '계좌편입')]
                excess = sorted_keep[:max(0, len(keep_tickers) - self.num_satellites)]
                for t in excess:
                    pos = self.satellite_positions.get(t)
                    if pos:
                        with self.lock:
                            shares_now = pos.shares
                        price_e = (self.live_prices.get(t)
                                   or (self.toss.get_current_price(t) if self.toss else 0)
                                   or pos.avg_price or 0)
                        sell_qty, excess_profit = 0, 0.0
                        if shares_now > 0 and price_e:
                            if self.toss and self.toss.sell_market_order(t, shares_now, price=int(price_e)):
                                with self.lock:
                                    if pos.shares > 0:
                                                                             
                                        sell_qty, excess_profit = pos.sell(price_e)
                                        self.pnl_this_turn += excess_profit
                        with self.lock:
                            freed_cash += pos.cash                               
                            if t in self.satellite_positions: del self.satellite_positions[t]
                        if sell_qty > 0:
                                                         
                            self._log_trade(t, pos.name, 'SELL', price_e, '위성초과정리',
                                            f'초과({self.num_satellites}개 한도) 강제 청산',
                                            profit=excess_profit)
                            self._record_daily_pnl(excess_profit)
                        keep_tickers.discard(t)
                        self.add_log(f"✂️ 위성 초과({self.num_satellites}개 한도) 정리: {pos.name}({t}) 청산")

                                                           
            replaceable_keeps = keep_tickers - strong_keeps
            n_needed = self.num_satellites - len(keep_tickers)
            if n_needed <= 0:
                if strong_keeps:
                    self.add_log(f"✅ 위성 {len(strong_keeps)}개 성장세 양호 — 전 슬롯 유지, 재스크리닝 스킵")
                return

                                                 
                                                              
            with self.lock:
                self._refresh_blacklist()
                                                                  
            with self.lock:
                n_rejects       = len(self._satellite_rejects)
                bl_set          = set(self._satellite_rejects.keys())
                                                                
                                                                   
                                                                    
            exclude_set = keep_tickers | bl_set
            raw_info, _new_hot = select_satellites(
                toss=self.toss, n=self.num_satellites + n_needed + 3,
                verbose=False, claude_client=self.claude, bear_mode=(self.market_regime == "BEAR"),
                sector_guide=self.sector_guide,
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
                                                                         
            pre_filter = [
                c for c in raw_info
                if c['ticker'] not in keep_tickers
                and not self._is_satellite_blacklisted(c['ticker'])
            ]
                                                     
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
                                                                      
                                                          

                                                                         
            keep_tickers = {t for t in keep_tickers if t in self.satellite_positions}
            for ticker in keep_tickers: freed_cash += self.satellite_positions[ticker].cash; self.satellite_positions[ticker].cash = 0

                                                                          
                                                                   
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
                _rescreen_now = _now_kst().strftime('%Y-%m-%d %H:%M')
                for _c in new_info:
                    _c.setdefault('screened_at', _rescreen_now)
                self.satellite_info = [c for c in self.satellite_info if c['ticker'] in keep_tickers] + new_info
                self._inject_user_satellites()                   

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
            if self.toss:
                for ticker, name in self.market_indices:
                    df = self._get_cached_base_ohlcv(ticker)
                    cp = self.live_prices.get(ticker) or self.toss.get_current_price(ticker)
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
            
            report_data = generate_daily_market_report(claude_client=self.claude, verbose=False, news_context=combined_context, toss=self.toss)
            if report_data:
                today_str = _now_kst().strftime('%Y-%m-%d')
                if not isinstance(self.daily_report, dict) or self.daily_report.get('date') != today_str: self.daily_report = {'date': today_str, '15:40': None}
                content = report_data.get('report_markdown') if isinstance(report_data, dict) else str(report_data)
                self.daily_report[time_slot] = content
                _surge = report_data.get('volume_surge_details', [])
                if _surge:
                    self.volume_surge_details = _surge
                self._save_state()

                phase_block = ''
                try:
                    from base.market_phase import get_phase_for_date, build_phase_context_str
                    from base.database import get_phase_strategy_stats, get_db_connection
                    phase_info = get_phase_for_date('KR', today_str)
                    conn = get_db_connection()
                    bt_total = conn.execute('SELECT COUNT(*) FROM backtest_optimal_points WHERE mode="KR"').fetchone()[0]
                    bt_today = conn.execute('SELECT COUNT(*) FROM backtest_optimal_points WHERE mode="KR" AND date(created_at)=date("now")').fetchone()[0]
                    conn.close()
                    ph = phase_info.get('phase', '')
                    ph_stats = get_phase_strategy_stats('KR', ph) if ph else []
                    stat_lines = ''
                    if ph_stats:
                        stat_lines = '\n'.join(
                            f"  {s['signal_type']}: 승률 {s['win_rate']}% (n={s['total']}, 평균 20일 {s['avg_pnl_20d']:+.1f}%)"
                            for s in ph_stats[:5]
                        )
                    phase_block = (
                        f"\n\n📊 <b>[시장 국면]</b> {phase_info.get('phase_kr','—')} "
                        f"(신뢰도 {int(phase_info.get('confidence',0)*100)}%)\n"
                        f"근거: {' | '.join(phase_info.get('evidence',[])[:3])}\n"
                        f"전략: {phase_info.get('advice','')}\n"
                        + (f"\n이 국면 역대 신호 승률:\n{stat_lines}" if stat_lines else '')
                        + f"\n\n🗂 백테스트 누적: {bt_total:,}개 포인트 (오늘 {bt_today:,}개 추가)"
                    )
                except Exception:
                    pass

                self._send_telegram(f"📝 [일일 레포트]\n\n{content[:3500]}{phase_block}")
        except Exception as e:
            logger.error(f"[{self.mode_name}] 일일 리포트 생성 오류: {e}", exc_info=True)

                                                             

    def _log_trade(self, ticker: str, name: str, action: str, price: float,
                   strategy: str, reason: str, profit: float = 0):
\
                                                   
        log_trade_journal(self.user_id, ticker, name, action, price, strategy, reason, profit)

                                  
        self._trades_since_reflection += 1
        if self._trades_since_reflection >= 10:
            self._trades_since_reflection = 0
            self.add_log("📚 [누적 10건 달성] 학습 반성 트리거")
            self._run_threaded(self._incremental_reflection)
            return                

                                              
        if action == 'SELL' and profit < self._EMERGENCY_LOSS_THRESHOLD:
            cooldown_remaining = self._EMERGENCY_COOLDOWN - (time.time() - self._last_emergency_reflection_ts)
            if cooldown_remaining <= 0:
                self.add_log(f"🚨 [큰 손실 감지] {name} {profit:,.0f}원 — 긴급 반성 시작")
                self._last_emergency_reflection_ts = time.time()
                self._run_threaded(lambda: self._emergency_reflection(ticker, name, profit, reason))

    def _weekly_self_reflection(self):
                                                     
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
        existing_rules = load_ai_rules(self.user_id)               
        if self.claude:
            new_rules = self.claude.generate_weekly_reflection(history_text, existing_rules)
            if new_rules:
                save_ai_rules(self.user_id, new_rules, trigger_type='weekly')
                self._send_telegram(f"🧠 [주간 학습 완료]\n\n{new_rules[:2000]}")

    def _incremental_reflection(self):
                                                
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

                                                               
                                        
        try:
            already_restored = getattr(self, '_init_state_restored', False)
            if not already_restored or not self.core_positions:
                if not self._restore_state():
                    self.initialize_portfolio(total_cash)
        except Exception as e:
            logger.error(f"[{self.mode_name}] 포트폴리오 초기화 실패 (기본 코어로 계속 진행): {e}", exc_info=True)

                                                            
                                                            
        try:
            from datetime import date as _date
            _today = _now_kst().date()
            _lsd = getattr(self, 'last_screen_date', None)
            _days_since = (_today - _lsd).days if _lsd else 999
            if _days_since >= 7:
                self.add_log(f"🔄 마지막 위성 스크린 {_days_since}일 전 → 시작 시 재스크린 예약 (5분 후)")
                def _delayed_rescreen():
                    import time as _t; _t.sleep(300)                 
                    now_kst = _now_kst()
                    if now_kst.weekday() < 5:                
                        self._rescreen_satellites(force=True)
                    else:              
                        self._weekend_satellite_scan()
                self._run_threaded(_delayed_rescreen)
        except Exception as e:
            logger.warning(f"[{self.mode_name}] 시작 시 재스크린 판단 오류: {e}")

                                                         
                                                               
        try:
            if self.toss:
                toss_bal = self.toss.get_account_balance()
                if toss_bal:
                    toss_shares = {s["ticker"]: s["shares"] for s in toss_bal.get("stocks", [])}
                    for pos in self.core_positions:
                        if pos.ticker == "TBD":
                            continue
                        real_qty = toss_shares.get(pos.ticker, 0)
                        if pos.shares != real_qty:
                            logger.info(f"[{self.mode_name}] 보유주수 보정 {pos.ticker}: DB={pos.shares} → 토스={real_qty}")
                            pos.shares = real_qty
                    for ticker, pos in list(self.satellite_positions.items()):
                        real_qty = toss_shares.get(ticker, 0)
                        if pos.shares != real_qty:
                            logger.info(f"[{self.mode_name}] 보유주수 보정 {ticker}: DB={pos.shares} → 토스={real_qty}")
                            pos.shares = real_qty
                    self.add_log(f"✅ 토스 실잔고로 보유 주수 검증 완료")
        except Exception as e:
            logger.warning(f"[{self.mode_name}] 시작 시 토스 잔고 검증 실패: {e}")

                                                              
                                                                    
        self.scheduler.every(1).minutes.do(self.trading_job)
        self.scheduler.every(30).minutes.do(lambda: self._run_threaded(self.analyze_continuous_market_flow))

        def _hourly_rescreen_if_empty():
                                                             
            with self.lock:
                has_empty = any(p.shares == 0 for p in self.satellite_positions.values())
            if has_empty:
                self._run_threaded(self._rescreen_satellites)

        self.scheduler.every(1).hours.do(_hourly_rescreen_if_empty)

                                                                        
                                                                
                                                                      
                                                             
        def _kst_midnight_rescreen():
                                                      
            kst_hm = _now_kst().strftime('%H:%M')
            if kst_hm == "00:05":
                self._run_threaded(self._rescreen_satellites)

        def _kst_friday_reflection():
                                                    
            now_kst = _now_kst()
            if now_kst.weekday() == 4 and now_kst.strftime('%H:%M') == "16:00":
                self._run_threaded(self._weekly_self_reflection)

        def _kst_morning_websocket():
                                                                              
            pass

        def _kst_morning_prescreen():
\
\
               
            if _now_kst().strftime('%H:%M') == "08:50":
                self.add_log("🔍 [08:50 사전 스크리닝] 9:05 첫 매매 대비 위성 종목 선정 시작")
                self._run_threaded(self._rescreen_satellites)

        def _kst_friday_lstm():
                                                      
            now_kst = _now_kst()
            if now_kst.weekday() == 4 and now_kst.strftime('%H:%M') == "02:00":
                self._run_threaded(self.run_lstm_training)

        def _kst_weekend_scan():
                                                         
            now_kst = _now_kst()
            if now_kst.weekday() >= 5 and now_kst.strftime('%H:%M') == "14:00":
                self._run_threaded(self._weekend_satellite_scan)

        def _kst_monday_execute():
                                          
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

                                                                    
        _backtest_done_slots = {}
        _KR_WEEKEND_HOURS = {'00:00', '06:00', '12:00', '18:00'}
        _US_WEEKEND_HOURS = {'03:00', '09:00', '15:00', '21:00'}

        def _build_backtest_ai():
            from base.database import get_db_connection
            from ai.client import get_ai_client_from_db
            _conn = get_db_connection()
            _ud = dict(_conn.execute('SELECT fred_api_key FROM users WHERE id=?', (self.user_id,)).fetchone() or {})
            _conn.close()
            return get_ai_client_from_db(self.user_id, role='backtest'), _ud.get('fred_api_key') or ''

        def _run_kr_backtest(batch_size: int, label: str):
            def _worker():
                try:
                    from KR.backtest_runner import BacktestRunner
                    from base.database import get_news_api_keys
                    _ai, _fred = _build_backtest_ai()
                    _nk = get_news_api_keys(self.user_id)
                    runner = BacktestRunner(self.user_id, _ai, self.toss, _fred,
                                           dart_key=_nk.get('dart_api_key') or '',
                                           naver_id=_nk.get('naver_client_id') or '',
                                           naver_secret=_nk.get('naver_client_secret') or '')
                    n = runner.run_batch(batch_size)
                    self.add_log(f"📊 [KR/{label}] 백테스트 완료: {n}개 신호 기록")
                    self._send_telegram(
                        f"📊 <b>[KR/{label}] 백테스트 완료</b>  {self.alert_icon}\n"
                        f"{n}개 신호 AI 학습 데이터 누적",
                        'backtest'
                    )
                except Exception as e:
                    logger.warning(f"[{self.mode_name}] KR 백테스트 오류: {e}")
            self._run_threaded(_worker)

        def _run_us_backtest(batch_size: int, label: str):
            def _worker():
                try:
                    from US.backtest_runner import USBacktestRunner
                    _ai, _ = _build_backtest_ai()
                    runner = USBacktestRunner(self.user_id, _ai)
                    n = runner.run_batch(batch_size)
                    self.add_log(f"📊 [US/{label}] 백테스트 완료: {n}개 신호 기록")
                    self._send_telegram(
                        f"📊 <b>[US/{label}] 백테스트 완료</b>  {self.alert_icon}\n"
                        f"{n}개 신호 AI 학습 데이터 누적",
                        'backtest'
                    )
                except Exception as e:
                    logger.warning(f"[{self.mode_name}] US 백테스트 오류: {e}")
            self._run_threaded(_worker)

        def _kst_nightly_backtest():
            now = _now_kst()
            slot = f"{now.strftime('%Y-%m-%d')}_{now.strftime('%H:%M')}"
            if slot in _backtest_done_slots:
                return
            t = now.strftime('%H:%M')
            if now.weekday() < 5:
                if t == '16:30':
                    _backtest_done_slots[slot] = True
                    _run_kr_backtest(100, '평일마감')
                elif t == '17:30':
                    _backtest_done_slots[slot] = True
                    _run_us_backtest(50, '평일마감')
            else:
                if t in _KR_WEEKEND_HOURS:
                    _backtest_done_slots[slot] = True
                    _run_kr_backtest(200, '주말')
                elif t in _US_WEEKEND_HOURS:
                    _backtest_done_slots[slot] = True
                    _run_us_backtest(150, '주말')

        self.scheduler.every(1).minutes.do(_kst_nightly_backtest)

        try:
            self.trading_job()
        except Exception as e:
            logger.error(f"[{self.mode_name}] 초기 trading_job 오류: {e}", exc_info=True)

        while self.is_running:
            try:
                self.scheduler.run_pending()
            except Exception as e:
                logger.error(f"[{self.mode_name}] 스케줄러 오류: {e}", exc_info=True)
                                                                       
                                                              
            if getattr(self, '_trading_job_running', False):
                _job_start = getattr(self, '_trading_job_start_ts', 0)
                if _job_start > 0 and (time.time() - _job_start) > 180:
                    logger.error(f"[{self.mode_name}] trading_job 180초 초과 — 강제 리셋 (watchdog)")
                    self.add_log("⚠️ [watchdog] trading_job 3분 초과 강제 리셋")
                    self._trading_job_running = False
                    self._trading_job_start_ts = 0
            time.sleep(1)
    
    def refresh_websocket(self):
                                                                             
        pass

    def run_lstm_training(self):
        try:
            import os, sys, subprocess
            script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "train_lstm.py")
            subprocess.run([sys.executable, script_path], capture_output=True, text=True)
        except Exception: pass

    def start(self, total_cash=10_000_000):
        if not self.toss: return False
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
            self._save_state()                            
            update_bot_status(self.user_id, False, is_mock=self._is_mock)
            if self.thread: self.thread.join(timeout=3)

    def get_pnl_data(self):
                                      
        from collections import defaultdict
        sorted_days = sorted(self.daily_pnl.keys())

                     
        daily_labels = sorted_days[-30:]
        daily_values = [round(self.daily_pnl[d]) for d in daily_labels]

                          
        weekly: dict = defaultdict(float)
        for d in sorted_days:
            try:
                dt = datetime.strptime(d, '%Y-%m-%d')
                week_key = dt.strftime('%Y-W%W')
                weekly[week_key] += self.daily_pnl[d]
            except Exception:
                pass
        weekly_labels = sorted(weekly.keys())[-26:]          
        weekly_values = [round(weekly[w]) for w in weekly_labels]

                         
        monthly: dict = defaultdict(float)
        for d in sorted_days:
            monthly[d[:7]] += self.daily_pnl[d]
        monthly_labels = sorted(monthly.keys())[-24:]           
        monthly_values = [round(monthly[m]) for m in monthly_labels]

                      
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
                                            
            "labels":  daily_labels,
            "values":  daily_values,
        }

    def get_status(self):
        try:
                                                            
            if not self.lock.acquire(timeout=2):
                return {"running": self.is_running, "is_running": self.is_running,
                        "cores": [], "satellites": [], "logs": list(self._log_buffer)[-30:],
                        "mock_total_asset": 0, "mock_pnl_rt": 0, "_lock_timeout": True}
            try:
                safe_core_positions = list(self.core_positions)
                safe_satellite_items = list(self.satellite_positions.items())
            finally:
                self.lock.release()

                                                           
                                                        
                                          
            _toss_avg: dict = {}                 
            _toss_price: dict = {}                           
            if self.cached_balance:
                for _s in self.cached_balance.get('stocks', []):
                    _t = _s.get('ticker', '')
                    _p = float(_s.get('purchase_price', 0))
                    _c = float(_s.get('current_price', 0))
                    if _t and _p > 0:
                        _toss_avg[_t] = _p
                    if _t and _c > 0:
                        _toss_price[_t] = _c

            total_realtime_stock_val = 0.0
            tracked_tickers = set()                                      
            cores_data = []
            for core in safe_core_positions:
                                                              
                cp = float(_toss_price.get(core.ticker, 0) or self.live_prices.get(core.ticker, 0) or getattr(core, '_last_price', 0) or getattr(core, 'toss_current_price', 0) or core.avg_price or 0)
                core_val = float(core.shares) * cp
                total_realtime_stock_val += core_val
                tracked_tickers.add(core.ticker)
                                               
                _avg_p = _toss_avg.get(core.ticker) or float(getattr(core, 'avg_price', 0) or 0)
                cores_data.append({"name": core.name, "ticker": core.ticker, "shares": core.shares, "floor": core.floor_shares, "price": cp, "value": core_val, "avg_price": _avg_p, "budget": float(getattr(core, 'cash', 0) or 0), "strategy": "장기 우상향" if core.ticker != self.core_ticker else "RSI + floor 보호", "status": getattr(core, 'status', '감시 중 👀'), "status_msg": getattr(core, 'status_msg', '지표 점검 중...'), "dca_mode": bool(getattr(core, 'dca_mode', False))})

            satellites = []
                                                       
            holding_items = [(t, p) for t, p in safe_satellite_items if p.shares > 0]
            empty_items   = [(t, p) for t, p in safe_satellite_items if p.shares == 0]
            capped_items  = (holding_items + empty_items)[:self.num_satellites]

                                                                            
                                                               
                                                
            _sat_price_cache: dict = {}
            for ticker, pos in safe_satellite_items:
                tracked_tickers.add(ticker)                            
                if pos.shares > 0:
                    sp = float(_toss_price.get(ticker, 0) or self.live_prices.get(ticker, 0) or getattr(pos, '_last_price', 0) or getattr(pos, 'toss_current_price', 0) or pos.avg_price or 0)
                    _sat_price_cache[ticker] = sp
                    total_realtime_stock_val += float(pos.shares) * sp

                                    
            for ticker, pos in capped_items:
                sp = _sat_price_cache.get(ticker) or float(_toss_price.get(ticker, 0) or self.live_prices.get(ticker, 0) or getattr(pos, '_last_price', 0) or getattr(pos, 'toss_current_price', 0) or pos.avg_price or 0)
                sat_val = float(pos.shares) * sp
                                               
                _avg_p = _toss_avg.get(ticker) or float(getattr(pos, 'avg_price', 0) or 0)
                satellites.append({"name": pos.name, "ticker": ticker, "shares": pos.shares, "price": sp, "value": sat_val, "avg_price": _avg_p, "budget": float(getattr(pos, 'cash', 0) or 0), "status": getattr(pos, 'status', '감시 중 👀'), "status_msg": getattr(pos, 'status_msg', '지표 점검 중...')})

            try:
                current_initial_cash = get_user_initial_cash(self.user_id, self._is_mock)
            except Exception: current_initial_cash = 10000000.0

                                             
            momentum_list = []

                                                                        
                                                                     
            if self.cached_balance:
                for _s in self.cached_balance.get('stocks', []):
                    _t = _s.get('ticker', '')
                    _sh = int(_s.get('shares', 0))
                    if _t and _t not in tracked_tickers and _sh > 0:
                        _p = self.live_prices.get(_t) or float(_s.get('current_price', 0))
                        total_realtime_stock_val += _sh * _p

                                                       
            if self.cached_balance or self.internal_cash is not None:
                                                            
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

                                          
            is_bear = (self.market_regime == "BEAR")
            defensive_list = []
            bal_stocks = {s['ticker']: int(s.get('shares', 0)) for s in (self.cached_balance or {}).get('stocks', [])} if self.cached_balance else {}
            for asset in DEFENSIVE_ASSETS:
                d_ticker = asset['ticker']
                d_price  = self.live_prices.get(d_ticker, 0)
                                                 
                if not d_price and self.toss:
                    try:
                        d_price = self.toss.get_current_price(d_ticker) or 0
                        if d_price:
                            with self.lock:
                                self.live_prices[d_ticker] = d_price
                    except Exception:
                        pass
                d_shares = bal_stocks.get(d_ticker, 0)
                                                                
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

                                                                        
            recent_logs = list(self.logs)[-30:]

                                          
            _held_sat_tickers = {t for t, p in self.satellite_positions.items() if p.shares > 0}
            sat_info_snapshot = []
            for c in self.satellite_info:
                if c.get('ticker') in _held_sat_tickers:
                    continue
                sat_info_snapshot.append({
                    "ticker":      c.get("ticker", ""),
                    "name":        c.get("name", ""),
                    "sector":      c.get("sector", "-"),
                    "momentum_20d": float(c.get("momentum_20d", c.get("return_pct", 0))),
                    "rsi":         c.get("rsi"),
                    "vol_ratio":   float(c.get("vol_ratio", c.get("volume_surge", 1.0))),
                    "frgn_inst":   bool(c.get("frgn_inst", False)),
                    "frgn_only":   bool(c.get("frgn_only", False)),
                    "pos_52w":     c.get("pos_52w"),
                    "dl_prob":     float(c.get("dl_prob", 0)),
                    "ai_reason":   c.get("ai_reason", ""),
                    "current_price": int(c.get("current_price", 0)),
                    "screened_at": c.get("screened_at", ""),
                })
                if len(sat_info_snapshot) >= 5:
                    break

                                  
            _today_str = _now_kst().strftime('%Y-%m-%d')
            pnl_today = float(self.daily_pnl.get(_today_str, 0.0)) if hasattr(self, 'daily_pnl') else 0.0

            return {"is_running": self.is_running, "is_mock": self._is_mock, "has_keys": self.toss is not None, "logs": recent_logs, "hot_sectors": self.hot_sectors, "num_satellites": self.num_satellites, "cores": cores_data, "satellites": satellites, "satellite_info": sat_info_snapshot, "momentum_list": momentum_list, "defensive_list": defensive_list, "market_regime": self.market_regime, "mock_total_asset": mock_total_asset, "mock_pnl": mock_pnl, "mock_pnl_rt": mock_pnl_rt, "initial_cash": current_initial_cash, "available_cash": available_cash, "pnl_today": pnl_today}
        except Exception as critical_e:
            return {"is_running": False, "is_mock": self._is_mock, "has_keys": False, "logs": [{"time": "Error", "message": f"오류: {str(critical_e)}"}], "hot_sectors": [], "num_satellites": self.num_satellites, "cores": [], "satellites": [], "momentum_list": [], "mock_total_asset": 0, "mock_pnl": 0, "mock_pnl_rt": 0, "initial_cash": 10000000}