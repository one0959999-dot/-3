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
    """S&P500 + 나스닥 보통주(ETF 제외) 유니버스.

    - S&P500: NYSE 대형주 포함 (constituents.csv)
    - 나스닥: 공식 nasdaqlisted.txt 에서 ETF=N 만
    ETF·비S&P NYSE 소형주·우선주는 제외. 데이터 없는 종목은 백테스트서 자동 스킵.
    """
    import requests
    from base.database import get_db_connection
    tickers = set()

    # 1) S&P 500
    try:
        r = requests.get(
            "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv",
            timeout=20)
        for line in r.text.strip().split('\n')[1:]:
            sym = line.split(',')[0].strip().upper()
            if sym and sym.isalpha():
                tickers.add(sym)
    except Exception as e:
        logger.warning(f"[US 백테스트] S&P500 로드 실패: {e}")

    # 2) 나스닥 보통주 (ETF 제외)
    try:
        r = requests.get("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", timeout=20)
        lines = r.text.strip().split('\n')
        hdr = lines[0].split('|')
        i_sym, i_etf, i_test = hdr.index('Symbol'), hdr.index('ETF'), hdr.index('Test Issue')
        for line in lines[1:]:
            p = line.split('|')
            if len(p) <= max(i_etf, i_test):
                continue
            sym = p[i_sym].strip().upper()
            # 보통주만: ETF=N, Test=N, 순수 알파벳(우선주/워런트/유닛 R·U·W 접미 제외)
            if p[i_etf] == 'N' and p[i_test] == 'N' and sym.isalpha() and len(sym) <= 5:
                tickers.add(sym)
    except Exception as e:
        logger.warning(f"[US 백테스트] 나스닥 리스트 로드 실패: {e}")

    tickers = sorted(tickers)
    if tickers:
        try:
            with get_db_connection() as conn:
                conn.execute('CREATE TABLE IF NOT EXISTS us_ticker_cache (ticker TEXT PRIMARY KEY)')
                conn.execute('DELETE FROM us_ticker_cache')
                conn.executemany('INSERT OR REPLACE INTO us_ticker_cache VALUES (?)',
                                 [(t,) for t in tickers])
                conn.commit()
        except Exception:
            pass
        logger.info(f"[US 백테스트] S&P500+나스닥 보통주 {len(tickers)}개 로드")
        return tickers

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
        close = df['close']
        # ① 소급조정 인플레이션 트림 (옛 조정가 >> 최근가). 진짜 우상향주는 트림 안 됨.
        recent_ref = float(close.tail(250).median())
        if recent_ref > 0:
            ok = close <= recent_ref * 10
            if ok.any() and not bool(ok.iloc[0]):
                df = df.loc[ok.idxmax():]
                close = df['close']
        # ② 비정상 일간 급변 이후만 사용
        ret = close.pct_change()
        bad = ret[(ret > 1.5) | (ret < -0.6)]
        if len(bad) > 0:
            last_bad_pos = df.index.get_loc(bad.index[-1])
            df = df.iloc[last_bad_pos + 1:]
        return df
    except Exception:
        return df


# 신호·지표는 공용 모듈(base.signals) 단일 기준 사용 (KR/US/라이브 동일 보장)
from base.signals import calc_indicators as _calc_indicators, detect_signals as _detect_signals


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
                         initial_cash: float = 10_000_000,
                         cost_pct: float = 0.001) -> dict:
    """정직한 포트폴리오 시뮬레이션 (룩어헤드 제거).
    실제 SELL 신호로만 청산 + 거래비용(US 왕복 ~0.1%) + MDD/승률 계산.
    """
    if not signal_records:
        return {}

    date_to_idx = {df.index[i].strftime('%Y-%m-%d'): i for i in range(len(df))}
    closes = df['close'].values

    cash = initial_cash; shares = 0.0; trade_count = 0; wins = 0
    entry_value = None; last_event_idx = 0; equity_curve = []

    def _mark_to_idx(upto_idx):
        for j in range(last_event_idx, min(upto_idx + 1, len(closes))):
            v = closes[j]
            if v is not None and not pd.isna(v):
                equity_curve.append(cash + shares * float(v))

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
        _mark_to_idx(idx); last_event_idx = idx

        if rec['signal_direction'] == 'BUY' and shares == 0:
            invest = cash * (1 - cost_pct)
            shares = invest / price; cash = 0.0; entry_value = invest
            trade_count += 1
        elif rec['signal_direction'] == 'SELL' and shares > 0:
            cash = shares * price * (1 - cost_pct); shares = 0.0
            trade_count += 1
            if entry_value is not None and cash > entry_value:
                wins += 1
            entry_value = None

    _mark_to_idx(len(closes) - 1)
    _lp = closes[-1]
    last_price  = float(_lp) if _lp is not None and not pd.isna(_lp) else 0.0
    final_value = cash + (shares * last_price if shares > 0 else 0)
    return_pct  = round((final_value / initial_cash - 1) * 100, 2)

    mdd = 0.0; peak = initial_cash
    for v in equity_curve:
        if v > peak: peak = v
        if peak > 0:
            dd = (v / peak - 1) * 100
            if dd < mdd: mdd = dd

    sell_trades = sum(1 for r in signal_records if r['signal_direction'] == 'SELL')
    win_rate = round(wins / sell_trades * 100, 1) if sell_trades else None

    return {
        'final_value_10m': round(final_value),
        'return_pct':      return_pct,
        'trade_count':     trade_count,
        'mdd_pct':         round(mdd, 2),
        'win_rate':        win_rate,
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
            if (timing.get('max_gain_pct', 0) or 0) > 500 or (timing.get('max_drawdown_pct', 0) or 0) < -95:
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
        mdd_pct=sim.get('mdd_pct'),
        win_rate=sim.get('win_rate'),
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
