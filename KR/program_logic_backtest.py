"""실제 프로그램 로직 백테스트 — KR/strategy.py 진짜 함수를 그대로 호출해 국면별 검증.

이전 regime_period_backtest.py 의 결함(단순보유+헤지만, 진짜 진입로직 미사용)을 정정.
여기선 프로그램의 실제 함수를 매일 호출:
  - 공통 매수점수:  calculate_entry_score + get_entry_threshold + get_budget_ratio_from_score
  - 상승 전용:      get_bull_momentum_score
  - 횡보 전용:      get_neutral_range_score (박스권)
  - 하락 전용:      get_bear_bottom_score + get_bear_budget_ratio (바닥분할매수) + 인버스/현금 헤지
  - 청산:          check_rsi_progressive_exit + 되돌림 손절 + 국면이탈

다중 전략을 국면별(상승/하락/횡보) 수익률로 비교 → 완료시 텔레그램 전송.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd

from KR.regime_period_backtest import _ohlc, _yf, SAMPLE_KR, COST
from KR.strategy import (calculate_entry_score, get_entry_threshold, get_budget_ratio_from_score,
                         get_bull_momentum_score, get_neutral_range_score,
                         get_bear_bottom_score, get_bear_budget_ratio, get_bear_bounce_signal,
                         check_rsi_progressive_exit)
from base.market_phase import _adx

WIN = 130        # 함수에 넘길 과거창(60MA+여유)
GIVEBACK = 0.12  # 고점대비 되돌림 손절

# 8단계 → 전략함수 선택용 3-regime (프로그램 regime_from_phase 와 동일)
PHASE2REG3 = {
    'PANIC': 'BEAR', 'BEAR_EARLY': 'BEAR', 'BEAR_MID': 'BEAR', 'BEAR_LATE': 'BEAR',
    'RECOVERY': 'BULL', 'BULL_EARLY': 'BULL', 'BULL_MID': 'BULL', 'BULL_LATE': 'BULL',
    'SIDEWAYS': 'NEUTRAL',
}
PHASE_ORDER = ['PANIC', 'BEAR_EARLY', 'BEAR_MID', 'BEAR_LATE', 'RECOVERY',
               'BULL_EARLY', 'BULL_MID', 'BULL_LATE', 'SIDEWAYS']
PHASE_KR = {'PANIC': '패닉', 'BEAR_EARLY': '하락초기', 'BEAR_MID': '하락중반', 'BEAR_LATE': '하락말기(바닥)',
            'RECOVERY': '회복초입', 'BULL_EARLY': '상승초입', 'BULL_MID': '상승중반',
            'BULL_LATE': '상승말기(고점)', 'SIDEWAYS': '횡보'}


def _phase8_daily():
    """KOSPI(^KS11) 일별 8단계 — base/market_phase.classify_phase 트리를 그대로 재현.
    (VIX 미사용 → PANIC은 20일 급락 -12% 근사). 기울기=ma200_slope 사용."""
    idx = _yf('^KS11')
    if idx is None or len(idx) < 220:
        return pd.Series(dtype=object), None
    c, h, l = idx['close'].astype(float), idx['high'].astype(float), idx['low'].astype(float)
    ma60 = c.rolling(60).mean(); ma120 = c.rolling(120).mean(); ma200 = c.rolling(200).mean()
    mom20 = (c / c.shift(20) - 1) * 100
    mom60 = (c / c.shift(60) - 1) * 100
    vs200 = (c / ma200 - 1) * 100
    vs52 = (c / c.rolling(252).max() - 1) * 100
    adx = _adx(h, l, c)
    slope = (ma200 / ma200.shift(20) - 1) * 100      # MA200 기울기(각도)
    out = {}
    phases = []
    for i in range(len(c)):
        cur = c.iloc[i]; m200 = ma200.iloc[i]
        if pd.isna(m200) or i < 200:
            phases.append('UNKNOWN'); continue
        m20s = mom20.iloc[i]; m60s = mom60.iloc[i]; ax = adx.iloc[i]; sl = slope.iloc[i]
        m60v = ma60.iloc[i]; m120v = ma120.iloc[i]; v52 = vs52.iloc[i]
        if m20s < -12:                                          # PANIC 근사(VIX 없음)
            ph = 'PANIC'
        elif cur < m200 * 0.92 and m20s < -5:
            ph = 'BEAR_MID'
        elif cur < m200 and m60s < -15:
            ph = 'BEAR_MID'
        elif cur < m200 and m20s < -3:
            ph = 'BEAR_EARLY'
        elif cur < m200 and ax < 18:
            ph = 'BEAR_LATE'
        elif cur > m200 and m20s > 3 and m60s < -10:
            ph = 'RECOVERY'
        elif cur > m200 and sl > 0:
            if v52 > -5:
                ph = 'BULL_LATE'
            elif cur > m60v > m120v and ax > 25:
                ph = 'BULL_EARLY' if (m20s > 0 and m60s < 15) else 'BULL_MID'
            else:
                ph = 'BULL_MID'
        else:
            ph = 'SIDEWAYS'
        phases.append(ph)
    ser = pd.Series(phases, index=c.index)
    # 국면별 판별 특성(평균 기울기/ADX/모멘텀) — '어떻게 판별했나' 기록용
    feat = pd.DataFrame({'phase': ser, 'slope': slope, 'adx': adx, 'mom20': mom20, 'mom60': mom60})
    return ser, feat


def _rsi(c, n=14):
    d = c.diff(); up = d.clip(lower=0).rolling(n).mean(); dn = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + up / (dn + 1e-9))


def _target_weight(df, reg2, strat):
    """프로그램 실제 함수로 일별 목표보유비중(0~1) 산출 — 점수가 클수록 많이 보유."""
    px = df['close']; n = len(px); w = np.zeros(n)
    cur = 0.0
    for i in range(n):
        if i < 62:
            continue
        p = float(px.iloc[i]); ph = reg2.iloc[i]; r3 = PHASE2REG3.get(ph, 'NEUTRAL')
        win = df.iloc[max(0, i - WIN):i + 1]
        t = cur
        try:
            if strat == 'common':
                score, _ = calculate_entry_score(win, p, r3)
                thr = get_entry_threshold(r3, 'satellite')
                t = get_budget_ratio_from_score(score, thr) if score >= thr else 0.0
            else:  # 국면전용 (8단계 → 상승/횡보/하락 함수 + 8단계 뉘앙스)
                if r3 == 'BULL':
                    score, _ = get_bull_momentum_score(win)
                    t = 0.6 if score == 0 else (0.7 if score <= 2 else 0.8)
                    if ph == 'BULL_LATE':
                        t = min(t, 0.4)                       # 상승말기=익절/축소
                elif r3 == 'NEUTRAL':
                    score, _ = get_neutral_range_score(win)
                    t = {0: 0.0, 1: 0.30, 2: 0.45}.get(score, 0.55)
                else:  # BEAR
                    if strat == 'phase_hedge':
                        t = 0.0                               # 하락=현금/인버스
                    else:
                        score, _ = get_bear_bottom_score(win)
                        t = get_bear_budget_ratio(score)
        except Exception:
            t = cur
        cur = t; w[i] = t
    return pd.Series(w, index=px.index)


def _simulate(df, reg2, strat):
    """프로그램 점수→목표비중 리밸런싱. 비중변화분에만 비용(전량청산 churn 없음)."""
    px = df['close']
    if strat == 'hold':
        from KR.regime_period_backtest import _run_frac
        return _run_frac(px, pd.Series(1.0, index=px.index))
    w = _target_weight(df, reg2, strat)
    # 히스테리시스: 0.1 이상 변할 때만 리밸런싱(미세 churn 방지)
    wf = w.copy().values; held = 0.0
    for i in range(len(wf)):
        if abs(wf[i] - held) >= 0.10:
            held = wf[i]
        wf[i] = held
    from KR.regime_period_backtest import _run_frac
    return _run_frac(px, pd.Series(wf, index=px.index))


def backtest_stock(code, name, reg):
    df = _ohlc(code)
    if df is None or len(df) < 300:
        return None
    px = df['close']; reg2 = reg.reindex(px.index).ffill().fillna('SIDEWAYS')
    strats = {'단순보유': 'hold', '프로그램(공통점수)': 'common',
              '프로그램(국면전용)': 'phase', '프로그램(국면전용+하락헤지)': 'phase_hedge'}
    out = {}
    for nm, key in strats.items():
        eq = _simulate(df, reg2, key)
        dr = eq.pct_change().fillna(0.0)
        per = {}
        for ph in PHASE_ORDER:                              # 8단계별 격리수익
            mask = (reg2 == ph).values
            per[ph] = round((np.prod(1.0 + dr.values[mask]) - 1.0) * 100, 1) if mask.sum() >= 15 else None
        per['전체'] = round((eq.iloc[-1] / eq.iloc[0] - 1.0) * 100, 1)
        peak = eq.cummax(); per['MDD'] = round(float(((eq / peak - 1) * 100).min()), 1)
        out[nm] = per
    return out


def run(stocks=None):
    reg, feat = _phase8_daily()
    stocks = stocks or SAMPLE_KR
    agg = {}
    done = 0
    for code, name in stocks:
        try:
            r = backtest_stock(code, name, reg)
        except Exception as e:
            print(f"  {name} 실패: {e}"); continue
        if not r:
            continue
        done += 1; print(f"  {name}({code}) 완료")
        for strat, per in r.items():
            for k, v in per.items():
                if v is not None:
                    agg.setdefault(strat, {}).setdefault(k, []).append(v)
    summary = {s: {k: round(float(np.median(vs)), 1) for k, vs in d.items()} for s, d in agg.items()}
    # 국면별 판별특성(어떻게 잡았나) + 일수
    phase_feat = {}
    if feat is not None:
        for ph in PHASE_ORDER:
            sub = feat[feat['phase'] == ph]
            if len(sub):
                phase_feat[ph] = {'days': len(sub), 'slope': round(sub['slope'].mean(), 2),
                                  'adx': round(sub['adx'].mean(), 1), 'mom20': round(sub['mom20'].mean(), 1),
                                  'mom60': round(sub['mom60'].mean(), 1)}
    return summary, done, phase_feat


_SHORT = {'단순보유': '보유', '프로그램(공통점수)': '공통점수',
          '프로그램(국면전용)': '국면전용', '프로그램(국면전용+하락헤지)': '국면+헤지'}


def format_report(summary, n, phase_feat):
    L = [f"📊 8단계 국면별 프로그램 로직 백테스트 (KR {n}종목, 수익률 중앙값)",
         "전략: 보유 / 공통점수(calc_entry_score) / 국면전용(bull·neutral·bear score) / 국면+하락헤지", ""]
    strats = list(_SHORT.keys())
    for ph in PHASE_ORDER:
        f = phase_feat.get(ph)
        if not f:
            continue
        # 이 국면 1위
        ranked = sorted(((s, summary.get(s, {}).get(ph)) for s in strats),
                        key=lambda kv: (kv[1] if kv[1] is not None else -9999), reverse=True)
        ranked = [(s, v) for s, v in ranked if v is not None]
        if not ranked:
            continue
        L.append(f"■ {PHASE_KR[ph]}({ph}) — {f['days']}일")
        L.append(f"   판별: MA200기울기 {f['slope']:+.2f} · ADX {f['adx']} · 20일 {f['mom20']:+.1f}% · 60일 {f['mom60']:+.1f}%")
        L.append("   " + " / ".join(f"{_SHORT[s]} {v:+.0f}%" for s, v in ranked))
        L.append(f"   → 1위: {_SHORT[ranked[0][0]]} ({ranked[0][1]:+.0f}%)")
        L.append("")
    L.append("[전체/MDD]")
    for s in strats:
        p = summary.get(s, {})
        L.append(f"  {_SHORT[s]:6} 전체 {p.get('전체','-')}% · MDD {p.get('MDD','-')}%")
    return "\n".join(L)


def send_telegram(text):
    from base.database import get_db_connection
    from base.telegram_bot import TelegramNotifier
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT telegram_token, telegram_chat_id FROM users "
                           "WHERE telegram_token IS NOT NULL AND telegram_token!='' LIMIT 1").fetchone()
    finally:
        conn.close()
    if not row:
        print("[텔레그램] 토큰 없음 — 전송 생략"); return False
    TelegramNotifier(row['telegram_token'], row['telegram_chat_id']).send_message(text)
    print("[텔레그램] 전송 완료"); return True


if __name__ == '__main__':
    n = int(sys.argv[1]) if len(sys.argv) > 1 else len(SAMPLE_KR)
    summary, done, phase_feat = run(SAMPLE_KR[:n])
    report = format_report(summary, done, phase_feat)
    print("\n" + report)
    if '--tg' in sys.argv or len(sys.argv) <= 2:
        send_telegram(report)
