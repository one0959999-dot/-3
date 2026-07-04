"""US 전체 유니버스 검증 — KR v3와 100% 동일 규칙 vs SPY.

규칙(재무필터 없음=미국 DART 없음, 나머지 동일): 저변동성(원시126일)+200MA추세+정체가격제외
  +최소거래일. 50종목 동일비중은 KR 저변동 슬리브와 대칭. N/구성 실험.
비교: 종목픽 단독 vs SPY(TR) vs 50/50 혼합. 2015~2026.6.
⚠️ US 상폐 데이터 부재 → 현재상장 유니버스(생존편향). 단 생존편향은 종목픽에 *유리* →
   그럼에도 SPY에 지면 'US=지수' 확정, 이기면 KR과 통일 검토.

실행: python KR/us_full_backtest.py [--telegram]
"""
import sys, os, pickle, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import warnings
warnings.filterwarnings('ignore')
import numpy as np
import pandas as pd

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
INIT = 1e7; BUY = 0.001; SELL = 0.001; START = '2015-01-01'


def mdd(eq):
    eq = np.asarray(eq, float); pk = np.maximum.accumulate(eq); return float((eq / pk - 1).min() * 100)


def cagr(mult, yrs):
    return (mult ** (1 / yrs) - 1) * 100


def bt(panel, N=25, rebal=63):
    ff = panel.ffill()
    ma = panel.rolling(200, min_periods=120).mean()
    trend = ((panel > ma) & (ma > ma.shift(20))).values
    ret = panel.pct_change(); vol = ret.rolling(126, min_periods=60).std().values
    zero = ((ret == 0) & panel.notna()).rolling(126, min_periods=60).sum()
    days = panel.notna().rolling(126, min_periods=60).sum()
    stale = (zero / days).values; active = (days >= 100).values
    trad = ~np.isnan(panel.values); ffv = ff.values
    cash = INIT; sh = np.zeros(panel.shape[1]); eq = []; cur = None; pend = None
    for i in range(len(panel)):
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
        if cur is None or (i // rebal) != cur:
            cur = i // rebal
            mask = trend[i] & trad[i] & ~np.isnan(vol[i]) & (stale[i] < 0.20) & active[i]
            idx = np.where(mask)[0]
            pend = list(idx[np.argsort(vol[i][idx])][:N]) if len(idx) else []
        eq.append(cash + np.nansum(sh * px))
    return pd.Series(eq, index=panel.index)


def main(telegram=False):
    d = pickle.load(open(P('data_cache_us_full.pkl'), 'rb'))
    spy = d.pop('SPY')['close'] if 'SPY' in d else None
    cl = {t: df['close'] for t, df in d.items()}
    panel = pd.DataFrame(cl).sort_index(); panel = panel[panel.index >= START]
    spy = spy[spy.index >= START]
    yrs = (panel.index[-1] - panel.index[0]).days / 365.25
    v3 = bt(panel, N=25)
    vr = v3.pct_change().fillna(0).values
    sp = spy.reindex(panel.index).ffill(); sr = sp.pct_change().fillna(0).values
    def seg26(s):
        c = s.index <= '2025-12-31'; return (float(s.iloc[-1]) / float(s[c].iloc[-1]) - 1) * 100
    def blend(w):
        a = INIT * w; b = INIT * (1 - w); e = []
        for i in range(len(panel)):
            a *= (1 + vr[i]); b *= (1 + sr[i])
            if i % 63 == 0 and i > 0:
                t = a + b; a = t * w; b = t * (1 - w)
            e.append(a + b)
        return pd.Series(e, index=panel.index)
    L = [f"🇺🇸 US 전체 유니버스 검증 — KR v3 동일규칙 ({panel.shape[1]}종목, 상폐없음=생존편향)", ""]
    L.append(f"{'구성(2015~2026.6)':24}{'CAGR':>7}{'MDD':>7}{'2026':>7}")
    spm = spy.iloc[-1] / spy.iloc[0]
    L.append(f"{'SPY 단독(TR)':24}{cagr(spm, yrs):>6.1f}%{mdd(sp.values):>6.0f}%{seg26(sp):>+6.0f}%")
    L.append(f"{'v3 저변동 25 단독':24}{cagr(v3.iloc[-1]/INIT, yrs):>6.1f}%{mdd(v3.values):>6.0f}%{seg26(v3):>+6.0f}%")
    for w in (0.5, 0.3):
        e = blend(w)
        L.append(f"{f'v3 {int(w*100)}% + SPY {int((1-w)*100)}%':24}{cagr(e.iloc[-1]/INIT, yrs):>6.1f}%{mdd(e.values):>6.0f}%{seg26(e):>+6.0f}%")
    v3c = cagr(v3.iloc[-1]/INIT, yrs); spc = cagr(spm, yrs)
    L.append(f"\n판정: v3 종목픽 {v3c:.1f}% vs SPY {spc:.1f}% → " +
             ("KR처럼 종목픽 우세 = 통일 가능" if v3c > spc + 1 else "SPY 우세 = US는 지수중심 확정"))
    L.append("⚠️ 생존편향은 종목픽에 유리한데도 이 결과 = 실제(상폐포함)론 종목픽이 더 불리.")
    rep = "\n".join(L); print(rep)
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
