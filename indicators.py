"""通用技術指標計算（均線 / RSI / MACD）。"""
from __future__ import annotations

import pandas as pd


def moving_average(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def detect_neckline_breakout(
    hist: pd.DataFrame,
    consolidation_window: int = 20,
    max_range_pct: float = 15.0,
    volume_ratio_threshold: float = 1.5,
) -> dict:
    """判斷「盤整後頸線突破」訊號。

    hist 需含 close/high/volume 欄位，且按日期由舊到新排序。

    邏輯：
      1. 觀察今天以前 consolidation_window 個交易日（不含今天）的收盤價波動幅度，
         (最高收盤-最低收盤)/最低收盤 <= max_range_pct% 才視為「盤整區間」。
      2. 頸線（壓力線）= 盤整區間內的最高價（實際盤中觸及過的壓力）。
      3. 突破 = 今日收盤價站上頸線，且昨日收盤價還在頸線之下（確保是今天才剛突破）。
      4. 量能確認 = 今日成交量 > 20日均量 * volume_ratio_threshold。
    三個條件同時成立，is_breakout 才是 True。
    """
    need = consolidation_window + 22  # 盤整窗格 + 20日均量所需天數 + 今日這一根
    if len(hist) < need:
        return {"is_breakout": False}

    window = hist.iloc[-(consolidation_window + 1):-1]  # 不含今天
    close_window = window["close"]
    neckline = window["high"].max()
    range_low = close_window.min()
    range_high = close_window.max()
    range_pct = (range_high - range_low) / range_low * 100 if range_low else float("inf")

    today = hist.iloc[-1]
    yesterday = hist.iloc[-2]

    in_consolidation = range_pct <= max_range_pct
    crossed_today = (yesterday["close"] <= neckline) and (today["close"] > neckline)

    vol_ma20 = hist["volume"].iloc[-21:-1].mean()
    vol_ratio = (today["volume"] / vol_ma20) if vol_ma20 else 0.0
    volume_confirmed = vol_ratio >= volume_ratio_threshold

    return {
        "neckline": round(float(neckline), 2),
        "range_pct": round(float(range_pct), 2),
        "in_consolidation": bool(in_consolidation),
        "crossed_today": bool(crossed_today),
        "vol_ratio": round(float(vol_ratio), 2),
        "volume_confirmed": bool(volume_confirmed),
        "is_breakout": bool(in_consolidation and crossed_today and volume_confirmed),
    }
