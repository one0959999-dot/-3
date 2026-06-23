"""신호 예측 — 국면×신호조합의 과거 통계로 보유기간/목표/손절 예측.

라이브 매수 신호 발생 시 호출 → "예상 보유 N일 / 목표 +X% / 손절 -Y%" 메시지 생성.
backtest_trade_signals(205만+) 원본을 직접 조회(인덱스 idx_bts_mode_phase 활용).
데이터가 쌓일수록 자동으로 더 정확해짐(별도 재학습 불필요).
"""
import json


def get_forecast(mode: str, market_phase: str, signal_types, min_n: int = 20) -> dict | None:
    """(mode, 국면, 신호조합) 과거 통계 반환. 표본 부족하면 신호 단독으로 폴백."""
    from base.database import get_db_connection
    if isinstance(signal_types, (list, tuple)):
        sig_json = json.dumps(list(signal_types), ensure_ascii=False)
        first_sig = signal_types[0] if signal_types else ''
    else:
        sig_json = signal_types
        first_sig = signal_types

    conn = get_db_connection()
    try:
        # 1순위: 정확한 (국면 × 신호조합) 일치
        for cond, params in (
            ("market_phase=? AND signal_types=?", (market_phase, sig_json)),
            ("market_phase=? AND signal_types LIKE ?", (market_phase, f'%{first_sig}%')),
            ("signal_types LIKE ?", (f'%{first_sig}%',)),
        ):
            row = conn.execute(f'''
                SELECT COUNT(*) n,
                   ROUND(AVG(days_to_peak),0) hold_days,
                   ROUND(AVG(max_gain_pct),1) target,
                   ROUND(AVG(max_drawdown_pct),1) stop,
                   ROUND(100.0*SUM(CASE WHEN max_gain_pct>=10 THEN 1 ELSE 0 END)/COUNT(*),0) win10
                FROM backtest_trade_signals
                WHERE mode=? AND signal_direction='BUY'
                  AND max_gain_pct<=300 AND max_drawdown_pct>=-90 AND {cond}
            ''', (mode, *params)).fetchone()
            if row and row['n'] and row['n'] >= min_n:
                return {'n': row['n'], 'hold_days': row['hold_days'],
                        'target': row['target'], 'stop': row['stop'], 'win10': row['win10']}
        return None
    finally:
        conn.close()


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
