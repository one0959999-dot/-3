"""종목 → 섹터/산업 조회 (yfinance .info 기반, 종목당 1회 + DB 영구 캐시).

섹터는 종목당 고정 속성이므로 신호마다가 아니라 종목당 1회만 조회.
KR/US 통일 소스(yfinance GICS 섹터). 강세 섹터 로테이션 분석에 사용.
"""
import logging

logger = logging.getLogger('lassi_bot')

_mem_cache: dict = {}   # 'ticker|market' → (sector, industry)


def _ensure_table():
    from base.database import get_db_connection
    with get_db_connection() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS ticker_sector (
            key TEXT PRIMARY KEY, ticker TEXT, market TEXT,
            sector TEXT, industry TEXT)''')
        conn.commit()


def get_sector(ticker: str, market: str = 'US') -> tuple:
    """(sector, industry) 반환. DB→메모리 캐시, 미스 시 yfinance 1회 조회.

    KR은 .KS 우선 .KQ 폴백. 실패 시 ('기타','') 캐시(재시도 방지).
    """
    key = f"{ticker}|{market}"
    if key in _mem_cache:
        return _mem_cache[key]
    # DB 캐시
    try:
        from base.database import get_db_connection
        _ensure_table()
        with get_db_connection() as conn:
            row = conn.execute('SELECT sector, industry FROM ticker_sector WHERE key=?', (key,)).fetchone()
        if row is not None:
            res = (row[0] or '기타', row[1] or '')
            _mem_cache[key] = res
            return res
    except Exception:
        pass

    # yfinance 조회
    sector, industry = '기타', ''
    try:
        import yfinance as yf
        candidates = [ticker]
        if market == 'KR':
            candidates = [f"{ticker}.KS", f"{ticker}.KQ"]
        for sym in candidates:
            try:
                info = yf.Ticker(sym).info
            except Exception:
                continue
            s = info.get('sector')
            if s:
                sector = s
                industry = info.get('industry') or ''
                break
    except Exception as e:
        logger.debug(f"[sector] {ticker} 조회 실패: {e}")

    # DB + 메모리 저장 (실패한 '기타'도 저장해 재시도 방지)
    try:
        from base.database import get_db_connection
        with get_db_connection() as conn:
            conn.execute('INSERT OR REPLACE INTO ticker_sector VALUES (?,?,?,?,?)',
                         (key, ticker, market, sector, industry))
            conn.commit()
    except Exception:
        pass
    _mem_cache[key] = (sector, industry)
    return sector, industry
