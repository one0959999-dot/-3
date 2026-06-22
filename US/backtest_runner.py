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

_AI_BATCH        = 5
_RATE_LIMIT_SEC  = 1
_SIGNAL_GAP      = 10
_PATH_DAYS       = 120
_MIN_VOL_US      = 500000  # 일평균 최소 거래량 — 미달 시 스킵

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


def _get_all_us_tickers() -> list[str]:
    """SEC EDGAR company_tickers.json → 전체 미국 상장 티커.

    실패 시 하드코딩 유니버스로 폴백. 거래량/데이터 없는 종목은 백테스트 단계에서 자동 스킵.
    """
    import requests
    from base.database import get_db_connection
    try:
        res = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": "lassi-bot backtest contact@example.com"},
            timeout=30,
        )
        data = res.json()
        tickers = []
        seen = set()
        for v in data.values():
            t = str(v.get('ticker', '')).upper().strip()
            # 보통주 위주: '.'/'-' 포함 우선주·워런트 제외
            if t and t not in seen and '.' not in t and '-' not in t:
                seen.add(t)
                tickers.append(t)
        if tickers:
            try:
                with get_db_connection() as conn:
                    conn.execute('CREATE TABLE IF NOT EXISTS us_ticker_cache (ticker TEXT PRIMARY KEY)')
                    conn.executemany('INSERT OR REPLACE INTO us_ticker_cache VALUES (?)',
                                     [(t,) for t in tickers])
                    conn.commit()
            except Exception:
                pass
            logger.info(f"[US 백테스트] EDGAR 전체 티커 {len(tickers)}개 로드")
            return tickers
    except Exception as e:
        logger.warning(f"[US 백테스트] EDGAR 티커 로드 실패: {e}")
    # 폴백: DB 캐시
    try:
        with get_db_connection() as conn:
            rows = conn.execute('SELECT ticker FROM us_ticker_cache').fetchall()
        if rows:
            return [r[0] for r in rows]
    except Exception:
        pass
    return list(_US_UNIVERSE)


def _get_full_history_us(ticker: str, toss_api=None) -> Optional[pd.DataFrame]:
    if toss_api:
        try:
            df = toss_api.get_full_history(ticker)
            if df is not None and not df.empty:
                return df
        except Exception:
            pass
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
        for c in ('open', 'high', 'low', 'close', 'volume'):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors='coerce')
        return df.dropna(subset=['close'])
    except Exception as e:
        logger.debug(f"[US 백테스트] {ticker} yfinance 실패: {e}")
        return None


def _sanitize_prices(df: pd.DataFrame) -> pd.DataFrame:
    """분할/병합 미반영 비정상 가격 급변 구간 제거 (마지막 급변 이후만 사용)."""
    if df is None or 'close' not in df.columns or len(df) < 2:
        return df
    try:
        ret = df['close'].pct_change()
        bad = ret[(ret > 1.5) | (ret < -0.6)]  # 미국은 가격제한 없음 — 더 보수적
        if len(bad) == 0:
            return df
        last_bad_pos = df.index.get_loc(bad.index[-1])
        return df.iloc[last_bad_pos + 1:]
    except Exception:
        return df


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
    raw = df.iloc[idx]['close']
    if raw is None or (isinstance(raw, float) and np.isnan(raw)):
        return {}
    price  = float(raw)
    future = df.iloc[idx + 1: idx + 1 + _PATH_DAYS]
    if future.empty or not price:
        return {}

    closes      = future['close'].dropna().values
    if len(closes) == 0:
        return {}
    pct_changes = [(float(c) / price - 1) * 100 for c in closes]

    peak_idx   = int(np.argmax(closes))
    trough_idx = int(np.argmin(closes))
    max_gain   = round((closes[peak_idx] / price - 1) * 100, 2)
    max_dd     = round((closes[trough_idx] / price - 1) * 100, 2)

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
    if not signal_records:
        return {}

    date_to_idx = {df.index[i].strftime('%Y-%m-%d'): i for i in range(len(df))}

    cash        = initial_cash
    shares      = 0.0
    trade_count = 0
    entry_idx   = None
    days_to_pk  = None

    for rec in sorted(signal_records, key=lambda r: r['trade_date']):
        idx = date_to_idx.get(rec['trade_date'])
        if idx is None:
            continue
        _raw = df.iloc[idx]['close']
        if _raw is None or pd.isna(_raw):
            continue
        price = float(_raw)
        if not price:
            continue

        # days_to_peak 도달 먼저 체크
        if shares > 0 and entry_idx is not None and days_to_pk:
            if idx >= entry_idx + days_to_pk:
                exit_idx   = min(entry_idx + days_to_pk, len(df) - 1)
                _ep = df.iloc[exit_idx]['close']
                if _ep is None or pd.isna(_ep):
                    continue
                exit_price = float(_ep)
                cash   = shares * exit_price
                shares = 0.0
                entry_idx = None
                trade_count += 1

        if rec['signal_direction'] == 'BUY' and shares == 0:
            shares      = cash / price
            cash        = 0.0
            entry_idx   = idx
            days_to_pk  = rec.get('days_to_peak') or 60
            trade_count += 1

        elif rec['signal_direction'] == 'SELL' and shares > 0:
            cash   = shares * price
            shares = 0.0
            entry_idx = None
            trade_count += 1

    _lp = df.iloc[-1]['close']
    last_price  = float(_lp) if _lp is not None and not pd.isna(_lp) else 0.0
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


def _batch_ai_analysis(records: list[dict], ticker: str,
                        ai_client) -> list[tuple[str, str]]:
    sections = []
    for i, r in enumerate(records):
        direction = '매수' if r['signal_direction'] == 'BUY' else '매도'
        sections.append(
            f"[{i+1}] {r['trade_date']} | {direction} 신호: {r['signal_types']}\n"
            f"Market phase: {r.get('market_phase_kr','?')} | Macro: {r.get('macro_str','N/A')[:200]}\n"
            f"RSI {r.get('rsi',0):.1f} | MACD {r.get('macd',0):.4f} | "
            f"BB ${r.get('bb_lower',0):.2f}~${r.get('bb_upper',0):.2f} | Vol {r.get('vol_ratio',0):.0f}%\n"
            f"SMA5:${r.get('sma5',0):.2f} SMA20:${r.get('sma20',0):.2f} "
            f"SMA60:${r.get('sma60',0):.2f} SMA120:${r.get('sma120',0):.2f}\n"
            f"Outcome: max gain {r.get('max_gain_pct',0):+.1f}% in {r.get('days_to_peak','?')}d | "
            f"max drawdown {r.get('max_drawdown_pct',0):+.1f}% in {r.get('days_to_max_drawdown','?')}d | "
            f"recovery: {r.get('days_to_recovery') or 'N/A'}d\n"
        )

    prompt = (
        f"[{ticker}] Past {len(records)} trade signals analysis\n\n"
        + '\n'.join(sections)
        + "\nFor each signal labeled [1][2]...:\n"
        "Line 1: Sector (e.g. Semiconductors, Biotech, Finance, Energy, Consumer, Cloud, EV, Defense, etc.)\n"
        "Lines 2-4: ① Why this signal worked/failed in this market phase "
        "② Optimal entry/exit timing pattern ③ Strategy when this combination repeats\n"
        "Be concise."
    )

    try:
        res = ai_client.generate_content(prompt, temperature=0.2)
        results = [('Other', '')] * len(records)
        parts = re.split(r'\[(\d+)\]', res)
        for j in range(1, len(parts), 2):
            idx = int(parts[j]) - 1
            if 0 <= idx < len(results) and j + 1 < len(parts):
                text  = parts[j + 1].strip()
                lines = text.split('\n', 1)
                sector   = lines[0].strip()[:30] if lines else 'Other'
                analysis = lines[1].strip()[:600] if len(lines) > 1 else text[:600]
                results[idx] = (sector, analysis)
        return results
    except Exception as e:
        logger.warning(f"[US 백테스트] AI 분석 오류: {e}")
        return [('Other', '')] * len(records)


def _buyhold_simulation(df: pd.DataFrame, first_signal_idx: int,
                         initial_cash: float = 10_000_000) -> dict:
    _ep = df.iloc[first_signal_idx]['close']
    _lp = df.iloc[-1]['close']
    if _ep is None or pd.isna(_ep) or _lp is None or pd.isna(_lp):
        return {}
    entry_price = float(_ep)
    last_price  = float(_lp)
    if not entry_price:
        return {}
    shares      = initial_cash / entry_price
    final_value = shares * last_price
    return {
        'buyhold_value_10m':  round(final_value),
        'buyhold_return_pct': round((final_value / initial_cash - 1) * 100, 2),
    }


def run_full_backtest_ticker_us(ticker: str, user_id: int, ai_client,
                                 toss_api=None, skip_ai: bool = False,
                                 news_monitor=None) -> int:
    from base.database import (log_trade_signal_backtest, update_backtest_full_progress,
                                rebuild_sector_phase_stats, rebuild_seasonality_stats,
                                delete_backtest_signals_for_ticker)
    from base.macro_collector import get_macro_for_date, build_macro_context_str
    from base.market_phase import get_phase_for_date

    def _mark_skipped():
        # 상장폐지/데이터 없음/거래량 부족 종목도 완료로 기록 → 무한 재시도 방지
        try:
            update_backtest_full_progress('US', ticker, datetime.now().strftime('%Y-%m-%d'), 0)
        except Exception:
            pass
        return 0

    df = _get_full_history_us(ticker, toss_api)
    if df is None or len(df) < 60:
        return _mark_skipped()

    df = _sanitize_prices(df)
    if df is None or len(df) < 60:
        return _mark_skipped()

    df = _calc_indicators(df).dropna(subset=['rsi', 'macd', 'bb_mid'])
    if len(df) < 60:
        return _mark_skipped()

    avg_vol = df['volume'].tail(60).mean()
    if avg_vol < _MIN_VOL_US:
        logger.debug(f"[US 백테스트] {ticker} 거래량 부족 ({avg_vol:.0f}주) 스킵")
        return _mark_skipped()

    # 섹터 조회 (종목당 1회 — 강세 섹터 로테이션 분석용)
    try:
        from base.sector_lookup import get_sector
        _sector, _ = get_sector(ticker, 'US')
    except Exception:
        _sector = '기타'

    # SEC EDGAR 공시 일괄 수집 (종목당 1회)
    all_disclosures = []
    if news_monitor:
        try:
            all_disclosures = news_monitor.get_all_edgar_disclosures(
                ticker,
                df.index.min().strftime('%Y-%m-%d'),
                df.index.max().strftime('%Y-%m-%d'),
            )
        except Exception as e:
            logger.debug(f"[US 백테스트] {ticker} EDGAR 일괄조회 실패: {e}")

    records = []
    last_buy_idx  = -_SIGNAL_GAP
    last_sell_idx = -_SIGNAL_GAP
    first_sig_idx = None

    for i in range(1, len(df) - 1):
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

            try:
                macro = get_macro_for_date(date)
            except Exception:
                macro = {}
            macro_str  = build_macro_context_str(macro)
            phase_info = get_phase_for_date('US', date, macro)
            support, resistance = _support_resistance(df, i)
            timing = _timing_stats(df, i)

            # 분할/병합 artifact 2차 필터: 120일 내 비현실적 결과 신호 제외
            if (timing.get('max_gain_pct', 0) or 0) > 900 or (timing.get('max_drawdown_pct', 0) or 0) < -95:
                continue

            news_summary = ''
            if all_disclosures:
                try:
                    from ai.news_monitor import NewsMonitor as _NM
                    news_summary = _NM.format_disclosures_around(all_disclosures, date, days=5)
                except Exception:
                    pass

            rec = {
                'user_id': user_id, 'mode': 'US',
                'ticker': ticker, 'stock_name': ticker,
                'trade_date': date,
                'signal_types':     json.dumps(sig_list, ensure_ascii=False),
                'signal_direction': direction,
                'signal_count':     len(sig_list),
                'price': price,
                'rsi':        round(float(row.get('rsi', 0) or 0), 2),
                'macd':       round(float(row.get('macd', 0) or 0), 6),
                'macd_signal':round(float(row.get('macd_signal', 0) or 0), 6),
                'bb_upper':   round(float(row.get('bb_upper', 0) or 0), 4),
                'bb_mid':     round(float(row.get('bb_mid', 0) or 0), 4),
                'bb_lower':   round(float(row.get('bb_lower', 0) or 0), 4),
                'sma5':   round(float(row.get('sma5', 0) or 0), 4),
                'sma20':  round(float(row.get('sma20', 0) or 0), 4),
                'sma60':  round(float(row.get('sma60', 0) or 0), 4),
                'sma120': round(float(row.get('sma120', 0) or 0), 4),
                'vol_ratio': float(row.get('vol_ratio', 0) or 0),
                'support': support, 'resistance': resistance,
                'market_phase':    phase_info.get('phase'),
                'market_phase_kr': phase_info.get('phase_kr'),
                'phase_confidence':phase_info.get('confidence'),
                'macro_str': macro_str,
                'news_summary': news_summary,
                'vix':     macro.get('vix'),
                'usd_krw': macro.get('usd_krw'),
                'us_10y':  macro.get('us_10y'),
                'kr_rate': macro.get('kr_rate'),
                **timing,
            }
            records.append(rec)

            if direction == 'BUY':
                if first_sig_idx is None:
                    first_sig_idx = i
                last_buy_idx = i
            else:
                last_sell_idx = i

    # 멱등성: 재처리 전 기존 신호 삭제 (크래시 후 재시작 시 중복 방지)
    delete_backtest_signals_for_ticker(user_id, 'US', ticker)

    for batch_start in range(0, len(records), _AI_BATCH):
        batch = records[batch_start: batch_start + _AI_BATCH]
        if skip_ai:
            for rec in batch:
                rec['sector']      = _sector
                rec['ai_analysis'] = ''
                log_trade_signal_backtest(rec)
        else:
            analyses = _batch_ai_analysis(batch, ticker, ai_client)
            for rec, (sector, analysis) in zip(batch, analyses):
                rec['sector']      = sector
                rec['ai_analysis'] = analysis
                log_trade_signal_backtest(rec)
            time.sleep(_RATE_LIMIT_SEC)

    sim     = _simulate_portfolio(df, records)
    buyhold = _buyhold_simulation(df, first_sig_idx) if first_sig_idx is not None else {}

    update_backtest_full_progress(
        'US', ticker, datetime.now().strftime('%Y-%m-%d'), len(records),
        final_value_10m=sim.get('final_value_10m'),
        return_pct=sim.get('return_pct'),
        trade_count=sim.get('trade_count', 0),
        buyhold_value_10m=buyhold.get('buyhold_value_10m'),
        buyhold_return_pct=buyhold.get('buyhold_return_pct'),
    )

    try:
        rebuild_sector_phase_stats('US')
        rebuild_seasonality_stats('US')
    except Exception:
        pass

    logger.info(
        f"[US 백테스트] {ticker}: {len(records)}개 신호 | "
        f"신호매매 1000만→{sim.get('final_value_10m',0):,} ({sim.get('return_pct',0):+.1f}%) | "
        f"보유시 1000만→{buyhold.get('buyhold_value_10m',0):,} ({buyhold.get('buyhold_return_pct',0):+.1f}%)"
    )
    return len(records)


class USBacktestRunner:

    def __init__(self, user_id: int, ai_client, toss_api=None, skip_ai: bool = False,
                 use_full_universe: bool = True):
        self.user_id  = user_id
        self.ai       = ai_client
        self.toss     = toss_api
        self.skip_ai  = skip_ai
        self.use_full_universe = use_full_universe
        # EDGAR 공시는 API 키 불필요 (CIK 기반) — dart_key 없이 NewsMonitor 생성
        self.news_monitor = None
        try:
            from ai.news_monitor import NewsMonitor
            self.news_monitor = NewsMonitor('', '', '')
            logger.info("[US 백테스트] NewsMonitor 초기화 완료 (SEC EDGAR 공시 활성)")
        except Exception as e:
            logger.warning(f"[US 백테스트] NewsMonitor 초기화 실패: {e}")

    def run_batch(self, batch_size: int = BATCH_SIZE_WEEKDAY,
                  progress_cb=None) -> int:
        from base.database import get_backtest_full_done

        universe = _get_all_us_tickers() if self.use_full_universe else list(_US_UNIVERSE)
        done    = get_backtest_full_done('US')
        pending = [t for t in universe if t not in done]
        if not pending:
            logger.info("[US 백테스트] 전체 완료 — 처음부터 재시작")
            pending = universe

        total = 0
        for i, ticker in enumerate(pending[:batch_size]):
            if progress_cb:
                progress_cb(f"US:{ticker}", i)
            try:
                n = run_full_backtest_ticker_us(ticker, self.user_id, self.ai, self.toss,
                                                skip_ai=self.skip_ai,
                                                news_monitor=self.news_monitor)
                total += n
            except Exception as e:
                logger.warning(f"[US 백테스트] {ticker} 오류: {e}", exc_info=True)
            time.sleep(1)

        return total
