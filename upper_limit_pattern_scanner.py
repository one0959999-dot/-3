"""
upper_limit_pattern_scanner.py — 상한가/급등 전일 패턴 분석 → 유사 종목 선제 매수
─────────────────────────────────────────────────────────────────────────────────────
전략 개요
  ① 장 마감 후(15:30~): 오늘 상한가(+29%↑) 또는 급등(+12%↑) 종목 수집
  ② 해당 종목들의 '전일(T-1)' 기술 지표 추출
     RSI(14) / 거래량비율 / BB% 위치 / MA 정배열 / MACD 방향 / MA20 이격률 / 3일 수익률
  ③ 평균 프로파일 → "급등 직전 패턴" JSON 저장
  ④ 다음 날 장 시작 전: 전체 시장에서 같은 패턴 보유 종목 스캔
  ⑤ 상위 N개를 모멘텀 슬롯 우선 후보로 반환

주의
  - 생존자 편향(Survivorship Bias) 존재 — 승률 100% 아님
  - 모멘텀 슬롯(20%) 안에서만 사용, 포지션 사이징 절제 필수
  - 급등 원인이 뉴스/테마인 경우 패턴 불일치 가능
"""

from __future__ import annotations

import json
import os
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import numpy as np

logger = logging.getLogger('lassi_bot')

# ── 설정 상수 ──────────────────────────────────────────────────────────────
SURGE_THRESHOLD_PCT   = 12.0   # 급등 기준 (이 이상 상승한 종목 수집)
UPPER_LIMIT_PCT       = 28.0   # 상한가 기준 (한국 +30%, 여유 -2%)
MIN_PRICE             = 1_000  # 동전주 제외
MIN_MARKET_CAP        = 500_000_000_000  # 시총 최소 5,000억 (소형주 제외)
MIN_TRADING_VALUE     = 2_000_000_000    # 일 거래대금 최소 20억
MAX_SURGE_STOCKS      = 30     # 패턴 추출에 사용할 급등 종목 최대 수
MATCH_TOP_N           = 10     # 패턴 매칭 후 반환할 상위 종목 수
MATCH_SCORE_THRESHOLD = 4      # 최소 매칭 점수 (10점 만점)

# 패턴 파일 저장 경로 (봇 루트 기준)
_BASE_DIR    = Path(__file__).parent
PATTERN_FILE = _BASE_DIR / "upper_limit_pattern.json"

_KST = timezone(timedelta(hours=9))


# ─────────────────────────────────────────────────────────────────────────────
# 내부 유틸
# ─────────────────────────────────────────────────────────────────────────────

def _now_kst() -> datetime:
    return datetime.now(_KST).replace(tzinfo=None)


def _calc_rsi14(close: pd.Series) -> float:
    if len(close) < 16:
        return 50.0
    d = close.diff()
    gain = d.clip(lower=0).rolling(14, min_periods=14).mean()
    loss = (-d.clip(upper=0)).rolling(14, min_periods=14).mean()
    rs = gain / (loss + 1e-10)
    val = (100 - 100 / (1 + rs)).iloc[-1]
    return float(val) if not pd.isna(val) else 50.0


def _extract_pattern(df: pd.DataFrame) -> dict | None:
    """
    OHLCV DataFrame에서 '마지막 봉' 기준 패턴 지표 추출.
    (급등 종목: 급등 당일 데이터를 제외하고 전일 데이터의 마지막 봉이 T-1이 됨)
    """
    try:
        if df is None or df.empty or len(df) < 26:
            return None

        c = df['close'].dropna()
        v = df['volume'].dropna() if 'volume' in df.columns else pd.Series(dtype=float)

        # ① RSI(14)
        rsi = _calc_rsi14(c)

        # ② 거래량 비율 (오늘 vs 20일 평균)
        vol_ratio = 0.0
        if len(v) >= 21:
            avg_vol   = float(v.iloc[-21:-1].mean())
            today_vol = float(v.iloc[-1])
            vol_ratio = today_vol / (avg_vol + 1e-9)

        # ③ 볼린저밴드 % 위치 (0~100)
        bb_pct = 50.0
        if len(c) >= 22:
            ma20    = float(c.rolling(20).mean().iloc[-1])
            std20   = float(c.rolling(20).std().iloc[-1])
            bb_upper = ma20 + 2 * std20
            bb_lower = ma20 - 2 * std20
            bb_range = bb_upper - bb_lower
            if bb_range > 0:
                bb_pct = (float(c.iloc[-1]) - bb_lower) / bb_range * 100

        # ④ MA 정배열 (5MA > 20MA)
        ma_aligned = False
        if len(c) >= 22:
            ma5  = float(c.rolling(5).mean().iloc[-1])
            ma20 = float(c.rolling(20).mean().iloc[-1])
            ma_aligned = ma5 > ma20

        # ⑤ MACD 히스토그램 부호 (+1 / -1 / 0)
        macd_hist_sign = 0
        if len(c) >= 30:
            ema12  = c.ewm(span=12, adjust=False).mean()
            ema26  = c.ewm(span=26, adjust=False).mean()
            macd   = ema12 - ema26
            signal = macd.ewm(span=9, adjust=False).mean()
            hist   = float(macd.iloc[-1] - signal.iloc[-1])
            macd_hist_sign = 1 if hist > 0 else (-1 if hist < 0 else 0)

        # ⑥ MA20 이격률 (%)
        ma20_gap = 0.0
        if len(c) >= 22:
            ma20 = float(c.rolling(20).mean().iloc[-1])
            if ma20 > 0:
                ma20_gap = (float(c.iloc[-1]) - ma20) / ma20 * 100

        # ⑦ 최근 3거래일 수익률 (%)
        ret3d = 0.0
        if len(c) >= 4:
            ret3d = (float(c.iloc[-1]) / float(c.iloc[-4]) - 1) * 100

        return {
            'rsi':           round(rsi, 1),
            'vol_ratio':     round(vol_ratio, 2),
            'bb_pct':        round(bb_pct, 1),
            'ma_aligned':    int(ma_aligned),    # 1 or 0
            'macd_sign':     macd_hist_sign,      # 1, 0, -1
            'ma20_gap':      round(ma20_gap, 2),
            'ret3d':         round(ret3d, 2),
        }
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# ① 장 마감 후 호출: 급등 종목 수집 + 전일 패턴 추출 → 저장
# ─────────────────────────────────────────────────────────────────────────────

def collect_and_save_pattern(kis=None, verbose: bool = False) -> dict | None:
    """
    오늘 급등/상한가 종목들의 '전일' 패턴을 수집하고 JSON 파일로 저장.
    kr_bot.py 의 15:35 슬롯에서 스레드로 호출.

    Returns
    -------
    저장된 패턴 프로파일 dict 또는 None (실패 시)
    """
    from pykrx import stock as pykrx_stock
    from stock_screener import fetch_ohlcv

    now      = _now_kst()
    today    = now.strftime('%Y%m%d')
    today_dt = now.strftime('%Y-%m-%d')

    if verbose:
        print(f"\n📊 [급등패턴] {today_dt} 급등/상한가 종목 수집 시작...")

    # ── 오늘 급등 종목 수집 ──────────────────────────────────────────
    surge_tickers: list[str] = []
    try:
        df_k = pykrx_stock.get_market_price_change_by_ticker(today, today, "KOSPI")
        df_q = pykrx_stock.get_market_price_change_by_ticker(today, today, "KOSDAQ")
        df_all = pd.concat([df_k, df_q])

        # 컬럼명 정규화
        chg_col = next((c for c in df_all.columns if '등락률' in c or 'change' in c.lower()), None)
        val_col = next((c for c in df_all.columns if '거래대금' in c or 'value' in c.lower()), None)

        if chg_col:
            surged = df_all[df_all[chg_col] >= SURGE_THRESHOLD_PCT]
            if val_col:
                surged = surged[surged[val_col] >= MIN_TRADING_VALUE]
            surged = surged.sort_values(by=chg_col, ascending=False)
            surge_tickers = surged.index.tolist()[:MAX_SURGE_STOCKS]

        if verbose:
            print(f"   급등 후보 {len(surge_tickers)}개 (기준: +{SURGE_THRESHOLD_PCT}%↑)")
    except Exception as e:
        logger.warning(f"[급등패턴] 급등 종목 수집 실패: {e}")
        return None

    if not surge_tickers:
        if verbose:
            print("   오늘 급등 종목 없음 — 패턴 저장 스킵")
        return None

    # ── 각 급등 종목의 '전일' 패턴 추출 ────────────────────────────
    patterns: list[dict] = []
    upper_limit_count = 0

    for ticker in surge_tickers:
        try:
            # OHLCV 40일치 조회 후 오늘 봉 제거 → 전일이 마지막이 됨
            df = fetch_ohlcv(ticker, days=42, kis=kis)
            if df is None or df.empty or len(df) < 25:
                continue

            price = float(df['close'].iloc[-1])
            if price < MIN_PRICE:
                continue

            # 오늘 봉 제거 (전일 패턴 추출용)
            df_prev = df.iloc[:-1].copy()
            if len(df_prev) < 25:
                continue

            pat = _extract_pattern(df_prev)
            if pat is None:
                continue

            # 상한가 여부 태깅
            today_chg = (float(df['close'].iloc[-1]) / float(df['close'].iloc[-2]) - 1) * 100
            pat['is_upper_limit'] = today_chg >= UPPER_LIMIT_PCT
            if pat['is_upper_limit']:
                upper_limit_count += 1

            pat['ticker'] = ticker
            patterns.append(pat)

            if verbose:
                flag = "🚀 상한가" if pat['is_upper_limit'] else f"+{today_chg:.1f}%"
                print(f"   {ticker}: {flag} | RSI {pat['rsi']:.0f} | BB {pat['bb_pct']:.0f}% "
                      f"| vol {pat['vol_ratio']:.1f}x | 정배열 {'✅' if pat['ma_aligned'] else '❌'}")

        except Exception:
            continue
        time.sleep(0.03)

    if not patterns:
        logger.warning("[급등패턴] 패턴 추출 실패 — 데이터 부족")
        return None

    # ── 평균 프로파일 계산 ───────────────────────────────────────────
    keys = ['rsi', 'vol_ratio', 'bb_pct', 'ma_aligned', 'macd_sign', 'ma20_gap', 'ret3d']
    profile = {}
    for k in keys:
        vals = [p[k] for p in patterns if k in p]
        profile[k]          = round(float(np.mean(vals)), 2) if vals else 0.0
        profile[f'{k}_std'] = round(float(np.std(vals)), 2) if vals else 0.0

    # 상한가 비율
    profile['upper_limit_ratio'] = round(upper_limit_count / len(patterns), 2)
    profile['sample_count']      = len(patterns)
    profile['date']              = today_dt
    profile['tickers']           = [p['ticker'] for p in patterns[:10]]  # 상위 10개만 저장

    # ── JSON 저장 ─────────────────────────────────────────────────────
    try:
        with open(PATTERN_FILE, 'w', encoding='utf-8') as f:
            json.dump(profile, f, ensure_ascii=False, indent=2)
        logger.info(f"[급등패턴] 패턴 저장 완료: {len(patterns)}개 종목 | 상한가 {upper_limit_count}개 "
                    f"| 파일: {PATTERN_FILE}")
        if verbose:
            print(f"\n✅ 패턴 저장: {PATTERN_FILE}")
            print(f"   평균 RSI: {profile['rsi']:.1f} | BB: {profile['bb_pct']:.1f}% "
                  f"| vol배율: {profile['vol_ratio']:.1f}x | 정배열비율: {profile['ma_aligned']:.0%}")
    except Exception as e:
        logger.warning(f"[급등패턴] 저장 실패: {e}")
        return None

    return profile


# ─────────────────────────────────────────────────────────────────────────────
# ② 다음 날 아침 호출: 저장된 패턴 로드 → 유사 종목 스캔
# ─────────────────────────────────────────────────────────────────────────────

def load_pattern() -> dict | None:
    """저장된 패턴 프로파일 로드. 파일 없거나 2일 이상 지난 경우 None 반환."""
    try:
        if not PATTERN_FILE.exists():
            return None
        with open(PATTERN_FILE, 'r', encoding='utf-8') as f:
            profile = json.load(f)
        # 2거래일 이상 지난 패턴은 무효
        saved_date = datetime.strptime(profile['date'], '%Y-%m-%d')
        days_old   = (_now_kst() - saved_date).days
        if days_old > 2:
            logger.info(f"[급등패턴] 패턴 파일 만료 ({days_old}일 경과) — 무시")
            return None
        return profile
    except Exception:
        return None


def _match_score(candidate_pat: dict, profile: dict) -> tuple[int, list[str]]:
    """
    후보 종목의 패턴을 프로파일과 비교해 유사도 점수(0~10) 계산.

    채점 기준 (각 1점):
      ① RSI가 프로파일 평균 ±15 이내
      ② 거래량 비율이 프로파일 평균 ±1.0 이내 (단, 최소 0.8x 이상)
      ③ BB% 위치가 프로파일 평균 ±20% 이내
      ④ MA 정배열 일치
      ⑤ MACD 부호 일치
      ⑥ MA20 이격률이 프로파일 ±5% 이내
      ⑦ 3일 수익률이 프로파일 ±5% 이내
      ⑧ RSI 40~70 건강 구간 (급등 직전 과열/과매도 아님)
      ⑨ 거래량 비율 ≥ 0.8 (거래 있는 종목)
      ⑩ BB% 위치 20~80% 구간 (극단 회피)
    """
    score  = 0
    passed = []

    rsi  = candidate_pat.get('rsi', 50)
    vol  = candidate_pat.get('vol_ratio', 1.0)
    bb   = candidate_pat.get('bb_pct', 50)
    ma   = candidate_pat.get('ma_aligned', 0)
    macd = candidate_pat.get('macd_sign', 0)
    gap  = candidate_pat.get('ma20_gap', 0)
    ret3 = candidate_pat.get('ret3d', 0)

    p_rsi  = profile.get('rsi', 50)
    p_vol  = profile.get('vol_ratio', 1.0)
    p_bb   = profile.get('bb_pct', 50)
    p_ma   = profile.get('ma_aligned', 0.5)
    p_macd = profile.get('macd_sign', 0)
    p_gap  = profile.get('ma20_gap', 0)
    p_ret3 = profile.get('ret3d', 0)

    if abs(rsi - p_rsi) <= 15:
        score += 1; passed.append(f"RSI유사({rsi:.0f}≈{p_rsi:.0f})")
    if abs(vol - p_vol) <= 1.0 and vol >= 0.8:
        score += 1; passed.append(f"거래량유사({vol:.1f}x≈{p_vol:.1f}x)")
    if abs(bb - p_bb) <= 20:
        score += 1; passed.append(f"BB유사({bb:.0f}%≈{p_bb:.0f}%)")
    if ma == round(p_ma):
        score += 1; passed.append("정배열일치" if ma else "역배열일치")
    if macd == (1 if p_macd > 0 else (-1 if p_macd < 0 else 0)):
        score += 1; passed.append("MACD방향일치")
    if abs(gap - p_gap) <= 5:
        score += 1; passed.append(f"이격률유사({gap:+.1f}%≈{p_gap:+.1f}%)")
    if abs(ret3 - p_ret3) <= 5:
        score += 1; passed.append(f"3일수익률유사({ret3:+.1f}%≈{p_ret3:+.1f}%)")
    if 40 <= rsi <= 70:
        score += 1; passed.append(f"RSI건강({rsi:.0f})")
    if vol >= 0.8:
        score += 1; passed.append("거래량정상")
    if 20 <= bb <= 80:
        score += 1; passed.append(f"BB정상구간({bb:.0f}%)")

    return score, passed


def scan_pattern_matches(
    kis=None,
    top_n: int = MATCH_TOP_N,
    exclude_tickers: set | None = None,
    verbose: bool = False,
) -> list[dict]:
    """
    저장된 패턴 프로파일과 유사한 종목을 시장 전체에서 스캔.

    Parameters
    ----------
    kis              : KIS API (None이면 pykrx 백업)
    top_n            : 반환할 상위 종목 수
    exclude_tickers  : 이미 보유/블랙리스트 종목 제외용 set
    verbose          : 로그 출력 여부

    Returns
    -------
    list of dict — momentum_candidates 형식과 호환:
        ticker, name, price, match_score, match_reasons,
        rsi, vol_ratio, bb_pct, trigger_reason, pattern_date
    """
    from pykrx import stock as pykrx_stock
    from stock_screener import fetch_ohlcv

    profile = load_pattern()
    if profile is None:
        if verbose:
            print("[급등패턴] 저장된 패턴 없음 — 스캔 스킵")
        return []

    if verbose:
        print(f"\n🔍 [급등패턴매칭] {profile['date']} 패턴 로드 | "
              f"샘플 {profile['sample_count']}개 | "
              f"RSI {profile['rsi']:.0f} / BB {profile['bb_pct']:.0f}% / vol {profile['vol_ratio']:.1f}x")

    exclude = exclude_tickers or set()
    now     = _now_kst()
    today   = now.strftime('%Y%m%d')

    # ── 후보 풀 구성 ─────────────────────────────────────────────────
    candidates: list[str] = []
    try:
        if kis is not None:
            # 거래량 상위 + 등락률 상위 종목 합산
            vol_k = kis.get_volume_rank(market_div="J", limit=80)
            vol_q = kis.get_volume_rank(market_div="Q", limit=80)
            candidates = list(dict.fromkeys(vol_k + vol_q))
        else:
            df_k = pykrx_stock.get_market_price_change_by_ticker(today, today, "KOSPI")
            df_q = pykrx_stock.get_market_price_change_by_ticker(today, today, "KOSDAQ")
            df_all = pd.concat([df_k, df_q])
            val_col = next((c for c in df_all.columns if '거래대금' in c), None)
            if val_col:
                candidates = df_all.sort_values(val_col, ascending=False).head(150).index.tolist()
            else:
                candidates = df_all.head(150).index.tolist()
    except Exception as e:
        logger.warning(f"[급등패턴] 후보 풀 수집 실패: {e}")
        return []

    if not candidates:
        return []

    if verbose:
        print(f"   후보 풀 {len(candidates)}개 → 패턴 매칭 시작")

    # ── 각 후보 패턴 추출 + 매칭 점수 계산 ─────────────────────────
    results: list[dict] = []

    for ticker in candidates:
        if ticker in exclude:
            continue
        try:
            df = fetch_ohlcv(ticker, days=42, kis=kis)
            if df is None or df.empty or len(df) < 25:
                continue

            price = float(df['close'].iloc[-1])
            if price < MIN_PRICE:
                continue

            # 거래대금 필터
            if 'volume' in df.columns:
                avg_val = (df['close'].iloc[-5:] * df['volume'].iloc[-5:]).mean()
                if avg_val < MIN_TRADING_VALUE:
                    continue

            pat = _extract_pattern(df)
            if pat is None:
                continue

            # 이미 오늘 +15% 이상 오른 종목은 제외 (이미 터진 것)
            if len(df) >= 2:
                today_chg = (float(df['close'].iloc[-1]) / float(df['close'].iloc[-2]) - 1) * 100
                if today_chg >= SURGE_THRESHOLD_PCT:
                    continue

            match_score, match_reasons = _match_score(pat, profile)
            if match_score < MATCH_SCORE_THRESHOLD:
                continue

            try:
                name = pykrx_stock.get_market_ticker_name(ticker)
            except Exception:
                name = ticker

            results.append({
                'ticker':         ticker,
                'name':           name,
                'price':          price,
                'match_score':    match_score,
                'match_reasons':  match_reasons,
                'rsi':            pat['rsi'],
                'vol_ratio':      pat['vol_ratio'],
                'bb_pct':         pat['bb_pct'],
                # hot_momentum_scanner 호환 필드
                'momentum_score': float(match_score) * 3.0,   # 10점 만점 → 30점 스케일 호환
                'price_chg_pct':  (float(df['close'].iloc[-1]) / float(df['close'].iloc[-2]) - 1) * 100
                                  if len(df) >= 2 else 0.0,
                'trigger_reason': f"패턴매칭({match_score}/10점) | " + " | ".join(match_reasons[:3]),
                'pattern_date':   profile['date'],
                'first_seen_time': now.strftime('%H:%M:%S'),
                'elapsed_min':    0,
            })

        except Exception:
            continue
        time.sleep(0.03)

    # 매칭 점수 내림차순
    results.sort(key=lambda x: x['match_score'], reverse=True)
    top = results[:top_n]

    if verbose and top:
        print(f"\n🎯 [급등패턴매칭] 상위 {len(top)}개:")
        for r in top:
            print(f"   {r['name']}({r['ticker']}) | "
                  f"매칭 {r['match_score']}/10 | {r['trigger_reason'][:60]}")

    logger.info(f"[급등패턴] 매칭 완료: 후보 {len(candidates)}개 → 통과 {len(results)}개 → 상위 {len(top)}개")
    return top


# ─────────────────────────────────────────────────────────────────────────────
# 단독 테스트
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print("=== 급등패턴 스캐너 테스트 ===")
    print("\n[1] 오늘 급등 종목 패턴 수집 & 저장")
    prof = collect_and_save_pattern(kis=None, verbose=True)
    if prof:
        print(f"\n[2] 저장된 패턴으로 유사 종목 스캔")
        matches = scan_pattern_matches(kis=None, top_n=5, verbose=True)
        if not matches:
            print("매칭 종목 없음")
    else:
        print("패턴 저장 실패")
