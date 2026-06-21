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

_AI_BATCH        = 5
_RATE_LIMIT_SEC  = 4
_SIGNAL_GAP      = 10    # 같은 방향 신호 재발생 최소 간격 (일)
_PATH_DAYS       = 120   # 신호 이후 추적 기간
_MIN_VOL_KR      = 50000 # 일평균 최소 거래량 (주) — 미달 시 스킵


def _get_all_kr_tickers() -> list[dict]:
    try:
        from pykrx import stock as pykrx_stock
        dt = datetime.now()
        weekday = dt.weekday()
        if weekday == 5:
            dt = dt.replace(day=dt.day - 1)
        elif weekday == 6:
            dt = dt.replace(day=dt.day - 2)
        date_str = dt.strftime('%Y%m%d')
        kospi  = pykrx_stock.get_market_ticker_list(date_str, market='KOSPI')
        kosdaq = pykrx_stock.get_market_ticker_list(date_str, market='KOSDAQ')
        tickers = []
        for t in kospi + kosdaq:
            name = pykrx_stock.get_market_ticker_name(t)
            tickers.append({'ticker': t, 'name': name})
        return tickers
    except Exception as e:
        logger.warning(f"[KR 백테스트] 종목 리스트 조회 실패: {e}")
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
                if hasattr(df.columns, 'get_level_values'):
                    df.columns = df.columns.get_level_values(0)
                df.columns = [c.lower() for c in df.columns]
                df.index = pd.to_datetime(df.index)
                return df.dropna(subset=['close'])
    except Exception as e:
        logger.debug(f"[KR 백테스트] {ticker} yfinance 실패: {e}")
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


def _detect_signals(row: pd.Series, prev_row: pd.Series) -> list[str]:
    """신호 이벤트 감지 — 크로스/터치 발생 시점만."""
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


def _timing_stats(df: pd.DataFrame, idx: int) -> dict:
    """신호 이후 실제 매매 타이밍 데이터 계산."""
    price  = float(df.iloc[idx]['close'])
    future = df.iloc[idx + 1: idx + 1 + _PATH_DAYS]
    if future.empty or not price:
        return {}

    closes      = future['close'].values
    pct_changes = [(c / price - 1) * 100 for c in closes]

    peak_idx    = int(np.argmax(closes))
    trough_idx  = int(np.argmin(closes))
    max_gain    = round((closes[peak_idx] / price - 1) * 100, 2)
    max_dd      = round((closes[trough_idx] / price - 1) * 100, 2)

    recovery = None
    if closes[trough_idx] < price:
        for j, c in enumerate(closes[trough_idx:], start=trough_idx):
            if c >= price:
                recovery = j
                break

    return {
        'days_to_peak':         peak_idx + 1,
        'max_gain_pct':         max_gain,
        'days_to_max_drawdown': trough_idx + 1,
        'max_drawdown_pct':     max_dd,
        'days_to_recovery':     recovery,
        'price_path_json':      json.dumps([round(p, 2) for p in pct_changes]),
    }


def _simulate_portfolio(df: pd.DataFrame, signal_records: list[dict],
                         initial_cash: float = 10_000_000) -> dict:
    """신호 기반 + days_to_peak 병합 기준 포트폴리오 시뮬레이션."""
    if not signal_records:
        return {}

    date_to_idx = {df.index[i].strftime('%Y-%m-%d'): i for i in range(len(df))}

    buy_signals  = sorted([r for r in signal_records if r['signal_direction'] == 'BUY'],
                           key=lambda r: r['trade_date'])
    sell_signals = sorted([r for r in signal_records if r['signal_direction'] == 'SELL'],
                           key=lambda r: r['trade_date'])

    cash        = initial_cash
    shares      = 0.0
    trade_count = 0
    entry_idx   = None
    days_to_pk  = None

    all_signals = sorted(signal_records, key=lambda r: r['trade_date'])

    for rec in all_signals:
        idx = date_to_idx.get(rec['trade_date'])
        if idx is None:
            continue
        price = float(df.iloc[idx]['close'])
        if not price:
            continue

        if rec['signal_direction'] == 'BUY' and shares == 0:
            shares      = cash / price
            cash        = 0.0
            entry_idx   = idx
            days_to_pk  = rec.get('days_to_peak') or 60
            trade_count += 1

        elif rec['signal_direction'] == 'SELL' and shares > 0:
            # SELL 신호 도달 또는 days_to_peak 이미 지난 경우
            peak_exit_idx = (entry_idx + days_to_pk) if entry_idx is not None else idx
            exit_idx = min(idx, peak_exit_idx, len(df) - 1)
            exit_price = float(df.iloc[exit_idx]['close'])
            cash   = shares * exit_price
            shares = 0.0
            entry_idx = None
            trade_count += 1

        # days_to_peak 도달 체크 (SELL 신호 없어도 청산)
        if shares > 0 and entry_idx is not None and days_to_pk:
            if idx >= entry_idx + days_to_pk:
                exit_idx   = min(entry_idx + days_to_pk, len(df) - 1)
                exit_price = float(df.iloc[exit_idx]['close'])
                cash   = shares * exit_price
                shares = 0.0
                entry_idx = None
                trade_count += 1

    # 마지막 보유 포지션은 최종 종가로 평가
    last_price = float(df.iloc[-1]['close'])
    final_value = cash + (shares * last_price if shares > 0 else 0)
    return_pct  = round((final_value / initial_cash - 1) * 100, 2)

    return {
        'final_value_10m': round(final_value),
        'return_pct':      return_pct,
        'trade_count':     trade_count,
    }


def _support_resistance(df: pd.DataFrame, idx: int):
    hist = df.iloc[max(0, idx - 60): idx + 1]
    support    = float(hist['low'].rolling(20).min().dropna().iloc[-1])  if len(hist) >= 20 else 0
    resistance = float(hist['high'].rolling(20).max().dropna().iloc[-1]) if len(hist) >= 20 else 0
    return support, resistance


def _batch_ai_analysis(records: list[dict], ticker: str, stock_name: str,
                        ai_client) -> list[tuple[str, str]]:
    """records 최대 _AI_BATCH개를 한 번의 AI 호출로 분석.
    반환: [(sector, analysis), ...]"""
    sections = []
    for i, r in enumerate(records):
        direction = '매수' if r['signal_direction'] == 'BUY' else '매도'
        sections.append(
            f"[{i+1}] {r['trade_date']} | {direction} 신호: {r['signal_types']}\n"
            f"시장국면: {r.get('market_phase_kr','?')} | 매크로: {r.get('macro_str','정보없음')[:200]}\n"
            f"RSI {r.get('rsi',0):.1f} | MACD {r.get('macd',0):.4f} | "
            f"BB {r.get('bb_lower',0):,.0f}~{r.get('bb_upper',0):,.0f} | 거래량 {r.get('vol_ratio',0):.0f}%\n"
            f"SMA5:{r.get('sma5',0):,.0f} SMA20:{r.get('sma20',0):,.0f} "
            f"SMA60:{r.get('sma60',0):,.0f} SMA120:{r.get('sma120',0):,.0f}\n"
            f"이후결과: 최대수익 {r.get('max_gain_pct',0):+.1f}%({r.get('days_to_peak','?')}일) | "
            f"최대낙폭 {r.get('max_drawdown_pct',0):+.1f}%({r.get('days_to_max_drawdown','?')}일) | "
            f"회복까지 {r.get('days_to_recovery') or '미회복'}일\n"
        )

    prompt = (
        f"[{stock_name}({ticker})] 과거 매매 신호 {len(records)}건 분석\n\n"
        + '\n'.join(sections)
        + "\n각 건에 대해 번호([1][2]...)로 구분해서 답변:\n"
        "첫 줄: 섹터명 (예: 반도체, 바이오, 금융, 에너지, 소비재, IT서비스, 화학, 자동차, 방산, 부동산 등)\n"
        "이후 3~4줄: ① 이 신호가 이 국면에서 유효했던/실패한 이유 "
        "② 최적 진입/청산 타이밍 패턴 ③ 다음에 이 조합 재현 시 전략\n"
        "간결하게."
    )

    try:
        res = ai_client.generate_content(prompt, temperature=0.2)
        results = [('기타', '')] * len(records)
        parts = re.split(r'\[(\d+)\]', res)
        for j in range(1, len(parts), 2):
            idx = int(parts[j]) - 1
            if 0 <= idx < len(results) and j + 1 < len(parts):
                text  = parts[j + 1].strip()
                lines = text.split('\n', 1)
                sector   = lines[0].strip()[:30] if lines else '기타'
                analysis = lines[1].strip()[:600] if len(lines) > 1 else text[:600]
                results[idx] = (sector, analysis)
        return results
    except Exception as e:
        logger.warning(f"[KR 백테스트] AI 분석 오류: {e}")
        return [('기타', '')] * len(records)


def _buyhold_simulation(df: pd.DataFrame, first_signal_idx: int,
                         initial_cash: float = 10_000_000) -> dict:
    """첫 신호 시점 매수 → 현재까지 보유 시 수익."""
    entry_price = float(df.iloc[first_signal_idx]['close'])
    last_price  = float(df.iloc[-1]['close'])
    if not entry_price:
        return {}
    shares      = initial_cash / entry_price
    final_value = shares * last_price
    return {
        'buyhold_value_10m':  round(final_value),
        'buyhold_return_pct': round((final_value / initial_cash - 1) * 100, 2),
    }


def run_full_backtest_ticker(ticker: str, stock_name: str, user_id: int,
                              ai_client, toss_api=None, fred_key: str = '') -> int:
    from base.database import (log_trade_signal_backtest, update_backtest_full_progress,
                                rebuild_sector_phase_stats, rebuild_seasonality_stats)
    from base.macro_collector import get_macro_for_date, build_macro_context_str
    from base.market_phase import get_phase_for_date

    df = _get_full_history(ticker, toss_api)
    if df is None or len(df) < 60:
        return 0

    df = _calc_indicators(df).dropna(subset=['rsi', 'macd', 'bb_mid'])
    if len(df) < 60:
        return 0

    # 거래량 필터 — 일평균 거래량 미달 종목 스킵
    avg_vol = df['volume'].tail(60).mean()
    if avg_vol < _MIN_VOL_KR:
        logger.debug(f"[KR 백테스트] {ticker} 거래량 부족 ({avg_vol:.0f}주) 스킵")
        return 0

    records = []
    last_buy_idx  = -_SIGNAL_GAP
    last_sell_idx = -_SIGNAL_GAP
    first_sig_idx = None

    for i in range(1, len(df) - _PATH_DAYS):
        row  = df.iloc[i]
        prev = df.iloc[i - 1]
        sigs = _detect_signals(row, prev)
        if not sigs:
            continue

        buy_sigs  = [s for s in sigs if 'BUY'  in s]
        sell_sigs = [s for s in sigs if 'SELL' in s]

        for direction, sig_list, last_idx_ref in [
            ('BUY',  buy_sigs,  last_buy_idx),
            ('SELL', sell_sigs, last_sell_idx),
        ]:
            if not sig_list:
                continue
            if i - last_idx_ref < _SIGNAL_GAP:
                continue

            date  = df.index[i].strftime('%Y-%m-%d')
            price = float(row['close'])
            if not price:
                continue

            if first_sig_idx is None:
                first_sig_idx = i

            try:
                macro = get_macro_for_date(date, fred_key=fred_key)
            except Exception:
                macro = {}
            macro_str  = build_macro_context_str(macro)
            phase_info = get_phase_for_date('KR', date, macro)
            support, resistance = _support_resistance(df, i)
            timing = _timing_stats(df, i)

            rec = {
                'user_id': user_id, 'mode': 'KR',
                'ticker': ticker, 'stock_name': stock_name,
                'trade_date': date,
                'signal_types':     json.dumps(sig_list, ensure_ascii=False),
                'signal_direction': direction,
                'signal_count':     len(sig_list),
                'price': price,
                'rsi':        round(float(row.get('rsi', 0) or 0), 2),
                'macd':       round(float(row.get('macd', 0) or 0), 6),
                'macd_signal':round(float(row.get('macd_signal', 0) or 0), 6),
                'bb_upper':   round(float(row.get('bb_upper', 0) or 0), 2),
                'bb_mid':     round(float(row.get('bb_mid', 0) or 0), 2),
                'bb_lower':   round(float(row.get('bb_lower', 0) or 0), 2),
                'sma5':   round(float(row.get('sma5', 0) or 0), 2),
                'sma20':  round(float(row.get('sma20', 0) or 0), 2),
                'sma60':  round(float(row.get('sma60', 0) or 0), 2),
                'sma120': round(float(row.get('sma120', 0) or 0), 2),
                'vol_ratio': float(row.get('vol_ratio', 0) or 0),
                'support': support, 'resistance': resistance,
                'market_phase':    phase_info.get('phase'),
                'market_phase_kr': phase_info.get('phase_kr'),
                'phase_confidence':phase_info.get('confidence'),
                'macro_str': macro_str,
                'vix':     macro.get('vix'),
                'usd_krw': macro.get('usd_krw'),
                'us_10y':  macro.get('us_10y'),
                'kr_rate': macro.get('kr_rate'),
                **timing,
            }
            records.append(rec)

            if direction == 'BUY':
                last_buy_idx = i
            else:
                last_sell_idx = i

    for batch_start in range(0, len(records), _AI_BATCH):
        batch    = records[batch_start: batch_start + _AI_BATCH]
        analyses = _batch_ai_analysis(batch, ticker, stock_name, ai_client)
        for rec, (sector, analysis) in zip(batch, analyses):
            rec['sector']      = sector
            rec['ai_analysis'] = analysis
            log_trade_signal_backtest(rec)
        time.sleep(_RATE_LIMIT_SEC)

    sim     = _simulate_portfolio(df, records)
    buyhold = _buyhold_simulation(df, first_sig_idx) if first_sig_idx is not None else {}

    update_backtest_full_progress(
        'KR', ticker, datetime.now().strftime('%Y-%m-%d'), len(records),
        final_value_10m=sim.get('final_value_10m'),
        return_pct=sim.get('return_pct'),
        trade_count=sim.get('trade_count', 0),
        buyhold_value_10m=buyhold.get('buyhold_value_10m'),
        buyhold_return_pct=buyhold.get('buyhold_return_pct'),
    )

    # 섹터 로테이션 / 계절성 통계 갱신
    try:
        rebuild_sector_phase_stats('KR')
        rebuild_seasonality_stats('KR')
    except Exception:
        pass

    logger.info(
        f"[KR 백테스트] {stock_name}({ticker}): {len(records)}개 신호 | "
        f"신호매매 1000만→{sim.get('final_value_10m',0):,}원 ({sim.get('return_pct',0):+.1f}%) | "
        f"보유시 {buyhold.get('buyhold_return_pct',0):+.1f}%"
    )
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
                total += n
            except Exception as e:
                logger.warning(f"[KR 백테스트] {item['ticker']} 오류: {e}")
            time.sleep(1)

        return total
