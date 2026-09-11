"""每晚 23:30 執行的融資融券資料補跑。

官方融資融券餘額通常要到晚上 21:00 左右才會產生，比每天 19:00 的主排程
（screener.py）晚，19:00 抓到的很可能還是昨天的舊資料。screener.py 在
執行時如果偵測到這個情況（見 margin_store.is_batch_stale），會先跳過
記錄，把當天的「融資背離(價漲資不增)」「融資連續下降次數」這兩個
Smart Money 因子留白（視為缺資料，權重轉給其他因子），不會用錯位的
舊資料硬算分數。

這支腳本在資料應該已經公布之後（23:30）重新抓一次，如果確認新鮮了，
就把它補進 data/margin_history.csv、重新算這兩個因子與總分，更新今天
的 output/picks_<日期>.csv 與 data/smart_money_log.csv，再重新產生
dashboard.html 並發布。

只補「資訊」，完全不會改變今天已經選出的股票名單——融資融券資料從頭
到尾只用在排序（Smart Money 分數）與報表呈現，不影響 stage1/stage2 的
入選條件。

用法：
    python margin_catchup.py

結束代碼：0 = 已更新；2 = 沒有東西需要補（今天沒有選股結果，或資料
仍然不新鮮）；1 = 發生錯誤。
"""
from __future__ import annotations

import sys

from datetime import datetime

import pandas as pd

import institutional_store
import smart_money
import smart_money_log
from data_sources import get_margin_balance
from margin_store import is_batch_stale, load_trend, record_snapshot
from screener import OUTPUT_DIR, PICKS_COLUMNS


def latest_picks_file():
    files = sorted(OUTPUT_DIR.glob("picks_*.csv"))
    return files[-1] if files else None


def main() -> int:
    picks_path = latest_picks_file()
    if picks_path is None:
        print("找不到 output/picks_*.csv，可能 screener.py 今天還沒執行過，跳過補跑。")
        return 2

    trade_date_str = picks_path.stem.replace("picks_", "")
    picks = pd.read_csv(picks_path, dtype={"code": str})
    if picks.empty:
        print(f"{picks_path.name} 是空的（今天沒有入選股票），沒有東西需要補。")
        return 2

    print(f"讀取 {picks_path.name}，共 {len(picks)} 檔，重新抓融資融券餘額...")
    margin_all = get_margin_balance()
    margin_picks = picks[["code"]].merge(margin_all, on="code", how="left")

    if is_batch_stale(margin_picks, trade_date_str):
        print("融資融券餘額看起來還是舊資料（可能今天官方也公布得特別晚），這次先不更新，維持缺資料狀態。")
        return 2

    n = record_snapshot(margin_picks, trade_date_str)
    print(f"已將本日融資餘額快照存入 data/margin_history.csv（{n} 檔證券）")

    print("重新計算融資相關的 Smart Money 因子與總分...")
    picks = picks.set_index("code")
    margin_picks = margin_picks.set_index("code")
    trading_calendar = {datetime.strptime(d, "%Y-%m-%d").date() for d in institutional_store.get_all_dates()}

    for code in picks.index:
        margin_balance = margin_picks.loc[code, "margin_balance"] if code in margin_picks.index else None
        margin_balance_prev = margin_picks.loc[code, "margin_balance_prev"] if code in margin_picks.index else None
        if pd.isna(margin_balance):
            margin_balance = None
        if pd.isna(margin_balance_prev):
            margin_balance_prev = None

        change, close = picks.loc[code, "change"], picks.loc[code, "close"]
        price_change_pct = None
        if pd.notna(change) and pd.notna(close) and (close - change):
            price_change_pct = change / (close - change)

        margin_trend = load_trend(code)
        margin_decline = smart_money.score_margin_decline_streak(margin_trend, trading_calendar)

        components = {
            "institutional_intensity": picks.loc[code, "sm_institutional_intensity"],
            "institutional_streak": picks.loc[code, "sm_institutional_streak"],
            "trust_momentum": picks.loc[code, "sm_trust_momentum"],
            "margin_divergence": smart_money.score_margin_divergence(
                margin_balance, margin_balance_prev, price_change_pct),
            "margin_decline_streak": margin_decline["score"],
            "big_holder_accumulation": picks.loc[code, "sm_big_holder_accumulation"],
            "retail_exit": picks.loc[code, "sm_retail_exit"],
            "volume_pullback_pattern": picks.loc[code, "sm_volume_pullback_pattern"],
        }
        # CSV 讀回來的缺資料是 NaN，aggregate() 要看到 None 才會正確判斷「這個因子沒資料」。
        components = {k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in components.items()}
        confidences = {"margin_decline_streak": margin_decline["confidence"]}

        result = smart_money.aggregate(components, confidences)
        picks.loc[code, "margin_balance"] = margin_balance
        picks.loc[code, "margin_balance_prev"] = margin_balance_prev
        picks.loc[code, "smart_money_score"] = result["score"]
        picks.loc[code, "smart_money_coverage_pct"] = result["coverage_pct"]
        picks.loc[code, "sm_margin_decline_confidence"] = margin_decline["confidence"]
        picks.loc[code, "sm_margin_decline_observation_days"] = margin_decline["trading_day_span"]
        for k, v in result["components"].items():
            picks.loc[code, f"sm_{k}"] = v

    picks = picks.reset_index()
    picks = picks.sort_values(["smart_money_score", "total_net"], ascending=[False, False], na_position="last")
    picks = picks.reindex(columns=PICKS_COLUMNS)
    picks.to_csv(picks_path, index=False, encoding="utf-8-sig")
    print(f"已更新 {picks_path.name}")

    smart_money_log.record_daily(trade_date_str, picks)
    print("已更新 data/smart_money_log.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
