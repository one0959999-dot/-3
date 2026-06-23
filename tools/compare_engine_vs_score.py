"""엔진(백테스트 신호기반) vs 점수제 — 동일 종목·기간 head-to-head 백테스트.

각 과거 봉에서 두 방식이 '매수'라 판단한 시점의 forward N일 수익률을 집계 비교.
승자 판정 근거 → 점수제 삭제 여부 결정.
※ pykrx 사용 → 백테스트 수집 중이면 rate limit 가능. 한가할 때 실행 권장.
사용: python tools/compare_engine_vs_score.py [--tickers 20] [--fwd 60]
"""
import sys, os, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import pandas as pd

from base.signals import calc_indicators, detect_signals
from base.signal_forecast import get_forecast
from KR.backtest_runner import _get_full_history
from KR.strategy import get_market_regime, calculate_entry_score, get_entry_threshold
from base.market_phase import classify_phase


def _phase(mode, date_str):
    try:
        return classify_phase(mode, date_str).get('phase')
    except Exception:
        return None


def run(n_tickers=20, fwd=60, step=3, min_win=55):
    import sqlite3
    c = sqlite3.connect('lassi.db', timeout=60)
    tickers = [r[0] for r in c.execute(
        "SELECT ticker FROM kr_ticker_cache LIMIT ?", (n_tickers*3,)).fetchall()]
    c.close()

    eng_rets, scr_rets = [], []
    done = 0
    for tk in tickers:
        if done >= n_tickers:
            break
        df = _get_full_history(tk, None)
        if df is None or len(df) < 300:
            continue
        df = calc_indicators(df)
        closes = df['close'].values
        dates = df.index
        done += 1
        for i in range(200, len(df) - fwd, step):
            sub = df.iloc[:i+1]
            price = float(closes[i])
            if price <= 0:
                continue
            fwd_ret = (closes[i+fwd] / price - 1) * 100
            # 엔진: 매수신호 + 승률 게이트
            sigs = [s for s in detect_signals(df.iloc[i], df.iloc[i-1]) if 'BUY' in s]
            if sigs:
                ph = _phase('KR', dates[i].strftime('%Y-%m-%d'))
                fc = get_forecast('KR', ph, sigs)
                if (fc and fc['win10'] >= min_win) or (not fc):
                    eng_rets.append(fwd_ret)
            # 점수제
            try:
                regime = get_market_regime(sub)
                score, _ = calculate_entry_score(sub, price, regime)
                if score >= get_entry_threshold(regime, 'satellite'):
                    scr_rets.append(fwd_ret)
            except Exception:
                pass

    def stat(name, rs):
        if not rs:
            return f"{name}: 진입 0건"
        import statistics
        win = 100*sum(1 for r in rs if r > 0)/len(rs)
        return (f"{name}: 진입 {len(rs):,}건 | 평균 {statistics.mean(rs):+.1f}% | "
                f"승률 {win:.0f}% | 중앙값 {statistics.median(rs):+.1f}%")

    print(f"=== 엔진 vs 점수제 ({done}종목, forward {fwd}일) ===")
    print(stat('엔진(신호기반)', eng_rets))
    print(stat('점수제      ', scr_rets))


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--tickers', type=int, default=20)
    ap.add_argument('--fwd', type=int, default=60)
    args = ap.parse_args()
    run(args.tickers, args.fwd)
