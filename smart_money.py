"""Smart Money Detector：在既有兩階段選股邏輯篩出的候選股中，
額外算出 0-100 的「疑似建倉分數」，只用來排序，完全不影響原本的
入選條件（stage1 籌碼粗篩 / stage2 均線多頭或頸線突破）。

四大類、共 8 個因子，涵蓋官方 API 能查到的所有籌碼面向：

  法人動能   institutional_intensity   今日三大法人買超占成交量比例
             institutional_streak      近期三大法人「連續買超天數」
             trust_momentum            投信近 5 日買超動能（投信被視為較內行的資金）
  融資結構   margin_divergence         股價上漲、但融資餘額沒有跟著大增（排除散戶追價）
             margin_deleveraging_streak 融資餘額連續下降天數（籌碼收斂、由大戶接手）
  大戶籌碼   big_holder_accumulation   集保大戶(100萬股以上)持股比例週增減
             retail_exit               集保散戶持股比例週增減（下降=散戶退場，反向計分）
  量價行為   volume_pullback_pattern   量縮拉回不破均線（惜售訊號），用既有技術面資料算出

刻意不做的事：
  - 券商分點進出：官方查詢系統（bsr.twse.com.tw）是純前端渲染頁面，用
    requests 抓不到真實資料，要抓到得加裝 Selenium/Playwright 這種重量級
    瀏覽器自動化套件，跟本專案「只用官方公開 JSON/RWD 端點」的風格差異
    太大、也更容易壞，因此不列入因子。
  - 正式歷史回測：三大法人與價量資料雖然能查任意過去日期、可以做真正的
    回測，但集保大戶持股分布（TDCC）官方只給「最新一週快照」、沒有歷史
    查詢參數，只能靠 shareholding_store.py 每天執行時累積。為了讓「回測
    方法」對所有因子一致，這裡統一採用前向驗證（forward validation）：
    每天把當天算出的分數記錄下來（smart_money_log.py），累積幾週後再用
    validate_smart_money.py 檢驗分數高低跟「之後」的實際報酬有沒有關聯，
    而不是回頭用歷史資料重建過去的分數。

資料不足時的處理：任何一個因子只要當下沒有足夠資料（例如某檔股票才
第一次出現在候選名單、還沒有前一週的集保快照可比較），就直接跳過該
因子、把它的權重排除，由其他有資料的因子按比例分攤——分數仍然維持在
0-100 區間，不會被「查無資料」拉低，但涵蓋率（coverage_pct）會如實
反映這次分數是用多少比例的因子算出來的。
"""
from __future__ import annotations

import pandas as pd

WEIGHTS = {
    "institutional_intensity": 20,
    "institutional_streak": 15,
    "trust_momentum": 10,
    "margin_divergence": 15,
    "margin_deleveraging_streak": 10,
    "big_holder_accumulation": 15,
    "retail_exit": 5,
    "volume_pullback_pattern": 10,
}

FACTOR_LABELS = {
    "institutional_intensity": "法人買超強度",
    "institutional_streak": "法人連續買超",
    "trust_momentum": "投信買超動能",
    "margin_divergence": "融資背離(價漲資不增)",
    "margin_deleveraging_streak": "融資連續去化",
    "big_holder_accumulation": "大戶籌碼增碼",
    "retail_exit": "散戶退場訊號",
    "volume_pullback_pattern": "量縮拉回不破均線",
}


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _linear_score(x: float, lo: float, hi: float) -> float:
    """把 x 從 [lo, hi] 線性映射到 0~100，超出範圍就截斷。"""
    if hi == lo:
        return 50.0
    return _clip01((x - lo) / (hi - lo)) * 100.0


def score_institutional_intensity(total_net, volume) -> float | None:
    """今日三大法人合計買超股數，占今日總成交量的比例。

    比例 <=0 給 0 分；比例達 15%（代表當天成交量有一大部分是法人在買）給滿分 100。
    """
    if total_net is None or volume is None or volume <= 0:
        return None
    return _linear_score(total_net / volume, 0.0, 0.15)


def score_institutional_streak(chip_history: list[dict]) -> float | None:
    """三大法人合計買超「連續天數」（由最近一天往回算，中斷就停），最多算到 10 天。

    chip_history 需按日期由舊到新排序，每筆含 total_net。
    """
    nets = [d.get("total_net") for d in chip_history if d.get("total_net") is not None]
    if not nets:
        return None
    streak = 0
    for v in reversed(nets):
        if v > 0:
            streak += 1
        else:
            break
    return _linear_score(streak, 0, 10)


def score_trust_momentum(chip_history: list[dict]) -> float | None:
    """投信近 5 個交易日的買超動能：買超天數愈多分數愈高，若今天买超力度
    又高於這 5 天平均，額外加分（代表動能還在增強，不是已經放緩）。
    """
    nets = [d.get("trust_net") for d in chip_history if d.get("trust_net") is not None]
    if len(nets) < 5:
        return None
    last5 = nets[-5:]
    positive_days = sum(1 for v in last5 if v > 0)
    avg5 = sum(last5) / len(last5)
    base = _linear_score(positive_days, 0, 5) * 0.8
    bonus = 20.0 if last5[-1] > avg5 and last5[-1] > 0 else 0.0
    return min(100.0, base + bonus)


def score_margin_divergence(margin_balance, margin_balance_prev, price_change_pct) -> float | None:
    """股價上漲時，融資餘額有沒有「不升反降」——這是典型的「主力/大戶在吃籌碼，
    散戶沒有追價」訊號；若融資餘額大增，代表這波上漲比較像散戶追價，扣分。

    股價沒有上漲時，這個因子的前提不成立，回傳中性 50 分（不参与判斷方向）。
    """
    if margin_balance is None or not margin_balance_prev:
        return None
    if price_change_pct is None:
        return None
    if price_change_pct <= 0:
        return 50.0
    delta_pct = (margin_balance - margin_balance_prev) / margin_balance_prev
    return _linear_score(-delta_pct, -0.05, 0.05)


def score_margin_deleveraging_streak(margin_trend: list[dict]) -> float | None:
    """融資餘額連續下降的天數（本地累積的每日快照才看得出來，最多算到 5 天）。

    margin_trend 需按日期由舊到新排序，每筆含 margin_balance；至少要有 3 筆
    本地累積的紀錄才有意義（不然只是單日雜訊）。
    """
    balances = [d.get("margin_balance") for d in margin_trend if d.get("margin_balance") is not None]
    if len(balances) < 3:
        return None
    streak = 0
    for i in range(len(balances) - 1, 0, -1):
        if balances[i] < balances[i - 1]:
            streak += 1
        else:
            break
    return _linear_score(streak, 0, 5)


def score_big_holder_accumulation(shareholding_trend: list[dict]) -> float | None:
    """集保大戶（100萬股以上級距）持股比例，最新一筆比上一筆的週變化（百分點）。

    需要至少 2 筆本地累積的集保快照才能算變化量；正 2 個百分點給滿分。
    """
    vals = [d.get("big_holder_shares_pct") for d in shareholding_trend if d.get("big_holder_shares_pct") is not None]
    if len(vals) < 2:
        return None
    return _linear_score(vals[-1] - vals[-2], -2.0, 2.0)


def score_retail_exit(shareholding_trend: list[dict]) -> float | None:
    """集保散戶持股比例的週變化（百分點），下降代表散戶退場，反向計分。"""
    vals = [d.get("retail_shares_pct") for d in shareholding_trend if d.get("retail_shares_pct") is not None]
    if len(vals) < 2:
        return None
    return _linear_score(-(vals[-1] - vals[-2]), -0.5, 0.5)


def score_volume_pullback_pattern(hist: pd.DataFrame | None) -> float | None:
    """「量縮拉回不破均線」的惜售訊號，拆成兩個子分數各半：

    1. 量縮：近 5 日均量，比前面那 20 日的均量縮多少（縮愈多分數愈高）。
    2. 貼近支撐：收盤價落在 MA20 附近的合理區間（略高於或略低於都算，
       離太遠——不論是已大幅突破還是已經破線——都會扣分)。
    """
    if hist is None or len(hist) < 25:
        return None
    volume = hist["volume"]
    recent5 = volume.iloc[-5:].mean()
    prior20 = volume.iloc[-25:-5].mean()
    if not prior20:
        return None
    vol_ratio = recent5 / prior20
    shrink_score = _linear_score(1 - vol_ratio, 0.0, 0.4)

    ma20 = hist["close"].rolling(20).mean().iloc[-1]
    last_close = hist["close"].iloc[-1]
    if not ma20:
        return None
    support_margin = (last_close - ma20) / ma20
    if -0.03 <= support_margin <= 0.05:
        support_score = 100.0
    elif support_margin < -0.03:
        support_score = _linear_score(support_margin, -0.10, -0.03)
    else:
        support_score = _linear_score(-support_margin, -0.15, -0.05)

    return 0.5 * shrink_score + 0.5 * support_score


def aggregate(components: dict[str, float | None]) -> dict:
    """把各因子的 0-100 子分數依權重加權平均成最終分數。

    缺資料的因子會被排除、權重由其他有資料的因子按比例分攤，所以最終
    分數永遠落在 0-100，但同時回傳 coverage_pct 讓使用者知道這次分數
    是用了多少比例的因子算出來的（涵蓋率愈低，分數的參考價值愈打折扣）。
    """
    available = {k: v for k, v in components.items() if v is not None and k in WEIGHTS}
    total_weight = sum(WEIGHTS.values())
    if not available:
        return {
            "score": None,
            "coverage_pct": 0.0,
            "components": {k: None for k in WEIGHTS},
        }
    used_weight = sum(WEIGHTS[k] for k in available)
    score = sum(WEIGHTS[k] * v for k, v in available.items()) / used_weight
    return {
        "score": round(score, 1),
        "coverage_pct": round(used_weight / total_weight * 100, 1),
        "components": {k: (round(components[k], 1) if components.get(k) is not None else None) for k in WEIGHTS},
    }
