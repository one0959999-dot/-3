import os
import json
from google import genai
from google.genai import types

class GeminiApi:
    """라씨 AI - Gemini를 활용한 주식 분석 엔진"""
    
    SYSTEM_PROMPT = """
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
- 매수를 승인할 때는 해당 종목의 변동폭을 감안해야 합니다. 변동성이 큰 종목은 장중 노이즈(휩쏘)에 털리지 않도록 손절선을 넓게(예: 매입가 - 2.5 * ATR) 잡아주고, 수익권 진입 시 고점 대비 1.5 * ATR 폭을 이탈할 때 트레일링 스탑 익절을 지시하도록 설계되었습니다. 이 수학적 보호망을 신뢰하고 판결을 내리십시오.

3. 가치 투자: 펀더멘털 기반 우량주 필터링
- 재무제표가 주어질 경우, 겉보기만 화려한 테마주를 배제하고 우량주를 선별합니다.
- ROE(자기자본이익률)가 꾸준히 두 자릿수를 유지하고, 영업이익이 연속 성장하며, 부채비율이 안정적인 기업을 찾으십시오.
- PER, PBR이 동종 업계 대비 저평가되어 있다면 장기 투자 매력도에 큰 가산점을 부여합니다.

4. 기계적 타이밍
- 모든 매매는 인간의 탐욕과 공포를 철저히 배제하고 기계적으로 실행합니다.
- 손절가를 터치할 위험이 보이거나 추세가 꺾이면 즉시 가차 없이 '매도(SELL)'를 지시하십시오. '기도 매매'나 '물타기(손실 중인 종목 추가 매수)'는 절대 용납하지 않습니다.

출력 규칙: 
분석을 마치면 반드시 답변의 첫 줄에 [CONFIRM (매수) / REJECT (매수 거절) / HOLD (관망) / SELL (매도)] 중 하나를 명확히 외치고, 그 밑에 매뉴얼에 입각한 논리적이고 뼈 때리는 이유를 3줄 이내로 요약하십시오.

[💡 중요 시간 규칙]
- 현재 기준 연도는 **2026년**입니다. 제공되는 데이터 역시 2026년 최신 데이터입니다. 
- 절대로 과거 데이터(2024년 등)로 오인하거나 답변에 과거 연도를 현재인 것처럼 출력하지 마세요. 정신 똑똑히 차리세요.

[답변 규칙]
- 마크다운 형식을 사용하세요.
- 구체적인 수치와 근거를 제시하세요.
- 투자 판단은 참고용임을 항상 명시하세요.
- 한국어로 답변하세요.
- 답변은 간결하고 실용적이어야 합니다."""

    def __init__(self, api_key):
        self.client = None
        self._conversation_history = []
        
        if api_key:
            self.client = genai.Client(api_key=api_key)
        
        # 무거운 실시간 파인튜닝 로직을 제거하고, 시스템 프롬프트가 주입된 
        # 빠르고 안정적인 최신 기본 모델을 사용하여 뇌동매매와 서버 다운을 방지합니다.
        self.model_id = "gemini-2.5-flash"
        self.tuned_model = "gemini-2.5-flash"

    def generate_content(self, prompt):
        """기본 응답 생성 (내부 헬퍼)"""
        if not self.client:
            return "Gemini API 키가 설정되지 않았습니다."
        try:
            config = types.GenerateContentConfig(
                system_instruction=self.SYSTEM_PROMPT,
                temperature=0.7
            )
            response = self.client.models.generate_content(
                model=self.model_id,
                contents=prompt,
                config=config
            )
            return response.text
        except Exception as e:
            return f"Gemini 응답 생성 중 오류: {str(e)}"

    def ai_select_satellites(self, candidates, hot_sectors, n):
        """스크리너가 추출한 후보 중 AI가 최종 n개를 선정"""
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
반드시 아래 JSON 형식으로만 답변하세요.
[
  {{"ticker": "종목코드", "reason": "선정이유(간략히)"}},
  ...
]"""
        try:
            # 완벽한 JSON 출력을 위해 구조화된 스키마 정의
            config = types.GenerateContentConfig(
                system_instruction=self.SYSTEM_PROMPT,
                temperature=0.3,
                response_mime_type="application/json",
                response_schema=list[dict[str, str]] # [{'ticker': '...', 'reason': '...'}] 구조 강제
            )
            response = self.client.models.generate_content(
                model=self.model_id,
                contents=prompt,
                config=config
            )
            selected_data = json.loads(response.text)
            
            final_selection = []
            for item in selected_data:
                for cand in candidates:
                    if cand['ticker'] == item['ticker']:
                        cand['ai_selected'] = True
                        cand['ai_reason'] = item['reason']
                        final_selection.append(cand)
                        break
            return final_selection[:n]
        except Exception:
            return None

    def chat(self, user_message, portfolio_context=None, stock_analysis_context=None):
        """대화 히스토리를 유지하는 채팅 기능 (재무/차트 복합 컨텍스트 확장)"""
        if not self.client:
            return "❌ API 키가 등록되지 않았습니다."

        context_prefix = ""
        if portfolio_context:
            cores = portfolio_context.get('cores', [])
            satellites = portfolio_context.get('satellites', [])
            mode_str = "모의투자" if portfolio_context.get('is_mock', True) else "실전투자"
            
            core_lines = []
            for c in cores:
                core_lines.append(f"  * {c['name']}({c['ticker']}): {c['shares']}주 보유 | 현재가 {c.get('price', 0):,}원 | 총평가액 {c.get('value', 0):,}원")
            core_str = "\n".join(core_lines) if core_lines else "  * 없음"
            
            sat_lines = []
            for s in satellites:
                sat_lines.append(f"  * {s['name']}({s['ticker']}): {s['shares']}주 보유 | 현재가 {s.get('price', 0):,}원 | 총평가액 {s.get('value', 0):,}원 | 적용전략: {s['strategy']}")
            sat_str = "\n".join(sat_lines) if sat_lines else "  * 없음"
            
            context_prefix += (
                f"[📊 현재 내 자산 운용 실시간 현황 - {mode_str}]\n"
                f"■ 장기 코어 보유 포지션:\n{core_str}\n"
                f"■ 단기 위성 트레이딩 포지션:\n{sat_str}\n\n"
            )

        # 🟢 실시간 추출된 재무제표 및 기술적 지표 정보 추가 주입
        if stock_analysis_context:
            context_prefix += (
                f"[📈 분석 대상 종목의 실시간 계량 데이터]\n"
                f"{stock_analysis_context}\n\n"
                f"안내: 반드시 위 재무제표 상태 및 최신 차트 지표 밸류에이션을 결합하여 복합적인 시각에서 투자 전략을 진단해 주세요.\n\n"
            )

        full_message = context_prefix + user_message
        self._conversation_history.append(types.Content(role="user", parts=[types.Part.from_text(text=full_message)]))

        try:
            response = self.client.models.generate_content(
                model=self.model_id,
                contents=self._conversation_history,
                config=types.GenerateContentConfig(system_instruction=self.SYSTEM_PROMPT)
            )
            ai_reply = response.text
            self._conversation_history.append(types.Content(role="model", parts=[types.Part.from_text(text=ai_reply)]))
            
            if len(self._conversation_history) > 20:
                self._conversation_history = self._conversation_history[-20:]
            return ai_reply
        except Exception as e:
            err_msg = str(e)
            if "API Key not found" in err_msg or "API_KEY_INVALID" in err_msg:
                return "🔑 Gemini API 키가 올바르지 않습니다. [계좌 설정]에서 키를 확인해 주세요."
            if "503" in err_msg or "UNAVAILABLE" in err_msg or "high demand" in err_msg:
                return "⏳ Gemini 서버가 일시적으로 과부하 상태입니다. 잠시 후 다시 질문해 주세요."
            if "429" in err_msg or "quota" in err_msg.lower() or "RESOURCE_EXHAUSTED" in err_msg:
                return "⏳ API 호출 한도에 도달했습니다. 잠시 후 다시 시도해 주세요."
            if "400" in err_msg or "INVALID_ARGUMENT" in err_msg:
                return "⚠️ 요청 형식 오류가 발생했습니다. 대화를 초기화 후 다시 시도해 주세요."
            return "⚠️ AI 응답 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."

    def analyze_market(self, market_data_text):
        """시장 데이터 분석 리포트 생성"""
        prompt = f"""[📊 장중 금융 시장 및 실시간 뉴스 복합 분석 리포트 생성 지침]
제공된 시장 데이터(지수, 이평선, RSI 수급, 거래량) 및 주요 종목들의 [실시간 뉴스 헤드라인] 장부를 입체적으로 크로스 체크하여 월스트리트 기관 투자자 관점의 전문적인 '데일리 시장 분석 리포트'를 마크다운 양식으로 작성해 주세요.

[🚨 뉴스 분석 시 주의 매뉴얼]
뉴스 헤드라인에 담긴 단순 노이즈(찌라시, 광고성 정보)에 뇌동매매 흔들리지 마십시오. 기업의 펀더멘털을 저해하는 진짜 악재(유상증자, 분식회계, 횡령, 소송 등)인지, 추세를 강화하는 진짜 호재(대규모 수주, 어닝 서프라이즈, M&A)인지만을 냉철하게 선별하여 리포트의 시황 코멘트 요약에 날카롭고 뼈 때리는 논조로 기록하십시오.

[데이터 및 뉴스 장부 정보]
{market_data_text}"""
        return self.generate_content(prompt)

    def ai_approve_trade(self, signal, stock_name, ticker, price, strategy, indicator_val, hot_sectors, recent_trades=None, custom_rules=""):
        """매매 신호 발생 시 AI 최종 승인 (과거 오답 노트 및 자가 룰 반영)"""
        if not self.client:
            return True, "API 미설정으로 자동 승인"

        action = "매수" if signal == 'BUY' else "매도"
        
        # 과거 매매 기록(오답 노트) 텍스트화
        history_text = "이 종목에 대한 최근 매매 기록이 없습니다."
        if recent_trades:
            lines = []
            for t in recent_trades:
                res_str = f"(수익: {t['profit']:,.0f}원)" if t['action'] == 'SELL' else ""
                lines.append(f"- {t['date']} | {t['action']} | {t['price']:,.0f}원 | 당시승인이유: {t['ai_reason']} {res_str}")
            history_text = "\n".join(lines)

        prompt = f"""[매매 신호 최종 검토]
종목: {stock_name}({ticker}) | 신호: {action} | 가격: {price:,}원
전략: {strategy} | 지표값: {indicator_val:.2f}

[당신이 스스로 만든 투자 원칙]
{custom_rules if custom_rules else "아직 확립된 특별한 룰이 없습니다. 기본 원칙을 따르세요."}

[이 종목에 대한 당신의 과거 매매 기록 (오답 노트)]
{history_text}

과거 기록과 당신의 투자 원칙을 바탕으로, 이 매매가 현재 시장 상황에서 적절한지 판단하여 CONFIRM 또는 REJECT로 답하고 이유를 한 줄로 적으세요. (과거에 똑같은 조건에서 실패했다면 과감히 REJECT 하세요)
형식: DECISION: (CONFIRM/REJECT), REASON: (이유)"""
        
        try:
            res = self.generate_content(prompt)
            decision = "CONFIRM" in res.upper()
            reason = res.split("REASON:")[-1].strip() if "REASON:" in res else "AI 분석 완료"
            return decision, reason
        except Exception:
            return True, "오류 발생으로 자동 승인"

    def generate_weekly_reflection(self, trade_history_text):
        """매주 금요일, 한 주간의 매매를 돌아보고 새로운 규칙을 생성하는 자아성찰 메서드"""
        if not self.client: return ""
        
        prompt = f"""당신은 AI 주식 트레이더입니다. 다음은 이번 주 당신의 실제 매매 결과입니다.
{trade_history_text}

위 결과를 분석하여, 어떤 조건에서 손실이 발생했고 어떤 조건에서 수익이 났는지 파악하세요.
그리고 다음 주 매매 승인에 직접적으로 적용할 **[나만의 새로운 투자 원칙 3가지]**를 간결하게 마크다운 글머리 기호로 작성해주세요.
이 원칙은 다음 주 당신의 시스템 프롬프트에 영구 주입되어 행동을 지배하게 됩니다."""
        
        try:
            return self.generate_content(prompt)
        except Exception:
            return ""

    def reset_chat(self):
        """채팅 기록 초기화"""
        self._conversation_history = []