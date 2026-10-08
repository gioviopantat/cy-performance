# time_in_zone_tid · 區間時間與訓練強度分布（TID）

## 這是什麼
- **時間在區間（Time in Zone, TIZ）**：一趟或一週裡，每個功率（或心率）區間各待了幾分鐘。我們用 icu sport settings 的 7 區 Coggan 功率區間（Z1 < 55 %、Z2 56–75、Z3 76–90、Z4 91–105、Z5 106–120、Z6 121–150、Z7 > 150 % FTP）。
- **訓練強度分布（Training Intensity Distribution, TID）**：把 7 區合併成 3 區（低／中／高）後一週的比例。常見模型：
  - **金字塔（pyramidal）**：低最多、中其次、高最少，例如 80/15/5。
  - **極化（polarized）**：低最多、高其次、中最少，例如 80/5/15。
  - **閾值型（threshold）**：中間最多——通常被認為效果最差、疲勞最高。
- **極化指數（Polarization Index, PI）**：Treff 2019 提出，把 3 區比例壓成一個數字，> 2.0 算極化。

## 為什麼用它
排程器每個階段都有目標 TID（docs/05 §2.1：基礎期 80/15/5、建構期 75/15/10 金字塔；閾值期 75/5/20 偏極化）。週報把實際 TID 和目標比，若「中區」偏高（典型的業餘選手病：每天都騎 Tempo）就是下週把 Z2 騎得更慢的訊號。這也是守衛「每週高強度 ≤ 1–2 次」背後的理由。

## 怎麼算
1. 1 Hz 功率序列對照區間邊界累加秒數（icu 欄位 `icu_zone_times`；心率版 `icu_hr_zone_times`）。
2. 3 區映射：**低** = Z1+Z2（< 76 % FTP，約 LT1 以下）、**中** = Z3+Z4（76–105 %，LT1–LT2 之間）、**高** = Z5+（> 105 %，LT2 以上）。
3. 一週把所有騎乘的秒數加總再換成百分比。
4. `PI = log10( (低/中) × 高 × 100 )`，三個都用 0–1 的比例；> 2.00 為極化。

**用你的數據舉例**：這週總騎乘 675 分鐘，其中低區 540 分、中區 100 分、高區 35 分。
- TID = **80 / 15 / 5 %**，符合基礎期目標。
- PI = log10( (0.80 / 0.15) × 0.05 × 100 ) = log10(26.7) = **1.43** → 不是極化，是金字塔，正確。
- 那趟 2 小時 NP 210 W 的騎乘本身：Z2 95 分、Z3 20 分、Z4 5 分 → 它貢獻的「中區」25 分鐘幾乎就是整週中區的四分之一，說明戶外 Z2 的坡段很容易把 TID 推向中間。

## 限制
- 用功率區間近似 LT1/LT2 是粗的；真正的 3 區要用乳酸或通氣閾值測。75 % FTP 當 LT1 對有氧基礎好的人偏低、對新手偏高。
- 以「時間」計算 vs 以「次數」計算（Seiler 原始研究用每次訓練的主要強度歸類）會得到很不一樣的分布；同樣的一週用時間算像金字塔、用次數算像極化。我們用時間，報告會註明。
- 間歇課的休息段會被算成低區，讓高強度日看起來「比較低」。
- 無功率的騎乘用心率區間替代，心率延遲會低估高區時間。

## 參考文獻
- Seiler KS, Kjerland GØ. "Quantifying training intensity distribution in elite endurance athletes: is there evidence for an 'optimal' distribution?" *Scandinavian Journal of Medicine & Science in Sports* 16(1):49–56, 2006.
- Seiler S. "What is best practice for training intensity and duration distribution in endurance athletes?" *International Journal of Sports Physiology and Performance* 5(3):276–291, 2010.
- Stöggl T, Sperlich B. "Polarized training has greater impact on key endurance variables than threshold, high intensity, or high volume training." *Frontiers in Physiology* 5:33, 2014.
- Treff G, Winkert K, Sareban M, Steinacker JM, Sperlich B. "The polarization-index: a simple calculation to distinguish polarized from non-polarized training intensity distributions." *Frontiers in Physiology* 10:707, 2019.
- Burnley M, Bearden SE, Jones AM. "Polarized training is not optimal for endurance athletes." / Foster C, et al. "Polarized training is optimal for endurance athletes." *MSSE* 54(6), 2022.（正反辯論）
- Allen H, Coggan A, McGregor S. *Training and Racing with a Power Meter*, 3rd ed., 2019（7 區定義）。
- intervals.icu：欄位 `icu_zone_times`, `icu_hr_zone_times`, `polarization_index`。
