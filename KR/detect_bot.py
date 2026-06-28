"""2단계-A: 봇(규칙기반) 전환점 탐지기 + 정답지 괴리율 측정.

정답지(answer_sheet.pkl)의 바닥(매수)·천장(매도)에 봇 신호가 얼마나 가까운지 측정.
봇 수법 총동원(과거데이터만=워크포워드): RSI·볼린저·이평이격·낙폭·거래량급증·MACD.
바닥신호 = 과매도 신호 군집(score>=thr), 천장신호 = 과열 신호 군집.
괴리: 시점(일수) + 가격(바닥대비 % 비싸게 샀나/천장대비 % 싸게 팔았나) + 포착률(±90일).
근거기록: 잘 잡은 신호에 어떤 지표가 켜졌는지.

실행: python KR/detect_bot.py [n_stocks]
"""
import sys, os, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
WINDOW = 90   # 전환점 ±이 일수 내 신호면 '포착'


def indicators(df):
    """과거데이터만으로 계산되는 지표들(워크포워드). df: close(+volume)."""
    c = df['close']
    d = c.diff()
    g = d.clip(lower=0).rolling(14).mean(); l = (-d.clip(upper=0)).rolling(14).mean()
    rsi = 100 - 100 / (1 + g / (l + 1e-9))
    ma20 = c.rolling(20).mean(); sd = c.rolling(20).std()
    bb_lo = ma20 - 2 * sd; bb_hi = ma20 + 2 * sd
    ma60 = c.rolling(60).mean()
    disp = (c / ma20 - 1) * 100
    dd = (c / c.cummax() - 1) * 100              # 고점대비 낙폭
    runup = (c / c.rolling(120).min() - 1) * 100  # 저점대비 상승
    e12 = c.ewm(span=12).mean(); e26 = c.ewm(span=26).mean(); macd = e12 - e26; sig = macd.ewm(span=9).mean()
    vol = df['volume'] if 'volume' in df.columns and df['volume'].sum() > 0 else None
    volr = (vol / vol.rolling(20).mean()) if vol is not None else pd.Series(1.0, index=c.index)
    return dict(rsi=rsi, bb_lo=bb_lo, bb_hi=bb_hi, ma20=ma20, ma60=ma60, disp=disp, dd=dd,
                runup=runup, macd=macd, sig=sig, volr=volr, c=c)


def bottom_signals(ind):
    """바닥신호: 과매도 수법 점수(0~N). 켜진 지표 dict도."""
    c = ind['c']
    feats = {
        'RSI<30': ind['rsi'] < 30,
        '볼린저하단이탈': c < ind['bb_lo'],
        '20이평-10%이격': ind['disp'] < -10,
        '고점대비-25%낙폭': ind['dd'] < -25,
        '거래량2배급증': ind['volr'] > 2.0,
        'MACD상향전환': (ind['macd'] > ind['sig']) & (ind['macd'].shift(1) <= ind['sig'].shift(1)),
    }
    score = sum(f.astype(float).fillna(0) for f in feats.values())
    return score, feats


def top_signals(ind):
    c = ind['c']
    feats = {
        'RSI>70': ind['rsi'] > 70,
        '볼린저상단이탈': c > ind['bb_hi'],
        '20이평+10%이격': ind['disp'] > 10,
        '저점대비+60%상승': ind['runup'] > 60,
        '거래량2배급증': ind['volr'] > 2.0,
        'MACD하향전환': (ind['macd'] < ind['sig']) & (ind['macd'].shift(1) >= ind['sig'].shift(1)),
    }
    score = sum(f.astype(float).fillna(0) for f in feats.values())
    return score, feats


def signal_days(score, thr=2):
    """score가 thr 이상 되는 '시작일'(군집 첫날)."""
    on = (score >= thr).fillna(False).values
    idx = score.index
    days = [idx[i] for i in range(len(on)) if on[i] and (i == 0 or not on[i - 1])]
    return days


def measure(points, buy_days, sell_days, ind, feats_b, feats_t):
    """정답지 전환점별 최근접 신호 괴리. 반환 통계 + 근거카운트."""
    c = ind['c']
    res = {'L': [], 'H': []}; feat_hits = {'L': {}, 'H': {}}
    for d, typ, price in points:
        sigs = buy_days if typ == 'L' else sell_days
        cand = [s for s in sigs if abs((s - d).days) <= WINDOW]
        if not cand:
            res[typ].append(None); continue
        near = min(cand, key=lambda s: abs((s - d).days))
        gap_days = (near - d).days                          # +면 늦게 잡음
        sp = float(c.get(near, np.nan))
        if typ == 'L':
            slip = (sp / price - 1) * 100 if price else np.nan   # 바닥보다 몇% 비싸게
        else:
            slip = (price / sp - 1) * 100 if sp else np.nan      # 천장보다 몇% 싸게
        res[typ].append((gap_days, slip))
        fdict = feats_b if typ == 'L' else feats_t
        for fn, fs in fdict.items():
            if bool(fs.get(near, False)):
                feat_hits[typ][fn] = feat_hits[typ].get(fn, 0) + 1
    return res, feat_hits


def summ(res, typ):
    caught = [r for r in res[typ] if r is not None]
    n = len(res[typ])
    if not caught:
        return f"{0}/{n} 포착", None
    days = np.mean([abs(r[0]) for r in caught]); slip = np.mean([r[1] for r in caught])
    return f"{len(caught)}/{n} 포착 · 평균 {days:.0f}일 · 가격괴리 {slip:+.1f}%", (len(caught), n, days, slip)


def run_one(name, df, points, thr=2):
    ind = indicators(df)
    sb, fb = bottom_signals(ind); st, ft = top_signals(ind)
    bd = signal_days(sb, thr); sd = signal_days(st, thr)
    res, fh = measure(points, bd, sd, ind, fb, ft)
    return res, fh, ind


def main(n_stocks=80):
    ans = pickle.load(open(P('data_answersheet.pkl'), 'rb'))
    # 지수
    idx_df = pickle.load(open(P('data_cache_wf.pkl'), 'rb'))['index']['KOSPI']
    idx_df = idx_df[idx_df.index <= '2025-12-31']
    L = ["🔎 2단계-A 봇(규칙기반) 전환점 탐지 — 정답지 괴리 (수법 총동원)"]
    res, fh, _ = run_one('지수', idx_df, ans['INDEX']['points'])
    sl, _ = summ(res, 'L'); sh, _ = summ(res, 'H')
    L.append(f"[지수] 바닥(매수): {sl}")
    L.append(f"       천장(매도): {sh}")
    # 종목 집계
    closes = {}
    for f in ('data_cache_big.pkl', 'data_cache_wf.pkl'):
        d = pickle.load(open(P(f), 'rb'))
        for mk in ('KOSPI', 'KOSDAQ'):
            for c, (n, df) in d.get(mk, {}).items():
                closes.setdefault(c, df)
    agg = {'L': [], 'H': []}; feat_all = {'L': {}, 'H': {}}
    done = 0
    for c, meta in ans['stocks'].items():
        if c not in closes or done >= n_stocks:
            continue
        df = closes[c][closes[c].index <= '2025-12-31']
        if len(df) < 200:
            continue
        res, fh, _ = run_one(meta['name'], df, meta['points'])
        for t in 'LH':
            agg[t] += [r for r in res[t] if r is not None]
            agg[t] += [None for r in res[t] if r is None]
            for fn, k in fh[t].items():
                feat_all[t][fn] = feat_all[t].get(fn, 0) + k
        done += 1
    L.append(f"\n[종목 {done}개 집계]")
    for t, nm in [('L', '바닥(매수)'), ('H', '천장(매도)')]:
        caught = [r for r in agg[t] if r is not None]; n = len(agg[t])
        if caught:
            days = np.mean([abs(r[0]) for r in caught]); slip = np.mean([r[1] for r in caught])
            L.append(f"  {nm}: {len(caught)}/{n} 포착({100*len(caught)/n:.0f}%) · 평균 {days:.0f}일 · 가격괴리 {slip:+.1f}%")
            top = sorted(feat_all[t].items(), key=lambda x: -x[1])[:4]
            L.append(f"    주요근거: " + ", ".join(f"{fn}({k})" for fn, k in top))
    L.append("\n판독: 가격괴리=바닥보다 몇% 비싸게 샀나(낮을수록 정답에 근접). 다음 AI와 비교.")
    print("\n".join(L))


if __name__ == '__main__':
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 80
    main(n)
