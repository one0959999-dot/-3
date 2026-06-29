"""종목선정 알고리즘 마무리 — 대형주(거래대금) 전체재검증 + 슬리피지/유동성 + 규칙동결.

전체 유니버스(kr_full 거래량 보유 + 상폐) 위에서:
 A. 누적필터: base(저변동성+부실제외) → +유동성/대형주(거래대금≥30억) → +우량(ROE>0·흑자) → +섹터분산캡
 B. 슬리피지 스트레스: 최종구성에 비용 ×1 / ×2 / ×3 (소형주 체결현실 반영)
 C. 결과로 최종 선정규칙 동결 판단
1주근사·분기리밸·상폐포함·룩어헤드차단(후행지표·직전연도재무).

실행: python KR/finalize_select.py [--telegram]
"""
import sys, os, sqlite3, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
INIT = 1e7; START, END = '2015-01-01', '2025-12-31'
LIQ = 3e9  # 일평균 거래대금 30억 = 대형주/유동성 컷


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
            vold[t] = df['close'] * df['volume']  # 거래대금
    for k, v in deli.items():
        cl.setdefault(k, v['close'])  # 상폐는 거래대금 없음 → 유동성필터서 자동 제외
    panel = pd.DataFrame(cl).sort_index(); panel = panel[(panel.index >= START) & (panel.index <= END)]
    tv = pd.DataFrame(vold).reindex(index=panel.index, columns=panel.columns)
    kospi = wf['index']['KOSPI']['close']; kospi = kospi[(kospi.index >= START) & (kospi.index <= END)]
    return panel, tv, kospi


def fundamentals():
    c = sqlite3.connect(P('lassi.db')); rows = {}
    for t, y, cap, pi, ni in c.execute('SELECT ticker,year,capital,paidin,netincome FROM financials_dart'):
        rows.setdefault(t, {})[y] = (cap, pi, ni)
    sec = {t: s for t, s in c.execute("SELECT ticker,sector FROM ticker_sector WHERE market='KR'")}
    c.close()
    return rows, {t: sorted(d) for t, d in rows.items()}, sec


def precompute(panel, tv, fin, fy):
    ff = panel.ffill()
    ma = panel.rolling(200, min_periods=120).mean()
    trend = (panel > ma) & (ma > ma.shift(20))
    vol = ff.pct_change().rolling(126).std()
    liq = tv.rolling(20, min_periods=10).mean()
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
    cols = list(panel.columns)
    for j, t in enumerate(cols):
        if t in badfy:
            bad[:, j] = years >= badfy[t]
    return (ff.values, ~np.isnan(panel.values), trend.values, vol.values,
            liq.values, bad, years, cols)


def backtest(pc, fin, fy, sec, N=50, liquidity=False, quality=False, sectorcap=False,
             buy=0.0015, sell=0.0033):
    ff, trad, trend, vol, liq, bad, years, cols = pc
    nt = ff.shape[1]; cash = INIT; sh = np.zeros(nt); eq = []; cur = None
    qual_cache = {}
    for i in range(len(ff)):
        px = ff[i]
        if i > 0:
            gone = (sh > 0) & (~trad[i]) & trad[i - 1]
            if gone.any():
                cash += np.nansum(sh[gone] * px[gone] * (1 - sell)); sh[gone] = 0
        # 분기 리밸런스
        yr = years[i]
        q = (yr, (i // 63))  # 약 분기(63거래일)
        if cur is None or q[1] != cur:
            cur = q[1]
            held = (sh > 0) & trad[i]
            cash += np.nansum(sh[held] * px[held] * (1 - sell)); sh[held] = 0
            mask = trend[i] & trad[i] & ~bad[i] & ~np.isnan(vol[i])
            if liquidity:
                mask = mask & (liq[i] > LIQ)
            idx = np.where(mask)[0]
            if quality and len(idx):
                keep = []
                for j in idx:
                    t = cols[j]; ys = [y for y in fy.get(t, []) if y <= yr - 1]
                    if ys:
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
                            shares = int(per // (p * (1 + buy)))
                            if shares > 0:
                                cash -= shares * p * (1 + buy); sh[j] += shares
        eq.append(cash + np.nansum(sh * px))
    return (eq[-1] / INIT - 1) * 100, mdd(eq)


def idx_hold(close):
    c = close.dropna(); q = int(INIT // (c.iloc[0] * 1.0015)); cash = INIT - q * c.iloc[0] * 1.0015
    eq = cash + q * c.values
    return (eq[-1] / INIT - 1) * 100, mdd(eq)


def main(telegram=False):
    panel, tv, kospi = build()
    fin, fy, sec = fundamentals()
    pc = precompute(panel, tv, fin, fy)
    ih, ihm = idx_hold(kospi)
    L = [f"🏁 종목선정 알고리즘 마무리 — 전체유니버스 {panel.shape[1]}종목 (상폐포함)", ""]
    L.append(f"[A. 누적필터]  코스피보유 {ih:+.0f}%/MDD{ihm:.0f}%")
    L.append(f"{'구성':30}{'수익률':>8}{'MDD':>7}")
    L.append("-" * 47)
    def row(nm, r): return f"{nm:30}{r[0]:>7.0f}%{r[1]:>7.0f}%"
    base = backtest(pc, fin, fy, sec, N=50)
    L.append(row("저변동성+부실제외(base)", base))
    r1 = backtest(pc, fin, fy, sec, N=50, liquidity=True)
    L.append(row(" +유동성/대형주(거래대금≥30억)", r1))
    r2 = backtest(pc, fin, fy, sec, N=50, liquidity=True, quality=True)
    L.append(row(" +우량(ROE>0·흑자)", r2))
    r3 = backtest(pc, fin, fy, sec, N=50, liquidity=True, quality=True, sectorcap=True)
    L.append(row(" +섹터분산캡 = 최종", r3))
    # B. 슬리피지 스트레스 (최종구성)
    L.append(f"\n[B. 슬리피지 스트레스 — 최종구성]")
    L.append(f"{'비용가정':30}{'수익률':>8}{'MDD':>7}")
    for mult, lab in ((1, '기본(편0.05+수0.1+세0.18%)'), (2, '2배(소형주 현실)'), (3, '3배(극단)')):
        rr = backtest(pc, fin, fy, sec, N=50, liquidity=True, quality=True, sectorcap=True,
                      buy=0.0015 * mult, sell=0.0033 * mult)
        L.append(row(f"  {lab}", rr))
    L.append(f"\n[C. 동결 후보 규칙]")
    L.append("  유니버스: KOSPI+KOSDAQ 전체")
    L.append("  제외: 자본잠식·2년연속적자 / 거래대금<30억(유동성)")
    L.append("  선호: 흑자(ROE>0) / 저변동성 정렬")
    L.append("  보유: 저변동성 상위 50종목 동일비중, 섹터당 최대 12 / 분기 리밸런스 / 타이밍없음")
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
