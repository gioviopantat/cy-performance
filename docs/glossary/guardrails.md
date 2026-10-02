# guardrails · 守衛（硬規則）

## 這是什麼
一組**不可協商**的規則，在任何課表提案之後執行——不管提案來自排程器、LLM 審閱者還是人工編輯（docs/05 §5，ADR-0004）。違規時系統不會「警告」，而是直接移除價值最低的訓練直到乾淨為止；移不乾淨就把那天留空並標記 `needs_review`。每一次修補都會產生一個 Explanation，告訴你是哪條規則、哪個數字。

## 為什麼用它
這個系統會自動把課表寫進日曆、推到車錶上；錯誤的代價是真實的疲勞與受傷。文獻裡最一致的受傷與過度訓練預測因子不是「練太多」而是「加太快」「高強度太密」「沒有休息日」。把這些寫成硬規則，排程器與 LLM 就可以放心地在界線內做判斷，而界線本身是可讀、可測試、有版本的。

## 怎麼算
| 規則 id | 預設（本季 config） | 看什麼數字 |
|---------|---------------------|-----------|
| `ramp_rate` | CTL 7 天漲幅 ≤ 6（基礎/建構）、≤ 4（閾值期） | PMC 模擬 |
| `tsb_floor` | TSB ≥ −30（基礎/建構）、≥ −25（閾值期）、≥ 0（測驗週） | PMC 模擬 |
| `hit_per_week` | 高強度 ≤ 1（基礎）、≤ 2（建構/閾值） | 課表 intent ∈ {sweetspot, threshold, vo2, anaerobic} |
| `hit_spacing` | 高強度之間 ≥ 48 h；長騎 > 150 TSS 的隔天不排高強度 | 日期差 |
| `weekly_tss_vs_mean` | 週 TSS ≤ 最近 4 週平均 × 1.15（恢復週向下不受限） | 週加總 |
| `single_ride` | ≤ 該星期幾的可用分鐘；≤ 最近 6 週最長騎乘 × 1.6 | `availability` + 歷史 |
| `rest_days` | 每週 ≥ 1 天；若一週內 readiness < 40 兩次則 ≥ 2 天 | 日曆 |
| `readiness` | SICK/INJURED → 休息到解除；REST 覆蓋一切 | `readiness_daily` |
| `publishing` | 只動 `external_id` 以 `cyp:` 開頭的事件；絕不刪改你自己建的事件；每次 ≤ 20 個事件；icu 回讀的負荷必須在目標 ±10 % | icu API |

移除順序：`value = 目標相關性 × 進程需要度`，最低的先拿掉（通常是零碎的 Z2，最後才是長騎與高強度）。

**用你的數據舉例**：建構期某週，最近 4 週平均 520 TSS。排程器初版排了 630 TSS（含週日 2 小時 NP 210 W 的 141 TSS、兩次閾值課、週六 4 小時長騎 180 TSS）。
- `weekly_tss_vs_mean`：630 / 520 = +21 % > 15 % → 超標 32 TSS。先移除週五的 Z2 60 分（43 TSS）→ 587（+13 %）✓。
- `hit_spacing`：週六長騎 180 TSS > 150，週日原本想排的閾值 2×20 被移到週二；週日改成那趟 Z2 長騎。
- `tsb_floor`：模擬到週日 TSB −27，> −30 ✓。
- Explanation：「本週目標從 630 降到 587，因為超過最近 4 週平均 15 %（規則 weekly_tss_vs_mean）；移除週五 Z2 60 分（價值 0.3）。閾值課從週日移到週二，因為週六長騎 180 TSS（規則 hit_spacing）。」

## 限制
- 守衛是**下限保護**，不是最佳化；它能防止明顯錯誤，不能保證課表是好的。課表品質來自模板庫與進程邏輯。
- 所有依賴 TSS 的規則都繼承 TSS 的誤差（FTP 錯、hrTSS 粗）。
- 規則之間可能互相拉扯（砍 Z2 去滿足週量，反而讓 monotony 上升）；v1 以表中順序處理，不做全域最佳化。
- 「可用分鐘」是你填的；它不知道你那天其實很累。這一塊交給 readiness。
- 守衛不會阻止你自己在 icu 上加課；它只管 `cyp:` 事件，但你的事件會進 PMC 影響接下來的計畫。

## 參考文獻
- `docs/05-training-engine.md` §5、`docs/adr/0004-deterministic-planner.md`、`config/athlete.yaml` 的 `planner` 區段。
- Gabbett TJ. "The training—injury prevention paradox." *British Journal of Sports Medicine* 50(5):273–280, 2016.（加太快的風險）
- Foster C. "Monitoring training in athletes with reference to overtraining syndrome." *MSSE* 30(7):1164–1168, 1998.（單調性、休息日）
- Seiler S, Haugen O, Kuffel E. "Autonomic recovery after exercise in trained athletes: intensity and duration effects." *MSSE* 39(8):1366–1373, 2007.（高強度後自主神經恢復需時，支持 48 h 間隔）
- Meeusen R, et al. "Prevention, diagnosis, and treatment of the overtraining syndrome." *MSSE* 45(1):186–205, 2013.
- intervals.icu API：`POST /athlete/{id}/events/bulk?upsert=true`、`external_id`、`icu_training_load` 回讀（docs/02 §3.3–3.4）。
