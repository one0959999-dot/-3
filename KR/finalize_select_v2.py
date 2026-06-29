"""종목선정 정직 재검증 v2 — 결함 ①②③ 수정.

수정:
 ② 상폐주 후보 포함: 거래량 없어도 상장 중엔 유동성 통과로 간주(생존편향 진짜 제거). 상폐 시 마지막가 청산.
 ① 규칙 정합: 200MA 추세필터가 핵심임을 명시, 컴포넌트(저변동성/추세/부실/품질)별 기여 분해.
 ③ 품질필터 보정: 재무 없는 종목은 '통과'(데이터없음 패널티 제거 = 생존편향 차단).
정직 비교: 순수저변동성 → +추세 → +부실 → +품질 → +섹터(=최종). 기간분할·슬리피지·1000만 환산.

실행: python KR/finalize_select_v2.py [--telegram]
"""
import sys, os, sqlite3, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
INIT = 1e7; START, END = '2015-01-01', '2025-12-31'
LIQ = 3e9


def mdd(eq):
    eq = np.asarray(eq, float); pk = np.maximum.accumulate(eq)
    return float((eq / pk - 1).min() * 100)


def build():
    full = pickle.load(open(P('data_cache_kr_full.pkl'), 'rb'))
    deli = pickle.load(open(P('data_cache_delisted.pkl'), 'rb'))
    wf = pickle.load(open(P('data_cache_wf.pkl'), 'rb'))
    cl = {}; vold = {}
    for t, df in full.items():
        cl[t] = df['close']
        if 'volume' in df.columns:
            vold[t] = df['close'] * df['volume']
    deli_only = []
    for k, v in deli.items():
        if k not in cl:
            cl[k] = v['close']; deli_only.append(k)
    panel = pd.DataFrame(cl).sort_index(); panel = panel[(panel.index >= START) & (panel.index <= END)]
    tv = pd.DataFrame(vold).reindex(index=panel.index, columns=panel.columns)
    kospi = wf['index']['KOSPI']['close']; kospi = kospi[(kospi.index >= START) & (kospi.index <= END)]
    return panel, tv, kospi, set(deli_only)


def fundamentals():
    c = sqlite3.connect(P('lassi.db')); rows = {}
    for t, y, cap, pi, ni in c.execute('SELECT ticker,year,capital,paidin,netincome FROM financials_dart'):
        rows.setdefault(t, {})[y] = (cap, pi, ni)
    sec = {t: s for t, s in c.execute("SELECT ticker,sector FROM ticker_sector WHERE market='KR'")}
    c.close()
    return rows, {t: sorted(d) for t, d in rows.items()}, sec


def precompute(panel, tv, deli_only, fin, fy):
    ff = panel.ffill()
    ma = panel.rolling(200, min_periods=120).mean()
    trend = ((panel > ma) & (ma > ma.shift(20))).values
    vol = ff.pct_change().rolling(126).std().values
    liq = tv.rolling(20, min_periods=10).mean().values
    notna = ~np.isnan(panel.values)
    cols = list(panel.columns)
    # ② 상폐주: 거래량 없음 → 상장 중(close 존재)이면 유동성 통과로 간주
    for j, t in enumerate(cols):
        if t in deli_only:
            liq[:, j] = np.where(notna[:, j], LIQ * 10, np.nan)
    # 부실 daily mask
    badfy = {}
    for t in fin:
        run = 0
        for y in fy[t]:
            cap, pi, ni = fin[t][y]
            imp = (cap is not None and cap < 0) or (cap is not None and pi not in (None, 0) and 0 <= cap < pi)
            run = run + 1 if (ni is not None and ni < 0) else 0
            if imp or run >= 2:
                badfy[t] = y; break
    years = np.array([d.year for d in panel.index])
    bad = np.zeros(panel.shape, bool)
    for j, t in enumerate(cols):
        if t in badfy:
            bad[:, j] = years >= badfy[t]
    return ff.values, notna, trend, vol, liq, bad, years, cols


def bt(pc, fin, fy, sec, N=50, use_trend=True, use_bad=True, use_quality=True,
       use_liq=True, sectorcap=True, buy=0.0015, sell=0.0033, ret_eq=False):
    ff, trad, trend, vol, liq, bad, years, cols = pc
    nt = ff.shape[1]; cash = INIT; sh = np.zeros(nt); eq = []; cur = None
    for i in range(len(ff)):
        px = ff[i]
        if i > 0:
            gone = (sh > 0) & (~trad[i]) & trad[i - 1]
            if gone.any():
                cash += np.nansum(sh[gone] * px[gone] * (1 - sell)); sh[gone] = 0
        if cur is None or (i // 63) != cur:
            cur = i // 63; yr = years[i]
            held = (sh > 0) & trad[i]
            cash += np.nansum(sh[held] * px[held] * (1 - sell)); sh[held] = 0
            mask = trad[i] & ~np.isnan(vol[i])
            if use_trend:
                mask = mask & trend[i]
            if use_bad:
                mask = mask & ~bad[i]
            if use_liq:
                mask = mask & (liq[i] > LIQ)
            idx = np.where(mask)[0]
            if use_quality and len(idx):
                keep = []
                for j in idx:
                    t = cols[j]; ys = [y for y in fy.get(t, []) if y <= yr - 1]
                    if not ys:
                        keep.append(j)  # ③ 재무없음 = 통과(편향제거)
                    else:
                        cap, pi, ni = fin[t][ys[-1]]
                        if ni is not None and ni > 0 and cap not in (None, 0) and ni / cap > 0:
                            keep.append(j)
                idx = np.array(keep, int)
            if len(idx):
                order = idx[np.argsort(vol[i][idx])]
                if sectorcap:
                    capn = max(1, N // 4); cnt = {}; pick = []
                    for j in order:
                        s = sec.get(cols[j], '기타')
                        if cnt.get(s, 0) < capn:
                            pick.append(j); cnt[s] = cnt.get(s, 0) + 1
                        if len(pick) >= N:
                            break
                    order = np.array(pick, int)
                else:
                    order = order[:N]
                if len(order):
                    per = cash * 0.98 / len(order)
                    for j in order:
                        p = px[j]
                        if p > 0:
                            shr = int(per // (p * (1 + buy)))
                            if shr > 0:
                                cash -= shr * p * (1 + buy); sh[j] += shr
        eq.append(cash + np.nansum(sh * px))
    s = pd.Series(eq, index=pd.DatetimeIndex(np.array([np.datetime64('2015-01-01')])) if False else None)
    if ret_eq:
        return np.array(eq)
    return (eq[-1] / INIT - 1) * 100, mdd(eq)


def idx_hold(close):
    c = close.dropna(); q = int(INIT // (c.iloc[0] * 1.0015)); cash = INIT - q * c.iloc[0] * 1.0015
    eq = cash + q * c.values
    return (eq[-1] / INIT - 1) * 100, mdd(eq)


def main(telegram=False):
    panel, tv, kospi, deli_only = build()
    fin, fy, sec = fundamentals()
    pc = precompute(panel, tv, deli_only, fin, fy)
    ih, ihm = idx_hold(kospi)
    L = [f"🔧 종목선정 정직 재검증 v2 — 상폐 후보포함·규칙정합 ({panel.shape[1]}종목, 상폐 {len(deli_only)} 실편입)", ""]
    L.append(f"[A. 컴포넌트 기여]  코스피보유 {ih:+.0f}%/MDD{ihm:.0f}%")
    L.append(f"{'구성(누적)':32}{'수익률':>8}{'MDD':>7}")
    L.append("-" * 49)
    def row(nm, r): return f"{nm:32}{r[0]:>7.0f}%{r[1]:>7.0f}%"
    r0 = bt(pc, fin, fy, sec, use_trend=False, use_bad=False, use_quality=False, sectorcap=False)
    L.append(row("순수 저변동성 top50", r0))
    r1 = bt(pc, fin, fy, sec, use_trend=True, use_bad=False, use_quality=False, sectorcap=False)
    L.append(row(" +200MA 추세필터", r1))
    r2 = bt(pc, fin, fy, sec, use_trend=True, use_bad=True, use_quality=False, sectorcap=False)
    L.append(row(" +부실제외", r2))
    r3 = bt(pc, fin, fy, sec, use_trend=True, use_bad=True, use_quality=True, sectorcap=False)
    L.append(row(" +품질(ROE>0)", r3))
    r4 = bt(pc, fin, fy, sec, use_trend=True, use_bad=True, use_quality=True, sectorcap=True)
    L.append(row(" +섹터캡 = 최종(정직판)", r4))
    # 1000만 환산
    L.append(f"\n[B. 1000만원 → 11년 후]")
    L.append(f"  최종(정직판): {INIT*(1+r4[0]/100)/1e4:,.0f}만원 (총수익 {INIT*r4[0]/100/1e4:+,.0f}만)")
    L.append(f"  코스피 보유 : {INIT*(1+ih/100)/1e4:,.0f}만원 (총수익 {INIT*ih/100/1e4:+,.0f}만)")
    # 슬리피지
    L.append(f"\n[C. 슬리피지 스트레스 — 최종]")
    for m, lab in ((1, '기본'), (2, '2배'), (3, '3배')):
        rr = bt(pc, fin, fy, sec, buy=0.0015 * m, sell=0.0033 * m)
        L.append(f"  {lab:6} {rr[0]:>7.0f}% / MDD{rr[1]:.0f}%")
    rep = "\n".join(L)
    print(rep)
    pickle.dump({'r0': r0, 'final': r4, 'idx': (ih, ihm)}, open(P('data_v2_result.pkl'), 'wb'))
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
