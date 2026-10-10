// 8-bit sound effects synthesised with Web Audio (no files): square / triangle blips and a noise
// burst, like an old console. A per-viewer preference (`localStorage` cyp.sound), on by default.

type Note = { f: number; to?: number; ms: number; wave?: OscillatorType; gain?: number };

export type Sfx = "blip" | "tab" | "open" | "close" | "step" | "sprint" | "coin" | "error";

const C5 = 523.25;
const E5 = 659.25;
const G5 = 783.99;
const B5 = 987.77;
const C6 = 1046.5;
const E6 = 1318.5;

const SOUNDS: Record<Sfx, Note[]> = {
  blip: [{ f: 880, to: 660, ms: 45 }],
  tab: [
    { f: E5, ms: 40 },
    { f: B5, ms: 55 },
  ],
  open: [
    { f: C5, ms: 40 },
    { f: E5, ms: 40 },
    { f: G5, ms: 40 },
    { f: C6, ms: 80 },
  ],
  close: [
    { f: G5, ms: 35 },
    { f: E5, ms: 35 },
    { f: C5, ms: 60 },
  ],
  step: [{ f: 520, ms: 30, wave: "triangle", gain: 0.12 }],
  sprint: [{ f: 220, to: 880, ms: 160 }],
  coin: [
    { f: B5, ms: 70 },
    { f: E6, ms: 220 },
  ],
  error: [{ f: 196, to: 110, ms: 200, wave: "sawtooth" }],
};

const KEY = "cyp.sound";
let ctx: AudioContext | null = null;
let enabled = (() => {
  try {
    return localStorage.getItem(KEY) !== "off";
  } catch {
    return true;
  }
})();
const listeners = new Set<() => void>();

export const soundOn = (): boolean => enabled;

export function setSound(on: boolean): void {
  enabled = on;
  try {
    localStorage.setItem(KEY, on ? "on" : "off");
  } catch {
    /* private mode */
  }
  listeners.forEach((l) => l());
  if (on) play("coin");
}

/** For useSyncExternalStore. */
export function subscribeSound(cb: () => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

/** The page's one AudioContext (shared with the background music), or null without Web Audio. */
export function audioContext(): AudioContext | null {
  if (typeof window === "undefined" || !("AudioContext" in window)) return null;
  ctx ??= new AudioContext();
  if (ctx.state === "suspended") void ctx.resume();
  return ctx;
}

/** Plays one effect (silently does nothing when muted or without Web Audio). */
export function play(name: Sfx): void {
  if (!enabled) return;
  try {
    const ctx = audioContext();
    if (!ctx) return;
    let t = ctx.currentTime + 0.005;
    const out = ctx.createGain();
    out.gain.value = 0.35;
    out.connect(ctx.destination);
    for (const n of SOUNDS[name]) {
      const osc = ctx.createOscillator();
      const g = ctx.createGain();
      osc.type = n.wave ?? "square";
      osc.frequency.setValueAtTime(n.f, t);
      const end = t + n.ms / 1000;
      if (n.to) osc.frequency.exponentialRampToValueAtTime(n.to, end);
      // Flat level with a tiny release: no click, still sounds like a chip.
      g.gain.setValueAtTime(n.gain ?? 0.18, t);
      g.gain.setValueAtTime(n.gain ?? 0.18, end - 0.008);
      g.gain.linearRampToValueAtTime(0, end);
      osc.connect(g).connect(out);
      osc.start(t);
      osc.stop(end + 0.01);
      t = end;
    }
  } catch {
    /* audio is decoration only */
  }
}

/**
 * Every button click blips, unless the button names its own sound with `data-sfx` (`none` for
 * buttons whose action plays something else, e.g. opening a modal).
 */
export function installClickSounds(): void {
  document.addEventListener(
    "click",
    (e) => {
      const el = (e.target as Element | null)?.closest?.("button, [role='button']");
      if (!el || (el as HTMLButtonElement).disabled) return;
      const name = (el as HTMLElement).dataset.sfx;
      if (name === "none") return;
      play(name !== undefined && name in SOUNDS ? (name as Sfx) : "blip");
    },
    true,
  );
}
