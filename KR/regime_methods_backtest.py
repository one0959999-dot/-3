"""
전문가 국면/추세 필터 비교 백테스트.
코스닥 지수에 여러 정식 필터를 적용 — 위험ON이면 종목 보유 / 위험OFF면 현금.
비교:
  1) 단순보유
  2) 20일선        (단기, 참고)
  3) 200일선 룰    (Meb Faber 전술자산배분의 대표 룰: 지수 200MA 위면 보유)
  4) 골든/데드크로스 (50일선 vs 200일선)
  5) 듀얼모멘텀     (12개월 수익률 > 0 이면 보유; Antonacci/시계열 모멘텀)
참고: 봇 8단계 국면 헤지 = 같은 하락장에서 +18.7%, MDD -14.2%.
대상: 에스앤에스텍(101490). 지수: 코스닥 ^KQ11(yfinance). 데이터: pykrx + yfinance.
실행: python3 KR/regime_methods_backtest.py
"""
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


def run_hold(px):
    p0 = buy_px(px.iloc[0])
    sh = INIT_CASH // p0
    cash = INIT_CASH - sh * p0
    return cash + sh * px, sh, 0


def run_filter(px, ok):
    cash = INIT_CASH
    sh = 0.0
    tr = 0
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
        eq.append(cash + sh * p)
    return pd.Series(eq, index=px.index), sh, tr


def report(name, res):
    eq, sh, tr = res
    f = eq.iloc[-1]
    print("")
    print("[" + name + "]")
    print("  수익률 : {:+.1f}%".format((f / INIT_CASH - 1) * 100))
    print("  MDD   : {:.1f}%".format(mdd(eq)))
    print("  매매   : {}".format(tr))


def main():
    spx = stock_close(TICKER)
    idx = yf_close(INDEX_YF)
    ma20 = idx.rolling(20).mean()
    ma50 = idx.rolling(50).mean()
    ma200 = idx.rolling(200).mean()
    mom12 = idx / idx.shift(252) - 1
    sig20 = (idx >= ma20)
    sig200 = (idx >= ma200)
    sigcross = (ma50 >= ma200)
    sigmom = (mom12 > 0)

    df = pd.DataFrame({"px": spx})

    def align(s):
        return s.reindex(df.index).ffill().fillna(False).astype(bool)

    df["s20"] = align(sig20)
    df["s200"] = align(sig200)
    df["sx"] = align(sigcross)
    df["sm"] = align(sigmom)
    df = df.dropna(subset=["px"])

    print("종목 {} | {}~{} | 코스닥 지수 기준 국면 필터 비교".format(TICKER, START, END))
    report("단순보유", run_hold(df["px"]))
    report("20일선", run_filter(df["px"], df["s20"]))
    report("200일선 룰(Faber)", run_filter(df["px"], df["s200"]))
    report("골든/데드크로스(50/200)", run_filter(df["px"], df["sx"]))
    report("듀얼모멘텀(12개월)", run_filter(df["px"], df["sm"]))
    print("")
    print(">>> 참고: 봇 8단계 헤지 = +18.7%, MDD -14.2% (같은 하락장)")


if __name__ == "__main__":
    main()
