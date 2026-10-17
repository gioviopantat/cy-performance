# time_in_zone_tid · 區間時間與訓練強度分布（TID）

## 這是什麼
- **時間在區間（Time in Zone, TIZ）**：一趟或一週裡，每個功率（或心率）區間各待了幾分鐘。我們用 icu sport settings 的 7 區 Coggan 功率區間（Z1 < 55 %、Z2 56–75、Z3 76–90、Z4 91–105、Z5 106–120、Z6 121–150、Z7 > 150 % FTP）。
- **訓練強度分布（Training Intensity Distribution, TID）**：一週的訓練在低／中／高三區的比例。有兩種算法：**以課次計**（每趟依它的主要強度算一次，Seiler 原始研究的算法，我們拿它跟目標比）和**以時間計**（7 區秒數合併成 3 區，只當參考）。常見模型：
  - **金字塔（pyramidal）**：低最多、中其次、高最少，例如 80/15/5。
  - **極化（polarized）**：低最多、高其次、中最少，例如 80/5/15。
  - **閾值型（threshold）**：中間最多——通常被認為效果最差、疲勞最高。
- **極化指數（Polarization Index, PI）**：Treff 2019 提出，把 3 區比例壓成一個數字，> 2.0 算極化。

## 為什麼用它
排程器每個階段都有目標 TID，**以課次計**（docs/05 §2.1：基礎期 80/15/5、建構期 75/15/10 金字塔；閾值期 75/5/20 偏極化；ADR-0014）。這些比例來自以課次計的文獻：以時間計，閾值期 20 % 在高區幾乎做不到——WorldTour 車手以功率時間計高於 CP 的只有 4–7 %（Mateo-March 2026）。週報把這週的課次比例和目標比，若「中強度課」偏多（典型的業餘選手病：每天都騎 Tempo）就是下週把 Z2 騎得更慢的訊號。這也是守衛「每週高強度 ≤ 1–2 次」背後的理由。

## 怎麼算
**以課次計（跟目標比的那個）**：每趟騎乘算一次（`analysis/longitudinal/tid.py` 的 `session_zone`）。
1. **有排課的騎乘看課表的目的**（文獻的「課的目標」算法）：恢復騎、耐力騎、賽前開腿 → **低**；節奏、甜蜜點 → **中**；閾值、VO2、無氧、測驗 → **高**。哪一趟算「執行了課表」跟騎乘報告一樣：先找配對到這趟的課，沒有就由當天最長的那趟對當天第一個課。
2. **沒排課的騎乘看實際騎了什麼**，而且「高」要求比較嚴：Z5+ 累積 ≥ 10 分鐘，**而且**至少 3 段持續的高強度（30 秒功率在 Z5 下限以上連續 ≥ 60 秒）才算高；零星的爬坡衝刺不算。否則 Z3+Z4 ≥ 20 分鐘 → 中；否則 → 低。
3. 功率計被判定不可靠的騎乘沒有「持續段數」，不會被算成高。
4. 一週：低／中／高各幾趟，除以總趟數。

**以時間計（參考）**：
1. 1 Hz 功率序列對照區間邊界累加秒數（icu 欄位 `icu_zone_times`；心率版 `icu_hr_zone_times`）。
2. 3 區映射：**低** = Z1+Z2（< 76 % FTP，約 LT1 以下）、**中** = Z3+Z4（76–105 %，LT1–LT2 之間）、**高** = Z5+（> 105 %，LT2 以上）。
3. 一週把所有騎乘的秒數加總再換成百分比。
4. `PI = log10( (低/中) × 高 × 100 )`，三個都用 0–1 的時間比例；> 2.00 為極化。PI 與「金字塔／極化」標籤只是描述。

**用你的數據舉例**：這週 5 趟、總騎乘 675 分鐘：一趟排好的 4×4 分鐘 VO2 課（Z5+ 16 分）、一趟排好的 2 小時耐力騎（NP 210 W；Z2 95 分、Z3 20 分、Z4 5 分）、三趟沒排課的爬坡騎（各有 5–10 分鐘零星的 Z5+，但沒有 3 段 ≥ 60 秒，Z3+Z4 也不到 20 分鐘）。
- 以課次計 = 低 4、中 0、高 1 趟 = **80 / 0 / 20 %** → 跟閾值期目標 75/5/20 比，高強度課剛好、中強度課少 5 個百分點。
- 以時間計 = 低 540 分、中 100 分、高 35 分 = 80 / 15 / 5 %（參考）。同一週以時間看高區只有 5 %，這就是為什麼目標不能拿時間比。
- PI = log10( (0.80 / 0.15) × 0.05 × 100 ) = log10(26.7) = **1.43** → 時間上是金字塔。
- 那趟 2 小時的騎乘貢獻了 25 分鐘中區時間，但以課次計它是一趟低強度課；中區時間偏多由限制因子「灰色地帶」（近 4 週中區時間 > 30 %）提醒。

## 限制
- 用功率區間近似 LT1/LT2 是粗的；真正的 3 區要用乳酸或通氣閾值測。75 % FTP 當 LT1 對有氧基礎好的人偏低、對新手偏高。
- 以「時間」計算 vs 以「次數」計算會得到很不一樣的分布（Sylta 2014：中高區比例差約 3 倍）；同樣的一週用時間算像金字塔、用次數算像極化。目標以次數比（ADR-0014，2026-10-16 起）；以時間的比例和限制因子「灰色地帶」仍用時間。
- 有排課的騎乘照課表的目的算，所以排了 VO2 卻騎得很輕鬆，仍算一趟高強度課（文獻也是這樣算）；有沒有真的練到，看騎乘報告的「課表 vs 實際」。沒排課時的 10 分鐘／3 段／60 秒／20 分鐘是我們定的。
- 以時間計時，間歇課的休息段會被算成低區，讓高強度日看起來「比較低」。
- 無功率的騎乘用心率區間替代，心率延遲會低估高區時間。

## 證據等級
等級 A／B 可以單獨支撐報告裡的判斷；C 只當參考，報告標示「參考（證據弱）」（定義見 [docs/07 §6](../07-explainability.md)）。標「（提議）」的是文獻月報建議新增、等 Roger 核准的主張，等級已經生效。這張表與 `src/cyp/core/evidence.py` 必須一致（測試會檢查）。

| 主張 | 等級 | 依據 | 最後審查 |
|------|------|------|----------|
| `time_in_zone_tid.low_volume` 大部分訓練量在低強度 | **A** | Seiler 2010 IJSPP；Seiler & Kjerland 2006 | 2026-10-16 |
| `time_in_zone_tid.polarized_superior` 極化優於金字塔分布（證據偏向否定） | **C** | Oliveira et al. 2024 Sports Med 統合分析（17 篇，[PMC11329428](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11329428/)，合併數字未讀）；Rosenblat et al. 2025 Sports Med（13 篇，[PMID 39888556](https://pubmed.ncbi.nlm.nih.gov/39888556/)）：VO2max 與計時賽無差異。PI > 2 只當描述，不當目標 | 2026-10-16 |
| `time_in_zone_tid.phase_targets` 各階段 TID 目標 80/15/5、75/15/10、75/5/20（以課次計；時間只當參考） | **C**（提議） | Seiler & Kjerland 2006（以課的目標歸類）；Mateo-March et al. 2026 Biol Sport：WorldTour 車手以時間計高於 CP 只有 4–7 %；Sylta et al. 2014（[PMID 24408353](https://pubmed.ncbi.nlm.nih.gov/24408353/)）：兩種算法差約 3 倍 | 2026-10-16 |

## 參考文獻
- Seiler KS, Kjerland GØ. "Quantifying training intensity distribution in elite endurance athletes: is there evidence for an 'optimal' distribution?" *Scandinavian Journal of Medicine & Science in Sports* 16(1):49–56, 2006.
- Seiler S. "What is best practice for training intensity and duration distribution in endurance athletes?" *International Journal of Sports Physiology and Performance* 5(3):276–291, 2010.
- Sylta Ø, Tønnessen E, Seiler S. "From heart-rate data to training quantification: a comparison of 3 methods of training-intensity analysis." *International Journal of Sports Physiology and Performance* 9(1):100–107, 2014.
- Stöggl T, Sperlich B. "Polarized training has greater impact on key endurance variables than threshold, high intensity, or high volume training." *Frontiers in Physiology* 5:33, 2014.
- Treff G, Winkert K, Sareban M, Steinacker JM, Sperlich B. "The polarization-index: a simple calculation to distinguish polarized from non-polarized training intensity distributions." *Frontiers in Physiology* 10:707, 2019.
- Burnley M, Bearden SE, Jones AM. "Polarized training is not optimal for endurance athletes." / Foster C, et al. "Polarized training is optimal for endurance athletes." *MSSE* 54(6), 2022.（正反辯論）
- Allen H, Coggan A, McGregor S. *Training and Racing with a Power Meter*, 3rd ed., 2019（7 區定義）。
- intervals.icu：欄位 `icu_zone_times`, `icu_hr_zone_times`, `polarization_index`。
