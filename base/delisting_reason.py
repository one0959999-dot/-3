"""상폐 사유 분류 — 주가경로 추론 + DART 공시 결합.

상폐는 유형마다 정반대 교훈(파산 vs 피인수)이므로 반드시 구분해 태깅한다.
- 주가경로: 마지막가/고점 비율로 부실(0수렴) vs 피인수(고가유지) 추론
- DART: 관리종목/감사의견거절/합병/자진상폐 등 실제 사유 공시 (선택, 키 있을 때)
"""
import logging

logger = logging.getLogger('lassi_bot')

# 사유 유형
BANKRUPT  = '부실상폐'      # 파산/자본잠식/감사의견거절 — 0 수렴
ACQUIRED  = '피인수상폐'    # M&A/공개매수 — 프리미엄·고가유지
VOLUNTARY = '자진상폐'      # 대주주 자진 — 안정적
UNKNOWN   = '상폐사유미상'

_DART_NEG = ('감사의견', '자본잠식', '파산', '회생절차', '상장폐지', '관리종목', '거래정지', '횡령', '배임')
_DART_MNA = ('합병', '주식교환', '주식의포괄적', '공개매수', '영업양도')


def infer_from_price(df) -> tuple:
    """주가경로로 상폐유형 1차 추론.
    반환: (reason_type, 상세dict)
    """
    try:
        import pandas as pd
        close = df['close'].dropna()
        if len(close) < 20:
            return UNKNOWN, {}
        last = float(close.iloc[-1])
        peak = float(close.tail(252).max())          # 최근 1년 고점
        last_vs_peak = last / peak if peak else 1.0
        # 막판 30일 급등 여부(공개매수 프리미엄 패턴)
        if len(close) >= 31:
            run30 = last / float(close.iloc[-31]) - 1
        else:
            run30 = 0.0
        detail = {'last_price': round(last, 2), 'peak_1y': round(peak, 2),
                  'last_vs_peak_pct': round(last_vs_peak * 100, 1),
                  'final_30d_pct': round(run30 * 100, 1)}

        if last_vs_peak <= 0.25:           # 고점 대비 -75% 이하로 죽음
            return BANKRUPT, detail
        if run30 >= 0.15 or last_vs_peak >= 0.8:   # 막판 급등 or 고가 유지
            return ACQUIRED, detail
        return VOLUNTARY, detail
    except Exception:
        return UNKNOWN, {}


def refine_with_dart(ticker: str, last_date: str, news_monitor, base_reason: str) -> str:
    """DART 상폐 전후 공시로 사유 보정 (news_monitor 있을 때만)."""
    if news_monitor is None:
        return base_reason
    try:
        # 상폐일 전후 60일 공시 일괄
        from datetime import datetime, timedelta
        end = last_date
        start = (datetime.strptime(last_date, '%Y-%m-%d') - timedelta(days=90)).strftime('%Y-%m-%d')
        dl = news_monitor.get_all_disclosures(ticker, start, end)
        if not dl:
            return base_reason
        names = ' '.join(d.get('nm', '') for d in dl)
        if any(k in names for k in _DART_MNA):
            return ACQUIRED
        if any(k in names for k in _DART_NEG):
            return BANKRUPT
    except Exception:
        pass
    return base_reason


def classify(ticker: str, df, news_monitor=None) -> dict:
    """최종 상폐 사유 분류. 반환: {reason, last_date, ...detail}"""
    base, detail = infer_from_price(df)
    last_date = None
    try:
        last_date = df.index.max().strftime('%Y-%m-%d')
    except Exception:
        pass
    reason = base
    if last_date:
        reason = refine_with_dart(ticker, last_date, news_monitor, base)
    detail.update({'reason': reason, 'last_date': last_date})
    return detail
