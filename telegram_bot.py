import requests

class TelegramNotifier:
    """텔레그램을 통해 매매 알림을 보내는 클래스입니다."""
    
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
            
    def send_message(self, text: str):
        """동기 방식으로 텔레그램 메시지 전송"""
        if not self.token or not self.chat_id:
            print(f"[텔레그램 알림] (설정안됨) {text}")
            return
            
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = {
            "chat_id": self.chat_id,
            "text": text
        }
        try:
            # 🛠️ [버그 수정] timeout=3.0을 추가하여 텔레그램 API 서버 지연 시 매매 로직 전체가 마비되는 것을 막습니다.
            res = requests.post(url, data=data, timeout=3.0)
            if res.status_code != 200:
                print(f"[텔레그램 전송 실패] {res.text}")
        except Exception as e:
            # 타임아웃 에러가 나도 메인 매매 스레드는 멈추지 않고 안전하게 다음 로직으로 넘어갑니다.
            print(f"⚠️ [텔레그램 알림 타임아웃/에러] 무시하고 매매를 계속 진행합니다: {e}")