"""
봇 8단계 강건성 검증 — 다른 종목에서도 봇 헤지가 단순보유를 이기나.
봇헤지 = KOSPI 8단계 ~bear (베어면 현금). 하락장 2021-11~2022-12. 조건 동일.
종목: 에스앤에스텍(101490, 기준) + 삼천당제약(000250).
두 종목 다 봇이 보유를 이기면 = 진짜 실력. 한쪽만 = 운빨 의심.
실행: python3 KR/robustness_backtest.py
"""
import pandas as pd
import numpy as np
import yfinance as yf
from pykrx import stock

KOSPI = "^KS11"
VIXSYM = "^VIX"
WARMUP = "2020-06-01"
START = "20211101"
END = "20221231"
INIT = 10_000_000
FEE = 0.00015
TAX = 0.0018
SLIP = 0.001
TICKERS = {"에스앤에스텍 101490": "101490", "삼천당제약 000250": "000250"}


def yf_ohlc(sym):
    e = "{}-{}-{}".format(END[:4], END[4:6], END[6:])
    df = yf.download(sym, start=WARMUP, end=e, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df.index = pd.to_datetime(df.index)
    return df


def stock_close(code):
    df = stock.get_market_ohlcv_by_date(START, END, code)
    s = df["종가"].astype(float)
    s.index = pd.to_datetime(s.index)
    return s


def adx14(high, low, close):
    up = high.diff()
    down = -low.diff()
    plus_dm = (((up > down) & (up > 0)) * up.clip(lower=0))
    minus_dm = (((down > up) & (down > 0)) * down.clip(lower=0))
    tr = pd.concat([(high - low),
                    (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / 14, adjust=False).mean()
    pdi = 100 * plus_dm.ewm(alpha=1.0 / 14, adjust=False).mean() / atr
    mdi = 100 * minus_dm.ewm(alpha=1.0 / 14, adjust=False).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1.0 / 14, adjust=False).mean()


def kospi_is_bear():
    k = yf_ohlc(KOSPI)
    c = k["Close"]
    ma200 = c.rolling(200).mean()
    mom20 = (c / c.shift(20) - 1) * 100
    mom60 = (c / c.shift(60) - 1) * 100
    adx = adx14(k["High"], k["Low"], c)
    try:
        v = yf_ohlc(VIXSYM)["Close"].reindex(c.index).ffill()
    except Exception:
        v = pd.Series(0.0, index=c.index)
    b1 = (v > 40) & (mom20 < -8)
    b2 = (c < ma200 * 0.92) & (mom20 < -5)
    b3 = (c < ma200) & (mom60 < -15)
    b4 = (c < ma200) & (mom20 < -3)
    b5 = (c < ma200) & (adx < 18)
    return (b1 | b2 | b3 | b4 | b5).fillna(False)


def buy_px(p):
    return p * (1 + SLIP) * (1 + FEE)


def sell_px(p):
    return p * (1 - SLIP) * (1 - FEE - TAX)


def mdd(eq):
    peak = eq.cummax()
    return ((eq - peak) / peak).min() * 100


def run2(px, hold):
    cash = INIT
    sh = 0.0
    tr = 0
    eq = []
    for d in px.index:
        p = px.loc[d]
        h = bool(hold.loc[d])
        if sh == 0 and h:
            bp = buy_px(p)
            sh = cash // bp
            cash -= sh * bp
            tr += 1
        elif sh > 0 and not h:
            cash += sh * sell_px(p)
            sh = 0.0
            tr += 1
        eq.append(cash + sh * p)
    e = pd.Series(eq, index=px.index)
    return (e.iloc[-1] / INIT - 1) * 100, mdd(e), tr


def main():
    bear = kospi_is_bear()
    print("봇 8단계 강건성 / 하락장 21.11-22.12 (수익% / MDD%)")
    for name, code in TICKERS.items():
        spx = stock_close(code)
        b = bear.reindex(spx.index).ffill().fillna(False).astype(bool)
        bh = run2(spx, pd.Series(True, index=spx.index))
        bot = run2(spx, ~b)
        print("")
        print("[" + name + "]")
        print(" 단순보유 {:+.0f}% / MDD {:.0f}%".format(bh[0], bh[1]))
        print(" 봇 헤지  {:+.0f}% / MDD {:.0f}% (매매 {})".format(bot[0], bot[1], bot[2]))
        print(" → " + ("봇 승" if bot[0] > bh[0] else "보유 승"))


if __name__ == "__main__":
    main()

