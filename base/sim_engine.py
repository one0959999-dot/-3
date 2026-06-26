"""price_path 기반 진입×청산 시뮬 — 실현수익(룩어헤드 없음).

backtest_trade_signals.price_path_json = 진입 후 일별 '누적 % 수익'(진입=0 기준).
  예: [-3.16, -7.63, ...] = 1일차 -3.16%, 2일차 -7.63% ...
청산 규칙을 인과적으로(과거 경로만 보고) 적용해 '실제로 팔 수 있는' 수익을 계산한다.
max_gain(고점)으로 채점하던 기존 방식의 룩어헤드 결함을 제거.
"""
import json

# 청산 전략 메뉴 (TODO STEP3): 고정보유 / 트레일링스톱 / 목표+손절
EXIT_STRATEGIES = [
    ('보유5일',   'hold', 5),
    ('보유10일',  'hold', 10),
    ('보유20일',  'hold', 20),
    ('보유60일',  'hold', 60),
    ('트레일-8%', 'trail', 8),
    ('트레일-15%','trail', 15),
    ('목표20손절10', 'tstop', (20, 10)),
    ('목표30손절15', 'tstop', (30, 15)),
]


def simulate_exit(path, kind, p):
    """경로에 청산규칙 적용 → (실현수익%, 보유중MDD%, 보유일).
    path: 누적% 리스트(진입=0). 인과적: i일까지 정보로만 청산 결정."""
    if not path:
        return 0.0, 0.0, 0
    n = len(path)
    if kind == 'hold':
        idx = min(p, n) - 1
        ret = path[idx]
        mdd = min([0.0] + path[:idx + 1])
        return ret, mdd, idx + 1
    if kind == 'trail':
        peak = 0.0
        for i, v in enumerate(path):
            if v > peak:
                peak = v
            if v <= peak - p:                      # 고점대비 p% 하락 → 청산
                mdd = min([0.0] + path[:i + 1])
                return v, mdd, i + 1
        return path[-1], min([0.0] + path), n       # 미발동 → 만기보유
    if kind == 'tstop':
        target, stop = p
        for i, v in enumerate(path):
            if v >= target:
                return target, min([0.0] + path[:i + 1]), i + 1
            if v <= -stop:
                return -stop, min([0.0] + path[:i + 1]), i + 1
        return path[-1], min([0.0] + path), n
    return path[-1], min([0.0] + path), n


def buy_hold_return(path):
    """단순보유(만기까지) 실현수익 = 경로 끝값."""
    return path[-1] if path else 0.0


def simulate_all(path):
    """모든 청산전략 결과 dict + buy_hold. {label: (ret, mdd, days)} + 'buyhold'."""
    out = {}
    for label, kind, p in EXIT_STRATEGIES:
        out[label] = simulate_exit(path, kind, p)
    out['buyhold'] = (buy_hold_return(path), min([0.0] + path) if path else 0.0, len(path))
    return out


def parse_path(s):
    try:
        p = json.loads(s) if s else None
        return p if isinstance(p, list) and p else None
    except Exception:
        return None
