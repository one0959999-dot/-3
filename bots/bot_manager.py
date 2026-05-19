from bots.real_bot import RealBotController
from bots.mock_bot import MockBotController
from gemini_api import GeminiApi

class BotManager:
    def __init__(self):
        self.bots = {}        
        self.ai_client = None  # 🧠 중앙 관제탑 (싱글톤 AI)
        self.current_ai_key = ""

    def get_bot(self, user_id, user_data=None):
        if not user_data: 
            return self.bots.get((user_id, True))
            
        is_mock = bool(user_data.get('is_mock', 1))
        bot_key = (user_id, is_mock)
        
        # 🚨 [버그 픽스] API 키가 DB에 없을 때(None) 서버가 뻗는 치명적 현상 완벽 방어!
        raw_api_key = user_data.get('gemini_api_key', '')
        if raw_api_key is None:
            raw_api_key = ''
        api_key_clean = raw_api_key.strip()
        
        # 🧠 1. AI 두뇌 중앙 집중화
        if api_key_clean and self.current_ai_key != api_key_clean:
            self.ai_client = GeminiApi(api_key=api_key_clean)
            self.current_ai_key = api_key_clean

        # 🤖 2. 봇 객체 생성 및 관리 (물리적으로 분리된 실전/모의 봇 분기)
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
            
            # 독립된 클래스로 생성하여 런타임 간섭 원천 차단
            if is_mock:
                self.bots[bot_key] = MockBotController(user_id, kis_config, tele_config, core_stocks=user_data.get('core_stocks'))
            else:
                self.bots[bot_key] = RealBotController(user_id, kis_config, tele_config, core_stocks=user_data.get('core_stocks'))
            
        # 3. 생성된 봇에 중앙 AI 두뇌 연결
        if self.ai_client: 
            self.bots[bot_key].gemini = self.ai_client
            
        return self.bots.get(bot_key)

    def stop_all(self):
        for bot in self.bots.values(): 
            bot.stop()

manager = BotManager()