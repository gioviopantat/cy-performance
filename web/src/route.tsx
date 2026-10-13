import { useEffect, useRef, useState, type CSSProperties, type MouseEvent } from "react";

import { play } from "./sfx";
import { CAPSULE, CRITTER, CRITTERS, RIDER_FRAMES, groundUrl, spriteUrl, type Critter } from "./sprites";

// Route strip under the header (docs/specs/route-strip.md): a trainer bikes along a pixel route,
// critters wait in the grass and say hello, a tap catches one and it follows the bike. The
// party (at most PARTY, first in first out) is a per-viewer, per-profile preference.

const PARTY = 10;
const WORLD_H = 32; // ground strip height in sprite pixels
const PATH_BOTTOM = 21; // rider and followers stand on the path
const LANES = [14, 31]; // bottoms of the upper and lower verges
const SHOUTS = ["衝刺！⚡", "抽車！", "加油加油！", "破風中…", "最後 200 m！"];
const HELLO = ["嗨！", "早安～", "一起騎吧！", "你好呀！", "今天天氣真好"];

type Wild = { uid: number; id: string; lane: number; greeted: boolean; leaving?: boolean };
type Bubble = { key: number; who: "rider" | number; text: string };

const reducedMotion = () =>
  typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

const partyKey = (slug: string) => `cyp.party.${slug || "default"}`;
function loadParty(slug: string): string[] {
  try {
    const v = JSON.parse(localStorage.getItem(partyKey(slug)) ?? "[]") as unknown;
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string" && x in CRITTER).slice(-PARTY) : [];
  } catch {
    return [];
  }
}
function saveParty(slug: string, party: string[]) {
  try {
    localStorage.setItem(partyKey(slug), JSON.stringify(party));
  } catch {
    /* private mode */
  }
}

const sprite = (c: Critter) => spriteUrl(c.id, c.map, c.palette);

export function RouteStrip({ slug, lead, readiness }: { slug: string; lead?: string; readiness?: number | null }) {
  const road = useRef<HTMLDivElement>(null);
  const ground = useRef<HTMLDivElement>(null);
  const rider = useRef<HTMLImageElement>(null);
  const speedOut = useRef<HTMLElement>(null);
  const rpmOut = useRef<HTMLElement>(null);
  const wildEls = useRef(new Map<number, HTMLElement>());
  const wildX = useRef(new Map<number, number>());
  const sprintUntil = useRef(0);
  const uid = useRef(0);
  const [wild, setWildState] = useState<Wild[]>([]);
  const wildRef = useRef<Wild[]>([]);
  const setWild = (next: Wild[]) => {
    wildRef.current = next;
    setWildState(next);
  };
  const [party, setParty] = useState<string[]>(() => loadParty(slug));
  const partyRef = useRef(party);
  partyRef.current = party;
  const [bubbles, setBubbles] = useState<Bubble[]>([]);
  const [ball, setBall] = useState<{ x: number; y: number; to: [number, number] } | null>(null);
  const [sprint, setSprint] = useState(false);
  const [scale, setScale] = useState(3);
  const [width, setWidth] = useState(800);
  const cruise = 26 + Math.max(0, Math.min(readiness ?? 50, 100)) * 0.12; // km/h, 26–38
  const still = reducedMotion();

  useEffect(() => setParty(loadParty(slug)), [slug]);

  const say = (who: Bubble["who"], text: string, ms = 2200) => {
    const key = ++uid.current;
    setBubbles((b) => [...b.filter((x) => x.who !== who), { key, who, text }]);
    window.setTimeout(() => setBubbles((b) => b.filter((x) => x.key !== key)), ms);
  };

  // Scale: 3 on desktop (96 px tall), 2 on a phone.
  useEffect(() => {
    const fit = () => {
      setScale(window.innerWidth <= 640 ? 2 : 3);
      setWidth(road.current?.clientWidth ?? 800);
    };
    fit();
    window.addEventListener("resize", fit);
    return () => window.removeEventListener("resize", fit);
  }, []);

  const riderX = () => (road.current?.clientWidth ?? width) * (scale === 2 ? 0.55 : 0.45);

  // The loop: scroll the ground, move the critters, greet the ones the bike reaches.
  useEffect(() => {
    const r = road.current;
    if (!r || still) {
      if (speedOut.current) speedOut.current.textContent = cruise.toFixed(1);
      return;
    }
    let kmh = cruise;
    let flow = 0;
    let last = performance.now();
    let nextSpawn = last + 1500;
    let frame = 0;
    let raf = 0;
    const tick = (t: number) => {
      const dt = Math.min(t - last, 50) / 1000;
      last = t;
      const target = sprintUntil.current > t ? cruise + 24 : cruise;
      kmh += (target - kmh) * Math.min(1, dt * 2.5);
      const v = kmh * 3.2 * (scale / 3); // px per second
      flow += v * dt;
      // Whole sprite pixels, like a handheld scrolling its tile map.
      const step = Math.round(flow / scale) * scale;
      if (ground.current) ground.current.style.backgroundPositionX = `${-step}px`;
      frame = Math.floor(flow / (6 * scale)) % 2;
      if (rider.current) rider.current.src = RIDER_FRAMES[frame]!;
      const rx = riderX();
      wildX.current.forEach((x, id) => {
        const nx = x - v * dt;
        wildX.current.set(id, nx);
        const el = wildEls.current.get(id);
        if (el) el.style.transform = `translateX(${Math.round(nx / scale) * scale}px)`;
      });
      let changed = false;
      const out = wildRef.current.flatMap((w) => {
        const x = wildX.current.get(w.uid) ?? 0;
        if (x < -60) {
          wildX.current.delete(w.uid);
          changed = true;
          return [];
        }
        if (!w.greeted && !w.leaving && x > rx && x - rx < 90) {
          // The bike reaches it: both say hello.
          const c = CRITTER[w.id]!;
          changed = true;
          play("greet");
          say("rider", c.rare ? `哇！是稀有的${c.name}！` : HELLO[w.uid % HELLO.length]!);
          say(w.uid, c.cry);
          return [{ ...w, greeted: true }];
        }
        return [w];
      });
      if (changed) setWild(out);
      if (t > nextSpawn) {
        nextSpawn = t + 3500 + Math.random() * 5500;
        spawn(r.clientWidth + 20);
      }
      if (speedOut.current) speedOut.current.textContent = kmh.toFixed(1);
      if (rpmOut.current) rpmOut.current.textContent = String(Math.round(kmh * 2.75));
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [cruise, scale, still]);

  const spawn = (x: number) => {
    const ws = wildRef.current;
    if (ws.length >= 3) return;
    // A rare one now and then; critters already in the party show up less often.
    const pool = CRITTERS.filter((c) => !c.rare);
    const rare = Math.random() < 0.05;
    let pick = rare ? CRITTERS.find((c) => c.rare)! : pool[Math.floor(Math.random() * pool.length)]!;
    if (!rare && partyRef.current.includes(pick.id) && Math.random() < 0.5) {
      pick = pool[Math.floor(Math.random() * pool.length)]!;
    }
    const id = ++uid.current;
    wildX.current.set(id, x);
    setWild([...ws, { uid: id, id: pick.id, lane: LANES[Math.floor(Math.random() * LANES.length)]!, greeted: false }]);
  };

  const sprintNow = () => {
    sprintUntil.current = performance.now() + 2600;
    setSprint(true);
    say("rider", SHOUTS[Math.floor(Math.random() * SHOUTS.length)]!, 1800);
    window.setTimeout(() => setSprint(false), 2600);
  };

  const catchOne = (e: MouseEvent, w: Wild) => {
    e.stopPropagation();
    if (w.leaving) return;
    const c = CRITTER[w.id]!;
    const x = wildX.current.get(w.uid) ?? 0;
    const rx = riderX();
    setWild(wildRef.current.map((o) => (o.uid === w.uid ? { ...o, leaving: true } : o)));
    setBall({ x: rx, y: (PATH_BOTTOM - 12) * scale, to: [x + 6 * scale, (w.lane - 8) * scale] });
    play("throw");
    window.setTimeout(() => {
      setBall(null);
      wildX.current.delete(w.uid);
      setWild(wildRef.current.filter((o) => o.uid !== w.uid));
      play("catch");
      say("rider", `收服了${c.name}！`);
      // Side effects stay out of the state updater (StrictMode runs updaters twice).
      const next = [...partyRef.current, c.id];
      if (next.length > PARTY) {
        const gone = CRITTER[next[0]!]!;
        window.setTimeout(() => {
          play("release");
          say("rider", `${gone.name}回到草叢了，掰掰～`);
        }, 1400);
      }
      const kept = next.slice(-PARTY);
      partyRef.current = kept;
      saveParty(slug, kept);
      setParty(kept);
    }, 520);
  };

  const px = (n: number) => `${n * scale}px`;
  const rx = riderX();
  const ridersBubble = bubbles.find((b) => b.who === "rider");
  return (
    <div
      className={`road route ${sprint ? "sprinting" : ""}`}
      ref={road}
      onClick={sprintNow}
      role="button"
      data-sfx="sprint"
      tabIndex={0}
      onKeyDown={(e) => {
        // Enter on a critter's own button catches it; only the strip itself sprints.
        if (e.target !== e.currentTarget || (e.key !== "Enter" && e.key !== " ")) return;
        e.preventDefault();
        play("sprint");
        sprintNow();
      }}
      aria-label="點路面讓騎士衝刺，點草叢裡的寶貝收服"
      style={{ height: px(WORLD_H) }}
    >
      <div
        className="route-ground"
        ref={ground}
        style={{ backgroundImage: `url("${groundUrl()}")`, backgroundSize: `${128 * scale}px ${WORLD_H * scale}px` }}
      />
      {wild.map((w) => {
        const c = CRITTER[w.id]!;
        const b = bubbles.find((x) => x.who === w.uid);
        return (
          <button
            key={w.uid}
            className={`mon wild ${w.leaving ? "caught" : ""} ${c.rare ? "rare" : ""}`}
            data-sfx="none"
            ref={(el) => {
              if (el) wildEls.current.set(w.uid, el);
              else wildEls.current.delete(w.uid);
            }}
            style={{ top: px(w.lane - 12), width: px(12), height: px(12), transform: `translateX(${wildX.current.get(w.uid) ?? 0}px)` }}
            onClick={(e) => catchOne(e, w)}
            aria-label={`收服${c.name}`}
            title={`${c.name}：點一下收服`}
          >
            <img src={sprite(c)} alt="" draggable={false} />
            {b ? <span className="speech">{b.text}</span> : null}
          </button>
        );
      })}
      {party
        .slice()
        .reverse()
        .map((id, i) => {
          const c = CRITTER[id]!;
          const gap = scale === 2 ? 19 : 33;
          return (
            <img
              key={`${id}-${party.length - i}`}
              className={`mon follower ${c.facesLeft ? "flip" : ""}`}
              src={sprite(c)}
              alt={c.name}
              title={c.name}
              draggable={false}
              style={{
                left: rx - 8 * scale - (i + 1) * gap,
                top: px(PATH_BOTTOM - 12),
                width: px(12),
                height: px(12),
                animationDelay: `${-i * 0.13}s`,
              }}
            />
          );
        })}
      <div className="trainer" style={{ left: rx - 8 * scale, top: px(PATH_BOTTOM - 16), width: px(16), height: px(16) }}>
        <img ref={rider} src={RIDER_FRAMES[0]} alt="" draggable={false} />
        {lead ? <span className="bib">{lead}</span> : null}
        {ridersBubble ? (
          <span className="speech mine" key={ridersBubble.key}>
            {ridersBubble.text}
          </span>
        ) : null}
      </div>
      {ball ? (
        <img
          className="capsule"
          src={CAPSULE}
          alt=""
          style={
            {
              left: ball.x,
              top: ball.y,
              width: px(8),
              height: px(8),
              ["--dx" as string]: `${ball.to[0] - ball.x}px`,
              ["--dy" as string]: `${ball.to[1] - ball.y}px`,
            } as CSSProperties
          }
        />
      ) : null}
      <div className="road-hud">
        <span>
          <b ref={speedOut}>{cruise.toFixed(1)}</b> km/h
        </span>
        <span>
          <b ref={rpmOut}>{Math.round(cruise * 2.75)}</b> rpm
        </span>
        <span className="party-count" title={party.map((id) => CRITTER[id]!.name).join("、")}>
          隊伍 {party.length}/{PARTY}
        </span>
        <span className="hint">點寶貝收服</span>
      </div>
    </div>
  );
}
