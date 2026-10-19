// Background music: an original lo-fi chiptune loop ("Morning Spin"), synthesised live with Web
// Audio like a handheld console: a pulse-wave lead, a soft pulse arpeggio, a triangle bass, noise
// drums with a lazy swing, everything through a warm low-pass with a little vinyl crackle.
// A per-viewer preference (`localStorage` cyp.music), on by default; browsers only allow audio
// after a tap or a click, so it starts on the first one.

import { audioContext } from "./sfx";

const BPM = 78;
const STEP = 60 / BPM / 4; // one 16th note, seconds
const SWING = 0.2; // late off-beat 16ths: the lo-fi lilt
const BARS = 8;
const KEY = "cyp.music";

/** [midi note | null (rest), length in 16ths] per bar; 16 steps each. Original melody, C major. */
const LEAD: [number | null, number][][] = [
  [[76, 4], [72, 2], [69, 2], [72, 4], [74, 2], [76, 2]], // Fmaj7
  [[79, 6], [76, 2], [74, 4], [71, 4]], // Em7
  [[69, 4], [72, 2], [74, 2], [77, 4], [76, 2], [74, 2]], // Dm7
  [[74, 8], [null, 4], [71, 2], [74, 2]], // G7
  [[76, 4], [79, 2], [81, 2], [79, 4], [76, 4]], // Fmaj7
  [[74, 2], [76, 2], [79, 4], [76, 4], [74, 4]], // Em7
  [[72, 4], [76, 2], [81, 2], [79, 4], [76, 4]], // Am7
  [[77, 4], [76, 2], [74, 2], [71, 4], [67, 4]], // Dm7 · G7
];

/** Chord tones for the arpeggio (low voicing), two chords in the last bar. */
const FMAJ7 = [53, 57, 60, 64];
const EM7 = [52, 55, 59, 62];
const DM7 = [50, 53, 57, 60];
const G7 = [55, 59, 62, 65];
const AM7 = [57, 60, 64, 67];
const CHORDS: number[][][] = [[FMAJ7], [EM7], [DM7], [G7], [FMAJ7], [EM7], [AM7], [DM7, G7]];
const ARP = [0, 1, 2, 3, 2, 1, 2, 3];

const midi = (n: number): number => 440 * 2 ** ((n - 69) / 12);

let enabled = (() => {
  try {
    return localStorage.getItem(KEY) !== "off";
  } catch {
    return true;
  }
})();
const listeners = new Set<() => void>();
let bus: { out: GainNode; pulse: PeriodicWave; soft: PeriodicWave; noise: AudioBuffer } | null = null;
let timer: number | null = null;
let step = 0;
let loop = 0;
let nextTime = 0;
let unlocked = false; // a tap or click happened: the browser lets us play

export const musicOn = (): boolean => enabled;

export function subscribeMusic(cb: () => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

export function setMusic(on: boolean): void {
  enabled = on;
  try {
    localStorage.setItem(KEY, on ? "on" : "off");
  } catch {
    /* private mode */
  }
  listeners.forEach((l) => l());
  if (on) start();
  else stop();
}

/** A pulse wave with the given duty cycle (Fourier series), the classic handheld timbre. */
function pulseWave(ctx: AudioContext, duty: number): PeriodicWave {
  const n = 32;
  const real = new Float32Array(n);
  const imag = new Float32Array(n);
  for (let k = 1; k < n; k++) imag[k] = (2 / (k * Math.PI)) * Math.sin(k * Math.PI * duty);
  return ctx.createPeriodicWave(real, imag);
}

function setup(ctx: AudioContext) {
  if (bus) return bus;
  const out = ctx.createGain();
  out.gain.value = 0;
  const warm = ctx.createBiquadFilter(); // lo-fi: roll off the fizz
  warm.type = "lowpass";
  warm.frequency.value = 2600;
  warm.Q.value = 0.4;
  out.connect(warm).connect(ctx.destination);
  const noise = ctx.createBuffer(1, ctx.sampleRate, ctx.sampleRate);
  const data = noise.getChannelData(0);
  for (let i = 0; i < data.length; i++) data[i] = Math.random() * 2 - 1;
  bus = { out, pulse: pulseWave(ctx, 0.25), soft: pulseWave(ctx, 0.5), noise };
  return bus;
}

function tone(
  ctx: AudioContext,
  dest: AudioNode,
  wave: PeriodicWave | OscillatorType,
  freq: number,
  t: number,
  dur: number,
  level: number,
  vibrato = false,
) {
  const osc = ctx.createOscillator();
  if (wave instanceof PeriodicWave) osc.setPeriodicWave(wave);
  else osc.type = wave;
  osc.frequency.setValueAtTime(freq, t);
  const g = ctx.createGain();
  g.gain.setValueAtTime(0, t);
  g.gain.linearRampToValueAtTime(level, t + 0.008);
  g.gain.linearRampToValueAtTime(level * 0.7, t + Math.min(0.12, dur * 0.5));
  g.gain.setValueAtTime(level * 0.7, t + dur - 0.03);
  g.gain.linearRampToValueAtTime(0, t + dur);
  osc.connect(g).connect(dest);
  if (vibrato && dur > 0.4) {
    // Delayed vibrato on long notes, like the old sound drivers.
    const lfo = ctx.createOscillator();
    const depth = ctx.createGain();
    lfo.frequency.value = 5.5;
    depth.gain.setValueAtTime(0, t);
    depth.gain.linearRampToValueAtTime(12, t + Math.min(0.35, dur));
    lfo.connect(depth).connect(osc.detune);
    lfo.start(t);
    lfo.stop(t + dur);
  }
  osc.start(t);
  osc.stop(t + dur + 0.02);
}

function hit(ctx: AudioContext, dest: AudioNode, noise: AudioBuffer, t: number, kind: "hat" | "snare" | "crackle") {
  const src = ctx.createBufferSource();
  src.buffer = noise;
  const f = ctx.createBiquadFilter();
  const g = ctx.createGain();
  const len = kind === "hat" ? 0.03 : kind === "snare" ? 0.12 : 0.006;
  const level = kind === "hat" ? 0.05 : kind === "snare" ? 0.09 : 0.05;
  f.type = kind === "snare" ? "bandpass" : "highpass";
  f.frequency.value = kind === "hat" ? 7000 : kind === "snare" ? 1800 : 2500;
  g.gain.setValueAtTime(level, t);
  g.gain.exponentialRampToValueAtTime(0.0001, t + len);
  src.connect(f).connect(g).connect(dest);
  src.start(t, Math.random() * 0.8, len + 0.01);
}

function kick(ctx: AudioContext, dest: AudioNode, t: number) {
  const osc = ctx.createOscillator();
  const g = ctx.createGain();
  osc.frequency.setValueAtTime(120, t);
  osc.frequency.exponentialRampToValueAtTime(45, t + 0.12);
  g.gain.setValueAtTime(0.22, t);
  g.gain.exponentialRampToValueAtTime(0.0001, t + 0.16);
  osc.connect(g).connect(dest);
  osc.start(t);
  osc.stop(t + 0.18);
}

/** Schedules everything that starts on one 16th step. */
function playStep(ctx: AudioContext, s: number, t: number) {
  const b = setup(ctx);
  const bar = Math.floor(s / 16);
  const i = s % 16;
  // Lead: every other pass an octave down on the triangle, to breathe.
  let pos = 0;
  for (const [note, len] of LEAD[bar]!) {
    if (pos === i && note !== null) {
      const soft = loop % 2 === 1;
      tone(ctx, b.out, soft ? "triangle" : b.pulse, midi(soft ? note - 12 : note), t, len * STEP * 0.95, soft ? 0.16 : 0.07, true);
    }
    pos += len;
  }
  // Arpeggio on 8ths.
  if (i % 2 === 0) {
    const chords = CHORDS[bar]!;
    const chord = chords.length > 1 && i >= 8 ? chords[1]! : chords[0]!;
    tone(ctx, b.out, b.soft, midi(chord[ARP[i / 2]!]! + 12), t, STEP * 1.6, 0.025);
  }
  // Bass: root, fifth, octave.
  const root = (CHORDS[bar]!.length > 1 && i >= 8 ? CHORDS[bar]![1]! : CHORDS[bar]![0]!)[0]! - 12;
  if (i === 0 || i === 8) tone(ctx, b.out, "triangle", midi(root), t, STEP * 5, 0.22);
  if (i === 6 || i === 14) tone(ctx, b.out, "triangle", midi(root + 7), t, STEP * 1.8, 0.16);
  // Drums: lazy kick, snare on 2 and 4, soft hats on the 8ths.
  if (i === 0 || i === 10) kick(ctx, b.out, t);
  if (i === 4 || i === 12) hit(ctx, b.out, b.noise, t, "snare");
  if (i % 2 === 0) hit(ctx, b.out, b.noise, t + (i % 4 === 2 ? 0.01 : 0), "hat");
  // Vinyl crackle.
  if (Math.random() < 0.18) hit(ctx, b.out, b.noise, t + Math.random() * STEP, "crackle");
}

function tick() {
  const ctx = audioContext();
  if (!ctx) return;
  while (nextTime < ctx.currentTime + 0.2) {
    const swing = step % 2 === 1 ? STEP * SWING : 0;
    playStep(ctx, step, nextTime + swing);
    nextTime += STEP;
    step = (step + 1) % (BARS * 16);
    if (step === 0) loop++;
  }
}

function start() {
  if (!enabled || !unlocked || timer !== null || document.hidden) return;
  const ctx = audioContext();
  if (!ctx) return;
  const b = setup(ctx);
  b.out.gain.cancelScheduledValues(ctx.currentTime);
  b.out.gain.setValueAtTime(b.out.gain.value, ctx.currentTime);
  b.out.gain.linearRampToValueAtTime(0.32, ctx.currentTime + 1.5); // fade in
  nextTime = ctx.currentTime + 0.1;
  timer = window.setInterval(tick, 50);
  tick();
}

function stop() {
  if (timer !== null) window.clearInterval(timer);
  timer = null;
  const ctx = audioContext();
  if (ctx && bus) {
    bus.out.gain.cancelScheduledValues(ctx.currentTime);
    bus.out.gain.setValueAtTime(bus.out.gain.value, ctx.currentTime);
    bus.out.gain.linearRampToValueAtTime(0, ctx.currentTime + 0.4);
  }
}

/** Starts the music on the first tap / click (browser autoplay rule); pauses in hidden tabs. */
export function installMusic(): void {
  const first = () => {
    window.removeEventListener("pointerdown", first, true);
    window.removeEventListener("keydown", first, true);
    unlocked = true;
    start();
  };
  window.addEventListener("pointerdown", first, true);
  window.addEventListener("keydown", first, true);
  document.addEventListener("visibilitychange", () => (document.hidden ? stop() : start()));
}
