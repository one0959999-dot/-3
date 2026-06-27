"""모멘텀 top4 전략의 9국면별 수익률 분해 — '상승장이라 좋았던 것 아니냐' 검증.

같은 전략(모멘텀 top4/12M)·같은 종목풀을, 코스피 지수 9국면(워크포워드 classify_phase)으로 쪼개
각 국면 '그 기간 동안'의 모멘텀 vs 보유 수익을 본다(격리=그 국면 날들만 복리).
→ 하락국면서 모멘텀이 얼마나 깨지는지(상승편향) 정직하게 드러냄.

실행: python KR/phase_breakdown.py [--telegram]
"""
import sys, os, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd
from KR.walkforward_backtest import classify_phase_walkforward, START, send_telegram
from KR.reliability_check import sim_momentum

PHASE_ORDER = ['PANIC', 'BEAR_EARLY', 'BEAR_MID', 'BEAR_LATE', 'RECOVERY',
               'BULL_EARLY', 'BULL_MID', 'BULL_LATE', 'SIDEWAYS']
PHASE_KR = {'PANIC': '패닉', 'BEAR_EARLY': '하락초기', 'BEAR_MID': '하락중반',
            'BEAR_LATE': '하락말기', 'RECOVERY': '회복초입', 'BULL_EARLY': '상승초입',
            'BULL_MID': '상승중반', 'BULL_LATE': '상승말기', 'SIDEWAYS': '횡보'}
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data_cache_wf.pkl')


def isolated_ret(daily_ret, phase, ph):
    mask = (phase == ph).values
    if mask.sum() < 5:
        return None, int(mask.sum())
    r = (1 + daily_ret.values[mask]).prod() - 1
    return r * 100, int(mask.sum())


def run():
    data = pickle.load(open(CACHE, 'rb'))
    stocks = {**data['KOSPI'], **data['KOSDAQ']}      # 61종목 = 모멘텀 풀
    idx_df = data['index']['KOSPI']
    cal = idx_df.index[idx_df.index >= START]
    R = pd.DataFrame({c: df['close'].pct_change().reindex(cal) for c, (n, df) in stocks.items()})

    # 워크포워드 9국면(어제판단→오늘, 룩어헤드 없음)
    phase = classify_phase_walkforward(idx_df, data.get('vix')).reindex(cal).ffill().shift(1).fillna('UNKNOWN')

    # 모멘텀 top4/12M 일별수익 / 보유(동일가중) 일별수익
    mom_eq = sim_momentum(R, 4, 12)
    mom_ret = mom_eq.pct_change().reindex(cal).fillna(0.0)
    hold_ret = R.mean(axis=1).reindex(cal).fillna(0.0)

    L = ["📊 모멘텀 top4 — 9국면별 수익 분해 (코스피 지수 국면, 격리=그 국면 날들만)",
         f"{START}~ · 같은 전략·같은 종목풀(61) · 워크포워드 국면", "=" * 60]
    L.append(f"{'국면':12}{'일수':>6}{'모멘텀':>10}{'보유':>10}{'모멘텀-보유':>12}")
    L.append("-" * 60)
    tot_days = 0
    for ph in PHASE_ORDER:
        m, d = isolated_ret(mom_ret, phase, ph)
        h, _ = isolated_ret(hold_ret, phase, ph)
        tot_days += d
        if m is None:
            L.append(f"{PHASE_KR[ph]:12}{d:>6}{'표본부족':>10}")
            continue
        diff = m - h
        flag = '🟢' if diff > 0 else '🔴'
        L.append(f"{PHASE_KR[ph]:12}{d:>6}{m:>+9.0f}%{h:>+9.0f}%{flag}{diff:>+9.0f}%p")
    L.append("-" * 60)
    # 전체
    mt = (mom_eq.iloc[-1] / mom_eq.iloc[0] - 1) * 100
    ht = ((1 + hold_ret).cumprod().iloc[-1] - 1) * 100
    L.append(f"{'전체':12}{tot_days:>6}{mt:>+9.0f}%{ht:>+9.0f}%")
    # 국면 그룹 요약
    bear = ['PANIC', 'BEAR_EARLY', 'BEAR_MID', 'BEAR_LATE']
    bull = ['RECOVERY', 'BULL_EARLY', 'BULL_MID', 'BULL_LATE']
    def grp(phs, ret):
        mask = phase.isin(phs).values
        return ((1 + ret.values[mask]).prod() - 1) * 100, int(mask.sum())
    bm, bd = grp(bear, mom_ret); bh, _ = grp(bear, hold_ret)
    um, ud = grp(bull, mom_ret); uh, _ = grp(bull, hold_ret)
    sm, sd = grp(['SIDEWAYS'], mom_ret); sh, _ = grp(['SIDEWAYS'], hold_ret)
    L.append("=" * 60)
    L.append("[국면 그룹 요약]")
    L.append(f"  하락계열({bd}일): 모멘텀 {bm:+.0f}% vs 보유 {bh:+.0f}%")
    L.append(f"  상승계열({ud}일): 모멘텀 {um:+.0f}% vs 보유 {uh:+.0f}%")
    L.append(f"  횡보({sd}일):     모멘텀 {sm:+.0f}% vs 보유 {sh:+.0f}%")
    L.append("=" * 60)
    L.append("판독: 모멘텀이 상승계열에 수익 몰림 = '상승장 편향' 사실. 하락계열 수익이 핵심 약점.")
    L.append("⚠️ 격리(그 국면 날들만 복리)라 실제 연속운용과 다름. 절대수익은 생존편향 과대.")
    return "\n".join(L)


if __name__ == '__main__':
    rep = run()
    print("\n" + rep)
    if '--telegram' in sys.argv:
        send_telegram(rep)
