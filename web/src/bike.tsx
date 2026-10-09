import { useEffect, useRef, useState } from "react";

// Cycling decoration: small inline SVGs (no assets, theme colours via currentColor / CSS vars).

// Pedal stroke keyframes (hip -> knee -> pedal, 8 crank angles), computed once by hand.
const LEG_A =
  "M21 19L29.5 27.5L38 37;M21 19L28.3 28.5L36.5 40.5;M21 19L26.9 29.5L33 42;M21 19L28.2 28.6L29.5 40.5;" +
  "M21 19L31.1 25.4L28 37;M21 19L32.6 21.9L29.5 33.5;M21 19L33 20L33 32;M21 19L32.6 22.1L36.5 33.5;M21 19L29.5 27.5L38 37";
const LEG_B = LEG_A.split(";").slice(4, 8).concat(LEG_A.split(";").slice(0, 5)).join(";");

function Leg({ frames, near, still, dur }: { frames: string; near?: boolean; still: boolean; dur: string }) {
  const first = frames.split(";")[0];
  return (
    <path d={first} className={near ? "leg near" : "leg"}>
      {!still && <animate attributeName="d" values={frames} dur={dur} repeatCount="indefinite" />}
    </path>
  );
}

const reducedMotion = () =>
  typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

/** Side-view road bike with a pedalling rider; wheels spin when the parent has `.rolling`. */
export function Bike({
  className,
  rider = true,
  fast = false,
}: {
  className?: string;
  rider?: boolean;
  fast?: boolean;
}) {
  const still = reducedMotion();
  const dur = fast ? "0.36s" : "0.7s";
  return (
    <svg className={`bike ${className ?? ""}`} viewBox="0 0 64 48" aria-hidden>
      {rider && <Leg frames={LEG_B} still={still} dur={dur} />}
      <g className="wheel" style={{ transformOrigin: "13px 37px" }}>
        <circle cx="13" cy="37" r="9" />
        <path d="M13 28v18M4 37h18M6.6 30.6l12.8 12.8M19.4 30.6 6.6 43.4" className="spokes" />
      </g>
      <g className="wheel" style={{ transformOrigin: "51px 37px" }}>
        <circle cx="51" cy="37" r="9" />
        <path d="M51 28v18M42 37h18M44.6 30.6l12.8 12.8M57.4 30.6 44.6 43.4" className="spokes" />
      </g>
      <path d="M13 37 23 25h21L33 37H13Zm10-12 10 12M23 25l-2-4m-3 0h6M44 25l7 12M44 25l-1-5h5" className="frame" />
      <circle cx="33" cy="37" r="2.6" className="chainring" />
      {rider && (
        <>
          <path d="M21 19 38 11M38 11l4 5 4 4" className="body" />
          <circle cx="42" cy="6.5" r="3.4" className="helmet" />
          <Leg frames={LEG_A} near still={still} dur={dur} />
        </>
      )}
    </svg>
  );
}

/** Spinning wheel used as the loading indicator. */
export function WheelSpinner({ label = "載入中…" }: { label?: string }) {
  return (
    <p className="spinner muted" role="status">
      <svg viewBox="0 0 24 24" aria-hidden>
        <circle cx="12" cy="12" r="10" />
        <path d="M12 2v20M2 12h20M4.9 4.9l14.2 14.2M19.1 4.9 4.9 19.1" />
        <circle cx="12" cy="12" r="2" className="hub" />
      </svg>
      {label}
    </p>
  );
}

const MARKERS = ["0 KM", "20 KM", "40 KM", "60 KM", "80 KM", "100 KM"];
const SHOUTS = ["衝刺！⚡", "抽車！", "加油加油！", "破風中…", "爬坡模式 ⛰", "最後 200 m！"];

/**
 * Header strip: a road with km posts and a two-rider group ride. Cruise speed follows today's
 * readiness; clicking the road launches a sprint. Positions are driven by requestAnimationFrame so
 * the speed can change smoothly.
 */
export function RoadStrip({ lead, chase, readiness }: { lead?: string; chase?: string; readiness?: number | null }) {
  const road = useRef<HTMLDivElement>(null);
  const group = useRef<HTMLDivElement>(null);
  const line = useRef<HTMLDivElement>(null);
  const posts = useRef<HTMLDivElement>(null);
  const speedOut = useRef<HTMLElement>(null);
  const rpmOut = useRef<HTMLElement>(null);
  const sprintUntil = useRef(0);
  const [sprint, setSprint] = useState(false);
  const [shout, setShout] = useState<{ text: string; n: number } | null>(null);
  const [sprints, setSprints] = useState(0);
  const cruise = 26 + Math.max(0, Math.min(readiness ?? 50, 100)) * 0.12; // km/h, 26–38

  useEffect(() => {
    const r = road.current;
    const g = group.current;
    if (!r || !g) return;
    if (reducedMotion()) {
      g.style.transform = `translateX(${r.clientWidth * 0.4}px)`;
      if (speedOut.current) speedOut.current.textContent = cruise.toFixed(1);
      return;
    }
    let x = r.clientWidth * 0.3;
    let flow = 0;
    let kmh = cruise;
    let last = performance.now();
    let raf = 0;
    const tick = (t: number) => {
      const dt = Math.min(t - last, 50) / 1000;
      last = t;
      const target = sprintUntil.current > t ? cruise + 24 : cruise;
      kmh += (target - kmh) * Math.min(1, dt * 2.5);
      x += kmh * 3.2 * dt;
      if (x > r.clientWidth + 20) x = -150;
      flow += kmh * 2.2 * dt;
      // The pixel skin moves in whole 4 px steps, like an old console sprite.
      const px = document.documentElement.dataset.skin === "pixel" ? Math.round(x / 4) * 4 : x;
      g.style.transform = `translateX(${px.toFixed(1)}px)`;
      if (line.current) line.current.style.backgroundPositionX = `${(-flow).toFixed(1)}px`;
      if (posts.current) posts.current.style.transform = `translateX(${(-(flow * 0.35) % (r.clientWidth * 1)).toFixed(1)}px)`;
      if (speedOut.current) speedOut.current.textContent = kmh.toFixed(1);
      if (rpmOut.current) rpmOut.current.textContent = String(Math.round(kmh * 2.75));
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [cruise]);

  const go = () => {
    sprintUntil.current = performance.now() + 2600;
    setSprint(true);
    setSprints((n) => n + 1);
    setShout({ text: SHOUTS[sprints % SHOUTS.length]!, n: sprints });
    window.setTimeout(() => setSprint(false), 2600);
  };

  return (
    <div
      className={`road ${sprint ? "sprinting" : ""}`}
      ref={road}
      onClick={go}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), go())}
      aria-label="點路面讓騎士衝刺"
    >
      <div className="road-posts" ref={posts}>
        {MARKERS.concat(MARKERS).map((m, i) => (
          <span key={i}>{m}</span>
        ))}
      </div>
      <div className="road-line" ref={line} />
      <div className="road-hud">
        <span>
          <b ref={speedOut}>{cruise.toFixed(1)}</b> km/h
        </span>
        <span>
          <b ref={rpmOut}>{Math.round(cruise * 2.75)}</b> rpm
        </span>
        <span className="hint">{sprints ? `衝刺 ×${sprints}` : "點路面衝刺"}</span>
      </div>
      <div className={`group rolling ${sprint ? "sprint" : ""}`} ref={group}>
        <div className="rider chase">
          <Bike fast={sprint} />
          {chase && <span className="bib">{chase}</span>}
        </div>
        <div className="rider lead">
          <Bike fast={sprint} />
          {lead && <span className="bib">{lead}</span>}
          {shout && (
            <span className="shout" key={shout.n}>
              {shout.text}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

/** Faint climb silhouette at the bottom of the page. */
export function ElevationFooter() {
  return (
    <footer className="elevation" aria-hidden>
      <svg viewBox="0 0 1200 90" preserveAspectRatio="none">
        <path d="M0 90V70l80-6 70-18 60 10 90-34 70 14 60-26 110 30 70-8 90 22 80-40 90 24 70-6 90 28 70-16 100 10V90Z" />
        <path
          d="M0 70l80-6 70-18 60 10 90-34 70 14 60-26 110 30 70-8 90 22 80-40 90 24 70-6 90 28 70-16 100 10"
          className="ridge"
        />
      </svg>
      <span>▲ KOM 在前方 · 踩穩節奏</span>
    </footer>
  );
}

type IconName = "today" | "plan" | "rides" | "runs" | "rest" | "long_ride" | "hit" | "endurance" | "test" | "climb";

const PATHS: Record<IconName, string> = {
  // head unit
  today: "M7 3h10a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Zm1 4h8v5H8Zm0 8h3m2 0h3",
  // chainring
  plan: "M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16Zm0-2v2m0 16v2M2 12h2m16 0h2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6Z",
  // route
  rides: "M5 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4Zm14-10a2 2 0 1 0 0-4 2 2 0 0 0 0 4ZM6.5 15.5C9 11 15 14 16 9.5",
  // cog
  runs: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Zm0-6 1.5 3h-3Zm0 20-1.5-3h3ZM2 12l3-1.5v3Zm20 0-3 1.5v-3ZM4.9 4.9l3.2 1.1-2.1 2.1Zm14.2 14.2-3.2-1.1 2.1-2.1ZM4.9 19.1l1.1-3.2 2.1 2.1Zm14.2-14.2-1.1 3.2-2.1-2.1Z",
  // coffee cup: the café stop
  rest: "M5 9h11v5a5 5 0 0 1-5 5h-1a5 5 0 0 1-5-5Zm11 1h2a2 2 0 0 1 0 4h-2M8 3c0 2 2 2 2 4m2-4c0 2 2 2 2 4",
  // finish flag: the long ride
  long_ride: "M6 21V4m0 0h12l-2.5 4L18 12H6M10 4v8m4-8v8",
  // lightning
  hit: "M13 2 5 13h6l-1 9 8-11h-6Z",
  // wheel
  endurance: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm0 0v18M3 12h18M5.6 5.6l12.8 12.8M18.4 5.6 5.6 18.4",
  // stopwatch
  test: "M12 7a7 7 0 1 0 0 14 7 7 0 0 0 0-14Zm0 3v4l3 2M10 2h4m-2 0v3",
  // mountain
  climb: "M2 20 9 8l4 6 3-4 6 10Z",
};

export function Icon({ name, className }: { name: IconName | string; className?: string }) {
  const d = PATHS[name as IconName] ?? PATHS.endurance;
  return (
    <svg className={`icon ${className ?? ""}`} viewBox="0 0 24 24" aria-hidden>
      <path d={d} />
    </svg>
  );
}
