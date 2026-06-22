"""날짜별 강세 섹터 계산 — 라이브 get_sector_momentum 과 동일 로직(테마섹터 바스켓).

강세 섹터는 날짜에만 의존(종목 무관)하므로 날짜당 1회 계산.
KR: KR.screener.SECTOR_STOCKS 테마섹터 대표종목 바스켓
US: SPDR 섹터 ETF
바스켓/ETF 전체 시계열을 1회 다운로드 후 메모리에서 슬라이싱(macro 와 동일 패턴).
"""
import logging
import pandas as pd

logger = logging.getLogger('lassi_bot')

# US GICS → SPDR 섹터 ETF
_US_SECTOR_ETF = {
    'Technology': 'XLK', 'Financial Services': 'XLF', 'Energy': 'XLE',
    'Healthcare': 'XLV', 'Industrials': 'XLI', 'Consumer Cyclical': 'XLY',
    'Consumer Defensive': 'XLP', 'Utilities': 'XLU', 'Basic Materials': 'XLB',
    'Real Estate': 'XLRE', 'Communication Services': 'XLC',
}

_REPS_PER_SECTOR = 12          # 섹터당 대표종목 수 (다운로드 부담 ↓)
_kr_basket_cache = None        # {sector: pd.Series(정규화 바스켓 지수)}
_us_etf_cache = None           # {sector: pd.Series(close)}
_hot_cache: dict = {}          # (market, date, top_n) → list


def _series(ticker: str) -> pd.Series:
    from base.macro_collector import _get_yf_series
    return _get_yf_series(ticker)


def _build_kr_baskets():
    global _kr_basket_cache
    if _kr_basket_cache is not None:
        return _kr_basket_cache
    from KR.screener import SECTOR_STOCKS
    baskets = {}
    for sector, tickers in SECTOR_STOCKS.items():
        cols = []
        for tk in tickers[:_REPS_PER_SECTOR]:
            for sfx in ('.KS', '.KQ'):
                s = _series(tk + sfx)
                if not s.empty:
                    cols.append(s / s.iloc[0])   # 정규화 후 평균 = 바스켓 지수
                    break
        if cols:
            df = pd.concat(cols, axis=1)
            baskets[sector] = df.mean(axis=1).dropna()
    _kr_basket_cache = baskets
    logger.info(f"[sector_strength] KR 섹터 바스켓 {len(baskets)}개 구축")
    return baskets


def _build_us_etfs():
    global _us_etf_cache
    if _us_etf_cache is not None:
        return _us_etf_cache
    out = {}
    for sector, etf in _US_SECTOR_ETF.items():
        s = _series(etf)
        if not s.empty:
            out[sector] = s
    _us_etf_cache = out
    logger.info(f"[sector_strength] US 섹터 ETF {len(out)}개 로드")
    return out


def _blended_return(series: pd.Series, date_str: str) -> float | None:
    """20일×0.7 + 5일×0.3 블렌디드 수익률 (라이브 로직과 동일)."""
    try:
        pos = series.index.searchsorted(pd.Timestamp(date_str), side='right') - 1
        if pos < 20:
            return None
        cur = float(series.iloc[pos])
        r20 = (cur / float(series.iloc[pos - 20]) - 1) * 100
        r5 = (cur / float(series.iloc[pos - 5]) - 1) * 100
        return round(r20 * 0.7 + r5 * 0.3, 2)
    except Exception:
        return None


def get_hot_sectors_for_date(market: str, date_str: str, top_n: int = 4) -> list:
    """해당 날짜의 강세 섹터 상위 N개 이름 리스트 (상대강세 기준)."""
    key = (market, date_str, top_n)
    if key in _hot_cache:
        return _hot_cache[key]
    baskets = _build_kr_baskets() if market == 'KR' else _build_us_etfs()
    scored = []
    for sector, series in baskets.items():
        r = _blended_return(series, date_str)
        if r is not None:
            scored.append((sector, r))
    scored.sort(key=lambda x: x[1], reverse=True)
    hot = [s for s, _ in scored[:top_n]]
    _hot_cache[key] = hot
    return hot
