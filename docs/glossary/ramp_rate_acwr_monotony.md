# ramp_rate_acwr_monotony · 負荷安全指標：CTL 漲幅、急慢性負荷比（ACWR）、單調性與張力

## 這是什麼
三個「你加太快了嗎」的警示燈，從不同角度看同一件事：

- **Ramp rate（CTL 漲幅）**：CTL 一週漲了多少。
- **ACWR（Acute:Chronic Workload Ratio，急慢性負荷比）**：最近 7 天平均負荷 ÷ 之前 21–28 天平均負荷。>1.3–1.5 代表這週比身體習慣的多很多。
- **Monotony（單調性）與 Strain（張力）**：Foster 提出。單調性 = 一週每日負荷的平均 ÷ 標準差，天天差不多重（沒有輕重交替）會很高；張力 = 週總負荷 × 單調性。

## 為什麼用它
PMC 的 TSB 只告訴你「累不累」，不告訴你「是怎麼累的」。文獻一致指出：負荷**變化的速度**比負荷**絕對值**更能預測受傷與過度訓練。排程器每天把候選課表丟進這三個指標，超線就移除價值最低的 TSS（docs/05 §3 步驟 3、§5）。

## 怎麼算
```
ramp_rate = CTL_今天 − CTL_7天前                          （守衛：基礎/建構 ≤ 6，閾值期 ≤ 4）
ACWR      = mean(TSS 最近 7 天) / mean(TSS 第 8–28 天)     （「非耦合」版本：分母不含最近 7 天；守衛 ≤ 1.5）
monotony  = mean(TSS 最近 7 天) / sd(TSS 最近 7 天)        （> 2.0 警示）
strain    = sum(TSS 最近 7 天) × monotony
```

**用你的數據舉例**：這週七天的 TSS 是 141（週日長騎 2 h NP 210 W）、0、65、81、0、168、43。
- 週總量 498，平均 71.1，標準差 60.1 → **monotony 1.18**（有輕重交替，健康）、**strain 588**。
- 前 21 天總 TSS 1 302 → 平均 62.0 → **ACWR = 71.1 / 62.0 = 1.15**（0.8–1.3 的「甜蜜區」內）。
- CTL 從 58.0 → 61.9 → **ramp +3.9/週**，基礎期上限 6 以內。

三個燈都是綠的，排程器可以照進程走。如果下週想排 600 TSS，ACWR 會變成 85.7/64 ≈ 1.34、ramp 約 +6，守衛會把週量壓回去。

## 限制
- ACWR 近年受到方法學批評（Impellizzeri 2020）：比值掩蓋了絕對負荷、對低負荷期過敏（從很少到一點點就會爆表）、而且原始研究是團隊運動的受傷資料，不是自行車的過度訓練資料。我們用它當**警示**而不是當目標。
- 單調性只看一週、只看 TSS；一週裡每天 Z2 90 分鐘 monotony 很高，但實際上風險不大。守衛要求每週 ≥ 1 天休息主要是為了這個。
- 其他運動（重訓）的負荷進了同一個池子，比值會被稀釋或放大。
- 所有指標都依賴 TSS 正確（見 `coggan_np_if_tss` 的限制）。

## 參考文獻
- Foster C. "Monitoring training in athletes with reference to overtraining syndrome." *Medicine & Science in Sports & Exercise* 30(7):1164–1168, 1998.（單調性、張力）
- Gabbett TJ. "The training—injury prevention paradox: should athletes be training smarter and harder?" *British Journal of Sports Medicine* 50(5):273–280, 2016.（ACWR 甜蜜區 0.8–1.3）
- Hulin BT, Gabbett TJ, Lawson DW, Caputi P, Sampson JA. "The acute:chronic workload ratio predicts injury." *BJSM* 50(4):231–236, 2016.
- Impellizzeri FM, Tenan MS, Kempton T, Novak A, Coutts AJ. "Acute:chronic workload ratio: conceptual issues and fundamental pitfalls." *International Journal of Sports Physiology and Performance* 15(6):907–913, 2020.（批評）
- Lolli L, et al. "Mathematical coupling causes spurious correlation within the conventional acute-to-chronic workload ratio calculations." *BJSM* 53(15):921–922, 2019.（非耦合版本的理由）
- intervals.icu：wellness `rampRate` 欄位說明（Fitness 頁面）。
