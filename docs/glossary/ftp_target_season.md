# ftp_target_season · 「FTP 目標」季節類型（本季：250 → 300 W，26 週）

## 這是什麼
一種沒有比賽的季節：目標是一個數字（FTP 300 W、4.7 W/kg）和一個日期（2027-04-02），不是某場賽事。所以**沒有減量期、沒有專項期**，取而代之的是一條「閾值發展進程」加上每 4 週一次的測驗。它是排程器在 `config/athlete.yaml` 看到 `season.type: ftp_target` 時使用的骨架（docs/05 §2.4）。

## 為什麼用它
一般的賽季骨架（基礎 → 建構 → 專項 → 減量）是倒推比賽日設計的，對「把 FTP 推高 20 %」這種目標會浪費最後 4–5 週在減量與專項上。FTP 目標季把那些週拿來做更多閾值與 VO2 工作，並且用**固定節奏的測驗**（Ramp 第 4/12/20 週、20 分鐘第 8/16/24/26 週）把「有沒有進步」變成可量測的事，而不是到季末才發現。+20 % 對已經 3.9 W/kg 的選手是野心很大的目標，所以設計原則是「盡快知道答案」。

## 怎麼算
季節骨架（2026-10-05 起 26 週，每個區塊 3:1 ×2）：

| 週 | 區塊 | 主要刺激 | 高強度/週 | 檢查點（報告預期值） |
|----|------|----------|-----------|----------------------|
| 1–8 | 基礎 | Z2 把 CTL 推向 15 h 上限；1 次甜蜜點 3×N（8 → 20 分）；週末長騎 3–4 h 後段 Tempo | 1 | 第 4 週 Ramp（基線）、第 8 週 20 分鐘 → ≈ 260–265 |
| 9–16 | 建構 | 甜蜜點 → 閾值進程 3×12 → 2×20 → 3×20 @ 95–100 %；第 13 週起 Over-Under；長騎後段甜蜜點 | 2 | 第 12 週 Ramp、第 16 週 20 分鐘 → ≈ 275–285 |
| 17–24 | 閾值／VO2 | VO2 4×5 → 5×5 @ 108–115 % 與閾值 2×20 @ 100–102 % 隔週交替；爬坡重複 @ 目標功率；長騎維持 | 2 | 第 20 週 Ramp、第 24 週 20 分鐘 → ≈ 290–300 |
| 25–26 | 測驗週 | 5 天迷你減量（量 −40 %）、開腿、20 分鐘測驗；重新設定下一季基線 | 1 短 | FTP ≥ 300 或誠實重設目標 |

規則：
- **FTP 只在測驗與 eFTP 檢查點更新**；表中的預期值是給報告用的，不是課表的輸入。
- 檢查點差 **> 3 %** → 下個區塊重複前兩週，不進階。
- 量的上限 15 h；預期基礎期 12–14 h、建構期 11–13 h（強度上去、時數下來）。
- 戶外優先：每個模板都有戶外版與心率備援；只有雨天（降雨機率 ≥ 60 %）、溫度超出 8–36 °C、或測驗才進室內（Rouvy）。

**用範例數據舉例**：今天 FTP 250 W、70 kg（3.6 W/kg）。
- 目標 300 W = 4.29 W/kg，+20 %。檢查點：第 8 週 262（+4.8 %）、第 16 週 280（+12 %）、第 24 週 295（+18 %）。
- 若第 8 週 20 分鐘測驗平均 270 W → FTP 估 0.95 × 270 = 256.5，比預期 262 低 2.1 %（< 3 %）→ 建構期照常進階。若只有 262 W → 249，差 5 % → 建構期第 9–10 週重做甜蜜點 3×15/3×20 再進閾值。
- 那趟 2 小時 NP 210 W 的騎乘以今天的 FTP 是 IF 0.84、141 TSS；若第 16 週 FTP 真的到 280，同樣一趟只剩 IF 0.75、113 TSS——這就是為什麼 FTP 更新後整個 PMC 會重算，也是為什麼不能亂改 FTP。

## 限制
- +20 % 在 26 週對已有訓練基礎的選手是文獻中少見的幅度（典型有訓練者一季 +5–10 %）。系統的任務是「盡快發現做不到」然後誠實重設，不是硬撐到第 26 週。
- 季節骨架假設可用時間穩定（週 15 h）；出差、生病會由每日重排吸收，但連續缺 2 週以上應該手動重設 `season.start`。
- 沒有比賽意味著沒有「真實情境」的回饋；爬坡重複 @ 目標功率是刻意補這一塊。
- 體重被視為常數；W/kg 目標若有減重成分，需另外處理，v1 不排營養。

## 參考文獻
- `docs/05-training-engine.md` §2.4（本季節類型的規格）、`config/athlete.yaml`。
- Allen H, Coggan A, McGregor S. *Training and Racing with a Power Meter*, 3rd ed., 2019, 第 3 章（FTP 測驗）與第 10 章（用功率規劃一季）。
- Seiler S, Jøranson K, Olesen BV, Hetlelid KJ. "Adaptations to aerobic interval training: interactive effects of exercise intensity and total work duration." *Scandinavian Journal of Medicine & Science in Sports* 23(1):74–83, 2013.（4×4／4×8／4×16 的比較，支持 4–8 分鐘間歇）
- Rønnestad BR, Hansen J, Nygaard H, Lundby C. "Superior performance improvements in elite cyclists following short-interval vs effort-matched long-interval training." *Scand J Med Sci Sports* 30(5):849–857, 2020.
- Laursen PB, Jenkins DG. "The scientific basis for high-intensity interval training." *Sports Medicine* 32(1):53–73, 2002.
- Zwift Support: "How does the Ramp Test work?"（0.75 × 最佳 1 分鐘的慣例）。
