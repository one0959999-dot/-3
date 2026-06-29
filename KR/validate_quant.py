"""3단계 토대 신뢰도 검증 — 파라미터 민감도 + 롤링 윈도우 + 미국 보조 OOS.

핵심 엔진(저변동성+부실제외+추세분산)을 numpy 고속 백테스트로 다각 검증:
 ② 파라미터 민감도: N={30,50,100} × 리밸런스={M,Q,2Q}  (특정값 운 아님?)
 ③ 롤링 윈도우: 여러 시작연도 5년 구간  (특정기간 운 아님?)
 (보조) 미국: 같은 룰(저변동성+추세, 재무필터 없음) vs 미국지수 — 단 미국캐시는 생존대형주 큐레이팅(편향)이라 참고용.
판정: 거의 모든 설정/기간에서 지수 대비 MDD↓·수익≥ 이면 토대 견고.

실행: python KR/validate_quant.py [--telegram]
"""
import sys, os, sqlite3, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
INIT = 1e7; BUY = 0.0015; SELL = 0.0033


def mdd(eq):
    eq = np.asarray(eq, float); pk = np.maximum.accumulate(eq)
    return float((eq / pk - 1).min() * 100)


def precompute(panel, use_bad=True):
    ff = panel.ffill()
    ma = panel.rolling(200, min_periods=120).mean()
    up = ma > ma.shift(20)
    trend = (panel > ma) & up
    vol = ff.pct_change().rolling(126).std()
    if use_bad:
        c = sqlite3.connect(P('lassi.db')); rows = {}
        for t, y, cap, pi, ni in c.execute('SELECT ticker,year,capital,paidin,netincome FROM financials_dart'):
            rows.setdefault(t, []).append((y, cap, pi, ni))
        c.close()
        badfy = {}
        for t, rs in rows.items():
            rs.sort(); run = 0
            for y, cap, pi, ni in rs:
                imp = (cap is not None and cap < 0) or (cap is not None and pi not in (None, 0) and 0 <= cap < pi)
                run = run + 1 if (ni is not None and ni < 0) else 0
                if imp or run >= 2:
                    badfy[t] = y; break
        years = np.array([d.year for d in panel.index])
        badmask = np.zeros(panel.shape, bool)
        for j, t in enumerate(panel.columns):
            if t in badfy:
                badmask[:, j] = years >= badfy[t]
        elig = trend.values & (~badmask)
    else:
        elig = trend.values
    return ff.values, ~np.isnan(panel.values), elig, vol.values, panel.index


def fast_bt(ff, trad, elig, vol, dates, N=50, rebal='Q'):
    nt = ff.shape[1]; cash = INIT; sh = np.zeros(nt); eq = []; cur = None
    for i in range(len(dates)):
        px = ff[i]
        if i > 0:
            gone = (sh > 0) & (~trad[i]) & trad[i - 1]
            if gone.any():
                cash += np.nansum(sh[gone] * px[gone] * (1 - SELL)); sh[gone] = 0
        dt = dates[i]
        key = (dt.year, (dt.month - 1) // 3) if rebal == 'Q' else ((dt.year, (dt.month - 1) // 6) if rebal == '2Q' else (dt.year, dt.month))
        if key != cur:
            cur = key
            held = (sh > 0) & trad[i]
            cash += np.nansum(sh[held] * px[held] * (1 - SELL)); sh[held] = 0
            mask = elig[i] & trad[i] & ~np.isnan(vol[i])
            idx = np.where(mask)[0]
            if len(idx):
                order = idx[np.argsort(vol[i][idx])][:N]
                per = cash * 0.98 / len(order)
                for j in order:
                    p = px[j]
                    if p > 0:
                        q = int(per // (p * (1 + BUY)))
                        if q > 0:
                            cash -= q * p * (1 + BUY); sh[j] += q
        eq.append(cash + np.nansum(sh * px))
    return (eq[-1] / INIT - 1) * 100, mdd(eq)


def idx_hold(close):
    c = close.dropna(); q = int(INIT // (c.iloc[0] * (1 + BUY))); cash = INIT - q * c.iloc[0] * (1 + BUY)
    eq = cash + q * c.values
    return (eq[-1] / INIT - 1) * 100, mdd(eq)


def kr_panel(start, end, full=True):
    wf = pickle.load(open(P('data_cache_wf.pkl'), 'rb'))
    deli = pickle.load(open(P('data_cache_delisted.pkl'), 'rb'))
    cl = {}
    if full and os.path.exists(P('data_cache_kr_full.pkl')):
        fullc = pickle.load(open(P('data_cache_kr_full.pkl'), 'rb'))
        for c, df in fullc.items():
            cl.setdefault(c, df['close'])
    else:
        big = pickle.load(open(P('data_cache_big.pkl'), 'rb'))
        for d in (big, wf):
            for mk in ('KOSPI', 'KOSDAQ'):
                for c, (n, df) in d.get(mk, {}).items():
                    cl.setdefault(c, df['close'])
    for k, v in deli.items():
        cl.setdefault(k, v['close'])
    panel = pd.DataFrame(cl).sort_index(); panel = panel[(panel.index >= start) & (panel.index <= end)]
    kospi = wf['index']['KOSPI']['close']; kospi = kospi[(kospi.index >= start) & (kospi.index <= end)]
    return panel, kospi


def main(telegram=False):
    L = ["🔬 3단계 토대 신뢰도 검증 (저변동성+부실제외+추세분산)", ""]
    # ② 파라미터 민감도 (full 2015-2025)
    panel, kospi = kr_panel('2015-01-01', '2025-12-31')
    ff, trad, elig, vol, dates = precompute(panel)
    ih, ihm = idx_hold(kospi)
    L.append(f"[② 파라미터 민감도]  (코스피보유 {ih:+.0f}%/MDD{ihm:.0f}%)")
    L.append(f"{'설정':14}{'수익률':>8}{'MDD':>7}{'지수대비':>8}")
    win = 0; tot = 0
    for N in (30, 50, 100):
        for rb in ('M', 'Q', '2Q'):
            r, m = fast_bt(ff, trad, elig, vol, dates, N=N, rebal=rb)
            beat = '승' if (r >= ih and m >= ihm) else ('수익승' if r >= ih else '패')
            win += 1 if (r >= ih and m >= ihm) else 0; tot += 1
            L.append(f"N{N}·{rb:<3}{'':6}{r:>7.0f}%{m:>7.0f}%{beat:>8}")
    L.append(f"→ 수익·MDD 둘다 지수 우위: {win}/{tot}")
    # ③ 롤링 윈도우 (5년)
    L.append(f"\n[③ 롤링 윈도우 5년] (N=50,Q)")
    L.append(f"{'기간':14}{'전략':>8}{'MDD':>7}{'지수':>8}{'지수MDD':>8}")
    rwin = 0; rtot = 0
    for s in ('2015', '2016', '2017', '2018', '2019', '2020'):
        e = str(int(s) + 4)
        pn, ks = kr_panel(f'{s}-01-01', f'{e}-12-31')
        f2, t2, e2, v2, d2 = precompute(pn)
        r, m = fast_bt(f2, t2, e2, v2, d2, N=50, rebal='Q')
        iih, iihm = idx_hold(ks)
        ok = (r >= iih and m >= iihm); rwin += ok; rtot += 1
        L.append(f"{s}-{e[2:]:<8}{r:>7.0f}%{m:>7.0f}%{iih:>7.0f}%{iihm:>7.0f}%{'  ✓' if ok else ''}")
    L.append(f"→ 둘다 지수 우위: {rwin}/{rtot}")
    # 보조: 미국
    try:
        us = pickle.load(open(P('data_cache_us.pkl'), 'rb'))
        cl = {}
        for grp in ('CORE', 'SAT'):
            for k, v in us.get(grp, {}).items():
                df = v[1] if isinstance(v, tuple) else v
                if hasattr(df, 'columns') and 'close' in df.columns:
                    cl[k] = df['close']
        up = pd.DataFrame(cl).sort_index()
        uidx = us['index']['US']; uidx = uidx['close'] if hasattr(uidx, 'columns') else uidx
        f3, t3, e3, v3, d3 = precompute(up, use_bad=False)
        r, m = fast_bt(f3, t3, e3, v3, d3, N=min(30, up.shape[1] // 2), rebal='Q')
        uih, uihm = idx_hold(uidx)
        L.append(f"\n[보조 미국OOS] (생존대형주 큐레이팅=편향, 참고용, {up.shape[1]}종목)")
        L.append(f"  저변동성분산 {r:+.0f}%/MDD{m:.0f}%  vs  미국지수 {uih:+.0f}%/MDD{uihm:.0f}%")
    except Exception as ex:
        L.append(f"\n[미국] 스킵: {ex}")
    L.append(f"\n판정: 대부분 설정·기간서 MDD↓·수익≥ → 토대 견고도 평가.")
    rep = "\n".join(L)
    print(rep)
    if telegram:
        try:
            cc = sqlite3.connect(P('lassi.db'), timeout=30)
            r = cc.execute("SELECT telegram_token, telegram_chat_id FROM users WHERE telegram_token IS NOT NULL AND telegram_token!='' LIMIT 1").fetchone(); cc.close()
            from base.telegram_bot import TelegramNotifier
            TelegramNotifier(r[0], r[1]).send_message(rep); print("텔레그램 ✓")
        except Exception as e:
            print("텔레그램 실패", e)


if __name__ == '__main__':
    main('--telegram' in sys.argv)
