"""모멘텀 종목수(K)×기간(lookback) 튜닝 — 수익 유지하며 MDD 최저 찾기.

월요일 모듈 최종 튜닝. 238종목, 2015~, 룩어헤드 제거(월말선정→익월), 실거래비용.
그리드: K∈{3,4,5,6,8,10} × 기간∈{6,9,12}개월. 지표: 누적·연·MDD·Calmar·전후반.
추천: MDD를 -55% 이내로 낮추면서 위험조정(Calmar) 최고인 설정.

실행: python KR/tune_topk.py [--telegram]
"""
import sys, os, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd
from KR.reliability_check import sim_momentum
from KR.walkforward_backtest import send_telegram

START = '2015-01-01'
SPLIT = '2021-01-01'
CACHE = os.environ.get('LASSI_CACHE') or os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data_cache_big.pkl')
KS = [3, 4, 5, 6, 8, 10]
LOOKS = [6, 9, 12]
MDD_TARGET = -55.0


def stats(eq, cal):
    ret = (eq.iloc[-1] / eq.iloc[0] - 1) * 100
    yrs = len(eq) / 252
    cagr = ((eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1) * 100
    mdd = float(((eq / eq.cummax() - 1) * 100).min())
    h1 = eq[cal < SPLIT]; h2 = eq[cal >= SPLIT]
    r1 = (h1.iloc[-1] / h1.iloc[0] - 1) * 100 if len(h1) > 20 else 0
    r2 = (h2.iloc[-1] / h2.iloc[0] - 1) * 100 if len(h2) > 20 else 0
    return {'ret': ret, 'cagr': cagr, 'mdd': mdd, 'calmar': cagr / (abs(mdd) + 1e-9), 'r1': r1, 'r2': r2}


def run():
    data = pickle.load(open(CACHE, 'rb'))
    stocks = {**data['KOSPI'], **data['KOSDAQ']}
    idx_df = data['index']['KOSPI']
    cal = idx_df.index[idx_df.index >= START]
    R = pd.DataFrame({c: df['close'].pct_change().reindex(cal) for c, (n, df) in stocks.items()})
    hold = stats((1 + R.mean(axis=1).fillna(0)).cumprod(), cal)

    grid = {}
    for k in KS:
        for lo in LOOKS:
            eq = sim_momentum(R, k, lo).reindex(cal).ffill()
            grid[(k, lo)] = stats(eq, cal)

    L = ["🎛️ 모멘텀 종목수×기간 튜닝 (238종목, 2015~)",
         f"단순보유 기준 {hold['ret']:+.0f}%/MDD{hold['mdd']:.0f}%/Calmar{hold['calmar']:.1f} · 목표 MDD≥{MDD_TARGET:.0f}%", "=" * 58]
    L.append(f"{'설정':12}{'누적':>9}{'연':>5}{'MDD':>6}{'Calmar':>7}{'전/후반':>14}")
    L.append("-" * 58)
    for k in KS:
        for lo in LOOKS:
            m = grid[(k, lo)]
            tgt = '✓' if m['mdd'] >= MDD_TARGET else ' '
            L.append(f"top{k}/{lo}M{tgt:<4}{m['ret']:>+8.0f}%{m['cagr']:>+5.0f}{m['mdd']:>+6.0f}{m['calmar']:>7.1f}{m['r1']:>+6.0f}/{m['r2']:>+6.0f}")
    L.append("-" * 58)
    # 추천: MDD 목표 이내 중 Calmar 최고, 없으면 전체 Calmar 최고
    elig = {kv: m for kv, m in grid.items() if m['mdd'] >= MDD_TARGET}
    pool = elig or grid
    best = max(pool.items(), key=lambda kv: kv[1]['calmar'])
    (bk, bl), bm = best
    # 최고 수익 설정도
    bestret = max(grid.items(), key=lambda kv: kv[1]['ret'])
    L.append(f"📌 최고 수익: top{bestret[0][0]}/{bestret[0][1]}M ({bestret[1]['ret']:+.0f}%, MDD{bestret[1]['mdd']:.0f}%)")
    L.append(f"✅ 추천(위험조정·MDD고려): top{bk}/{bl}M  {bm['ret']:+.0f}%·연{bm['cagr']:+.0f}%·MDD{bm['mdd']:.0f}%·Calmar{bm['calmar']:.1f}")
    if not elig:
        L.append(f"   ⚠️ MDD {MDD_TARGET:.0f}% 이내 설정 없음 — 모멘텀 본질이 고변동. 추천은 그중 최선.")
    L.append("⚠️ 절대수익은 생존편향 과대. 종목수↑=수익↓·MDD↓ 경향. 방향·상대비교만 신뢰.")
    return "\n".join(L), (bk, bl, bm, hold)


if __name__ == '__main__':
    rep, _ = run()
    print("\n" + rep)
    if '--telegram' in sys.argv:
        send_telegram(rep)
