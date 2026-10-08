import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, type S } from "../api/client";
import { daysAgoIso, todayIso, useJob } from "../api/hooks";
import { ErrorNote, Explain, FitnessChart, Panel, Tile, WorkoutProfile, fmt, mmdd, weekdayZh } from "../ui";
import { JobStatus } from "./Runs";

const PHASE_ZH: Record<string, string> = { base: "基礎期", build: "建構期", threshold: "閾值期", test: "測驗週" };

/** Readiness inputs that can be missing: label + how to provide it. */
const MISSING_ZH: Record<string, [string, string]> = {
  hrv: ["HRV（睡眠心率變異）", "睡覺戴有 HRV 的手錶，並讓 Garmin 健康資料同步到 intervals.icu"],
  rhr: ["安靜心率", "睡覺戴手錶，或在 intervals.icu 的 wellness 填"],
  sleep: ["睡眠", "睡覺戴手錶，或在 intervals.icu 的 wellness 填"],
  ride: ["昨天的騎乘反應", "昨天沒有可分析的騎乘；有騎就會自動有"],
  subjective: ["主觀感受（疲勞、痠痛、壓力、心情）", "每天早上在 intervals.icu 的 wellness 填，約 30 秒"],
};

export function Today({ profile, info }: { profile: string; info: S["ProfileOut"] | undefined }) {
  const today = todayIso();
  const meta = useQuery({ queryKey: ["meta", profile], queryFn: () => api.meta(profile) });
  const ready = useQuery({ queryKey: ["readiness", profile, today], queryFn: () => api.readiness(profile, today) });
  const plan = useQuery({ queryKey: ["plan", profile, 4], queryFn: () => api.plan(profile, 4) });
  const fit = useQuery({ queryKey: ["fitness", profile], queryFn: () => api.fitness(profile, daysAgoIso(120)) });
  const latest = useQuery({ queryKey: ["activities", profile, "latest"], queryFn: () => api.activities(profile, 0, 1) });

  const days = plan.data ?? [];
  const doneToday = latest.data?.items[0]?.date === today ? latest.data.items[0] : null;
  // Once today's ride is in, the card moves on to the next session.
  const next = days.find((d) => (doneToday ? d.date > today : d.date >= today) && d.template_id) ?? null;
  const last = fit.data?.points.filter((p) => p.ctl !== null).at(-1);
  const a = meta.data?.athlete;
  const s = meta.data?.season;

  return (
    <div className="grid today">
      <Panel
        className="hero"
        kicker={next ? `${mmdd(next.date)}（${weekdayZh(next.date)}）${next.date === today ? "今天" : "下一堂"}` : "課表"}
        title={next?.name_zh ?? "今天休息"}
      >
        {doneToday ? (
          <p className="done">
            ✓ 今天已騎：{doneToday.name} · {Math.round((doneToday.moving_s ?? 0) / 60)} 分 ·{" "}
            {fmt(doneToday.tss)} TSS
          </p>
        ) : null}
        {next ? (
          <>
            <div className="tiles">
              <Tile label="時間" value={fmt(next.minutes)} unit="分" />
              <Tile label="TSS" value={fmt(next.tss)} tone="hot" />
              <Tile label="場地" value={next.outdoor ? "室外" : "室內"} />
              <Tile label="角色" value={next.role_zh} />
            </div>
            <WorkoutProfile steps={next.steps} />
            {next.note_zh ? <p className="note">{next.note_zh}</p> : null}
            <Explain e={next.explanation} />
          </>
        ) : (
          <p className="muted">接下來幾天沒有排課。</p>
        )}
        <ErrorNote error={plan.error} />
      </Panel>

      <Panel className="ready" kicker={`準備度 · ${today}`} title={ready.data?.recommendation_zh ?? "—"}>
        <div className="gauge" style={{ ["--score" as string]: ready.data?.score ?? 0 }}>
          <span>{fmt(ready.data?.score)}</span>
          <small>{ready.data?.status ?? ""}</small>
        </div>
        {ready.data?.missing?.length ? (
          <div className="missing">
            <p className="muted">以下資料今天沒有，分數改用其他項目計算：</p>
            <ul>
              {ready.data.missing.map((m) => (
                <li key={m}>
                  <strong>{MISSING_ZH[m]?.[0] ?? m}</strong>
                  <span className="muted">{MISSING_ZH[m]?.[1] ?? ""}</span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        <Explain e={ready.data?.explanation} />
        <ErrorNote error={ready.error} />
      </Panel>

      <Panel className="numbers" kicker={a?.name ?? ""} title="狀態">
        <div className="tiles six">
          <Tile label="CTL 體能" value={fmt(last?.ctl, 1)} tone="cool" />
          <Tile label="ATL 疲勞" value={fmt(last?.atl, 1)} tone="hot" />
          <Tile label="TSB 狀態" value={fmt(last?.tsb, 1)} tone={(last?.tsb ?? 0) < -20 ? "warn" : undefined} />
          <Tile label="FTP" value={fmt(a?.ftp)} unit="W" />
          <Tile label="W/kg" value={fmt(a?.w_kg, 2)} />
          <Tile
            label={`週次 · ${PHASE_ZH[s?.phase ?? ""] ?? s?.phase ?? ""}`}
            value={s?.week_index ? `${s.week_index}/${s.weeks_total}` : "—"}
          />
        </div>
        {fit.data ? <FitnessChart points={fit.data.points} /> : null}
        <p className="legend">
          <i className="sw ctl" /> CTL 體能 <i className="sw atl" /> ATL 疲勞 <i className="sw tsb" /> TSB
        </p>
      </Panel>

      <Actions profile={profile} info={info} />
    </div>
  );
}

function Actions({ profile, info }: { profile: string; info: S["ProfileOut"] | undefined }) {
  const { job, start, busy, error: jobError } = useJob(profile);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const go = async (fn: () => Promise<S["JobOut"]>) => {
    setError(null);
    try {
      start(await fn());
    } catch (e) {
      setError(e);
    }
  };
  return (
    <Panel className="actions" kicker="手動執行" title="一鍵">
      <div className="buttons">
        <button disabled={busy} onClick={() => void go(() => api.sync(profile))}>
          同步最新騎乘
        </button>
        <button disabled={busy} onClick={() => void go(() => api.runAutopilot(profile, false))}>
          試跑今日流程<small>不寫入行事曆</small>
        </button>
        {info?.calendar_write_allowed ? (
          <button className="primary" disabled={busy} onClick={() => setConfirming(true)}>
            執行並寫入行事曆
          </button>
        ) : (
          <p className="muted small">
            {info?.planner_mode === "apply" ? "寫入功能已關閉（api.calendar_write）" : "只算不寫模式：用 cyp profile promote 開啟"}
          </p>
        )}
      </div>
      {confirming ? (
        <div className="confirm" role="dialog" aria-modal="true">
          <p>
            這會把課表<strong>寫進 {info?.display_name} 的 intervals.icu 行事曆</strong>
            {info?.garmin_upload_workouts ? "，並推到 Garmin" : ""}。
          </p>
          <div className="buttons">
            <button
              className="primary"
              onClick={() => {
                setConfirming(false);
                void go(() => api.runAutopilot(profile, true));
              }}
            >
              確定寫入 {info?.display_name}
            </button>
            <button onClick={() => setConfirming(false)}>取消</button>
          </div>
        </div>
      ) : null}
      <JobStatus job={job} />
      <ErrorNote error={error ?? jobError} />
    </Panel>
  );
}
