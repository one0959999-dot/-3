"""0단계: 아티팩트 제거 재검증 — 엣지 존재 판정 (패널 지적 반영).

패널 지적 아티팩트:
 A. 상폐주 유동성 우회(liq=LIQ*10 sentinel) = 미래정보 + 피인수 고정가주(변동성≈0)를 저변동 랭킹이 대량 선택
 B. ffill 변동성 = 정체가격(무변동) 종목을 저변동 상위로 밀어올림
 C. 당일 신호·당일 체결(t+0)
 D. 벤치마크 배당 비대칭(전략=배당포함 조정가, 코스피=가격지수)

변형:
 V0 현행 동결규칙 재현 (아티팩트 포함 — 기준선)
 V1 상폐 유동성우회 제거 (상폐주는 거래대금 없어 탈락 — 패널 반사실)
 V2 대칭 규칙: 거래대금필터 대신 [정체가격 제외(126일 무변동일>20%) + 최소거래일 100/126] 전종목 동일적용
    + 변동성은 원시수익률(ffill 아님) — 상폐·생존 공평, 고정가주 자동 제외
 V3 V2 + t+1 체결 (신호일 다음날 가격으로 매매)
 V4 V3 + 종목당 비중캡 2% (부족분 현금 — 약세장 몰빵 차단)
벤치: 코스피 가격지수 + 배당 ~1.8%/년 가산한 TR 근사 병기.

실행: python KR/step0_artifact_check.py [--telegram]
"""
import sys, os, sqlite3, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
INIT = 1e7; START, END = '2015-01-01', '2025-12-31'
LIQ = 3e9; BUY = 0.0015; SELL = 0.0033


def mdd(eq):
    eq = np.asarray(eq, float); pk = np.maximum.accumulate(eq)
    return float((eq / pk - 1).min() * 100)


def cagr(mult, yrs):
    return (mult ** (1 / yrs) - 1) * 100


def build():
    full = pickle.load(open(P('data_cache_kr_full.pkl'), 'rb'))
    deli = pickle.load(open(P('data_cache_delisted.pkl'), 'rb'))
    wf = pickle.load(open(P('data_cache_wf.pkl'), 'rb'))
    cl = {}; vold = {}
    for t, df in full.items():
        cl[t] = df['close']
        if 'volume' in df.columns:
            vold[t] = df['close'] * df['volume']
    deli_only = set()
    for k, v in deli.items():
        if k not in cl:
            cl[k] = v['close']; deli_only.add(k)
    panel = pd.DataFrame(cl).sort_index(); panel = panel[(panel.index >= START) & (panel.index <= END)]
    tv = pd.DataFrame(vold).reindex(index=panel.index, columns=panel.columns)
    kospi = wf['index']['KOSPI']['close']; kospi = kospi[(kospi.index >= START) & (kospi.index <= END)]
    return panel, tv, kospi, deli_only


def fundamentals():
    c = sqlite3.connect(P('lassi.db')); rows = {}
    for t, y, cap, pi, ni in c.execute('SELECT ticker,year,capital,paidin,netincome FROM financials_dart'):
        rows.setdefault(t, {})[y] = (cap, pi, ni)
    c.close()
    return rows, {t: sorted(d) for t, d in rows.items()}


def precompute(panel, tv, deli_only, fin, fy):
    ff = panel.ffill()
    ma = panel.rolling(200, min_periods=120).mean()
    trend = ((panel > ma) & (ma > ma.shift(20))).values
    vol_ff = ff.pct_change().rolling(126, min_periods=60).std().values      # 기존(아티팩트 B 포함)
    ret_raw = panel.pct_change()
    vol_raw = ret_raw.rolling(126, min_periods=60).std().values             # 원시수익률 변동성
    zero = ((ret_raw == 0) & panel.notna()).rolling(126, min_periods=60).sum()
    days = panel.notna().rolling(126, min_periods=60).sum()
    stale = (zero / days).values                                             # 무변동일 비율
    active = (days >= 100).values                                            # 최소 거래일
    liq = tv.rolling(20, min_periods=10).mean().values
    liq_bypass = liq.copy()                                                  # V0: 상폐 sentinel
    notna = ~np.isnan(panel.values)
    cols = list(panel.columns)
    for j, t in enumerate(cols):
        if t in deli_only:
            liq_bypass[:, j] = np.where(notna[:, j], LIQ * 10, np.nan)
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
            bad[:, j] = years >= badfy[t] + 1
    return dict(ff=ff.values, trad=notna, trend=trend, vol_ff=vol_ff, vol_raw=vol_raw,
                stale=stale, active=active, liq=liq, liq_bypass=liq_bypass,
                bad=bad, years=years, cols=cols)


def quality_idx(idx, cols, fin, fy, yr):
    keep = []
    for j in idx:
        t = cols[j]; ys = [y for y in fy.get(t, []) if y <= yr - 1]
        if not ys:
            keep.append(j)
        else:
            cap, pi, ni = fin[t][ys[-1]]
            if ni is not None and ni > 0 and cap not in (None, 0) and ni / cap > 0:
                keep.append(j)
    return np.array(keep, int)


def bt(pc, fin, fy, N=50, liq_mode='bypass', vol_mode='ff', exec_lag=0, cap=None):
    ff = pc['ff']; trad = pc['trad']; trend = pc['trend']; bad = pc['bad']; years = pc['years']; cols = pc['cols']
    vol = pc['vol_ff'] if vol_mode == 'ff' else pc['vol_raw']
    n_days, nt = ff.shape
    cash = INIT; sh = np.zeros(nt); eq = []; cur = None; pending = None
    for i in range(n_days):
        px = ff[i]
        if i > 0:
            gone = (sh > 0) & (~trad[i]) & trad[i - 1]
            if gone.any():
                cash += np.nansum(sh[gone] * px[gone] * (1 - SELL)); sh[gone] = 0
        # 지연 체결
        if pending is not None:
            order = pending; pending = None
            held = (sh > 0) & trad[i]
            cash += np.nansum(sh[held] * px[held] * (1 - SELL)); sh[held] = 0
            order = [j for j in order if trad[i][j]]
            if order:
                per_base = cash * 0.98 / len(order)
                for j in order:
                    p = px[j]
                    if p > 0:
                        per = per_base if cap is None else min(per_base, (cash + np.nansum(sh * px)) * cap)
                        q = int(per // (p * (1 + BUY)))
                        if q > 0:
                            cash -= q * p * (1 + BUY); sh[j] += q
        if cur is None or (i // 63) != cur:
            cur = i // 63; yr = years[i]
            mask = trend[i] & trad[i] & ~bad[i] & ~np.isnan(vol[i])
            if liq_mode == 'bypass':
                mask = mask & (pc['liq_bypass'][i] > LIQ)
            elif liq_mode == 'strict':
                mask = mask & (pc['liq'][i] > LIQ)
            else:  # symmetric
                mask = mask & (pc['stale'][i] < 0.20) & pc['active'][i]
            idx = np.where(mask)[0]
            idx = quality_idx(idx, cols, fin, fy, yr)
            order = idx[np.argsort(vol[i][idx])][:N] if len(idx) else np.array([], int)
            if exec_lag == 0:
                held = (sh > 0) & trad[i]
                cash += np.nansum(sh[held] * px[held] * (1 - SELL)); sh[held] = 0
                if len(order):
                    per_base = cash * 0.98 / len(order)
                    for j in order:
                        p = px[j]
                        if p > 0:
                            per = per_base if cap is None else min(per_base, (cash + np.nansum(sh * px)) * cap)
                            q = int(per // (p * (1 + BUY)))
                            if q > 0:
                                cash -= q * p * (1 + BUY); sh[j] += q
            else:
                pending = list(order)
        eq.append(cash + np.nansum(sh * px))
    yrs = n_days / 252
    return (eq[-1] / INIT - 1) * 100, mdd(eq), cagr(eq[-1] / INIT, yrs)


def main(telegram=False):
    panel, tv, kospi, deli_only = build()
    fin, fy = fundamentals()
    pc = precompute(panel, tv, deli_only, fin, fy)
    ks = kospi.dropna(); yrs = len(panel) / 252
    kmult = ks.iloc[-1] / ks.iloc[0]
    k_ret = (kmult - 1) * 100; k_cagr = cagr(kmult, yrs)
    k_tr_cagr = k_cagr + 1.8  # 배당 근사
    keq = INIT * (ks / ks.iloc[0]).values
    L = [f"⚖️ 0단계: 아티팩트 제거 재검증 — 엣지 존재 판정 ({panel.shape[1]}종목)", ""]
    L.append(f"벤치: 코스피 {k_ret:+.0f}% (CAGR {k_cagr:.1f}%) / 배당포함 근사 CAGR ~{k_tr_cagr:.1f}% / MDD {mdd(keq):.0f}%")
    L.append("")
    L.append(f"{'변형':34}{'수익률':>8}{'CAGR':>7}{'MDD':>7}")
    L.append("-" * 58)
    tests = [
        ("V0 현행규칙(아티팩트 포함)", dict(liq_mode='bypass', vol_mode='ff', exec_lag=0)),
        ("V1 상폐 유동성우회 제거", dict(liq_mode='strict', vol_mode='ff', exec_lag=0)),
        ("V2 대칭규칙(정체제외+원시변동성)", dict(liq_mode='symmetric', vol_mode='raw', exec_lag=0)),
        ("V3 V2 + t+1 체결", dict(liq_mode='symmetric', vol_mode='raw', exec_lag=1)),
        ("V4 V3 + 종목당 2%캡", dict(liq_mode='symmetric', vol_mode='raw', exec_lag=1, cap=0.02)),
    ]
    res = {}
    for name, kw in tests:
        r, m, c = bt(pc, fin, fy, **kw)
        res[name] = (r, m, c)
        L.append(f"{name:34}{r:>7.0f}%{c:>6.1f}%{m:>7.0f}%")
        print(L[-1], flush=True)
    L.append("-" * 58)
    v3 = res["V3 V2 + t+1 체결"]
    edge = v3[2] - k_tr_cagr
    L.append(f"\n판정 기준: V3(정직판)가 코스피 배당포함(~{k_tr_cagr:.1f}%)을 CAGR로 이기고 MDD 개선?")
    L.append(f"  V3 CAGR {v3[2]:.1f}% vs 벤치TR {k_tr_cagr:.1f}% → 초과 {edge:+.1f}%p, MDD {v3[1]:.0f}% vs {mdd(keq):.0f}%")
    L.append(f"  → {'엣지 생존' if edge > 1 and v3[1] > mdd(keq) else ('경계선' if edge > 0 else '엣지 소멸')}")
    rep = "\n".join(L)
    print("\n" + rep)
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
