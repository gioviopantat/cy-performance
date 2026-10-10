# coggan_np_if_tss · 標準化功率（NP）、強度因子（IF）、訓練壓力分數（TSS）

## 這是什麼
Coggan 提出的三個功率計指標，把一趟高低起伏的騎乘壓縮成三個數字：

- **NP（Normalized Power，標準化功率）**：如果這趟是「穩定輸出」，要多少瓦才會造成一樣的生理代價。它會比平均功率高，起伏越大差越多。
- **IF（Intensity Factor，強度因子）**：NP 除以 FTP。1.0 代表整趟相當於騎在閾值。
- **TSS（Training Stress Score，訓練壓力分數）**：把強度與時間合在一起的「訓練劑量」。定義上 1 小時全力的閾值騎乘 = 100 TSS。

## 為什麼用它
我們需要一個單一的「今天騎了多重」的數字來餵給 PMC（體能／疲勞模型）、守衛（週負荷上限）和排程器（每日目標 TSS）。TSS 是 intervals.icu 與絕大多數教練共同使用的幣別，所以我們的模擬值可以直接對照 icu 的帳本（`icu_training_load`）。

## 怎麼算
1. 把功率做 30 秒滾動平均（模擬生理反應的延遲）。
2. 每個 30 秒平均值取四次方、再取整趟的平均、再開四次方根：
   `NP = ( mean( P30s ^ 4 ) ) ^ (1/4)`
3. `IF = NP / FTP`
4. `TSS = ( 秒數 × NP × IF ) / ( FTP × 3600 ) × 100`，等價於 `TSS = 小時數 × IF² × 100`。
5. 順帶一提 **VI（Variability Index）= NP / 平均功率**，1.0 是完全穩定，1.1 以上代表起伏很大。

**用你的數據舉例**：FTP 250 W，一趟 2 小時、NP 210 W、平均功率 195 W 的騎乘。
- IF = 210 / 250 = **0.84**
- TSS = 2 h × 0.84² × 100 = 2 × 0.7056 × 100 = **141**
- VI = 210 / 195 = **1.08**（有一些起伏，典型的戶外 Z2 加幾段坡）

課表預估用的是「閉式」版本：每一段取目標區間中點當作 NP，`TSS = Σ 段時數 × (中點/FTP)² × 100`，例如甜蜜點 3×10 分 @ 88–92 % 整趟約 55 TSS（見 `workout_library`）。

## 限制
- NP 的四次方權重是經驗性的，對非常短、非常猛的間歇（30/30）會高估代謝代價；對超過 3–4 小時的騎乘則低估「耐久力」成本（糖原耗盡不在公式裡）。
- TSS 完全依賴 FTP 正確；FTP 低估 5 % 會讓所有 TSS 高估約 10 %。這也是我們每 4 週測一次的原因。
- 沒有功率計的騎乘（Edge 530）用 hrTSS 估算，誤差大，報告會標記 `confidence: low`。
- 不同運動（重訓、瑜珈）的 icu 負荷是另一套估法，進 PMC 時當作同一幣別只是近似。

## 參考文獻
- Allen H, Coggan A, McGregor S. *Training and Racing with a Power Meter*, 3rd ed. VeloPress, 2019. 第 7–8 章（NP/IF/TSS 的定義與推導）。
- Coggan A. "Normalized Power, Intensity Factor and Training Stress Score." TrainingPeaks 部落格／原 wattage 論壇文章（2003–2006）。
- intervals.icu 說明：Settings → "Training Load"（icu 對有功率與無功率活動的負荷計算說明）。
