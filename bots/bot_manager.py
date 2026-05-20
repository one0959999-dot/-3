from bots.real_bot import RealBotController
from bots.mock_bot import MockBotController
from claude_api import ClaudeApi
from kis_brokers.kis_real_api import KisRealApi


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

        # 모의봇이면 실전 KIS를 real_kis로 주입 — 외인/기관 데이터 조회에 사용
        # 실전봇이 실행 중이면 그 인스턴스 재사용, 없으면 실전 키로 별도 인스턴스 생성
        if is_mock:
            real_bot = self.bots.get((user_id, False))
            if real_bot and real_bot.kis is not None:
                bot.real_kis = real_bot.kis
            elif user_data.get('real_app_key') and user_data.get('real_app_secret'):
                # 실전봇 미실행 상태에서도 외인/기관 조회용 KisRealApi 인스턴스 생성
                try:
                    existing = getattr(bot, 'real_kis', None)
                    app_key = user_data['real_app_key'].strip()
                    # 키가 바뀌지 않았으면 재생성 안 함
                    if not existing or getattr(existing, 'app_key', '') != app_key:
                        bot.real_kis = KisRealApi(
                            app_key=app_key,
                            app_secret=user_data['real_app_secret'].strip(),
                            account_no=(user_data.get('real_account_no') or '').strip()
                        )
                except Exception:
                    pass

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