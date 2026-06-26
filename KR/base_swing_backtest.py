"""
베이스+스윙 코어 백테스트
3가지 비교: 단순보유 / 풀스윙 / 베이스+스윙
- 베이스+스윙: 자금 일부(BASE_RATIO)는 안 팔고 보유(상승 챙김),
  나머지는 멀티신호로 능동 스윙
데이터: pykrx (무료). 실행: python3 KR/base_swing_backtest.py
"""
import sys
import pandas as pd
from pykrx import stock

# ===== 설정 (여기만 바꾸면 됨) =====
TICKER = "005930"        # 코어 종목코드
START = "20230101"
END = "20251231"
INIT_CASH = 10_000_000   # 초기 자금(원)
BASE_RATIO = 0.7         # 베이스 비중 (0.7 = 70% 보유 / 30% 스윙)

# 거래비용
FEE = 0.00015
TAX = 0.0018
SLIPPAGE = 0.001

# 신호 파라미터
RSI_N = 14
RSI_BUY, RSI_SELL = 35, 65
BB_N = 20
MA_FAST, MA_SLOW = 5, 20


def load_prices(ticker, start, end):
    df = stock.get_market_ohlcv_by_date(start, end, ticker)
    if df is None or df.empty:
        sys.exit("데이터 없음: " + ticker)
    df = df.rename(columns={"시가": "open", "고가": "high",
                            "저가": "low", "종가": "close", "거래량": "volume"})
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def add_indicators(df):
    c = df["close"]
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(RSI_N).mean()
    loss = (-delta.clip(upper=0)).rolling(RSI_N).mean()
    rs = gain / loss.replace(0, 1e-9)
    df["rsi"] = 100 - 100 / (1 + rs)
    ma = c.rolling(BB_N).mean()
    sd = c.rolling(BB_N).std()
    df["bb_up"] = ma + 2 * sd
    df["bb_dn"] = ma - 2 * sd
    df["ma_fast"] = c.rolling(MA_FAST).mean()
    df["ma_slow"] = c.rolling(MA_SLOW).mean()
    ema12 = c.ewm(span=12).mean()
    ema26 = c.ewm(span=26).mean()
    df["macd"] = ema12 - ema26
    df["macd_sig"] = df["macd"].ewm(span=9).mean()
    return df


def buy_votes(r):
    v = 0
    if r.rsi < RSI_BUY:
        v += 1
    if r.close < r.bb_dn:
        v += 1
    if r.ma_fast > r.ma_slow:
        v += 1
    if r.macd > r.macd_sig:
        v += 1
    return v


def sell_votes(r):
    v = 0
    if r.rsi > RSI_SELL:
        v += 1
    if r.close > r.bb_up:
        v += 1
    if r.ma_fast < r.ma_slow:
        v += 1
    if r.macd < r.macd_sig:
        v += 1
    return v


def buy_px(p):
    return p * (1 + SLIPPAGE) * (1 + FEE)


def sell_px(p):
    return p * (1 - SLIPPAGE) * (1 - FEE - TAX)


def run_buyhold(df):
    p0 = buy_px(df["close"].iloc[0])
    shares = INIT_CASH // p0
    cash = INIT_CASH - shares * p0
    equity = cash + shares * df["close"]
    return equity, shares, 0


def run_swing(df, capital):
    cash = capital
    shares = 0.0
    trades = 0
    eq = []
    for r in df.itertuples():
        price = r.close
        if shares == 0 and buy_votes(r) >= 2:
            bp = buy_px(price)
            shares = cash // bp
            cash -= shares * bp
            trades += 1
        elif shares > 0 and sell_votes(r) >= 2:
            cash += shares * sell_px(price)
            shares = 0.0
            trades += 1
        eq.append(cash + shares * price)
    return pd.Series(eq, index=df.index), shares, trades


def run_base_swing(df):
    base_cap = INIT_CASH * BASE_RATIO
    swing_cap = INIT_CASH - base_cap
    p0 = buy_px(df["close"].iloc[0])
    base_shares = base_cap // p0
    base_cash = base_cap - base_shares * p0
    base_eq = base_cash + base_shares * df["close"]
    swing_eq, swing_shares, trades = run_swing(df, swing_cap)
    total_eq = base_eq + swing_eq
    return total_eq, base_shares + swing_shares, trades


def mdd(equity):
    peak = equity.cummax()
    return ((equity - peak) / peak).min() * 100


def report(name, equity, shares, trades):
    final = equity.iloc[-1]
    ret = (final / INIT_CASH - 1) * 100
    print("")
    print("[" + name + "]")
    print("  최종 평가액 : {:,.0f}원".format(final))
    print("  총 수익률   : {:+.1f}%".format(ret))
    print("  MDD        : {:.1f}%".format(mdd(equity)))
    print("  매매 횟수   : {}".format(trades))
    print("  최종 보유주 : {:,.0f}주".format(shares))


def main():
    print("종목 {} | {}~{} | 초기 {:,}원 | 베이스 {:.0%}".format(
        TICKER, START, END, INIT_CASH, BASE_RATIO))
    df = add_indicators(load_prices(TICKER, START, END)).dropna()
    if df.empty:
        sys.exit("기간 내 데이터가 부족합니다.")
    bh = run_buyhold(df)
    sw = run_swing(df, INIT_CASH)
    bs = run_base_swing(df)
    report("단순보유 Buy&Hold", bh[0], bh[1], bh[2])
    report("풀스윙 멀티신호", sw[0], sw[1], sw[2])
    report("베이스+스윙", bs[0], bs[1], bs[2])
    print("")
    print(">>> 베이스+스윙 - 단순보유: {:+,.0f}원".format(bs[0].iloc[-1] - bh[0].iloc[-1]))
    print(">>> 베이스+스윙 - 풀스윙:   {:+,.0f}원".format(bs[0].iloc[-1] - sw[0].iloc[-1]))


if __name__ == "__main__":
    main()
