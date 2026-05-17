import json, sqlite3, os

# 🎉 수정 후: 본진 데이터베이스인 lassi.db 파일명을 절대 경로로 안전하게 정조준합니다.
db_path = os.path.join(os.path.dirname(__file__), 'lassi.db')
conn = sqlite3.connect(db_path)

c=conn.cursor()
c.execute('SELECT state_json FROM bot_states WHERE user_id=1')
row=c.fetchone()
if row and row[0]:
    state = json.loads(row[0])
    print(f"cores: {len(state.get('cores', []))}")
    print(f"satellites: {len(state.get('satellites', {}))}")
    print(f"satellite_info: {len(state.get('satellite_info', []))}")
    print(f"last_screen_month: {state.get('last_screen_month')}")
else:
    print("NO STATE")
