"""백테스트 기반 진입 규칙 엔진 — 점수제를 대체할 단일 의사결정 엔진.

입력: 종목 OHLCV(df) + 시장국면 + 섹터
처리: base.signals(백테스트 동일 신호) + signal_forecast(과거 통계) 결합
출력: {decision, score, expected_return, hold_days, stop_pct, win_rate, signals, reason}

라이브(후보선정·진입)와 백테스트가 '같은 엔진'을 쓰게 하는 것이 목적.
"""
from base.signals import detect_latest_signals
from base.signal_forecast import get_forecast


def evaluate(mode: str, df, market_phase: str, sector: str = '기타',
             min_win: float = 55.0, min_n: int = 20) -> dict:
    """매수 후보 평가. 백테스트 신호+통계로 진입 여부·예상치 산출.

    decision=True 조건: 매수신호 존재 + 과거 동일조건 승률(min_win) 이상.
    """
    sigs = detect_latest_signals(df)
    buy_sigs = [s for s in sigs if 'BUY' in s]
    base = {
        'decision': False, 'score': 0, 'signals': sigs, 'buy_signals': buy_sigs,
        'expected_return': None, 'hold_days': None, 'stop_pct': None,
        'win_rate': None, 'n': 0, 'market_phase': market_phase, 'sector': sector,
        'reason': '',
    }
    if not buy_sigs:
        base['reason'] = '매수 신호 없음'
        return base

    fc = get_forecast(mode, market_phase, buy_sigs, min_n=min_n)
    if not fc:
        base['reason'] = f"{'·'.join(buy_sigs)} (과거 표본 부족)"
        # 신호는 있으나 통계 부족 → 약한 매수(보수적 통과)
        base['decision'] = True
        base['score'] = 50
        return base

    base.update({
        'expected_return': fc['target'], 'hold_days': fc['hold_days'],
        'stop_pct': fc['stop'], 'win_rate': fc['win10'], 'n': fc['n'],
    })
    # 점수 = 승률 기반 (정렬·우선순위용). 신호 2개 이상이면 가산점.
    score = fc['win10'] + (10 if len(buy_sigs) >= 2 else 0)
    base['score'] = round(score, 1)
    base['decision'] = fc['win10'] >= min_win
    base['reason'] = (f"{'·'.join(buy_sigs)} | {market_phase} | "
                      f"과거 {fc['n']:,}건 승률 {fc['win10']:.0f}% "
                      f"(목표 +{fc['target']:.0f}% / 보유 ~{fc['hold_days']:.0f}일 / 손절 {fc['stop']:.0f}%)")
    return base


def evaluate_ensemble(mode: str, df, market_phase: str, score_agrees: bool,
                      sector: str = '기타', min_win: float = 55.0) -> dict:
    """앙상블 진입 — 백테스트(신호+통계) AND 점수제(score_agrees) 둘 다 동의할 때만 매수.

    대결 결과(앙상블 평균수익 최고)에 근거한 진입 방식.
    score_agrees: 라이브 점수제가 매수 동의하는지(calculate_entry_score>=threshold 결과) 전달.
    """
    r = evaluate(mode, df, market_phase, sector, min_win=min_win)
    engine_buy = r['decision']
    r['engine_buy'] = engine_buy
    r['score_buy'] = bool(score_agrees)
    # 앙상블: 둘 다 동의해야 최종 매수
    r['decision'] = engine_buy and bool(score_agrees)
    if engine_buy and not score_agrees:
        r['reason'] = '엔진 매수 but 점수제 미동의 → 보류 ' + r.get('reason', '')
    elif score_agrees and not engine_buy:
        r['reason'] = '점수제 매수 but 엔진 미동의 → 보류'
    elif r['decision']:
        r['reason'] = '✅앙상블 동의(신호+점수) | ' + r.get('reason', '')
    return r


def rank_candidates(mode: str, candidates: list, market_phase: str) -> list:
    """후보 종목들 평가 후 '예상수익률(score) 높은 순' 정렬.

    candidates: [{'ticker','name','df','sector'}, ...]
    반환: evaluate 결과 dict 리스트 (decision=True만, score desc)
    """
    out = []
    for c in candidates:
        try:
            r = evaluate(mode, c.get('df'), market_phase, c.get('sector', '기타'))
            if r['decision']:
                r['ticker'] = c.get('ticker')
                r['name'] = c.get('name')
                r['price'] = c.get('price')
                out.append(r)
        except Exception:
            continue
    # 예상수익률 우선, 동률이면 승률
    out.sort(key=lambda r: (r.get('expected_return') or 0, r.get('win_rate') or 0), reverse=True)
    return out
