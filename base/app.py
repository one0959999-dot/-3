# -*- coding: utf-8 -*-
"""Lassi 대시보드 v4 — 클린/밝은 토스풍. 조회 전용(주문 안 냄, 구봇 0).

계좌=toss_api 직접조회. 원금=원가기준 자동계산(매입가합+현금)=미실현 수익률.
수동 원금설정 없음. 색: 한국식(빨강=상승/이익, 파랑=하락/손실).
실행: python base/app.py (로컬) / gunicorn base.app:app (서버)
"""
import os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from flask import Flask, request, redirect, url_for, render_template_string
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                         login_required, current_user)
from base.database import get_db_connection, verify_user, init_db
from base.toss_api import TossInvestApi

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def P(f):
    return os.path.join(ROOT, f)

app = Flask(__name__)
app.secret_key = os.environ.get('LASSI_SECRET', 'lassi-dash-v4')
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


def kr_snapshot(row):
    """원가기준 자동계산: 원가원금 = Σ(매입가×수량)+현금 → 미실현 수익률."""
    out = {'holdings': [], 'cash': 0, 'total': 0, 'hold_val': 0,
           'cost_basis': 0, 'ret': None, 'pl': 0, 'error': None}
    try:
        t = _toss(row)
        bal = t.get_account_balance()
        if not bal:
            out['error'] = '계좌조회 실패(토큰/IP/API 확인)'; return out
        hv = 0.0; cost = 0.0
        for s in bal.get('stocks', []):
            q = int(s.get('shares', 0) or 0)
            if q <= 0:
                continue
            px = float(s.get('current_price', 0) or 0) or float(s.get('purchase_price', 0) or 0)
            bp = float(s.get('purchase_price', 0) or 0) or px
            val = q * px; hv += val; cost += q * bp
            out['holdings'].append({'name': s.get('name', s['ticker']), 'ticker': s['ticker'],
                                    'qty': q, 'price': px, 'buy': bp, 'value': val,
                                    'plpct': (px / bp - 1) * 100 if bp else 0,
                                    'is_etf': s['ticker'] == '069500'})
        cash = t.get_buyable_cash(default=None)
        cash = float(cash) if cash is not None else 0.0
        out['cash'] = cash; out['hold_val'] = hv; out['total'] = cash + hv
        out['cost_basis'] = cost + cash
        if out['cost_basis'] > 0:
            out['ret'] = (out['total'] / out['cost_basis'] - 1) * 100
            out['pl'] = out['total'] - out['cost_basis']
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


def recent_trades(uid, n=20):
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
    st = {}
    for label, f in [('deadman 감시', 'heartbeat.txt'), ('신규자금 배분', 'auto_deploy_done.txt'),
                     ('US 통화검증', 'first_fill_verified_us.txt'), ('리밸런스 상태', 'rebalance_state.txt')]:
        try:
            st[label] = open(P(f)).read().strip()[:40] or '—'
        except Exception:
            st[label] = '—'
    try:
        from KR.reference import artifact_tickers
        st['참고서 아티팩트 제외'] = f"{len(artifact_tickers())}종목"
    except Exception:
        st['참고서 아티팩트 제외'] = '—'
    try:
        from KR.live_v1 import is_rebalance_week
        st['리밸런스 주간'] = '예 (교체)' if is_rebalance_week() else '아니오 (유지)'
    except Exception:
        st['리밸런스 주간'] = '—'
    return st


PAGE = """<!doctype html><html lang=ko><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Lassi</title><style>
:root{--bg:#f2f4f8;--card:#fff;--txt:#191f28;--sub:#8b95a1;--line:#eef1f5;
--up:#f04452;--down:#3182f6;--pri:#3182f6;--pri-bg:#e8f2ff;--soft:#f7f9fc}
*{box-sizing:border-box;margin:0;-webkit-tap-highlight-color:transparent}
body{font-family:-apple-system,'Malgun Gothic','Apple SD Gothic Neo',system-ui,sans-serif;
background:var(--bg);color:var(--txt);line-height:1.4;letter-spacing:-.2px}
.wrap{max-width:520px;margin:0 auto;padding:14px 16px 70px}
.top{display:flex;justify-content:space-between;align-items:center;padding:8px 2px 4px}
.logo{font-size:20px;font-weight:800} .logo em{font-style:normal;color:var(--pri)}
.top a{color:var(--sub);text-decoration:none;font-size:14px;font-weight:600}
.note{font-size:12px;color:var(--sub);margin:2px 2px 10px}
.seg{display:flex;background:#e9edf3;border-radius:14px;padding:4px;gap:4px;margin:10px 0 4px}
.seg div{flex:1;text-align:center;padding:11px;border-radius:10px;font-weight:700;font-size:15px;color:var(--sub);cursor:pointer;transition:.15s}
.seg div.on{background:#fff;color:var(--txt);box-shadow:0 2px 6px rgba(0,0,0,.08)}
.pane{display:none} .pane.on{display:block;animation:f .22s} @keyframes f{from{opacity:0;transform:translateY(6px)}to{opacity:1}}
.card{background:var(--card);border-radius:20px;padding:20px;margin:12px 0;box-shadow:0 1px 3px rgba(0,20,60,.06),0 8px 24px rgba(0,20,60,.04)}
.hero .lab{font-size:13px;color:var(--sub);font-weight:600}
.hero .amt{font-size:34px;font-weight:800;margin:2px 0 10px;letter-spacing:-1.2px}
.hero .amt small{font-size:19px;font-weight:700;color:var(--sub)}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:15px;font-weight:800;padding:7px 12px;border-radius:12px}
.up{color:var(--up)} .down{color:var(--down)} .pill.up{background:#fde8ea} .pill.down{background:#e8f0fe}
.row2{display:flex;gap:10px;margin-top:14px} .row2>div{flex:1;background:var(--soft);border-radius:14px;padding:12px 14px}
.row2 .k{font-size:12px;color:var(--sub);font-weight:600} .row2 .v{font-size:16px;font-weight:800;margin-top:3px}
.h{font-size:14px;font-weight:800;color:var(--txt);margin:22px 4px 8px}
table{width:100%;border-collapse:collapse;font-size:14px}
td,th{padding:12px 6px;border-bottom:1px solid var(--line)} tr:last-child td{border:0}
th{color:var(--sub);font-size:12px;font-weight:600;text-align:right} th.l,td.l{text-align:left} td{text-align:right;font-weight:600}
.nm{font-weight:700} .tk{color:var(--sub);font-size:12px;font-weight:500}
.tag{display:inline-block;font-size:11px;font-weight:800;padding:2px 7px;border-radius:8px;margin-right:5px}
.tag.etf{background:var(--pri-bg);color:var(--pri)} .tag.b{background:#fde8ea;color:var(--up)} .tag.s{background:#e8f0fe;color:var(--down)}
.st{display:flex;justify-content:space-between;padding:11px 2px;border-bottom:1px solid var(--line);font-size:14px} .st:last-child{border:0}
.st .kk{color:var(--sub);font-weight:600} .st .vv{font-weight:700}
.warn{background:#fff4e5;color:#c2681a;font-weight:600}
.mut{color:var(--sub)} .cap{font-size:12px;color:var(--sub);margin:8px 4px 0}
.foot{text-align:center;font-size:12px;color:#b0b8c1;margin:26px 0}
</style></head><body><div class=wrap>
<div class=top><div class=logo>Lassi<em>.</em></div><a href="{{url_for('logout')}}">로그아웃</a></div>
<div class=note>{{now}} · 크론 자동매매 · 이 화면은 주문을 내지 않습니다 · <a style=color:var(--pri) href="{{url_for('dashboard')}}">↻ 새로고침</a></div>

<div class=seg><div class="on" onclick="sw('kr')">🇰🇷 국내</div><div onclick="sw('us')">🇺🇸 미국</div></div>

<!-- KR -->
<div id=kr class="pane on">
{% if kr.error %}<div class="card warn">⚠️ {{kr.error}}</div>{% else %}
<div class="card hero">
  <div class=lab>총자산</div>
  <div class=amt>{{ '{:,.0f}'.format(kr.total) }}<small> 원</small></div>
  <span class="pill {{'up' if (kr.ret or 0)>=0 else 'down'}}">{{ '▲' if (kr.ret or 0)>=0 else '▼' }} {{ '%.2f'|format(kr.ret|abs) if kr.ret is not none else '—' }}% · {{ '{:+,.0f}'.format(kr.pl) }}원</span>
  <div class=row2>
    <div><div class=k>매입원가</div><div class=v>{{ '{:,.0f}'.format(kr.cost_basis) }}</div></div>
    <div><div class=k>현금(미투입)</div><div class=v>{{ '{:,.0f}'.format(kr.cash) }}</div></div>
  </div>
</div>
<div class=h>보유 종목 <span class=mut style=font-weight:500>{{kr.holdings|length}}개</span></div>
<div class=card style=padding:8px><table><tr><th class=l>종목</th><th>수익률</th><th>평가액</th><th>비중</th></tr>
{% for h in kr.holdings %}<tr><td class=l><div>{% if h.is_etf %}<span class="tag etf">지수</span>{% endif %}<span class=nm>{{h.name}}</span></div>
<div class=tk>{{h.qty}}주 · 매입 {{ '{:,.0f}'.format(h.buy) }}</div></td>
<td class="{{'up' if h.plpct>=0 else 'down'}}">{{ '%+.1f'|format(h.plpct) }}%</td>
<td>{{ '{:,.0f}'.format(h.value) }}</td><td class=mut>{{ '%.0f'|format(h.value/kr.total*100 if kr.total else 0) }}%</td></tr>{% endfor %}
{% if not kr.holdings %}<tr><td colspan=4 class="l mut" style=padding:16px>보유 종목 없음</td></tr>{% endif %}
</table></div>
<div class=cap>* 수익률 = 매입원가 대비 미실현 손익 (매입가 자동계산)</div>
{% endif %}
</div>

<!-- US -->
<div id=us class=pane>
{% if us.error %}<div class="card warn">⚠️ {{us.error}}</div>{% else %}
<div class="card hero"><div class=lab>USD 예수금</div><div class=amt>${{ '%.2f'|format(us.cash_usd) }}</div>
<div class=cap style=margin-top:4px>전략 = SPY(S&P500) 보유. 환전하면 크론이 통화검증 후 자동매수.</div></div>
{% if us.holdings %}<div class=card style=padding:8px><table><tr><th class=l>종목</th><th>수량</th></tr>
{% for h in us.holdings %}<tr><td class="l nm">{{h.ticker}}</td><td>{{ '%.4f'|format(h.qty) }}주</td></tr>{% endfor %}</table></div>
{% else %}<div class="card mut" style=text-align:center;padding:24px>SPY 미보유 — USD 환전 시 자동매수 대기</div>{% endif %}{% endif %}
</div>

<!-- 공통 -->
<div class=h>⚙️ 자동화 상태</div>
<div class=card>{% for k,v in status.items() %}<div class=st><span class=kk>{{k}}</span><span class=vv>{{v}}</span></div>{% endfor %}
<div class=cap>크론: 리밸 평일 10:00 · 신규배분 평일 10:30 · US 미국장 · deadman 매일</div></div>

<div class=h>📜 최근 거래</div>
<div class=card style=padding:8px><table><tr><th class=l>종목</th><th>가격</th><th>수량</th><th class=l>일시</th></tr>
{% for t in trades %}<tr><td class=l><span class="tag {{'s' if t.action=='SELL' else 'b'}}">{{ '매도' if t.action=='SELL' else '매수' }}</span><span class=nm>{{t.stock_name}}</span> <span class=tk>{{t.mode}}</span></td>
<td>{{ '{:,.0f}'.format(t.price) }}</td><td>{{ '{:.0f}'.format(t.shares) }}</td><td class="l tk">{{t.created_at[5:16]}}</td></tr>{% endfor %}
{% if not trades %}<tr><td colspan=4 class="l mut" style=padding:16px>거래 없음</td></tr>{% endif %}</table></div>

<div class=foot>Lassi · 조회 전용 · 매매는 검증된 크론이 담당</div>
</div>
<script>function sw(x){document.querySelectorAll('.seg div').forEach(t=>t.classList.remove('on'));
document.querySelectorAll('.pane').forEach(p=>p.classList.remove('on'));
document.getElementById(x).classList.add('on');event.currentTarget.classList.add('on');}</script>
</body></html>"""

LOGIN = """<!doctype html><html lang=ko><head><meta charset=utf-8><title>Lassi 로그인</title><style>
body{font-family:-apple-system,'Malgun Gothic',system-ui,sans-serif;background:#f2f4f8;color:#191f28;
display:flex;height:100vh;align-items:center;justify-content:center;margin:0}
form{background:#fff;padding:34px 28px;border-radius:22px;width:310px;box-shadow:0 10px 40px rgba(0,20,60,.1)}
h2{margin:0 0 4px;font-size:24px} .s{color:#8b95a1;font-size:13px;margin-bottom:18px}
input{width:100%;padding:14px;margin:7px 0;background:#f7f9fc;border:1px solid #eef1f5;color:#191f28;border-radius:12px;font-size:15px}
input:focus{outline:none;border-color:#3182f6;background:#fff}
button{width:100%;padding:14px;background:#3182f6;color:#fff;border:0;border-radius:12px;margin-top:12px;cursor:pointer;font-weight:800;font-size:16px}
.e{color:#f04452;font-size:13px;margin-bottom:6px;font-weight:600}</style></head><body>
<form method=post><h2>Lassi<span style=color:#3182f6>.</span></h2><div class=s>교과서 v3 + 참고서 · 자동매매</div>
{% if error %}<div class=e>{{error}}</div>{% endif %}
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


if __name__ == '__main__':
    try:
        init_db()
    except Exception:
        pass
    app.run(host='0.0.0.0', port=5000, debug=False)
