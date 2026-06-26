"""검증된 알고리즘 — algo_ruletable(OOS 실현수익 검증)에서 파생한 실행 로직.

상승/하락/횡보 3대 국면별로 "진입할지 / 어떻게 청산할지"를 데이터가 정한 룰대로 결정.
손으로 만든 규칙이 아니라 backtest_trade_signals(209만) → price_path 실현수익 OOS 검증 결과.

핵심 결론(데이터):
- 대부분 국면: 단순보유가 최강 → 신호매매 자제, 들고가기 (특히 US 전 국면, KR 하락중/패닉/상승초중).
- 예외(채택): KR 횡보=MA단기스윙 / KR 하락초기=RSI+MACD장기 / KR 상승말기=MACD+BREAK단기익절.
사용법: 봇이 decide_entry/decide_exit 호출 → 룰표 기반 행동. 룰표는 로컬 재빌드로 갱신(자동 반영).
"""
import re

# 8단계 → 3대 국면
SUPER_REGIME = {
    'PANIC': '하락', 'BEAR_EARLY': '하락', 'BEAR_MID': '하락', 'BEAR_LATE': '하락',
    'SIDEWAYS': '횡보',
    'BULL_EARLY': '상승', 'BULL_MID': '상승', 'BULL_LATE': '상승',
}


def _parse_exit(exit_rule: str):
    """청산규칙 문자열 → (kind, param)."""
    if not exit_rule or '만기' in exit_rule or '보유' == exit_rule:
        return ('hold_to_end', None)
    m = re.match(r'보유(\d+)일', exit_rule)
    if m:
        return ('hold_days', int(m.group(1)))
    m = re.match(r'트레일-(\d+)%', exit_rule)
    if m:
        return ('trail', float(m.group(1)))
    m = re.match(r'목표(\d+)손절(\d+)', exit_rule)
    if m:
        return ('tstop', (float(m.group(1)), float(m.group(2))))
    return ('hold_to_end', None)


def _parse_entry(signal: str):
    """진입기법 문자열 → (kind, required_signals|min_count)."""
    if not signal or '단순보유' in signal:
        return ('hold_only', None)          # 신호매매 안 함 (코어/보유)
    if signal.startswith('봇앙상블'):
        return ('ensemble', 2)              # 2개 이상 신호 동의
    if signal.startswith('조합:'):
        return ('and', signal.split(':', 1)[1].split('+'))
    if signal.startswith('단독:'):
        return ('single', [signal.split(':', 1)[1]])
    return ('single', [signal])


def get_rule(mode: str, phase: str) -> dict | None:
    """그 (mode, 국면)의 검증된 최적 룰 (algo_ruletable)."""
    from base.database import get_db_connection
    conn = get_db_connection()
    try:
        r = conn.execute('''SELECT phase,signal,exit_rule,verdict,ret_med,beat_bh,buyhold
            FROM algo_ruletable WHERE mode=? AND phase=?''', (mode, phase)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def decide_entry(mode: str, phase: str, fired_signals: list) -> dict:
    """진입 결정. 반환: {buy, mode_kind('hold'|'signal'), exit_kind, exit_param, reason}.
    - 채택전략이 없으면(단순보유 우위) buy=False → 신호매매 말고 보유 권장.
    - 채택전략이고 진입조건(신호) 충족 시 buy=True + 청산계획 제공."""
    rule = get_rule(mode, phase)
    if not rule:
        return {'buy': False, 'kind': 'hold', 'reason': '룰표 없음 — 보유'}
    adopt = '채택' in (rule.get('verdict') or '')
    ekind, ereq = _parse_entry(rule['signal'])
    xkind, xparam = _parse_exit(rule['exit_rule'])
    if not adopt or ekind == 'hold_only':
        return {'buy': False, 'kind': 'hold',
                'reason': f"{SUPER_REGIME.get(phase, phase)}({phase}): 검증결과 단순보유 우위 — 신호매매 자제",
                'exit_kind': 'hold_to_end', 'exit_param': None}
    fired = set(fired_signals or [])
    ok = (len([s for s in fired if 'BUY' in s]) >= ereq) if ekind == 'ensemble' \
        else all(req in fired for req in ereq)
    return {'buy': bool(ok), 'kind': 'signal', 'rule': rule['signal'],
            'exit_kind': xkind, 'exit_param': xparam,
            'reason': (f"{SUPER_REGIME.get(phase, phase)}({phase}): {rule['signal']} 충족→매수, "
                       f"{rule['exit_rule']} 청산 (검증 보유이김 {rule.get('beat_bh',0):.0f}%)"
                       if ok else f"{rule['signal']} 진입조건 미충족 — 대기")}


def decide_exit(exit_kind, exit_param, days_held: int, pnl_pct: float, peak_pnl_pct: float) -> dict:
    """청산 결정 (진입시 받은 exit 계획대로). 반환 {sell, reason}."""
    if exit_kind == 'hold_days':
        if days_held >= exit_param:
            return {'sell': True, 'reason': f'{exit_param}일 보유 만료'}
    elif exit_kind == 'trail':
        if peak_pnl_pct - pnl_pct >= exit_param:
            return {'sell': True, 'reason': f'트레일링 고점대비 -{exit_param}%'}
    elif exit_kind == 'tstop':
        tgt, stp = exit_param
        if pnl_pct >= tgt:
            return {'sell': True, 'reason': f'목표 +{tgt}% 도달'}
        if pnl_pct <= -stp:
            return {'sell': True, 'reason': f'손절 -{stp}% 도달'}
    return {'sell': False, 'reason': '보유 지속'}


def algorithm_spec(mode: str) -> dict:
    """상승/하락/횡보 3대 국면별 알고리즘 요약 (룰표 집계)."""
    from base.database import get_db_connection
    conn = get_db_connection()
    spec = {'상승': [], '하락': [], '횡보': []}
    try:
        for r in conn.execute('''SELECT phase,signal,exit_rule,verdict,ret_med,beat_bh,buyhold
            FROM algo_ruletable WHERE mode=? ORDER BY phase''', (mode,)).fetchall():
            sr = SUPER_REGIME.get(r['phase'], r['phase'])
            adopt = '채택' in (r['verdict'] or '')
            spec.setdefault(sr, []).append({
                'phase': r['phase'], 'action': (f"{r['signal']} · {r['exit_rule']}" if adopt else '단순보유(들고가기)'),
                'adopt': adopt, 'ret_med': r['ret_med'], 'beat_bh': r['beat_bh'], 'buyhold': r['buyhold'],
                'verdict': r['verdict']})
        return spec
    finally:
        conn.close()


if __name__ == '__main__':
    import logging, sys
    logging.disable(logging.CRITICAL)
    m = sys.argv[1] if len(sys.argv) > 1 else 'KR'
    sp = algorithm_spec(m)
    for reg in ('상승', '하락', '횡보'):
        print(f"\n[{reg}장] ({m})")
        for x in sp.get(reg, []):
            print(f"  {x['phase']:11} → {x['action']}  ({x['verdict']})")
