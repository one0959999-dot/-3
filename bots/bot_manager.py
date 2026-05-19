from bots.real_bot import RealBotController
from bots.mock_bot import MockBotController
from gemini_api import GeminiApi

def _make_ai_client(user_data: dict):
    """
    claude_api_key 또는 gemini_api_key 중 입력된 키로 AI 클라이언트 생성.
    claude_api_key가 있으면 Claude를 우선 사용하고, 없으면 Gemini 사용.
    """
    claude_key = (user_data.get('claude_api_key') or '').strip()
    gemini_key = (user_data.get('gemini_api_key') or '').strip()

    if claude_key:
        try:
            from claude_api import ClaudeApi
            client = ClaudeApi(api_key=claude_key)
            client._api_key = claude_key
            return client
        except Exception:
            pass  # Claude 초기화 실패 시 Gemini로 폴백

    if gemini_key:
        client = GeminiApi(api_key=gemini_key)
        client._api_key = gemini_key
        return client

    return None


class BotManager:
    def __init__(self):
        self.bots = {}

    def get_bot(self, user_id, user_data=None):
        if not user_data:
            return self.bots.get((user_id, True))

        is_mock = bool(user_data.get('is_mock', 1))
        bot_key = (user_id, is_mock)

        if bot_key not in self.bots:
            prefix = 'mock_' if is_mock else 'real_'
            kis_config = {
                "app_key": user_data.get(f'{prefix}app_key'),
                "app_secret": user_data.get(f'{prefix}app_secret'),
                "account_no": user_data.get(f'{prefix}account_no')
            }
            tele_config = {
                "token": user_data.get('telegram_token'),
                "chat_id": user_data.get('telegram_chat_id')
            }
            if is_mock:
                self.bots[bot_key] = MockBotController(user_id, kis_config, tele_config, core_stocks=user_data.get('core_stocks'))
            else:
                self.bots[bot_key] = RealBotController(user_id, kis_config, tele_config, core_stocks=user_data.get('core_stocks'))

        bot = self.bots[bot_key]

        # 각 봇은 자체 AI 인스턴스를 가짐 — 유저 간 채팅 히스토리 공유 방지
        # claude_api_key 우선, 없으면 gemini_api_key 사용
        active_key = (user_data.get('claude_api_key') or user_data.get('gemini_api_key') or '').strip()
        if active_key:
            if bot.gemini is None or getattr(bot.gemini, '_api_key', '') != active_key:
                bot.gemini = _make_ai_client(user_data)

        return bot

    def stop_all(self):
        for bot in self.bots.values():
            bot.stop()

manager = BotManager()