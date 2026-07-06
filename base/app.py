# -*- coding: utf-8 -*-
"""클린 대시보드 v2 — 새 체제(교과서 v3 + 참고서, 크론 자동매매) 조회 전용.

★구봇/매매 코드 전무. 이 앱은 절대 주문을 내지 않는다(매매는 EC2 크론=auto_order·auto_deploy·
  auto_order_us가 담당). 계좌는 toss_api 직접조회, 원금은 사용자 수동설정(자동감지 없음 = 원금버그 근절).
실행: python base/app.py (로컬)  /  gunicorn base.app:app (서버)
"""
import os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from flask import Flask, request, redirect, url_for, render_template_string, flash
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                         login_required, current_user)
from base.database import (get_db_connection, verify_user, get_user_initial_cash,
                           set_user_initial_cash, init_db)
from base.toss_api import TossInvestApi

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def P(f):
    return os.path.join(ROOT, f)

app = Flask(__name__)
app.secret_key = os.environ.get('LASSI_SECRET', 'lassi-dash-v2')
login_manager = LoginManager(app)
login_manager.login_view = 'login'


class User(UserMixin):
    def __init__(self, row):
        self.id = str(row['id']); self.username = row['username']; self.row = row


def _user_row(uid):
    c = get_db_connection()
    try:
        return c.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    finally:
        c.close()


@login_manager.user_loader
def load_user(uid):
    row = _user_row(uid)
    return User(row) if row else None


def _toss(row):
    return TossInvestApi(row['toss_client_id'], row['toss_client_secret'], row['toss_account_seq'] or '')


def _fmt(n, won=True):
    try:
        return (f"{float(n):,.0f}원" if won else f"${float(n):,.2f}")
    except Exception:
        return "-"


def kr_snapshot(row):
    """KR 계좌 스냅샷 — toss 직접조회. 실패시 error."""
    out = {'holdings': [], 'cash': 0, 'total': 0, 'principal': 0, 'ret': None, 'error': None}
    try:
        t = _toss(row)
        bal = t.get_account_balance()
        if not bal:
            out['error'] = '계좌조회 실패(토큰/IP/API)'; return out
        hv = 0.0
        for s in bal.get('stocks', []):
            q = int(s.get('shares', 0) or 0)
            if q <= 0:
                continue
            px = float(s.get('current_price', 0) or 0) or float(s.get('purchase_price', 0) or 0)
            val = q * px
            hv += val
            out['holdings'].append({'name': s.get('name', s['ticker']), 'ticker': s['ticker'],
                                    'qty': q, 'price': px, 'value': val,
                                    'is_etf': s['ticker'] == '069500'})
        cash = t.get_buyable_cash(default=None)
        cash = float(cash) if cash is not None else 0.0
        out['cash'] = cash
        out['total'] = cash + hv
        out['hold_val'] = hv
        pr = get_user_initial_cash(int(row['id']), is_mock=False)
        out['principal'] = pr
        if pr and pr > 0:
            out['ret'] = (out['total'] / pr - 1) * 100
        out['holdings'].sort(key=lambda x: (-x['is_etf'], -x['value']))
    except Exception as e:
        out['error'] = f'{type(e).__name__}: {e}'
    return out


def us_snapshot(row):
    out = {'holdings': [], 'cash_usd': 0, 'error': None}
    try:
        t = _toss(row)
        b = t.get_balance()
        if not b:
            out['error'] = 'US 계좌조회 실패'; return out
        out['cash_usd'] = float(b.get('cash_usd', 0) or 0)
        for s in b.get('stocks', []):
            q = float(s.get('shares', 0) or 0)
            if q > 0:
                out['holdings'].append({'ticker': s.get('ticker'), 'qty': q})
    except Exception as e:
        out['error'] = f'{type(e).__name__}: {e}'
    return out


def recent_trades(uid, n=15):
    c = get_db_connection()
    try:
        rows = c.execute("SELECT ticker, stock_name, action, price, shares, strategy, created_at, mode "
                         "FROM trade_journal WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, n)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        c.close()


def automation_status():
    """크론 자동화 상태 — heartbeat/마커 파일 기반(로컬·EC2 공통 경로)."""
    st = {}
    for label, f in [('deadman', 'heartbeat.txt'), ('신규배분', 'auto_deploy_done.txt'),
                     ('US통화검증', 'first_fill_verified_us.txt'), ('리밸상태', 'rebalance_state.txt')]:
        try:
            st[label] = open(P(f)).read().strip()[:40]
        except Exception:
            st[label] = '—'
    # 참고서 필터
    try:
        from KR.reference import artifact_tickers
        st['참고서_아티팩트제외'] = f"{len(artifact_tickers())}종목"
    except Exception:
        st['참고서_아티팩트제외'] = '—'
    # 다음 리밸주간
    try:
        from KR.live_v1 import is_rebalance_week
        st['리밸주간'] = '예(1·4·7·10월 첫주)' if is_rebalance_week() else '아니오(유지주간)'
    except Exception:
        st['리밸주간'] = '—'
    return st


PAGE = """<!doctype html><html lang=ko><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Lassi 대시보드</title><style>
*{box-sizing:border-box} body{font-family:system-ui,'Malgun Gothic',sans-serif;margin:0;background:#0f1420;color:#e6ebf5}
.wrap{max-width:960px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:8px 0} h2{font-size:15px;color:#9fb3d1;margin:20px 0 8px;border-bottom:1px solid #223}
.card{background:#171f30;border:1px solid #26324a;border-radius:12px;padding:16px;margin:10px 0}
.big{font-size:28px;font-weight:700} .pos{color:#4ade80} .neg{color:#f87171} .muted{color:#7a8aa5;font-size:13px}
table{width:100%;border-collapse:collapse;font-size:13px} td,th{padding:6px 8px;text-align:right;border-bottom:1px solid #1f2942}
th{color:#9fb3d1;text-align:right} td.l,th.l{text-align:left}
.etf{color:#60a5fa;font-weight:600} .row{display:flex;gap:12px;flex-wrap:wrap} .row>div{flex:1;min-width:140px}
input{background:#0f1420;border:1px solid #2c3a58;color:#e6ebf5;padding:8px;border-radius:8px;width:140px}
button{background:#2563eb;color:#fff;border:0;padding:8px 14px;border-radius:8px;cursor:pointer}
a{color:#60a5fa} .warn{background:#3a2318;border-color:#7c3a1a;color:#fbbf24}
.st{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid #1f2942;font-size:13px}
</style></head><body><div class=wrap>
<h1>🤖 Lassi 대시보드 <span class=muted>· 교과서(v3) + 참고서 · 크론 자동매매 · 조회전용</span></h1>
<div class=muted>{{now}} · <a href="{{url_for('logout')}}">로그아웃</a> · 매매는 EC2 크론이 담당(이 화면은 주문 안 냄)</div>

<h2>🇰🇷 KR 계좌</h2>
{% if kr.error %}<div class="card warn">⚠️ {{kr.error}}</div>{% else %}
<div class=card><div class=row>
  <div><div class=muted>총자산</div><div class=big>{{ '{:,.0f}'.format(kr.total) }}원</div></div>
  <div><div class=muted>원금</div><div style="font-size:20px">{{ '{:,.0f}'.format(kr.principal) }}원</div></div>
  <div><div class=muted>수익률</div><div class="big {{'pos' if (kr.ret or 0)>=0 else 'neg'}}">{{ '%+.2f'|format(kr.ret) if kr.ret is not none else '-' }}%</div></div>
  <div><div class=muted>현금(미투입)</div><div style="font-size:20px">{{ '{:,.0f}'.format(kr.cash) }}원</div></div>
</div></div>
<div class=card><table><tr><th class=l>종목</th><th>수량</th><th>현재가</th><th>평가액</th><th>비중</th></tr>
{% for h in kr.holdings %}<tr><td class="l {{'etf' if h.is_etf}}">{{h.name}} <span class=muted>{{h.ticker}}</span></td>
<td>{{h.qty}}</td><td>{{ '{:,.0f}'.format(h.price) }}</td><td>{{ '{:,.0f}'.format(h.value) }}</td>
<td>{{ '%.1f'|format(h.value/kr.total*100 if kr.total else 0) }}%</td></tr>{% endfor %}
</table></div>{% endif %}

<h2>💵 원금 수동설정 <span class=muted>(자동감지 없음 = 원금버그 근절)</span></h2>
<div class=card><form method=post action="{{url_for('set_principal')}}">
실제 투자원금: <input name=amount type=number placeholder="예: 6380000"> 원
<button type=submit>원금 확정</button>
<div class=muted style="margin-top:6px">수익률 = 총자산 ÷ 이 값 − 1. 실제 넣은 돈을 입력하세요.</div>
</form></div>

<h2>🇺🇸 US 계좌</h2>
{% if us.error %}<div class="card warn">⚠️ {{us.error}}</div>{% else %}
<div class=card>USD 예수금 <b>${{ '%.2f'|format(us.cash_usd) }}</b>
{% for h in us.holdings %} · {{h.ticker}} {{h.qty}}주{% endfor %}
{% if not us.holdings %}<span class=muted>(SPY 미보유 — 환전 후 크론이 자동매수)</span>{% endif %}</div>{% endif %}

<h2>⚙️ 자동화 상태</h2>
<div class=card>
{% for k,v in status.items() %}<div class=st><span class=muted>{{k}}</span><span>{{v}}</span></div>{% endfor %}
<div class=muted style="margin-top:8px">크론: 리밸 평일10:00 · 신규배분 평일10:30 · US 새벽(미국장) · deadman 매일</div>
</div>

<h2>📜 최근 거래</h2>
<div class=card><table><tr><th class=l>일시</th><th class=l>종목</th><th>구분</th><th>가격</th><th>수량</th><th class=l>전략</th></tr>
{% for t in trades %}<tr><td class=l>{{t.created_at[5:16]}}</td><td class=l>{{t.stock_name}}</td>
<td class="{{'neg' if t.action=='SELL' else 'pos'}}">{{t.action}}</td>
<td>{{ '{:,.0f}'.format(t.price) }}</td><td>{{ '{:.0f}'.format(t.shares) }}</td><td class=l>{{t.strategy or '-'}}</td></tr>{% endfor %}
</table></div>
<div class=muted style="text-align:center;margin:20px 0"><a href="{{url_for('dashboard')}}">↻ 새로고침</a></div>
</div></body></html>"""

LOGIN = """<!doctype html><html lang=ko><head><meta charset=utf-8><title>로그인</title>
<style>body{font-family:system-ui,sans-serif;background:#0f1420;color:#e6ebf5;display:flex;height:100vh;align-items:center;justify-content:center}
form{background:#171f30;padding:28px;border-radius:12px;border:1px solid #26324a;width:280px}
input{width:100%;padding:10px;margin:6px 0;background:#0f1420;border:1px solid #2c3a58;color:#e6ebf5;border-radius:8px}
button{width:100%;padding:10px;background:#2563eb;color:#fff;border:0;border-radius:8px;margin-top:8px;cursor:pointer}
.e{color:#f87171;font-size:13px}</style></head><body>
<form method=post><h2>🤖 Lassi 대시보드</h2>{% if error %}<div class=e>{{error}}</div>{% endif %}
<input name=username placeholder=아이디 autofocus><input name=password type=password placeholder=비밀번호>
<button>로그인</button></form></body></html>"""


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        u = verify_user(request.form.get('username', ''), request.form.get('password', ''))
        if u:
            login_user(User(u)); return redirect(url_for('dashboard'))
        error = '아이디 또는 비밀번호가 틀립니다.'
    return render_template_string(LOGIN, error=error)


@app.route('/logout')
@login_required
def logout():
    logout_user(); return redirect(url_for('login'))


@app.route('/')
@login_required
def dashboard():
    row = current_user.row
    return render_template_string(
        PAGE, now=datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
        kr=kr_snapshot(row), us=us_snapshot(row),
        trades=recent_trades(int(row['id'])), status=automation_status())


@app.route('/set_principal', methods=['POST'])
@login_required
def set_principal():
    try:
        amt = float(request.form.get('amount', 0))
        if amt > 0:
            set_user_initial_cash(int(current_user.row['id']), amt, is_mock=False)
            flash('원금 확정됨')
    except Exception:
        pass
    return redirect(url_for('dashboard'))


if __name__ == '__main__':
    try:
        init_db()
    except Exception:
        pass
    app.run(host='0.0.0.0', port=5000, debug=False)
