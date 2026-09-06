"""台灣證券交易所（TWSE）公開資料存取層。

只用官方公開、不需要金鑰的端點：
- openapi.twse.com.tw ：全市場單日快照（收盤行情 / 融資融券餘額）
- www.twse.com.tw/rwd ：三大法人買賣超（依日期查詢）、個股歷史日線（依月份查詢）
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import pandas as pd
import requests

TWSE_OPENAPI = "https://openapi.twse.com.tw/v1"
TWSE_RWD = "https://www.twse.com.tw/rwd/zh"
TDCC_OPENAPI = "https://openapi.tdcc.com.tw/v1/opendata"

# 集保戶股權分散表：第1級距(1-999股)是散戶指標常用的級距；第17級距是合計。
# 第16級距官方未清楚定義（推測是集保與過戶登記時點落差造成的調整數），標示為「調整數」。
SHAREHOLDING_LEVEL_LABELS = {
    "1": "1-999股", "2": "1,000-5,000股", "3": "5,001-10,000股",
    "4": "10,001-15,000股", "5": "15,001-20,000股", "6": "20,001-30,000股",
    "7": "30,001-40,000股", "8": "40,001-50,000股", "9": "50,001-100,000股",
    "10": "100,001-200,000股", "11": "200,001-400,000股", "12": "400,001-600,000股",
    "13": "600,001-800,000股", "14": "800,001-1,000,000股", "15": "1,000,001股以上",
    "16": "調整數", "17": "合計",
}

HEADERS = {"User-Agent": "Mozilla/5.0"}

# 只保留一般普通股代號（4 碼、非 0 開頭），排除 ETF(00xxx)、權證等
_ORDINARY_STOCK_CODE = r"^[1-9]\d{3}$"


def _to_number(x):
    if x is None:
        return None
    if isinstance(x, str):
        x = x.replace(",", "").strip()
        if x in ("", "--"):
            return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _roc_to_date(s: str) -> date:
    y, m, d = s.split("/")
    return date(int(y) + 1911, int(m), int(d))


def get_all_daily_price() -> pd.DataFrame:
    """全上市股票最近一個交易日的收盤行情。"""
    data = _get_json_with_retry(f"{TWSE_OPENAPI}/exchangeReport/STOCK_DAY_ALL", params={})
    df = pd.DataFrame(data or [])
    df = df[df["Code"].str.match(_ORDINARY_STOCK_CODE)]
    df = df.rename(columns={
        "Code": "code", "Name": "name", "TradeVolume": "volume",
        "OpeningPrice": "open", "HighestPrice": "high", "LowestPrice": "low",
        "ClosingPrice": "close", "Change": "change", "Transaction": "transactions",
    })
    for col in ["volume", "open", "high", "low", "close", "change", "transactions"]:
        df[col] = df[col].map(_to_number)
    df["name"] = df["name"].str.strip()
    return df[["code", "name", "volume", "open", "high", "low", "close", "change", "transactions"]]


def get_institutional_net(trade_date: date) -> pd.DataFrame:
    """指定交易日的三大法人買賣超股數（該日非交易日則回傳空表）。"""
    date_str = trade_date.strftime("%Y%m%d")
    payload = _get_json_with_retry(
        f"{TWSE_RWD}/fund/T86",
        params={"date": date_str, "selectType": "ALL", "response": "json"},
    )
    cols = ["code", "name", "foreign_net", "trust_net", "dealer_net", "total_net"]
    if not payload or payload.get("stat") != "OK" or not payload.get("data"):
        return pd.DataFrame(columns=cols)

    df = pd.DataFrame(payload["data"], columns=payload["fields"])
    df = df[df["證券代號"].str.match(_ORDINARY_STOCK_CODE)]
    out = pd.DataFrame({
        "code": df["證券代號"],
        "name": df["證券名稱"].str.strip(),
        "foreign_net": df["外陸資買賣超股數(不含外資自營商)"].map(_to_number),
        "trust_net": df["投信買賣超股數"].map(_to_number),
        "dealer_net": df["自營商買賣超股數"].map(_to_number),
        "total_net": df["三大法人買賣超股數"].map(_to_number),
    })
    return out.reset_index(drop=True)


def get_margin_balance() -> pd.DataFrame:
    """全上市股票融資融券餘額（最近一個交易日快照）。"""
    data = _get_json_with_retry(f"{TWSE_OPENAPI}/exchangeReport/MI_MARGN", params={})
    df = pd.DataFrame(data or [])
    df = df[df["股票代號"].str.match(_ORDINARY_STOCK_CODE)]
    out = pd.DataFrame({
        "code": df["股票代號"],
        "margin_balance": df["融資今日餘額"].map(_to_number),
        "margin_balance_prev": df["融資前日餘額"].map(_to_number),
    })
    return out.reset_index(drop=True)


def find_latest_trading_date(max_lookback: int = 7) -> date:
    """從今天往回找，回傳最近一個有三大法人資料（代表有開盤）的日期。"""
    d = date.today()
    for _ in range(max_lookback):
        if not get_institutional_net(d).empty:
            return d
        d -= timedelta(days=1)
    raise RuntimeError("找不到最近的交易日資料，請檢查網路連線或稍後再試")


def _get_json_with_retry(url: str, params: dict, retries: int = 3, backoff: float = 2.0) -> dict | None:
    """呼叫官方 API 並在遇到限流/暫時性錯誤時重試，最終仍失敗則回傳 None。"""
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=20)
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.RequestException, ValueError):
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
            else:
                return None
    return None


def get_institutional_history(codes: list[str], calendar_days: int = 45) -> dict[str, list[dict]]:
    """回傳指定股票在近 calendar_days 天內、每個實際交易日的三大法人買賣超。

    只呼叫一次「全市場單日」端點就能取得當天所有股票的資料，
    所以不論要追蹤幾檔股票，API 呼叫次數都固定等於交易日數，不會隨股票數增加。
    """
    codes = set(codes)
    result: dict[str, list[dict]] = {c: [] for c in codes}
    d = date.today()
    for _ in range(calendar_days):
        df = get_institutional_net(d)
        if not df.empty:
            day_rows = df[df["code"].isin(codes)]
            for _, row in day_rows.iterrows():
                result[row["code"]].append({
                    "date": d.isoformat(),
                    "foreign_net": _to_number(row["foreign_net"]),
                    "trust_net": _to_number(row["trust_net"]),
                    "dealer_net": _to_number(row["dealer_net"]),
                    "total_net": _to_number(row["total_net"]),
                })
        d -= timedelta(days=1)
        time.sleep(0.4)
    for c in result:
        result[c].sort(key=lambda r: r["date"])
    return result


def get_shareholding_distribution() -> pd.DataFrame:
    """集保戶股權分散表（全市場單次快照，每週更新，涵蓋所有集保有價證券）。"""
    data = _get_json_with_retry(f"{TDCC_OPENAPI}/1-5", params={}, retries=2)
    cols = ["code", "level", "holders", "shares", "pct", "report_date"]
    if not data:
        return pd.DataFrame(columns=cols)

    date_key = next((k for k in data[0] if k.endswith("資料日期")), "資料日期")
    rows = [{
        "code": d["證券代號"].strip(),
        "level": d["持股分級"],
        "holders": _to_number(d["人數"]),
        "shares": _to_number(d["股數"]),
        "pct": _to_number(d["占集保庫存數比例%"]),
        "report_date": d.get(date_key),
    } for d in data]
    return pd.DataFrame(rows, columns=cols)


def shareholding_summary(dist: pd.DataFrame, code: str) -> dict | None:
    """算出單一股票的散戶佔比 / 大股東集中度摘要，以及完整級距明細（供圖表使用）。"""
    rows = dist[dist["code"] == code]
    if rows.empty:
        return None
    total_row = rows[rows["level"] == "17"]
    if total_row.empty:
        return None
    total_holders = total_row.iloc[0]["holders"]
    retail_row = rows[rows["level"] == "1"]
    big_row = rows[rows["level"] == "15"]
    retail_holders = retail_row.iloc[0]["holders"] if not retail_row.empty else None

    brackets = rows[rows["level"] != "17"].copy()
    brackets["level_int"] = brackets["level"].astype(int)
    brackets = brackets.sort_values("level_int")

    return {
        "report_date": total_row.iloc[0]["report_date"],
        "total_holders": total_holders,
        "retail_holders_pct": (
            round(retail_holders / total_holders * 100, 2)
            if retail_holders is not None and total_holders else None
        ),
        "retail_shares_pct": retail_row.iloc[0]["pct"] if not retail_row.empty else None,
        "big_holder_shares_pct": big_row.iloc[0]["pct"] if not big_row.empty else None,
        "brackets": [
            {
                "level": r["level"],
                "label": SHAREHOLDING_LEVEL_LABELS.get(r["level"], r["level"]),
                "holders": r["holders"],
                "shares": r["shares"],
                "pct": r["pct"],
            }
            for _, r in brackets.iterrows()
        ],
    }


def get_stock_history(stock_no: str, months: int = 4) -> pd.DataFrame:
    """單一股票近 N 個月的日線資料（技術指標計算用）。"""
    frames = []
    cursor = date.today().replace(day=1)
    for _ in range(months):
        date_str = cursor.strftime("%Y%m%d")
        payload = _get_json_with_retry(
            f"{TWSE_RWD}/afterTrading/STOCK_DAY",
            params={"date": date_str, "stockNo": stock_no, "response": "json"},
        )
        if payload and payload.get("stat") == "OK" and payload.get("data"):
            frames.append(pd.DataFrame(payload["data"], columns=payload["fields"]))
        cursor = (cursor - timedelta(days=1)).replace(day=1)
        time.sleep(0.6)  # 對官方主機客氣一點，避免觸發限流

    if not frames:
        return pd.DataFrame()

    hist = pd.concat(frames, ignore_index=True)
    hist = hist.rename(columns={
        "日期": "date", "成交股數": "volume", "成交金額": "amount",
        "開盤價": "open", "最高價": "high", "最低價": "low",
        "收盤價": "close", "漲跌價差": "change", "成交筆數": "transactions",
    })
    hist["date"] = hist["date"].map(_roc_to_date)
    for col in ["volume", "amount", "open", "high", "low", "close", "transactions"]:
        hist[col] = hist[col].map(_to_number)
    hist = hist.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    return hist
