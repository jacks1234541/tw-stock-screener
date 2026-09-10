"""記錄每天 Smart Money Detector 算出的分數，供之後做「前向驗證」用。

這不是正式回測：分數是用「執行當下」看得到的資料算出來的，天然沒有
lookahead bias，但要驗證「分數高的股票，之後報酬是不是真的比較好」，
得等時間過去、有了後續報酬才能算，所以這裡只負責日復一日誠實記錄，
實際驗證交給 validate_smart_money.py 之後手動執行（見該檔案說明）。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

LOG_PATH = Path(__file__).parent / "data" / "smart_money_log.csv"

COLUMNS = [
    "trade_date", "code", "name", "close",
    "smart_money_score", "smart_money_coverage_pct",
    "sm_institutional_intensity", "sm_institutional_streak", "sm_trust_momentum",
    "sm_margin_divergence", "sm_margin_deleveraging_streak", "sm_big_holder_accumulation",
    "sm_retail_exit", "sm_volume_pullback_pattern",
]


def record_daily(trade_date, picks: pd.DataFrame) -> int:
    """把今天每檔候選股的 SmartMoneyScore 明細記一筆。

    同一天重複執行只會覆蓋當天的舊資料，不會產生重複列。
    """
    if picks.empty:
        return 0
    date_str = trade_date.strftime("%Y%m%d") if hasattr(trade_date, "strftime") else str(trade_date)

    rows = picks.copy()
    rows.insert(0, "trade_date", date_str)
    rows = rows.reindex(columns=COLUMNS)

    LOG_PATH.parent.mkdir(exist_ok=True)
    if LOG_PATH.exists():
        existing = pd.read_csv(LOG_PATH, dtype={"code": str, "trade_date": str})
        existing = existing[existing["trade_date"] != date_str]
        combined = pd.concat([existing, rows], ignore_index=True)
    else:
        combined = rows

    combined = combined.sort_values(["trade_date", "code"])
    combined.to_csv(LOG_PATH, index=False, encoding="utf-8-sig")
    return len(rows)


def load_all() -> pd.DataFrame:
    if not LOG_PATH.exists():
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(LOG_PATH, dtype={"code": str, "trade_date": str})
