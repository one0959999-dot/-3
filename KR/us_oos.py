"""미국 OOS — v3 동일 룰(저변동성 원시수익률 + 200MA추세 + 정체제외 + 최소거래일, 재무필터 없음).

목적: '검증'(한국 과적합 여부) — 배포 아님. 유니버스 = 미국 대형/중형 ~230(현존 종목 = 생존편향
있음을 명시. 단 대형주라 상폐영향 작고, 메커니즘 재현 여부가 핵심). 벤치 = SPY(auto_adjust
= 배당포함 TR — 전략도 조정가라 대칭).
N=30, 분기리밸, t+1 체결, 2015~2025.

실행: python KR/us_oos.py [--telegram]
"""
import sys, os, pickle, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import warnings
warnings.filterwarnings('ignore')
import numpy as np
import pandas as pd
import yfinance as yf

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
START, END = '2014-01-01', '2025-12-31'
INIT = 1e7; BUY = 0.0010; SELL = 0.0010  # 미국: 세금없음·저비용

TICKERS = """AAPL MSFT NVDA AMZN GOOGL META TSLA AVGO BRK-B LLY JPM V UNH XOM MA PG COST HD JNJ WMT
ABBV NFLX BAC CRM ORCL CVX MRK KO AMD PEP ADBE TMO CSCO ACN MCD LIN ABT WFC IBM GE TXN QCOM CAT
DHR VZ INTU AMGN PFE NOW ISRG NEE SPGI UBER PM RTX HON UNP GS COP T LOW BKNG ELV SYK AXP BLK MDT
VRTX PLD TJX SCHW C LMT ADP CB MMC AMT SBUX DE PANW GILD MDLZ BSX ETN ADI LRCX REGN CI MU BX FI
KLAC SNPS EOG CME SO ITW DUK CDNS SLB BDX CSX ICE TGT MPC WM MCK CL EMR SHW APH PYPL PSX AON FCX
ROP NXPI PH MSI ECL APD TT CMG ORLY WELL MCO AJG NSC AZO CARR OXY TDG PCAR FTNT NKE GM F DAL AAL
UAL LUV ROST DG DLTR EL KMB GIS K HSY STZ KDP KHC HRL CAG CPB SJM TSN TAP MKC CHD CLX MNST DIS
CMCSA CHTR TMUS AMAT INTC HPQ DELL WDC STX NTAP CRWD SNOW NET DDOG ZS OKTA TEAM MDB PLTR ABNB
DASH COIN SHOP SPOT RBLX U TWLO PINS SNAP LYFT ZM DOCU ROKU ETSY EBAY MAR HLT RCL CCL NCLH MGM
WYNN LVS DPZ YUM QSR DRI BBY KR SYY WBA CVS HUM CNC MOH ZTS IDXX IQV A EW HCA UHS DGX LH BIIB
MRNA ILMN ALGN DXCM PODD RMD BAX BMY VTRS GEHC KVUE CEG VST SMCI ARM APP AXON TPL ERIE BRO""".split()


def mdd(eq):
    eq = np.asarray(eq, float); pk = np.maximum.accumulate(eq)
    return float((eq / pk - 1).min() * 100)


def main(telegram=False):
    print(f"미국 유니버스 {len(TICKERS)}종목 수집...", flush=True)
    data = yf.download(TICKERS + ['SPY'], start=START, end=END, progress=False, auto_adjust=True,
                       threads=True, group_by='ticker')
    cl = {}
    for t in TICKERS:
        try:
            c = data[t]['Close'].dropna()
            if len(c) >= 300:
                cl[t] = c
        except Exception:
            continue
    spy = data['SPY']['Close'].dropna()
    panel = pd.DataFrame(cl).sort_index()
    panel = panel[(panel.index >= '2015-01-01') & (panel.index <= END)]
    spy = spy[(spy.index >= '2015-01-01') & (spy.index <= END)]
    print(f"수집 {panel.shape[1]}종목 / {len(panel)}일", flush=True)
    # v3 동일 룰 (재무필터 없음)
    ff = panel.ffill()
    ma = panel.rolling(200, min_periods=120).mean()
    trend = ((panel > ma) & (ma > ma.shift(20))).values
    ret = panel.pct_change()
    vol = ret.rolling(126, min_periods=60).std().values
    zero = ((ret == 0) & panel.notna()).rolling(126, min_periods=60).sum()
    days = panel.notna().rolling(126, min_periods=60).sum()
    stale = (zero / days).values
    active = (days >= 100).values
    trad = ~np.isnan(panel.values)
    ffv = ff.values
    N = 30
    cash = INIT; sh = np.zeros(panel.shape[1]); eq = []; cur = None; pending = None
    for i in range(len(panel)):
        px = ffv[i]
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
        if cur is None or (i // 63) != cur:
            cur = i // 63
            mask = trend[i] & trad[i] & ~np.isnan(vol[i]) & (stale[i] < 0.20) & active[i]
            idx = np.where(mask)[0]
            pending = list(idx[np.argsort(vol[i][idx])][:N]) if len(idx) else []
        eq.append(cash + np.nansum(sh * px))
    yrs = len(panel) / 252
    smult = eq[-1] / INIT; scagr = (smult ** (1 / yrs) - 1) * 100
    bmult = spy.iloc[-1] / spy.iloc[0]; bcagr = (bmult ** (1 / yrs) - 1) * 100
    beq = (spy / spy.iloc[0]).values * INIT
    # 연도별
    se = pd.Series(eq, index=panel.index); sp = spy.reindex(panel.index).ffill()
    ylines = []; ywin = 0; ytot = 0
    for y in range(2015, 2026):
        a = se[se.index.year == y]; b = sp[sp.index.year == y]
        if len(a) < 2:
            continue
        rs = (a.iloc[-1] / a.iloc[0] - 1) * 100; rb = (b.iloc[-1] / b.iloc[0] - 1) * 100
        ok = rs > rb; ywin += ok; ytot += 1
        ylines.append(f"  {y}: {rs:+5.0f}% vs SPY {rb:+5.0f}%  {'승' if ok else '패'}")
    L = [f"🇺🇸 미국 OOS — v3 동일룰 ({panel.shape[1]}종목, 재무필터 없음, N=30·분기·t+1)", ""]
    L.append(f"전략: {(smult-1)*100:+.0f}% (CAGR {scagr:.1f}%) MDD {mdd(eq):.0f}%")
    L.append(f"SPY(배당포함): {(bmult-1)*100:+.0f}% (CAGR {bcagr:.1f}%) MDD {mdd(beq):.0f}%")
    L.append(f"→ 초과 {scagr-bcagr:+.1f}%p/년, MDD {'개선' if mdd(eq)>mdd(beq) else '악화'}")
    L += ylines
    L.append(f"연도별 승률: {ywin}/{ytot}")
    L.append("⚠️ 유니버스=현존 대형주(생존편향)라 절대수익 과대 가능 — 메커니즘(방어형·MDD개선) 재현 여부가 핵심.")
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
