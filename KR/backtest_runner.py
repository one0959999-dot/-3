import time
import logging
import pandas as pd
import numpy as np
from datetime import datetime
from typing import Optional

logger = logging.getLogger('lassi_bot')

BATCH_SIZE_WEEKDAY = 100
BATCH_SIZE_WEEKEND = 200


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


def _get_full_history_toss(ticker: str, toss_api) -> Optional[pd.DataFrame]:
    try:
        df = toss_api.get_full_history(ticker)
        if df is not None and not df.empty:
            return df
    except Exception:
        pass
    return _get_full_history_yfinance(ticker)


def _get_full_history_yfinance(ticker: str) -> Optional[pd.DataFrame]:
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
        logger.debug(f"[백테스트] {ticker} yfinance 조회 실패: {e}")
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

    df['high_20'] = high.rolling(20).max()
    df['low_20']  = low.rolling(20).min()

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

    close     = row.get('close')
    bb_l      = row.get('bb_lower')
    bb_u      = row.get('bb_upper')
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

    high_20   = row.get('high_20')
    low_20    = row.get('low_20')
    prev_high = prev_row.get('high_20')
    prev_low  = prev_row.get('low_20')
    if all(pd.notna(x) for x in [close, high_20, low_20, prev_high, prev_low, prev_close]):
        if prev_close < prev_high and close >= high_20:
            signals.append('BREAK_BUY')
        elif prev_close > prev_low and close <= low_20:
            signals.append('BREAK_SELL')

    return signals


def _calc_support_resistance(hist: pd.DataFrame) -> tuple[float, float]:
    try:
        lows   = hist['low'].rolling(20).min().dropna()
        highs  = hist['high'].rolling(20).max().dropna()
        support    = float(lows.iloc[-1]) if not lows.empty else 0
        resistance = float(highs.iloc[-1]) if not highs.empty else 0
        return support, resistance
    except Exception:
        return 0, 0


def _calc_fibonacci(support: float, resistance: float) -> tuple[float, float, float]:
    if not support or not resistance or resistance <= support:
        return 0, 0, 0
    rng = resistance - support
    return (
        round(resistance - rng * 0.382, 2),
        round(resistance - rng * 0.500, 2),
        round(resistance - rng * 0.618, 2),
    )


def _future_stats(df: pd.DataFrame, idx: int, days: int) -> dict:
    end = idx + days
    if end >= len(df):
        end = len(df) - 1
    future = df.iloc[idx+1:end+1]
    if future.empty:
        return {}
    price = df.iloc[idx]['close']
    min_p  = float(future['close'].min())
    max_p  = float(future['close'].max())
    min_d  = int(future['close'].argmin()) + 1
    max_d  = int(future['close'].argmax()) + 1
    pnl    = round((df.iloc[end]['close'] / price - 1) * 100, 2)
    return {
        f'min_price_{days}d': min_p,
        f'max_price_{days}d': max_p,
        f'days_to_min_{days}d': min_d,
        f'days_to_max_{days}d': max_d,
        f'pnl_{days}d': pnl,
    }


def _optimal_zones(price: float, support: float, resistance: float,
                   bb_lower: float, bb_upper: float, fib_618: float, fib_382: float) -> tuple[str, str]:
    buy_candidates  = [x for x in [support, bb_lower, fib_618] if x and x > 0]
    sell_candidates = [x for x in [resistance, bb_upper, fib_382] if x and x > 0]
    buy_zone  = f"{min(buy_candidates):,.0f}~{max(buy_candidates):,.0f}" if buy_candidates else ""
    sell_zone = f"{min(sell_candidates):,.0f}~{max(sell_candidates):,.0f}" if sell_candidates else ""
    return buy_zone, sell_zone


def run_full_backtest_ticker(ticker: str, stock_name: str, user_id: int,
                              claude_client, toss_api=None, fred_key: str = '') -> int:
    from base.database import load_ai_rules, log_backtest_signal
    from base.macro_collector import get_macro_for_date, build_macro_context_str

    df = _get_full_history_toss(ticker, toss_api) if toss_api else _get_full_history_yfinance(ticker)
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

        macro = get_macro_for_date(date_str, fred_key=fred_key)
        macro_str = build_macro_context_str(macro)

        hist = df.iloc[:i+1]
        support, resistance = _calc_support_resistance(hist)
        fib_382, fib_500, fib_618 = _calc_fibonacci(support, resistance)
        buy_zone, sell_zone = _optimal_zones(
            price, support, resistance,
            float(row.get('bb_lower') or 0), float(row.get('bb_upper') or 0),
            fib_618, fib_382
        )

        stats_5d  = _future_stats(df, i, 5)
        stats_20d = _future_stats(df, i, 20)

        for sig_type in signal_types:
            signal = 'BUY' if 'BUY' in sig_type else 'SELL'

            context = (
                f"[종목] {stock_name}({ticker}) | 날짜: {date_str} | 현재가: {price:,.0f}원\n"
                f"[기술지표] RSI:{row.get('rsi', 0):.1f} | MACD:{row.get('macd', 0):.2f} | "
                f"볼린저 {row.get('bb_lower', 0):,.0f}~{row.get('bb_upper', 0):,.0f}\n"
                f"SMA5:{row.get('sma5', 0):,.0f} SMA20:{row.get('sma20', 0):,.0f} "
                f"SMA60:{row.get('sma60', 0):,.0f} SMA120:{row.get('sma120', 0):,.0f}\n"
                f"거래량: 평소대비 {row.get('vol_ratio', 0):.0f}% | 신호: {sig_type}\n"
                f"지지: {support:,.0f} | 저항: {resistance:,.0f} | "
                f"피보나치 38.2%:{fib_382:,.0f} 50%:{fib_500:,.0f} 61.8%:{fib_618:,.0f}\n"
                f"매수구간: {buy_zone} | 매도구간: {sell_zone}\n"
                f"{macro_str}"
            )

            try:
                result = claude_client.ai_approve_trade(
                    signal, stock_name, ticker, price, sig_type,
                    row.get('rsi', 0), [], [], ai_rules,
                    context=context,
                    portfolio_context="[백테스트 모드]"
                )
                decision   = result[0]
                reason     = result[1]
                confidence = result[2] if len(result) > 2 else 75
            except Exception as e:
                logger.debug(f"[백테스트] {ticker} AI 오류: {e}")
                time.sleep(2)
                continue

            row_data = {
                'user_id': user_id, 'mode': 'KR',
                'ticker': ticker, 'stock_name': stock_name,
                'trade_date': date_str, 'signal': signal, 'signal_type': sig_type,
                'price': price,
                'ai_decision': 'CONFIRM' if decision else 'REJECT',
                'confidence': confidence, 'ai_reason': reason[:400],
                'macro_date': date_str,
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
                'vol_ratio':  float(row.get('vol_ratio', 0)),
                'support': support, 'resistance': resistance,
                'fib_382': fib_382, 'fib_500': fib_500, 'fib_618': fib_618,
                'optimal_buy_zone':  buy_zone,
                'optimal_sell_zone': sell_zone,
                **stats_5d, **stats_20d,
            }
            log_backtest_signal(row_data)
            signals_logged += 1
            time.sleep(0.15)

    return signals_logged


class BacktestRunner:

    def __init__(self, user_id: int, claude_client, toss_api=None, fred_key: str = ''):
        self.user_id  = user_id
        self.claude   = claude_client
        self.toss     = toss_api
        self.fred_key = fred_key

    def run_batch(self, batch_size: int = BATCH_SIZE_WEEKDAY) -> int:
        from base.database import get_backtest_full_done, update_backtest_full_progress

        logger.info(f"[백테스트] 배치 시작 ({batch_size}종목)")
        all_tickers = _get_all_kr_tickers()
        if not all_tickers:
            logger.warning("[백테스트] 종목 리스트 조회 실패")
            return 0

        done = get_backtest_full_done('KR')
        pending = [t for t in all_tickers if t['ticker'] not in done]
        if not pending:
            logger.info("[백테스트] 전체 순환 완료 — 처음부터 재시작")
            pending = all_tickers

        batch = pending[:batch_size]
        total = 0

        for item in batch:
            ticker = item['ticker']
            name   = item['name']
            try:
                n = run_full_backtest_ticker(ticker, name, self.user_id, self.claude, self.toss, self.fred_key)
                if n > 0:
                    update_backtest_full_progress(
                        'KR', ticker, datetime.now().strftime('%Y-%m-%d'), n
                    )
                    total += n
                    logger.info(f"[백테스트] {name}({ticker}): {n}개 신호 완료")
            except Exception as e:
                logger.warning(f"[백테스트] {name}({ticker}) 오류: {e}")
            time.sleep(1)

        logger.info(f"[백테스트] 배치 완료 — {len(batch)}종목 / {total}신호")
        return total
