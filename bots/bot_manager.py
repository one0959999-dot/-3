from bots.real_bot import RealBotController
from bots.mock_bot import MockBotController
from claude_api import ClaudeApi


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

        # 모의봇이면 실전봇의 KIS를 real_kis로 주입 — 외인/기관 데이터 조회에 사용
        if is_mock:
            real_bot = self.bots.get((user_id, False))
            if real_bot and real_bot.kis is not None:
                bot.real_kis = real_bot.kis

        # 각 봇은 자체 ClaudeApi 인스턴스를 가짐 — 유저 간 채팅 히스토리 공유 방지
        api_key = (user_data.get('claude_api_key') or '').strip()
        if api_key:
            if bot.gemini is None or getattr(bot.gemini, '_api_key', '') != api_key:
                try:
                    bot.gemini = ClaudeApi(api_key=api_key)
                    # anthropic 미설치 시 client=None → AI 기능 비활성화, 봇은 정상 동작
                except Exception as e:
                    import logging
                    logging.getLogger('lassi_bot').warning(f"ClaudeApi 초기화 실패 (AI 비활성화): {e}")
                    bot.gemini = None

        return bot

    def stop_all(self):
        for bot in self.bots.values():
            bot.stop()

manager = BotManager()