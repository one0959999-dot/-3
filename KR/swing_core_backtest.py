"""
스윙 코어 백테스트
단순보유(Buy&Hold) vs 풀스윙(멀티신호 전량매매) 비교
데이터: pykrx (무료, API키 불필요)
실행: python3 KR/swing_core_backtest.py
"""
import sys
import pandas as pd
from pykrx import stock

# ===== 설정 (여기만 바꾸면 됨) =====
TICKER = "005930"        # 코어 종목코드 (예: 005930 삼성전자)
START = "20230101"
END = "20251231"
INIT_CASH = 10_000_000   # 초기 자금(원)

# 거래비용 (한국 시장)
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


def run_swing(df):
    cash = INIT_CASH
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
    print("종목 {} | {}~{} | 초기자금 {:,}원".format(TICKER, START, END, INIT_CASH))
    df = add_indicators(load_prices(TICKER, START, END)).dropna()
    if df.empty:
        sys.exit("기간 내 데이터가 부족합니다.")
    bh = run_buyhold(df)
    sw = run_swing(df)
    report("단순보유 Buy&Hold", bh[0], bh[1], bh[2])
    report("풀스윙 멀티신호", sw[0], sw[1], sw[2])
    diff = sw[0].iloc[-1] - bh[0].iloc[-1]
    print("")
    print(">>> 스윙 - 단순보유 차이: {:+,.0f}원".format(diff))
    print(">>> 양수면 스윙 승, 음수면 단순보유 승")


if __name__ == "__main__":
    main()
