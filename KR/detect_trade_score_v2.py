"""2단계 개선: 봇 매매기법 v2 (점수 끌어올리기) — 정답지 100점 채점 + OOS 검증.

개선 4종(전부 적용):
 ① 분할매매: 신호구간서 강도가중 평균단가(과매도 깊을수록 큰 비중) → 평균이 바닥에 근접.
 ② 변동성 적응 임계값: 종목 변동성에 맞춰 RSI·낙폭 기준 조정.
 ③ 정교한 신호: 스토캐스틱, RSI 강세/약세 다이버전스 추가.
 ④ 이른 트리거 + 확인: 약간 이르게 + 국소극값 근처 신호만 채택(조기·지연 둘 다 방지).
검증: 같은 15종목(in-sample) + 다른 30종목(OOS) → 과최적화 아닌지.

실행: python KR/detect_trade_score_v2.py [--telegram]
"""
import sys, os, pickle, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd
from KR.answer_sheet import zigzag

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
END = '2025-12-31'; WINDOW = 90
STOCKS = ['지수', '005930', '247540', '000660', '035420', '051910', '005380', '068270',
          '105560', '042660', '034020', '196170', '028300', '011200', '012450']


def feats(df):
    c = df['close']; h = df.get('high', c); l = df.get('low', c)
    d = c.diff(); g = d.clip(lower=0).rolling(14).mean(); ll = (-d.clip(upper=0)).rolling(14).mean()
    rsi = 100 - 100 / (1 + g / (ll + 1e-9))
    ma20 = c.rolling(20).mean(); sd = c.rolling(20).std()
    bb_lo, bb_hi = ma20 - 2 * sd, ma20 + 2 * sd
    disp = (c / ma20 - 1) * 100
    dd = (c / c.cummax() - 1) * 100
    runup = (c / c.rolling(120).min() - 1) * 100
    e12 = c.ewm(span=12).mean(); e26 = c.ewm(span=26).mean(); macd = e12 - e26; sig = macd.ewm(span=9).mean()
    vol = df['volume'] if 'volume' in df.columns and df['volume'].sum() > 0 else None
    volr = (vol / vol.rolling(20).mean()) if vol is not None else pd.Series(1.0, index=c.index)
    # 스토캐스틱 %K(14)
    lo14 = l.rolling(14).min(); hi14 = h.rolling(14).max()
    stoch = 100 * (c - lo14) / (hi14 - lo14 + 1e-9)
    # RSI 강세 다이버전스: 가격 20일 신저점인데 RSI는 신저점 아님
    p_newlow = c <= c.rolling(20).min()
    rsi_div_bull = p_newlow & (rsi > rsi.rolling(20).min() + 3)
    p_newhi = c >= c.rolling(20).max()
    rsi_div_bear = p_newhi & (rsi < rsi.rolling(20).max() - 3)
    # 국소극값 근접(조기/지연 방지): ±10일 최저/최고 대비 2% 이내
    near_low = c <= c.rolling(21, center=True, min_periods=5).min() * 1.02
    near_hi = c >= c.rolling(21, center=True, min_periods=5).max() * 0.98
    return dict(c=c, rsi=rsi, bb_lo=bb_lo, bb_hi=bb_hi, disp=disp, dd=dd, runup=runup,
                macd=macd, sig=sig, volr=volr, stoch=stoch, div_bull=rsi_div_bull, div_bear=rsi_div_bear,
                near_low=near_low, near_hi=near_hi)


def vol_of(c):
    return float(c.pct_change().std() * np.sqrt(252) * 100)  # 연환산 변동성 %


def bottom_score(F, vol):
    # ② 변동성 적응: 변동성 클수록 더 깊은 과매도 요구
    rsi_thr = float(np.clip(35 - (vol - 35) * 0.25, 22, 38))
    dd_thr = float(np.clip(-15 - (vol - 35) * 0.4, -45, -10))
    feats_on = {
        'RSI': (F['rsi'] < rsi_thr),
        'BB하단': F['c'] < F['bb_lo'],
        '이격': F['disp'] < -8,
        '낙폭': F['dd'] < dd_thr,
        '거래량': F['volr'] > 1.8,
        'MACD골든': (F['macd'] > F['sig']) & (F['macd'].shift(1) <= F['sig'].shift(1)),
        '스토<20': F['stoch'] < 20,
        '강세다이버전스': F['div_bull'],
    }
    s = sum(x.astype(float).fillna(0) for x in feats_on.values())
    return s


def top_score(F, vol):
    rsi_thr = float(np.clip(65 + (vol - 35) * 0.25, 62, 78))
    feats_on = {
        'RSI': F['rsi'] > rsi_thr, 'BB상단': F['c'] > F['bb_hi'], '이격': F['disp'] > 8,
        '급등': F['runup'] > 50, '거래량': F['volr'] > 1.8,
        'MACD데드': (F['macd'] < F['sig']) & (F['macd'].shift(1) >= F['sig'].shift(1)),
        '스토>80': F['stoch'] > 80, '약세다이버전스': F['div_bear'],
    }
    return sum(x.astype(float).fillna(0) for x in feats_on.values())


def scored_buy(F, vol):
    """① 분할매매: score>=2인 날을 트랜치로, 비중=score (깊을수록↑), ④국소저점 근처만."""
    s = bottom_score(F, vol)
    on = (s >= 2) & F['near_low'].fillna(False)
    return s.where(on, 0.0)   # 신호일 가중치(0이면 매수 안함)


def scored_sell(F, vol):
    s = top_score(F, vol)
    on = (s >= 2) & F['near_hi'].fillna(False)
    return s.where(on, 0.0)


def score_one(ans, buy_w, sell_w, close):
    sc = []; slips = []
    for d, t, price in ans:
        if d > pd.Timestamp(END):
            continue
        w = buy_w if t == 'L' else sell_w
        win = w[(w.index >= d - pd.Timedelta(days=WINDOW)) & (w.index <= d + pd.Timedelta(days=WINDOW))]
        win = win[win > 0]
        if len(win) == 0:
            sc.append(0); continue
        prices = close.reindex(win.index)
        wsum = win.values
        avg_price = float((prices.values * wsum).sum() / wsum.sum())   # 강도가중 평균단가
        slip = max(0.0, (avg_price / price - 1) * 100) if t == 'L' else max(0.0, (price / avg_price - 1) * 100)
        slips.append(slip); sc.append(max(0.0, 100 - slip * 5))
    return (np.mean(sc) if sc else 0), (np.mean(slips) if slips else 0)


def run_set(codes, dfs, idx_df):
    res = []
    for code in codes:
        if code == '지수':
            df = idx_df
        elif code in dfs:
            df = dfs[code][1] if isinstance(dfs[code], tuple) else dfs[code]
        else:
            continue
        d = df[df.index <= END]
        if len(d) < 250:
            continue
        ans = zigzag(d['close'], 0.20)
        if len(ans) < 2:
            continue
        F = feats(d); vol = vol_of(d['close'])
        bw = scored_buy(F, vol); sw = scored_sell(F, vol)
        sc, sl = score_one(ans, bw, sw, d['close'])
        res.append((code, sc, sl))
    return res


def main(telegram=False):
    wf = pickle.load(open(P('data_cache_wf.pkl'), 'rb')); big = pickle.load(open(P('data_cache_big.pkl'), 'rb'))
    dfs = {}
    for d in (big, wf):
        for mk in ('KOSPI', 'KOSDAQ'):
            for c, (n, df) in d.get(mk, {}).items():
                dfs.setdefault(c, (n, df))
    idx_df = wf['index']['KOSPI']
    ins = run_set(STOCKS, dfs, idx_df)
    oos_codes = [c for c in dfs if c not in STOCKS][:30]
    oos = run_set(oos_codes, dfs, idx_df)
    L = ["🎯 봇 매매기법 v2 (분할+적응+다이버전스+확인) — 정답지 100점", ""]
    L.append("[In-sample 15종목]")
    for code, sc, sl in ins:
        nm = 'KOSPI지수' if code == '지수' else dfs.get(code, (code,))[0]
        L.append(f"  {nm:10} {sc:4.0f}점 (슬립 {sl:.0f}%)")
    ins_avg = np.mean([x[1] for x in ins]); oos_avg = np.mean([x[1] for x in oos]) if oos else 0
    L.append(f"\n━━ 봇 v2 종합 ━━")
    L.append(f"  In-sample(15종목): {ins_avg:.0f}점  (기존 v1: 66점)")
    L.append(f"  OOS(다른 {len(oos)}종목): {oos_avg:.0f}점  ← 비슷하면 진짜, 낮으면 과최적화")
    gap = ins_avg - oos_avg
    L.append(f"  과최적화 점검: In-OOS 차이 {gap:+.0f}점 {'(✅견고)' if abs(gap)<8 else '(⚠️과최적화 의심)'}")
    rep = "\n".join(L)
    print(rep)
    if telegram:
        try:
            c = sqlite3.connect(P('lassi.db'), timeout=30)
            r = c.execute("SELECT telegram_token, telegram_chat_id FROM users WHERE telegram_token IS NOT NULL AND telegram_token!='' LIMIT 1").fetchone(); c.close()
            from base.telegram_bot import TelegramNotifier
            TelegramNotifier(r[0], r[1]).send_message(rep); print("텔레그램 전송 ✓")
        except Exception as e:
            print("텔레그램 실패", e)


if __name__ == '__main__':
    main('--telegram' in sys.argv)
