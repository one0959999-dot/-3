import websocket
import json
import threading
import time

class KisWebSocket:
    def __init__(self, approval_key, is_mock=True, price_callback=None):
        self.approval_key = approval_key
        self.is_mock = is_mock
        self.price_callback = price_callback
        # 실전/모의투자에 따른 웹소켓 전용 포트 구분
        self.ws_url = "ws://ops.koreainvestment.com:31000" if is_mock else "ws://ops.koreainvestment.com:21000"
        self.ws = None
        self.wst = None
        self.subscribed_tickers = set()

    def on_message(self, ws, message):
        # 1. KIS 서버에서 날아온 주식 체결 데이터 파싱
        if message.startswith('0') or message.startswith('1'):
            parts = message.split('|')
            if len(parts) >= 4:
                data_parts = parts[3].split('^')
                ticker = data_parts[0]
                price = float(data_parts[2])  # 실시간 현재가
                
                # 콜백 함수를 통해 봇 컨트롤러 메모리에 가격을 0.1초 만에 꽂아줌
                if self.price_callback:
                    self.price_callback(ticker, price)
                    
        # 2. 증권사 방화벽 생존 신고 (Ping-Pong)
        elif "PINGPONG" in message or "PING" in message.upper():
            self.ws.send(message)

    def on_error(self, ws, error):
        print(f"[WebSocket Error] 통신 에러 발생: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        print("[WebSocket] 서버 연결이 끊어졌습니다. 3초 후 자동 재연결합니다...")
        time.sleep(3)
        self.start()
        # 재연결 성공 시, 기존에 감시 중이던 종목들 자동으로 다시 감시 등록
        time.sleep(1)
        for ticker in list(self.subscribed_tickers):
            self._send_subscribe(ticker)

    def on_open(self, ws):
        print("[WebSocket] 한투증권 실시간 웹소켓 서버 연결 성공! (API 차단 위험 0%)")

    def start(self):
        self.ws = websocket.WebSocketApp(
            self.ws_url,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        # 백그라운드 데몬 스레드로 웹소켓 무한 가동
        self.wst = threading.Thread(target=self.ws.run_forever, daemon=True)
        self.wst.start()

    def _send_subscribe(self, ticker, tr_type="1"):
        """tr_type: '1'은 구독 등록, '2'는 구독 해제"""
        if not self.ws or not self.ws.sock or not self.ws.sock.connected:
            return
            
        header = {
            "approval_key": self.approval_key,
            "custtype": "P",
            "tr_type": tr_type,
            "content-type": "utf-8"
        }
        body = {
            "input": {
                "tr_id": "H0STCNT0",  # 국내주식 실시간 체결가 식별자
                "tr_key": ticker
            }
        }
        self.ws.send(json.dumps({"header": header, "body": body}))

    def subscribe(self, ticker):
        if ticker not in self.subscribed_tickers:
            self.subscribed_tickers.add(ticker)
            self._send_subscribe(ticker, "1")

    def unsubscribe(self, ticker):
        if ticker in self.subscribed_tickers:
            self.subscribed_tickers.remove(ticker)
            self._send_subscribe(ticker, "2")