// Small shared pieces: data tiles, SVG charts, workout profile, explanations.
import type { ReactNode } from "react";

import type { S } from "./api/client";

const WEEKDAY_ZH = ["日", "一", "二", "三", "四", "五", "六"];

export const weekdayZh = (iso: string): string =>
  WEEKDAY_ZH[new Date(`${iso}T12:00:00`).getDay()] ?? "";

export const mmdd = (iso: string): string => `${iso.slice(5, 7)}/${iso.slice(8, 10)}`;

export const fmt = (v: number | null | undefined, digits = 0, unit = ""): string =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : `${v.toFixed(digits)}${unit}`;

export const minutes = (seconds: number | null | undefined): string =>
  seconds ? `${Math.round(seconds / 60)}′` : "—";

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
  return (
    <div className={`tile ${tone ?? ""}`}>
      <span className="tile-label">{label}</span>
      <span className="tile-value">
        {value}
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

/** Intensity profile of a workout: bar height = % FTP, width = duration. */
export function WorkoutProfile({ steps }: { steps?: S["PlanStep"][] | undefined }) {
  const flat: { s: number; pct: number; kind: string }[] = [];
  for (const st of steps ?? []) {
    for (let r = 0; r < Math.max(st.repeat ?? 1, 1); r++) {
      const pct = ((st.lo ?? 50) + (st.hi ?? st.lo ?? 50)) / 2;
      flat.push({ s: st.duration_s, pct, kind: st.kind });
    }
  }
  const total = flat.reduce((a, b) => a + b.s, 0);
  if (!total) return null;
  let x = 0;
  return (
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
          />
        );
        x += w;
        return rect;
      })}
    </svg>
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
