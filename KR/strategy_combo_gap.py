"""매매기법 조합 × 답안지/봇/AI 괴리율 전수 탐색 (2015~).

사용자 프레임 확장: 단일규칙(하락=현금) 말고 [국면그룹→기법] 조합을 여러개 만들어
각 조합마다 답안지(사후국면=천장) vs 봇/AI(실시간판단)로 매매 → 수익·괴리율 전부 비교.
괴리율 = (답안지수익-실제수익)/|답안지수익|. 작을수록 그 조합을 실시간으로 잘 구현(국면감지 충분).

국면그룹: 하락계열/상승계열/횡보.
기법 후보: 하락∈{현금,인버스,보유} · 상승∈{보유,레버리지2x,모멘텀top4} · 횡보∈{보유,모멘텀top4,현금}
대상: 코스피 종목 EW(보유/현금/인버스/레버리지 기준자산). 모멘텀=top4/12M. 2015~.

실행: python KR/strategy_combo_gap.py [--telegram]
"""
import sys, os, pickle, itertools
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd
from KR.walkforward_backtest import classify_phase_walkforward, send_telegram
from KR.phase_answersheet_backtest import classify_hindsight, BEAR
from KR.phase_source_backtest import gemini_index_phase
from KR.reliability_check import sim_momentum

START = '2015-01-01'
COST = 0.0021
BORROW = 0.045 / 252
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data_cache_wf.pkl')
BULL = {'RECOVERY', 'BULL_EARLY', 'BULL_MID', 'BULL_LATE'}

DOWN = ['현금', '인버스', '보유']
UP = ['보유', '레버리지', '모멘텀']
SIDE = ['보유', '모멘텀', '현금']


def group(ph):
    if ph in BEAR: return 'D'
    if ph in BULL: return 'U'
    return 'S'


def build_streams(ew, mom_ret):
    return {
        '보유':   ew,
        '현금':   pd.Series(0.0, index=ew.index),
        '인버스': -ew,                                   # 합성 인버스(gross)
        '레버리지': 2 * ew - BORROW,
        '모멘텀': mom_ret,
    }


def sim_combo(phase, mmap, streams, cal, shift):
    """phase(라벨) + mmap{'D':기법,'U':기법,'S':기법} → 일별수익. shift=실시간."""
    p = phase.reindex(cal).ffill()
    if shift:
        p = p.shift(1).fillna('SIDEWAYS')
    method_day = p.map(lambda x: mmap[group(x)])
    r = pd.Series(0.0, index=cal)
    for m in set(mmap.values()):
        sel = (method_day == m).values
        r.values[sel] = streams[m].reindex(cal).fillna(0.0).values[sel]
    change = (method_day != method_day.shift()).fillna(True)
    r = r - change.astype(float) * COST
    return (1 + r).cumprod()


def stats(eq):
    ret = (eq.iloc[-1] / eq.iloc[0] - 1) * 100
    mdd = float(((eq / eq.cummax() - 1) * 100).min())
    return ret, mdd


def run():
    data = pickle.load(open(CACHE, 'rb'))
    stocks = {**data['KOSPI'], **data['KOSDAQ']}
    idx_df = data['index']['KOSPI']; idx_df = idx_df[idx_df.index >= '2014-06-01']
    cal = idx_df.index[idx_df.index >= START]
    R = pd.DataFrame({c: df['close'].pct_change().reindex(cal) for c, (n, df) in stocks.items()})
    ew = R.mean(axis=1).fillna(0.0)
    mom_ret = sim_momentum(R, 4, 12).pct_change().reindex(cal).fillna(0.0)
    streams = build_streams(ew, mom_ret)

    ans = classify_hindsight(idx_df)
    bot = classify_phase_walkforward(idx_df, data.get('vix'))
    try:
        from base.database import get_db_connection
        from ai.gemini_api import GeminiApi
        conn = get_db_connection()
        k = conn.execute("SELECT gemini_api_key FROM users WHERE gemini_api_key IS NOT NULL AND gemini_api_key!='' LIMIT 1").fetchone()
        conn.close()
        ai, _, _ = gemini_index_phase('KOSPI', idx_df, GeminiApi(k['gemini_api_key']), anonymize=True)
    except Exception as e:
        print("AI 실패:", e); ai = bot

    hold_ret = stats((1 + ew).cumprod())[0]
    rows = []
    for d, u, s in itertools.product(DOWN, UP, SIDE):
        mmap = {'D': d, 'U': u, 'S': s}
        ra, ma = stats(sim_combo(ans, mmap, streams, cal, shift=False))
        rb, mb = stats(sim_combo(bot, mmap, streams, cal, shift=True))
        ri, mi = stats(sim_combo(ai, mmap, streams, cal, shift=True))
        gap_b = (ra - rb) / abs(ra) * 100 if ra else 0
        gap_i = (ra - ri) / abs(ra) * 100 if ra else 0
        rows.append({'combo': f"하락:{d}/상승:{u}/횡보:{s}", 'ans': ra, 'bot': rb, 'ai': ri,
                     'mdd_ai': mi, 'gap_b': gap_b, 'gap_i': gap_i})

    L = ["🧩 매매기법 조합 × 답안지/봇/AI 괴리율 (2015~, 코스피)",
         f"단순보유 기준 {hold_ret:+.0f}% · 답안지=사후국면(천장)·실제=실시간 · 괴리=(답안지-실제)/답안지", "=" * 78]
    # AI 실제수익 순 정렬
    rows_by_ai = sorted(rows, key=lambda x: x['ai'], reverse=True)
    L.append("[AI 실제수익 상위 10 조합]")
    L.append(f"{'조합':30}{'답안지':>8}{'봇':>8}{'AI':>8}{'괴리봇':>7}{'괴리AI':>7}")
    for r in rows_by_ai[:10]:
        L.append(f"{r['combo']:30}{r['ans']:>+7.0f}%{r['bot']:>+7.0f}%{r['ai']:>+7.0f}%{r['gap_b']:>+6.0f}%{r['gap_i']:>+6.0f}%")
    L.append("")
    # 괴리 최소(AI) 정렬
    rows_by_gap = sorted([r for r in rows if r['ans'] > 0], key=lambda x: x['gap_i'])
    L.append("[괴리율(AI) 최소 = 실시간 구현 잘되는 조합 상위 8]")
    L.append(f"{'조합':30}{'답안지':>8}{'AI실제':>8}{'괴리AI':>7}{'AI MDD':>8}")
    for r in rows_by_gap[:8]:
        L.append(f"{r['combo']:30}{r['ans']:>+7.0f}%{r['ai']:>+7.0f}%{r['gap_i']:>+6.0f}%{r['mdd_ai']:>+7.0f}%")
    L.append("=" * 78)
    best_ai = rows_by_ai[0]
    L.append(f"📌 AI 실제수익 1위: {best_ai['combo']}  {best_ai['ai']:+.0f}% (답안지 {best_ai['ans']:+.0f}%, 괴리 {best_ai['gap_i']:+.0f}%)")
    beat = [r for r in rows_by_ai if r['ai'] > hold_ret]
    L.append(f"   보유({hold_ret:+.0f}%) 초과 조합: AI기준 {len(beat)}/{len(rows)}개")
    if beat:
        b = beat[0]
        L.append(f"   → 보유 이기는 최고 AI조합: {b['combo']} {b['ai']:+.0f}% (괴리 {b['gap_i']:+.0f}%, MDD {b['mdd_ai']:+.0f}%)")
    L.append("⚠️ 답안지=미래정보(천장). 인버스=합성(-EW). 모멘텀 절대수익은 생존편향 과대. 2015~ 상승편중.")
    return "\n".join(L)


if __name__ == '__main__':
    rep = run()
    print("\n" + rep)
    if '--telegram' in sys.argv:
        send_telegram(rep)
