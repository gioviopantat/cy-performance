# periodization_3_1 · 週期化與 3:1 負荷節奏

## 這是什麼
**週期化（periodization）**是把一季切成有不同目的的區塊（基礎 → 建構 → 專項／閾值 → 測驗或比賽），每個區塊再切成「加壓幾週、放鬆一週」的循環。**3:1** 指三週負荷逐步上升、第四週刻意減量（量 −40 %、高強度 −1 次），讓累積的疲勞散掉、適應浮現。替代方案 **2:1** 用在恢復較慢、或 readiness 常出現 BLUNTED 的選手。

## 為什麼用它
身體在「壓力 → 恢復」的循環裡變強，不是在壓力裡變強。沒有恢復週的連續加壓在 PMC 上看得很清楚：TSB 一路往 −30 掉、ramp rate 超標、守衛天天在砍 TSS。3:1 把恢復排進日曆而不是等身體抗議。它也給測驗一個自然的位置：恢復週的第 3–5 天是測 FTP 最公平的時間點（docs/05 §2.4 把測驗放在第 4、8、12… 週）。

## 怎麼算
排程器對每個區塊做的事（`season.py`，docs/05 §2.1）：

```
週目標 TSS_k = f( CTL_now, ramp_target )      k = 1..3 加壓週，依 PMC 閉式解反推能讓 CTL 每週漲 ramp_target 的週 TSS
週目標 TSS_4 = 0.6 × TSS_3                     恢復週（量 −40 %）
高強度次數  = 階段上限（基礎 1、建構 2、閾值 2）；恢復週 −1
```
反推公式：CTL 一週要漲 Δ，平均每日 TSS 大約要是 `CTL + Δ × 42 / 7 = CTL + 6Δ`。

**用你的數據舉例**：基礎期第一個 3:1，起始 CTL 55，ramp 目標 +4/週。
- 每日平均 TSS ≈ 55 + 6×4 = 79 → **週 1 ≈ 480 TSS**（約 10.5 小時，IF 0.68 的 Z2 為主，含一次甜蜜點 3×8）
- 週 2 ≈ 500、週 3 ≈ 525（CTL 到約 67）
- **週 4 恢復 ≈ 315 TSS**（−40 %），只保留長騎縮短版 + Z2；CTL 微降到約 65，TSB 從 −20 回到 +5 左右，第 4 週週四做 Ramp 測驗。
- 那趟 2 小時 NP 210 W 的騎乘（141 TSS）在加壓週是「週日次長騎」的典型大小，在恢復週就會換成 90 分鐘 Z2（65 TSS）。

區塊之間如果檢查點沒達標（差 > 3 %），下一個區塊**重複前兩週**而不是進階（docs/05 §2.4）。

## 限制
- 3:1 是慣例不是定律。對 40 歲以上、睡眠不穩、或有重訓/工作壓力的人，2:1 常常更好；系統會在「最近 3 週有 2 週 BLUNTED 或低完成率」時自動插入恢復週並降 ramp（docs/05 §4）。
- 以週為單位是行事曆方便，不是生理週期；身體不知道今天星期幾。
- 恢復週減量 40 % 是中位數建議，Mujika 的減量研究顯示 40–60 % 都有效，關鍵是保留強度、砍時間。
- 傳統線性週期化（Bompa）與區塊週期化（Issurin）在精英選手上各有證據；對一週 ≤ 15 小時、沒有比賽的業餘選手，差異遠小於「有沒有持續 26 週不中斷」。

## 參考文獻
- Bompa TO, Buzzichelli C. *Periodization: Theory and Methodology of Training*, 6th ed. Human Kinetics, 2018.
- Issurin VB. "New horizons for the methodology and physiology of training periodization." *Sports Medicine* 40(3):189–206, 2010.
- Mujika I, Padilla S. "Scientific bases for precompetition tapering strategies." *MSSE* 35(7):1182–1187, 2003.
- Bosquet L, Montpetit J, Arvisais D, Mujika I. "Effects of tapering on performance: a meta-analysis." *MSSE* 39(8):1358–1365, 2007.
- Friel J. *The Cyclist's Training Bible*, 5th ed., VeloPress, 2018.（3:1 / 2:1 週結構的實務版本）
- Allen H, Coggan A, McGregor S. *Training and Racing with a Power Meter*, 3rd ed., 2019, 第 9–10 章（用 PMC 規劃區塊）。
