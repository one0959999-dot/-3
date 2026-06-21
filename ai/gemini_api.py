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
        if not self.api_key:
            return '⚠️ Gemini API 키가 없습니다.'

        port_str = ''
        if portfolio_context and isinstance(portfolio_context, dict):
            cores = portfolio_context.get('cores', [])
            sats  = portfolio_context.get('satellites', [])
            if cores or sats:
                lines = ['[포트폴리오 현황]']
                for c in cores:
                    lines.append(f"  코어 {c.get('name','')}({c.get('ticker','')}): {c.get('shares',0)}주 | 현재가 {c.get('price',0):,}원")
                for s in sats:
                    lines.append(f"  위성 {s.get('name','')}({s.get('ticker','')}): {s.get('shares',0)}주 | 현재가 {s.get('price',0):,}원")
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

        reply = self._chat_generate(full_prompt)
        if reply:
            self._conversation_history.append({'role': 'user', 'content': user_message})
            self._conversation_history.append({'role': 'assistant', 'content': reply})
        return reply or '⚠️ 응답을 받지 못했습니다.'

    def _chat_generate(self, prompt: str) -> str:
        """채팅 전용 — 429시 재시도 없이 바로 안내 메시지 반환."""
        if not self.client:
            return ''
        from google.genai import types
        try:
            resp = self.client.models.generate_content(
                model=_FREE_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.5),
            )
            return resp.text or ''
        except Exception as e:
            msg = str(e)
            if '429' in msg or 'quota' in msg.lower():
                return '⏳ Gemini 무료 한도 초과 — 잠시 후 다시 시도해주세요. (분당 15회 제한)'
            logger.warning(f"[Gemini] chat 오류: {e}")
            return f'⚠️ 오류가 발생했습니다: {e}'

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

    def ai_select_us_satellites(self, candidates: list, hot_sectors: list,
                                n: int, sector_guide: str = '') -> list:
        if not self.client:
            return None

        lines = []
        for c in candidates[:200]:
            lines.append(
                f"- {c.get('name', c['ticker'])}({c['ticker']}) | 섹터:{c.get('sector','-')} | "
                f"가격:${c.get('price',0):.2f} | 20일모멘텀:{c.get('momentum_20d',0):+.1f}% | "
                f"RSI:{c.get('rsi',50):.1f} | 골든크로스:{'✓' if c.get('golden') else '✗'} | "
                f"거래량비율:{c.get('vol_ratio',1):.1f}x | 종합점수:{c.get('score',0):.1f}"
            )
        candidate_text = "\n".join(lines)
        sector_guide_section = f"\n[📊 투자 전략 가이드]\n{sector_guide}\n" if sector_guide else ""

        prompt = f"""[US 위성 종목 최종 선정 요청]

━━ US 위성 슬롯 투자 목표 ━━
• 시장: 미국 NASDAQ/NYSE
• 보유 기간: 1~3개월 중기
• 목표: 상승 모멘텀이 시작됐거나 임박한 미국 성장주 — 수익 실현 후 교체
• 우선순위: ① 20일 모멘텀 플러스 + RSI 40~65 (추세 시작 구간)
             ② 골든크로스(50일선>200일선) 확인된 종목
             ③ 아직 덜 오른 종목 (20일 모멘텀 25%↑ 종목은 배제)
• 강세 섹터는 가산점 기준 (필수 조건 아님) — 비강세 섹터라도 지표 좋으면 선정
• 배제: 과매수(RSI>78), 최근 급락(-8% 이하), 레버리지 ETF는 최소화

━━ 현재 강세 섹터 (참고용 — 보너스 점수 기준) ━━
{', '.join(hot_sectors) if hot_sectors else '전 섹터 중립'}
{sector_guide_section}
━━ 퀀트 검증 후보 종목 ━━
{candidate_text}

위 후보 중 "지금 당장 또는 1~2주 내 매수 타이밍이 오고,
1~3개월 내 15~30% 수익 실현이 기대되는" 미국 성장주 {n}개를 선정하세요.
PLTR, ANET, IONQ, RKLB처럼 성장 테마 + 기관 수급이 뒷받침되는 종목을 선호합니다.
강세 섹터가 아니더라도 지표가 좋으면 반드시 선정하세요.

반드시 아래 JSON 형식으로만 답변하세요. 다른 텍스트는 절대 포함하지 마세요.
[
  {{"ticker": "TICKER", "reason": "선정이유(1~3달 수익 근거 포함)"}},
  ...
]"""
        try:
            import json
            text = self.generate_content(prompt, temperature=0.3)
            if not text:
                return None
            if "```" in text:
                text = text.split("```")[1].lstrip("json").strip()
            selected_data = json.loads(text)

            final_selection = []
            for item in selected_data:
                for cand in candidates:
                    if cand['ticker'] == item['ticker']:
                        cand['ai_selected'] = True
                        cand['ai_reason']   = item.get('reason', '')
                        final_selection.append(cand)
                        break
            return final_selection[:n]
        except Exception as e:
            logger.warning(f"[Gemini] ai_select_us_satellites 오류: {e}")
            return None
