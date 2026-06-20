import re
import time
import logging

logger = logging.getLogger('lassi_bot')

_FREE_MODEL = 'gemini-2.0-flash'


class GeminiApi:

    def __init__(self, api_key: str = ''):
        self.api_key = api_key
        self.client = None
        if api_key:
            try:
                from google import genai
                self.client = genai.Client(api_key=api_key)
            except Exception as e:
                logger.warning(f"[Gemini] 초기화 실패: {e}")

    def generate_content(self, prompt: str, temperature: float = 0.3,
                         model: str = _FREE_MODEL) -> str:
        if not self.client:
            return ""
        from google.genai import types
        for attempt in range(3):
            try:
                resp = self.client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(temperature=temperature),
                )
                return resp.text or ""
            except Exception as e:
                msg = str(e)
                if '429' in msg or 'quota' in msg.lower():
                    wait = 60 * (attempt + 1)
                    logger.warning(f"[Gemini] 요청 한도 초과 — {wait}초 대기")
                    time.sleep(wait)
                else:
                    logger.warning(f"[Gemini] generate_content 오류: {e}")
                    time.sleep(5)
        return ""

    def ai_approve_trade(self, signal, stock_name, ticker, price, strategy,
                         indicator_val, hot_sectors=None, recent_trades=None,
                         custom_rules="", context: str = "",
                         portfolio_context: str = ""):
        if not self.client:
            return True, "Gemini API 미설정 — 자동 승인", 75

        action = "매수" if signal == 'BUY' else "매도"
        ind_str = (f"{indicator_val:.2f}" if isinstance(indicator_val, (int, float))
                   else str(indicator_val))
        context_section = f"\n[분석 데이터]\n{context}\n" if context else ""

        prompt = f"""[백테스트 매매 신호 검토 — {action}]
종목: {stock_name}({ticker}) | 신호: {action} | 가격: {price:,}
전략: {strategy} | 지표값: {ind_str}
{context_section}
매매 원칙:
{custom_rules if custom_rules else "기본 원칙 적용"}

위 데이터를 근거로 {action} 신호의 실행 여부를 판단하십시오.

답변 형식 (반드시 준수):
DECISION: CONFIRM 또는 REJECT
CONFIDENCE: 50~100 사이 정수
REASON: 핵심 근거 1~2줄"""

        try:
            res = self.generate_content(prompt, temperature=0.1)
            upper = res.upper()

            decision_line = next((ln for ln in upper.splitlines() if 'DECISION:' in ln), "")
            if decision_line:
                after = decision_line.split('DECISION:', 1)[-1].strip()
                first = after.split()[0] if after.split() else ""
                decision = first == 'CONFIRM'
            else:
                decision = 'CONFIRM' in upper and 'REJECT' not in upper

            confidence = 75
            conf_line = next((ln for ln in upper.splitlines() if 'CONFIDENCE:' in ln), "")
            if conf_line:
                m = re.search(r'CONFIDENCE:\s*(\d+)', conf_line)
                if m:
                    confidence = max(50, min(100, int(m.group(1))))

            reason = res.split('REASON:')[-1].strip() if 'REASON:' in res else res.strip()
            return decision, reason[:400], confidence

        except Exception as e:
            logger.warning(f"[Gemini] ai_approve_trade 오류: {e}")
            return True, f"오류로 자동 승인: {e}", 60
