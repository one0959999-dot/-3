"""자기개선 루프 — 제안 → 백테스트 검증 → 자동반영 (안전게이트 포함).

국면별 진입 품질 게이트(min_win20)를 backtest_trade_signals로 야간 최적화한다.
라이브 entry_engine.evaluate 가 strategy_params 에서 이 값을 읽어 즉시 반영(완전 자동).

안전 원칙(라이브 매매 파라미터를 자동변경하므로):
  - 범위 제한 [FLOOR, CEIL]                — 극단값 차단
  - 표본 최소치 / 거래 보존율               — 과최적화·과잉제한 방지
  - 개선 마진 충족시에만 반영               — 잡음 변경 차단
  - 모든 변경 이력 보존(strategy_params_history) — 감사·롤백
  - 전역 토글 SELF_IMPROVE_ENABLED          — 즉시 중단 가능
  - 손절/포지션사이징/killswitch 는 절대 건드리지 않음 (진입 품질 게이트만)
"""
import logging

logger = logging.getLogger('lassi_bot')

SELF_IMPROVE_ENABLED = True          # 자동반영 전역 스위치
PARAM_NAME           = 'min_win20'
WIN20_FLOOR          = 35.0          # 임계값 하한(너무 느슨 방지)
WIN20_CEIL           = 60.0          # 임계값 상한(너무 빡빡=거래없음 방지)
GRID                 = [35, 40, 45, 50, 55, 60]
DEFAULT_WIN20        = 45.0
MIN_RETAIN_FRAC      = 0.20          # 국면 전체 거래의 20% 이상 보존
MIN_ABS_TRADES       = 200           # 채택 임계값에서 최소 거래 수
IMPROVE_MARGIN       = 1.0           # 현행 대비 가중평균 최대상승 +1%p 이상일 때만 반영
PHASES = ['PANIC', 'BEAR_EARLY', 'BEAR_MID', 'BEAR_LATE',
          'SIDEWAYS', 'BULL_EARLY', 'BULL_MID', 'BULL_LATE']


def _phase_combos(conn, mode, phase):
    """국면 내 신호조합별 (거래수, 평균최대상승, win20) 목록."""
    rows = conn.execute('''
        SELECT signal_types,
               COUNT(*) n,
               AVG(max_gain_pct) avg_gain,
               100.0*SUM(CASE WHEN max_gain_pct>=20 THEN 1 ELSE 0 END)/COUNT(*) win20
        FROM backtest_trade_signals
        WHERE mode=? AND signal_direction='BUY' AND market_phase=?
          AND max_gain_pct<=300 AND max_drawdown_pct>=-90
        GROUP BY signal_types
    ''', (mode, phase)).fetchall()
    return [(r['n'], r['avg_gain'] or 0.0, r['win20'] or 0.0) for r in rows]


def _eval_threshold(combos, t):
    """임계값 t 적용시 (거래수, 가중평균 최대상승). win20>=t 조합만 진입."""
    inc = [(n, g) for (n, g, w) in combos if w >= t]
    total = sum(n for n, _ in inc)
    if total == 0:
        return 0, 0.0
    wgain = sum(g * n for n, g in inc) / total
    return total, wgain


def optimize_entry_thresholds(mode: str, dry_run: bool = False) -> list:
    """국면별 최적 min_win20 탐색 후 (안전게이트 통과시) 자동반영.
    반환: 변경/제안 내역 리스트(dict)."""
    from base.database import get_db_connection, get_strategy_param, set_strategy_param
    results = []
    conn = get_db_connection()
    try:
        for phase in PHASES:
            combos = _phase_combos(conn, mode, phase)
            all_trades = sum(n for n, _, _ in combos)
            if all_trades < MIN_ABS_TRADES:
                continue
            base_total, base_gain = _eval_threshold(combos, DEFAULT_WIN20)
            # 안전범위 내 후보 평가
            best_t, best_total, best_gain = DEFAULT_WIN20, base_total, base_gain
            for t in GRID:
                if not (WIN20_FLOOR <= t <= WIN20_CEIL):
                    continue
                total, wgain = _eval_threshold(combos, t)
                if total < MIN_ABS_TRADES:
                    continue
                if total < all_trades * MIN_RETAIN_FRAC:   # 과잉제한 방지
                    continue
                if wgain > best_gain:
                    best_t, best_total, best_gain = t, total, wgain
            cur = get_strategy_param(mode, phase, PARAM_NAME, DEFAULT_WIN20)
            improve = best_gain - base_gain
            rec = {'mode': mode, 'phase': phase, 'current': cur, 'proposed': float(best_t),
                   'base_gain': round(base_gain, 2), 'new_gain': round(best_gain, 2),
                   'improve_pp': round(improve, 2), 'trades': best_total, 'applied': False}
            # 자동반영 게이트: 개선마진 충족 + 현행과 다름
            if (SELF_IMPROVE_ENABLED and not dry_run
                    and improve >= IMPROVE_MARGIN and abs(best_t - cur) >= 1e-9):
                basis = (f"opt {mode}/{phase}: win20>={best_t:.0f} 가중상승 "
                         f"{base_gain:.1f}→{best_gain:.1f}% ({best_total:,}건)")
                if set_strategy_param(mode, phase, PARAM_NAME, float(best_t), basis):
                    rec['applied'] = True
                    logger.info(f"[자기개선] {basis}")
            results.append(rec)
    finally:
        conn.close()
    return results


def run_all(dry_run: bool = False) -> dict:
    """KR+US 전체 자기개선 1회 실행. 반환: {mode: [recs]}"""
    out = {}
    for mode in ('KR', 'US'):
        try:
            out[mode] = optimize_entry_thresholds(mode, dry_run=dry_run)
        except Exception as e:
            logger.error(f"[자기개선] {mode} 오류: {e}")
            out[mode] = []
    return out


if __name__ == '__main__':
    import json
    logging.disable(logging.CRITICAL)
    print(json.dumps(run_all(dry_run=True), ensure_ascii=False, indent=2, default=str))
