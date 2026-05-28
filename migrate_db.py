"""
lassi.db 스키마 마이그레이션 스크립트
- mock_app_key/secret/account → us_app_key/secret/account 로 컬럼 추가 후 데이터 복사
- mock_initial_cash → us_initial_cash 복사
- 누락된 모든 컬럼 추가 (dart, naver, sector_guide 등)
실행: python migrate_db.py
"""
import sqlite3, os, shutil
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), 'lassi.db')

def migrate():
    # ── 백업 ────────────────────────────────────────────────────
    backup = DB_PATH + '.bak.' + datetime.now().strftime('%Y%m%d_%H%M%S')
    shutil.copy2(DB_PATH, backup)
    print(f'✅ 백업 완료: {backup}')

    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    existing = {r[1] for r in cur.execute('PRAGMA table_info(users)').fetchall()}
    print(f'현재 컬럼 {len(existing)}개: {sorted(existing)}')

    # ── 추가할 컬럼 정의 (컬럼명, 타입, 기본값) ─────────────────
    add_cols = [
        ('us_app_key',          'TEXT',    None),
        ('us_app_secret',       'TEXT',    None),
        ('us_account_no',       'TEXT',    None),
        ('us_initial_cash',     'REAL',    10000000),
        ('dart_api_key',        'TEXT',    None),
        ('naver_client_id',     'TEXT',    None),
        ('naver_client_secret', 'TEXT',    None),
        ('sector_guide',        'TEXT',    None),
        ('satellite_stocks',    'TEXT',    None),
        ('us_core_stocks',      'TEXT',    None),
        ('us_satellite_stocks', 'TEXT',    None),
        ('chat_history_kr',     'TEXT',    None),
        ('chat_history_us',     'TEXT',    None),
    ]

    for col, typ, default in add_cols:
        if col in existing:
            print(f'  skip  {col} (이미 존재)')
            continue
        if default is not None:
            cur.execute(f'ALTER TABLE users ADD COLUMN {col} {typ} DEFAULT {default}')
        else:
            cur.execute(f'ALTER TABLE users ADD COLUMN {col} {typ}')
        print(f'  +추가  {col}')

    # ── mock_* → us_* 데이터 복사 ───────────────────────────────
    copies = [
        ('mock_app_key',    'us_app_key'),
        ('mock_app_secret', 'us_app_secret'),
        ('mock_account_no', 'us_account_no'),
        ('mock_initial_cash','us_initial_cash'),
    ]
    for src_col, dst_col in copies:
        if src_col in existing:
            cur.execute(f'''
                UPDATE users SET {dst_col} = {src_col}
                WHERE {dst_col} IS NULL AND {src_col} IS NOT NULL
            ''')
            changed = cur.rowcount
            print(f'  복사  {src_col} → {dst_col}  ({changed}행)')
        else:
            print(f'  skip  {src_col} 없음')

    conn.commit()
    conn.close()

    # ── 결과 검증 ────────────────────────────────────────────────
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute('SELECT * FROM users LIMIT 1').fetchone() or {})
    conn.close()

    print()
    print('=== 마이그레이션 후 주요 컬럼 ===')
    check = ['real_app_key','real_account_no','us_app_key','us_account_no',
             'us_initial_cash','real_initial_cash','claude_api_key','sector_guide']
    for k in check:
        v = row.get(k)
        disp = (str(v)[:12]+'****') if v and len(str(v)) > 12 else v
        status = '✅' if v else '⬜ (비어있음)'
        print(f'  {k:22s}: {disp}  {status}')

    print()
    print('마이그레이션 완료!')

if __name__ == '__main__':
    migrate()
