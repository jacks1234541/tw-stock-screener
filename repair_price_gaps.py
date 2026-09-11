"""股價快取的補洞腳本。

get_stock_history() 的本地快取（price_store.py）只要某個月份「存過至少
一天」就視為涵蓋、之後不會再重抓——如果剛好那次抓的時候遇到限流只抓到
部分天數，那個缺口會永久留在資料庫裡，不會自動修好（只有「本月」會
每天重抓，一旦月份翻頁就不會了，這是刻意的取捨，詳見
data_sources.get_stock_history 的說明）。

這支腳本主要用「三大法人歷史資料庫（institutional_history.db）」當對照：
它是全市場單一批次公布的資料，不會有「部分股票缺、部分不缺」的問題，
拿它「某個月實際看到過幾個不同的交易日」當基準最可靠。但這個資料庫
剛開始累積時可能還沒涵蓋很久以前的月份，這種情況會再搭配「股價資料庫
裡這個月任何一檔股票實際存到的最多天數」當補充對照，兩者取較大值。
找出哪些「股票代碼＋月份」存的天數明顯比對照少之後，針對那些缺口重新
抓一次來補齊。不會動到「本月」（本來就每天會自動重抓），也不會動到
從來沒抓過的股票（沒抓過不算「缺口」，等之後真的被選為候選股時會自然
冷啟動抓好）。

股價資料一旦存進去就不會過期或被官方修改，缺口不會自己變多，只可能
因為限流暫時抓不到——補一次之後就會一直是好的，不需要天天跑，建議
偶爾（例如每週）手動執行一次即可。

用法：
    python repair_price_gaps.py [--lookback-months 5] [--threshold 0.8] [--dry-run]
"""
from __future__ import annotations

import argparse
import time
from datetime import date, timedelta

import institutional_store
import price_store
from data_sources import fetch_stock_month

DEFAULT_LOOKBACK_MONTHS = 5
DEFAULT_THRESHOLD = 0.8


def _month_cursors(n: int) -> list[date]:
    cursor = date.today().replace(day=1)
    months = []
    for _ in range(n):
        months.append(cursor)
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    return months


def find_gaps(
    lookback_months: int = DEFAULT_LOOKBACK_MONTHS, threshold: float = DEFAULT_THRESHOLD
) -> list[tuple[str, date, int, int]]:
    """回傳需要補的 (股票代碼, 月份, 目前天數, 對照天數) 清單，本月不列入檢查範圍。

    對照天數取兩者較大值：三大法人資料庫（全市場單一批次公布，通常最
    可靠）跟股價資料庫「這個月裡任何一檔股票實際存到的最多天數」。後者
    是為了因應三大法人資料庫剛開始累積、還沒涵蓋很久以前月份的情況——
    這種時候至少能靠股價資料庫內部彼此比較抓出明顯偏少的缺口。
    """
    months = _month_cursors(lookback_months)
    past_months = months[1:]  # 排除本月，本月本來就每天會自動重抓

    institutional_reference = institutional_store.get_reference_day_counts()
    stored = price_store.get_stored_day_counts()
    codes = price_store.get_all_codes()

    max_stored_by_month: dict[str, int] = {}
    for (_, ym), cnt in stored.items():
        max_stored_by_month[ym] = max(max_stored_by_month.get(ym, 0), cnt)

    gaps = []
    for code in sorted(codes):
        for m in past_months:
            ym = m.strftime("%Y-%m")
            expected = max(institutional_reference.get(ym, 0), max_stored_by_month.get(ym, 0))
            if expected == 0:
                continue  # 兩個對照組都沒資料（太久遠、還沒開始追蹤那個月），沒辦法判斷，跳過
            have = stored.get((code, ym), 0)
            if have < expected * threshold:
                gaps.append((code, m, have, expected))
    return gaps


def repair_month(code: str, month: date, coverage_passes: int = 2) -> bool:
    """重抓單一股票、單一月份的資料，補進本地資料庫。回傳這次是不是有抓到新資料。"""
    for pass_num in range(coverage_passes):
        if pass_num > 0:
            time.sleep(8)
        df = fetch_stock_month(code, month)
        if df is not None and not df.empty:
            price_store.upsert_daily_rows(code, df)
            return True
        time.sleep(0.6)
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-months", type=int, default=DEFAULT_LOOKBACK_MONTHS,
                         help="往回檢查幾個月（含本月，本月會自動排除不檢查）")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                         help="某股票某月存的交易日數，低於「對照天數 * threshold」就視為缺口")
    parser.add_argument("--dry-run", action="store_true", help="只列出缺口，不實際重抓")
    args = parser.parse_args()

    gaps = find_gaps(args.lookback_months, args.threshold)
    if not gaps:
        print("沒有發現需要補的缺口，本地股價資料看起來完整。")
        return

    print(f"發現 {len(gaps)} 個「股票＋月份」缺口：")
    for code, month, have, expected in gaps:
        print(f"  {code}  {month.strftime('%Y-%m')}  目前 {have}/{expected} 天")

    if args.dry_run:
        print("\n--dry-run 模式，不會實際重抓，僅列出缺口。")
        return

    print("\n開始重抓...")
    fixed = 0
    for i, (code, month, have, expected) in enumerate(gaps, 1):
        print(f"({i}/{len(gaps)}) 重抓 {code} {month.strftime('%Y-%m')}（目前 {have}/{expected} 天）...")
        if repair_month(code, month):
            fixed += 1
        time.sleep(0.6)

    print(f"\n完成，{fixed}/{len(gaps)} 個缺口這次成功補上"
          f"（剩下的可能還是遇到限流，之後可以再跑一次這支腳本）。")


if __name__ == "__main__":
    main()
