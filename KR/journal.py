"""봇 일기장 — 매주 실행결과·목표포트폴리오·계좌상태를 EC2에 누적 기록.

목적: 나중에 "백테스트대로 갔나 / 실제와 얼마나 맞나"를 분석 (3·4번 항목).
저장: algo_journal.jsonl (한 줄 = 한 실행). live_v1/auto_order가 호출.
또 dead-man용 heartbeat.txt(마지막 정상실행 시각) 갱신.

조회: python KR/journal.py            # 최근 기록 요약
      python KR/journal.py --report   # 백테스트 대조 리포트(텔레그램)
"""
import sys, os, json, sqlite3, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
JOURNAL = P('algo_journal.jsonl')
HEARTBEAT = P('heartbeat.txt')


def now_iso():
    import time
    return time.strftime('%Y-%m-%d %H:%M:%S')


def record(kind, market, payload):
    """한 실행 기록 추가 + heartbeat 갱신. kind: plan/probe/execute. payload: dict."""
    row = {'ts': now_iso(), 'kind': kind, 'market': market}
    row.update(payload)
    try:
        with open(JOURNAL, 'a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
    except Exception as e:
        print('journal 기록 실패:', e)
    beat(market, kind)


def beat(market='', kind=''):
    """dead-man용: 마지막 정상 실행 시각·주체 기록."""
    try:
        tmp = HEARTBEAT + '.tmp'
        open(tmp, 'w').write(f"{now_iso()}|{market}|{kind}")
        os.replace(tmp, HEARTBEAT)
    except Exception:
        pass


def read_journal(n=None):
    if not os.path.exists(JOURNAL):
        return []
    rows = [json.loads(l) for l in open(JOURNAL, encoding='utf-8') if l.strip()]
    return rows[-n:] if n else rows


def summary():
    rows = read_journal()
    if not rows:
        print("기록 없음"); return
    print(f"총 {len(rows)}건 기록 ({rows[0]['ts']} ~ {rows[-1]['ts']})")
    for r in rows[-10:]:
        extra = r.get('n_picks', r.get('deploy', r.get('note', '')))
        print(f"  {r['ts']} [{r['market']}/{r['kind']}] {extra}")


def report(telegram=False):
    """최근 기록으로 백테스트 대조 요약."""
    rows = read_journal()
    plans = [r for r in rows if r['kind'] == 'plan' and r['market'] == 'KR']
    L = [f"📒 봇 일기장 리포트 ({len(rows)}건 누적)"]
    if plans:
        first, last = plans[0], plans[-1]
        L.append(f"KR 계획 기록: {len(plans)}주 ({first['ts'][:10]} ~ {last['ts'][:10]})")
        L.append(f"최근 목표: 저변동 {last.get('n_picks','?')}종목, 상위: {', '.join(last.get('top', [])[:5])}")
    else:
        L.append("아직 계획 기록 없음 (첫 크론 실행 대기)")
    hb = open(HEARTBEAT).read() if os.path.exists(HEARTBEAT) else '없음'
    L.append(f"마지막 정상실행(heartbeat): {hb}")
    rep = "\n".join(L)
    print(rep)
    if telegram:
        try:
            c = sqlite3.connect(P('lassi.db'), timeout=30)
            r = c.execute("SELECT telegram_token, telegram_chat_id FROM users WHERE telegram_token IS NOT NULL AND telegram_token!='' LIMIT 1").fetchone(); c.close()
            from base.telegram_bot import TelegramNotifier
            TelegramNotifier(r[0], r[1]).send_message(rep)
        except Exception:
            pass


if __name__ == '__main__':
    if '--report' in sys.argv:
        report('--telegram' in sys.argv or True)
    else:
        summary()
