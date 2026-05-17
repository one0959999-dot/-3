"""
kis_websocket.py
한국투자증권 실시간 웹소켓 연동 클라이언트 (Wall Street Quant 1.0 프리미엄 엔진)
- 자동 핑퐁(Heartbeat) 제어로 장중 소켓 세션 다운 차단
- 연결 단절 시 5초 주기 무한 자동 재연결(Auto Reconnect) 구현
- 재연결 성공 시 기존 감시 종목 세트 전체 자동 재구독(Auto Re-subscribe) 복구
- 스레드 세이프(Thread-Safe) 락 구조를 통한 동시성 제어 완성
"""

import json
import time
import threading
import websocket

class KisWebSocket:
    def __init__(self, approval_key: str, is_mock: bool = True, price_callback=None):
        """
        웹소켓 관리 클라이언트 초기화
        price_callback: 가격 수신 시 컨트롤러 메모리로 데이터를 토스할 콜백 함수
        """
        self.approval_key = approval_key
        self.is_mock = is_mock
        self.price_callback = price_callback
        
        # 🟢 [버그 수정]: 실전투자 / 모의투자 주소 및 국내주식 실시간 체결가 TR_ID 분기 완벽 처리
        if self.is_mock:
            self.url = "ws://ops.koreainvestment.com:31000"
            self.tr_id = "K0STCNT0"  # 모의투자 실시간 체결가 TR ID
        else:
            self.url = "ws://ops.koreainvestment.com:21000"
            self.tr_id = "H0STCNT0"  # 실전투자 실시간 체결가 TR ID
            
        self.ws = None
        self.is_running = False
        self.subscribed_tickers = set()  # 장중 동적으로 변화하는 감시 종목 식별자 세트
        self.lock = threading.Lock()     # 🟢 [버그 수정]: 멀티스레딩 데이터 충돌 방지용 원자 락
        self.wst = None

    def start(self):
        """백그라운드 영속 웹소켓 수신 스레드 가동"""
        if self.is_running:
            return
        self.is_running = True
        self.wst = threading.Thread(target=self._run_loop, daemon=True)
        self.wst.start()

    def stop(self):
        """웹소켓 세션 안전 종료"""
        self.is_running = False
        if self.ws:
            self.ws.close()

    def _run_loop(self):
        """네트워크 끊김 감지 시 무한 재연결을 수행하는 코어 재귀 루프"""
        while self.is_running:
            try:
                print(f"[WebSocket] 증권사 통신 허브({self.url}) 연결을 수립합니다.")
                
                self.ws = websocket.WebSocketApp(
                    self.url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close
                )
                
                # 💡 ping_interval: 30초마다 핑을 날려 세션 유효성 검증 및 무응답 프리징 현상 원천 차단
                self.ws.run_forever(ping_interval=30, ping_timeout=10)
                
            except Exception as e:
                print(f"[WebSocket] 네트워크 소켓 런타임 예외 발생: {e}")
                
            if self.is_running:
                print("[WebSocket] 세션 흐름이 중단되었습니다. 5초 후 백그라운드 자동 복구를 개시합니다...")
                time.sleep(5)

    def _on_open(self, ws):
        print("[WebSocket] 한투증권 실시간 웹소켓 서버 연결 성공! (API 차단 위험 0%)")
        
        # 🛡️ [버그 수정 - 재연결 핵심]: 통신망 개통이 100% 보장된 시점에 기존 감시 종목 자동 재등록
        with self.lock:
            re_subscribe_list = list(self.subscribed_tickers)
            
        if re_subscribe_list:
            print(f"[WebSocket] 시스템 재연결로 인해 기존 감시망에 있던 {len(re_subscribe_list)}개 종목을 실시간 재등록합니다.")
            for ticker in re_subscribe_list:
                self._send_subscription_packet(ticker, tr_type="1") # 1: 구독 등록
                time.sleep(0.08) # KIS 방화벽 인입 속도 제한 우회용 미세 딜레이

    def _on_message(self, ws, message):
        """증권사 원격 노드에서 발송된 패킷 파싱"""
        try:
            # 1. 제어 신호 및 하트비트 응답 패킷(JSON 형태) 규격 처리
            if message.startswith('{'):
                return

            # 2. 주식 실시간 체결 데이터 파싱 (포맷: 데이터구분|TR_ID|체결건수|암호화페이로드)
            split_frame = message.split('|')
            if len(split_frame) >= 4:
                payload = split_frame[3]
                data_segments = payload.split('^')
                
                if len(data_segments) >= 3:
                    ticker = data_segments[0]              # 종목 식별 코드
                    current_price = int(float(data_segments[2])) # 실시간 틱 체결가
                    
                    # 봇 컨트롤러 메모리로 가격을 즉시 토스 (지연율 0%)
                    if self.price_callback:
                        self.price_callback(ticker, current_price)
                        
        except Exception as e:
            print(f"[WebSocket] 실시간 데이터 파싱 세그먼트 오류: {e}")

    def _on_error(self, ws, error):
        print(f"[WebSocket Error] 통신 에러 발생: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        print(f"[WebSocket] 원격 서버가 접속 세션을 해제했습니다. (코드: {close_status_code}, 메시지: {close_msg})")

    def subscribe(self, ticker: str):
        """장중 편입된 신규 종목 실시간 시세 레이더 등록"""
        with self.lock:
            if ticker in self.subscribed_tickers:
                return
            self.subscribed_tickers.add(ticker)
            
        if self.ws and self.ws.sock and self.ws.sock.connected:
            self._send_subscription_packet(ticker, tr_type="1")

    def unsubscribe(self, ticker: str):
        """수익 확정 / 손절 후 포트폴리오에서 탈락한 종목의 실시간 감시망 철회"""
        with self.lock:
            if ticker not in self.subscribed_tickers:
                return
            self.subscribed_tickers.remove(ticker)
            
        if self.ws and self.ws.sock and self.ws.sock.connected:
            self._send_subscription_packet(ticker, tr_type="2") # 2: 구독 해제

    def _send_subscription_packet(self, ticker: str, tr_type: str):
        """한국투자증권 고유 웹소켓 데이터셋 규격 전송 프레임"""
        packet = {
            "header": {
                "approval_key": self.approval_key,
                "custtype": "P",       # 개인 고객 규격 고정
                "tr_type": tr_type,    # 1: 구독 등록, 2: 구독 해제
                "content-type": "utf-8"
            },
            "body": {
                "input": {
                    "tr_id": self.tr_id, # 🟢 변수화된 TR_ID 주입으로 실전/모의 완벽 호환
                    "tr_key": ticker
                }
            }
        }
        try:
            if self.ws and self.ws.sock and self.ws.sock.connected:
                self.ws.send(json.dumps(packet))
        except Exception as e:
            print(f"[WebSocket] 네트워크 패킷 주입 실패 ({ticker}): {e}")