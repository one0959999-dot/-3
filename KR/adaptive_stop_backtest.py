"""
적응형 스톱 백테스트 — 'AI는 모드만, 봇이 타이밍' 설계 검증.
3가지 비교:
  1) 단순보유
  2) 시장방어   = 코스닥 20MA 이탈 시 즉시 전량매도(AI가 직접 타이밍, 거친 버전)
  3) 적응형스톱 = 평소 느슨한 손절, 방어모드(코스닥 약세)엔 타이트한 손절
                 (AI는 모드만 켜고 실제 매도는 봇의 트레일링 스톱이 잰다)
※ 트레일링 스톱은 봇 실제 손절(giveback/RSI)의 핵심(고점대비 하락 손절)을 단일종목용으로 근사.
대상: 에스앤에스텍(101490, 코스닥). 데이터: pykrx + yfinance(실제 코스닥 지수 ^KQ11).
실행: python3 KR/adaptive_stop_backtest.py
"""
import pandas as pd
import yfinance as yf
from pykrx import stock

TICKER = "101490"
INDEX_YF = "^KQ11"
WARMUP = "2020-06-01"
START = "20211101"
END = "20221231"
INIT_CASH = 10_000_000
MA_N = 20
NORMAL_STOP = 0.18      # 평소 트레일링 스톱 (고점 대비 -18%)
TIGHT_STOP = 0.08       # 방어모드 트레일링 스톱 (-8%)

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


def run_marketdef(px, healthy):
    cash = INIT_CASH
    sh = 0.0
    tr = 0
    eq = []
    for d in px.index:
        p = px.loc[d]
        ok = bool(healthy.loc[d])
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


def run_adaptive(px, healthy):
    cash = INIT_CASH
    sh = 0.0
    peak = 0.0
    tr = 0
    eq = []
    for d in px.index:
        p = px.loc[d]
        ok = bool(healthy.loc[d])
        if sh == 0:
            if ok:
                bp = buy_px(p)
                sh = cash // bp
                cash -= sh * bp
                peak = p
                tr += 1
        else:
            if p > peak:
                peak = p
            stop = NORMAL_STOP if ok else TIGHT_STOP
            if p <= peak * (1 - stop):
                cash += sh * sell_px(p)
                sh = 0.0
                tr += 1
        eq.append(cash + sh * p)
    return pd.Series(eq, index=px.index), sh, tr


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
    kq = yf_close(INDEX_YF)
    healthy_all = (kq >= kq.rolling(MA_N).mean())
    df = pd.DataFrame({"px": spx})
    df["ok"] = healthy_all.reindex(df.index).ffill().fillna(False).astype(bool)
    df = df.dropna(subset=["px"])
    print("종목 {} | {}~{} | 적응형 스톱 (평소 -{:.0%} / 방어 -{:.0%})".format(
        TICKER, START, END, NORMAL_STOP, TIGHT_STOP))
    bh = run_hold(df["px"])
    md = run_marketdef(df["px"], df["ok"])
    ad = run_adaptive(df["px"], df["ok"])
    report("단순보유", bh[0], bh[1], bh[2])
    report("시장방어(즉시전량)", md[0], md[1], md[2])
    report("적응형스톱(AI모드+봇타이밍)", ad[0], ad[1], ad[2])
    print("")
    print(">>> 적응형 - 시장방어: {:+,.0f}원".format(ad[0].iloc[-1] - md[0].iloc[-1]))
    print(">>> 적응형 - 단순보유: {:+,.0f}원".format(ad[0].iloc[-1] - bh[0].iloc[-1]))


if __name__ == "__main__":
    main()
