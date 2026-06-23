"""신호·지표 단일 기준(Source of Truth).

백테스트와 라이브 봇이 '똑같은' 함수로 신호를 탐지·라벨링하게 하는 공용 모듈.
백테스트가 기준이므로, 라이브 봇도 여기 detect_signals 를 호출해
RSI_BUY / MACD_BUY / BB_BUY / MA_BUY / VOL_BUY / BREAK_BUY 동일 라벨을 생성한다.
(기존 KR/US backtest_runner 의 _calc_indicators / _detect_signals 를 통합)
"""
import pandas as pd
import numpy as np


def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """OHLCV → 지표 컬럼 추가 (백테스트 기준 그대로)."""
    df = df.copy()
    close = df['close']
    high  = df['high']
    low   = df['low']
    vol   = df['volume']

    df['sma5']   = close.rolling(5).mean()
    df['sma20']  = close.rolling(20).mean()
    df['sma60']  = close.rolling(60).mean()
    df['sma120'] = close.rolling(120).mean()

    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    df['rsi'] = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df['macd']        = ema12 - ema26
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()

    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    df['bb_mid']   = sma20
    df['bb_upper'] = sma20 + 2 * std20
    df['bb_lower'] = sma20 - 2 * std20

    df['vol_ma20']  = vol.rolling(20).mean()
    df['vol_ratio'] = (vol / df['vol_ma20'] * 100).round(1)

    df['high_20'] = high.rolling(20).max()
    df['low_20']  = low.rolling(20).min()

    return df


def detect_signals(row: pd.Series, prev_row: pd.Series) -> list[str]:
    """신호 이벤트 감지 — 크로스/터치 발생 시점만 (백테스트 기준 그대로).

    반환 라벨: RSI_BUY/SELL, MACD_BUY/SELL, BB_BUY/SELL, VOL_BUY/SELL,
              MA_BUY/SELL, BREAK_BUY/SELL
    """
    sigs = []
    rsi, prev_rsi = row.get('rsi'), prev_row.get('rsi')
    if pd.notna(rsi) and pd.notna(prev_rsi):
        if prev_rsi > 30 and rsi <= 30:
            sigs.append('RSI_BUY')
        elif prev_rsi < 70 and rsi >= 70:
            sigs.append('RSI_SELL')

    m, ms = row.get('macd'), row.get('macd_signal')
    pm, ps = prev_row.get('macd'), prev_row.get('macd_signal')
    if all(pd.notna(x) for x in [m, ms, pm, ps]):
        if pm <= ps and m > ms:
            sigs.append('MACD_BUY')
        elif pm >= ps and m < ms:
            sigs.append('MACD_SELL')

    close, bl, bu = row.get('close'), row.get('bb_lower'), row.get('bb_upper')
    pc = prev_row.get('close')
    pbl, pbu = prev_row.get('bb_lower'), prev_row.get('bb_upper')
    if all(pd.notna(x) for x in [close, bl, bu, pc, pbl, pbu]):
        if pc > pbl and close <= bl:
            sigs.append('BB_BUY')
        elif pc < pbu and close >= bu:
            sigs.append('BB_SELL')

    vr, sma20 = row.get('vol_ratio'), row.get('sma20')
    if all(pd.notna(x) for x in [vr, close, sma20, pc]) and vr >= 200:
        sigs.append('VOL_BUY' if close > sma20 else 'VOL_SELL')

    s5, s20v = row.get('sma5'), row.get('sma20')
    ps5, ps20 = prev_row.get('sma5'), prev_row.get('sma20')
    if all(pd.notna(x) for x in [s5, s20v, ps5, ps20]):
        if ps5 <= ps20 and s5 > s20v:
            sigs.append('MA_BUY')
        elif ps5 >= ps20 and s5 < s20v:
            sigs.append('MA_SELL')

    h20, l20 = row.get('high_20'), row.get('low_20')
    ph20, pl20 = prev_row.get('high_20'), prev_row.get('low_20')
    if all(pd.notna(x) for x in [close, h20, l20, ph20, pl20, pc]):
        if pc < ph20 and close >= h20:
            sigs.append('BREAK_BUY')
        elif pc > pl20 and close <= l20:
            sigs.append('BREAK_SELL')

    return sigs


def detect_latest_signals(df: pd.DataFrame) -> list[str]:
    """라이브용 — OHLCV df 받아 지표계산 후 '가장 최근 봉'의 신호 라벨 반환.

    라이브 봇이 이 함수만 호출하면 백테스트와 동일 라벨을 얻는다.
    """
    if df is None or len(df) < 21 or 'close' not in df.columns:
        return []
    d = calc_indicators(df)
    if len(d) < 2:
        return []
    return detect_signals(d.iloc[-1], d.iloc[-2])
