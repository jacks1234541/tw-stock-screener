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


def is_batch_stale(candidates: pd.DataFrame, today_date_str: str) -> bool:
    """判斷這次剛抓到的融資融券餘額，是不是官方還沒更新今天的資料。

    官方融資融券餘額通常要到晚上 21:00 左右才會產生，比 19:00 的主排程
    晚，19:00 抓到的很可能還是舊資料。API 回應本身沒有日期欄位可以直接
    判斷新鮮度，所以改用比對的方式：拿這次回應裡的「前日餘額」，跟我們
    自己上一個交易日記錄的「今日餘額」比對——如果吻合，代表這批資料真的
    往前推進了一個交易日，是新鮮的；如果大多數股票都對不起來，很可能
    官方還沒更新，回傳的其實還是更早之前公布的舊資料。

    融資融券餘額是官方一次性公布全市場的單一批次檔案，不會只有部分股票
    更新，所以用「多數股票是否吻合」來判斷整批的新鮮度，避免單一檔股票
    剛好巧合而誤判。樣本數太少（例如候選股都是本地第一次看到、沒有歷史
    可以比對）時沒辦法判斷，保守當作「新鮮」處理，維持原本的行為。
    """
    checked, mismatched = 0, 0
    for _, row in candidates.iterrows():
        prev = row.get("margin_balance_prev")
        if prev is None or pd.isna(prev):
            continue
        # 排除「今天」自己的紀錄：如果今天稍早已經成功記錄過一次新鮮資料
        # （例如晚一點的補跑重複執行），要拿「前一個交易日」的紀錄來比對，
        # 不能拿今天自己比自己。
        trend = [t for t in load_trend(row["code"], max_days=6) if t["date"] != today_date_str]
        if not trend:
            continue
        last_recorded = trend[-1]["margin_balance"]
        if last_recorded is None:
            continue
        checked += 1
        if abs(float(prev) - last_recorded) > 1e-6:
            mismatched += 1

    if checked < 3:
        return False
    return mismatched / checked > 0.5
