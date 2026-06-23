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

    # 승률맵 미리계산: (국면, 신호) → 10%달성률 (필터 게이트용, 빠른 조회)
    import sqlite3 as _sq
    _c = _sq.connect('lassi.db', timeout=60)
    win_map = {}
    SIGS = ['RSI_BUY','MACD_BUY','BB_BUY','MA_BUY','VOL_BUY','BREAK_BUY']
    PHASES = ['PANIC','BEAR_EARLY','BEAR_MID','BEAR_LATE','SIDEWAYS','BULL_EARLY','BULL_MID','BULL_LATE']
    for ph in PHASES:
        for sg in SIGS:
            r = _c.execute('''SELECT 100.0*SUM(CASE WHEN max_gain_pct>=10 THEN 1 ELSE 0 END)/COUNT(*) w, COUNT(*) n
                FROM backtest_trade_signals WHERE mode='KR' AND signal_direction='BUY'
                AND market_phase=? AND signal_types LIKE ? AND max_gain_pct<=300''', (ph, f'%{sg}%')).fetchone()
            if r and r[1] and r[1] >= 30:
                win_map[(ph, sg)] = r[0]
    _c.close()

    eng_rets, scr_rets, engf_rets, ens_rets = [], [], [], []
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
            ph = _phase('KR', dates[i].strftime('%Y-%m-%d'))
            # 엔진(날것): 백테스트 매수신호 발생
            sigs = [s for s in detect_signals(df.iloc[i], df.iloc[i-1]) if 'BUY' in s]
            eng_hit = bool(sigs)
            if eng_hit:
                eng_rets.append(fwd_ret)
            # 엔진(필터): 신호 중 현재국면 승률 min_win 이상인 게 있을 때만
            engf_hit = eng_hit and any(win_map.get((ph, s), 0) >= min_win for s in sigs)
            if engf_hit:
                engf_rets.append(fwd_ret)
            # 점수제
            scr_hit = False
            try:
                regime = get_market_regime(sub)
                score, _ = calculate_entry_score(sub, price, regime)
                scr_hit = score >= get_entry_threshold(regime, 'satellite')
                if scr_hit:
                    scr_rets.append(fwd_ret)
            except Exception:
                pass
            # 앙상블: 필터엔진 + 점수제 둘 다 동의
            if engf_hit and scr_hit:
                ens_rets.append(fwd_ret)

    def stat(name, rs):
        if not rs:
            return f"{name}: 진입 0건"
        import statistics
        win = 100*sum(1 for r in rs if r > 0)/len(rs)
        return (f"{name}: 진입 {len(rs):,}건 | 평균 {statistics.mean(rs):+.1f}% | "
                f"승률 {win:.0f}% | 중앙값 {statistics.median(rs):+.1f}%")

    print(f"=== 엔진 vs 점수제 vs 앙상블 ({done}종목, forward {fwd}일, 승률게이트 {min_win}%) ===")
    print(stat('엔진(날것신호) ', eng_rets))
    print(stat('엔진(필터)    ', engf_rets))
    print(stat('점수제       ', scr_rets))
    print(stat('앙상블(필터+점수)', ens_rets))


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--tickers', type=int, default=20)
    ap.add_argument('--fwd', type=int, default=60)
    args = ap.parse_args()
    run(args.tickers, args.fwd)
