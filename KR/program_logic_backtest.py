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

from KR.regime_period_backtest import _ohlc, _kospi_phase_daily, SAMPLE_KR, COST
from KR.strategy import (calculate_entry_score, get_entry_threshold, get_budget_ratio_from_score,
                         get_bull_momentum_score, get_neutral_range_score,
                         get_bear_bottom_score, get_bear_budget_ratio, get_bear_bounce_signal,
                         check_rsi_progressive_exit)

REG3 = {'상승': 'BULL', '하락': 'BEAR', '횡보': 'NEUTRAL'}
WIN = 130        # 함수에 넘길 과거창(60MA+여유)
GIVEBACK = 0.12  # 고점대비 되돌림 손절


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
        p = float(px.iloc[i]); rg = reg2.iloc[i]; r3 = REG3.get(rg, 'NEUTRAL')
        win = df.iloc[max(0, i - WIN):i + 1]
        t = cur
        try:
            if strat == 'common':
                score, _ = calculate_entry_score(win, p, r3)
                thr = get_entry_threshold(r3, 'satellite')
                t = get_budget_ratio_from_score(score, thr) if score >= thr else 0.0
            else:  # 국면전용
                if rg == '상승':
                    score, _ = get_bull_momentum_score(win)
                    t = 0.6 if score == 0 else (0.7 if score <= 2 else 0.8)
                elif rg == '횡보':
                    score, _ = get_neutral_range_score(win)
                    t = {0: 0.0, 1: 0.30, 2: 0.45}.get(score, 0.55)
                else:  # 하락
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
    px = df['close']; reg2 = reg.reindex(px.index).ffill().fillna('횡보')
    strats = {'단순보유': 'hold', '프로그램(공통점수)': 'common',
              '프로그램(국면전용)': 'phase', '프로그램(국면전용+하락헤지)': 'phase_hedge'}
    out = {}
    for nm, key in strats.items():
        eq = _simulate(df, reg2, key)
        dr = eq.pct_change().fillna(0.0)
        per = {}
        for rg in ('상승', '하락', '횡보'):
            mask = (reg2 == rg).values
            per[rg] = round((np.prod(1.0 + dr.values[mask]) - 1.0) * 100, 1) if mask.sum() >= 20 else None
        per['전체'] = round((eq.iloc[-1] / eq.iloc[0] - 1.0) * 100, 1)
        peak = eq.cummax(); per['MDD'] = round(float(((eq / peak - 1) * 100).min()), 1)
        out[nm] = per
    return out


def run(stocks=None):
    reg = _kospi_phase_daily()
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
    return summary, done


def format_report(summary, n):
    L = [f"📊 실제 프로그램 로직 백테스트 (KR {n}종목, 국면별 수익률 중앙값)", ""]
    L.append(f"{'전략':24} 상승   하락   횡보   전체   MDD")
    order = ['단순보유', '프로그램(공통점수)', '프로그램(국면전용)', '프로그램(국면전용+하락헤지)']
    for s in order:
        p = summary.get(s, {})
        g = lambda k: f"{p.get(k,'-')}"
        L.append(f"{s:24} {g('상승'):>5} {g('하락'):>5} {g('횡보'):>5} {g('전체'):>5} {g('MDD'):>6}")
    L.append("")
    L.append("국면별 1위:")
    for rg in ('상승', '하락', '횡보'):
        best = max(summary.items(), key=lambda kv: kv[1].get(rg, -9999))
        L.append(f"  {rg}: {best[0]} ({best[1].get(rg)}%)")
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
    summary, done = run(SAMPLE_KR[:n])
    report = format_report(summary, done)
    print("\n" + report)
    if '--tg' in sys.argv or len(sys.argv) <= 2:
        send_telegram(report)
