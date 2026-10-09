// Small shared pieces: data tiles, SVG charts, workout profile, explanations.
import { useEffect, useRef, useState, useSyncExternalStore, type PointerEvent, type ReactNode } from "react";

import type { S } from "./api/client";

const WEEKDAY_ZH = ["日", "一", "二", "三", "四", "五", "六"];

export const weekdayZh = (iso: string): string =>
  WEEKDAY_ZH[new Date(`${iso}T12:00:00`).getDay()] ?? "";

export const mmdd = (iso: string): string => `${iso.slice(5, 7)}/${iso.slice(8, 10)}`;

export const fmt = (v: number | null | undefined, digits = 0, unit = ""): string =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : `${v.toFixed(digits)}${unit}`;

export const minutes = (seconds: number | null | undefined): string =>
  seconds ? `${Math.round(seconds / 60)}′` : "—";

const PHONE = "(max-width: 640px)";

/** True on a phone-width screen; follows rotation and window resizes. */
export function usePhone(): boolean {
  return useSyncExternalStore(
    (cb) => {
      const m = window.matchMedia(PHONE);
      m.addEventListener("change", cb);
      return () => m.removeEventListener("change", cb);
    },
    () => window.matchMedia(PHONE).matches,
  );
}

/** Scrolls an element to the top of the screen after the next paint (once the layout settled). */
export const scrollToTop = (id: string): void => {
  requestAnimationFrame(() =>
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" }),
  );
};

const reducedMotion = () =>
  typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

/** Rolls a numeric string up from zero (like a head unit waking up); other strings pass through. */
export function useCountUp(value: string, ms = 800): string {
  const m = /^(-?)(\d+)(?:\.(\d+))?$/.exec(value);
  const [shown, setShown] = useState(m && !reducedMotion() ? value.replace(/\d/g, "0") : value);
  useEffect(() => {
    if (!m || reducedMotion()) {
      setShown(value);
      return;
    }
    const target = Number(value);
    const digits = m[3]?.length ?? 0;
    const t0 = performance.now();
    let raf = 0;
    const step = (t: number) => {
      const k = Math.min((t - t0) / ms, 1);
      const eased = 1 - (1 - k) ** 3;
      setShown((target * eased).toFixed(digits));
      if (k < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [value, ms]);
  return shown;
}

/** A head-unit style data field: tiny label, big condensed number. */
export function Tile({
  label,
  value,
  unit,
  tone,
}: {
  label: string;
  value: string;
  unit?: string;
  tone?: "hot" | "cool" | "warn";
}) {
  const shown = useCountUp(value);
  return (
    <div className={`tile ${tone ?? ""}`}>
      <span className="tile-label">{label}</span>
      <span className="tile-value">
        {shown}
        {unit ? <small>{unit}</small> : null}
      </span>
    </div>
  );
}

export function Panel({
  title,
  kicker,
  actions,
  children,
  className,
}: {
  title: string;
  kicker?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className ?? ""}`}>
      <header className="panel-head">
        <div>
          {kicker ? <span className="kicker">{kicker}</span> : null}
          <h2>{title}</h2>
        </div>
        {actions}
      </header>
      {children}
    </section>
  );
}

export function Explain({ e }: { e: S["Explanation"] | null | undefined }) {
  if (!e) return null;
  return (
    <div className="explain">
      <p className="explain-head">{e.headline_zh}</p>
      {e.because?.length ? (
        <ul>
          {e.because.slice(0, 4).map((b, i) => (
            <li key={i}>{b.text_zh}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/**
 * Intensity profile of a workout: bar height = % FTP, width = duration. Hovering scrubs a little
 * rider along the workout and reads out minute, % FTP and zone.
 */
export function WorkoutProfile({ steps }: { steps?: S["PlanStep"][] | undefined }) {
  const [at, setAt] = useState<number | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const flat: { s: number; pct: number; kind: string }[] = [];
  for (const st of steps ?? []) {
    for (let r = 0; r < Math.max(st.repeat ?? 1, 1); r++) {
      const pct = ((st.lo ?? 50) + (st.hi ?? st.lo ?? 50)) / 2;
      flat.push({ s: st.duration_s, pct, kind: st.kind });
    }
  }
  const total = flat.reduce((a, b) => a + b.s, 0);
  if (!total) return null;
  const move = (e: PointerEvent) => {
    const r = box.current?.getBoundingClientRect();
    if (r) setAt(Math.min(Math.max((e.clientX - r.left) / r.width, 0), 1));
  };
  let hover: { pct: number; min: number } | null = null;
  if (at !== null) {
    let acc = 0;
    for (const b of flat) {
      acc += b.s;
      if (acc / total >= at) {
        hover = { pct: b.pct, min: Math.round((at * total) / 60) };
        break;
      }
    }
  }
  let x = 0;
  return (
    <div className="profile-wrap" ref={box} onPointerMove={move} onPointerDown={move} onPointerLeave={() => setAt(null)}>
      <svg className="profile" viewBox="0 0 1000 120" preserveAspectRatio="none" role="img">
        <title>強度輪廓</title>
        {[55, 75, 90, 105].map((y) => (
          <line key={y} x1={0} x2={1000} y1={120 - y} y2={120 - y} className="grid" />
        ))}
        {flat.map((b, i) => {
          const w = (b.s / total) * 1000;
          const h = Math.min(Math.max(b.pct, 30), 120);
          const rect = (
            <rect
              key={i}
              x={x}
              y={120 - h}
              width={Math.max(w - 1, 1)}
              height={h}
              className={`zone z${zoneOf(b.pct)}`}
              style={{ animationDelay: `${Math.min(i * 25, 600)}ms` }}
            />
          );
          x += w;
          return rect;
        })}
      </svg>
      {at !== null && hover ? (
        <div
          className="scrub"
          style={{ left: `${at * 100}%`, bottom: `${(Math.min(Math.max(hover.pct, 30), 120) / 120) * 100}%` }}
        >
          <span className="scrub-dot" />
          <span className="scrub-tip">
            第 {hover.min} 分 · {Math.round(hover.pct)}% FTP · Z{zoneOf(hover.pct)}
          </span>
        </div>
      ) : (
        <span className="scrub-hint">滑過或按住拖曳看每一段 →</span>
      )}
    </div>
  );
}

const zoneOf = (pct: number): number =>
  pct < 56 ? 1 : pct < 76 ? 2 : pct < 91 ? 3 : pct < 106 ? 4 : pct < 121 ? 5 : 6;

/** CTL / ATL lines over TSB bars. */
export function FitnessChart({ points }: { points: S["FitnessPoint"][] }) {
  const pts = points.filter((p) => p.ctl !== null && p.atl !== null);
  if (pts.length < 2) return <p className="muted">還沒有足夠的體能資料。</p>;
  const W = 1000;
  const H = 220;
  const max = Math.max(...pts.map((p) => Math.max(p.ctl ?? 0, p.atl ?? 0)), 10) * 1.1;
  const tsbMax = Math.max(...pts.map((p) => Math.abs(p.tsb ?? 0)), 10);
  const sx = (i: number) => (i / (pts.length - 1)) * W;
  const sy = (v: number) => H - 40 - (v / max) * (H - 60);
  const line = (key: "ctl" | "atl") =>
    pts.map((p, i) => `${i ? "L" : "M"}${sx(i).toFixed(1)},${sy(p[key] ?? 0).toFixed(1)}`).join("");
  const last = pts[pts.length - 1]!;
  return (
    <FitnessHover pts={pts} sx={sx} sy={sy} W={W} H={H}>
      <svg className="fitness" viewBox={`0 0 ${W} ${H}`} role="img">
        <title>體能（CTL）與疲勞（ATL）</title>
        {pts.map((p, i) => {
          const v = p.tsb ?? 0;
          const h = (Math.abs(v) / tsbMax) * 30;
          return (
            <rect
              key={p.date}
              x={sx(i) - 1}
              width={2.4}
              y={v >= 0 ? H - 20 - h : H - 20}
              height={h}
              className={v >= 0 ? "tsb pos" : "tsb neg"}
            />
          );
        })}
        <line x1={0} x2={W} y1={H - 20} y2={H - 20} className="axis" />
        <path d={line("atl")} className="atl" />
        <path d={line("ctl")} className="ctl" />
        <circle cx={sx(pts.length - 1)} cy={sy(last.ctl ?? 0)} r={5} className="ctl-dot" />
      </svg>
    </FitnessHover>
  );
}

/** Crosshair over the fitness chart: date, CTL, ATL, TSB of the day under the pointer. */
function FitnessHover({
  pts,
  sx,
  sy,
  W,
  H,
  children,
}: {
  pts: S["FitnessPoint"][];
  sx: (i: number) => number;
  sy: (v: number) => number;
  W: number;
  H: number;
  children: ReactNode;
}) {
  const [i, setI] = useState<number | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const p = i === null ? null : pts[i];
  const pick = (e: PointerEvent) => {
    const r = box.current?.getBoundingClientRect();
    if (r) setI(Math.round(Math.min(Math.max((e.clientX - r.left) / r.width, 0), 1) * (pts.length - 1)));
  };
  return (
    <div
      className="fitness-wrap"
      ref={box}
      onPointerMove={pick}
      onPointerDown={pick}
      onPointerLeave={() => setI(null)}
    >
      {children}
      {p && i !== null ? (
        <>
          <span className="xhair" style={{ left: `${(sx(i) / W) * 100}%` }} />
          <span className="xhair-dot" style={{ left: `${(sx(i) / W) * 100}%`, top: `${(sy(p.ctl ?? 0) / H) * 100}%` }} />
          <span className={`xhair-tip ${sx(i) > W * 0.7 ? "left" : ""}`} style={{ left: `${(sx(i) / W) * 100}%` }}>
            <b>{mmdd(p.date)}</b> CTL {fmt(p.ctl, 1)} · ATL {fmt(p.atl, 1)} · TSB {fmt(p.tsb, 1)}
          </span>
        </>
      ) : null}
    </div>
  );
}

/** Horizontal stacked bar of time in zones. */
export function ZoneBar({ zones, label }: { zones: Record<string, number> | null | undefined; label: string }) {
  if (!zones) return null;
  const keys = Object.keys(zones).sort();
  const total = keys.reduce((a, k) => a + (zones[k] ?? 0), 0);
  if (!total) return null;
  return (
    <div className="zonebar">
      <span className="zonebar-label">{label}</span>
      <div className="zonebar-track">
        {keys.map((k) => {
          const share = (zones[k] ?? 0) / total;
          return share > 0.004 ? (
            <span
              key={k}
              className={`zone z${Math.min(Number(k.replace(/\D/g, "")) || 1, 6)}`}
              style={{ flexGrow: share }}
              title={`${k} ${(share * 100).toFixed(0)}%`}
            >
              {share > 0.08 ? `${k} ${(share * 100).toFixed(0)}%` : ""}
            </span>
          ) : null;
        })}
      </div>
    </div>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  return <p className="error">⚠ {error instanceof Error ? error.message : String(error)}</p>;
}
