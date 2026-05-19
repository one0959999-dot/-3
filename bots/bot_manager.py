from bots.real_bot import RealBotController
from bots.mock_bot import MockBotController
from gemini_api import GeminiApi

class BotManager:
    def __init__(self):
        self.bots = {}
        self.ai_client = None
        self.current_ai_key = ""

    def get_bot(self, user_id, user_data=None):
        if not user_data:
            return self.bots.get((user_id, True))

        is_mock = bool(user_data.get('is_mock', 1))
        bot_key = (user_id, is_mock)

        raw_api_key = user_data.get('gemini_api_key', '') or ''
        api_key_clean = raw_api_key.strip()

        if api_key_clean and self.current_ai_key != api_key_clean:
            self.ai_client = GeminiApi(api_key=api_key_clean)
            self.current_ai_key = api_key_clean

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

        if self.ai_client:
            self.bots[bot_key].gemini = self.ai_client

        return self.bots.get(bot_key)

    def stop_all(self):
        for bot in self.bots.values():
            bot.stop()

manager = BotManager()