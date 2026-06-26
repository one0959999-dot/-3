"""
시장 방어 백테스트 — 코스닥 종목을 '코스닥 지수 국면'으로 방어할 때 vs 단순보유.
- 코스닥 지수가 20일선 위(건강)면 보유, 아래(약세)로 내려가면 전량 매도(현금 대피),
  다시 위로 올라오면 재매수. 종목 자체 신호가 아니라 '시장 건강도'로만 타이밍.
대상: 에스앤에스텍(101490, 코스닥). 데이터: pykrx(무료).
실행: python3 KR/market_defense_backtest.py
"""
import pandas as pd
from pykrx import stock

# ===== 설정 =====
TICKER = "101490"        # 에스앤에스텍 (코스닥)
INDEX_CODE = "^KQ11"      # 코스닥 지수 (코스피는 "1001")
START = "20230101"
END = "20251231"
INIT_CASH = 10_000_000
MA_N = 20                # 시장 국면 판단 이동평균

# 거래비용
FEE = 0.00015
TAX = 0.0018
SLIPPAGE = 0.001


def load_close(code, is_index):
    if is_index:
        import yfinance as yf
        s = "{}-{}-{}".format(START[:4], START[4:6], START[6:])
        e = "{}-{}-{}".format(END[:4], END[4:6], END[6:])
        df = yf.download(code, start=s, end=e, progress=False)
        c = df["Close"]
        if hasattr(c, "columns"):
            c = c.iloc[:, 0]
        out = c.astype(float)
        out.index = pd.to_datetime(out.index)
        return out
    df = stock.get_market_ohlcv_by_date(START, END, code)
    if df is None or df.empty:
        raise RuntimeError("데이터 없음: " + code)
    out = df["종가"].astype(float)
    out.index = pd.to_datetime(out.index)
    return out


def buy_px(p):
    return p * (1 + SLIPPAGE) * (1 + FEE)


def sell_px(p):
    return p * (1 - SLIPPAGE) * (1 - FEE - TAX)


def mdd(eq):
    peak = eq.cummax()
    return ((eq - peak) / peak).min() * 100


def run_buyhold(px):
    p0 = buy_px(px.iloc[0])
    sh = INIT_CASH // p0
    cash = INIT_CASH - sh * p0
    eq = cash + sh * px
    return eq, sh, 0


def run_defense(px, healthy):
    cash = INIT_CASH
    sh = 0.0
    trades = 0
    eq = []
    for d in px.index:
        price = px.loc[d]
        h = bool(healthy.loc[d])
        if sh == 0 and h:
            bp = buy_px(price)
            sh = cash // bp
            cash -= sh * bp
            trades += 1
        elif sh > 0 and not h:
            cash += sh * sell_px(price)
            sh = 0.0
            trades += 1
        eq.append(cash + sh * price)
    return pd.Series(eq, index=px.index), sh, trades


def report(name, eq, sh, tr):
    final = eq.iloc[-1]
    print("")
    print("[" + name + "]")
    print("  최종 평가액 : {:,.0f}원".format(final))
    print("  총 수익률   : {:+.1f}%".format((final / INIT_CASH - 1) * 100))
    print("  MDD        : {:.1f}%".format(mdd(eq)))
    print("  매매 횟수   : {}".format(tr))
    print("  최종 보유주 : {:,.0f}주".format(sh))


def main():
    spx = load_close(TICKER, False)
    ipx = load_close(INDEX_CODE, True)
    ima = ipx.rolling(MA_N).mean()
    healthy = (ipx >= ima)
    df = pd.DataFrame({"px": spx})
    df["healthy"] = healthy.reindex(df.index).ffill().fillna(False).astype(bool)
    df = df.dropna(subset=["px"])
    if df.empty:
        raise RuntimeError("정렬 후 데이터 부족")
    print("종목 {} (코스닥) | 시장지수 {} | {}~{} | 초기 {:,}원".format(
        TICKER, INDEX_CODE, START, END, INIT_CASH))
    bh = run_buyhold(df["px"])
    de = run_defense(df["px"], df["healthy"])
    report("단순보유", bh[0], bh[1], bh[2])
    report("코스닥 국면 방어", de[0], de[1], de[2])
    diff = de[0].iloc[-1] - bh[0].iloc[-1]
    print("")
    print(">>> 방어 - 단순보유: {:+,.0f}원".format(diff))
    print(">>> 양수면 시장방어 효과 있음, 음수면 단순보유가 나음")


if __name__ == "__main__":
    main()
