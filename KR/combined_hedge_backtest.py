"""
결합 백테스트 — 봇 국면 헤지(8단계, KOSPI) + 시장방어(코스닥 20일선) 병합 효과 검증.
4가지 비교: 단순보유 / 봇헤지 / 시장방어 / 병합.
- 봇헤지 모델: market_phase.py의 8단계 분류를 KOSPI(^KS11)+VIX+ADX로 재현해 BEAR 판정,
  동작은 'BEAR면 현금화'로 단순화(부분손절/코어floor/킬스위치 세부는 단일종목용 근사 생략).
- 워밍업: 지수는 200일선 계산 위해 2020-06부터 받아옴.
대상: 에스앤에스텍(101490, 코스닥). 데이터: pykrx + yfinance.
실행: python3 KR/combined_hedge_backtest.py
"""
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
MA_N = 20

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
    atr = tr.ewm(alpha=1.0/14, adjust=False).mean()
    pdi = 100 * plus_dm.ewm(alpha=1.0/14, adjust=False).mean() / atr
    mdi = 100 * minus_dm.ewm(alpha=1.0/14, adjust=False).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1.0/14, adjust=False).mean()


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
    return p * (1 + SLIPPAGE) * (1 + FEE)


def sell_px(p):
    return p * (1 - SLIPPAGE) * (1 - FEE - TAX)


def mdd(eq):
    peak = eq.cummax()
    return ((eq - peak) / peak).min() * 100


def run(px, hold_ok):
    cash = INIT_CASH
    sh = 0.0
    tr = 0
    eq = []
    for d in px.index:
        p = px.loc[d]
        ok = bool(hold_ok.loc[d])
        if sh == 0 and ok:
            bp = buy_px(p)
            sh = cash // bp
            cash -= sh * bp
            tr += 1
        elif sh > 0 and not ok:
            cash += sh * sell_px(p)
            sh = 0.0
            tr += 1
        eq.append(cash + sh * p)
    return pd.Series(eq, index=px.index), sh, tr


def run_hold(px):
    p0 = buy_px(px.iloc[0])
    sh = INIT_CASH // p0
    cash = INIT_CASH - sh * p0
    return cash + sh * px, sh, 0


def report(name, eq, sh, tr):
    f = eq.iloc[-1]
    print("")
    print("[" + name + "]")
    print("  평가액 : {:,.0f}원".format(f))
    print("  수익률 : {:+.1f}%".format((f / INIT_CASH - 1) * 100))
    print("  MDD   : {:.1f}%".format(mdd(eq)))
    print("  매매   : {}".format(tr))
    print("  보유주 : {:,.0f}".format(sh))


def main():
    spx = stock_close(TICKER)
    bear = kospi_is_bear()
    kq = yf_ohlc(KOSDAQ)["Close"]
    kqok_all = (kq >= kq.rolling(MA_N).mean())
    df = pd.DataFrame({"px": spx})
    df["bear"] = bear.reindex(df.index).ffill().fillna(False).astype(bool)
    df["kqok"] = kqok_all.reindex(df.index).ffill().fillna(False).astype(bool)
    df = df.dropna(subset=["px"])
    print("종목 {} | {}~{} | 봇국면(KOSPI 8단계)+시장방어(KOSDAQ 20MA)".format(TICKER, START, END))
    bh = run_hold(df["px"])
    bot = run(df["px"], ~df["bear"])
    mkt = run(df["px"], df["kqok"])
    comb = run(df["px"], (~df["bear"]) & df["kqok"])
    report("단순보유", bh[0], bh[1], bh[2])
    report("봇 국면 헤지", bot[0], bot[1], bot[2])
    report("시장방어(코스닥20MA)", mkt[0], mkt[1], mkt[2])
    report("병합(봇헤지+시장방어)", comb[0], comb[1], comb[2])


if __name__ == "__main__":
    main()
