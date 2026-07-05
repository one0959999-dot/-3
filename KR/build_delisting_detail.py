# -*- coding: utf-8 -*-
"""상폐 세부사유 분류 — '참고서' 2단계.

상폐 911종목(부실444/피인수325/자진142)을 재무제표(financials_dart: 자본총계·자본금·순이익)
+ 가격 궤적(final_30d·last_vs_peak)으로 세부 분류. 웹리서치 없이 데이터로 전수 분류.

부실상폐 세부사유:
  완전자본잠식 / 부분자본잠식 / 적자지속 / 급성붕괴(횡령·감사의견 추정) / 일반부실(재무불명)
+ 상폐 前 경고패턴(자본잠식 시작·적자연수) 추출 → 참고서 회피필터의 기준.

출력: lassi.db:delisting_detail + reference_data/delisting_detail.csv
실행: venv/Scripts/python KR/build_delisting_detail.py
"""
import os, sqlite3, pickle
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def P(f):
    return os.path.join(ROOT, f)


def classify():
    con = sqlite3.connect(P('lassi.db'))
    fin = pd.read_sql('SELECT ticker, year, capital, paidin, netincome FROM financials_dart', con)
    fin['ticker'] = fin['ticker'].astype(str).str.zfill(6)
    deli = pickle.load(open(P('data_cache_delisted.pkl'), 'rb'))
    name_map = dict(con.execute('SELECT ticker, name FROM kr_ticker_cache').fetchall())

    rows = []
    for tk, rec in deli.items():
        reason = rec.get('reason', '')
        last_date = rec.get('last_date')
        last_year = None
        try:
            last_year = int(str(last_date)[:4])
        except Exception:
            pass
        lvp = rec.get('last_vs_peak_pct')
        f30 = rec.get('final_30d_pct')

        f = fin[fin.ticker == tk]
        capital_impair = '재무없음'
        loss_years = None
        impair_first_year = None
        recent = pd.DataFrame()
        if len(f):
            fr = f[f.year <= (last_year or 9999)].sort_values('year')
            recent = fr.tail(3)
            if len(recent):
                cap, pi, ni = recent['capital'], recent['paidin'], recent['netincome']
                full = bool((cap <= 0).any())
                partial = bool(((cap > 0) & (cap < pi)).any())
                loss_years = int((ni < 0).sum())
                capital_impair = '완전자본잠식' if full else ('부분자본잠식' if partial else '정상')
                # 자본잠식(cap<paidin) 최초 연도
                imp = fr[fr.capital < fr.paidin]
                if len(imp):
                    impair_first_year = int(imp['year'].iloc[0])

        acute = (f30 is not None and f30 < -60)  # 마지막 30일 -60%↓ = 급성 붕괴

        if reason != '부실상폐':
            sub = reason  # 피인수/자진은 그대로(손실 아님)
        elif capital_impair == '완전자본잠식':
            sub = '완전자본잠식'
        elif capital_impair == '부분자본잠식':
            sub = '부분자본잠식'
        elif loss_years is not None and loss_years >= 2:
            sub = '적자지속'
        elif capital_impair == '정상' and acute:
            sub = '급성붕괴(정상재무→급락:횡령·감사의견 추정)'  # 재무 멀쩡한데 급락 = 진짜 급성사건
        elif capital_impair == '재무없음':
            sub = '소형·재무자료없음'  # DART 재무 없는 소형주(대부분 급성상폐)
        else:
            sub = '일반부실(재무불명)'

        rows.append(dict(
            ticker=tk, name=rec.get('name', name_map.get(tk, '')), reason=reason, sub_reason=sub,
            capital_impair=capital_impair, loss_years=loss_years, impair_first_year=impair_first_year,
            last_year=last_year, last_vs_peak_pct=lvp, final_30d_pct=f30, last_date=last_date,
        ))

    df = pd.DataFrame(rows)
    outdir = P('reference_data'); os.makedirs(outdir, exist_ok=True)
    df.to_csv(os.path.join(outdir, 'delisting_detail.csv'), index=False, encoding='utf-8-sig')
    df.to_sql('delisting_detail', con, if_exists='replace', index=False)
    con.commit(); con.close()
    return df


if __name__ == '__main__':
    df = classify()
    bad = df[df.reason == '부실상폐']
    print(f"저장: reference_data/delisting_detail.csv + lassi.db:delisting_detail ({len(df)}행)")
    print(f"\n===== 부실상폐 {len(bad)}개 세부사유 분류 =====")
    print(bad['sub_reason'].value_counts().to_string())
    print(f"\n자본잠식 상태별:")
    print(bad['capital_impair'].value_counts().to_string())
    print(f"\n[상폐 前 경고패턴] 부실상폐 중 자본잠식(cap<자본금) 이력: "
          f"{int((bad.capital_impair.isin(['완전자본잠식','부분자본잠식'])).sum())}/{len(bad)}개")
    print(f"[경고패턴] 적자 2년+ : {int((bad.loss_years.fillna(0)>=2).sum())}/{len(bad)}개")
    print(f"\n급성붕괴(횡령·감사의견 추정) 샘플:")
    ac = bad[bad.sub_reason.str.startswith('급성')].sort_values('final_30d_pct')
    print(ac[['ticker', 'name', 'final_30d_pct', 'last_vs_peak_pct', 'loss_years']].head(10).to_string(index=False))
