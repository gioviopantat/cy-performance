# eftp · 估計 FTP（intervals.icu eFTP）

## 這是什麼
intervals.icu 從你**最近的最佳努力**（預設 42 天的功率曲線）用功率–時間模型反推出來的 FTP 估計值。它每次有新騎乘都會更新，不需要專門測驗。icu 在活動上給 `icu_eftp`（這一趟暗示的 FTP），在選手層級給整體 eFTP；當 eFTP 明顯高於設定的 FTP，icu 會在日曆上建議 `SET_EFTP`。

## 為什麼用它
我們的季節規則是「FTP 只在測驗與 eFTP 的檢查點重新估計」（docs/05 §2.4）。測驗每 4 週一次，中間若 FTP 其實已經上升，課表的 % 會全部偏低、TSS 會偏高。eFTP 是測驗之間的**連續證據**：若 icu eFTP 或我們自己的 CP 擬合連續兩週 ≥ FTP + 3 %，而且最近有一段 ≥ 20 分鐘的努力支持，週報就提出 `SET_EFTP` 提案（永不自動改 FTP，v1 規則）。

## 怎麼算
icu 的做法（依論壇說明；沒有公開的封閉公式）：
1. 取視窗內（預設 42 天）每個時長的最佳平均功率（MMP 曲線）。
2. 用選定的功率–時間模型擬合（預設 Morton 三參數 CP/W′/Pmax，也可選其他模型；見 `cp_wprime`）。
3. eFTP ≈ 模型預測在長時間（約 1 小時）可維持的功率，實務上非常接近擬合出來的 CP。
4. 單趟活動的 `icu_eftp` 則是只用這趟的最佳努力算出的 FTP 估計，所以一趟 Z2 的 `icu_eftp` 會很低，這是正常的——只有包含真正努力的騎乘才有意義。

我們的交叉檢查：用自己的 42 天與 90 天 MMP 做 2 參數與 3 參數擬合，和 icu eFTP 並列。

**用你的數據舉例**：`cp_wprime` 例子裡 42 天的最佳 5 分鐘 320 W、20 分鐘 262 W 給出 CP ≈ 243 W；假設 icu eFTP 顯示 247 W。設定 FTP 是 250 W：
- 247 / 250 = −1.2 %，243 / 250 = −2.9 % → 都在 ±3 % 內，**不提案**，課表照 250 走。
- 若兩週後 2×20 閾值課做到 262 W 平均、icu eFTP 變成 259 W（+3.6 %）並連續兩週維持 → 週報寫一行「建議 SET_EFTP 259」，由你在 icu 上確認。

## 限制
- eFTP 只會反映你**做過的**努力。基礎期幾乎沒有 ≥ 95 % 的長段落時，eFTP 會「漂低」，這不代表 FTP 掉了。所以規則要求「最近有 ≥ 20 分鐘努力支持」才採信。
- 42 天視窗意味著一次很好的測驗會「撐」6 週，之後掉下來。
- 模型對短努力（衝刺、1 分鐘）敏感；一次順風衝刺可能讓 Pmax 跳動並連帶影響 eFTP 幾瓦。
- eFTP、CP、20 分鐘 × 0.95、Ramp × 0.75 是四種不同的估法，彼此差 3–5 % 是常態。報告會四個並列，不會把其中一個當真理。

## 證據等級
等級 A／B 可以單獨支撐報告裡的判斷；C 只當參考，報告標示「參考（證據弱）」（定義見 [docs/07 §6](../07-explainability.md)）。標「（提議）」的是文獻月報建議新增、等 Roger 核准的主張，等級已經生效。這張表與 `src/cyp/core/evidence.py` 必須一致（測試會檢查）。

| 主張 | 等級 | 依據 | 最後審查 |
|------|------|------|----------|
| `eftp.ftp_mlss` FTP ≈ MLSS（區間所依據的閾值） | **B** | Borszcz et al. 2019 IJSPP（[PMID 30676826](https://pubmed.ncbi.nlm.nih.gov/30676826/)）：偏差 1.4 % ± 9.2 %（一致性界限），訓練程度越低越差；Lillo-Beviá et al. 2022 JSCR 36(1):167–173（[PMID 31714462](https://pubmed.ncbi.nlm.nih.gov/31714462/)）：20 分 × 0.95 仍與 MLSS 差約 12 W（摘要） | 2026-10-16 |
| `eftp.icu_value` intervals.icu 的 eFTP 數值能估計 FTP | **C** | 專有演算法，沒有公開驗證 | 2026-10-16 |
| `eftp.cp_to_ftp` FTP = 0.97 × CP | **C**（提議） | Karsten et al. 2021 Front Physiol（[PMC7862708](https://pmc.ncbi.nlm.nih.gov/articles/PMC7862708/)，n = 17） | 2026-10-16 |
| `eftp.proposal_3pct` 估計值高於 FTP 3 % 持續 14 天就提案調整 | **C**（提議） | Lillo-Beviá et al. 2022：最小可偵測變化約 3 %，剛好在雜訊邊緣 | 2026-10-16 |
| `eftp.ramp_075` Ramp 測驗 FTP = 0.75 × 最佳 1 分鐘 | **C**（提議） | 找不到同儕審查驗證 | 2026-10-16 |

## 參考文獻
- intervals.icu 論壇（David Tinker）："eFTP and power curve models"、"How is eFTP calculated?" 等討論串（forum.intervals.icu）；Settings → Power → "Estimate FTP" 說明。
- intervals.icu API：activity 欄位 `icu_eftp`, `icu_ftp`, `icu_pm_cp`, `icu_pm_w_prime`, `icu_pm_p_max`；`GET /athlete/{id}/mmp-model`；事件類別 `SET_EFTP`。
- Morton RH. "A 3-parameter critical power model." *Ergonomics* 39(4):611–619, 1996.
- Allen H, Coggan A, McGregor S. *Training and Racing with a Power Meter*, 3rd ed., 2019, 第 3 章（FTP 的多種測法與其差異）。
