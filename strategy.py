"""
strategy.py
코어-위성 전략의 매매 신호 및 포지션 상태를 관리합니다.
- 코어: 보령(003850) 장기 보유 (플로어 물량 제외 익절)
- 위성: 종목별 최적화 지표 기반 (하락장 과매도 맹목적 매수 금지, 반등 확인 매수 적용)
- 재투자: 위성 수익 실현 시 수익금의 50%로 보령 추가 매수
"""

from pykrx import stock
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

CORE_TICKER        = "003850"
CORE_NAME          = "보령"
REINVEST_RATIO     = 0.50   # 위성 수익 중 보령 재투자 비율
CORE_MIN_FLOOR_RATIO = 0.30  # 매도 후에도 초기 보유량의 최소 30%는 항상 유지
RSI_PERIOD         = 9
RSI_OVERSOLD       = 30
RSI_OVERBOUGHT     = 70


def calc_rsi(series, period=RSI_PERIOD):
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    return 100 - 100 / (1 + gain / (loss + 1e-10))


def get_recent_prices(ticker, kis_api=None, days=30):
    """최근 N일 종가 Series 반환 (KIS API 사용)"""
    if kis_api is None:
        import pandas as pd
        return pd.Series(dtype=float)
        
    df = kis_api.get_ohlcv(ticker, "D")
    if df.empty or 'close' not in df.columns:
        import pandas as pd
        return pd.Series(dtype=float)
        
    return df['close'].dropna().tail(days)


def get_rsi_signal(ticker, kis_api=None, df=None):
    """
    RSI(9) 기반 현재 매매 신호 반환 (떨어지는 칼날 방지 및 캐시 데이터프레임 우선 연동)
    Returns: ('BUY' | 'SELL' | 'HOLD', current_price, rsi_value)
    """
    if df is not None and not df.empty:
        prices = df['close'].dropna().tail(30)
    else:
        prices = get_recent_prices(ticker, kis_api, days=30)
        
    if len(prices) < RSI_PERIOD + 2:
        return 'HOLD', 0, 0

    rsi_series  = calc_rsi(prices)
    current_rsi = rsi_series.iloc[-1]
    prev_rsi    = rsi_series.iloc[-2]
    price       = int(prices.iloc[-1])

    # 🟢 무릎 매수: 이전엔 30 밑이었는데, 지금 30을 위로 돌파(골든크로스)할 때만 매수
    if prev_rsi < RSI_OVERSOLD and current_rsi >= RSI_OVERSOLD:
        return 'BUY', price, current_rsi
    # 🔴 어깨 매도: 이전엔 70 위였는데, 지금 70을 아래로 깰(데드크로스) 때만 매도
    elif prev_rsi > RSI_OVERBOUGHT and current_rsi <= RSI_OVERBOUGHT:
        return 'SELL', price, current_rsi

    return 'HOLD', price, current_rsi


def get_signal_by_strategy(ticker, strategy_name, kis_api=None, df=None):
    """
    전략 이름에 따라 실시간 매매 신호 생성 (로컬 패치 캐시 및 KIS 하이브리드 지원)
    Returns: ('BUY' | 'SELL' | 'HOLD', price, indicator_value)
    """
    if kis_api is None and df is None:
        return 'HOLD', 0, 0

    # 주입된 캐시 장부가 없다면 백업용으로 KIS API 직접 호출
    if df is None or df.empty:
        df = kis_api.get_ohlcv(ticker, "D")
    
    # 외부 데이터 연동 시 대소문자 불일치(KeyError) 방지 방어 코드 추가
    if df is not None and not df.empty:
        df.columns = [str(c).lower() for c in df.columns]
    
    if df.empty or 'close' not in df.columns:
        return 'HOLD', 0, 0
        
    df = df.dropna(subset=['close'])

    if len(df) < 25:
        return 'HOLD', 0, 0

    c = df['close']
    h = df['high']
    l = df['low']
    price = int(c.iloc[-1])

    def _ema(s, n): return s.ewm(span=n, adjust=False).mean()
    def _sma(s, n): return s.rolling(n).mean()
    def _rsi(s, p):
        d = s.diff()
        g = d.clip(lower=0).rolling(p).mean()
        lo = (-d.clip(upper=0)).rolling(p).mean()
        return 100 - 100 / (1 + g / (lo + 1e-10))
    def _cross(fast, slow):
        now_above  = fast.iloc[-1]  > slow.iloc[-1]
        prev_above = fast.iloc[-2]  > slow.iloc[-2]
        if now_above and not prev_above: return 'BUY'
        if not now_above and prev_above: return 'SELL'
        return 'HOLD'
        
    def _thresh(ind, lo, hi):
        """임계값 돌파(반등/꺾임) 확인 로직"""
        cur, prev = ind.iloc[-1], ind.iloc[-2]
        if prev < lo and cur >= lo: return 'BUY', cur
        if prev > hi and cur <= hi: return 'SELL', cur
        return 'HOLD', cur

    try:
        sn = strategy_name
        if "RSI(9)" in sn:
            sig, val = _thresh(_rsi(c, 9), 30, 70)
            return sig, price, val
        elif "RSI(14) 30" in sn:
            sig, val = _thresh(_rsi(c, 14), 30, 70)
            return sig, price, val
        elif "RSI(14) 40" in sn:
            sig, val = _thresh(_rsi(c, 14), 40, 60)
            return sig, price, val
        elif "EMA 5/20" in sn:
            return _cross(_ema(c, 5), _ema(c, 20)), price, _ema(c, 5).iloc[-1]
        elif "EMA 3/10" in sn:
            return _cross(_ema(c, 3), _ema(c, 10)), price, _ema(c, 3).iloc[-1]
        elif "SMA 5/20" in sn:
            return _cross(_sma(c, 5), _sma(c, 20)), price, _sma(c, 5).iloc[-1]
        elif "SMA 3/10" in sn:
            return _cross(_sma(c, 3), _sma(c, 10)), price, _sma(c, 3).iloc[-1]
        elif "SMA 3/20" in sn:
            return _cross(_sma(c, 3), _sma(c, 20)), price, _sma(c, 3).iloc[-1]
        elif "MACD" in sn:
            m = _ema(c, 12) - _ema(c, 26); ms = _ema(m, 9)
            return _cross(m, ms), price, m.iloc[-1]
        elif "볼린저" in sn:
            mid = _sma(c, 20); sd = c.rolling(20).std()
            lower = mid - (2 * sd)
            upper = mid + (2 * sd)
            
            prev_c, cur_c = c.iloc[-2], c.iloc[-1]
            prev_l, cur_l = lower.iloc[-2], lower.iloc[-1]
            prev_u, cur_u = upper.iloc[-2], upper.iloc[-1]
            
            if prev_c < prev_l and cur_c >= cur_l: return 'BUY', price, cur_c 
            if prev_c > prev_u and cur_c <= cur_u: return 'SELL', price, cur_c 
            return 'HOLD', price, cur_c
        elif "Stochastic" in sn:
            lo_r = l.rolling(14).min(); hi_r = h.rolling(14).max()
            k = 100*(c-lo_r)/(hi_r-lo_r+1e-10); d = k.rolling(3).mean()
            return _cross(k, d), price, k.iloc[-1]
        elif "CCI" in sn:
            tp = (h+l+c)/3; ma = _sma(tp, 20)
            md = tp.rolling(20).apply(lambda x: np.mean(np.abs(x-x.mean())), raw=True)
            cci_v = (tp-ma)/(0.015*md+1e-10)
            sig, val = _thresh(cci_v, -100, 100)
            return sig, price, val
        elif "Williams" in sn:
            wr = -100*(h.rolling(14).max()-c)/(h.rolling(14).max()-l.rolling(14).min()+1e-10)
            sig, val = _thresh(wr, -80, -20)
            return sig, price, val
        else:
            return get_rsi_signal(ticker, df=df)
    except Exception as e:
        print(f"[{ticker}] {strategy_name} 전략 에러: {e}")
        return 'HOLD', price, 0


class Position:
    """개별 종목 포지션 상태 관리 (실전 거래세 및 매매 수수료 완벽 시뮬레이션 적용)"""
    def __init__(self, ticker, name, budget):
        self.ticker    = ticker
        self.name      = name
        self.budget    = budget      # 배정 자금
        self.initial_cash = budget
        self.cash      = budget      # 가용 현금
        self.shares    = 0           # 보유 주식 수
        self.avg_price = 0           # 평균 매수가
        self.trades    = []          # 거래 기록
        self.max_price = 0
        self.order_pending = False   # (기존 유지)
        self.last_order_time = 0.0   # 🟢 [최종보완] 10분 절대 쿨타임 측정용 타임스탬프
        
        # 한국 금융시장 표준 수수료 및 거래세율 정의
        self.fee_rate = 0.00015      # 실전 및 모의 온라인 매매 수수료 기본율 (0.015%)
        self.tax_rate = 0.0018       # 장내 매도 시 국가 증권거래세율 (0.18%)

    def buy(self, price, all_in=True):
        if self.shares > 0 or self.cash < price:
            return 0
            
        # 수수료 비용 및 시장가(최유리지정가) 매수 시 증거금 버퍼(1%)를 반영하여 예수금 펑크를 방지합니다.
        qty = int((self.cash * 0.99) // (price * (1 + self.fee_rate)))
        if qty == 0:
            return 0
            
        stock_cost = qty * price
        brokerage_fee = round(stock_cost * self.fee_rate, 2)
        total_cost = stock_cost + brokerage_fee
        
        self.shares    = qty
        self.avg_price = round(total_cost / qty, 2) # 수수료가 포함된 정밀한 실전 평단가 산출
        self.cash     -= total_cost
        
        self.trades.append({
            'type': 'BUY', 'price': price, 'qty': qty, 
            'fee': brokerage_fee, 'tax': 0.0, 'time': datetime.now()
        })
        return qty

    def sell(self, price):
        if self.shares == 0:
            return 0, 0
            
        gross_revenue = self.shares * price
        brokerage_fee = round(gross_revenue * self.fee_rate, 2)
        trading_tax   = round(gross_revenue * self.tax_rate, 2)
        total_deduction = brokerage_fee + trading_tax
        net_revenue   = gross_revenue - total_deduction # 수수료와 세금이 원천징수된 실제 인출 가능 현금
        
        profit  = net_revenue - (self.avg_price * self.shares)
        self.cash  += net_revenue
        qty         = self.shares
        
        self.shares = 0
        self.avg_price = 0
        
        self.trades.append({
            'type': 'SELL', 'price': price, 'qty': qty, 
            'fee': brokerage_fee, 'tax': trading_tax, 'profit': profit, 'time': datetime.now()
        })
        return qty, profit

    @property
    def current_value(self):
        return self.cash + (self.shares * self.avg_price if self.shares > 0 else 0)


class CorePosition:
    """코어 포지션 - RSI 신호 기반 트레이딩, 단 최소 수량(Floor)은 항상 유지 (수수료 모델 이식 완료)"""
    def __init__(self, ticker, name, initial_cash):
        self.ticker      = ticker
        self.name        = name
        self.shares      = 0
        self.floor_shares = 0     # 절대 팔지 않을 최소 수량
        self.avg_price   = 0
        self.initial_cash = initial_cash
        self.cash        = initial_cash
        self.buy_log     = []
        self.sell_log    = []
        self.order_pending = False # 🟢 중복 주문 방지용 락 플래그 추가
        
        self.fee_rate = 0.00015   # 수수료율 (0.015%)
        self.tax_rate = 0.0018    # 거래세율 (0.18%)

    def buy(self, price, cash_to_use=None):
        """매수 (cash_to_use 미지정 시 전액 매수)"""
        budget = cash_to_use if cash_to_use else self.cash
        available_budget = min(budget, self.cash)
        
        # 수수료 비용 및 시장가(최유리지정가) 매수 시 증거금 버퍼(1%)를 반영하여 예수금 펑크를 방지합니다.
        qty = int((available_budget * 0.99) // (price * (1 + self.fee_rate)))
        if qty == 0:
            return 0
            
        stock_cost = qty * price
        brokerage_fee = round(stock_cost * self.fee_rate, 2)
        total_cost = stock_cost + brokerage_fee
        
        current_total_investment = self.avg_price * self.shares + total_cost
        self.shares    += qty
        self.avg_price  = round(current_total_investment / self.shares, 2)
        self.cash      -= total_cost
        
        if self.floor_shares == 0:
            self.floor_shares = max(1, int(self.shares * CORE_MIN_FLOOR_RATIO))
            
        self.buy_log.append({
            'time': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'price': price, 'qty': qty,
            'total_shares': self.shares,
            'fee': brokerage_fee,
            'reason': 'initial' if len(self.buy_log) == 0 else 'reinvest'
        })
        return qty

    def sell(self, price):
        """매도 - floor(최소 수량) 이상의 수량만 매도"""
        sellable = self.shares - self.floor_shares
        if sellable <= 0:
            return 0, 0
            
        gross_revenue = sellable * price
        brokerage_fee = round(gross_revenue * self.fee_rate, 2)
        trading_tax   = round(gross_revenue * self.tax_rate, 2)
        net_revenue   = gross_revenue - (brokerage_fee + trading_tax)
        
        profit  = net_revenue - (self.avg_price * sellable)
        self.cash   += net_revenue
        self.shares -= sellable
        
        self.sell_log.append({
            'time': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'price': price, 'qty': sellable, 'profit': profit,
            'fee': brokerage_fee, 'tax': trading_tax, 'remaining': self.shares
        })
        return sellable, profit

    def current_value(self, current_price):
        return self.shares * current_price + self.cash