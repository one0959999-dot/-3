import re
import json
import time
import logging
import pandas as pd
import numpy as np
from datetime import datetime
from typing import Optional

logger = logging.getLogger('lassi_bot')

BATCH_SIZE_WEEKDAY = 100
BATCH_SIZE_WEEKEND = 200

_MIN_MOVE_PCT   = 10.0   # 바닥/고점 인정 최소 등락폭 (%)
_WINDOW         = 20     # 로컬 미니멈/맥시멈 탐지 윈도우 (일)
_MIN_GAP_DAYS   = 15     # 포인트 간 최소 간격 (일)
_AI_BATCH       = 5      # AI 호출 1회당 포인트 수
_RATE_LIMIT_SEC = 4      # Gemini free 15RPM → 4초 간격


def _get_all_kr_tickers() -> list[dict]:
    try:
        from pykrx import stock as pykrx_stock
        today = datetime.now().strftime('%Y%m%d')
        kospi  = pykrx_stock.get_market_ticker_list(today, market='KOSPI')
        kosdaq = pykrx_stock.get_market_ticker_list(today, market='KOSDAQ')
        tickers = []
        for t in kospi + kosdaq:
            name = pykrx_stock.get_market_ticker_name(t)
            tickers.append({'ticker': t, 'name': name})
        return tickers
    except Exception as e:
        logger.warning(f"[백테스트] 종목 리스트 조회 실패: {e}")
        return []


def _get_full_history(ticker: str, toss_api=None) -> Optional[pd.DataFrame]:
    if toss_api:
        try:
            df = toss_api.get_full_history(ticker)
            if df is not None and not df.empty:
                return df
        except Exception:
            pass
    try:
        import yfinance as yf
        for suffix in ['.KS', '.KQ']:
            df = yf.download(ticker + suffix, period='max', interval='1d',
                             progress=False, auto_adjust=True)
            if not df.empty:
                df.columns = [c.lower() for c in df.columns]
                df.index = pd.to_datetime(df.index)
                return df.dropna(subset=['close'])
    except Exception as e:
        logger.debug(f"[백테스트] {ticker} yfinance 실패: {e}")
    return None


def _calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
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


def _find_optimal_points(df: pd.DataFrame) -> list[dict]:
    close  = df['close'].values
    n      = len(close)
    raw    = []

    for i in range(_WINDOW, n - _WINDOW):
        window_slice = close[i - _WINDOW: i + _WINDOW + 1]

        if close[i] == window_slice.min():
            recent_peak = close[max(0, i - 60): i + 1].max()
            drawdown = (recent_peak - close[i]) / recent_peak * 100
            if drawdown >= _MIN_MOVE_PCT:
                raw.append({'idx': i, 'type': 'BOTTOM', 'magnitude': round(drawdown, 2)})

        if close[i] == window_slice.max():
            recent_trough = close[max(0, i - 60): i + 1].min()
            rally = (close[i] - recent_trough) / recent_trough * 100
            if rally >= _MIN_MOVE_PCT:
                raw.append({'idx': i, 'type': 'TOP', 'magnitude': round(rally, 2)})

    raw.sort(key=lambda x: x['idx'])

    filtered, last_idx = [], -_MIN_GAP_DAYS
    for p in raw:
        if p['idx'] - last_idx >= _MIN_GAP_DAYS:
            filtered.append(p)
            last_idx = p['idx']

    return filtered


def _active_signals(row: pd.Series, prev_row: pd.Series) -> list[str]:
    sigs = []
    rsi = row.get('rsi')
    if pd.notna(rsi):
        if rsi <= 30: sigs.append('RSI_BUY')
        elif rsi >= 70: sigs.append('RSI_SELL')

    m, ms, pm, ps = (row.get(k) for k in ['macd','macd_signal','macd','macd_signal'])
    pm, ps = prev_row.get('macd'), prev_row.get('macd_signal')
    m, ms  = row.get('macd'), row.get('macd_signal')
    if all(pd.notna(x) for x in [m, ms, pm, ps]):
        if pm <= ps and m > ms: sigs.append('MACD_BUY')
        elif pm >= ps and m < ms: sigs.append('MACD_SELL')

    close, bl, bu = row.get('close'), row.get('bb_lower'), row.get('bb_upper')
    if all(pd.notna(x) for x in [close, bl, bu]):
        if close <= bl: sigs.append('BB_BUY')
        elif close >= bu: sigs.append('BB_SELL')

    vr, sma20, pc = row.get('vol_ratio'), row.get('sma20'), prev_row.get('close')
    if all(pd.notna(x) for x in [vr, close, sma20, pc]):
        if vr >= 200 and close > sma20:
            sigs.append('VOL_BUY' if close > pc else 'VOL_SELL')

    s5, s20, ps5, ps20 = (row.get('sma5'), row.get('sma20'),
                           prev_row.get('sma5'), prev_row.get('sma20'))
    if all(pd.notna(x) for x in [s5, s20, ps5, ps20]):
        if ps5 <= ps20 and s5 > s20: sigs.append('MA_BUY')
        elif ps5 >= ps20 and s5 < s20: sigs.append('MA_SELL')

    h20, l20, ph20, pl20 = (row.get('high_20'), row.get('low_20'),
                              prev_row.get('high_20'), prev_row.get('low_20'))
    if all(pd.notna(x) for x in [close, h20, l20, ph20, pl20, pc]):
        if pc < ph20 and close >= h20: sigs.append('BREAK_BUY')
        elif pc > pl20 and close <= l20: sigs.append('BREAK_SELL')

    return sigs


def _future_stats(df: pd.DataFrame, idx: int) -> dict:
    price = df.iloc[idx]['close']
    result = {}
    for days in [5, 20, 60]:
        end = min(idx + days, len(df) - 1)
        future = df.iloc[idx + 1: end + 1]
        if future.empty:
            continue
        result[f'pnl_{days}d']      = round((df.iloc[end]['close'] / price - 1) * 100, 2)
        result[f'max_gain_{days}d'] = round((future['close'].max() / price - 1) * 100, 2)
        result[f'max_loss_{days}d'] = round((future['close'].min() / price - 1) * 100, 2)
    return result


def _support_resistance(df: pd.DataFrame, idx: int):
    hist = df.iloc[max(0, idx - 60): idx + 1]
    support    = float(hist['low'].rolling(20).min().dropna().iloc[-1])  if len(hist) >= 20 else 0
    resistance = float(hist['high'].rolling(20).max().dropna().iloc[-1]) if len(hist) >= 20 else 0
    return support, resistance


def _fibonacci(support: float, resistance: float):
    if not support or not resistance or resistance <= support:
        return 0, 0, 0
    rng = resistance - support
    return (round(resistance - rng * 0.382, 2),
            round(resistance - rng * 0.500, 2),
            round(resistance - rng * 0.618, 2))


def _batch_ai_analysis(points: list[dict], ticker: str, stock_name: str, ai_client) -> list[str]:
    """포인트 최대 _AI_BATCH개를 한 번의 AI 호출로 분석."""
    sections = []
    for i, p in enumerate(points):
        ptype_kr = '바닥' if p['point_type'] == 'BOTTOM' else '고점'
        mag_label = '하락폭' if p['point_type'] == 'BOTTOM' else '상승폭'
        sections.append(
            f"[{i+1}] {ptype_kr} — {p['date']} — {p['price']:,.0f}원 ({mag_label} {p['magnitude_pct']:.1f}%)\n"
            f"RSI {p['rsi']:.1f} | MACD {p['macd']:.3f} | "
            f"볼린저 {p['bb_lower']:,.0f}~{p['bb_upper']:,.0f} | 거래량 {p['vol_ratio']:.0f}%\n"
            f"SMA5:{p['sma5']:,.0f} SMA20:{p['sma20']:,.0f} SMA60:{p['sma60']:,.0f} SMA120:{p['sma120']:,.0f}\n"
            f"활성 신호: {', '.join(p['signals_active']) or '없음'}\n"
            f"지지:{p['support']:,.0f} 저항:{p['resistance']:,.0f}\n"
            f"매크로: {p.get('macro_str','정보없음')}\n"
            f"실제결과: 5일 {p.get('pnl_5d',0):+.1f}% | 20일 {p.get('pnl_20d',0):+.1f}% | "
            f"60일 {p.get('pnl_60d',0):+.1f}% | 60일최대수익 {p.get('max_gain_60d',0):+.1f}%\n"
        )

    prompt = (
        f"[{stock_name}({ticker})] 과거 실제 최적 매매 포인트 {len(points)}개 분석\n\n"
        + '\n'.join(sections)
        + "\n각 포인트에 대해 번호([1][2]...)로 구분해서:\n"
        "① 이 시점이 바닥/고점이었던 이유 (어떤 지표 조합이 핵심 시그널이었나)\n"
        "② 당시 시장/매크로 국면 특징\n"
        "③ 다음에 이 조합이 나오면 어떻게 대응해야 하는가\n"
        "각 포인트를 3~5줄로 간결하게."
    )

    try:
        res = ai_client.generate_content(prompt, temperature=0.2)
        analyses = [''] * len(points)
        parts = re.split(r'\[(\d+)\]', res)
        for j in range(1, len(parts), 2):
            idx = int(parts[j]) - 1
            if 0 <= idx < len(analyses) and j + 1 < len(parts):
                analyses[idx] = parts[j + 1].strip()[:600]
        return analyses
    except Exception as e:
        logger.warning(f"[백테스트] AI 분석 오류: {e}")
        return [''] * len(points)


def run_full_backtest_ticker(ticker: str, stock_name: str, user_id: int,
                              ai_client, toss_api=None, fred_key: str = '') -> int:
    from base.database import log_optimal_point, update_backtest_full_progress
    from base.macro_collector import get_macro_for_date, build_macro_context_str

    df = _get_full_history(ticker, toss_api)
    if df is None or len(df) < 60:
        return 0

    df = _calc_indicators(df).dropna(subset=['rsi', 'macd', 'bb_mid'])
    if len(df) < 60:
        return 0

    optimal = _find_optimal_points(df)
    if not optimal:
        return 0

    records = []
    for p in optimal:
        i    = p['idx']
        row  = df.iloc[i]
        prev = df.iloc[i - 1]
        date = df.index[i].strftime('%Y-%m-%d')
        price = float(row['close'] or 0)
        if not price:
            continue

        try:
            macro = get_macro_for_date(date, fred_key=fred_key)
        except Exception:
            macro = {}
        macro_str = build_macro_context_str(macro)
        from base.market_phase import get_phase_for_date, build_phase_context_str
        phase_info = get_phase_for_date('KR', date, macro)
        signals   = _active_signals(row, prev)
        support, resistance = _support_resistance(df, i)
        fib_382, fib_500, fib_618 = _fibonacci(support, resistance)
        stats = _future_stats(df, i)

        records.append({
            'user_id': user_id, 'mode': 'KR',
            'ticker': ticker, 'stock_name': stock_name,
            'date': date, 'point_type': p['type'],
            'price': price, 'magnitude_pct': p['magnitude'],
            'market_phase':    phase_info.get('phase'),
            'market_phase_kr': phase_info.get('phase_kr'),
            'phase_confidence':phase_info.get('confidence'),
            'rsi':        round(float(row.get('rsi', 0)), 2),
            'macd':       round(float(row.get('macd', 0)), 4),
            'macd_signal':round(float(row.get('macd_signal', 0)), 4),
            'bb_upper':   round(float(row.get('bb_upper', 0)), 2),
            'bb_mid':     round(float(row.get('bb_mid', 0)), 2),
            'bb_lower':   round(float(row.get('bb_lower', 0)), 2),
            'sma5':   round(float(row.get('sma5', 0)), 2),
            'sma20':  round(float(row.get('sma20', 0)), 2),
            'sma60':  round(float(row.get('sma60', 0)), 2),
            'sma120': round(float(row.get('sma120', 0)), 2),
            'vol_ratio':   float(row.get('vol_ratio', 0)),
            'support': support, 'resistance': resistance,
            'fib_382': fib_382, 'fib_500': fib_500, 'fib_618': fib_618,
            'signals_active': json.dumps(signals, ensure_ascii=False),
            'signal_count':   len(signals),
            'macro_date': date,
            'macro_str':  macro_str + '\n' + build_phase_context_str(phase_info),
            **stats,
        })

    for batch_start in range(0, len(records), _AI_BATCH):
        batch = records[batch_start: batch_start + _AI_BATCH]
        analyses = _batch_ai_analysis(batch, ticker, stock_name, ai_client)
        for rec, analysis in zip(batch, analyses):
            rec['ai_analysis'] = analysis
            log_optimal_point(rec)
        time.sleep(_RATE_LIMIT_SEC)

    update_backtest_full_progress('KR', ticker, datetime.now().strftime('%Y-%m-%d'), len(records))
    return len(records)


class BacktestRunner:

    def __init__(self, user_id: int, ai_client, toss_api=None, fred_key: str = ''):
        self.user_id  = user_id
        self.ai       = ai_client
        self.toss     = toss_api
        self.fred_key = fred_key

    def run_batch(self, batch_size: int = BATCH_SIZE_WEEKDAY) -> int:
        from base.database import get_backtest_full_done

        all_tickers = _get_all_kr_tickers()
        if not all_tickers:
            return 0

        done    = get_backtest_full_done('KR')
        pending = [t for t in all_tickers if t['ticker'] not in done]
        if not pending:
            logger.info("[KR 백테스트] 전체 완료 — 처음부터 재시작")
            pending = all_tickers

        total = 0
        for item in pending[:batch_size]:
            try:
                n = run_full_backtest_ticker(
                    item['ticker'], item['name'],
                    self.user_id, self.ai, self.toss, self.fred_key
                )
                if n:
                    logger.info(f"[KR 백테스트] {item['name']}({item['ticker']}): {n}개 포인트")
                    total += n
            except Exception as e:
                logger.warning(f"[KR 백테스트] {item['ticker']} 오류: {e}")
            time.sleep(1)

        return total
