"""把 screener.py 選出的股票，組成一份給互動式儀表板用的 JSON 資料，
並產生一個可以直接發布/開啟的獨立 dashboard.html。

用法：
    python screener.py          # 先產生今天的選股結果
    python build_dashboard.py   # 再產生視覺化儀表板
"""
from __future__ import annotations

import json
from pathlib import Path
from time import monotonic

import pandas as pd

from data_sources import (
    get_institutional_history,
    get_monthly_revenue_snapshot,
    get_shareholding_distribution,
    get_stock_history,
    shareholding_summary,
)
from indicators import macd as macd_indicator
from indicators import moving_average, rsi
from product_info import get_product_info
from revenue_store import load_trend as load_revenue_trend
from revenue_store import record_snapshot as record_revenue_snapshot
from shareholding_store import load_trend, record_snapshot

MOPS_INVESTOR_CONFERENCE_URL = "https://mops.twse.com.tw/mops/web/t100sb02_1"

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
TEMPLATE_PATH = BASE_DIR / "dashboard_template.html"
DASHBOARD_PATH = BASE_DIR / "dashboard.html"

DISPLAY_DAYS = 60          # 圖表顯示近幾個交易日的K線
CHIP_HISTORY_DAYS = 45     # 三大法人趨勢往回抓幾個「日曆天」
HISTORY_FETCH_TIME_BUDGET_SECONDS = 600  # 超過就對剩下的股票停止額外重試（見 screener.py 同名常數的說明）


def latest_picks_file() -> Path:
    files = sorted(OUTPUT_DIR.glob("picks_*.csv"))
    if not files:
        raise SystemExit("找不到 output/picks_*.csv，請先執行 screener.py")
    return files[-1]


def _num(x):
    """把 numpy/pandas 數值轉成原生 Python float，NaN 轉成 None，讓 json.dumps 能處理。"""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    try:
        if pd.isna(x):
            return None
    except (TypeError, ValueError):
        pass
    return float(x)


def build_stock_payload(row: pd.Series, coverage_passes: int = 2) -> dict:
    code, name = row["code"], row["name"]
    print(f"抓取 {code} {name} 歷史日線...")
    hist = get_stock_history(code, months=4, coverage_passes=coverage_passes)
    if hist.empty:
        print(f"  警告：{code} {name} 抓不到歷史日線資料，已略過（可能是暫時性限流，可重跑試試）")
        return None

    close = hist["close"]
    ma5 = moving_average(close, 5)
    ma20 = moving_average(close, 20)
    ma60 = moving_average(close, 60)
    rsi14 = rsi(close, 14)
    macd_line, macd_signal, macd_hist = macd_indicator(close)

    hist = hist.tail(DISPLAY_DAYS).reset_index(drop=True)
    idx = hist.index

    def series_to_list(s: pd.Series) -> list:
        s = s.tail(DISPLAY_DAYS).reset_index(drop=True)
        return [_num(v) for v in s]

    candles = [
        {
            "date": d.isoformat(),
            "open": _num(r["open"]), "high": _num(r["high"]), "low": _num(r["low"]),
            "close": _num(r["close"]),
            "volume": int(r["volume"]) if pd.notna(r["volume"]) else 0,
        }
        for d, r in zip(hist["date"], hist.to_dict("records"))
    ]

    return {
        "code": code,
        "name": name,
        "snapshot": {
            "close": _num(row["close"]),
            "ma5": _num(row["ma5"]), "ma20": _num(row["ma20"]), "ma60": _num(row["ma60"]),
            "rsi14": _num(row["rsi14"]), "macd_hist": _num(row["macd_hist"]),
            "foreign_net": _num(row["foreign_net"]), "trust_net": _num(row["trust_net"]),
            "dealer_net": _num(row["dealer_net"]), "total_net": _num(row["total_net"]),
            "signal": row.get("signal") or "",
            "neckline": _num(row.get("neckline")),
            "breakout_vol_ratio": _num(row.get("breakout_vol_ratio")),
        },
        "candles": candles,
        "series": {
            "ma5": series_to_list(ma5),
            "ma20": series_to_list(ma20),
            "ma60": series_to_list(ma60),
            "rsi14": series_to_list(rsi14),
            "macd_hist": series_to_list(macd_hist),
        },
        "chip_history": [],  # 之後填入
        "shareholding": None,  # 之後填入
        "product_info": get_product_info(code),
        "investor_conference_url": MOPS_INVESTOR_CONFERENCE_URL,
        "revenue_trend": [],  # 之後填入
        "smart_money": {
            "score": _num(row.get("smart_money_score")),
            "coverage_pct": _num(row.get("smart_money_coverage_pct")),
            "components": {
                "institutional_intensity": _num(row.get("sm_institutional_intensity")),
                "institutional_streak": _num(row.get("sm_institutional_streak")),
                "trust_momentum": _num(row.get("sm_trust_momentum")),
                "margin_divergence": _num(row.get("sm_margin_divergence")),
                "margin_deleveraging_streak": _num(row.get("sm_margin_deleveraging_streak")),
                "big_holder_accumulation": _num(row.get("sm_big_holder_accumulation")),
                "retail_exit": _num(row.get("sm_retail_exit")),
                "volume_pullback_pattern": _num(row.get("sm_volume_pullback_pattern")),
            },
        },
    }


def main():
    picks_path = latest_picks_file()
    trade_date = picks_path.stem.replace("picks_", "")
    picks = pd.read_csv(picks_path, dtype={"code": str})
    print(f"讀取 {picks_path.name}，共 {len(picks)} 檔候選股票\n")

    stocks = []
    fetch_start = monotonic()
    budget_exceeded_announced = False
    for _, row in picks.iterrows():
        over_budget = (monotonic() - fetch_start) > HISTORY_FETCH_TIME_BUDGET_SECONDS
        if over_budget and not budget_exceeded_announced:
            print(f"  時間預算（{HISTORY_FETCH_TIME_BUDGET_SECONDS}秒）已用完，"
                  f"剩下的股票改用快速模式（不做缺月份重試）")
            budget_exceeded_announced = True
        payload = build_stock_payload(row, coverage_passes=1 if over_budget else 2)
        if payload:
            stocks.append(payload)

    print(f"\n抓取三大法人近 {CHIP_HISTORY_DAYS} 天趨勢...")
    codes = [s["code"] for s in stocks]
    chip_hist = get_institutional_history(codes, calendar_days=CHIP_HISTORY_DAYS)
    for s in stocks:
        s["chip_history"] = chip_hist.get(s["code"], [])

    print("抓取每月營收快照...")
    revenue = get_monthly_revenue_snapshot()
    n_revenue = record_revenue_snapshot(revenue)
    print(f"已將本月營收快照存入 data/revenue_history.csv（{n_revenue} 檔證券）")
    for s in stocks:
        s["revenue_trend"] = load_revenue_trend(s["code"])

    print("抓取集保戶股權分散表...")
    dist = get_shareholding_distribution()

    n_recorded = record_snapshot(dist)
    print(f"已將本週股權分散快照存入 data/shareholding_history.csv（{n_recorded} 檔證券）")

    for s in stocks:
        summary = shareholding_summary(dist, s["code"])
        if summary:
            rd = str(summary["report_date"])
            s["shareholding"] = {
                "report_date": f"{rd[0:4]}-{rd[4:6]}-{rd[6:8]}" if len(rd) == 8 else rd,
                "total_holders": _num(summary["total_holders"]),
                "retail_holders_pct": _num(summary["retail_holders_pct"]),
                "retail_shares_pct": _num(summary["retail_shares_pct"]),
                "big_holder_shares_pct": _num(summary["big_holder_shares_pct"]),
                "brackets": [
                    {
                        "level": b["level"], "label": b["label"],
                        "holders": _num(b["holders"]),
                        "shares": _num(b["shares"]),
                        "pct": _num(b["pct"]),
                    }
                    for b in summary["brackets"]
                ],
                "trend": load_trend(s["code"]),
            }

    data = {"trade_date": trade_date, "stocks": stocks}

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    html = template.replace(
        "/*__DASHBOARD_DATA__*/",
        json.dumps(data, ensure_ascii=False),
    )
    DASHBOARD_PATH.write_text(html, encoding="utf-8")
    print(f"\n完成！已產生 {DASHBOARD_PATH}")


if __name__ == "__main__":
    main()
