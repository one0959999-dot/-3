"""
claude_api.py
라씨봇 AI 엔진 — Claude(Anthropic) 기반.
global_market_training.jsonl (1537개 실전 매매 정답 데이터)에서 추출한
하락장/횡보장 패턴 규칙이 시스템 프롬프트에 내장되어 있음.
"""

import os
import json
from datetime import datetime

try:
    import anthropic
except ImportError:
    anthropic = None


class ClaudeApi:
    """라씨 AI - Claude(Anthropic)를 활용한 주식 분석 엔진"""

    _SYSTEM_PROMPT_TEMPLATE = """
당신은 월스트리트 상위 1% 수익률을 자랑하는 전설적인 퀀트 트레이더이자, 인간의 감정이 완벽히 배제된 AI 매매 엔진입니다.
당신에게 주어지는 모든 시장 데이터(차트, 재무제표, 거시경제 지표, 수급, ATR 변동성)를 분석할 때, 다음의 [절대 투자 매뉴얼]을 엄격하게 적용하여 매매를 판단하십시오.

[절대 투자 매뉴얼]

1. 최우선 원칙: 유연한 시장 평가 및 '군계일학(주도주)' 단기 트레이딩 허용
- 코스피와 코스닥 지수는 철저히 독립적으로 평가하되, 지수가 20일 이동평균선 아래에 있다고 해서 무조건 겁먹고 일반 주식 매수를 거절(REJECT)하지 마십시오.
- 당신은 시장이 폭락할 때도 오르는 종목을 찾아 수익을 내는 상위 1% 퀀트 트레이더입니다. 해당 시장이 하락장이더라도, 프롬프트에 제공된 '현재 강세 섹터(Hot Sectors)'에 속해 있거나, 개별 종목의 차트 모멘텀(RSI, 거래량 등)이 살아 있다면 단기 위성 트레이딩 목적의 매수를 과감히 승인(CONFIRM)하십시오.
- 알고리즘 본체가 이미 하락장에 대비해 현금 비중과 헷징을 조절하고 있으므로, 당신은 승인 요청이 들어온 개별 종목의 '타점'이 좋은지에만 집중하면 됩니다.
- 물론, 시장 폭락에 배팅하는 'KODEX 인버스' 계열이나 방어 자산(달러, 금 등) 매수 신호가 오면 이는 적극적으로 승인(CONFIRM)하십시오.

2. 리스크 관리: ATR(Average True Range) 기반 변동성 적응형 리스크 관리
- 과거의 일률적인 고정 퍼센트(-5%) 손절은 구시대적 발상입니다. 이제 종목 고유의 최근 14일 ATR(평균 실질 변동폭) 데이터를 적극 활용합니다.
- 매수를 승인할 때는 해당 종목의 변동폭을 감안해야 합니다. 변동성이 큰 종목은 장중 노이즈(휩쏘)에 털리지 않도록 손절선을 넓게(예: 매입가 - 2.5 * ATR) 잡아주고, 수익권 진입 시 고점 대비 1.5 * ATR 폭을 이탈할 때 트레일링 스탑 익절을 지시하도록 설계되었습니다.

3. 가치 투자: 펀더멘털 기반 우량주 필터링
- 재무제표가 주어질 경우, 겉보기만 화려한 테마주를 배제하고 우량주를 선별합니다.
- ROE(자기자본이익률)가 꾸준히 두 자릿수를 유지하고, 영업이익이 연속 성장하며, 부채비율이 안정적인 기업을 찾으십시오.
- PER, PBR이 동종 업계 대비 저평가되어 있다면 장기 투자 매력도에 큰 가산점을 부여합니다.

4. 기계적 타이밍
- 모든 매매는 인간의 탐욕과 공포를 철저히 배제하고 기계적으로 실행합니다.
- 손절가를 터치할 위험이 보이거나 추세가 꺾이면 즉시 가차 없이 '매도(SELL)'를 지시하십시오.

[🧠 실전 매매 학습 데이터 기반 패턴 규칙 — 1537개 역사적 매매 정답지 분석 결과]

이 규칙은 KOSPI·KOSDAQ·NASDAQ의 2000~2024년 실전 차트 데이터에서 학습된 패턴입니다.
반드시 아래 규칙을 우선 적용한 뒤 판단하십시오.

【하락장 패턴 규칙 (120일선 붕괴 역배열 장세)】
- RSI 35~40 → 86% 확률로 REJECT. "가짜 반등의 전형 — 추가 폭락 위험 높음"
- RSI 25~35 → 67% 확률로 REJECT. "떨어지는 칼날(falling knife). 단순 RSI 과매도만으로는 매수 불가"
- RSI 20~25 → 69% 확률로 REJECT. "여전히 하락 추세 중. 극단적 과매도라도 하락장 진행 시 반등 실패 다수"
- RSI < 20  → CONFIRM 가능 조건: RSI < 20 + 볼린저 하단 터치 + 거래량 급증 3조건 동시 충족 시에만
- 하락장에서 CONFIRM할 때는 반드시 "소액(30% 규모)" 분할 진입 원칙을 명시하십시오

【횡보장/불확실 패턴 규칙】
- RSI 25~35 + 거래량 부족 → REJECT. "변동성만 크고 추세 없음 — 관망"
- RSI < 20 + 명확한 지지선 확인 → CONFIRM 가능

【상승장 패턴 규칙 (120일선 위 정배열)】
- RSI 30 돌파 상향 크로스: CONFIRM — "상승 추세 재개 신호"
- RSI 70 하향 이탈: SELL — "과열 해소, 차익 실현 타이밍"
- 강세 섹터 + 모멘텀 유효: 지수가 약해도 개별 종목 CONFIRM 가능

【학습 데이터 핵심 교훈】
과거 하락장(2000년 닷컴버블, 2008년 금융위기, 2020년 코로나 패닉)에서
RSI 과매도 단일 지표만 믿고 매수한 경우의 67~86%가 손실로 귀결되었습니다.
하락장에서는 "기다리는 것이 수익"이라는 패턴이 통계적으로 확인되었습니다.

출력 규칙:
분석을 마치면 반드시 답변의 첫 줄에 [CONFIRM (매수) / REJECT (매수 거절) / HOLD (관망) / SELL (매도)] 중 하나를 명확히 외치고, 그 밑에 매뉴얼에 입각한 논리적이고 뼈 때리는 이유를 3줄 이내로 요약하십시오.

[💡 중요 시간 규칙]
- 현재 기준 연도는 **{year}년**입니다. 제공되는 데이터 역시 {year}년 최신 데이터입니다.
- 절대로 과거 데이터로 오인하거나 답변에 과거 연도를 현재인 것처럼 출력하지 마세요.

[답변 규칙]
- 마크다운 형식을 사용하세요.
- 구체적인 수치와 근거를 제시하세요.
- 투자 판단은 참고용임을 항상 명시하세요.
- 한국어로 답변하세요.
- 답변은 간결하고 실용적이어야 합니다."""

    # 기본 모델: claude-sonnet-4-6 (속도/비용 균형), opus는 더 정확하지만 느리고 비쌈
    DEFAULT_MODEL = "claude-sonnet-4-6"

    @property
    def SYSTEM_PROMPT(self):
        return self._SYSTEM_PROMPT_TEMPLATE.format(year=datetime.now().year)

    def __init__(self, api_key: str, model: str = None):
        self.client = None
        self._conversation_history = []
        self._api_key = api_key

        if not anthropic:
            raise ImportError("anthropic 패키지가 설치되지 않았습니다. pip install anthropic")

        if api_key:
            self.client = anthropic.Anthropic(api_key=api_key)

        self.model_id = model or self.DEFAULT_MODEL

    def generate_content(self, prompt: str, temperature: float = 0.3) -> str:
        """기본 응답 생성 (내부 헬퍼) — GeminiApi.generate_content 호환"""
        if not self.client:
            return "Claude API 키가 설정되지 않았습니다."
        try:
            resp = self.client.messages.create(
                model=self.model_id,
                max_tokens=2048,
                temperature=temperature,
                system=self.SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text
        except Exception as e:
            return f"Claude 응답 생성 중 오류: {str(e)}"

    def ai_select_satellites(self, candidates, hot_sectors, n):
        """스크리너가 추출한 후보 중 AI가 최종 n개를 선정 — GeminiApi 호환"""
        if not self.client:
            return None

        candidate_text = "\n".join([
            f"- {c['name']}({c['ticker']}): 수익률 {c['return_pct']}%, 점수 {c['score']}, 섹터 {c['sector']}"
            for c in candidates[:15]
        ])

        prompt = f"""[위성 종목 최종 선정 요청]
현재 강세 섹터: {', '.join(hot_sectors)}
후보 종목 리스트:
{candidate_text}

위 후보 중 기술적 지표와 섹터 정렬이 가장 우수한 종목 {n}개를 선정해주세요.
반드시 아래 JSON 형식으로만 답변하세요. 다른 텍스트는 절대 포함하지 마세요.
[
  {{"ticker": "종목코드", "reason": "선정이유(간략히)"}},
  ...
]"""
        try:
            resp = self.client.messages.create(
                model=self.model_id,
                max_tokens=1024,
                temperature=0.3,
                system=self.SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text.strip()
            # JSON 블록만 추출
            if "```" in text:
                text = text.split("```")[1].lstrip("json").strip()
            selected_data = json.loads(text)

            final_selection = []
            for item in selected_data:
                for cand in candidates:
                    if cand['ticker'] == item['ticker']:
                        cand['ai_selected'] = True
                        cand['ai_reason'] = item.get('reason', '')
                        final_selection.append(cand)
                        break
            return final_selection[:n]
        except Exception:
            return None

    def chat(self, user_message: str, portfolio_context=None, stock_analysis_context=None) -> str:
        """대화 히스토리를 유지하는 채팅 기능 — GeminiApi.chat 호환"""
        if not self.client:
            return "❌ API 키가 등록되지 않았습니다."

        context_prefix = ""
        if portfolio_context:
            cores = portfolio_context.get('cores', [])
            satellites = portfolio_context.get('satellites', [])
            mode_str = "모의투자" if portfolio_context.get('is_mock', True) else "실전투자"

            core_lines = [f"  * {c['name']}({c['ticker']}): {c['shares']}주 | 현재가 {c.get('price', 0):,}원" for c in cores]
            sat_lines = [f"  * {s['name']}({s['ticker']}): {s['shares']}주 | 전략: {s['strategy']}" for s in satellites]

            context_prefix += (
                f"[📊 현재 내 자산 운용 현황 - {mode_str}]\n"
                f"■ 코어: {chr(10).join(core_lines) if core_lines else '없음'}\n"
                f"■ 위성: {chr(10).join(sat_lines) if sat_lines else '없음'}\n\n"
            )

        if stock_analysis_context:
            context_prefix += f"[📈 종목 실시간 데이터]\n{stock_analysis_context}\n\n"

        full_message = context_prefix + user_message

        self._conversation_history.append({"role": "user", "content": full_message})

        # 히스토리 최대 20개 메시지 유지
        if len(self._conversation_history) > 20:
            self._conversation_history = self._conversation_history[-20:]

        try:
            resp = self.client.messages.create(
                model=self.model_id,
                max_tokens=2048,
                system=self.SYSTEM_PROMPT,
                messages=self._conversation_history,
            )
            ai_reply = resp.content[0].text
            self._conversation_history.append({"role": "assistant", "content": ai_reply})
            return ai_reply

        except anthropic.AuthenticationError:
            return "🔑 Claude API 키가 올바르지 않습니다. [계좌 설정]에서 키를 확인해 주세요."
        except anthropic.RateLimitError:
            return "⏳ API 호출 한도에 도달했습니다. 잠시 후 다시 시도해 주세요."
        except anthropic.APIStatusError as e:
            if e.status_code == 503:
                return "⏳ Claude 서버가 일시적으로 과부하 상태입니다. 잠시 후 다시 질문해 주세요."
            return "⚠️ AI 응답 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
        except Exception:
            return "⚠️ AI 응답 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."

    def analyze_market(self, market_data_text: str) -> str:
        """시장 데이터 분석 리포트 생성 — GeminiApi.analyze_market 호환"""
        prompt = f"""[📊 장중 금융 시장 및 실시간 뉴스 복합 분석 리포트 생성 지침]
제공된 시장 데이터(지수, 이평선, RSI 수급, 거래량) 및 주요 종목들의 뉴스 헤드라인을 바탕으로
월스트리트 기관 투자자 관점의 전문적인 '데일리 시장 분석 리포트'를 마크다운 양식으로 작성해 주세요.

[데이터 및 뉴스 장부 정보]
{market_data_text}"""
        return self.generate_content(prompt, temperature=0.7)

    def ai_approve_trade(self, signal, stock_name, ticker, price, strategy,
                         indicator_val, hot_sectors, recent_trades=None, custom_rules=""):
        """매매 신호 발생 시 AI 최종 승인 — GeminiApi.ai_approve_trade 호환"""
        if not self.client:
            return True, "API 미설정으로 자동 승인"

        action = "매수" if signal == 'BUY' else "매도"

        history_text = "이 종목에 대한 최근 매매 기록이 없습니다."
        if recent_trades:
            lines = []
            for t in recent_trades:
                res_str = f"(수익: {t['profit']:,.0f}원)" if t['action'] == 'SELL' else ""
                lines.append(f"- {t['date']} | {t['action']} | {t['price']:,.0f}원 | {t['ai_reason']} {res_str}")
            history_text = "\n".join(lines)

        prompt = f"""[매매 신호 최종 검토]
종목: {stock_name}({ticker}) | 신호: {action} | 가격: {price:,}원
전략: {strategy} | 지표값: {indicator_val:.2f}

[당신이 스스로 만든 투자 원칙]
{custom_rules if custom_rules else "아직 확립된 특별한 룰이 없습니다. 기본 원칙을 따르세요."}

[이 종목에 대한 당신의 과거 매매 기록 (오답 노트)]
{history_text}

과거 기록과 당신의 투자 원칙을 바탕으로 이 매매가 현재 시장 상황에서 적절한지 판단하여
CONFIRM 또는 REJECT로 답하고 이유를 한 줄로 적으세요.
형식: DECISION: (CONFIRM/REJECT), REASON: (이유)"""

        try:
            res = self.generate_content(prompt, temperature=0.1)
            decision = "CONFIRM" in res.upper()
            reason = res.split("REASON:")[-1].strip() if "REASON:" in res else "AI 분석 완료"
            return decision, reason
        except Exception:
            return False, "AI 오류 발생으로 자동 거절 (안전 모드)"

    def generate_weekly_reflection(self, trade_history_text: str) -> str:
        """매주 금요일, 한 주간의 매매를 돌아보고 새로운 규칙을 생성 — GeminiApi 호환"""
        if not self.client:
            return ""

        prompt = f"""당신은 AI 주식 트레이더입니다. 다음은 이번 주 당신의 실제 매매 결과입니다.
{trade_history_text}

위 결과를 분석하여, 어떤 조건에서 손실이 발생했고 어떤 조건에서 수익이 났는지 파악하세요.
그리고 다음 주 매매 승인에 직접적으로 적용할 **[나만의 새로운 투자 원칙 3가지]**를 간결하게 마크다운 글머리 기호로 작성해주세요."""

        try:
            return self.generate_content(prompt)
        except Exception:
            return ""

    def reset_chat(self):
        """채팅 기록 초기화 — GeminiApi 호환"""
        self._conversation_history = []
