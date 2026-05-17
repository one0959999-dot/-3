"""
strategy.py
코어-위성 전략의 매매 신호 및 포지션 상태를 관리합니다.
- 코어: 보령(003850) 장기 보유 (매도 없음)
- 위성: RSI(9) 30/70 신호 기반 매수/매도
- 재투자: 위성 수익 실현 시 수익금의 50%로 보령 추가 매수
"""

from pykrx import stock
from datetime import datetime, timedelta
import pandas as pd
import numpy as np  # 💡 [필수 추가] CCI 등 고급 수학 지표 계산을 위한 numpy 라이브러리 추가

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


def get_recent_prices(ticker, days=30):
    """최근 N일 종가 Series 반환 (pykrx 사용)"""
    end   = datetime.today()
    start = end - timedelta(days=days + 20)
    df = stock.get_market_ohlcv_by_date(
        start.strftime("%Y%m%d"),
        end.strftime("%Y%m%d"),
        ticker
    )
    return df['종가'].dropna().tail(days)


def get_rsi_signal(ticker):
    prices = get_recent_prices(ticker, days=30)
    if len(prices) < RSI_PERIOD + 2:
        return 'HOLD', 0, 0

    rsi_series  = calc_rsi(prices)
    current_rsi = rsi_series.iloc[-1]
    prev_rsi    = rsi_series.iloc[-2]
    price       = int(prices.iloc[-1])

    # [수정 후] 30 선을 아래에서 위로 뚫고 올라올 때만 매수!
    if prev_rsi < RSI_OVERSOLD and current_rsi >= RSI_OVERSOLD:
        return 'BUY', price, current_rsi
    elif prev_rsi > RSI_OVERBOUGHT and current_rsi <= RSI_OVERBOUGHT:
        return 'SELL', price, current_rsi
    
    # ❌ 아래 두 줄(떨어지는 칼날 매수, 맹목적 과매수 매도)은 삭제하거나 주석 처리합니다.
    # elif current_rsi < RSI_OVERSOLD:
    #     return 'BUY', price, current_rsi
    # elif current_rsi > RSI_OVERBOUGHT:
    #     return 'SELL', price, current_rsi

    return 'HOLD', price, current_rsi


def get_signal_by_strategy(ticker, strategy_name):
    """
    전략 이름에 따라 실시간 매매 신호 생성
    Returns: ('BUY' | 'SELL' | 'HOLD', price, indicator_value)
    """
    from pykrx import stock as krx_stock
    from datetime import timedelta

    end   = datetime.now()
    start = end - timedelta(days=90)
    df = krx_stock.get_market_ohlcv_by_date(
        start.strftime("%Y%m%d"),
        end.strftime("%Y%m%d"),
        ticker
    )
    df.rename(columns={'시가':'open','고가':'high','저가':'low','종가':'close','거래량':'volume'}, inplace=True)
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
        cur, prev = ind.iloc[-1], ind.iloc[-2]
        
        # [수정 후] 기준선을 아래에서 위로 돌파(반등)할 때만 매수, 위에서 아래로 깨질 때만 매도
        if prev < lo and cur >= lo: return 'BUY', cur
        if prev > hi and cur <= hi: return 'SELL', cur
        
        # ❌ 아래 두 줄 역시 과매도/과매수 구간에서 무조건 신호를 쏘므로 주석 처리합니다.
        # if cur < lo: return 'BUY', cur
        # if cur > hi: return 'SELL', cur
        
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
            ratio = (c / mid)
            sig, val = _thresh(ratio, 0.97, 1.03)
            return sig, price, val
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
            return get_rsi_signal(ticker)  # fallback
    except Exception as e:
        return 'HOLD', price, 0


class Position:
    """개별 종목 포지션 상태 관리"""
    def __init__(self, ticker, name, budget):
        self.ticker    = ticker
        self.name      = name
        self.budget    = budget      # 배정 자금
        self.initial_cash = budget
        self.cash      = budget      # 가용 현금
        self.shares    = 0           # 보유 주식 수
        self.avg_price = 0           # 평균 매수가
        self.trades    = []          # 거래 기록

    def buy(self, price, all_in=True):
        if self.shares > 0 or self.cash < price:
            return 0
        qty = int(self.cash // price)
        if qty == 0:
            return 0
        self.shares    = qty
        self.avg_price = price
        self.cash     -= qty * price
        self.trades.append({'type': 'BUY', 'price': price, 'qty': qty, 'time': datetime.now()})
        return qty

    def sell(self, price):
        if self.shares == 0:
            return 0, 0
        revenue = self.shares * price
        profit  = revenue - (self.avg_price * self.shares)
        self.cash  += revenue
        qty         = self.shares
        self.shares = 0
        self.avg_price = 0
        self.trades.append({'type': 'SELL', 'price': price, 'qty': qty, 'profit': profit, 'time': datetime.now()})
        return qty, profit

    @property
    def current_value(self):
        return self.cash + (self.shares * self.avg_price if self.shares > 0 else 0)


class CorePosition:
    """코어 포지션 - RSI 신호 기반 트레이딩, 단 최소 수량(Floor)은 항상 유지"""
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

    def buy(self, price, cash_to_use=None):
        """보령 매수 (cash_to_use 미지정 시 전액 매수)"""
        budget = cash_to_use if cash_to_use else self.cash
        qty = int(min(budget, self.cash) // price)
        if qty == 0:
            return 0
        cost = qty * price
        total_cost = self.avg_price * self.shares + cost
        self.shares    += qty
        self.avg_price  = total_cost / self.shares if self.shares > 0 else price
        self.cash      -= cost
        # floor 설정: 처음 매수한 수량의 30%는 영구 보존
        if self.floor_shares == 0:
            self.floor_shares = max(1, int(self.shares * CORE_MIN_FLOOR_RATIO))
        self.buy_log.append({
            'time': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'price': price, 'qty': qty,
            'total_shares': self.shares,
            'reason': 'initial' if len(self.buy_log) == 0 else 'reinvest'
        })
        return qty

    def sell(self, price):
        """보령 매도 - floor(최소 수량) 이상의 수량만 매도"""
        sellable = self.shares - self.floor_shares
        if sellable <= 0:
            return 0, 0
        revenue = sellable * price
        profit  = revenue - (self.avg_price * sellable)
        self.cash   += revenue
        self.shares -= sellable
        self.sell_log.append({
            'time': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'price': price, 'qty': sellable, 'profit': profit,
            'remaining': self.shares
        })
        return sellable, profit

    def current_value(self, current_price):
        return self.shares * current_price + self.cash
