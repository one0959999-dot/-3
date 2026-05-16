import sqlite3
import os
import json
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

DB_PATH = 'users.db'

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. 사용자 테이블 (users) 생성
    # SQL 내부 주석은 -- 를 사용해야 합니다.
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        
        -- 실전투자용 API 키 및 계좌번호
        real_app_key TEXT,
        real_app_secret TEXT,
        real_account_no TEXT,
        
        -- 모의투자용 API 키 및 계좌번호
        mock_app_key TEXT,
        mock_app_secret TEXT,
        mock_account_no TEXT,
        
        -- 기존 호환용 컬럼 (유지)
        kis_app_key TEXT,
        kis_app_secret TEXT,
        kis_account_no TEXT,
        
        telegram_token TEXT,
        telegram_chat_id TEXT,
        gemini_api_key TEXT,
        initial_cash REAL DEFAULT 10000000,
        is_running INTEGER DEFAULT 0,
        is_mock INTEGER DEFAULT 1,
        core_stocks TEXT
    )
    ''')

    # 2. 기존 사용자를 위한 컬럼 추가 (ALTER TABLE)
    new_columns = [
        ('real_app_key', 'TEXT'), ('real_app_secret', 'TEXT'), ('real_account_no', 'TEXT'),
        ('mock_app_key', 'TEXT'), ('mock_app_secret', 'TEXT'), ('mock_account_no', 'TEXT'),
        ('gemini_api_key', 'TEXT'), ('is_running', 'INTEGER DEFAULT 0'), 
        ('core_stocks', 'TEXT'), ('is_mock', 'INTEGER DEFAULT 1')
    ]
    for col_name, col_type in new_columns:
        try:
            cursor.execute(f'ALTER TABLE users ADD COLUMN {col_name} {col_type}')
        except sqlite3.OperationalError:
            pass 

    # 3. 봇 상태 테이블 (bot_states) - 실전/모의 장부 분리
    # 💡 마이그레이션용 DROP 구문을 제거하여 서버 재시작 시 기존 데이터가 증발하는 현상을 방지합니다.
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS bot_states (
        user_id INTEGER,
        is_mock INTEGER, 
        state_json TEXT,
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, is_mock),
        FOREIGN KEY (user_id) REFERENCES users (id)
    )
    ''')
    
    # [추가] 서버가 새로 시작될 때, 비정상 종료로 인해 DB에 1(실행중)로 남아있던 좀비 상태를 0(정지)으로 클리어합니다.
    # 이로써 사용자가 대시보드에서 직접 [시작] 버튼을 눌러야만 매매가 시작되도록 제어합니다.
    cursor.execute('UPDATE users SET is_running = 0')
    
    conn.commit()
    conn.close()

def add_user(username, password):
    conn = get_db_connection()
    cursor = conn.cursor()
    password_hash = generate_password_hash(password)
    try:
        cursor.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)', (username, password_hash))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def verify_user(username, password):
    conn = get_db_connection()
    cursor = conn.cursor()
    user = cursor.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
    conn.close()
    
    if user and check_password_hash(user['password_hash'], password):
        return dict(user)
    return None

def update_user_keys(user_id, keys_dict):
    """모든 API 키(실전/모의 포함)를 업데이트합니다."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE users 
        SET real_app_key = ?, real_app_secret = ?, real_account_no = ?,
            mock_app_key = ?, mock_app_secret = ?, mock_account_no = ?,
            telegram_token = ?, telegram_chat_id = ?, gemini_api_key = ?, 
            core_stocks = ?, is_mock = ?
        WHERE id = ?
    ''', (
        keys_dict.get('real_app_key'),
        keys_dict.get('real_app_secret'),
        keys_dict.get('real_account_no'),
        keys_dict.get('mock_app_key'),
        keys_dict.get('mock_app_secret'),
        keys_dict.get('mock_account_no'),
        keys_dict.get('telegram_token'),
        keys_dict.get('telegram_chat_id'),
        keys_dict.get('gemini_api_key'),
        keys_dict.get('core_stocks'),
        keys_dict.get('is_mock', 1),
        user_id
    ))
    conn.commit()
    conn.close()

def update_bot_status(user_id, is_running):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET is_running = ? WHERE id = ?', (1 if is_running else 0, user_id))
    conn.commit()
    conn.close()

def save_portfolio_state(user_id, state, is_mock):
    """실전/모의투자 모드에 맞춰 상태를 분리 저장합니다."""
    mode = 1 if is_mock else 0
    conn = get_db_connection()
    conn.execute('''
        INSERT OR REPLACE INTO bot_states (user_id, is_mock, state_json, last_updated) 
        VALUES (?, ?, ?, ?)
    ''', (user_id, mode, json.dumps(state, ensure_ascii=False), datetime.now()))
    conn.commit()
    conn.close()

def load_portfolio_state(user_id, is_mock):
    """실전/모의투자 모드에 맞춰 상태를 불러옵니다."""
    mode = 1 if is_mock else 0
    conn = get_db_connection()
    row = conn.execute('SELECT state_json FROM bot_states WHERE user_id = ? AND is_mock = ?', 
                       (user_id, mode)).fetchone()
    conn.close()
    if row and row['state_json']:
        return json.loads(row['state_json'])
    return None

if __name__ == '__main__':
    init_db()
    print("Database initialized.")