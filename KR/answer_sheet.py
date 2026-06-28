"""1단계(재정의): 정답지 = 각 9국면의 '이상적 매매방법 + 이상적 수익률'.

정의(사용자): 정답지는 9개 국면 각각에서 이상적으로 뭘 하고(매매), 그러면 얼마 버는가(수익)를 표현.
방법: 사후 전환점(zigzag 바닥/천장)을 앵커로 9국면을 라벨링 →
  국면별 [이상적 매매 / 그 국면 평균 가격변화 / 이상적 행동시 캡처수익 / 일수] 표.
지수 + 종목별(상폐 포함). 저장 data_answersheet.pkl.

국면 라벨(사후): 상승레그(바닥→천장)를 진행률로 회복초입/상승초입/중반/말기,
 하락레그(천장→바닥)를 하락초기/중반/말기, 급락(5일-12%↓)=패닉, 미세레그=횡보.
이상적 매매: 상승계열=보유/매수, 상승말기=매도, 하락계열=현금(회피), 하락말기/회복=매수, 횡보=스윙.

실행: python KR/answer_sheet.py [pct]
"""
import sys, os, pickle, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
OUT = P('data_answersheet.pkl'); DB = P('lassi.db'); END = '2025-12-31'
PHASE_ORDER = ['패닉', '하락초기', '하락중반', '하락말기', '회복초입', '상승초입', '상승중반', '상승말기', '횡보']
IDEAL = {'패닉': '현금/숏(급락회피)', '하락초기': '현금(하락회피)', '하락중반': '현금/숏',
         '하락말기': '분할매수(바닥)', '회복초입': '매수', '상승초입': '보유/추가매수',
         '상승중반': '보유', '상승말기': '매도/익절(천장)', '횡보': '스윙/관망'}
# 상승계열=가격상승 캡처(LONG), 하락계열=하락회피가 이득(AVOID)
DIRN = {'회복초입': 'LONG', '상승초입': 'LONG', '상승중반': 'LONG', '상승말기': 'LONG',
        '하락초기': 'AVOID', '하락중반': 'AVOID', '패닉': 'AVOID', '하락말기': 'LONG', '횡보': 'FLAT'}


def zigzag(close, pct=0.20):
    c = close.dropna()
    if len(c) < 20: return []
    v = c.values; idx = c.index; pts = []
    hi_i, hi = 0, v[0]; lo_i, lo = 0, v[0]; dirn = 0
    for i in range(1, len(v)):
        p = v[i]
        if dirn >= 0:
            if p > hi: hi, hi_i = p, i
            if p <= hi * (1 - pct):
                pts.append((idx[hi_i], 'H', hi)); dirn = -1; lo, lo_i = p, i; continue
        if dirn <= 0:
            if p < lo: lo, lo_i = p, i
            if p >= lo * (1 + pct):
                pts.append((idx[lo_i], 'L', lo)); dirn = 1; hi, hi_i = p, i
    return pts


def label_phases(close, pts):
    """전환점 앵커로 일별 9국면 라벨(사후)."""
    c = close.dropna(); ph = pd.Series('횡보', index=c.index)
    drop5 = c / c.shift(5) - 1
    for k in range(len(pts) - 1):
        d0, t0, p0 = pts[k]; d1, t1, p1 = pts[k + 1]
        seg = c[(c.index >= d0) & (c.index <= d1)]
        if len(seg) < 2: continue
        rng = abs(p1 - p0)
        if rng / p0 < 0.18:                      # 미세 = 횡보
            ph.loc[seg.index] = '횡보'; continue
        frac = pd.Series(np.linspace(0, 1, len(seg)), index=seg.index)  # 시간기반(단조) — 가격출렁임 라벨뒤집힘 방지
        if t0 == 'L' and t1 == 'H':              # 상승레그
            lab = pd.cut(frac, [-9, .15, .45, .85, 9], labels=['회복초입', '상승초입', '상승중반', '상승말기'])
            ph.loc[seg.index] = lab.astype(str)
        elif t0 == 'H' and t1 == 'L':            # 하락레그
            lab = pd.cut(frac, [-9, .30, .75, 9], labels=['하락초기', '하락중반', '하락말기'])
            ph.loc[seg.index] = lab.astype(str)
            panic = seg.index[(drop5.reindex(seg.index) < -0.12).fillna(False).values]
            ph.loc[panic] = '패닉'
    return ph


def phase_table(close, ph):
    """국면별 '연환산 이상수익': 해당국면 전체일의 일별수익을 이상적행동(상승=롱/하락=숏회피)으로 복리→연환산."""
    c = close.dropna().reindex(ph.index)
    ret = c.pct_change().fillna(0.0)
    out = {}
    for p in set(ph.dropna()):
        mask = (ph == p).values
        days = int(mask.sum())
        if days < 5:
            continue
        r = ret.values[mask]
        d = DIRN.get(p, 'FLAT')
        signed = r if d == 'LONG' else (-r if d == 'AVOID' else r)
        out[p] = {'days': days,
                  'mean_daily': float(r.mean()) * 100,            # 일평균 가격변화(%/일)
                  'ideal_daily': float(np.mean(signed)) * 100}    # 이상행동 일평균(%/일)
    return out


def main(pct=0.20):
    idx_df = pickle.load(open(P('data_cache_wf.pkl'), 'rb'))['index']['KOSPI']
    idxc = idx_df['close']; idxc = idxc[idxc.index <= END]
    ip = zigzag(idxc, pct); iph = label_phases(idxc, ip); itab = phase_table(idxc, iph)
    # 종목 집계
    closes = {}
    for f in ('data_cache_big.pkl', 'data_cache_wf.pkl'):
        d = pickle.load(open(P(f), 'rb'))
        for mk in ('KOSPI', 'KOSDAQ'):
            for c, (n, df) in d.get(mk, {}).items():
                closes.setdefault(c, df['close'])
    if os.path.exists(P('data_cache_delisted.pkl')):
        for c, v in pickle.load(open(P('data_cache_delisted.pkl'), 'rb')).items():
            closes.setdefault(c, v['close'])
    agg = {p: [] for p in PHASE_ORDER}; store = {}
    for c, s in closes.items():
        s = s[s.index <= END]
        pts = zigzag(s, pct)
        if len(pts) < 3: continue
        ph = label_phases(s, pts); tab = phase_table(s, ph)
        store[c] = {'points': pts, 'phase_table': tab}
        for p, v in tab.items():
            agg[p].append(v['ideal_daily'])
    pickle.dump({'pct': pct, 'INDEX': {'points': ip, 'phase_table': itab}, 'stocks': store}, open(OUT, 'wb'))

    L = [f"📑 정답지 = 9국면별 이상적 매매 + 수익률 (zigzag {pct*100:.0f}%, 사후)",
         "=" * 70,
         f"{'국면':8}{'이상적매매':18}{'일평균변화':>10}{'이상행동 일평균':>14}{'일수':>7}",
         "-" * 70]
    for p in PHASE_ORDER:
        t = itab.get(p)
        if t:
            L.append(f"{p:8}{IDEAL[p]:18}{t['mean_daily']:>+9.2f}%{t['ideal_daily']:>+13.2f}%{t['days']:>7}")
        else:
            L.append(f"{p:8}{IDEAL[p]:18}{'(지수기간내 없음)':>10}")
    L.append("-" * 70)
    L.append(f"[종목 {len(store)}개 집계: 국면별 '이상행동 일평균수익' 중앙값]")
    for p in PHASE_ORDER:
        if agg[p]:
            L.append(f"  {p:8} {IDEAL[p]:18} 이상 일평균 {np.median(agg[p]):+.2f}%/일  ({len(agg[p])}종목)")
    L.append("=" * 70)
    L.append("판독: '이상행동 일평균'=그 국면서 이상매매(상승=롱/하락=숏·회피)시 하루평균 수익률. 양수·클수록 그 국면의 이상가치 큼.")
    L.append("→ 2단계: 봇/AI가 실시간으로 이 국면을 맞추고 이 수익에 얼마나 근접하나(괴리) 측정.")
    print("\n".join(L))


if __name__ == '__main__':
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 0.20)
