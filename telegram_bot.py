import requests

# 텔레그램 단일 메시지 최대 길이 (API 제한 4096자)
_TG_MAX_LEN = 4096

class TelegramNotifier:
    """텔레그램을 통해 매매 알림을 보내는 클래스입니다."""

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id

    def _post(self, text: str):
        """단일 청크를 Telegram API로 전송합니다.
        parse_mode 없이 순수 텍스트로 전송 — HTML 특수문자(< > & * 등)가
        포함된 AI 응답에서 텔레그램 파서가 메시지를 끊는 문제 방지.
        """
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = {"chat_id": self.chat_id, "text": text}
        try:
            # timeout=5.0 — 기존 3.0보다 여유 있게, 단 매매 로직 차단 방지
            res = requests.post(url, data=data, timeout=5.0)
            if res.status_code != 200:
                print(f"[텔레그램 전송 실패] {res.text}")
        except Exception as e:
            print(f"⚠️ [텔레그램 알림 타임아웃/에러] 무시하고 매매를 계속 진행합니다: {e}")

    def send_message(self, text: str):
        """동기 방식으로 텔레그램 메시지 전송.
        4096자 초과 시 줄 단위로 분할해 연속 전송합니다 (짤림 방지).
        """
        if not self.token or not self.chat_id:
            print(f"[텔레그램 알림] (설정안됨) {text}")
            return

        if len(text) <= _TG_MAX_LEN:
            self._post(text)
            return

        # 4096자 초과 → 줄 단위로 청크 분할
        chunks = []
        current = []
        current_len = 0
        for line in text.splitlines(keepends=True):
            if current_len + len(line) > _TG_MAX_LEN and current:
                chunks.append("".join(current))
                current = []
                current_len = 0
            current.append(line)
            current_len += len(line)
        if current:
            chunks.append("".join(current))

        for i, chunk in enumerate(chunks):
            if i > 0:
                # 연속 메시지임을 표시
                chunk = f"📄 (이어서 {i+1}/{len(chunks)})\n" + chunk
            self._post(chunk)