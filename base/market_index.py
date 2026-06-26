"""
실제 코스피/코스닥 지수 조회 + 종목 상장시장 분류 헬퍼.
- 토스 API엔 지수(포인트)가 없어서 pykrx로 실제 지수를 가져온다.
  (ETF 가격을 '지수'라고 잘못 부르던 문제 보완용)
- 종목이 코스피인지 코스닥인지 분류하고, 그 시장의 건강도(20일선 위/아래)를 알려준다.
어떤 이유로든 실패하면 빈 값/문자열을 반환 — 호출부를 절대 깨뜨리지 않는다.
"""
import datetime
from pykrx import stock

NL = chr(10)
KOSPI_CODE = "1001"
KOSDAQ_CODE = "2001"


def _index_status(code):
    """지수 현재값, 20일선, 위/아래 상태. 실패 시 None."""
    try:
        end = datetime.datetime.now().strftime("%Y%m%d")
        start = (datetime.datetime.now() - datetime.timedelta(days=60)).strftime("%Y%m%d")
        df = stock.get_index_ohlcv_by_date(start, end, code)
        if df is None or df.empty:
            return None
        close = df["종가"]
        last = float(close.iloc[-1])
        ma20 = float(close.tail(20).mean())
        return {"value": last, "ma20": ma20, "above": last >= ma20}
    except Exception:
        return None


def get_market_status():
    """코스피/코스닥 실제 지수 상태 dict. 실패한 쪽은 None."""
    return {"KOSPI": _index_status(KOSPI_CODE), "KOSDAQ": _index_status(KOSDAQ_CODE)}


_mkt_cache = {"date": None, "KOSPI": set(), "KOSDAQ": set()}


def _load_market_sets():
    today = datetime.datetime.now().strftime("%Y%m%d")
    if _mkt_cache["date"] == today and (_mkt_cache["KOSPI"] or _mkt_cache["KOSDAQ"]):
        return
    try:
        _mkt_cache["KOSPI"] = set(stock.get_market_ticker_list(today, market="KOSPI"))
        _mkt_cache["KOSDAQ"] = set(stock.get_market_ticker_list(today, market="KOSDAQ"))
        _mkt_cache["date"] = today
    except Exception:
        pass


def classify_market(ticker):
    """ticker가 'KOSPI' / 'KOSDAQ' / '?' 중 어디인지 반환."""
    try:
        t = str(ticker).zfill(6)
        _load_market_sets()
        if t in _mkt_cache["KOSPI"]:
            return "KOSPI"
        if t in _mkt_cache["KOSDAQ"]:
            return "KOSDAQ"
        return "?"
    except Exception:
        return "?"


def holdings_market_context(tickers):
    """보유 종목들이 각각 어느 시장이고 그 시장이 건강한지 요약 문자열.
    실패하면 빈 문자열."""
    try:
        tickers = [t for t in (tickers or []) if t]
        ms = get_market_status()

        def mline(name, st):
            if not st:
                return name + ": 지수 조회 실패"
            pos = "20일선 위(안정)" if st["above"] else "20일선 아래(약세)"
            return "{}: {:,.2f}p ({})".format(name, st["value"], pos)

        out = ["[시장 위치 — 실제 지수 & 보유종목 상장시장]"]
        out.append("아래는 ETF가 아니라 실제 지수 포인트다. 시장을 말할 때 이 값을 써라.")
        out.append(mline("코스피", ms["KOSPI"]))
        out.append(mline("코스닥", ms["KOSDAQ"]))
        if tickers:
            out.append("보유종목 상장시장:")
            for t in tickers:
                m = classify_market(t)
                st = ms.get(m)
                warn = " <- 약세 시장, 리스크 주의" if (st and not st["above"]) else ""
                out.append("- {} : {}{}".format(t, m, warn))
        return NL + NL + NL.join(out) + NL
    except Exception:
        return ""
