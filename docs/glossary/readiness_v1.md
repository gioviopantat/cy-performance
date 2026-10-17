# readiness_v1 · 每日準備度判斷（v2）

> 檔名沿用 `readiness_v1`（主張 id 不變）；目前的演算法是 `readiness_v2`（2026-10-16，ADR-0013）。v1 → v2 的差別：HRV 改用 7 日平均、昨天的解耦與心率延遲不再是輸入（ADR-0012）。

## 這是什麼
每天早上系統把「你今天能不能練、能練多重」壓成一個 0–100 的分數與四個狀態之一：
`REST`（今天休息）、`EASY`（只能 Z2，TSS 上限為原計畫的 60 %）、`AS_PLANNED`（照課表）、`UPGRADE`（狀態好，允許進程 +1 步，但不加新的高強度）。排程器的每日重排（docs/05 §3 步驟 2）直接吃這個結果。

## 為什麼用它
PMC 只知道你訓練了多少，不知道你睡得怎樣、有沒有生病。文獻（Plews、Buchheit）顯示 HRV 與安靜心率的**個人化趨勢**是恢復狀態最實用的客觀訊號——而且是趨勢：Plews 2013 裡 7 日平均 ln rMSSD 與體能變化相關 r = .72，單日值只有 −.06，所以 v2 改看 7 日平均。HRV 引導的訓練在統合分析裡跟固定課表效果差不多，好處是無反應者較少、強度課排得較少（Düking 2021）；我們用它是為了避開壞日子，不是因為它能讓你進步更多，而主觀感受（疲勞、酸痛）往往比任何儀器更早。v1 的設計原則是**規則優先、加權其次、全部透明**：每個因素的權重與數值都寫進 Explanation，讓你可以對照自己的體感回頭調權重。

## 怎麼算
**第一步：硬規則（任何一條命中就直接決定）**
- icu 日曆有涵蓋當天的 `SICK` / `INJURED` 事件，或 wellness `injury` ≥ 3 → `REST`（狀態 `SICK`）
- HRV 分量 ≤ −1.5（= 7 日平均低於基準 1.25 個標準差以上）**且** 安靜心率 z > +1.5 → `REST`（狀態 `OVERREACHED`）。v1 是「單日 HRV z < −1.5」；v2 用同一個 −1.5，但量的是下面 7 日平均換算的分量，意思仍是「HRV 明顯偏低、而且今天安靜心率也高」。
- 昨天騎完的感覺 5/5，或 RPE 比這類課預期高 ≥ 3 → 至多 `EASY`（功能 `readiness.ride_feel` 開著才用）
- （已移除，ADR-0017）昨天負荷超過計畫不再限制準備度；隔天減量由規劃器處理，準備度只看身體訊號

規則只會把結果往下壓，不會往上抬；分數算出來比規則更保守時以分數為準。

**個人量表（`readiness_v2.4`，功能 `plan.personal_scales`，ADR-0019）**：每個人打 RPE 的習慣不同（Roger 兩小時 Z2 打 1，dad 每趟都打 5）。系統用你自己過去 90 天的紀錄學一個「這類課你平常比表定高／低幾分」（每類至少 5 趟，不夠就用全部類別的中位數；往 0 收縮 n/(n+5)，最多 ±2），RPE 的「預期」改成表定＋你的偏移，上面的 RPE 規則與下面的 `ride` 子訊號都用它。騎完感覺也一樣：你平常的感覺（90 天中位數，至少 8 筆）比 3 差時，要再差 2 分才算「感受很差」；**個人量表只會放寬感覺規則，不會讓它更嚴**。athlete.yaml 的 `planner.scales` 寫明的值優先於學到的值。Explanation 會多一行「個人量表：……」說明用了哪個基準。

**第二步：加權分數**
每個輸入先對自己**當天之前 60 天**的基準做 z 分數（當天不算進基準；基準少於 14 筆或標準差為 0 → 該輸入視為缺值）。方向統一成「正 = 好」，再夾在 ±2：

| 輸入 | z 怎麼來 | 權重 |
|------|----------|------|
| `hrv` | 7 日平均 ln(rMSSD) 對基準，±0.5 標準差內算 0（見下） | 0.30 |
| `rhr` 安靜心率 | −z | 0.15 |
| `sleep` 睡眠 | 睡眠時數 z 與睡眠分數 z 的平均（有哪個用哪個） | 0.20 |
| `tsb` | 進入當天的 TSB（昨天的 icu TSB，沒有才用我們的重播）：−30 → −1、0 → 0、+10 → +1 | 0.15 |
| `ride` 昨天的騎乘反應 | 下列子訊號的平均（負荷對計畫已移除，ADR-0017）：騎完感覺 1（很強）→ +1、3 → 0、5（很弱）→ −1；RPE 比預期每高 2 分扣 1（後兩項需 `readiness.ride_feel`）。解耦與心率延遲不算（ADR-0012） | 0.20 |
| `subjective` 主觀 | 酸痛、疲勞、壓力、心情 z 的平均取負號（icu 1–4 分，**1 = 最好**） | 0.20 |

**HRV 的 7 日平均（v2）**：
```
窗口   = 今天與前 6 天（共 7 天）有 HRV 的夜晚，缺的夜晚直接跳過；少於 3 晚 → HRV 視為缺值（Plews 2014：每週 3 晚就夠）
mean7  = 窗口內 ln(rMSSD) 的平均（今天沒量也照算，只要窗口內還有 3 晚）
z7     = (mean7 − 基準平均) / 基準標準差   ← 基準 = 前 60 天每天的 ln(rMSSD)，至少 14 筆
分量   = |z7| ≤ 0.5 → 0（正常範圍，HRV 引導試驗的「最小有意義變化」帶）
         否則 sign(z7) × (|z7| − 0.5) / 0.5，夾在 ±2（帶外每多 0.5 個標準差算 1）
```
例：z7 = −1.0 → 分量 −1；z7 = −1.25 → −1.5（REST 規則的線）；z7 = +0.75 → +0.5。單晚很低（例如前 60 天平均以下 4 個標準差）只會把 7 日平均拉低約 0.6 個標準差，分量約 −0.16，不會單獨觸發休息。

**缺值不會被當成 0**：沒有的輸入整個拿掉，剩下的權重重新正規化（例如沒戴錶睡覺、也沒填主觀，就只用 TSB 與昨天騎乘）。**但有下限**（v2，ADR-0016）：有資料的權重合計少於 0.5 時，分母用 0.5，不足的部分當成中性 0。例：只有 TSB −39（分量 −1.29、權重 0.15）→ S = 0.15 × (−1.29) / 0.5 = −0.39 → 40 分 EASY；沒有下限時是 −1.29 → 18 分 REST，等於只靠 TSB 一個 C 級輸入就叫你休息。

```
S     = Σ 權重_i × z_i' / max(Σ 權重_i, 0.5)（只加總有資料的輸入）
score = clamp( 50 + 25 × S, 0, 100 )
建議  = score ≥ 65 → UPGRADE（還要 TSB > −10 且昨天不是 vo2／threshold／sweetspot／race；否則 AS_PLANNED）
        45–64 → AS_PLANNED
        30–44 → EASY
        < 30  → REST
狀態  = UPGRADE → FRESH；AS_PLANNED／EASY → NORMAL；REST → OVERREACHED（規則命中時以規則的狀態為準）
信心  = HRV、安靜心率、睡眠三個客觀輸入都有 → high；至少一個（或任兩個輸入）→ medium；否則 low
```

每天的結果寫進 `readiness_daily`（分數、狀態、建議、每個輸入的 z 與權重、缺了哪些、命中哪條規則），完整 Explanation 用 `cyp explain readiness.YYYY-MM-DD` 查。`cyp readiness --coverage` 會列出最近 60 天 wellness 每個欄位有幾天有值——先看這個，才知道你的裝置實際提供了哪些輸入。

**用你的數據舉例**（昨天那趟 2 小時 NP 210 W、141 TSS 之後的早上）：
HRV 7 日平均 z7 −0.8（→ 分量 −0.6）、安靜心率 z +0.4（→ −0.4）、睡眠 z +0.5、TSB −14（→ −0.47）、昨天騎乘：TSS 比計畫 +20 %（→ −0.5），沒填主觀、沒填騎完感受。
- 五個輸入的權重剛好加總 1.00（主觀缺值、已拿掉），所以 S = 0.30×(−0.6) + 0.15×(−0.4) + 0.20×(0.5) + 0.15×(−0.47) + 0.20×(−0.5) = −0.18 − 0.06 + 0.10 − 0.07 − 0.10 = **−0.31**
- score = 50 + 25 × (−0.31) = **42 → EASY**
- Explanation 會寫：「HRV 7 日平均（7 晚）比你的基準低 0.8 個標準差，低於正常範圍（權重 30 %），昨天負荷比計畫多 20 %（權重 20 %），所以今天只排 Z2 60 分，原本的甜蜜點 3×12 往後推 48 小時。」

## 限制
- 權重是 v1 的起點，不是科學定論（v2 沒改權重）；系統會記錄每天的分數與你事後的 `feel`/`icu_rpe`，M5 之後再據此調整。HRV 分量的換算（帶外每 0.5 個標準差算 1）和 REST 的 −1.5 線是我們的選擇，用意是讓 v2 的 HRV 跟 v1 單日 z 的份量差不多；試驗只規定「低於帶就減量」。程式碼（`src/cyp/analysis/readiness.py`）是權威，本文已與 v2 程式對齊；單元測試 `tests/analysis/test_readiness.py::test_glossary_worked_example` 會驗證上面的例子。
- 主觀分數的方向（1 = 最好）是依 icu 介面的排列假設的；若你的 icu 顯示相反，把 `SUBJECTIVE_HIGHER_IS_WORSE` 改成 `False` 即可。
- HRV 需要穩定的量測條件（Garmin 整夜 HRV 比晨間單點好，但也會被晚睡、喝酒、感冒前期干擾）。基準需要至少 2–3 週的資料才可靠；季初前 3 週 readiness 的 `confidence` 會是 low。
- z 分數假設常態分布；HRV 已取對數改善這點，主觀分數（1–4）則很粗。
- 7 日平均反應比較慢：真的生病的第一晚，HRV 分量可能還在正常範圍；急性狀況靠安靜心率、主觀感受和 icu 的 `SICK` 事件補。60 天基準比試驗常用的 4 週長，合理但沒有驗證。
- REST 規則（HRV 低＋安靜心率高）抓的是急性壓力或生病前兆；耐力選手的過度訓練常見 HRV 上升、安靜心率下降（Le Meur 2013），這條抓不到。
- 2026-10-16 之前存的結果版本是 `readiness_v1`／`readiness_v1.1`（單日 HRV、含解耦與 lag）；之後是 `readiness_v2`／`readiness_v2.1`（有騎完感受時）。
- 個人量表是描述性的（ADR-0019）：門檻（90 天、5 筆、k = 5、±2、feel 差 2 分）是我們的選擇（C 級，`personal_scales.rpe_offset`、`personal_scales.feel_baseline`）。RPE 偏移是「依課的分類」學的，分類不準時（例如 dad 的 Z2 常被分成 tempo／mixed）偏移也會偏。
- 它不是醫療判斷。連續多日 REST、或安靜心率持續上升卻沒有訓練負荷解釋，應該看醫生而不是看課表。

## 證據等級
等級 A／B 可以單獨支撐報告裡的判斷；C 只當參考，報告標示「參考（證據弱）」（定義見 [docs/07 §6](../07-explainability.md)）。標「（提議）」的是文獻月報建議新增、等 Roger 核准的主張，等級已經生效。這張表與 `src/cyp/core/evidence.py` 必須一致（測試會檢查）。

| 主張 | 等級 | 依據 | 最後審查 |
|------|------|------|----------|
| `readiness_v1.subjective_wellness` 主觀身心狀態能追蹤訓練反應 | **B** | Saw et al. 2016 BJSM 系統性回顧 | 2026-10-16 |
| `readiness_v1.hrv_guided` HRV 引導訓練優於固定計畫（統合分析：沒有顯著優勢） | **C** | Granero-Gallegos et al. 2020 IJERPH 統合分析：VO2max 小幅正效果（[PMC7663087](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7663087/)）；Düking et al. 2021 JSAMS（8 篇，[PMID 34489178](https://pubmed.ncbi.nlm.nih.gov/34489178/)）：VO2peak 效果不顯著；Manresa-Rocamora et al. 2021 IJERPH（[PMC8507742](https://pmc.ncbi.nlm.nih.gov/articles/PMC8507742/)）：VO2max 與表現都不顯著；等級依 2026-10 文獻月報，2026-10-16 核准 | 2026-10-16 |
| `readiness_v1.weights` 我們 v1 的權重與切點 | **C** | 自訂，只是起點 | 2026-10-16 |
| `readiness_v1.hrv_single_day` 7 日平均 ln rMSSD（至少 3 晚）對照基準平均 ± 0.5 標準差反映準備度；單晚不行（readiness_v2） | **C**（提議） | Plews et al. 2013（[PMID 23479420](https://pubmed.ncbi.nlm.nih.gov/23479420/)）：7 日平均 r = .72，單日只有 −.06；Plews et al. 2014（[PMID 24334285](https://pubmed.ncbi.nlm.nih.gov/24334285/)）：每週 3 晚就夠；Manresa-Rocamora et al. 2021：HRV 引導試驗都用這個形式 | 2026-10-16 |
| `readiness_v1.overreached_rule` HRV 7 日平均低於基準 1.25 個標準差以上且安靜心率 z > +1.5 ＝急性壓力（休息） | **C**（提議） | Bellenger et al. 2016（[PMID 26888648](https://pubmed.ncbi.nlm.nih.gov/26888648/)）：耐力選手過度訓練常見 HRV 上升 | 2026-10-16 |
| `readiness_v1.missing_shrinkage` 有資料的輸入權重合計 < 0.5 時，不足的部分當成中性 0，不再全部分給剩下的輸入 | **C**（提議） | 我們的選擇（ADR-0016）：避免只有 TSB 之類的單一 C 級輸入決定今天 | 2026-10-17 |
| `readiness_v1.sleep` 睡不好會降低隔天耐力表現 | **B**（提議） | Craven et al. 2022 Sports Med（[PMID 35708888](https://pubmed.ncbi.nlm.nih.gov/35708888/)） | 2026-10-16 |
| `readiness_v1.rpe_surprise` RPE 比預期高 ≥ 3 → 隔天至多 EASY | **C**（提議） | Inoue et al. 2022 Sports Med Open（統合分析，證據確定性極低） | 2026-10-16 |

## 參考文獻
- Plews DJ, Laursen PB, Stanley J, Kilding AE, Buchheit M. "Training adaptation and heart rate variability in elite endurance athletes: opening the door to effective monitoring." *Sports Medicine* 43(9):773–781, 2013.
- Plews DJ, Laursen PB, Kilding AE, Buchheit M. "Evaluating training adaptation with heart-rate measures: a methodological comparison." *International Journal of Sports Physiology and Performance* 8(6):688–691, 2013.（7 日平均 vs 單日）
- Plews DJ, Laursen PB, Le Meur Y, Hausswirth C, Kilding AE, Buchheit M. "Monitoring training with heart-rate variability: how much compliance is needed for valid assessment?" *International Journal of Sports Physiology and Performance* 9(5):783–790, 2014.（每週 3 晚）
- Javaloyes A, Sarabia JM, Lamberts RP, Moya-Ramon M. "Training prescription guided by heart-rate variability in cycling." *International Journal of Sports Physiology and Performance* 14(1):23–32, 2019.（7 日平均 vs 基準 ± 0.5 SD）
- Buchheit M. "Monitoring training status with HR measures: do all roads lead to Rome?" *Frontiers in Physiology* 5:73, 2014.
- Kiviniemi AM, Hautala AJ, Kinnunen H, Tulppo MP. "Endurance training guided individually by daily heart rate variability measurements." *European Journal of Applied Physiology* 101:743–751, 2007.（HRV 導向訓練）
- Saw AE, Main LC, Gastin PB. "Monitoring the athlete training response: subjective self-reported measures trump commonly used objective measures." *British Journal of Sports Medicine* 50:281–291, 2016.（主觀指標的價值）
- Meeusen R, et al. "Prevention, diagnosis, and treatment of the overtraining syndrome: joint consensus statement of the ECSS and ACSM." *MSSE* 45(1):186–205, 2013.
- intervals.icu：wellness 欄位 `hrv, hrvSDNN, restingHR, sleepSecs, sleepScore, soreness, fatigue, stress, mood, readiness`。
