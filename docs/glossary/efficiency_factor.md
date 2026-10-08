# efficiency_factor · 效率因子（EF）

## 這是什麼
**EF（Efficiency Factor）= 標準化功率 ÷ 平均心率**，單位是「每一下心跳換到多少瓦」。同樣的心率能踩出更多瓦，代表有氧系統更有效率。它是 Friel 推廣的指標，intervals.icu 在每趟活動與每個區間都會顯示。

## 為什麼用它
FTP 測驗每 4 週一次，EF 每趟 Z2 都有。整季在**相同強度**（Z2，IF 0.62–0.72）、**相同條件**（戶外、相近溫度）下追蹤 EF，是看有氧基礎有沒有進步最便宜的方法；基礎期的目標之一就是「Z2 的 EF 上升」。我們也在長騎裡追蹤「累積 1 000 kJ 之後的 EF」作為耐久力指標（docs/04 §3）。

## 怎麼算
```
EF = NP / 平均心率            （整趟或某區間）
EF_1000kJ = 累積功超過 1 000 kJ 之後那段的 NP / 平均心率
```
趨勢只比較「同類型」的騎乘：以 (intent, 時長帶) 分組建立基準（docs/04 §1 的 `longitudinal/compare.py`），例如「戶外 Z2 90–150 分鐘」。

**用你的數據舉例**：2 小時 NP 210 W、平均心率 143 bpm → **EF = 210 / 143 = 1.47 W/bpm**。
- 若 8 週前同類型的騎乘 EF 是 1.38，現在 1.47 → +6.5 %，和 FTP 從 250 到 262 的預期（+4.8 %）方向一致，是有氧基礎進步的佐證。
- 這趟累積到 1 000 kJ 的時間點大約在 80 分鐘；之後 40 分鐘 NP 207 W、心率 148 → EF_1000kJ = 1.40。與整趟 1.47 相比掉了 4.8 %，這個差距隨訓練縮小就是耐久力在進步。

## 限制
- 心率的日間變異（熱、睡眠、咖啡因、脫水）有 ±5 bpm 很正常，等於 EF ±3–4 %；單趟不要解讀，看 4–6 週的趨勢。
- 強度不同不能比：高強度趟的 EF 本來就高（心率上限壓住了分母）。所以只在 Z2 類別內比。
- 戶外 vs 室內不能直接比（室內心率通常高 5–8 bpm）。
- 心率隨年齡與訓練年資下降（最大心率下降）本身會讓 EF 微幅上升，不全是體能。
- 無功率的騎乘沒有 EF。

## 參考文獻
- Friel J. "Efficiency Factor and Decoupling." joefrieltraining.com，2009；*The Cyclist's Training Bible*, 5th ed., 2018.
- Allen H, Coggan A, McGregor S. *Training and Racing with a Power Meter*, 3rd ed., 2019（Power:HR 相關章節）。
- Lucía A, Hoyos J, Pérez M, Santalla A, Chicharro JL. "Inverse relationship between VO2max and economy/efficiency in world-class cyclists." *MSSE* 34(12):2079–2084, 2002.（效率與表現的關係）
- Maunder E, et al. "The importance of 'durability' in the physiological profiling of endurance athletes." *Sports Medicine* 51:1619–1628, 2021.（EF 在累積功之後的衰退作為耐久力）
- intervals.icu：activity 與 interval 欄位 `efficiency_factor`（NP/HR）。
