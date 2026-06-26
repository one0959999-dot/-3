"""알고리즘 엔진 — (진입신호 × 청산전략)을 price_path에 시뮬해 실현수익으로 OOS 랭킹.

기존 method_ranking(고점 win20, 룩어헤드)의 결함을 대체:
- 실현수익(인과적 청산) 으로 채점
- 기준선 = 단순보유(만기) 실제 수익
- 채점 = 검증기간 'buy-hold 이긴 비율' × (실현수익 중앙값 / 위험) — TODO STEP5
- OOS 일관성(만들기↔검증) 통과한 것만 채택
"""
import json
import statistics as _st
from base.sim_engine import simulate_exit, buy_hold_return, parse_path, EXIT_STRATEGIES

SPLIT_DATE = '2021-01-01'
MIN_N = 60
SIGNALS = ['RSI_BUY', 'MACD_BUY', 'BB_BUY', 'MA_BUY', 'VOL_BUY', 'BREAK_BUY']


def _agg(paths, kind, p):
    """경로집합에 청산전략 적용 → 실현 통계."""
    if not paths:
        return None
    rets, mdds, beats = [], [], 0
    for path in paths:
        r, m, _ = simulate_exit(path, kind, p)
        rets.append(r); mdds.append(m)
        if r > buy_hold_return(path):
            beats += 1
    n = len(rets)
    med = _st.median(rets)
    mdd_med = _st.median(mdds)
    return {'n': n, 'ret_med': round(med, 1), 'ret_avg': round(_st.mean(rets), 1),
            'win': round(100 * sum(1 for x in rets if x > 0) / n, 0),
            'beat_bh': round(100 * beats / n, 0), 'mdd_med': round(mdd_med, 1)}


def _load_paths(conn, mode, phase, sig, period):
    cond = "trade_date < ?" if period == 'build' else "trade_date >= ?"
    rows = conn.execute(f'''
        SELECT price_path_json FROM backtest_trade_signals
        WHERE mode=? AND market_phase=? AND signal_direction='BUY'
          AND signal_types LIKE ? AND price_path_json LIKE '[%' AND {cond}
    ''', (mode, phase, f'%{sig}%', SPLIT_DATE)).fetchall()
    out = [parse_path(r[0]) for r in rows]
    return [p for p in out if p]


def analyze(mode, phase):
    """국면별 (신호×청산) 실현수익 OOS 랭킹 + 단순보유 기준선."""
    from base.database import get_db_connection
    conn = get_db_connection()
    try:
        results = []
        # 단순보유 기준선 (이 국면 전체 신호, 만기보유 실현)
        all_b = _load_paths(conn, mode, phase, '', 'build') if False else None
        for sig in SIGNALS:
            bpaths = _load_paths(conn, mode, phase, sig, 'build')
            vpaths = _load_paths(conn, mode, phase, sig, 'valid')
            if len(vpaths) < MIN_N or len(bpaths) < MIN_N:
                continue
            v_bh = round(_st.median([buy_hold_return(p) for p in vpaths]), 1)
            # 이 신호에서 각 청산전략 평가 → 검증 best 선택
            cand = []
            for label, kind, p in EXIT_STRATEGIES:
                vb = _agg(vpaths, kind, p); bb = _agg(bpaths, kind, p)
                if not vb or not bb:
                    continue
                # 점수 = 보유이김% × (실현수익중앙값 / 위험)  (검증기간)
                risk = abs(vb['mdd_med']) + 5
                score = round(vb['beat_bh'] * (vb['ret_med'] / risk) , 2)
                gap = abs(vb['beat_bh'] - bb['beat_bh'])
                cand.append({'exit': label, 'valid': vb, 'build': bb,
                             'score': score, 'gap': gap, 'consistent': gap <= 15})
            if not cand:
                continue
            best = max(cand, key=lambda c: c['score'])
            results.append({'signal': sig, 'exit': best['exit'], 'valid': best['valid'],
                            'build': best['build'], 'score': best['score'], 'gap': best['gap'],
                            'consistent': best['consistent'], 'buyhold': v_bh,
                            'all_exits': cand})
        # 정렬: 검증 점수 desc
        results.sort(key=lambda x: x['score'], reverse=True)
        for i, r in enumerate(results):
            r['rank'] = i + 1
            v = r['valid']
            beat_bh = v['beat_bh']
            if not r['consistent']:
                verdict = '⚠️ 노이즈(만들기↔검증 불일치)'
            elif beat_bh >= 55 and v['ret_med'] > r['buyhold']:
                verdict = '✅ 단순보유 초과 — 채택후보'
            elif beat_bh >= 48:
                verdict = '△ 보유 수준'
            else:
                verdict = '✗ 보유 미달(들고가는 게 나음)'
            r['verdict'] = verdict
            r['conclusion'] = (
                f"{verdict} · 점수 {r['score']}\n"
                f"전략: {r['signal']} 진입 → {r['exit']} 청산\n"
                f"검증(2021~): 실현 중앙값 {v['ret_med']:+.0f}% · 승률 {v['win']:.0f}% · "
                f"보유이김 {v['beat_bh']:.0f}% · 보유중MDD {v['mdd_med']:.0f}% · 표본 {v['n']:,}\n"
                f"단순보유(만기) 실현 중앙값 {r['buyhold']:+.0f}% — "
                + ("전략이 더 나음" if v['ret_med'] > r['buyhold'] else "보유가 더 나음")
                + f" · 만들기일관 {'O' if r['consistent'] else 'X'}"
            )
        return results
    finally:
        conn.close()


def _has_raw(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='backtest_trade_signals'").fetchone() is not None \
        and conn.execute("SELECT 1 FROM backtest_trade_signals LIMIT 1").fetchone() is not None


PHASES = ['BULL_LATE', 'BULL_MID', 'BULL_EARLY', 'BEAR_MID', 'BEAR_EARLY',
          'BEAR_LATE', 'SIDEWAYS', 'PANIC']


def build_algo_cache():
    """로컬 계산 → algo_ranking_cache(국면별 전략랭킹) + algo_ruletable(국면→최적전략)."""
    import datetime as _dt
    from base.database import get_db_connection, db_lock
    with db_lock:
        conn = get_db_connection()
        try:
            conn.execute('''CREATE TABLE IF NOT EXISTS algo_ranking_cache (
                mode TEXT, phase TEXT, json TEXT, updated_at TEXT, PRIMARY KEY(mode,phase))''')
            conn.execute('''CREATE TABLE IF NOT EXISTS algo_ruletable (
                mode TEXT, phase TEXT, signal TEXT, exit_rule TEXT, verdict TEXT,
                ret_med REAL, beat_bh REAL, buyhold REAL, json TEXT, updated_at TEXT,
                PRIMARY KEY(mode,phase))''')
            now = _dt.datetime.now().isoformat(timespec='seconds')
            n = 0
            for mode in ('KR', 'US'):
                for ph in PHASES:
                    rows = analyze(mode, ph)
                    conn.execute('INSERT OR REPLACE INTO algo_ranking_cache VALUES (?,?,?,?)',
                                 (mode, ph, json.dumps(rows, ensure_ascii=False), now))
                    # 룰표: 1위가 '채택후보'면 그 전략, 아니면 단순보유 유지
                    top = rows[0] if rows else None
                    if top and '채택' in top['verdict']:
                        conn.execute('INSERT OR REPLACE INTO algo_ruletable VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (mode, ph, top['signal'], top['exit'], top['verdict'],
                             top['valid']['ret_med'], top['valid']['beat_bh'], top['buyhold'],
                             json.dumps(top, ensure_ascii=False), now))
                    else:
                        conn.execute('INSERT OR REPLACE INTO algo_ruletable VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (mode, ph, '단순보유', '만기보유', '보유 우위(전략 무의미)',
                             top['buyhold'] if top else 0, 0, top['buyhold'] if top else 0, '{}', now))
                    n += 1
            conn.commit()
            return n
        finally:
            conn.close()


def get_algo_ranking(mode, phase):
    from base.database import get_db_connection
    conn = get_db_connection()
    try:
        if _has_raw(conn) and phase:
            return analyze(mode, phase)
        row = conn.execute('SELECT json FROM algo_ranking_cache WHERE mode=? AND phase=?',
                           (mode, phase)).fetchone()
        return json.loads(row['json']) if row and row['json'] else []
    finally:
        conn.close()


def get_ruletable(mode):
    from base.database import get_db_connection
    conn = get_db_connection()
    try:
        rows = conn.execute('''SELECT phase,signal,exit_rule,verdict,ret_med,beat_bh,buyhold
            FROM algo_ruletable WHERE mode=? ORDER BY phase''', (mode,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
