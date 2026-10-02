# workout_library · 課表模板庫

## 這是什麼
約 30 個 YAML 檔（`src/cyp/planning/library/*.yaml`），每個是一種**可參數化**的訓練課：恢復騎、Z2 有氧（60–240 分，有/無高迴轉練習）、長騎後段 Tempo／甜蜜點、Tempo 2×20、甜蜜點 3×N／2×20／3×20、Over-Under、閾值 3×12／2×15／2×20／3×20、VO2 5×3／4×5／5×5／30-30／40-20、無氧 8×1、爬坡重複 @ 目標功率、Ramp 測驗、20 分鐘測驗、開腿。排程器選一個模板、在允許範圍內填參數、渲染成 intervals.icu 的文字格式，發布到日曆後由 icu 推到車錶。

## 為什麼用它
ADR-0004 的結論是「模板庫的品質是課表品質的主要槓桿」。把課表寫成資料而不是程式碼有三個好處：（1）每一課都帶著 `explain_zh`——它練什麼、為什麼在這個階段——報告可以直接引用；（2）進程是明確的參數範圍（甜蜜點 work_min 8 → 20），不是藏在程式裡的魔術數字；（3）戶外版、心率備援版和室內版來自同一份定義，不會互相漂移。

## 怎麼算
**結構**（完整規格見 `src/cyp/planning/library/README.md`）：
```
id, version, name, name_zh, purpose_zh, intent, phases, slot_roles,
requires_power, indoor_ok, outdoor_ok,
params:  {reps: {min, max, default}, work_min: {...}, pct: {...}, rest_min: 4}
progression: [work_min, reps, pct]        # 排程器先拉長、再加趟數、最後加 %
steps:   [{cue, kind: steady|ramp|work|rest|freeride, duration, lo, hi, cadence}, {repeat, steps: [...]}]
outdoor_rendering: {widen_pct, rests_as_freeride, ramps_as_range, note_zh}
hr_fallback: {steps 以 % LTHR 表示} 或 null
tss_model: closed_form
explain_zh
```

**TSS 預估（閉式）**：`TSS = Σ 每段時數 × (區間中點 / 100)² × 100`。排程器用它把參數縮放到讓預估 TSS 落在當天目標 ±8 % 內；發布後再用 icu 回算的 `icu_training_load` 驗證（±10 %）。

**渲染規則**：`m` 是分鐘（`mtr` 才是公尺）；重複區塊前後空一行、不可嵌套；一份課表只能有功率或只能有心率目標。

**用你的數據舉例**：基礎期第 3 週，週三 150 分時段的高強度日目標 65 TSS。排程器選 `ss_3x_n`，進程指數給 work_min = 12：

```
Warmup
- 10m ramp 50-70%

Main Set 3x
- 12m 88-92% 85-95rpm
- 4m 50-55%

Cooldown
- 8m 50%
```
閉式 TSS：熱身 10 分中點 60 % → 6.0；工作 3 × (12/60 × 0.90² × 100 = 16.2) = 48.6；休息 3 × 1.8 = 5.5；收操 3.3 → **63 TSS，64 分，IF 0.77**（目標 65 的 −3 %，通過）。以 FTP 250 W 來說，工作段是 220–230 W。
戶外版同一課：工作段變 `85-95%`（±3 %），休息變 `4m freeride`，熱身變 `10m 50-70%`。
心率備援版（Edge 530）：工作段 `12m 90-97% LTHR`，整份課表沒有任何 `%` 功率目標。

和那趟 2 小時 NP 210 W（141 TSS）相比，這一課只有它的 45 % 負荷，但 36 分鐘在 88–92 % 的刺激是 Z2 長騎給不了的。

## 限制
- 閉式 TSS 用「區間中點」估，實際執行偏上緣或下緣會差 ±5 %；戶外的自由騎段用假設值（下坡 40 %、休息 50 %）更粗。
- 心率備援把 % LTHR 當作 % FTP 算 TSS 是明顯的近似，報告會標 `confidence: low`；icu 那邊會算 hrTSS。
- 模板的 % 區間是通用值（Coggan 區間）；個人的甜蜜點可能是 85–90 而不是 88–92。進程與測驗會逐步校正，但模板本身不會自動學習。
- icu 文字格式的細節（例如 `85-90% LTHR` 範圍、`upsert` 的行為）在 M3 的 API 驗證前都是「照文件寫」，尚未實測。
- 沒有「比賽日」模板，因為本季沒有比賽；需要時再加。

## 參考文獻
- `src/cyp/planning/library/README.md`（schema 與渲染規則）、`docs/05-training-engine.md` §6、`docs/02-integrations.md` §3.5。
- intervals.icu 論壇："Workout Builder Syntax Quick Guide"（t/123701）；zonepace.cc/intervals-workout-format。
- Allen H, Coggan A, McGregor S. *Training and Racing with a Power Meter*, 3rd ed., 2019, 第 4 章（功率區間）與附錄（課表範例）。
- Seiler S, et al. 2013；Rønnestad BR, et al. 2020；Billat LV. "Interval training for performance: a scientific and empirical practice." *Sports Medicine* 31(1):13–31 & 31(2):75–90, 2001.（間歇課設計的依據，見 `ftp_target_season`）
- Zwift Support: "How does the Ramp Test work?"；Allen & Coggan 20 分鐘測驗協定（同上書第 3 章）。
