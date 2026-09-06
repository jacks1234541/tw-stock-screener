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

用法：
    python screener.py

執行後於 output/picks_YYYYMMDD.csv 產生結果，並印在終端機。
"""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd

from data_sources import (
    find_latest_trading_date,
    get_all_daily_price,
    get_institutional_net,
    get_margin_balance,
    get_shareholding_distribution,
    get_stock_history,
    shareholding_summary,
)
from indicators import detect_neckline_breakout
from indicators import macd as macd_indicator
from indicators import moving_average, rsi

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
]
PICKS_COLUMNS = STAGE2_COLUMNS + ["retail_holders_pct", "retail_shares_pct", "big_holder_shares_pct"]


def stage2_technical_filter(candidates: pd.DataFrame) -> pd.DataFrame:
    rows = []
    total = len(candidates)
    for i, row in candidates.iterrows():
        code, name = row["code"], row["name"]
        print(f"[2/4] ({i + 1}/{total}) 抓取 {code} {name} 歷史日線並計算技術指標...")
        hist = get_stock_history(code, months=4)
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
        })
    return pd.DataFrame(rows, columns=STAGE2_COLUMNS)


def main():
    trade_date = find_latest_trading_date()
    print(f"最近交易日：{trade_date}\n")

    candidates = stage1_chip_filter(trade_date)
    print(f"\n[1/4] 籌碼面初篩留下 {len(candidates)} 檔，進入技術面複核...\n")

    picks = stage2_technical_filter(candidates)

    if not picks.empty:
        print("\n[3/4] 抓取集保戶股權分散表，計算散戶佔比...")
        dist = get_shareholding_distribution()
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
    else:
        picks = picks.reindex(columns=PICKS_COLUMNS)

    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = OUTPUT_DIR / f"picks_{trade_date.strftime('%Y%m%d')}.csv"

    if not picks.empty:
        picks = picks.sort_values("total_net", ascending=False)

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
