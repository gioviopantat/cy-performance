import { useQuery } from "@tanstack/react-query";
import { useEffect, useState, useSyncExternalStore } from "react";

import { api } from "./api/client";
import { todayIso } from "./api/hooks";
import { ElevationFooter, Icon, RoadStrip, WheelSpinner } from "./bike";
import { musicOn, setMusic, subscribeMusic } from "./music";
import { setSound, soundOn, subscribeSound } from "./sfx";
import { ErrorNote } from "./ui";
import { Calendar } from "./views/Calendar";
import { Runs } from "./views/Runs";
import { Today } from "./views/Today";

const TABS = [
  ["today", "今天"],
  ["calendar", "行事曆"],
  ["runs", "執行"],
] as const;
type Tab = (typeof TABS)[number][0];

const remembered = (key: string, fallback: string): string => {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
};
const remember = (key: string, value: string): void => {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* private mode */
  }
};

/** Mute switch for the 8-bit sound effects (a per-viewer preference). */
function SoundToggle() {
  const on = useSyncExternalStore(subscribeSound, soundOn);
  return (
    <button
      className={`sound-toggle ${on ? "on" : ""}`}
      data-sfx="none"
      onClick={() => setSound(!on)}
      title={on ? "關閉音效" : "開啟音效"}
      aria-pressed={on}
    >
      <svg viewBox="0 0 8 8" aria-hidden shapeRendering="crispEdges">
        <path d={on ? "M0 3h2v2H0zM2 2h1v4H2zM3 1h1v6H3zM5 2h1v1H5zm0 3h1v1H5zm1-2h1v2H6z" : "M0 3h2v2H0zM2 2h1v4H2zM3 1h1v6H3zM5 2h1v1H5zm2 0h1v1H7zM6 3h1v2H6zM5 5h1v1H5zm2 0h1v1H7z"} />
      </svg>
      <span>{on ? "音效" : "靜音"}</span>
    </button>
  );
}

/** Background music switch (the lo-fi chiptune loop in music.ts). */
function MusicToggle() {
  const on = useSyncExternalStore(subscribeMusic, musicOn);
  return (
    <button
      className={`sound-toggle ${on ? "on" : ""}`}
      onClick={() => setMusic(!on)}
      title={on ? "關閉背景音樂" : "開啟背景音樂"}
      aria-pressed={on}
    >
      <svg viewBox="0 0 8 8" aria-hidden shapeRendering="crispEdges">
        <path d={on ? "M3 0h4v1H3zM3 1h1v4H3zM6 1h1v4H6zM1 5h3v2H1zM4 5h3v2H4z" : "M3 0h4v1H3zM3 1h1v4H3zM6 1h1v4H6zM1 5h3v2H1zM4 5h3v2H4zM0 0h1v1H0zm7 7h1v1H7zM1 1h1v1H1zm5 5h1v1H6z"} />
      </svg>
      <span>{on ? "音樂" : "無音樂"}</span>
    </button>
  );
}

export function App() {
  const profiles = useQuery({ queryKey: ["profiles", "*"], queryFn: api.profiles, refetchInterval: 60_000 });
  const [slug, setSlug] = useState(() => remembered("cyp.profile", ""));
  const [tab, setTab] = useState<Tab>(() => {
    // 課表 / 紀錄 were merged into 行事曆: old links and remembered tabs land there.
    const legacy = (id: string) => (id === "plan" || id === "rides" ? "calendar" : id);
    const fromHash = legacy(window.location.hash.slice(1));
    const wanted = TABS.some(([id]) => id === fromHash) ? fromHash : legacy(remembered("cyp.tab", "today"));
    return (TABS.some(([id]) => id === wanted) ? wanted : "today") as Tab;
  });
  const list = profiles.data ?? [];
  const known = list.some((p) => p.slug === slug);

  useEffect(() => {
    if (list.length && !known) setSlug((list.find((p) => p.default) ?? list[0])!.slug);
  }, [list, known]);

  const info = list.find((p) => p.slug === slug);
  const chase = list.find((p) => p.slug !== slug)?.display_name;
  const today = todayIso();
  const ready = useQuery({
    queryKey: ["readiness", slug, today],
    queryFn: () => api.readiness(slug, today),
    enabled: known,
  });

  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <span className="mark" aria-hidden>
            <svg viewBox="0 0 32 32">
              <path d="M5 22 L12 14 L17 18 L27 8" />
            </svg>
          </span>
          <span className="brand-text">
            cy<b>·</b>performance
          </span>
          <SoundToggle />
          <MusicToggle />
        </div>
        <nav className="profiles" aria-label="選手">
          {list.map((p) => (
            <button
              key={p.slug}
              className={p.slug === slug ? "on" : ""}
              onClick={() => {
                setSlug(p.slug);
                remember("cyp.profile", p.slug);
              }}
            >
              <span className={`dot ${p.last_run_status ?? "none"}`} />
              {p.display_name}
              <em className={`mode ${p.planner_mode}`}>{p.planner_mode === "apply" ? "自動" : "預覽"}</em>
            </button>
          ))}
        </nav>
        <nav className="tabs" aria-label="頁面">
          {TABS.map(([id, label]) => (
            <button
              key={id}
              data-sfx="tab"
              className={tab === id ? "on" : ""}
              onClick={() => {
                setTab(id);
                remember("cyp.tab", id);
                window.history.replaceState(null, "", `#${id}`);
              }}
            >
              <Icon name={id} />
              {label}
            </button>
          ))}
        </nav>
      </header>
      <RoadStrip lead={info?.display_name} chase={chase} readiness={ready.data?.score} />
      {info ? (
        <p className="status-line">
          <span>{info.icu_athlete_id ?? "?"}</span>
          <span>上次執行 {info.last_run ?? "—"} {info.last_run_status ?? ""}</span>
          <span>Garmin 推送 {info.garmin_upload_workouts === false ? "關閉 ⚠" : info.garmin_upload_workouts ? "開啟" : "未知"}</span>
        </p>
      ) : null}
      <ErrorNote error={profiles.error} />
      <main key={`${slug}:${tab}`}>
        {slug ? (
          tab === "today" ? (
            <Today profile={slug} info={info} />
          ) : tab === "calendar" ? (
            <Calendar profile={slug} info={info} />
          ) : (
            <Runs profile={slug} />
          )
        ) : (
          <WheelSpinner />
        )}
      </main>
      <ElevationFooter />
    </div>
  );
}
