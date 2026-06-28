"""충실 백테스트 — 봇의 '진짜 알고리즘'(국면판단 + 진입점수 + 예산비율)으로 1주 단위·1000만 매매.

사용자 지적: 일반 모멘텀 말고 '봇 실제 알고리즘'을 써야 검증임. 1주 살 수 있는지(端수)도 따져야.
→ KR/strategy.py 진짜 함수 그대로 호출:
   국면: classify_phase(지수, 워크포워드) → 3분류(BULL/BEAR/NEUTRAL)
   코어: calculate_core_entry_score + get_core_entry_threshold
   위성: calculate_entry_score + get_entry_threshold + get_budget_ratio_from_score
실행: 1000만 현금 시작, 매월 재평가, 점수>=임계면 예산비율만큼 '1주 단위' 매수(살 수 있을 때만).
코어=코스피(60%), 위성=코스닥+상폐(40%, 생존편향 교정). 룩어헤드 제거(어제국면→오늘), 거래비용.

실행: python KR/algo_faithful_backtest.py [--telegram]
"""
import sys, os, pickle, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd
from KR.strategy import (calculate_entry_score, get_entry_threshold, get_budget_ratio_from_score,
                         calculate_core_entry_score, get_core_entry_threshold)
from KR.walkforward_backtest import classify_phase_walkforward, send_telegram

START = '2016-01-01'
INIT = 10_000_000
COST = 0.0021
WIN = 130
CORE_W, SAT_W = 0.60, 0.40
P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
DB = P('lassi.db')
PHASE2REG3 = {'PANIC': 'BEAR', 'BEAR_EARLY': 'BEAR', 'BEAR_MID': 'BEAR', 'BEAR_LATE': 'BEAR',
              'RECOVERY': 'BULL', 'BULL_EARLY': 'BULL', 'BULL_MID': 'BULL', 'BULL_LATE': 'BULL',
              'SIDEWAYS': 'NEUTRAL', 'UNKNOWN': 'NEUTRAL'}


def load_kr():
    closes, market = {}, {}
    for f in ('data_cache_big.pkl', 'data_cache_wf.pkl'):
        d = pickle.load(open(P(f), 'rb'))
        for mk in ('KOSPI', 'KOSDAQ'):
            for c, (n, df) in d.get(mk, {}).items():
                closes.setdefault(c, df); market.setdefault(c, mk)
    dd = pickle.load(open(P('data_cache_delisted.pkl'), 'rb'))
    for c, v in dd.items():
        if v['close'].index.max().year >= 2015 and c not in closes:
            closes[c] = pd.DataFrame({'close': v['close']})  # 상폐는 종가만
    con = sqlite3.connect(DB)
    for t, m in con.execute("SELECT ticker, market FROM ticker_market_dart WHERE market IS NOT NULL").fetchall():
        if t in closes:
            market[t] = m
    con.close()
    core = [c for c in closes if market.get(c) == 'KOSPI']
    sat = [c for c in closes if market.get(c) in ('KOSDAQ', 'E')]
    idx = pickle.load(open(P('data_cache_wf.pkl'), 'rb'))['index']['KOSPI']
    return closes, core, sat, idx


def _ensure_ohlc(df):
    """상폐는 close만 있음 → 진짜 함수가 high/low 쓰면 close로 대체."""
    if 'high' not in df.columns:
        df = df.copy()
        df['high'] = df['close']; df['low'] = df['close']; df['open'] = df['close']
        df['volume'] = 0
    return df


def _healthy(fin, t, year):
    if t not in fin:
        return True
    ys = [y for y in fin[t] if y <= year - 1 and fin[t][y] is not None]
    return True if not ys else fin[t][max(ys)] >= 0


def run(use_healthy=True):
    closes, core, sat, idx_df = load_kr()
    cal = idx_df.index[idx_df.index >= START]
    phase = classify_phase_walkforward(idx_df, None).reindex(cal).ffill().shift(1).fillna('UNKNOWN')
    reg3 = phase.map(lambda p: PHASE2REG3.get(p, 'NEUTRAL'))
    s = pd.Series(cal, index=cal); me = [pd.Timestamp(d) for d in s.groupby([cal.year, cal.month]).last().values]
    px = {c: closes[c]['close'].reindex(cal) for c in closes}     # 실제 거래일(매수 가능여부)
    pxf = {c: px[c].ffill() for c in closes}                       # 평가·청산용(상폐는 마지막가 유지)
    dfs = {c: _ensure_ohlc(closes[c]) for c in closes}
    # 건전성 재무
    fin = {}
    con = sqlite3.connect(DB)
    for t, y, cap in con.execute("SELECT ticker, year, capital FROM financials_dart").fetchall():
        fin.setdefault(t, {})[y] = cap
    con.close()

    cash = float(INIT); holds = {}
    eq = []; me_set = set(me); n_hold_log = []
    for d in cal:
        # 일별 평가
        if d in me_set:
            reg = reg3.loc[d]
            # 전량매도 (상폐는 마지막가 pxf로 청산)
            for t, q in list(holds.items()):
                p = pxf[t].get(d)
                if p == p and p > 0:
                    cash += q * p * (1 - COST)
            holds = {}
            equity = cash
            # 코어/위성 각각 매수
            for pool, score_fn, thr_fn, budget_w, slot in [
                (core, 'core', None, CORE_W, 'core'), (sat, 'sat', None, SAT_W, 'satellite')]:
                budget = equity * budget_w; spent = 0.0
                cands = []
                i = cal.get_loc(d)
                for t in pool:
                    p = px[t].get(d)
                    if p != p or p <= 0 or i < WIN:
                        continue
                    if use_healthy and not _healthy(fin, t, d.year):    # 자본잠식·적자 제외
                        continue
                    w = dfs[t].iloc[max(0, i - WIN):i + 1]
                    if len(w) < 65 or w['close'].isna().all():
                        continue
                    try:
                        if score_fn == 'core':
                            sc, _ = calculate_core_entry_score(w, float(p), reg)
                            thr = get_core_entry_threshold(reg); ratio = 0.10 if sc >= thr else 0
                        else:
                            sc, _ = calculate_entry_score(w, float(p), reg)
                            thr = get_entry_threshold(reg, 'satellite')
                            ratio = get_budget_ratio_from_score(sc, thr) if sc >= thr else 0
                    except Exception:
                        continue
                    if ratio > 0:
                        cands.append((sc, ratio, t, float(p)))
                cands.sort(reverse=True)
                for sc, ratio, t, p in cands:
                    if spent >= budget:
                        break
                    alloc = min(ratio * equity, budget - spent)
                    q = int(alloc // (p * (1 + COST)))
                    if q > 0 and cash >= q * p * (1 + COST):
                        cash -= q * p * (1 + COST); holds[t] = holds.get(t, 0) + q; spent += q * p
            n_hold_log.append(len(holds))
        val = cash + sum(q * (pxf[t].get(d) if pxf[t].get(d) == pxf[t].get(d) else 0) for t, q in holds.items())
        eq.append(val)
    e = pd.Series(eq, index=cal)
    ret = (e.iloc[-1] / INIT - 1) * 100
    yrs = len(e) / 252
    cagr = ((e.iloc[-1] / INIT) ** (1 / yrs) - 1) * 100
    mdd = float(((e / e.cummax() - 1) * 100).min())
    avg_hold = np.mean(n_hold_log) if n_hold_log else 0
    return ret, cagr, mdd, avg_hold, e.iloc[-1], len(core), len(sat)


def report():
    L = ["🤖 봇 '진짜 알고리즘' 충실 백테스트 (국면판단+진입점수, 1주단위, 1000만, 2016~)"]
    r0 = run(use_healthy=False)
    r1 = run(use_healthy=True)
    L.append(f"코어풀(코스피) {r0[5]} · 위성풀(코스닥+상폐) {r0[6]} · 상폐포함(생존편향교정)·상폐는 마지막가 청산")
    L.append("=" * 58)
    L.append(f"{'버전':22}{'1000만→':>9}{'수익':>8}{'연':>5}{'MDD':>6}{'보유':>5}")
    L.append("-" * 58)
    for tag, r in [('건전성필터 없음', r0), ('★건전성필터 있음(자본잠식·적자 제외)', r1)]:
        L.append(f"{tag:22}{r[4]/1e4:>8,.0f}만{r[0]:>+7.0f}%{r[1]:>+4.0f}{r[2]:>+6.0f}{r[3]:>4.1f}")
    L.append("=" * 58)
    L.append("봇 실제 함수(classify_phase·calculate_entry_score·budget_ratio) 사용.")
    L.append("판독: 건전성필터가 부실/상폐 매수를 막아 결과를 살리는지 비교. 절대수익은 코어 미교정 생존편향 잔존.")
    return "\n".join(L)


if __name__ == '__main__':
    rep = report()
    print("\n" + rep)
    if '--telegram' in sys.argv:
        send_telegram(rep)
