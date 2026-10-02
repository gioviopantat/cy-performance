# banister_pmc · Banister 體能–疲勞模型與 PMC（CTL / ATL / TSB）

## 這是什麼
一個把每天的訓練劑量（TSS）轉換成三條曲線的模型：

- **CTL（Chronic Training Load，長期負荷／「體能」）**：過去約 6 週 TSS 的指數加權平均，變化慢。
- **ATL（Acute Training Load，短期負荷／「疲勞」）**：過去約 1 週的指數加權平均，變化快。
- **TSB（Training Stress Balance，「狀態」）**：CTL − ATL。負值代表疲勞大於體能（正在累積訓練），正值代表恢復過來（適合測驗或比賽）。

這三條線畫在一起就是 PMC（Performance Management Chart），intervals.icu 的 Fitness/Fatigue/Form 就是這個。

## 為什麼用它
排程器需要知道「現在有多累、還能加多少」。CTL 每週可以漲多少（ramp rate）、TSB 不能低於多少（floor），都是守衛的硬規則。因為公式是閉式的，排程器可以把未來 14 天的候選課表「模擬」進去，確認不會踩線後再發布。icu 的 CTL/ATL 是帳本，我們的模擬必須跟它差在 ±1 以內。

## 怎麼算
每天更新一次（τ 是時間常數，icu 預設 42 天與 7 天）：

```
CTL_今天 = CTL_昨天 + (TSS_今天 − CTL_昨天) × (1 − e^(−1/42))   ≈ CTL_昨天 + (TSS − CTL_昨天) / 42
ATL_今天 = ATL_昨天 + (TSS_今天 − ATL_昨天) × (1 − e^(−1/7))    ≈ ATL_昨天 + (TSS − ATL_昨天) / 7
TSB_今天 = CTL_昨天 − ATL_昨天      （用昨天的值，代表「今天早上起床時的狀態」）
```

這是 Banister 原始模型的簡化版：原模型預測「表現」= k1 × 體能 − k2 × 疲勞，PMC 只保留兩個指數濾波器、不擬合 k1/k2。我們的模擬器（`cyp simulate`）會用完整版（含 k1、k2）來產生合成選手。

**用你的數據舉例**：昨天 CTL 60、ATL 65（TSB −5），今天騎了 2 小時 NP 210 W → 141 TSS。
- CTL = 60 + (141 − 60) / 42 = 60 + 1.93 = **61.9**
- ATL = 65 + (141 − 65) / 7 = 65 + 10.9 = **75.9**
- 明天早上的 TSB = 61.9 − 75.9 = **−14**（基礎期的下限是 −30，還有空間）

每週 CTL 漲幅（ramp rate）= 今天 CTL − 7 天前 CTL；若 7 天前是 58.0，就是 +3.9/週，在基礎期上限 6 以內。

## 限制
- 所有壓力只有一種幣別（TSS）；一趟 141 TSS 的 Z2 長騎和一趟 141 TSS 的 VO2 課在模型裡一樣，但恢復需求完全不同。所以守衛另外限制「高強度次數」與「間隔 48 小時」。
- τ = 42/7 是通用預設值，個人差異很大（文獻中體能 τ 從 30 到 60 天都有）。我們不自動擬合，但週報會比較 readiness 與 TSB 的關係，供人工判斷。
- 模型不知道睡眠、生病、工作壓力；這些由 `readiness_v1` 補上。
- TSB 很正不代表「狀態好」，可能只是沒練；要配 CTL 一起看。

## 參考文獻
- Banister EW, Calvert TW, Savage MV, Bach T. "A systems model of training for athletic performance." *Australian Journal of Sports Medicine* 7(3):57–61, 1975.
- Banister EW. "Modeling elite athletic performance." In: MacDougall JD, Wenger HA, Green HJ (eds.), *Physiological Testing of Elite Athletes*, Human Kinetics, 1991, pp. 403–424.
- Morton RH, Fitz-Clarke JR, Banister EW. "Modeling human performance in running." *Journal of Applied Physiology* 69(3):1171–1177, 1990.
- Allen H, Coggan A, McGregor S. *Training and Racing with a Power Meter*, 3rd ed., 2019, 第 9 章（PMC）。
- Clarke DC, Skiba PF. "Rationale and resources for teaching the mathematical modeling of athletic training and performance." *Advances in Physiology Education* 37:134–152, 2013.
- intervals.icu 論壇：「Fitness, Fatigue and Form」說明文（David Tinker），含 icu 使用的指數公式。
