# ramp_rate_acwr_monotony · 負荷安全指標：CTL 漲幅、急慢性負荷比（ACWR）、單調性與張力

## 這是什麼
三個「你加太快了嗎」的警示燈，從不同角度看同一件事：

- **Ramp rate（CTL 漲幅）**：CTL 一週漲了多少。
- **ACWR（Acute:Chronic Workload Ratio，急慢性負荷比）**：最近 7 天平均負荷 ÷ 之前 21–28 天平均負荷。>1.3–1.5 代表這週比身體習慣的多很多。
- **Monotony（單調性）與 Strain（張力）**：Foster 提出。單調性 = 一週每日負荷的平均 ÷ 標準差，天天差不多重（沒有輕重交替）會很高；張力 = 週總負荷 × 單調性。

## 為什麼用它
PMC 的 TSB 只告訴你「累不累」，不告訴你「是怎麼累的」。這三個指標描述負荷**變化的速度**；「變化太快會受傷或過度訓練」的研究多來自團隊運動或單一研究（見下方證據等級，都是 C），所以只有 CTL 漲幅進入守衛（docs/05 §3 步驟 3、§5，超線就移除價值最低的 TSS），ACWR 與單調性只在報告裡當參考。

## 怎麼算
```
ramp_rate = CTL_今天 − CTL_7天前                          （守衛：基礎/建構 ≤ 6，閾值期 ≤ 4）
ACWR      = mean(TSS 最近 7 天) / mean(TSS 第 8–28 天)     （「非耦合」版本：分母不含最近 7 天；只當參考，守衛不用）
monotony  = mean(TSS 最近 7 天) / sd(TSS 最近 7 天)        （> 2.0 警示）
strain    = sum(TSS 最近 7 天) × monotony
```

**用你的數據舉例**：這週七天的 TSS 是 141（週日長騎 2 h NP 210 W）、0、65、81、0、168、43。
- 週總量 498，平均 71.1，標準差 60.1 → **monotony 1.18**（有輕重交替，健康）、**strain 588**。
- 前 21 天總 TSS 1 302 → 平均 62.0 → **ACWR = 71.1 / 62.0 = 1.15**（0.8–1.3 的「甜蜜區」內）。
- CTL 從 58.0 → 61.9 → **ramp +3.9/週**，基礎期上限 6 以內。

三個燈都是綠的，排程器可以照進程走。如果下週想排 600 TSS，ACWR 會變成 85.7/64 ≈ 1.34（報告標示參考）、ramp 約 +6，守衛會依 ramp 上限把週量壓回去。

## 限制
- ACWR 近年受到方法學批評（Impellizzeri 2020）：比值掩蓋了絕對負荷、對低負荷期過敏（從很少到一點點就會爆表）、而且原始研究是團隊運動的受傷資料，不是自行車的過度訓練資料。我們用它當**警示**而不是當目標。
- 單調性只看一週、只看 TSS；一週裡每天 Z2 90 分鐘 monotony 很高，但實際上風險不大。守衛要求每週 ≥ 1 天休息主要是為了這個。
- 其他運動（重訓）的負荷進了同一個池子，比值會被稀釋或放大。
- 所有指標都依賴 TSS 正確（見 `coggan_np_if_tss` 的限制）。

## 證據等級
等級 A／B 可以單獨支撐報告裡的判斷；C 只當參考，報告標示「參考（證據弱）」（定義見 [docs/07 §6](../07-explainability.md)）。標「（提議）」的是文獻月報建議新增、等 Roger 核准的主張，等級已經生效。這張表與 `src/cyp/core/evidence.py` 必須一致（測試會檢查）。

| 主張 | 等級 | 依據 | 最後審查 |
|------|------|------|----------|
| `ramp_rate_acwr_monotony.acwr_injury` ACWR > 1.3 代表受傷風險較高 | **C** | Impellizzeri et al. 2020 IJSPP（概念缺陷）；Andrade, Wik et al. 2020 Sports Med（[PMID 32572824](https://pubmed.ncbi.nlm.nih.gov/32572824/)，有關聯、方法差異大）；Qin, Li & Chen 2025（[PMID 41029871](https://pubmed.ncbi.nlm.nih.gov/41029871/)）有關聯但建議謹慎；全是團隊運動受傷資料，沒有自行車 | 2026-10-16 |
| `ramp_rate_acwr_monotony.monotony_overtraining` 單調性／張力（Foster 1998）能標示過度訓練 | **C** | 只有原始的單一研究；未找到自行車驗證 | 2026-10-16 |
| `ramp_rate_acwr_monotony.ramp_cap` 每週 CTL 漲幅上限能讓加量保持安全 | **C** | 教練經驗法則 | 2026-10-16 |
| `ramp_rate_acwr_monotony.acwr_low` ACWR < 0.8 代表負荷明顯下降，非恢復週時值得留意 | **C**（提議） | Gabbett 2016 BJSM（團隊運動的「甜蜜區」0.8–1.3） | 2026-10-16 |

## 參考文獻
- Foster C. "Monitoring training in athletes with reference to overtraining syndrome." *Medicine & Science in Sports & Exercise* 30(7):1164–1168, 1998.（單調性、張力）
- Gabbett TJ. "The training—injury prevention paradox: should athletes be training smarter and harder?" *British Journal of Sports Medicine* 50(5):273–280, 2016.（ACWR 甜蜜區 0.8–1.3）
- Hulin BT, Gabbett TJ, Lawson DW, Caputi P, Sampson JA. "The acute:chronic workload ratio predicts injury." *BJSM* 50(4):231–236, 2016.
- Impellizzeri FM, Tenan MS, Kempton T, Novak A, Coutts AJ. "Acute:chronic workload ratio: conceptual issues and fundamental pitfalls." *International Journal of Sports Physiology and Performance* 15(6):907–913, 2020.（批評）
- Lolli L, et al. "Mathematical coupling causes spurious correlation within the conventional acute-to-chronic workload ratio calculations." *BJSM* 53(15):921–922, 2019.（非耦合版本的理由）
- intervals.icu：wellness `rampRate` 欄位說明（Fitness 頁面）。
