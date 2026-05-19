from bots.real_bot import RealBotController
from bots.mock_bot import MockBotController
from gemini_api import GeminiApi

class BotManager:
    def __init__(self):
        self.bots = {}

    def get_bot(self, user_id, user_data=None):
        if not user_data:
            return self.bots.get((user_id, True))

        is_mock = bool(user_data.get('is_mock', 1))
        bot_key = (user_id, is_mock)

        raw_api_key = user_data.get('gemini_api_key', '') or ''
        api_key_clean = raw_api_key.strip()

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

        # 각 봇은 자체 GeminiApi 인스턴스를 가짐 — 유저 간 채팅 히스토리 공유 방지
        if api_key_clean:
            if bot.gemini is None or getattr(bot.gemini, '_api_key', '') != api_key_clean:
                bot.gemini = GeminiApi(api_key=api_key_clean)
                bot.gemini._api_key = api_key_clean

        return bot

    def stop_all(self):
        for bot in self.bots.values():
            bot.stop()

manager = BotManager()