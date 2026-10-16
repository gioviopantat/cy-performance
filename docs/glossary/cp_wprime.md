# cp_wprime · 臨界功率（CP）與 W′（無氧工作容量）

## 這是什麼
把「你能撐多久、多大功率」畫成一條曲線的最簡模型：

- **CP（Critical Power，臨界功率）**：理論上可以「無限」維持的功率（實際約 30–60 分鐘）。它和 FTP 是近親，通常略高一點。
- **W′（唸 W-prime）**：CP 之上可以額外做的「功」，單位是焦耳（kJ）。像一個電池：超過 CP 就放電，低於 CP 就充電。
- **W′bal（W′ balance）**：Skiba 的延伸，逐秒追蹤電池剩多少；間歇課表「第幾趟會爆」可以由它預測。

## 為什麼用它
1. 它用兩個數字描述整條功率–時間曲線，讓我們能從日常騎乘的最佳努力（不用專門測驗）估出 FTP 的變化，與 icu eFTP 互相驗證。
2. W′ 告訴我們無氧能力是不是這位選手的限制（低 W′ 的人 20 分鐘測驗起步會「卡」，8×1 分課就是為了維持它）。
3. W′bal 讓騎乘報告能說「第 4 趟你的電池只剩 3 kJ，所以掉功率是正常的」。

## 怎麼算
兩參數模型（Monod & Scherrer 1965）：`P(t) = CP + W′ / t`，其中 t 是秒。
只要兩個不同時長的最佳努力就能解：

```
W′ = (P1 − P2) / (1/t1 − 1/t2)
CP = P2 − W′ / t2
```

**用你的數據舉例**：最近 42 天最佳 5 分鐘 320 W、最佳 20 分鐘 262 W。
- W′ = (320 − 262) / (1/300 − 1/1200) = 58 / 0.0025 = **23 200 J ≈ 23.2 kJ**
- CP = 262 − 23 200 / 1200 = 262 − 19.3 = **242.7 W**（≈ 3.8 W/kg，FTP 250 的 97 %）
- 驗算：5 分鐘預測 = 242.7 + 23 200/300 = 320 W ✓

三參數模型（Morton 1996）多加 **Pmax**（瞬間最大功率），修正短時間的高估：`t = W′ / (P − CP) − W′ / (Pmax − CP)`。icu 的 eFTP 與 `mmp-model` 用的是這一族。

W′bal（Skiba 2012 的積分式，icu 的 `wbal` 欄位）：在 CP 以上時 `W′bal` 以 (P − CP) 的速率下降；在 CP 以下時以時間常數 τ 指數回充，τ 取決於低於 CP 多少。

## 限制
- 兩參數模型對 < 2 分鐘的預測嚴重高估（上例會算出 1 分鐘 629 W，不可能），對 > 40 分鐘低估；只在 3–30 分鐘內可信。
- 擬合品質完全取決於「最佳努力」是不是真的全力。日常騎乘常常沒有真正的 5 分鐘全力，CP 會被低估；這就是為什麼我們每 4 週還是要測。
- CP 與 FTP 不是同一個東西（CP 偏向 30–40 分鐘功率），差 3–5 % 是正常的；報告中會分開列。
- W′ 的日間變異可達 ±15 %，單次數字不要過度解讀。

## 證據等級
等級 A／B 可以單獨支撐報告裡的判斷；C 只當參考，報告標示「參考（證據弱）」（定義見 [docs/07 §6](../07-explainability.md)）。標「（提議）」的是文獻月報建議新增、等 Roger 核准的主張，等級已經生效。這張表與 `src/cyp/core/evidence.py` 必須一致（測試會檢查）。

| 主張 | 等級 | 依據 | 最後審查 |
|------|------|------|----------|
| `cp_wprime.cp_boundary` CP 是重度／極重度強度的分界；2–15 分鐘內 P(t) = CP + W′/t 成立 | **A** | Jones et al. 2010 MSSE 回顧；Monod & Scherrer 1965 | 2026-10-16 |
| `cp_wprime.wbal_prediction` W′bal（Skiba 2012 單指數）能預測間歇第幾趟會爆 | **C** | Chorley, Bott, Marwood & Lamb 2022 EJAP 122(3):677–689：雙指數恢復更貼近、個體差異大、反覆極重度後恢復更慢（[PMC8854279](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC8854279/)）；Skiba & Clarke 2021 IJSPP 16(11):1561–1572（[PMID 34686611](https://pubmed.ncbi.nlm.nih.gov/34686611/)） | 2026-10-16 |

## 參考文獻
- Monod H, Scherrer J. "The work capacity of a synergic muscular group." *Ergonomics* 8(3):329–338, 1965.
- Moritani T, Nagata A, deVries HA, Muro M. "Critical power as a measure of physical work capacity and anaerobic threshold." *Ergonomics* 24(5):339–350, 1981.
- Morton RH. "A 3-parameter critical power model." *Ergonomics* 39(4):611–619, 1996.
- Skiba PF, Chidnok W, Vanhatalo A, Jones AM. "Modeling the expenditure and reconstitution of work capacity above critical power." *Medicine & Science in Sports & Exercise* 44(8):1526–1532, 2012.
- Jones AM, Vanhatalo A, Burnley M, Morton RH, Poole DC. "Critical power: implications for determination of VO2max and exercise tolerance." *MSSE* 42(10):1876–1890, 2010.（綜述）
- intervals.icu：activity 欄位 `icu_pm_cp`, `icu_pm_w_prime`, `icu_pm_p_max`；`GET /athlete/{id}/mmp-model`。
