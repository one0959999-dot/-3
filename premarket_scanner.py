"""
premarket_scanner.py — 장 마감 후 다음날 단타 후보 선정 스캐너
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
핵심 로직:
  ① 박스권 횡보 중인 종목 탐색 (20일 고가-저가 범위 12% 이내)
  ② 최근 3일 거래량이 이전 10일 평균보다 30% 이상 증가 (매집 신호)
  ③ 52주 고점 대비 -5%~-40% 구간 (돌파 직전 or 충분한 조정 후)
  ④ RSI 35~60 (과매수·과매도 아님, 축적 구간)

당일 진입 조건 (09:00~09:15):
  ⑤ 시초가가 전일 종가 대비 ±3% 이내 (갭 없음)
  ⑥ 당일 누적 거래량 ≥ 전일 일 거래량의 15% (15분 내)
  ⑦ 현재가 ≥ 시초가 (양봉 출발)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

logger = logging.getLogger('lassi_bot')

# ── 선정 상수 ──────────────────────────────────────────────────────
PM_BOX_RANGE_MAX     = 0.12   # 20일 박스권 고저 폭 최대 12%
PM_VOL_TREND_RATIO   = 1.30   # 최근 3일 거래량 ≥ 이전 10일 평균 × 1.30
PM_52W_LOW_OFFSET    = 0.05   # 52주 고점 대비 최소 -5% 이하
PM_52W_HIGH_OFFSET   = 0.40   # 52주 고점 대비 최대 -40% 이내
PM_RSI_MIN           = 35
PM_RSI_MAX           = 62
PM_MIN_PRICE         = 2_000
PM_MIN_AVG_TRADE_VAL = 3_000_000_000   # 일평균 거래대금 30억 이상 (유동성)
PM_CANDIDATES_POOL   = 250             # 사전 후보 풀 크기

# 당일 진입 상수
PM_GAP_MAX           = 0.03   # 시초가 갭 최대 ±3%
PM_VOL_INTRADAY_MIN  = 0.15   # 당일 누적 거래량 ≥ 전일의 15%


def _calc_rsi(close: pd.Series, period: int = 14) -> float:
    if len(close) < period + 2:
        return 50.0
    d = close.diff()
    gain = d.clip(lower=0).rolling(period, min_periods=period).mean()
    loss = (-d.clip(upper=0)).rolling(period, min_periods=period).mean()
    series = 100 - 100 / (1 + gain / (loss + 1e-10))
    val = series.iloc[-1]
    return float(val) if not pd.isna(val) else 50.0


def _score_candidate(df: pd.DataFrame, price: float) -> tuple[float, list[str]]:
    """후보 점수 산출. (score, reasons) 반환."""
    score, reasons = 0.0, []
    c = df['close'].dropna()
    v = df['volume'].dropna() if 'volume' in df.columns else pd.Series(dtype=float)

    # ① 박스권 폭이 좁을수록 가산
    if len(c) >= 20:
        hi20 = c.rolling(20).max().iloc[-1]
        lo20 = c.rolling(20).min().iloc[-1]
        box_range = (hi20 - lo20) / (lo20 + 1e-9)
        if box_range < 0.06:
            score += 3; reasons.append(f"초좁은 박스권({box_range*100:.1f}%) +3")
        elif box_range < 0.09:
            score += 2; reasons.append(f"좁은 박스권({box_range*100:.1f}%) +2")
        elif box_range < PM_BOX_RANGE_MAX:
            score += 1; reasons.append(f"박스권({box_range*100:.1f}%) +1")

    # ② 거래량 증가 추세
    if len(v) >= 14:
        vol_recent3 = float(v.iloc[-3:].mean())
        vol_prev10  = float(v.iloc[-13:-3].mean())
        if vol_prev10 > 0:
            trend = vol_recent3 / vol_prev10
            if trend >= 2.0:
                score += 3; reasons.append(f"거래량 2배↑({trend:.1f}x) +3")
            elif trend >= PM_VOL_TREND_RATIO:
                score += 2; reasons.append(f"거래량 증가({trend:.1f}x) +2")

    # ③ 52주 고점 대비 위치
    if len(c) >= 120:
        hi52 = float(c.rolling(240, min_periods=120).max().iloc[-1])
        ratio = (price - hi52) / (hi52 + 1e-9)  # 음수
        if -0.15 <= ratio <= -0.05:
            score += 2; reasons.append(f"고점 근접({ratio*100:.0f}%) +2")
        elif -0.30 <= ratio < -0.15:
            score += 1; reasons.append(f"고점 대비 조정({ratio*100:.0f}%) +1")

    # ④ RSI
    rsi = _calc_rsi(c)
    if 40 <= rsi <= 55:
        score += 2; reasons.append(f"RSI 중립({rsi:.0f}) +2")
    elif PM_RSI_MIN <= rsi < 40:
        score += 1; reasons.append(f"RSI 저평가({rsi:.0f}) +1")

    return score, reasons


def scan_premarket_candidates(
    kis=None,
    top_n: int = 10,
    verbose: bool = False,
) -> list[dict]:
    """
    장 마감 후 다음날 단타 후보 선정.

    Returns
    -------
    list of dict:
        ticker, name, price, prev_volume, score, reasons
    """
    from pykrx import stock as pykrx_stock

    _KST = timezone(timedelta(hours=9))
    now = datetime.now(_KST).replace(tzinfo=None)
    today_str = now.strftime("%Y%m%d")

    # ── 후보 풀 구성 (거래대금 상위) ──────────────────────────────────
    candidates: list[str] = []
    try:
        df_k = pykrx_stock.get_market_price_change_by_ticker(today_str, today_str, "KOSPI")
        df_q = pykrx_stock.get_market_price_change_by_ticker(today_str, today_str, "KOSDAQ")
        df_all = pd.concat([df_k, df_q])
        col = '거래대금' if '거래대금' in df_all.columns else df_all.columns[-1]
        candidates = df_all.sort_values(by=col, ascending=False).head(PM_CANDIDATES_POOL).index.tolist()
        if verbose:
            logger.info(f"[사전스캐너] 후보 풀 {len(candidates)}개")
    except Exception as e:
        logger.warning(f"[사전스캐너] 후보 풀 구성 실패: {e}")
        return []

    if not candidates:
        return []

    # ── 각 후보 평가 ──────────────────────────────────────────────────
    results = []
    for ticker in candidates:
        try:
            if kis is not None:
                df = kis.get_ohlcv(ticker, "D")
                if df is None or df.empty:
                    continue
                df = df.dropna(subset=['close']).tail(60)
            else:
                from stock_screener import fetch_ohlcv
                df = fetch_ohlcv(ticker, days=60, kis=None)

            if len(df) < 20:
                continue

            price = float(df['close'].iloc[-1])

            # 최소 주가 필터
            if price < PM_MIN_PRICE:
                continue

            # 일평균 거래대금 필터
            if 'volume' in df.columns:
                avg_tv = float((df['close'].iloc[-10:] * df['volume'].iloc[-10:]).mean())
                if avg_tv < PM_MIN_AVG_TRADE_VAL:
                    continue

            # 박스권 조건 선검사 (빠른 탈락)
            c = df['close'].dropna()
            if len(c) >= 20:
                hi20 = float(c.rolling(20).max().iloc[-1])
                lo20 = float(c.rolling(20).min().iloc[-1])
                box_range = (hi20 - lo20) / (lo20 + 1e-9)
                if box_range > PM_BOX_RANGE_MAX:
                    continue

            # RSI 선검사
            rsi = _calc_rsi(c)
            if not (PM_RSI_MIN <= rsi <= PM_RSI_MAX):
                continue

            # 52주 고점 대비 위치 선검사
            if len(c) >= 120:
                hi52 = float(c.rolling(240, min_periods=120).max().iloc[-1])
                ratio = (price - hi52) / (hi52 + 1e-9)
                if ratio > -PM_52W_LOW_OFFSET or ratio < -PM_52W_HIGH_OFFSET:
                    continue

            # 거래량 증가 선검사
            if 'volume' in df.columns and len(df) >= 14:
                v = df['volume'].dropna()
                vol_recent3 = float(v.iloc[-3:].mean())
                vol_prev10  = float(v.iloc[-13:-3].mean())
                if vol_prev10 > 0 and (vol_recent3 / vol_prev10) < 1.1:
                    continue

            # 종목명 조회
            name = ticker
            try:
                if kis is not None:
                    name = kis.get_stock_name(ticker) or ticker
                else:
                    name = pykrx_stock.get_market_ticker_name(ticker) or ticker
            except Exception:
                pass

            # 전일 거래량 (진입 조건 비교용)
            prev_volume = int(df['volume'].iloc[-1]) if 'volume' in df.columns else 0

            score, reasons = _score_candidate(df, price)
            if score < 3.0:
                continue

            results.append({
                'ticker':      ticker,
                'name':        name,
                'price':       price,
                'prev_volume': prev_volume,
                'score':       score,
                'reasons':     reasons,
            })

        except Exception as e:
            logger.debug(f"[사전스캐너] {ticker} 평가 오류: {e}")
            continue

    # 점수 내림차순 정렬
    results.sort(key=lambda x: x['score'], reverse=True)
    top = results[:top_n]

    if verbose or top:
        logger.info(f"[사전스캐너] 후보 {len(top)}개 선정: {[r['name'] for r in top]}")

    return top


def check_morning_entry(
    candidate: dict,
    kis,
    now_vol_threshold: float = PM_VOL_INTRADAY_MIN,
) -> tuple[bool, str]:
    """
    장 시작 후(09:00~09:15) 사전 선정 후보의 진입 가능 여부 확인.

    Parameters
    ----------
    candidate : scan_premarket_candidates() 반환 항목
    kis       : KIS API 객체
    now_vol_threshold : 당일 누적 거래량 / 전일 거래량 최소 비율

    Returns
    -------
    (ok: bool, reason: str)
    """
    ticker      = candidate['ticker']
    prev_vol    = candidate.get('prev_volume', 0)
    prev_close  = candidate.get('price', 0)

    try:
        rt = kis.get_realtime_price_data(ticker)
        if not rt:
            return False, "실시간 데이터 없음"

        open_p    = rt.get('open', 0)
        current_p = rt.get('close', 0)   # 현재가
        today_vol = rt.get('volume', 0)

        if open_p <= 0 or current_p <= 0:
            return False, "가격 데이터 없음"

        # ⑤ 갭 체크: 시초가가 전일 종가 대비 ±3% 이내
        if prev_close > 0:
            gap = abs(open_p - prev_close) / prev_close
            if gap > PM_GAP_MAX:
                return False, f"갭 과대 ({gap*100:.1f}% > {PM_GAP_MAX*100:.0f}%)"

        # ⑥ 거래량 체크: 당일 누적 거래량 ≥ 전일의 15%
        if prev_vol > 0:
            vol_ratio = today_vol / prev_vol
            if vol_ratio < now_vol_threshold:
                return False, f"거래량 부족 ({vol_ratio*100:.1f}% < {now_vol_threshold*100:.0f}%)"

        # ⑦ 양봉 체크: 현재가 ≥ 시초가
        if current_p < open_p * 0.995:
            return False, f"음봉 진행 (시초가 {open_p:,} > 현재 {current_p:,})"

        return True, f"시초가 {open_p:,} | 거래량 {vol_ratio*100:.0f}% | 현재 {current_p:,}"

    except Exception as e:
        return False, f"체크 오류: {e}"
