# decoupling_hr_lag · 有氧解耦（Pw:Hr decoupling）與心率延遲（HR lag）

## 這是什麼
兩個用「功率 vs 心率」看身體狀態的指標：

- **有氧解耦（aerobic decoupling, Pw:Hr）**：一趟穩定騎乘的後半段，心率是不是比前半段「貴」了——同樣的功率要用更高的心率。數字越大代表後段漂移越多，耐久力（durability）越差，或當天沒恢復、脫水、太熱。
- **心率延遲（HR lag）**：你加大功率之後，心率要幾秒才跟上。它反映自主神經的反應速度；恢復不足或疲勞時心率會「鈍」（遲遲上不去、或整趟異常低），我們稱為 BLUNTED。

## 為什麼用它
這位選手的目標是 GC 型的耐久力，而 FTP 測驗只量新鮮狀態。解耦是每一趟長騎都能免費量到的耐久力指標，整季追蹤它（在相同強度、累積相同 kJ 之後）比 FTP 更能看出長騎能力有沒有進步。HR lag 與心率鈍化則是 `readiness_v1` 的輸入之一：昨天的騎乘心率明顯鈍化 → 今天建議 EASY。

## 怎麼算
**解耦**（Friel 的定義，icu 的 `decoupling` 欄位採同一精神）：
```
EF_前半 = NP_前半 / 平均心率_前半
EF_後半 = NP_後半 / 平均心率_後半
decoupling % = (EF_前半 − EF_後半) / EF_前半 × 100
```
只在「穩定、有氧」的區段算（IF < 0.85、沒有長停等）；間歇課不算。

**HR lag**：取功率與心率的 1 Hz 序列，對功率的「階躍」（例如從 Z2 跳到 Z4）找心率上升到變化量 63 % 所需的秒數；或對整趟做交叉相關，找讓兩條曲線最吻合的時間位移。正常約 30–60 秒，疲勞或鈍化時 > 90 秒或整趟相關性很差。

**用你的數據舉例**：2 小時 NP 210 W 的騎乘。
- 前 1 小時：NP 212 W、平均心率 140 → EF 1.514
- 後 1 小時：NP 208 W、平均心率 147 → EF 1.415
- decoupling = (1.514 − 1.415) / 1.514 = **6.5 %**（> 5 %，表示在這個強度 2 小時後有氧狀態已開始「漂」；若這是在高溫下騎的，先懷疑熱與水分）
- 同一趟在 3 次 Z2 → 坡段的功率階躍上，心率平均 42 秒跟上 → **lag 42 秒，正常**。若某天 lag 95 秒且整趟心率比同功率低 8 bpm → 標記 BLUNTED，隔天 readiness 規則直接給 EASY。

## 限制
- 心率受熱、脫水、咖啡因、睡眠、情緒影響；單趟 6 % 的解耦不等於耐久力差，要看趨勢（同強度、同天氣下連續幾週）。
- 短於 60–90 分鐘的騎乘解耦意義不大；停等多的市區騎乘也會失真。
- HR lag 需要乾淨的功率階躍，自由起伏的戶外騎乘常常找不到合格的階躍，那天就沒有值（缺值不算壞值）。
- 光學心率（手錶）在高迴轉、流汗時會漂，盡量用胸帶。

## 參考文獻
- Friel J. "Aerobic Endurance and Decoupling." joefrieltraining.com 部落格，2009；以及 *The Cyclist's Training Bible*, 5th ed., VeloPress 2018（Pw:Hr 5 % 準則）。
- Maunder E, Seiler S, Mildenhall MJ, Kilding AE, Plews DJ. "The importance of 'durability' in the physiological profiling of endurance athletes." *Sports Medicine* 51:1619–1628, 2021.
- Spragg J, Leo P, Swart J. "The relationship between training characteristics and durability in professional cyclists across a competitive season." *European Journal of Sport Science* 23(4):489–498, 2023.
- Bearden SE, Moffatt RJ. "VO2 and heart rate kinetics in cycling: transitions from an elevated baseline." *Journal of Applied Physiology* 90:2081–2087, 2001.（心率動力學時間常數）
- Buchheit M. "Monitoring training status with HR measures: do all roads lead to Rome?" *Frontiers in Physiology* 5:73, 2014.
- intervals.icu：activity 欄位 `decoupling`、`GET /activity/{id}/power-vs-hr`。
