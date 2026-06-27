"""워크포워드 실전형 백테스트 — '답안지' 없이, 매 시점 실시간 국면판단 → 국면별 기법 매매.

사용자 요구:
- 투자원금 1000만원. 지금까지 순위로 확정한 [국면→매매기법] 알고리즘을 타이밍 맞춰 적용.
- 국면은 사후 최적라벨(답안지)이 아니라, 실제 classify_phase 로직으로 '그 시점까지의 데이터만' 보고 판단.
  (=어제까지 보고 국면확정 → 오늘 그대로 매매. 룩어헤드 없음. 실전 그대로.)
- 코스피/코스닥 분리(각자 지수·인버스ETF로 판단·매매).
- 알고리즘 실효성 + 문제점(전환 휩쏘/감지지연/인버스드래그/비용)을 본다. 1회 과거 백테스트.

[국면 → 기법] (순위 1위 확정안)
  패닉/하락초기/하락중반 → 인버스(방어)
  하락말기(바닥)         → 박스권스윙
  회복초입/상승초입/상승중반 → 박스권스윙
  상승말기               → 200일선 위 보유
  횡보                   → 보유

검증: 알고리즘 포트(1000만) vs 단순보유 포트(1000만), 코스피/코스닥 각각. + 문제점 진단.
실행: python KR/walkforward_backtest.py [--telegram]
"""
import sys, os, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd

from KR.regime_period_backtest import _yf, SAMPLE_KR, COST
from KR.final_algorithm import EXTRA_KR

PRINCIPAL = 10_000_000      # 1000만원
START = '2018-01-01'        # 200MA 워밍업 후 실거래 시작
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data_cache_wf.pkl')

# 코스닥 종목(명시) — 나머지는 코스피. (yfinance .KS/.KQ 자동판별이 부정확해 직접 지정)
KOSDAQ_SET = {'101490', '000250', '247540', '086520', '028300',
              '196170', '058470', '240810', '357780'}

# 시장별 지수 / 인버스 ETF
MARKETS = {
    'KOSPI':  {'index': '^KS11', 'inverse': '114800.KS'},   # KODEX 인버스
    'KOSDAQ': {'index': '^KQ11', 'inverse': '251340.KS'},   # KODEX 코스닥150선물인버스
}

# [국면 → 매매기법] 알고리즘 (순위 1위 확정안)
ALGO = {
    'PANIC': '인버스', 'BEAR_EARLY': '인버스', 'BEAR_MID': '인버스',
    'BEAR_LATE': '박스권스윙', 'RECOVERY': '박스권스윙',
    'BULL_EARLY': '박스권스윙', 'BULL_MID': '박스권스윙',
    'BULL_LATE': '200일선', 'SIDEWAYS': '보유',
}
PHASE_KR = {'PANIC': '패닉', 'BEAR_EARLY': '하락초기', 'BEAR_MID': '하락중반',
            'BEAR_LATE': '하락말기', 'RECOVERY': '회복초입', 'BULL_EARLY': '상승초입',
            'BULL_MID': '상승중반', 'BULL_LATE': '상승말기', 'SIDEWAYS': '횡보', 'UNKNOWN': '미확정'}
UNIVERSE = SAMPLE_KR + EXTRA_KR


# ──────────────────────────────────────────────────────────────────────────
# ADX (base.market_phase._adx와 동일 공식)
# ──────────────────────────────────────────────────────────────────────────
def _adx_series(df, period=14):
    high, low, close = df['high'], df['low'], df['close']
    tr = pd.concat([high - low, (high - close.shift(1)).abs(),
                    (low - close.shift(1)).abs()], axis=1).max(axis=1)
    up, dn = high.diff(), -low.diff()
    plus_dm = up.where((up > dn) & (up > 0), 0.0)
    minus_dm = dn.where((dn > up) & (dn > 0), 0.0)
    atr = tr.rolling(period).mean()
    pdi = 100 * plus_dm.rolling(period).mean() / (atr + 1e-10)
    mdi = 100 * minus_dm.rolling(period).mean() / (atr + 1e-10)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi + 1e-10)
    return dx.rolling(period).mean()


def classify_phase_walkforward(idx_df, vix):
    """실제 base.market_phase.classify_phase 트리를 일별 워크포워드로 재현.
    각 날짜는 '그 시점까지의 데이터'만으로 판단(룩어헤드 없음). VIX 포함."""
    c = idx_df['close']
    ma60, ma120, ma200 = c.rolling(60).mean(), c.rolling(120).mean(), c.rolling(200).mean()
    mom20 = (c / c.shift(20) - 1) * 100
    mom60 = (c / c.shift(60) - 1) * 100
    vs200 = (c / ma200 - 1) * 100
    hi52 = c.rolling(252).max()
    vs52 = (c / hi52 - 1) * 100
    slope = (ma200 / ma200.shift(20) - 1) * 100
    adx = _adx_series(idx_df)
    v = vix.reindex(c.index).ffill().fillna(0) if vix is not None else pd.Series(0.0, index=c.index)

    out = []
    for i in range(len(c)):
        if i < 200 or pd.isna(ma200.iloc[i]):
            out.append('UNKNOWN'); continue
        cur = c.iloc[i]; m200 = ma200.iloc[i]; m20 = mom20.iloc[i]; m60 = mom60.iloc[i]
        ax = adx.iloc[i]; sl = slope.iloc[i]; v52 = vs52.iloc[i]; vx = v.iloc[i]
        m60v, m120v = ma60.iloc[i], ma120.iloc[i]
        if vx > 40 and m20 < -8:                         ph = 'PANIC'
        elif cur < m200 * 0.92 and m20 < -5:             ph = 'BEAR_MID'
        elif cur < m200 and m60 < -15:                   ph = 'BEAR_MID'
        elif cur < m200 and m20 < -3:                    ph = 'BEAR_EARLY'
        elif cur < m200 and ax < 18:                     ph = 'BEAR_LATE'
        elif cur > m200 and m20 > 3 and m60 < -10:       ph = 'RECOVERY'
        elif cur > m200 and sl > 0:
            if v52 > -5:                                 ph = 'BULL_LATE'
            elif cur > m60v > m120v and ax > 25:
                ph = 'BULL_EARLY' if (m20 > 0 and m60 < 15) else 'BULL_MID'
            else:                                        ph = 'BULL_MID'
        else:                                            ph = 'SIDEWAYS'
        out.append(ph)
    return pd.Series(out, index=c.index)


# ──────────────────────────────────────────────────────────────────────────
# 기법별 종목 일별 목표비중(0~1)
# ──────────────────────────────────────────────────────────────────────────
def _box_swing(df):
    c = df['close']; ma = c.rolling(20).mean(); sd = c.rolling(20).std()
    lo, up = ma - 2 * sd, ma + 2 * sd
    e = (c < lo).fillna(False).values; x = (c > up).fillna(False).values
    h = False; out = np.zeros(len(c))
    for i in range(len(c)):
        if not h and e[i]: h = True
        elif h and x[i]: h = False
        out[i] = 1.0 if h else 0.0
    return pd.Series(out, index=c.index)


def _above200(df):
    return (df['close'] > df['close'].rolling(200).mean()).astype(float)


def stock_method_weight(df, method):
    if method == '보유':     return pd.Series(1.0, index=df.index)
    if method == '현금':     return pd.Series(0.0, index=df.index)
    if method == '인버스':   return pd.Series(0.0, index=df.index)   # 종목은 비우고 인버스ETF로
    if method == '박스권스윙': return _box_swing(df)
    if method == '200일선':  return _above200(df)
    return pd.Series(1.0, index=df.index)


# ──────────────────────────────────────────────────────────────────────────
# 데이터 로드(1회 캐시) — 종목별 시장(코스피/코스닥) 자동판별
# ──────────────────────────────────────────────────────────────────────────
def _fetch_market(code):
    mkt = 'KOSDAQ' if code in KOSDAQ_SET else 'KOSPI'
    suf = '.KQ' if mkt == 'KOSDAQ' else '.KS'
    d = _yf(code + suf)
    if d is not None and len(d) > 250 and 'close' in d.columns:
        return d[['open', 'high', 'low', 'close', 'volume']].astype(float).dropna(subset=['close']), mkt
    return None, None


def load(force=False):
    if os.path.exists(CACHE) and not force:
        with open(CACHE, 'rb') as f:
            return pickle.load(f)
    print("  데이터 다운로드(1회)...")
    data = {'KOSPI': {}, 'KOSDAQ': {}, 'index': {}, 'inverse': {}}
    vix = _yf('^VIX')
    data['vix'] = vix['close'] if vix is not None else None
    for mkt, m in MARKETS.items():
        di = _yf(m['index']); data['index'][mkt] = di
        dv = _yf(m['inverse']); data['inverse'][mkt] = dv
        print(f"    {mkt} 지수/인버스 ✓" if di is not None and dv is not None else f"    {mkt} 지수/인버스 일부실패")
    for i, (code, name) in enumerate(UNIVERSE, 1):
        df, mkt = _fetch_market(code)
        if df is not None and len(df) >= 400 and mkt:
            data[mkt][code] = (name, df)
            print(f"    [{i}/{len(UNIVERSE)}] {mkt[:2]} {name}")
        else:
            print(f"    [{i}/{len(UNIVERSE)}] ✗ {name}")
    with open(CACHE, 'wb') as f:
        pickle.dump(data, f)
    print(f"  캐시 저장 → {CACHE}")
    return data


# ──────────────────────────────────────────────────────────────────────────
# 포트폴리오 시뮬레이션 (1000만원, 동일가중, 워크포워드 국면→기법)
# ──────────────────────────────────────────────────────────────────────────
def simulate(data, mkt, algo=True):
    """algo=True: 국면별 알고리즘. algo=False: 단순보유 벤치마크."""
    stocks = data[mkt]
    idx_df = data['index'][mkt]
    inv_df = data['inverse'][mkt]
    if not stocks or idx_df is None:
        return None
    # 공통 거래일(시작일 이후)
    cal = idx_df.index[idx_df.index >= START]
    if len(cal) < 100:
        return None
    # 워크포워드 국면(어제까지 판단 → 오늘 매매: shift1)
    phase = classify_phase_walkforward(idx_df, data.get('vix'))
    phase_t = phase.reindex(idx_df.index).ffill().shift(1).reindex(cal).fillna('UNKNOWN')
    inv_ret = inv_df['close'].pct_change().reindex(cal).fillna(0.0) if inv_df is not None else pd.Series(0.0, index=cal)

    # 종목별 일수익 + 기법별 비중 사전계산
    srets, mweights = {}, {}
    for code, (name, df) in stocks.items():
        srets[code] = df['close'].pct_change().reindex(cal).fillna(0.0)
        if algo:
            mweights[code] = {mth: stock_method_weight(df, mth).reindex(cal).ffill().fillna(0.0)
                              for mth in set(ALGO.values()) if mth != '인버스'}
    codes = list(stocks.keys()); N = len(codes)

    # 일별 포트수익
    eq = [PRINCIPAL]
    prev_wstock = {c: 0.0 for c in codes}; prev_winv = 0.0
    cost_sum = 0.0; switches = 0; prev_ph = None
    inv_days = 0; inv_pnl_contrib = 0.0
    phase_days = {}
    for d in cal:
        ph = phase_t.loc[d]
        phase_days[ph] = phase_days.get(ph, 0) + 1
        if prev_ph is not None and ph != prev_ph:
            switches += 1
        prev_ph = ph
        method = ALGO.get(ph, '보유') if algo else '보유'
        # 비중 결정
        winv = 0.0; wstock = {}
        if algo and method == '인버스':
            winv = 1.0
            for c in codes: wstock[c] = 0.0
        else:
            for c in codes:
                w = float(mweights[c][method].loc[d]) if algo else 1.0
                wstock[c] = w / N         # 동일가중
        # 일수익 = Σ 종목 + 인버스 - 전환비용
        port_r = winv * float(inv_ret.loc[d])
        for c in codes:
            port_r += wstock[c] * float(srets[c].loc[d])
        # 전환비용(비중 변동분)
        turn = abs(winv - prev_winv) + sum(abs(wstock[c] - prev_wstock[c]) for c in codes)
        cost = turn * COST
        cost_sum += cost * eq[-1]
        port_r -= cost
        if winv > 0:
            inv_days += 1; inv_pnl_contrib += winv * float(inv_ret.loc[d])
        eq.append(eq[-1] * (1 + port_r))
        prev_wstock = wstock; prev_winv = winv
    eqs = pd.Series(eq[1:], index=cal)
    peak = eqs.cummax(); mdd = float(((eqs / peak - 1) * 100).min())
    yrs = len(cal) / 252
    ret = (eqs.iloc[-1] / PRINCIPAL - 1) * 100
    cagr = ((eqs.iloc[-1] / PRINCIPAL) ** (1 / yrs) - 1) * 100 if yrs > 0 else 0
    return {'final': eqs.iloc[-1], 'ret': ret, 'cagr': cagr, 'mdd': mdd, 'eq': eqs,
            'switches': switches, 'cost': cost_sum, 'inv_days': inv_days,
            'inv_pnl': inv_pnl_contrib * 100, 'phase_days': phase_days,
            'days': len(cal), 'N': N}


def report(data):
    L = []
    L.append("🧪 워크포워드 실전형 백테스트 (1000만원, 실시간 국면판단·답안지無)")
    L.append(f"기간 {START}~ · 코스피/코스닥 분리 · [국면→기법] 알고리즘 vs 단순보유")
    L.append("=" * 60)
    for mkt in ('KOSPI', 'KOSDAQ'):
        algo = simulate(data, mkt, algo=True)
        hold = simulate(data, mkt, algo=False)
        if not algo or not hold:
            L.append(f"\n[{mkt}] 데이터부족"); continue
        L.append(f"\n[{mkt}] {algo['N']}종목 · {algo['days']}거래일(~{algo['days']/252:.1f}년)")
        L.append(f"  {'알고리즘':10} 1000만→{algo['final']/1e4:,.0f}만  수익 {algo['ret']:+.0f}%  연{algo['cagr']:+.0f}%  MDD {algo['mdd']:.0f}%")
        L.append(f"  {'단순보유':10} 1000만→{hold['final']/1e4:,.0f}만  수익 {hold['ret']:+.0f}%  연{hold['cagr']:+.0f}%  MDD {hold['mdd']:.0f}%")
        diff = algo['ret'] - hold['ret']
        L.append(f"  → 알고리즘 {'승 🟢' if diff>0 else '패 🔴'} (보유대비 {diff:+.0f}%p)")
        L.append(f"  [문제점 진단]")
        L.append(f"   · 국면전환 {algo['switches']}회 (휩쏘 위험) · 누적 전환비용 {algo['cost']/1e4:,.0f}만원")
        L.append(f"   · 인버스 방어일 {algo['inv_days']}일 · 인버스 기여수익 {algo['inv_pnl']:+.0f}%(누적합산)")
        topph = sorted(algo['phase_days'].items(), key=lambda x: -x[1])[:4]
        L.append(f"   · 실시간 감지 국면분포: " + ", ".join(f"{PHASE_KR.get(p,p)}{d}일" for p, d in topph))
    L.append("\n" + "=" * 60)
    return "\n".join(L), data


def send_telegram(text):
    import sqlite3
    db = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lassi.db')
    c = sqlite3.connect(db, timeout=30)
    r = c.execute("SELECT telegram_token, telegram_chat_id FROM users "
                  "WHERE telegram_token IS NOT NULL AND telegram_token!='' LIMIT 1").fetchone()
    c.close()
    if not r:
        print("  텔레그램 자격증명 없음"); return
    from base.telegram_bot import TelegramNotifier
    TelegramNotifier(r[0], r[1]).send_message(text)
    print("  텔레그램 전송 완료 ✓")


if __name__ == '__main__':
    data = load(force='--refresh' in sys.argv)
    rep, _ = report(data)
    print("\n" + rep)
    if '--telegram' in sys.argv:
        send_telegram(rep)
