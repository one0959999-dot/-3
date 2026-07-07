# -*- coding: utf-8 -*-
"""시나브로 대시보드 — 반응형(데스크톱 2열/모바일 1열) + 종목상세 + 봇상태캡슐 + AI챗.

조회 전용(구봇 0). 계좌=toss_api. 원금=원가기준 자동. 자동로그인. 봇상태=crontab.
종목 클릭→매수이유+참고서. AI챗=Gemini. 색: 한국식(빨강=상승/파랑=하락).
"""
import os, sys, csv, datetime, subprocess
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from flask import Flask, request, redirect, url_for, render_template_string, jsonify
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                         login_required, current_user)
from base.database import get_db_connection, verify_user, init_db
from base.toss_api import TossInvestApi

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def P(f):
    return os.path.join(ROOT, f)

app = Flask(__name__)
app.secret_key = os.environ.get('LASSI_SECRET', 'sinabro-dash')
login_manager = LoginManager(app)
login_manager.login_view = 'login'
AUTO_LOGIN = True
AUTO_LOGIN_UID = '1'
_MASTER = None


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


@app.before_request
def _auto_login():
    if not AUTO_LOGIN or request.endpoint in ('login', 'logout', 'static'):
        return
    if not current_user.is_authenticated:
        row = _user_row(AUTO_LOGIN_UID)
        if row:
            login_user(User(row), remember=True)


def _toss(row):
    return TossInvestApi(row['toss_client_id'], row['toss_client_secret'], row['toss_account_seq'] or '')


def _master():
    global _MASTER
    if _MASTER is None:
        _MASTER = {}
        try:
            with open(P('reference_data/stock_master.csv'), encoding='utf-8-sig') as f:
                for r in csv.DictReader(f):
                    _MASTER[str(r.get('ticker', '')).zfill(6)] = r
        except Exception:
            pass
    return _MASTER


def _equity_snapshot(total):
    """총자산 일별 스냅샷(equity_history.json, 하루 1키·방문시 갱신) → (최근 90일 값들, 어제대비)."""
    import json as _j
    today = datetime.date.today().isoformat()
    try:
        with open(P('equity_history.json'), encoding='utf-8') as f:
            h = _j.load(f)
    except Exception:
        h = {}
    days = sorted(h)
    prev = None
    if days:
        if days[-1] < today:
            prev = h[days[-1]]
        elif len(days) > 1:
            prev = h[days[-2]]
    if total > 0 and h.get(today) != total:
        h[today] = total
        h = {k: h[k] for k in sorted(h)[-180:]}
        try:
            tmp = P('equity_history.json') + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                _j.dump(h, f)
            os.replace(tmp, P('equity_history.json'))
        except Exception:
            pass
    pts = [h[k] for k in sorted(h)][-90:]
    return pts, ((total - prev) if prev else None), prev


def _spark(pts, w=300, hgt=40):
    """스파크라인 SVG polyline 좌표 (2점 이상일 때만)."""
    if len(pts) < 2:
        return ''
    lo, hi = min(pts), max(pts)
    rng = (hi - lo) or 1
    step = w / (len(pts) - 1)
    return ' '.join(f"{i*step:.1f},{hgt - 5 - (p - lo) / rng * (hgt - 10):.1f}" for i, p in enumerate(pts))


def dca_status():
    """지수 DCA 원장(dca_index_plan.json) 읽어 진행상황. 진행중 아니면 None."""
    try:
        import json as _json, math as _math
        with open(P('dca_index_plan.json'), encoding='utf-8') as f:
            d = _json.load(f)
        reserved = int(d.get('reserved', 0)); tranche = int(d.get('tranche', 0))
        if reserved <= 0:
            return None
        months = _math.ceil(reserved / tranche) if tranche > 0 else 1
        return {'reserved': reserved, 'months': months, 'tranche': tranche}
    except Exception:
        return None


def kr_snapshot(row):
    out = {'holdings': [], 'cash': 0, 'total': 0, 'hold_val': 0, 'cost_basis': 0,
           'ret': None, 'pl': 0, 'error': None, 'alloc': [], 'conic': ''}
    try:
        t = _toss(row)
        bal = t.get_account_balance()
        if not bal:
            out['error'] = '계좌조회 실패(토큰/IP/API 확인)'; return out
        hv = 0.0; cost = 0.0; etf_v = 0.0; stk_v = 0.0
        for s in bal.get('stocks', []):
            q = int(s.get('shares', 0) or 0)
            if q <= 0:
                continue
            px = float(s.get('current_price', 0) or 0) or float(s.get('purchase_price', 0) or 0)
            bp = float(s.get('purchase_price', 0) or 0) or px
            val = q * px; hv += val; cost += q * bp
            is_etf = s['ticker'] == '069500'
            etf_v += val if is_etf else 0; stk_v += 0 if is_etf else val
            tk = str(s['ticker'])
            out['holdings'].append({'name': s.get('name', tk), 'ticker': tk, 'qty': q, 'price': px,
                                    'buy': bp, 'value': val, 'plpct': (px / bp - 1) * 100 if bp else 0,
                                    'plw': (px - bp) * q,
                                    'is_etf': is_etf, 'hue': (int(tk) % 360) if tk.isdigit() else 210})
        cash = t.get_buyable_cash(default=None)
        cash = float(cash) if cash is not None else 0.0
        out['cash'] = cash; out['hold_val'] = hv; out['total'] = cash + hv
        out['cost_basis'] = cost + cash
        if out['cost_basis'] > 0:
            out['ret'] = (out['total'] / out['cost_basis'] - 1) * 100
            out['pl'] = out['total'] - out['cost_basis']
        out['holdings'].sort(key=lambda x: (-x['is_etf'], -x['value']))
        tot = out['total'] or 1
        cum = 0.0; stops = []
        for label, v, col in [('지수 ETF', etf_v, '#149a6e'), ('저변동 25종목', stk_v, '#8fd6b4'), ('현금', cash, '#c4cdd8')]:
            f = v / tot * 100
            out['alloc'].append({'label': label, 'val': v, 'pct': f, 'color': col})
            stops.append(f"{col} {cum:.2f}% {cum + f:.2f}%")
            cum += f
        out['conic'] = ', '.join(stops)
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


def bot_status():
    st = {'kr': None, 'us': None, 'deadman': None, 'heartbeat': '—', 'rebal': '—', 'artifact': '—'}
    try:
        # gunicorn 서비스는 PATH=venv/bin 뿐이라 'crontab'을 못 찾음 → 절대경로 폴백 필수
        cron = ''
        for cmd in (['crontab', '-l'], ['/usr/bin/crontab', '-l'], ['/bin/crontab', '-l']):
            try:
                cron = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
                if cron:
                    break
            except FileNotFoundError:
                continue
        st['kr'] = ('auto_deploy.py --execute' in cron) or ('auto_order.py --rebalance --execute' in cron)
        st['us'] = 'auto_order_us.py --execute' in cron
        st['deadman'] = 'deadman.py' in cron
    except Exception:
        pass
    for k, f in [('heartbeat', 'heartbeat.txt'), ('rebal', 'rebalance_state.txt')]:
        try:
            st[k] = open(P(f)).read().strip()[:24] or '—'
        except Exception:
            pass
    try:
        from KR.reference import artifact_tickers
        st['artifact'] = f"{len(artifact_tickers())}종목 제외"
    except Exception:
        pass
    return st


def bot_details(bot, us, dca):
    """봇 상태 박스 클릭시 보여줄 친절한 설명 (상태별 '왜 이런지' + '뭘 하면 되는지')."""
    d = {}
    now = datetime.datetime.now()  # EC2=KST
    kr_open = now.weekday() < 5 and (9, 0) <= (now.hour, now.minute) < (15, 30)
    kr_mkt = f"<br>· 지금 국내 장: <b>{'열림' if kr_open else '마감'}</b> — 매매는 장중에만 실행돼요"
    # ── 국내 ──
    if bot.get('kr'):
        dca_line = ''
        if dca:
            dca_line = (f"<br>· 지금은 <b>지수 나눠사기(DCA) 진행중</b> — 예약 {dca['reserved']/1e4:,.0f}만원을 "
                        f"매달 1회씩 약 {dca['months']}개월에 걸쳐 지수를 삽니다 (고점에 몰빵하지 않으려는 의도)")
        d['kr'] = {'title': '국내 자동매매', 'sub': '🟢 정상 가동중',
                   'html': ("<div class=rsn><b>서버가 정해진 시간에 알아서 매매해요</b><br>"
                            "· 평일 <b>10:00</b> — 분기 리밸런스 확인 (1·4·7·10월 첫 주에만 실제 종목 교체)<br>"
                            "· 평일 <b>10:30</b> — 계좌에 새 현금이 생기면 자동으로 나눠서 매수"
                            + dca_line + kr_mkt +
                            "</div><div class=rsn><b>따로 하실 일은 없어요</b><br>"
                            "매매가 일어나면 그때마다 텔레그램으로 알려드립니다. "
                            "이 화면은 언제든 들어와서 구경만 하셔도 됩니다.</div>")}
    else:
        d['kr'] = {'title': '국내 자동매매', 'sub': '⏸️ 정지됨',
                   'html': ("<div class=rsn><b>지금은 봇이 완전히 쉬는 상태예요</b><br>"
                            "서버에 국내 매매 예약이 걸려 있지 않아요. 아무것도 사거나 팔지 않고, "
                            "갖고 있는 주식은 그대로 있습니다.</div>"
                            "<div class=rsn><b>다시 켜려면</b><br>"
                            "이 화면에서는 켤 수 없고, 개발 채팅(클로드)에서 \"국내 봇 켜줘\"라고 하면 됩니다.</div>")}
    # ── 미국 ──
    us_cash = (us or {}).get('cash_usd') or 0
    us_hold = bool((us or {}).get('holdings'))
    if bot.get('us') and bot.get('us_wait'):
        d['us'] = {'title': '미국 자동매매', 'sub': '🟡 환전 대기중 (봇은 켜져 있어요)',
                   'html': ("<div class=rsn><b>왜 아무것도 안 사나요?</b><br>"
                            "봇은 켜져 있는데, 계좌에 <b>달러(USD)가 $0</b>이라 살 돈이 없어요. "
                            "토스는 원화를 자동으로 환전해 주지 않아서, 원화가 있어도 미국 주식은 못 삽니다.</div>"
                            "<div class=rsn><b>뭘 하면 되나요?</b><br>"
                            "토스증권 앱에서 <b>원화 → 달러 환전</b>을 한 번만 해두세요. "
                            "그러면 다음 미국장 아침(한국시간 새벽)에 봇이 자동으로 SPY(미국 S&P500 ETF)를 삽니다. "
                            "안 쓰실 거면 그대로 두셔도 아무 문제 없어요.</div>")}
    elif bot.get('us'):
        body = (f"달러 <b>${us_cash:,.2f}</b>가 확인됐어요. 다음 미국장 아침(한국시간 새벽)에 자동으로 SPY를 삽니다."
                if us_cash >= 1 else "SPY를 보유중이에요. 새 달러가 들어오면 자동으로 추가 매수합니다.")
        d['us'] = {'title': '미국 자동매매', 'sub': '🟢 정상 가동중',
                   'html': (f"<div class=rsn><b>지금 상태</b><br>{body}</div>"
                            "<div class=rsn><b>전략</b><br>미국은 단순해요 — 달러가 생기면 SPY 하나만 삽니다. "
                            "매매 결과는 텔레그램으로 알려드립니다.</div>")}
    else:
        d['us'] = {'title': '미국 자동매매', 'sub': '⏸️ 정지됨',
                   'html': ("<div class=rsn><b>지금은 봇이 완전히 쉬는 상태예요</b><br>"
                            "서버에 미국 매매 예약이 걸려 있지 않아요. 달러가 있어도 사지 않습니다.</div>"
                            "<div class=rsn><b>다시 켜려면</b><br>"
                            "이 화면에서는 켤 수 없고, 개발 채팅(클로드)에서 \"미국 봇 켜줘\"라고 하면 됩니다.</div>")}
    # ── 감시장치 ──
    hb = bot.get('heartbeat', '—')
    if bot.get('deadman'):
        d['dm'] = {'title': '감시장치', 'sub': '🟢 켜져 있음',
                   'html': ("<div class=rsn><b>뭘 하는 건가요?</b><br>"
                            "매일 자정에 '봇이 살아있나'를 스스로 점검하는 안전장치예요. "
                            "봇이 멈추거나 며칠째 아무 기록이 없으면 텔레그램으로 바로 알려줍니다.<br><br>"
                            f"마지막 생존신호: <b>{hb}</b></div>"
                            "<div class=rsn><b>여행 가도 되나요?</b><br>"
                            "네. 문제가 생기면 이 장치가 알려주니, 알림이 없다는 건 잘 돌고 있다는 뜻이에요.</div>")}
    else:
        d['dm'] = {'title': '감시장치', 'sub': '⏸️ 꺼져 있음',
                   'html': ("<div class=rsn><b>봇이 멈춰도 알려줄 장치가 꺼져 있어요</b><br>"
                            "매매 봇과는 별개라 매매는 계속되지만, 봇에 문제가 생겨도 알림이 안 옵니다. "
                            "켜두는 걸 추천해요 — 개발 채팅(클로드)에서 \"감시 켜줘\"라고 하면 됩니다.</div>")}
    return d


def recent_trades(uid, n=30):
    c = get_db_connection()
    try:
        rows = c.execute("SELECT stock_name, action, price, shares, created_at, mode "
                         "FROM trade_journal WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, n)).fetchall()
        out = []
        today = datetime.date.today()
        for r in rows:
            t = dict(r)
            t['amt'] = (t.get('price') or 0) * (t.get('shares') or 0)
            ds = (t.get('created_at') or '')[:10]
            try:
                d = datetime.date.fromisoformat(ds)
                t['day'] = '오늘' if d == today else ('어제' if (today - d).days == 1 else f"{d.month}/{d.day}")
            except Exception:
                t['day'] = ds[5:]
            out.append(t)
        return out
    except Exception:
        return []
    finally:
        c.close()


def _gemini(key, prompt):
    import requests
    last = 'unknown'
    for model in ('gemini-2.5-flash', 'gemini-2.0-flash-001'):
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=20)
            j = r.json()
            if 'candidates' in j:
                return j['candidates'][0]['content']['parts'][0]['text']
            last = j.get('error', {}).get('message', str(j))
        except Exception as e:
            last = str(e)
    return f"(AI 응답 실패: {last[:120]})"


PAGE = """<!doctype html><html lang=ko><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=theme-color content="#e9f1ec" media="(prefers-color-scheme: light)">
<meta name=theme-color content="#101418" media="(prefers-color-scheme: dark)">
<meta name=apple-mobile-web-app-capable content=yes>
<meta name=apple-mobile-web-app-status-bar-style content=black-translucent>
<meta name=apple-mobile-web-app-title content=시나브로>
<link rel=manifest href=/manifest.json><link rel=apple-touch-icon href=/icon.png>
<link rel=icon type=image/png href=/icon.png>
<title>시나브로</title><style>
:root{--bg:#f2f5f4;--card:#fff;--line:#eef2f0;--txt:#191f28;--sub:#8b95a1;--faint:#b6bdc7;--up:#f04452;--down:#3182f6;--pri:#149a6e;--soft:#f4f8f6;--grn:#12b886;
--sh:0 1px 2px rgba(23,32,64,.04),0 10px 30px rgba(23,32,64,.06);--sh2:0 2px 6px rgba(23,32,64,.06),0 16px 40px rgba(23,32,64,.09)}
*{box-sizing:border-box;margin:0;-webkit-tap-highlight-color:transparent}
html{overscroll-behavior-y:none}
body.mlock{overflow:hidden}
body{font-family:Pretendard,-apple-system,'Malgun Gothic','Apple SD Gothic Neo',system-ui,sans-serif;
background:linear-gradient(180deg,#e9f1ec 0,var(--bg) 260px);color:var(--txt);letter-spacing:-.3px;font-size:14px;min-height:100vh}
body:before{content:'';position:fixed;inset:0;z-index:-1;pointer-events:none;background:
radial-gradient(640px 420px at 88% -8%,rgba(20,154,110,.11),transparent),
radial-gradient(520px 380px at -12% 6%,rgba(18,184,134,.07),transparent)}
body:after{content:'';position:fixed;top:0;left:0;right:0;height:env(safe-area-inset-top);background:#173a2c;z-index:50;pointer-events:none}
.wrap{max-width:1060px;margin:0 auto;padding:0 20px 64px}
.num,.amt,.hval,.hpl,.vv,.lp,.pill{font-variant-numeric:tabular-nums}
/* 스티키 헤더 */
.top{position:sticky;top:0;z-index:15;display:flex;justify-content:space-between;align-items:center;
margin:0 -20px 2px;padding:calc(13px + env(safe-area-inset-top)) 22px 11px;background:rgba(236,243,239,.78);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px)}
.logo{font-size:20px;font-weight:800;letter-spacing:-.6px} .logo em{font-style:normal;color:var(--pri)}
.top a{color:var(--sub);text-decoration:none;font-size:12.5px;font-weight:600;padding:6px 11px;border-radius:9px;transition:.15s}
.top a:hover{background:rgba(255,255,255,.85);color:var(--txt)}
.note{font-size:11.5px;color:#6b7684;margin:6px 2px 12px} .note a{color:var(--pri);text-decoration:none;font-weight:600}
/* 봇 상태 캡슐 (한 줄, 상태별 색 배경으로 한눈에) */
.sbar{display:flex;gap:6px;background:var(--card);border:1px solid rgba(15,30,70,.045);border-radius:99px;
padding:6px;box-shadow:var(--sh);margin-bottom:14px}
.sseg{flex:1;display:flex;align-items:center;justify-content:center;gap:6px;font-size:12.5px;font-weight:600;
cursor:pointer;padding:9px 0;border-radius:99px;transition:filter .15s}
.sseg:hover{filter:brightness(.97)} .sseg:active{transform:scale(.97)}
.sseg b{font-weight:800;font-size:12.5px}
.sseg.s-on{background:#e0f4ea;color:#22795c} .sseg.s-on b{color:#0f8a60}
.sseg.s-wait{background:#fff1dc;color:#a86a10} .sseg.s-wait b{color:#e08600}
.sseg.s-off{background:#f0f2f4;color:#8b95a1} .sseg.s-off b{color:#6b7684}
.led{width:9px;height:9px;border-radius:50%;flex-shrink:0} .led.off{background:#cbd3dd}
.led.on{background:var(--grn);animation:pulse 2.4s ease-out infinite}
.led.wait{background:#ff9500;animation:pulsew 2.4s ease-out infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(18,184,134,.35)}70%{box-shadow:0 0 0 7px rgba(18,184,134,0)}100%{box-shadow:0 0 0 0 rgba(18,184,134,0)}}
@keyframes pulsew{0%{box-shadow:0 0 0 0 rgba(255,149,0,.35)}70%{box-shadow:0 0 0 7px rgba(255,149,0,0)}100%{box-shadow:0 0 0 0 rgba(255,149,0,0)}}
/* 탭 */
.seg{display:flex;background:rgba(222,229,238,.75);border-radius:14px;padding:4px;gap:4px;margin:0 0 14px;max-width:340px}
.seg div{flex:1;text-align:center;padding:9px 0;border-radius:11px;font-weight:700;font-size:14px;color:var(--sub);cursor:pointer;transition:.18s}
.seg div.on{background:#fff;color:var(--txt);box-shadow:0 2px 8px rgba(23,32,64,.1)}
/* 반응형 2열 */
.grid{display:grid;grid-template-columns:1fr;gap:16px} @media(min-width:840px){.grid{grid-template-columns:1.25fr 1fr;align-items:start}}
.pane{display:none} .pane.on{display:block;animation:f .2s ease} @keyframes f{from{opacity:0}to{opacity:1}}
/* 카드 스태거는 첫 로드에만(body.boot) — 탭 전환시 재실행되면 뚝뚝 끊겨 보임 */
.boot .grid .card{animation:cardin .45s ease backwards}
.boot .grid .card:nth-child(2){animation-delay:.06s} .boot .grid .card:nth-child(3){animation-delay:.12s} .boot .grid .card:nth-child(4){animation-delay:.18s}
@keyframes cardin{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
.card{background:var(--card);border:1px solid rgba(15,30,70,.045);border-radius:20px;padding:20px;margin-bottom:14px;box-shadow:var(--sh)}
.h{font-size:14px;font-weight:800;margin:0 4px 8px}
/* hero — 딥그린 프리미엄 카드 */
.hero{background:linear-gradient(155deg,#17372c 0,#1e4f3d 48%,#153228 100%);text-align:center;padding:32px 20px 28px;
position:relative;overflow:hidden;border:0;box-shadow:0 2px 6px rgba(16,50,38,.18),0 18px 44px rgba(16,50,38,.22),inset 0 1px 0 rgba(255,255,255,.07)}
.hero:before{content:'';position:absolute;width:260px;height:260px;border-radius:50%;top:-120px;right:-80px;
background:radial-gradient(closest-side,rgba(94,214,167,.22),transparent)}
.hero:after{content:'';position:absolute;width:200px;height:200px;border-radius:50%;bottom:-110px;left:-70px;
background:radial-gradient(closest-side,rgba(46,160,120,.20),transparent)}
.hero>*{position:relative}
/* 펄 도장 — 은은한 광택 + 불규칙 플레이크(큰 타일 2장, 주기 어긋나 반복 안 보임) */
.hero .pearl{position:absolute;inset:0;pointer-events:none;
background:linear-gradient(115deg,transparent 32%,rgba(255,255,255,.035) 44%,rgba(150,255,215,.075) 50%,rgba(255,255,255,.03) 56%,transparent 68%);
background-size:240% 240%;animation:sheen 11s ease-in-out infinite alternate}
@keyframes sheen{from{background-position:0% 40%}to{background-position:100% 60%}}
.hero .pearl:before{content:'';position:absolute;inset:0;
animation:tw1 6.5s ease-in-out infinite;
background-image:
radial-gradient(circle at 13px 27px,rgba(255,255,255,.85) 0,transparent 1.1px),
radial-gradient(circle at 87px 9px,rgba(195,255,228,.7) 0,transparent .8px),
radial-gradient(circle at 41px 73px,rgba(255,255,255,.6) 0,transparent .7px),
radial-gradient(circle at 121px 54px,rgba(255,242,205,.55) 0,transparent .9px),
radial-gradient(circle at 66px 118px,rgba(255,255,255,.75) 0,transparent .8px),
radial-gradient(circle at 139px 131px,rgba(190,255,225,.6) 0,transparent 1px),
radial-gradient(circle at 24px 102px,rgba(255,255,255,.5) 0,transparent .6px),
radial-gradient(circle at 98px 88px,rgba(255,246,214,.45) 0,transparent .7px),
radial-gradient(circle at 7px 58px,rgba(255,255,255,.7) 0,transparent .8px),
radial-gradient(circle at 52px 140px,rgba(200,255,232,.6) 0,transparent .9px),
radial-gradient(circle at 112px 12px,rgba(255,255,255,.55) 0,transparent .7px),
radial-gradient(circle at 133px 93px,rgba(255,244,208,.5) 0,transparent .8px),
radial-gradient(circle at 75px 44px,rgba(255,255,255,.65) 0,transparent .6px);
background-size:149px 149px}
.hero .pearl:after{content:'';position:absolute;inset:0;
animation:tw2 8.5s ease-in-out infinite;
background-image:
radial-gradient(circle at 33px 151px,rgba(255,255,255,.8) 0,transparent .9px),
radial-gradient(circle at 172px 44px,rgba(195,255,230,.65) 0,transparent 1.1px),
radial-gradient(circle at 109px 187px,rgba(255,255,255,.55) 0,transparent .7px),
radial-gradient(circle at 58px 12px,rgba(255,255,255,.65) 0,transparent .8px),
radial-gradient(circle at 190px 166px,rgba(255,240,200,.5) 0,transparent .9px),
radial-gradient(circle at 146px 99px,rgba(255,255,255,.7) 0,transparent .6px),
radial-gradient(circle at 15px 88px,rgba(255,255,255,.6) 0,transparent .7px),
radial-gradient(circle at 84px 60px,rgba(198,255,230,.55) 0,transparent .8px),
radial-gradient(circle at 201px 120px,rgba(255,255,255,.75) 0,transparent 1px),
radial-gradient(circle at 122px 143px,rgba(255,246,212,.5) 0,transparent .7px),
radial-gradient(circle at 47px 199px,rgba(255,255,255,.6) 0,transparent .8px),
radial-gradient(circle at 160px 203px,rgba(195,255,228,.6) 0,transparent .9px),
radial-gradient(circle at 94px 26px,rgba(255,255,255,.55) 0,transparent .6px);
background-size:211px 211px}
@keyframes tw1{0%,100%{opacity:.15}50%{opacity:.40}}
@keyframes tw2{0%,100%{opacity:.38}45%{opacity:.14}}
.hero .pearl.tilt{animation:none;transition:background-position .2s ease-out}
.hero .lab{font-size:12.5px;color:rgba(255,255,255,.68);font-weight:700}
.hero .amt{font-size:40px;font-weight:800;margin:5px 0 4px;letter-spacing:-2px;color:#fff}
.hero .amt small{font-size:19px;color:rgba(255,255,255,.62);font-weight:700;letter-spacing:-.5px}
.pill{display:inline-flex;align-items:center;gap:5px;font-size:13px;font-weight:800;padding:7px 14px;border-radius:99px}
.up{color:var(--up)} .down{color:var(--down)} .pill.up{background:#fdeaec} .pill.down{background:#e9f1fe}
.hero .pill.up{background:rgba(240,68,82,.28);color:#ffaab1} .hero .pill.down{background:rgba(120,170,255,.2);color:#a9c9ff}
.dchg{font-size:14.5px;font-weight:800;margin:3px 0 11px} .hero .dchg.up{color:#ffb3ba} .hero .dchg.down{color:#a9c9ff}
.spk{width:100%;height:40px;margin-top:13px;display:block;opacity:.92}
.spk polyline{fill:none;stroke:rgba(255,255,255,.8);stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.trow{display:flex;align-items:center;gap:8px;padding:9.5px 2px;border-bottom:1px solid var(--line);font-size:13.5px} .trow:last-child{border:0}
.tnm{flex:1;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tend{text-align:right;flex-shrink:0} .tend b{font-size:13.5px}
.tend small{display:block;color:var(--sub);font-size:11px;margin-top:1px;font-weight:500}
#ptr{position:fixed;top:calc(12px + env(safe-area-inset-top));left:50%;transform:translateX(-50%);width:34px;height:34px;border-radius:50%;
background:var(--pri);color:#fff;display:flex;align-items:center;justify-content:center;font-size:17px;
opacity:0;transition:opacity .15s;z-index:40;box-shadow:0 4px 14px rgba(20,154,110,.4);pointer-events:none}
#ptr.spin{animation:pspin .6s linear infinite}
@keyframes pspin{to{transform:translateX(-50%) rotate(360deg)}}
/* 참고서 막대(종목상세) — 다크모드 자동대응 */
.bar{display:flex;align-items:center;gap:8px;margin:6px 0}
.bar .bl{width:56px;font-size:12px;color:var(--sub)}
.bar .trk{flex:1;height:7px;background:var(--line);border-radius:4px;overflow:hidden}
.bar .fill{display:block;height:100%;border-radius:4px}
.bar .bv{width:58px;text-align:right;font-size:12px;font-weight:700}
/* 도넛 */
.donut{display:flex;align-items:center;gap:18px}
.dc{position:relative;width:120px;height:120px;flex-shrink:0} .pie{width:100%;height:100%;border-radius:50%;box-shadow:inset 0 0 0 1px rgba(15,30,70,.04);animation:pin .55s ease}
@keyframes pin{from{transform:scale(.85) rotate(-14deg);opacity:0}to{transform:none;opacity:1}}
.hole{position:absolute;inset:21px;background:#fff;border-radius:50%;display:flex;flex-direction:column;align-items:center;justify-content:center;box-shadow:0 0 10px rgba(23,32,64,.06)}
.hole .t1{font-size:10px;color:var(--sub);font-weight:700} .hole .t2{font-size:18px;font-weight:800}
.leg{flex:1} .legrow{display:flex;align-items:center;gap:9px;padding:6.5px 0}
.dot{width:10px;height:10px;border-radius:3.5px;flex-shrink:0} .legrow .ln{flex:1;font-size:13.5px;font-weight:700}
.legrow .lv{font-size:11.5px;color:var(--sub);font-weight:500;margin-top:1px} .legrow .lp{font-weight:800;font-size:14.5px}
/* 보유종목 */
.hold{display:flex;align-items:center;gap:12px;padding:12px 8px;border-bottom:1px solid var(--line);cursor:pointer;border-radius:12px;transition:background .15s,transform .12s}
.hold:last-child{border:0} .hold:hover{background:var(--soft)} .hold:active{transform:scale(.988)}
.hold:hover .chev{transform:translateX(2px);color:var(--pri)}
.hicon{width:40px;height:40px;border-radius:13px;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:13px;color:#fff;flex-shrink:0;box-shadow:0 3px 8px rgba(23,32,64,.10)}
.hicon.etf{background:#e4f4ec !important;color:var(--pri);box-shadow:none}
.hmid{flex:1;min-width:0} .hnm{font-weight:700;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.prices{display:flex;gap:12px;margin-top:3px;font-size:12px}
.prices .pb{color:var(--sub)} .prices .pc{font-weight:700} .prices b{font-weight:800}
.chev{color:#c4cdd8;font-size:16px;margin-left:2px;transition:.15s}
.hend{text-align:right;flex-shrink:0} .hval{font-weight:800;font-size:15px} .hpl{font-size:12.5px;font-weight:800;margin-top:2px}
/* 리스트/기타 */
.st{display:flex;justify-content:space-between;padding:9.5px 2px;border-bottom:1px solid var(--line);font-size:13.5px} .st:last-child{border:0}
.st .kk{color:var(--sub);font-weight:600} .st .vv{font-weight:700}
.warn{background:#fff4e5;color:#c2681a;font-weight:600} .mut{color:var(--sub)}
.cap{font-size:11.5px;color:var(--sub);margin:9px 3px 0;line-height:1.55}
.tag{display:inline-block;font-size:11px;font-weight:800;padding:2.5px 7px;border-radius:7px;margin-right:6px}
.tag.b{background:#fdeaec;color:var(--up)} .tag.s{background:#e9f1fe;color:var(--down)}
details{margin-top:6px} summary{cursor:pointer;font-size:13px;color:var(--sub);font-weight:700;padding:5px 2px;list-style:none;display:flex;align-items:center;gap:6px}
summary::-webkit-details-marker{display:none}
summary:before{content:'▸';display:inline-block;transition:transform .18s;font-size:11px;color:var(--faint)}
details[open] summary:before{transform:rotate(90deg)}
/* 채팅 */
.chat .msgs{min-height:230px;max-height:440px;overflow-y:auto;display:flex;flex-direction:column;gap:9px;padding:2px;scrollbar-width:thin;overscroll-behavior:contain}
.msgs::-webkit-scrollbar{width:5px} .msgs::-webkit-scrollbar-thumb{background:#dfe5ec;border-radius:3px}
.m{max-width:85%;padding:10px 13px;border-radius:16px;font-size:13.5px;line-height:1.55;white-space:pre-wrap;word-break:break-word}
.m.u{align-self:flex-end;background:linear-gradient(135deg,#1fb583,#149a6e);color:#fff;border-bottom-right-radius:5px;box-shadow:0 3px 10px rgba(20,154,110,.25)}
.m.a{align-self:flex-start;background:var(--soft);border:1px solid var(--line);border-bottom-left-radius:5px}
.cin{display:flex;gap:8px;margin-top:12px}
.cin input{flex:1;padding:12px 14px;border:1.5px solid var(--line);border-radius:13px;font-size:14px;background:var(--soft);transition:.15s}
.cin input:focus{outline:none;border-color:var(--pri);background:#fff;box-shadow:0 0 0 3px rgba(20,154,110,.13)}
.cin button{padding:12px 17px;background:var(--pri);color:#fff;border:0;border-radius:13px;font-weight:800;cursor:pointer;transition:.15s}
.cin button:hover{background:#0f8159} .cin button:active{transform:scale(.96)}
.cin button:disabled,.cin input:disabled{opacity:.55;cursor:default}
.m.a.typing{display:flex;gap:4px;align-items:center;padding:14px 16px}
.td{width:7px;height:7px;border-radius:50%;background:#b3bcc6;animation:td 1.1s infinite}
.td:nth-child(2){animation-delay:.15s} .td:nth-child(3){animation-delay:.3s}
@keyframes td{0%,60%,100%{transform:translateY(0);opacity:.45}30%{transform:translateY(-4px);opacity:1}}
/* 모달 — 데스크톱 중앙 / 모바일 바텀시트 */
.modal{display:none;position:fixed;inset:0;background:rgba(12,20,40,.5);z-index:30;align-items:center;justify-content:center;padding:16px;backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px)}
.modal.on{display:flex}
.sheet{background:#fff;border-radius:24px;width:100%;max-width:440px;padding:26px 24px;max-height:86vh;overflow-y:auto;box-shadow:var(--sh2);animation:pop .22s ease;overscroll-behavior:contain}
@keyframes pop{from{opacity:0;transform:translateY(14px) scale(.98)}to{opacity:1;transform:none}}
@media(max-width:560px){.modal{align-items:flex-end;padding:0}
.sheet{max-width:none;border-radius:24px 24px 0 0;max-height:88vh;animation:slide .25s ease;padding-bottom:calc(26px + env(safe-area-inset-bottom))}
.sheet:before{content:'';display:block;width:44px;height:5px;border-radius:3px;background:var(--line);margin:-10px auto 13px}}
@keyframes slide{from{opacity:.5;transform:translateY(48px)}to{opacity:1;transform:none}}
.sheet h3{font-size:19px;margin-bottom:3px;letter-spacing:-.5px} .sheet .sub{color:var(--sub);font-size:12.5px;margin-bottom:16px}
.mrow{display:flex;justify-content:space-between;padding:10.5px 0;border-bottom:1px solid var(--line);font-size:14px} .mrow .k{color:var(--sub);font-weight:600}
.rsn{background:var(--soft);border:1px solid var(--line);border-radius:16px;padding:15px;font-size:13.5px;line-height:1.6;margin:14px 0}
.rsn b{color:var(--pri)}
.rsn.load{min-height:250px;background:linear-gradient(100deg,var(--soft) 40%,var(--line) 50%,var(--soft) 60%);
background-size:200% 100%;animation:shim 1.2s infinite linear}
@keyframes shim{from{background-position:120% 0}to{background-position:-80% 0}}
.mclose{width:100%;padding:14px;background:#f1f3f7;border:0;border-radius:14px;font-weight:800;cursor:pointer;margin-top:8px;font-size:15px;transition:.15s}
.mclose:hover{background:#e8ebf1}
.foot{text-align:center;font-size:11.5px;color:var(--faint);margin:18px 0 0}
/* ── 모바일 (폰 최적화) ── */
@media(max-width:600px){
.wrap{padding:0 14px calc(52px + env(safe-area-inset-bottom))}
.top{margin:0 -14px 2px;padding:calc(11px + env(safe-area-inset-top)) 16px 9px} .logo{font-size:19px}
.hpl{font-size:11.5px}
.note{margin:5px 2px 10px}
.sbar{padding:5px;gap:5px;margin-bottom:11px}
.sseg{font-size:11px;gap:4px;padding:8px 0} .sseg b{font-size:11px}
.seg{max-width:none;margin-bottom:12px}
.card{padding:16px;border-radius:18px;margin-bottom:11px}
.hero{padding:24px 16px 21px} .hero .amt{font-size:33px} .hero .amt small{font-size:16px}
.pill{font-size:13.5px;padding:7px 13px}
.donut{gap:13px} .dc{width:104px;height:104px} .hole{inset:18px} .hole .t2{font-size:16px}
.hold{padding:11px 4px;gap:10px} .hicon{width:37px;height:37px;border-radius:12px}
.hnm{font-size:14px} .hval{font-size:14px}
.grid{gap:12px} .chat .msgs{min-height:170px;max-height:46vh}
.h{font-size:13.5px}
}
@media(prefers-reduced-motion:reduce){*,*:before,*:after{animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}}
/* ── 다크모드 (기기 설정 따라 자동) ── */
@media(prefers-color-scheme:dark){
:root{--bg:#101418;--card:#181d24;--line:#242b34;--txt:#e7ebf0;--sub:#93a0ad;--faint:#5c6773;--soft:#1f252d;
--sh:0 1px 2px rgba(0,0,0,.25),0 10px 30px rgba(0,0,0,.35);--sh2:0 2px 6px rgba(0,0,0,.35),0 16px 40px rgba(0,0,0,.5);--down:#62a1ff}
body{background:linear-gradient(180deg,#131a17 0,var(--bg) 260px)}
body:before{background:radial-gradient(640px 420px at 88% -8%,rgba(20,154,110,.13),transparent)}
.top{background:rgba(16,20,24,.78)} .top a:hover{background:rgba(255,255,255,.06);color:var(--txt)}
.card{border-color:rgba(255,255,255,.05)}
.seg{background:rgba(36,43,52,.8)} .seg div.on{background:#232b35;color:var(--txt);box-shadow:none}
.sseg.s-on{background:rgba(18,184,134,.14);color:#54cfa0} .sseg.s-on b{color:#43c793}
.sseg.s-wait{background:rgba(255,149,0,.13);color:#ffb04d} .sseg.s-wait b{color:#ff9f2e}
.sseg.s-off{background:#232a32;color:#7d8894} .sseg.s-off b{color:#9aa5b0}
.hole{background:var(--card);box-shadow:none}
.pill.up{background:rgba(240,68,82,.16)} .pill.down{background:rgba(98,161,255,.15)}
.hicon.etf{background:rgba(20,154,110,.16)!important;color:#4ecf9e}
.m.a{background:var(--soft);border-color:var(--line)}
.cin input{background:var(--soft);border-color:var(--line);color:var(--txt)}
.tag.b{background:rgba(240,68,82,.16)} .tag.s{background:rgba(98,161,255,.18)}
.warn{background:#33270f;color:#f0a24a}
.modal{background:rgba(0,0,0,.62)} .sheet{background:var(--card)}
.rsn{background:var(--soft);border-color:var(--line)}
.mclose{background:#232b34;color:var(--txt)} .mclose:hover{background:#2a3340}
.td{background:#5c6773}
.note{color:var(--sub)}
.cin input:focus{background:var(--card)}
}
</style></head><body class=boot><div class=wrap>
<div class=top><div class=logo>시나브로<em>.</em></div><a href="{{url_for('logout')}}">로그아웃</a></div>
<div class=note>{{now}} 기준 · <span style="color:{{'#149a6e' if mkt else 'inherit'}};font-weight:600">{{ '장중' if mkt else '장 마감' }}</span> · <a href="{{url_for('dashboard')}}">↻ 새로고침</a></div>

<div class=sbar>
  <div class="sseg {{'s-on' if bot.kr else 's-off'}}" onclick="openBot('kr')"><span class="led {{'on' if bot.kr else 'off'}}"></span>국내 <b>{{ '가동중' if bot.kr else '정지' }}</b></div>
  <div class="sseg {{'s-wait' if bot.us_wait else ('s-on' if bot.us else 's-off')}}" onclick="openBot('us')"><span class="led {{'wait' if bot.us_wait else ('on' if bot.us else 'off')}}"></span>미국 <b>{{ '환전 대기' if bot.us_wait else ('가동중' if bot.us else '정지') }}</b></div>
  <div class="sseg {{'s-on' if bot.deadman else 's-off'}}" onclick="openBot('dm')"><span class="led {{'on' if bot.deadman else 'off'}}"></span>감시 <b>{{ '켜짐' if bot.deadman else '꺼짐' }}</b></div>
</div>
<div class=seg><div class="on" onclick="sw('kr')">🇰🇷 국내</div><div onclick="sw('us')">🇺🇸 미국</div></div>

<div class=grid>
<div><!-- 왼쪽: 계좌/포트폴리오/보유 -->
<div id=kr class="pane on">
{% if kr.error %}<div class="card warn">⚠️ {{kr.error}}</div>{% else %}
<div class="card hero"><i class=pearl></i><div class=lab>총 자산</div><div class=amt><span class=cnt>{{ '{:,.0f}'.format(kr.total) }}</span><small> 원</small></div>
  {% if kr.get('delta') is not none %}<div class="dchg {{'up' if kr.delta>=0 else 'down'}}">어제보다 {{ '{:+,.0f}'.format(kr.delta) }}원{% if kr.get('delta_pct') is not none %} ({{ '%+.1f'|format(kr.delta_pct) }}%){% endif %}</div>{% endif %}
  <span class="pill {{'up' if (kr.ret or 0)>=0 else 'down'}}">{{ '▲' if (kr.ret or 0)>=0 else '▼' }} {{ '%.2f'|format(kr.ret|abs) if kr.ret is not none else '—' }}% <span style=opacity:.5>·</span> {{ '{:+,.0f}'.format(kr.pl) }}원</span>
  {% if kr.get('spark') %}<svg class=spk viewBox="0 0 300 40" preserveAspectRatio=none>
  <defs><linearGradient id=sg x1=0 y1=0 x2=0 y2=1><stop offset=0 stop-color="rgba(255,255,255,.30)"/><stop offset=1 stop-color="rgba(255,255,255,0)"/></linearGradient></defs>
  <polygon points="0,40 {{kr.spark}} 300,40" fill="url(#sg)"/>
  <polyline points="{{kr.spark}}"/></svg>{% endif %}</div>
<div class=card><div class=h style=margin-bottom:12px>포트폴리오 구성</div>
  <div class=donut><div class=dc><div class=pie style="background:conic-gradient({{kr.conic}})"></div>
    <div class=hole><div class=t1>투자중</div><div class=t2>{{ '%.0f'|format(100 - kr.alloc[2].pct) }}%</div></div></div>
    <div class=leg>{% for s in kr.alloc %}<div class=legrow><span class=dot style=background:{{s.color}}></span>
      <span class=ln>{{s.label}}<div class=lv>{{ '{:,.0f}'.format(s.val) }}원</div></span>
      <span class=lp>{{ '%.0f'|format(s.pct) }}%</span></div>{% endfor %}</div></div>
  {% if dca %}<div class=cap style="color:#ff9500;font-weight:600">📅 지수는 매달 나눠서 사는 중 · 남은 {{ '{:,.0f}'.format(dca.reserved/10000) }}만원 · 약 {{dca.months}}개월</div>
  {% elif kr.alloc[0].pct < 40 %}<div class=cap>⚠️ 지수 비중 부족 · 현금 {{ '%.0f'|format(kr.alloc[2].pct) }}% 재배분 대기</div>{% endif %}</div>
<div class=card><div class=h style=margin-bottom:2px>보유 종목 {{kr.holdings|length}}</div>
{% for h in kr.holdings %}<div class=hold onclick="openStock('{{h.ticker}}','{{h.name}}',{{h.qty}},{{h.buy}},{{h.price}},{{h.plpct}},{{'1' if h.is_etf else '0'}})">
  <div class="hicon {{'etf' if h.is_etf}}" {% if not h.is_etf %}style="background:linear-gradient(135deg,hsl({{h.hue}},62%,58%),hsl({{h.hue}},66%,47%))"{% endif %}>{{ '📊' if h.is_etf else h.name[:2] }}</div>
  <div class=hmid><div class=hnm>{{h.name}}</div>
    <div class=prices><span class=pb>매입 {{ '{:,.0f}'.format(h.buy) }}</span><span class="pc {{'up' if h.plpct>=0 else 'down'}}">현재 <b>{{ '{:,.0f}'.format(h.price) }}</b></span></div></div>
  <div class=hend><div class=hval>{{ '{:,.0f}'.format(h.value) }}</div><div class="hpl {{'up' if h.plpct>=0 else 'down'}}">{{ '{:+,.0f}'.format(h.plw) }} ({{ '%+.1f'|format(h.plpct) }}%)</div></div>
  <span class=chev>›</span></div>{% endfor %}
{% if not kr.holdings %}<div class="mut" style=text-align:center;padding:16px>보유 종목 없음</div>{% endif %}</div>
{% endif %}</div>

<div id=us class=pane>
{% if us.error %}<div class="card warn">⚠️ {{us.error}}</div>{% else %}
<div class="card hero"><i class=pearl></i><div class=lab>USD 예수금</div><div class=amt>$<span class=cnt>{{ '%.2f'|format(us.cash_usd) }}</span></div></div>
{% if us.holdings %}<div class=card>{% for h in us.holdings %}<div class=hold style=cursor:default>
<div class=hicon style=background:linear-gradient(135deg,#f04452,#d63a48)>{{h.ticker[:3]}}</div><div class=hmid><div class=hnm>{{h.ticker}}</div></div>
<div class=hend><div class=hval>{{ '%.4f'|format(h.qty) }}주</div></div></div>{% endfor %}</div>
{% else %}<div class="card mut" style=text-align:center;padding:24px>SPY 미보유<br><span style=font-size:12px>USD 환전 시 자동매수 대기</span></div>{% endif %}{% endif %}</div>
</div>

<div><!-- 오른쪽: AI / 자동화상세 / 거래 -->
<div class="card chat"><div class=h style=margin-bottom:10px>💬 AI 어시스턴트</div>
  <div class=msgs id=msgs><div class="m a">무엇이든 물어보세요.</div></div>
  <div class=cin><input id=ci placeholder="메시지 입력..." onkeydown="if(event.key=='Enter')send()"><button id=cbtn onclick=send()>전송</button></div></div>

<div class=card><details><summary style="cursor:pointer;font-weight:800;font-size:15px;outline:none">⚙️ 자동화 상세</summary>
  <div style=margin-top:12px>
  <div class=st><span class=kk>리밸런스 상태</span><span class=vv>{{bot.rebal}}</span></div>
  <div class=st><span class=kk>참고서 필터</span><span class=vv>{{bot.artifact}}</span></div>
  <div class=st><span class=kk>감시 heartbeat</span><span class=vv>{{bot.heartbeat}}</span></div>
  <div class=cap>크론: 리밸 평일10:00 · 신규배분 평일10:30 · US 미국장 · deadman 매일</div></div></details></div>

<div class=card><div class=h style=margin-bottom:4px>📜 최근 거래</div>
  {% for t in trades[:3] %}<div class=trow><span class="tag {{'s' if t.action=='SELL' else 'b'}}">{{ '매도' if t.action=='SELL' else '매수' }}</span>
  <span class=tnm>{{t.stock_name}}</span>
  <span class=tend><b>{{ '{:,.0f}'.format(t.amt) }}원</b><small>{{t.shares}}주 · {{t.day}}</small></span></div>{% endfor %}
  {% if trades|length > 3 %}<details><summary>전체 {{trades|length}}건</summary>
  {% for t in trades[3:] %}<div class=trow><span class="tag {{'s' if t.action=='SELL' else 'b'}}">{{ '매도' if t.action=='SELL' else '매수' }}</span>
  <span class=tnm>{{t.stock_name}}</span>
  <span class=tend><b>{{ '{:,.0f}'.format(t.amt) }}원</b><small>{{t.shares}}주 · {{t.day}}</small></span></div>{% endfor %}</details>{% endif %}
  {% if not trades %}<div class="mut" style=text-align:center;padding:10px>거래 없음</div>{% endif %}</div>
</div>
</div>

</div>

<div id=modal class=modal onclick="if(event.target==this)closeM()"><div class=sheet id=sheet></div></div>
<div id=ptr>↻</div>

<script>
var BOTD={{botd|tojson}};
/* 종목 매수이유 프리페치 — 모달이 한 번에 완성형으로 뜨게(2단 로딩 제거) */
var RSN={};
(function(){var tks={{ kr.holdings|map(attribute='ticker')|list|tojson if not kr.error else '[]' }};
if(tks.length)fetch('/api/stocks?t='+tks.join(',')).then(function(r){return r.json()}).then(function(j){RSN=j||{}}).catch(function(){});})();
function openBot(k){var d=BOTD[k];if(!d)return;var s=document.getElementById('sheet');
s.innerHTML='<h3>'+d.title+'</h3><div class=sub>'+d.sub+'</div>'+d.html+'<button class=mclose onclick=closeM()>닫기</button>';
s.scrollTop=0;
document.getElementById('modal').classList.add('on');document.body.classList.add('mlock');}
function sw(x){var was=document.querySelector('.pane.on');
document.querySelectorAll('.seg div').forEach(t=>t.classList.remove('on'));
document.querySelectorAll('.pane').forEach(p=>p.classList.remove('on'));
document.getElementById(x).classList.add('on');event.currentTarget.classList.add('on');
if(was&&was.id!=x)window.scrollTo(0,0);}
function closeM(){var sh=document.getElementById('sheet');
document.getElementById('modal').classList.remove('on');document.body.classList.remove('mlock');
sh.style.transform='';sh.style.transition='';}
/* 바텀시트 아래로 드래그해서 닫기 (내용이 맨 위일 때만) */
(function(){var sh=document.getElementById('sheet'),sy=-1,dy=0;
sh.addEventListener('touchstart',function(e){sy=(sh.scrollTop<=0)?e.touches[0].clientY:-1;dy=0;},{passive:true});
sh.addEventListener('touchmove',function(e){if(sy<0)return;dy=e.touches[0].clientY-sy;
if(dy>0){sh.style.transition='none';sh.style.transform='translateY('+dy+'px)';}},{passive:true});
sh.addEventListener('touchend',function(){
if(dy>110){sh.style.transition='transform .22s ease';sh.style.transform='translateY(110%)';setTimeout(closeM,190);}
else if(dy>0){sh.style.transition='transform .2s ease';sh.style.transform='';}
sy=-1;dy=0;},{passive:true});})();
async function openStock(tk,nm,qty,buy,price,pl,etf){
var col=pl>=0?'#f04452':'#3182f6';
var s=document.getElementById('sheet');
s.innerHTML='<h3>'+nm+'</h3><div class=sub>'+tk+(etf==1?' · 지수 ETF':' · 저변동 선정')+'</div>'
+'<div class=mrow><span class=k>보유</span><span>'+qty+'주</span></div>'
+'<div class=mrow><span class=k>매입가</span><span>'+buy.toLocaleString()+'원</span></div>'
+'<div class=mrow><span class=k>현재가</span><span style="color:'+col+';font-weight:800">'+price.toLocaleString()+'원 ('+(pl>=0?'+':'')+pl.toFixed(1)+'%)</span></div>'
+'<div class=mrow><span class=k>평가액</span><span style=font-weight:800>'+(qty*price).toLocaleString()+'원</span></div>'
+'<div class=mrow><span class=k>평가손익</span><span style="color:'+col+';font-weight:800">'+((price-buy)*qty>=0?'+':'')+Math.round((price-buy)*qty).toLocaleString()+'원</span></div>'
+'<div class="rsn'+(RSN[tk]?'':' load')+'" id=rsn>'+(RSN[tk]||'')+'</div><button class=mclose onclick=closeM()>닫기</button>';
s.scrollTop=0;
document.getElementById('modal').classList.add('on');document.body.classList.add('mlock');
if(!RSN[tk]){try{var r=await fetch('/api/stock/'+tk);var j=await r.json();RSN[tk]=j.html;
var el=document.getElementById('rsn');el.classList.remove('load');el.innerHTML=j.html;}
catch(e){var el2=document.getElementById('rsn');el2.classList.remove('load');el2.textContent='정보 로드 실패';}}}
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function md(s){return esc(s).replace(/\\*\\*(.+?)\\*\\*/g,'<b>$1</b>').replace(/^\\s*[\\*\\-]\\s+/gm,'· ')}
async function send(){var i=document.getElementById('ci'),b=document.getElementById('cbtn'),m=document.getElementById('msgs'),v=i.value.trim();if(!v||i.disabled)return;
i.value='';i.disabled=true;b.disabled=true;
var u=document.createElement('div');u.className='m u';u.textContent=v;m.appendChild(u);
var a=document.createElement('div');a.className='m a typing';a.innerHTML='<span class=td></span><span class=td></span><span class=td></span>';
m.appendChild(a);m.scrollTop=m.scrollHeight;
try{var r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:v})});
var j=await r.json();a.classList.remove('typing');a.innerHTML=md(j.reply||'(응답 없음)');}
catch(e){a.classList.remove('typing');a.textContent='(오류 — 다시 보내주세요)';}
i.disabled=false;b.disabled=false;i.focus();m.scrollTop=m.scrollHeight;}
/* 당겨서 새로고침 — 릴리즈 방식(끝까지 당겼다 놓으면 실행), 모달 열림시 무시 */
var _py=-1,_armed=false;
document.addEventListener('touchstart',function(e){
_armed=false;
_py=(window.scrollY<=0&&!document.getElementById('modal').classList.contains('on'))?e.touches[0].clientY:-1;},{passive:true});
document.addEventListener('touchmove',function(e){if(_py<0)return;
var d=e.touches[0].clientY-_py,p=document.getElementById('ptr');
if(d<=0){p.style.opacity=0;_armed=false;return}
p.style.opacity=Math.min(d/90,1);
p.style.transform='translateX(-50%) translateY('+Math.min(d/3,28)+'px) rotate('+(d*2)+'deg)';
if(d>90&&!_armed){_armed=true;if(navigator.vibrate)navigator.vibrate(10);}},{passive:true});
document.addEventListener('touchend',function(){
var p=document.getElementById('ptr');
if(_armed){p.classList.add('spin');p.style.transform='translateX(-50%)';setTimeout(function(){location.reload()},250);}
else{p.style.opacity=0;p.style.transform='translateX(-50%)';}
_py=-1;_armed=false;},{passive:true});
/* 좌우 스와이프로 국내/미국 탭 전환 */
var _sx=0,_sy=0;
document.addEventListener('touchstart',function(e){_sx=e.touches[0].clientX;_sy=e.touches[0].clientY},{passive:true});
document.addEventListener('touchend',function(e){
var dx=e.changedTouches[0].clientX-_sx,dy=e.changedTouches[0].clientY-_sy;
if(Math.abs(dx)>75&&Math.abs(dy)<45&&!document.getElementById('modal').classList.contains('on')){
var t=document.querySelectorAll('.seg div');(dx<0?t[1]:t[0]).click();}});
setTimeout(function(){document.body.classList.remove('boot')},900);
/* 펄 광원: 자이로 지원시(HTTPS)만 기울기 반사 — 평소엔 자동 광택+트윙클만(은은) */
window.addEventListener('deviceorientation',function(e){
if(e.gamma==null)return;
var g=Math.max(-28,Math.min(28,e.gamma)),b=Math.max(-28,Math.min(28,(e.beta||40)-40));
document.querySelectorAll('.pearl').forEach(function(p){
p.classList.add('tilt');p.style.backgroundPosition=(50+g*1.7)+'% '+(50+b*1.3)+'%';});});
var _loaded=Date.now();
document.addEventListener('visibilitychange',function(){
if(document.hidden)return;
var busy=document.querySelector('#msgs .m.u')||document.getElementById('modal').classList.contains('on')||document.getElementById('ci').value;
if(!busy&&Date.now()-_loaded>60000)location.reload();});
document.querySelectorAll('.cnt').forEach(function(el){
var t=el.textContent.trim(),dec=(t.split('.')[1]||'').length,v=parseFloat(t.replace(/,/g,''));
if(isNaN(v))return;var s=performance.now(),D=620;
(function f(n){var p=Math.min((n-s)/D,1),e=1-Math.pow(1-p,3);
el.textContent=(v*e).toLocaleString(undefined,{minimumFractionDigits:dec,maximumFractionDigits:dec});
if(p<1)requestAnimationFrame(f);})(s);});
</script></body></html>"""

LOGIN = """<!doctype html><html lang=ko><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>시나브로 로그인</title><style>
*{box-sizing:border-box}
body{font-family:Pretendard,-apple-system,'Malgun Gothic',system-ui,sans-serif;background:linear-gradient(160deg,#eff3f1,#ddeee6);color:#191f28;display:flex;height:100vh;align-items:center;justify-content:center;margin:0;letter-spacing:-.3px}
form{background:#fff;padding:36px 30px;border-radius:26px;width:320px;box-shadow:0 2px 6px rgba(23,32,64,.05),0 24px 60px rgba(0,25,80,.13)}
h2{margin:0 0 4px;font-size:26px;letter-spacing:-.8px} h2 span{color:#149a6e} .s{color:#8b95a1;font-size:13px;margin-bottom:20px}
input{width:100%;padding:14px;margin:6px 0;background:#f5f8f6;border:1.5px solid #eef2f0;color:#191f28;border-radius:13px;font-size:15px;transition:.15s}
input:focus{outline:none;border-color:#149a6e;background:#fff;box-shadow:0 0 0 3px rgba(20,154,110,.13)}
button{width:100%;padding:14px;background:#149a6e;color:#fff;border:0;border-radius:13px;margin-top:14px;cursor:pointer;font-weight:800;font-size:16px;transition:.15s}
button:hover{background:#0f8159}
.e{color:#f04452;font-size:13px;margin-bottom:6px;font-weight:600}</style></head><body>
<form method=post><h2>시나브로<span>.</span></h2><div class=s>모르는 사이 조금씩 · 자동매매</div>
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
    kr = kr_snapshot(row); us = us_snapshot(row); bot = bot_status(); dca = dca_status()
    # US '환전 대기' 상태: 봇은 무장인데 USD 0 + SPY 미보유 → 배너에서 바로 이유를 보여줌
    bot['us_wait'] = bool(bot.get('us')) and not us.get('error') and (us.get('cash_usd') or 0) < 1 and not us.get('holdings')
    if not kr.get('error'):
        try:
            pts, delta, prev = _equity_snapshot(kr['total'])
            kr['spark'] = _spark(pts); kr['delta'] = delta
            kr['delta_pct'] = (delta / prev * 100) if (delta is not None and prev) else None
        except Exception:
            pass
    now = datetime.datetime.now()
    mkt = now.weekday() < 5 and (9, 0) <= (now.hour, now.minute) < (15, 30)
    return render_template_string(
        PAGE, now=now.strftime('%Y-%m-%d %H:%M'), mkt=mkt,
        kr=kr, us=us, dca=dca, trades=recent_trades(int(row['id'])),
        bot=bot, botd=bot_details(bot, us, dca))


# 수익패턴 → 평이한 한글 라벨 + 한줄설명
_PAT = {
    'spike':              ('📈 급등형', '평소 잠잠하다 가끔 크게 튀는 유형'),
    'decline':            ('📉 하락형', '장기적으로 우하향해온 유형'),
    'sideways':           ('➡️ 횡보형', '뚜렷한 방향 없이 오르내린 유형'),
    'steady_grower':      ('🌱 꾸준상승형', '완만하게 우상향해온 유형'),
    'artifact_confirmed': ('⚠️ 데이터의심', '거래 이상 신호 — 회피 대상'),
    'delisted':           ('⛔ 상장폐지', ''),
}


def _bar(label, raw, scale, color=None, signed=True):
    """작은 가로 막대 1줄. raw=비율(0.42=42%). Korean color(빨강+/파랑-) 기본.
    signed=False면 부호 없이 절대값(변동성처럼 방향 없는 값). 색은 CSS 변수 기반(.bar) = 다크모드 자동."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return (f'<div class=bar><span class=bl>{label}</span><span class=trk></span>'
                f'<span class=bv style="color:var(--sub)">—</span></div>')
    w = min(abs(v) / scale, 1.0) * 100
    col = color or ('#3182f6' if v < 0 else '#f04452')
    val = f"{v*100:+.1f}%" if signed else f"{abs(v)*100:.1f}%"
    return (f'<div class=bar><span class=bl>{label}</span>'
            f'<span class=trk><span class=fill style="width:{w:.0f}%;background:{col}"></span></span>'
            f'<span class=bv style="color:{col}">{val}</span></div>')


def _stock_reason(m):
    plabel, pdesc = _PAT.get(m.get('pattern', ''), ('· 패턴 정보 없음', ''))
    # 참고서 데이터 신뢰도 등급 + 평이한 설명
    tmap = {'clean': ('데이터 정상', '#00c473', '과거 시세에서 이상 신호 없음 — 믿을 만한 데이터'),
            'watch': ('검토 필요', '#ff9500', '이상 신호 1개(약한 의심) — 배제까진 아니고 참고 표시'),
            'confirmed': ('데이터 아티팩트 의심', '#f04452', '이상 신호 강함 — 데이터 왜곡 가능, 라이브에선 제외됨')}
    tlabel, tcol, tdesc = tmap.get(m.get('artifact_tier'), ('정보 없음', '#8b95a1', ''))
    best = (m.get('best_year') or '').replace(':', '년 ')
    worst = (m.get('worst_year') or '').replace(':', '년 ')
    yr = (f"<div style='font-size:12px;color:var(--sub);margin-top:9px'>최고 {best or '—'} · 최악 {worst or '—'}</div>"
          if (best or worst) else '')
    fy = (m.get('first_date') or '')[:4]; ly = (m.get('last_date') or '')[:4]
    span = f"{fy}~{ly}" if (fy and ly) else "상장 이후"
    why = (
        "<b>왜 이 종목을 샀나</b>"
        "<div style='margin:8px 0 4px;line-height:1.95;font-size:13px'>"
        "✅ <b>주가가 안정적</b> — 최근 6개월 등락이 작은 편<br>"
        "✅ <b>상승 추세</b> — 200일 평균선 위<br>"
        "✅ <b>부실기업 아님</b> — 자본잠식·연속적자·거래정지 같은 위험기업은 애초에 걸러냈고, 이 종목은 그 관문을 통과"
        "</div>"
        "<div style='font-size:12px;color:var(--sub)'>이렇게 통과한 저변동 25종목을 같은 비중으로, "
        "사고팔기(타이밍)·손절 없이 분기 동안 보유합니다.</div>"
    )
    viz = (
        "<div style='margin-top:14px;padding-top:12px;border-top:1px solid var(--line)'>"
        "<div style='display:flex;align-items:center;justify-content:space-between;margin-bottom:3px'>"
        "<b>수익 패턴</b>"
        f"<span style='font-size:14px;font-weight:800'>{plabel}</span></div>"
        f"<div style='font-size:12px;color:var(--sub);margin-bottom:9px'>{pdesc}</div>"
        f"<div style='font-size:11px;color:var(--faint);margin-bottom:9px;line-height:1.5'>※ 아래는 {span} <b>장기</b> 기록입니다. "
        f"위 '주가 안정적'은 <b>최근 6개월</b> 기준이라, 장기론 더 크게 출렁였을 수 있어요.</div>"
        + _bar('총수익', m.get('total_ret'), 2.0)
        + _bar('연수익', m.get('cagr'), 0.3)
        + _bar('최대낙폭', m.get('mdd'), 1.0, color='#3182f6')
        + _bar('변동성', m.get('ann_vol'), 0.8, color='#ff9500', signed=False)
        + yr
        + f"<div style='font-size:12px;margin-top:10px'>데이터 신뢰도 "
        f"<span class=mut style='font-weight:500'>· 참고서가 시세데이터 품질 평가</span>: "
        f"<b style='color:{tcol}'>{tlabel}</b></div>"
        + (f"<div style='font-size:11px;color:var(--sub);margin-top:2px;line-height:1.5'>{tdesc}</div>" if tdesc else '')
        + "</div>"
    )
    return why + viz


_ICON = None


@app.route('/icon.png')
def icon():
    """홈화면 앱 아이콘 — 딥그린 배경 + 시나브로(조금씩 상승) 계단 3개. PIL로 1회 생성 후 캐시."""
    global _ICON
    from io import BytesIO
    from flask import send_file
    if _ICON is None:
        from PIL import Image, ImageDraw
        s = 512
        img = Image.new('RGB', (s, s), '#173a2c')
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, s, s], radius=0, fill='#1b4636')
        d.ellipse([-160, -160, 300, 300], fill='#1e5240')       # 좌상단 은은한 글로우
        bw, gap, base = 88, 40, 396                              # 계단 3개(조금씩 ↑)
        x0 = (s - bw * 3 - gap * 2) // 2
        for i, (hh, col) in enumerate([(120, '#8fd6b4'), (190, '#c9ecd9'), (268, '#ffffff')]):
            x = x0 + i * (bw + gap)
            d.rounded_rectangle([x, base - hh, x + bw, base], radius=26, fill=col)
        buf = BytesIO(); img.save(buf, 'PNG'); _ICON = buf.getvalue()
    return send_file(BytesIO(_ICON), mimetype='image/png', max_age=86400)


@app.route('/manifest.json')
def manifest():
    return jsonify({
        'name': '시나브로', 'short_name': '시나브로', 'display': 'standalone',
        'start_url': '/', 'background_color': '#101418', 'theme_color': '#173a2c',
        'icons': [{'src': '/icon.png', 'sizes': '512x512', 'type': 'image/png'}]})


def _reason_html(tk):
    tk = str(tk).zfill(6)
    if tk == '069500':
        return ("<b>지수 슬리브 (코스피200)</b><br>KODEX200 = 코스피200 시총가중 ETF. "
                "폭등장에 함께 오르는 역할로 <b>포트폴리오의 50%</b>를 담당합니다. "
                "지수를 그대로 따라가, 저변동 종목들이 상승장에서 덜 오르는 약점을 메웁니다.")
    return _stock_reason(_master().get(tk, {}))


@app.route('/api/stock/<ticker>')
@login_required
def api_stock(ticker):
    return jsonify(html=_reason_html(ticker))


@app.route('/api/stocks')
@login_required
def api_stocks():
    """보유 전 종목의 매수이유를 한 번에 — 페이지 로드시 프리페치용(모달 즉시 오픈)."""
    tks = [t.strip() for t in (request.args.get('t') or '')[:2000].split(',') if t.strip().isdigit()]
    return jsonify({t.zfill(6): _reason_html(t) for t in tks[:40]})


@app.route('/api/chat', methods=['POST'])
@login_required
def api_chat():
    msg = (request.json or {}).get('message', '') if request.is_json else request.form.get('message', '')
    msg = (msg or '').strip()[:500]
    if not msg:
        return jsonify(reply='메시지를 입력해주세요.')
    row = current_user.row
    key = row['gemini_api_key']
    if not key:
        return jsonify(reply='Gemini API 키가 설정되어 있지 않습니다.')
    kr = kr_snapshot(row)
    ctx = (f"총자산 {kr['total']:,.0f}원, 미실현수익률 {kr['ret']:.2f}%, 보유 {len(kr['holdings'])}종목, "
           f"현금 {kr['cash']:,.0f}원(미투입). 전략=KODEX200 지수ETF 50% + v3저변동 25종목 50%, 분기 리밸런스, "
           f"참고서(데이터아티팩트·부실상폐 회피). 저변동 목표는 25종목이지만 배정액보다 1주 가격이 비싼 종목은 "
           f"건너뛰어 실제 보유수가 더 적을 수 있음(분기 리밸런스가 채움). 미국은 SPY(S&P500 ETF) 하나만 매수. "
           f"매매는 서버가 정해진 시간에 자동 실행.") if not kr['error'] else '계좌조회 실패'
    prompt = ("너는 '시나브로' 자동투자 대시보드의 어시스턴트다. 아래 맥락으로 사용자 질문에 한국어로 간결·친근하게 답해라. "
              "너는 매매 실행이나 봇 켜기/끄기를 할 수 없다(그건 개발 채팅에서만 가능) — 설명·조언만 한다. "
              "전문용어(크론·EC2 등) 금지, 마크다운 기호(**굵게**, * 목록) 금지 — 짧은 일반 문장으로, 5문장 이내. "
              "인사말 없이 질문에 바로 답해라.\n\n[포트폴리오]\n" + ctx + "\n\n[질문]\n" + msg)
    return jsonify(reply=_gemini(key, prompt))


if __name__ == '__main__':
    try:
        init_db()
    except Exception:
        pass
    app.run(host='0.0.0.0', port=5000, debug=False)
