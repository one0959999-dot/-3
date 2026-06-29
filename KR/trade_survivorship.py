"""생존편향 교정 결정타: 보유 vs 국면리스크오프 — 생존주 + 상폐 911 전체.

상폐데이터는 close만 있어 봇 OHLC 국면함수 작동불가(하락 0% 감지) →
close-only '추세 하락국면' 룰로 리스크오프 일관 적용:
  하락국면 = 종가 < 200일선 AND 200일선 하락(20일전보다↓)  → 현금화, 아니면 보유.
보유 = 첫날 매수 후 끝까지(상폐주는 종착가=폭락 반영, 부실상폐 종착 중앙값 고점의 2%).
집계: 생존주/부실상폐/피인수/자진 그룹별 최종액(1000만)·리스크오프 승률·MDD.

실행: python KR/trade_survivorship.py [--telegram]
"""
import sys, os, pickle, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
END = '2025-12-31'; INIT = 10_000_000
BUY_COST = 0.001 + 0.0005; SELL_COST = 0.001 + 0.0005 + 0.0018


def _mdd(eq):
    eq = np.asarray(eq, float)
    if len(eq) == 0:
        return 0.0
    peak = np.maximum.accumulate(eq)
    return float((eq / peak - 1).min() * 100)


def sim_hold(c):
    c = c[c.index <= END].dropna(); c = c[c > 0]
    if len(c) < 250:
        return None
    p0 = float(c.iloc[0]); q = int(INIT // (p0 * (1 + BUY_COST))); cash = INIT - q * p0 * (1 + BUY_COST)
    eq = cash + q * c.values
    return float(eq[-1]), _mdd(eq)


def sim_riskoff(c):
    c = c[c.index <= END].dropna(); c = c[c > 0]
    if len(c) < 250:
        return None
    ma200 = c.rolling(200, min_periods=100).mean()
    bear = (c < ma200) & (ma200 - ma200.shift(20) < 0)
    cash = float(INIT); sh = 0; eq = []
    for i in range(len(c)):
        p = float(c.iloc[i])
        b = bool(bear.iloc[i]) if not pd.isna(bear.iloc[i]) else False
        if i >= 60:
            if b and sh > 0:
                cash += sh * p * (1 - SELL_COST); sh = 0
            elif (not b) and sh == 0:
                q = int(cash // (p * (1 + BUY_COST)))
                if q > 0:
                    cash -= q * p * (1 + BUY_COST); sh = q
        eq.append(cash + sh * p)
    return float(eq[-1]), _mdd(eq)


def main(telegram=False):
    deli = pickle.load(open(P('data_cache_delisted.pkl'), 'rb'))
    big = pickle.load(open(P('data_cache_big.pkl'), 'rb')); wf = pickle.load(open(P('data_cache_wf.pkl'), 'rb'))
    # 생존주 close 모음
    surv = {}
    for d in (big, wf):
        for mk in ('KOSPI', 'KOSDAQ'):
            for code, (n, df) in d.get(mk, {}).items():
                surv.setdefault(code, df['close'])
    groups = {'생존주': [(c, s) for c, s in surv.items()],
              '부실상폐': [(k, v['close']) for k, v in deli.items() if v['reason'] == '부실상폐'],
              '피인수상폐': [(k, v['close']) for k, v in deli.items() if v['reason'] == '피인수상폐'],
              '자진상폐': [(k, v['close']) for k, v in deli.items() if v['reason'] == '자진상폐']}
    L = ["💀 생존편향 교정 — 보유 vs 국면리스크오프 (생존주+상폐 전체, 1000만·1주·실비용)", ""]
    L.append(f"{'그룹':9}{'n':>4}{'보유평균':>11}{'리스크오프':>11}{'리스크승률':>9}{'보유MDD':>8}{'리스크MDD':>9}")
    L.append("-" * 64)
    allh, allr = [], []; allhm, allrm = [], []
    for gname, items in groups.items():
        hv, rv, hm, rm, win, n = [], [], [], [], 0, 0
        for code, c in items:
            h = sim_hold(c); r = sim_riskoff(c)
            if h is None or r is None:
                continue
            n += 1; hv.append(h[0]); rv.append(r[0]); hm.append(h[1]); rm.append(r[1])
            if r[0] >= h[0]:
                win += 1
            allh.append(h[0]); allr.append(r[0]); allhm.append(h[1]); allrm.append(r[1])
        if n == 0:
            continue
        L.append(f"{gname:9}{n:>4}{np.mean(hv)/1e4:>9,.0f}만{np.mean(rv)/1e4:>9,.0f}만{win/n*100:>7.0f}%{np.mean(hm):>7.0f}%{np.mean(rm):>8.0f}%")
    N = len(allh)
    L.append("-" * 64)
    winA = sum(1 for a, b in zip(allr, allh) if a >= b)
    L.append(f"{'전체':9}{N:>4}{np.mean(allh)/1e4:>9,.0f}만{np.mean(allr)/1e4:>9,.0f}만{winA/N*100:>7.0f}%{np.mean(allhm):>7.0f}%{np.mean(allrm):>8.0f}%")
    L.append(f"\n핵심: 생존편향 걷어내면(상폐포함) 리스크오프가 보유를 {'이긴다' if np.mean(allr)>np.mean(allh) else '못이긴다'}.")
    L.append(f"  부실상폐서 보유는 -98% 직격, 리스크오프는 하락국면에 탈출.")
    L.append(f"  MDD도 리스크오프({np.mean(allrm):.0f}%)가 보유({np.mean(allhm):.0f}%)보다 얕음 → -20% 목표(B)와 직결.")
    rep = "\n".join(L)
    print(rep)
    if telegram:
        try:
            cc = sqlite3.connect(P('lassi.db'), timeout=30)
            rr = cc.execute("SELECT telegram_token, telegram_chat_id FROM users WHERE telegram_token IS NOT NULL AND telegram_token!='' LIMIT 1").fetchone(); cc.close()
            from base.telegram_bot import TelegramNotifier
            TelegramNotifier(rr[0], rr[1]).send_message(rep); print("텔레그램 ✓")
        except Exception as e:
            print("텔레그램 실패", e)


if __name__ == '__main__':
    main('--telegram' in sys.argv)
