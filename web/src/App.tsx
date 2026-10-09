import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { flushSync } from "react-dom";

import { api } from "./api/client";
import { todayIso } from "./api/hooks";
import { ElevationFooter, Icon, RoadStrip, WheelSpinner } from "./bike";
import { ErrorNote } from "./ui";
import { Plan } from "./views/Plan";
import { Rides } from "./views/Rides";
import { Runs } from "./views/Runs";
import { Today } from "./views/Today";

const TABS = [
  ["today", "今天"],
  ["plan", "課表"],
  ["rides", "紀錄"],
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

type Skin = "default" | "pixel";

/** One-click 8-bit skin: sets `data-skin` on <html>; the swap dissolves in steps where supported. */
function SkinToggle() {
  const [skin, setSkin] = useState<Skin>(() => (remembered("cyp.skin", "default") === "pixel" ? "pixel" : "default"));
  const apply = (next: Skin) => {
    document.documentElement.dataset.skin = next;
    remember("cyp.skin", next);
    setSkin(next);
  };
  const toggle = () => {
    const next: Skin = skin === "pixel" ? "default" : "pixel";
    const doc = document as Document & { startViewTransition?: (cb: () => void) => unknown };
    if (doc.startViewTransition) doc.startViewTransition(() => flushSync(() => apply(next)));
    else apply(next);
  };
  return (
    <button
      className={`skin-toggle ${skin === "pixel" ? "on" : ""}`}
      onClick={toggle}
      title={skin === "pixel" ? "切回一般風格" : "切換成像素風格"}
      aria-pressed={skin === "pixel"}
    >
      <svg viewBox="0 0 8 8" aria-hidden shapeRendering="crispEdges">
        <path d="M2 0h4v1H2zM1 1h6v1H1zM0 2h2v1H0zm3 0h2v1H3zm3 0h2v1H6zM0 3h8v2H0zM1 5h2v1H1zm4 0h2v1H5zM0 6h2v1H0zm6 0h2v1H6z" />
      </svg>
      <span>{skin === "pixel" ? "8-BIT" : "像素"}</span>
    </button>
  );
}

export function App() {
  const profiles = useQuery({ queryKey: ["profiles", "*"], queryFn: api.profiles, refetchInterval: 60_000 });
  const [slug, setSlug] = useState(() => remembered("cyp.profile", ""));
  const [tab, setTab] = useState<Tab>(() => {
    const fromHash = window.location.hash.slice(1);
    return (TABS.some(([id]) => id === fromHash) ? fromHash : remembered("cyp.tab", "today")) as Tab;
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
          <SkinToggle />
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
          ) : tab === "plan" ? (
            <Plan profile={slug} />
          ) : tab === "rides" ? (
            <Rides profile={slug} info={info} />
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
