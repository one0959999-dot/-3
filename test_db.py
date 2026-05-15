import sqlite3
import json
import os  # 1. os 모듈을 추가합니다.

# 2. 고정된 경로 대신 파일 위치 기준 경로를 사용합니다.
db_path = os.path.join(os.path.dirname(__file__), 'users.db')
conn = sqlite3.connect(db_path)

# 3. 커서 생성 (이 줄이 빠져서 에러가 났던 것입니다!)
c = conn.cursor()

# Get all users
users = c.execute('SELECT id, username FROM users').fetchall() #
print(f"Users: {users}")

# Get all bot_states (참고: 테이블 이름이 bot_states인지 portfolio_state인지 확인 필요)
states = c.execute('SELECT user_id, last_updated FROM bot_states').fetchall()
print(f"States found for user_ids: {states}")

# Dump bot_state for user 1
row = c.execute('SELECT state_json FROM bot_states WHERE user_id=1').fetchone()
if row and row[0]:
    state = json.loads(row[0])
    print(f"User 1 State cores: {len(state.get('cores', []))}")
    print(f"User 1 State satellites: {len(state.get('satellites', {}))}")
else:
    print("NO STATE for User 1")

conn.close()