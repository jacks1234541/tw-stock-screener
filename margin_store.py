"""本地累積候選股的融資餘額，用來算「融資連續去化天數」這類需要多日
趨勢的 Smart Money 因子。

官方 openapi 的融資融券餘額只給「今日 vs 前一交易日」兩天，沒有更長的
歷史，所以要看連續好幾天的變化，只能靠我們自己每次執行時把當天
stage1 候選股（三大法人買超前 60 檔）的快照存進 data/margin_history.csv
慢慢累積——執行得越久，能抓到的連續天數就越長。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

HISTORY_PATH = Path(__file__).parent / "data" / "margin_history.csv"


def record_snapshot(candidates: pd.DataFrame, trade_date) -> int:
    """把這次 stage1 候選股的融資餘額，累積寫進本地歷史檔。

    同一個交易日重複執行只會覆蓋掉那天的舊資料，不會產生重複列
    （這樣「同一天重跑好幾次」結果才會一致）。回傳這次寫入的股票檔數。
    """
    if candidates.empty:
        return 0
    date_str = trade_date.strftime("%Y%m%d") if hasattr(trade_date, "strftime") else str(trade_date)

    snapshot = candidates[["code", "margin_balance"]].dropna(subset=["margin_balance"]).copy()
    if snapshot.empty:
        return 0
    snapshot.insert(0, "trade_date", date_str)

    HISTORY_PATH.parent.mkdir(exist_ok=True)
    if HISTORY_PATH.exists():
        existing = pd.read_csv(HISTORY_PATH, dtype={"code": str, "trade_date": str})
        existing = existing[existing["trade_date"] != date_str]
        combined = pd.concat([existing, snapshot], ignore_index=True)
    else:
        combined = snapshot

    combined = combined.sort_values(["trade_date", "code"])
    combined.to_csv(HISTORY_PATH, index=False, encoding="utf-8-sig")
    return len(snapshot)


def load_trend(code: str, max_days: int = 10) -> list[dict]:
    """讀出單一股票近期累積的融資餘額走勢，按日期由舊到新排序，只取最近 max_days 筆。"""
    if not HISTORY_PATH.exists():
        return []
    df = pd.read_csv(HISTORY_PATH, dtype={"code": str, "trade_date": str})
    rows = df[df["code"] == code].sort_values("trade_date").tail(max_days)
    return [
        {
            "date": r["trade_date"],
            "margin_balance": None if pd.isna(r["margin_balance"]) else float(r["margin_balance"]),
        }
        for _, r in rows.iterrows()
    ]
