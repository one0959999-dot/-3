"""
news_monitor.py — DART 공시 + Naver 뉴스 연동 모듈
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
기능:
  1. 보유 종목 악재 공시 감지 (DART) → 손절 검토 트리거
  2. 실적 발표 예정일 감지 (DART) → 포지션 축소 트리거
  3. 매수 전 최근 뉴스 요약 (Naver) → AI 심사 컨텍스트 제공
"""

from __future__ import annotations

import re
import logging
import requests
from datetime import datetime, timedelta, timezone
from functools import lru_cache

logger = logging.getLogger('lassi_bot')

_KST = timezone(timedelta(hours=9))


def _now_kst() -> datetime:
    return datetime.now(_KST).replace(tzinfo=None)


# ── 악재 공시 키워드 ───────────────────────────────────────────────
_NEGATIVE_KEYWORDS = [
    '횡령', '배임', '소송', '부도', '파산', '회생', '조사', '압수수색',
    '손실', '적자', '감자', '상장폐지', '불성실공시', '매출감소', '영업손실',
    '과징금', '제재', '고발', '수사', '조기상환', '기한이익상실', '계약해지',
    '리콜', '영업정지', '면허취소', '대규모손해',
]

# ── 호재 공시 키워드 (AI 심사에 참고 정보로 전달) ──────────────────
_POSITIVE_KEYWORDS = [
    '수주', '계약', '특허', '인허가', '신제품', '흑자전환', '영업이익증가',
    '배당', '자사주취득', '합병', '인수', '상장',
]


class NewsMonitor:
    """DART 공시 + Naver 뉴스를 조회해 매매 판단에 활용하는 모니터."""

    def __init__(self, dart_api_key: str, naver_client_id: str, naver_client_secret: str):
        self.dart_key      = dart_api_key.strip()
        self.naver_id      = naver_client_id.strip()
        self.naver_secret  = naver_client_secret.strip()
        self._corp_cache: dict[str, str] = {}   # ticker → corp_code

    # ══════════════════════════════════════════════════════════════════
    # DART 공시 조회
    # ══════════════════════════════════════════════════════════════════

    def get_corp_code(self, ticker: str) -> str | None:
        """종목코드(6자리) → DART 고유번호(8자리) 변환. 결과 캐싱."""
        if ticker in self._corp_cache:
            return self._corp_cache[ticker]
        try:
            res = requests.get(
                "https://opendart.fss.or.kr/api/company.json",
                params={"crtfc_key": self.dart_key, "stock_code": ticker},
                timeout=5,
            )
            data = res.json()
            if data.get('status') == '000':
                corp_code = data['corp_code']
                self._corp_cache[ticker] = corp_code
                return corp_code
        except Exception as e:
            logger.warning(f"[NewsMonitor] DART corp_code 조회 실패 ({ticker}): {e}")
        return None

    def get_recent_disclosures(self, ticker: str, days: int = 3) -> list[dict]:
        """최근 N일간 해당 종목의 DART 공시 목록 반환."""
        corp_code = self.get_corp_code(ticker)
        if not corp_code:
            return []
        now = _now_kst()
        try:
            res = requests.get(
                "https://opendart.fss.or.kr/api/list.json",
                params={
                    "crtfc_key": self.dart_key,
                    "corp_code": corp_code,
                    "bgn_de":    (now - timedelta(days=days)).strftime('%Y%m%d'),
                    "end_de":    now.strftime('%Y%m%d'),
                },
                timeout=5,
            )
            data = res.json()
            if data.get('status') == '000':
                return data.get('list', [])
        except Exception as e:
            logger.warning(f"[NewsMonitor] DART 공시 목록 조회 실패 ({ticker}): {e}")
        return []

    def classify_disclosures(self, disclosures: list[dict]) -> dict:
        """공시 목록을 악재/호재/중립으로 분류."""
        negative, positive, neutral = [], [], []
        for d in disclosures:
            nm = d.get('report_nm', '')
            if any(k in nm for k in _NEGATIVE_KEYWORDS):
                negative.append(d)
            elif any(k in nm for k in _POSITIVE_KEYWORDS):
                positive.append(d)
            else:
                neutral.append(d)
        return {'negative': negative, 'positive': positive, 'neutral': neutral}

    def check_negative_disclosure(self, ticker: str, days: int = 2) -> list[dict]:
        """최근 N일 내 악재 공시만 반환. 없으면 빈 리스트."""
        disclosures = self.get_recent_disclosures(ticker, days=days)
        classified = self.classify_disclosures(disclosures)
        return classified['negative']

    def get_disclosure_summary(self, ticker: str, days: int = 3) -> str:
        """AI 컨텍스트용 공시 요약 문자열 생성."""
        disclosures = self.get_recent_disclosures(ticker, days=days)
        if not disclosures:
            return ""
        classified = self.classify_disclosures(disclosures)

        lines = []
        for d in classified['negative']:
            lines.append(f"⚠️ [악재공시] {d.get('rcept_dt','')} {d.get('report_nm','')}")
        for d in classified['positive']:
            lines.append(f"✅ [호재공시] {d.get('rcept_dt','')} {d.get('report_nm','')}")
        for d in classified['neutral'][:3]:   # 중립은 최근 3개만
            lines.append(f"📋 [공시] {d.get('rcept_dt','')} {d.get('report_nm','')}")
        return "\n".join(lines) if lines else ""

    # ══════════════════════════════════════════════════════════════════
    # 실적 발표 예정일 추정 (DART 분기보고서 패턴 기반)
    # ══════════════════════════════════════════════════════════════════

    def get_upcoming_earnings(self, ticker: str) -> dict | None:
        """
        최근 분기/반기/사업보고서 제출일 기준으로 다음 실적 발표를 추정.
        향후 14일 이내이면 딕셔너리 반환, 아니면 None.

        Returns:
            {'expected_date': str, 'days_until': int, 'last_report': str}
        """
        corp_code = self.get_corp_code(ticker)
        if not corp_code:
            return None
        now = _now_kst()
        try:
            res = requests.get(
                "https://opendart.fss.or.kr/api/list.json",
                params={
                    "crtfc_key": self.dart_key,
                    "corp_code": corp_code,
                    "bgn_de":    (now - timedelta(days=120)).strftime('%Y%m%d'),
                    "end_de":    now.strftime('%Y%m%d'),
                    "pblntf_ty": "A",   # 정기공시(분기·반기·사업보고서)
                },
                timeout=5,
            )
            data = res.json()
            if data.get('status') != '000':
                return None

            reports = [
                d for d in data.get('list', [])
                if any(kw in d.get('report_nm', '') for kw in ['분기보고서', '반기보고서', '사업보고서'])
            ]
            if not reports:
                return None

            last = reports[0]
            last_date = datetime.strptime(last['rcept_dt'], '%Y%m%d')
            next_expected = last_date + timedelta(days=91)   # 분기 ~3개월
            days_until = (next_expected - now).days

            if -3 <= days_until <= 14:   # 3일 전~14일 후 범위
                return {
                    'expected_date': next_expected.strftime('%Y-%m-%d'),
                    'days_until':    days_until,
                    'last_report':   last.get('report_nm', ''),
                    'last_date':     last.get('rcept_dt', ''),
                }
        except Exception as e:
            logger.warning(f"[NewsMonitor] DART 실적 예정일 조회 실패 ({ticker}): {e}")
        return None

    # ══════════════════════════════════════════════════════════════════
    # Naver 뉴스 검색
    # ══════════════════════════════════════════════════════════════════

    def get_news(self, stock_name: str, display: int = 5) -> list[str]:
        """종목명으로 최근 뉴스 헤드라인 목록 반환 (HTML 태그 제거)."""
        try:
            res = requests.get(
                "https://openapi.naver.com/v1/search/news.json",
                headers={
                    "X-Naver-Client-Id":     self.naver_id,
                    "X-Naver-Client-Secret": self.naver_secret,
                },
                params={"query": f"{stock_name} 주식", "display": display, "sort": "date"},
                timeout=5,
            )
            if res.status_code == 200:
                items = res.json().get('items', [])
                result = []
                for item in items:
                    title = re.sub(r'<[^>]+>', '', item.get('title', ''))
                    desc  = re.sub(r'<[^>]+>', '', item.get('description', ''))
                    pub   = item.get('pubDate', '')[:16]
                    result.append(f"[{pub}] {title} — {desc[:80]}")
                return result
            else:
                logger.warning(f"[NewsMonitor] Naver API 오류: {res.status_code} {res.text[:100]}")
        except Exception as e:
            logger.warning(f"[NewsMonitor] Naver 뉴스 조회 실패 ({stock_name}): {e}")
        return []

    def get_news_summary(self, stock_name: str, display: int = 5) -> str:
        """AI 컨텍스트용 뉴스 요약 문자열."""
        news = self.get_news(stock_name, display)
        if not news:
            return ""
        return f"📰 최근 뉴스 ({stock_name}):\n" + "\n".join(f"  • {n}" for n in news)

    def get_full_context(self, ticker: str, stock_name: str) -> str:
        """
        매수 전 AI 심사용 통합 컨텍스트.
        뉴스 + 공시 요약을 하나의 문자열로 합쳐 반환.
        """
        parts = []
        news_txt = self.get_news_summary(stock_name)
        if news_txt:
            parts.append(news_txt)
        disc_txt = self.get_disclosure_summary(ticker, days=5)
        if disc_txt:
            parts.append(f"📋 최근 공시:\n{disc_txt}")
        return "\n\n".join(parts)
