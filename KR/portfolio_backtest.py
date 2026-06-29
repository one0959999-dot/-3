"""③ 분산 포트폴리오 백테스트 — -20% MDD가 분산으로 풀리는지 증명.

생존편향 교정: 생존주 + 상폐 전체(상장 중일 때만 보유, 상폐일 마지막가 청산).
구성요소(2단계 결론 전부 투입):
 - 부실/하락회피 선택: 가격>상승하는 200일선(부실상폐 87% 사전 -50%붕괴를 회피)
 - 분산: 후보 중 6개월 모멘텀 상위 N종목 동일비중
 - 국면 리스크오프: 코스피 하락국면이면 전액 현금
 - -20% 서킷브레이커: 포트폴리오 고점대비 -20%면 청산, 코스피 회복때 복귀
 - 월간 리밸런스, 1주 근사(동일비중 금액), 실거래비용
비교: 전략 vs 코스피보유 vs 생존주 동일비중보유.

실행: python KR/portfolio_backtest.py [--telegram] [N]
"""
import sys, os, pickle, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
END = '2025-12-31'; START = '2015-01-01'; INIT = 10_000_000
BUY_COST = 0.0015; SELL_COST = 0.0033


def mdd(eq):
    eq = np.asarray(eq, float); pk = np.maximum.accumulate(eq)
    return float((eq / pk - 1).min() * 100)


def build_panel():
    deli = pickle.load(open(P('data_cache_delisted.pkl'), 'rb'))
    big = pickle.load(open(P('data_cache_big.pkl'), 'rb')); wf = pickle.load(open(P('data_cache_wf.pkl'), 'rb'))
    closes = {}
    for d in (big, wf):
        for mk in ('KOSPI', 'KOSDAQ'):
            for code, (n, df) in d.get(mk, {}).items():
                closes.setdefault(code, df['close'])
    for k, v in deli.items():
        closes.setdefault(k, v['close'])
    panel = pd.DataFrame(closes).sort_index()
    panel = panel[(panel.index >= START) & (panel.index <= END)]
    kospi = wf['index']['KOSPI']['close']
    kospi = kospi[(kospi.index >= START) & (kospi.index <= END)]
    return panel, kospi


def bad_year_map():
    """부실 첫 발생연도(자본잠식 or 2년연속적자) → 그 해부터 제외(시점일관)."""
    c = sqlite3.connect(P('lassi.db')); rows = {}
    for t, y, cap, pi, ni in c.execute('SELECT ticker, year, capital, paidin, netincome FROM financials_dart'):
        rows.setdefault(t, []).append((y, cap, pi, ni))
    c.close()
    bad = {}
    for t, rs in rows.items():
        rs.sort(); run = 0; fy = None
        for y, cap, pi, ni in rs:
            imp = (cap is not None and cap < 0) or (cap is not None and pi not in (None, 0) and 0 <= cap < pi)
            run = run + 1 if (ni is not None and ni < 0) else 0
            if imp or run >= 2:
                fy = y; break
        if fy is not None:
            bad[t] = fy
    return bad


def backtest(panel, kospi, N=20, regime=True, breaker=True, select=True,
             sel_mode='mom', rebal='M', bad=None):
    bad = bad or {}
    ff = panel.ffill()
    ma200 = panel.rolling(200, min_periods=120).mean()
    up200 = ma200 > ma200.shift(20)
    eligible = (panel > ma200) & up200 if select else panel.notna()
    mom = ff / ff.shift(126) - 1
    vol = ff.pct_change().rolling(126).std()
    tradable = panel.notna()
    kma = kospi.rolling(200, min_periods=120).mean()
    kbear = ((kospi < kma) & (kma < kma.shift(20))).reindex(panel.index).ffill().fillna(False)

    dates = panel.index
    cash = float(INIT); sh = {}; eq = []; peak = INIT; halted = False
    cur_month = None
    for di, dt in enumerate(dates):
        px = ff.loc[dt]
        # 상폐 처리: 어제 거래되다 오늘 거래중단 → 마지막가 청산
        if di > 0:
            prev = tradable.iloc[di - 1]; now = tradable.loc[dt]
            for t in list(sh.keys()):
                if t in now.index and (not now[t]) and prev.get(t, False):
                    cash += sh[t] * float(px[t]) * (1 - SELL_COST); del sh[t]
        val = cash + sum(q * float(px[t]) for t, q in sh.items())
        peak = max(peak, val)
        # 서킷브레이커
        if breaker and not halted and val <= peak * 0.8:
            for t, q in list(sh.items()):
                if tradable.loc[dt].get(t, False):
                    cash += q * float(px[t]) * (1 - SELL_COST)
            sh = {t: q for t, q in sh.items() if not tradable.loc[dt].get(t, False)}
            halted = True
        if halted and not bool(kbear.loc[dt]):
            halted = False
        # 리밸런스 (월/분기)
        m = (dt.year, dt.month) if rebal == 'M' else (dt.year, (dt.month - 1) // 3)
        if m != cur_month:
            cur_month = m
            risk_off = (regime and bool(kbear.loc[dt])) or halted
            # 청산
            for t, q in list(sh.items()):
                if tradable.loc[dt].get(t, False):
                    cash += q * float(px[t]) * (1 - SELL_COST); del sh[t]
            if not risk_off:
                elig = [t for t in panel.columns if bool(eligible.loc[dt].get(t, False)) and bool(tradable.loc[dt].get(t, False))]
                elig = [t for t in elig if t not in bad or dt.year < bad[t]]  # 부실 제외(시점)
                if sel_mode == 'lowvol':
                    elig = [t for t in elig if not np.isnan(vol.loc[dt].get(t, np.nan))]
                    elig.sort(key=lambda t: vol.loc[dt][t])  # 저변동성 우선
                else:
                    elig = [t for t in elig if not np.isnan(mom.loc[dt].get(t, np.nan))]
                    elig.sort(key=lambda t: mom.loc[dt][t], reverse=True)
                picks = elig[:N]
                if picks:
                    per = (cash * 0.98) / len(picks)
                    for t in picks:
                        p = float(px[t])
                        if p > 0:
                            q = int(per // (p * (1 + BUY_COST)))
                            if q > 0:
                                cash -= q * p * (1 + BUY_COST); sh[t] = sh.get(t, 0) + q
        eq.append(cash + sum(q * float(px[t]) for t, q in sh.items()))
    return float(eq[-1]), mdd(eq), pd.Series(eq, index=dates)


def bench_hold_index(kospi):
    c = kospi.dropna()
    q = int(INIT // (c.iloc[0] * (1 + BUY_COST))); cash = INIT - q * c.iloc[0] * (1 + BUY_COST)
    eq = cash + q * c.values
    return float(eq[-1]), mdd(eq)


def bench_ew_hold(panel, N=20):
    # 시작시점 거래되는 종목 동일비중 보유(생존편향 포함 비교용은 아니지만 단순분산 기준)
    ff = panel.ffill(); first = panel.index[0]
    tradable0 = panel.loc[first].notna()
    tk = [t for t in panel.columns if tradable0[t]][:200]
    if not tk:
        return None, None
    per = INIT / len(tk); sh = {}
    for t in tk:
        p = float(panel.loc[first][t])
        if p > 0:
            sh[t] = (per / (p * (1 + BUY_COST)))
    eqs = []
    for dt in panel.index:
        px = ff.loc[dt]; eqs.append(sum(q * float(px[t]) for t, q in sh.items()))
    return float(eqs[-1]), mdd(eqs)


def main(telegram=False, N=50):
    panel, kospi = build_panel()
    bad = bad_year_map()
    L = [f"📊 ③-A 저변동성·우량주 포트폴리오 ({START[:4]}~2025, 1000만, 상폐포함·실비용)", ""]
    L.append(f"유니버스 {panel.shape[1]}종목, 분기리밸런스, 저변동성 {N}종목 동일비중, 부실({len(bad)}종목) 제외\n")
    fin_ns, md_ns, _ = backtest(panel, kospi, N=N, regime=False, breaker=False, select=True, sel_mode='lowvol', rebal='Q', bad=bad)
    fin_nb, md_nb, _ = backtest(panel, kospi, N=N, regime=True, breaker=False, select=True, sel_mode='lowvol', rebal='Q', bad=bad)
    fin, md, _ = backtest(panel, kospi, N=N, regime=True, breaker=True, select=True, sel_mode='lowvol', rebal='Q', bad=bad)
    ih, ihm = bench_hold_index(kospi)
    L.append(f"{'전략':30}{'최종액':>10}{'수익률':>8}{'MDD':>7}")
    L.append("-" * 57)
    def row(nm, f, m): return f"{nm:30}{f/1e4:>8,.0f}만{(f/INIT-1)*100:>7.0f}%{m:>7.0f}%"
    L.append(row("저변동성+분산(N=%d)" % N, fin_ns, md_ns))
    L.append(row("  +국면리스크오프", fin_nb, md_nb))
    L.append(row("  +(-20%서킷브레이커)", fin, md))
    L.append(row("[벤치] 코스피 보유", ih, ihm))
    L.append("")
    L.append(f"핵심: 저변동성 우량주 분산이 코스피보유({(ih/INIT-1)*100:.0f}%, MDD{ihm:.0f}%)를 이기고 MDD를 낮추나.")
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
    args = [a for a in sys.argv[1:] if a != '--telegram']
    main('--telegram' in sys.argv, int(args[0]) if args else 20)
