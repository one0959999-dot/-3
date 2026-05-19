from bots.base_bot import BaseBot
from kis_brokers.kis_mock_api import KisMockApi
from kis_brokers.kis_mock_websocket import KisMockWebSocket

class MockBotController(BaseBot):
    """오직 모의투자 API 연동만을 책임지는 날렵한 자식 봇 클래스"""
    
    def __init__(self, user_id, kis_config=None, telegram_config=None, core_stocks=None):
        # 부모 클래스(BaseBot)에게 "나는 모의 모드(is_mock=True)야!"라고 알려주며 초기화합니다.
        super().__init__(user_id, kis_config, telegram_config, core_stocks, is_mock=True)
        
    def _init_api(self, kis_config):
        """부모의 빈 메서드를 채워, 모의투자 API 객체를 장착합니다."""
        if kis_config and kis_config.get('app_key'):
            self.kis = KisMockApi(
                app_key=kis_config.get('app_key', '').strip(),
                app_secret=kis_config.get('app_secret', '').strip(),
                account_no=kis_config.get('account_no', '').strip()
            )
        else:
            self.kis = None
            
    def _create_websocket(self, app_key, callback):
        """부모의 빈 메서드를 채워, 모의투자 전용 웹소켓을 연결합니다."""
        return KisMockWebSocket(app_key, price_callback=callback)