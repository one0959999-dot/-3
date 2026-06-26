"""방식(신호조합) 랭킹 — backtest_trade_signals(기존 데이터)로 OOS 검증 + 봇 기존로직 비교.

BACKTEST_TODO.md 원칙:
- 재수집 금지 — DB의 backtest_trade_signals(신호별 미래결과) 그대로 사용.
- "과거 수익 1등" 믿기 금지 → 만들기(≤2020)·검증(≥2021) 둘 다 강한 것만.
- 기존 봇 로직(8단계 국면조건부)·단순보유를 같은 표에 넣어 비교 → "봇을 이기나" 판정.
- 국면별로 세분화 — 각 국면에서 무엇이 최강인지 따로.
"""
import json

SPLIT_DATE = '2021-01-01'
MIN_VALID_N = 80
SIGNALS = ['RSI_BUY', 'MACD_BUY', 'BB_BUY', 'MA_BUY', 'VOL_BUY', 'BREAK_BUY']
import itertools as _it
PAIRS = list(_it.combinations(SIGNALS, 2))   # 2신호 조합 15개


def _stats(conn, mode, sig_cond, sig_params, date_cond, date_params):
    row = conn.execute(f'''
        SELECT COUNT(*) n,
            ROUND(AVG(max_gain_pct),1) avg_gain, ROUND(AVG(max_drawdown_pct),1) avg_mdd,
            ROUND(AVG(days_to_peak),0) hold,
            ROUND(100.0*SUM(CASE WHEN max_gain_pct>=20 THEN 1 ELSE 0 END)/COUNT(*),0) win20,
            ROUND(100.0*SUM(CASE WHEN max_gain_pct>=10 THEN 1 ELSE 0 END)/COUNT(*),0) win10,
            COUNT(DISTINCT ticker) tickers
        FROM backtest_trade_signals
        WHERE mode=? AND signal_direction='BUY'
          AND max_gain_pct<=300 AND max_drawdown_pct>=-90
          AND {sig_cond} AND {date_cond}
    ''', (mode, *sig_params, *date_params)).fetchone()
    if not row or not row['n']:
        return None
    return {'n': row['n'], 'avg_gain': row['avg_gain'] or 0, 'avg_mdd': row['avg_mdd'] or 0,
            'hold': row['hold'] or 0, 'win20': row['win20'] or 0, 'win10': row['win10'] or 0,
            'tickers': row['tickers'] or 0}


def _candidate(conn, mode, label, kind, sig_cond, sig_params, ph_cond, ph_params):
    build = _stats(conn, mode, sig_cond, sig_params, f"trade_date < ?{ph_cond}", [SPLIT_DATE, *ph_params])
    valid = _stats(conn, mode, sig_cond, sig_params, f"trade_date >= ?{ph_cond}", [SPLIT_DATE, *ph_params])
    if not valid or valid['n'] < MIN_VALID_N or not build:
        return None
    gap = abs(valid['win20'] - build['win20'])
    consistent = gap <= 12
    consist_w = max(0.4, 1 - gap / 40.0)
    risk_w = 1.0 / (1 + abs(valid['avg_mdd']) / 60.0)
    score = round(valid['win20'] * consist_w * risk_w, 1)
    return {'method': label, 'kind': kind, 'build': build, 'valid': valid,
            'score': score, 'gap': gap, 'consistent': consistent}


def rank_methods(mode: str, market_phase: str = None) -> list:
    """방식 + 기존봇 + 단순보유 통합 랭킹. 국면 주면 그 국면 세분화 비교.
    kind: baseline_hold / baseline_bot / signal / combo."""
    from base.database import get_db_connection
    conn = get_db_connection()
    out = []
    try:
        ph_cond = (' AND market_phase=?' if market_phase else '')
        ph_params = ([market_phase] if market_phase else [])
        cands = []
        # ① 단순보유(국면 무관 전체 평균) — regime 안 보는 베이스라인
        cands.append(_candidate(conn, mode, '단순보유(국면무관)', 'baseline_hold', '1=1', [], '', []))
        # ② 봇 8단계(국면조건부) — 이 국면 전체 신호 평균 = 봇이 국면 구분으로 얻는 성과
        if market_phase:
            cands.append(_candidate(conn, mode, f'봇 8단계({market_phase})', 'baseline_bot', '1=1', [], ph_cond, ph_params))
        # ③ 개별 신호 6종
        for sig in SIGNALS:
            cands.append(_candidate(conn, mode, sig, 'signal', 'signal_types LIKE ?', [f'%{sig}%'], ph_cond, ph_params))
        # ④ 2신호 조합 15
        for a, b in PAIRS:
            cands.append(_candidate(conn, mode, f'{a}+{b}', 'combo',
                                    'signal_types LIKE ? AND signal_types LIKE ?', [f'%{a}%', f'%{b}%'], ph_cond, ph_params))
        out = [c for c in cands if c]
        out.sort(key=lambda x: x['score'], reverse=True)
        # 봇/단순보유 기준선 점수 (비교용)
        bot_ref = next((c['valid']['win20'] for c in out if c['kind'] == 'baseline_bot'), None)
        hold_ref = next((c['valid']['win20'] for c in out if c['kind'] == 'baseline_hold'), None)
        for i, m in enumerate(out):
            m['rank'] = i + 1
            v, b = m['valid'], m['build']
            base_cmp = bot_ref if bot_ref is not None else hold_ref
            delta = (v['win20'] - base_cmp) if (base_cmp is not None and m['kind'] in ('signal', 'combo')) else None
            if m['kind'] == 'baseline_hold':
                verdict = '⚪ 기준선(단순보유)'
            elif m['kind'] == 'baseline_bot':
                verdict = '🤖 기준선(봇 국면)'
            elif not m['consistent']:
                verdict = '⚠️ 노이즈 의심'
            elif delta is not None and delta >= 3 and v['win20'] >= 45:
                verdict = '✅ 봇 초과 — 채택후보'
            elif v['win20'] >= 45:
                verdict = '△ 봇 수준'
            else:
                verdict = '✗ 봇 미달'
            m['verdict'] = verdict
            m['vs_bot'] = round(delta, 0) if delta is not None else None
            cmp_line = (f"\n→ 봇 국면기준 대비 win20 {('+' if delta>=0 else '')}{delta:.0f}%p" if delta is not None else '')
            m['conclusion'] = (
                f"{verdict} · 점수 {m['score']}\n"
                f"검증(2021~): 20%달성 {v['win20']:.0f}% · 평균상승 +{v['avg_gain']:.0f}% · 낙폭 {v['avg_mdd']:.0f}% · "
                f"보유 ~{v['hold']:.0f}일 · 표본 {v['n']:,}건({v['tickers']}종목)\n"
                f"만들기(~2020): 20%달성 {b['win20']:.0f}% (차이 {m['gap']:.0f}%p — "
                + ("일관, 우위 정당" if m['consistent'] else "불일치, 운빨 가능") + ")"
                + cmp_line
            )
        return out
    finally:
        conn.close()


def _has_raw(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='backtest_trade_signals'").fetchone() is not None \
        and conn.execute("SELECT 1 FROM backtest_trade_signals LIMIT 1").fetchone() is not None


def rebuild_method_ranking_cache():
    """로컬에서 방식 랭킹 미리계산 → method_ranking_cache(JSON). EC2 경량 db가 읽음."""
    import datetime as _dt
    from base.database import get_db_connection, db_lock
    phases = [None, 'BULL_LATE', 'BULL_MID', 'BULL_EARLY', 'BEAR_MID', 'BEAR_EARLY',
              'BEAR_LATE', 'SIDEWAYS', 'PANIC']
    with db_lock:
        conn = get_db_connection()
        try:
            conn.execute('''CREATE TABLE IF NOT EXISTS method_ranking_cache (
                mode TEXT, phase TEXT, json TEXT, updated_at TEXT, PRIMARY KEY (mode, phase))''')
            now = _dt.datetime.now().isoformat(timespec='seconds')
            n = 0
            for mode in ('KR', 'US'):
                for ph in phases:
                    rows = rank_methods(mode, ph)
                    conn.execute('INSERT OR REPLACE INTO method_ranking_cache VALUES (?,?,?,?)',
                                 (mode, ph or '*', json.dumps(rows, ensure_ascii=False), now))
                    n += 1
            conn.commit()
            return n
        finally:
            conn.close()


def get_ranked_methods(mode: str, market_phase: str = None) -> list:
    """원본 있으면 즉석계산, 없으면(EC2 경량) 캐시 조회."""
    from base.database import get_db_connection
    conn = get_db_connection()
    try:
        if _has_raw(conn):
            return rank_methods(mode, market_phase)
        row = conn.execute('SELECT json FROM method_ranking_cache WHERE mode=? AND phase=?',
                           (mode, market_phase or '*')).fetchone()
        return json.loads(row['json']) if row and row['json'] else []
    finally:
        conn.close()


if __name__ == '__main__':
    import logging, sys
    logging.disable(logging.CRITICAL)
    m = sys.argv[1] if len(sys.argv) > 1 else 'KR'
    ph = sys.argv[2] if len(sys.argv) > 2 else None
    for x in rank_methods(m, ph)[:8]:
        print(f"[{x['rank']}] {x['method']:22} {x['verdict']:18} 점수{x['score']} 검증win20 {x['valid']['win20']:.0f}%")
