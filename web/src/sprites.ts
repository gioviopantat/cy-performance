// Original pixel art for the route strip: a trainer on a bike, eleven roadside critters and the
// ground tiles. Everything here is drawn for this project (no game assets): a map is rows of
// palette letters, "." is transparent; it renders to a tiny SVG data URL (crisp, scalable).

export type Critter = {
  id: string;
  name: string;
  cry: string;
  palette: Record<string, string>;
  map: string[];
  facesLeft?: boolean; // drawn facing the incoming rider; flipped when it follows
  rare?: boolean;
};

const K = "#1a1c2c"; // outline
const W = "#f4f4f4";

export const CRITTERS: Critter[] = [
  {
    id: "sprout",
    name: "葉芽球",
    cry: "啵啵～",
    palette: { k: K, g: "#38b764", G: "#257179", l: "#a7f070", w: W, e: K },
    map: [
      "....kk......",
      "...klk......",
      "....kllk....",
      ".....kk.....",
      "...kkkkkk...",
      "..kggggggk..",
      ".kgggggggGk.",
      ".kgweggweGk.",
      ".kggggggggk.",
      ".kGggggggGk.",
      "..kGGGGGGk..",
      "...kkkkkk...",
    ],
  },
  {
    id: "pup",
    name: "火尾犬",
    cry: "汪嗚！",
    facesLeft: true,
    palette: { k: K, o: "#ef7d57", O: "#b13e53", y: "#ffcd75", c: "#ffe9c5", w: W, e: K },
    map: [
      "............",
      "............",
      ".k.k........",
      "kokok....y..",
      "kooook..yyk.",
      "kweook.yyk..",
      "koooook.yk..",
      ".kccook.k...",
      ".kccooookk..",
      "..kooooook..",
      "..kOk..kOk..",
      "..kk....kk..",
    ],
  },
  {
    id: "drop",
    name: "水滴丸",
    cry: "噗嚕嚕",
    palette: { k: K, b: "#41a6f6", B: "#3b5dc9", w: W, e: K },
    map: [
      ".....kk.....",
      "....kbbk....",
      "...kbbbbk...",
      "..kbwbbbbk..",
      "..kbbbbbbk..",
      ".kbbbbbbbbk.",
      ".kbwebbwebk.",
      ".kbbbbbbbbk.",
      ".kbbbkkbbbk.",
      "..kBbbbbBk..",
      "...kBBBBk...",
      "....kkkk....",
    ],
  },
  {
    id: "zap",
    name: "電毛球",
    cry: "滋滋～",
    palette: { k: K, y: "#ffcd75", Y: "#ef7d57", s: "#73eff7", w: W, e: K },
    map: [
      "......s.....",
      ".....sk.....",
      "......k.....",
      "..kk.kkk.kk.",
      ".kyykyyykyyk",
      ".kyyyyyyyyyk",
      ".kyweyyyweyk",
      ".kyyyyYyyyyk",
      ".kYyyyyyyyYk",
      "..kYyyyyyYk.",
      "...kkkkkkk..",
      "............",
    ],
  },
  {
    id: "bug",
    name: "石殼蟲",
    cry: "喀喀",
    facesLeft: true,
    palette: { k: K, s: "#94b0c2", S: "#566c86", w: W, e: K },
    map: [
      "............",
      "............",
      "....kkkkk...",
      "...kssssSk..",
      "..kssSssSSk.",
      ".kssssssSSk.",
      "kwessssSSSk.",
      "kessssSSSk..",
      ".kkkkkkkkk..",
      ".k.k.k.k.k..",
      "............",
      "............",
    ],
  },
  {
    id: "finch",
    name: "雲朵雀",
    cry: "啾啾！",
    facesLeft: true,
    palette: { k: K, w: W, c: "#73eff7", o: "#ffcd75", e: K },
    map: [
      "............",
      "....kkkk....",
      "...kwwwwk...",
      "..kwewwwwk..",
      ".ookwwwwwk..",
      "..kwwccccwk.",
      "..kwcccccck.",
      "...kwwwwwk..",
      "....kkkkk...",
      ".....k.k....",
      "............",
      "............",
    ],
  },
  {
    id: "ghost",
    name: "夜影團",
    cry: "嘿嘿嘿…",
    palette: { k: K, p: "#b55088", P: "#5d275d", w: W, e: K },
    map: [
      "............",
      "....kkkk....",
      "...kppppk...",
      "..kppppppk..",
      ".kppwppwppk.",
      ".kppeppeppk.",
      ".kppppppppk.",
      ".kpppPPpppk.",
      ".kppppppPPk.",
      ".kPkpPkpPkk.",
      ".k..k..k..k.",
      "............",
    ],
  },
  {
    id: "fly",
    name: "花瓣蝶",
    cry: "飄～",
    palette: { k: K, m: "#f6a2c0", M: "#b55088", e: K },
    map: [
      "............",
      ".kkk....kkk.",
      "kmmmk..kmmmk",
      "kmMmmkkmmMmk",
      "kmmmmekmmmmk",
      ".kmmmkkmmmk.",
      "..kkmkkmkk..",
      "..kmmkkmmk..",
      "..kMmk.kmMk.",
      "...kk...kk..",
      "............",
      "............",
    ],
  },
  {
    id: "shroom",
    name: "菇菇仔",
    cry: "咕咕",
    palette: { k: K, r: "#b13e53", d: W, t: "#ffe9c5", e: K },
    map: [
      "............",
      "...kkkkkk...",
      "..krrdrrrk..",
      ".krrrrrrdrk.",
      ".kdrrrrrrrk.",
      ".kkkkkkkkkk.",
      "..kttttttk..",
      "..ktettetk..",
      "..kttttttk..",
      "...kttttk...",
      "...kk..kk...",
      "............",
    ],
  },
  {
    id: "bunny",
    name: "冰角兔",
    cry: "噗噗！",
    palette: { k: K, i: "#e8f8fc", c: "#73eff7", y: "#ffcd75", w: W, e: K },
    map: [
      "..k....k....",
      ".kck..kck...",
      ".kck..kck...",
      ".kiikkiik...",
      "kiiiiiiiik..",
      "kiweiiweik..",
      "kiiiiyiiik..",
      ".kiicciik...",
      ".kiiiiiik...",
      "..kikkik....",
      "..kk..kk....",
      "............",
    ],
  },
  {
    id: "dragon",
    name: "星光龍",
    cry: "嗷——！✨",
    rare: true,
    facesLeft: true,
    palette: { k: K, a: "#ffcd75", A: "#ef7d57", w: W, e: K },
    map: [
      ".w.....k.k..",
      "......kakak.",
      ".....kaaaaak",
      "....kaeaaaak",
      "..kkaaaaaak.",
      ".kAAkaaaaak.",
      "kAAAkaaaak..",
      ".kAAkaaaak.w",
      "..kkaaaaak..",
      "...kaaAaak..",
      "..kak..kak..",
      "..kk....kk..",
    ],
  },
];

export const CRITTER = Object.fromEntries(CRITTERS.map((c) => [c.id, c])) as Record<string, Critter>;

const cache = new Map<string, string>();

/** A pixel map as an SVG data URL (one rect per pixel, crisp edges). */
export function spriteUrl(key: string, map: string[], palette: Record<string, string>): string {
  const hit = cache.get(key);
  if (hit) return hit;
  const h = map.length;
  const w = Math.max(...map.map((r) => r.length));
  let rects = "";
  map.forEach((row, y) => {
    // One rect per run of equal pixels keeps the ground strip small.
    const cells = [...row];
    for (let x = 0; x < cells.length; ) {
      let n = 1;
      while (cells[x + n] === cells[x]) n++;
      const fill = palette[cells[x]!];
      if (cells[x] !== "." && fill) rects += `<rect x="${x}" y="${y}" width="${n + 0.02}" height="1.02" fill="${fill}"/>`;
      x += n;
    }
  });
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}" shape-rendering="crispEdges">${rects}</svg>`;
  const url = `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
  cache.set(key, url);
  return url;
}

// ------------------------------------------------------------------ the trainer on a bike

const RIDER_PALETTE: Record<string, string> = {
  k: K,
  h: "#b13e53", // cap
  s: "#f4c9a0", // skin
  e: K,
  j: "#3b5dc9", // jacket
  p: "#333c57", // trousers
  b: "#38b764", // bike frame
  g: "#94b0c2", // hubs
};

const RIDER_TOP = [
  "......kkkk......",
  ".....khhhhk.....",
  ".....khhhhhhk...",
  "......kssek.....",
  "......kssk......",
  ".....kjjjjk.....",
  ".....kjjjjssk...",
  "......kjjk......",
];
const WHEELS = [
  "..kkk......kkk..",
  ".k...k....k...k.",
  "k..g..k..k..g..k",
  "k.ggg.k..k.ggg.k",
  "k..g..k..k..g..k",
  ".k...k....k...k.",
  "..kkk......kkk..",
];

function line(grid: string[][], x0: number, y0: number, x1: number, y1: number, ch: string) {
  const dx = Math.abs(x1 - x0);
  const dy = -Math.abs(y1 - y0);
  const sx = x0 < x1 ? 1 : -1;
  const sy = y0 < y1 ? 1 : -1;
  let err = dx + dy;
  for (;;) {
    if (grid[y0]?.[x0] !== undefined) grid[y0]![x0] = ch;
    if (x0 === x1 && y0 === y1) break;
    const e2 = 2 * err;
    if (e2 >= dy) {
      err += dy;
      x0 += sx;
    }
    if (e2 <= dx) {
      err += dx;
      y0 += sy;
    }
  }
}

/** Two pedal frames of the trainer (16 x 16), facing right. */
function riderFrame(frame: 0 | 1): string[] {
  const grid = [...RIDER_TOP, "................", ...WHEELS].map((r) => [...r]);
  // Frame: seat stay, chain stay, seat tube, top tube, down tube, fork, bars.
  line(grid, 6, 8, 3, 12, "b");
  line(grid, 3, 12, 7, 12, "b");
  line(grid, 6, 8, 7, 12, "b");
  line(grid, 6, 8, 11, 8, "b");
  line(grid, 11, 8, 7, 12, "b");
  line(grid, 11, 7, 12, 12, "b");
  grid[7]![12] = "k";
  // Legs from the hip, two crank positions.
  if (frame === 0) {
    line(grid, 6, 7, 9, 10, "p");
    line(grid, 9, 10, 9, 12, "p");
    grid[13]![9] = "k";
  } else {
    line(grid, 6, 7, 8, 9, "p");
    line(grid, 8, 9, 6, 11, "p");
    grid[11]![5] = "k";
  }
  return grid.map((r) => r.join(""));
}

export const RIDER_FRAMES = [0, 1].map((f) => spriteUrl(`rider${f}`, riderFrame(f as 0 | 1), RIDER_PALETTE));

/** The capture capsule (original design: a teal capsule with a star band). */
export const CAPSULE = spriteUrl(
  "capsule",
  ["..kkkk..", ".kcccck.", "kcccccck", "kwwyywwk", "kwwyywwk", "kddddddk", ".kddddk.", "..kkkk.."],
  { k: K, c: "#38b764", w: W, y: "#ffcd75", d: "#257179" },
);

// ------------------------------------------------------------------ ground tiles

/** A seamless 128 x 32 strip of route: trees, grass, a dirt path, flowers and tall grass. */
export function groundUrl(): string {
  const key = "ground";
  const hit = cache.get(key);
  if (hit) return hit;
  const W_ = 128;
  const H = 32;
  const grid: string[][] = Array.from({ length: H }, (_, y) =>
    Array.from({ length: W_ }, (_, x) => {
      if (y >= 14 && y <= 21) return y === 14 || y === 21 ? "D" : (x * 7 + y * 3) % 23 === 0 ? "d" : "t";
      return (x + y) % 2 === 0 && (x * 13 + y * 5) % 9 === 0 ? "G" : "g";
    }),
  );
  let seed = 7;
  const rnd = () => {
    seed = (seed * 16807) % 2147483647;
    return seed / 2147483647;
  };
  // Tree line on top: round canopies with trunks, repeating every 16 px.
  for (let x0 = 0; x0 < W_; x0 += 16) {
    const canopy = ["...kkkkkk...", "..kLLLLLLk..", ".kLLlLLLLLk.", ".kLLLLLLlLk.", ".kLLLLLLLLk.", "..kLLLLLLk..", "...kkNNkk...", ".....NN....."];
    canopy.forEach((row, y) =>
      [...row].forEach((ch, x) => {
        if (ch !== ".") grid[y]![(x0 + x + 2) % W_] = ch;
      }),
    );
  }
  // Tall grass tufts and flowers on the lower verge, a few on the upper one.
  const tuft = [".v.v.", "vVvVv", "VvVvV"];
  for (let i = 0; i < 9; i++) {
    const x0 = Math.floor(rnd() * (W_ - 5));
    const y0 = rnd() < 0.3 ? 10 : 24 + Math.floor(rnd() * 4);
    tuft.forEach((row, y) =>
      [...row].forEach((ch, x) => {
        if (ch !== ".") grid[y0 + y]![x0 + x] = ch;
      }),
    );
  }
  for (let i = 0; i < 14; i++) {
    const x = Math.floor(rnd() * W_);
    const y = rnd() < 0.3 ? 9 + Math.floor(rnd() * 4) : 23 + Math.floor(rnd() * 8);
    grid[y]![x] = rnd() < 0.5 ? "r" : "y";
  }
  const pal: Record<string, string> = {
    g: "#38b764",
    G: "#2f9c55",
    v: "#257179",
    V: "#1f5f66",
    t: "#e3c28a",
    d: "#c8a26a",
    D: "#a7835a",
    L: "#257179",
    l: "#38b764",
    N: "#8a5a3c",
    k: "#1a3a3a",
    r: "#f6a2c0",
    y: "#ffcd75",
  };
  return spriteUrl(key, grid.map((r) => r.join("")), pal);
}
