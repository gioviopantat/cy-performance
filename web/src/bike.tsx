
// Small inline SVGs: loading wheel, page footer, icons (no assets, colours via currentColor).

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

type IconName = "today" | "calendar" | "plan" | "rides" | "runs" | "rest" | "long_ride" | "hit" | "endurance" | "test" | "climb";

const PATHS: Record<IconName, string> = {
  // head unit
  today: "M7 3h10a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Zm1 4h8v5H8Zm0 8h3m2 0h3",
  // calendar page with a ride day ticked
  calendar: "M5 5h14a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1Zm-1 5h16M8 3v4m8-4v4m-7.5 7 2 2 4-4",
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
