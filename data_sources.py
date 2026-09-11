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

import institutional_store
import price_store

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


def get_monthly_revenue_snapshot() -> pd.DataFrame:
    """全上市公司最新一個月的營收（含官方算好的月增率/年增率）。

    這是「最新一個月」的單次快照，沒有查歷史月份的參數；近幾季的走勢
    要靠 revenue_store.py 每次執行時把這個月的快照存下來慢慢累積。
    """
    data = _get_json_with_retry(f"{TWSE_OPENAPI}/opendata/t187ap05_L", params={})
    cols = ["code", "year_month", "revenue", "revenue_mom_pct", "revenue_yoy_pct"]
    if not data:
        return pd.DataFrame(columns=cols)

    df = pd.DataFrame(data)
    df = df[df["公司代號"].str.match(_ORDINARY_STOCK_CODE)]
    out = pd.DataFrame({
        "code": df["公司代號"],
        "year_month": df["資料年月"],
        "revenue": df["營業收入-當月營收"].map(_to_number),
        "revenue_mom_pct": df["營業收入-上月比較增減(%)"].map(_to_number),
        "revenue_yoy_pct": df["營業收入-去年同月增減(%)"].map(_to_number),
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

    只呼叫一次「全市場單日」端點就能取得當天所有股票的資料，所以不論要
    追蹤幾檔股票，理論上的 API 呼叫次數都固定等於交易日數、不會隨股票數
    增加；但這個函式常常一次執行內就被呼叫兩次（screener.py 算 Smart
    Money 分數一次、build_dashboard.py 畫圖表又一次），而且「查過的日期」
    每天執行都會整批重查一遍。

    本地用 institutional_store.py（SQLite）依日期累積全市場資料：某個
    日期只要之前任何一次執行成功抓過，就永久沿用、不再重查（三大法人
    公布後的歷史資料不會再修改，不像股價需要對「本月」特別處理）。還沒
    抓到資料的日期（假日、或當天資料還沒公布）會一直維持「未快取」，
    之後執行時自然會再嘗試。
    """
    d = date.today()
    for _ in range(calendar_days):
        if not institutional_store.is_date_cached(d):
            df = get_institutional_net(d)
            if not df.empty:
                institutional_store.store_date(d, df)
            time.sleep(0.4)  # 只有真的打了 API 才需要對官方主機客氣一點
        d -= timedelta(days=1)
    return institutional_store.load_history(codes, calendar_days)


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


def get_stock_history(stock_no: str, months: int = 4, coverage_passes: int = 2) -> pd.DataFrame:
    """單一股票近 N 個月的日線資料（技術指標計算用）。

    本地用 price_store.py（SQLite）累積每次抓到的資料：只有「本月」每次
    都需要重抓（因為本月每天都會多一天新資料出來），更早的月份只要本地
    已經有資料就直接沿用、不再打 API。這把每檔股票平常的 API 呼叫量從
    固定 N 次砍到通常只剩 1 次，同時大幅降低被限流、產生「缺月份」的
    機率——也讓同一次執行內（例如 screener.py 抓過的股票，build_dashboard.py
    接著處理同一批股票）不會為了同一批資料重複打兩次 API。

    如果某檔股票是本地第一次查、或本地累積的月份還不夠 N 個月，那些
    缺的月份還是得照樣整批抓，行為跟以前一樣：如果第一輪抓完還有月份
    沒拿到，會再等一小段時間、針對「還缺的月份」單獨重試一次
    （coverage_passes=2）。這裡刻意不做太多輪、每輪等待也不長：如果真的
    遇到大範圍限流或官方主機封鎖（例如 WAF 擋下所有請求），重試太多輪
    會讓單一檔股票卡上好幾分鐘，60 檔候選股疊加起來可能拖成好幾小時。
    真正需要「同一天重跑結果要一致」這種可靠度時，交給呼叫端
    （screener.py 的 stage2_technical_filter）用全域時間預算來控制，而
    不是在這裡無限重試。

    取捨：本地某個月份只要存過「至少一天」就視為「這個月涵蓋了」，不會
    每次都去檢查那個月是不是每個交易日都存到——如果某次執行遇到限流、
    某個歷史月份只抓到一部分天數，之後也不會自動回頭補洞，資料完整度
    只會隨著之後每天執行往前滾動變好，不會反過來修補久遠的舊缺口。
    """
    month_cursors = []
    cursor = date.today().replace(day=1)
    for _ in range(months):
        month_cursors.append(cursor)
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    current_month = month_cursors[0]

    covered = price_store.get_covered_months(stock_no)
    pending = [m for m in month_cursors if m == current_month or m not in covered]

    frames_by_month: dict[date, pd.DataFrame] = {}

    for pass_num in range(coverage_passes):
        if not pending:
            break
        if pass_num > 0:
            print(f"    {stock_no} 還有 {len(pending)} 個月份沒抓到，等待後重試（第 {pass_num + 1} 輪）...")
            time.sleep(8)

        still_pending = []
        for m in pending:
            date_str = m.strftime("%Y%m%d")
            payload = _get_json_with_retry(
                f"{TWSE_RWD}/afterTrading/STOCK_DAY",
                params={"date": date_str, "stockNo": stock_no, "response": "json"},
            )
            if payload and payload.get("stat") == "OK" and payload.get("data"):
                frames_by_month[m] = pd.DataFrame(payload["data"], columns=payload["fields"])
            else:
                still_pending.append(m)
            time.sleep(0.6)  # 對官方主機客氣一點，避免觸發限流
        pending = still_pending

    if pending:
        print(f"    警告：{stock_no} 有 {len(pending)} 個月份的資料最終仍抓不到，這次歷史資料會有缺口")

    if frames_by_month:
        fetched = pd.concat(frames_by_month.values(), ignore_index=True)
        fetched = fetched.rename(columns={
            "日期": "date", "成交股數": "volume", "成交金額": "amount",
            "開盤價": "open", "最高價": "high", "最低價": "low",
            "收盤價": "close", "漲跌價差": "change", "成交筆數": "transactions",
        })
        fetched["date"] = fetched["date"].map(_roc_to_date)
        for col in ["volume", "amount", "open", "high", "low", "close", "transactions"]:
            fetched[col] = fetched[col].map(_to_number)
        fetched = fetched.dropna(subset=["close"])
        price_store.upsert_daily_rows(stock_no, fetched)

    return price_store.load_history(stock_no, months=months)
