import json
import time
import threading
import websocket
from datetime import datetime, timezone, timedelta

_KST = timezone(timedelta(hours=9))

def _is_market_hours() -> bool:
    """KST 기준 평일 08:20~18:10 사이인지 확인 (웹소켓 유효 시간)."""
    now = datetime.now(_KST)
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return 8 * 60 + 20 <= t <= 18 * 60 + 10

def _secs_until_market_open() -> int:
    """다음 장 시작(08:20 KST)까지 남은 초."""
    now = datetime.now(_KST)
    target = now.replace(hour=8, minute=20, second=0, microsecond=0)
    if now >= target:
        target = target + timedelta(days=1)
    while target.weekday() >= 5:
        target = target + timedelta(days=1)
    return max(60, int((target - now).total_seconds()))

class KisMockWebSocket:
    """모의투자 전용 웹소켓 클라이언트"""
    
    def __init__(self, approval_key: str, price_callback=None):
        self.approval_key = approval_key
        self.price_callback = price_callback
        self.url = "ws://ops.koreainvestment.com:31000"
        self.tr_id = "H0STCNT0"
            
        self.ws = None
        self.is_running = False
        self.subscribed_tickers = set()
        self.lock = threading.Lock()
        self.wst = None

    def start(self):
        if self.is_running:
            return
        self.is_running = True
        self.wst = threading.Thread(target=self._run_loop, daemon=True)
        self.wst.start()

    def stop(self):
        self.is_running = False
        if self.ws:
            self.ws.close()

    def _run_loop(self):
        while self.is_running:
            # ── 장외 시간: 연결 시도 자제, 개장까지 대기 ──────────────────
            if not _is_market_hours():
                wait = _secs_until_market_open()
                print(f"[WebSocket 모의] 장외 시간 — {wait//60}분 후 개장 시 재연결 예정 (불필요한 연결 차단)")
                for _ in range(wait // 60):
                    if not self.is_running:
                        return
                    time.sleep(60)
                continue

            try:
                print(f"[WebSocket 모의] 증권사 통신 허브({self.url}) 연결을 수립합니다.")

                self.ws = websocket.WebSocketApp(
                    self.url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close
                )

                self.ws.run_forever(ping_interval=30, ping_timeout=10)

            except Exception as e:
                print(f"[WebSocket 모의] 네트워크 소켓 런타임 예외 발생: {e}")

            if self.is_running:
                if _is_market_hours():
                    print("[WebSocket 모의] 세션 흐름이 중단되었습니다. 5초 후 백그라운드 자동 복구를 개시합니다...")
                    time.sleep(5)
                else:
                    print("[WebSocket 모의] 장 마감 — 재연결 대기 모드로 전환합니다.")

    def _on_open(self, ws):
        print("[WebSocket 모의] 한투증권 모의투자 실시간 웹소켓 서버 연결 성공!")
        
        with self.lock:
            re_subscribe_list = list(self.subscribed_tickers)
            
        if re_subscribe_list:
            print(f"[WebSocket 모의] 시스템 재연결로 인해 기존 감시망에 있던 {len(re_subscribe_list)}개 종목을 실시간 재등록합니다.")
            for ticker in re_subscribe_list:
                self._send_subscription_packet(ticker, tr_type="1")
                time.sleep(0.15)

    def _on_message(self, ws, message):
        try:
            if message.startswith('{'):
                return

            split_frame = message.split('|')
            if len(split_frame) >= 4:
                payload = split_frame[3]
                data_segments = payload.split('^')
                
                if len(data_segments) >= 3:
                    ticker = data_segments[0]
                    current_price = int(float(data_segments[2]))
                    
                    if self.price_callback:
                        self.price_callback(ticker, current_price)
                        
        except Exception as e:
            print(f"[WebSocket 모의] 실시간 데이터 파싱 세그먼트 오류: {e}")

    def _on_error(self, ws, error):
        print(f"[WebSocket 모의 Error] 통신 에러 발생: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        print(f"[WebSocket 모의] 원격 서버가 접속 세션을 해제했습니다. (코드: {close_status_code}, 메시지: {close_msg})")

    def subscribe(self, ticker: str):
        with self.lock:
            if ticker in self.subscribed_tickers:
                return
            self.subscribed_tickers.add(ticker)
            
        if self.ws and self.ws.sock and self.ws.sock.connected:
            self._send_subscription_packet(ticker, tr_type="1")

    def unsubscribe(self, ticker: str):
        with self.lock:
            if ticker not in self.subscribed_tickers:
                return
            self.subscribed_tickers.remove(ticker)
            
        if self.ws and self.ws.sock and self.ws.sock.connected:
            self._send_subscription_packet(ticker, tr_type="2")

    def _send_subscription_packet(self, ticker: str, tr_type: str):
        packet = {
            "header": {
                "approval_key": self.approval_key,
                "custtype": "P",
                "tr_type": tr_type,
                "content-type": "utf-8"
            },
            "body": {
                "input": {
                    "tr_id": self.tr_id,
                    "tr_key": ticker
                }
            }
        }
        try:
            if self.ws and self.ws.sock and self.ws.sock.connected:
                self.ws.send(json.dumps(packet))
        except Exception as e:
            print(f"[WebSocket 모의] 네트워크 패킷 주입 실패 ({ticker}): {e}")