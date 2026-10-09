import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, type S } from "../api/client";
import { Bike, Icon } from "../bike";
import { todayIso } from "../api/hooks";
import { ErrorNote, Explain, Panel, WorkoutProfile, fmt, mmdd, scrollToTop, weekdayZh } from "../ui";

export function Plan({ profile }: { profile: string }) {
  const plan = useQuery({ queryKey: ["plan", profile, 14], queryFn: () => api.plan(profile, 14) });
  const season = useQuery({ queryKey: ["season", profile], queryFn: () => api.season(profile) });
  const [open, setOpen] = useState<string | null>(null);
  const today = todayIso();

  return (
    <div className="grid plan">
      <Panel kicker="未來 14 天" title="課表">
        <RouteMap
          days={plan.data ?? []}
          today={today}
          open={open}
          onPick={(date) => {
            setOpen(date);
            scrollToTop(`day-${date}`);
          }}
        />
        <ol className="days">
          {(plan.data ?? []).map((d) => {
            const rest = !d.template_id;
            const isOpen = open === d.date;
            return (
              <li key={d.date} id={`day-${d.date}`} className={`day ${rest ? "rest" : ""} ${d.date === today ? "is-today" : ""}`}>
                <button
                  className="day-row"
                  onClick={() => {
                    setOpen(isOpen ? null : d.date);
                    // The day that was open above may just have folded up: bring this one to the top.
                    if (!isOpen) scrollToTop(`day-${d.date}`);
                  }}
                  aria-expanded={isOpen}
                >
                  <span className="day-date">
                    {mmdd(d.date)}
                    <small>週{weekdayZh(d.date)}</small>
                  </span>
                  <span className="day-name">
                    <Icon name={rest ? "rest" : d.role} className={`role ${rest ? "rest" : d.role}`} />
                    {rest ? "休息 · 咖啡站" : d.name_zh}
                  </span>
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

/**
 * The next two weeks drawn as a stage profile: each day is a point whose height is its TSS, rest
 * days are the valleys (café stops), and the rider sits on today. Click a point to open that day.
 */
function RouteMap({
  days,
  today,
  open,
  onPick,
}: {
  days: S["PlannedDayOut"][];
  today: string;
  open: string | null;
  onPick: (date: string) => void;
}) {
  if (days.length < 2) return null;
  const max = Math.max(...days.map((d) => d.tss ?? 0), 1);
  const pts = days.map((d, i) => ({
    d,
    x: 3 + (i / (days.length - 1)) * 94,
    y: 88 - ((d.template_id ? (d.tss ?? 0) : 0) / max) * 66,
  }));
  // Smooth line through the points (Catmull-Rom as cubic Béziers) in a 1000 x 100 box: close to
  // the drawn aspect ratio, so the stroke is not distorted much without non-scaling-stroke.
  const X = (v: number) => (v * 10).toFixed(1);
  let path = `M${X(pts[0]!.x)},${pts[0]!.y}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[Math.max(i - 1, 0)]!;
    const p1 = pts[i]!;
    const p2 = pts[i + 1]!;
    const p3 = pts[Math.min(i + 2, pts.length - 1)]!;
    path += `C${X(p1.x + (p2.x - p0.x) / 6)},${p1.y + (p2.y - p0.y) / 6} ${X(p2.x - (p3.x - p1.x) / 6)},${p2.y - (p3.y - p1.y) / 6} ${X(p2.x)},${p2.y}`;
  }
  return (
    <div className="routemap-scroll">
      <div className="routemap">
        <svg viewBox="0 0 1000 100" preserveAspectRatio="none" aria-hidden>
          <defs>
            <linearGradient id="route-fill" x1="0" x2="0" y1="0" y2="1">
              <stop offset="0" className="stop-a" />
              <stop offset="1" className="stop-b" />
            </linearGradient>
          </defs>
          <path d={`${path}L970,100L30,100Z`} className="route-area" />
          <path d={path} className="route-line" pathLength={1} />
        </svg>
        {pts.map(({ d, x, y }) => {
          const rest = !d.template_id;
          return (
            <button
              key={d.date}
              className={`stop ${rest ? "rest" : d.role} ${d.date === today ? "is-today" : ""} ${open === d.date ? "on" : ""}`}
              style={{ left: `${x}%`, top: `${y}%` }}
              onClick={() => onPick(d.date)}
              title={rest ? `${mmdd(d.date)} 休息` : `${mmdd(d.date)} ${d.name_zh} · ${fmt(d.tss)} TSS`}
            >
              {d.date === today ? (
                <span className="here">
                  <Bike />
                </span>
              ) : null}
              <Icon name={rest ? "rest" : d.role} />
              <small>
                {mmdd(d.date)}
                <i>週{weekdayZh(d.date)}</i>
              </small>
            </button>
          );
        })}
      </div>
    </div>
  );
}
