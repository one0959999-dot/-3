"""
시너지 백테스트 — 봇 8단계 국면(W) + 지금까지 쓴 신호(골든/200/모멘)를 전 조합 비교.
4신호 AND(모두 보유신호여야 보유, 아니면 현금)의 모든 경우의 수(15개)를 한 화면에.
W=봇8단계(KOSPI 8단계 ~bear, 단독이 +18.7% 재현)
B=골든크로스(코스닥 MA50>=MA200) C=200일선(코스닥) D=듀얼모멘텀(코스닥 12개월+)
대상: 에스앤에스텍(101490). 데이터: pykrx + yfinance. 조건 기존과 동일.
실행: python3 KR/synergy_backtest.py
"""
import itertools
import pandas as pd
import numpy as np
import yfinance as yf
from pykrx import stock

TICKER = "101490"
KOSPI = "^KS11"
KOSDAQ = "^KQ11"
VIXSYM = "^VIX"
WARMUP = "2020-06-01"
START = "20211101"
END = "20221231"
INIT_CASH = 10_000_000
FEE = 0.00015
TAX = 0.0018
SLIPPAGE = 0.001


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


def kosdaq_signals():
    kq = yf_ohlc(KOSDAQ)["Close"]
    return {
        "B": (kq.rolling(50).mean() >= kq.rolling(200).mean()),
        "C": (kq >= kq.rolling(200).mean()),
        "D": (kq / kq.shift(252) - 1 > 0),
    }


def buy_px(p):
    return p * (1 + SLIPPAGE) * (1 + FEE)


def sell_px(p):
    return p * (1 - SLIPPAGE) * (1 - FEE - TAX)


def mdd(eq):
    peak = eq.cummax()
    return ((eq - peak) / peak).min() * 100


def run2(px, ok):
    cash = INIT_CASH
    sh = 0.0
    tr = 0
    held = 0
    eq = []
    for d in px.index:
        p = px.loc[d]
        h = bool(ok.loc[d])
        if sh == 0 and h:
            bp = buy_px(p)
            sh = cash // bp
            cash -= sh * bp
            tr += 1
        elif sh > 0 and not h:
            cash += sh * sell_px(p)
            sh = 0.0
            tr += 1
        if sh > 0:
            held += 1
        eq.append(cash + sh * p)
    e = pd.Series(eq, index=px.index)
    return (e.iloc[-1] / INIT_CASH - 1) * 100, mdd(e), tr, 100.0 * held / len(px)


def main():
    spx = stock_close(TICKER)
    bear = kospi_is_bear()
    ksig = kosdaq_signals()
    df = pd.DataFrame({"px": spx})
    df["W"] = (~bear).reindex(df.index).ffill().fillna(False).astype(bool)
    for k, v in ksig.items():
        df[k] = v.reindex(df.index).ffill().fillna(False).astype(bool)
    df = df.dropna(subset=["px"])
    keys = ["W", "B", "C", "D"]
    rows = []
    for r in range(1, len(keys) + 1):
        for combo in itertools.combinations(keys, r):
            ok = df[list(combo)].all(axis=1)
            ret, m, tr, hp = run2(df["px"], ok)
            rows.append(("".join(combo), ret, m, hp))
    rows.sort(key=lambda x: x[1], reverse=True)
    bh = run2(df["px"], pd.Series(True, index=df.index))
    print("W=봇8 B=골든 C=200 D=모멘 (선택신호 AND=보유)")
    print("조합  수익% MDD 보유%")
    for name, ret, m, hp in rows:
        print("{:<5}{:+5.0f}% {:4.0f} {:3.0f}%".format(name, ret, m, hp))
    print("[기준] 단순보유 {:+.0f}%".format(bh[0]))


if __name__ == "__main__":
    main()

