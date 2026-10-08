import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api, type S } from "../api/client";
import { ErrorNote, Explain, Panel, Tile, ZoneBar, fmt, minutes, mmdd } from "../ui";

const PAGE = 25;

export function Rides({ profile, info }: { profile: string; info: S["ProfileOut"] | undefined }) {
  const [offset, setOffset] = useState(0);
  const list = useQuery({
    queryKey: ["activities", profile, offset],
    queryFn: () => api.activities(profile, offset, PAGE),
  });
  const [selected, setSelected] = useState<number | null>(null);
  const items = list.data?.items ?? [];
  useEffect(() => {
    if (selected === null && items[0]) setSelected(items[0].id);
  }, [items, selected]);
  useEffect(() => setSelected(null), [profile]);

  return (
    <div className="grid rides">
      <Panel kicker={`共 ${list.data?.total ?? "—"} 趟`} title="騎乘紀錄">
        <ul className="ride-list">
          {items.map((r) => (
            <li key={r.id}>
              <button className={selected === r.id ? "on" : ""} onClick={() => setSelected(r.id)}>
                <span className="ride-date">{mmdd(r.date)}</span>
                <span className="ride-name">{r.name}</span>
                <span className="ride-meta">
                  {minutes(r.moving_s)} · {fmt(r.tss)} TSS · IF {fmt(r.intensity_factor, 2)}
                </span>
                {r.classification_zh ? <em className="chip">{r.classification_zh}</em> : null}
              </button>
            </li>
          ))}
        </ul>
        <div className="pager">
          <button disabled={offset === 0} onClick={() => setOffset(Math.max(offset - PAGE, 0))}>
            ← 較新
          </button>
          <button
            disabled={!list.data || offset + PAGE >= list.data.total}
            onClick={() => setOffset(offset + PAGE)}
          >
            較舊 →
          </button>
        </div>
        <ErrorNote error={list.error} />
      </Panel>
      {selected !== null ? <RideDetail profile={profile} id={selected} info={info} /> : null}
    </div>
  );
}

function RideDetail({ profile, id, info }: { profile: string; id: number; info: S["ProfileOut"] | undefined }) {
  const d = useQuery({ queryKey: ["activity", profile, id], queryFn: () => api.activity(profile, id) });
  const log = useQuery({ queryKey: ["ride-log", profile, id], queryFn: () => api.rideLog(profile, id), retry: false });
  const a = d.data;
  const s = a?.summary;
  return (
    <Panel className="ride" kicker={s ? `${s.date} · ${s.classification_zh ?? ""}` : ""} title={s?.name ?? "…"}>
      {a && s ? (
        <>
          <div className="tiles six">
            <Tile label="時間" value={minutes(s.moving_s)} />
            <Tile label="距離" value={fmt((a.distance_m ?? 0) / 1000, 1)} unit="km" />
            <Tile label="爬升" value={fmt(a.elev_gain_m)} unit="m" />
            <Tile label="NP" value={fmt(s.np_w)} unit="W" tone="hot" />
            <Tile label="TSS" value={fmt(s.tss)} />
            <Tile label="IF" value={fmt(s.intensity_factor, 2)} />
            <Tile label="平均心率" value={fmt(a.avg_hr)} unit="bpm" />
            <Tile label="心率漂移" value={fmt(s.decoupling_pct, 1, "%")} tone={(s.decoupling_pct ?? 0) > 5 ? "warn" : "cool"} />
            <Tile label="VI" value={fmt(a.vi, 2)} tone={(a.vi ?? 1) > 1.2 ? "warn" : undefined} />
          </div>
          <Feedback profile={profile} id={id} />
          <ZoneBar zones={a.time_in_zone_power} label="功率區間" />
          <ZoneBar zones={a.time_in_zone_hr} label="心率區間" />
          <Explain e={a.explanation} />
          {a.climbs?.length ? (
            <table className="climbs">
              <thead>
                <tr>
                  <th>爬坡</th>
                  <th>時間</th>
                  <th>爬升</th>
                  <th>坡度</th>
                  <th>功率</th>
                  <th>心率</th>
                </tr>
              </thead>
              <tbody>
                {(a.climbs ?? [])
                  .filter((c) => c["kind"] === "climb")
                  .map((c, i) => (
                    <tr key={i}>
                      <td>#{i + 1}</td>
                      <td>{minutes(Number(c["moving_s"]))}</td>
                      <td>{fmt(Number(c["gain_m"]))} m</td>
                      <td>{fmt(Number(c["avg_grade_pct"]), 1)}%</td>
                      <td>{fmt(Number(c["avg_w"]))} W</td>
                      <td>{fmt(Number(c["avg_hr"]))}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          ) : null}
        </>
      ) : null}
      {log.data ? (
        <RideLogEditor key={id} profile={profile} id={id} rideName={s?.name ?? ""} log={log.data} info={info} />
      ) : null}
      <ErrorNote error={d.error} />
    </Panel>
  );
}


/** RIDE.LOG text: editable (add the poem), copy, preview / write to Strava (flag-gated).
 *
 *  The textarea is never replaced while it holds unsaved edits: the server text is adopted only
 *  when it is clean, and after a save from the save response. A Strava write sends exactly the
 *  text that was previewed, after a confirmation naming the athlete and the ride. */
function RideLogEditor({
  profile,
  id,
  rideName,
  log,
  info,
}: {
  profile: string;
  id: number;
  rideName: string;
  log: S["RideLogOut"];
  info: S["ProfileOut"] | undefined;
}) {
  const client = useQueryClient();
  const [server, setServer] = useState(log);
  const [text, setText] = useState(log.text);
  const dirty = text !== server.text;
  useEffect(() => {
    // A newer server copy (e.g. after a Strava write elsewhere) replaces only a clean editor.
    if (log === server) return;
    if (text === server.text) setText(log.text);
    setServer(log);
  }, [log, server, text]);
  const adopt = (fresh: S["RideLogOut"]) => {
    client.setQueryData(["ride-log", profile, id], fresh);
    setServer(fresh);
    setText(fresh.text);
  };
  const [copied, setCopied] = useState(false);
  const [preview, setPreview] = useState<{ text: string; result: S["StravaDescriptionOut"] } | null>(null);
  const [confirming, setConfirming] = useState<"strava" | "restore" | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
      setConfirming(null);
    }
  };
  const save = (value: string) => run(async () => adopt(await api.saveRideLog(profile, id, value)));
  const previewStrava = () =>
    run(async () => setPreview({ text, result: await api.stravaDescription(profile, id, text, false) }));
  const writeStrava = (previewed: string) =>
    run(async () => {
      const result = await api.stravaDescription(profile, id, previewed, true);
      setPreview({ text: previewed, result });
      if (result.written) adopt(await api.rideLog(profile, id));
    });
  const previewIsCurrent = preview !== null && !preview.result.written && preview.text === text;
  const who = info?.display_name ?? profile;
  return (
    <div className="ridelog">
      <div className="ridelog-head">
        <span className="kicker">
          RIDE.LOG · {server.source === "saved" ? "已儲存版本（含你加的內容）" : "自動產生（可編輯、加上藏頭詩）"}
          {dirty ? " · 未儲存" : ""}
        </span>
        <button
          onClick={() => {
            void navigator.clipboard.writeText(text).then(() => {
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            });
          }}
        >
          {copied ? "已複製 ✓" : "複製"}
        </button>
      </div>
      <textarea className="code ridelog-text" value={text} onChange={(e) => setText(e.target.value)} rows={Math.max(text.split("\n").length + 1, 10)} />
      <div className="buttons row">
        <button disabled={busy || !dirty} onClick={() => void save(text)}>
          儲存
        </button>
        {server.source === "saved" ? (
          <button disabled={busy} onClick={() => setConfirming("restore")}>
            還原成自動產生
          </button>
        ) : null}
      </div>
      {confirming === "restore" ? (
        <div className="confirm" role="dialog" aria-modal="true">
          <p>會刪掉已儲存的版本（含你加的詩），改回自動產生的內容。上一版會留一份備份。</p>
          <div className="buttons row">
            <button className="primary" onClick={() => void save("")}>
              確定還原
            </button>
            <button onClick={() => setConfirming(null)}>取消</button>
          </div>
        </div>
      ) : null}
      {info?.strava_write_allowed ? (
        <div className="buttons row">
          <button disabled={busy} onClick={() => void previewStrava()}>
            預覽 Strava 說明
          </button>
          <button
            className="primary"
            disabled={busy || !previewIsCurrent}
            title={previewIsCurrent ? "" : "先按「預覽」，確認內容後才能寫入"}
            onClick={() => setConfirming("strava")}
          >
            寫入 Strava
          </button>
        </div>
      ) : (
        <p className="muted small">寫入 Strava 功能未開啟（strava.write_description）；可先複製貼上。</p>
      )}
      {confirming === "strava" && preview ? (
        <div className="confirm" role="dialog" aria-modal="true">
          <p>
            會更新 <strong>{who}</strong> 在 Strava 上「<strong>{rideName || `活動 ${id}`}</strong>」的說明：原本寫的文字會保留，舊的
            RIDE.LOG 區塊會被取代，並把這段文字存成這趟的版本。寫入後會變成：
          </p>
          <pre className="code">{preview.result.after}</pre>
          <div className="buttons row">
            <button className="primary" onClick={() => void writeStrava(preview.text)}>
              確定寫入 {who} 的 Strava
            </button>
            <button onClick={() => setConfirming(null)}>取消</button>
          </div>
        </div>
      ) : null}
      {preview && confirming === null ? (
        <div className="job ok">
          <p>
            {preview.result.written
              ? `✓ 已寫入 Strava（活動 ${preview.result.strava_id}）`
              : preview.text === text
                ? "預覽：Strava 上會變成這樣"
                : "預覽（文字已修改，寫入前請重新預覽）"}
          </p>
          <pre className="code">{preview.result.after}</pre>
        </div>
      ) : null}
      <ErrorNote error={error} />
    </div>
  );
}


const FEEL = [
  [1, "很強"],
  [2, "不錯"],
  [3, "普通"],
  [4, "很累"],
  [5, "很差"],
] as const;

/** Post-ride feeling: RPE 1–10 and feel 1–5. Saved at once; tomorrow's readiness uses it. */
function Feedback({ profile, id }: { profile: string; id: number }) {
  const q = useQuery({ queryKey: ["feedback", profile, id], queryFn: () => api.feedback(profile, id) });
  const [error, setError] = useState<unknown>(null);
  const save = async (rpe: number | null, feel: number | null) => {
    setError(null);
    try {
      await api.saveFeedback(profile, id, rpe, feel);
      void q.refetch();
    } catch (e) {
      setError(e);
    }
  };
  const rpe = q.data?.rpe ?? null;
  const feel = q.data?.feel ?? null;
  return (
    <div className="feedback">
      <div className="feedback-head">
        <span className="kicker">騎完感受</span>
        <span className="muted small">
          {q.data?.source === "web"
            ? "已記錄，明天的課表會參考"
            : q.data?.source === "intervals.icu"
              ? "來自 intervals.icu"
              : "還沒填：點一下就好，明天的課表會參考"}
        </span>
      </div>
      <div className="scale" role="group" aria-label="RPE 自覺強度 1–10">
        <span className="scale-label">RPE</span>
        {Array.from({ length: 10 }, (_, i) => i + 1).map((n) => (
          <button key={n} className={rpe === n ? "on" : ""} onClick={() => void save(n, feel)} title={`RPE ${n}`}>
            {n}
          </button>
        ))}
      </div>
      <div className="scale" role="group" aria-label="感覺">
        <span className="scale-label">感覺</span>
        {FEEL.map(([n, label]) => (
          <button key={n} className={feel === n ? "on" : ""} onClick={() => void save(rpe, n)}>
            {label}
          </button>
        ))}
      </div>
      <ErrorNote error={error} />
    </div>
  );
}
