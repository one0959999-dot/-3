"""
strategy.py
코어-위성 전략의 매매 신호 및 포지션 상태를 관리합니다.
- 코어: 보령(003850) 장기 보유 (플로어 물량 제외 익절)
- 위성: 종목별 최적화 지표 기반 (하락장 과매도 맹목적 매수 금지, 반등 확인 매수 적용)
- 재투자: 위성 수익 실현 시 수익금의 50%로 보령 추가 매수
"""

from pykrx import stock
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

CORE_TICKER        = "003850"
CORE_NAME          = "보령"
REINVEST_RATIO     = 1.00   # 위성·단타 수익의 코어 재투자 비율 (1.00 = 수익 전액 코어로 재분배)
CORE_MIN_FLOOR_RATIO = 0.30  # 매도 후에도 초기 보유량의 최소 30%는 항상 유지
RSI_PERIOD         = 9
RSI_OVERSOLD       = 30
RSI_OVERBOUGHT     = 70

# 인버스 ETF (하위 호환 유지)
INVERSE_ETF_TICKER = "114800"   # KODEX 인버스 (KOSPI200 1x)
INVERSE_ETF_NAME   = "KODEX 인버스"
INVERSE_BUDGET_RATIO = 0.15     # 기존 20% → 3종 분산으로 비율 조정

# ── 방어 자산 포트폴리오 (BEAR 국면 자동 편입) ─────────────────────────────
# 하락장에서 서로 역상관(uncorrelated) 또는 안전자산 특성을 가진 3가지 수단으로 분산.
# 총 배분: 인버스 15% + 달러선물 10% + 금선물 5% = 30% 헤지 포지션
DEFENSIVE_ASSETS = [
    {
        "ticker": "114800",
        "name":   "KODEX 인버스",
        "ratio":  0.15,    # 총자산의 15% — KOSPI200 1배 인버스
        "type":   "inverse",
        "emoji":  "📉",
    },
    {
        "ticker": "130730",
        "name":   "KODEX 달러선물",
        "ratio":  0.10,    # 총자산의 10% — 원/달러 상승(원화 약세) 수익
        "type":   "dollar",
        "emoji":  "💵",
    },
    {
        "ticker": "132030",
        "name":   "KODEX 골드선물(H)",
        "ratio":  0.05,    # 총자산의 5% — 금 안전자산 (환헤지)
        "type":   "gold",
        "emoji":  "🥇",
    },
]


def _calc_adx(df, period: int = 14) -> tuple:
    """
    ADX(14) 계산.
    Returns: (adx, plus_di, minus_di)
      adx      : 추세 강도 0~100 (방향 무관)
      plus_di  : 상승 방향 강도
      minus_di : 하락 방향 강도

    해석 기준:
      ADX < 20  → 추세 없음 (NEUTRAL 구간)
      ADX 20~40 → 추세 형성 중
      ADX > 40  → 추세 과열 (막바지 경고)
      ADX > 60  → 극단적 과열 (반전 임박)
    """
    try:
        if df is None or df.empty or len(df) < period * 2 + 1:
            return 0.0, 0.0, 0.0
        if not all(col in df.columns for col in ['high', 'low', 'close']):
            return 0.0, 0.0, 0.0

        high  = df['high'].astype(float)
        low   = df['low'].astype(float)
        close = df['close'].astype(float)

        # True Range
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low  - close.shift(1)).abs(),
        ], axis=1).max(axis=1)

        # Directional Movement: 겹치는 쪽은 0으로 처리
        up_move   = high.diff()
        down_move = (-low.diff())
        plus_dm   = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm  = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

        atr14      = tr.rolling(period).mean()
        plus_di14  = 100 * plus_dm.rolling(period).mean()  / (atr14 + 1e-10)
        minus_di14 = 100 * minus_dm.rolling(period).mean() / (atr14 + 1e-10)

        dx  = 100 * (plus_di14 - minus_di14).abs() / (plus_di14 + minus_di14 + 1e-10)
        adx = dx.rolling(period).mean()

        return (
            round(float(adx.iloc[-1]),       1),
            round(float(plus_di14.iloc[-1]), 1),
            round(float(minus_di14.iloc[-1]),1),
        )
    except Exception:
        return 0.0, 0.0, 0.0


def _get_up_streak(close: pd.Series) -> int:
    """최근 연속 상승일 수 반환 (전일 종가 기준 diff > 0)."""
    try:
        diffs = close.diff().dropna()
        streak = 0
        for d in reversed(diffs.values):
            if d > 0:
                streak += 1
            else:
                break
        return streak
    except Exception:
        return 0


def _calc_regime_score(c: pd.Series) -> tuple:
    """
    7신호 스코어 계산 (내부 공통 헬퍼).
    Returns: (score, rsi, ret22)
    """
    price    = float(c.iloc[-1])
    ma5      = c.rolling(5).mean()
    ma20     = c.rolling(20).mean()
    ma60     = c.rolling(60).mean()
    m5_now   = float(ma5.iloc[-1])
    m5_3ago  = float(ma5.iloc[-4])
    m20_now  = float(ma20.iloc[-1])
    m20_5ago = float(ma20.iloc[-6])
    m60_now  = float(ma60.iloc[-1])
    p22ago   = float(c.iloc[-23]) if len(c) >= 23 else float(c.iloc[0])

    d   = c.diff()
    g   = d.clip(lower=0).rolling(14).mean()
    lo  = (-d.clip(upper=0)).rolling(14).mean()
    rsi = float((100 - 100 / (1 + g / (lo + 1e-10))).iloc[-1])

    score = 0
    score += 1 if price > m5_now    else -1   # S1
    score += 1 if m5_now > m5_3ago  else -1   # S2
    score += 1 if price > m20_now   else -1   # M1
    score += 1 if m20_now > m20_5ago else -1  # M2
    if rsi > 55:   score += 1                  # M3
    elif rsi < 45: score -= 1
    score += 1 if m20_now > m60_now else -1   # L1
    ret22 = (price / p22ago - 1) * 100 if p22ago > 0 else 0.0
    if ret22 > 3.0:    score += 1             # L2
    elif ret22 < -3.0: score -= 1

    return score, round(rsi, 1), round(ret22, 2)


def get_market_regime(kis_api) -> str:
    """
    KOSPI200 ETF(069500) 일봉 기준 시장 국면 판단.
    Returns: 'BULL' | 'BEAR' | 'NEUTRAL'

    판단 기준: 최근 2일~1달(22 거래일) 다중 타임프레임 7개 신호 종합 스코어
    ─────────────────────────────────────────────────────────────────────────
    [단기 2~5일]
      S1. 현재가 vs 5일선  : 위 +1 / 아래 -1
      S2. 5일선 기울기(최근 3일): 상승 +1 / 하락 -1

    [중기 10~20일]
      M1. 현재가 vs 20일선 : 위 +1 / 아래 -1
      M2. 20일선 기울기(최근 5일): 상승 +1 / 하락 -1
      M3. RSI(14)          : >55 → +1 | <45 → -1 | 45~55 → 0

    [장기 ~1달]
      L1. 20일선 vs 60일선 : 골든크로스(위) +1 / 데드크로스(아래) -1
      L2. 최근 22일(≈1달) 수익률 : >+3% → +1 | <-3% → -1 | 그 외 → 0

    [과열 필터 — BULL 강등]
      F1. ADX(14) ≥ 40  → 추세 막바지 → NEUTRAL 강등
      F2. 연속 상승일 ≥ 8 → 단기 과열  → NEUTRAL 강등

    최종 판정: score ≥ +5 → BULL (과열 필터 통과 시) | score ≤ -4 → BEAR | 그 외 → NEUTRAL
    """
    try:
        df = kis_api.get_ohlcv("069500", "D")
        if df is None or df.empty or len(df) < 65:
            return "NEUTRAL"
        c = df['close'].dropna()
        if len(c) < 65:
            return "NEUTRAL"

        score, rsi, _ = _calc_regime_score(c)

        adx, plus_di, minus_di = _calc_adx(df)
        up_streak = _get_up_streak(c)

        if score <= -4:
            # ── 과열 필터: BEAR 점수 충족해도 추세 미확인/패닉 저점이면 NEUTRAL 강등 ──
            if adx < 20:
                return "NEUTRAL"   # 하락 추세 미확인 — 노이즈 가능성, 방어 포지션 자제
            if adx >= 50 and minus_di > 40:
                return "NEUTRAL"   # 패닉 클라이막스 — 낙폭 과대, 반등 임박 가능성
            return "BEAR"

        if score >= 5:
            # ── 과열 필터: BULL 점수 충족해도 진짜 과열이면 NEUTRAL 강등 ──
            # ADX ≥ 40 조건 제거: ADX 40은 "강한 추세"를 의미하지 막바지가 아님 (Wilder 원 기준)
            # 강한 상승장에서 ADX 40~60 유지는 정상 → BULL 유지
            if up_streak >= 8:
                return "NEUTRAL"   # 8일 연속 상승 — 단기 과열
            if rsi >= 80:
                return "NEUTRAL"   # RSI 80 이상 — 극도 과열 (68~70은 정상 상승장)
            return "BULL"

        return "NEUTRAL"
    except Exception:
        return "NEUTRAL"


def get_market_regime_detail(kis_api) -> dict:
    """
    get_market_regime()의 상세 진단 버전.
    국면 판단 근거(점수·ADX·연속일·RSI)를 함께 반환해 로그/알림에 활용.

    Returns: {
        'regime'          : 'BULL' | 'BEAR' | 'NEUTRAL',
        'score'           : int,          # 7신호 합산 (-7 ~ +7)
        'adx'             : float,        # ADX(14)
        'plus_di'         : float,
        'minus_di'        : float,
        'up_streak'       : int,          # 연속 상승일
        'rsi'             : float,        # RSI(14)
        'ret22'           : float,        # 22일 수익률 %
        'downgrade_reason': str,          # BULL→NEUTRAL 강등 이유 (없으면 '')
    }
    """
    result = {
        'regime': 'NEUTRAL', 'score': 0,
        'adx': 0.0, 'plus_di': 0.0, 'minus_di': 0.0,
        'up_streak': 0, 'rsi': 50.0, 'ret22': 0.0,
        'downgrade_reason': '',
    }
    try:
        df = kis_api.get_ohlcv("069500", "D")
        if df is None or df.empty or len(df) < 65:
            return result
        c = df['close'].dropna()
        if len(c) < 65:
            return result

        score, rsi, ret22     = _calc_regime_score(c)
        adx, plus_di, minus_di = _calc_adx(df)
        up_streak              = _get_up_streak(c)

        result.update({
            'score'    : score,
            'adx'      : adx,
            'plus_di'  : plus_di,
            'minus_di' : minus_di,
            'up_streak': up_streak,
            'rsi'      : rsi,
            'ret22'    : ret22,
        })

        if score <= -4:
            if adx < 20:
                result['regime']           = 'NEUTRAL'
                result['downgrade_reason'] = f'BEAR→NEUTRAL 강등: ADX {adx:.1f} < 20 (하락 추세 미확인, 노이즈 가능성)'
            elif adx >= 50 and minus_di > 40:
                result['regime']           = 'NEUTRAL'
                result['downgrade_reason'] = f'BEAR→NEUTRAL 강등: ADX {adx:.1f} ≥ 50 패닉 클라이막스 (낙폭 과대, 반등 임박)'
            else:
                result['regime'] = 'BEAR'
        elif score >= 5:
            # ADX ≥ 40 조건 제거: 강한 추세 = 막바지가 아님. RSI 80+ 또는 8일 연속 상승만 진짜 과열로 판단
            if up_streak >= 8:
                result['regime']           = 'NEUTRAL'
                result['downgrade_reason'] = f'BULL→NEUTRAL 강등: {up_streak}일 연속 상승 (단기 과열, 조정 임박)'
            elif rsi >= 80:
                result['regime']           = 'NEUTRAL'
                result['downgrade_reason'] = f'BULL→NEUTRAL 강등: RSI {rsi:.1f} ≥ 80 (극도 과열)'
            else:
                result['regime'] = 'BULL'
        # else: NEUTRAL (기본값 유지)

    except Exception:
        pass
    return result


def get_bear_bounce_signal(df) -> bool:
    """하위 호환용 — get_bear_bottom_score() 래퍼. 점수 1 이상이면 True."""
    score, _ = get_bear_bottom_score(df)
    return score >= 1


# ─────────────────────────────────────────────────────────────────────────────
# 하락장 저점 포착 — 다중 전략 종합 스코어링 시스템
# ─────────────────────────────────────────────────────────────────────────────

def _get_vol(df) -> pd.Series:
    """volume 컬럼 추출 헬퍼."""
    if 'volume' in df.columns:
        return df['volume'].dropna()
    return pd.Series(dtype=float)


def _calc_rsi14(close: pd.Series) -> float:
    """RSI(14) 최신값 반환."""
    d  = close.diff()
    g  = d.clip(lower=0).rolling(14).mean()
    lo = (-d.clip(upper=0)).rolling(14).mean()
    return float((100 - 100 / (1 + g / (lo + 1e-10))).iloc[-1])


def _signal_panic_climax(df) -> tuple:
    """
    [전략 1] 패닉 클라이막스 반등
    - RSI(14) < 25 + 볼린저 하단 이탈 + 거래량 2배 이상
    - 전통적인 '최후의 투매 후 반등' 패턴
    """
    try:
        if df is None or df.empty or len(df) < 22:
            return False, ""
        c = df['close'].dropna()
        v = _get_vol(df)
        rsi = _calc_rsi14(c)
        if rsi >= 25:
            return False, ""
        mid   = c.rolling(20).mean()
        lower = mid - 2 * c.rolling(20).std()
        if float(c.iloc[-1]) > float(lower.iloc[-1]):
            return False, ""
        if len(v) >= 20 and float(v.iloc[-20:-1].mean()) > 0:
            if float(v.iloc[-1]) < float(v.iloc[-20:-1].mean()) * 2:
                return False, ""
        return True, f"패닉클라이막스(RSI{rsi:.0f}+볼하단+거래량급증)"
    except Exception:
        return False, ""


def _signal_hammer_candle(df) -> tuple:
    """
    [전략 2] 망치형·역망치형 캔들 패턴
    - 망치형: 아래꼬리가 몸통의 2배 이상, 위꼬리 거의 없음 → 매도세 소진
    - 역망치형: 위꼬리가 몸통의 2배 이상, 아래꼬리 거의 없음 → 매수 탐색 시작
    """
    try:
        if df is None or df.empty or len(df) < 5:
            return False, ""
        o = float(df['open'].iloc[-1])
        h = float(df['high'].iloc[-1])
        l = float(df['low'].iloc[-1])
        c = float(df['close'].iloc[-1])
        body = abs(c - o)
        if body < 1e-9:
            return False, ""
        lower_wick = min(o, c) - l
        upper_wick = h - max(o, c)
        # 망치형
        if lower_wick >= body * 2 and upper_wick <= body * 0.5:
            return True, "망치형캔들(아래꼬리≥2×몸통)"
        # 역망치형
        if upper_wick >= body * 2 and lower_wick <= body * 0.5:
            return True, "역망치형캔들(위꼬리≥2×몸통)"
        return False, ""
    except Exception:
        return False, ""


def _signal_bullish_engulfing(df) -> tuple:
    """
    [전략 3] 상승 장악형 (Bullish Engulfing)
    - 전일 음봉을 오늘 양봉이 완전히 감싸는 패턴
    - 매도 세력이 매수 세력에 완전히 흡수되었음을 의미
    """
    try:
        if df is None or df.empty or len(df) < 3:
            return False, ""
        prev_o = float(df['open'].iloc[-2])
        prev_c = float(df['close'].iloc[-2])
        curr_o = float(df['open'].iloc[-1])
        curr_c = float(df['close'].iloc[-1])
        prev_bearish = prev_c < prev_o
        curr_bullish = curr_c > curr_o
        engulfs = curr_o <= prev_c and curr_c >= prev_o
        if prev_bearish and curr_bullish and engulfs:
            return True, "상승장악형캔들(전일음봉완전포위)"
        return False, ""
    except Exception:
        return False, ""


def _signal_morning_star(df) -> tuple:
    """
    [전략 4] 모닝스타 (Morning Star) — 3캔들 반전 패턴
    - 1일차: 큰 음봉 (하락 추세 확인)
    - 2일차: 갭하락 후 작은 몸통 (도지/팽이) — 매도 소진
    - 3일차: 큰 양봉이 1일차 몸통 중간 이상 회복
    """
    try:
        if df is None or df.empty or len(df) < 4:
            return False, ""
        o1, c1 = float(df['open'].iloc[-3]), float(df['close'].iloc[-3])
        o2, c2 = float(df['open'].iloc[-2]), float(df['close'].iloc[-2])
        o3, c3 = float(df['open'].iloc[-1]), float(df['close'].iloc[-1])
        # [W-10] 비율 계산: (o1-c1)/o1 > 1% — 절대값 비교 시 고가종목 편향 발생
        first_bearish  = c1 < o1 and (o1 - c1) / o1 > 0.01
        second_small   = abs(c2 - o2) < abs(o1 - c1) * 0.5
        third_bullish  = c3 > o3 and c3 > (o1 + c1) / 2
        if first_bearish and second_small and third_bullish:
            return True, "모닝스타(3캔들반전패턴)"
        return False, ""
    except Exception:
        return False, ""


def _signal_rsi_divergence(df) -> tuple:
    """
    [전략 5] RSI 강세 다이버전스 (Bullish Divergence)
    - 가격: 최근 저점 < 이전 저점 (신저가 경신)
    - RSI:  최근 저점 > 이전 저점 (RSI는 덜 떨어짐)
    → 하락 모멘텀이 약해지고 있다는 신호
    """
    try:
        if df is None or df.empty or len(df) < 30:
            return False, ""
        c = df['close'].dropna()
        d  = c.diff()
        g  = d.clip(lower=0).rolling(14).mean()
        lo = (-d.clip(upper=0)).rolling(14).mean()
        rsi_series = 100 - 100 / (1 + g / (lo + 1e-10))

        # 최근 10~20봉 구간에서 가격 저점 vs RSI 저점 비교
        window = min(20, len(c) - 5)
        price_recent_low = float(c.iloc[-window:].min())
        price_prev_low   = float(c.iloc[-window*2:-window].min())
        rsi_recent_low   = float(rsi_series.iloc[-window:].min())
        rsi_prev_low     = float(rsi_series.iloc[-window*2:-window].min())

        # 가격은 신저점, RSI는 전 저점보다 높음
        if price_recent_low < price_prev_low and rsi_recent_low > rsi_prev_low + 3:
            return True, f"RSI강세다이버전스(가격신저점RSI반등{rsi_recent_low:.0f})"
        return False, ""
    except Exception:
        return False, ""


def _signal_ma_oversold_gap(df) -> tuple:
    """
    [전략 6] 이동평균 과이격 반등 (Mean Reversion)
    - 현재가가 20일선 대비 -15% 이상 이탈 → 평균 회귀 반등 기대
    - 단, RSI 35 미만이어야 함 (단순 하락이 아닌 과매도 확인)
    """
    try:
        if df is None or df.empty or len(df) < 22:
            return False, ""
        c = df['close'].dropna()
        rsi = _calc_rsi14(c)
        if rsi >= 35:
            return False, ""
        ma20 = float(c.rolling(20).mean().iloc[-1])
        price = float(c.iloc[-1])
        gap_pct = (price / ma20 - 1) * 100
        if gap_pct <= -15:
            return True, f"20일선과이격반등({gap_pct:.1f}%,RSI{rsi:.0f})"
        return False, ""
    except Exception:
        return False, ""


def _signal_support_rebound(df) -> tuple:
    """
    [전략 7] 이전 저점 지지 반등
    - 최근 60일 최저가 근처(±2%)에서 현재가 위치
    - 당일 종가 > 당일 저가의 1.5% 이상 (저점 대비 반등 확인)
    """
    try:
        if df is None or df.empty or len(df) < 20:
            return False, ""
        c = df['close'].dropna()
        l = df['low'].dropna() if 'low' in df.columns else c
        price = float(c.iloc[-1])
        low_today = float(l.iloc[-1])
        support = float(c.iloc[-60:].min()) if len(c) >= 60 else float(c.min())
        near_support = abs(price / support - 1) <= 0.02
        bounced_off  = price >= low_today * 1.015
        if near_support and bounced_off:
            return True, f"이전저점지지반등(지지{support:,.0f})"
        return False, ""
    except Exception:
        return False, ""


def _signal_consecutive_drop_reversal(df) -> tuple:
    """
    [전략 8] 연속 하락 후 첫 양봉
    - 3일 이상 연속 음봉 후 오늘 양봉 + RSI 40 미만
    - 단기 과매도 소진 후 매수세 유입 첫 신호
    """
    try:
        if df is None or df.empty or len(df) < 20:  # RSI(14) 최소 데이터 보장
            return False, ""
        # open/close를 df에서 직접 사용 (동일 인덱스 보장)
        o = df['open'].values
        c = df['close'].values
        # 직전 3일 연속 음봉 확인 (음봉 = 종가 < 시가)
        consecutive_red = all(c[i] < o[i] for i in range(-4, -1))
        today_green = c[-1] > o[-1]
        if not (consecutive_red and today_green):
            return False, ""
        close_series = df['close'].dropna()
        if len(close_series) < 16:
            return False, ""
        rsi = _calc_rsi14(close_series)
        if pd.isna(rsi) or rsi >= 40:
            return False, ""
        return True, f"연속하락후첫양봉(RSI{rsi:.0f})"
    except Exception:
        return False, ""


def _signal_volume_accumulation(df) -> tuple:
    """
    [전략 9] 저점 거래량 축적 (Accumulation)
    - 가격은 하락하는데 OBV(On-Balance Volume)가 상승 → 세력 축적
    - 최근 5일 OBV 추세가 양수이고, 가격은 전주 대비 하락 중
    """
    try:
        if df is None or df.empty or len(df) < 10:
            return False, ""
        c = df['close'].dropna()
        v = _get_vol(df)
        if len(v) < 10:
            return False, ""
        # OBV 계산
        obv = (np.sign(c.diff()) * v).cumsum()
        obv_slope = float(obv.iloc[-1]) - float(obv.iloc[-5])
        price_slope = float(c.iloc[-1]) - float(c.iloc[-5])
        # 가격↓ + OBV↑ = 세력 축적
        if price_slope < 0 and obv_slope > 0:
            return True, f"OBV축적(가격↓{price_slope:+.0f}원,수급↑)"
        return False, ""
    except Exception:
        return False, ""


def _signal_stochastic_golden(df) -> tuple:
    """
    [전략 10] 스토캐스틱 과매도 골든크로스
    - 스토캐스틱 K(5,3,3)이 20 아래에서 D선을 상향 돌파
    - RSI보다 단기 반응이 빠른 지표 — 저점에서 선행 신호
    """
    try:
        if df is None or df.empty or len(df) < 20:
            return False, ""
        c = df['close'].dropna()
        h = df['high'].dropna() if 'high' in df.columns else c
        l = df['low'].dropna()  if 'low'  in df.columns else c
        period_k = 5
        lowest_l  = l.rolling(period_k).min()
        highest_h = h.rolling(period_k).max()
        k = 100 * (c - lowest_l) / (highest_h - lowest_l + 1e-9)
        d = k.rolling(3).mean()
        # K가 20 아래에서 D를 상향 돌파
        prev_below = float(k.iloc[-2]) < float(d.iloc[-2]) and float(k.iloc[-2]) < 20
        curr_above = float(k.iloc[-1]) > float(d.iloc[-1])
        if prev_below and curr_above:
            return True, f"스토캐스틱골든크로스(K{k.iloc[-1]:.0f}<20)"
        return False, ""
    except Exception:
        return False, ""


def get_bear_bottom_score(df) -> tuple:
    """
    하락장 저점 포착 종합 스코어링.
    10개 전략을 독립 실행 후 감지된 신호 수(score)와 사유 목록을 반환.

    Returns:
        (score: int, reasons: list[str])
        score 0 = 신호 없음 (매수 차단)
        score 1 = 약한 신호 (20% 소액)
        score 2 = 중간 신호 (30% 진입)
        score 3+= 강한 신호 (40% 진입)
    """
    checkers = [
        _signal_panic_climax,
        _signal_hammer_candle,
        _signal_bullish_engulfing,
        _signal_morning_star,
        _signal_rsi_divergence,
        _signal_ma_oversold_gap,
        _signal_support_rebound,
        _signal_consecutive_drop_reversal,
        _signal_volume_accumulation,
        _signal_stochastic_golden,
    ]
    score = 0
    reasons = []
    for fn in checkers:
        try:
            hit, reason = fn(df)
            if hit:
                score += 1
                reasons.append(reason)
        except Exception:
            pass
    return score, reasons


# ─────────────────────────────────────────────────────────────────────────────
# 상승장 모멘텀 포착 — 10개 전략
# ─────────────────────────────────────────────────────────────────────────────

def _bull_ma_golden_cross(df) -> tuple:
    """
    [상승 1] 20일선이 60일선을 상향 돌파 (골든크로스)
    - 중기 추세 전환의 가장 고전적인 신호
    - 직전 봉: 20일선 < 60일선 / 현재 봉: 20일선 > 60일선
    """
    try:
        if df is None or df.empty or len(df) < 62:
            return False, ""
        c = df['close'].dropna()
        ma20 = c.rolling(20).mean()
        ma60 = c.rolling(60).mean()
        if ma20.iloc[-2] < ma60.iloc[-2] and ma20.iloc[-1] > ma60.iloc[-1]:
            return True, f"20/60일골든크로스(MA20:{ma20.iloc[-1]:,.0f})"
        return False, ""
    except Exception:
        return False, ""


def _bull_rsi_oversold_exit(df) -> tuple:
    """
    [상승 2] RSI(14) 40 이하에서 50 이상으로 상향 돌파
    - 상승 추세 중 눌림목 조정이 끝나고 재가속하는 타이밍
    - (BEAR 신호의 RSI < 25와 달리, 상승장에서는 40~50 구간이 매수 기회)
    """
    try:
        if df is None or df.empty or len(df) < 17:
            return False, ""
        c = df['close'].dropna()
        d  = c.diff()
        g  = d.clip(lower=0).rolling(14).mean()
        lo = (-d.clip(upper=0)).rolling(14).mean()
        rsi = 100 - 100 / (1 + g / (lo + 1e-10))
        if rsi.iloc[-2] < 45 and rsi.iloc[-1] >= 50:
            return True, f"RSI눌림목회복({rsi.iloc[-2]:.0f}→{rsi.iloc[-1]:.0f})"
        return False, ""
    except Exception:
        return False, ""


def _bull_bb_midline_cross(df) -> tuple:
    """
    [상승 3] 볼린저밴드 중심선(20일 SMA) 상향 돌파
    - 조정 후 중심선을 다시 회복 = 상승 추세 복귀 신호
    """
    try:
        if df is None or df.empty or len(df) < 22:
            return False, ""
        c = df['close'].dropna()
        mid = c.rolling(20).mean()
        prev_below = float(c.iloc[-2]) < float(mid.iloc[-2])
        curr_above = float(c.iloc[-1]) >= float(mid.iloc[-1])
        if prev_below and curr_above:
            return True, f"볼린저중심선돌파({mid.iloc[-1]:,.0f})"
        return False, ""
    except Exception:
        return False, ""


def _bull_volume_surge_green(df) -> tuple:
    """
    [상승 4] 거래량 폭발 + 양봉
    - 오늘 거래량이 20일 평균의 1.5배 이상이고 양봉 마감
    - 세력이 적극 참여 중임을 의미
    """
    try:
        if df is None or df.empty or len(df) < 22:
            return False, ""
        c = df['close'].dropna()
        o = df['open'].dropna()
        v = _get_vol(df)
        if len(v) < 22:
            return False, ""
        is_green = float(c.iloc[-1]) > float(o.iloc[-1])
        vol_avg = float(v.iloc[-21:-1].mean())
        vol_ratio = float(v.iloc[-1]) / (vol_avg + 1)
        if is_green and vol_ratio >= 1.5:
            return True, f"거래량폭발양봉({vol_ratio:.1f}배)"
        return False, ""
    except Exception:
        return False, ""


def _bull_macd_golden_zero(df) -> tuple:
    """
    [상승 5] MACD 골든크로스 (0선 위에서 발생)
    - 0선 아래 골든크로스보다 신뢰도 높음 — 추세 지속 확인
    """
    try:
        if df is None or df.empty or len(df) < 30:
            return False, ""
        c = df['close'].dropna()
        ema12 = c.ewm(span=12, adjust=False).mean()
        ema26 = c.ewm(span=26, adjust=False).mean()
        macd  = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        cross_up  = macd.iloc[-2] < signal.iloc[-2] and macd.iloc[-1] > signal.iloc[-1]
        above_zero = float(macd.iloc[-1]) > 0
        if cross_up and above_zero:
            return True, f"MACD골든크로스(0선위,히스토:{macd.iloc[-1]-signal.iloc[-1]:+.2f})"
        return False, ""
    except Exception:
        return False, ""


def _bull_new_high_breakout(df) -> tuple:
    """
    [상승 6] 52주 신고가 돌파
    - 저항선이 없는 구간 — 모멘텀 가속 가능성 높음
    - 거래량 동반 필수
    """
    try:
        if df is None or df.empty or len(df) < 60:
            return False, ""
        c  = df['close'].dropna()
        v  = _get_vol(df)
        high_252 = float(c.iloc[-252:].max()) if len(c) >= 252 else float(c.max())
        prev_high = float(c.iloc[-2])
        curr      = float(c.iloc[-1])
        vol_avg   = float(v.iloc[-21:-1].mean()) if len(v) >= 22 else 1
        vol_ok    = float(v.iloc[-1]) >= vol_avg * 1.2 if vol_avg > 0 else True
        if curr > high_252 and prev_high <= high_252 and vol_ok:
            return True, f"52주신고가돌파({curr:,.0f})"
        return False, ""
    except Exception:
        return False, ""


def _bull_pullback_ma20(df) -> tuple:
    """
    [상승 7] 상승 추세 중 20일선 눌림목 반등
    - 현재가가 20일선에 근접(±2%)했다가 반등 마감
    - 상승 추세 유지 확인(120일선 위)이 전제
    """
    try:
        if df is None or df.empty or len(df) < 62:
            return False, ""
        c = df['close'].dropna()
        ma20  = c.rolling(20).mean()
        ma120 = c.rolling(120, min_periods=60).mean()
        price  = float(c.iloc[-1])
        low_td = float(df['low'].iloc[-1]) if 'low' in df.columns else price
        above_120 = price > float(ma120.iloc[-1])
        touched_20 = low_td <= float(ma20.iloc[-1]) * 1.01
        bounced    = price   >= float(ma20.iloc[-1]) * 0.99
        if above_120 and touched_20 and bounced:
            return True, f"20일선눌림목반등(MA20:{ma20.iloc[-1]:,.0f})"
        return False, ""
    except Exception:
        return False, ""


def _bull_ema_short_cross(df) -> tuple:
    """
    [상승 8] EMA 5일이 EMA 20일을 상향 돌파 (단기 골든크로스)
    - 빠른 반응 — 추세 초입 포착에 유리
    """
    try:
        if df is None or df.empty or len(df) < 22:
            return False, ""
        c    = df['close'].dropna()
        e5   = c.ewm(span=5,  adjust=False).mean()
        e20  = c.ewm(span=20, adjust=False).mean()
        if e5.iloc[-2] < e20.iloc[-2] and e5.iloc[-1] > e20.iloc[-1]:
            return True, f"EMA5/20골든크로스({e5.iloc[-1]:,.0f})"
        return False, ""
    except Exception:
        return False, ""


def _bull_consecutive_green(df) -> tuple:
    """
    [상승 9] 3일 연속 양봉 + 거래량 증가 추세
    - 단순하지만 강력한 모멘텀 연속성 확인
    """
    try:
        if df is None or df.empty or len(df) < 6:
            return False, ""
        o = df['open']
        c = df['close']
        v = _get_vol(df)
        three_green = all(float(c.iloc[i]) > float(o.iloc[i]) for i in range(-3, 0))
        if not three_green:
            return False, ""
        vol_increasing = len(v) >= 3 and float(v.iloc[-1]) > float(v.iloc[-3])
        if vol_increasing:
            return True, "3연속양봉+거래량증가"
        return False, ""
    except Exception:
        return False, ""


def _bull_obv_new_high(df) -> tuple:
    """
    [상승 10] OBV 신고가 선행 돌파
    - 가격 신고가보다 OBV가 먼저 신고가 — 수급이 가격을 선도
    - 가장 신뢰할 수 있는 세력 참여 증거
    """
    try:
        if df is None or df.empty or len(df) < 20:
            return False, ""
        c = df['close'].dropna()
        v = _get_vol(df)
        if len(v) < 20:
            return False, ""
        obv = (np.sign(c.diff()) * v).cumsum()
        obv_curr_high = float(obv.iloc[-1]) >= float(obv.iloc[-20:].max())
        price_not_yet = float(c.iloc[-1]) < float(c.iloc[-20:].max()) * 0.98
        if obv_curr_high and price_not_yet:
            return True, "OBV선행신고가(수급이가격선도)"
        return False, ""
    except Exception:
        return False, ""


def get_bull_momentum_score(df) -> tuple:
    """
    상승장 모멘텀 포착 종합 스코어링.
    Returns: (score: int, reasons: list[str])
      score 0   = 신호 없음 → 60% 기본 진입 (상승장이므로 차단 안 함)
      score 1~2 = 보통 신호 → 70% 진입
      score 3+  = 강한 신호 → 80% 풀 베팅
    """
    checkers = [
        _bull_ma_golden_cross,
        _bull_rsi_oversold_exit,
        _bull_bb_midline_cross,
        _bull_volume_surge_green,
        _bull_macd_golden_zero,
        _bull_new_high_breakout,
        _bull_pullback_ma20,
        _bull_ema_short_cross,
        _bull_consecutive_green,
        _bull_obv_new_high,
    ]
    score, reasons = 0, []
    for fn in checkers:
        try:
            hit, reason = fn(df)
            if hit:
                score += 1
                reasons.append(reason)
        except Exception:
            pass
    return score, reasons


# ─────────────────────────────────────────────────────────────────────────────
# 횡보장 레인지 트레이딩 — 10개 전략
# ─────────────────────────────────────────────────────────────────────────────

def _neutral_bb_lower_bounce(df) -> tuple:
    """
    [횡보 1] 볼린저밴드 하단 반등
    - 밴드 하단 터치(±1%) 후 종가가 하단 위로 마감
    - 레인지 하단 = 매수 구간의 핵심
    """
    try:
        if df is None or df.empty or len(df) < 22:
            return False, ""
        c = df['close'].dropna()
        mid   = c.rolling(20).mean()
        lower = mid - 2 * c.rolling(20).std()
        low_td = float(df['low'].iloc[-1]) if 'low' in df.columns else float(c.iloc[-1])
        touched = low_td <= float(lower.iloc[-1]) * 1.01
        closed_above = float(c.iloc[-1]) > float(lower.iloc[-1])
        if touched and closed_above:
            return True, f"볼린저하단반등(하단:{lower.iloc[-1]:,.0f})"
        return False, ""
    except Exception:
        return False, ""


def _neutral_rsi_range_buy(df) -> tuple:
    """
    [횡보 2] RSI(14) 35~45 구간에서 상향 반전
    - 횡보장 과매도 구간 — 지나치게 낮지 않고 회복 모멘텀 확인
    """
    try:
        if df is None or df.empty or len(df) < 17:
            return False, ""
        c = df['close'].dropna()
        rsi = _calc_rsi14(c)
        d   = c.diff()
        g   = d.clip(lower=0).rolling(14).mean()
        lo  = (-d.clip(upper=0)).rolling(14).mean()
        rsi_s = 100 - 100 / (1 + g / (lo + 1e-10))
        prev, curr = float(rsi_s.iloc[-2]), float(rsi_s.iloc[-1])
        if 30 <= prev <= 45 and curr > prev + 2:
            return True, f"RSI횡보과매도반등({prev:.0f}→{curr:.0f})"
        return False, ""
    except Exception:
        return False, ""


def _neutral_macd_hist_turn(df) -> tuple:
    """
    [횡보 3] MACD 히스토그램 부(-)에서 정(+)으로 전환
    - 0선 근처에서의 방향 전환 — 횡보 탈출 초기 모멘텀
    """
    try:
        if df is None or df.empty or len(df) < 30:
            return False, ""
        c = df['close'].dropna()
        macd   = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
        signal = macd.ewm(span=9, adjust=False).mean()
        hist   = macd - signal
        if float(hist.iloc[-2]) < 0 and float(hist.iloc[-1]) >= 0:
            return True, f"MACD히스토그램부→정전환({hist.iloc[-1]:+.2f})"
        return False, ""
    except Exception:
        return False, ""


def _neutral_narrow_band_break(df) -> tuple:
    """
    [횡보 4] 볼린저밴드 수렴 후 상향 이탈 (스퀴즈 돌파)
    - 밴드폭이 최근 20일 중 최소 수준에서 상향 돌파
    - 에너지가 응축되다 위로 터지는 패턴
    """
    try:
        if df is None or df.empty or len(df) < 30:
            return False, ""
        c = df['close'].dropna()
        mid   = c.rolling(20).mean()
        std   = c.rolling(20).std()
        upper = mid + 2 * std
        lower = mid - 2 * std
        bw    = (upper - lower) / (mid + 1e-9)  # 밴드폭 비율
        bw_min_20 = float(bw.iloc[-20:].min())
        curr_bw   = float(bw.iloc[-1])
        price_break = float(c.iloc[-1]) > float(upper.iloc[-1]) * 0.99
        if curr_bw <= bw_min_20 * 1.05 and price_break:
            return True, f"볼린저스퀴즈상향이탈(밴드폭{curr_bw:.3f})"
        return False, ""
    except Exception:
        return False, ""


def _neutral_double_bottom(df) -> tuple:
    """
    [횡보 5] 이중 바닥 (W자 패턴)
    - 최근 20~40봉에서 두 개의 유사한 저점 형성 후 현재가 반등
    - 박스권 하단을 두 번 지지한 강력한 반전 신호
    """
    try:
        if df is None or df.empty or len(df) < 40:
            return False, ""
        c = df['close'].dropna()
        low_series = df['low'].dropna() if 'low' in df.columns else c
        window = min(40, len(c) - 2)
        seg1 = low_series.iloc[-window:-window//2]
        seg2 = low_series.iloc[-window//2:-1]
        low1 = float(seg1.min())
        low2 = float(seg2.min())
        similar = abs(low1 - low2) / (max(low1, low2) + 1e-9) <= 0.03
        rebounding = float(c.iloc[-1]) > low2 * 1.02
        if similar and rebounding:
            return True, f"이중바닥W패턴(저점1:{low1:,.0f}≈저점2:{low2:,.0f})"
        return False, ""
    except Exception:
        return False, ""


def _neutral_vwap_support(df) -> tuple:
    """
    [횡보 6] VWAP(거래량 가중 평균가) 지지 반등
    - 현재가가 VWAP 아래로 내려갔다가 회복 마감
    - 기관/세력의 평균 매수 단가 = 강력한 지지선
    """
    try:
        if df is None or df.empty or len(df) < 5:
            return False, ""
        c = df['close'].dropna()
        v = _get_vol(df)
        if len(v) < 5:
            return False, ""
        vwap = (c * v).rolling(20, min_periods=5).sum() / (v.rolling(20, min_periods=5).sum() + 1e-9)
        low_td = float(df['low'].iloc[-1]) if 'low' in df.columns else float(c.iloc[-1])
        touched_below = low_td < float(vwap.iloc[-1])
        closed_above  = float(c.iloc[-1]) >= float(vwap.iloc[-1])
        if touched_below and closed_above:
            return True, f"VWAP지지반등(VWAP:{vwap.iloc[-1]:,.0f})"
        return False, ""
    except Exception:
        return False, ""


def _neutral_williams_r(df) -> tuple:
    """
    [횡보 7] Williams %R 과매도(-80 이하) 반등
    - RSI보다 단기 반응 빠름 — 횡보 구간 저점 포착에 유리
    """
    try:
        if df is None or df.empty or len(df) < 15:
            return False, ""
        c = df['close'].dropna()
        h = df['high'].dropna() if 'high' in df.columns else c
        l = df['low'].dropna()  if 'low'  in df.columns else c
        highest_h = h.rolling(14).max()
        lowest_l  = l.rolling(14).min()
        wr = -100 * (highest_h - c) / (highest_h - lowest_l + 1e-9)
        if float(wr.iloc[-2]) < -80 and float(wr.iloc[-1]) > -80:
            return True, f"Williams%R과매도탈출({wr.iloc[-1]:.0f})"
        return False, ""
    except Exception:
        return False, ""


def _neutral_box_range_support(df) -> tuple:
    """
    [횡보 8] 박스권 하단 지지 확인
    - 최근 20일 고가/저가로 박스권 정의 후 하단 ±2% 근처에서 반등
    """
    try:
        if df is None or df.empty or len(df) < 22:
            return False, ""
        c = df['close'].dropna()
        box_high = float(c.iloc[-20:].max())
        box_low  = float(c.iloc[-20:].min())
        box_range = box_high - box_low
        if box_range / (box_low + 1e-9) < 0.03:
            return False, ""
        price = float(c.iloc[-1])
        near_bottom = price <= box_low + box_range * 0.15
        not_breakdown = price >= box_low * 0.98
        if near_bottom and not_breakdown:
            return True, f"박스권하단지지(박스:{box_low:,.0f}~{box_high:,.0f})"
        return False, ""
    except Exception:
        return False, ""


def _neutral_adx_low_reversal(df) -> tuple:
    """
    [횡보 9] ADX 낮음(< 20) + RSI 반등 — 추세 없는 구간 역추세 매매
    - ADX < 20 = 추세 없는 횡보 확인
    - 이 구간에서는 추세 추종이 아닌 평균 회귀가 유효
    """
    try:
        if df is None or df.empty or len(df) < 30:
            return False, ""
        # dropna 대신 df 원본 사용 → 인덱스 정렬 보장
        c = df['close']
        h = df['high']  if 'high' in df.columns else df['close']
        l = df['low']   if 'low'  in df.columns else df['close']
        # ADX 계산 (같은 DataFrame 인덱스 기준)
        plus_dm  = h.diff().clip(lower=0)
        minus_dm = (-l.diff()).clip(lower=0)
        tr = pd.concat([h - l,
                        (h - c.shift(1)).abs(),
                        (l - c.shift(1)).abs()], axis=1).max(axis=1)
        atr14 = tr.rolling(14, min_periods=14).mean()
        pdi = 100 * plus_dm.rolling(14, min_periods=14).mean()  / (atr14 + 1e-9)
        mdi = 100 * minus_dm.rolling(14, min_periods=14).mean() / (atr14 + 1e-9)
        dx  = 100 * (pdi - mdi).abs() / (pdi + mdi + 1e-9)
        adx = dx.rolling(14, min_periods=14).mean()
        adx_val = float(adx.iloc[-1])
        if pd.isna(adx_val) or adx_val >= 20:
            return False, ""
        rsi = _calc_rsi14(c.dropna())
        if pd.isna(rsi) or rsi >= 40:
            return False, ""
        return True, f"ADX횡보구간역추세(ADX{adx_val:.0f},RSI{rsi:.0f})"
    except Exception:
        return False, ""


def _neutral_pivot_support(df) -> tuple:
    """
    [횡보 10] 피벗 포인트 지지 반등
    - 전일 고/저/종가로 당일 피벗 계산, 현재가가 S1(지지1) 근처에서 반등
    - 단기 트레이더들이 공통으로 보는 지지/저항선
    """
    try:
        if df is None or df.empty or len(df) < 3:
            return False, ""
        prev_h = float(df['high'].iloc[-2])  if 'high'  in df.columns else float(df['close'].iloc[-2])
        prev_l = float(df['low'].iloc[-2])   if 'low'   in df.columns else float(df['close'].iloc[-2])
        prev_c = float(df['close'].iloc[-2])
        pivot  = (prev_h + prev_l + prev_c) / 3
        s1     = 2 * pivot - prev_h
        price  = float(df['close'].iloc[-1])
        low_td = float(df['low'].iloc[-1]) if 'low' in df.columns else price
        near_s1   = low_td <= s1 * 1.01
        closed_above = price >= s1
        if near_s1 and closed_above:
            return True, f"피벗S1지지반등(S1:{s1:,.0f})"
        return False, ""
    except Exception:
        return False, ""


def get_neutral_range_score(df) -> tuple:
    """
    횡보장 레인지 트레이딩 종합 스코어링.
    Returns: (score: int, reasons: list[str])
      score 0   = 신호 없음 → 매수 차단 (횡보장은 근거 없이 진입 금지)
      score 1   = 약한 신호 → 30% 소액
      score 2   = 중간 신호 → 45% 진입
      score 3+  = 강한 신호 → 55% 진입
    """
    checkers = [
        _neutral_bb_lower_bounce,
        _neutral_rsi_range_buy,
        _neutral_macd_hist_turn,
        _neutral_narrow_band_break,
        _neutral_double_bottom,
        _neutral_vwap_support,
        _neutral_williams_r,
        _neutral_box_range_support,
        _neutral_adx_low_reversal,
        _neutral_pivot_support,
    ]
    score, reasons = 0, []
    for fn in checkers:
        try:
            hit, reason = fn(df)
            if hit:
                score += 1
                reasons.append(reason)
        except Exception:
            pass
    return score, reasons


def calc_rsi(series, period=RSI_PERIOD):
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    return 100 - 100 / (1 + gain / (loss + 1e-10))


def get_recent_prices(ticker, kis_api=None, days=30):
    """최근 N일 종가 Series 반환 (KIS API 사용)"""
    if kis_api is None:
        import pandas as pd
        return pd.Series(dtype=float)
        
    df = kis_api.get_ohlcv(ticker, "D")
    if df is None or df.empty or 'close' not in df.columns:
        import pandas as pd
        return pd.Series(dtype=float)

    return df['close'].dropna().tail(days)


def get_rsi_signal(ticker, kis_api=None, df=None):
    """
    RSI(9) 기반 현재 매매 신호 반환 (떨어지는 칼날 방지 및 캐시 데이터프레임 우선 연동)
    Returns: ('BUY' | 'SELL' | 'HOLD', current_price, rsi_value)
    """
    if df is not None and not df.empty:
        prices = df['close'].dropna().tail(30)
    else:
        prices = get_recent_prices(ticker, kis_api, days=30)
        
    if len(prices) < RSI_PERIOD + 2:
        return 'HOLD', 0, 0

    rsi_series  = calc_rsi(prices)
    current_rsi = rsi_series.iloc[-1]
    prev_rsi    = rsi_series.iloc[-2]
    price       = int(prices.iloc[-1])

    # 🟢 무릎 매수: 이전엔 30 밑이었는데, 지금 30을 위로 돌파(골든크로스)할 때만 매수
    if prev_rsi < RSI_OVERSOLD and current_rsi >= RSI_OVERSOLD:
        return 'BUY', price, current_rsi
    # 🔴 어깨 매도: 이전엔 70 위였는데, 지금 70을 아래로 깰(데드크로스) 때만 매도
    elif prev_rsi > RSI_OVERBOUGHT and current_rsi <= RSI_OVERBOUGHT:
        return 'SELL', price, current_rsi

    return 'HOLD', price, current_rsi


def get_signal_by_strategy(ticker, strategy_name, kis_api=None, df=None):
    """
    전략 이름에 따라 실시간 매매 신호 생성 (로컬 패치 캐시 및 KIS 하이브리드 지원)
    Returns: ('BUY' | 'SELL' | 'HOLD', price, indicator_value)
    """
    if kis_api is None and df is None:
        return 'HOLD', 0, 0

    # 주입된 캐시 장부가 없다면 백업용으로 KIS API 직접 호출
    if df is None or df.empty:
        df = kis_api.get_ohlcv(ticker, "D")

    if df is None or df.empty:
        return 'HOLD', 0, 0

    # 외부 데이터 연동 시 대소문자 불일치(KeyError) 방지 방어 코드 추가
    df.columns = [str(c).lower() for c in df.columns]

    if 'close' not in df.columns:
        return 'HOLD', 0, 0
        
    df = df.dropna(subset=['close'])

    if len(df) < 25:
        return 'HOLD', 0, 0

    c = df['close']
    # W-12: high/low 컬럼이 없을 때 KeyError 방어 — close로 대체해 전략 중단 방지
    h = df['high'] if 'high' in df.columns else c
    l = df['low'] if 'low' in df.columns else c
    price = int(c.iloc[-1])

    # 🟢 강제 매수 코드가 삭제되고, 원래의 지표 계산식으로 정상 연결됩니다.
    def _ema(s, n): return s.ewm(span=n, adjust=False).mean()
    def _sma(s, n): return s.rolling(n).mean()
    def _rsi(s, p):
        d = s.diff()
        g = d.clip(lower=0).rolling(p).mean()
        lo = (-d.clip(upper=0)).rolling(p).mean()
        return 100 - 100 / (1 + g / (lo + 1e-10))
    def _cross(fast, slow):
        now_above  = fast.iloc[-1]  > slow.iloc[-1]
        prev_above = fast.iloc[-2]  > slow.iloc[-2]
        if now_above and not prev_above: return 'BUY'
        if not now_above and prev_above: return 'SELL'
        return 'HOLD'
        
    def _thresh(ind, lo, hi):
        """임계값 돌파(반등/꺾임) 확인 로직"""
        cur, prev = ind.iloc[-1], ind.iloc[-2]
        if prev < lo and cur >= lo: return 'BUY', cur
        if prev > hi and cur <= hi: return 'SELL', cur
        return 'HOLD', cur

    try:
        sn = strategy_name
        if "RSI(9)" in sn:
            sig, val = _thresh(_rsi(c, 9), 30, 70)
            return sig, price, val
        elif "RSI(14) 30" in sn:
            # RSI(14) 30/70은 STRATEGY_REGISTRY에서 제거됨.
            # 구버전 저장 상태에서 복원된 경우 RSI(9) 30/70으로 자동 업그레이드.
            sig, val = _thresh(_rsi(c, 9), 30, 70)
            return sig, price, val
        elif "RSI(14) 40" in sn:
            sig, val = _thresh(_rsi(c, 14), 40, 60)
            return sig, price, val
        elif "EMA 5/20" in sn:
            return _cross(_ema(c, 5), _ema(c, 20)), price, _ema(c, 5).iloc[-1]
        elif "EMA 3/10" in sn:
            return _cross(_ema(c, 3), _ema(c, 10)), price, _ema(c, 3).iloc[-1]
        elif "SMA 5/20" in sn:
            return _cross(_sma(c, 5), _sma(c, 20)), price, _sma(c, 5).iloc[-1]
        elif "SMA 3/10" in sn:
            return _cross(_sma(c, 3), _sma(c, 10)), price, _sma(c, 3).iloc[-1]
        elif "SMA 3/20" in sn:
            return _cross(_sma(c, 3), _sma(c, 20)), price, _sma(c, 3).iloc[-1]
        elif "MACD" in sn:
            m = _ema(c, 12) - _ema(c, 26); ms = _ema(m, 9)
            return _cross(m, ms), price, m.iloc[-1]
        elif "볼린저" in sn:
            mid = _sma(c, 20); sd = c.rolling(20).std()
            lower = mid - (2 * sd)
            upper = mid + (2 * sd)
            
            prev_c, cur_c = c.iloc[-2], c.iloc[-1]
            prev_l, cur_l = lower.iloc[-2], lower.iloc[-1]
            prev_u, cur_u = upper.iloc[-2], upper.iloc[-1]
            
            if prev_c < prev_l and cur_c >= cur_l: return 'BUY', price, cur_c 
            if prev_c > prev_u and cur_c <= cur_u: return 'SELL', price, cur_c 
            return 'HOLD', price, cur_c
        elif "Stochastic" in sn:
            lo_r = l.rolling(14).min(); hi_r = h.rolling(14).max()
            k = 100*(c-lo_r)/(hi_r-lo_r+1e-10); d = k.rolling(3).mean()
            return _cross(k, d), price, k.iloc[-1]
        elif "CCI" in sn:
            tp = (h+l+c)/3; ma = _sma(tp, 20)
            md = tp.rolling(20).apply(lambda x: np.mean(np.abs(x-x.mean())), raw=True)
            cci_v = (tp-ma)/(0.015*md+1e-10)
            sig, val = _thresh(cci_v, -100, 100)
            return sig, price, val
        elif "Williams" in sn:
            wr = -100*(h.rolling(14).max()-c)/(h.rolling(14).max()-l.rolling(14).min()+1e-10)
            sig, val = _thresh(wr, -80, -20)
            return sig, price, val
        else:
            return get_rsi_signal(ticker, df=df)
    except Exception as e:
        print(f"[{ticker}] {strategy_name} 전략 에러: {e}")
        return 'HOLD', price, 0


def get_composite_signal(df) -> tuple:
    """
    5개 지표를 동시 평가해 매수/매도 타이밍 점수를 집계.
    단일 전략 의존 탈피 — 2개 이상 동의 시 BUY, 3개 이상 동의 시 SELL.

    매수 신호 (과매도·반등 포착):
      ① RSI(9) ≤ 35  ② MACD 히스토그램 개선  ③ BB 하단 30% 이하
      ④ Stochastic K ≤ 25  ⑤ 60MA 위

    매도 신호 (과매수·추세 이탈):
      ① RSI(9) ≥ 70  ② MACD 히스토그램 악화(음수)  ③ BB 상단 80% 이상
      ④ Stochastic K ≥ 75  ⑤ 60MA 이탈

    Returns: ('BUY'|'SELL'|'HOLD', buy_score: int, sell_score: int, reasons: list[str])
    """
    if df is None or df.empty or 'close' not in df.columns:
        return 'HOLD', 0, 0, []

    df = df.copy()
    df.columns = [str(col).lower() for col in df.columns]
    c = df['close'].dropna()
    h = df['high'].dropna() if 'high' in df.columns else c
    l = df['low'].dropna()  if 'low'  in df.columns else c

    if len(c) < 26:
        return 'HOLD', 0, 0, []

    buy_hits  = []
    sell_hits = []

    try:
        # ① RSI(9)
        d    = c.diff()
        gain = d.clip(lower=0).rolling(9).mean()
        loss = (-d.clip(upper=0)).rolling(9).mean()
        rsi9 = float((100 - 100 / (1 + gain / (loss + 1e-10))).iloc[-1])
        if rsi9 <= 35:
            buy_hits.append(f"RSI(9)={rsi9:.0f} 과매도")
        if rsi9 >= 70:
            sell_hits.append(f"RSI(9)={rsi9:.0f} 과매수")

        # ② MACD 히스토그램 방향
        ema12     = c.ewm(span=12, adjust=False).mean()
        ema26     = c.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        sig_line  = macd_line.ewm(span=9, adjust=False).mean()
        hist_now  = float(macd_line.iloc[-1] - sig_line.iloc[-1])
        hist_prev = float(macd_line.iloc[-2] - sig_line.iloc[-2])
        if hist_now > hist_prev:                         # 히스토그램 개선 중
            buy_hits.append(f"MACD히스토그램 상승({hist_now:+.3f})")
        if hist_now < hist_prev and hist_now < 0:        # 히스토그램 악화 + 음수
            sell_hits.append(f"MACD히스토그램 하락({hist_now:+.3f})")

        # ③ 볼린저밴드 위치
        if len(c) >= 22:
            ma20  = float(c.rolling(20).mean().iloc[-1])
            std20 = float(c.rolling(20).std().iloc[-1])
            bb_up = ma20 + 2 * std20
            bb_lo = ma20 - 2 * std20
            bb_rng = bb_up - bb_lo
            if bb_rng > 0:
                bb_pct = (float(c.iloc[-1]) - bb_lo) / bb_rng * 100
                if bb_pct <= 30:
                    buy_hits.append(f"BB하단근접({bb_pct:.0f}%)")
                if bb_pct >= 80:
                    sell_hits.append(f"BB상단근접({bb_pct:.0f}%)")

        # ④ Stochastic K(14)
        if len(c) >= 14:
            lo14    = float(l.rolling(14).min().iloc[-1])
            hi14    = float(h.rolling(14).max().iloc[-1])
            stoch_k = 100 * (float(c.iloc[-1]) - lo14) / (hi14 - lo14 + 1e-10)
            if stoch_k <= 25:
                buy_hits.append(f"Stochastic K={stoch_k:.0f} 과매도")
            if stoch_k >= 75:
                sell_hits.append(f"Stochastic K={stoch_k:.0f} 과매수")

        # ⑤ 60MA 지지/이탈
        if len(c) >= 62:
            ma60 = float(c.rolling(60).mean().iloc[-1])
            px   = float(c.iloc[-1])
            if px >= ma60:
                buy_hits.append(f"60MA 위({px:,.0f}≥{ma60:,.0f})")
            else:
                sell_hits.append(f"60MA 이탈({px:,.0f}<{ma60:,.0f})")

    except Exception:
        pass

    buy_score  = len(buy_hits)
    sell_score = len(sell_hits)

    BUY_THRESHOLD  = 2   # 5개 중 2개 이상 → 매수 타이밍
    SELL_THRESHOLD = 3   # 5개 중 3개 이상 → 매도 타이밍 (1-3달 보유 취지 보호)

    if buy_score >= BUY_THRESHOLD and buy_score >= sell_score:
        return 'BUY', buy_score, sell_score, buy_hits + sell_hits
    if sell_score >= SELL_THRESHOLD:
        return 'SELL', buy_score, sell_score, sell_hits + buy_hits
    return 'HOLD', buy_score, sell_score, buy_hits + sell_hits


# ─────────────────────────────────────────────────────────────────────────────
# 실전 원칙 기반 5분봉 반납률 이탈 신호
# (출처: 실전 매매 원칙 — 02_intraday_operating_rules.md / 03_sell_and_reentry_rules.md)
# ─────────────────────────────────────────────────────────────────────────────

def check_giveback_stop(
    candles: list,
    entry_price: float,
    peak_price: float,
    is_momentum_ride: bool = False,
) -> tuple:
    """
    5분봉 상승분 반납률 기반 이탈 신호 판단.

    실전 원칙 요약:
      일반주:
        - MA5 이탈 + 다음 2봉 회복 실패 + 고점 미달 → 30% 축소
        - 상승분 50~60% 반납 + MA5 이탈 → 1차 전량 익절
        - 상승분 70% 이상 반납 + 회복 실패 → 강제 익절
      급등주 (MA5 라이드형 / is_momentum_ride=True):
        - 10% 이상 상승 후 첫 MA5 이탈 또는 첫 강한 음봉 → 30% 익절
        - 상승분 30% 반납 → 추가 40% 익절
        - 상승분 50% 반납 → 잔여 30% 익절

    Parameters
    ----------
    candles        : list of dict {'open', 'high', 'low', 'close', 'volume', 'time'}
                     5분봉 최소 5개 이상 (최신봉이 마지막)
    entry_price    : 진입 평균 단가
    peak_price     : 보유 중 최고점
    is_momentum_ride : 급등주 MA5 라이드형 여부

    Returns
    -------
    (signal, giveback_pct, reason)
      signal: 'HOLD' | 'PARTIAL_EXIT_30' | 'PARTIAL_EXIT_70' | 'FULL_EXIT'
      giveback_pct: 현재 반납률(%)
      reason: 사유 문자열
    """
    if not candles or len(candles) < 3 or entry_price <= 0:
        return 'HOLD', 0.0, '데이터 부족'
    if peak_price <= entry_price:
        # [W-05] 진입 후 한 번도 진입가를 상회하지 못한 경우 — 반납률 계산 불가.
        # giveback_stop 은 "상승분을 얼마나 반납했는가" 기반이므로 여기선 HOLD.
        # 손절은 상위 호출부의 ATR 하드 손절 또는 고정 -3% 손절이 담당한다.
        return 'HOLD', 0.0, 'peak≤entry (ATR 손절 대기)'

    gain = peak_price - entry_price       # 총 상승폭
    current_price = float(candles[-1]['close'])
    giveback = peak_price - current_price  # 고점에서 현재까지 반납폭
    giveback_pct = (giveback / gain * 100) if gain > 0 else 0.0

    # 5분봉 MA5 계산
    closes = [float(c['close']) for c in candles]
    ma5_series = []
    for i in range(len(closes)):
        window = closes[max(0, i - 4): i + 1]
        ma5_series.append(sum(window) / len(window))

    latest_close = closes[-1]
    latest_ma5   = ma5_series[-1]
    prev_ma5     = ma5_series[-2] if len(ma5_series) >= 2 else latest_ma5

    ma5_broken = latest_close < latest_ma5   # 종가 MA5 이탈 여부

    # 고점 대비 MA5 이탈 후 반등 고점 확인 (최근 2봉)
    recent_high_after_break = max(float(candles[-1]['high']), float(candles[-2]['high'])) \
        if len(candles) >= 2 else float(candles[-1]['high'])
    reference_high = max(float(c['high']) for c in candles[-5:]) if len(candles) >= 5 else peak_price
    high_lower = recent_high_after_break < reference_high * 0.997  # 반등 고점 미달

    # 거래량 기준 강한 음봉 여부
    vols = [float(c.get('volume', 0)) for c in candles]
    avg_vol_10 = sum(vols[-11:-1]) / 10 if len(vols) >= 11 else (sum(vols[:-1]) / max(1, len(vols) - 1))
    latest_vol = vols[-1]
    is_heavy_candle = (latest_vol > avg_vol_10 * 1.5) and (latest_close < float(candles[-1]['open']))

    # ── 급등주 MA5 라이드형 ───────────────────────────────
    if is_momentum_ride:
        total_gain_pct = (peak_price / entry_price - 1) * 100
        if total_gain_pct >= 10 and ma5_broken and (is_heavy_candle or high_lower):
            return 'PARTIAL_EXIT_30', giveback_pct, f'급등주 MA5 첫 이탈 (고점+{total_gain_pct:.1f}%)'
        if giveback_pct >= 50:
            return 'FULL_EXIT',        giveback_pct, f'급등주 50% 반납 → 잔여 전량 익절'
        if giveback_pct >= 30:
            return 'PARTIAL_EXIT_70', giveback_pct, f'급등주 30% 반납 → 추가 40% 익절 구간'
        return 'HOLD', giveback_pct, 'MA5 라이드 추세 유지'

    # ── 일반주 ────────────────────────────────────────────
    if ma5_broken and high_lower:
        if giveback_pct >= 70:
            return 'FULL_EXIT', giveback_pct, f'70% 반납 + MA5 회복 실패 → 강제 익절'
        if giveback_pct >= 50:
            return 'PARTIAL_EXIT_70', giveback_pct, f'50~70% 반납 + MA5 이탈 → 1차 익절 구간'
        if giveback_pct >= 30:
            return 'PARTIAL_EXIT_30', giveback_pct, f'MA5 이탈+고점미달 → 30% 축소'

    if giveback_pct >= 70:
        return 'FULL_EXIT', giveback_pct, f'70% 반납 → 늦은 강제 손실 제한'

    return 'HOLD', giveback_pct, f'반납률 {giveback_pct:.0f}% — 보유 유지'


def check_theme_overextension_exit(df, current_price: float, sector_bonus: int = 0) -> tuple:
    """
    테마주 과열 청산 신호 — 급락 패턴 역공학 분석 반영.

    분석 결과 요약:
      - 에코프로·HLB·알테오젠·레인보우로보틱스·SMCI 급락 전 공통 패턴
      - 60일 이동평균 이격 평균 +45% (가장 신뢰도 높은 단일 신호)
      - RSI 다이버전스 + 거래량 소멸 복합 조건 = 강력 청산

    Parameters
    ----------
    df            : OHLCV DataFrame (close/high/low/volume 컬럼)
    current_price : 현재가
    sector_bonus  : 섹터 보너스 점수 (테마 강도 — 10 이상이면 강한 테마)

    Returns
    -------
    (signal, score, reason)
      signal : 'HOLD' | 'PARTIAL_EXIT_30' | 'PARTIAL_EXIT_60' | 'FULL_EXIT'
      score  : 감지된 위험 신호 수 (0~4)
      reason : 사유 문자열
    """
    if df is None or df.empty or len(df) < 40 or 'close' not in df.columns:
        return 'HOLD', 0, '데이터 부족'

    close  = df['close'].dropna()
    volume = df['volume'].dropna() if 'volume' in df.columns else None
    price  = float(current_price)

    risk_signals = []

    # ── ① 60일선 이격도 (핵심 신호 — 분석 평균 +45% 초과 시 급락) ─────
    if len(close) >= 60:
        ma60 = float(close.rolling(60).mean().iloc[-1])
        if ma60 > 0:
            gap60 = (price - ma60) / ma60 * 100
            if gap60 >= 50:
                risk_signals.append(f"60일선 극이격 +{gap60:.0f}%")
            elif gap60 >= 30:
                risk_signals.append(f"60일선 이격 +{gap60:.0f}%")

    # ── ② 20일선 이격도 ────────────────────────────────────────────────
    if len(close) >= 20:
        ma20 = float(close.rolling(20).mean().iloc[-1])
        if ma20 > 0:
            gap20 = (price - ma20) / ma20 * 100
            if gap20 >= 30:
                risk_signals.append(f"20일선 이격 +{gap20:.0f}%")

    # ── ③ RSI 베어리시 다이버전스 ────────────────────────────────────
    if len(close) >= 40:
        d   = close.diff()
        g   = d.clip(lower=0).rolling(14).mean()
        lo  = (-d.clip(upper=0)).rolling(14).mean()
        rsi = 100 - 100 / (1 + g / (lo + 1e-10))

        w1_rsi_max   = float(rsi.iloc[-20:].max())   # 최근 20일 RSI 최고
        w2_rsi_max   = float(rsi.iloc[-40:-20].max())  # 이전 20일 RSI 최고
        w1_price_max = float(close.iloc[-20:].max())
        w2_price_max = float(close.iloc[-40:-20].max())

        price_new_high = w1_price_max > w2_price_max          # 가격 신고점
        rsi_lower      = w1_rsi_max < w2_rsi_max - 5          # RSI 약화 (5점 이상)
        if price_new_high and rsi_lower:
            risk_signals.append(f"RSI 베어다이버전스 ({w2_rsi_max:.0f}→{w1_rsi_max:.0f})")

    # ── ④ 거래량 소멸 ────────────────────────────────────────────────
    if volume is not None and len(volume) >= 40:
        vol_w1 = float(volume.iloc[-20:].mean())   # 최근 20일 거래량
        vol_w2 = float(volume.iloc[-40:-20].mean())  # 이전 20일 거래량
        if vol_w2 > 0:
            vol_fade = vol_w1 / vol_w2
            if vol_fade < 0.7:
                risk_signals.append(f"거래량 소멸 {vol_fade:.2f}x")
            elif vol_fade < 0.85:
                risk_signals.append(f"거래량 감소 {vol_fade:.2f}x")

    # ── 신호 수 → 청산 등급 결정 ────────────────────────────────────
    n = len(risk_signals)
    reason = " | ".join(risk_signals) if risk_signals else "이상 없음"

    # 강한 테마 수혜주는 임계값을 1단계 높임 (한국 분석: 테마 수혜 시 더 달림)
    threshold_adjust = 1 if sector_bonus >= 10 else 0

    if n == 0:
        return 'HOLD', 0, reason
    elif n == 1:
        # 단일 신호 → 30% 익절
        if n > threshold_adjust:
            return 'PARTIAL_EXIT_30', n, reason
        return 'HOLD', n, f"[테마완화] {reason}"
    elif n == 2:
        # 2개 신호 → 60% 익절
        if n > threshold_adjust:
            return 'PARTIAL_EXIT_60', n, reason
        return 'PARTIAL_EXIT_30', n, f"[테마완화 -1단계] {reason}"
    else:
        # 3개 이상 → 전량 청산
        if n > threshold_adjust:
            return 'FULL_EXIT', n, reason
        return 'PARTIAL_EXIT_60', n, f"[테마완화 -1단계] {reason}"


def check_rsi_progressive_exit(df, current_price: float, avg_price: float) -> tuple:
    """
    RSI 구간별 점진적 익절 — 테마주 급락 분석 반영.

    분석 근거:
      HLB: 고점 RSI 71 후 -56% / NVDA 조정: RSI 80 후 -14%
      RSI 75 이상 구간부터 단계적 익절로 고점 물리는 리스크 감소

    Parameters
    ----------
    df            : OHLCV DataFrame
    current_price : 현재가
    avg_price     : 보유 평균 단가

    Returns
    -------
    (signal, rsi_val, reason)
      signal : 'HOLD' | 'PARTIAL_EXIT_30' | 'PARTIAL_EXIT_60' | 'FULL_EXIT'
    """
    if df is None or df.empty or len(df) < 20 or 'close' not in df.columns:
        return 'HOLD', 0.0, '데이터 부족'

    # 수익 중일 때만 RSI 익절 적용 (손실 구간에서 RSI로 청산 방지)
    profit_pct = (current_price / avg_price - 1) * 100 if avg_price > 0 else 0
    if profit_pct < 5.0:
        return 'HOLD', 0.0, f'수익률 {profit_pct:.1f}% — RSI 익절 대기 (5% 이상 시 발동)'

    close = df['close'].dropna()
    d  = close.diff()
    g  = d.clip(lower=0).rolling(14).mean()
    lo = (-d.clip(upper=0)).rolling(14).mean()
    rsi_series = 100 - 100 / (1 + g / (lo + 1e-10))
    rsi = float(rsi_series.iloc[-1])

    if rsi >= 90:
        return 'FULL_EXIT',        rsi, f'RSI {rsi:.0f} — 극과매수, 전량 익절 준비'
    elif rsi >= 85:
        return 'PARTIAL_EXIT_60',  rsi, f'RSI {rsi:.0f} — 강한 과매수, 60% 익절'
    elif rsi >= 75:
        return 'PARTIAL_EXIT_30',  rsi, f'RSI {rsi:.0f} — 과매수 진입, 30% 선익절'
    else:
        return 'HOLD',             rsi, f'RSI {rsi:.0f} — 정상 범위'


# ─────────────────────────────────────────────────────────────────────────────
# 코어 전용 진입 점수 — RSI 저평가 + 장기 이동평균만 사용
# 모멘텀·거래량·외국계·MACD 완전 제외 (장기 프로젝트 원칙)
# ─────────────────────────────────────────────────────────────────────────────

def calculate_core_entry_score(df, price: float, regime: str = 'NEUTRAL') -> tuple:
    """
    코어 전용 진입 점수 (최대 6점).
    RSI 저평가 + 120MA/60MA 위치만 판단.
    모멘텀, 거래량, 외국계, MACD 완전 무시.

    Returns: (score: int, reasons: list[str])
    """
    if df is None or df.empty or 'close' not in df.columns:
        return 0, []

    score   = 0
    reasons = []
    try:
        c = df['close'].dropna()
        if len(c) < 16:
            return 0, []

        # ① RSI 저평가 (최대 +3) — 핵심 기준
        rsi = _calc_rsi14(c)
        if rsi <= 32:
            score += 3
            reasons.append(f"RSI 극저평가({rsi:.0f}≤32) +3")
        elif rsi <= 38:
            score += 2
            reasons.append(f"RSI 저평가({rsi:.0f}≤38) +2")
        elif rsi <= 45:
            score += 1
            reasons.append(f"RSI 과매도근접({rsi:.0f}≤45)")

        # ② 120MA 위 (+2) — 장기 우상향 안전 확인
        if len(c) >= 120:
            ma120 = float(c.rolling(120).mean().iloc[-1])
            if price > ma120:
                score += 2
                reasons.append(f"120MA 위({ma120:,.0f}) +2")

        # ③ 60MA 위 (+1) — 중기 지지 확인
        if len(c) >= 62:
            ma60 = float(c.rolling(60).mean().iloc[-1])
            if price > ma60:
                score += 1
                reasons.append(f"60MA 위({ma60:,.0f})")

    except Exception:
        pass

    return score, reasons


def get_core_entry_threshold(regime: str) -> int:
    """
    코어 전용 진입 기준점.
    - BULL:    RSI만 저평가여도 진입 (threshold=2 → RSI≤38만으로 충족)
    - NEUTRAL: RSI≤45(+1) + 120MA(+2) = 3점 충족
    - BEAR:    RSI≤38(+2) + 120MA(+2) = 4점 충족 (하락장 안전 강화)
    """
    return {'BULL': 2, 'NEUTRAL': 3, 'BEAR': 4}.get(regime, 3)


# ─────────────────────────────────────────────────────────────────────────────
# 통합 매수 강도 점수 — KR / US 공통 (10점 만점)
# ─────────────────────────────────────────────────────────────────────────────

def calculate_entry_score(df, price: float, regime: str = 'NEUTRAL',
                           frgn_net: int = 0, momentum_20d: float = 0.0) -> tuple:
    """
    10개 지표 합산 매수 강도 점수.  KR/US 공통 사용.
    frgn_net    : KR 외국계 순매수 주수 (양수이면 +1점)
    momentum_20d: US 20일 가격 모멘텀 % (3% 초과이면 +1점)

    Returns: (score: int, reasons: list[str])
    """
    if df is None or df.empty or 'close' not in df.columns:
        return 0, []

    score   = 0
    reasons = []
    try:
        c = df['close'].dropna()
        if len(c) < 6:
            return 0, []

        # ① 20일선 위 (+1)
        if len(c) >= 22:
            ma20 = float(c.rolling(20).mean().iloc[-1])
            if price > ma20:
                score += 1
                reasons.append(f"가격>20MA({ma20:,.0f})")

        # ② 60일선 위 (+1)
        if len(c) >= 62:
            ma60 = float(c.rolling(60).mean().iloc[-1])
            if price > ma60:
                score += 1
                reasons.append(f"가격>60MA({ma60:,.0f})")

        # ③ 5MA > 20MA 단기 정배열 (+1)
        if len(c) >= 22:
            ma5_v  = float(c.rolling(5).mean().iloc[-1])
            ma20_v = float(c.rolling(20).mean().iloc[-1])
            if ma5_v > ma20_v:
                score += 1
                reasons.append("5MA>20MA 정배열")

        # ④ MACD 히스토그램 플러스 (+1)
        if len(c) >= 30:
            ema12       = c.ewm(span=12, adjust=False).mean()
            ema26       = c.ewm(span=26, adjust=False).mean()
            macd_line   = ema12 - ema26
            signal_line = macd_line.ewm(span=9, adjust=False).mean()
            hist        = float(macd_line.iloc[-1] - signal_line.iloc[-1])
            if hist > 0:
                score += 1
                reasons.append(f"MACD히스토그램+({hist:+.2f})")

        # ⑤ RSI 과매도 근접 (최대 +2) — 30 근처일수록 높은 점수
        if len(c) >= 16:
            rsi = _calc_rsi14(c)
            if rsi <= 32:
                score += 2
                reasons.append(f"RSI과매도({rsi:.0f}≤32) +2")
            elif rsi <= 38:
                score += 1
                reasons.append(f"RSI과매도근접({rsi:.0f}≤38)")
            elif rsi <= 45:
                score += 1
                reasons.append(f"RSI하락접근({rsi:.0f}≤45)")

        # ⑥ 거래량 100% 이상 (+1) — 평소 거래량 이상이면 OK (완화: 130% → 100%)
        if 'volume' in df.columns:
            v = df['volume'].dropna()
            if len(v) >= 21:
                avg_vol   = float(v.iloc[-21:-1].mean())
                today_vol = float(v.iloc[-1])
                if avg_vol > 0 and today_vol >= avg_vol * 1.00:
                    score += 1
                    reasons.append(f"거래량정상({today_vol / avg_vol * 100:.0f}%)")

        # ⑦ 전일 종가 이상 (+1)
        if len(c) >= 2:
            prev_close = float(c.iloc[-2])
            if price >= prev_close:
                score += 1
                reasons.append("전일종가회복")

        # ⑧ 볼린저밴드 25~75% 구간 (+1) — 극단 회피 (BULL 장에서는 상단 90%까지 허용)
        if len(c) >= 22:
            ma20_bb = float(c.rolling(20).mean().iloc[-1])
            std20   = float(c.rolling(20).std().iloc[-1])
            bb_upper = ma20_bb + 2 * std20
            bb_lower = ma20_bb - 2 * std20
            bb_range = bb_upper - bb_lower
            if bb_range > 0:
                bb_pct = (price - bb_lower) / bb_range * 100
                bb_upper_limit = 90 if regime == 'BULL' else 75
                if 25 <= bb_pct <= bb_upper_limit:
                    score += 1
                    reasons.append(f"BB내위치({bb_pct:.0f}%)")

        # ⑨ 수급: KR 외국계 순매수 OR US 20일 모멘텀 (+1)
        if frgn_net > 0:
            score += 1
            reasons.append(f"외국계순매수(+{frgn_net:,}주)")
        elif momentum_20d > 3.0:
            score += 1
            reasons.append(f"20일모멘텀({momentum_20d:+.1f}%)")

        # ⑩ 120일선 위 (+1) — 장기 우상향 확인 (데이터 120일치 이상일 때만)
        if len(c) >= 120:
            ma120 = float(c.rolling(120).mean().iloc[-1])
            if price > ma120:
                score += 1
                reasons.append(f"가격>120MA({ma120:,.0f})")

    except Exception:
        pass

    return score, reasons


def get_entry_threshold(regime: str, slot: str = 'satellite') -> int:
    """
    국면·슬롯별 최소 진입 점수.
    slot: 'core' | 'satellite'
    """
    table = {
        ('BULL',    'core'):      6,
        ('BULL',    'satellite'): 5,
        ('NEUTRAL', 'core'):      7,
        ('NEUTRAL', 'satellite'): 6,
        ('BEAR',    'core'):      8,
        ('BEAR',    'satellite'): 7,
    }
    # 주의: RSI ⑤번 항목이 최대 +2로 변경돼 총 만점은 11점.
    # 합격선은 기존 유지 — RSI 과매도 구간(≤32)에서 +1 추가 보너스가 진입을 더 유리하게 만듦.
    return table.get((regime, slot), 6)


def get_budget_ratio_from_score(score: int, threshold: int) -> float:
    """
    임계치 초과분 → 예산 투입 비율.
    +0 → 30 % | +1 → 45 % | +2 → 60 % | +3 → 75 % | +4+ → 90 %
    """
    excess = max(0, score - threshold)
    ratios = [0.30, 0.45, 0.60, 0.75, 0.90]
    return ratios[min(excess, len(ratios) - 1)]


def check_early_drop_stop(current_price: float, entry_price: float) -> tuple:
    """
    장초 09:00~09:10 급락 분기 원칙 (02_intraday_operating_rules.md).

    Parameters
    ----------
    current_price : 현재가
    entry_price   : 진입가 (기준가)

    Returns
    -------
    (stage, sell_pct, reason)
      stage: 0=정상 | 1=-2% | 2=-3~4% | 3=-5%이상
      sell_pct: 매도 비율 (0.0~1.0)
      reason: 사유 문자열
    """
    if entry_price <= 0:
        return 0, 0.0, '기준가 없음'

    drop_pct = (current_price / entry_price - 1) * 100  # 음수 = 하락

    if drop_pct <= -5.0:
        return 3, 1.0, f'장초 -5% 이상 급락 → 강제 전량 손실 제한 ({drop_pct:.1f}%)'
    elif drop_pct <= -3.0:
        return 2, 1.0, f'장초 -3~4% 하락 → 잔여 전량 축소 우선 ({drop_pct:.1f}%)'
    elif drop_pct <= -2.0:
        return 1, 0.5, f'장초 -2% 도달 → 보유 50% 축소 (thesis 1차 훼손) ({drop_pct:.1f}%)'
    else:
        return 0, 0.0, f'정상 범위 ({drop_pct:.1f}%)'


class Position:
    """개별 종목 포지션 상태 관리 (실전 거래세 및 매매 수수료 완벽 시뮬레이션 적용)"""
    def __init__(self, ticker, name, budget):
        self.ticker    = ticker
        self.name      = name
        self.budget    = budget      # 배정 자금
        self.initial_cash = budget
        self.cash      = budget      # 가용 현금
        self.shares    = 0           # 보유 주식 수
        self.avg_price = 0           # 평균 매수가
        self.trades    = []          # 거래 기록
        self.max_price = 0
        self.order_pending = False   # (기존 유지)
        self.last_order_time = 0.0   # 쿨타임 측정용 타임스탬프

        # ── 고성능 매매 전략 속성 ─────────────────────────────────
        # 분할 매수: 1차 매수 후 눌림목에서 2차 추가 매수
        self.second_buy_price = 0.0   # 2차 매수 발동 기준가 (1차 매수가 × 0.98)
        self.second_buy_cash  = 0.0   # 2차 매수용 유보 현금
        self.second_buy_done  = False # 2차 매수 완료 여부

        # 피라미딩: 수익 중인 포지션에 추가 매수
        self.pyramid_done     = False # 피라미딩 완료 여부

        # 분할 익절: 1차 50% 선익절 후 나머지 ATR 트레일링
        self.partial_sold     = False # 1차 익절 완료 여부
        self.partial_sold_2   = False # 2차 익절 완료 여부
        self.overext_sell_count = 0   # 과열 선익절 횟수 (최대 3차: 30%→30%→전량)

        # AI 익절 판단 (백그라운드 스레드)
        self.ai_exit_pending     = False  # AI 요청 진행 중
        self.ai_exit_decision    = None   # 'SELL_PARTIAL' / 'SELL_ALL' / 'HOLD' / None
        self.ai_exit_asked_price = 0.0    # 마지막 AI 문의 시점 가격 (새 고점 갱신 시 재요청)

        # 한국 금융시장 표준 수수료 및 거래세율 정의
        self.fee_rate = 0.00015      # 실전 및 모의 온라인 매매 수수료 기본율 (0.015%)
        self.tax_rate = 0.0018       # 장내 매도 시 국가 증권거래세율 (0.18%)

        # 상태 뱃지 및 메시지 초기화
        self.status = "감시 중 👀"
        self.status_msg = "현재 지정된 전략에 따라 차트 및 지표를 실시간 감시하고 있습니다."

    def buy(self, price, all_in=True):
        if self.shares > 0 or self.cash < price:
            return 0
            
        # 수수료 비용 및 시장가(최유리지정가) 매수 시 증거금 버퍼(1%)를 반영하여 예수금 펑크를 방지합니다.
        qty = int((self.cash * 0.99) // (price * (1 + self.fee_rate)))
        if qty == 0:
            return 0
            
        stock_cost = qty * price
        brokerage_fee = round(stock_cost * self.fee_rate, 2)
        total_cost = stock_cost + brokerage_fee
        
        self.shares    = qty
        self.avg_price = round(total_cost / qty, 2) # 수수료가 포함된 정밀한 실전 평단가 산출
        self.cash     -= total_cost
        
        self.trades.append({
            'type': 'BUY', 'price': price, 'qty': qty, 
            'fee': brokerage_fee, 'tax': 0.0, 'time': datetime.now()
        })
        return qty

    def sell(self, price):
        if self.shares == 0:
            return 0, 0
            
        gross_revenue = self.shares * price
        brokerage_fee = round(gross_revenue * self.fee_rate, 2)
        trading_tax   = round(gross_revenue * self.tax_rate, 2)
        total_deduction = brokerage_fee + trading_tax
        net_revenue   = gross_revenue - total_deduction # 수수료와 세금이 원천징수된 실제 인출 가능 현금
        
        profit  = net_revenue - (self.avg_price * self.shares)
        self.cash  += net_revenue
        qty         = self.shares
        
        self.shares = 0
        self.avg_price = 0
        
        self.trades.append({
            'type': 'SELL', 'price': price, 'qty': qty, 
            'fee': brokerage_fee, 'tax': trading_tax, 'profit': profit, 'time': datetime.now()
        })
        return qty, profit

    @property
    def current_value(self):
        return self.cash + (self.shares * self.avg_price if self.shares > 0 else 0)


class CorePosition:
    """코어 포지션 - RSI 신호 기반 트레이딩, 단 최소 수량(Floor)은 항상 유지 (수수료 모델 이식 완료)"""
    def __init__(self, ticker, name, initial_cash):
        self.ticker      = ticker
        self.name        = name
        self.shares      = 0
        self.floor_shares = 0     # 절대 팔지 않을 최소 수량
        self.avg_price   = 0
        self.initial_cash = initial_cash
        self.cash        = initial_cash
        self.buy_log     = []
        self.sell_log    = []
        self.order_pending = False # 🟢 중복 주문 방지용 락 플래그 추가
        
        self.fee_rate = 0.00015   # 수수료율 (0.015%)
        self.tax_rate = 0.0018    # 거래세율 (0.18%)
        
        # 상태 및 익절 추적 플래그
        self.status = "감시 중 👀"
        self.status_msg = "현재 지정된 전략에 따라 차트 및 지표를 실시간 감시하고 있습니다."
        self.partial_sold      = False  # 1차 익절(+10%) 완료 여부
        self.partial_sold_2    = False  # 2차 익절(+20%) 완료 여부
        self.max_price         = 0      # 보유 중 최고가
        self.ai_exit_pending   = False  # AI 요청 진행 중
        self.ai_exit_decision  = None   # 'SELL_PARTIAL' / 'HOLD' / None
        self.ai_exit_hold_until= 0.0    # HOLD 판단 후 재요청 금지 시각

        # ── 2차 분할 매수 (위성과 동일 구조) ────────────────────────
        self.second_buy_price  = 0.0    # 2차 매수 발동가 (1차 진입가 × 0.98)
        self.second_buy_cash   = 0.0    # 2차 매수 유보 예산
        self.second_buy_done   = False  # 2차 매수 완료 여부

        # ── 적립식(DCA) 모드 ─────────────────────────────────────────
        self.dca_mode           = False   # 적립식 모드 활성화 여부
        self.dca_amount         = 0       # 1회 적립 금액 (0=잔여 예산의 10%)
        self.dca_interval_hours = 72      # 정기 적립 주기 (기본 3일 = 72시간)
        self.dca_dip_pct        = 3.0     # 눌림목 트리거: 평단 대비 -X% 하락 시 추가 매수
        self.last_dca_time      = 0.0     # 마지막 DCA 매수 시각 (unix timestamp)

    def buy(self, price, cash_to_use=None):
        """매수 (cash_to_use 미지정 시 전액 매수)"""
        # [C-06] cash_to_use=0.0 → 'if cash_to_use' 는 False → 전액 매수 오인.
        # is not None 패턴으로 수정.
        budget = cash_to_use if cash_to_use is not None else self.cash
        available_budget = min(budget, self.cash)
        
        # 수수료 비용 및 시장가(최유리지정가) 매수 시 증거금 버퍼(1%)를 반영하여 예수금 펑크를 방지합니다.
        qty = int((available_budget * 0.99) // (price * (1 + self.fee_rate)))
        if qty == 0:
            return 0
            
        stock_cost = qty * price
        brokerage_fee = round(stock_cost * self.fee_rate, 2)
        total_cost = stock_cost + brokerage_fee
        
        current_total_investment = self.avg_price * self.shares + total_cost
        self.shares    += qty
        self.avg_price  = round(current_total_investment / self.shares, 2)
        self.cash      -= total_cost
        
        # floor_shares는 최초 매수 시 설정 + 재투자로 보유량이 늘어날 때마다 갱신
        # (최초 설정값 고정 시 장기 보유 중 보호 물량 비율이 점차 줄어드는 버그 수정)
        self.floor_shares = max(self.floor_shares, max(1, int(self.shares * CORE_MIN_FLOOR_RATIO)))
            
        # [C-NEW-07] append 전에 reason 계산해야 len()==0 조건이 의미있음
        buy_reason = 'initial' if len(self.buy_log) == 0 else 'reinvest'
        self.buy_log.append({
            'time': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'price': price, 'qty': qty,
            'total_shares': self.shares,
            'fee': brokerage_fee,
            'reason': buy_reason,
        })
        return qty

    def sell(self, price):
        """매도 - floor(최소 수량) 이상의 수량만 매도"""
        sellable = self.shares - self.floor_shares
        if sellable <= 0:
            return 0, 0
            
        gross_revenue = sellable * price
        brokerage_fee = round(gross_revenue * self.fee_rate, 2)
        trading_tax   = round(gross_revenue * self.tax_rate, 2)
        net_revenue   = gross_revenue - (brokerage_fee + trading_tax)
        
        profit  = net_revenue - (self.avg_price * sellable)
        self.cash   += net_revenue
        self.shares -= sellable
        
        self.sell_log.append({
            'time': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'price': price, 'qty': sellable, 'profit': profit,
            'fee': brokerage_fee, 'tax': trading_tax, 'remaining': self.shares
        })
        return sellable, profit

    def current_value(self, current_price):
        return self.shares * current_price + self.cash