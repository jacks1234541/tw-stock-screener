"""Smart Money Detector：在既有兩階段選股邏輯篩出的候選股中，
額外算出 0-100 的「疑似建倉分數」，只用來排序，完全不影響原本的
入選條件（stage1 籌碼粗篩 / stage2 均線多頭或頸線突破）。

四大類、共 8 個因子，涵蓋官方 API 能查到的所有籌碼面向：

  法人動能   institutional_intensity   今日三大法人買超占成交量比例
             institutional_streak      近期三大法人「連續買超天數」
             trust_momentum            投信近 5 日買超動能（投信被視為較內行的資金）
  融資結構   margin_divergence         股價上漲、但融資餘額沒有跟著大增（排除散戶追價）
             margin_decline_streak    融資餘額「連續下降次數」（見下方說明，不是連續交易日）
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

「融資連續下降次數」因子的特殊處理（margin_decline_streak）：
  這個因子的原始資料（margin_history.csv）只在一檔股票「進入 stage1
  候選名單那天」才會記一筆，同一檔股票兩次被記錄之間可能隔了好幾個
  交易日、甚至好幾週（如果它中間很少再被三大法人買超排進前 60 名）。
  單純比較「資料庫裡最近幾筆」的數字大小，沒辦法區分「這幾天連續在
  降」跟「剛好抽查到的幾次都在降、但其實隔了一個月」——後者的訊號
  強度應該打折扣，不能跟前者同等看待。

  因此這裡把「訊號」跟「可信度」分開算：
    - score：下降次數本身轉成的 0-100 分數，衡量「這個訊號多強」。
    - confidence：這幾次觀察点實際涵蓋的交易日範圍有多密集（下降
      次數 ÷ 這段期間的交易日數），衡量「這個訊號多可信」。密集
      （例如連續交易日都有資料且都在降）可信度接近 1；稀疏（例如
      一個月才抽查到兩三次）可信度會接近 0。
    - 這兩者不會混在一起：分數不會因為可信度低就被打折降低（訊號
      強度就是訊號強度），而是在併入總分（aggregate）時，讓可信度
      低的因子對總分的實際影響力（有效權重）跟著降低——可信度接近
      0 時，這個因子幾乎不影響總分，效果類似「資料不足」，但分數
      本身仍然誠實反映「目前看到的下降次數」，不會被抹成 0，避免
      把「資料不足」跟「真的沒有下降」混為一談。
    - 「交易日範圍」優先用 institutional_history.db 裡實際看到過的
      交易日期當日曆算（全市場單一批次公布，可靠）；如果那份資料庫
      還沒涵蓋到這麼久以前（例如它本身才剛開始累積），才退回用日曆
      天數概算——日曆天數一定 >= 實際交易日數，所以這種概算只會讓
      密度被低估、不會高估，是保守的做法。
"""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd

WEIGHTS = {
    "institutional_intensity": 20,
    "institutional_streak": 15,
    "trust_momentum": 10,
    "margin_divergence": 15,
    "margin_decline_streak": 10,
    "big_holder_accumulation": 15,
    "retail_exit": 5,
    "volume_pullback_pattern": 10,
}

FACTOR_LABELS = {
    "institutional_intensity": "法人買超強度",
    "institutional_streak": "法人連續買超",
    "trust_momentum": "投信買超動能",
    "margin_divergence": "融資背離(價漲資不增)",
    "margin_decline_streak": "融資連續下降次數",
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


def _trading_days_between(start: date, end: date, trading_calendar: set[date] | None) -> tuple[int, bool]:
    """算 start（不含）到 end（含）之間經過幾個交易日。

    回傳 (交易日數, is_exact)。trading_calendar 若有涵蓋 start 這個時間點
    （即 start 不早於日曆最早的日期），就用日曆裡實際看到的交易日數，
    is_exact=True；日曆沒涵蓋這麼久以前，就退回日曆天數概算，is_exact=
    False——日曆天數一定 >= 交易日數，只會讓後續算出來的密度偏低，不會
    偏高，是保守的近似。
    """
    if trading_calendar:
        calendar_min = min(trading_calendar)
        if start >= calendar_min:
            count = sum(1 for d in trading_calendar if start < d <= end)
            return count, True
    return (end - start).days, False


def score_margin_decline_streak(
    margin_trend: list[dict], trading_calendar: set[date] | None = None
) -> dict:
    """融資餘額「連續下降次數」，附帶可信度（見本檔案開頭「特殊處理」說明）。

    margin_trend 需按日期由舊到新排序，每筆含 date（'YYYYMMDD' 字串）與
    margin_balance；trading_calendar 是選用的交易日曆（date 物件集合），
    用來把「下降次數」換算成「這段觀察期間有多密集」。

    回傳 dict：
      score：0-100，下降次數本身的訊號強度；None 代表資料點不足（少於
             2 筆有效觀察），無法判斷，這跟「score=0（觀察到沒有下降）」
             是兩件不同的事，呼叫端要分開處理，不要把「沒資料」當成
             「確定沒有下降」。
      confidence：0-1，觀察密度（下降次數 ÷ 涵蓋的交易日數），資料點
             不足時為 0；「確定沒有下降」（streak=0）視為完全可信（這是
             直接比較兩個真實數字，不涉及跨時間推論），confidence=1。
      decline_count：這次算出的連續下降次數（原始整數，供顯示/除錯）。
      trading_day_span：這段連續下降涵蓋的交易日數（int，用不到日曆時
             為日曆天數概算）。
      span_is_exact：trading_day_span 是不是用真正的交易日曆算出來的。
    """
    valid = [
        (datetime.strptime(d["date"], "%Y%m%d").date(), d["margin_balance"])
        for d in margin_trend if d.get("margin_balance") is not None
    ]
    valid.sort(key=lambda x: x[0])

    if len(valid) < 2:
        return {
            "score": None, "confidence": 0.0, "decline_count": 0,
            "trading_day_span": None, "span_is_exact": False,
        }

    streak = 0
    for i in range(len(valid) - 1, 0, -1):
        if valid[i][1] < valid[i - 1][1]:
            streak += 1
        else:
            break

    if streak == 0:
        # 有實際資料可以比較，而且比較結果確定是「沒有下降」（持平或上升），
        # 這是可信的觀察，不是資料不足。
        return {
            "score": 0.0, "confidence": 1.0, "decline_count": 0,
            "trading_day_span": 0, "span_is_exact": True,
        }

    window_start = valid[len(valid) - 1 - streak][0]
    window_end = valid[-1][0]
    span, is_exact = _trading_days_between(window_start, window_end, trading_calendar)
    span = max(span, streak)  # 交易日數不會比觀察次數少，避免除以過小的數膨脹密度

    density = streak / span if span else 0.0
    score = _linear_score(streak, 0, 5)
    confidence = round(_clip01(density), 3)

    return {
        "score": score,
        "confidence": confidence,
        "decline_count": streak,
        "trading_day_span": span,
        "span_is_exact": is_exact,
    }


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


def aggregate(components: dict[str, float | None], confidences: dict[str, float] | None = None) -> dict:
    """把各因子的 0-100 子分數依權重加權平均成最終分數。

    缺資料的因子會被排除、權重由其他有資料的因子按比例分攤，所以最終
    分數永遠落在 0-100，但同時回傳 coverage_pct 讓使用者知道這次分數
    是用了多少比例的因子算出來的（涵蓋率愈低，分數的參考價值愈打折扣）。

    confidences 是選用參數：某些因子除了「有沒有資料」，還有「這筆資料
    有多可信」的問題（目前只有 margin_decline_streak 會用到，見該因子
    的說明）。可以用因子名稱對應一個 0-1 的可信度，讓它在加權平均時的
    「有效權重」= 原始權重 * 可信度；可信度愈低，這個因子對總分的影響
    力愈小，但分數本身不會被打折（訊號強度跟可信度是分開的兩件事）。
    沒有在 confidences 裡指定的因子，可信度視為 1.0，計算結果跟完全
    不傳 confidences 時完全一樣——不影響其他因子原本的行為。
    """
    available = {k: v for k, v in components.items() if v is not None and k in WEIGHTS}
    total_weight = sum(WEIGHTS.values())
    if not available:
        return {
            "score": None,
            "coverage_pct": 0.0,
            "components": {k: None for k in WEIGHTS},
        }

    confidences = confidences or {}
    effective_weights = {k: WEIGHTS[k] * confidences.get(k, 1.0) for k in available}
    used_weight = sum(effective_weights.values())
    if used_weight <= 0:
        # 可信度全部趨近 0（例如僅有的融資因子資料太零散），等同這次沒有
        # 真正可用的因子，但仍然誠實回報各因子原始分數供參考。
        return {
            "score": None,
            "coverage_pct": 0.0,
            "components": {k: (round(components[k], 1) if components.get(k) is not None else None) for k in WEIGHTS},
        }

    score = sum(effective_weights[k] * v for k, v in available.items()) / used_weight
    return {
        "score": round(score, 1),
        "coverage_pct": round(used_weight / total_weight * 100, 1),
        "components": {k: (round(components[k], 1) if components.get(k) is not None else None) for k in WEIGHTS},
    }
