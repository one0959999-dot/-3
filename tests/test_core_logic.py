"""핵심 결정 로직 characterization 테스트 — 리팩터 안전망.

목적: 현재 동작을 '박제'해서, 앞으로 코드를 바꿔도 결정 로직이 변하지 않았는지 즉시 확인.
실행: python tests/test_core_logic.py   (pytest 불필요, 순수 assert)

원칙: 실데이터 값이 변해도 깨지지 않도록 '불변식(invariant)'을 검증한다.
(예: win20<기준이면 반드시 차단, killswitch -20%면 반드시 L2)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import pandas as pd
import numpy as np

_passed = 0
_failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name}")


def _synth_df(n=300, trend=0.0, start=10000.0):
    """합성 OHLCV — 신호/지표 테스트용."""
    rng = np.random.default_rng(42)
    close = start * np.cumprod(1 + trend + rng.normal(0, 0.015, n))
    high = close * (1 + np.abs(rng.normal(0, 0.01, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.01, n)))
    vol = rng.integers(1e5, 1e6, n).astype(float)
    return pd.DataFrame({'open': close, 'high': high, 'low': low,
                         'close': close, 'volume': vol})


# ── 1. base.signals ──────────────────────────────────────────────
def test_signals():
    from base.signals import calc_indicators, detect_latest_signals
    df = calc_indicators(_synth_df())
    check("signals: 지표컬럼 생성", 'rsi' in df.columns and 'macd' in df.columns)
    sigs = detect_latest_signals(_synth_df())
    check("signals: detect_latest 리스트반환", isinstance(sigs, list))
    check("signals: 라벨형식 *_BUY/SELL", all('_' in s for s in sigs) if sigs else True)


# ── 2. base.entry_engine — 게이트 불변식 ─────────────────────────
def test_entry_engine():
    import base.entry_engine as ee
    # 신호 없는 df → decision False
    r = ee.evaluate('KR', _synth_df(), 'BULL_MID')
    check("entry: 필수키 존재", all(k in r for k in
          ('decision', 'score', 'win_rate', 'win20', 'reason')))
    # 불변식: win10/win20 둘 다 통과해야만 decision True (fc 있을 때)
    # 합성 fc로 직접 게이트 로직 검증
    # entry_engine은 import시 get_forecast/detect_latest_signals를 자기 네임스페이스에 바인딩하므로
    # 반드시 ee.* 를 직접 패치해야 효과가 있다(중요: 모듈원본 패치는 무효).
    import base.entry_engine as ee2
    orig_f, orig_d = ee2.get_forecast, ee2.detect_latest_signals
    try:
        ee2.detect_latest_signals = lambda df: ['RSI_BUY']
        ee2.get_forecast = lambda *a, **k: {'n': 999, 'hold_days': 10, 'target': 30,
                                            'stop': -15, 'win10': 80, 'win20': 60}
        r2 = ee2.evaluate('KR', _synth_df(), 'PANIC', min_win=55, min_win20=45)
        check("entry: win10=80·win20=60 → 통과", r2['decision'] is True)
        ee2.get_forecast = lambda *a, **k: {'n': 999, 'hold_days': 10, 'target': 20,
                                            'stop': -15, 'win10': 80, 'win20': 40}
        r3 = ee2.evaluate('KR', _synth_df(), 'BULL_LATE', min_win=55, min_win20=45)
        check("entry: win20=40<45 → 차단(불변식)", r3['decision'] is False)
        ee2.get_forecast = lambda *a, **k: {'n': 999, 'hold_days': 10, 'target': 30,
                                            'stop': -15, 'win10': 50, 'win20': 60}
        r4 = ee2.evaluate('KR', _synth_df(), 'PANIC', min_win=55, min_win20=45)
        check("entry: win10=50<55 → 차단(불변식)", r4['decision'] is False)
    finally:
        ee2.get_forecast, ee2.detect_latest_signals = orig_f, orig_d


# ── 3. base.signal_forecast — 구조/폴백 ──────────────────────────
def test_forecast():
    from base.signal_forecast import get_forecast, get_phase_avg
    fc = get_forecast('KR', 'BULL_MID', ['RSI_BUY'])
    if fc:
        check("forecast: win20 키 포함", 'win20' in fc)
        check("forecast: 표본 양수", fc['n'] > 0)
    else:
        check("forecast: None 허용(데이터 의존)", True)
    fc_bad = get_forecast('KR', 'PANIC', ['RSI_BUY'], min_n=10**9)
    check("forecast: 표본부족 → None(폴백 트리거)", fc_bad is None)


# ── 4. base.self_improve — 안전범위 불변식 ───────────────────────
def test_self_improve():
    import base.self_improve as si
    res = si.run_all(dry_run=True)
    for mode, recs in res.items():
        for r in recs:
            check(f"self_improve[{mode}/{r['phase']}]: 제안 [35,60] 범위",
                  si.WIN20_FLOOR <= r['proposed'] <= si.WIN20_CEIL)


# ── 5. killswitch 수학 (실제 메서드, full-init 없이) ─────────────
def test_killswitch():
    from KR.bot import KRBotController
    b = object.__new__(KRBotController)
    b.KILLSWITCH_ENABLED = True
    b.KILL_PAUSE_DD = 0.10
    b.KILL_LIQUIDATE_DD = 0.20
    b._equity_peak_date = None
    b._equity_peak_today = 0.0
    b._last_total_equity = 1000.0
    check("killswitch: 고점=현재 → L0", b._killswitch_level() == 0)
    b._last_total_equity = 890.0   # -11%
    check("killswitch: -11% → L1(신규중단)", b._killswitch_level() == 1)
    b._last_total_equity = 790.0   # -21%
    check("killswitch: -21% → L2(전량청산)", b._killswitch_level() == 2)
    b._last_total_equity = 950.0   # 회복(-5%) 고점 유지
    check("killswitch: 고점대비 -5% → L0", b._killswitch_level() == 0)


# ── 6. 집中도 상한 수학 (실제 메서드) ────────────────────────────
def test_concentration():
    from KR.bot import KRBotController
    b = object.__new__(KRBotController)
    b.MAX_STOCK_PCT = 0.30
    b._last_total_equity = 1_000_000.0
    b.live_prices = {'005930': 1000.0}
    b.satellite_positions = {}
    b.core_positions = []
    b.add_log = lambda *a, **k: None
    # 100주×1000원=10만원 = 10% < 30% → 통과
    check("집中도: 10% → 통과", b._concentration_blocked('005930', 100, 'X') is False)
    # 400주=40만원=40% > 30% → 차단
    check("집中도: 40% → 차단(불변식)", b._concentration_blocked('005930', 400, 'X') is True)


def main():
    print("=== 핵심 로직 characterization 테스트 ===")
    for fn in (test_signals, test_entry_engine, test_forecast,
               test_self_improve, test_killswitch, test_concentration):
        print(f"\n[{fn.__name__}]")
        try:
            fn()
        except Exception as e:
            global _failed
            _failed += 1
            print(f"  ❌ 예외: {e}")
    print(f"\n=== 결과: {_passed} 통과 / {_failed} 실패 ===")
    sys.exit(1 if _failed else 0)


if __name__ == '__main__':
    main()
