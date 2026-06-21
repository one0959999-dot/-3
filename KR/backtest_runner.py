import re
import json
import time
import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger('lassi_bot')

BATCH_SIZE_WEEKDAY = 100
BATCH_SIZE_WEEKEND = 200

_AI_BATCH        = 5
_RATE_LIMIT_SEC  = 1
_SIGNAL_GAP      = 10    # 같은 방향 신호 재발생 최소 간격 (일)
_PATH_DAYS       = 120   # 신호 이후 추적 기간
_MIN_VOL_KR      = 50000 # 일평균 최소 거래량 (주) — 미달 시 스킵


def _get_all_kr_tickers() -> list[dict]:
    from base.database import get_db_connection
    # 1) pykrx로 최신 리스트 시도
    try:
        from pykrx import stock as pykrx_stock
        dt = datetime.now()
        while dt.weekday() >= 5:
            dt -= timedelta(days=1)
        date_str = dt.strftime('%Y%m%d')
        kospi  = pykrx_stock.get_market_ticker_list(date_str, market='KOSPI')
        kosdaq = pykrx_stock.get_market_ticker_list(date_str, market='KOSDAQ')
        if kospi or kosdaq:
            name_map = pykrx_stock.get_market_ticker_name
            tickers = []
            for t in kospi + kosdaq:
                try:
                    name = name_map(t)
                except Exception:
                    name = t
                tickers.append({'ticker': t, 'name': name})
            # DB에 캐싱
            with get_db_connection() as conn:
                conn.execute('''CREATE TABLE IF NOT EXISTS kr_ticker_cache
                    (ticker TEXT PRIMARY KEY, name TEXT)''')
                conn.executemany('INSERT OR REPLACE INTO kr_ticker_cache VALUES (?,?)',
                                 [(t['ticker'], t['name']) for t in tickers])
                conn.commit()
            logger.info(f"[KR 백테스트] 종목 리스트 {len(tickers)}개 갱신")
            return tickers
    except Exception as e:
        logger.warning(f"[KR 백테스트] pykrx 조회 실패: {e}")

    # 2) DB 캐시에서 읽기
    try:
        with get_db_connection() as conn:
            rows = conn.execute('SELECT ticker, name FROM kr_ticker_cache').fetchall()
        if rows:
            logger.info(f"[KR 백테스트] DB 캐시에서 종목 {len(rows)}개 로드")
            return [{'ticker': r['ticker'], 'name': r['name']} for r in rows]
    except Exception:
        pass

    logger.warning("[KR 백테스트] 종목 리스트 없음 — pykrx 실패 + DB 캐시 없음")
    return []


def _get_full_history(ticker: str, toss_api=None) -> Optional[pd.DataFrame]:
    """KR 종목 일봉 전체 이력.

    1순위 yfinance(.KS/.KQ): 2000년부터 제공 — 2008 금융위기 등 충격 구간 포함.
    pykrx는 최근 3000거래일(~2014년)만 반환하므로 fallback 으로만 사용.
    """
    # 1순위: yfinance (가장 긴 이력)
    try:
        import yfinance as yf
        for suffix in ['.KS', '.KQ']:
            try:
                df = yf.download(ticker + suffix, period='max', interval='1d',
                                 progress=False, auto_adjust=True)
            except Exception:
                continue
            if df is not None and not df.empty:
                if hasattr(df.columns, 'get_level_values'):
                    df.columns = df.columns.get_level_values(0)
                df.columns = [c.lower() for c in df.columns]
                df.index = pd.to_datetime(df.index)
                for c in ('open', 'high', 'low', 'close', 'volume'):
                    if c in df.columns:
                        df[c] = pd.to_numeric(df[c], errors='coerce')
                df = df.dropna(subset=['close'])
                if len(df) >= 60:
                    return df
    except Exception as e:
        logger.debug(f"[KR 백테스트] {ticker} yfinance 실패: {e}")

    # fallback: pykrx (최근 3000거래일 한정)
    try:
        from pykrx import stock as pykrx_stock
        start = '19900101'
        end = datetime.now().strftime('%Y%m%d')
        raw = pykrx_stock.get_market_ohlcv_by_date(start, end, ticker)
        if raw is not None and not raw.empty:
            raw.columns = [c.lower() for c in raw.columns]
            col_map = {'시가': 'open', '고가': 'high', '저가': 'low', '종가': 'close', '거래량': 'volume'}
            raw = raw.rename(columns=col_map)
            raw.index = pd.to_datetime(raw.index)
            if 'close' in raw.columns:
                return raw.dropna(subset=['close'])
    except Exception as e:
        logger.debug(f"[KR 백테스트] {ticker} pykrx 실패: {e}")
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

    for rec in sorted(signal_records, key=lambda r: r['trade_date']):
        idx = date_to_idx.get(rec['trade_date'])
        if idx is None:
            continue
        price = float(df.iloc[idx]['close'])
        if not price:
            continue

        # days_to_peak 도달 먼저 체크 (SELL 신호 전에 청산)
        if shares > 0 and entry_idx is not None and days_to_pk:
            if idx >= entry_idx + days_to_pk:
                exit_idx   = min(entry_idx + days_to_pk, len(df) - 1)
                exit_price = float(df.iloc[exit_idx]['close'])
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
                              ai_client, toss_api=None, fred_key: str = '',
                              skip_ai: bool = False,
                              rebuild_stats: bool = True,
                              news_monitor=None) -> int:
    from base.database import (log_trade_signal_backtest, update_backtest_full_progress,
                                rebuild_sector_phase_stats, rebuild_seasonality_stats,
                                delete_backtest_signals_for_ticker)
    from base.macro_collector import get_macro_for_date, build_macro_context_str
    from base.market_phase import get_phase_for_date

    def _mark_skipped():
        # 데이터 없음/부족 종목도 완료로 기록 → 무한 재시도 방지(pending에서 제외)
        try:
            update_backtest_full_progress('KR', ticker, datetime.now().strftime('%Y-%m-%d'), 0)
        except Exception:
            pass
        return 0

    df = _get_full_history(ticker, toss_api)
    if df is None or len(df) < 60:
        return _mark_skipped()

    df = _calc_indicators(df).dropna(subset=['rsi', 'macd', 'bb_mid'])
    if len(df) < 60:
        return _mark_skipped()

    # 거래량 필터 — 일평균 거래량 미달 종목 스킵
    avg_vol = df['volume'].tail(60).mean()
    if avg_vol < _MIN_VOL_KR:
        logger.debug(f"[KR 백테스트] {ticker} 거래량 부족 ({avg_vol:.0f}주) 스킵")
        return _mark_skipped()

    # DART 공시 일괄 수집 (종목당 1회 — 신호별 호출 제거로 속도 대폭 개선)
    all_disclosures = []
    if news_monitor:
        try:
            all_disclosures = news_monitor.get_all_disclosures(
                ticker,
                df.index.min().strftime('%Y-%m-%d'),
                df.index.max().strftime('%Y-%m-%d'),
            )
        except Exception as e:
            logger.debug(f"[KR 백테스트] {ticker} 공시 일괄조회 실패: {e}")

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

            if first_sig_idx is None and direction == 'BUY':
                first_sig_idx = i

            try:
                macro = get_macro_for_date(date, fred_key=fred_key)
            except Exception:
                macro = {}
            macro_str  = build_macro_context_str(macro)
            phase_info = get_phase_for_date('KR', date, macro)
            support, resistance = _support_resistance(df, i)
            timing = _timing_stats(df, i)

            # DART 공시: 일괄 수집분에서 신호일 ±5일 메모리 추출
            news_summary = ''
            if all_disclosures:
                try:
                    from ai.news_monitor import NewsMonitor as _NM
                    news_summary = _NM.format_disclosures_around(all_disclosures, date, days=5)
                except Exception:
                    pass

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
                'news_summary': news_summary,
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

    # 멱등성: 재처리 전 기존 신호 삭제 (크래시 후 재시작 시 중복 방지)
    delete_backtest_signals_for_ticker(user_id, 'KR', ticker)

    for batch_start in range(0, len(records), _AI_BATCH):
        batch = records[batch_start: batch_start + _AI_BATCH]
        if skip_ai:
            for rec in batch:
                rec['sector']      = '기타'
                rec['ai_analysis'] = ''
                log_trade_signal_backtest(rec)
        else:
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

    # 섹터 로테이션 / 계절성 통계 갱신 (병렬 모드에서는 호출하지 않음)
    if rebuild_stats:
        try:
            rebuild_sector_phase_stats('KR')
            rebuild_seasonality_stats('KR')
        except Exception:
            pass

    logger.info(
        f"[KR 백테스트] {stock_name}({ticker}): {len(records)}개 신호 | "
        f"신호매매 1000만→{sim.get('final_value_10m',0):,}원 ({sim.get('return_pct',0):+.1f}%) | "
        f"보유시 1000만→{buyhold.get('buyhold_value_10m',0):,}원 ({buyhold.get('buyhold_return_pct',0):+.1f}%)"
    )
    return len(records)


class BacktestRunner:

    def __init__(self, user_id: int, ai_client, toss_api=None, fred_key: str = '',
                 skip_ai: bool = False, dart_key: str = '',
                 naver_id: str = '', naver_secret: str = ''):
        self.user_id  = user_id
        self.ai       = ai_client
        self.toss     = toss_api
        self.fred_key = fred_key
        self.skip_ai  = skip_ai
        self.news_monitor = None
        if dart_key:
            try:
                from ai.news_monitor import NewsMonitor
                self.news_monitor = NewsMonitor(dart_key, naver_id or '', naver_secret or '')
                logger.info("[KR 백테스트] NewsMonitor 초기화 완료 (DART 공시 수집 활성)")
            except Exception as e:
                logger.warning(f"[KR 백테스트] NewsMonitor 초기화 실패: {e}")

    def run_batch(self, batch_size: int = BATCH_SIZE_WEEKDAY,
                  progress_cb=None) -> int:
        from base.database import get_backtest_full_done

        all_tickers = _get_all_kr_tickers()
        if not all_tickers:
            return 0

        done    = get_backtest_full_done('KR')
        pending = [t for t in all_tickers if t['ticker'] not in done]
        if not pending:
            logger.info("[KR 백테스트] 전체 완료 — 처음부터 재시작")
            pending = all_tickers

        PARALLEL_WORKERS = 1
        batch = pending[:batch_size]
        total = 0

        def _process(item):
            try:
                return run_full_backtest_ticker(
                    item['ticker'], item['name'],
                    self.user_id, self.ai, self.toss, self.fred_key,
                    skip_ai=self.skip_ai, rebuild_stats=False,
                    news_monitor=self.news_monitor,
                )
            except Exception as e:
                logger.warning(f"[KR 백테스트] {item['ticker']} 오류: {e}")
                return 0

        from concurrent.futures import ThreadPoolExecutor, as_completed
        done_count = 0
        with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as ex:
            futures = {ex.submit(_process, item): item for item in batch}
            for future in as_completed(futures):
                item = futures[future]
                done_count += 1
                if progress_cb:
                    progress_cb(f"KR:{item['ticker']}({item['name']})", done_count)
                total += future.result() or 0

        # 전체 완료 후 통계 1회 재빌드
        try:
            rebuild_sector_phase_stats('KR')
            rebuild_seasonality_stats('KR')
        except Exception:
            pass

        return total
