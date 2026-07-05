# -*- coding: utf-8 -*-
"""전 종목 정량 마스터 데이터 빌더 — '참고서' 1단계.

생존(data_cache_kr_full) + 상폐(data_cache_delisted) 전수 종목에 대해:
  수익률/CAGR/MDD/변동성/거래대금/섹터/상폐여부·사유 + 데이터 아티팩트 플래그
    (거래량0 점프 · 정수배 점프 · 플랫라인 · 일일제한 초과)
를 산출해 lassi.db 테이블 `stock_master` + reference_data/stock_master.csv 로 저장.

교과서(v3 동결규칙)는 그대로 두고, 이 데이터를 참고서 레이어로 쌓기 위한 뼈대.
실행: venv/Scripts/python KR/build_stock_master.py
"""
import os, sqlite3
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def P(f):
    return os.path.join(ROOT, f)

KR_LIMIT = 0.30  # 한국 일일 가격제한폭 ±30%


def max_true_run(mask_int):
    s = pd.Series(mask_int)
    if s.sum() == 0:
        return 0
    grp = (s != s.shift()).cumsum()
    runs = s.groupby(grp).sum()
    return int(runs.max())


def analyze(close, volume=None):
    close = pd.Series(close).dropna().astype(float)
    close = close[close > 0]
    if len(close) < 30:
        return None
    close = close.sort_index()
    ret = close.pct_change(fill_method=None)
    total_ret = close.iloc[-1] / close.iloc[0] - 1.0
    days = (close.index[-1] - close.index[0]).days or 1
    yrs = max(days / 365.25, 0.1)
    cagr = (close.iloc[-1] / close.iloc[0]) ** (1 / yrs) - 1.0
    eq = close / close.iloc[0]
    mdd = float((eq / eq.cummax() - 1).min())
    ann_vol = float(ret.std() * np.sqrt(252))

    # 연도별 수익률(급등 시점 파악)
    yearly = close.resample('YE').last().pct_change(fill_method=None).dropna()
    best_year = f"{yearly.idxmax().year}:{yearly.max()*100:.0f}%" if len(yearly) else ""
    worst_year = f"{yearly.idxmin().year}:{yearly.min()*100:.0f}%" if len(yearly) else ""

    # ── 아티팩트 플래그 ──
    absret = ret.abs()
    n_over_limit = int((absret > KR_LIMIT + 0.02).sum())        # 일일제한(+여유2%p) 초과 = 비정상
    mult = (1 + ret).dropna()
    intmask = (mult >= 1.9) & (np.abs(mult - mult.round()) < 0.02) & (mult.round() >= 2)
    n_intmult = int(intmask.sum())                              # 정수배(2~9x) 점프
    maxflat = max_true_run((close.diff() == 0).astype(int).values)  # 최장 플랫라인

    zero_vol_pct = None
    jump_zero_vol = 0
    med_value = None
    if volume is not None:
        v = pd.Series(volume).reindex(close.index)
        zero_vol_pct = float((v == 0).mean())
        jump_zero_vol = int(((absret > KR_LIMIT + 0.02) & (v == 0)).sum())  # 점프인데 거래량0 = 강한 아티팩트
        val = (close * v).dropna()
        med_value = float(val.median()) if len(val) else None

    # 등급제: confirmed(거의 확실한 데이터오류) / watch(단일신호·검토필요) / clean
    strong = (jump_zero_vol >= 1) or (n_intmult >= 2) or (n_over_limit >= 5)
    weak = (not strong) and (
        n_over_limit >= 1 or n_intmult >= 1 or (maxflat > 120 and abs(total_ret) > 1.0)
    )
    artifact_tier = 'confirmed' if strong else ('watch' if weak else 'clean')
    artifact = strong  # 백테스트 제외 대상 = confirmed만

    # 수익 패턴 분류(자동, 코스): 아티팩트 / 급등스파이크 / 완만성장 / 하락 / 횡보
    if artifact:
        pattern = 'artifact_confirmed'
    elif total_ret <= -0.5:
        pattern = 'decline'
    elif len(yearly) and yearly.max() > 1.0 and (yearly > 1.0).sum() <= 2:
        pattern = 'spike'          # 특정 1~2년에 2배+ 집중
    elif cagr > 0.10 and mdd > -0.5:
        pattern = 'steady_grower'
    else:
        pattern = 'sideways'

    return dict(
        total_ret=round(total_ret, 4), cagr=round(cagr, 4), mdd=round(mdd, 4),
        ann_vol=round(ann_vol, 4), best_year=best_year, worst_year=worst_year,
        med_daily_value=med_value, n_days=len(close),
        first_date=close.index[0].date().isoformat(), last_date=close.index[-1].date().isoformat(),
        n_over_limit=n_over_limit, n_intmult=n_intmult, jump_zero_vol=jump_zero_vol,
        zero_vol_pct=(round(zero_vol_pct, 3) if zero_vol_pct is not None else None),
        max_flat_run=maxflat, artifact_suspect=artifact, artifact_tier=artifact_tier, pattern=pattern,
    )


def build():
    import pickle
    con = sqlite3.connect(P('lassi.db'))

    # 섹터/시장/이름 맵
    name_map = dict(con.execute('SELECT ticker, name FROM kr_ticker_cache').fetchall())

    def load_kv(table):
        try:
            cols = [r[1] for r in con.execute(f'PRAGMA table_info({table})').fetchall()]
        except Exception:
            return {}
        if not cols:
            return {}
        tcol = next((c for c in cols if c.lower() in ('ticker', 'code', 'symbol')), cols[0])
        vcol = next((c for c in cols if c != tcol and c.lower() not in ('id',)), None)
        if vcol is None:
            return {}
        out = {}
        for t, v in con.execute(f'SELECT {tcol}, {vcol} FROM {table}'):
            if t is not None:
                out[str(t).zfill(6) if str(t).isdigit() else str(t)] = v
        return out

    sector_map = load_kv('ticker_sector_dart') or load_kv('ticker_sector')
    market_map = load_kv('ticker_market_dart')
    print(f"[maps] names={len(name_map)} sector={len(sector_map)} market={len(market_map)}")

    rows = []

    # ── 생존 종목 ──
    surv = pickle.load(open(P('data_cache_kr_full.pkl'), 'rb'))
    print(f"[survivors] {len(surv)} 종목 분석...")
    for tk, dfv in surv.items():
        try:
            if not isinstance(dfv, pd.DataFrame) or 'close' not in dfv.columns:
                continue
            dfv = dfv.copy(); dfv.index = pd.to_datetime(dfv.index)
            a = analyze(dfv['close'], dfv['volume'] if 'volume' in dfv.columns else None)
            if a is None:
                continue
            a.update(ticker=tk, name=name_map.get(tk, ''), sector=sector_map.get(tk, ''),
                     market=market_map.get(tk, ''), status='survivor',
                     delist_reason='', last_vs_peak_pct=None, final_30d_pct=None)
            rows.append(a)
        except Exception:
            continue

    # ── 상폐 종목 ──
    deli = pickle.load(open(P('data_cache_delisted.pkl'), 'rb'))
    print(f"[delisted] {len(deli)} 종목 분석...")
    for tk, rec in deli.items():
        try:
            close = rec.get('close')
            if close is None:
                continue
            close = pd.Series(close); close.index = pd.to_datetime(close.index)
            a = analyze(close, None)
            if a is None:
                a = dict(total_ret=None, cagr=None, mdd=None, ann_vol=None, best_year='', worst_year='',
                         med_daily_value=None, n_days=len(close), first_date='', last_date='',
                         n_over_limit=0, n_intmult=0, jump_zero_vol=0, zero_vol_pct=None,
                         max_flat_run=0, artifact_suspect=False, artifact_tier='clean', pattern='delisted')
            a.update(ticker=tk, name=rec.get('name', name_map.get(tk, '')),
                     sector=sector_map.get(tk, ''), market=market_map.get(tk, ''),
                     status='delisted', delist_reason=rec.get('reason', ''),
                     last_vs_peak_pct=rec.get('last_vs_peak_pct'), final_30d_pct=rec.get('final_30d_pct'))
            rows.append(a)
        except Exception:
            continue

    df = pd.DataFrame(rows)
    cols = ['ticker', 'name', 'sector', 'market', 'status', 'delist_reason',
            'total_ret', 'cagr', 'mdd', 'ann_vol', 'best_year', 'worst_year',
            'med_daily_value', 'pattern', 'artifact_tier', 'artifact_suspect',
            'n_over_limit', 'n_intmult', 'jump_zero_vol', 'zero_vol_pct', 'max_flat_run',
            'last_vs_peak_pct', 'final_30d_pct', 'n_days', 'first_date', 'last_date']
    df = df[[c for c in cols if c in df.columns]]

    outdir = P('reference_data'); os.makedirs(outdir, exist_ok=True)
    csv_path = os.path.join(outdir, 'stock_master.csv')
    df.to_csv(csv_path, index=False, encoding='utf-8-sig')
    df.to_sql('stock_master', con, if_exists='replace', index=False)
    con.commit(); con.close()

    print(f"\n저장: {csv_path}  +  lassi.db:stock_master  ({len(df)} 행)")
    return df


if __name__ == '__main__':
    df = build()
    print("\n===== 요약 =====")
    print("상태별:", df['status'].value_counts().to_dict())
    print("아티팩트 등급별:", df['artifact_tier'].value_counts().to_dict())
    print("  → confirmed(백테스트 제외대상):", int((df.artifact_tier == 'confirmed').sum()), "종목")
    print("패턴별:", df['pattern'].value_counts().to_dict())
    print("\n상폐 사유별:")
    print(df[df.status == 'delisted']['delist_reason'].value_counts().to_string())
    print("\n아티팩트 confirmed 상위(수익률순) — 백테스트 제외 대상:")
    art = df[df.artifact_tier == 'confirmed'].sort_values('total_ret', ascending=False)
    print(art[['ticker', 'name', 'total_ret', 'n_over_limit', 'n_intmult', 'jump_zero_vol', 'max_flat_run']].head(15).to_string(index=False))
