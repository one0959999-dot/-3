"""방식(신호조합) 랭킹 — backtest_trade_signals(기존 데이터)로 OOS 검증 랭킹 산출.

BACKTEST_TODO.md 원칙:
- 재수집 금지 — DB의 backtest_trade_signals(신호별 미래결과) 그대로 사용.
- "과거 수익 1등" 믿기 금지 → 만들기기간(≤2020)·검증기간(≥2021) 둘 다 강한 것만 채택.
- 1종목/1기간 1등은 운빨 → 전 종목 집계 + 검증기간 성과로 채점.
- 채점: 검증기간 성과(win20·평균상승/낙폭) × 만들기↔검증 일관성(노이즈 배제).
"""
import json

SPLIT_DATE = '2021-01-01'        # 만들기 < 이 날짜 ≤ 검증
MIN_VALID_N = 100                # 검증기간 최소 표본
SIGNALS = ['RSI_BUY', 'MACD_BUY', 'BB_BUY', 'MA_BUY', 'VOL_BUY', 'BREAK_BUY']


def _stats(conn, mode, sig_like, date_cond, params):
    row = conn.execute(f'''
        SELECT COUNT(*) n,
            ROUND(AVG(max_gain_pct),1) avg_gain,
            ROUND(AVG(max_drawdown_pct),1) avg_mdd,
            ROUND(AVG(days_to_peak),0) hold,
            ROUND(100.0*SUM(CASE WHEN max_gain_pct>=20 THEN 1 ELSE 0 END)/COUNT(*),0) win20,
            ROUND(100.0*SUM(CASE WHEN max_gain_pct>=10 THEN 1 ELSE 0 END)/COUNT(*),0) win10,
            COUNT(DISTINCT ticker) tickers
        FROM backtest_trade_signals
        WHERE mode=? AND signal_direction='BUY'
          AND max_gain_pct<=300 AND max_drawdown_pct>=-90
          AND signal_types LIKE ? AND {date_cond}
    ''', (mode, sig_like, *params)).fetchone()
    if not row or not row['n']:
        return None
    return {'n': row['n'], 'avg_gain': row['avg_gain'] or 0, 'avg_mdd': row['avg_mdd'] or 0,
            'hold': row['hold'] or 0, 'win20': row['win20'] or 0, 'win10': row['win10'] or 0,
            'tickers': row['tickers'] or 0}


def rank_methods(mode: str, market_phase: str = None) -> list:
    """방식별 OOS 검증 랭킹. market_phase 주면 그 국면만, 없으면 전체.
    반환: [{rank, method, build, valid, score, consistent, verdict, conclusion}, ...] 점수 내림차순."""
    from base.database import get_db_connection
    conn = get_db_connection()
    out = []
    try:
        ph_cond = ''
        ph_params = []
        if market_phase:
            ph_cond = ' AND market_phase=?'
            ph_params = [market_phase]
        for sig in SIGNALS:
            like = f'%{sig}%'
            build = _stats(conn, mode, like, f"trade_date < ?{ph_cond}", [SPLIT_DATE, *ph_params])
            valid = _stats(conn, mode, like, f"trade_date >= ?{ph_cond}", [SPLIT_DATE, *ph_params])
            if not valid or valid['n'] < MIN_VALID_N or not build:
                continue
            # 일관성: 만들기↔검증 win20 차이 (작을수록 강건, 노이즈 아님)
            gap = abs(valid['win20'] - build['win20'])
            consistent = gap <= 12
            # 점수 = 검증 win20 × 일관성가중 (불일치하면 깎음). 낙폭 큰 건 소폭 패널티.
            consist_w = max(0.4, 1 - gap / 40.0)
            risk_w = 1.0 / (1 + abs(valid['avg_mdd']) / 60.0)
            score = round(valid['win20'] * consist_w * risk_w, 1)
            if consistent and valid['win20'] >= 45:
                verdict = '✅ 채택가능'
            elif not consistent:
                verdict = '⚠️ 노이즈 의심'
            else:
                verdict = '△ 보통'
            out.append({'method': sig, 'build': build, 'valid': valid,
                        'score': score, 'gap': gap, 'consistent': consistent, 'verdict': verdict})
        out.sort(key=lambda x: x['score'], reverse=True)
        for i, m in enumerate(out):
            m['rank'] = i + 1
            v = m['valid']; b = m['build']
            m['conclusion'] = (
                f"{m['verdict']} · 점수 {m['score']}\n"
                f"검증기간(2021~): 20%달성 {v['win20']:.0f}% · 평균상승 +{v['avg_gain']:.0f}% · "
                f"평균낙폭 {v['avg_mdd']:.0f}% · 보유 ~{v['hold']:.0f}일 · 표본 {v['n']:,}건({v['tickers']}종목)\n"
                f"만들기기간(~2020): 20%달성 {b['win20']:.0f}% (검증과 차이 {m['gap']:.0f}%p — "
                + ("일관됨, 통계적 우위 정당" if m['consistent'] else "불일치, 과거 운빨 가능성") + ")"
            )
        return out
    finally:
        conn.close()


def _has_raw(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='backtest_trade_signals'").fetchone() is not None \
        and conn.execute("SELECT 1 FROM backtest_trade_signals LIMIT 1").fetchone() is not None


def rebuild_method_ranking_cache():
    """로컬에서 방식 랭킹을 미리 계산해 method_ranking_cache에 저장(JSON).
    EC2 경량 db(원본 없음)는 이 캐시를 읽음 — 워크플로우: 로컬 계산→캐시만 배포."""
    import json as _json
    from base.database import get_db_connection, db_lock
    phases = [None, 'BULL_LATE', 'BULL_MID', 'BULL_EARLY', 'BEAR_MID', 'BEAR_EARLY',
              'BEAR_LATE', 'SIDEWAYS', 'PANIC']
    with db_lock:
        conn = get_db_connection()
        try:
            conn.execute('''CREATE TABLE IF NOT EXISTS method_ranking_cache (
                mode TEXT, phase TEXT, json TEXT, updated_at TEXT,
                PRIMARY KEY (mode, phase))''')
            n = 0
            now = __import__('datetime').datetime.now().isoformat(timespec='seconds')
            for mode in ('KR', 'US'):
                for ph in phases:
                    rows = rank_methods(mode, ph)
                    conn.execute('INSERT OR REPLACE INTO method_ranking_cache VALUES (?,?,?,?)',
                                 (mode, ph or '*', _json.dumps(rows, ensure_ascii=False), now))
                    n += 1
            conn.commit()
            return n
        finally:
            conn.close()


def get_ranked_methods(mode: str, market_phase: str = None) -> list:
    """방식 랭킹 — 원본 있으면 즉석계산, 없으면(EC2 경량) 캐시 조회."""
    import json as _json
    from base.database import get_db_connection
    conn = get_db_connection()
    try:
        if _has_raw(conn):
            return rank_methods(mode, market_phase)
        row = conn.execute('SELECT json FROM method_ranking_cache WHERE mode=? AND phase=?',
                           (mode, market_phase or '*')).fetchone()
        return _json.loads(row['json']) if row and row['json'] else []
    finally:
        conn.close()


def rank_summary(mode: str) -> dict:
    """전체 + 주요 국면별 랭킹 묶음 (UI용)."""
    res = {'overall': rank_methods(mode)}
    for ph in ('BULL_LATE', 'BEAR_MID', 'SIDEWAYS', 'PANIC'):
        r = rank_methods(mode, ph)
        if r:
            res[ph] = r
    return res


if __name__ == '__main__':
    import logging, sys
    logging.disable(logging.CRITICAL)
    m = sys.argv[1] if len(sys.argv) > 1 else 'KR'
    for x in rank_methods(m)[:5]:
        print(f"\n[{x['rank']}위] {x['method']}\n{x['conclusion']}")
