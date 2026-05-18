import sqlite3
import os
from kis_api import KisApi

print("🚀 [독립 테스트] KIS API 1주 강제 매수 스크립트 시작 (DB 연동)...")

# 1. DB에서 키 읽어오기
db_path = os.path.join(os.path.dirname(__file__), 'lassi.db')
try:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    user = conn.execute("SELECT * FROM users WHERE id = 1").fetchone()
    conn.close()
    
    if not user:
        print("❌ DB에서 사용자 정보를 찾을 수 없습니다.")
        exit()
        
    is_mock = bool(user['is_mock'])
    prefix = 'mock_' if is_mock else 'real_'
    
    app_key = user[f'{prefix}app_key']
    app_secret = user[f'{prefix}app_secret']
    account_no = user[f'{prefix}account_no']
    
    if not app_key or not app_secret or not account_no:
        print(f"❌ DB에 {prefix} API 키 또는 계좌번호가 비어있습니다. 웹 설정창에서 먼저 입력해주세요.")
        exit()
        
except Exception as e:
    print(f"❌ DB 읽기 실패: {e}")
    exit()

mode_str = "모의투자" if is_mock else "실전투자"
print(f"🔑 [{mode_str}] 계좌 정보 로드 완료.")

# 2. KIS API 연결
kis = KisApi(app_key=app_key, app_secret=app_secret, account_no=account_no, is_mock=is_mock)
token = kis.get_access_token()

if not token:
    print("❌ KIS API 토큰 발급 실패. API 키와 계좌번호를 확인하세요.")
    exit()

# 3. 강제 매수 실행 (삼성전자: 005930)
target_ticker = "005930"
target_qty = 1

print(f"🎯 삼성전자({target_ticker}) {target_qty}주 시장가 매수 주문 발송 중...")
result = kis.buy_market_order(target_ticker, target_qty)

if result:
    print(f"\n✅ [테스트 대성공] 주문이 정상적으로 접수되었습니다!")
    print(f"응답 데이터: {result}")
    print(f"👉 지금 바로 한국투자증권 앱({mode_str})에 들어가서 체결 내역을 확인해 보세요!")
else:
    print(f"\n❌ [테스트 실패] 주문이 거절되었습니다. (장외 시간, 증거금 부족 등 터미널 오류 메시지를 확인하세요)")
