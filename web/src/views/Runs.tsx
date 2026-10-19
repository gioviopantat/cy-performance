import { useQuery } from "@tanstack/react-query";

import { api, type S } from "../api/client";
import { ErrorNote, Panel } from "../ui";

const STATUS_ZH: Record<string, string> = { ok: "完成", skipped: "略過", failed: "失敗" };

export function Runs({ profile }: { profile: string }) {
  const runs = useQuery({ queryKey: ["runs", profile], queryFn: () => api.runs(profile), refetchInterval: 30_000 });
  return (
    <div className="grid runs">
      <Panel kicker="每天 05:30 自動執行 + 手動" title="執行紀錄">
        {(runs.data ?? []).length === 0 ? <p className="muted">還沒有執行紀錄。</p> : null}
        <ol className="run-list">
          {(runs.data ?? []).map((r, i) => (
            <li key={`${r.at}-${i}`} className={r.ok ? "ok" : "failed"}>
              <div className="run-head">
                <strong>{r.at?.replace("T", " ").slice(0, 16) ?? r.day}</strong>
                <span className={`badge ${r.planner_mode}`}>{r.planner_mode === "apply" ? "自動寫入" : "只算不寫"}</span>
                <span className={`badge ${r.ok ? "good" : "bad"}`}>{r.ok ? "成功" : "失敗"}</span>
              </div>
              <Stages stages={r.stages} />
            </li>
          ))}
        </ol>
        <ErrorNote error={runs.error} />
      </Panel>
    </div>
  );
}

export function Stages({ stages }: { stages?: S["AutopilotStage"][] | undefined }) {
  return (
    <ul className="stages">
      {(stages ?? []).map((s) => (
        <li key={s.name} className={s.status}>
          <span className="stage-name">{s.name}</span>
          <span className="stage-status">{STATUS_ZH[s.status] ?? s.status}</span>
          <span className="stage-detail">{s.detail}</span>
        </li>
      ))}
    </ul>
  );
}

/** Live status of a job started from the UI (sync / autopilot). */
export function JobStatus({ job }: { job: S["JobOut"] | null }) {
  if (!job) return null;
  const result = job.result as { stages?: S["AutopilotStage"][] } | null | undefined;
  return (
    <div className={`job ${job.status}`}>
      <p>
        <span className="pulse" /> {job.kind} · {job.status === "queued" ? "排隊中" : job.status === "running" ? "執行中…" : job.status === "ok" ? "完成" : "失敗"}
      </p>
      {result?.stages ? <Stages stages={result.stages} /> : null}
      {job.error ? <p className="error">{job.error}</p> : null}
    </div>
  );
}
