"""코어2 + 위성2 (±고정지수) 백테스트 — KR(생존편향 교정) / US. 1000만원.

사용자 스펙: 1000만 · 코어2 + 위성2 · 고정지수 넣고/안넣고 · KR,US 동시.
구조:
 - 코어 = 대형(KR:코스피 / US:대형주 리스트), 위성 = 성장(KR:코스닥 / US:성장주 리스트)
 - 매월 각 풀에서 12개월 모멘텀 top2 선정, 월 리밸런싱(룩어헤드 제거: 월말선정→익월)
 - 고정지수: KR=^KS11, US=SPY — '넣고' 버전은 5번째 고정슬롯(교체안됨), '안넣고'는 4종목
 - KR만: 상폐 포함(생존편향 교정) + 건전성 필터(자본잠식=자본총계<0 제외, 시점별)
 - US: 상폐데이터 없음→생존편향 낌(낙관적, 참고용)

실행: python KR/core_satellite_v2_backtest.py [--telegram]
"""
import sys, os, pickle, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd
from KR.walkforward_backtest import send_telegram

START = '2016-01-01'
COST = 0.0021
LOOK = 252
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lassi.db')
P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)


# ── 데이터 로드 ──
def load_kr():
    closes, market = {}, {}
    for f in ('data_cache_big.pkl', 'data_cache_wf.pkl'):
        if os.path.exists(P(f)):
            d = pickle.load(open(P(f), 'rb'))
            for mk in ('KOSPI', 'KOSDAQ'):
                for c, (n, df) in d.get(mk, {}).items():
                    closes.setdefault(c, df['close']); market.setdefault(c, mk)
    if os.path.exists(P('data_cache_delisted.pkl')):
        for c, v in pickle.load(open(P('data_cache_delisted.pkl'), 'rb')).items():
            if v['close'].index.max().year >= 2015:
                closes.setdefault(c, v['close'])
    # 시장구분(KOSPI/KOSDAQ): 별도 corp_cls 테이블 ticker_market_dart 우선(있으면). 기존 ticker_sector(국가구분)는 안 씀.
    con = sqlite3.connect(DB)
    try:
        for t, m in con.execute("SELECT ticker, market FROM ticker_market_dart WHERE market IN ('KOSPI','KOSDAQ')").fetchall():
            if t in closes:
                market[t] = m
    except Exception:
        pass
    # 건전성: {ticker:{year:capital}}
    fin = {}
    for t, y, cap in con.execute("SELECT ticker, year, capital FROM financials_dart").fetchall():
        fin.setdefault(t, {})[y] = cap
    con.close()
    idx = pickle.load(open(P('data_cache_wf.pkl'), 'rb'))['index']['KOSPI']['close']
    core = [c for c in closes if market.get(c) == 'KOSPI']
    sat = [c for c in closes if market.get(c) == 'KOSDAQ']
    return closes, core, sat, fin, idx


def load_us():
    d = pickle.load(open(P('data_cache_us.pkl'), 'rb'))
    closes = {}
    core = [c for c, (n, df) in d['CORE'].items()]
    sat = [c for c, (n, df) in d['SAT'].items()]
    for c, (n, df) in {**d['CORE'], **d['SAT']}.items():
        closes[c] = df['close']
    idx = d['index']['US']['close']
    return closes, core, sat, {}, idx


def healthy(fin, t, year):
    """t가 year 시점에 자본잠식 아님? (year-1 이하 최신 재무 capital>=0). 데이터없으면 통과."""
    if t not in fin:
        return True
    ys = [y for y in fin[t] if y <= year - 1 and fin[t][y] is not None]
    if not ys:
        return True
    return fin[t][max(ys)] >= 0


def month_ends(cal):
    s = pd.Series(cal, index=cal)
    return [pd.Timestamp(d) for d in s.groupby([cal.year, cal.month]).last().values]


def select(closes, pool, cal, t, k, fin, healthy_on):
    i = cal.get_loc(t); year = t.year
    mom = {}
    for c in pool:
        s = closes[c].reindex(cal)
        if i < LOOK or pd.isna(s.iloc[i]) or pd.isna(s.iloc[i - LOOK]) or s.iloc[i - LOOK] <= 0:
            continue
        if healthy_on and not healthy(fin, c, year):
            continue
        mom[c] = s.iloc[i] / s.iloc[i - LOOK] - 1
    return [c for c, _ in sorted(mom.items(), key=lambda kv: kv[1], reverse=True)[:k]]


def run_bt(closes, core, sat, fin, idx, cal, with_index, healthy_on):
    R = {c: closes[c].reindex(cal).pct_change() for c in closes}
    idx_ret = idx.reindex(cal).pct_change().fillna(0.0)
    me = month_ends(cal)
    sel_at = {}
    for t in me:
        c2 = select(closes, core, cal, t, 2, fin, healthy_on)
        s2 = select(closes, sat, cal, t, 2, fin, healthy_on)
        sel_at[t] = c2 + s2
    me_sorted = sorted(sel_at)
    eq = [1.0]; cur = []; prev = None; ptr = 0
    for d in cal[1:]:
        while ptr < len(me_sorted) and me_sorted[ptr] < d:
            cur = sel_at[me_sorted[ptr]]; ptr += 1
        slots = (cur + ['__IDX__']) if with_index else cur
        if not slots:
            eq.append(eq[-1]); continue
        w = 1.0 / len(slots)
        r = 0.0
        for s in slots:
            rr = idx_ret.loc[d] if s == '__IDX__' else R[s].loc[d]
            r += w * (rr if rr == rr else 0.0)
        if cur is not prev:
            r -= COST  # 전환비용 근사
            prev = cur
        eq.append(eq[-1] * (1 + r))
    e = pd.Series(eq, index=cal)
    ret = (e.iloc[-1] - 1) * 100
    yrs = len(e) / 252
    cagr = (e.iloc[-1] ** (1 / yrs) - 1) * 100
    mdd = float(((e / e.cummax() - 1) * 100).min())
    return ret, cagr, mdd


def market_block(name, closes, core, sat, fin, idx, healthy_on, note):
    cal = idx.index[idx.index >= START]
    # 공통 달력: 지수 거래일
    L = [f"[{name}] 코어풀 {len(core)} · 위성풀 {len(sat)}종목  {note}"]
    for label, wi in [('지수 안넣고(코어2+위성2)', False), ('지수 넣고(+고정지수)', True)]:
        ret, cagr, mdd = run_bt(closes, core, sat, fin, idx, cal, wi, healthy_on)
        final = 1000 * (1 + ret / 100)
        L.append(f"  {label:22} 1000만→{final:>7,.0f}만  {ret:>+7.0f}%  연{cagr:>+5.0f}%  MDD{mdd:>+5.0f}%")
    return "\n".join(L)


def main():
    out = ["⚖️ 코어2+위성2 (±고정지수) 백테스트 · 1000만원 · 2016~", "=" * 60]
    # KR
    try:
        ck, core, sat, fin, idx = load_kr()
        out.append(market_block('KR', ck, core, sat, fin, idx, healthy_on=True,
                                 note='(상폐포함·건전성필터=생존편향 교정)'))
    except Exception as e:
        out.append(f"[KR] 실패: {e}")
    out.append("")
    # US
    try:
        cu, core, sat, fin, idx = load_us()
        out.append(market_block('US', cu, core, sat, fin, idx, healthy_on=False,
                                 note='(⚠️상폐데이터無=생존편향 낌, 낙관적·참고용)'))
    except Exception as e:
        out.append(f"[US] 실패: {e}")
    out.append("=" * 60)
    out.append("판독: '넣고'가 '안넣고'보다 나으면 고정지수 효과(보통 낙폭↓·수익↓).")
    out.append("⚠️ KR=생존편향 교정(정직), US=교정안됨(낙관). 둘은 직접비교 불가. 절대수익 과대주의.")
    return "\n".join(out)


if __name__ == '__main__':
    rep = main()
    print("\n" + rep)
    if '--telegram' in sys.argv:
        send_telegram(rep)
