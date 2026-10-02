# readiness_v1 · 每日準備度判斷（v1）

## 這是什麼
每天早上系統把「你今天能不能練、能練多重」壓成一個 0–100 的分數與四個狀態之一：
`REST`（今天休息）、`EASY`（只能 Z2，TSS 上限為原計畫的 60 %）、`AS_PLANNED`（照課表）、`UPGRADE`（狀態好，允許進程 +1 步，但不加新的高強度）。排程器的每日重排（docs/05 §3 步驟 2）直接吃這個結果。

## 為什麼用它
PMC 只知道你訓練了多少，不知道你睡得怎樣、有沒有生病、昨天的心率有沒有鈍化。文獻（Plews、Buchheit）顯示 HRV 與安靜心率的**個人化趨勢**是恢復狀態最實用的客觀訊號，而主觀感受（疲勞、酸痛）往往比任何儀器更早。v1 的設計原則是**規則優先、加權其次、全部透明**：每個因素的權重與數值都寫進 Explanation，讓你可以對照自己的體感回頭調權重。

## 怎麼算
**第一步：硬規則（任何一條命中就直接決定）**
- icu 日曆有涵蓋當天的 `SICK` / `INJURED` 事件，或 wellness `injury` ≥ 3 → `REST`（狀態 `SICK`）
- HRV z < −1.5 **且** 安靜心率 z > +1.5 → `REST`（狀態 `OVERREACHED`）
- 昨天騎乘判定 `BLUNTED`（心率延遲 ≥ 90 秒）→ 至多 `EASY`（狀態 `BLUNTED`）
- 昨天負荷 > 計畫 130 % → 至多 `EASY`（計畫優先取我們的 `planned_workouts.target_tss`，沒有才用 icu 日曆 WORKOUT 的負荷）

規則只會把結果往下壓，不會往上抬；分數算出來比規則更保守時以分數為準。

**第二步：加權分數**
每個輸入先對自己**當天之前 60 天**的基準做 z 分數（當天不算進基準；基準少於 14 筆或標準差為 0 → 該輸入視為缺值）。方向統一成「正 = 好」，再夾在 ±2：

| 輸入 | z 怎麼來 | 權重 |
|------|----------|------|
| `hrv` | ln(rMSSD) 的 z | 0.30 |
| `rhr` 安靜心率 | −z | 0.15 |
| `sleep` 睡眠 | 睡眠時數 z 與睡眠分數 z 的平均（有哪個用哪個） | 0.20 |
| `tsb` | 進入當天的 TSB（昨天的 icu TSB，沒有才用我們的重播）：−30 → −1、0 → 0、+10 → +1 | 0.15 |
| `ride` 昨天的騎乘反應 | 下列子訊號的平均：可靠的解耦 > 5 % 時每多 3 % 扣 1；負荷超過計畫 110 % 時每多 20 % 扣 1；HR lag > 45 秒時到 90 秒扣 1；`BLUNTED` 直接 −1 | 0.20 |
| `subjective` 主觀 | 酸痛、疲勞、壓力、心情 z 的平均取負號（icu 1–4 分，**1 = 最好**） | 0.20 |

**缺值不會被當成 0**：沒有的輸入整個拿掉，剩下的權重重新正規化（例如沒戴錶睡覺、也沒填主觀，就只用 HRV／安靜心率以外的 TSB 與昨天騎乘）。

```
S     = Σ 權重_i × z_i' / Σ 權重_i（只加總有資料的輸入）
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
HRV z −0.6、安靜心率 z +0.4（→ −0.4）、睡眠 z +0.5、TSB −14（→ −0.47）、昨天騎乘：TSS 比計畫 +8 %（0）、解耦 6.5 %（→ −0.5），沒填主觀。
- 五個輸入的權重剛好加總 1.00（主觀缺值、已拿掉），所以 S = 0.30×(−0.6) + 0.15×(−0.4) + 0.20×(0.5) + 0.15×(−0.47) + 0.20×(−0.5) = −0.18 − 0.06 + 0.10 − 0.07 − 0.10 = **−0.31**
- score = 50 + 25 × (−0.31) = **42 → EASY**
- Explanation 會寫：「HRV 比你的基準低 0.6 個標準差（權重 30 %），昨天長騎後段心率漂移 6.5 %（權重 20 %），所以今天只排 Z2 60 分，原本的甜蜜點 3×12 往後推 48 小時。」

## 限制
- 權重是 v1 的起點，不是科學定論；系統會記錄每天的分數與你事後的 `feel`/`icu_rpe`，M5 之後再據此調整。程式碼（`src/cyp/analysis/readiness.py`）是權威，本文已與 v1 程式對齊；單元測試 `tests/analysis/test_readiness.py::test_glossary_worked_example` 會驗證上面的例子。
- 主觀分數的方向（1 = 最好）是依 icu 介面的排列假設的；若你的 icu 顯示相反，把 `SUBJECTIVE_HIGHER_IS_WORSE` 改成 `False` 即可。
- HRV 需要穩定的量測條件（Garmin 整夜 HRV 比晨間單點好，但也會被晚睡、喝酒、感冒前期干擾）。基準需要至少 2–3 週的資料才可靠；季初前 3 週 readiness 的 `confidence` 會是 low。
- z 分數假設常態分布；HRV 已取對數改善這點，主觀分數（1–4）則很粗。
- 它不是醫療判斷。連續多日 REST、或安靜心率持續上升卻沒有訓練負荷解釋，應該看醫生而不是看課表。

## 參考文獻
- Plews DJ, Laursen PB, Stanley J, Kilding AE, Buchheit M. "Training adaptation and heart rate variability in elite endurance athletes: opening the door to effective monitoring." *Sports Medicine* 43(9):773–781, 2013.
- Buchheit M. "Monitoring training status with HR measures: do all roads lead to Rome?" *Frontiers in Physiology* 5:73, 2014.
- Kiviniemi AM, Hautala AJ, Kinnunen H, Tulppo MP. "Endurance training guided individually by daily heart rate variability measurements." *European Journal of Applied Physiology* 101:743–751, 2007.（HRV 導向訓練）
- Saw AE, Main LC, Gastin PB. "Monitoring the athlete training response: subjective self-reported measures trump commonly used objective measures." *British Journal of Sports Medicine* 50:281–291, 2016.（主觀指標的價值）
- Meeusen R, et al. "Prevention, diagnosis, and treatment of the overtraining syndrome: joint consensus statement of the ECSS and ACSM." *MSSE* 45(1):186–205, 2013.
- intervals.icu：wellness 欄位 `hrv, hrvSDNN, restingHR, sleepSecs, sleepScore, soreness, fatigue, stress, mood, readiness`。
