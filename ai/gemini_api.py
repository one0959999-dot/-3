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

    def chat(self, user_message: str, portfolio_context=None,
             stock_analysis_context: str = '') -> str:
        if not self.client:
            return '⚠️ Gemini API 키가 설정되지 않았습니다. 설정에서 Gemini API 키를 입력해주세요.'

        port_str = ''
        if portfolio_context and isinstance(portfolio_context, dict):
            cores = portfolio_context.get('cores', [])
            sats  = portfolio_context.get('satellites', [])
            if cores or sats:
                lines = ['[포트폴리오 현황]']
                for c in cores:
                    lines.append(f"  코어 {c.get('name','')}({c.get('ticker','')}): {c.get('shares',0)}주 | 현재가 {c.get('price',0):,}원")
                for s in sats:
                    lines.append(f"  위성 {s.get('name',''')}({s.get('ticker','')}): {s.get('shares',0)}주 | 현재가 {s.get('price',0):,}원")
                port_str = '\n'.join(lines)

        system = (
            '당신은 라씨 AI입니다. 주식 매매 분석 전문가로서 시장 분석, 포트폴리오 관리, '
            '기술적 분석(RSI, MACD, 볼린저밴드 등)에 능통합니다. '
            '한국어로 명확하고 간결하게 답변하세요.'
        )
        context_parts = [p for p in [stock_analysis_context, port_str] if p]
        context_str = '\n\n'.join(context_parts)
        full_prompt = f'{system}\n\n{context_str}\n\n사용자: {user_message}\n\n라씨 AI:' if context_str else f'{system}\n\n사용자: {user_message}\n\n라씨 AI:'

        if not hasattr(self, '_conversation_history'):
            self._conversation_history = []

        reply = self.generate_content(full_prompt, temperature=0.5)
        if reply:
            self._conversation_history.append({'role': 'user', 'content': user_message})
            self._conversation_history.append({'role': 'assistant', 'content': reply})
        return reply or '⚠️ 응답을 받지 못했습니다.'

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
