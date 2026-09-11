"""本地用 SQLite 累積每檔股票查過的日線資料（開高低收/成交量等），讓
get_stock_history() 之後同一檔股票只需要抓「本月」就好，不用每次都
把 N 個月的資料整批重抓一遍。

用 SQLite 而不是像 shareholding_history.csv / margin_history.csv 那樣
用單一 CSV，是因為這裡的查詢型態不一樣：stage2 要在一個迴圈裡對 60 檔
候選股各自查「這檔股票有哪些月份本地已經有資料」，用 SQLite 的索引查詢
比每次重讀、重解析一份只會越長越大的 CSV 快很多；而且 sqlite3 是
Python 內建模組，不需要額外安裝套件。
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).parent / "data" / "price_history.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, change REAL,
            volume REAL, amount REAL, transactions REAL,
            PRIMARY KEY (code, date)
        )
    """)
    return conn


def get_covered_months(code: str) -> set[date]:
    """回傳這檔股票本地已經有資料的月份（每個月只要存過至少一天就算涵蓋）。

    只代表「這個月查過、拿到至少一些資料」，不保證那個月每個交易日都
    存到了——如果某次執行遇到限流只抓到部分天數，之後也不會自動回頭
    補洞，這是刻意的取捨（見 data_sources.get_stock_history 的說明）。
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT substr(date, 1, 7) FROM price_history WHERE code = ?", (code,)
        ).fetchall()
    finally:
        conn.close()
    months = set()
    for (ym,) in rows:
        y, m = ym.split("-")
        months.add(date(int(y), int(m), 1))
    return months


def upsert_daily_rows(code: str, df: pd.DataFrame) -> int:
    """把新抓到的日線資料存進本地資料庫。

    同一個 (code, date) 重複寫入只會覆蓋掉那一天的舊資料，不會產生
    重複列，所以同一天重跑好幾次結果會一致。
    """
    if df.empty:
        return 0
    conn = _connect()
    try:
        rows = [
            (
                code,
                r["date"].isoformat() if hasattr(r["date"], "isoformat") else str(r["date"]),
                r.get("open"), r.get("high"), r.get("low"), r.get("close"), r.get("change"),
                r.get("volume"), r.get("amount"), r.get("transactions"),
            )
            for r in df.to_dict("records")
        ]
        conn.executemany(
            """INSERT OR REPLACE INTO price_history
               (code, date, open, high, low, close, change, volume, amount, transactions)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def get_all_codes() -> list[str]:
    """回傳本地資料庫裡曾經抓過股價的所有股票代碼，給補洞腳本用。"""
    conn = _connect()
    try:
        rows = conn.execute("SELECT DISTINCT code FROM price_history").fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def get_stored_day_counts() -> dict[tuple[str, str], int]:
    """回傳每個 (股票代碼, 年-月) 組合本地存了幾個交易日，給補洞腳本判斷缺口用。"""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT code, substr(date, 1, 7) AS ym, COUNT(*) FROM price_history GROUP BY code, ym"
        ).fetchall()
    finally:
        conn.close()
    return {(code, ym): count for code, ym, count in rows}


def load_history(code: str, months: int = 4) -> pd.DataFrame:
    """讀出本地累積的日線資料，只取最近 months 個月份範圍內的部分。"""
    cursor = date.today().replace(day=1)
    for _ in range(months - 1):
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    cutoff = cursor.isoformat()

    conn = _connect()
    try:
        df = pd.read_sql_query(
            "SELECT * FROM price_history WHERE code = ? AND date >= ? ORDER BY date",
            conn, params=(code, cutoff),
        )
    finally:
        conn.close()
    if df.empty:
        return df
    df = df.drop(columns=["code"])
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df.reset_index(drop=True)
