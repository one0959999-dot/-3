"""1단계: 정답지 생성 — 사후로 본 '이상적 매매 타이밍'(저점=매수, 고점=매도) 전환점.

정의(사용자): 정답지 = 최저점에서 사고 최고점에서 파는 시점.
→ zigzag(되돌림 임계%)로 유의미한 바닥/천장을 추출. 지수 + 종목별(상폐 포함).
저장: data_answersheet.pkl { 'INDEX': {...}, 'stocks': {ticker: {'name','points':[(date,'L'/'H',price)], 'market'}} }
2단계(봇/AI)가 이 전환점에 실시간 신호가 얼마나 가까운지(괴리율) 측정하는 데 사용.

실행: python KR/answer_sheet.py [pct]   # pct=되돌림 임계(기본 0.20=20%)
"""
import sys, os, pickle, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
OUT = P('data_answersheet.pkl')
DB = P('lassi.db')


END = '2025-12-31'   # 2026 지수데이터 손상(1년 2.7배)이라 컷


def zigzag(close: pd.Series, pct=0.20):
    """되돌림 pct 이상에서 전환점 확정. 고점/저점 '분리 추적'. 반환: [(date,'L'|'H',price)]."""
    c = close.dropna()
    if len(c) < 20:
        return []
    v = c.values; idx = c.index
    pts = []
    hi_i, hi = 0, v[0]; lo_i, lo = 0, v[0]; dirn = 0
    for i in range(1, len(v)):
        p = v[i]
        if dirn >= 0:                         # 상승(또는 미정): 고점 추적
            if p > hi:
                hi, hi_i = p, i
            if p <= hi * (1 - pct):           # 고점서 pct 하락 → 고점 확정, 하락전환
                pts.append((idx[hi_i], 'H', hi)); dirn = -1; lo, lo_i = p, i
                continue
        if dirn <= 0:                         # 하락(또는 미정): 저점 추적
            if p < lo:
                lo, lo_i = p, i
            if p >= lo * (1 + pct):           # 저점서 pct 상승 → 저점 확정, 상승전환
                pts.append((idx[lo_i], 'L', lo)); dirn = 1; hi, hi_i = p, i
    return pts


def load_all():
    closes, market, names = {}, {}, {}
    for f in ('data_cache_big.pkl', 'data_cache_wf.pkl'):
        if os.path.exists(P(f)):
            d = pickle.load(open(P(f), 'rb'))
            for mk in ('KOSPI', 'KOSDAQ'):
                for c, (n, df) in d.get(mk, {}).items():
                    closes.setdefault(c, df['close']); market.setdefault(c, mk); names.setdefault(c, n)
    if os.path.exists(P('data_cache_delisted.pkl')):
        for c, v in pickle.load(open(P('data_cache_delisted.pkl'), 'rb')).items():
            closes.setdefault(c, v['close']); names.setdefault(c, v.get('name', c)); market.setdefault(c, 'DELISTED')
    con = sqlite3.connect(DB)
    try:
        for t, m in con.execute("SELECT ticker, market FROM ticker_market_dart WHERE market IS NOT NULL").fetchall():
            if t in market:
                market[t] = m
    except Exception:
        pass
    con.close()
    idx = pickle.load(open(P('data_cache_wf.pkl'), 'rb'))['index']['KOSPI']['close']
    # 손상된 2026 구간 컷
    idx = idx[idx.index <= END]
    closes = {c: s[s.index <= END] for c, s in closes.items()}
    return idx, closes, market, names


def main(pct=0.20):
    idx, closes, market, names = load_all()
    out = {'pct': pct, 'stocks': {}}
    ip = zigzag(idx, pct)
    out['INDEX'] = {'name': 'KOSPI지수', 'points': ip}
    n_stock = 0
    for c, s in closes.items():
        pts = zigzag(s, pct)
        if len(pts) >= 2:
            out['stocks'][c] = {'name': names.get(c, c), 'market': market.get(c, '?'), 'points': pts}
            n_stock += 1
    pickle.dump(out, open(OUT, 'wb'))
    # 요약
    L = [f"📑 정답지(전환점) 생성 — zigzag 되돌림 {pct*100:.0f}%",
         f"지수 전환점 {len(ip)}개 (저점{sum(1 for _,t,_ in ip if t=='L')}·고점{sum(1 for _,t,_ in ip if t=='H')})",
         f"종목 {n_stock}개 정답지 생성 (상폐 포함)",
         "", "지수 최근 전환점 8개:"]
    for d, t, p in ip[-8:]:
        L.append(f"  {d.strftime('%Y-%m')} {'바닥(매수)' if t=='L' else '천장(매도)'} {p:,.0f}")
    allpts = [len(v['points']) for v in out['stocks'].values()]
    L.append(f"\n종목당 평균 전환점 {np.mean(allpts):.1f}개 · 저장 → {OUT}")
    print("\n".join(L))


if __name__ == '__main__':
    pct = float(sys.argv[1]) if len(sys.argv) > 1 else 0.20
    main(pct)
