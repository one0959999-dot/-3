"""
신호 조합 전수 백테스트 — 모든 조합(AND)으로 보유/현금 결정.
어떤 조합이 작동하나 + 신호가 너무 빡빡한지(보유% 낮음) 한눈에.
신호(코스닥 지수 기준, 각각 'bullish=보유 신호'):
  A20=20일선위  B골크=골든크로스(50>=200)  C200=200일선위  D모멘=듀얼모멘텀(12개월+)
조합 = 선택 신호들 AND(모두 bullish여야 보유, 아니면 현금).
대상: 에스앤에스텍(101490). 데이터: pykrx + yfinance(코스닥 ^KQ11).
실행: python3 KR/signal_combo_backtest.py
"""
import itertools
import pandas as pd
import yfinance as yf
from pykrx import stock

TICKER = "101490"
INDEX_YF = "^KQ11"
WARMUP = "2019-06-01"
START = "20211101"
END = "20221231"
INIT_CASH = 10_000_000
FEE = 0.00015
TAX = 0.0018
SLIPPAGE = 0.001


def stock_close(code):
    df = stock.get_market_ohlcv_by_date(START, END, code)
    s = df["종가"].astype(float)
    s.index = pd.to_datetime(s.index)
    return s


def yf_close(sym):
    e = "{}-{}-{}".format(END[:4], END[4:6], END[6:])
    df = yf.download(sym, start=WARMUP, end=e, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    s = df["Close"].astype(float)
    s.index = pd.to_datetime(s.index)
    return s


def buy_px(p):
    return p * (1 + SLIPPAGE) * (1 + FEE)


def sell_px(p):
    return p * (1 - SLIPPAGE) * (1 - FEE - TAX)


def mdd(eq):
    peak = eq.cummax()
    return ((eq - peak) / peak).min() * 100


def run_filter(px, ok):
    cash = INIT_CASH
    sh = 0.0
    tr = 0
    held = 0
    eq = []
    for d in px.index:
        p = px.loc[d]
        hold = bool(ok.loc[d])
        if sh == 0 and hold:
            bp = buy_px(p)
            sh = cash // bp
            cash -= sh * bp
            tr += 1
        elif sh > 0 and not hold:
            cash += sh * sell_px(p)
            sh = 0.0
            tr += 1
        if sh > 0:
            held += 1
        eq.append(cash + sh * p)
    eqs = pd.Series(eq, index=px.index)
    return eqs.iloc[-1], mdd(eqs), tr, 100.0 * held / len(px)


def main():
    spx = stock_close(TICKER)
    idx = yf_close(INDEX_YF)
    sigs = {
        "A": (idx >= idx.rolling(20).mean()),
        "B": (idx.rolling(50).mean() >= idx.rolling(200).mean()),
        "C": (idx >= idx.rolling(200).mean()),
        "D": (idx / idx.shift(252) - 1 > 0),
    }
    df = pd.DataFrame({"px": spx})
    for k, v in sigs.items():
        df[k] = v.reindex(df.index).ffill().fillna(False).astype(bool)
    df = df.dropna(subset=["px"])
    keys = list(sigs.keys())
    rows = []
    for r in range(1, len(keys) + 1):
        for combo in itertools.combinations(keys, r):
            ok = df[list(combo)].all(axis=1)
            fin, m, tr, hp = run_filter(df["px"], ok)
            rows.append(("".join(combo), (fin / INIT_CASH - 1) * 100, m, hp))
    rows.sort(key=lambda x: x[1], reverse=True)
    print("A=20일선 B=골든 C=200일 D=모멘")
    print("조합  수익%  MDD  보유%")
    for name, ret, m, hp in rows:
        print("{:<5}{:+5.0f}% {:4.0f} {:3.0f}%".format(name, ret, m, hp))
    print("[기준] 단순보유 -28% / 봇8단계 +18.7%")


if __name__ == "__main__":
    main()
