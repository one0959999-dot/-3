"""
챗 컨텍스트용 백테스트 통계 헬퍼.
backtest_trade_signals 에서 보유 종목별 백테스트 성과(승률/평균최대수익/낙폭/고점도달일)를
집계해, AI 챗이 추측이 아니라 실제 백테스트 수치를 근거로 답하도록 컨텍스트 문자열을 만든다.
어떤 이유로든 실패하면 빈 문자열을 반환한다 — 챗을 절대 깨뜨리지 않는다.
"""
from base.database import get_db_connection

NL = chr(10)


def build_backtest_context(tickers, max_rows=15):
    """보유 종목 리스트(tickers)에 대한 백테스트 통계 컨텍스트 문자열을 반환.
    실패하거나 데이터가 없으면 빈 문자열."""
    try:
        tickers = [str(t) for t in (tickers or []) if t]
        if not tickers:
            return ""
        conn = get_db_connection()
        try:
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='backtest_trade_signals'"
            ).fetchone()
            if exists is None:
                return ""
            ph = ",".join("?" * len(tickers))
            rows = conn.execute(
                "SELECT ticker, MAX(stock_name) stock_name, COUNT(*) n, "
                "AVG(max_gain_pct) avg_gain, AVG(days_to_peak) avg_days, "
                "AVG(max_drawdown_pct) avg_dd, "
                "100.0*SUM(CASE WHEN max_gain_pct>=20 THEN 1 ELSE 0 END)/COUNT(*) win20 "
                "FROM backtest_trade_signals "
                "WHERE signal_direction='BUY' AND ticker IN (" + ph + ") "
                "AND max_gain_pct<=300 AND max_drawdown_pct>=-90 "
                "GROUP BY ticker LIMIT ?",
                tickers + [max_rows],
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return ""
        out = ["[백테스트 성과 — 과거 데이터 기반]"]
        out.append(
            "아래는 실제 백테스트 통계다. 목표가/예상 보유기간/승률을 물으면 "
            "추측하지 말고 이 수치를 근거로 답하라. 해당 종목 데이터가 없으면 "
            "'백테스트 데이터 없음'이라고 솔직히 말하라."
        )
        for r in rows:
            name = r["stock_name"] or r["ticker"]
            out.append(
                "- {} ({}): 백테스트 매수신호 {}건, 승률(최대수익 20%+) {:.0f}%, "
                "평균 최대수익 {:.1f}%, 평균 최대낙폭 {:.1f}%, "
                "고점까지 평균 {:.0f}일".format(
                    name,
                    r["ticker"],
                    r["n"],
                    r["win20"] or 0,
                    r["avg_gain"] or 0,
                    r["avg_dd"] or 0,
                    r["avg_days"] or 0,
                )
            )
        return NL + NL + NL.join(out) + NL
    except Exception:
        return ""
