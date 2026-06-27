"""실전형 모멘텀 로테이션 — 진짜 1000만원, 1주 단위 반올림, 실제 수수료/세금/슬리피지.

목적: 사용자 결정(여윳돈·하이리스크OK)에 맞춰 3~5종목 모멘텀 로테이션을 '실제 체결조건'으로 검증.
- 1주 단위(整數)만 매수 → 고가주(하이닉스 267만 등) 端수·현금드래그 반영
- 매월 트레일링 12M 모멘텀 상위 K종목 → 다음달 보유(룩어헤드 없음: 월말선정→익월 체결)
- 추세 브레이크 옵션: 지수<MA200이면 그달 현금(낙폭 방어)
- 수수료 0.015%+슬리피지 0.1%(편도), 매도세 0.18%

실행: python KR/realistic_momentum.py [--telegram]
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd
from KR.walkforward_backtest import load, START, PRINCIPAL, send_telegram

MKT = 'KOSPI'
LOOK = 12 * 21          # 12개월 모멘텀
BUY_COST = 0.00015 + 0.001          # 수수료+슬리피지(편도)
SELL_COST = 0.00015 + 0.0018 + 0.001  # +매도세
SPLIT = '2022-01-01'


def frames(data, mkt):
    stocks = data[mkt]; idx_df = data['index'][mkt]
    cal = idx_df.index[idx_df.index >= START]
    px = pd.DataFrame({c: df['close'].reindex(cal) for c, (n, df) in stocks.items()})
    names = {c: n for c, (n, df) in stocks.items()}
    return cal, px, idx_df['close'].reindex(cal).ffill(), names


def month_first_days(cal):
    s = pd.Series(cal, index=cal)
    firsts = s.groupby([cal.year, cal.month]).first().values
    return [pd.Timestamp(d) for d in firsts]


def month_end_before(cal, day):
    prev = cal[cal < day]
    return prev[-1] if len(prev) else None


def select(px, cal, t, K):
    """월말 t 기준 12M 모멘텀 상위 K (룩어헤드 없음)."""
    i = cal.get_loc(t)
    if i < LOOK:
        return []
    mom = (px.iloc[i] / px.iloc[i - LOOK] - 1).dropna()
    return list(mom.sort_values(ascending=False).head(K).index)


def run(data, K, trend_filter=False):
    cal, px, idx, names = frames(data, MKT)
    ma200 = idx.rolling(200).mean()
    rebs = set(month_first_days(cal))
    cash = float(PRINCIPAL); holds = {}     # code -> shares
    eq = []; n_held_hist = []; cash_drag_hist = []
    for d in cal:
        prices_today = px.loc[d]
        if d in rebs:
            t = month_end_before(cal, d)
            if t is not None:
                # 현 보유 전량 매도
                for c, q in holds.items():
                    p = prices_today.get(c)
                    if p == p and p > 0:
                        cash += q * p * (1 - SELL_COST)
                holds = {}
                go_cash = trend_filter and not (idx.loc[d] > ma200.loc[d])
                if not go_cash:
                    sel = select(px, cal, t, K)
                    sel = [c for c in sel if prices_today.get(c) == prices_today.get(c) and prices_today.get(c, 0) > 0]
                    if sel:
                        target = (cash) / len(sel)      # 종목당 목표금액
                        for c in sel:
                            p = float(prices_today[c])
                            q = int(target // (p * (1 + BUY_COST)))   # 1주 단위 반올림(내림)
                            if q > 0:
                                cash -= q * p * (1 + BUY_COST); holds[c] = q
                # 진단: 실제 담긴 종목수 / 현금잔량비율
                val = cash + sum(holds[c] * float(prices_today.get(c, 0) or 0) for c in holds)
                n_held_hist.append(len(holds))
                cash_drag_hist.append(cash / val * 100 if val > 0 else 0)
        val = cash + sum(holds[c] * float(prices_today.get(c, 0) or 0) for c in holds)
        eq.append(val)
    eqs = pd.Series(eq, index=cal)
    ret = (eqs.iloc[-1] / PRINCIPAL - 1) * 100
    yrs = len(cal) / 252
    cagr = ((eqs.iloc[-1] / PRINCIPAL) ** (1 / yrs) - 1) * 100
    mdd = float(((eqs / eqs.cummax() - 1) * 100).min())
    h1 = eqs[cal < SPLIT]; h2 = eqs[cal >= SPLIT]
    r1 = (h1.iloc[-1] / h1.iloc[0] - 1) * 100 if len(h1) > 20 else 0
    r2 = (h2.iloc[-1] / h2.iloc[0] - 1) * 100 if len(h2) > 20 else 0
    return {'final': eqs.iloc[-1], 'ret': ret, 'cagr': cagr, 'mdd': mdd,
            'r1': r1, 'r2': r2,
            'n_held': np.mean(n_held_hist) if n_held_hist else 0,
            'cash_drag': np.mean(cash_drag_hist) if cash_drag_hist else 0}


def hold_bench(data):
    cal, px, idx, names = frames(data, MKT)
    # 동일가중 단순보유(1주단위, 초기 10M 분산)
    first = px.iloc[LOOK] if len(px) > LOOK else px.iloc[0]
    valid = [c for c in px.columns if first.get(c) == first.get(c) and first.get(c, 0) > 0]
    target = PRINCIPAL / len(valid); cash = float(PRINCIPAL); holds = {}
    d0 = cal[LOOK]
    for c in valid:
        p = float(px.loc[d0, c]); q = int(target // (p * (1 + BUY_COST)))
        if q > 0:
            cash -= q * p * (1 + BUY_COST); holds[c] = q
    eq = []
    for d in cal[LOOK:]:
        val = cash + sum(holds[c] * float(px.loc[d, c] or 0) for c in holds if px.loc[d, c] == px.loc[d, c])
        eq.append(val)
    eqs = pd.Series(eq, index=cal[LOOK:])
    ret = (eqs.iloc[-1] / PRINCIPAL - 1) * 100
    yrs = len(eqs) / 252
    cagr = ((eqs.iloc[-1] / PRINCIPAL) ** (1 / yrs) - 1) * 100
    mdd = float(((eqs / eqs.cummax() - 1) * 100).min())
    return {'final': eqs.iloc[-1], 'ret': ret, 'cagr': cagr, 'mdd': mdd}


def report(data):
    L = ["💰 실전형 모멘텀 로테이션 (진짜 1000만원·1주단위·실수수료/세금)",
         f"{MKT} · {START}~ · 12개월 모멘텀 · 룩어헤드 제거", "=" * 66]
    hb = hold_bench(data)
    L.append(f"기준 단순보유(동일가중): 1000만→{hb['final']/1e4:,.0f}만 ({hb['ret']:+.0f}%, 연{hb['cagr']:+.0f}%, MDD{hb['mdd']:.0f}%)")
    L.append("")
    L.append(f"{'전략':22}{'1000만→':>10}{'누적%':>8}{'연%':>6}{'MDD':>6}{'실담긴종목':>9}{'현금잔량':>8}")
    for K in (3, 4, 5):
        for tf in (False, True):
            r = run(data, K, tf)
            tag = f"top{K} 모멘텀" + ("+추세브레이크" if tf else "")
            L.append(f"{tag:22}{r['final']/1e4:>9,.0f}만{r['ret']:>+8.0f}{r['cagr']:>+6.0f}{r['mdd']:>+6.0f}"
                     f"{r['n_held']:>8.1f}개{r['cash_drag']:>7.0f}%")
    L.append("=" * 66)
    L.append("판독: '실담긴종목'이 K보다 작으면 端수로 다 못 담은 것, '현금잔량' 높으면 드래그.")
    L.append("⚠️ 절대수익은 생존편향으로 과대. 단 '모멘텀>보유'·종목수/브레이크 효과는 참고가치.")
    return "\n".join(L)


if __name__ == '__main__':
    data = load()
    rep = report(data)
    print("\n" + rep)
    if '--telegram' in sys.argv:
        send_telegram(rep)
