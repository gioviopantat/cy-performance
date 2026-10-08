import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";
import { todayIso } from "../api/hooks";
import { ErrorNote, Explain, Panel, WorkoutProfile, fmt, mmdd, weekdayZh } from "../ui";

export function Plan({ profile }: { profile: string }) {
  const plan = useQuery({ queryKey: ["plan", profile, 14], queryFn: () => api.plan(profile, 14) });
  const season = useQuery({ queryKey: ["season", profile], queryFn: () => api.season(profile) });
  const [open, setOpen] = useState<string | null>(null);
  const today = todayIso();

  return (
    <div className="grid plan">
      <Panel kicker="未來 14 天" title="課表">
        <ol className="days">
          {(plan.data ?? []).map((d) => {
            const rest = !d.template_id;
            const isOpen = open === d.date;
            return (
              <li key={d.date} className={`day ${rest ? "rest" : ""} ${d.date === today ? "is-today" : ""}`}>
                <button className="day-row" onClick={() => setOpen(isOpen ? null : d.date)} aria-expanded={isOpen}>
                  <span className="day-date">
                    {mmdd(d.date)}
                    <small>週{weekdayZh(d.date)}</small>
                  </span>
                  <span className="day-name">{rest ? "休息" : d.name_zh}</span>
                  <span className="day-meta">
                    {rest ? "" : `${fmt(d.minutes)}′ · ${fmt(d.tss)} TSS`}
                    {!rest ? <em className={d.outdoor ? "chip out" : "chip in"}>{d.outdoor ? "室外" : "室內"}</em> : null}
                  </span>
                </button>
                {isOpen && !rest ? (
                  <div className="day-detail">
                    <WorkoutProfile steps={d.steps} />
                    {d.note_zh ? <p className="note">{d.note_zh}</p> : null}
                    <Explain e={d.explanation} />
                    {d.workout_text ? <pre className="code">{d.workout_text}</pre> : null}
                  </div>
                ) : null}
              </li>
            );
          })}
        </ol>
        <ErrorNote error={plan.error} />
      </Panel>

      <Panel
        kicker={season.data ? `${season.data.start} → ${season.data.goal_date}` : "季節"}
        title={season.data?.goal_name ?? "整季"}
      >
        <SeasonBars />
        <p className="legend season-legend">
          <i className="sw base" /> 基礎期 <i className="sw build" /> 建構期 <i className="sw threshold" /> 閾值期{" "}
          <i className="sw test" /> 測驗週 · 淡色＝恢復週 · 測＝測驗 · 長＝超長騎
        </p>
        <ErrorNote error={season.error} />
      </Panel>
    </div>
  );

  function SeasonBars() {
    const weeks = season.data?.weeks ?? [];
    if (!weeks.length) return null;
    const max = Math.max(...weeks.map((w) => w.target_tss));
    const now = today;
    return (
      <div className="season">
        {weeks.map((w) => {
          const end = new Date(new Date(`${w.start}T12:00:00`).getTime() + 6 * 86_400_000).toISOString().slice(0, 10);
          const current = w.start <= now && now <= end;
          return (
            <div
              key={w.index}
              className={`wk ${w.phase} ${w.recovery ? "recovery" : ""} ${current ? "current" : ""}`}
              title={`第 ${w.index} 週 ${w.phase_zh}${w.recovery ? "（恢復）" : ""} · ${Math.round(w.target_tss)} TSS · ${w.target_hours.toFixed(1)} h`}
            >
              <span className="bar" style={{ height: `${(w.target_tss / max) * 100}%` }} />
              {w.test ? <span className="flag">測</span> : null}
              {w.over_distance ? <span className="flag long">長</span> : null}
              <span className="wk-n">{w.index}</span>
            </div>
          );
        })}
      </div>
    );
  }
}
