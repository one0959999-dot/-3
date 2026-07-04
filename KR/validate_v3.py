"""① 검증엔진 통일 — v3 동결규칙 '그대로' 파라미터 민감도·롤링윈도우·연도별 재검증.

기존 validate_quant.py는 동결규칙과 다른 엔진(유동성·품질·재무랙 없음)으로 돌려 증거 오염(패널 지적).
여기서는 step0의 v3 엔진(대칭규칙: 정체가격제외+최소거래일, 원시수익률 변동성, t+1 체결,
부실 재무랙+1년, 품질 ROE>0)을 단일 소스로 재사용:
 [1] 파라미터 민감도: N={30,50,100} × 리밸런스={21,63,126일}
 [2] 롤링 5년: 2015-19 … 2020-24 (6구간, 80% 중첩=독립표본 ~2 주의 병기)
 [3] 연도별 승패: 전략 vs 코스피TR(가격지수+1.8%p/년 근사)
벤치: 코스피 배당포함 근사(CAGR+1.8%p).

실행: python KR/validate_v3.py [--telegram]
"""
import sys, os, sqlite3, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd
from KR.step0_artifact_check import (build, fundamentals, precompute, quality_idx,
                                     mdd, cagr, INIT, BUY, SELL)

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
DIV = 1.8  # 코스피 배당 근사 %p/년


def bt_v3(pc, fin, fy, N=50, rebal_days=63, ret_eq=False):
    """v3 동결규칙: 대칭 유동성 + 원시변동성 + t+1 체결."""
    ff = pc['ff']; trad = pc['trad']; trend = pc['trend']; bad = pc['bad']
    years = pc['years']; cols = pc['cols']; vol = pc['vol_raw']
    stale = pc['stale']; active = pc['active']
    n_days, nt = ff.shape
    cash = INIT; sh = np.zeros(nt); eq = []; cur = None; pending = None
    for i in range(n_days):
        px = ff[i]
        if i > 0:
            gone = (sh > 0) & (~trad[i]) & trad[i - 1]
            if gone.any():
                cash += np.nansum(sh[gone] * px[gone] * (1 - SELL)); sh[gone] = 0
        if pending is not None:
            order = pending; pending = None
            held = (sh > 0) & trad[i]
            cash += np.nansum(sh[held] * px[held] * (1 - SELL)); sh[held] = 0
            order = [j for j in order if trad[i][j]]
            if order:
                per = cash * 0.98 / len(order)
                for j in order:
                    p = px[j]
                    if p > 0:
                        q = int(per // (p * (1 + BUY)))
                        if q > 0:
                            cash -= q * p * (1 + BUY); sh[j] += q
        if cur is None or (i // rebal_days) != cur:
            cur = i // rebal_days; yr = years[i]
            mask = trend[i] & trad[i] & ~bad[i] & ~np.isnan(vol[i]) & (stale[i] < 0.20) & active[i]
            idx = np.where(mask)[0]
            idx = quality_idx(idx, cols, fin, fy, yr)
            pending = list(idx[np.argsort(vol[i][idx])][:N]) if len(idx) else []
        eq.append(cash + np.nansum(sh * px))
    if ret_eq:
        return np.array(eq)
    yrs = n_days / 252
    return (eq[-1] / INIT - 1) * 100, mdd(eq), cagr(eq[-1] / INIT, yrs)


def slice_pc(panel, tv, kospi, deli_only, fin, fy, s, e):
    pn = panel[(panel.index >= s) & (panel.index <= e)]
    tvn = tv.reindex(index=pn.index)
    ks = kospi[(kospi.index >= s) & (kospi.index <= e)]
    return precompute(pn, tvn, deli_only, fin, fy), pn, ks


def idx_stats(ks, n_days):
    kmult = ks.dropna().iloc[-1] / ks.dropna().iloc[0]
    yrs = n_days / 252
    kc = cagr(kmult, yrs)
    keq = (ks.dropna() / ks.dropna().iloc[0]).values * INIT
    return kc, kc + DIV, mdd(keq)


def main(telegram=False):
    panel, tv, kospi, deli_only = build()
    fin, fy = fundamentals()
    pc = precompute(panel, tv, deli_only, fin, fy)
    kc, ktr, km = idx_stats(kospi, len(panel))
    L = [f"✅ ① 검증엔진 통일 — v3 동결규칙 그대로 재검증 ({panel.shape[1]}종목)", ""]
    L.append(f"벤치(2015~25): 코스피 CAGR {kc:.1f}% / TR근사 {ktr:.1f}% / MDD {km:.0f}%")
    # [1] 파라미터 민감도
    L.append(f"\n[1] 파라미터 민감도 (v3 엔진)")
    L.append(f"{'설정':14}{'CAGR':>7}{'MDD':>7}{'판정':>8}")
    win = tot = 0
    for N in (30, 50, 100):
        for rd, lab in ((21, '월'), (63, '분기'), (126, '반기')):
            r, m, c = bt_v3(pc, fin, fy, N=N, rebal_days=rd)
            ok = c > ktr and m > km
            win += ok; tot += 1
            L.append(f"N{N}·{lab:3}{'':6}{c:>6.1f}%{m:>7.0f}%{'승' if ok else ('수익승' if c>ktr else '패'):>8}")
            print(L[-1], flush=True)
    L.append(f"→ CAGR·MDD 둘다 TR벤치 우위: {win}/{tot}")
    # [2] 롤링 5년
    L.append(f"\n[2] 롤링 5년 (N=50·분기, 중첩표본 주의: 유효독립 ~2)")
    L.append(f"{'구간':12}{'CAGR':>7}{'MDD':>7}{'벤치TR':>8}{'벤치MDD':>8}")
    rwin = rtot = 0
    for s in range(2015, 2021):
        pcw, pnw, ksw = slice_pc(panel, tv, kospi, deli_only, fin, fy, f'{s}-01-01', f'{s+4}-12-31')
        r, m, c = bt_v3(pcw, fin, fy, N=50, rebal_days=63)
        kcw, ktrw, kmw = idx_stats(ksw, len(pnw))
        ok = c > ktrw and m > kmw
        rwin += ok; rtot += 1
        L.append(f"{s}-{str(s+4)[2:]:8}{c:>6.1f}%{m:>7.0f}%{ktrw:>7.1f}%{kmw:>7.0f}%{' ✓' if ok else ''}")
        print(L[-1], flush=True)
    L.append(f"→ 둘다 우위: {rwin}/{rtot}")
    # [3] 연도별
    eq = bt_v3(pc, fin, fy, N=50, rebal_days=63, ret_eq=True)
    se = pd.Series(eq, index=panel.index)
    ksr = kospi.reindex(panel.index).ffill()
    L.append(f"\n[3] 연도별 (전략 vs 코스피+{DIV}%p)")
    ywin = ytot = 0
    for y in range(2015, 2026):
        a = se[se.index.year == y]; b = ksr[ksr.index.year == y]
        if len(a) < 2:
            continue
        rs = (a.iloc[-1] / a.iloc[0] - 1) * 100
        rk = (b.iloc[-1] / b.iloc[0] - 1) * 100 + DIV
        ok = rs > rk; ywin += ok; ytot += 1
        L.append(f"  {y}: 전략 {rs:+5.0f}% vs 벤치TR {rk:+5.0f}%  {'승' if ok else '패'}")
    L.append(f"→ 연도별 승률: {ywin}/{ytot}")
    L.append(f"\n종합: 파라미터 {win}/{tot} · 롤링 {rwin}/{rtot} · 연도 {ywin}/{ytot} (전부 v3 엔진, 벤치=배당포함)")
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
