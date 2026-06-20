import time
import logging
import pandas as pd
import numpy as np
from datetime import datetime
from typing import Optional

logger = logging.getLogger('lassi_bot')

BATCH_SIZE_WEEKDAY = 50
BATCH_SIZE_WEEKEND = 150

_US_UNIVERSE = [
    'AAPL','MSFT','NVDA','AMZN','GOOGL','META','TSLA','AVGO','JPM','UNH',
    'LLY','XOM','V','MA','JNJ','PG','HD','COST','ABBV','MRK','NFLX','CRM',
    'BAC','CVX','AMD','ORCL','CSCO','PEP','TMO','ACN','MCD','ABT','INTC',
    'ADBE','QCOM','TXN','NKE','WMT','DIS','AMGN','PM','RTX','HON','UPS',
    'LOW','IBM','GS','SPGI','AXP','BLK','SCHW','T','VZ','DE','MMM','CAT',
    'BA','GE','LMT','NOC','F','GM','UBER','LYFT','SNAP','PINS','RBLX','COIN',
    'PLTR','RIVN','LCID','SOFI','HOOD','APP','DKNG','MSTR','SMCI','ARM',
    'TSM','ASML','SHOP','SQ','PYPL','AFRM','UPST','SOXX','QQQ','SPY',
    'RKLB','JOBY','ACHR','LUNR','ASTS','SOUN','IONQ','RGTI','QUBT',
    'MRVL','KLAC','AMAT','LRCX','MU','WDC','STX','ONTO','WOLF',
    'ENPH','SEDG','FSLR','PLUG','BLNK','CHPT','NEE','TSLA',
    'ZM','DOCU','DDOG','SNOW','NET','CRWD','S','PANW','OKTA','ZS',
]


def _get_full_history_us(ticker: str) -> Optional[pd.DataFrame]:
    try:
        import yfinance as yf
        df = yf.download(ticker, period='max', interval='1d',
                         progress=False, auto_adjust=True)
        if df.empty:
            return None
        df.columns = [c.lower() for c in df.columns]
        df.index = pd.to_datetime(df.index)
        return df.dropna(subset=['close'])
    except Exception as e:
        logger.debug(f"[US백테스트] {ticker} 조회 실패: {e}")
        return None


def _calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close = df['close']
    high  = df['high']
    low   = df['low']
    vol   = df['volume']

    df['sma5']   = close.rolling(5).mean()
    df['sma20']  = close.rolling(20).mean()
    df['sma60']  = close.rolling(60).mean()
    df['sma200'] = close.rolling(200).mean()

    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / loss.replace(0, np.nan)
    df['rsi'] = 100 - (100 / (1 + rs))

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

    return df


def _detect_signals(row: pd.Series, prev_row: pd.Series) -> list[str]:
    signals = []
    rsi = row.get('rsi')
    if pd.notna(rsi):
        if rsi <= 30:
            signals.append('RSI_BUY')
        elif rsi >= 70:
            signals.append('RSI_SELL')

    macd      = row.get('macd')
    macd_sig  = row.get('macd_signal')
    prev_macd = prev_row.get('macd')
    prev_sig  = prev_row.get('macd_signal')
    if all(pd.notna(x) for x in [macd, macd_sig, prev_macd, prev_sig]):
        if prev_macd <= prev_sig and macd > macd_sig:
            signals.append('MACD_BUY')
        elif prev_macd >= prev_sig and macd < macd_sig:
            signals.append('MACD_SELL')

    close = row.get('close')
    bb_l  = row.get('bb_lower')
    bb_u  = row.get('bb_upper')
    if all(pd.notna(x) for x in [close, bb_l, bb_u]):
        if close <= bb_l:
            signals.append('BB_BUY')
        elif close >= bb_u:
            signals.append('BB_SELL')

    vol_ratio  = row.get('vol_ratio')
    sma20      = row.get('sma20')
    prev_close = prev_row.get('close')
    if all(pd.notna(x) for x in [vol_ratio, close, sma20, prev_close]):
        if vol_ratio >= 200 and close > sma20:
            signals.append('VOL_BUY' if close > prev_close else 'VOL_SELL')

    sma5       = row.get('sma5')
    prev_sma5  = prev_row.get('sma5')
    prev_sma20 = prev_row.get('sma20')
    if all(pd.notna(x) for x in [sma5, sma20, prev_sma5, prev_sma20]):
        if prev_sma5 <= prev_sma20 and sma5 > sma20:
            signals.append('MA_BUY')
        elif prev_sma5 >= prev_sma20 and sma5 < sma20:
            signals.append('MA_SELL')

    return signals


def _future_stats(df: pd.DataFrame, idx: int, days: int) -> dict:
    end = min(idx + days, len(df) - 1)
    future = df.iloc[idx+1:end+1]
    if future.empty:
        return {}
    price = df.iloc[idx]['close']
    min_p = float(future['close'].min())
    max_p = float(future['close'].max())
    pnl   = round((df.iloc[end]['close'] / price - 1) * 100, 2)
    return {
        f'min_price_{days}d': min_p,
        f'max_price_{days}d': max_p,
        f'days_to_min_{days}d': int(future['close'].argmin()) + 1,
        f'days_to_max_{days}d': int(future['close'].argmax()) + 1,
        f'pnl_{days}d': pnl,
    }


def run_full_backtest_ticker_us(ticker: str, user_id: int, claude_client) -> int:
    from base.database import load_ai_rules, log_backtest_signal
    from base.macro_collector import get_macro_for_date, build_macro_context_str

    df = _get_full_history_us(ticker)
    if df is None or len(df) < 60:
        return 0

    df = _calc_indicators(df)
    df = df.dropna(subset=['rsi', 'macd', 'bb_mid'])
    ai_rules = load_ai_rules(user_id)
    signals_logged = 0

    for i in range(1, len(df) - 21):
        row      = df.iloc[i]
        prev_row = df.iloc[i - 1]
        price    = float(row['close'])
        date_str = df.index[i].strftime('%Y-%m-%d')

        signal_types = _detect_signals(row, prev_row)
        if not signal_types:
            continue

        macro     = get_macro_for_date(date_str)
        macro_str = build_macro_context_str(macro)

        hist = df.iloc[:i+1]
        lows  = hist['low'].rolling(20).min().dropna()
        highs = hist['high'].rolling(20).max().dropna()
        support    = float(lows.iloc[-1]) if not lows.empty else 0
        resistance = float(highs.iloc[-1]) if not highs.empty else 0

        rng = resistance - support
        fib_382 = round(resistance - rng * 0.382, 2) if rng > 0 else 0
        fib_500 = round(resistance - rng * 0.500, 2) if rng > 0 else 0
        fib_618 = round(resistance - rng * 0.618, 2) if rng > 0 else 0

        sma200 = row.get('sma200')
        above_200 = price > sma200 if pd.notna(sma200) and sma200 else None

        stats_5d  = _future_stats(df, i, 5)
        stats_20d = _future_stats(df, i, 20)

        for sig_type in signal_types:
            signal = 'BUY' if 'BUY' in sig_type else 'SELL'

            context = (
                f"[종목] {ticker} | 날짜: {date_str} | 현재가: ${price:.2f}\n"
                f"[기술지표] RSI:{row.get('rsi', 0):.1f} | MACD:{row.get('macd', 0):.3f} | "
                f"볼린저 ${row.get('bb_lower', 0):.2f}~${row.get('bb_upper', 0):.2f}\n"
                f"SMA20:${row.get('sma20', 0):.2f} SMA200:${sma200:.2f if pd.notna(sma200) else 0} "
                f"({'200일선 위' if above_200 else '200일선 아래'}) | 거래량: 평소대비 {row.get('vol_ratio', 0):.0f}%\n"
                f"신호: {sig_type} | 지지: ${support:.2f} | 저항: ${resistance:.2f}\n"
                f"피보나치 38.2%:${fib_382:.2f} 50%:${fib_500:.2f} 61.8%:${fib_618:.2f}\n"
                f"{macro_str}"
            )

            try:
                result = claude_client.ai_approve_trade(
                    signal, ticker, ticker, price, sig_type,
                    row.get('rsi', 0), [], [], ai_rules,
                    context=context,
                    portfolio_context="[US 백테스트 모드]"
                )
                decision   = result[0]
                reason     = result[1]
                confidence = result[2] if len(result) > 2 else 75
            except Exception as e:
                logger.debug(f"[US백테스트] {ticker} AI 오류: {e}")
                time.sleep(2)
                continue

            log_backtest_signal({
                'user_id': user_id, 'mode': 'US',
                'ticker': ticker, 'stock_name': ticker,
                'trade_date': date_str, 'signal': signal, 'signal_type': sig_type,
                'price': price,
                'ai_decision': 'CONFIRM' if decision else 'REJECT',
                'confidence': confidence, 'ai_reason': reason[:400],
                'macro_date': date_str,
                'rsi':         round(float(row.get('rsi', 0)), 2),
                'macd':        round(float(row.get('macd', 0)), 4),
                'macd_signal': round(float(row.get('macd_signal', 0)), 4),
                'bb_upper':    round(float(row.get('bb_upper', 0)), 2),
                'bb_mid':      round(float(row.get('bb_mid', 0)), 2),
                'bb_lower':    round(float(row.get('bb_lower', 0)), 2),
                'sma5':   round(float(row.get('sma5', 0)), 2),
                'sma20':  round(float(row.get('sma20', 0)), 2),
                'sma60':  round(float(row.get('sma60', 0) or 0), 2),
                'sma120': round(float(row.get('sma200', 0) or 0), 2),
                'vol_ratio': float(row.get('vol_ratio', 0)),
                'support': support, 'resistance': resistance,
                'fib_382': fib_382, 'fib_500': fib_500, 'fib_618': fib_618,
                **stats_5d, **stats_20d,
            })
            signals_logged += 1
            time.sleep(0.15)

    return signals_logged


class USBacktestRunner:

    def __init__(self, user_id: int, claude_client):
        self.user_id = user_id
        self.claude  = claude_client

    def run_batch(self, batch_size: int = BATCH_SIZE_WEEKDAY) -> int:
        from base.database import get_backtest_full_done, update_backtest_full_progress

        logger.info(f"[US백테스트] 배치 시작 ({batch_size}종목)")
        done    = get_backtest_full_done('US')
        pending = [t for t in _US_UNIVERSE if t not in done]
        if not pending:
            logger.info("[US백테스트] 전체 순환 완료 — 처음부터 재시작")
            pending = _US_UNIVERSE

        batch = pending[:batch_size]
        total = 0

        for ticker in batch:
            try:
                n = run_full_backtest_ticker_us(ticker, self.user_id, self.claude)
                if n > 0:
                    update_backtest_full_progress('US', ticker, datetime.now().strftime('%Y-%m-%d'), n)
                    total += n
                    logger.info(f"[US백테스트] {ticker}: {n}개 신호 완료")
            except Exception as e:
                logger.warning(f"[US백테스트] {ticker} 오류: {e}")
            time.sleep(1)

        logger.info(f"[US백테스트] 배치 완료 — {len(batch)}종목 / {total}신호")
        return total
