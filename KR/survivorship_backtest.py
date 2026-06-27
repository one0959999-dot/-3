"""생존편향 교정 백테스트 — 현재종목만 vs (현재+상폐) 모멘텀/보유 비교.

상폐 종목(data_cache_delisted.pkl)을 유니버스에 넣어, 그동안의 '거품'이 얼마였는지·
모멘텀이 정직한 유니버스에서도 보유를 이기는지 확인. 룩어헤드 제거(월말선정→익월).
상폐주는 last_date 이후 시세 NaN → 자동으로 선정불가(폐지후)·보유중 폐지시 마지막가 반영.

실행: python KR/survivorship_backtest.py [--telegram]
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
BIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data_cache_big.pkl')
DEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data_cache_delisted.pkl')
K, LOOK = 6, 12


def stats(eq):
    ret = (eq.iloc[-1] / eq.iloc[0] - 1) * 100
    yrs = len(eq) / 252
    cagr = ((eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1) * 100
    mdd = float(((eq / eq.cummax() - 1) * 100).min())
    return ret, cagr, mdd


def run():
    big = pickle.load(open(BIG, 'rb'))
    cur_stocks = {**big['KOSPI'], **big['KOSDAQ']}
    idx = big['index']['KOSPI']
    cal = idx.index[idx.index >= START]

    cur_close = {c: df['close'] for c, (n, df) in cur_stocks.items()}
    deladd = {}
    ndel = 0
    if os.path.exists(DEL):
        dd = pickle.load(open(DEL, 'rb'))
        for code, v in dd.items():
            s = v['close']
            if s.index.max().year >= 2015 and len(s[s.index >= START]) > 60:
                deladd[code] = s; ndel += 1

    R_cur = pd.DataFrame(cur_close).reindex(cal).pct_change()
    all_close = {**cur_close, **deladd}
    R_all = pd.DataFrame(all_close).reindex(cal).pct_change()

    out = {}
    out['현재만 보유'] = stats((1 + R_cur.mean(axis=1).fillna(0)).cumprod())
    out['현재만 모멘텀'] = stats(sim_momentum(R_cur, K, LOOK).reindex(cal).ffill())
    out['+상폐 보유'] = stats((1 + R_all.mean(axis=1).fillna(0)).cumprod())
    out['+상폐 모멘텀'] = stats(sim_momentum(R_all, K, LOOK).reindex(cal).ffill())

    L = ["🪦 생존편향 교정 백테스트 (현재 vs 현재+상폐, top6/12M, 2015~)",
         f"현재 {len(cur_close)}종목 + 상폐(2015+시세) {ndel}종목 추가", "=" * 56]
    L.append(f"{'전략':16}{'누적':>10}{'연':>6}{'MDD':>7}")
    L.append("-" * 56)
    for k, (r, c, m) in out.items():
        L.append(f"{k:16}{r:>+9.0f}%{c:>+5.0f}%{m:>+6.0f}%")
    L.append("-" * 56)
    # 편향 크기
    bias_hold = out['현재만 보유'][0] - out['+상폐 보유'][0]
    bias_mom = out['현재만 모멘텀'][0] - out['+상폐 모멘텀'][0]
    L.append(f"생존편향(거품): 보유 {bias_hold:+.0f}%p · 모멘텀 {bias_mom:+.0f}%p (현재만이 그만큼 부풀려짐)")
    mom_beats = out['+상폐 모멘텀'][0] - out['+상폐 보유'][0]
    L.append(f"📌 정직한 유니버스서 모멘텀 vs 보유: {mom_beats:+.0f}%p {'🟢모멘텀 우위' if mom_beats>0 else '🔴모멘텀 미달'}")
    L.append("⚠️ 상폐 시세는 last_date까지만(폐지후 0수렴분 일부 미반영) → 편향교정은 보수적(실제 거품 더 클수).")
    L.append("   상폐 유동성 미스크리닝·DART목록 한계로 완전한 생존편향제거는 아님. 방향성 참고.")
    return "\n".join(L)


if __name__ == '__main__':
    rep = run()
    print("\n" + rep)
    if '--telegram' in sys.argv:
        send_telegram(rep)
