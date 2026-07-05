# -*- coding: utf-8 -*-
"""참고서(reference) 레이어 — 교과서(v3 동결규칙)에 부가하는 데이터 기반 회피/참고 필터.

교과서 select()는 건드리지 않고, 유니버스(후보 종목풀)에 대해:
  (1) 데이터 아티팩트 종목 제외 (stock_master.artifact_tier='confirmed' — DH오토넥스류)
  (2) 상폐리스크 종목 회피 (financials_dart 기반 자본잠식·적자지속 = 부실상폐 경고패턴)
을 적용. 상폐 데이터(delisting_detail)에서 학습한 '부실상폐 = 자본잠식/적자' 패턴을 현 종목에 투영.

데이터 소스(모두 참고서 빌더 산출): lassi.db의 stock_master, delisting_detail, financials_dart.
테이블이 없으면 조용히 무필터(교과서 원본 동작) — 안전 폴백.
"""
import os
import csv as _csv
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def _dbpath():
    return os.path.join(ROOT, 'lassi.db')

def _master_csv():
    return os.path.join(ROOT, 'reference_data', 'stock_master.csv')

def _safe_connect():
    try:
        return sqlite3.connect(_dbpath())
    except Exception:
        return None


def _table_exists(con, name):
    r = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return r is not None


_ARTIFACT_CACHE = None


def artifact_tickers(con=None):
    """confirmed 아티팩트(데이터오류) 티커 집합.
    우선순위: 커밋된 CSV(reference_data/stock_master.csv — git으로 EC2에도 배포됨)
      → lassi.db:stock_master(로컬 개발) → 빈 집합(안전 폴백). 결과 캐시."""
    global _ARTIFACT_CACHE
    if _ARTIFACT_CACHE is not None:
        return _ARTIFACT_CACHE
    # 1) 커밋 CSV (이식성 — EC2 포함 어디서든 동작)
    try:
        path = _master_csv()
        if os.path.exists(path):
            out = set()
            with open(path, encoding='utf-8-sig') as f:
                for row in _csv.DictReader(f):
                    if row.get('artifact_tier') == 'confirmed':
                        out.add(str(row.get('ticker', '')).zfill(6))
            if out:
                _ARTIFACT_CACHE = out
                return out
    except Exception:
        pass
    # 2) DB 폴백 (연결 실패해도 크래시 없이 빈 집합)
    dbcon = con if con is not None else _safe_connect()
    if dbcon is not None:
        try:
            if _table_exists(dbcon, 'stock_master'):
                rows = dbcon.execute("SELECT ticker FROM stock_master WHERE artifact_tier='confirmed'").fetchall()
                _ARTIFACT_CACHE = {str(t[0]).zfill(6) for t in rows}
                return _ARTIFACT_CACHE
        except Exception:
            pass
        finally:
            if con is None:
                dbcon.close()
    _ARTIFACT_CACHE = set()
    return _ARTIFACT_CACHE


def delisting_risk(ticker, con=None):
    """현 종목의 상폐리스크 판정 (부실상폐 경고패턴 = 자본잠식/적자지속).
    반환 (level, reasons): level ∈ {'high','watch','ok','unknown'}."""
    ticker = str(ticker).zfill(6)
    own = con is None
    con = con or sqlite3.connect(_dbpath())
    try:
        if not _table_exists(con, 'financials_dart'):
            return ('unknown', [])
        rows = con.execute(
            "SELECT year, capital, paidin, netincome FROM financials_dart WHERE ticker=? ORDER BY year",
            (ticker,)).fetchall()
        if not rows:
            return ('unknown', [])
        recent = rows[-3:]
        reasons = []
        caps = [r[1] for r in recent if r[1] is not None]
        full = any(c <= 0 for c in caps)
        partial = any((r[1] is not None and r[2] is not None and 0 < r[1] < r[2]) for r in recent)
        loss_yrs = sum(1 for r in recent if r[3] is not None and r[3] < 0)
        if full:
            reasons.append('완전자본잠식')
        elif partial:
            reasons.append('부분자본잠식')
        if loss_yrs >= 2:
            reasons.append('적자2년+')
        if full or partial:
            return ('high', reasons)          # 자본잠식 = 부실상폐 최강 예고 → 회피
        if reasons:
            return ('watch', reasons)          # 적자지속만 = 주의
        return ('ok', [])
    finally:
        if own:
            con.close()


def refine_universe(tickers, drop_artifact=True, drop_delisting_risk=True, verbose=False):
    """교과서 유니버스에서 참고서 필터로 회피종목 제거.
    반환 (kept, dropped): dropped=[(ticker, 사유), ...]. 데이터 없으면 원본 반환(안전)."""
    arts = artifact_tickers() if drop_artifact else set()   # CSV 기반 — DB 없어도 동작
    con = _safe_connect() if drop_delisting_risk else None   # 상폐리스크만 DB 필요
    try:
        kept, dropped = [], []
        for t in tickers:
            t6 = str(t).zfill(6)
            if t6 in arts:
                dropped.append((t6, 'artifact'))
                continue
            if drop_delisting_risk and con is not None:
                try:
                    lvl, rs = delisting_risk(t6, con)
                except Exception:
                    lvl, rs = 'unknown', []
                if lvl == 'high':
                    dropped.append((t6, '상폐리스크:' + '·'.join(rs)))
                    continue
            kept.append(t)
        if verbose:
            print(f"[참고서] 유니버스 {len(tickers)} → {len(kept)} (제외 {len(dropped)}: "
                  f"아티팩트 {sum(1 for _,r in dropped if r=='artifact')}, "
                  f"상폐리스크 {sum(1 for _,r in dropped if r.startswith('상폐'))})")
        return kept, dropped
    finally:
        if con is not None:
            con.close()


if __name__ == '__main__':
    # 자체 점검 (배포 후 EC2 연동 확인용: python KR/reference.py)
    arts = artifact_tickers()
    src = 'CSV' if os.path.exists(_master_csv()) else 'DB/none'
    print(f"[1] 아티팩트(confirmed) 티커: {len(arts)}개 (출처={src}), 샘플 {list(arts)[:5]}")
    print("[2] 상폐리스크 판정:")
    for tk in ['005930', '000660', '196170']:
        print(f"    {tk}:", delisting_risk(tk))
    print("[3] 실 유니버스 필터 자체점검:")
    try:
        con = sqlite3.connect(_dbpath())
        tickers = [t for (t,) in con.execute('SELECT ticker FROM kr_ticker_cache')
                   if t and len(t) == 6 and t.isdigit()]
        con.close()
        kept, dropped = refine_universe(tickers, verbose=True)
        arts_dropped = sum(1 for _, r in dropped if r == 'artifact')
        ok = arts_dropped > 0
        print(f"    → {'✅ 연동정상' if ok else '⚠️ 아티팩트 미제외(데이터 확인 필요)'}: "
              f"{len(tickers)}종목 중 {len(dropped)}개 제외(아티팩트 {arts_dropped})")
    except Exception as e:
        print('    유니버스 점검 skip:', e)
