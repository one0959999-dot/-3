"""신호 예측 — 국면×신호조합의 과거 통계로 보유기간/목표/손절 예측.

라이브 매수 신호 발생 시 호출 → "예상 보유 N일 / 목표 +X% / 손절 -Y%" 메시지 생성.
backtest_trade_signals(205만+) 원본을 직접 조회(인덱스 idx_bts_mode_phase 활용).
데이터가 쌓일수록 자동으로 더 정확해짐(별도 재학습 불필요).
"""
import json

# 신호 종류 (단일신호 폴백 캐시 키 생성용)
_SIGNAL_TYPES = ['RSI_BUY', 'MACD_BUY', 'BB_BUY', 'MA_BUY', 'VOL_BUY', 'BREAK_BUY']
_PHASE_AVG_KEY = '__PHASE_AVG__'
_ANY_PHASE = '*'
_AGG_COLS = '''COUNT(*) n, ROUND(AVG(days_to_peak),0) hold_days,
    ROUND(AVG(max_gain_pct),1) target, ROUND(AVG(max_drawdown_pct),1) stop,
    ROUND(100.0*SUM(CASE WHEN max_gain_pct>=10 THEN 1 ELSE 0 END)/COUNT(*),0) win10,
    ROUND(100.0*SUM(CASE WHEN max_gain_pct>=20 THEN 1 ELSE 0 END)/COUNT(*),0) win20'''


def _has_cache(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='forecast_cache'").fetchone() is not None


def _has_raw(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='backtest_trade_signals'").fetchone() is not None


def rebuild_forecast_cache(mode: str):
    """get_forecast/get_phase_avg가 쓰는 모든 조회를 미리 계산 → forecast_cache.
    이러면 라이브가 원본(3.9GB)을 런타임에 안 읽어도 됨(EC2 렉 해결). 로컬에서 1회 빌드 후 배포.
    캐시 키: 정확조합(json) / 단일신호+국면 / 단일신호+'*' / 국면평균('__PHASE_AVG__')."""
    from base.database import get_db_connection, db_lock
    with db_lock:
        conn = get_db_connection()
        try:
            conn.execute('''CREATE TABLE IF NOT EXISTS forecast_cache (
                mode TEXT, phase TEXT, sig_key TEXT, n INTEGER, hold_days REAL,
                target REAL, stop REAL, win10 REAL, win20 REAL,
                PRIMARY KEY (mode, phase, sig_key))''')
            conn.execute('DELETE FROM forecast_cache WHERE mode=?', (mode,))
            base_where = "mode=? AND signal_direction='BUY' AND max_gain_pct<=300 AND max_drawdown_pct>=-90"
            ins = '''INSERT OR REPLACE INTO forecast_cache
                     (mode,phase,sig_key,n,hold_days,target,stop,win10,win20) VALUES (?,?,?,?,?,?,?,?,?)'''
            # 1) 정확조합: (phase, signal_types) 별
            for r in conn.execute(f'''SELECT market_phase phase, signal_types sk, {_AGG_COLS}
                    FROM backtest_trade_signals WHERE {base_where}
                    GROUP BY market_phase, signal_types''', (mode,)).fetchall():
                conn.execute(ins, (mode, r['phase'], r['sk'], r['n'], r['hold_days'],
                                   r['target'], r['stop'], r['win10'], r['win20']))
            # 2) 단일신호 × 국면  /  3) 단일신호 × 전체국면
            for sig in _SIGNAL_TYPES:
                like = f'%{sig}%'
                for r in conn.execute(f'''SELECT market_phase phase, {_AGG_COLS}
                        FROM backtest_trade_signals WHERE {base_where} AND signal_types LIKE ?
                        GROUP BY market_phase''', (mode, like)).fetchall():
                    conn.execute(ins, (mode, r['phase'], sig, r['n'], r['hold_days'],
                                       r['target'], r['stop'], r['win10'], r['win20']))
                ra = conn.execute(f'''SELECT {_AGG_COLS} FROM backtest_trade_signals
                        WHERE {base_where} AND signal_types LIKE ?''', (mode, like)).fetchone()
                if ra and ra['n']:
                    conn.execute(ins, (mode, _ANY_PHASE, sig, ra['n'], ra['hold_days'],
                                       ra['target'], ra['stop'], ra['win10'], ra['win20']))
            # 4) 국면평균
            for r in conn.execute(f'''SELECT market_phase phase, {_AGG_COLS}
                    FROM backtest_trade_signals WHERE {base_where}
                    GROUP BY market_phase''', (mode,)).fetchall():
                conn.execute(ins, (mode, r['phase'], _PHASE_AVG_KEY, r['n'], r['hold_days'],
                                   r['target'], r['stop'], r['win10'], r['win20']))
            conn.commit()
            return conn.execute('SELECT COUNT(*) FROM forecast_cache WHERE mode=?', (mode,)).fetchone()[0]
        finally:
            conn.close()


def _row_to_fc(row):
    return {'n': row['n'], 'hold_days': row['hold_days'], 'target': row['target'],
            'stop': row['stop'], 'win10': row['win10'], 'win20': row['win20']}


def get_forecast(mode: str, market_phase: str, signal_types, min_n: int = 20) -> dict | None:
    """(mode, 국면, 신호조합) 과거 통계. forecast_cache 우선(O(1)), 없으면 원본 조회."""
    from base.database import get_db_connection
    if isinstance(signal_types, (list, tuple)):
        sig_json = json.dumps(list(signal_types), ensure_ascii=False)
        first_sig = signal_types[0] if signal_types else ''
    else:
        sig_json = signal_types
        first_sig = signal_types

    conn = get_db_connection()
    try:
        if _has_cache(conn):
            # 캐시 3단계 폴백 (원본 쿼리와 동일 우선순위)
            for ph, sk in ((market_phase, sig_json), (market_phase, first_sig), (_ANY_PHASE, first_sig)):
                row = conn.execute('SELECT * FROM forecast_cache WHERE mode=? AND phase=? AND sig_key=?',
                                   (mode, ph, sk)).fetchone()
                if row and row['n'] and row['n'] >= min_n:
                    return _row_to_fc(row)
            if not _has_raw(conn):
                return None
        # 원본 직접 조회 (로컬 / 캐시미스)
        for cond, params in (
            ("market_phase=? AND signal_types=?", (market_phase, sig_json)),
            ("market_phase=? AND signal_types LIKE ?", (market_phase, f'%{first_sig}%')),
            ("signal_types LIKE ?", (f'%{first_sig}%',)),
        ):
            row = conn.execute(f'''SELECT {_AGG_COLS} FROM backtest_trade_signals
                WHERE mode=? AND signal_direction='BUY'
                  AND max_gain_pct<=300 AND max_drawdown_pct>=-90 AND {cond}''',
                (mode, *params)).fetchone()
            if row and row['n'] and row['n'] >= min_n:
                return _row_to_fc(row)
        return None
    finally:
        conn.close()


def get_phase_avg(mode: str, market_phase: str) -> dict | None:
    """국면 전체 BUY 평균. 캐시 우선, 없으면 원본."""
    from base.database import get_db_connection
    conn = get_db_connection()
    try:
        if _has_cache(conn):
            row = conn.execute('SELECT * FROM forecast_cache WHERE mode=? AND phase=? AND sig_key=?',
                               (mode, market_phase, _PHASE_AVG_KEY)).fetchone()
            if row and row['n'] and row['n'] >= 30:
                return _row_to_fc(row)
            if not _has_raw(conn):
                return None
        row = conn.execute(f'''SELECT {_AGG_COLS} FROM backtest_trade_signals
            WHERE mode=? AND signal_direction='BUY' AND market_phase=?
              AND max_gain_pct<=300 AND max_drawdown_pct>=-90''',
            (mode, market_phase)).fetchone()
        if row and row['n'] and row['n'] >= 30:
            return _row_to_fc(row)
        return None
    finally:
        conn.close()


def estimate_candidate_return(mode: str, df, market_phase: str) -> dict | None:
    """후보 종목 '지금 사면 예상수익' 추정.
    신호 발생시 그 신호 통계, 미발생시 국면평균. (정렬·표시용)
    """
    from base.signals import detect_latest_signals
    sigs = [s for s in detect_latest_signals(df) if 'BUY' in s]
    if sigs:
        fc = get_forecast(mode, market_phase, sigs)
        if fc:
            fc['basis'] = '·'.join(sigs); return fc
    pa = get_phase_avg(mode, market_phase)
    if pa:
        pa['basis'] = '국면평균'; return pa
    return None


def format_forecast_msg(name, ticker, price, market_phase_kr, signal_types, fc: dict) -> str:
    """예측 매수 메시지 4줄 생성."""
    sig = ' · '.join(signal_types) if isinstance(signal_types, (list, tuple)) else signal_types
    if not fc:
        return (f"🎣 {name}({ticker}) 매수\n"
                f"├ 진입가: {price:,.0f}\n"
                f"└ 신호: {sig} (과거 표본 부족 — 예측 보류)")
    stop_price = price * (1 + fc['stop'] / 100)
    return (f"🎣 {name}({ticker}) 매수  ·  {market_phase_kr}\n"
            f"├ 진입가: {price:,.0f}\n"
            f"├ 예상 보유: ~{fc['hold_days']:.0f}일\n"
            f"├ 목표수익: +{fc['target']:.0f}%\n"
            f"├ 손절선: {fc['stop']:.0f}% ({stop_price:,.0f})\n"
            f"└ 근거: {sig} | 과거 {fc['n']:,}건 중 10%달성 {fc['win10']:.0f}%")
