"""本地用 SQLite 依日期累積三大法人買賣超（全市場），讓
get_institutional_history() 之後同一個日期不用每次執行都重查一遍。

跟股價快取（price_store.py）不同的地方：三大法人資料一旦公布就不會再
修改，所以「某個日期查過了」可以永久沿用，不需要像股價那樣特別對
「本月」做例外重抓。還沒抓到資料的日期（假日、或當天資料還沒公布）
會一直維持「未快取」狀態，之後執行時自然會再嘗試。

T86 端點本來就是「一次呼叫抓全市場當天所有股票」，所以這裡存的也是
全市場，不只存呼叫當下要的那幾檔——這樣不管之後哪次執行要追蹤哪些
股票，只要日期本地有資料就能直接用，不用管上次是誰查的。
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).parent / "data" / "institutional_history.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS institutional_net (
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            foreign_net REAL, trust_net REAL, dealer_net REAL, total_net REAL,
            PRIMARY KEY (code, date)
        )
    """)
    return conn


def is_date_cached(d: date) -> bool:
    """這個日期本地是不是已經有全市場的三大法人資料了。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM institutional_net WHERE date = ? LIMIT 1", (d.isoformat(),)
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def store_date(d: date, df: pd.DataFrame) -> int:
    """把某一天全市場的三大法人買賣超存進本地資料庫。"""
    if df.empty:
        return 0
    conn = _connect()
    try:
        date_str = d.isoformat()
        rows = [
            (r["code"], date_str, r.get("foreign_net"), r.get("trust_net"),
             r.get("dealer_net"), r.get("total_net"))
            for r in df.to_dict("records")
        ]
        conn.executemany(
            """INSERT OR REPLACE INTO institutional_net
               (code, date, foreign_net, trust_net, dealer_net, total_net)
               VALUES (?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def load_history(codes: list[str], calendar_days: int = 45) -> dict[str, list[dict]]:
    """讀出指定股票在近 calendar_days 天內、本地已經有的三大法人買賣超，按日期由舊到新排序。"""
    codes = list(dict.fromkeys(codes))
    result: dict[str, list[dict]] = {c: [] for c in codes}
    if not codes:
        return result

    cutoff = (date.today() - timedelta(days=calendar_days)).isoformat()
    conn = _connect()
    try:
        placeholders = ",".join("?" * len(codes))
        rows = conn.execute(
            f"""SELECT code, date, foreign_net, trust_net, dealer_net, total_net
                FROM institutional_net
                WHERE date >= ? AND code IN ({placeholders})
                ORDER BY date""",
            [cutoff, *codes],
        ).fetchall()
    finally:
        conn.close()

    for code, d, foreign_net, trust_net, dealer_net, total_net in rows:
        result[code].append({
            "date": d, "foreign_net": foreign_net, "trust_net": trust_net,
            "dealer_net": dealer_net, "total_net": total_net,
        })
    return result
