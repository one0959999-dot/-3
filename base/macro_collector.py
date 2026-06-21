import logging
import pandas as pd
from datetime import datetime, timedelta
from base.database import save_macro_snapshot, get_macro_snapshot

logger = logging.getLogger('lassi_bot')

_fred_cache: dict = {}
_yf_series_cache: dict = {}   # ticker → 전체 Close 시계열 (1회 다운로드 후 메모리 슬라이싱)

_FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"


def _get_yf_series(ticker: str) -> pd.Series:
    """지표용 전체 Close 시계열을 1회만 다운로드해 캐싱.

    매크로 계산 시 날짜·필드마다 yfinance 를 호출하던 병목 제거.
    """
    if ticker in _yf_series_cache:
        return _yf_series_cache[ticker]
    s = pd.Series(dtype=float)
    try:
        import yfinance as yf
        df = yf.download(ticker, period='max', interval='1d',
                         progress=False, auto_adjust=True)
        if df is not None and not df.empty:
            if hasattr(df.columns, 'get_level_values'):
                df.columns = df.columns.get_level_values(0)
            if 'Close' in df.columns:
                s = pd.to_numeric(df['Close'], errors='coerce').dropna()
                s.index = pd.to_datetime(s.index)
    except Exception as e:
        logger.debug(f"[macro] {ticker} 전체 시계열 로드 실패: {e}")
    _yf_series_cache[ticker] = s
    return s


def _fred_get(series_id: str, fred_key: str,
              start: str = '1990-01-01', end: str = None) -> pd.Series:
    key = f"{series_id}_{start}"
    if key in _fred_cache:
        return _fred_cache[key]
    try:
        import requests
        params = {
            'series_id':         series_id,
            'api_key':           fred_key,
            'file_type':         'json',
            'observation_start': start,
            'observation_end':   end or datetime.now().strftime('%Y-%m-%d'),
        }
        r = requests.get(_FRED_BASE, params=params, timeout=15)
        obs = r.json().get('observations', [])
        s = pd.Series(
            {o['date']: float(o['value']) for o in obs if o['value'] != '.'},
            dtype=float
        )
        s.index = pd.to_datetime(s.index)
        _fred_cache[key] = s
        return s
    except Exception as e:
        logger.debug(f"[FRED] {series_id} 조회 실패: {e}")
        return pd.Series(dtype=float)


def _get_us_rate(date_str: str, fred_key: str = '') -> float | None:
    if fred_key:
        s = _fred_get('DFF', fred_key)
        if not s.empty:
            target = pd.Timestamp(date_str)
            idx = s.index.searchsorted(target, side='right') - 1
            if 0 <= idx < len(s):
                return round(float(s.iloc[idx]), 4)
    v = _yf_close('^IRX', date_str)
    return round(v, 4) if v is not None else None


def _get_kr_rate(date_str: str) -> float | None:
    try:
        from pykrx import bond as pykrx_bond
        date_fmt = date_str.replace('-', '')
        start_fmt = (datetime.strptime(date_str, '%Y-%m-%d') - timedelta(days=10)).strftime('%Y%m%d')
        df = pykrx_bond.get_otc_treasury_yields_in_date_range(start_fmt, date_fmt)
        if df is not None and not df.empty:
            col = [c for c in df.columns if '3년' in c or '3Y' in c.upper()]
            if col:
                vals = df[col[0]].dropna()
                if not vals.empty:
                    return round(float(vals.iloc[-1]), 4)
    except Exception:
        pass
    return None


def _is_fomc_week(date_str: str, fred_key: str = '') -> int:
    if fred_key:
        s = _fred_get('DFF', fred_key)
        if not s.empty:
            target = pd.Timestamp(date_str)
            week_start = target - timedelta(days=target.weekday())
            week_end   = week_start + timedelta(days=6)
            week_data  = s[(s.index >= week_start) & (s.index <= week_end)]
            if len(week_data) >= 2 and week_data.diff().abs().max() >= 0.1:
                return 1
            return 0
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    return 1 if dt.day <= 7 and dt.weekday() <= 4 else 0


def _is_cpi_week(date_str: str) -> int:
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    return 1 if 8 <= dt.day <= 16 and dt.weekday() <= 4 else 0


def _yf_download_safe(ticker: str, start: str, end: str, timeout: int = 10):
    try:
        import requests, yfinance as yf
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter()
        session.mount('https://', adapter)
        session.mount('http://', adapter)
        return yf.download(ticker, start=start, end=end, progress=False,
                           auto_adjust=True, session=session,
                           timeout=timeout)
    except Exception:
        return None


def _yf_close(ticker: str, date_str: str) -> float | None:
    try:
        s = _get_yf_series(ticker)
        if s.empty:
            return None
        target = pd.Timestamp(date_str)
        idx = s.index.searchsorted(target, side='right') - 1
        if idx < 0:
            return None
        if idx >= len(s):
            idx = len(s) - 1
        return float(s.iloc[idx])
    except Exception:
        return None


def _yf_chg(ticker: str, date_str: str) -> float | None:
    try:
        s = _get_yf_series(ticker)
        if s.empty or len(s) < 2:
            return None
        target = pd.Timestamp(date_str)
        idx = s.index.searchsorted(target, side='right') - 1
        if idx <= 0:
            return 0.0 if idx == 0 else None
        if idx >= len(s):
            idx = len(s) - 1
        c = float(s.iloc[idx]); p = float(s.iloc[idx - 1])
        return round((c / p - 1) * 100, 2) if p else None
    except Exception:
        return None


def get_macro_for_date(date_str: str, fred_key: str = '') -> dict:
    cached = get_macro_snapshot(date_str)
    if cached:
        return cached

    data = {'date': date_str}

    data['sp500_chg']    = _yf_chg('^GSPC', date_str)
    data['nasdaq_chg']   = _yf_chg('^IXIC', date_str)
    data['nikkei_chg']   = _yf_chg('^N225', date_str)
    data['shanghai_chg'] = _yf_chg('000001.SS', date_str)
    data['vix']          = _yf_close('^VIX', date_str)
    data['dxy']          = _yf_close('DX-Y.NYB', date_str)
    data['usd_krw']      = _yf_close('KRW=X', date_str)
    data['wti']          = _yf_close('CL=F', date_str)
    data['gold']         = _yf_close('GC=F', date_str)
    data['copper']       = _yf_close('HG=F', date_str)
    data['sox_chg']      = _yf_chg('^SOX', date_str)
    data['us_10y']       = _yf_close('^TNX', date_str)

    us_10y = data.get('us_10y') or 0
    data['us_rate']      = _get_us_rate(date_str, fred_key)
    data['kr_rate']      = _get_kr_rate(date_str)

    us_2y = _yf_close('^IRX', date_str)
    data['us_2y']        = us_2y
    data['yield_spread'] = round(us_10y - (us_2y or 0), 3) if us_10y and us_2y else None

    try:
        end_dt = datetime.strptime(date_str, '%Y-%m-%d')
        close  = _get_yf_series('^KS11')  # 전체 시계열 캐시 후 슬라이싱
        if not close.empty:
            pos = close.index.searchsorted(pd.Timestamp(date_str), side='right') - 1
            if pos >= 0:
                data['kospi_close'] = round(float(close.iloc[pos]), 2)
                window = close.iloc[max(0, pos - 251): pos + 1]
                if len(window) >= 200:
                    ma200 = float(window.tail(200).mean())
                    data['kospi_vs_ma200'] = round((data['kospi_close'] / ma200 - 1) * 100, 2)
                if len(window) >= 252:
                    high52 = float(window.max()); low52 = float(window.min())
                    rng = high52 - low52
                    data['kospi_52w_pct'] = round((data['kospi_close'] - low52) / rng * 100, 1) if rng else 50
    except Exception:
        pass

    data['is_fomc_week'] = _is_fomc_week(date_str, fred_key)
    data['is_cpi_week']  = _is_cpi_week(date_str)

    save_macro_snapshot(date_str, data)
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
    if macro.get('sp500_chg')    is not None: parts.append(f"S&P500 {macro['sp500_chg']:+.1f}%")
    if macro.get('nasdaq_chg')   is not None: parts.append(f"나스닥 {macro['nasdaq_chg']:+.1f}%")
    if macro.get('nikkei_chg')   is not None: parts.append(f"닛케이 {macro['nikkei_chg']:+.1f}%")
    if macro.get('shanghai_chg') is not None: parts.append(f"상해 {macro['shanghai_chg']:+.1f}%")
    if parts: lines.append(f"[글로벌] {' | '.join(parts)}")

    parts2 = []
    if macro.get('usd_krw'):    parts2.append(f"달러원 {macro['usd_krw']:.0f}")
    if macro.get('us_rate'):    parts2.append(f"미기준금리 {macro['us_rate']:.2f}%")
    if macro.get('kr_rate'):    parts2.append(f"한기준금리 {macro['kr_rate']:.2f}%")
    if macro.get('us_10y'):     parts2.append(f"미10년 {macro['us_10y']:.2f}%")
    if macro.get('yield_spread') is not None:
        inv = ' ⚠️역전' if macro['yield_spread'] < 0 else ''
        parts2.append(f"장단기 {macro['yield_spread']:+.3f}{inv}")
    if parts2: lines.append(f"[금리/환율] {' | '.join(parts2)}")

    parts3 = []
    if macro.get('wti'):             parts3.append(f"WTI ${macro['wti']:.1f}")
    if macro.get('gold'):            parts3.append(f"금 ${macro['gold']:.0f}")
    if macro.get('copper'):          parts3.append(f"구리 ${macro['copper']:.2f}")
    if macro.get('sox_chg') is not None: parts3.append(f"SOX {macro['sox_chg']:+.1f}%")
    if parts3: lines.append(f"[원자재/반도체] {' | '.join(parts3)}")

    if macro.get('foreign_net_buy') is not None:
        fb = macro['foreign_net_buy'] / 1e8
        ib = (macro.get('institution_net_buy') or 0) / 1e8
        lines.append(f"[수급] 외국인 {fb:+.0f}억 | 기관 {ib:+.0f}억")

    events = []
    if macro.get('is_fomc_week'): events.append('FOMC 주간')
    if macro.get('is_cpi_week'):  events.append('CPI 발표 주간')
    if events: lines.append(f"[이벤트] {' | '.join(events)}")

    return '\n'.join(lines)
