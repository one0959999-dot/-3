# -*- coding: utf-8 -*-
"""Lassi 대시보드 v8 — 반응형(데스크톱 2열/모바일 1열) + 종목상세 + 봇상태배너 + AI챗.

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
app.secret_key = os.environ.get('LASSI_SECRET', 'lassi-dash-v8')
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
        for label, v, col in [('지수 ETF', etf_v, '#3182f6'), ('저변동 25종목', stk_v, '#20c997'), ('현금', cash, '#c4cdd8')]:
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
        cron = subprocess.run(['crontab', '-l'], capture_output=True, text=True, timeout=5).stdout
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
                            + dca_line +
                            "</div><div class=rsn><b>따로 하실 일은 없어요</b><br>"
                            "매매가 일어나면 그때마다 텔레그램으로 알려드립니다. "
                            "이 화면은 언제든 들어와서 구경만 하셔도 됩니다.</div>")}
    else:
        d['kr'] = {'title': '국내 자동매매', 'sub': '⏸️ 정지됨',
                   'html': ("<div class=rsn><b>왜 정지인가요?</b><br>"
                            "서버의 예약작업(크론)에서 국내 매매가 해제되어 있어요. "
                            "이 상태에서는 리밸런스도, 신규 현금 매수도 실행되지 않습니다.</div>"
                            "<div class=rsn><b>다시 켜려면</b><br>"
                            "EC2 서버의 crontab에 auto_order(리밸런스)·auto_deploy(신규자금) 항목을 "
                            "다시 등록해야 해요. 채팅으로 요청해 주시면 도와드릴게요.</div>")}
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
                   'html': ("<div class=rsn><b>왜 정지인가요?</b><br>"
                            "서버의 예약작업(크론)에서 미국 매매가 해제되어 있어요.</div>"
                            "<div class=rsn><b>다시 켜려면</b><br>"
                            "EC2 crontab에 auto_order_us 항목을 다시 등록하면 됩니다. "
                            "채팅으로 요청해 주시면 도와드릴게요.</div>")}
    # ── 감시장치 ──
    hb = bot.get('heartbeat', '—')
    if bot.get('deadman'):
        d['dm'] = {'title': '감시장치 (deadman)', 'sub': '🟢 켜져 있음',
                   'html': ("<div class=rsn><b>뭘 하는 건가요?</b><br>"
                            "매일 자정에 '봇이 살아있나'를 스스로 점검하는 안전장치예요. "
                            "봇이 멈추거나 며칠째 아무 기록이 없으면 텔레그램으로 바로 알려줍니다.<br><br>"
                            f"마지막 생존신호: <b>{hb}</b></div>"
                            "<div class=rsn><b>여행 가도 되나요?</b><br>"
                            "네. 문제가 생기면 이 장치가 알려주니, 알림이 없다는 건 잘 돌고 있다는 뜻이에요.</div>")}
    else:
        d['dm'] = {'title': '감시장치 (deadman)', 'sub': '⏸️ 꺼져 있음',
                   'html': ("<div class=rsn><b>주의</b><br>"
                            "봇이 멈춰도 알려줄 감시가 꺼져 있어요. 매매 봇 자체는 별개로 돌 수 있지만, "
                            "문제가 생겨도 모를 수 있으니 켜두는 걸 추천해요. 채팅으로 요청해 주세요.</div>")}
    return d


def recent_trades(uid, n=30):
    c = get_db_connection()
    try:
        rows = c.execute("SELECT stock_name, action, price, shares, created_at, mode "
                         "FROM trade_journal WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, n)).fetchall()
        return [dict(r) for r in rows]
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
<meta name=viewport content="width=device-width,initial-scale=1"><title>Lassi</title><style>
:root{--bg:#f2f4f8;--card:#fff;--line:#f0f2f6;--txt:#191f28;--sub:#8b95a1;--faint:#b6bdc7;--up:#f04452;--down:#3182f6;--pri:#3182f6;--soft:#f6f8fb;--grn:#12b886;
--sh:0 1px 2px rgba(23,32,64,.04),0 10px 30px rgba(23,32,64,.06);--sh2:0 2px 6px rgba(23,32,64,.06),0 16px 40px rgba(23,32,64,.09)}
*{box-sizing:border-box;margin:0;-webkit-tap-highlight-color:transparent}
body{font-family:Pretendard,-apple-system,'Malgun Gothic','Apple SD Gothic Neo',system-ui,sans-serif;
background:linear-gradient(180deg,#eaeff8 0,var(--bg) 260px);color:var(--txt);letter-spacing:-.3px;font-size:14px;min-height:100vh}
.wrap{max-width:1060px;margin:0 auto;padding:0 20px 64px}
.num,.amt,.hval,.hpl,.vv,.lp,.pill{font-variant-numeric:tabular-nums}
/* 스티키 헤더 */
.top{position:sticky;top:0;z-index:15;display:flex;justify-content:space-between;align-items:center;
margin:0 -20px 2px;padding:13px 22px 11px;background:rgba(238,242,249,.78);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px)}
.logo{font-size:20px;font-weight:800;letter-spacing:-.6px} .logo em{font-style:normal;color:var(--pri)}
.top a{color:var(--sub);text-decoration:none;font-size:12.5px;font-weight:600;padding:6px 11px;border-radius:9px;transition:.15s}
.top a:hover{background:rgba(255,255,255,.85);color:var(--txt)}
.note{font-size:11.5px;color:var(--faint);margin:6px 2px 12px} .note a{color:var(--pri);text-decoration:none;font-weight:600}
/* 봇 상태 배너 */
.banner{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}
.bstat{flex:1;min-width:148px;display:flex;align-items:center;gap:11px;background:var(--card);border:1px solid rgba(15,30,70,.045);
border-radius:16px;padding:13px 15px;box-shadow:var(--sh);cursor:pointer;transition:transform .15s,box-shadow .15s}
.bstat:hover{transform:translateY(-1px);box-shadow:var(--sh2)} .bstat:active{transform:scale(.985)}
.binfo{color:#c4cdd8;font-size:16px;transition:.15s} .bstat:hover .binfo{color:var(--pri);transform:translateX(2px)}
.led{width:10px;height:10px;border-radius:50%;flex-shrink:0} .led.off{background:#cbd3dd}
.led.on{background:var(--grn);animation:pulse 2.4s ease-out infinite}
.led.wait{background:#ff9500;animation:pulsew 2.4s ease-out infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(18,184,134,.35)}70%{box-shadow:0 0 0 7px rgba(18,184,134,0)}100%{box-shadow:0 0 0 0 rgba(18,184,134,0)}}
@keyframes pulsew{0%{box-shadow:0 0 0 0 rgba(255,149,0,.35)}70%{box-shadow:0 0 0 7px rgba(255,149,0,0)}100%{box-shadow:0 0 0 0 rgba(255,149,0,0)}}
.bstat .bl{font-size:11.5px;color:var(--sub);font-weight:600} .bstat .bv{font-size:14.5px;font-weight:800;margin-top:1px}
.bstat .bv.on{color:var(--grn)} .bstat .bv.off{color:var(--sub)} .bstat .bv.wait{color:#ff9500}
/* 탭 */
.seg{display:flex;background:rgba(222,229,238,.75);border-radius:14px;padding:4px;gap:4px;margin:0 0 14px;max-width:340px}
.seg div{flex:1;text-align:center;padding:9px 0;border-radius:11px;font-weight:700;font-size:14px;color:var(--sub);cursor:pointer;transition:.18s}
.seg div.on{background:#fff;color:var(--txt);box-shadow:0 2px 8px rgba(23,32,64,.1)}
/* 반응형 2열 */
.grid{display:grid;grid-template-columns:1fr;gap:16px} @media(min-width:840px){.grid{grid-template-columns:1.25fr 1fr;align-items:start}}
.pane{display:none} .pane.on{display:block;animation:f .22s ease} @keyframes f{from{opacity:0;transform:translateY(7px)}to{opacity:1}}
.card{background:var(--card);border:1px solid rgba(15,30,70,.045);border-radius:20px;padding:20px;margin-bottom:14px;box-shadow:var(--sh)}
.h{font-size:14px;font-weight:800;margin:0 4px 8px}
/* hero */
.hero{background:linear-gradient(150deg,#fff 0,#f2f7ff 55%,#ebf2ff 100%);text-align:center;padding:30px 20px 26px;position:relative;overflow:hidden}
.hero:before{content:'';position:absolute;width:240px;height:240px;border-radius:50%;top:-110px;right:-70px;
background:radial-gradient(closest-side,rgba(49,130,246,.12),transparent)}
.hero:after{content:'';position:absolute;width:180px;height:180px;border-radius:50%;bottom:-100px;left:-60px;
background:radial-gradient(closest-side,rgba(18,184,134,.08),transparent)}
.hero>*{position:relative}
.hero .lab{font-size:12.5px;color:var(--sub);font-weight:700}
.hero .amt{font-size:40px;font-weight:800;margin:5px 0 13px;letter-spacing:-2px} .hero .amt small{font-size:19px;color:var(--sub);font-weight:700;letter-spacing:-.5px}
.pill{display:inline-flex;align-items:center;gap:5px;font-size:14.5px;font-weight:800;padding:8px 15px;border-radius:99px}
.up{color:var(--up)} .down{color:var(--down)} .pill.up{background:#fdeaec} .pill.down{background:#e9f1fe}
/* 도넛 */
.donut{display:flex;align-items:center;gap:18px}
.dc{position:relative;width:120px;height:120px;flex-shrink:0} .pie{width:100%;height:100%;border-radius:50%;box-shadow:inset 0 0 0 1px rgba(15,30,70,.04)}
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
.hicon.etf{background:#e7f0ff !important;color:var(--pri);box-shadow:none}
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
.chat .msgs{max-height:290px;overflow-y:auto;display:flex;flex-direction:column;gap:9px;padding:2px;scrollbar-width:thin}
.msgs::-webkit-scrollbar{width:5px} .msgs::-webkit-scrollbar-thumb{background:#dfe5ec;border-radius:3px}
.m{max-width:85%;padding:10px 13px;border-radius:16px;font-size:13.5px;line-height:1.55;white-space:pre-wrap;word-break:break-word}
.m.u{align-self:flex-end;background:linear-gradient(135deg,#3b8bff,#2c78ec);color:#fff;border-bottom-right-radius:5px;box-shadow:0 3px 10px rgba(49,130,246,.25)}
.m.a{align-self:flex-start;background:var(--soft);border:1px solid var(--line);border-bottom-left-radius:5px}
.cin{display:flex;gap:8px;margin-top:12px}
.cin input{flex:1;padding:12px 14px;border:1.5px solid var(--line);border-radius:13px;font-size:14px;background:var(--soft);transition:.15s}
.cin input:focus{outline:none;border-color:var(--pri);background:#fff;box-shadow:0 0 0 3px rgba(49,130,246,.12)}
.cin button{padding:12px 17px;background:var(--pri);color:#fff;border:0;border-radius:13px;font-weight:800;cursor:pointer;transition:.15s}
.cin button:hover{background:#2b74e0} .cin button:active{transform:scale(.96)}
/* 모달 — 데스크톱 중앙 / 모바일 바텀시트 */
.modal{display:none;position:fixed;inset:0;background:rgba(12,20,40,.5);z-index:30;align-items:center;justify-content:center;padding:16px;backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px)}
.modal.on{display:flex}
.sheet{background:#fff;border-radius:24px;width:100%;max-width:440px;padding:26px 24px;max-height:86vh;overflow-y:auto;box-shadow:var(--sh2);animation:pop .22s ease}
@keyframes pop{from{opacity:0;transform:translateY(14px) scale(.98)}to{opacity:1;transform:none}}
@media(max-width:560px){.modal{align-items:flex-end;padding:0}
.sheet{max-width:none;border-radius:24px 24px 0 0;max-height:88vh;animation:slide .25s ease}}
@keyframes slide{from{opacity:.5;transform:translateY(48px)}to{opacity:1;transform:none}}
.sheet h3{font-size:19px;margin-bottom:3px;letter-spacing:-.5px} .sheet .sub{color:var(--sub);font-size:12.5px;margin-bottom:16px}
.mrow{display:flex;justify-content:space-between;padding:10.5px 0;border-bottom:1px solid var(--line);font-size:14px} .mrow .k{color:var(--sub);font-weight:600}
.rsn{background:var(--soft);border:1px solid var(--line);border-radius:16px;padding:15px;font-size:13.5px;line-height:1.6;margin:14px 0}
.rsn b{color:var(--pri)}
.mclose{width:100%;padding:14px;background:#f1f3f7;border:0;border-radius:14px;font-weight:800;cursor:pointer;margin-top:8px;font-size:15px;transition:.15s}
.mclose:hover{background:#e8ebf1}
.foot{text-align:center;font-size:11.5px;color:var(--faint);margin:18px 0 0}
</style></head><body><div class=wrap>
<div class=top><div class=logo>Lassi<em>.</em></div><a href="{{url_for('logout')}}">로그아웃</a></div>
<div class=note>{{now}} 기준 · <a href="{{url_for('dashboard')}}">↻ 새로고침</a></div>

<div class=banner>
  <div class=bstat onclick="openBot('kr')"><span class="led {{'on' if bot.kr else 'off'}}"></span><div style=flex:1><div class=bl>국내 자동매매</div><div class="bv {{'on' if bot.kr else 'off'}}">{{ '가동중' if bot.kr else '정지' }}</div></div><span class=binfo>›</span></div>
  <div class=bstat onclick="openBot('us')"><span class="led {{'wait' if bot.us_wait else ('on' if bot.us else 'off')}}"></span><div style=flex:1><div class=bl>미국 자동매매</div><div class="bv {{'wait' if bot.us_wait else ('on' if bot.us else 'off')}}">{{ '환전 대기' if bot.us_wait else ('가동중' if bot.us else '정지') }}</div></div><span class=binfo>›</span></div>
  <div class=bstat onclick="openBot('dm')"><span class="led {{'on' if bot.deadman else 'off'}}"></span><div style=flex:1><div class=bl>감시장치</div><div class="bv {{'on' if bot.deadman else 'off'}}">{{ '켜짐' if bot.deadman else '꺼짐' }}</div></div><span class=binfo>›</span></div>
</div>
<div class=cap style="margin:-6px 4px 12px">궁금하면 눌러보세요 — 각 항목이 지금 왜 이 상태인지 알려드려요</div>

<div class=seg><div class="on" onclick="sw('kr')">🇰🇷 국내</div><div onclick="sw('us')">🇺🇸 미국</div></div>

<div class=grid>
<div><!-- 왼쪽: 계좌/포트폴리오/보유 -->
<div id=kr class="pane on">
{% if kr.error %}<div class="card warn">⚠️ {{kr.error}}</div>{% else %}
<div class="card hero"><div class=lab>총 자산</div><div class=amt>{{ '{:,.0f}'.format(kr.total) }}<small> 원</small></div>
  <span class="pill {{'up' if (kr.ret or 0)>=0 else 'down'}}">{{ '▲' if (kr.ret or 0)>=0 else '▼' }} {{ '%.2f'|format(kr.ret|abs) if kr.ret is not none else '—' }}% <span style=opacity:.5>·</span> {{ '{:+,.0f}'.format(kr.pl) }}원</span></div>
<div class=card><div class=lab style=margin-bottom:12px>포트폴리오 구성</div>
  <div class=donut><div class=dc><div class=pie style="background:conic-gradient({{kr.conic}})"></div>
    <div class=hole><div class=t1>보유</div><div class=t2>{{kr.holdings|length}}개</div></div></div>
    <div class=leg>{% for s in kr.alloc %}<div class=legrow><span class=dot style=background:{{s.color}}></span>
      <span class=ln>{{s.label}}<div class=lv>{{ '{:,.0f}'.format(s.val) }}원</div></span>
      <span class=lp>{{ '%.0f'|format(s.pct) }}%</span></div>{% endfor %}</div></div>
  {% if dca %}<div class=cap style="color:#ff9500;font-weight:600">📅 지수 DCA 진행중 · {{ '{:,.0f}'.format(dca.reserved/10000) }}만 예약 · 약 {{dca.months}}개월 분할 남음 <span class=mut style=font-weight:400>(의도적 지수 저비중 — 고점 몰빵 회피)</span></div>
  {% elif kr.alloc[0].pct < 40 %}<div class=cap>⚠️ 지수 비중 부족 · 현금 {{ '%.0f'|format(kr.alloc[2].pct) }}% 재배분 대기</div>{% endif %}</div>
<div class=card><div class=h style=margin-bottom:2px>보유 종목 {{kr.holdings|length}} <span class=mut style=font-weight:500;font-size:11px>· 종목 누르면 매수이유</span></div>
{% for h in kr.holdings %}<div class=hold onclick="openStock('{{h.ticker}}','{{h.name}}',{{h.qty}},{{h.buy}},{{h.price}},{{h.plpct}},{{'1' if h.is_etf else '0'}})">
  <div class="hicon {{'etf' if h.is_etf}}" {% if not h.is_etf %}style="background:linear-gradient(135deg,hsl({{h.hue}},62%,58%),hsl({{h.hue}},66%,47%))"{% endif %}>{{ '📊' if h.is_etf else h.name[:2] }}</div>
  <div class=hmid><div class=hnm>{{h.name}}</div>
    <div class=prices><span class=pb>매입 {{ '{:,.0f}'.format(h.buy) }}</span><span class="pc {{'up' if h.plpct>=0 else 'down'}}">현재 <b>{{ '{:,.0f}'.format(h.price) }}</b></span></div></div>
  <div class=hend><div class=hval>{{ '{:,.0f}'.format(h.value) }}</div><div class="hpl {{'up' if h.plpct>=0 else 'down'}}">{{ '%+.1f'|format(h.plpct) }}%</div></div>
  <span class=chev>›</span></div>{% endfor %}
{% if not kr.holdings %}<div class="mut" style=text-align:center;padding:16px>보유 종목 없음</div>{% endif %}</div>
{% endif %}</div>

<div id=us class=pane>
{% if us.error %}<div class="card warn">⚠️ {{us.error}}</div>{% else %}
<div class="card hero"><div class=lab>USD 예수금</div><div class=amt>${{ '%.2f'|format(us.cash_usd) }}</div>
<div class=cap style=margin-top:1px>달러로 환전해두면 봇이 자동으로 SPY(미국 대표지수 ETF)를 사요. 원화는 자동 환전되지 않아요.</div></div>
{% if us.holdings %}<div class=card>{% for h in us.holdings %}<div class=hold style=cursor:default>
<div class=hicon style=background:linear-gradient(135deg,#f04452,#d63a48)>{{h.ticker[:3]}}</div><div class=hmid><div class=hnm>{{h.ticker}}</div></div>
<div class=hend><div class=hval>{{ '%.4f'|format(h.qty) }}주</div></div></div>{% endfor %}</div>
{% else %}<div class="card mut" style=text-align:center;padding:24px>SPY 미보유<br><span style=font-size:12px>USD 환전 시 자동매수 대기</span></div>{% endif %}{% endif %}</div>
</div>

<div><!-- 오른쪽: AI / 자동화상세 / 거래 -->
<div class=card chat><div class=h style=margin-bottom:10px>💬 AI 어시스턴트 <span class=mut style=font-weight:500;font-size:11px>· 뭐든 물어보세요</span></div>
  <div class=msgs id=msgs><div class="m a">안녕하세요! 포트폴리오·전략에 대해 물어보세요. 예: "지금 수익률 어때?", "참고서가 뭐야?", "왜 현금이 많아?"</div></div>
  <div class=cin><input id=ci placeholder="메시지 입력..." onkeydown="if(event.key=='Enter')send()"><button onclick=send()>전송</button></div></div>

<div class=card><details><summary style="cursor:pointer;font-weight:800;font-size:15px;outline:none">⚙️ 자동화 상세 <span class=mut style=font-weight:500;font-size:11px>· 눌러서 펼치기</span></summary>
  <div style=margin-top:12px>
  <div class=st><span class=kk>리밸런스 상태</span><span class=vv>{{bot.rebal}}</span></div>
  <div class=st><span class=kk>참고서 필터</span><span class=vv>{{bot.artifact}}</span></div>
  <div class=st><span class=kk>감시 heartbeat</span><span class=vv>{{bot.heartbeat}}</span></div>
  <div class=cap>크론: 리밸 평일10:00 · 신규배분 평일10:30 · US 미국장 · deadman 매일</div></div></details></div>

<div class=card><div class=h style=margin-bottom:4px>📜 최근 거래</div>
  <details {{ 'open' if trades|length<=4 else '' }}><summary>{{trades|length}}건 보기</summary>
  {% for t in trades %}<div class=st><span><span class="tag {{'s' if t.action=='SELL' else 'b'}}">{{ '매도' if t.action=='SELL' else '매수' }}</span>{{t.stock_name}}</span>
  <span class=vv>{{ '{:,.0f}'.format(t.price) }} <span class=mut style=font-weight:500;font-size:11px>{{t.created_at[5:16]}}</span></span></div>{% endfor %}
  {% if not trades %}<div class="mut" style=text-align:center;padding:10px>거래 없음</div>{% endif %}</details></div>
</div>
</div>

<div class=foot>Lassi · 매매는 서버가 알아서 해요 — 이 화면은 구경만 하셔도 됩니다</div>
</div>

<div id=modal class=modal onclick="if(event.target==this)closeM()"><div class=sheet id=sheet></div></div>

<script>
var BOTD={{botd|tojson}};
function openBot(k){var d=BOTD[k];if(!d)return;var s=document.getElementById('sheet');
s.innerHTML='<h3>'+d.title+'</h3><div class=sub>'+d.sub+'</div>'+d.html+'<button class=mclose onclick=closeM()>닫기</button>';
document.getElementById('modal').classList.add('on');}
function sw(x){document.querySelectorAll('.seg div').forEach(t=>t.classList.remove('on'));
document.querySelectorAll('.pane').forEach(p=>p.classList.remove('on'));
document.getElementById(x).classList.add('on');event.currentTarget.classList.add('on');}
function closeM(){document.getElementById('modal').classList.remove('on');}
async function openStock(tk,nm,qty,buy,price,pl,etf){
var col=pl>=0?'#f04452':'#3182f6';
var s=document.getElementById('sheet');
s.innerHTML='<h3>'+nm+'</h3><div class=sub>'+tk+(etf==1?' · 지수 ETF':' · 저변동 선정')+'</div>'
+'<div class=mrow><span class=k>보유</span><span>'+qty+'주</span></div>'
+'<div class=mrow><span class=k>매입가</span><span>'+buy.toLocaleString()+'원</span></div>'
+'<div class=mrow><span class=k>현재가</span><span style="color:'+col+';font-weight:800">'+price.toLocaleString()+'원 ('+(pl>=0?'+':'')+pl.toFixed(1)+'%)</span></div>'
+'<div class=mrow><span class=k>평가액</span><span style=font-weight:800>'+(qty*price).toLocaleString()+'원</span></div>'
+'<div class=rsn id=rsn>불러오는 중…</div><button class=mclose onclick=closeM()>닫기</button>';
document.getElementById('modal').classList.add('on');
try{var r=await fetch('/api/stock/'+tk);var j=await r.json();
document.getElementById('rsn').innerHTML=j.html;}catch(e){document.getElementById('rsn').textContent='정보 로드 실패';}}
async function send(){var i=document.getElementById('ci'),m=document.getElementById('msgs'),v=i.value.trim();if(!v)return;
i.value='';m.innerHTML+='<div class="m u">'+v.replace(/</g,'&lt;')+'</div>';
var a=document.createElement('div');a.className='m a';a.textContent='…';m.appendChild(a);m.scrollTop=m.scrollHeight;
try{var r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:v})});
var j=await r.json();a.textContent=j.reply||'(응답 없음)';}catch(e){a.textContent='(오류)';}m.scrollTop=m.scrollHeight;}
</script></body></html>"""

LOGIN = """<!doctype html><html lang=ko><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Lassi 로그인</title><style>
*{box-sizing:border-box}
body{font-family:Pretendard,-apple-system,'Malgun Gothic',system-ui,sans-serif;background:linear-gradient(160deg,#eef1f6,#e0ebfa);color:#191f28;display:flex;height:100vh;align-items:center;justify-content:center;margin:0;letter-spacing:-.3px}
form{background:#fff;padding:36px 30px;border-radius:26px;width:320px;box-shadow:0 2px 6px rgba(23,32,64,.05),0 24px 60px rgba(0,25,80,.13)}
h2{margin:0 0 4px;font-size:26px;letter-spacing:-.8px} h2 span{color:#3182f6} .s{color:#8b95a1;font-size:13px;margin-bottom:20px}
input{width:100%;padding:14px;margin:6px 0;background:#f6f8fb;border:1.5px solid #eef1f5;color:#191f28;border-radius:13px;font-size:15px;transition:.15s}
input:focus{outline:none;border-color:#3182f6;background:#fff;box-shadow:0 0 0 3px rgba(49,130,246,.12)}
button{width:100%;padding:14px;background:#3182f6;color:#fff;border:0;border-radius:13px;margin-top:14px;cursor:pointer;font-weight:800;font-size:16px;transition:.15s}
button:hover{background:#2b74e0}
.e{color:#f04452;font-size:13px;margin-bottom:6px;font-weight:600}</style></head><body>
<form method=post><h2>Lassi<span>.</span></h2><div class=s>교과서 v3 + 참고서 · 자동매매</div>
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
    return render_template_string(
        PAGE, now=datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
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
    signed=False면 부호 없이 절대값 표시(변동성처럼 방향 없는 값)."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return (f'<div style="display:flex;align-items:center;gap:8px;margin:6px 0">'
                f'<span style="width:56px;font-size:12px;color:#8b95a1">{label}</span>'
                f'<span style="flex:1;height:7px;background:#e9edf2;border-radius:4px"></span>'
                f'<span style="width:58px;text-align:right;font-size:12px;color:#8b95a1">—</span></div>')
    w = min(abs(v) / scale, 1.0) * 100
    col = color or ('#3182f6' if v < 0 else '#f04452')
    val = f"{v*100:+.1f}%" if signed else f"{abs(v)*100:.1f}%"
    return (f'<div style="display:flex;align-items:center;gap:8px;margin:6px 0">'
            f'<span style="width:56px;font-size:12px;color:#8b95a1">{label}</span>'
            f'<span style="flex:1;height:7px;background:#e9edf2;border-radius:4px;overflow:hidden">'
            f'<span style="display:block;height:100%;width:{w:.0f}%;background:{col};border-radius:4px"></span></span>'
            f'<span style="width:58px;text-align:right;font-size:12px;font-weight:700;color:{col}">{val}</span></div>')


def _stock_reason(m):
    plabel, pdesc = _PAT.get(m.get('pattern', ''), ('· 패턴 정보 없음', ''))
    # 참고서 데이터 신뢰도 등급 + 평이한 설명
    tmap = {'clean': ('데이터 정상', '#00c473', '과거 시세에서 이상 신호 없음 — 믿을 만한 데이터'),
            'watch': ('검토 필요', '#ff9500', '이상 신호 1개(약한 의심) — 배제까진 아니고 참고 표시'),
            'confirmed': ('데이터 아티팩트 의심', '#f04452', '이상 신호 강함 — 데이터 왜곡 가능, 라이브에선 제외됨')}
    tlabel, tcol, tdesc = tmap.get(m.get('artifact_tier'), ('정보 없음', '#8b95a1', ''))
    best = (m.get('best_year') or '').replace(':', '년 ')
    worst = (m.get('worst_year') or '').replace(':', '년 ')
    yr = (f"<div style='font-size:12px;color:#8b95a1;margin-top:9px'>최고 {best or '—'} · 최악 {worst or '—'}</div>"
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
        "<div style='font-size:12px;color:#8b95a1'>이렇게 통과한 저변동 25종목을 같은 비중으로, "
        "사고팔기(타이밍)·손절 없이 분기 동안 보유합니다.</div>"
    )
    viz = (
        "<div style='margin-top:14px;padding-top:12px;border-top:1px solid #e4e9ef'>"
        "<div style='display:flex;align-items:center;justify-content:space-between;margin-bottom:3px'>"
        "<b>수익 패턴</b>"
        f"<span style='font-size:14px;font-weight:800'>{plabel}</span></div>"
        f"<div style='font-size:12px;color:#8b95a1;margin-bottom:9px'>{pdesc}</div>"
        f"<div style='font-size:11px;color:#aeb6bf;margin-bottom:9px;line-height:1.5'>※ 아래는 {span} <b>장기</b> 기록입니다. "
        f"위 '주가 안정적'은 <b>최근 6개월</b> 기준이라, 장기론 더 크게 출렁였을 수 있어요.</div>"
        + _bar('총수익', m.get('total_ret'), 2.0)
        + _bar('연수익', m.get('cagr'), 0.3)
        + _bar('최대낙폭', m.get('mdd'), 1.0, color='#3182f6')
        + _bar('변동성', m.get('ann_vol'), 0.8, color='#ff9500', signed=False)
        + yr
        + f"<div style='font-size:12px;margin-top:10px'>데이터 신뢰도 "
        f"<span class=mut style='font-weight:500'>· 참고서가 시세데이터 품질 평가</span>: "
        f"<b style='color:{tcol}'>{tlabel}</b></div>"
        + (f"<div style='font-size:11px;color:#8b95a1;margin-top:2px;line-height:1.5'>{tdesc}</div>" if tdesc else '')
        + "</div>"
    )
    return why + viz


@app.route('/api/stock/<ticker>')
@login_required
def api_stock(ticker):
    tk = str(ticker).zfill(6)
    m = _master().get(tk, {})
    if tk == '069500':
        html = ("<b>지수 슬리브 (코스피200)</b><br>KODEX200 = 코스피200 시총가중 ETF. "
                "폭등장에 함께 오르는 역할로 <b>포트폴리오의 50%</b>를 담당합니다. "
                "지수를 그대로 따라가, 저변동 종목들이 상승장에서 덜 오르는 약점을 메웁니다.")
    else:
        html = _stock_reason(m)
    return jsonify(html=html)


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
           f"참고서(데이터아티팩트·부실상폐 회피). US=SPY. 매매는 EC2 크론 자동.") if not kr['error'] else '계좌조회 실패'
    prompt = ("너는 Lassi 자동투자 대시보드 어시스턴트다. 아래 맥락으로 사용자 질문에 한국어로 간결·친근하게 답해라. "
              "매매 실행은 못 하고 설명·조언만 한다.\n\n[포트폴리오]\n" + ctx + "\n\n[질문]\n" + msg)
    return jsonify(reply=_gemini(key, prompt))


if __name__ == '__main__':
    try:
        init_db()
    except Exception:
        pass
    app.run(host='0.0.0.0', port=5000, debug=False)
