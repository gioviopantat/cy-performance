import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api } from "./api/client";
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
              {label}
            </button>
          ))}
        </nav>
      </header>
      {info ? (
        <p className="status-line">
          <span>{info.icu_athlete_id ?? "?"}</span>
          <span>上次執行 {info.last_run ?? "—"} {info.last_run_status ?? ""}</span>
          <span>Garmin 推送 {info.garmin_upload_workouts === false ? "關閉 ⚠" : info.garmin_upload_workouts ? "開啟" : "未知"}</span>
        </p>
      ) : null}
      <ErrorNote error={profiles.error} />
      <main key={slug}>
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
          <p className="muted">載入中…</p>
        )}
      </main>
    </div>
  );
}
