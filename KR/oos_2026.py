"""② 2026 상반기 진짜 OOS — v3 동결규칙을 2026 데이터로 연장 실행 (백테스트 아님, 미래 대조).

방법: 2015~2025 패널에 2026 YTD를 '비율 스플라이스'(조정가 기준차 보정)로 연결 →
bt_v3를 전 기간 걸어 2026 구간만 추출 (리밸런스도 걸어서 진짜 walk-forward).
정합성 검사: 12월 겹침 구간 가격 일치율, 지수 일변동 이상치(>15%) 여부.

실행: python KR/oos_2026.py [--telegram]
"""
import sys, os, pickle, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd
from KR.step0_artifact_check import build, fundamentals, precompute, quality_idx, mdd, INIT
from KR.validate_v3 import bt_v3, DIV

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)


def splice(old, new):
    """겹치는 마지막 거래일 비율로 new를 old 스케일에 맞춰 연장분만 반환."""
    common = old.dropna().index.intersection(new.dropna().index)
    if len(common) == 0:
        return None, None
    d = common[-1]
    f = float(old[d]) / float(new[d])
    ext = new[new.index > old.dropna().index[-1]] * f
    return ext, abs(f - 1)


def main(telegram=False):
    panel, tv, kospi, deli_only = build()
    fin, fy = fundamentals()
    d26 = pickle.load(open(P('data_cache_kr_2026.pkl'), 'rb'))
    idx26 = d26.pop('__INDEX__')
    if hasattr(idx26, 'columns'):
        idx26 = idx26.iloc[:, 0]
    # 지수 스플라이스 + 정합성
    kext, kdev = splice(kospi, idx26)
    kospi2 = pd.concat([kospi, kext])
    kret = kospi2.pct_change().dropna()
    k26 = kret[kret.index >= '2026-01-01']
    jump = (k26.abs() > 0.15).sum()
    L = [f"🔮 ② 2026 상반기 진짜 OOS (v3 동결규칙 walk-forward)", ""]
    L.append(f"[정합성] 지수 스플라이스 편차 {kdev*100:.1f}% / 2026 일변동>15% {jump}회 / "
             f"지수 {float(kospi2[kospi2.index<='2025-12-31'].iloc[-1]):,.0f}→{float(kospi2.iloc[-1]):,.0f}")
    # 종목 스플라이스
    devs = []; ext_cols = {}
    for t, s in d26.items():
        if t in panel.columns:
            ext, dev = splice(panel[t], s)
            if ext is not None and len(ext):
                ext_cols[t] = ext; devs.append(dev)
    L.append(f"[정합성] 종목 연장 {len(ext_cols)}개 / 스플라이스 편차 중앙값 {np.median(devs)*100:.2f}% "
             f"(0%=완전일치, 배당낙 보정분)")
    ext_df = pd.DataFrame(ext_cols).reindex(columns=panel.columns).sort_index()
    panel2 = pd.concat([panel, ext_df])
    tv2 = tv.reindex(index=panel2.index)
    pc2 = precompute(panel2, tv2, deli_only, fin, fy)
    eq = bt_v3(pc2, fin, fy, N=50, rebal_days=63, ret_eq=True)
    se = pd.Series(eq, index=panel2.index)
    s26 = se[se.index >= '2026-01-01']
    s25 = float(se[se.index <= '2025-12-31'].iloc[-1])
    strat_r = (float(s26.iloc[-1]) / s25 - 1) * 100
    k25 = float(kospi2[kospi2.index <= '2025-12-31'].iloc[-1])
    k_r = (float(kospi2.iloc[-1]) / k25 - 1) * 100
    smdd = mdd(np.concatenate([[s25], s26.values]) / s25 * INIT)
    kmdd = mdd(np.concatenate([[k25], kospi2[kospi2.index >= '2026-01-01'].values]) / k25 * INIT)
    L.append("")
    L.append(f"[2026 YTD 결과 — 학습에 안 쓴 진짜 미래]")
    L.append(f"  전략 v3   : {strat_r:+.1f}%  (구간 MDD {smdd:.0f}%)")
    L.append(f"  코스피    : {k_r:+.1f}%  (+배당근사 {k_r + DIV/2:+.1f}%, 구간 MDD {kmdd:.0f}%)")
    L.append(f"  → {'승' if strat_r > k_r + DIV/2 else '패'} ({strat_r - k_r - DIV/2:+.1f}%p)")
    # 참고: 2026 첫 리밸런스 픽 10개
    names = {}
    try:
        c = sqlite3.connect(P('lassi.db'))
        names = {t: n for t, n in c.execute('SELECT ticker,name FROM kr_ticker_cache')}
        c.close()
    except Exception:
        pass
    i0 = panel2.index.get_indexer([panel2.index[panel2.index >= '2026-01-01'][0]])[0]
    trend = pc2['trend']; trad = pc2['trad']; bad = pc2['bad']; vol = pc2['vol_raw']
    stale = pc2['stale']; active = pc2['active']; cols = pc2['cols']
    mask = trend[i0] & trad[i0] & ~bad[i0] & ~np.isnan(vol[i0]) & (stale[i0] < 0.20) & active[i0]
    idx = quality_idx(np.where(mask)[0], cols, fin, fy, 2026)
    top = idx[np.argsort(vol[i0][idx])][:10]
    L.append(f"\n[2026 첫 리밸런스 픽 상위10] " + ", ".join(names.get(cols[j], cols[j]) for j in top))
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
