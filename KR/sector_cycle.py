"""섹터 사이클 연구 — '잘나가는 업종 포착'이 v3에 이득인가 (정직 검증).

가설: 섹터별 모멘텀/추세로 강세 업종을 골라 그 안에서 종목선정하면 더 나은가?
테스트(2015~2026.6, 상폐포함, v3 규칙 위에서):
 A v3 베이스 (전 섹터 저변동 25)
 B 강세섹터限 (6개월 모멘텀 상위 섹터 종목만)
 C 약세섹터除 (하위 섹터 제외)
 D 섹터캡 (한 섹터 최대 N개 = 강제분산)
⚠️ 패널 경고: '타이밍=독', 섹터 52% 미분류. 이득 없으면 '섹터사이클 불채택' 확정.

실행: python KR/sector_cycle.py [--telegram]
"""
import sys, os, sqlite3, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import warnings
warnings.filterwarnings('ignore')
import numpy as np
import pandas as pd
from KR.step0_artifact_check import build, fundamentals, precompute, quality_idx, mdd, INIT, BUY, SELL
from KR.oos_2026 import splice

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)


def load_sectors():
    c = sqlite3.connect(P('lassi.db'))
    sec = {t: s for t, s in c.execute("SELECT ticker,sector FROM ticker_sector WHERE market='KR' AND sector IS NOT NULL AND sector!=''")}
    c.close()
    return sec


def build_panel2():
    panel, tv, kospi, deli = build(); fin, fy = fundamentals()
    d26 = pickle.load(open(P('data_cache_kr_2026.pkl'), 'rb')); d26.pop('__INDEX__', None)
    ext = {}
    for t, s in d26.items():
        if t in panel.columns:
            e, _ = splice(panel[t], s)
            if e is not None and len(e):
                ext[t] = e
    panel2 = pd.concat([panel, pd.DataFrame(ext).reindex(columns=panel.columns).sort_index()])
    return panel2, tv, deli, fin, fy


def run(pc, fin, fy, sec, panel2, mode, topk=3, seccap=5, N=25):
    ffv = pc['ff']; trad = pc['trad']; trend = pc['trend']; bad = pc['bad']; years = pc['years']; cols = pc['cols']
    vol = pc['vol_raw']; stale = pc['stale']; active = pc['active']
    # 섹터 6개월 모멘텀 (구성종목 평균)
    ff_df = panel2.ffill()
    mom = (ff_df.shift(21) / ff_df.shift(126) - 1)
    col_sec = [sec.get(c, '기타') for c in cols]
    sectors = sorted(set(s for s in col_sec if s != '기타'))
    sec_idx = {s: [j for j, cs in enumerate(col_sec) if cs == s] for s in sectors}
    mom_v = mom.values
    ND = len(ffv); cash = INIT; sh = np.zeros(ffv.shape[1]); eq = []; cur = None; pend = None
    for i in range(ND):
        px = ffv[i]
        if i > 0:
            gone = (sh > 0) & (~trad[i]) & trad[i - 1]
            if gone.any():
                cash += np.nansum(sh[gone] * px[gone] * (1 - SELL)); sh[gone] = 0
        if pend is not None:
            order = pend; pend = None
            held = (sh > 0) & trad[i]; cash += np.nansum(sh[held] * px[held] * (1 - SELL)); sh[held] = 0
            order = [j for j in order if trad[i][j]]
            if order:
                per = cash * 0.98 / len(order)
                for j in order:
                    p = px[j]
                    if p > 0:
                        q = int(per // (p * (1 + BUY)))
                        if q > 0:
                            cash -= q * p * (1 + BUY); sh[j] += q
        if cur is None or (i // 63) != cur:
            cur = i // 63; yr = years[i]
            mask = trend[i] & trad[i] & (~bad[i]) & ~np.isnan(vol[i]) & (stale[i] < 0.20) & active[i]
            idx = quality_idx(np.where(mask)[0], cols, fin, fy, yr)
            # 섹터 랭킹
            smom = {}
            for s, js in sec_idx.items():
                vals = [mom_v[i][j] for j in js if not np.isnan(mom_v[i][j])]
                if vals:
                    smom[s] = np.mean(vals)
            ranked = sorted(smom, key=lambda s: smom[s], reverse=True)
            if mode == 'top_sector':
                allow = set(ranked[:topk])
                idx = np.array([j for j in idx if col_sec[j] in allow], int)
            elif mode == 'excl_weak':
                weak = set(ranked[-max(1, len(ranked) // 4):])
                idx = np.array([j for j in idx if col_sec[j] not in weak], int)
            order = idx[np.argsort(vol[i][idx])] if len(idx) else np.array([], int)
            if mode == 'seccap':
                cnt = {}; pick = []
                for j in order:
                    s = col_sec[j]
                    if cnt.get(s, 0) < seccap:
                        pick.append(j); cnt[s] = cnt.get(s, 0) + 1
                    if len(pick) >= N:
                        break
                order = np.array(pick, int)
            else:
                order = order[:N]
            pend = list(order)
        eq.append(cash + np.nansum(sh * px))
    eq = np.array(eq); yrs = (panel2.index[-1] - panel2.index[0]).days / 365.25
    return ((eq[-1] / INIT) ** (1 / yrs) - 1) * 100, mdd(eq)


def main(telegram=False):
    panel2, tv, deli, fin, fy = build_panel2()
    pc = precompute(panel2, tv.reindex(index=panel2.index), deli, fin, fy)
    sec = load_sectors()
    ncov = sum(1 for c in panel2.columns if c in sec)
    L = [f"🔬 섹터 사이클 연구 — 강세업종 포착이 이득인가 ({panel2.shape[1]}종목, 섹터커버 {ncov})", ""]
    L.append(f"{'전략':26}{'CAGR':>7}{'MDD':>7}")
    L.append("-" * 40)
    tests = [('base', 'A v3 베이스(전섹터)'), ('top_sector', 'B 강세섹터上위3만'),
             ('excl_weak', 'C 약세섹터 제외'), ('seccap', 'D 섹터캡(강제분산)')]
    base_c = None
    for mode, nm in tests:
        c, m = run(pc, fin, fy, sec, panel2, mode)
        if mode == 'base':
            base_c = c
        mark = '' if mode == 'base' else (' ↑' if c > base_c else ' ↓')
        L.append(f"{nm:26}{c:>6.1f}%{m:>7.0f}%{mark}")
        print(L[-1], flush=True)
    L.append("")
    L.append("판정: 섹터 오버레이가 베이스(A)를 못 이기면 → 섹터사이클 불채택(패널 경고대로).")
    rep = "\n".join(L)
    print("\n" + rep)
    if telegram:
        try:
            c = sqlite3.connect(P('lassi.db'), timeout=30)
            r = c.execute("SELECT telegram_token, telegram_chat_id FROM users WHERE telegram_token IS NOT NULL AND telegram_token!='' LIMIT 1").fetchone(); c.close()
            from base.telegram_bot import TelegramNotifier
            TelegramNotifier(r[0], r[1]).send_message(rep)
        except Exception:
            pass


if __name__ == '__main__':
    main('--telegram' in sys.argv)
