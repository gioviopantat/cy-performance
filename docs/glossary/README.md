# Glossary (zh-TW)

每個模型一個檔案，格式見 [../07-explainability.md](../07-explainability.md) §2：
**這是什麼 · 為什麼用它 · 怎麼算（公式 + 用這位選手的數據舉例）· 限制 · 參考文獻**。
範例一律用同一組數字：FTP 250 W、64 kg、一趟 2 小時 NP 210 W（IF 0.84、141 TSS）的騎乘，
讓不同條目可以互相對照。`Explanation.glossary_terms` 以下表的 id 引用這些檔案。

| id | 中文名 | 一句話說明 |
|----|--------|-----------|
| [coggan_np_if_tss](coggan_np_if_tss.md) | 標準化功率／強度因子／訓練壓力分數 | 把一趟騎乘壓成「相當於多少瓦、多接近閾值、多少劑量」三個數字。 |
| [banister_pmc](banister_pmc.md) | Banister 體能–疲勞模型（CTL/ATL/TSB） | 每天的 TSS 經兩個指數濾波器變成體能、疲勞與狀態三條曲線。 |
| [ramp_rate_acwr_monotony](ramp_rate_acwr_monotony.md) | CTL 漲幅／急慢性負荷比／單調性 | 三個「你加太快了嗎」的警示燈，守衛的主要輸入。 |
| [cp_wprime](cp_wprime.md) | 臨界功率與 W′ | 兩個數字描述整條功率–時間曲線；W′bal 預測間歇第幾趟會爆。 |
| [eftp](eftp.md) | 估計 FTP（intervals.icu eFTP） | 從最近 42 天的最佳努力反推 FTP，測驗之間的連續證據。 |
| [decoupling_hr_lag](decoupling_hr_lag.md) | 有氧解耦與心率延遲 | 後半段心率是否變「貴」、心率多久才跟上功率——耐久力與恢復的訊號。 |
| [efficiency_factor](efficiency_factor.md) | 效率因子（EF） | NP ÷ 平均心率；同強度下整季追蹤有氧基礎是否進步。 |
| [time_in_zone_tid](time_in_zone_tid.md) | 區間時間與訓練強度分布 | 一週低／中／高強度各佔多少，與階段目標（金字塔／極化）比較。 |
| [readiness_v1](readiness_v1.md) | 每日準備度判斷 | HRV、安靜心率、睡眠、TSB、昨天的騎乘 → 今天 REST／EASY／照課表／可升級。 |
| [periodization_3_1](periodization_3_1.md) | 週期化與 3:1 節奏 | 三週加壓、一週減量 40 %，測驗放在恢復週。 |
| [ftp_target_season](ftp_target_season.md) | FTP 目標季節類型 | 本季骨架：基礎 → 建構 → 閾值/VO2 → 測驗週，26 週 250 → 300 W，每 4 週測一次。 |
| [guardrails](guardrails.md) | 守衛（硬規則） | 任何提案之後都要過的九條不可協商規則；違規就移除最低價值的訓練。 |
| [workout_library](workout_library.md) | 課表模板庫 | 約 30 個參數化 YAML 模板，含戶外版、心率備援與 explain_zh。 |

狀態：M2 填入公式與範例（本版）；M5 隨報告模板完成最終校稿。
