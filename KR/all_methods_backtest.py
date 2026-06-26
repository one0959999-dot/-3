"""
전체 기법 백테스트 — 모든 주가 매매 기법을 같은 조건으로 단독 비교.
대상: 에스앤에스텍(101490) 자체 가격(High/Low/Close) 기준.
기간: 하락장 2021-11 ~ 2022-12. 워밍업 2019-01부터(200MA/12개월모멘텀).
수수료/세금/슬리피지: 기존 백테스트와 동일.
각 기법 = 보유(신호 ON)/현금(OFF) 타이밍 룰. 단독 성능을 한 표로 비교.
실행: python3 KR/all_methods_backtest.py
"""
import numpy as np
import pandas as pd
from pykrx import stock

TICKER = "101490"
WARMUP = "20190101"
START = "20211101"
END = "20221231"
INIT = 10_000_000
FEE = 0.00015
TAX = 0.0018
SLIP = 0.001


def load():
    df = stock.get_market_ohlcv_by_date(WARMUP, END, TICKER)
    df = df.rename(columns={"시가": "O", "고가": "H", "저가": "L", "종가": "C", "거래량": "V"})
    df.index = pd.to_datetime(df.index)
    return df[["O", "H", "L", "C", "V"]].astype(float)


def buy_px(p):
    return p * (1 + SLIP) * (1 + FEE)


def sell_px(p):
    return p * (1 - SLIP) * (1 - FEE - TAX)


def mdd(eq):
    pk = eq.cummax()
    return ((eq - pk) / pk).min() * 100


def run(px, hold):
    cash = INIT
    sh = 0.0
    tr = 0
    held = 0
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
        if sh > 0:
            held += 1
        eq.append(cash + sh * p)
    e = pd.Series(eq, index=px.index)
    return (e.iloc[-1] / INIT - 1) * 100, mdd(e), tr, 100.0 * held / len(px)


def sma(s, n):
    return s.rolling(n).mean()


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0)
    dn = -d.clip(upper=0)
    rs = up.ewm(alpha=1 / n, adjust=False).mean() / dn.ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + rs)


def atr(df, n=14):
    h, l, c = df.H, df.L, df.C
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def adx(df, n=14):
    h, l, c = df.H, df.L, df.C
    up = h.diff()
    dn = -l.diff()
    plus = np.where((up > dn) & (up > 0), up, 0.0)
    minus = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    a = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * pd.Series(plus, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / a
    mdi = 100 * pd.Series(minus, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / a
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean(), pdi, mdi


def stoch(df, n=14):
    ll = df.L.rolling(n).min()
    hh = df.H.rolling(n).max()
    return 100 * (df.C - ll) / (hh - ll).replace(0, np.nan)


def aroon(df, n=25):
    up = df.H.rolling(n + 1).apply(lambda x: np.argmax(x) / n * 100, raw=True)
    dn = df.L.rolling(n + 1).apply(lambda x: np.argmin(x) / n * 100, raw=True)
    return up, dn


def ichimoku(df):
    conv = (df.H.rolling(9).max() + df.L.rolling(9).min()) / 2
    base = (df.H.rolling(26).max() + df.L.rolling(26).min()) / 2
    spanA = ((conv + base) / 2).shift(26)
    spanB = ((df.H.rolling(52).max() + df.L.rolling(52).min()) / 2).shift(26)
    return spanA, spanB


def psar(df, af0=0.02, step=0.02, afmax=0.2):
    h = df.H.values
    l = df.L.values
    n = len(h)
    out = np.zeros(n)
    bull = True
    af = af0
    ep = h[0]
    out[0] = l[0]
    for i in range(1, n):
        prior = out[i - 1]
        cur = prior + af * (ep - prior)
        if bull:
            cur = min(cur, l[i - 1], l[i - 2] if i >= 2 else l[i - 1])
            if l[i] < cur:
                bull = False
                cur = ep
                ep = l[i]
                af = af0
            elif h[i] > ep:
                ep = h[i]
                af = min(af + step, afmax)
        else:
            cur = max(cur, h[i - 1], h[i - 2] if i >= 2 else h[i - 1])
            if h[i] > cur:
                bull = True
                cur = ep
                ep = h[i]
                af = af0
            elif l[i] < ep:
                ep = l[i]
                af = min(af + step, afmax)
        out[i] = cur
    return pd.Series(out, index=df.index)


def supertrend(df, n=10, mult=3.0):
    a = atr(df, n)
    hl2 = (df.H + df.L) / 2
    ub = (hl2 + mult * a).values
    lb = (hl2 - mult * a).values
    c = df.C.values
    fub = ub.copy()
    flb = lb.copy()
    for i in range(1, len(c)):
        fub[i] = ub[i] if (ub[i] < fub[i - 1] or c[i - 1] > fub[i - 1]) else fub[i - 1]
        flb[i] = lb[i] if (lb[i] > flb[i - 1] or c[i - 1] < flb[i - 1]) else flb[i - 1]
    dirn = np.ones(len(c))
    st = fub.copy()
    for i in range(1, len(c)):
        if st[i - 1] == fub[i - 1]:
            if c[i] <= fub[i]:
                st[i] = fub[i]
                dirn[i] = -1
            else:
                st[i] = flb[i]
                dirn[i] = 1
        else:
            if c[i] >= flb[i]:
                st[i] = flb[i]
                dirn[i] = 1
            else:
                st[i] = fub[i]
                dirn[i] = -1
    return pd.Series(dirn, index=df.index)


def build_signals(df):
    C, H, L = df.C, df.H, df.L
    s20, s50, s60, s120, s200 = sma(C, 20), sma(C, 50), sma(C, 60), sma(C, 120), sma(C, 200)
    a22 = atr(df, 22)
    adxv, pdi, mdi = adx(df, 14)
    macd_line = ema(C, 12) - ema(C, 26)
    macd_sig = ema(macd_line, 9)
    au, ad = aroon(df, 25)
    spanA, spanB = ichimoku(df)
    return {
        "MA20": C > s20,
        "MA60": C > s60,
        "MA120": C > s120,
        "MA200": C > s200,
        "GC50": s50 > s200,
        "GC20": s20 > s60,
        "MOM12": C / C.shift(252) > 1,
        "MOM6": C / C.shift(126) > 1,
        "ROC20": C / C.shift(20) > 1,
        "RSI": rsi(C, 14) > 50,
        "STOCH": stoch(df, 14) > 50,
        "MACD": macd_line > macd_sig,
        "BOLL": C >= (s20 - 2 * C.rolling(20).std()),
        "DONC": C > (H.rolling(20).max() + L.rolling(20).min()) / 2,
        "KELT": C > ema(C, 20),
        "CHAND": C > (H.rolling(22).max() - 3 * a22),
        "STREND": supertrend(df, 10, 3.0) > 0,
        "ADX": (adxv > 20) & (pdi > mdi),
        "AROON": au > ad,
        "PSAR": C > psar(df),
        "ICHI": C > pd.concat([spanA, spanB], axis=1).max(axis=1),
        "HI52": C > 0.80 * C.rolling(252).max(),
    }


def main():
    df = load()
    sig = build_signals(df)
    m = (df.index >= pd.to_datetime(START)) & (df.index <= pd.to_datetime(END))
    px = df.C[m]
    rows = []
    rows.append(("BUYHOLD",) + run(px, pd.Series(True, index=px.index)))
    for name, s in sig.items():
        hold = s.reindex(px.index).fillna(False).astype(bool)
        rows.append((name,) + run(px, hold))
    rows.sort(key=lambda x: x[1], reverse=True)
    print("종목 101490 / 하락장 21.11-22.12 / 신호=종목자체")
    print("기법     수익% MDD  매매 보유%")
    for name, ret, md, tr, hp in rows:
        print("{:<7}{:+4.0f}% {:4.0f} {:3.0f} {:3.0f}%".format(name, ret, md, tr, hp))
    print("[기준] 봇8단계(헤지) +18.7% (별도 백테스트)")


if __name__ == "__main__":
    main()

