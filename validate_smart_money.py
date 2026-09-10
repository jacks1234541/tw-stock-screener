"""前向驗證 Smart Money Detector 的分數有沒有用。

讀取 data/smart_money_log.csv 累積的歷史分數，對每一筆「某天、某檔股票
的分數」去抓它「之後 N 個交易日」的實際收盤價，算出之後報酬，再看分數
高低跟之後報酬有沒有正相關（Spearman 等級相關）。

這不是正式回測（沒有拿歷史資料重建過去某天的分數），純粹是「今天算出
來的分數，未來真的有沒有用」的前向驗證，資料要隨著每日排程累積到有
足夠時間跨度後才有意義——建議至少讓排程連續跑 3-4 週以上再執行這支
腳本看結果，跑太早樣本數不足只會印出「樣本數不足」。

用法：
    python validate_smart_money.py [--horizon 5,10,20]
"""
from __future__ import annotations

import argparse
from datetime import datetime

import pandas as pd

from data_sources import get_stock_history
from smart_money_log import load_all


def _forward_return(code: str, from_date: str, horizon: int) -> float | None:
    months = max(2, horizon // 15 + 2)
    hist = get_stock_history(code, months=months, coverage_passes=1)
    if hist.empty:
        return None
    hist = hist.sort_values("date").reset_index(drop=True)
    from_d = datetime.strptime(from_date, "%Y%m%d").date()
    before = hist[hist["date"] <= from_d]
    after = hist[hist["date"] > from_d]
    if before.empty or len(after) < horizon:
        return None
    base_close = before["close"].iloc[-1]
    target_close = after["close"].iloc[horizon - 1]
    if not base_close:
        return None
    return (target_close - base_close) / base_close * 100


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--horizon", default="5,10,20", help="之後幾個交易日的報酬，逗號分隔")
    args = parser.parse_args()
    horizons = [int(h) for h in args.horizon.split(",")]

    log = load_all()
    log = log.dropna(subset=["smart_money_score"])
    if log.empty:
        print("data/smart_money_log.csv 裡還沒有可用的分數資料，請先讓每日排程多跑幾天/幾週再回來執行這支腳本。")
        return

    print(f"累積樣本：{len(log)} 筆（{log['trade_date'].nunique()} 個交易日、{log['code'].nunique()} 檔股票）\n")

    for horizon in horizons:
        rows = []
        for _, r in log.iterrows():
            ret = _forward_return(r["code"], r["trade_date"], horizon)
            if ret is not None:
                rows.append({"code": r["code"], "trade_date": r["trade_date"],
                             "score": r["smart_money_score"], "return": ret})

        if len(rows) < 10:
            print(f"[之後 {horizon} 個交易日的報酬] 目前可比對的樣本數只有 {len(rows)} 筆，"
                  f"還不足以看出趨勢，之後累積更多資料再跑一次。\n")
            continue

        df = pd.DataFrame(rows)
        ic = df["score"].corr(df["return"], method="spearman")
        try:
            df["bucket"] = pd.qcut(df["score"], q=min(4, df["score"].nunique()), duplicates="drop")
            bucket_avg = df.groupby("bucket", observed=True)["return"].agg(["mean", "count"])
        except ValueError:
            bucket_avg = None

        print(f"[之後 {horizon} 個交易日的報酬]  樣本數={len(df)}  Spearman IC={ic:.3f}")
        print("  （IC 為正代表分數愈高、之後報酬也傾向愈高；一般認為 |IC| > 0.05 才算有微弱訊號）")
        if bucket_avg is not None:
            print("  分數分組後的平均報酬（分組愈高分越應該報酬愈好，才代表分數有效）：")
            print(bucket_avg.to_string())
        print()


if __name__ == "__main__":
    main()
