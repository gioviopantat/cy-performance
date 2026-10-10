import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState, type TouchEvent } from "react";
import { createPortal } from "react-dom";

import { api, type S } from "../api/client";
import { todayIso } from "../api/hooks";
import { Icon } from "../bike";
import { play } from "../sfx";
import { ErrorNote, Explain, Panel, Tile, WorkoutProfile, fmt, mmdd, usePhone, weekdayZh } from "../ui";
import { RideDetail } from "./RideDetail";

// 行事曆: planned and done in one grid (docs/specs/calendar-view.md). A cell shows only what
// answers "what was planned, what happened, how did it go"; everything else opens in a modal.

type Day = S["CalendarDay"];
type Status = Day["status"];

const WEEKS = 6;
const DAY_MS = 86_400_000;
const WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"];

const STATUS_ZH: Record<Status, string> = {
  done: "照計畫完成",
  over: "超過計畫",
  under: "做不到一半",
  skipped: "沒騎",
  extra: "計畫外",
  pending: "今天待騎",
  planned: "未來的課",
  rest: "休息",
};
const LEGEND: Status[] = ["done", "over", "under", "skipped", "extra", "planned"];

const addDays = (iso: string, n: number): string =>
  new Date(new Date(`${iso}T12:00:00Z`).getTime() + n * DAY_MS).toISOString().slice(0, 10);
const monday = (iso: string): string => addDays(iso, -((new Date(`${iso}T12:00:00Z`).getUTCDay() + 6) % 7));

/** "Z2 有氧耐力 120 分" -> "Z2 120′": what fits in a cell; the modal shows the full name. */
export const shortName = (name: string): string =>
  name
    .replace(/有氧耐力|耐力騎|重複/g, "")
    .replace(/ 分/g, "′")
    .replace(/\s+/g, " ")
    .trim();

/** Icon for a day: our role when known, else a guess from the name (the athlete's own events). */
const iconOf = (d: Day): string => {
  const role = d.planned?.role;
  if (role) return role === "recovery" ? "rest" : role;
  const name = d.planned?.name_zh ?? "";
  if (/恢復/.test(name)) return "rest";
  if (/長/.test(name)) return "long_ride";
  if (/坡/.test(name)) return "climb";
  if (/甜蜜點|Tempo|閾值|VO2|間歇/i.test(name)) return "hit";
  if (/測/.test(name)) return "test";
  return d.planned ? "endurance" : d.activities.some((a) => a.is_ride) ? "rides" : "rest";
};

const ratio = (d: Day): number | null =>
  d.planned?.tss && d.load ? Math.round((d.load / d.planned.tss) * 100) : null;

export function Calendar({ profile, info }: { profile: string; info: S["ProfileOut"] | undefined }) {
  const today = todayIso();
  const [start, setStart] = useState(() => addDays(monday(today), -7 * (WEEKS - 3)));
  const end = addDays(start, 7 * WEEKS - 1);
  const cal = useQuery({
    queryKey: ["calendar", profile, start, end],
    queryFn: () => api.calendar(profile, start, end),
    placeholderData: (prev) => prev,
  });
  const [open, setOpen] = useState<string | null>(null);
  const phone = usePhone();
  const days = cal.data?.days ?? [];
  const weeks = cal.data?.weeks ?? [];
  const shift = (n: number) => setStart(addDays(start, 7 * n));
  const touch = useRef<number | null>(null);
  const onTouchStart = (e: TouchEvent) => {
    touch.current = e.touches[0]?.clientX ?? null;
  };
  const onTouchEnd = (e: TouchEvent) => {
    const x0 = touch.current;
    const x1 = e.changedTouches[0]?.clientX;
    touch.current = null;
    if (x0 === null || x1 === undefined || Math.abs(x1 - x0) < 60) return;
    play("step");
    shift(x1 < x0 ? 2 : -2);
  };

  // While a step crosses into weeks still loading, keep showing the last day instead of closing.
  const lastOpen = useRef<Day | null>(null);
  const found = days.find((d) => d.date === open) ?? null;
  if (found) lastOpen.current = found;
  const openDay = open ? (found ?? lastOpen.current) : null;
  const step = (n: number) => {
    if (!open) return;
    play("step");
    const next = addDays(open, n);
    if (next < start) shift(-2);
    if (next > end) shift(2);
    setOpen(next);
  };

  return (
    <div className="grid calendar-page">
      <Panel
        className="calendar"
        kicker={`${mmdd(start)} – ${mmdd(end)}`}
        title="行事曆"
      >
        <div className="cal-nav">
          <button data-sfx="step" onClick={() => shift(-2)} aria-label="較早兩週">
            ← 較早
          </button>
          <button data-sfx="step" onClick={() => setStart(addDays(monday(today), -7 * (WEEKS - 3)))}>
            今天
          </button>
          <button data-sfx="step" onClick={() => shift(2)} aria-label="較晚兩週">
            較晚 →
          </button>
        </div>
        <div className="cal" onTouchStart={onTouchStart} onTouchEnd={onTouchEnd}>
          <div className="cal-head" aria-hidden>
            {WEEKDAYS.map((w) => (
              <span key={w}>{w}</span>
            ))}
            {!phone ? <span className="cal-week-head">本週</span> : null}
          </div>
          {weeks.map((w, i) => (
            <div className={`cal-row ${w.start <= today && today <= addDays(w.start, 6) ? "now" : ""}`} key={w.start}>
              {phone ? <WeekSummary w={w} /> : null}
              {days.slice(i * 7, i * 7 + 7).map((d) => (
                <Cell key={d.date} d={d} today={today} onOpen={() => setOpen(d.date)} />
              ))}
              {!phone ? <WeekSummary w={w} /> : null}
            </div>
          ))}
        </div>
        <p className="legend cal-legend">
          {LEGEND.map((s) => (
            <span key={s}>
              <i className={`sw s-${s}`} /> {STATUS_ZH[s]}
            </span>
          ))}
          <span>
            <i className="sw dot" /> 準備度
          </span>
        </p>
        <ErrorNote error={cal.error} />
      </Panel>

      <SeasonPanel profile={profile} today={today} />

      {openDay ? (
        <DayModal
          profile={profile}
          info={info}
          d={openDay}
          today={today}
          onClose={() => setOpen(null)}
          onStep={step}
        />
      ) : null}
    </div>
  );
}

function Cell({ d, today, onOpen }: { d: Day; today: string; onOpen: () => void }) {
  const rest = !d.planned && !d.activities.some((a) => a.is_ride);
  const ride = d.activities.find((a) => a.is_ride);
  const name = d.planned ? shortName(d.planned.name_zh) : ride ? (ride.classification_zh ?? ride.name ?? "騎乘") : "休息";
  const past = d.date < today;
  // Main number: done load (or planned for days still ahead); "/planned" is dropped on a phone.
  const ahead = d.status === "planned" || d.status === "pending";
  const main = ahead ? d.planned?.tss : d.load || (d.planned?.tss != null ? 0 : null);
  const of = !ahead && d.planned?.tss != null ? `/${fmt(d.planned.tss)}` : "";
  const dayNum = Number(d.date.slice(8, 10));
  const readinessTone = d.readiness_score == null ? "" : d.readiness_score >= 70 ? "go" : d.readiness_score >= 40 ? "easy" : "stop";
  return (
    <button
      data-sfx="none"
      className={`cell s-${d.status} ${d.date === today ? "is-today" : ""} ${past ? "past" : ""} ${rest ? "is-rest" : ""}`}
      onClick={onOpen}
      title={`${d.date} ${d.planned?.name_zh ?? name} · ${STATUS_ZH[d.status]}`}
      aria-label={`${mmdd(d.date)} 週${weekdayZh(d.date)} ${d.planned?.name_zh ?? name}，${STATUS_ZH[d.status]}`}
    >
      <span className="cell-top">
        <span className="cell-date">
          {dayNum === 1 ? <small className="cell-month">{Number(d.date.slice(5, 7))}/</small> : null}
          {dayNum}
        </span>
        {readinessTone ? <i className={`rdot ${readinessTone}`} title={`準備度 ${fmt(d.readiness_score)}`} /> : null}
      </span>
      <span className="cell-name">
        <Icon name={rest ? "rest" : iconOf(d)} />
        <span className="cell-label">{name}</span>
      </span>
      {main != null ? (
        <span className="cell-load">
          {fmt(main)}
          {of ? <small className="cell-of">{of}</small> : null}
        </span>
      ) : null}
      {d.notes.length ? <span className="cell-note">{d.notes[0]}</span> : null}
    </button>
  );
}

function WeekSummary({ w }: { w: S["CalendarWeek"] }) {
  const goal = w.target_tss ?? w.load_planned;
  const pct = goal ? Math.min((w.load_done / goal) * 100, 130) : 0;
  return (
    <div className={`cal-week ${w.recovery ? "recovery" : ""}`}>
      <span className="cal-week-title">
        {mmdd(w.start)} {w.phase_zh ?? ""}
        {w.recovery ? "・恢復週" : ""}
      </span>
      <span className="cal-week-load">
        <b>{fmt(w.load_done)}</b>
        {goal ? `/${fmt(goal)}` : ""} TSS
      </span>
      {goal ? (
        <span className="cal-week-bar" aria-hidden>
          <i style={{ width: `${pct}%` }} />
        </span>
      ) : null}
      <span className="cal-week-meta">
        {fmt(w.hours_done, 1)} h{w.ctl_end != null ? ` · CTL ${fmt(w.ctl_end, 0)}` : ""}
      </span>
    </div>
  );
}

/** One day in full: the planned workout (profile, why), how it went, and each ride's analysis. */
function DayModal({
  profile,
  info,
  d,
  today,
  onClose,
  onStep,
}: {
  profile: string;
  info: S["ProfileOut"] | undefined;
  d: Day;
  today: string;
  onClose: () => void;
  onStep: (n: number) => void;
}) {
  const plan = useQuery({
    queryKey: ["plan", profile, 1, d.date],
    queryFn: () => api.plan(profile, 1, d.date),
    enabled: d.planned !== null && d.planned !== undefined,
  });
  // Our stored proposal, shown only when it is the workout the calendar shows.
  const ours = plan.data?.find((p) => p.date === d.date && p.template_id && p.name_zh === d.planned?.name_zh);
  const close = useRef(onClose);
  close.current = onClose;
  const dialog = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // Braces matter: scrollTo returns a Promise in newer browsers, not a cleanup function.
    dialog.current?.scrollTo({ top: 0 });
  }, [d.date]);

  useEffect(() => {
    // The back gesture / button closes the modal instead of leaving the page: the modal owns one
    // history entry, and every close goes back through it (see requestClose).
    window.history.pushState({ calModal: true }, "");
    const onPop = () => close.current();
    window.addEventListener("popstate", onPop);
    document.body.classList.add("modal-open");
    dialog.current?.focus();
    play("open");
    return () => {
      play("close");
      window.removeEventListener("popstate", onPop);
      document.body.classList.remove("modal-open");
    };
  }, []);
  const requestClose = () => {
    if ((window.history.state as { calModal?: boolean } | null)?.calModal) window.history.back();
    else onClose();
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Keys typed into the RIDE.LOG editor stay there: Esc must not throw away an unsaved poem.
      if (e.target instanceof HTMLTextAreaElement || e.target instanceof HTMLInputElement) return;
      if (e.key === "Escape") requestClose();
      else if (e.key === "ArrowLeft") onStep(-1);
      else if (e.key === "ArrowRight") onStep(1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const pct = ratio(d);
  const rides = d.activities.filter((a) => a.is_ride);
  const others = d.activities.filter((a) => !a.is_ride);
  // A portal: the page grid animates with a transform, which would trap a fixed-position child.
  return createPortal(
    <div className="modal-backdrop" onClick={requestClose}>
      <div
        className={`modal s-${d.status}`}
        role="dialog"
        aria-modal="true"
        aria-label={`${d.date} 的課表與紀錄`}
        tabIndex={-1}
        ref={dialog}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="modal-head">
          <button className="ghost" data-sfx="none" onClick={() => onStep(-1)} aria-label="前一天">
            ‹
          </button>
          <div className="modal-title">
            <span className="kicker">
              {d.date} · 週{weekdayZh(d.date)}
              {d.date === today ? " · 今天" : ""}
            </span>
            <h2>{d.planned?.name_zh ?? (rides[0] ? (rides[0].name ?? "騎乘") : "休息日")}</h2>
          </div>
          <button className="ghost" data-sfx="none" onClick={() => onStep(1)} aria-label="後一天">
            ›
          </button>
          <button className="ghost close" data-sfx="none" onClick={requestClose} aria-label="關閉">
            ✕
          </button>
        </header>

        <div className="modal-body">
          <p className="modal-status">
            <em className={`chip s-${d.status}`}>{STATUS_ZH[d.status]}</em>
            {d.planned ? (
              <span>
                {d.load ? `完成 ${fmt(d.load)} / ` : ""}計畫 {fmt(d.planned.tss)} TSS
                {pct !== null ? `（${pct}%）` : ""}
                {d.planned.source === "proposal" ? " · 尚未寫入行事曆" : ""}
              </span>
            ) : d.load ? (
              <span>計畫外騎乘 {fmt(d.load)} TSS</span>
            ) : null}
            {d.readiness_score != null ? (
              <span>
                準備度 {fmt(d.readiness_score)} · {d.readiness_zh}
              </span>
            ) : null}
            {d.notes.map((n) => (
              <em className="chip" key={n}>
                {n}
              </em>
            ))}
          </p>

          {d.planned ? (
            <section className="modal-plan">
              <h3>課表</h3>
              {ours ? (
                <>
                  <div className="tiles">
                    <Tile label="時間" value={fmt(ours.minutes)} unit="分" />
                    <Tile label="TSS" value={fmt(ours.tss)} tone="hot" />
                    <Tile label="場地" value={ours.outdoor ? "室外" : "室內"} />
                    <Tile label="角色" value={ours.role_zh} />
                  </div>
                  <WorkoutProfile steps={ours.steps} />
                  {ours.note_zh ? <p className="note">{ours.note_zh}</p> : null}
                  <Explain e={ours.explanation} />
                  {ours.workout_text ? (
                    <details>
                      <summary>課表原文（intervals.icu 格式）</summary>
                      <pre className="code">{ours.workout_text}</pre>
                    </details>
                  ) : null}
                </>
              ) : (
                <p className="muted">
                  {d.planned.name_zh}
                  {d.planned.tss != null ? ` · ${fmt(d.planned.tss)} TSS` : ""}
                  {plan.isLoading ? "" : " · 行事曆上的課（不是這裡產生的，沒有細節）"}
                </p>
              )}
            </section>
          ) : !rides.length ? (
            <p className="muted rest-note">
              <Icon name="rest" /> 休息 · 咖啡站
            </p>
          ) : null}

          {rides.map((r) => (
            <section className="modal-ride" key={r.id}>
              <RideDetail profile={profile} id={r.id} info={info} inline />
            </section>
          ))}
          {others.length ? (
            <ul className="modal-others">
              {others.map((a) => (
                <li key={a.id}>
                  {a.name ?? a.sport} · {a.minutes}′ · {fmt(a.load)} TSS
                </li>
              ))}
            </ul>
          ) : null}
          <ErrorNote error={plan.error} />
        </div>
      </div>
    </div>,
    document.body,
  );
}

function SeasonPanel({ profile, today }: { profile: string; today: string }) {
  const season = useQuery({ queryKey: ["season", profile], queryFn: () => api.season(profile) });
  const weeks = season.data?.weeks ?? [];
  const max = Math.max(...weeks.map((w) => w.target_tss), 1);
  return (
    <Panel
      className="season-panel"
      kicker={season.data ? `${season.data.start} → ${season.data.goal_date}` : "季節"}
      title={season.data?.goal_name ?? "整季"}
    >
      {weeks.length ? (
        <div className="season">
          {weeks.map((w) => {
            const current = w.start <= today && today <= addDays(w.start, 6);
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
      ) : null}
      <p className="legend season-legend">
        <i className="sw base" /> 基礎期 <i className="sw build" /> 建構期 <i className="sw threshold" /> 閾值期{" "}
        <i className="sw test" /> 測驗週 · 淡色＝恢復週 · 測＝測驗 · 長＝超長騎
      </p>
      <ErrorNote error={season.error} />
    </Panel>
  );
}
