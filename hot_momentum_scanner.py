"""
hot_momentum_scanner.py — 테마주·급등주 초기 포착 실시간 스캐너
────────────────────────────────────────────────────────────────
핵심 로직:
  ① 전체 시장(KOSPI+KOSDAQ)에서 실시간으로 급등 초기 신호 탐색
  ② 거래량 폭발(20일 평균 대비 3배 이상) + 가격 급등(+3% 이상) 동시 조건
  ③ 발동 시점이 최근 30분 이내인 종목만 유효 (늦으면 이미 고점)
  ④ 거래량 지속성 확인: 직전 봉 대비 거래량 유지 여부
  ⑤ 점수 계산: 거래량비율 + 가격등락률 + RSI 급등 + 섹터 테마 가중치

반환값 (상위 N개):
  { ticker, name, price, vol_ratio, price_chg_pct, rsi, momentum_score,
    trigger_reason, first_seen_time }
"""

from __future__ import annotations  # I-04: Python 3.8 이하에서 list[dict] 등 타입힌트 호환

import time
import threading
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import numpy as np

logger = logging.getLogger('lassi_bot')

# ── 상수 ──────────────────────────────────────────────────────────
MOMENTUM_VOL_RATIO     = 3.0   # 20일 평균 대비 거래량 배율 기준
MOMENTUM_PRICE_CHG     = 3.0   # 당일 최소 상승률(%)
MOMENTUM_MAX_VALID_MIN = 30    # 신호 발동 후 유효 시간(분) — 이 시간 지나면 무효
MOMENTUM_RSI_MIN       = 50    # RSI 최소값 (급등 추세 확인)
MOMENTUM_RSI_MAX       = 80    # RSI 최대값 (과열 종목 제외)
MOMENTUM_MIN_PRICE     = 1000  # 최소 주가(동전주 제외)
MOMENTUM_MIN_TRADING_VAL = 500_000_000  # 일평균 거래대금 최소 5억 (유동성)

# 테마 키워드 → 가중 점수 보너스
THEME_BONUS = {
    "AI": 5, "로봇": 5, "방산": 4, "2차전지": 4, "반도체": 4,
    "바이오": 3, "제약": 3, "우주": 3, "수소": 3,
}

# ── 캐시 (프로세스 내 싱글턴) ─────────────────────────────────────
_scan_cache: dict = {}       # { ticker: { 'first_seen': datetime, 'score': float, ... } }
_cache_lock = threading.Lock()


def _calc_rsi(close: pd.Series, period: int = 14) -> float:
    """RSI(period) 마지막 값 반환. 데이터 부족 시 50.0 반환."""
    if len(close) < period + 2:
        return 50.0
    d = close.diff()
    gain = d.clip(lower=0).rolling(period, min_periods=period).mean()
    loss = (-d.clip(upper=0)).rolling(period, min_periods=period).mean()
    rsi_series = 100 - 100 / (1 + gain / (loss + 1e-10))
    val = rsi_series.iloc[-1]
    return float(val) if not pd.isna(val) else 50.0


def _theme_bonus(name: str):
    """종목명에서 테마 키워드 탐색 → (보너스점수, 테마명)"""
    for keyword, bonus in THEME_BONUS.items():
        if keyword in name:
            return bonus, keyword
    return 0, ""


def _get_vol_ratio_and_chg(df: pd.DataFrame) -> tuple[float, float]:
    """
    DataFrame(OHLCV, 'close'/'volume' 컬럼)에서
    거래량비율(20일 평균 대비 오늘)과 당일 가격 변화율(%) 계산.
    """
    if len(df) < 5 or 'close' not in df.columns or 'volume' not in df.columns:
        return 0.0, 0.0

    # 거래량 비율: 마지막 봉 vs 이전 20일 평균
    vol_series = df['volume']
    if len(vol_series) >= 21:
        vol_avg20 = vol_series.iloc[-21:-1].mean()
    else:
        vol_avg20 = vol_series.iloc[:-1].mean()
    today_vol = vol_series.iloc[-1]
    vol_ratio = today_vol / (vol_avg20 + 1e-9)

    # 가격 변화율: 전일 종가 → 현재가
    prev_close = df['close'].iloc[-2]
    today_close = df['close'].iloc[-1]
    price_chg = (today_close / (prev_close + 1e-9) - 1) * 100

    return float(vol_ratio), float(price_chg)


def scan_hot_momentum(
    kis=None,
    top_n: int = 3,
    verbose: bool = False,
) -> list[dict]:
    """
    장중 실시간 테마주·급등주 초기 포착 스캐너.

    Parameters
    ----------
    kis       : KIS API 객체 (None이면 pykrx 백업)
    top_n     : 반환할 상위 종목 수
    verbose   : 로그 출력 여부

    Returns
    -------
    list of dict 형태로 최대 top_n개 반환:
        ticker, name, price, vol_ratio, price_chg_pct,
        rsi, momentum_score, trigger_reason, first_seen_time
    """
    from pykrx import stock as pykrx_stock

    _KST = timezone(timedelta(hours=9))
    now = datetime.now(_KST).replace(tzinfo=None)  # [BUG-M5] EC2(UTC)에서 KST 날짜 기준으로 조회
    results = []

    # ── 후보 풀 구성 ────────────────────────────────────────────────
    # KIS API로 실시간 거래량·등락률 상위 종목 우선 수집 (빠른 포착)
    candidates: list[str] = []

    if kis is not None:
        try:
            vol_top_k  = kis.get_volume_rank(market_div="J", limit=50)   # KOSPI 거래량 상위 50
            vol_top_q  = kis.get_volume_rank(market_div="Q", limit=50)   # KOSDAQ 거래량 상위 50
            rise_top_k = kis.get_price_change_rank(market_div="J", limit=30)
            rise_top_q = kis.get_price_change_rank(market_div="Q", limit=30)
            seen = set()
            for t in vol_top_k + vol_top_q + rise_top_k + rise_top_q:
                if t not in seen:
                    seen.add(t)
                    candidates.append(t)
            if verbose:
                print(f"🔥 [모멘텀 스캐너] 실시간 후보 {len(candidates)}개 수집")
        except Exception as e:
            logger.warning(f"[모멘텀 스캐너] 실시간 후보 수집 실패: {e}")

    # KIS 실패 또는 None이면 pykrx로 당일 거래대금 상위 100 폴백
    if len(candidates) < 10:
        try:
            today_str = now.strftime("%Y%m%d")
            df_k = pykrx_stock.get_market_price_change_by_ticker(today_str, today_str, "KOSPI")
            df_q = pykrx_stock.get_market_price_change_by_ticker(today_str, today_str, "KOSDAQ")
            df_all = pd.concat([df_k, df_q])
            col = '거래대금' if '거래대금' in df_all.columns else df_all.columns[-1]
            top100 = df_all.sort_values(by=col, ascending=False).head(100).index.tolist()
            for t in top100:
                if t not in candidates:
                    candidates.append(t)
            if verbose:
                print(f"   pykrx 폴백 후보 추가 → 총 {len(candidates)}개")
        except Exception as e:
            logger.warning(f"[모멘텀 스캐너] pykrx 폴백 실패: {e}")

    if not candidates:
        return []

    # ── 각 후보 평가 ────────────────────────────────────────────────
    for ticker in candidates:
        try:
            # OHLCV 수집
            if kis is not None:
                df = kis.get_ohlcv(ticker, "D")
                if df is None or df.empty:
                    raise ValueError("KIS OHLCV 없음")
                if 'close' not in df.columns:
                    raise ValueError("컬럼 없음")
                df = df.dropna(subset=['close']).tail(30)
            else:
                # M-04: end/start 변수 제거 (fetch_ohlcv에서 사용 안 함 — 데드코드였음)
                from stock_screener import fetch_ohlcv
                df = fetch_ohlcv(ticker, days=30, kis=None)

            if len(df) < 5:
                continue

            price = float(df['close'].iloc[-1])
            if price < MOMENTUM_MIN_PRICE:
                continue

            # 거래대금 최소 조건
            if 'volume' in df.columns:
                avg_trade_val = (df['close'].iloc[-5:] * df['volume'].iloc[-5:]).mean()
                if avg_trade_val < MOMENTUM_MIN_TRADING_VAL:
                    continue

            vol_ratio, price_chg = _get_vol_ratio_and_chg(df)

            # 핵심 필터: 거래량 3x 이상 + 가격 +3% 이상
            if vol_ratio < MOMENTUM_VOL_RATIO or price_chg < MOMENTUM_PRICE_CHG:
                continue

            # RSI 필터 (과열·과매도 제외)
            rsi = _calc_rsi(df['close'])
            if rsi < MOMENTUM_RSI_MIN or rsi > MOMENTUM_RSI_MAX:
                continue

            # 종목명 조회
            try:
                name = pykrx_stock.get_market_ticker_name(ticker)
            except Exception:
                name = ticker

            # 테마 가중치
            theme_bonus, theme_name = _theme_bonus(name)

            # 모멘텀 점수 산출
            # 거래량비율(최대 20점) + 가격등락률(최대 20점) + 테마보너스 + RSI위치
            score = (min(vol_ratio, 10.0) * 2.0       # 최대 20점
                     + min(price_chg, 10.0) * 2.0      # 최대 20점
                     + theme_bonus                      # 테마 보너스 0~5점
                     + (rsi - 50) * 0.1)                # RSI 위치 가중

            # 이전 감지 시간 관리 (처음 발견 시 기록)
            with _cache_lock:
                if ticker in _scan_cache:
                    first_seen = _scan_cache[ticker]['first_seen']
                    # 발동 후 유효 시간 초과 → 무효 처리
                    elapsed_min = (now - first_seen).total_seconds() / 60
                    if elapsed_min > MOMENTUM_MAX_VALID_MIN:
                        del _scan_cache[ticker]
                        # [BUG-12] 캐시 만료 처리 흐름:
                        # ① 기존 캐시 삭제 후 ② 이번 스캔에서 신호가 여전히 유효하면
                        # 재등록하여 재진입 허용. continue 를 쓰면 이번 스캔 결과에서
                        # 해당 종목이 제외되어 만료 직후 신규진입 기회를 놓치는 버그 발생.
                        _scan_cache[ticker] = {'first_seen': now, 'score': score}
                        first_seen = now
                else:
                    _scan_cache[ticker] = {
                        'first_seen': now,
                        'score': score,
                    }
                    first_seen = now

            elapsed_min = (now - first_seen).total_seconds() / 60

            # ── 신선도 보너스: 최초 감지 후 경과 시간이 짧을수록 우선 진입 ──
            # 늦게 진입할수록 고점 리스크 상승 → 오래된 신호는 점수 억제
            if elapsed_min <= 5:
                freshness_bonus = 8.0    # 방금 발견 — 최우선 진입
            elif elapsed_min <= 10:
                freshness_bonus = 5.0    # 초기 구간 — 여전히 신선
            elif elapsed_min <= 20:
                freshness_bonus = 1.0    # 진행 중 — 중립
            else:
                freshness_bonus = -5.0   # 20분↑ — 진입 억제 (고점 리스크)

            score += freshness_bonus

            # 트리거 사유 문자열
            reasons = [f"거래량 {vol_ratio:.1f}x↑", f"상승 +{price_chg:.1f}%"]
            if theme_name:
                reasons.append(f"테마:{theme_name}")
            if rsi >= 60:
                reasons.append(f"RSI {rsi:.0f} 강세")
            reasons.append(f"감지+{elapsed_min:.0f}분")  # 신선도 표시

            results.append({
                'ticker':         ticker,
                'name':           name,
                'price':          price,
                'vol_ratio':      round(vol_ratio, 2),
                'price_chg_pct':  round(price_chg, 2),
                'rsi':            round(rsi, 1),
                'momentum_score': round(score, 2),
                'trigger_reason': " | ".join(reasons),
                'first_seen_time': first_seen.strftime('%H:%M:%S'),
                'elapsed_min':    round(elapsed_min, 1),
            })

        except Exception:
            continue

        time.sleep(0.05)

    # 점수 내림차순 정렬
    results.sort(key=lambda x: x['momentum_score'], reverse=True)
    top = results[:top_n]

    if verbose and top:
        print(f"\n🚀 [테마·급등주 초기 포착] 상위 {len(top)}개:")
        for r in top:
            print(f"   {r['name']}({r['ticker']}) | "
                  f"점수 {r['momentum_score']:.1f} | "
                  f"{r['trigger_reason']} | "
                  f"첫감지 {r['first_seen_time']} (+{r['elapsed_min']:.0f}분)")

    return top


def clear_expired_cache():
    """30분 초과된 캐시 항목 제거 (주기적으로 호출)."""
    now = datetime.now(timezone(timedelta(hours=9))).replace(tzinfo=None)  # [BUG-M5] KST 기준
    with _cache_lock:
        expired = [t for t, v in _scan_cache.items()
                   if (now - v['first_seen']).total_seconds() / 60 > MOMENTUM_MAX_VALID_MIN]
        for t in expired:
            del _scan_cache[t]


# ── 단독 테스트 ───────────────────────────────────────────────────
if __name__ == '__main__':
    print("🔥 테마·급등주 초기 포착 스캐너 단독 테스트")
    hits = scan_hot_momentum(kis=None, top_n=5, verbose=True)
    if not hits:
        print("현재 조건에 맞는 급등주 없음 (장 마감 또는 조건 미충족)")
    else:
        for h in hits:
            print(h)
