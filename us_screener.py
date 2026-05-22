"""
us_screener.py — 미국장 위성 종목 스크리너 (yfinance 기반)
─────────────────────────────────────────────────────────
① 섹터별 유니버스 정의 (~60개 종목)
② 모멘텀(20일/60일) + 골든크로스 + 거래량 서지 + RSI 필터
③ 종합 스코어 → 상위 N개 반환
"""

import time
import logging
import threading

import yfinance as yf
import pandas as pd

logger = logging.getLogger('lassi_bot')

# ── 스캔 유니버스: 섹터별 대표 종목 ─────────────────────────────────
US_UNIVERSE: dict[str, list[str]] = {
    "AI/반도체":    ["NVDA", "AMD", "AVGO", "ARM", "SMCI", "MRVL", "AMAT", "LRCX", "QCOM"],
    "빅테크":       ["MSFT", "AAPL", "META", "GOOGL", "AMZN", "ORCL", "CRM", "PLTR", "SNOW"],
    "우주/방산":    ["RKLB", "LMT", "NOC", "RTX", "HII", "ACHR"],
    "바이오/헬스":  ["LLY", "NVO", "ABBV", "MRNA", "REGN", "ISRG"],
    "에너지":       ["XOM", "CVX", "SLB", "HAL", "OXY"],
    "금융":         ["JPM", "GS", "V", "MA", "COIN"],
    "소비/유통":    ["TSLA", "HD", "COST", "MCD", "NKE", "ULTA"],
    "ETF 레버리지": ["TQQQ", "SOXL", "UPRO", "TNA"],
}

# 코어 ETF — 위성 스캔에서 제외
CORE_ETF_EXCLUDE = {"SPY", "QQQ", "IWM", "VTI", "VOO"}

# 종목명 캐시 (yfinance info 조회 비용 절감)
_name_cache: dict[str, str] = {}
_name_lock = threading.Lock()


def _calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / (loss + 1e-10)
    return 100 - 100 / (1 + rs)


def _get_name(ticker: str) -> str:
    """종목명 조회 (캐시 우선)"""
    with _name_lock:
        if ticker in _name_cache:
            return _name_cache[ticker]
    try:
        info = yf.Ticker(ticker).fast_info
        name = (getattr(info, 'name', '') or
                getattr(info, 'short_name', '') or ticker)
    except Exception:
        name = ticker
    with _name_lock:
        _name_cache[ticker] = name
    return name


def scan_us_satellites(n: int = 5, exclude: set = None) -> list[dict]:
    """
    미국 위성 종목 스캔.

    Returns list of dicts:
      ticker, name, sector, score, price, momentum_20d, rsi, golden, vol_ratio
    """
    exclude = (exclude or set()) | CORE_ETF_EXCLUDE

    # 전체 티커 리스트 + 섹터 맵 구성
    all_tickers: list[str] = []
    ticker_sector: dict[str, str] = {}
    for sector, tickers in US_UNIVERSE.items():
        for t in tickers:
            if t not in exclude:
                all_tickers.append(t)
                ticker_sector[t] = sector

    if not all_tickers:
        return []

    # ── 배치 다운로드 (6개월치 일봉) ────────────────────────────────
    logger.info(f"[US스크리너] {len(all_tickers)}개 종목 다운로드 시작...")
    try:
        raw = yf.download(
            all_tickers,
            period="6mo",
            interval="1d",
            progress=False,
            auto_adjust=True,
        )
    except Exception as e:
        logger.error(f"[US스크리너] 배치 다운로드 실패: {e}")
        return []

    if raw is None or raw.empty:
        logger.warning("[US스크리너] 다운로드 결과 없음")
        return []

    # Close / Volume DataFrame 추출
    # yfinance ≥0.2: columns = MultiIndex (field, ticker) or (ticker, field)
    try:
        if isinstance(raw.columns, pd.MultiIndex):
            lv0 = raw.columns.get_level_values(0).unique().tolist()
            # (field, ticker) 형태
            if "Close" in lv0:
                closes  = raw["Close"]
                volumes = raw["Volume"]
            # (ticker, field) 형태
            else:
                closes  = raw.xs("Close",  axis=1, level=1, drop_level=True)
                volumes = raw.xs("Volume", axis=1, level=1, drop_level=True)
        else:
            # 단일 종목일 때 → Series 형태
            closes  = raw[["Close"]].rename(columns={"Close": all_tickers[0]})
            volumes = raw[["Volume"]].rename(columns={"Volume": all_tickers[0]})
    except Exception as e:
        logger.error(f"[US스크리너] 컬럼 파싱 실패: {e}")
        return []

    results: list[dict] = []

    for ticker in all_tickers:
        try:
            # 해당 티커 Close/Volume 추출
            if ticker not in closes.columns:
                continue
            close  = closes[ticker].dropna()
            volume = volumes[ticker].dropna() if ticker in volumes.columns else pd.Series(dtype=float)

            if len(close) < 60:
                continue

            price    = float(close.iloc[-1])
            if price <= 0:
                continue

            # ── 지표 계산 ──────────────────────────────────────────
            # 모멘텀
            mom_20 = (price / float(close.iloc[-20]) - 1) * 100 if len(close) >= 20 else 0.0
            mom_60 = (price / float(close.iloc[-60]) - 1) * 100 if len(close) >= 60 else 0.0

            # 이동평균
            sma50  = float(close.rolling(50).mean().iloc[-1])  if len(close) >= 50  else price
            sma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else price
            # 골든크로스: 현재가 > 50일 > 200일×0.98(근접 허용)
            golden = price > sma50 > sma200 * 0.98

            # RSI
            rsi = float(_calc_rsi(close).iloc[-1]) if len(close) >= 20 else 50.0

            # 거래량 서지
            if len(volume) >= 60:
                vol_recent = float(volume.iloc[-5:].mean())
                vol_base   = float(volume.iloc[-60:-5].mean())
                vol_ratio  = vol_recent / (vol_base + 1)
            else:
                vol_ratio  = 1.0

            # ── 스코어 (0~100) ─────────────────────────────────────
            score = 0.0
            score += min(40.0, max(0.0, mom_20 * 2.0))   # 20일 모멘텀 (20%↑=40점)
            if golden:                score += 20.0                            # 골든크로스
            if 40 <= rsi <= 65:       score += 15.0                           # RSI 적정구간
            elif 35 <= rsi <= 70:     score += 8.0
            score += min(15.0, (vol_ratio - 1) * 10.0)   # 거래량 서지
            if mom_60 > 0:            score += min(10.0, mom_60 * 0.5)        # 60일 모멘텀 플러스

            # ── 최소 필터: 과매수·급락 제외 ────────────────────────
            if rsi > 82 or mom_20 < -8:
                continue

            results.append({
                "ticker":       ticker,
                "name":         ticker,        # 빠른 반환 — 이름은 나중에 채움
                "sector":       ticker_sector[ticker],
                "score":        round(score, 1),
                "price":        round(price, 2),
                "momentum_20d": round(mom_20, 2),
                "rsi":          round(rsi, 1),
                "golden":       golden,
                "vol_ratio":    round(vol_ratio, 2),
            })

        except Exception as e:
            logger.debug(f"[US스크리너] {ticker} 처리 실패: {e}")
            continue

    # 스코어 내림차순 정렬
    results.sort(key=lambda x: x["score"], reverse=True)

    # 상위 결과에만 종목명 채우기 (API 호출 최소화)
    top = results[:n * 2]
    for r in top:
        r["name"] = _get_name(r["ticker"])
        time.sleep(0.05)

    logger.info(f"[US스크리너] 스캔 완료: {len(results)}개 통과 → 상위 {n}개 선정")
    for r in top[:n]:
        logger.info(
            f"  {r['ticker']:6s}  {r['sector']:12s}  스코어:{r['score']:5.1f}"
            f"  RSI:{r['rsi']:4.1f}  20d:{r['momentum_20d']:+5.1f}%"
            f"  {'🟡골든' if r['golden'] else '  '}"
            f"  거래량:{r['vol_ratio']:.1f}x"
        )

    return top[:n]


# ── 실시간 가격 조회 ──────────────────────────────────────────────────

def get_us_prices_batch(tickers) -> dict[str, float]:
    """
    복수 종목 USD 가격 배치 조회.
    Returns {ticker: price_usd}
    """
    tickers = list(tickers)
    if not tickers:
        return {}

    prices: dict[str, float] = {}

    try:
        raw = yf.download(
            tickers,
            period="2d",
            interval="1d",
            progress=False,
            auto_adjust=True,
        )
        if raw is None or raw.empty:
            raise ValueError("empty")

        if isinstance(raw.columns, pd.MultiIndex):
            lv0 = raw.columns.get_level_values(0).unique().tolist()
            close_df = raw["Close"] if "Close" in lv0 else raw.xs("Close", axis=1, level=1, drop_level=True)
        else:
            # 단일 종목
            close_df = raw[["Close"]].rename(columns={"Close": tickers[0]})

        for t in tickers:
            if t in close_df.columns:
                s = close_df[t].dropna()
                if not s.empty:
                    prices[t] = float(s.iloc[-1])

    except Exception as e:
        logger.debug(f"[US스크리너] 배치 가격 조회 실패 ({e}), 개별 조회로 폴백")
        for t in tickers:
            try:
                hist = yf.Ticker(t).history(period="2d")
                if not hist.empty:
                    prices[t] = float(hist["Close"].dropna().iloc[-1])
                time.sleep(0.1)
            except Exception:
                pass

    return prices


def generate_us_daily_report(gemini_client=None, positions: dict = None,
                              satellite_info: list = None) -> dict:
    """
    미국장 일일 리포트 생성.
    - 주요 지수(SPY·QQQ·DIA) + 섹터 ETF 흐름 수집 (yfinance)
    - 보유 위성 포지션 손익 요약 포함
    - gemini_client 제공 시 Claude AI 분석, 없으면 룰 기반 리포트
    """
    from datetime import datetime, timezone, timedelta
    _ET = timezone(timedelta(hours=-4))
    today_str  = datetime.now(_ET).strftime('%Y년 %m월 %d일 (%a)')
    today_key  = datetime.now(_ET).strftime('%Y-%m-%d')

    lines: list[str] = [f"날짜: {today_str} (ET 기준)"]

    # ── 1. 주요 지수 ──────────────────────────────────────────────────
    indices = {
        "S&P 500 (SPY)":   "SPY",
        "NASDAQ 100 (QQQ)":"QQQ",
        "Dow Jones (DIA)": "DIA",
        "Russell 2000 (IWM)": "IWM",
        "VIX (공포지수)":  "^VIX",
    }
    lines.append("\n[주요 지수 데이터]")
    for name, sym in indices.items():
        try:
            df = yf.download(sym, period="35d", interval="1d",
                             progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.dropna(subset=["Close"])
            if len(df) < 2:
                continue
            close   = df["Close"]
            cur     = float(close.iloc[-1])
            prev    = float(close.iloc[-2])
            chg_pct = (cur / prev - 1) * 100
            sma5    = float(close.rolling(5).mean().iloc[-1]) if len(close) >= 5 else cur
            sma20   = float(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else cur
            # RSI
            delta = close.diff()
            gain  = delta.clip(lower=0).rolling(14).mean()
            loss  = (-delta.clip(upper=0)).rolling(14).mean()
            rs    = gain / loss.replace(0, float('nan'))
            rsi   = float((100 - 100 / (1 + rs)).iloc[-1]) if not rs.empty else 50.0
            lines.append(
                f"- {name}: ${cur:.2f} ({chg_pct:+.2f}%)  "
                f"5일선 ${sma5:.2f} / 20일선 ${sma20:.2f}  RSI {rsi:.1f}"
            )
        except Exception:
            pass

    # ── 2. 섹터 ETF 흐름 ──────────────────────────────────────────────
    sector_etfs = {
        "기술(XLK)": "XLK", "금융(XLF)": "XLF", "에너지(XLE)": "XLE",
        "헬스케어(XLV)": "XLV", "소비재(XLY)": "XLY", "산업재(XLI)": "XLI",
    }
    lines.append("\n[섹터 ETF 5일 수익률]")
    sector_perf: list[tuple[str, float]] = []
    for name, sym in sector_etfs.items():
        try:
            df = yf.download(sym, period="10d", interval="1d",
                             progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.dropna(subset=["Close"])
            if len(df) >= 6:
                ret = (float(df["Close"].iloc[-1]) / float(df["Close"].iloc[-6]) - 1) * 100
                sector_perf.append((name, ret))
        except Exception:
            pass
    sector_perf.sort(key=lambda x: x[1], reverse=True)
    for name, ret in sector_perf:
        lines.append(f"- {name}: {ret:+.2f}%")

    # ── 3. 보유 위성 포지션 손익 ──────────────────────────────────────
    if positions:
        holding = {t: p for t, p in positions.items() if p.shares > 0}
        if holding:
            lines.append("\n[현재 보유 위성 포지션]")
            for ticker, pos in holding.items():
                try:
                    df = yf.download(ticker, period="2d", interval="1d",
                                     progress=False, auto_adjust=True)
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    cur_p = float(df["Close"].dropna().iloc[-1]) if not df.empty else pos.avg_price_usd
                except Exception:
                    cur_p = pos.avg_price_usd
                pnl_pct = (cur_p / pos.avg_price_usd - 1) * 100 if pos.avg_price_usd > 0 else 0.0
                lines.append(
                    f"- {pos.name}({ticker}): {int(pos.shares)}주  "
                    f"평균단가 ${pos.avg_price_usd:.2f} → 현재 ${cur_p:.2f}  ({pnl_pct:+.1f}%)"
                )

    # ── 4. 스캔 후보 ──────────────────────────────────────────────────
    if satellite_info:
        lines.append("\n[오늘의 위성 후보 종목]")
        for info in satellite_info:
            lines.append(
                f"- {info['name']}({info['ticker']})  "
                f"섹터: {info['sector']}  점수: {info['score']:.0f}"
            )

    market_data_text = "\n".join(lines)

    # ── 5. AI 분석 or 룰 기반 리포트 ─────────────────────────────────
    if gemini_client:
        prompt = (
            f"당신은 미국 주식 시장 전문 애널리스트입니다.\n"
            f"아래 데이터를 바탕으로 한국어로 오늘의 미국 시장 분석 리포트를 작성해주세요.\n"
            f"형식: 마크다운 (제목/소제목/불릿 포인트 사용)\n"
            f"포함 내용: ① 전체 시장 방향성 ② 주목 섹터 ③ 보유 포지션 의견 ④ 오늘의 전략 제안\n\n"
            f"{market_data_text}"
        )
        try:
            report_text = gemini_client.analyze_market(prompt)
        except Exception:
            report_text = market_data_text
    else:
        report_text = (
            f"### 📊 Lassi US Bot 시장 분석 리포트\n\n"
            f"```\n{market_data_text}\n```\n\n"
            f"> AI 분석 비활성화 상태입니다. Claude API 키를 등록하면 정교한 분석을 받을 수 있습니다."
        )

    return {"date": today_key, "report_markdown": report_text}


def get_us_ohlcv(ticker: str, days: int = 200) -> pd.DataFrame:
    """
    단일 종목 OHLCV (일봉, USD).
    columns: Open, High, Low, Close, Volume
    """
    try:
        period = f"{max(days // 20 + 1, 12)}mo"
        df = yf.download(ticker, period=period, interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or df.empty:
            return pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna(subset=["Close"]).tail(days)
        df.columns = [c.capitalize() for c in df.columns]
        return df
    except Exception as e:
        logger.debug(f"[US스크리너] {ticker} OHLCV 조회 실패: {e}")
        return pd.DataFrame()
