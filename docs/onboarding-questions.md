# Onboarding questions (new athlete profile)

Asked once when a profile is created (`cyp profile add`), in zh-TW, about 3 minutes. Rule:
**never ask what intervals.icu already knows**, and only ask what changes the plan or its safety.

## Read automatically from intervals.icu (not asked)
Weight, FTP / eFTP, time zone, resting and max HR seen, HR/power zones, ride history (how often,
how long, how hard, which weekdays), wellness (HRV, sleep), whether a power meter / HR strap is
used, whether Garmin is linked and allowed to receive planned workouts
(`icu_garmin_upload_workouts`).

## The questions

| # | 問題（給選手） | 選項 / 格式 | Drives |
|---|---|---|---|
| 1 | 你是哪一年出生的？ | 西元年 | recovery rhythm (3:1 vs 2:1), intensity cap, HR max sanity check |
| 2 | 以下有任何一項嗎？ (a) 醫生說過你有心臟問題或高血壓 (b) 運動時曾胸痛、頭暈或昏倒 (c) 正在吃會影響心跳的藥（例如β阻斷劑） (d) 最近有受傷、開刀，或關節常痛 | 每項 是／否 | any "是" → medical clearance advised, HR-based targets, no maximal tests, max 1 hard day/week |
| 3 | 騎車最主要想要什麼？ | 健康和開心／爬坡更輕鬆／變更強（FTP）／完成一個活動（日期、名稱）／減重 | season type and goal |
| 4 | 一週哪幾天可以騎？每次大約多久？ | 例：二 60、四 60、六 180、日 120 | availability (minutes per weekday) |
| 5 | 哪一天一定要休息？ | 星期幾（可多選或「沒有」） | rest days the planner never uses |
| 6 | 你喜歡怎麼騎？ | 輕鬆聊天騎／偶爾喘一下也好／喜歡被操 | hard sessions per week, intensity distribution |
| 7 | 有室內訓練台嗎？用什麼平台？ | 沒有／有（Zwift、Rouvy…） | indoor policy, rainy-day fallback |
| 8 | 願意偶爾做體能測驗嗎（20 分鐘全力騎）？ | 願意／不要 | season tests on/off |
| 9 | 常騎、喜歡的爬坡有哪些？大概騎多久？ | 選填，名稱＋分鐘 | climb suggestions in outdoor workouts |
| 10 | 除了騎車還有做什麼運動？一週幾次？ | 重訓／跑步／瑜伽／其他 | load accounting, no hard ride after strength days |

The owner answers only one thing: whether the profile may write to the calendar right away or
starts in `propose` (default) for a week of review.

## Mapping status

| Answer | Config today | Gap (needs a spec) |
|---|---|---|
| 4, 5 | `availability` (0 minutes = rest day) | – |
| 6 | `planner.intensity` (easy / moderate / hard) + `planner.hit_per_week`; hard days: `planner.hit_days` | – |
| 7 | `location.indoor_policy`, `indoor_platform` | – |
| 8 | `season.tests`, `season.final_test` | – |
| 9 | `location.climbs` | – |
| 10 | `other_sports` | – |
| 1 | `athlete.birth_year` → age defaults (docs/05 §2.5) | – |
| 2 | `athlete.health_flags` → hard caps (docs/05 §2.5) | HR-medication rendering, report note |
| 3 = distance ("150 km 不累") | goal `kind: distance` + `availability.long_ride_max_minutes` | – |
| 3 = 健康／爬坡 | – (only `ftp_target` is implemented) | a `maintain` season type: no phases chase FTP, no tests, CTL held, 1 optional quality ride |
