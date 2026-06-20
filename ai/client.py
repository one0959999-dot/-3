"""
통합 AI 클라이언트 팩토리
어떤 AI 제공자든 같은 인터페이스로 사용한다.

지원 provider 값:
  'claude'  — Anthropic Claude (기본 실거래)
  'gemini'  — Google Gemini (기본 백테스트, 무료)
  'openai'  — OpenAI GPT
  'grok'    — xAI Grok
"""

import logging

logger = logging.getLogger('lassi_bot')

PROVIDERS = {
    'claude': 'Anthropic Claude',
    'gemini': 'Google Gemini',
    'openai': 'OpenAI GPT',
    'grok':   'xAI Grok',
}


def get_ai_client(provider: str, api_key: str):
    """provider 이름과 API 키로 통합 클라이언트를 반환한다."""
    provider = (provider or '').lower().strip()

    if not api_key:
        logger.warning(f"[AI] {provider} API 키 없음 — NullClient 반환")
        return NullAIClient(provider)

    if provider == 'claude':
        from ai.claude_api import ClaudeApi
        return ClaudeApi(api_key=api_key)

    if provider == 'gemini':
        from ai.gemini_api import GeminiApi
        return GeminiApi(api_key=api_key)

    if provider == 'openai':
        return OpenAIClient(api_key=api_key)

    if provider == 'grok':
        return GrokClient(api_key=api_key)

    logger.warning(f"[AI] 알 수 없는 provider '{provider}' — NullClient 반환")
    return NullAIClient(provider)


def get_ai_client_from_db(user_id: int, role: str = 'trade'):
    """
    DB에서 유저 설정을 읽어 해당 role의 AI 클라이언트를 반환한다.

    role:
      'trade'    — 실거래 판단 AI
      'backtest' — 백테스트 데이터 수집 AI
    """
    from base.database import get_db_connection
    conn = get_db_connection()
    try:
        row = conn.execute(
            'SELECT trade_ai_provider, trade_ai_key, backtest_ai_provider, backtest_ai_key, gemini_api_key, claude_api_key FROM users WHERE id=?',
            (user_id,)
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return NullAIClient('unknown')

    row = dict(row)
    fallback_key = row.get('gemini_api_key') or row.get('claude_api_key') or ''
    if role == 'trade':
        return get_ai_client(row.get('trade_ai_provider') or 'gemini',
                             row.get('trade_ai_key') or fallback_key)
    else:
        return get_ai_client(row.get('backtest_ai_provider') or 'gemini',
                             row.get('backtest_ai_key') or fallback_key)


# ── OpenAI ────────────────────────────────────────────────────────────────

class OpenAIClient:
    _MODEL = 'gpt-4o-mini'

    def __init__(self, api_key: str):
        self.api_key = api_key
        try:
            from openai import OpenAI
            self.client = OpenAI(api_key=api_key)
        except Exception as e:
            logger.warning(f"[OpenAI] 초기화 실패: {e}")
            self.client = None

    def generate_content(self, prompt: str, temperature: float = 0.3,
                         model: str = '') -> str:
        if not self.client:
            return ''
        try:
            resp = self.client.chat.completions.create(
                model=model or self._MODEL,
                messages=[{'role': 'user', 'content': prompt}],
                temperature=temperature,
            )
            return resp.choices[0].message.content or ''
        except Exception as e:
            logger.warning(f"[OpenAI] generate_content 오류: {e}")
            return ''

    def chat(self, user_message: str, portfolio_context=None,
             stock_analysis_context: str = '') -> str:
        if not self.client:
            return '⚠️ OpenAI API 키가 설정되지 않았습니다.'
        if not hasattr(self, '_conversation_history'):
            self._conversation_history = []
        prompt = f'{stock_analysis_context}\n\n{user_message}' if stock_analysis_context else user_message
        reply = self.generate_content(prompt, temperature=0.5)
        if reply:
            self._conversation_history.append({'role': 'user', 'content': user_message})
            self._conversation_history.append({'role': 'assistant', 'content': reply})
        return reply or '⚠️ 응답을 받지 못했습니다.'

    def ai_approve_trade(self, signal, stock_name, ticker, price, strategy,
                         indicator_val, hot_sectors=None, recent_trades=None,
                         custom_rules='', context='', portfolio_context=''):
        return _generic_approve(self, signal, stock_name, ticker, price,
                                strategy, indicator_val, custom_rules,
                                context, portfolio_context)


# ── Grok (xAI) ────────────────────────────────────────────────────────────

class GrokClient:
    _BASE_URL = 'https://api.x.ai/v1'
    _MODEL    = 'grok-2-latest'

    def __init__(self, api_key: str):
        self.api_key = api_key
        try:
            from openai import OpenAI
            self.client = OpenAI(api_key=api_key, base_url=self._BASE_URL)
        except Exception as e:
            logger.warning(f"[Grok] 초기화 실패: {e}")
            self.client = None

    def generate_content(self, prompt: str, temperature: float = 0.3,
                         model: str = '') -> str:
        if not self.client:
            return ''
        try:
            resp = self.client.chat.completions.create(
                model=model or self._MODEL,
                messages=[{'role': 'user', 'content': prompt}],
                temperature=temperature,
            )
            return resp.choices[0].message.content or ''
        except Exception as e:
            logger.warning(f"[Grok] generate_content 오류: {e}")
            return ''

    def chat(self, user_message: str, portfolio_context=None,
             stock_analysis_context: str = '') -> str:
        if not self.client:
            return '⚠️ Grok API 키가 설정되지 않았습니다.'
        if not hasattr(self, '_conversation_history'):
            self._conversation_history = []
        prompt = f'{stock_analysis_context}\n\n{user_message}' if stock_analysis_context else user_message
        reply = self.generate_content(prompt, temperature=0.5)
        if reply:
            self._conversation_history.append({'role': 'user', 'content': user_message})
            self._conversation_history.append({'role': 'assistant', 'content': reply})
        return reply or '⚠️ 응답을 받지 못했습니다.'

    def ai_approve_trade(self, signal, stock_name, ticker, price, strategy,
                         indicator_val, hot_sectors=None, recent_trades=None,
                         custom_rules='', context='', portfolio_context=''):
        return _generic_approve(self, signal, stock_name, ticker, price,
                                strategy, indicator_val, custom_rules,
                                context, portfolio_context)


# ── Null (키 없을 때 fallback) ────────────────────────────────────────────

class NullAIClient:
    def __init__(self, provider: str = ''):
        self.provider = provider
        self._conversation_history = []

    def generate_content(self, prompt: str, **kwargs) -> str:
        return ''

    def chat(self, user_message: str, **kwargs) -> str:
        return '⚠️ AI API 키가 설정되지 않았습니다. 설정에서 Gemini API 키를 입력해주세요.'

    def ai_approve_trade(self, *args, **kwargs):
        return True, f'AI 미설정({self.provider}) — 자동 승인', 60


# ── 공통 ai_approve_trade 구현 ─────────────────────────────────────────────

def _generic_approve(client, signal, stock_name, ticker, price, strategy,
                     indicator_val, custom_rules, context, portfolio_context):
    import re
    action  = '매수' if signal == 'BUY' else '매도'
    ind_str = f'{indicator_val:.2f}' if isinstance(indicator_val, (int, float)) else str(indicator_val)
    ctx_sec = f'\n[분석 데이터]\n{context}\n' if context else ''
    port_sec = f'\n[포트폴리오]\n{portfolio_context}\n' if portfolio_context else ''

    prompt = (
        f'[매매 신호 검토 — {action}]\n'
        f'종목: {stock_name}({ticker}) | 신호: {action} | 현재가: {price:,}\n'
        f'전략: {strategy} | 지표값: {ind_str}\n'
        f'{ctx_sec}{port_sec}'
        f'매매 원칙:\n{custom_rules or "기본 원칙 적용"}\n\n'
        f'{action} 신호의 실행 여부를 판단하십시오.\n\n'
        f'답변 형식 (반드시 준수):\n'
        f'DECISION: CONFIRM 또는 REJECT\n'
        f'CONFIDENCE: 50~100 사이 정수\n'
        f'REASON: 핵심 근거 2~3줄'
    )

    try:
        res   = client.generate_content(prompt, temperature=0.1)
        upper = res.upper()

        dec_line = next((l for l in upper.splitlines() if 'DECISION:' in l), '')
        if dec_line:
            first = dec_line.split('DECISION:', 1)[-1].strip().split()
            decision = bool(first) and first[0] == 'CONFIRM'
        else:
            decision = 'CONFIRM' in upper and 'REJECT' not in upper

        confidence = 75
        conf_line = next((l for l in upper.splitlines() if 'CONFIDENCE:' in l), '')
        if conf_line:
            m = re.search(r'CONFIDENCE:\s*(\d+)', conf_line)
            if m:
                confidence = max(50, min(100, int(m.group(1))))

        reason = res.split('REASON:')[-1].strip() if 'REASON:' in res else res.strip()
        return decision, reason[:400], confidence

    except Exception as e:
        logger.warning(f'[AI approve] 오류: {e}')
        return True, f'오류로 자동 승인: {e}', 60
