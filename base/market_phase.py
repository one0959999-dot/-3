"""
시장 국면 분류기
모든 매매 판단의 기반이 되는 현재/과거 시장 국면을 분류한다.
"""

import logging
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

logger = logging.getLogger('lassi_bot')

PHASES = {
    'BULL_EARLY':  '상승장 초입',
    'BULL_MID':    '상승장 중반',
    'BULL_LATE':   '상승장 말기',
    'BEAR_EARLY':  '하락장 초기',
    'BEAR_MID':    '하락장 중반',
    'BEAR_LATE':   '하락장 말기 (바닥권)',
    'SIDEWAYS':    '횡보장',
    'PANIC':       '패닉 / 급락',
    'RECOVERY':    '회복 구간',
}

PHASE_BEST_SIGNALS = {
    'BULL_EARLY':  ['BREAK_BUY', 'MA_BUY', 'MACD_BUY'],
    'BULL_MID':    ['MA_BUY', 'RSI_BUY', 'MACD_BUY'],
    'BULL_LATE':   ['RSI_SELL', 'BB_SELL', 'VOL_SELL'],
    'BEAR_EARLY':  ['RSI_SELL', 'MACD_SELL', 'BB_SELL'],
    'BEAR_MID':    ['RSI_SELL', 'MACD_SELL'],
    'BEAR_LATE':   ['RSI_BUY', 'BB_BUY', 'VOL_BUY'],
    'SIDEWAYS':    ['BB_BUY', 'BB_SELL', 'RSI_BUY', 'RSI_SELL'],
    'PANIC':       ['BB_BUY', 'RSI_BUY'],
    'RECOVERY':    ['BREAK_BUY', 'MA_BUY', 'MACD_BUY', 'RSI_BUY'],
}

PHASE_ADVICE = {
    'BULL_EARLY':  '추세 추종 매수 유리. 눌림목 시 적극 매수. 손절보다 홀딩 우선.',
    'BULL_MID':    '상승 지속 중. RSI 과열 주의하며 분할 매도 준비.',
    'BULL_LATE':   '고점권 접근. 신규 매수 자제. 익절 타이밍 탐색.',
    'BEAR_EARLY':  '하락 초기. 신규 매수 금지. 기존 보유분 손절 검토.',
    'BEAR_MID':    '하락 중. 현금 보유 최우선. 반등 매도 기회 활용.',
    'BEAR_LATE':   '바닥권 접근. 소량 분할 매수 시작 고려. RSI 20 이하 시 역발상 매수.',
    'SIDEWAYS':    '범위 내 박스권. 볼린저 하단 매수/상단 매도 전략. 추세 돌파 대기.',
    'PANIC':       '극단적 공포 구간. 역발상 분할 매수 기회. 단기 반등 70%+ 확률.',
    'RECOVERY':    '반등 초기. 돌파 종목 중심 매수. 추세 전환 확인 후 비중 확대.',
}


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()

    up   = high.diff()
    down = -low.diff()
    pdm  = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    ndm  = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)

    pdi = 100 * pdm.ewm(span=period, adjust=False).mean() / atr.replace(0, np.nan)
    ndi = 100 * ndm.ewm(span=period, adjust=False).mean() / atr.replace(0, np.nan)

    dx  = (100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan))
    return dx.ewm(span=period, adjust=False).mean()


def _get_index_data(mode: str, date_str: str) -> pd.DataFrame | None:
    try:
        import yfinance as yf
        symbol = '^KS11' if mode == 'KR' else '^GSPC'
        end   = datetime.strptime(date_str, '%Y-%m-%d') + timedelta(days=3)
        start = end - timedelta(days=500)
        df = yf.download(symbol, start=start.strftime('%Y-%m-%d'),
                         end=end.strftime('%Y-%m-%d'),
                         interval='1d', progress=False, auto_adjust=True)
        if df.empty:
            return None
        if hasattr(df.columns, 'get_level_values'):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        df.index = pd.to_datetime(df.index)
        return df.dropna(subset=['close'])
    except Exception as e:
        logger.debug(f"[국면] 지수 데이터 조회 실패: {e}")
        return None


def classify_phase(mode: str, date_str: str, macro: dict | None = None) -> dict:
    """
    주어진 날짜의 시장 국면을 분류한다.

    Returns:
        {
            'phase': 'BULL_MID',
            'phase_kr': '상승장 중반',
            'confidence': 0.82,
            'evidence': [...],           # 판단 근거 목록
            'best_signals': [...],       # 이 국면에서 유효한 신호
            'advice': '...',             # 전략 조언
            'index_vs_ma200': 5.2,       # 지수의 MA200 대비 %
            'momentum_20d': 3.1,         # 지수 20일 수익률
            'adx': 28.4,                 # 추세 강도
            'vix': 18.2,                 # VIX (있을 경우)
        }
    """
    df = _get_index_data(mode, date_str)

    if df is None or len(df) < 60:
        return _unknown_phase()

    target_date = pd.Timestamp(date_str)
    avail = df[df.index <= target_date]
    if avail.empty or len(avail) < 60:
        return _unknown_phase()

    close   = avail['close']
    high    = avail['high']
    low     = avail['low']
    current = float(close.iloc[-1])

    ma20   = float(close.rolling(20).mean().iloc[-1])
    ma60   = float(close.rolling(60).mean().iloc[-1])
    ma120  = float(close.rolling(120).mean().iloc[-1]) if len(avail) >= 120 else ma60
    ma200  = float(close.rolling(200).mean().iloc[-1]) if len(avail) >= 200 else ma120

    momentum_20d = round((current / float(close.iloc[-21]) - 1) * 100, 2) if len(avail) > 21 else 0
    momentum_60d = round((current / float(close.iloc[-61]) - 1) * 100, 2) if len(avail) > 61 else 0
    vs_ma200     = round((current / ma200 - 1) * 100, 2)
    vs_52w_high  = round((current / float(close.rolling(252).max().iloc[-1]) - 1) * 100, 2) if len(avail) >= 252 else 0

    adx_val  = float(_adx(high, low, close).iloc[-1])
    ma200_slope = round((float(close.rolling(200).mean().iloc[-1]) /
                          float(close.rolling(200).mean().iloc[-21]) - 1) * 100, 2) \
                  if len(avail) >= 221 else 0

    vix = float(macro.get('vix', 0)) if macro else 0
    us_rate = float(macro.get('us_rate', 0)) if macro else 0

    evidence = []

    # ── 국면 판단 로직 ──────────────────────────────
    phase = 'SIDEWAYS'
    confidence = 0.5

    if vix > 40 and momentum_20d < -8:
        phase = 'PANIC'
        confidence = 0.90
        evidence.append(f'VIX {vix:.1f} (극단적 공포)')
        evidence.append(f'20일 급락 {momentum_20d:+.1f}%')

    elif current < ma200 * 0.92 and momentum_20d < -5:
        phase = 'BEAR_MID'
        confidence = 0.80
        evidence.append(f'지수 MA200 {vs_ma200:+.1f}% 하방')
        evidence.append(f'20일 수익률 {momentum_20d:+.1f}%')

    elif current < ma200 and momentum_60d < -15:
        phase = 'BEAR_MID'
        confidence = 0.75
        evidence.append(f'MA200 하방 + 60일 {momentum_60d:+.1f}%')

    elif current < ma200 and momentum_20d < -3:
        phase = 'BEAR_EARLY'
        confidence = 0.70
        evidence.append(f'MA200 {vs_ma200:+.1f}% — 하락 초기')

    elif current < ma200 and adx_val < 18:
        phase = 'BEAR_LATE'
        confidence = 0.65
        evidence.append(f'MA200 하방이나 ADX {adx_val:.1f} (추세 소멸)')
        evidence.append('바닥 다지기 구간')

    elif current > ma200 and momentum_20d > 3 and momentum_60d < -10:
        phase = 'RECOVERY'
        confidence = 0.75
        evidence.append(f'MA200 회복 + 60일 저점 대비 반등')
        evidence.append(f'20일 {momentum_20d:+.1f}%')

    elif current > ma200 and ma200_slope > 0:
        if vs_52w_high > -5:
            phase = 'BULL_LATE'
            confidence = 0.78
            evidence.append(f'52주 고점 대비 {vs_52w_high:+.1f}% (고점권)')
            evidence.append(f'MA200 상방 {vs_ma200:+.1f}%')
        elif current > ma60 > ma120 and adx_val > 25:
            if momentum_20d > 0 and momentum_60d < 15:
                phase = 'BULL_EARLY'
                confidence = 0.80
                evidence.append('단기/중기 이평 정배열 시작')
                evidence.append(f'ADX {adx_val:.1f} (추세 강화 중)')
            else:
                phase = 'BULL_MID'
                confidence = 0.75
                evidence.append(f'이평 정배열 + ADX {adx_val:.1f}')
                evidence.append(f'MA200 {vs_ma200:+.1f}% 상방')
        else:
            phase = 'BULL_MID'
            confidence = 0.65
            evidence.append(f'MA200 상방 {vs_ma200:+.1f}%')

    else:
        phase = 'SIDEWAYS'
        confidence = 0.60
        evidence.append(f'ADX {adx_val:.1f} (추세 불명확)')
        evidence.append(f'MA200 대비 {vs_ma200:+.1f}%')

    if vix > 0:
        evidence.append(f'VIX {vix:.1f}')
    if us_rate > 0:
        evidence.append(f'미국 기준금리 {us_rate:.2f}%')
    evidence.append(f'지수 20일 {momentum_20d:+.1f}% / 60일 {momentum_60d:+.1f}%')

    return {
        'phase':          phase,
        'phase_kr':       PHASES.get(phase, phase),
        'confidence':     confidence,
        'evidence':       evidence,
        'best_signals':   PHASE_BEST_SIGNALS.get(phase, []),
        'advice':         PHASE_ADVICE.get(phase, ''),
        'index_vs_ma200': vs_ma200,
        'momentum_20d':   momentum_20d,
        'momentum_60d':   momentum_60d,
        'adx':            round(adx_val, 1),
        'vix':            vix,
        'vs_52w_high':    vs_52w_high,
    }


def _unknown_phase() -> dict:
    return {
        'phase': 'UNKNOWN', 'phase_kr': '판단불가', 'confidence': 0,
        'evidence': [], 'best_signals': [], 'advice': '',
        'index_vs_ma200': 0, 'momentum_20d': 0, 'momentum_60d': 0,
        'adx': 0, 'vix': 0, 'vs_52w_high': 0,
    }


def build_phase_context_str(phase_info: dict) -> str:
    if not phase_info or phase_info.get('phase') == 'UNKNOWN':
        return ''
    lines = [
        f"[시장 국면] {phase_info['phase_kr']} (신뢰도 {int(phase_info['confidence']*100)}%)",
        f"근거: {' | '.join(phase_info['evidence'][:4])}",
        f"이 국면 유효 신호: {', '.join(phase_info['best_signals'])}",
        f"전략 조언: {phase_info['advice']}",
    ]
    return '\n'.join(lines)


def get_phase_for_date(mode: str, date_str: str, macro: dict | None = None) -> dict:
    """캐시 포함 국면 조회 (DB macro_daily_snapshot 활용)."""
    try:
        from base.database import get_macro_snapshot
        if macro is None:
            macro = get_macro_snapshot(date_str) or {}
    except Exception:
        macro = {}
    return classify_phase(mode, date_str, macro)
