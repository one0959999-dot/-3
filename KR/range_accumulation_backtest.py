"""횡보장 누적 스윙 전략 검증 — 박스권서 주식수를 불리고, 돌파하면 전량보유로 크게 먹기.

사용자 가설: 횡보장에 천장매도/바닥매수로 '주식 수'를 늘려두면(평가금액↑),
이후 상승 돌파시 늘어난 주식으로 크게 수익한다.
→ 최선의 조건을 주고(깨끗한 박스권 + 돌파시 전량보유 전환) 보유와 비교.

측정: 최종 평가가치(수익률) + 최종 주식수(보유 대비 배수) + MDD.
비교: 단순보유 / 누적스윙+돌파보유 / 프로그램 내장(get_neutral_range_score).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd
from KR.regime_period_backtest import _ohlc, SAMPLE_KR, COST
from base.market_phase import _adx

TRIM = 0.40        # 천장서 매도 비율
ADX_RANGE = 22     # 이 미만이면 박스권(추세약함)
BREAKOUT = 1.03    # 박스 상단 +3% 돌파 = 전량보유 전환


def accumulation_swing(df):
    c, h, l = df['close'], df['high'], df['low']
    ma = c.rolling(20).mean(); sd = c.rolling(20).std()
    up, lo = ma + 2 * sd, ma - 2 * sd
    hi20 = c.rolling(20).max()
    adx = _adx(h, l, c)
    px = c.values
    cash = 0.0; shares = 1.0 / px[0]   # $1 전량매수 시작
    eq = []
    for i in range(len(px)):
        p = px[i]; nav = cash + shares * p
        if i >= 20 and not np.isnan(up.iloc[i]):
            ax = adx.iloc[i]
            in_range = ax < ADX_RANGE
            # 상단 돌파 → 전량보유 전환(현금 전부 매수)
            if p > hi20.iloc[i - 1] * BREAKOUT and cash > 0:
                shares += cash * (1 - COST) / p; cash = 0.0
            elif in_range:
                if p >= up.iloc[i] and shares * p > nav * 0.1:      # 천장 → 일부매도
                    sv = shares * TRIM * p; cash += sv * (1 - COST); shares *= (1 - TRIM)
                elif p <= lo.iloc[i] and cash > nav * 0.1:          # 바닥 → 전부 재매수(주식수↑)
                    shares += cash * (1 - COST) / p; cash = 0.0
        eq.append(cash + shares * p)
    return pd.Series(eq, index=df.index), shares, cash, px[-1]


def buy_hold(df):
    px = df['close'].values
    sh = 1.0 / px[0]
    return pd.Series(sh * px, index=df.index), sh


def program_neutral(df):
    """프로그램 내장 횡보로직(get_neutral_range_score)로 비중조절."""
    from KR.strategy import get_neutral_range_score
    c = df['close']; px = c.values
    cash = 0.0; shares = 1.0 / px[0]; eq = []
    held = 1.0
    for i in range(len(px)):
        p = px[i]; nav = cash + shares * p
        if i >= 62:
            try:
                sc, _ = get_neutral_range_score(df.iloc[max(0, i - 130):i + 1])
                tgt = {0: 0.0, 1: 0.30, 2: 0.45}.get(sc, 0.55)
            except Exception:
                tgt = held
            if abs(tgt - held) >= 0.15:
                tv = tgt * nav; cv = shares * p; d = tv - cv
                shares = tv / p; cash = nav - tv - abs(d) * COST; held = tgt
        eq.append(cash + shares * p)
    return pd.Series(eq, index=df.index), shares


def _mdd(eq):
    peak = eq.cummax(); return round(float(((eq / peak - 1) * 100).min()), 1)


def range_score(df):
    """박스권(횡보) 점수 0~100 — 높을수록 진짜 박스권(추세약함). 추세주 배제용.
    구성: ADX낮음 + 200MA기울기 평탄 + 추세설명력(R²) 낮음 + 박스권 일수비율."""
    c, h, l = df['close'], df['high'], df['low']
    adx = _adx(h, l, c)
    adx_mean = float(adx.iloc[200:].mean()) if len(adx) > 200 else float(adx.mean())
    ma200 = c.rolling(200).mean()
    slope = abs(float((ma200.iloc[-1] / ma200.dropna().iloc[0] - 1)) * 100) / max(len(c) / 252, 1)  # 연환산 추세
    pct_lowadx = float((adx < ADX_RANGE).mean()) * 100
    # 추세 설명력 R² (로그가격 vs 시간) — 높으면 강추세
    y = np.log(c.values); x = np.arange(len(y))
    r2 = float(np.corrcoef(x, y)[0, 1] ** 2)
    s_adx = max(0, (30 - adx_mean) / 30) * 100          # ADX 30→0점, 0→100점
    s_slope = max(0, (40 - slope) / 40) * 100           # 연 40%↑ 추세=0점
    s_r2 = (1 - r2) * 100
    score = round(0.35 * s_adx + 0.25 * s_slope + 0.20 * pct_lowadx + 0.20 * s_r2, 0)
    return score


def run(stocks=None):
    stocks = stocks or SAMPLE_KR
    rows = []
    for code, name in stocks:
        df = _ohlc(code)
        if df is None or len(df) < 400:
            continue
        eqs, sh_s, cash_s, lastp = accumulation_swing(df)
        eqh, sh_h = buy_hold(df)
        eqn, sh_n = program_neutral(df)
        rs = range_score(df)
        rows.append({
            'name': name, 'range_score': rs,
            'swing_ret': round((eqs.iloc[-1] / eqs.iloc[0] - 1) * 100, 1), 'swing_mdd': _mdd(eqs),
            'swing_shares': round(sh_s / sh_h, 2),        # 보유대비 주식수 배수
            'hold_ret': round((eqh.iloc[-1] / eqh.iloc[0] - 1) * 100, 1), 'hold_mdd': _mdd(eqh),
            'prog_ret': round((eqn.iloc[-1] / eqn.iloc[0] - 1) * 100, 1), 'prog_mdd': _mdd(eqn),
        })
        win = '✅스윙승' if rows[-1]['swing_ret'] > rows[-1]['hold_ret'] else '✗보유승'
        print(f"  {name}: 박스권{rs:.0f} | 스윙 {rows[-1]['swing_ret']:+.0f}%(주식{rows[-1]['swing_shares']}배) vs 보유 {rows[-1]['hold_ret']:+.0f}% {win}")
    return rows


def report(rows):
    if not rows:
        return "데이터 없음"
    md = lambda k: round(float(np.median([r[k] for r in rows])), 1)
    win_swing = sum(1 for r in rows if r['swing_ret'] > r['hold_ret'])
    more_shares = sum(1 for r in rows if r['swing_shares'] > 1.0)
    # 박스권 점수가 스윙성공을 예측하나 — 상/하위 그룹 비교
    srt = sorted(rows, key=lambda r: r['range_score'], reverse=True)
    half = max(1, len(srt) // 2)
    hi, loo = srt[:half], srt[half:]
    hi_win = sum(1 for r in hi if r['swing_ret'] > r['hold_ret'])
    lo_win = sum(1 for r in loo if r['swing_ret'] > r['hold_ret'])
    L = [f"📦 횡보 누적스윙 vs 보유 vs 내장로직 (KR {len(rows)}종목, 전기간)", "",
         "[박스권점수 높은 종목(상위절반) — 추세주 배제]",
         f"  스윙 승률: {hi_win}/{len(hi)}  (박스권점수 중앙값 {round(float(np.median([r['range_score'] for r in hi])))})",
         "[박스권점수 낮은 종목(추세주)]",
         f"  스윙 승률: {lo_win}/{len(loo)}  (박스권점수 중앙값 {round(float(np.median([r['range_score'] for r in loo])))})",
         "",
         f"{'전략':16} 수익중앙값  MDD중앙값",
         f"{'누적스윙+돌파보유':16} {md('swing_ret'):+}%   {md('swing_mdd')}%",
         f"{'단순보유':16} {md('hold_ret'):+}%   {md('hold_mdd')}%",
         f"{'프로그램 내장횡보':16} {md('prog_ret'):+}%   {md('prog_mdd')}%", "",
         f"누적스윙이 보유 이긴 종목: {win_swing}/{len(rows)}",
         f"누적스윙 주식수 보유보다 많은 종목: {more_shares}/{len(rows)} (주식수배수 중앙값 {md('swing_shares')}배)"]
    return "\n".join(L)


if __name__ == '__main__':
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else len(SAMPLE_KR)
    rows = run(SAMPLE_KR[:n])
    rep = report(rows)
    print("\n" + rep)
    if '--tg' in sys.argv:
        from KR.program_logic_backtest import send_telegram
        send_telegram(rep)
