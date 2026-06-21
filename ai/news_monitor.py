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
import threading
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
        self._corp_cache_lock = threading.Lock()  # [BUG-M3] 멀티스레드 캐시 보호
        self._corp_map_loaded = False             # corpCode.xml 전체 매핑 로드 여부

    # ══════════════════════════════════════════════════════════════════
    # DART 공시 조회
    # ══════════════════════════════════════════════════════════════════

    def _load_corp_map(self) -> None:
        """DART corpCode.xml 전체 매핑(종목코드→고유번호)을 1회 다운로드해 캐싱.

        company.json 은 stock_code 로 역조회가 불가(필수값 corp_code 누락).
        전체 상장사 매핑 파일을 받아 종목코드→corp_code 딕셔너리를 구성한다.
        """
        import zipfile, io
        import xml.etree.ElementTree as ET
        try:
            res = requests.get(
                "https://opendart.fss.or.kr/api/corpCode.xml",
                params={"crtfc_key": self.dart_key},
                timeout=30,
            )
            if res.status_code != 200 or len(res.content) < 1000:
                logger.warning(f"[NewsMonitor] corpCode.xml 다운로드 실패 (status={res.status_code})")
                return
            z = zipfile.ZipFile(io.BytesIO(res.content))
            xml = z.read(z.namelist()[0])
            root = ET.fromstring(xml)
            mapping = {}
            for el in root.iter('list'):
                sc = (el.findtext('stock_code') or '').strip()
                cc = (el.findtext('corp_code') or '').strip()
                if sc and cc:
                    mapping[sc] = cc
            with self._corp_cache_lock:
                self._corp_cache.update(mapping)
                self._corp_map_loaded = True
            logger.info(f"[NewsMonitor] DART 종목 매핑 로드 완료: {len(mapping)}개")
        except Exception as e:
            logger.warning(f"[NewsMonitor] corpCode.xml 로드 오류: {e}")

    def get_corp_code(self, ticker: str) -> str | None:
        """종목코드(6자리) → DART 고유번호(8자리) 변환. 전체 매핑 캐싱."""
        ticker = (ticker or '').strip()
        with self._corp_cache_lock:
            if ticker in self._corp_cache:
                return self._corp_cache[ticker]
            loaded = self._corp_map_loaded
        if not loaded:
            self._load_corp_map()
            with self._corp_cache_lock:
                return self._corp_cache.get(ticker)
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
        """AI 컨텍스트용 공시 요약 문자열 생성 (현재 기준)."""
        disclosures = self.get_recent_disclosures(ticker, days=days)
        if not disclosures:
            return ""
        classified = self.classify_disclosures(disclosures)

        lines = []
        for d in classified['negative']:
            lines.append(f"⚠️ [악재공시] {d.get('rcept_dt','')} {d.get('report_nm','')}")
        for d in classified['positive']:
            lines.append(f"✅ [호재공시] {d.get('rcept_dt','')} {d.get('report_nm','')}")
        for d in classified['neutral'][:3]:
            lines.append(f"📋 [공시] {d.get('rcept_dt','')} {d.get('report_nm','')}")
        return "\n".join(lines) if lines else ""

    def get_disclosure_summary_for_date(self, ticker: str, date_str: str, days: int = 5) -> str:
        """백테스트용 — 특정 날짜 기준 ±days일 DART 공시 요약."""
        corp_code = self.get_corp_code(ticker)
        if not corp_code:
            return ""
        try:
            target = datetime.strptime(date_str, '%Y-%m-%d')
            bgn = (target - timedelta(days=days)).strftime('%Y%m%d')
            end = (target + timedelta(days=days)).strftime('%Y%m%d')
            res = requests.get(
                "https://opendart.fss.or.kr/api/list.json",
                params={
                    "crtfc_key": self.dart_key,
                    "corp_code": corp_code,
                    "bgn_de": bgn,
                    "end_de": end,
                },
                timeout=5,
            )
            data = res.json()
            if data.get('status') != '000':
                return ""
            disclosures = data.get('list', [])
            if not disclosures:
                return ""
            classified = self.classify_disclosures(disclosures)
            lines = []
            for d in classified['negative']:
                lines.append(f"⚠️ [악재공시] {d.get('rcept_dt','')} {d.get('report_nm','')}")
            for d in classified['positive']:
                lines.append(f"✅ [호재공시] {d.get('rcept_dt','')} {d.get('report_nm','')}")
            for d in classified['neutral'][:2]:
                lines.append(f"📋 [공시] {d.get('rcept_dt','')} {d.get('report_nm','')}")
            return "\n".join(lines) if lines else ""
        except Exception as e:
            logger.warning(f"[NewsMonitor] 날짜기준 공시 조회 실패 ({ticker} {date_str}): {e}")
            return ""

    # ══════════════════════════════════════════════════════════════════
    # 백테스트용 일괄 공시 조회 (종목당 1회 — 신호별 호출 제거)
    # ══════════════════════════════════════════════════════════════════

    def get_all_disclosures(self, ticker: str, start_date: str, end_date: str) -> list[dict]:
        """[KR/DART] 전체 기간 공시를 연도별로 일괄 수집.

        반환: [{'dt': 'YYYYMMDD', 'nm': 보고서명, 'label': '악재'|'호재'|'공시'}, ...]
        신호별 API 호출(수백 회) 대신 종목당 ~연수 회로 축소.
        """
        corp_code = self.get_corp_code(ticker)
        if not corp_code:
            return []
        from datetime import datetime as _dt
        try:
            y0 = int(start_date[:4]); y1 = int(end_date[:4])
        except Exception:
            return []
        out = []
        for year in range(y0, y1 + 1):
            bgn = f"{year}0101"
            end = f"{year}1231"
            page = 1
            while True:
                try:
                    res = requests.get(
                        "https://opendart.fss.or.kr/api/list.json",
                        params={
                            "crtfc_key": self.dart_key, "corp_code": corp_code,
                            "bgn_de": bgn, "end_de": end,
                            "page_no": page, "page_count": 100,
                        },
                        timeout=10,
                    )
                    data = res.json()
                except Exception as e:
                    logger.debug(f"[NewsMonitor] DART 일괄조회 실패 ({ticker} {year} p{page}): {e}")
                    break
                if data.get('status') != '000':
                    break
                for d in data.get('list', []):
                    nm = d.get('report_nm', '')
                    if any(k in nm for k in _NEGATIVE_KEYWORDS):
                        label = '악재'
                    elif any(k in nm for k in _POSITIVE_KEYWORDS):
                        label = '호재'
                    else:
                        label = '공시'
                    out.append({'dt': d.get('rcept_dt', ''), 'nm': nm, 'label': label})
                total_page = int(data.get('total_page', 1) or 1)
                if page >= total_page:
                    break
                page += 1
        return out

    def get_cik(self, ticker: str) -> str | None:
        """[US/EDGAR] 티커 → CIK(10자리) 변환. 전체 매핑 1회 다운로드 캐싱."""
        ticker = (ticker or '').strip().upper()
        cache = getattr(self, '_cik_cache', None)
        if cache is None:
            cache = {}
            try:
                res = requests.get(
                    "https://www.sec.gov/files/company_tickers.json",
                    headers={"User-Agent": "lassi-bot backtest contact@example.com"},
                    timeout=30,
                )
                data = res.json()
                for v in data.values():
                    t = str(v.get('ticker', '')).upper()
                    cik = str(v.get('cik_str', '')).zfill(10)
                    if t:
                        cache[t] = cik
                logger.info(f"[NewsMonitor] EDGAR CIK 매핑 로드: {len(cache)}개")
            except Exception as e:
                logger.warning(f"[NewsMonitor] EDGAR CIK 매핑 로드 실패: {e}")
            self._cik_cache = cache
        return cache.get(ticker)

    # EDGAR 공시 폼 분류
    _EDGAR_NEG = ('NT ', 'SC 13D', 'BANKRUPT', '15-', '25-')
    _EDGAR_POS = ('8-K', 'SC 13G')

    def get_all_edgar_disclosures(self, ticker: str, start_date: str, end_date: str) -> list[dict]:
        """[US/EDGAR] 전체 기간 SEC 공시(submissions)를 일괄 수집.

        반환: [{'dt': 'YYYYMMDD', 'nm': form, 'label': ...}, ...]
        """
        cik = self.get_cik(ticker)
        if not cik:
            return []
        out = []
        try:
            res = requests.get(
                f"https://data.sec.gov/submissions/CIK{cik}.json",
                headers={"User-Agent": "lassi-bot backtest contact@example.com"},
                timeout=30,
            )
            data = res.json()
        except Exception as e:
            logger.debug(f"[NewsMonitor] EDGAR 조회 실패 ({ticker}): {e}")
            return []

        # 의미있는 공시 폼만 — Form 3/4/5(내부자 거래) 등 노이즈 제외
        KEEP = ('10-K', '10-Q', '8-K', 'SC 13D', 'SC 13G', 'S-1', 'DEF 14A', '20-F', '6-K', 'NT ')

        def _consume(forms, dates):
            for form, date in zip(forms, dates):
                d = (date or '').replace('-', '')
                if not d or d < start_date.replace('-', '') or d > end_date.replace('-', ''):
                    continue
                f = form or ''
                if not any(f.startswith(k) or k in f for k in KEEP):
                    continue
                if any(k in f for k in self._EDGAR_NEG):
                    label = '악재'
                elif any(k in f for k in self._EDGAR_POS):
                    label = '호재'
                else:
                    label = '공시'
                out.append({'dt': d, 'nm': f, 'label': label})

        try:
            recent = data.get('filings', {}).get('recent', {})
            _consume(recent.get('form', []), recent.get('filingDate', []))
            # 오래된 분량은 별도 파일 참조
            for extra in data.get('filings', {}).get('files', []):
                try:
                    r2 = requests.get(
                        f"https://data.sec.gov/submissions/{extra.get('name')}",
                        headers={"User-Agent": "lassi-bot backtest contact@example.com"},
                        timeout=30,
                    )
                    d2 = r2.json()
                    _consume(d2.get('form', []), d2.get('filingDate', []))
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"[NewsMonitor] EDGAR 파싱 실패 ({ticker}): {e}")
        return out

    @staticmethod
    def format_disclosures_around(disclosures: list[dict], date_str: str, days: int = 5) -> str:
        """일괄 수집한 공시 리스트에서 특정 날짜 ±days일 구간만 메모리에서 추출·포맷."""
        if not disclosures:
            return ""
        from datetime import datetime as _dt, timedelta as _td
        try:
            target = _dt.strptime(date_str, '%Y-%m-%d')
        except Exception:
            return ""
        bgn = (target - _td(days=days)).strftime('%Y%m%d')
        end = (target + _td(days=days)).strftime('%Y%m%d')
        neg, pos, neu = [], [], []
        for d in disclosures:
            dt = d.get('dt', '')
            if not (bgn <= dt <= end):
                continue
            if d['label'] == '악재':
                neg.append(d)
            elif d['label'] == '호재':
                pos.append(d)
            else:
                neu.append(d)
        lines = []
        for d in neg:
            lines.append(f"⚠️ [악재공시] {d['dt']} {d['nm']}")
        for d in pos:
            lines.append(f"✅ [호재공시] {d['dt']} {d['nm']}")
        for d in neu[:2]:
            lines.append(f"📋 [공시] {d['dt']} {d['nm']}")
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

            reports = sorted(reports, key=lambda d: d.get('rcept_dt', ''), reverse=True)  # [BUG-C4] 최신 보고서 기준
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
