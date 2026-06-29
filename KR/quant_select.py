"""3단계 퀀트 종목선정 — 대형주+우량주+저변동성, 섹터분산. 누적 필터 기여 분석.

검증된 베이스(저변동성 분산 +549%/MDD-31%) 위에 필터를 쌓아 견고성 확인:
 ① 대형주: 자본총계 상위 풀(시점일관, 직전연도)
 ② 부실제외: 자본잠식·2년연속적자
 ③ 우량: ROE>0·흑자(직전 공시연도)
 ④ 저변동성 정렬 → N종목, 섹터 분산캡(섹터당 최대 N//4)
분기 리밸런스, 타이밍 없음, 상폐포함, 1주근사·실비용.

실행: python KR/quant_select.py [--telegram] [N]
"""
import sys, os, sqlite3, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd
from KR.portfolio_backtest import build_panel, bench_hold_index, mdd, INIT, BUY_COST, SELL_COST

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)


def load_fundamentals():
    c = sqlite3.connect(P('lassi.db'))
    fin = {}
    for t, y, cap, pi, ni in c.execute('SELECT ticker, year, capital, paidin, netincome FROM financials_dart'):
        fin.setdefault(t, {})[y] = (cap, pi, ni)
    sec = {t: s for t, s in c.execute("SELECT ticker, sector FROM ticker_sector WHERE market='KR'")}
    c.close()
    # 시점일관 조회용: 정렬된 연도
    fin_years = {t: sorted(d) for t, d in fin.items()}
    return fin, fin_years, sec


def fund_asof(fin, fin_years, t, year):
    """직전 공시연도(year-1 이하) 재무 반환 (룩어헤드 차단)."""
    ys = [y for y in fin_years.get(t, []) if y <= year - 1]
    if not ys:
        return None
    return fin[t][ys[-1]]


def bad_asof(fin, fin_years, t, year):
    """직전까지 자본잠식 or 2년연속적자 발생했나."""
    run = 0
    for y in fin_years.get(t, []):
        if y > year - 1:
            break
        cap, pi, ni = fin[t][y]
        imp = (cap is not None and cap < 0) or (cap is not None and pi not in (None, 0) and 0 <= cap < pi)
        run = run + 1 if (ni is not None and ni < 0) else 0
        if imp or run >= 2:
            return True
    return False


def backtest(panel, kospi, fin, fin_years, sec, N=50,
             large=False, quality=False, sectorcap=False, large_top=400):
    ff = panel.ffill()
    ma200 = panel.rolling(200, min_periods=120).mean()
    up200 = ma200 > ma200.shift(20)
    eligible = (panel > ma200) & up200
    vol = ff.pct_change().rolling(126).std()
    tradable = panel.notna()
    kma = kospi.rolling(200, min_periods=120).mean()
    kbear = ((kospi < kma) & (kma < kma.shift(20))).reindex(panel.index).ffill().fillna(False)
    cols = list(panel.columns)

    dates = panel.index
    cash = float(INIT); sh = {}; eq = []; cur_q = None
    for di, dt in enumerate(dates):
        px = ff.loc[dt]
        if di > 0:
            prev = tradable.iloc[di - 1]; now = tradable.loc[dt]
            for t in list(sh.keys()):
                if t in now.index and (not now[t]) and prev.get(t, False):
                    cash += sh[t] * float(px[t]) * (1 - SELL_COST); del sh[t]
        q = (dt.year, (dt.month - 1) // 3)
        if q != cur_q:
            cur_q = q
            for t, qq in list(sh.items()):
                if tradable.loc[dt].get(t, False):
                    cash += qq * float(px[t]) * (1 - SELL_COST); del sh[t]
            elig = [t for t in cols if bool(eligible.loc[dt].get(t, False)) and bool(tradable.loc[dt].get(t, False))
                    and not np.isnan(vol.loc[dt].get(t, np.nan))]
            # ② 부실 제외 (항상)
            elig = [t for t in elig if not bad_asof(fin, fin_years, t, dt.year)]
            # ① 대형주 컷
            if large:
                caps = []
                for t in elig:
                    f = fund_asof(fin, fin_years, t, dt.year)
                    if f and f[0] is not None:
                        caps.append((t, f[0]))
                caps.sort(key=lambda x: x[1], reverse=True)
                big = set(t for t, _ in caps[:large_top])
                elig = [t for t in elig if t in big]
            # ③ 우량: ROE>0·흑자
            if quality:
                keep = []
                for t in elig:
                    f = fund_asof(fin, fin_years, t, dt.year)
                    if f and f[2] is not None and f[2] > 0 and f[0] not in (None, 0) and f[2] / f[0] > 0:
                        keep.append(t)
                elig = keep
            # ④ 저변동성 정렬 + 섹터캡
            elig.sort(key=lambda t: vol.loc[dt][t])
            if sectorcap:
                cap_per = max(1, N // 4); cnt = {}; picks = []
                for t in elig:
                    s = sec.get(t, '기타')
                    if cnt.get(s, 0) < cap_per:
                        picks.append(t); cnt[s] = cnt.get(s, 0) + 1
                    if len(picks) >= N:
                        break
            else:
                picks = elig[:N]
            if picks:
                per = (cash * 0.98) / len(picks)
                for t in picks:
                    p = float(px[t])
                    if p > 0:
                        qq = int(per // (p * (1 + BUY_COST)))
                        if qq > 0:
                            cash -= qq * p * (1 + BUY_COST); sh[t] = sh.get(t, 0) + qq
        eq.append(cash + sum(qq * float(px[t]) for t, qq in sh.items()))
    return float(eq[-1]), mdd(eq)


def main(telegram=False, N=50):
    panel, kospi = build_panel()
    fin, fin_years, sec = load_fundamentals()
    ih, ihm = bench_hold_index(kospi)
    L = [f"📈 3단계 퀀트 종목선정 (2015~2025, 1000만, 상폐포함, 분기리밸, N={N})", ""]
    L.append(f"{'구성':30}{'수익률':>8}{'MDD':>7}")
    L.append("-" * 47)
    def row(nm, fm): return f"{nm:30}{(fm[0]/INIT-1)*100:>7.0f}%{fm[1]:>7.0f}%"
    base = backtest(panel, kospi, fin, fin_years, sec, N=N)
    L.append(row("저변동성+부실제외(베이스)", base))
    r1 = backtest(panel, kospi, fin, fin_years, sec, N=N, large=True)
    L.append(row(" +대형주컷(자본총계상위400)", r1))
    r2 = backtest(panel, kospi, fin, fin_years, sec, N=N, large=True, quality=True)
    L.append(row(" +우량(ROE>0·흑자)", r2))
    r3 = backtest(panel, kospi, fin, fin_years, sec, N=N, large=True, quality=True, sectorcap=True)
    L.append(row(" +섹터분산캡", r3))
    L.append(row("[벤치] 코스피보유", (ih, ihm)))
    L.append("\n각 필터가 +549% 베이스 대비 수익·MDD에 어떻게 기여하는지.")
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
    main('--telegram' in sys.argv, int(args[0]) if args else 50)
