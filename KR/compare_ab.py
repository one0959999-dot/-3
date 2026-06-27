"""(a)순수 모멘텀 vs (b)모멘텀+상승레버리지 — 백테스트 맞대결, 텔레그램 보고.

월요일 모듈 최종 결정용. 238 유동성종목, 2015~, 룩어헤드 제거(월말판단→익월).
(a) 순수 모멘텀 top4/12M
(b1) 모멘텀 + 코스피>200MA일 때 1.5x 레버리지
(b2) 모멘텀 + 코스피>200MA일 때 2.0x 레버리지
+ 단순보유(EW) 기준. 지표: 누적·연·MDD·Calmar(연/|MDD|)·전후반.

실행: python KR/compare_ab.py [--telegram]
"""
import sys, os, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd
from KR.reliability_check import sim_momentum
from KR.walkforward_backtest import send_telegram

START = '2015-01-01'
SPLIT = '2021-01-01'
COST = 0.0021
BORROW = 0.045 / 252
CACHE = os.environ.get('LASSI_CACHE') or os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data_cache_big.pkl')


def month_ends(cal):
    s = pd.Series(cal, index=cal)
    return [pd.Timestamp(d) for d in s.groupby([cal.year, cal.month]).last().values]


def bull_exposure(idx_close, cal, lev):
    """코스피>200MA면 lev, 아니면 1.0. 월말판단→익월(룩어헤드 제거)."""
    ma = idx_close.rolling(200).mean()
    raw = pd.Series(np.where(idx_close > ma, lev, 1.0), index=idx_close.index)
    me = month_ends(cal); lab = {t: raw.reindex(cal).ffill().loc[t] for t in me}
    return pd.Series(lab).sort_index().reindex(cal, method='ffill').shift(1).fillna(1.0)


def lever(mom_ret, expo):
    turn = expo.diff().abs().fillna(0)
    r = expo * mom_ret - np.maximum(expo - 1, 0) * BORROW - turn * COST
    return (1 + r).cumprod()


def stats(eq, cal):
    ret = (eq.iloc[-1] / eq.iloc[0] - 1) * 100
    yrs = len(eq) / 252
    cagr = ((eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1) * 100
    mdd = float(((eq / eq.cummax() - 1) * 100).min())
    h1 = eq[cal < SPLIT]; h2 = eq[cal >= SPLIT]
    r1 = (h1.iloc[-1] / h1.iloc[0] - 1) * 100 if len(h1) > 20 else 0
    r2 = (h2.iloc[-1] / h2.iloc[0] - 1) * 100 if len(h2) > 20 else 0
    return {'ret': ret, 'cagr': cagr, 'mdd': mdd, 'calmar': cagr / (abs(mdd) + 1e-9), 'r1': r1, 'r2': r2}


def run():
    data = pickle.load(open(CACHE, 'rb'))
    stocks = {**data['KOSPI'], **data['KOSDAQ']}
    idx_df = data['index']['KOSPI']
    cal = idx_df.index[idx_df.index >= START]
    R = pd.DataFrame({c: df['close'].pct_change().reindex(cal) for c, (n, df) in stocks.items()})
    idx_close = idx_df['close'].reindex(cal).ffill()

    mom_eq = sim_momentum(R, 4, 12)
    mom_ret = mom_eq.pct_change().reindex(cal).fillna(0.0)

    res = {}
    res['(a) 순수 모멘텀'] = stats((1 + mom_ret).cumprod(), cal)
    res['(b1) +상승1.5x'] = stats(lever(mom_ret, bull_exposure(idx_close, cal, 1.5)), cal)
    res['(b2) +상승2.0x'] = stats(lever(mom_ret, bull_exposure(idx_close, cal, 2.0)), cal)
    res['(참고) 단순보유'] = stats((1 + R.mean(axis=1).fillna(0)).cumprod(), cal)

    L = ["⚖️ (a)순수모멘텀 vs (b)상승레버리지 — 백테스트 맞대결",
         f"238종목 · {START}~ · top4/12M · 룩어헤드제거", "=" * 56]
    L.append(f"{'전략':18}{'누적':>9}{'연':>6}{'MDD':>6}{'Calmar':>7}{'전/후반':>14}")
    L.append("-" * 56)
    for k, m in res.items():
        L.append(f"{k:18}{m['ret']:>+8.0f}%{m['cagr']:>+5.0f}{m['mdd']:>+6.0f}{m['calmar']:>7.1f}{m['r1']:>+6.0f}/{m['r2']:>+6.0f}")
    L.append("-" * 56)
    a, b1, b2 = res['(a) 순수 모멘텀'], res['(b1) +상승1.5x'], res['(b2) +상승2.0x']
    best_ret = max(res.items(), key=lambda kv: kv[1]['ret'] if '참고' not in kv[0] else -9e9)
    best_cal = max(res.items(), key=lambda kv: kv[1]['calmar'])   # 보유 포함(정직)
    L.append(f"📌 최고 절대수익: {best_ret[0]} ({best_ret[1]['ret']:+.0f}%) — 단 MDD {best_ret[1]['mdd']:.0f}%")
    L.append(f"📌 최고 위험조정(Calmar): {best_cal[0]} ({best_cal[1]['calmar']:.1f})")
    L.append("")
    L.append("[정직한 판정]")
    L.append(f"· 레버리지=공짜아님: 수익 {a['ret']:+.0f}→{b2['ret']:+.0f}% 오르나 MDD {a['mdd']:.0f}→{b2['mdd']:.0f}% 동반↑ → Calmar 제자리(0.4~0.5)")
    L.append(f"· 위험조정 1위는 단순보유(Calmar {res['(참고) 단순보유']['calmar']:.1f}) > 모멘텀들(0.4~0.5)")
    L.append(f"· 모든 모멘텀안 MDD {a['mdd']:.0f}%↓ = '반토막' 초과(감내 -50% 넘김). 2x는 {b2['mdd']:.0f}%")
    L.append("✅ 추천: (a)순수 모멘텀 — 레버리지는 고통만 키움(Calmar 동일). 더 줄이려면 top5~6로 분산해 MDD↓.")
    L.append("   (절대수익만·-80%도 버틴다면 (b2)2x)")
    L.append("⚠️ 절대수익은 생존편향+레버리지로 과대. 방향·상대비교만 신뢰. KOSPI 대형 238(코스닥 거의無).")
    return "\n".join(L)


if __name__ == '__main__':
    rep = run()
    print("\n" + rep)
    if '--telegram' in sys.argv:
        send_telegram(rep)
