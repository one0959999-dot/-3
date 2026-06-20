import requests
import logging

logger = logging.getLogger('lassi_bot')

_BASE = "https://api.perplexity.ai"
_MODEL = "sonar"
_MODEL_PRO = "sonar-pro"


class PerplexityClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

    def _chat(self, prompt: str, model: str = _MODEL, max_tokens: int = 500) -> str:
        try:
            res = requests.post(
                f"{_BASE}/chat/completions",
                headers=self._headers,
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                    "temperature": 0.1,
                    "search_recency_filter": "week",
                },
                timeout=15
            )
            data = res.json()
            return data.get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            logger.warning(f"[Perplexity] API 오류: {e}")
            return ""

    def search_stock_news(self, stock_name: str, ticker: str = "", days: int = 3) -> str:
        query = f"{stock_name}"
        if ticker:
            query += f" ({ticker})"
        prompt = (
            f"한국 주식 '{query}'에 대한 최근 {days}일 이내 주요 뉴스와 이슈를 검색해서 요약해줘. "
            f"호재/악재 여부를 명확히 구분하고, 주가에 영향을 줄 수 있는 내용만 3줄 이내로 정리해줘. "
            f"없으면 '특이 뉴스 없음'이라고만 답해줘."
        )
        result = self._chat(prompt, max_tokens=300)
        if result and "특이 뉴스 없음" not in result:
            return f"[Perplexity 실시간 뉴스]\n{result.strip()}"
        return ""

    def search_market_overview(self, mode: str = 'KR') -> str:
        if mode == 'KR':
            prompt = (
                "오늘 한국 주식시장(코스피/코스닥) 주요 이슈와 동향을 검색해서 요약해줘. "
                "외국인/기관 수급, 강세 섹터, 주요 이벤트를 5줄 이내로 정리해줘."
            )
        else:
            prompt = (
                "오늘 미국 주식시장(나스닥/S&P500) 주요 이슈와 동향을 검색해서 요약해줘. "
                "연준 동향, 주요 실적, 강세 섹터를 5줄 이내로 정리해줘."
            )
        return self._chat(prompt, max_tokens=400)

    def search_sector_trend(self, sectors: list[str]) -> str:
        if not sectors:
            return ""
        sector_str = ", ".join(sectors[:3])
        prompt = (
            f"한국 주식시장에서 {sector_str} 섹터의 최근 동향과 주도 이유를 검색해서 "
            f"3줄 이내로 요약해줘."
        )
        return self._chat(prompt, max_tokens=200)

    def search_dart_disclosure(self, stock_name: str, ticker: str = "") -> str:
        query = f"{stock_name} {ticker} 공시 IR"
        prompt = (
            f"'{query}' 관련 최근 1주일 이내 주요 공시나 IR 내용을 검색해서 "
            f"2줄 이내로 요약해줘. 없으면 '공시 없음'이라고만 답해줘."
        )
        result = self._chat(prompt, max_tokens=200)
        if result and "공시 없음" not in result:
            return f"[Perplexity 공시]\n{result.strip()}"
        return ""
