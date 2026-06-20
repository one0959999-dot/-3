import logging
import pandas as pd
from datetime import datetime, timedelta
from base.database import save_macro_snapshot, get_macro_snapshot

logger = logging.getLogger('lassi_bot')

_FOMC_DATES = {
    '2020-01-29','2020-03-03','2020-03-15','2020-04-29','2020-06-10',
    '2020-07-29','2020-09-16','2020-11-05','2020-12-16',
    '2021-01-27','2021-03-17','2021-04-28','2021-06-16','2021-07-28',
    '2021-09-22','2021-11-03','2021-12-15',
    '2022-01-26','2022-03-16','2022-05-04','2022-06-15','2022-07-27',
    '2022-09-21','2022-11-02','2022-12-14',
    '2023-02-01','2023-03-22','2023-05-03','2023-06-14','2023-07-26',
    '2023-09-20','2023-11-01','2023-12-13',
    '2024-01-31','2024-03-20','2024-05-01','2024-06-12','2024-07-31',
    '2024-09-18','2024-11-07','2024-12-18',
    '2025-01-29','2025-03-19','2025-05-07','2025-06-18',
}

_CPI_DATES = {
    '2022-01-12','2022-02-10','2022-03-10','2022-04-12','2022-05-11',
    '2022-06-10','2022-07-13','2022-08-10','2022-09-13','2022-10-13',
    '2022-11-10','2022-12-13',
    '2023-01-12','2023-02-14','2023-03-14','2023-04-12','2023-05-10',
    '2023-06-13','2023-07-12','2023-08-10','2023-09-13','2023-10-12',
    '2023-11-14','2023-12-12',
    '2024-01-11','2024-02-13','2024-03-12','2024-04-10','2024-05-15',
    '2024-06-12','2024-07-11','2024-08-14','2024-09-11','2024-10-10',
    '2024-11-13','2024-12-11',
    '2025-01-15','2025-02-12','2025-03-12','2025-04-10','2025-05-13',
    '2025-06-11',
}

_macro_cache: dict = {}


def _yf_close(ticker: str, date_str: str, window: int = 5) -> float | None:
    try:
        import yfinance as yf
        end = datetime.strptime(date_str, '%Y-%m-%d') + timedelta(days=window)
        start = datetime.strptime(date_str, '%Y-%m-%d') - timedelta(days=window)
        df = yf.download(ticker, start=start.strftime('%Y-%m-%d'),
                         end=end.strftime('%Y-%m-%d'), progress=False, auto_adjust=True)
        if df.empty:
            return None
        target = pd.Timestamp(date_str)
        idx = df.index.searchsorted(target, side='left')
        if idx >= len(df):
            idx = len(df) - 1
        return float(df['Close'].iloc[idx])
    except Exception:
        return None


def _yf_chg(ticker: str, date_str: str) -> float | None:
    try:
        import yfinance as yf
        end = datetime.strptime(date_str, '%Y-%m-%d') + timedelta(days=5)
        start = datetime.strptime(date_str, '%Y-%m-%d') - timedelta(days=5)
        df = yf.download(ticker, start=start.strftime('%Y-%m-%d'),
                         end=end.strftime('%Y-%m-%d'), progress=False, auto_adjust=True)
        if df.empty or len(df) < 2:
            return None
        target = pd.Timestamp(date_str)
        idx = df.index.searchsorted(target, side='left')
        if idx >= len(df):
            idx = len(df) - 1
        if idx == 0:
            return 0.0
        c = float(df['Close'].iloc[idx])
        p = float(df['Close'].iloc[idx - 1])
        return round((c / p - 1) * 100, 2) if p else None
    except Exception:
        return None


def get_macro_for_date(date_str: str) -> dict:
    if date_str in _macro_cache:
        return _macro_cache[date_str]

    cached = get_macro_snapshot(date_str)
    if cached:
        _macro_cache[date_str] = cached
        return cached

    data = {'date': date_str}

    try:
        import yfinance as yf

        data['sp500_chg']   = _yf_chg('^GSPC', date_str)
        data['nasdaq_chg']  = _yf_chg('^IXIC', date_str)
        data['nikkei_chg']  = _yf_chg('^N225', date_str)
        data['shanghai_chg']= _yf_chg('000001.SS', date_str)
        data['vix']         = _yf_close('^VIX', date_str)
        data['dxy']         = _yf_close('DX-Y.NYB', date_str)
        data['usd_krw']     = _yf_close('KRW=X', date_str)
        data['wti']         = _yf_close('CL=F', date_str)
        data['gold']        = _yf_close('GC=F', date_str)
        data['copper']      = _yf_close('HG=F', date_str)
        data['sox_chg']     = _yf_chg('^SOX', date_str)
        data['us_10y']      = _yf_close('^TNX', date_str)
        data['us_2y']       = _yf_close('^IRX', date_str)

        us_10y = data.get('us_10y') or 0
        us_2y  = data.get('us_2y') or 0
        data['yield_spread'] = round(us_10y - us_2y, 3) if us_10y and us_2y else None

        try:
            from pykrx import stock as pykrx_stock
            date_fmt = date_str.replace('-', '')
            kospi_df = pykrx_stock.get_index_ohlcv_by_date(date_fmt, date_fmt, '1001')
            if not kospi_df.empty:
                data['kospi_close'] = float(kospi_df['종가'].iloc[-1])

                hist = pykrx_stock.get_index_ohlcv_by_date(
                    (datetime.strptime(date_str, '%Y-%m-%d') - timedelta(days=300)).strftime('%Y%m%d'),
                    date_fmt, '1001'
                )
                if len(hist) >= 200:
                    ma200 = float(hist['종가'].rolling(200).mean().iloc[-1])
                    data['kospi_vs_ma200'] = round((data['kospi_close'] / ma200 - 1) * 100, 2)
                if len(hist) >= 252:
                    high52 = float(hist['종가'].rolling(252).max().iloc[-1])
                    low52  = float(hist['종가'].rolling(252).min().iloc[-1])
                    rng = high52 - low52
                    data['kospi_52w_pct'] = round((data['kospi_close'] - low52) / rng * 100, 1) if rng else 50

            inv_df = pykrx_stock.get_market_trading_value_by_date(date_fmt, date_fmt, '1001')
            if not inv_df.empty:
                data['foreign_net_buy']     = float(inv_df['외국인합계'].iloc[-1]) if '외국인합계' in inv_df else None
                data['institution_net_buy'] = float(inv_df['기관합계'].iloc[-1]) if '기관합계' in inv_df else None
        except Exception:
            pass

    except Exception as e:
        logger.debug(f"[매크로] {date_str} 수집 오류: {e}")

    data['is_fomc_week'] = 1 if date_str in _FOMC_DATES else 0
    data['is_cpi_week']  = 1 if date_str in _CPI_DATES else 0

    save_macro_snapshot(date_str, data)
    _macro_cache[date_str] = data
    return data


def build_macro_context_str(macro: dict) -> str:
    lines = []
    if macro.get('kospi_vs_ma200') is not None:
        regime = 'BULL' if macro['kospi_vs_ma200'] > 0 else 'BEAR'
        lines.append(f"[시장국면] KOSPI {regime} (MA200대비 {macro['kospi_vs_ma200']:+.1f}%)")
    vix = macro.get('vix')
    if vix:
        fear = '극단공포' if vix > 35 else '공포' if vix > 25 else '중립' if vix > 15 else '탐욕'
        lines.append(f"[공포지수] VIX {vix:.1f} ({fear})")
    parts = []
    if macro.get('sp500_chg') is not None:  parts.append(f"S&P500 {macro['sp500_chg']:+.1f}%")
    if macro.get('nasdaq_chg') is not None: parts.append(f"나스닥 {macro['nasdaq_chg']:+.1f}%")
    if macro.get('nikkei_chg') is not None: parts.append(f"닛케이 {macro['nikkei_chg']:+.1f}%")
    if parts: lines.append(f"[글로벌] {' | '.join(parts)}")
    parts2 = []
    if macro.get('usd_krw'):    parts2.append(f"달러원 {macro['usd_krw']:.0f}")
    if macro.get('us_10y'):     parts2.append(f"미10년 {macro['us_10y']:.2f}%")
    if macro.get('yield_spread') is not None:
        inv = ' ⚠️역전' if macro['yield_spread'] < 0 else ''
        parts2.append(f"장단기스프레드 {macro['yield_spread']:+.3f}{inv}")
    if parts2: lines.append(f"[금리/환율] {' | '.join(parts2)}")
    parts3 = []
    if macro.get('wti'):    parts3.append(f"WTI ${macro['wti']:.1f}")
    if macro.get('gold'):   parts3.append(f"금 ${macro['gold']:.0f}")
    if macro.get('sox_chg') is not None: parts3.append(f"SOX {macro['sox_chg']:+.1f}%")
    if parts3: lines.append(f"[원자재] {' | '.join(parts3)}")
    if macro.get('foreign_net_buy') is not None:
        fb = macro['foreign_net_buy'] / 1e8
        lines.append(f"[수급] 외국인 {fb:+.0f}억")
    events = []
    if macro.get('is_fomc_week'): events.append('FOMC 주간')
    if macro.get('is_cpi_week'):  events.append('CPI 발표 주간')
    if events: lines.append(f"[이벤트] {' | '.join(events)}")
    return '\n'.join(lines)
