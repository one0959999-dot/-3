"""수급(거래대금) 선행 가설 검증 — '돈이 먼저 들어오는 섹터 = 다음에 뜰 섹터'인가.

가설(사용자): 거래량/거래대금이 가격보다 선행. 섹터로 수급이 몰리면 곧 오른다.
테스트(2015~2026.6, v3 규칙 위): 분기 리밸런스때 섹터를 수급신호로 필터 후 저변동 25선정.
 A 베이스 (전 섹터)
 B 거래대금 급증 섹터限 (최근20일/직전60일 거래대금비 상위)
 C 수급+가격 동반 (거래대금급증 AND 가격모멘텀 양)
 D ★선행 (거래대금급증 AND 가격모멘텀 낮음 = 돈은 왔는데 가격 아직) ← 가설 핵심
 E 수급 약한 섹터 제외
⚠️ 거래대금=종가×거래량. 섹터플로우는 생존주(거래량보유)로 집계.

실행: python KR/volume_flow.py [--telegram]
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


def main(telegram=False):
    panel, tv, kospi, deli = build(); fin, fy = fundamentals()
    d26 = pickle.load(open(P('data_cache_kr_2026.pkl'), 'rb')); d26.pop('__INDEX__', None)
    ext = {}
    for t, s in d26.items():
        if t in panel.columns:
            e, _ = splice(panel[t], s)
            if e is not None and len(e):
                ext[t] = e
    panel2 = pd.concat([panel, pd.DataFrame(ext).reindex(columns=panel.columns).sort_index()])
    pc = precompute(panel2, tv.reindex(index=panel2.index), deli, fin, fy)
    ffv = pc['ff']; trad = pc['trad']; trend = pc['trend']; bad = pc['bad']; years = pc['years']; cols = pc['cols']
    vol = pc['vol_raw']; stale = pc['stale']; active = pc['active']

    # 섹터 매핑 + 거래대금 패널
    c = sqlite3.connect(P('lassi.db'))
    sec = {t: s for t, s in c.execute("SELECT ticker,sector FROM ticker_sector WHERE market='KR' AND sector!='' AND sector!='기타'")}
    c.close()
    full = pickle.load(open(P('data_cache_kr_full.pkl'), 'rb'))
    # 종목별 거래대금 시계열 (종가×거래량), panel2 인덱스에 정렬
    tv_amt = {}
    for t, df in full.items():
        if t in sec and 'volume' in df.columns:
            tv_amt[t] = (df['close'] * df['volume'])
    amt = pd.DataFrame(tv_amt).reindex(index=panel2.index).ffill()
    col_sec = [sec.get(cc, None) for cc in cols]
    sectors = sorted(set(v for v in col_sec if v))
    sidx = {s: [j for j, cs in enumerate(col_sec) if cs == s] for s in sectors}

    # 섹터별 거래대금 합 → 수급 급증비(20/60), 가격 모멘텀(126d)
    ff_df = panel2.ffill()
    price_mom = (ff_df.shift(21) / ff_df.shift(126) - 1)
    pm = price_mom.values
    sec_amt = {}  # sector -> Series
    for s, js in sidx.items():
        members = [cols[j] for j in js if cols[j] in amt.columns]
        if members:
            sec_amt[s] = amt[members].sum(axis=1)
    sec_amt_df = pd.DataFrame(sec_amt)
    surge = (sec_amt_df.rolling(20).mean() / sec_amt_df.rolling(60).mean())  # >1 = 수급 급증
    surge_v = surge.values; surge_cols = list(surge.columns)
    surge_ci = {s: i for i, s in enumerate(surge_cols)}

    def sec_signal(i, mode):
        """i시점 섹터별 신호 → 허용 섹터 집합."""
        row = surge_v[i] if i < len(surge_v) else None
        sg = {s: row[surge_ci[s]] for s in surge_cols if not np.isnan(row[surge_ci[s]])} if row is not None else {}
        if not sg:
            return None
        # 섹터 가격모멘텀
        spm = {}
        for s, js in sidx.items():
            vals = [pm[i][j] for j in js if not np.isnan(pm[i][j])]
            if vals:
                spm[s] = np.mean(vals)
        ranked_surge = sorted(sg, key=lambda s: sg[s], reverse=True)
        top_surge = set(ranked_surge[:4])
        if mode == 'surge':
            return top_surge
        if mode == 'confirm':  # 수급급증 AND 가격 양
            return set(s for s in top_surge if spm.get(s, 0) > 0)
        if mode == 'lead':  # ★수급급증 AND 가격 아직 낮음(하위절반)
            med = np.median(list(spm.values())) if spm else 0
            return set(s for s in top_surge if spm.get(s, 99) <= med)
        if mode == 'excl_weak':
            weak = set(ranked_surge[-max(1, len(ranked_surge) // 3):])
            return set(surge_cols) - weak
        return None

    def run(mode):
        cash = INIT; sh = np.zeros(ffv.shape[1]); eq = []; cur = None; pend = None; N = 25
        for i in range(len(ffv)):
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
                if mode != 'base':
                    allow = sec_signal(i, mode)
                    if allow is not None:
                        idx = np.array([j for j in idx if col_sec[j] in allow], int)
                pend = list(idx[np.argsort(vol[i][idx])][:N]) if len(idx) else []
            eq.append(cash + np.nansum(sh * px))
        eq = np.array(eq); yrs = (panel2.index[-1] - panel2.index[0]).days / 365.25
        return ((eq[-1] / INIT) ** (1 / yrs) - 1) * 100, mdd(eq)

    L = [f"💰 수급(거래대금) 선행 가설 검증 — '돈 먼저 들어온 섹터=다음 상승?' ({len(sectors)}섹터)", ""]
    L.append(f"{'전략':30}{'CAGR':>7}{'MDD':>7}")
    L.append("-" * 44)
    base_c = None
    for mode, nm in [('base', 'A 베이스(전섹터)'), ('surge', 'B 거래대금 급증섹터'),
                     ('confirm', 'C 수급+가격 동반'), ('lead', 'D ★선행(돈왔는데 가격아직)'),
                     ('excl_weak', 'E 수급약한 섹터제외')]:
        cc, m = run(mode)
        if mode == 'base':
            base_c = cc
        mark = '' if mode == 'base' else (f' ↑{cc-base_c:+.1f}' if cc > base_c else f' ↓{cc-base_c:+.1f}')
        L.append(f"{nm:30}{cc:>6.1f}%{m:>7.0f}%{mark}")
        print(L[-1], flush=True)
    L.append("\n판정: D(선행)가 베이스를 유의하게 이기면 → 수급선행 채택 검토. 아니면 불채택.")
    rep = "\n".join(L); print("\n" + rep)
    if telegram:
        try:
            cc = sqlite3.connect(P('lassi.db'), timeout=30)
            r = cc.execute("SELECT telegram_token, telegram_chat_id FROM users WHERE telegram_token IS NOT NULL AND telegram_token!='' LIMIT 1").fetchone(); cc.close()
            from base.telegram_bot import TelegramNotifier
            TelegramNotifier(r[0], r[1]).send_message(rep)
        except Exception:
            pass


if __name__ == '__main__':
    main('--telegram' in sys.argv)
