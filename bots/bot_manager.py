from bots.kr_bot import KRBotController   # KR 실전 봇
from bots.us_bot import USBotController   # US 실전 매매 봇
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
            tele_config = {
                "token": user_data.get('telegram_token'),
                "chat_id": user_data.get('telegram_chat_id')
            }
            if is_mock:
                # ── US 모드 (is_mock=True) → 미국장 실전 매매 봇 (KIS 해외주식) ──
                us_kis_config = {
                    "app_key":    user_data.get('mock_app_key'),
                    "app_secret": user_data.get('mock_app_secret'),
                    "account_no": user_data.get('mock_account_no'),
                }
                self.bots[bot_key] = USBotController(
                    user_id,
                    kis_config=us_kis_config,
                    telegram_config=tele_config,
                    core_stocks=user_data.get('core_stocks'),
                )
            else:
                # ── KR 모드 (is_mock=False) → 한국 실전 봇 ──
                kis_config = {
                    "app_key":    user_data.get('real_app_key'),
                    "app_secret": user_data.get('real_app_secret'),
                    "account_no": user_data.get('real_account_no'),
                }
                self.bots[bot_key] = KRBotController(
                    user_id, kis_config, tele_config,
                    core_stocks=user_data.get('core_stocks'),
                )

        bot = self.bots[bot_key]

        # KR 실전봇: real_kis 인스턴스 주입 (외인/기관 데이터 조회용)
        # US 봇(is_mock=True)은 yfinance만 사용하므로 KIS 주입 불필요
        if not is_mock:
            real_bot = self.bots.get((user_id, False))
            if real_bot and getattr(real_bot, 'real_kis', None) is None:
                if user_data.get('real_app_key') and user_data.get('real_app_secret'):
                    try:
                        app_key = user_data['real_app_key'].strip()
                        existing = getattr(bot, 'real_kis', None)
                        if not existing or getattr(existing, 'app_key', '') != app_key:
                            bot.real_kis = KisRealApi(
                                app_key=app_key,
                                app_secret=user_data['real_app_secret'].strip(),
                                account_no=(user_data.get('real_account_no') or '').strip(),
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