import re
import json
import time
import logging
import pandas as pd
import numpy as np
from datetime import datetime
from typing import Optional

logger = logging.getLogger('lassi_bot')

BATCH_SIZE_WEEKDAY = 50
BATCH_SIZE_WEEKEND = 150

_MIN_MOVE_PCT   = 10.0
_WINDOW         = 20
_MIN_GAP_DAYS   = 15
_AI_BATCH       = 5
_RATE_LIMIT_SEC = 4

_US_UNIVERSE = [
    'AAPL','MSFT','NVDA','AMZN','GOOGL','META','TSLA','AVGO','JPM','UNH',
    'LLY','XOM','V','MA','JNJ','PG','HD','COST','ABBV','MRK','NFLX','CRM',
    'BAC','CVX','AMD','ORCL','CSCO','PEP','TMO','ACN','MCD','ABT','INTC',
    'ADBE','QCOM','TXN','NKE','WMT','DIS','AMGN','PM','RTX','HON','UPS',
    'LOW','IBM','GS','SPGI','AXP','BLK','SCHW','T','VZ','DE','MMM','CAT',
    'BA','GE','LMT','NOC','F','GM','UBER','LYFT','SNAP','PINS','RBLX','COIN',
    'PLTR','RIVN','LCID','SOFI','HOOD','APP','DKNG','MSTR','SMCI','ARM',
    'TSM','ASML','SHOP','SQ','PYPL','AFRM','UPST',
    'RKLB','JOBY','ACHR','LUNR','ASTS','SOUN','IONQ','RGTI','QUBT',
    'MRVL','KLAC','AMAT','LRCX','MU','WDC','STX',
    'ENPH','FSLR','PLUG','NEE',
    'ZM','DOCU','DDOG','SNOW','NET','CRWD','PANW','OKTA','ZS',
]


def _get_full_history_us(ticker: str) -> Optional[pd.DataFrame]:
    try:
        import yfinance as yf
        df = yf.download(ticker, period='max', interval='1d',
                         progress=False, auto_adjust=True)
        if df.empty:
            return None
        if hasattr(df.columns, 'get_level_values'):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        df.index = pd.to_datetime(df.index)
        return df.dropna(subset=['close'])
    except Exception as e:
        logger.debug(f"[US 백테스트] {ticker} yfinance 실패: {e}")
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
    close = df['close'].values
    n     = len(close)
    raw   = []

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

    m, ms = row.get('macd'), row.get('macd_signal')
    pm, ps = prev_row.get('macd'), prev_row.get('macd_signal')
    if all(pd.notna(x) for x in [m, ms, pm, ps]):
        if pm <= ps and m > ms: sigs.append('MACD_BUY')
        elif pm >= ps and m < ms: sigs.append('MACD_SELL')

    close, bl, bu = row.get('close'), row.get('bb_lower'), row.get('bb_upper')
    if all(pd.notna(x) for x in [close, bl, bu]):
        if close <= bl: sigs.append('BB_BUY')
        elif close >= bu: sigs.append('BB_SELL')

    vr, s20, pc = row.get('vol_ratio'), row.get('sma20'), prev_row.get('close')
    if all(pd.notna(x) for x in [vr, close, s20, pc]):
        if vr >= 200 and close > s20:
            sigs.append('VOL_BUY' if close > pc else 'VOL_SELL')

    s5, s20v, ps5, ps20 = (row.get('sma5'), row.get('sma20'),
                            prev_row.get('sma5'), prev_row.get('sma20'))
    if all(pd.notna(x) for x in [s5, s20v, ps5, ps20]):
        if ps5 <= ps20 and s5 > s20v: sigs.append('MA_BUY')
        elif ps5 >= ps20 and s5 < s20v: sigs.append('MA_SELL')

    h20, l20 = row.get('high_20'), row.get('low_20')
    ph20, pl20 = prev_row.get('high_20'), prev_row.get('low_20')
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
    sections = []
    for i, p in enumerate(points):
        ptype_en = 'BOTTOM' if p['point_type'] == 'BOTTOM' else 'TOP'
        sections.append(
            f"[{i+1}] {ptype_en} — {p['date']} — ${p['price']:.2f} (move {p['magnitude_pct']:.1f}%)\n"
            f"RSI {p['rsi']:.1f} | MACD {p['macd']:.4f} | "
            f"BB ${p['bb_lower']:.2f}~${p['bb_upper']:.2f} | Vol {p['vol_ratio']:.0f}%\n"
            f"SMA5:${p['sma5']:.2f} SMA20:${p['sma20']:.2f} SMA60:${p['sma60']:.2f} SMA120:${p['sma120']:.2f}\n"
            f"Active signals: {', '.join(p['signals_active']) or 'none'}\n"
            f"Macro: {p.get('macro_str','N/A')}\n"
            f"Outcome: 5d {p.get('pnl_5d',0):+.1f}% | 20d {p.get('pnl_20d',0):+.1f}% | "
            f"60d {p.get('pnl_60d',0):+.1f}% | 60d max gain {p.get('max_gain_60d',0):+.1f}%\n"
        )

    prompt = (
        f"[{stock_name}({ticker})] Historical optimal trading points analysis\n\n"
        + '\n'.join(sections)
        + "\nFor each point, labeled [1][2]... analyze:\n"
        "① Why this was a bottom/top (which indicator combination was the key signal)\n"
        "② Market/macro environment at that time\n"
        "③ How to respond when this combination appears again\n"
        "Keep each point to 3-5 lines."
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
        logger.warning(f"[US 백테스트] AI 분석 오류: {e}")
        return [''] * len(points)


def run_full_backtest_ticker_us(ticker: str, user_id: int, ai_client) -> int:
    from base.database import log_optimal_point, update_backtest_full_progress
    from base.macro_collector import get_macro_for_date, build_macro_context_str

    df = _get_full_history_us(ticker)
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
        i     = p['idx']
        row   = df.iloc[i]
        prev  = df.iloc[i - 1]
        date  = df.index[i].strftime('%Y-%m-%d')
        price = float(row['close'])

        macro     = get_macro_for_date(date)
        macro_str = build_macro_context_str(macro)
        from base.market_phase import get_phase_for_date, build_phase_context_str
        phase_info = get_phase_for_date('US', date, macro)
        signals   = _active_signals(row, prev)
        support, resistance = _support_resistance(df, i)
        fib_382, fib_500, fib_618 = _fibonacci(support, resistance)
        stats = _future_stats(df, i)

        records.append({
            'user_id': user_id, 'mode': 'US',
            'ticker': ticker, 'stock_name': ticker,
            'date': date, 'point_type': p['type'],
            'price': price, 'magnitude_pct': p['magnitude'],
            'market_phase':    phase_info.get('phase'),
            'market_phase_kr': phase_info.get('phase_kr'),
            'phase_confidence':phase_info.get('confidence'),
            'rsi':        round(float(row.get('rsi', 0)), 2),
            'macd':       round(float(row.get('macd', 0)), 6),
            'macd_signal':round(float(row.get('macd_signal', 0)), 6),
            'bb_upper':   round(float(row.get('bb_upper', 0)), 4),
            'bb_mid':     round(float(row.get('bb_mid', 0)), 4),
            'bb_lower':   round(float(row.get('bb_lower', 0)), 4),
            'sma5':   round(float(row.get('sma5', 0)), 4),
            'sma20':  round(float(row.get('sma20', 0)), 4),
            'sma60':  round(float(row.get('sma60', 0)), 4),
            'sma120': round(float(row.get('sma120', 0)), 4),
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
        analyses = _batch_ai_analysis(batch, ticker, ticker, ai_client)
        for rec, analysis in zip(batch, analyses):
            rec['ai_analysis'] = analysis
            log_optimal_point(rec)
        time.sleep(_RATE_LIMIT_SEC)

    update_backtest_full_progress('US', ticker, datetime.now().strftime('%Y-%m-%d'), len(records))
    return len(records)


class USBacktestRunner:

    def __init__(self, user_id: int, ai_client):
        self.user_id = user_id
        self.ai      = ai_client

    def run_batch(self, batch_size: int = BATCH_SIZE_WEEKDAY) -> int:
        from base.database import get_backtest_full_done

        done    = get_backtest_full_done('US')
        pending = [t for t in _US_UNIVERSE if t not in done]
        if not pending:
            logger.info("[US 백테스트] 전체 완료 — 처음부터 재시작")
            pending = _US_UNIVERSE

        total = 0
        for ticker in pending[:batch_size]:
            try:
                n = run_full_backtest_ticker_us(ticker, self.user_id, self.ai)
                if n:
                    logger.info(f"[US 백테스트] {ticker}: {n}개 포인트")
                    total += n
            except Exception as e:
                logger.warning(f"[US 백테스트] {ticker} 오류: {e}")
            time.sleep(1)

        return total
