"""台股每日選股策略：三大法人籌碼認養 + 技術面訊號（均線多頭排列 或 盤整後頸線突破）。

策略邏輯（預設參數，可自行調整下方常數）：
  第一階段（籌碼面粗篩，全市場單次 API 呼叫）：
    - 收盤價 >= MIN_CLOSE_PRICE、成交量 >= MIN_VOLUME（排除雞蛋水餃股/流動性太差）
    - 三大法人今日合計買超 > 0
    - 依買超張數排序，取前 CHIP_SHORTLIST_SIZE 檔進入第二階段
  第二階段（技術面複核，只對候選股個別抓歷史日線）：只要符合下列任一訊號就入選
    訊號A「均線多頭」：
      - 收盤價 > MA5 > MA20 > MA60（均線多頭排列）
      - RSI_LOW <= RSI14 <= RSI_HIGH（避免超賣或超買）
      - MACD 柱狀圖 > 0（動能偏多）
    訊號B「頸線突破」：
      - 今日以前 CONSOLIDATION_WINDOW 天的收盤價波動幅度 <= MAX_CONSOLIDATION_RANGE_PCT%（視為盤整）
      - 頸線 = 盤整區間內最高價；今日收盤站上頸線、昨日收盤還在頸線之下（今天才突破）
      - 今日成交量 > 20日均量 * VOLUME_SURGE_RATIO（量能確認）
    兩訊號皆符合的股票會同時標註在 signal 欄位。

  第三階段（Smart Money 疑似建倉分數，見 smart_money.py）：
    不改變上面兩階段決定的入選名單，只對已經選出的候選股額外算一個
    0-100 分的排序依據，並把結果排在 CSV 最前面。分數綜合三大法人
    買超強度/連續性、投信動能、融資背離與連續去化、集保大戶增碼、
    散戶退場、量縮拉回不破均線等 8 個因子；因子涉及的多日趨勢（融資、
    集保）需要跑過一段時間累積本地歷史檔才會愈算愈準，詳見
    smart_money.py 開頭的說明。

用法：
    python screener.py

執行後於 output/picks_YYYYMMDD.csv 產生結果，並印在終端機。
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from time import monotonic

import pandas as pd

from data_sources import (
    find_latest_trading_date,
    get_all_daily_price,
    get_institutional_history,
    get_institutional_net,
    get_margin_balance,
    get_shareholding_distribution,
    get_stock_history,
    shareholding_summary,
)
import institutional_store
from indicators import detect_neckline_breakout
from indicators import macd as macd_indicator
from indicators import moving_average, rsi
from margin_store import is_batch_stale as margin_is_batch_stale
from margin_store import load_trend as load_margin_trend
from margin_store import record_snapshot as record_margin_snapshot
import smart_money
import smart_money_log
from shareholding_store import load_trend as load_shareholding_trend
from shareholding_store import record_snapshot as record_shareholding_snapshot

OUTPUT_DIR = Path(__file__).parent / "output"

# ---- 篩選參數：之後想調整策略就改這裡 ----
MIN_CLOSE_PRICE = 10
MIN_VOLUME = 500_000
CHIP_SHORTLIST_SIZE = 60
RSI_LOW, RSI_HIGH = 45, 75

# 訊號B：盤整後頸線突破
CONSOLIDATION_WINDOW = 20          # 盤整區間天數（不含今天）
MAX_CONSOLIDATION_RANGE_PCT = 15   # 盤整區間允許的最大收盤價波動幅度(%)
VOLUME_SURGE_RATIO = 1.5           # 今日成交量門檻 = 20日均量 * 此倍數

# 全域時間預算：stage2 抓歷史日線最多花這麼多秒，超過就對剩下的股票停止
# 額外的「缺月份重試」（只抓一輪，抓多少算多少），避免遇到大範圍限流/官方
# 主機封鎖時，60 檔候選股疊加重試時間拖成好幾小時。
HISTORY_FETCH_TIME_BUDGET_SECONDS = 900  # 15 分鐘


def stage1_chip_filter(trade_date) -> pd.DataFrame:
    print(f"[1/4] 抓取 {trade_date} 全市場收盤行情...")
    price = get_all_daily_price()

    print(f"[1/4] 抓取 {trade_date} 三大法人買賣超...")
    chip = get_institutional_net(trade_date)

    print("[1/4] 抓取融資融券餘額...")
    margin = get_margin_balance()

    df = price.merge(chip[["code", "foreign_net", "trust_net", "dealer_net", "total_net"]], on="code")
    df = df.merge(margin, on="code", how="left")

    df = df[(df["close"] >= MIN_CLOSE_PRICE) & (df["volume"] >= MIN_VOLUME)]
    df = df[df["total_net"] > 0]
    df = df.sort_values("total_net", ascending=False).head(CHIP_SHORTLIST_SIZE)
    return df.reset_index(drop=True)


STAGE2_COLUMNS = [
    "code", "name", "close", "ma5", "ma20", "ma60", "rsi14", "macd_hist",
    "signal", "neckline", "breakout_vol_ratio",
    "foreign_net", "trust_net", "dealer_net", "total_net",
    "volume", "change", "margin_balance", "margin_balance_prev",
    "smart_money_f1", "smart_money_f8",
]
PICKS_COLUMNS = STAGE2_COLUMNS + [
    "retail_holders_pct", "retail_shares_pct", "big_holder_shares_pct",
    "smart_money_score", "smart_money_coverage_pct",
    "sm_institutional_intensity", "sm_institutional_streak", "sm_trust_momentum",
    "sm_margin_divergence", "sm_margin_decline_streak", "sm_big_holder_accumulation",
    "sm_retail_exit", "sm_volume_pullback_pattern",
    # margin_decline_streak 的可信度/觀察細節，供除錯與之後前向驗證用，
    # 不是加權平均會用到的核心欄位（見 smart_money.score_margin_decline_streak 說明）。
    "sm_margin_decline_confidence", "sm_margin_decline_observation_days",
]


def stage2_technical_filter(candidates: pd.DataFrame) -> pd.DataFrame:
    rows = []
    total = len(candidates)
    start = monotonic()
    budget_exceeded_announced = False
    for i, row in candidates.iterrows():
        code, name = row["code"], row["name"]
        print(f"[2/4] ({i + 1}/{total}) 抓取 {code} {name} 歷史日線並計算技術指標...")

        over_budget = (monotonic() - start) > HISTORY_FETCH_TIME_BUDGET_SECONDS
        if over_budget and not budget_exceeded_announced:
            print(f"  時間預算（{HISTORY_FETCH_TIME_BUDGET_SECONDS}秒）已用完，"
                  f"剩下的股票改用快速模式（不做缺月份重試），避免大範圍限流時拖太久")
            budget_exceeded_announced = True
        hist = get_stock_history(code, months=4, coverage_passes=1 if over_budget else 2)
        time.sleep(0.5)  # 換下一檔股票前稍微停頓，避免被官方主機限流
        if len(hist) < 60:
            continue  # 資料不足以算 MA60，跳過

        close = hist["close"]
        ma5 = moving_average(close, 5).iloc[-1]
        ma20 = moving_average(close, 20).iloc[-1]
        ma60 = moving_average(close, 60).iloc[-1]
        rsi14 = rsi(close, 14).iloc[-1]
        _, _, hist_macd = macd_indicator(close)
        macd_hist = hist_macd.iloc[-1]
        last_close = close.iloc[-1]

        bullish_alignment = last_close > ma5 > ma20 > ma60
        rsi_ok = RSI_LOW <= rsi14 <= RSI_HIGH
        macd_ok = macd_hist > 0
        signal_trend = bullish_alignment and rsi_ok and macd_ok

        breakout = detect_neckline_breakout(
            hist, CONSOLIDATION_WINDOW, MAX_CONSOLIDATION_RANGE_PCT, VOLUME_SURGE_RATIO,
        )
        signal_breakout = breakout.get("is_breakout", False)

        if not (signal_trend or signal_breakout):
            continue

        signal_labels = []
        if signal_trend:
            signal_labels.append("均線多頭")
        if signal_breakout:
            signal_labels.append("頸線突破")

        rows.append({
            "code": code, "name": name, "close": last_close,
            "ma5": round(ma5, 2), "ma20": round(ma20, 2), "ma60": round(ma60, 2),
            "rsi14": round(rsi14, 1), "macd_hist": round(macd_hist, 2),
            "signal": "+".join(signal_labels),
            "neckline": breakout.get("neckline"),
            "breakout_vol_ratio": breakout.get("vol_ratio"),
            "foreign_net": row["foreign_net"], "trust_net": row["trust_net"],
            "dealer_net": row["dealer_net"], "total_net": row["total_net"],
            "volume": row["volume"], "change": row.get("change"),
            "margin_balance": row.get("margin_balance"), "margin_balance_prev": row.get("margin_balance_prev"),
            # Smart Money 因子中，這兩個用得到的原始資料（今日法人買超/成交量、
            # 本檔股票的歷史日線）這裡剛好都還在手上，先算好子分數存起來，
            # 避免之後（main() 裡）為了算分又重抓一次歷史日線浪費 API 呼叫。
            "smart_money_f1": smart_money.score_institutional_intensity(row["total_net"], row["volume"]),
            "smart_money_f8": smart_money.score_volume_pullback_pattern(hist),
        })
    return pd.DataFrame(rows, columns=STAGE2_COLUMNS)


def main():
    trade_date = find_latest_trading_date()
    print(f"最近交易日：{trade_date}\n")

    candidates = stage1_chip_filter(trade_date)
    print(f"\n[1/4] 籌碼面初篩留下 {len(candidates)} 檔，進入技術面複核...\n")

    # 官方融資融券餘額通常晚上 21:00 左右才會產生，比 19:00 的主排程晚，
    # 這個時間點抓到的很可能還是舊資料。偵測到還沒更新的話，這次就不記錄
    # 也不拿來算分（讓相關的兩個 Smart Money 因子當作缺資料、權重轉給
    # 其他因子），避免把錯位的舊資料當成今天的數字用。晚上 23:30 另外有
    # 一個補跑（margin_catchup.py）會在資料應該已經公布後重新抓一次、
    # 補上這兩個因子並更新報表，見該檔案開頭的說明。
    trade_date_str = trade_date.strftime("%Y%m%d")
    if margin_is_batch_stale(candidates, trade_date_str):
        print("融資融券餘額看起來還沒更新（官方通常晚上21:00左右才公布，19:00執行可能太早），"
              "本次先不記錄、相關 Smart Money 因子當天先留白，晚上 11:30 的補跑會自動補上。")
        candidates = candidates.assign(margin_balance=None, margin_balance_prev=None)
    else:
        n_margin = record_margin_snapshot(candidates, trade_date)
        if n_margin:
            print(f"已將本日融資餘額快照存入 data/margin_history.csv（{n_margin} 檔證券）")

    picks = stage2_technical_filter(candidates)

    if not picks.empty:
        print("\n[3/4] 抓取集保戶股權分散表，計算散戶佔比...")
        dist = get_shareholding_distribution()
        n_recorded = record_shareholding_snapshot(dist)
        if n_recorded:
            print(f"已將本週股權分散快照存入 data/shareholding_history.csv（{n_recorded} 檔證券）")
        shareholding_rows = []
        for code in picks["code"]:
            summary = shareholding_summary(dist, code)
            shareholding_rows.append({
                "code": code,
                "retail_holders_pct": summary["retail_holders_pct"] if summary else None,
                "retail_shares_pct": summary["retail_shares_pct"] if summary else None,
                "big_holder_shares_pct": summary["big_holder_shares_pct"] if summary else None,
            })
        picks = picks.merge(pd.DataFrame(shareholding_rows), on="code", how="left")

        print("\n[Smart Money] 計算疑似建倉分數（僅用於排序，不影響上面的入選結果）...")
        chip_hist_map = get_institutional_history(picks["code"].tolist(), calendar_days=20)
        # 給「融資連續下降次數」因子當交易日曆用（見 smart_money.py 的說明）。
        trading_calendar = {datetime.strptime(d, "%Y-%m-%d").date() for d in institutional_store.get_all_dates()}
        score_rows = []
        for _, r in picks.iterrows():
            code = r["code"]
            chip_hist = chip_hist_map.get(code, [])
            margin_trend = load_margin_trend(code)
            sh_trend = load_shareholding_trend(code)

            price_change_pct = None
            change, close = r.get("change"), r.get("close")
            if change is not None and close not in (None, 0) and not pd.isna(change):
                prev_close = close - change
                if prev_close:
                    price_change_pct = change / prev_close

            margin_decline = smart_money.score_margin_decline_streak(margin_trend, trading_calendar)

            components = {
                "institutional_intensity": r.get("smart_money_f1"),
                "institutional_streak": smart_money.score_institutional_streak(chip_hist),
                "trust_momentum": smart_money.score_trust_momentum(chip_hist),
                "margin_divergence": smart_money.score_margin_divergence(
                    r.get("margin_balance"), r.get("margin_balance_prev"), price_change_pct),
                "margin_decline_streak": margin_decline["score"],
                "big_holder_accumulation": smart_money.score_big_holder_accumulation(sh_trend),
                "retail_exit": smart_money.score_retail_exit(sh_trend),
                "volume_pullback_pattern": r.get("smart_money_f8"),
            }
            confidences = {"margin_decline_streak": margin_decline["confidence"]}
            result = smart_money.aggregate(components, confidences)
            score_rows.append({
                "code": code,
                "smart_money_score": result["score"],
                "smart_money_coverage_pct": result["coverage_pct"],
                **{f"sm_{k}": v for k, v in result["components"].items()},
                "sm_margin_decline_confidence": margin_decline["confidence"],
                "sm_margin_decline_observation_days": margin_decline["trading_day_span"],
            })
        picks = picks.merge(pd.DataFrame(score_rows), on="code", how="left")
    else:
        picks = picks.reindex(columns=PICKS_COLUMNS)

    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = OUTPUT_DIR / f"picks_{trade_date.strftime('%Y%m%d')}.csv"

    if not picks.empty:
        # 依 Smart Money 疑似建倉分數排序（缺資料算不出分數的排最後），
        # 三大法人合計買超當作同分時的排序依據。這裡只是排序，前面兩階段
        # 已經決定的入選名單不會因此改變。
        picks = picks.sort_values(
            ["smart_money_score", "total_net"], ascending=[False, False], na_position="last")
        smart_money_log.record_daily(trade_date, picks)

    # 不論今天有沒有選到股票都要寫檔，避免自動排程時 build_dashboard.py
    # 因為抓不到「今天」的檔案，誤用前一天的舊結果、卻讓人以為是當天資料。
    picks.to_csv(out_path, index=False, encoding="utf-8-sig")

    if not picks.empty:
        print(f"\n[4/4] 完成！共選出 {len(picks)} 檔，結果已存到 {out_path}\n")
        print(picks.to_string(index=False))
    else:
        print(f"\n[4/4] 完成，但今天沒有股票同時符合籌碼面條件、且觸發均線多頭或頸線突破訊號。已寫入空白結果 {out_path}")


if __name__ == "__main__":
    main()
