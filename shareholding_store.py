"""本地累積集保股權分散週報，用來畫散戶指數的長期趨勢圖。

TDCC 的股權分散表官方只提供「最新一週」的快照，沒有查歷史的參數，
所以歷史趨勢只能靠我們自己每次執行時，把當週結果存進 data/shareholding_history.csv
慢慢累積出來——執行得越久，能看到的趨勢就越長。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

HISTORY_PATH = Path(__file__).parent / "data" / "shareholding_history.csv"


def all_shareholding_summaries(dist: pd.DataFrame) -> pd.DataFrame:
    """一次算出全市場所有證券代碼的散戶/大戶集中度摘要（比逐檔查詢快很多）。"""
    if dist.empty:
        return pd.DataFrame(columns=[
            "report_date", "code", "total_holders",
            "retail_holders_pct", "retail_shares_pct", "big_holder_shares_pct",
        ])

    total = dist[dist["level"] == "17"][["code", "holders", "report_date"]].rename(
        columns={"holders": "total_holders"})
    retail = dist[dist["level"] == "1"][["code", "holders", "pct"]].rename(
        columns={"holders": "retail_holders", "pct": "retail_shares_pct"})
    big = dist[dist["level"] == "15"][["code", "pct"]].rename(columns={"pct": "big_holder_shares_pct"})

    out = total.merge(retail, on="code", how="left").merge(big, on="code", how="left")
    out["retail_holders_pct"] = (out["retail_holders"] / out["total_holders"] * 100).round(2)
    out["retail_shares_pct"] = out["retail_shares_pct"].round(2)
    out["big_holder_shares_pct"] = out["big_holder_shares_pct"].round(2)
    return out[[
        "report_date", "code", "total_holders",
        "retail_holders_pct", "retail_shares_pct", "big_holder_shares_pct",
    ]]


def record_snapshot(dist: pd.DataFrame) -> int:
    """把這次抓到的股權分散資料，累積寫進本地歷史檔。

    同一個 report_date 重複執行只會覆蓋掉那一週的舊資料，不會產生重複列。
    回傳這次寫入的股票檔數。
    """
    summary = all_shareholding_summaries(dist)
    if summary.empty:
        return 0

    HISTORY_PATH.parent.mkdir(exist_ok=True)
    report_date = summary["report_date"].iloc[0]

    if HISTORY_PATH.exists():
        existing = pd.read_csv(HISTORY_PATH, dtype={"code": str, "report_date": str})
        existing = existing[existing["report_date"] != report_date]
        combined = pd.concat([existing, summary], ignore_index=True)
    else:
        combined = summary

    combined = combined.sort_values(["report_date", "code"])
    combined.to_csv(HISTORY_PATH, index=False, encoding="utf-8-sig")
    return len(summary)


def load_trend(code: str) -> list[dict]:
    """讀出單一股票歷來累積的散戶佔比走勢，按日期由舊到新排序。"""
    if not HISTORY_PATH.exists():
        return []
    df = pd.read_csv(HISTORY_PATH, dtype={"code": str, "report_date": str})
    rows = df[df["code"] == code].sort_values("report_date")
    return [
        {
            "date": f"{r['report_date'][0:4]}-{r['report_date'][4:6]}-{r['report_date'][6:8]}",
            "retail_shares_pct": None if pd.isna(r["retail_shares_pct"]) else float(r["retail_shares_pct"]),
            "big_holder_shares_pct": None if pd.isna(r["big_holder_shares_pct"]) else float(r["big_holder_shares_pct"]),
        }
        for _, r in rows.iterrows()
    ]
