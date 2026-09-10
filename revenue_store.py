"""本地累積每月營收快照，用來畫「近四季」營收年增/月增趨勢圖。

TWSE 的月營收 OpenAPI 只給「最新一個月」的快照，沒有查歷史月份的參數，
所以近四季（近12個月）的走勢只能靠我們自己每次執行時，把當月結果存進
data/revenue_history.csv 慢慢累積出來——執行得越久，能看到的季數就越長。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

HISTORY_PATH = Path(__file__).parent / "data" / "revenue_history.csv"


def record_snapshot(revenue: pd.DataFrame) -> int:
    """把這次抓到的月營收快照，累積寫進本地歷史檔。

    同一個 year_month 重複執行只會覆蓋掉那個月的舊資料，不會產生重複列。
    回傳這次寫入的公司家數。
    """
    if revenue.empty:
        return 0

    HISTORY_PATH.parent.mkdir(exist_ok=True)
    year_month = revenue["year_month"].iloc[0]

    if HISTORY_PATH.exists():
        existing = pd.read_csv(HISTORY_PATH, dtype={"code": str, "year_month": str})
        existing = existing[existing["year_month"] != year_month]
        combined = pd.concat([existing, revenue], ignore_index=True)
    else:
        combined = revenue

    combined = combined.sort_values(["year_month", "code"])
    combined.to_csv(HISTORY_PATH, index=False, encoding="utf-8-sig")
    return len(revenue)


def _roc_yyyymm_to_iso(yyyymm: str) -> str:
    """轉成 YYYY-MM-01（補一個假的日期，方便前端沿用既有的日期格式化函式）。"""
    yyyymm = str(yyyymm)
    roc_year, month = int(yyyymm[:-2]), int(yyyymm[-2:])
    return f"{roc_year + 1911:04d}-{month:02d}-01"


def load_trend(code: str) -> list[dict]:
    """讀出單一股票歷來累積的月營收年增/月增走勢，按月份由舊到新排序。"""
    if not HISTORY_PATH.exists():
        return []
    df = pd.read_csv(HISTORY_PATH, dtype={"code": str, "year_month": str})
    rows = df[df["code"] == code].sort_values("year_month")
    return [
        {
            "month": _roc_yyyymm_to_iso(r["year_month"]),
            "revenue": None if pd.isna(r["revenue"]) else float(r["revenue"]),
            "mom_pct": None if pd.isna(r["revenue_mom_pct"]) else float(r["revenue_mom_pct"]),
            "yoy_pct": None if pd.isna(r["revenue_yoy_pct"]) else float(r["revenue_yoy_pct"]),
        }
        for _, r in rows.iterrows()
    ]
