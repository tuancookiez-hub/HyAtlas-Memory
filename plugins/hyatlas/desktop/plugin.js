import __React from "react";
var __jsxCompat = (t, p, k) => __React.createElement(t, k == null ? p : Object.assign({}, p, { key: k }));
var jsx = __jsxCompat;
var jsxs = __jsxCompat;
var jsxDEV = __jsxCompat;
var jsxDEV2 = __jsxCompat;
var jsxDEV3 = __jsxCompat;
var jsxDEV4 = __jsxCompat;
var jsxDEV5 = __jsxCompat;
var jsxDEV6 = __jsxCompat;
var jsxDEV7 = __jsxCompat;
var jsxDEV8 = __jsxCompat;
var jsxDEV9 = __jsxCompat;
// entry.tsx
import {
  KEYBINDS_AREA,
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  Badge,
  Button as Button2,
  EmptyState,
  ErrorState,
  SegmentedControl,
  SearchField,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  StatusDot,
  Textarea,
  host,
  relativeTime
} from "@hermes/plugin-sdk";
import { useEffect as useEffect4, useState as useState5 } from "react";


// ../../../node_modules/nanostores/clean-stores/index.js
var clean = Symbol("clean");

// ../../../node_modules/nanostores/atom/index.js
var listenerQueue = [];
var lqIndex = 0;
var batchSeen = null;
var QUEUE_ITEMS_PER_LISTENER = 4;
var nanostoresGlobal = globalThis.nanostoresGlobal ||= { epoch: 0 };
var drainQueue = () => {
  for (lqIndex = 0;lqIndex < listenerQueue.length; lqIndex += QUEUE_ITEMS_PER_LISTENER) {
    listenerQueue[lqIndex](listenerQueue[lqIndex + 1].value, listenerQueue[lqIndex + 2], listenerQueue[lqIndex + 3]);
  }
  listenerQueue.length = 0;
};
var atom = (initialValue) => {
  let listeners = [];
  let $atom = {
    get() {
      if (!$atom.lc) {
        $atom.listen(() => {})();
      }
      return $atom.value;
    },
    init: initialValue,
    lc: 0,
    listen(listener) {
      $atom.lc = listeners.push(listener);
      return () => {
        for (let i = lqIndex + QUEUE_ITEMS_PER_LISTENER;i < listenerQueue.length; ) {
          if (listenerQueue[i] === listener) {
            listenerQueue.splice(i, QUEUE_ITEMS_PER_LISTENER);
          } else {
            i += QUEUE_ITEMS_PER_LISTENER;
          }
        }
        let index = listeners.indexOf(listener);
        if (~index) {
          listeners.splice(index, 1);
          if (!--$atom.lc)
            $atom.off();
        }
      };
    },
    notify(oldValue, changedKey) {
      nanostoresGlobal.epoch++;
      let runListenerQueue = !listenerQueue.length && !batchSeen;
      for (let listener of listeners) {
        if (batchSeen?.has(listener))
          continue;
        batchSeen?.add(listener);
        listenerQueue.push(listener, $atom, oldValue, batchSeen ? undefined : changedKey);
      }
      if (runListenerQueue) {
        drainQueue();
      }
    },
    off() {},
    set(newValue) {
      let oldValue = $atom.value;
      if (oldValue !== newValue) {
        $atom.value = newValue;
        $atom.notify(oldValue);
      }
    },
    subscribe(listener) {
      let unbind = $atom.listen(listener);
      listener($atom.value);
      return unbind;
    },
    value: initialValue
  };
  if (true) {
    $atom[clean] = () => {
      listeners = [];
      $atom.lc = 0;
      $atom.off();
    };
  }
  return $atom;
};
// starmap/star-map.tsx
import { useCallback as useCallback2, useEffect as useEffect3, useMemo as useMemo2, useRef as useRef2, useState as useState4 } from "react";

// shim/hooks/use-theme-epoch.ts
import { useEffect, useState } from "react";
var ATTRS = ["class", "style", "data-hermes-mode", "data-hermes-theme"];
var listeners = new Set;
var observer = null;
function onThemeRepaint(fn) {
  if (!observer && typeof document !== "undefined") {
    observer = new MutationObserver(() => listeners.forEach((l) => l()));
    observer.observe(document.documentElement, { attributeFilter: ATTRS, attributes: true });
  }
  listeners.add(fn);
  return () => void listeners.delete(fn);
}
function useThemeEpoch() {
  const [epoch, setEpoch] = useState(0);
  useEffect(() => onThemeRepaint(() => setEpoch((e) => e + 1)), []);
  return epoch;
}

// shim/lib/trackpad-gestures.ts
function createDoubleTapDetector(ms = 350) {
  let last = 0;
  return () => {
    const now = performance.now();
    const dbl = now - last < ms;
    last = now;
    return dbl;
  };
}
function isSmartZoomWheel(_e) {
  return false;
}

// starmap/constants.ts
var RING_INNER = 36;
var RING_OUTER = 340;
var ZOOM_MIN = 0.3;
var ZOOM_MAX = 5;
var FIT_PADDING = 48;
var TILT = 1;
var RING_STEPS = 5;
var WHITE = { b: 255, g: 255, r: 255 };
var BLACK = { b: 0, g: 0, r: 0 };
var AGE_GRADIENT = { mid: 0.52, midInk: 0.74, newInk: 0.95, oldInk: 0.42, reach: 1 };
var NODE_SHAPE = { memory: "diamond", skill: "circle" };
var ORB_DARKEN = 0.3;
var WHITEISH_SHEEN = 0.95;
var LIT_BAND_ALPHA = 0.04;
var MODE_DEFAULTS = {
  dark: {
    lineAlpha: 0.24,
    lineDash: 1.5,
    lineDashed: true,
    lineWidth: 0.5,
    ringAlpha: 0.22,
    ringDash: 4,
    ringDashed: false,
    ringWidth: 2.5
  },
  light: {
    lineAlpha: 0.18,
    lineDash: 1.5,
    lineDashed: true,
    lineWidth: 0.5,
    ringAlpha: 0.1,
    ringDash: 4,
    ringDashed: false,
    ringWidth: 2.5
  }
};
var RING_PARAMS = {
  dark: { bandAlpha: 0.018, lightSize: 0.64, ringAlpha: 0.16, sheen: 0.12 },
  light: { bandAlpha: 0.03, lightSize: 0.27, ringAlpha: 0.07, sheen: 0.1 }
};

// starmap/geometry.ts
function clamp(v, lo, hi) {
  return Math.max(lo, Math.min(hi, v));
}
function hash(input) {
  let h = 2166136261;
  for (let i = 0;i < input.length; i += 1) {
    h ^= input.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}
function nodeRadius(n) {
  if (n.kind === "memory") {
    return 4.4;
  }
  const base = n.state === "archived" || n.state === "stale" ? 2.4 : 3;
  return base + Math.sqrt(Math.max(0, n.useCount)) * 0.55 + (n.pinned ? 0.8 : 0);
}
function recencyInk(rec) {
  const reach = Math.max(0.01, AGE_GRADIENT.reach);
  const mid = clamp(AGE_GRADIENT.mid, 0.01, 0.99);
  const t = clamp(rec / reach, 0, 1);
  if (t <= mid) {
    const p2 = t / mid;
    return AGE_GRADIENT.oldInk + (AGE_GRADIENT.midInk - AGE_GRADIENT.oldInk) * (p2 * p2 * (3 - 2 * p2));
  }
  const p = (t - mid) / (1 - mid);
  return AGE_GRADIENT.midInk + (AGE_GRADIENT.newInk - AGE_GRADIENT.midInk) * (p * p * (3 - 2 * p));
}
function shapePath(ctx, shape, x, y, r) {
  ctx.beginPath();
  if (shape === "square") {
    ctx.rect(x - r, y - r, r * 2, r * 2);
    return;
  }
  if (shape === "circle") {
    ctx.arc(x, y, r, 0, Math.PI * 2);
    return;
  }
  const pts = shape === "diamond" ? 4 : shape === "triangle" ? 3 : 6;
  const rot = shape === "hexagon" ? Math.PI / 6 : -Math.PI / 2;
  for (let i = 0;i < pts; i += 1) {
    const a = rot + i / pts * Math.PI * 2;
    const px = x + Math.cos(a) * r;
    const py = y + Math.sin(a) * r;
    if (i === 0) {
      ctx.moveTo(px, py);
    } else {
      ctx.lineTo(px, py);
    }
  }
  ctx.closePath();
}
function fitViewport(w, h, outer = RING_OUTER) {
  if (w <= 0 || h <= 0) {
    return { k: 1, x: w / 2, y: h / 2 };
  }
  const kFor = (r) => {
    const spanX = (r + 30) * 2;
    return Math.min((w - FIT_PADDING * 2) / spanX, (h - FIT_PADDING * 2) / (spanX * TILT), 2.2);
  };
  const k = clamp(Math.max(kFor(outer), kFor(RING_OUTER)), ZOOM_MIN, ZOOM_MAX);
  return { k, x: w / 2, y: h / 2 + h * 0.05 };
}
function radiusForRecency(rec, outer = RING_OUTER) {
  return RING_INNER + rec * (outer - RING_INNER);
}
var fitScale = (w, h, rings) => fitViewport(w, h, rings.at(-1)?.r ?? RING_OUTER).k;
function distToSegmentSq(px, py, ax, ay, bx, by) {
  const dx = bx - ax;
  const dy = by - ay;
  const len = dx * dx + dy * dy;
  const t = len ? clamp(((px - ax) * dx + (py - ay) * dy) / len, 0, 1) : 0;
  const cx = ax + dx * t;
  const cy = ay + dy * t;
  return (px - cx) ** 2 + (py - cy) ** 2;
}

// starmap/color.ts
var _probe = null;
function resolveRgb(color) {
  if (!_probe) {
    const c = document.createElement("canvas");
    c.width = 1;
    c.height = 1;
    _probe = c.getContext("2d", { willReadFrequently: true });
  }
  if (!_probe) {
    return { b: 184, g: 163, r: 148 };
  }
  _probe.clearRect(0, 0, 1, 1);
  _probe.fillStyle = "#888888";
  _probe.fillStyle = color;
  _probe.fillRect(0, 0, 1, 1);
  const d = _probe.getImageData(0, 0, 1, 1).data;
  return { b: d[2], g: d[1], r: d[0] };
}
function rgba(c, a) {
  return `rgba(${c.r},${c.g},${c.b},${a})`;
}
function mixRgb(a, b, t) {
  const p = clamp(t, 0, 1);
  return {
    b: Math.round(a.b + (b.b - a.b) * p),
    g: Math.round(a.g + (b.g - a.g) * p),
    r: Math.round(a.r + (b.r - a.r) * p)
  };
}
function darken(c, amount) {
  return mixRgb(c, BLACK, amount);
}
function luminance(r, g, b) {
  return (0.2126 * r + 0.7152 * g + 0.114 * b) / 255;
}
function rgbToHsl(c) {
  const r = c.r / 255;
  const g = c.g / 255;
  const b = c.b / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const l = (max + min) / 2;
  const d = max - min;
  let h = 0;
  let s = 0;
  if (d) {
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    h = (max === r ? (g - b) / d + (g < b ? 6 : 0) : max === g ? (b - r) / d + 2 : (r - g) / d + 4) * 60;
  }
  return [h, s, l];
}
function hslToRgb(h, s, l) {
  const hue = (h % 360 + 360) % 360;
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs(hue / 60 % 2 - 1));
  const m = l - c / 2;
  const [r, g, b] = hue < 60 ? [c, x, 0] : hue < 120 ? [x, c, 0] : hue < 180 ? [0, c, x] : hue < 240 ? [0, x, c] : hue < 300 ? [x, 0, c] : [c, 0, x];
  return { b: Math.round((b + m) * 255), g: Math.round((g + m) * 255), r: Math.round((r + m) * 255) };
}
function complementaryInk(c) {
  const [h, s, l] = rgbToHsl(c);
  return hslToRgb(h + 165, Math.max(s, 0.5), clamp(l, 0.5, 0.7));
}
function memoryInkFor(primary, bg) {
  return mixRgb(complementaryInk(primary), bg, 0.45);
}
function computePalette(canvas) {
  const style = getComputedStyle(canvas);
  const fg = resolveRgb(style.color);
  const darkTheme = luminance(fg.r, fg.g, fg.b) > 0.55;
  const base = darkTheme ? { b: 255, g: 255, r: 255 } : { b: 0, g: 0, r: 0 };
  const primary = resolveRgb(style.getPropertyValue("--theme-primary").trim() || style.color);
  const bg = resolveRgb(style.getPropertyValue("--background").trim() || style.getPropertyValue("--dt-background").trim() || (darkTheme ? "#000" : "#fff"));
  return {
    bandInk: mixRgb(primary, base, darkTheme ? 0.3 : 0),
    base,
    bg,
    c: MODE_DEFAULTS[darkTheme ? "dark" : "light"],
    chipBg: darkTheme ? "rgba(0,0,0,0.72)" : "rgba(255,255,255,0.85)",
    darkTheme,
    inkInv: darkTheme ? "rgba(0,0,0,1)" : "rgba(255,255,255,1)",
    memoryInk: memoryInkFor(primary, bg),
    primary,
    skillInk: mixRgb(primary, base, darkTheme ? 0.12 : 0.18)
  };
}

// starmap/layers.ts
var hex = (h) => ({
  b: parseInt(h.slice(5, 7), 16),
  g: parseInt(h.slice(3, 5), 16),
  r: parseInt(h.slice(1, 3), 16)
});
var HYATLAS_LAYERS = [
  ["l1_profile", { color: "#60a5fa", label: "L1 Profile", rgb: hex("#60a5fa"), shape: "square" }],
  ["l2_raw", { color: "#f97316", label: "L2 Raw", rgb: hex("#f97316"), shape: "triangle" }],
  ["l3_fact", { color: "#a78bfa", label: "L3 Fact", rgb: hex("#a78bfa"), shape: "circle" }],
  ["l4_summary", { color: "#f472b6", label: "L4 Summary", rgb: hex("#f472b6"), shape: "diamond" }],
  ["l5_knowledge", { color: "#34d399", label: "L5 Knowledge", rgb: hex("#34d399"), shape: "hexagon" }],
  ["l6_schema", { color: "#facc15", label: "L6 Schema", rgb: hex("#facc15"), shape: "square" }],
  ["l7_intention", { color: "#f87171", label: "L7 Intention", rgb: hex("#f87171"), shape: "triangle" }]
];
var byKey = new Map(HYATLAS_LAYERS);
function layerOf(n) {
  return byKey.get(String(n.category ?? "")) ?? null;
}
function layerIndexOf(n) {
  const key = String(n.category ?? "");
  return HYATLAS_LAYERS.findIndex(([k]) => k === key);
}

// starmap/node-context-menu.tsx
function NodeContextMenu() {
  return null;
}

// shim/lib/time.ts
var fmtDate = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" });

// starmap/text.ts
function formatDate(ts) {
  if (!ts) {
    return "unknown";
  }
  try {
    return fmtDate.format(new Date(ts * 1000));
  } catch {
    return "unknown";
  }
}
function metaBadges(n) {
  const out = [formatDate(n.timestamp)];
  if (n.kind === "memory") {
    out.push(layerOf(n)?.label ?? "memory");
  } else {
    out.push(n.category);
    if (n.createdBy === "agent") {
      out.push("learned");
    }
    if (n.pinned) {
      out.push("pinned");
    }
  }
  return out.filter(Boolean);
}
function countLabel(n) {
  return n.kind === "skill" && n.useCount > 0 ? `x${n.useCount}` : null;
}
function nodeFooter(node) {
  return null;
}
function wrapText(ctx, text, maxW) {
  const words = text.split(/\s+/).filter(Boolean);
  const lines = [];
  let line = "";
  for (const word of words) {
    const next = line ? `${line} ${word}` : word;
    if (!line || ctx.measureText(next).width <= maxW) {
      line = next;
    } else {
      lines.push(line);
      line = word;
    }
  }
  if (line) {
    lines.push(line);
  }
  return lines;
}
function ellipsize(ctx, text, maxW) {
  if (ctx.measureText(text).width <= maxW) {
    return text;
  }
  let s = text;
  while (s.length > 1 && ctx.measureText(`${s}…`).width > maxW) {
    s = s.slice(0, -1);
  }
  return `${s.trimEnd()}…`;
}

// starmap/render.ts
var ease = (t) => {
  const u = t < 0 ? 0 : t > 1 ? 1 : t;
  return u * u * (3 - 2 * u);
};
var WARP_FROM = 0.32;
var warpIn = (t) => {
  const u = t < 0 ? 0 : t > 1 ? 1 : t;
  return u >= 1 ? 1 : 1 - 2 ** (-9 * u);
};
var RING_BIRTH = { down: 0.055, up: 0.032 };
var NODE_BIRTH = { down: 0.11, up: 0.075 };
var SCRAMBLE_CHARS = "ﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾜﾝｦｱｳｴｵｶｷｹｺｻｼｽｾﾀﾁﾂﾃﾅﾆﾇﾈ0123456789:.=*+<>Ξ╳";
var SPRITE_R = 96;
var spriteCache = new Map;
function sphereSprite(ink, strength, bodyDarken) {
  const key = `${ink.r},${ink.g},${ink.b}|${strength}|${bodyDarken}`;
  const cached = spriteCache.get(key);
  if (cached) {
    return cached;
  }
  const R = SPRITE_R;
  const pad = Math.ceil(R * 0.15) + 1;
  const size = (R + pad) * 2;
  const c = R + pad;
  const cv = document.createElement("canvas");
  cv.width = size;
  cv.height = size;
  const g2 = cv.getContext("2d");
  if (!g2) {
    return cv;
  }
  const mx = Math.max(ink.r, ink.g, ink.b);
  const mn = Math.min(ink.r, ink.g, ink.b);
  const sat = mx ? (mx - mn) / mx : 0;
  const whiteness = clamp((luminance(ink.r, ink.g, ink.b) - 0.7) / 0.3, 0, 1) * (1 - sat);
  const eff = strength + (WHITEISH_SHEEN - strength) * whiteness;
  const hi = mixRgb(ink, WHITE, 0.7 * eff);
  const body = darken(ink, bodyDarken * (1 - whiteness));
  const grad = g2.createRadialGradient(c - R * 0.35, c - R * 0.4, R * 0.05, c, c, R * 1.15);
  grad.addColorStop(0, rgba(hi, 1));
  grad.addColorStop(0.5, rgba(body, 1));
  grad.addColorStop(1, rgba(body, 0.85));
  g2.fillStyle = grad;
  g2.beginPath();
  g2.arc(c, c, R, 0, Math.PI * 2);
  g2.fill();
  spriteCache.set(key, cv);
  return cv;
}
function sphereFill(ctx, x, y, r, ink, strength, bodyDarken) {
  const sprite = sphereSprite(ink, strength, bodyDarken);
  const scale = r / SPRITE_R;
  const drawSize = sprite.width * scale;
  ctx.drawImage(sprite, x - drawSize / 2, y - drawSize / 2, drawSize, drawSize);
}
var rectsOverlap = (a, b) => a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
function drawScene(scene) {
  const {
    adjacency,
    byId,
    ctx,
    dpr,
    fades,
    focusId,
    hoverId,
    hoverLink,
    hoverRing,
    links,
    memById,
    nodes,
    palette,
    reveal,
    rings,
    selectedRing,
    size,
    snapMotion = false,
    vp
  } = scene;
  const seen = (rec) => rec <= reveal + 0.001;
  let frontier = 0;
  for (const fn of nodes) {
    if (fn.rec <= reveal + 0.001 && fn.rec > frontier) {
      frontier = fn.rec;
    }
  }
  const erec = (rec) => frontier > 0 ? clamp(rec / frontier, 0, 1) : 1;
  const { h, w } = size;
  const { bandInk, base, bg, c, chipBg, darkTheme, inkInv, memoryInk, skillInk } = palette;
  const { bandAlpha, lightSize, ringAlpha, sheen } = RING_PARAMS[darkTheme ? "dark" : "light"];
  let animating = false;
  const ringLabelRects = [];
  const fadeAlpha = (bucket, key, target, snapUp = false, rates) => {
    const targetAlpha = clamp(target, 0, 1);
    const prev = bucket.get(key);
    if (snapMotion) {
      bucket.set(key, targetAlpha);
      return targetAlpha;
    }
    if (prev == null || snapUp && targetAlpha > prev) {
      bucket.set(key, targetAlpha);
      return targetAlpha;
    }
    const up = rates?.up ?? 0.22;
    const down = rates?.down ?? 0.32;
    const rate = targetAlpha > prev ? up : down;
    const next = prev + (targetAlpha - prev) * rate;
    if (Math.abs(next - targetAlpha) < 0.01) {
      bucket.set(key, targetAlpha);
      return targetAlpha;
    }
    animating = true;
    bucket.set(key, next);
    return next;
  };
  const shade = (a) => `rgba(${base.r},${base.g},${base.b},${a})`;
  const projX = (wx) => wx * vp.k + vp.x;
  const projY = (wy) => wy * vp.k * TILT + vp.y;
  const nodeK = fitScale(w, h, rings);
  const focusSet = focusId ? adjacency.get(focusId) ?? new Set : null;
  const ringIdx = selectedRing;
  const ring = ringIdx != null ? rings[ringIdx] ?? null : null;
  const ringLo = ring && ringIdx != null ? (rings[ringIdx - 1]?.ratio ?? 0) - 0.001 : 0;
  const ringHi = ring ? ring.ratio + 0.001 : 1;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  ctx.globalAlpha = 1;
  ctx.setTransform(vp.k * dpr, 0, 0, vp.k * TILT * dpr, vp.x * dpr, vp.y * dpr);
  const litRingIdx = hoverRing ?? ringIdx;
  const ringSeen = (i) => {
    const threshold = rings[i - 1]?.ratio ?? 0;
    return i === 0 || (threshold <= 0 ? reveal > 0.001 : reveal + 0.001 >= threshold);
  };
  const ringAppear = rings.map((rg, i) => ease(fadeAlpha(fades.appear, `ring:${i}`, ringSeen(i) ? 1 : 0, false, RING_BIRTH)));
  const ringDrawR = rings.map((rg, i) => {
    const startR = ringSeen(i) ? rings[i - 1]?.r ?? rg.r : RING_INNER;
    return startR + (rg.r - startR) * (ringAppear[i] ?? 1);
  });
  const ringVis = ringAppear.map((a) => clamp(a / 0.55, 0, 1));
  if (bandAlpha > 0 || litRingIdx != null) {
    for (let i = 0;i < rings.length - 1; i += 1) {
      const lit = litRingIdx != null && i + 1 === litRingIdx;
      if (!lit && bandAlpha <= 0) {
        continue;
      }
      if ((ringAppear[i + 1] ?? 1) <= 0.01) {
        continue;
      }
      const inner = ringDrawR[i] ?? 0;
      const outer = ringDrawR[i + 1] ?? 0;
      if (lit) {
        ctx.fillStyle = rgba(bandInk, LIT_BAND_ALPHA);
      } else {
        const grad = ctx.createRadialGradient(0, 0, inner, 0, 0, outer);
        if (darkTheme) {
          grad.addColorStop(0, rgba(bandInk, 0));
          grad.addColorStop(clamp(1 - lightSize, 0.01, 0.99), rgba(bandInk, 0));
          grad.addColorStop(1, rgba(bandInk, bandAlpha));
        } else {
          grad.addColorStop(0, rgba(bandInk, bandAlpha));
          grad.addColorStop(clamp(lightSize, 0.01, 0.99), rgba(bandInk, 0));
          grad.addColorStop(1, rgba(bandInk, 0));
        }
        ctx.fillStyle = grad;
      }
      ctx.beginPath();
      ctx.arc(0, 0, outer, 0, Math.PI * 2);
      ctx.arc(0, 0, inner, 0, Math.PI * 2, true);
      ctx.fill();
    }
  }
  ctx.lineWidth = c.ringWidth / vp.k;
  ctx.setLineDash(c.ringDashed ? [c.ringDash / vp.k, c.ringDash / vp.k] : []);
  rings.forEach((rg, i) => {
    const emphasized = ringIdx != null && (i === ringIdx || i === ringIdx - 1);
    const emphasisAlpha = emphasized ? clamp(LIT_BAND_ALPHA * 2, 0, 1) : ringAlpha;
    const coreFade = i === 0 ? clamp(reveal / 0.08, 0, 1) : 1;
    const ringAlphaNow = fadeAlpha(fades.rings, String(i), emphasisAlpha, emphasized) * (ringVis[i] ?? 1) * coreFade;
    if (ringAlphaNow < 0.004) {
      return;
    }
    ctx.strokeStyle = shade(ringAlphaNow);
    ctx.beginPath();
    ctx.arc(0, 0, ringDrawR[i] ?? rg.r, 0, Math.PI * 2);
    ctx.stroke();
  });
  ctx.setLineDash([]);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const focusNode = focusId ? byId.get(focusId) ?? null : null;
  const focusRingR = focusNode ? nodeRadius(focusNode) * nodeK + 4 : 0;
  for (const link of links) {
    const s = typeof link.source === "object" ? link.source : byId.get(String(link.source));
    const t = typeof link.target === "object" ? link.target : byId.get(String(link.target));
    if (!s || !t) {
      continue;
    }
    const revealed = seen(s.rec) && seen(t.rec);
    const lit = revealed && !!focusId && (s.id === focusId || t.id === focusId || !!focusSet && focusSet.has(s.id) && focusSet.has(t.id));
    let x1 = projX(s.x);
    let y1 = projY(s.y);
    let x2 = projX(t.x);
    let y2 = projY(t.y);
    if (s.id === focusId) {
      const d = Math.hypot(x2 - x1, y2 - y1) || 1;
      x1 += (x2 - x1) / d * focusRingR;
      y1 += (y2 - y1) / d * focusRingR;
    }
    if (t.id === focusId) {
      const d = Math.hypot(x1 - x2, y1 - y2) || 1;
      x2 += (x1 - x2) / d * focusRingR;
      y2 += (y1 - y2) / d * focusRingR;
    }
    const key = `${s.id}->${t.id}`;
    const ambient = recencyInk(erec((s.rec + t.rec) / 2)) * c.lineAlpha;
    const targetAlpha = !revealed ? 0 : lit ? 1 : key === hoverLink ? clamp(ambient * 2, 0, 0.7) : focusId || ring ? 0.025 : ambient;
    const linkAlpha = fadeAlpha(fades.links, key, targetAlpha, lit);
    if (linkAlpha < 0.004) {
      continue;
    }
    ctx.strokeStyle = shade(linkAlpha);
    ctx.setLineDash(lit || !c.lineDashed ? [] : [c.lineDash, c.lineDash]);
    ctx.lineWidth = lit ? 1.5 : c.lineWidth;
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
  }
  ctx.setLineDash([]);
  const revealedRings = new Set;
  for (const n of nodes) {
    const landLaid = (ringAppear[n.outerRingIndex] ?? 1) >= 0.5;
    const revealed = seen(n.rec) && landLaid;
    if (revealed) {
      revealedRings.add(n.outerRingIndex);
    }
    const isFocus = revealed && n.id === focusId;
    const isNeighbor = revealed && !!focusSet && focusSet.has(n.id);
    const inRing = !!ring && n.rec >= ringLo && n.rec < ringHi;
    const nodeHigh = isFocus || isNeighbor;
    const er = erec(n.rec);
    const ageScale = nodeHigh || inRing ? 1 : 0.34 + Math.min(1, er / 0.4) * 0.66;
    const r = nodeRadius(n) * nodeK * ageScale;
    const baseAlpha = nodeHigh ? 1 : ring ? inRing ? focusId ? 0.55 : 1 : 0.16 : focusId ? 0.16 : recencyInk(er);
    const alpha = fadeAlpha(fades.nodes, n.id, revealed ? baseAlpha : 0, nodeHigh || inRing);
    const rawBorn = fadeAlpha(fades.appear, n.id, revealed ? 1 : 0, nodeHigh || inRing, NODE_BIRTH);
    const born = ease(rawBorn);
    const vis = alpha * born;
    if (vis < 0.004) {
      continue;
    }
    const posScale = WARP_FROM + (1 - WARP_FROM) * warpIn(rawBorn);
    const sx = projX(n.x * posScale);
    const sy = projY(n.y * posScale);
    ctx.globalAlpha = vis;
    const layer = layerOf(n);
    const nodeInk = nodeHigh ? base : layer ? layer.rgb : n.kind === "memory" ? memoryInk : skillInk;
    const shape = layer ? layer.shape : NODE_SHAPE[n.kind];
    if (shape === "circle") {
      sphereFill(ctx, sx, sy, r, nodeInk, sheen, nodeHigh ? 0 : ORB_DARKEN);
    } else {
      shapePath(ctx, shape, sx, sy, r);
      ctx.fillStyle = rgba(nodeInk, 1);
      ctx.fill();
    }
    if (isFocus) {
      ctx.globalAlpha = 1;
      ctx.strokeStyle = rgba(nodeInk, 1);
      ctx.lineWidth = 1.4;
      shapePath(ctx, shape, sx, sy, r + 4);
      ctx.stroke();
    }
  }
  ctx.globalAlpha = 1;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.font = "600 13px ui-sans-serif, system-ui, sans-serif";
  ctx.textAlign = "center";
  const LABEL_GAP = 22;
  let lastLabelY = Number.POSITIVE_INFINITY;
  rings.forEach((rg, i) => {
    if (!rg.label || !revealedRings.has(i)) {
      return;
    }
    const sx = projX(0);
    const sy = projY(-(ringDrawR[i] ?? rg.r));
    if (sy < 8 || sy > h - 8 || lastLabelY - sy < LABEL_GAP) {
      return;
    }
    lastLabelY = sy;
    const tw = ctx.measureText(rg.label).width;
    const boxW = tw + 12;
    const isThis = ringIdx === i || hoverRing === i;
    const faded = (focusId != null || ringIdx != null) && !isThis;
    const emphasisAlpha = faded ? 0.33 : 1;
    const labelAlpha = fadeAlpha(fades.labels, String(i), emphasisAlpha, isThis) * (ringVis[i] ?? 1);
    if (labelAlpha < 0.01) {
      return;
    }
    ctx.globalAlpha = labelAlpha;
    ctx.fillStyle = rgba(bg, 1);
    ctx.fillRect(sx - boxW / 2, sy - 8, boxW, 17);
    ctx.fillStyle = shade(isThis ? 1 : 0.62);
    ctx.fillText(rg.label, sx, sy + 4);
    ctx.globalAlpha = 1;
    ringLabelRects.push({ h: 20, i, w: boxW + 6, x: sx - boxW / 2 - 3, y: sy - 10 });
  });
  const tipNode = focusId ? byId.get(focusId) : null;
  const tip = tipNode && seen(tipNode.rec) ? tipNode : null;
  let tipRect = null;
  if (tip) {
    const PADX = 6;
    const PADY = 4;
    const BADGE_H = 14;
    const ROW_GAP = 3;
    const LINE_H = 16;
    const ITEM_GAP = 8;
    const badgeFont = "9px ui-sans-serif, system-ui, sans-serif";
    const monoFont = "9px ui-monospace, SFMono-Regular, Menlo, monospace";
    const titleFont = "600 11px ui-sans-serif, system-ui, sans-serif";
    const footerFont = "9px ui-sans-serif, system-ui, sans-serif";
    const FOOTER_H = 13;
    const badgeFontFor = (i) => i === 0 ? badgeFont : monoFont;
    const badges = metaBadges(tip);
    const use = countLabel(tip);
    const titleText = tip.kind === "memory" ? memById.get(tip.id)?.body.split(`
`)[0]?.trim() || tip.label : tip.label;
    const badgeW = badges.map((b, i) => {
      ctx.font = badgeFontFor(i);
      return ctx.measureText(b).width;
    });
    const rowW = badgeW.reduce((a, b) => a + b, 0) + ITEM_GAP * Math.max(0, badges.length - 1);
    ctx.font = monoFont;
    const useW = use ? ctx.measureText(use).width : 0;
    const metaW = rowW + (use ? ITEM_GAP + useW : 0);
    ctx.font = titleFont;
    const maxTitleW = Math.min(380, w - 16) - PADX * 2;
    const titleLines = wrapText(ctx, titleText, maxTitleW);
    const titleW = Math.max(0, ...titleLines.map((l) => ctx.measureText(l).width));
    const titleBgW = titleW + PADX * 2;
    const titleBgH = titleLines.length * LINE_H + PADY * 2;
    const footerText = nodeFooter(tip);
    ctx.font = footerFont;
    const footerW = footerText ? ctx.measureText(footerText).width : 0;
    const totalW = Math.max(metaW, footerW, titleBgW);
    const totalH = BADGE_H + ROW_GAP + titleBgH + (footerText ? ROW_GAP + FOOTER_H : 0);
    const bx = clamp(projX(tip.x) - totalW / 2, 4, Math.max(4, w - totalW - 4));
    const by = clamp(projY(tip.y) - (nodeRadius(tip) * nodeK + 8) - totalH, 4, Math.max(4, h - totalH - 4));
    tipRect = { h: totalH, w: totalW, x: bx, y: by };
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    const badgeMidY = by + BADGE_H / 2;
    ctx.fillStyle = shade(0.7);
    let cx = bx;
    badges.forEach((label, i) => {
      ctx.font = badgeFontFor(i);
      ctx.fillText(label, cx, badgeMidY);
      cx += badgeW[i] + ITEM_GAP;
    });
    if (use) {
      ctx.font = monoFont;
      ctx.fillStyle = shade(0.5);
      ctx.fillText(use, cx, badgeMidY);
    }
    const ty = by + BADGE_H + ROW_GAP;
    ctx.fillStyle = shade(1);
    ctx.fillRect(bx, ty, titleBgW, titleBgH);
    ctx.font = titleFont;
    ctx.fillStyle = inkInv;
    titleLines.forEach((line, i) => {
      ctx.fillText(line, bx + PADX, ty + PADY + LINE_H * i + LINE_H / 2);
    });
    if (footerText) {
      ctx.font = footerFont;
      ctx.fillStyle = shade(0.45);
      ctx.fillText(footerText, bx, ty + titleBgH + ROW_GAP + FOOTER_H / 2);
    }
    ctx.textBaseline = "alphabetic";
  }
  ctx.font = "11px ui-sans-serif, system-ui, sans-serif";
  ctx.textAlign = "center";
  const LBL_M = 6;
  const LBL_H = 15;
  const placed = ringLabelRects.map((r) => ({ h: r.h, w: r.w, x: r.x, y: r.y }));
  if (tipRect) {
    placed.push(tipRect);
  }
  for (const id of focusSet ?? []) {
    if (id === hoverId) {
      continue;
    }
    const n = byId.get(id);
    if (!n || !seen(n.rec)) {
      continue;
    }
    const label = ellipsize(ctx, n.label, Math.min(180, w * 0.32));
    const bw = ctx.measureText(label).width + 8;
    const x = clamp(projX(n.x) - bw / 2, LBL_M, Math.max(LBL_M, w - bw - LBL_M));
    const top = projY(n.y) - (nodeRadius(n) * nodeK + 7) - LBL_H + 4;
    const clampY = (v) => clamp(v, LBL_M, Math.max(LBL_M, h - LBL_H - LBL_M));
    const step = LBL_H + 3;
    let y = null;
    for (let k = 0;k <= 7 && y == null; k += 1) {
      for (const dy of k === 0 ? [0] : [-k * step, k * step]) {
        const cand = { h: LBL_H, w: bw, x, y: clampY(top + dy) };
        if (!placed.some((p) => rectsOverlap(cand, p))) {
          y = cand.y;
          break;
        }
      }
    }
    if (y == null) {
      continue;
    }
    placed.push({ h: LBL_H, w: bw, x, y });
    ctx.fillStyle = chipBg;
    ctx.fillRect(x, y, bw, LBL_H);
    ctx.fillStyle = shade(0.85);
    ctx.fillText(label, x + bw / 2, y + 11);
  }
  return { animating, ringLabelRects };
}
var SCRAMBLE_RADIUS = 6;
var SCRAMBLE_CELL_MIN = 5;
var SCRAMBLE_CELL_MAX = 13;
function drawScramble({
  ctx,
  dpr,
  palette,
  rings,
  vp
}) {
  const { bg, darkTheme, primary } = palette;
  const projX = (wx) => wx * vp.k + vp.x;
  const projY = (wy) => wy * vp.k * TILT + vp.y;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const coreX = projX(0);
  const coreY = projY(0);
  const coreRx = (rings[0]?.r ?? RING_INNER) * vp.k * 1.25;
  if (coreRx <= 0) {
    return;
  }
  const washR = coreRx * 1.15;
  ctx.save();
  ctx.translate(coreX, coreY);
  ctx.scale(1, TILT);
  const wash = ctx.createRadialGradient(0, 0, 0, 0, 0, washR);
  wash.addColorStop(0, rgba(bg, darkTheme ? 0.9 : 0.93));
  wash.addColorStop(0.62, rgba(bg, darkTheme ? 0.84 : 0.88));
  wash.addColorStop(1, rgba(bg, 0));
  ctx.fillStyle = wash;
  ctx.beginPath();
  ctx.arc(0, 0, washR, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();
  const cell = clamp(coreRx / SCRAMBLE_RADIUS, SCRAMBLE_CELL_MIN, SCRAMBLE_CELL_MAX);
  const coreRy = coreRx * TILT;
  const half = Math.max(3, Math.round(coreRx / cell));
  const now = performance.now();
  const t = now / 1000;
  ctx.save();
  ctx.font = `${cell}px "JetBrains Mono", "Hiragino Sans", "Noto Sans JP", ui-monospace, monospace`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  for (let r = -half;r <= half; r += 1) {
    const rowSeed = r * 19349663 >>> 0 || 1;
    const dir = rowSeed & 1 ? 1 : -1;
    const speed = 8 + rowSeed % 16;
    const scroll = now / 1000 * speed * dir;
    const ny = r * cell / coreRy;
    const rowDim = 1 - 0.5 * Math.min(1, Math.abs(ny));
    const kMin = Math.floor((-coreRx - scroll) / cell) - 1;
    const kMax = Math.ceil((coreRx - scroll) / cell) + 1;
    for (let k = kMin;k <= kMax; k += 1) {
      const sx = k * cell + scroll;
      const nx = sx / coreRx;
      const d2 = nx * nx + ny * ny;
      if (d2 > 1) {
        continue;
      }
      const seed = (rowSeed ^ (k >>> 0) * 73856093) >>> 0;
      const ch = SCRAMBLE_CHARS[seed % SCRAMBLE_CHARS.length] ?? "0";
      const edge = clamp((1 - Math.sqrt(d2)) / 0.4, 0, 1);
      const flick = 0.7 + 0.3 * ((seed >>> 5) % 100 / 100);
      const phase = (seed & 7) * 0.35;
      const glow = Math.sin(nx * 4.5 + t * 1.3 + phase) * Math.sin(ny * 4.5 - t * 0.9 + phase);
      const pop = 1 + clamp((glow - 0.25) / 0.75, 0, 1) * 2.6;
      const a = clamp((darkTheme ? 0.22 : 0.3) * edge * flick * rowDim * pop, 0, 0.9);
      if (a < 0.02) {
        continue;
      }
      ctx.fillStyle = rgba(primary, a);
      ctx.fillText(ch, coreX + sx, coreY + r * cell);
    }
  }
  ctx.restore();
  ctx.globalAlpha = 1;
}

// ../../../node_modules/fflate/esm/browser.js
var u8 = Uint8Array;
var u16 = Uint16Array;
var i32 = Int32Array;
var fleb = new u8([0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0, 0, 0, 0]);
var fdeb = new u8([0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13, 0, 0]);
var clim = new u8([16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15]);
var freb = function(eb, start) {
  var b = new u16(31);
  for (var i = 0;i < 31; ++i) {
    b[i] = start += 1 << eb[i - 1];
  }
  var r = new i32(b[30]);
  for (var i = 1;i < 30; ++i) {
    for (var j = b[i];j < b[i + 1]; ++j) {
      r[j] = j - b[i] << 5 | i;
    }
  }
  return { b, r };
};
var _a = freb(fleb, 2);
var fl = _a.b;
var revfl = _a.r;
fl[28] = 258, revfl[258] = 28;
var _b = freb(fdeb, 0);
var fd = _b.b;
var revfd = _b.r;
var rev = new u16(32768);
for (i = 0;i < 32768; ++i) {
  x = (i & 43690) >> 1 | (i & 21845) << 1;
  x = (x & 52428) >> 2 | (x & 13107) << 2;
  x = (x & 61680) >> 4 | (x & 3855) << 4;
  rev[i] = ((x & 65280) >> 8 | (x & 255) << 8) >> 1;
}
var x;
var i;
var hMap = function(cd, mb, r) {
  var s = cd.length;
  var i2 = 0;
  var l = new u16(mb);
  for (;i2 < s; ++i2) {
    if (cd[i2])
      ++l[cd[i2] - 1];
  }
  var le = new u16(mb);
  for (i2 = 1;i2 < mb; ++i2) {
    le[i2] = le[i2 - 1] + l[i2 - 1] << 1;
  }
  var co;
  if (r) {
    co = new u16(1 << mb);
    var rvb = 15 - mb;
    for (i2 = 0;i2 < s; ++i2) {
      if (cd[i2]) {
        var sv = i2 << 4 | cd[i2];
        var r_1 = mb - cd[i2];
        var v = le[cd[i2] - 1]++ << r_1;
        for (var m = v | (1 << r_1) - 1;v <= m; ++v) {
          co[rev[v] >> rvb] = sv;
        }
      }
    }
  } else {
    co = new u16(s);
    for (i2 = 0;i2 < s; ++i2) {
      if (cd[i2]) {
        co[i2] = rev[le[cd[i2] - 1]++] >> 15 - cd[i2];
      }
    }
  }
  return co;
};
var flt = new u8(288);
for (i = 0;i < 144; ++i)
  flt[i] = 8;
var i;
for (i = 144;i < 256; ++i)
  flt[i] = 9;
var i;
for (i = 256;i < 280; ++i)
  flt[i] = 7;
var i;
for (i = 280;i < 288; ++i)
  flt[i] = 8;
var i;
var fdt = new u8(32);
for (i = 0;i < 32; ++i)
  fdt[i] = 5;
var i;
var flm = /* @__PURE__ */ hMap(flt, 9, 0);
var flrm = /* @__PURE__ */ hMap(flt, 9, 1);
var fdm = /* @__PURE__ */ hMap(fdt, 5, 0);
var fdrm = /* @__PURE__ */ hMap(fdt, 5, 1);
var max = function(a) {
  var m = a[0];
  for (var i2 = 1;i2 < a.length; ++i2) {
    if (a[i2] > m)
      m = a[i2];
  }
  return m;
};
var bits = function(d, p, m) {
  var o = p / 8 | 0;
  return (d[o] | d[o + 1] << 8) >> (p & 7) & m;
};
var bits16 = function(d, p) {
  var o = p / 8 | 0;
  return (d[o] | d[o + 1] << 8 | d[o + 2] << 16) >> (p & 7);
};
var shft = function(p) {
  return (p + 7) / 8 | 0;
};
var slc = function(v, s, e) {
  if (s == null || s < 0)
    s = 0;
  if (e == null || e > v.length)
    e = v.length;
  return new u8(v.subarray(s, e));
};
var ec = [
  "unexpected EOF",
  "invalid block type",
  "invalid length/literal",
  "invalid distance",
  "stream finished",
  "no stream handler",
  ,
  "no callback",
  "invalid UTF-8 data",
  "extra field too long",
  "date not in range 1980-2099",
  "filename too long",
  "stream finishing",
  "invalid zip data"
];
var err = function(ind, msg, nt) {
  var e = new Error(msg || ec[ind]);
  e.code = ind;
  if (Error.captureStackTrace)
    Error.captureStackTrace(e, err);
  if (!nt)
    throw e;
  return e;
};
var inflt = function(dat, st, buf, dict) {
  var sl = dat.length, dl = dict ? dict.length : 0;
  if (!sl || st.f && !st.l)
    return buf || new u8(0);
  var noBuf = !buf;
  var resize = noBuf || st.i != 2;
  var noSt = st.i;
  if (noBuf)
    buf = new u8(sl * 3);
  var cbuf = function(l2) {
    var bl = buf.length;
    if (l2 > bl) {
      var nbuf = new u8(Math.max(bl * 2, l2));
      nbuf.set(buf);
      buf = nbuf;
    }
  };
  var final = st.f || 0, pos = st.p || 0, bt = st.b || 0, lm = st.l, dm = st.d, lbt = st.m, dbt = st.n;
  var tbts = sl * 8;
  do {
    if (!lm) {
      final = bits(dat, pos, 1);
      var type = bits(dat, pos + 1, 3);
      pos += 3;
      if (!type) {
        var s = shft(pos) + 4, l = dat[s - 4] | dat[s - 3] << 8, t = s + l;
        if (t > sl) {
          if (noSt)
            err(0);
          break;
        }
        if (resize)
          cbuf(bt + l);
        buf.set(dat.subarray(s, t), bt);
        st.b = bt += l, st.p = pos = t * 8, st.f = final;
        continue;
      } else if (type == 1)
        lm = flrm, dm = fdrm, lbt = 9, dbt = 5;
      else if (type == 2) {
        var hLit = bits(dat, pos, 31) + 257, hcLen = bits(dat, pos + 10, 15) + 4;
        var tl = hLit + bits(dat, pos + 5, 31) + 1;
        pos += 14;
        var ldt = new u8(tl);
        var clt = new u8(19);
        for (var i2 = 0;i2 < hcLen; ++i2) {
          clt[clim[i2]] = bits(dat, pos + i2 * 3, 7);
        }
        pos += hcLen * 3;
        var clb = max(clt), clbmsk = (1 << clb) - 1;
        var clm = hMap(clt, clb, 1);
        for (var i2 = 0;i2 < tl; ) {
          var r = clm[bits(dat, pos, clbmsk)];
          pos += r & 15;
          var s = r >> 4;
          if (s < 16) {
            ldt[i2++] = s;
          } else {
            var c = 0, n = 0;
            if (s == 16)
              n = 3 + bits(dat, pos, 3), pos += 2, c = ldt[i2 - 1];
            else if (s == 17)
              n = 3 + bits(dat, pos, 7), pos += 3;
            else if (s == 18)
              n = 11 + bits(dat, pos, 127), pos += 7;
            while (n--)
              ldt[i2++] = c;
          }
        }
        var lt = ldt.subarray(0, hLit), dt = ldt.subarray(hLit);
        lbt = max(lt);
        dbt = max(dt);
        lm = hMap(lt, lbt, 1);
        dm = hMap(dt, dbt, 1);
      } else
        err(1);
      if (pos > tbts) {
        if (noSt)
          err(0);
        break;
      }
    }
    if (resize)
      cbuf(bt + 131072);
    var lms = (1 << lbt) - 1, dms = (1 << dbt) - 1;
    var lpos = pos;
    for (;; lpos = pos) {
      var c = lm[bits16(dat, pos) & lms], sym = c >> 4;
      pos += c & 15;
      if (pos > tbts) {
        if (noSt)
          err(0);
        break;
      }
      if (!c)
        err(2);
      if (sym < 256)
        buf[bt++] = sym;
      else if (sym == 256) {
        lpos = pos, lm = null;
        break;
      } else {
        var add = sym - 254;
        if (sym > 264) {
          var i2 = sym - 257, b = fleb[i2];
          add = bits(dat, pos, (1 << b) - 1) + fl[i2];
          pos += b;
        }
        var d = dm[bits16(dat, pos) & dms], dsym = d >> 4;
        if (!d)
          err(3);
        pos += d & 15;
        var dt = fd[dsym];
        if (dsym > 3) {
          var b = fdeb[dsym];
          dt += bits16(dat, pos) & (1 << b) - 1, pos += b;
        }
        if (pos > tbts) {
          if (noSt)
            err(0);
          break;
        }
        if (resize)
          cbuf(bt + 131072);
        var end = bt + add;
        if (bt < dt) {
          var shift = dl - dt, dend = Math.min(dt, end);
          if (shift + bt < 0)
            err(3);
          for (;bt < dend; ++bt)
            buf[bt] = dict[shift + bt];
        }
        for (;bt < end; ++bt)
          buf[bt] = buf[bt - dt];
      }
    }
    st.l = lm, st.p = lpos, st.b = bt, st.f = final;
    if (lm)
      final = 1, st.m = lbt, st.d = dm, st.n = dbt;
  } while (!final);
  return bt != buf.length && noBuf ? slc(buf, 0, bt) : buf.subarray(0, bt);
};
var wbits = function(d, p, v) {
  v <<= p & 7;
  var o = p / 8 | 0;
  d[o] |= v;
  d[o + 1] |= v >> 8;
};
var wbits16 = function(d, p, v) {
  v <<= p & 7;
  var o = p / 8 | 0;
  d[o] |= v;
  d[o + 1] |= v >> 8;
  d[o + 2] |= v >> 16;
};
var hTree = function(d, mb) {
  var t = [];
  for (var i2 = 0;i2 < d.length; ++i2) {
    if (d[i2])
      t.push({ s: i2, f: d[i2] });
  }
  var s = t.length;
  var t2 = t.slice();
  if (!s)
    return { t: et, l: 0 };
  if (s == 1) {
    var v = new u8(t[0].s + 1);
    v[t[0].s] = 1;
    return { t: v, l: 1 };
  }
  t.sort(function(a, b) {
    return a.f - b.f;
  });
  t.push({ s: -1, f: 25001 });
  var l = t[0], r = t[1], i0 = 0, i1 = 1, i22 = 2;
  t[0] = { s: -1, f: l.f + r.f, l, r };
  while (i1 != s - 1) {
    l = t[t[i0].f < t[i22].f ? i0++ : i22++];
    r = t[i0 != i1 && t[i0].f < t[i22].f ? i0++ : i22++];
    t[i1++] = { s: -1, f: l.f + r.f, l, r };
  }
  var maxSym = t2[0].s;
  for (var i2 = 1;i2 < s; ++i2) {
    if (t2[i2].s > maxSym)
      maxSym = t2[i2].s;
  }
  var tr = new u16(maxSym + 1);
  var mbt = ln(t[i1 - 1], tr, 0);
  if (mbt > mb) {
    var i2 = 0, dt = 0;
    var lft = mbt - mb, cst = 1 << lft;
    t2.sort(function(a, b) {
      return tr[b.s] - tr[a.s] || a.f - b.f;
    });
    for (;i2 < s; ++i2) {
      var i2_1 = t2[i2].s;
      if (tr[i2_1] > mb) {
        dt += cst - (1 << mbt - tr[i2_1]);
        tr[i2_1] = mb;
      } else
        break;
    }
    dt >>= lft;
    while (dt > 0) {
      var i2_2 = t2[i2].s;
      if (tr[i2_2] < mb)
        dt -= 1 << mb - tr[i2_2]++ - 1;
      else
        ++i2;
    }
    for (;i2 >= 0 && dt; --i2) {
      var i2_3 = t2[i2].s;
      if (tr[i2_3] == mb) {
        --tr[i2_3];
        ++dt;
      }
    }
    mbt = mb;
  }
  return { t: new u8(tr), l: mbt };
};
var ln = function(n, l, d) {
  return n.s == -1 ? Math.max(ln(n.l, l, d + 1), ln(n.r, l, d + 1)) : l[n.s] = d;
};
var lc = function(c) {
  var s = c.length;
  while (s && !c[--s])
    ;
  var cl = new u16(++s);
  var cli = 0, cln = c[0], cls = 1;
  var w = function(v) {
    cl[cli++] = v;
  };
  for (var i2 = 1;i2 <= s; ++i2) {
    if (c[i2] == cln && i2 != s)
      ++cls;
    else {
      if (!cln && cls > 2) {
        for (;cls > 138; cls -= 138)
          w(32754);
        if (cls > 2) {
          w(cls > 10 ? cls - 11 << 5 | 28690 : cls - 3 << 5 | 12305);
          cls = 0;
        }
      } else if (cls > 3) {
        w(cln), --cls;
        for (;cls > 6; cls -= 6)
          w(8304);
        if (cls > 2)
          w(cls - 3 << 5 | 8208), cls = 0;
      }
      while (cls--)
        w(cln);
      cls = 1;
      cln = c[i2];
    }
  }
  return { c: cl.subarray(0, cli), n: s };
};
var clen = function(cf, cl) {
  var l = 0;
  for (var i2 = 0;i2 < cl.length; ++i2)
    l += cf[i2] * cl[i2];
  return l;
};
var wfblk = function(out, pos, dat) {
  var s = dat.length;
  var o = shft(pos + 2);
  out[o] = s & 255;
  out[o + 1] = s >> 8;
  out[o + 2] = out[o] ^ 255;
  out[o + 3] = out[o + 1] ^ 255;
  for (var i2 = 0;i2 < s; ++i2)
    out[o + i2 + 4] = dat[i2];
  return (o + 4 + s) * 8;
};
var wblk = function(dat, out, final, syms, lf, df, eb, li, bs, bl, p) {
  wbits(out, p++, final);
  ++lf[256];
  var _a2 = hTree(lf, 15), dlt = _a2.t, mlb = _a2.l;
  var _b2 = hTree(df, 15), ddt = _b2.t, mdb = _b2.l;
  var _c = lc(dlt), lclt = _c.c, nlc = _c.n;
  var _d = lc(ddt), lcdt = _d.c, ndc = _d.n;
  var lcfreq = new u16(19);
  for (var i2 = 0;i2 < lclt.length; ++i2)
    ++lcfreq[lclt[i2] & 31];
  for (var i2 = 0;i2 < lcdt.length; ++i2)
    ++lcfreq[lcdt[i2] & 31];
  var _e = hTree(lcfreq, 7), lct = _e.t, mlcb = _e.l;
  var nlcc = 19;
  for (;nlcc > 4 && !lct[clim[nlcc - 1]]; --nlcc)
    ;
  var flen = bl + 5 << 3;
  var ftlen = clen(lf, flt) + clen(df, fdt) + eb;
  var dtlen = clen(lf, dlt) + clen(df, ddt) + eb + 14 + 3 * nlcc + clen(lcfreq, lct) + 2 * lcfreq[16] + 3 * lcfreq[17] + 7 * lcfreq[18];
  if (bs >= 0 && flen <= ftlen && flen <= dtlen)
    return wfblk(out, p, dat.subarray(bs, bs + bl));
  var lm, ll, dm, dl;
  wbits(out, p, 1 + (dtlen < ftlen)), p += 2;
  if (dtlen < ftlen) {
    lm = hMap(dlt, mlb, 0), ll = dlt, dm = hMap(ddt, mdb, 0), dl = ddt;
    var llm = hMap(lct, mlcb, 0);
    wbits(out, p, nlc - 257);
    wbits(out, p + 5, ndc - 1);
    wbits(out, p + 10, nlcc - 4);
    p += 14;
    for (var i2 = 0;i2 < nlcc; ++i2)
      wbits(out, p + 3 * i2, lct[clim[i2]]);
    p += 3 * nlcc;
    var lcts = [lclt, lcdt];
    for (var it = 0;it < 2; ++it) {
      var clct = lcts[it];
      for (var i2 = 0;i2 < clct.length; ++i2) {
        var len = clct[i2] & 31;
        wbits(out, p, llm[len]), p += lct[len];
        if (len > 15)
          wbits(out, p, clct[i2] >> 5 & 127), p += clct[i2] >> 12;
      }
    }
  } else {
    lm = flm, ll = flt, dm = fdm, dl = fdt;
  }
  for (var i2 = 0;i2 < li; ++i2) {
    var sym = syms[i2];
    if (sym > 255) {
      var len = sym >> 18 & 31;
      wbits16(out, p, lm[len + 257]), p += ll[len + 257];
      if (len > 7)
        wbits(out, p, sym >> 23 & 31), p += fleb[len];
      var dst = sym & 31;
      wbits16(out, p, dm[dst]), p += dl[dst];
      if (dst > 3)
        wbits16(out, p, sym >> 5 & 8191), p += fdeb[dst];
    } else {
      wbits16(out, p, lm[sym]), p += ll[sym];
    }
  }
  wbits16(out, p, lm[256]);
  return p + ll[256];
};
var deo = /* @__PURE__ */ new i32([65540, 131080, 131088, 131104, 262176, 1048704, 1048832, 2114560, 2117632]);
var et = /* @__PURE__ */ new u8(0);
var dflt = function(dat, lvl, plvl, pre, post, st) {
  var s = st.z || dat.length;
  var o = new u8(pre + s + 5 * (1 + Math.ceil(s / 7000)) + post);
  var w = o.subarray(pre, o.length - post);
  var lst = st.l;
  var pos = (st.r || 0) & 7;
  if (lvl) {
    if (pos)
      w[0] = st.r >> 3;
    var opt = deo[lvl - 1];
    var n = opt >> 13, c = opt & 8191;
    var msk_1 = (1 << plvl) - 1;
    var prev = st.p || new u16(32768), head = st.h || new u16(msk_1 + 1);
    var bs1_1 = Math.ceil(plvl / 3), bs2_1 = 2 * bs1_1;
    var hsh = function(i3) {
      return (dat[i3] ^ dat[i3 + 1] << bs1_1 ^ dat[i3 + 2] << bs2_1) & msk_1;
    };
    var syms = new i32(25000);
    var lf = new u16(288), df = new u16(32);
    var lc_1 = 0, eb = 0, i2 = st.i || 0, li = 0, wi = st.w || 0, bs = 0;
    for (;i2 + 2 < s; ++i2) {
      var hv = hsh(i2);
      var imod = i2 & 32767, pimod = head[hv];
      prev[imod] = pimod;
      head[hv] = imod;
      if (wi <= i2) {
        var rem = s - i2;
        if ((lc_1 > 7000 || li > 24576) && (rem > 423 || !lst)) {
          pos = wblk(dat, w, 0, syms, lf, df, eb, li, bs, i2 - bs, pos);
          li = lc_1 = eb = 0, bs = i2;
          for (var j = 0;j < 286; ++j)
            lf[j] = 0;
          for (var j = 0;j < 30; ++j)
            df[j] = 0;
        }
        var l = 2, d = 0, ch_1 = c, dif = imod - pimod & 32767;
        if (rem > 2 && hv == hsh(i2 - dif)) {
          var maxn = Math.min(n, rem) - 1;
          var maxd = Math.min(32767, i2);
          var ml = Math.min(258, rem);
          while (dif <= maxd && --ch_1 && imod != pimod) {
            if (dat[i2 + l] == dat[i2 + l - dif]) {
              var nl = 0;
              for (;nl < ml && dat[i2 + nl] == dat[i2 + nl - dif]; ++nl)
                ;
              if (nl > l) {
                l = nl, d = dif;
                if (nl > maxn)
                  break;
                var mmd = Math.min(dif, nl - 2);
                var md = 0;
                for (var j = 0;j < mmd; ++j) {
                  var ti = i2 - dif + j & 32767;
                  var pti = prev[ti];
                  var cd = ti - pti & 32767;
                  if (cd > md)
                    md = cd, pimod = ti;
                }
              }
            }
            imod = pimod, pimod = prev[imod];
            dif += imod - pimod & 32767;
          }
        }
        if (d) {
          syms[li++] = 268435456 | revfl[l] << 18 | revfd[d];
          var lin = revfl[l] & 31, din = revfd[d] & 31;
          eb += fleb[lin] + fdeb[din];
          ++lf[257 + lin];
          ++df[din];
          wi = i2 + l;
          ++lc_1;
        } else {
          syms[li++] = dat[i2];
          ++lf[dat[i2]];
        }
      }
    }
    for (i2 = Math.max(i2, wi);i2 < s; ++i2) {
      syms[li++] = dat[i2];
      ++lf[dat[i2]];
    }
    pos = wblk(dat, w, lst, syms, lf, df, eb, li, bs, i2 - bs, pos);
    if (!lst) {
      st.r = pos & 7 | w[pos / 8 | 0] << 3;
      pos -= 7;
      st.h = head, st.p = prev, st.i = i2, st.w = wi;
    }
  } else {
    for (var i2 = st.w || 0;i2 < s + lst; i2 += 65535) {
      var e = i2 + 65535;
      if (e >= s) {
        w[pos / 8 | 0] = lst;
        e = s;
      }
      pos = wfblk(w, pos + 1, dat.subarray(i2, e));
    }
    st.i = s;
  }
  return slc(o, 0, pre + shft(pos) + post);
};
var dopt = function(dat, opt, pre, post, st) {
  if (!st) {
    st = { l: 1 };
    if (opt.dictionary) {
      var dict = opt.dictionary.subarray(-32768);
      var newDat = new u8(dict.length + dat.length);
      newDat.set(dict);
      newDat.set(dat, dict.length);
      dat = newDat;
      st.w = dict.length;
    }
  }
  return dflt(dat, opt.level == null ? 6 : opt.level, opt.mem == null ? st.l ? Math.ceil(Math.max(8, Math.min(13, Math.log(dat.length))) * 1.5) : 20 : 12 + opt.mem, pre, post, st);
};
function deflateSync(data, opts) {
  return dopt(data, opts || {}, 0, 0);
}
function inflateSync(data, opts) {
  return inflt(data, { i: 2 }, opts && opts.out, opts && opts.dictionary);
}
var td = typeof TextDecoder != "undefined" && /* @__PURE__ */ new TextDecoder;
var tds = 0;
try {
  td.decode(et, { stream: true });
  tds = 1;
} catch (e) {}

// shim/lib/text.ts
var capitalize = (v) => v ? v.charAt(0).toUpperCase() + v.slice(1) : v;

// shim/lib/loadout.ts
class BitWriter {
  bits = [];
  bit(v) {
    this.bits.push(v ? 1 : 0);
  }
  uint(value, width) {
    let v = value >>> 0;
    for (let i2 = 0;i2 < width; i2 += 1) {
      this.bits.push(v & 1);
      v >>>= 1;
    }
  }
  varint(value) {
    let v = Math.max(0, Math.floor(value));
    do {
      const group = v & 127;
      v = Math.floor(v / 128);
      this.bit(v > 0 ? 1 : 0);
      this.uint(group, 7);
    } while (v > 0);
  }
  str(s) {
    const bytes = new TextEncoder().encode(s);
    this.varint(bytes.length);
    for (const b of bytes) {
      this.uint(b, 8);
    }
  }
  bytes() {
    const out = new Uint8Array(Math.ceil(this.bits.length / 8));
    for (let i2 = 0;i2 < this.bits.length; i2 += 1) {
      if (this.bits[i2]) {
        out[i2 >> 3] |= 1 << (i2 & 7);
      }
    }
    return out;
  }
}

class BitReader {
  buf;
  pos = 0;
  constructor(buf) {
    this.buf = buf;
  }
  bit() {
    if (this.pos >= this.buf.length * 8) {
      throw new RangeError("loadout truncated");
    }
    const i2 = this.pos++;
    return this.buf[i2 >> 3] >> (i2 & 7) & 1;
  }
  uint(width) {
    let v = 0;
    for (let i2 = 0;i2 < width; i2 += 1) {
      v |= this.bit() << i2;
    }
    return v >>> 0;
  }
  varint() {
    let v = 0;
    let shift = 0;
    for (;; ) {
      const cont = this.bit();
      v += this.uint(7) * 2 ** shift;
      shift += 7;
      if (!cont) {
        return v;
      }
    }
  }
  str() {
    const len = this.varint();
    const bytes = new Uint8Array(len);
    for (let i2 = 0;i2 < len; i2 += 1) {
      bytes[i2] = this.uint(8);
    }
    return new TextDecoder().decode(bytes);
  }
}

class Dict {
  index = new Map;
  list = [];
  id(s) {
    const hit = this.index.get(s);
    if (hit !== undefined) {
      return hit;
    }
    const id = this.list.length;
    this.index.set(s, id);
    this.list.push(s);
    return id;
  }
}
var idxOf = (table, value) => {
  const i2 = table.indexOf(value);
  return i2 < 0 ? 0 : i2;
};
var indexBits = (n) => n <= 1 ? 1 : Math.ceil(Math.log2(n));
function toBase64Url(buf) {
  let bin = "";
  for (const b of buf) {
    bin += String.fromCharCode(b);
  }
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function fromBase64Url(s) {
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(b64 + "=".repeat((4 - b64.length % 4) % 4));
  const out = new Uint8Array(bin.length);
  for (let i2 = 0;i2 < bin.length; i2 += 1) {
    out[i2] = bin.charCodeAt(i2);
  }
  return out;
}
function checksum16(buf) {
  let h = 2166136261;
  for (const b of buf) {
    h ^= b;
    h = Math.imul(h, 16777619);
  }
  return h >>> 0 & 65535;
}

class LoadoutError extends Error {
}
var HEAD_BYTES = 3;
function createLoadout(spec) {
  const Err = spec.error ?? LoadoutError;
  const noun = spec.noun ?? "code";
  const Noun = capitalize(noun);
  const encode = (value) => {
    const body = new BitWriter;
    spec.write(body, value);
    const payload = deflateSync(body.bytes(), { level: 9 });
    const head = new BitWriter;
    head.uint(spec.version, 8);
    head.uint(checksum16(payload), 16);
    const headBytes = head.bytes();
    const framed = new Uint8Array(headBytes.length + payload.length);
    framed.set(headBytes, 0);
    framed.set(payload, headBytes.length);
    return spec.prefix + toBase64Url(framed);
  };
  const decode = (code) => {
    const cleaned = code.replace(/\s+/g, "");
    const raw = cleaned.startsWith(spec.prefix) ? cleaned.slice(spec.prefix.length) : cleaned;
    if (!raw) {
      throw new Err(`That doesn't look like a ${noun}.`);
    }
    let framed;
    try {
      framed = fromBase64Url(raw);
    } catch {
      throw new Err(`That doesn't look like a ${noun}.`);
    }
    if (framed.length <= HEAD_BYTES) {
      throw new Err(`${Noun} is too short to be valid.`);
    }
    const head = new BitReader(framed.subarray(0, HEAD_BYTES));
    const version = head.uint(8);
    const storedSum = head.uint(16);
    if (version !== spec.version) {
      throw new Err(`${Noun} is version ${version}; this build reads version ${spec.version}.`);
    }
    const payload = framed.subarray(HEAD_BYTES);
    if (checksum16(payload) !== storedSum) {
      throw new Err(`${Noun} looks corrupted (checksum mismatch).`);
    }
    try {
      return spec.read(new BitReader(inflateSync(payload)));
    } catch (err2) {
      throw new Err(err2 instanceof Error ? `${Noun} is malformed: ${err2.message}` : `${Noun} is malformed.`);
    }
  };
  return { decode, encode };
}

// starmap/share-code.ts
var VERSION = 3;
var PREFIX = "HML";
var MAX_LABEL = 64;
var trim = (s) => s.length > MAX_LABEL ? s.slice(0, MAX_LABEL) : s;
var KINDS = ["skill", "memory"];
var STATES = ["active", "archived", "disabled", "draft"];
var MEM_SOURCES = ["none", "memory", "profile"];
var CREATED_BY = ["none", "agent", "user"];
var REC_BITS = 12;
var REC_MAX = (1 << REC_BITS) - 1;
var finiteTs = (v) => typeof v === "number" && Number.isFinite(v) ? Math.max(0, Math.round(v)) : null;
function writeNode(w, n, dict, minTs, span) {
  w.uint(idxOf(KINDS, n.kind), 1);
  w.varint(dict.id(trim(n.label || "")));
  w.varint(dict.id(n.category || ""));
  w.varint(Math.max(0, n.useCount | 0));
  w.uint(idxOf(STATES, n.state), 2);
  w.uint(idxOf(MEM_SOURCES, n.memorySource ?? "none"), 2);
  w.uint(idxOf(CREATED_BY, n.createdBy ?? "none"), 2);
  w.bit(n.pinned);
  const ts = finiteTs(n.timestamp);
  if (ts === null) {
    w.bit(0);
  } else {
    w.bit(1);
    w.uint(span > 0 ? Math.round((ts - minTs) / span * REC_MAX) : 0, REC_BITS);
  }
}
function readNode(r, dict, i2, minTs, span) {
  const kind = KINDS[r.uint(1)] ?? "skill";
  const label = dict[r.varint()] ?? "";
  const category = dict[r.varint()] ?? "";
  const useCount = r.varint();
  const state = STATES[r.uint(2)] ?? "active";
  const memSrc = MEM_SOURCES[r.uint(2)] ?? "none";
  const createdBy = CREATED_BY[r.uint(2)] ?? "none";
  const pinned = r.bit() === 1;
  const timestamp = r.bit() === 1 ? minTs + (span > 0 ? Math.round(r.uint(REC_BITS) / REC_MAX * span) : 0) : null;
  const isMemory = kind === "memory";
  const source = memSrc === "none" ? "memory" : memSrc;
  return {
    category,
    createdBy: createdBy === "none" ? null : createdBy,
    id: isMemory ? `memory:${source}:${i2}` : `s${i2}`,
    kind,
    label,
    memorySource: isMemory ? source : undefined,
    pinned,
    state,
    timestamp,
    useCount
  };
}
function writeGraph(w, graph) {
  const dict = new Dict;
  for (const n of graph.nodes) {
    dict.id(trim(n.label || ""));
    dict.id(n.category || "");
  }
  const stamps = graph.nodes.map((n) => finiteTs(n.timestamp)).filter((v) => v !== null);
  const minTs = stamps.length ? Math.min(...stamps) : 0;
  const maxTs = stamps.length ? Math.max(...stamps) : 0;
  const span = maxTs - minTs;
  w.varint(minTs);
  w.varint(maxTs);
  w.varint(dict.list.length);
  for (const s of dict.list) {
    w.str(s);
  }
  w.varint(graph.nodes.length);
  for (const n of graph.nodes) {
    writeNode(w, n, dict, minTs, span);
  }
  const order = new Map(graph.nodes.map((n, i2) => [n.id, i2]));
  const edges = graph.edges.filter((e) => order.has(e.source) && order.has(e.target));
  const bits2 = indexBits(graph.nodes.length);
  w.varint(edges.length);
  for (const e of edges) {
    w.uint(order.get(e.source), bits2);
    w.uint(order.get(e.target), bits2);
  }
}
function readGraph(r) {
  const minTs = r.varint();
  const maxTs = r.varint();
  const span = maxTs - minTs;
  const dictLen = r.varint();
  const dict = [];
  for (let i2 = 0;i2 < dictLen; i2 += 1) {
    dict.push(r.str());
  }
  const nodeCount = r.varint();
  const nodes = [];
  for (let i2 = 0;i2 < nodeCount; i2 += 1) {
    nodes.push(readNode(r, dict, i2, minTs, span));
  }
  const bits2 = indexBits(nodeCount);
  const edgeCount = r.varint();
  const edges = [];
  for (let i2 = 0;i2 < edgeCount; i2 += 1) {
    const src = nodes[r.uint(bits2)];
    const dst = nodes[r.uint(bits2)];
    if (src && dst) {
      edges.push({ source: src.id, target: dst.id });
    }
  }
  const counts = new Map;
  for (const n of nodes) {
    counts.set(n.category, (counts.get(n.category) ?? 0) + 1);
  }
  const clusters = [...counts.entries()].map(([category, count]) => ({ category, count })).sort((a, b) => b.count - a.count);
  return { clusters, edges, memory: [], nodes, stats: { imported: true } };
}

class ShareCodeError extends LoadoutError {
}
var codec = createLoadout({
  error: ShareCodeError,
  noun: "map code",
  prefix: PREFIX,
  read: readGraph,
  version: VERSION,
  write: writeGraph
});
function encodeShareCode(graph) {
  return codec.encode(graph);
}
function decodeShareCode(code) {
  return codec.decode(code);
}

// starmap/share-controls.tsx
import { useState as useState3 } from "react";

// shim/components/ui/button.tsx

function Button({
  children,
  className,
  disabled,
  onClick,
  size = "sm",
  style,
  type = "button",
  variant = "default"
}) {
  const base = {
    alignItems: "center",
    borderRadius: 6,
    boxSizing: "border-box",
    cursor: disabled ? "default" : "pointer",
    display: "inline-flex",
    fontSize: 12,
    justifyContent: "center",
    lineHeight: 1.2,
    opacity: disabled ? 0.45 : 1
  };
  if (variant === "outline") {
    Object.assign(base, { background: "transparent", border: "1px solid rgba(255,255,255,.16)", color: "var(--sm-fg,#e8e8ea)", padding: "5px 12px" });
  } else if (variant === "ghost") {
    Object.assign(base, { background: "transparent", color: "var(--sm-muted,rgba(232,232,234,.55))", padding: size === "icon" ? 6 : "5px 10px" });
  } else if (variant === "text") {
    Object.assign(base, { background: "transparent", color: "var(--sm-muted,rgba(232,232,234,.55))", padding: 0 });
  } else {
    Object.assign(base, { background: "rgba(96,165,250,.18)", border: "none", color: "var(--sm-fg,#e8e8ea)", padding: "7px 14px" });
  }
  if (size === "xs")
    base.fontSize = 11;
  return /* @__PURE__ */ jsxDEV("button", {
    className,
    disabled,
    onClick,
    style: { ...base, ...style },
    type,
    children
  }, undefined, false, undefined, this);
}

// shim/components/ui/copy-button.tsx
import { useState as useState2 } from "react";

function CopyButton({
  className,
  label = "Copy",
  showLabel = false,
  text = ""
}) {
  const [done, setDone] = useState2(false);
  return /* @__PURE__ */ jsxDEV2("button", {
    className,
    onClick: () => {
      try {
        navigator.clipboard?.writeText(text);
      } catch {}
      setDone(true);
      setTimeout(() => setDone(false), 1500);
    },
    style: {
      alignItems: "center",
      background: "rgba(255,255,255,.06)",
      border: "none",
      borderRadius: 6,
      color: "var(--sm-fg,#e8e8ea)",
      cursor: "pointer",
      display: "inline-flex",
      fontSize: 11,
      padding: showLabel ? "3px 8px" : 4
    },
    type: "button",
    children: done ? "Copied!" : showLabel ? label : "⧉"
  }, undefined, false, undefined, this);
}

// shim/components/ui/dialog.tsx
import { createContext, cloneElement, isValidElement, useContext } from "react";

var Ctx = createContext({ open: false, setOpen: () => {} });
function Dialog({
  children,
  onOpenChange,
  open = false
}) {
  return /* @__PURE__ */ jsxDEV3(Ctx.Provider, {
    value: { open, setOpen: (v) => onOpenChange?.(v) },
    children
  }, undefined, false, undefined, this);
}
function DialogTrigger({ asChild, children }) {
  const ctx = useContext(Ctx);
  if (asChild && isValidElement(children)) {
    return cloneElement(children, {
      onClick: (e) => {
        children.props.onClick?.(e);
        ctx.setOpen(true);
      }
    });
  }
  return /* @__PURE__ */ jsxDEV3("button", {
    onClick: () => ctx.setOpen(true),
    style: { background: "none", border: "none", color: "inherit", cursor: "pointer" },
    type: "button",
    children
  }, undefined, false, undefined, this);
}
function DialogContent({ children, className, style }) {
  const ctx = useContext(Ctx);
  if (!ctx.open) {
    return null;
  }
  return /* @__PURE__ */ jsxDEV3("div", {
    onMouseDown: (e) => {
      if (e.target === e.currentTarget)
        ctx.setOpen(false);
    },
    style: {
      alignItems: "center",
      background: "rgba(0,0,0,.6)",
      display: "flex",
      inset: 0,
      justifyContent: "center",
      position: "fixed",
      zIndex: 9999
    },
    children: /* @__PURE__ */ jsxDEV3("div", {
      className,
      style: {
        background: "#101014",
        border: "1px solid rgba(255,255,255,.12)",
        borderRadius: 10,
        color: "var(--sm-fg,#e8e8ea)",
        display: "flex",
        flexDirection: "column",
        gap: 10,
        padding: 18,
        ...style
      },
      children
    }, undefined, false, undefined, this)
  }, undefined, false, undefined, this);
}
function DialogHeader({ children }) {
  return /* @__PURE__ */ jsxDEV3("div", {
    children
  }, undefined, false, undefined, this);
}
function DialogTitle({ children }) {
  return /* @__PURE__ */ jsxDEV3("div", {
    style: { fontSize: 14, fontWeight: 600 },
    children
  }, undefined, false, undefined, this);
}
function DialogDescription({ children }) {
  return /* @__PURE__ */ jsxDEV3("div", {
    style: { color: "var(--sm-muted,rgba(232,232,234,.55))", fontSize: 12 },
    children
  }, undefined, false, undefined, this);
}

// shim/components/ui/tooltip.tsx

function Tip({ children, label }) {
  return /* @__PURE__ */ jsxDEV4("span", {
    style: { display: "inline-flex" },
    title: label,
    children
  }, undefined, false, undefined, this);
}

// shim/i18n.ts
var starmap = {
  close: "Close memory graph",
  loadFailed: "Could not load memory graph",
  loading: "Loading…",
  emptyTitle: "Nothing learned yet",
  emptyDesc: "As Hermes builds skills and memories for your work, they appear here.",
  shareHint: "Copy the code to share this map, or paste one to load. It only includes the layout, not your memory or skill text.",
  shareTitle: "Import / export map",
  sharePlaceholder: "Paste a map code…",
  copy: "Copy map code",
  importBtn: "Load",
  importEmpty: "Paste a map code to load it.",
  resetToMine: "Back to my map"
};
function useI18n() {
  return { t: { starmap } };
}

// shim/lib/icons.tsx

var Upload = (props) => /* @__PURE__ */ jsxDEV5("svg", {
  fill: "none",
  stroke: "currentColor",
  strokeLinecap: "round",
  strokeLinejoin: "round",
  strokeWidth: "2",
  viewBox: "0 0 24 24",
  width: "1em",
  height: "1em",
  ...props,
  children: [
    /* @__PURE__ */ jsxDEV5("path", {
      d: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"
    }, undefined, false, undefined, this),
    /* @__PURE__ */ jsxDEV5("polyline", {
      points: "17 8 12 3 7 8"
    }, undefined, false, undefined, this),
    /* @__PURE__ */ jsxDEV5("line", {
      x1: "12",
      x2: "12",
      y1: "3",
      y2: "15"
    }, undefined, false, undefined, this)
  ]
}, undefined, true, undefined, this);

// starmap/share-controls.tsx

function ShareControls({ imported = false, onImport, onResetMap, shareCode }) {
  const { t } = useI18n();
  const [open, setOpen] = useState3(false);
  const [value, setValue] = useState3("");
  const [error, setError] = useState3(null);
  const own = (shareCode ?? "").trim();
  const code = value.trim();
  const canLoad = code !== "" && code !== own;
  const load = () => {
    if (!code) {
      setError(t.starmap.importEmpty);
      return;
    }
    const err2 = onImport?.(code) ?? null;
    setError(err2);
    if (err2 === null) {
      setOpen(false);
    }
  };
  return /* @__PURE__ */ jsxDEV6("div", {
    className: "flex items-center gap-1",
    children: [
      imported && /* @__PURE__ */ jsxDEV6(Button, {
        className: "text-muted-foreground hover:text-foreground",
        onClick: () => onResetMap?.(),
        size: "xs",
        variant: "text",
        children: t.starmap.resetToMine
      }, undefined, false, undefined, this),
      /* @__PURE__ */ jsxDEV6(Dialog, {
        onOpenChange: (next) => {
          setOpen(next);
          setError(null);
          if (next) {
            setValue(shareCode ?? "");
          }
        },
        open,
        children: [
          /* @__PURE__ */ jsxDEV6(Tip, {
            label: t.starmap.shareTitle,
            children: /* @__PURE__ */ jsxDEV6(DialogTrigger, {
              asChild: true,
              children: /* @__PURE__ */ jsxDEV6(Button, {
                "aria-label": t.starmap.shareTitle,
                className: "text-muted-foreground hover:text-foreground",
                size: "icon",
                variant: "ghost",
                children: /* @__PURE__ */ jsxDEV6(Upload, {
                  className: "size-3.5"
                }, undefined, false, undefined, this)
              }, undefined, false, undefined, this)
            }, undefined, false, undefined, this)
          }, undefined, false, undefined, this),
          /* @__PURE__ */ jsxDEV6(DialogContent, {
            className: "max-w-md",
            children: [
              /* @__PURE__ */ jsxDEV6(DialogHeader, {
                children: [
                  /* @__PURE__ */ jsxDEV6(DialogTitle, {
                    children: t.starmap.shareTitle
                  }, undefined, false, undefined, this),
                  /* @__PURE__ */ jsxDEV6(DialogDescription, {
                    children: t.starmap.shareHint
                  }, undefined, false, undefined, this)
                ]
              }, undefined, true, undefined, this),
              /* @__PURE__ */ jsxDEV6("div", {
                className: "group/code relative",
                children: [
                  /* @__PURE__ */ jsxDEV6("textarea", {
                    "aria-label": t.starmap.shareTitle,
                    className: "h-24 w-full resize-none rounded-md bg-foreground/5 p-2.5 pr-9 font-mono text-xs leading-relaxed break-all text-muted-foreground/90 outline-none transition placeholder:text-muted-foreground/50 focus-visible:text-foreground focus-visible:ring-1 focus-visible:ring-ring/40",
                    onChange: (e) => {
                      setValue(e.target.value);
                      setError(null);
                    },
                    placeholder: t.starmap.sharePlaceholder,
                    spellCheck: false,
                    value
                  }, undefined, false, undefined, this),
                  code !== "" && /* @__PURE__ */ jsxDEV6(CopyButton, {
                    appearance: "inline",
                    className: "absolute right-1.5 top-1.5 h-5 gap-0 rounded-md px-1 opacity-0 transition-opacity focus-visible:opacity-100 group-hover/code:opacity-100 hover:opacity-100",
                    iconClassName: "size-3",
                    label: t.starmap.copy,
                    showLabel: false,
                    text: value
                  }, undefined, false, undefined, this)
                ]
              }, undefined, true, undefined, this),
              error && /* @__PURE__ */ jsxDEV6("p", {
                className: "text-[0.7rem] text-destructive",
                children: error
              }, undefined, false, undefined, this),
              /* @__PURE__ */ jsxDEV6(Button, {
                className: "w-full",
                disabled: !canLoad,
                onClick: load,
                type: "button",
                children: t.starmap.importBtn
              }, undefined, false, undefined, this)
            ]
          }, undefined, true, undefined, this)
        ]
      }, undefined, true, undefined, this)
    ]
  }, undefined, true, undefined, this);
}
// ../../../node_modules/d3-quadtree/src/add.js
function add_default(d) {
  const x2 = +this._x.call(null, d), y = +this._y.call(null, d);
  return add(this.cover(x2, y), x2, y, d);
}
function add(tree, x2, y, d) {
  if (isNaN(x2) || isNaN(y))
    return tree;
  var parent, node = tree._root, leaf = { data: d }, x0 = tree._x0, y0 = tree._y0, x1 = tree._x1, y1 = tree._y1, xm, ym, xp, yp, right, bottom, i2, j;
  if (!node)
    return tree._root = leaf, tree;
  while (node.length) {
    if (right = x2 >= (xm = (x0 + x1) / 2))
      x0 = xm;
    else
      x1 = xm;
    if (bottom = y >= (ym = (y0 + y1) / 2))
      y0 = ym;
    else
      y1 = ym;
    if (parent = node, !(node = node[i2 = bottom << 1 | right]))
      return parent[i2] = leaf, tree;
  }
  xp = +tree._x.call(null, node.data);
  yp = +tree._y.call(null, node.data);
  if (x2 === xp && y === yp)
    return leaf.next = node, parent ? parent[i2] = leaf : tree._root = leaf, tree;
  do {
    parent = parent ? parent[i2] = new Array(4) : tree._root = new Array(4);
    if (right = x2 >= (xm = (x0 + x1) / 2))
      x0 = xm;
    else
      x1 = xm;
    if (bottom = y >= (ym = (y0 + y1) / 2))
      y0 = ym;
    else
      y1 = ym;
  } while ((i2 = bottom << 1 | right) === (j = (yp >= ym) << 1 | xp >= xm));
  return parent[j] = node, parent[i2] = leaf, tree;
}
function addAll(data) {
  var d, i2, n = data.length, x2, y, xz = new Array(n), yz = new Array(n), x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (i2 = 0;i2 < n; ++i2) {
    if (isNaN(x2 = +this._x.call(null, d = data[i2])) || isNaN(y = +this._y.call(null, d)))
      continue;
    xz[i2] = x2;
    yz[i2] = y;
    if (x2 < x0)
      x0 = x2;
    if (x2 > x1)
      x1 = x2;
    if (y < y0)
      y0 = y;
    if (y > y1)
      y1 = y;
  }
  if (x0 > x1 || y0 > y1)
    return this;
  this.cover(x0, y0).cover(x1, y1);
  for (i2 = 0;i2 < n; ++i2) {
    add(this, xz[i2], yz[i2], data[i2]);
  }
  return this;
}

// ../../../node_modules/d3-quadtree/src/cover.js
function cover_default(x2, y) {
  if (isNaN(x2 = +x2) || isNaN(y = +y))
    return this;
  var x0 = this._x0, y0 = this._y0, x1 = this._x1, y1 = this._y1;
  if (isNaN(x0)) {
    x1 = (x0 = Math.floor(x2)) + 1;
    y1 = (y0 = Math.floor(y)) + 1;
  } else {
    var z = x1 - x0 || 1, node = this._root, parent, i2;
    while (x0 > x2 || x2 >= x1 || y0 > y || y >= y1) {
      i2 = (y < y0) << 1 | x2 < x0;
      parent = new Array(4), parent[i2] = node, node = parent, z *= 2;
      switch (i2) {
        case 0:
          x1 = x0 + z, y1 = y0 + z;
          break;
        case 1:
          x0 = x1 - z, y1 = y0 + z;
          break;
        case 2:
          x1 = x0 + z, y0 = y1 - z;
          break;
        case 3:
          x0 = x1 - z, y0 = y1 - z;
          break;
      }
    }
    if (this._root && this._root.length)
      this._root = node;
  }
  this._x0 = x0;
  this._y0 = y0;
  this._x1 = x1;
  this._y1 = y1;
  return this;
}

// ../../../node_modules/d3-quadtree/src/data.js
function data_default() {
  var data = [];
  this.visit(function(node) {
    if (!node.length)
      do
        data.push(node.data);
      while (node = node.next);
  });
  return data;
}

// ../../../node_modules/d3-quadtree/src/extent.js
function extent_default(_) {
  return arguments.length ? this.cover(+_[0][0], +_[0][1]).cover(+_[1][0], +_[1][1]) : isNaN(this._x0) ? undefined : [[this._x0, this._y0], [this._x1, this._y1]];
}

// ../../../node_modules/d3-quadtree/src/quad.js
function quad_default(node, x0, y0, x1, y1) {
  this.node = node;
  this.x0 = x0;
  this.y0 = y0;
  this.x1 = x1;
  this.y1 = y1;
}

// ../../../node_modules/d3-quadtree/src/find.js
function find_default(x2, y, radius) {
  var data, x0 = this._x0, y0 = this._y0, x1, y1, x22, y2, x3 = this._x1, y3 = this._y1, quads = [], node = this._root, q, i2;
  if (node)
    quads.push(new quad_default(node, x0, y0, x3, y3));
  if (radius == null)
    radius = Infinity;
  else {
    x0 = x2 - radius, y0 = y - radius;
    x3 = x2 + radius, y3 = y + radius;
    radius *= radius;
  }
  while (q = quads.pop()) {
    if (!(node = q.node) || (x1 = q.x0) > x3 || (y1 = q.y0) > y3 || (x22 = q.x1) < x0 || (y2 = q.y1) < y0)
      continue;
    if (node.length) {
      var xm = (x1 + x22) / 2, ym = (y1 + y2) / 2;
      quads.push(new quad_default(node[3], xm, ym, x22, y2), new quad_default(node[2], x1, ym, xm, y2), new quad_default(node[1], xm, y1, x22, ym), new quad_default(node[0], x1, y1, xm, ym));
      if (i2 = (y >= ym) << 1 | x2 >= xm) {
        q = quads[quads.length - 1];
        quads[quads.length - 1] = quads[quads.length - 1 - i2];
        quads[quads.length - 1 - i2] = q;
      }
    } else {
      var dx = x2 - +this._x.call(null, node.data), dy = y - +this._y.call(null, node.data), d2 = dx * dx + dy * dy;
      if (d2 < radius) {
        var d = Math.sqrt(radius = d2);
        x0 = x2 - d, y0 = y - d;
        x3 = x2 + d, y3 = y + d;
        data = node.data;
      }
    }
  }
  return data;
}

// ../../../node_modules/d3-quadtree/src/remove.js
function remove_default(d) {
  if (isNaN(x2 = +this._x.call(null, d)) || isNaN(y = +this._y.call(null, d)))
    return this;
  var parent, node = this._root, retainer, previous, next, x0 = this._x0, y0 = this._y0, x1 = this._x1, y1 = this._y1, x2, y, xm, ym, right, bottom, i2, j;
  if (!node)
    return this;
  if (node.length)
    while (true) {
      if (right = x2 >= (xm = (x0 + x1) / 2))
        x0 = xm;
      else
        x1 = xm;
      if (bottom = y >= (ym = (y0 + y1) / 2))
        y0 = ym;
      else
        y1 = ym;
      if (!(parent = node, node = node[i2 = bottom << 1 | right]))
        return this;
      if (!node.length)
        break;
      if (parent[i2 + 1 & 3] || parent[i2 + 2 & 3] || parent[i2 + 3 & 3])
        retainer = parent, j = i2;
    }
  while (node.data !== d)
    if (!(previous = node, node = node.next))
      return this;
  if (next = node.next)
    delete node.next;
  if (previous)
    return next ? previous.next = next : delete previous.next, this;
  if (!parent)
    return this._root = next, this;
  next ? parent[i2] = next : delete parent[i2];
  if ((node = parent[0] || parent[1] || parent[2] || parent[3]) && node === (parent[3] || parent[2] || parent[1] || parent[0]) && !node.length) {
    if (retainer)
      retainer[j] = node;
    else
      this._root = node;
  }
  return this;
}
function removeAll(data) {
  for (var i2 = 0, n = data.length;i2 < n; ++i2)
    this.remove(data[i2]);
  return this;
}

// ../../../node_modules/d3-quadtree/src/root.js
function root_default() {
  return this._root;
}

// ../../../node_modules/d3-quadtree/src/size.js
function size_default() {
  var size = 0;
  this.visit(function(node) {
    if (!node.length)
      do
        ++size;
      while (node = node.next);
  });
  return size;
}

// ../../../node_modules/d3-quadtree/src/visit.js
function visit_default(callback) {
  var quads = [], q, node = this._root, child, x0, y0, x1, y1;
  if (node)
    quads.push(new quad_default(node, this._x0, this._y0, this._x1, this._y1));
  while (q = quads.pop()) {
    if (!callback(node = q.node, x0 = q.x0, y0 = q.y0, x1 = q.x1, y1 = q.y1) && node.length) {
      var xm = (x0 + x1) / 2, ym = (y0 + y1) / 2;
      if (child = node[3])
        quads.push(new quad_default(child, xm, ym, x1, y1));
      if (child = node[2])
        quads.push(new quad_default(child, x0, ym, xm, y1));
      if (child = node[1])
        quads.push(new quad_default(child, xm, y0, x1, ym));
      if (child = node[0])
        quads.push(new quad_default(child, x0, y0, xm, ym));
    }
  }
  return this;
}

// ../../../node_modules/d3-quadtree/src/visitAfter.js
function visitAfter_default(callback) {
  var quads = [], next = [], q;
  if (this._root)
    quads.push(new quad_default(this._root, this._x0, this._y0, this._x1, this._y1));
  while (q = quads.pop()) {
    var node = q.node;
    if (node.length) {
      var child, x0 = q.x0, y0 = q.y0, x1 = q.x1, y1 = q.y1, xm = (x0 + x1) / 2, ym = (y0 + y1) / 2;
      if (child = node[0])
        quads.push(new quad_default(child, x0, y0, xm, ym));
      if (child = node[1])
        quads.push(new quad_default(child, xm, y0, x1, ym));
      if (child = node[2])
        quads.push(new quad_default(child, x0, ym, xm, y1));
      if (child = node[3])
        quads.push(new quad_default(child, xm, ym, x1, y1));
    }
    next.push(q);
  }
  while (q = next.pop()) {
    callback(q.node, q.x0, q.y0, q.x1, q.y1);
  }
  return this;
}

// ../../../node_modules/d3-quadtree/src/x.js
function defaultX(d) {
  return d[0];
}
function x_default(_) {
  return arguments.length ? (this._x = _, this) : this._x;
}

// ../../../node_modules/d3-quadtree/src/y.js
function defaultY(d) {
  return d[1];
}
function y_default(_) {
  return arguments.length ? (this._y = _, this) : this._y;
}

// ../../../node_modules/d3-quadtree/src/quadtree.js
function quadtree(nodes, x2, y) {
  var tree = new Quadtree(x2 == null ? defaultX : x2, y == null ? defaultY : y, NaN, NaN, NaN, NaN);
  return nodes == null ? tree : tree.addAll(nodes);
}
function Quadtree(x2, y, x0, y0, x1, y1) {
  this._x = x2;
  this._y = y;
  this._x0 = x0;
  this._y0 = y0;
  this._x1 = x1;
  this._y1 = y1;
  this._root = undefined;
}
function leaf_copy(leaf) {
  var copy = { data: leaf.data }, next = copy;
  while (leaf = leaf.next)
    next = next.next = { data: leaf.data };
  return copy;
}
var treeProto = quadtree.prototype = Quadtree.prototype;
treeProto.copy = function() {
  var copy = new Quadtree(this._x, this._y, this._x0, this._y0, this._x1, this._y1), node = this._root, nodes, child;
  if (!node)
    return copy;
  if (!node.length)
    return copy._root = leaf_copy(node), copy;
  nodes = [{ source: node, target: copy._root = new Array(4) }];
  while (node = nodes.pop()) {
    for (var i2 = 0;i2 < 4; ++i2) {
      if (child = node.source[i2]) {
        if (child.length)
          nodes.push({ source: child, target: node.target[i2] = new Array(4) });
        else
          node.target[i2] = leaf_copy(child);
      }
    }
  }
  return copy;
};
treeProto.add = add_default;
treeProto.addAll = addAll;
treeProto.cover = cover_default;
treeProto.data = data_default;
treeProto.extent = extent_default;
treeProto.find = find_default;
treeProto.remove = remove_default;
treeProto.removeAll = removeAll;
treeProto.root = root_default;
treeProto.size = size_default;
treeProto.visit = visit_default;
treeProto.visitAfter = visitAfter_default;
treeProto.x = x_default;
treeProto.y = y_default;
// ../../../node_modules/d3-force/src/constant.js
function constant_default(x2) {
  return function() {
    return x2;
  };
}

// ../../../node_modules/d3-force/src/jiggle.js
function jiggle_default(random) {
  return (random() - 0.5) * 0.000001;
}

// ../../../node_modules/d3-force/src/collide.js
function x2(d) {
  return d.x + d.vx;
}
function y(d) {
  return d.y + d.vy;
}
function collide_default(radius) {
  var nodes, radii, random, strength = 1, iterations = 1;
  if (typeof radius !== "function")
    radius = constant_default(radius == null ? 1 : +radius);
  function force() {
    var i2, n = nodes.length, tree, node, xi, yi, ri, ri2;
    for (var k = 0;k < iterations; ++k) {
      tree = quadtree(nodes, x2, y).visitAfter(prepare);
      for (i2 = 0;i2 < n; ++i2) {
        node = nodes[i2];
        ri = radii[node.index], ri2 = ri * ri;
        xi = node.x + node.vx;
        yi = node.y + node.vy;
        tree.visit(apply);
      }
    }
    function apply(quad, x0, y0, x1, y1) {
      var { data, r: rj } = quad, r = ri + rj;
      if (data) {
        if (data.index > node.index) {
          var x3 = xi - data.x - data.vx, y2 = yi - data.y - data.vy, l = x3 * x3 + y2 * y2;
          if (l < r * r) {
            if (x3 === 0)
              x3 = jiggle_default(random), l += x3 * x3;
            if (y2 === 0)
              y2 = jiggle_default(random), l += y2 * y2;
            l = (r - (l = Math.sqrt(l))) / l * strength;
            node.vx += (x3 *= l) * (r = (rj *= rj) / (ri2 + rj));
            node.vy += (y2 *= l) * r;
            data.vx -= x3 * (r = 1 - r);
            data.vy -= y2 * r;
          }
        }
        return;
      }
      return x0 > xi + r || x1 < xi - r || y0 > yi + r || y1 < yi - r;
    }
  }
  function prepare(quad) {
    if (quad.data)
      return quad.r = radii[quad.data.index];
    for (var i2 = quad.r = 0;i2 < 4; ++i2) {
      if (quad[i2] && quad[i2].r > quad.r) {
        quad.r = quad[i2].r;
      }
    }
  }
  function initialize() {
    if (!nodes)
      return;
    var i2, n = nodes.length, node;
    radii = new Array(n);
    for (i2 = 0;i2 < n; ++i2)
      node = nodes[i2], radii[node.index] = +radius(node, i2, nodes);
  }
  force.initialize = function(_nodes, _random) {
    nodes = _nodes;
    random = _random;
    initialize();
  };
  force.iterations = function(_) {
    return arguments.length ? (iterations = +_, force) : iterations;
  };
  force.strength = function(_) {
    return arguments.length ? (strength = +_, force) : strength;
  };
  force.radius = function(_) {
    return arguments.length ? (radius = typeof _ === "function" ? _ : constant_default(+_), initialize(), force) : radius;
  };
  return force;
}
// ../../../node_modules/d3-force/src/link.js
function index(d) {
  return d.index;
}
function find(nodeById, nodeId) {
  var node = nodeById.get(nodeId);
  if (!node)
    throw new Error("node not found: " + nodeId);
  return node;
}
function link_default(links) {
  var id = index, strength = defaultStrength, strengths, distance = constant_default(30), distances, nodes, count, bias, random, iterations = 1;
  if (links == null)
    links = [];
  function defaultStrength(link) {
    return 1 / Math.min(count[link.source.index], count[link.target.index]);
  }
  function force(alpha) {
    for (var k = 0, n = links.length;k < iterations; ++k) {
      for (var i2 = 0, link, source, target, x3, y2, l, b;i2 < n; ++i2) {
        link = links[i2], source = link.source, target = link.target;
        x3 = target.x + target.vx - source.x - source.vx || jiggle_default(random);
        y2 = target.y + target.vy - source.y - source.vy || jiggle_default(random);
        l = Math.sqrt(x3 * x3 + y2 * y2);
        l = (l - distances[i2]) / l * alpha * strengths[i2];
        x3 *= l, y2 *= l;
        target.vx -= x3 * (b = bias[i2]);
        target.vy -= y2 * b;
        source.vx += x3 * (b = 1 - b);
        source.vy += y2 * b;
      }
    }
  }
  function initialize() {
    if (!nodes)
      return;
    var i2, n = nodes.length, m = links.length, nodeById = new Map(nodes.map((d, i3) => [id(d, i3, nodes), d])), link;
    for (i2 = 0, count = new Array(n);i2 < m; ++i2) {
      link = links[i2], link.index = i2;
      if (typeof link.source !== "object")
        link.source = find(nodeById, link.source);
      if (typeof link.target !== "object")
        link.target = find(nodeById, link.target);
      count[link.source.index] = (count[link.source.index] || 0) + 1;
      count[link.target.index] = (count[link.target.index] || 0) + 1;
    }
    for (i2 = 0, bias = new Array(m);i2 < m; ++i2) {
      link = links[i2], bias[i2] = count[link.source.index] / (count[link.source.index] + count[link.target.index]);
    }
    strengths = new Array(m), initializeStrength();
    distances = new Array(m), initializeDistance();
  }
  function initializeStrength() {
    if (!nodes)
      return;
    for (var i2 = 0, n = links.length;i2 < n; ++i2) {
      strengths[i2] = +strength(links[i2], i2, links);
    }
  }
  function initializeDistance() {
    if (!nodes)
      return;
    for (var i2 = 0, n = links.length;i2 < n; ++i2) {
      distances[i2] = +distance(links[i2], i2, links);
    }
  }
  force.initialize = function(_nodes, _random) {
    nodes = _nodes;
    random = _random;
    initialize();
  };
  force.links = function(_) {
    return arguments.length ? (links = _, initialize(), force) : links;
  };
  force.id = function(_) {
    return arguments.length ? (id = _, force) : id;
  };
  force.iterations = function(_) {
    return arguments.length ? (iterations = +_, force) : iterations;
  };
  force.strength = function(_) {
    return arguments.length ? (strength = typeof _ === "function" ? _ : constant_default(+_), initializeStrength(), force) : strength;
  };
  force.distance = function(_) {
    return arguments.length ? (distance = typeof _ === "function" ? _ : constant_default(+_), initializeDistance(), force) : distance;
  };
  return force;
}
// ../../../node_modules/d3-dispatch/src/dispatch.js
var noop = { value: () => {} };
function dispatch() {
  for (var i2 = 0, n = arguments.length, _ = {}, t;i2 < n; ++i2) {
    if (!(t = arguments[i2] + "") || t in _ || /[\s.]/.test(t))
      throw new Error("illegal type: " + t);
    _[t] = [];
  }
  return new Dispatch(_);
}
function Dispatch(_) {
  this._ = _;
}
function parseTypenames(typenames, types) {
  return typenames.trim().split(/^|\s+/).map(function(t) {
    var name = "", i2 = t.indexOf(".");
    if (i2 >= 0)
      name = t.slice(i2 + 1), t = t.slice(0, i2);
    if (t && !types.hasOwnProperty(t))
      throw new Error("unknown type: " + t);
    return { type: t, name };
  });
}
Dispatch.prototype = dispatch.prototype = {
  constructor: Dispatch,
  on: function(typename, callback) {
    var _ = this._, T = parseTypenames(typename + "", _), t, i2 = -1, n = T.length;
    if (arguments.length < 2) {
      while (++i2 < n)
        if ((t = (typename = T[i2]).type) && (t = get(_[t], typename.name)))
          return t;
      return;
    }
    if (callback != null && typeof callback !== "function")
      throw new Error("invalid callback: " + callback);
    while (++i2 < n) {
      if (t = (typename = T[i2]).type)
        _[t] = set(_[t], typename.name, callback);
      else if (callback == null)
        for (t in _)
          _[t] = set(_[t], typename.name, null);
    }
    return this;
  },
  copy: function() {
    var copy = {}, _ = this._;
    for (var t in _)
      copy[t] = _[t].slice();
    return new Dispatch(copy);
  },
  call: function(type, that) {
    if ((n = arguments.length - 2) > 0)
      for (var args = new Array(n), i2 = 0, n, t;i2 < n; ++i2)
        args[i2] = arguments[i2 + 2];
    if (!this._.hasOwnProperty(type))
      throw new Error("unknown type: " + type);
    for (t = this._[type], i2 = 0, n = t.length;i2 < n; ++i2)
      t[i2].value.apply(that, args);
  },
  apply: function(type, that, args) {
    if (!this._.hasOwnProperty(type))
      throw new Error("unknown type: " + type);
    for (var t = this._[type], i2 = 0, n = t.length;i2 < n; ++i2)
      t[i2].value.apply(that, args);
  }
};
function get(type, name) {
  for (var i2 = 0, n = type.length, c;i2 < n; ++i2) {
    if ((c = type[i2]).name === name) {
      return c.value;
    }
  }
}
function set(type, name, callback) {
  for (var i2 = 0, n = type.length;i2 < n; ++i2) {
    if (type[i2].name === name) {
      type[i2] = noop, type = type.slice(0, i2).concat(type.slice(i2 + 1));
      break;
    }
  }
  if (callback != null)
    type.push({ name, value: callback });
  return type;
}
var dispatch_default = dispatch;
// ../../../node_modules/d3-timer/src/timer.js
var frame = 0;
var timeout = 0;
var interval = 0;
var pokeDelay = 1000;
var taskHead;
var taskTail;
var clockLast = 0;
var clockNow = 0;
var clockSkew = 0;
var clock = typeof performance === "object" && performance.now ? performance : Date;
var setFrame = typeof window === "object" && window.requestAnimationFrame ? window.requestAnimationFrame.bind(window) : function(f) {
  setTimeout(f, 17);
};
function now() {
  return clockNow || (setFrame(clearNow), clockNow = clock.now() + clockSkew);
}
function clearNow() {
  clockNow = 0;
}
function Timer() {
  this._call = this._time = this._next = null;
}
Timer.prototype = timer.prototype = {
  constructor: Timer,
  restart: function(callback, delay, time) {
    if (typeof callback !== "function")
      throw new TypeError("callback is not a function");
    time = (time == null ? now() : +time) + (delay == null ? 0 : +delay);
    if (!this._next && taskTail !== this) {
      if (taskTail)
        taskTail._next = this;
      else
        taskHead = this;
      taskTail = this;
    }
    this._call = callback;
    this._time = time;
    sleep();
  },
  stop: function() {
    if (this._call) {
      this._call = null;
      this._time = Infinity;
      sleep();
    }
  }
};
function timer(callback, delay, time) {
  var t = new Timer;
  t.restart(callback, delay, time);
  return t;
}
function timerFlush() {
  now();
  ++frame;
  var t = taskHead, e;
  while (t) {
    if ((e = clockNow - t._time) >= 0)
      t._call.call(undefined, e);
    t = t._next;
  }
  --frame;
}
function wake() {
  clockNow = (clockLast = clock.now()) + clockSkew;
  frame = timeout = 0;
  try {
    timerFlush();
  } finally {
    frame = 0;
    nap();
    clockNow = 0;
  }
}
function poke() {
  var now2 = clock.now(), delay = now2 - clockLast;
  if (delay > pokeDelay)
    clockSkew -= delay, clockLast = now2;
}
function nap() {
  var t0, t1 = taskHead, t2, time = Infinity;
  while (t1) {
    if (t1._call) {
      if (time > t1._time)
        time = t1._time;
      t0 = t1, t1 = t1._next;
    } else {
      t2 = t1._next, t1._next = null;
      t1 = t0 ? t0._next = t2 : taskHead = t2;
    }
  }
  taskTail = t0;
  sleep(time);
}
function sleep(time) {
  if (frame)
    return;
  if (timeout)
    timeout = clearTimeout(timeout);
  var delay = time - clockNow;
  if (delay > 24) {
    if (time < Infinity)
      timeout = setTimeout(wake, time - clock.now() - clockSkew);
    if (interval)
      interval = clearInterval(interval);
  } else {
    if (!interval)
      clockLast = clock.now(), interval = setInterval(poke, pokeDelay);
    frame = 1, setFrame(wake);
  }
}
// ../../../node_modules/d3-force/src/lcg.js
var a = 1664525;
var c = 1013904223;
var m = 4294967296;
function lcg_default() {
  let s = 1;
  return () => (s = (a * s + c) % m) / m;
}

// ../../../node_modules/d3-force/src/simulation.js
function x3(d) {
  return d.x;
}
function y2(d) {
  return d.y;
}
var initialRadius = 10;
var initialAngle = Math.PI * (3 - Math.sqrt(5));
function simulation_default(nodes) {
  var simulation, alpha = 1, alphaMin = 0.001, alphaDecay = 1 - Math.pow(alphaMin, 1 / 300), alphaTarget = 0, velocityDecay = 0.6, forces = new Map, stepper = timer(step), event = dispatch_default("tick", "end"), random = lcg_default();
  if (nodes == null)
    nodes = [];
  function step() {
    tick();
    event.call("tick", simulation);
    if (alpha < alphaMin) {
      stepper.stop();
      event.call("end", simulation);
    }
  }
  function tick(iterations) {
    var i2, n = nodes.length, node;
    if (iterations === undefined)
      iterations = 1;
    for (var k = 0;k < iterations; ++k) {
      alpha += (alphaTarget - alpha) * alphaDecay;
      forces.forEach(function(force) {
        force(alpha);
      });
      for (i2 = 0;i2 < n; ++i2) {
        node = nodes[i2];
        if (node.fx == null)
          node.x += node.vx *= velocityDecay;
        else
          node.x = node.fx, node.vx = 0;
        if (node.fy == null)
          node.y += node.vy *= velocityDecay;
        else
          node.y = node.fy, node.vy = 0;
      }
    }
    return simulation;
  }
  function initializeNodes() {
    for (var i2 = 0, n = nodes.length, node;i2 < n; ++i2) {
      node = nodes[i2], node.index = i2;
      if (node.fx != null)
        node.x = node.fx;
      if (node.fy != null)
        node.y = node.fy;
      if (isNaN(node.x) || isNaN(node.y)) {
        var radius = initialRadius * Math.sqrt(0.5 + i2), angle = i2 * initialAngle;
        node.x = radius * Math.cos(angle);
        node.y = radius * Math.sin(angle);
      }
      if (isNaN(node.vx) || isNaN(node.vy)) {
        node.vx = node.vy = 0;
      }
    }
  }
  function initializeForce(force) {
    if (force.initialize)
      force.initialize(nodes, random);
    return force;
  }
  initializeNodes();
  return simulation = {
    tick,
    restart: function() {
      return stepper.restart(step), simulation;
    },
    stop: function() {
      return stepper.stop(), simulation;
    },
    nodes: function(_) {
      return arguments.length ? (nodes = _, initializeNodes(), forces.forEach(initializeForce), simulation) : nodes;
    },
    alpha: function(_) {
      return arguments.length ? (alpha = +_, simulation) : alpha;
    },
    alphaMin: function(_) {
      return arguments.length ? (alphaMin = +_, simulation) : alphaMin;
    },
    alphaDecay: function(_) {
      return arguments.length ? (alphaDecay = +_, simulation) : +alphaDecay;
    },
    alphaTarget: function(_) {
      return arguments.length ? (alphaTarget = +_, simulation) : alphaTarget;
    },
    velocityDecay: function(_) {
      return arguments.length ? (velocityDecay = 1 - _, simulation) : 1 - velocityDecay;
    },
    randomSource: function(_) {
      return arguments.length ? (random = _, forces.forEach(initializeForce), simulation) : random;
    },
    force: function(name, _) {
      return arguments.length > 1 ? (_ == null ? forces.delete(name) : forces.set(name, initializeForce(_)), simulation) : forces.get(name);
    },
    find: function(x4, y3, radius) {
      var i2 = 0, n = nodes.length, dx, dy, d2, node, closest;
      if (radius == null)
        radius = Infinity;
      else
        radius *= radius;
      for (i2 = 0;i2 < n; ++i2) {
        node = nodes[i2];
        dx = x4 - node.x;
        dy = y3 - node.y;
        d2 = dx * dx + dy * dy;
        if (d2 < radius)
          closest = node, radius = d2;
      }
      return closest;
    },
    on: function(name, _) {
      return arguments.length > 1 ? (event.on(name, _), simulation) : event.on(name);
    }
  };
}

// ../../../node_modules/d3-force/src/manyBody.js
function manyBody_default() {
  var nodes, node, random, alpha, strength = constant_default(-30), strengths, distanceMin2 = 1, distanceMax2 = Infinity, theta2 = 0.81;
  function force(_) {
    var i2, n = nodes.length, tree = quadtree(nodes, x3, y2).visitAfter(accumulate);
    for (alpha = _, i2 = 0;i2 < n; ++i2)
      node = nodes[i2], tree.visit(apply);
  }
  function initialize() {
    if (!nodes)
      return;
    var i2, n = nodes.length, node2;
    strengths = new Array(n);
    for (i2 = 0;i2 < n; ++i2)
      node2 = nodes[i2], strengths[node2.index] = +strength(node2, i2, nodes);
  }
  function accumulate(quad) {
    var strength2 = 0, q, c2, weight = 0, x4, y3, i2;
    if (quad.length) {
      for (x4 = y3 = i2 = 0;i2 < 4; ++i2) {
        if ((q = quad[i2]) && (c2 = Math.abs(q.value))) {
          strength2 += q.value, weight += c2, x4 += c2 * q.x, y3 += c2 * q.y;
        }
      }
      quad.x = x4 / weight;
      quad.y = y3 / weight;
    } else {
      q = quad;
      q.x = q.data.x;
      q.y = q.data.y;
      do
        strength2 += strengths[q.data.index];
      while (q = q.next);
    }
    quad.value = strength2;
  }
  function apply(quad, x1, _, x22) {
    if (!quad.value)
      return true;
    var x4 = quad.x - node.x, y3 = quad.y - node.y, w = x22 - x1, l = x4 * x4 + y3 * y3;
    if (w * w / theta2 < l) {
      if (l < distanceMax2) {
        if (x4 === 0)
          x4 = jiggle_default(random), l += x4 * x4;
        if (y3 === 0)
          y3 = jiggle_default(random), l += y3 * y3;
        if (l < distanceMin2)
          l = Math.sqrt(distanceMin2 * l);
        node.vx += x4 * quad.value * alpha / l;
        node.vy += y3 * quad.value * alpha / l;
      }
      return true;
    } else if (quad.length || l >= distanceMax2)
      return;
    if (quad.data !== node || quad.next) {
      if (x4 === 0)
        x4 = jiggle_default(random), l += x4 * x4;
      if (y3 === 0)
        y3 = jiggle_default(random), l += y3 * y3;
      if (l < distanceMin2)
        l = Math.sqrt(distanceMin2 * l);
    }
    do
      if (quad.data !== node) {
        w = strengths[quad.data.index] * alpha / l;
        node.vx += x4 * w;
        node.vy += y3 * w;
      }
    while (quad = quad.next);
  }
  force.initialize = function(_nodes, _random) {
    nodes = _nodes;
    random = _random;
    initialize();
  };
  force.strength = function(_) {
    return arguments.length ? (strength = typeof _ === "function" ? _ : constant_default(+_), initialize(), force) : strength;
  };
  force.distanceMin = function(_) {
    return arguments.length ? (distanceMin2 = _ * _, force) : Math.sqrt(distanceMin2);
  };
  force.distanceMax = function(_) {
    return arguments.length ? (distanceMax2 = _ * _, force) : Math.sqrt(distanceMax2);
  };
  force.theta = function(_) {
    return arguments.length ? (theta2 = _ * _, force) : Math.sqrt(theta2);
  };
  return force;
}
// ../../../node_modules/d3-force/src/radial.js
function radial_default(radius, x4, y3) {
  var nodes, strength = constant_default(0.1), strengths, radiuses;
  if (typeof radius !== "function")
    radius = constant_default(+radius);
  if (x4 == null)
    x4 = 0;
  if (y3 == null)
    y3 = 0;
  function force(alpha) {
    for (var i2 = 0, n = nodes.length;i2 < n; ++i2) {
      var node = nodes[i2], dx = node.x - x4 || 0.000001, dy = node.y - y3 || 0.000001, r = Math.sqrt(dx * dx + dy * dy), k = (radiuses[i2] - r) * strengths[i2] * alpha / r;
      node.vx += dx * k;
      node.vy += dy * k;
    }
  }
  function initialize() {
    if (!nodes)
      return;
    var i2, n = nodes.length;
    strengths = new Array(n);
    radiuses = new Array(n);
    for (i2 = 0;i2 < n; ++i2) {
      radiuses[i2] = +radius(nodes[i2], i2, nodes);
      strengths[i2] = isNaN(radiuses[i2]) ? 0 : +strength(nodes[i2], i2, nodes);
    }
  }
  force.initialize = function(_) {
    nodes = _, initialize();
  };
  force.strength = function(_) {
    return arguments.length ? (strength = typeof _ === "function" ? _ : constant_default(+_), initialize(), force) : strength;
  };
  force.radius = function(_) {
    return arguments.length ? (radius = typeof _ === "function" ? _ : constant_default(+_), initialize(), force) : radius;
  };
  force.x = function(_) {
    return arguments.length ? (x4 = +_, force) : x4;
  };
  force.y = function(_) {
    return arguments.length ? (y3 = +_, force) : y3;
  };
  return force;
}
// starmap/time-axis.ts
var LEAD_IN = 0.06;
var recForRatio = (ratio) => LEAD_IN + (1 - LEAD_IN) * clamp(ratio, 0, 1);
function computeRecency(nodes) {
  const known = nodes.map((n) => typeof n.timestamp === "number" && Number.isFinite(n.timestamp) ? Number(n.timestamp) : null).filter((v) => v !== null);
  const minTs = known.length ? Math.min(...known) : null;
  const maxTs = known.length ? Math.max(...known) : null;
  const timed = minTs !== null && maxTs !== null && maxTs > minTs;
  const ordered = [...nodes].sort((a2, b) => {
    const at = typeof a2.timestamp === "number" ? a2.timestamp : Infinity;
    const bt = typeof b.timestamp === "number" ? b.timestamp : Infinity;
    return at === bt ? a2.id.localeCompare(b.id) : at - bt;
  });
  const ordRatio = new Map(ordered.map((n, i2) => [n.id, ordered.length > 1 ? i2 / (ordered.length - 1) : 0]));
  const rec = new Map;
  for (const n of nodes) {
    const ratio = timed && typeof n.timestamp === "number" && minTs !== null && maxTs !== null ? (Number(n.timestamp) - minTs) / (maxTs - minTs) : ordRatio.get(n.id) ?? 0;
    rec.set(n.id, recForRatio(ratio));
  }
  return { maxTs, minTs, rec, timed };
}
function buildTimeAxis(graph, bucketCount = 48) {
  const { maxTs, minTs, rec, timed } = computeRecency(graph.nodes);
  const n = Math.max(1, bucketCount);
  const buckets = Array.from({ length: n }, () => ({ memory: 0, skill: 0, total: 0 }));
  for (const node of graph.nodes) {
    const r = rec.get(node.id) ?? 0;
    const idx = clamp(Math.floor(r * n), 0, n - 1);
    const b = buckets[idx];
    b.total += 1;
    if (node.kind === "memory") {
      b.memory += 1;
    } else {
      b.skill += 1;
    }
  }
  const maxTotal = buckets.reduce((m2, b) => Math.max(m2, b.total), 0);
  return { buckets, maxTotal, maxTs, minTs, size: graph.nodes.length, timed };
}
function dateAtReveal(axis, reveal) {
  if (!axis.timed || axis.minTs === null || axis.maxTs === null) {
    return null;
  }
  return Math.round(axis.minTs + clamp(reveal, 0, 1) * (axis.maxTs - axis.minTs));
}

// starmap/simulation.ts
var TAU = Math.PI * 2;
function sectorAngleOf(n) {
  const i2 = layerIndexOf(n);
  if (i2 < 0)
    return hash(n.id) % 3600 / 3600 * TAU;
  const jitter = (hash(n.id) % 1000 / 1000 - 0.5) * (TAU / HYATLAS_LAYERS.length) * 0.7;
  return -Math.PI / 2 + i2 / HYATLAS_LAYERS.length * TAU + jitter;
}
var DAY = 86400;
var CLUSTER_SIZE = 5;
var RING_CORE = radiusForRecency(recForRatio(0));
var RING_BAND = (radiusForRecency(recForRatio(1)) - RING_CORE) / RING_STEPS;
var ringRadius = (i2) => RING_CORE + i2 * RING_BAND;
var placeRadius = (i2, id) => {
  const outer = ringRadius(i2);
  const inner = i2 > 0 ? ringRadius(i2 - 1) : RING_CORE - RING_BAND * 0.5;
  const h = hash(id) % 1000 / 1000;
  return outer - (0.15 + 0.7 * h) * (outer - inner);
};
var UNITS = [
  { kind: "day", step: 1 },
  { kind: "day", step: 2 },
  { kind: "day", step: 7 },
  { kind: "day", step: 14 },
  { kind: "month", step: 1 },
  { kind: "month", step: 2 },
  { kind: "month", step: 3 },
  { kind: "month", step: 6 },
  { kind: "month", step: 12 }
];
function bucketStart(ts, { kind, step }) {
  if (kind === "day") {
    const period = step * DAY;
    return Math.floor(ts / period) * period;
  }
  const d = new Date(ts * 1000);
  d.setUTCHours(0, 0, 0, 0);
  const absMonth = Math.floor((d.getUTCFullYear() * 12 + d.getUTCMonth()) / step) * step;
  d.setUTCFullYear(Math.floor(absMonth / 12), absMonth % 12, 1);
  return Math.floor(d.getTime() / 1000);
}
var populatedStarts = (stamps, u) => [...new Set(stamps.map((t) => bucketStart(t, u)))].sort((a2, b) => a2 - b);
function chooseUnit(stamps, spanDays) {
  const target = clamp(Math.round(4 + Math.log2(Math.max(1, spanDays / 60))), 5, 12);
  let best = UNITS[0];
  let bestScore = Infinity;
  for (const u of UNITS) {
    const count = populatedStarts(stamps, u).length;
    if (!count) {
      continue;
    }
    const score = Math.abs(count - target) + (count > target ? 0.5 : 0);
    if (score < bestScore) {
      bestScore = score;
      best = u;
    }
  }
  return best;
}
function bucketLabel(ts, { kind, step }) {
  if (kind === "day") {
    return formatDate(ts);
  }
  try {
    const d = new Date(ts * 1000);
    return step >= 12 ? String(d.getUTCFullYear()) : d.toLocaleDateString(undefined, { month: "short", timeZone: "UTC", year: "numeric" });
  } catch {
    return formatDate(ts);
  }
}
function evenLayout(recById, minTs, maxTs, timed) {
  const rings = Array.from({ length: RING_STEPS + 1 }, (_, i2) => ({
    label: timed && minTs !== null && maxTs !== null ? formatDate(Math.round(minTs + (maxTs - minTs) * (i2 / RING_STEPS))) : null,
    r: ringRadius(i2),
    ratio: recForRatio(i2 / RING_STEPS)
  }));
  const capRing = (rec) => {
    for (let i2 = 0;i2 < rings.length; i2 += 1) {
      if ((rings[i2]?.ratio ?? 1) >= rec - 0.001) {
        return i2;
      }
    }
    return rings.length - 1;
  };
  return {
    index: (n) => capRing(recById.get(n.id) ?? 0),
    rec: (n) => recById.get(n.id) ?? 0,
    rings,
    tr: (n) => radiusForRecency(recById.get(n.id) ?? 0)
  };
}
function buildLayout(graph, recById, minTs, maxTs, timed) {
  const stamps = graph.nodes.map((n) => Number(n.timestamp)).filter(Number.isFinite);
  if (!(timed && minTs !== null && maxTs !== null && maxTs > minTs && stamps.length)) {
    return evenLayout(recById, minTs, maxTs, timed);
  }
  const span = maxTs - minTs;
  const unit = chooseUnit(stamps, span / DAY);
  const starts = populatedStarts(stamps, unit);
  if (starts.length < 2) {
    return evenLayout(recById, minTs, maxTs, timed);
  }
  const indexOfStart = new Map(starts.map((s, i2) => [s, i2]));
  const last = Math.max(1, starts.length - 1);
  const rings = starts.map((s, i2) => ({
    label: bucketLabel(s, unit),
    r: ringRadius(i2),
    ratio: recForRatio(i2 / last)
  }));
  const indexFor = (n) => {
    const ts = Number(n.timestamp);
    return Number.isFinite(ts) ? indexOfStart.get(bucketStart(ts, unit)) ?? starts.length - 1 : starts.length - 1;
  };
  const buckets = starts.map(() => []);
  for (const n of graph.nodes) {
    buckets[indexFor(n)].push(n);
  }
  const tsOf = (n) => Number.isFinite(Number(n.timestamp)) ? Number(n.timestamp) : Infinity;
  const recByNode = new Map;
  buckets.forEach((bucket, i2) => {
    bucket.sort((a2, b) => tsOf(a2) === tsOf(b) ? a2.id.localeCompare(b.id) : tsOf(a2) - tsOf(b));
    const hi = rings[i2].ratio;
    const lo = i2 > 0 ? rings[i2 - 1].ratio : 0;
    const m2 = bucket.length;
    const clusters = Math.max(1, Math.round(m2 / CLUSTER_SIZE));
    bucket.forEach((n, k) => {
      const c2 = Math.min(clusters - 1, Math.floor(k / m2 * clusters));
      const jitter = (hash(n.id) % 100 / 100 - 0.5) * (0.5 / clusters);
      const f = clamp((c2 + 1) / clusters + jitter, 0.02, 1);
      recByNode.set(n.id, lo + f * (hi - lo));
    });
  });
  return {
    index: indexFor,
    rec: (n) => recByNode.get(n.id) ?? 0,
    rings,
    tr: (n) => placeRadius(indexFor(n), n.id)
  };
}
function buildSimulation(graph, onTick) {
  const { maxTs, minTs, rec: recById, timed } = computeRecency(graph.nodes);
  const { index: index2, rec: recOf, rings, tr: trOf } = buildLayout(graph, recById, minTs, maxTs, timed);
  const nodes = graph.nodes.map((n) => {
    const rec = recOf(n);
    const tr = trOf(n);
    const angle = sectorAngleOf(n);
    return { ...n, outerRingIndex: index2(n), rec, tr, vx: 0, vy: 0, x: Math.cos(angle) * tr, y: Math.sin(angle) * tr };
  });
  const bandBounds = new Map;
  for (const n of nodes) {
    const idx = n.outerRingIndex;
    const hi = Math.max(14, (rings[idx]?.r ?? rings[rings.length - 1]?.r ?? 340) - 3);
    const lo = Math.max(rings[0].r * 0.45, (rings[idx - 1]?.r ?? 0) + 3);
    bandBounds.set(n, [Math.min(lo, hi - 1), hi]);
  }
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const links = graph.edges.filter((e) => byId.has(e.source) && byId.has(e.target)).map((e) => ({ source: e.source, target: e.target }));
  const sim = simulation_default(nodes).alphaDecay(0.05).velocityDecay(0.62).force("charge", manyBody_default().strength(-12)).force("link", link_default(links).id((n) => n.id).distance(26).strength(0.06)).force("collide", collide_default().radius((n) => nodeRadius(n) + 2).iterations(2)).force("sector", (alpha) => {
    for (const n of nodes) {
      const r = Math.hypot(n.x, n.y);
      if (r < 0.001)
        continue;
      const cur = Math.atan2(n.y, n.x);
      const target = sectorAngleOf(n);
      const d = Math.atan2(Math.sin(target - cur), Math.cos(target - cur));
      const next = cur + d * 0.4;
      n.vx += (Math.cos(next) * r - n.x) * 0.9 * alpha;
      n.vy += (Math.sin(next) * r - n.y) * 0.9 * alpha;
    }
  }).force("radial", radial_default((n) => n.tr, 0, 0).strength(0.92)).on("tick", () => {
    for (const n of nodes) {
      const band = bandBounds.get(n);
      if (!band)
        continue;
      const r = Math.hypot(n.x, n.y) || 0.000001;
      if (r > band[1]) {
        n.x = n.x / r * band[1];
        n.y = n.y / r * band[1];
      } else if (r < band[0]) {
        n.x = n.x / r * band[0];
        n.y = n.y / r * band[0];
      }
    }
    onTick();
  });
  return { byId, links, nodes, rings, sim };
}

// starmap/timeline.tsx
import { memo, useCallback, useEffect as useEffect2, useMemo, useRef } from "react";

// shim/components/ui/codicon.tsx

function Codicon({ name, size = "0.9rem" }) {
  const glyph = name === "debug-pause" ? "⏸" : name === "triangle-right" ? "▶" : "•";
  return /* @__PURE__ */ jsxDEV7("span", {
    style: { display: "inline-block", fontSize: size, lineHeight: 1 },
    children: glyph
  }, undefined, false, undefined, this);
}

// starmap/timeline.tsx

var ACTIVE_MARKER_CLASS = "opacity-100";
var INACTIVE_MARKER_CLASS = "opacity-30";
var MAX_STARS_PER_BUCKET = 7;
function rng(seed) {
  let a2 = seed >>> 0;
  return () => {
    a2 += 1831565813;
    let t = a2;
    t = Math.imul(t ^ t >>> 15, t | 1);
    t ^= t + Math.imul(t ^ t >>> 7, t | 61);
    return ((t ^ t >>> 14) >>> 0) / 4294967296;
  };
}
function buildStars(axis) {
  const n = Math.max(1, axis.buckets.length);
  const stars = [];
  axis.buckets.forEach((b, i2) => {
    if (b.total === 0) {
      return;
    }
    const intensity = axis.maxTotal > 0 ? b.total / axis.maxTotal : 0;
    const count = Math.max(1, Math.round(Math.sqrt(intensity) * MAX_STARS_PER_BUCKET));
    const skillCount = Math.round(b.skill / b.total * count);
    const r = rng(i2 * 9973 + 7);
    const slot = 1 / n;
    const center = (i2 + 0.5) / n;
    for (let s = 0;s < count; s++) {
      const jitter = (r() - 0.5) * slot * 0.9;
      const vertical = (r() + r()) / 2;
      stars.push({
        delay: r() * 3,
        duration: 2.4 + r() * 2.6,
        kind: s < skillCount ? "skill" : "memory",
        leftPct: Math.max(0, Math.min(1, center + jitter)) * 100,
        opacity: 0.5 + r() * 0.5,
        size: 2 + Math.round(r() * r() * 2.2),
        topPct: 12 + vertical * 76
      });
    }
  });
  return stars;
}
var Timeline = memo(function Timeline2({
  axis,
  memoryColor = "var(--theme-secondary)",
  onScrub,
  onTogglePlay,
  playing,
  revealStore,
  ringStops = []
}) {
  const trackRef = useRef(null);
  const draggingRef = useRef(false);
  const markerRefs = useRef([]);
  const stars = useMemo(() => buildStars(axis), [axis]);
  const syncReveal = useCallback((value) => {
    const reveal = Math.max(0, Math.min(1, value));
    const track = trackRef.current;
    if (track) {
      track.style.setProperty("--starmap-reveal", String(reveal));
      track.setAttribute("aria-valuenow", String(Math.round(reveal * 100)));
    }
    ringStops.forEach((stop, i2) => {
      const el = markerRefs.current[i2];
      if (!el) {
        return;
      }
      const active = stop <= reveal;
      el.classList.toggle(ACTIVE_MARKER_CLASS, active);
      el.classList.toggle(INACTIVE_MARKER_CLASS, !active);
    });
  }, [ringStops]);
  useEffect2(() => revealStore.subscribe(syncReveal), [revealStore, syncReveal]);
  useEffect2(() => {
    markerRefs.current.length = ringStops.length;
    syncReveal(revealStore.get());
  }, [revealStore, ringStops.length, syncReveal]);
  const ratioAt = (clientX) => {
    const rect = trackRef.current?.getBoundingClientRect();
    if (!rect || rect.width === 0) {
      return revealStore.get();
    }
    return Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
  };
  const onPointerDown = (e) => {
    draggingRef.current = true;
    e.currentTarget.setPointerCapture(e.pointerId);
    onScrub(ratioAt(e.clientX));
  };
  const onPointerMove = (e) => {
    if (draggingRef.current) {
      onScrub(ratioAt(e.clientX));
    }
  };
  const onPointerUp = (e) => {
    draggingRef.current = false;
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
  };
  const colorFor = (kind) => kind === "skill" ? "var(--theme-primary)" : memoryColor;
  return /* @__PURE__ */ jsxDEV8("div", {
    className: "pointer-events-auto flex w-[28rem] max-w-full items-center gap-3 [-webkit-app-region:no-drag]",
    children: [
      /* @__PURE__ */ jsxDEV8("style", {
        children: "@keyframes starmap-twinkle{0%,100%{opacity:var(--o,1)}50%{opacity:calc(var(--o,1) * 0.35)}}"
      }, undefined, false, undefined, this),
      /* @__PURE__ */ jsxDEV8("button", {
        "aria-label": playing ? "Pause" : "Play timeline",
        className: "flex size-5 shrink-0 items-center justify-center text-foreground/75 transition-colors hover:text-foreground",
        onClick: onTogglePlay,
        type: "button",
        children: /* @__PURE__ */ jsxDEV8(Codicon, {
          name: playing ? "debug-pause" : "triangle-right",
          size: playing ? "0.8rem" : "0.95rem"
        }, undefined, false, undefined, this)
      }, undefined, false, undefined, this),
      /* @__PURE__ */ jsxDEV8("div", {
        "aria-label": "Timeline scrubber",
        "aria-valuemax": 100,
        "aria-valuemin": 0,
        "aria-valuenow": Math.round(revealStore.get() * 100),
        className: "relative h-7 min-w-0 flex-1 cursor-pointer select-none touch-none",
        onPointerDown,
        onPointerMove,
        onPointerUp,
        ref: trackRef,
        role: "slider",
        style: { "--starmap-reveal": revealStore.get() },
        tabIndex: 0,
        children: [
          /* @__PURE__ */ jsxDEV8("div", {
            "aria-hidden": true,
            className: "pointer-events-none absolute inset-x-0 top-1/2 -translate-y-1/2 border-t border-dashed border-foreground/5"
          }, undefined, false, undefined, this),
          /* @__PURE__ */ jsxDEV8("div", {
            "aria-hidden": true,
            className: "absolute inset-0",
            children: stars.map((star, i2) => /* @__PURE__ */ jsxDEV8("div", {
              className: "absolute -translate-x-1/2 -translate-y-1/2 rounded-full",
              style: {
                backgroundColor: colorFor(star.kind),
                height: star.size,
                left: `${star.leftPct}%`,
                opacity: 0.22,
                top: `${star.topPct}%`,
                width: star.size
              }
            }, i2, false, undefined, this))
          }, undefined, false, undefined, this),
          /* @__PURE__ */ jsxDEV8("div", {
            "aria-hidden": true,
            className: "absolute inset-0",
            style: { clipPath: "inset(0 calc((1 - var(--starmap-reveal, 1)) * 100%) 0 0)" },
            children: stars.map((star, i2) => {
              const color = colorFor(star.kind);
              return /* @__PURE__ */ jsxDEV8("div", {
                className: "absolute -translate-x-1/2 -translate-y-1/2 rounded-full",
                style: {
                  "--o": star.opacity,
                  animation: `starmap-twinkle ${star.duration}s ease-in-out ${star.delay}s infinite`,
                  backgroundColor: color,
                  height: star.size,
                  left: `${star.leftPct}%`,
                  opacity: star.opacity,
                  top: `${star.topPct}%`,
                  width: star.size
                }
              }, i2, false, undefined, this);
            })
          }, undefined, false, undefined, this),
          /* @__PURE__ */ jsxDEV8("div", {
            "aria-hidden": true,
            className: "absolute inset-0",
            children: ringStops.map((stop, i2) => /* @__PURE__ */ jsxDEV8("div", {
              className: `pointer-events-none absolute top-1/2 size-1 -translate-x-1/2 -translate-y-1/2 rounded-full bg-[var(--theme-primary)] ${INACTIVE_MARKER_CLASS}`,
              ref: (el) => {
                if (el) {
                  markerRefs.current[i2] = el;
                }
              },
              style: { left: `${stop * 100}%` }
            }, i2, false, undefined, this))
          }, undefined, false, undefined, this),
          /* @__PURE__ */ jsxDEV8("div", {
            "aria-hidden": true,
            className: "pointer-events-none absolute inset-y-0 w-0.5 -translate-x-1/2 bg-foreground",
            style: { left: "calc(var(--starmap-reveal, 1) * 100%)" }
          }, undefined, false, undefined, this)
        ]
      }, undefined, true, undefined, this)
    ]
  }, undefined, true, undefined, this);
});

// starmap/star-map.tsx

function LayerGlyph({ color, shape }) {
  return /* @__PURE__ */ jsxDEV9("svg", {
    className: "size-2 shrink-0",
    viewBox: "0 0 8 8",
    children: shape === "circle" ? /* @__PURE__ */ jsxDEV9("circle", {
      cx: "4",
      cy: "4",
      r: "4",
      fill: color
    }, undefined, false, undefined, this) : shape === "square" ? /* @__PURE__ */ jsxDEV9("rect", {
      height: "8",
      fill: color,
      width: "8"
    }, undefined, false, undefined, this) : shape === "diamond" ? /* @__PURE__ */ jsxDEV9("polygon", {
      fill: color,
      points: "4,0 8,4 4,8 0,4"
    }, undefined, false, undefined, this) : shape === "triangle" ? /* @__PURE__ */ jsxDEV9("polygon", {
      fill: color,
      points: "4,0 8,8 0,8"
    }, undefined, false, undefined, this) : /* @__PURE__ */ jsxDEV9("polygon", {
      fill: color,
      points: "4,0 7.6,2 7.6,6 4,8 0.4,6 0.4,2"
    }, undefined, false, undefined, this)
  }, undefined, false, undefined, this);
}
var SWEEP_MS = 15000;
var GENTLE = 0.45;
function cineEase(t) {
  const u = t < 0 ? 0 : t > 1 ? 1 : t;
  const smooth = u * u * (3 - 2 * u);
  return GENTLE * u + (1 - GENTLE) * smooth;
}
function invCineEase(y3) {
  let lo = 0;
  let hi = 1;
  for (let i2 = 0;i2 < 24; i2 += 1) {
    const mid = (lo + hi) / 2;
    if (cineEase(mid) < y3) {
      lo = mid;
    } else {
      hi = mid;
    }
  }
  return (lo + hi) / 2;
}
function revealText(axis, reveal) {
  const date = dateAtReveal(axis, reveal);
  return date !== null ? formatDate(date) : `${Math.round(reveal * axis.size)} / ${axis.size}`;
}
function RevealLabel({ axis, revealStore }) {
  const labelRef = useRef2(null);
  const sync = useCallback2((reveal) => {
    const el = labelRef.current;
    if (el) {
      el.textContent = revealText(axis, reveal);
    }
  }, [axis]);
  useEffect3(() => revealStore.subscribe(sync), [revealStore, sync]);
  useEffect3(() => {
    sync(revealStore.get());
  }, [revealStore, sync]);
  return /* @__PURE__ */ jsxDEV9("span", {
    className: "tabular-nums text-foreground/75",
    ref: labelRef,
    children: revealText(axis, revealStore.get())
  }, undefined, false, undefined, this);
}
function StarMap({
  graph,
  imported = false,
  onImport,
  onResetMap
}) {
  const canvasRef = useRef2(null);
  const wrapRef = useRef2(null);
  const simRef = useRef2(null);
  const nodesRef = useRef2([]);
  const linksRef = useRef2([]);
  const byIdRef = useRef2(new Map);
  const adjacencyRef = useRef2(new Map);
  const memByIdRef = useRef2(new Map);
  const ringsRef = useRef2([]);
  const ringLabelRectsRef = useRef2([]);
  const fadeRef = useRef2({
    appear: new Map,
    labels: new Map,
    links: new Map,
    nodes: new Map,
    rings: new Map
  });
  const doubleTapRef = useRef2(createDoubleTapDetector());
  const paletteRef = useRef2(null);
  const themeDirtyRef = useRef2(true);
  const invalidateRef = useRef2(() => {});
  const viewportRef = useRef2({ k: 1, x: 0, y: 0 });
  const hoverRef = useRef2(null);
  const hoveredLinkRef = useRef2(null);
  const hoveredRingRef = useRef2(null);
  const selectedRingRef = useRef2(null);
  const selectedIdRef = useRef2(null);
  const sizeRef = useRef2({ h: 0, w: 0 });
  const dprRef = useRef2(1);
  const dirtyRef = useRef2(true);
  const snapMotionRef = useRef2(false);
  const dragRef = useRef2({ id: null, mode: "none", moved: false, ring: null, sx: 0, sy: 0, vp: { k: 1, x: 0, y: 0 } });
  const [selectedId, setSelectedId] = useState4(null);
  const [menuTarget, setMenuTarget] = useState4(null);
  const [size, setSize] = useState4({ h: 0, w: 0 });
  const themeEpoch = useThemeEpoch();
  const [memoryColor, setMemoryColor] = useState4("var(--theme-secondary)");
  const revealStore = useMemo2(() => atom(1), []);
  const [playing, setPlaying] = useState4(false);
  const [ringStops, setRingStops] = useState4([]);
  const revealRef = useRef2(1);
  const camRadiusRef = useRef2(RING_OUTER);
  const timeAxis = useMemo2(() => buildTimeAxis(graph, 72), [graph]);
  const shareCode = useMemo2(() => encodeShareCode(graph), [graph]);
  const importCode = useCallback2((code) => {
    try {
      const next = decodeShareCode(code);
      onImport?.(next);
      return null;
    } catch (err2) {
      return err2 instanceof ShareCodeError ? err2.message : "Could not read that map code.";
    }
  }, [onImport]);
  const invalidate = useCallback2(() => invalidateRef.current(), []);
  const setRevealValue = useCallback2((value) => {
    const next = clamp(value, 0, 1);
    revealRef.current = next;
    revealStore.set(next);
    invalidate();
  }, [invalidate, revealStore]);
  const resetFades = useCallback2(() => {
    for (const bucket of Object.values(fadeRef.current)) {
      bucket.clear();
    }
  }, []);
  const memById = useMemo2(() => {
    const m2 = new Map;
    graph.memory.forEach((card, i2) => m2.set(`memory:${card.source}:${i2}`, card));
    return m2;
  }, [graph.memory]);
  const adjacency = useMemo2(() => {
    const m2 = new Map;
    for (const n of graph.nodes) {
      m2.set(n.id, new Set);
    }
    for (const e of graph.edges) {
      m2.get(e.source)?.add(e.target);
      m2.get(e.target)?.add(e.source);
    }
    return m2;
  }, [graph.edges, graph.nodes]);
  useEffect3(() => {
    const el = wrapRef.current;
    if (!el) {
      return;
    }
    const sync = () => setSize({ h: el.clientHeight, w: el.clientWidth });
    const ro = new ResizeObserver(sync);
    ro.observe(el);
    sync();
    return () => ro.disconnect();
  }, []);
  useEffect3(() => {
    sizeRef.current = size;
    if (size.w === 0 || size.h === 0) {
      return;
    }
    const { byId, links, nodes, rings, sim } = buildSimulation(graph, invalidate);
    simRef.current = sim;
    nodesRef.current = nodes;
    linksRef.current = links;
    byIdRef.current = byId;
    ringsRef.current = rings;
    setRingStops(rings.map((rg, i2) => rg.label != null ? rings[i2 - 1]?.ratio ?? 0 : -1).filter((v) => v >= 0));
    resetFades();
    viewportRef.current = fitViewport(size.w, size.h, rings[rings.length - 1]?.r ?? RING_OUTER);
    invalidate();
    if (selectedIdRef.current && !byId.has(selectedIdRef.current)) {
      selectedIdRef.current = null;
      setSelectedId(null);
    }
    return () => {
      sim.stop();
      if (simRef.current === sim) {
        simRef.current = null;
      }
    };
  }, [graph, invalidate, resetFades, size]);
  useEffect3(() => {
    adjacencyRef.current = adjacency;
    memByIdRef.current = memById;
    invalidate();
  }, [adjacency, invalidate, memById]);
  useEffect3(() => {
    document.fonts?.load('1em "JetBrains Mono"').then(invalidate, () => {});
  }, [invalidate]);
  useEffect3(() => {
    selectedIdRef.current = selectedId;
    invalidate();
  }, [invalidate, selectedId]);
  useEffect3(() => {
    camRadiusRef.current = RING_OUTER;
    snapMotionRef.current = false;
    setRevealValue(1);
    setPlaying(false);
  }, [graph, setRevealValue]);
  const targetRadius = useCallback2((rev2) => {
    const rings = ringsRef.current;
    if (!rings.length) {
      return RING_OUTER;
    }
    const lead = rings.findIndex((rg) => rg.ratio > rev2 + 0.001);
    const i2 = lead === -1 ? rings.length - 1 : lead;
    const band = (rings[1]?.r ?? RING_OUTER) - (rings[0]?.r ?? 0);
    return rings[i2].r + band * 0.35;
  }, []);
  const applyFit = useCallback2((radius) => {
    const { h, w } = sizeRef.current;
    if (w > 0 && h > 0) {
      viewportRef.current = fitViewport(w, h, radius);
    }
  }, []);
  const fitForReveal = useCallback2((rev2) => {
    camRadiusRef.current = targetRadius(rev2);
    applyFit(camRadiusRef.current);
  }, [applyFit, targetRadius]);
  useEffect3(() => {
    if (!playing) {
      return;
    }
    let raf = 0;
    let start = 0;
    const step = (now2) => {
      if (!start) {
        start = now2 - invCineEase(revealRef.current) * SWEEP_MS;
      }
      const progress = Math.min(1, (now2 - start) / SWEEP_MS);
      const next = cineEase(progress);
      const target = targetRadius(next);
      camRadiusRef.current += (target - camRadiusRef.current) * 0.1;
      applyFit(camRadiusRef.current);
      setRevealValue(next);
      if (progress >= 1 && Math.abs(target - camRadiusRef.current) < 0.5) {
        camRadiusRef.current = target;
        applyFit(target);
        setPlaying(false);
        return;
      }
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [applyFit, playing, setRevealValue, targetRadius]);
  const onTogglePlay = useCallback2(() => {
    if (playing) {
      setPlaying(false);
      return;
    }
    snapMotionRef.current = false;
    if (revealRef.current >= 1) {
      resetFades();
      fitForReveal(0);
      setRevealValue(0);
    }
    setPlaying(true);
  }, [fitForReveal, playing, resetFades, setRevealValue]);
  const onScrub = useCallback2((value) => {
    const next = clamp(value, 0, 1);
    setPlaying(false);
    snapMotionRef.current = true;
    fitForReveal(next);
    setRevealValue(next);
  }, [fitForReveal, setRevealValue]);
  useEffect3(() => {
    const onKey = (e) => {
      if (e.code !== "Space" && e.key !== " ") {
        return;
      }
      const el = document.activeElement;
      const tag = el?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "BUTTON" || el?.isContentEditable) {
        return;
      }
      e.preventDefault();
      onTogglePlay();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onTogglePlay]);
  useEffect3(() => {
    const el = canvasRef.current ?? wrapRef.current;
    if (!el) {
      return;
    }
    const style = getComputedStyle(el);
    const val = style.getPropertyValue("--theme-primary").trim();
    if (val) {
      const bgVal = style.getPropertyValue("--background").trim() || style.getPropertyValue("--dt-background").trim() || "#000";
      setMemoryColor(rgba(memoryInkFor(resolveRgb(val), resolveRgb(bgVal)), 0.9));
    }
  }, [size, themeEpoch]);
  useEffect3(() => {
    themeDirtyRef.current = true;
    invalidate();
  }, [invalidate, themeEpoch]);
  useEffect3(() => {
    let raf = 0;
    const ANIM_MS = 1000 / 30;
    let lastAnimTs = 0;
    let force = true;
    const isPaused = () => typeof document !== "undefined" && document.hidden || typeof document.hasFocus === "function" && !document.hasFocus();
    let paused = isPaused();
    const schedule = () => {
      if (!paused && !raf) {
        raf = requestAnimationFrame(frame2);
      }
    };
    let staticCanvas = null;
    const paint = () => {
      const canvas = canvasRef.current;
      const ctx = canvas?.getContext("2d");
      if (!canvas || !ctx) {
        return;
      }
      if (!staticCanvas) {
        staticCanvas = document.createElement("canvas");
      }
      if (staticCanvas.width !== canvas.width || staticCanvas.height !== canvas.height) {
        staticCanvas.width = canvas.width;
        staticCanvas.height = canvas.height;
        dirtyRef.current = true;
      }
      const offCtx = staticCanvas.getContext("2d");
      if (!offCtx) {
        return;
      }
      if (themeDirtyRef.current || !paletteRef.current) {
        paletteRef.current = computePalette(canvas);
        themeDirtyRef.current = false;
        dirtyRef.current = true;
      }
      const palette = paletteRef.current;
      if (!palette) {
        return;
      }
      if (dirtyRef.current) {
        const { animating, ringLabelRects } = drawScene({
          adjacency: adjacencyRef.current,
          byId: byIdRef.current,
          ctx: offCtx,
          dpr: dprRef.current,
          fades: fadeRef.current,
          focusId: selectedIdRef.current ?? hoverRef.current,
          hoverId: hoverRef.current,
          hoverLink: hoveredLinkRef.current,
          hoverRing: hoveredRingRef.current,
          links: linksRef.current,
          memById: memByIdRef.current,
          nodes: nodesRef.current,
          palette,
          reveal: revealRef.current,
          rings: ringsRef.current,
          selectedRing: selectedRingRef.current,
          size: sizeRef.current,
          snapMotion: snapMotionRef.current,
          vp: viewportRef.current
        });
        snapMotionRef.current = false;
        ringLabelRectsRef.current = ringLabelRects;
        dirtyRef.current = animating;
      }
      const focused = selectedIdRef.current ?? hoverRef.current;
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      if (focused) {
        drawScramble({ ctx, dpr: dprRef.current, palette, rings: ringsRef.current, vp: viewportRef.current });
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.drawImage(staticCanvas, 0, 0);
      } else {
        ctx.drawImage(staticCanvas, 0, 0);
        drawScramble({ ctx, dpr: dprRef.current, palette, rings: ringsRef.current, vp: viewportRef.current });
      }
    };
    const frame2 = (ts) => {
      raf = 0;
      if (!force && ts - lastAnimTs < ANIM_MS) {
        schedule();
        return;
      }
      force = false;
      lastAnimTs = ts;
      paint();
      schedule();
    };
    invalidateRef.current = () => {
      dirtyRef.current = true;
      force = true;
      schedule();
    };
    const onActivity = () => {
      const next = isPaused();
      if (next === paused) {
        return;
      }
      paused = next;
      if (paused) {
        if (raf) {
          cancelAnimationFrame(raf);
          raf = 0;
        }
      } else {
        dirtyRef.current = true;
        force = true;
        schedule();
      }
    };
    document.addEventListener("visibilitychange", onActivity);
    window.addEventListener("blur", onActivity);
    window.addEventListener("focus", onActivity);
    schedule();
    return () => {
      cancelAnimationFrame(raf);
      document.removeEventListener("visibilitychange", onActivity);
      window.removeEventListener("blur", onActivity);
      window.removeEventListener("focus", onActivity);
      invalidateRef.current = () => {};
    };
  }, []);
  useEffect3(() => {
    sizeRef.current = size;
    dprRef.current = Math.min(2, window.devicePixelRatio || 1);
    const canvas = canvasRef.current;
    if (canvas && size.w > 0 && size.h > 0) {
      canvas.width = Math.round(size.w * dprRef.current);
      canvas.height = Math.round(size.h * dprRef.current);
      canvas.style.width = `${size.w}px`;
      canvas.style.height = `${size.h}px`;
    }
    invalidate();
  }, [invalidate, size]);
  const pickNode = (cssX, cssY) => {
    const vp = viewportRef.current;
    const nodeK = fitScale(sizeRef.current.w, sizeRef.current.h, ringsRef.current);
    let best = null;
    let bestD = Infinity;
    for (const n of nodesRef.current) {
      const r = nodeRadius(n) * nodeK + 6;
      const sx = n.x * vp.k + vp.x;
      const sy = n.y * vp.k * TILT + vp.y;
      const d = (sx - cssX) ** 2 + (sy - cssY) ** 2;
      if (d < r * r && d < bestD) {
        bestD = d;
        best = n;
      }
    }
    return best;
  };
  const pickLink = (cssX, cssY) => {
    const vp = viewportRef.current;
    let best = null;
    let bestD = 25;
    for (const link of linksRef.current) {
      const s = typeof link.source === "object" ? link.source : byIdRef.current.get(String(link.source));
      const t = typeof link.target === "object" ? link.target : byIdRef.current.get(String(link.target));
      if (!s || !t) {
        continue;
      }
      const d = distToSegmentSq(cssX, cssY, s.x * vp.k + vp.x, s.y * vp.k * TILT + vp.y, t.x * vp.k + vp.x, t.y * vp.k * TILT + vp.y);
      if (d < bestD) {
        bestD = d;
        best = `${s.id}->${t.id}`;
      }
    }
    return best;
  };
  const pickRingLabel = (cssX, cssY) => {
    for (const r of ringLabelRectsRef.current) {
      if (cssX >= r.x && cssX <= r.x + r.w && cssY >= r.y && cssY <= r.y + r.h) {
        return r.i;
      }
    }
    return null;
  };
  const localXY = (e) => {
    const rect = canvasRef.current?.getBoundingClientRect();
    return { x: e.clientX - (rect?.left ?? 0), y: e.clientY - (rect?.top ?? 0) };
  };
  const resetView = () => {
    setPlaying(false);
    viewportRef.current = fitViewport(sizeRef.current.w, sizeRef.current.h, ringsRef.current[ringsRef.current.length - 1]?.r ?? RING_OUTER);
    selectedRingRef.current = null;
    invalidate();
    setSelectedId(null);
  };
  const onMouseDown = (e) => {
    if (e.button !== 0) {
      return;
    }
    const { x: x4, y: y3 } = localXY(e);
    const ringHit = pickRingLabel(x4, y3);
    hoveredRingRef.current = null;
    const nodeId = ringHit == null ? pickNode(x4, y3)?.id ?? null : null;
    dragRef.current = {
      id: nodeId,
      mode: "pan",
      moved: false,
      ring: ringHit,
      sx: e.clientX,
      sy: e.clientY,
      vp: viewportRef.current
    };
  };
  const onMouseMove = (e) => {
    const drag = dragRef.current;
    if (drag.mode === "none") {
      const { x: x4, y: y3 } = localXY(e);
      const ringHit = pickRingLabel(x4, y3);
      const id = ringHit == null ? pickNode(x4, y3)?.id ?? null : null;
      const linkKey = ringHit == null && id == null ? pickLink(x4, y3) : null;
      if (id !== hoverRef.current || ringHit !== hoveredRingRef.current || linkKey !== hoveredLinkRef.current) {
        hoverRef.current = id;
        hoveredRingRef.current = ringHit;
        hoveredLinkRef.current = linkKey;
        invalidate();
      }
      return;
    }
    const dx = e.clientX - drag.sx;
    const dy = e.clientY - drag.sy;
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) {
      drag.moved = true;
    }
    if (drag.mode === "pan") {
      if (drag.moved) {
        setPlaying(false);
      }
      viewportRef.current = { ...drag.vp, x: drag.vp.x + dx, y: drag.vp.y + dy };
      invalidate();
    }
  };
  const endDrag = () => {
    const drag = dragRef.current;
    if (drag.mode === "pan" && !drag.moved) {
      if (doubleTapRef.current()) {
        resetView();
        dragRef.current = { id: null, mode: "none", moved: false, ring: null, sx: 0, sy: 0, vp: viewportRef.current };
        return;
      }
      if (drag.ring != null) {
        selectedRingRef.current = selectedRingRef.current === drag.ring ? null : drag.ring;
      } else if (drag.id) {
        setSelectedId((prev) => prev === drag.id ? null : drag.id);
      } else {
        selectedRingRef.current = null;
        setSelectedId(null);
      }
      invalidate();
    }
    dragRef.current = { id: null, mode: "none", moved: false, ring: null, sx: 0, sy: 0, vp: viewportRef.current };
  };
  const onMouseLeave = () => {
    hoverRef.current = null;
    hoveredRingRef.current = null;
    hoveredLinkRef.current = null;
    invalidate();
    endDrag();
  };
  const onContextMenu = (e) => {
    e.preventDefault();
    const { x: x4, y: y3 } = localXY(e);
    const node = pickNode(x4, y3);
    if (!node) {
      return setMenuTarget(null);
    }
    setSelectedId(node.id);
    setMenuTarget({
      id: node.id,
      kind: node.kind === "memory" ? "memory" : "skill",
      label: node.label,
      x: e.clientX,
      y: e.clientY
    });
  };
  const onWheel = (e) => {
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect) {
      return;
    }
    if (isSmartZoomWheel(e)) {
      resetView();
      return;
    }
    setPlaying(false);
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;
    const vp = viewportRef.current;
    const k = clamp(vp.k * (e.deltaY > 0 ? 0.9 : 1.1), ZOOM_MIN, ZOOM_MAX);
    viewportRef.current = { k, x: px - (px - vp.x) / vp.k * k, y: py - (py - vp.y) / vp.k * k };
    invalidate();
  };
  return /* @__PURE__ */ jsxDEV9("div", {
    className: "relative min-h-0 flex-1 overflow-hidden",
    ref: wrapRef,
    children: [
      /* @__PURE__ */ jsxDEV9("canvas", {
        className: "block touch-none select-none text-foreground",
        onContextMenu,
        onDoubleClick: resetView,
        onMouseDown,
        onMouseLeave,
        onMouseMove,
        onMouseUp: endDrag,
        onWheel,
        ref: canvasRef
      }, undefined, false, undefined, this),
      /* @__PURE__ */ jsxDEV9(NodeContextMenu, {
        onClose: () => setMenuTarget(null),
        onNodeRemoved: () => {
          setMenuTarget(null);
          setSelectedId(null);
        },
        target: menuTarget
      }, undefined, false, undefined, this),
      /* @__PURE__ */ jsxDEV9("div", {
        className: "pointer-events-none absolute inset-x-0 top-6 z-20 flex justify-center px-12",
        children: /* @__PURE__ */ jsxDEV9(Timeline, {
          axis: timeAxis,
          memoryColor,
          onScrub,
          onTogglePlay,
          playing,
          revealStore,
          ringStops
        }, undefined, false, undefined, this)
      }, undefined, false, undefined, this),
      /* @__PURE__ */ jsxDEV9("div", {
        className: "pointer-events-auto absolute bottom-2 right-2 z-20 [-webkit-app-region:no-drag]",
        children: /* @__PURE__ */ jsxDEV9(ShareControls, {
          imported,
          onImport: importCode,
          onResetMap,
          shareCode
        }, undefined, false, undefined, this)
      }, undefined, false, undefined, this),
      /* @__PURE__ */ jsxDEV9("div", {
        className: "pointer-events-none absolute bottom-2 left-2 flex flex-col gap-1 text-[0.62rem] text-muted-foreground",
        children: [
          HYATLAS_LAYERS.map(([key, style]) => /* @__PURE__ */ jsxDEV9("span", {
            className: "flex items-center gap-1.5",
            children: [
              /* @__PURE__ */ jsxDEV9(LayerGlyph, {
                color: style.color,
                shape: style.shape
              }, undefined, false, undefined, this),
              " ",
              style.label
            ]
          }, key, true, undefined, this)),
          /* @__PURE__ */ jsxDEV9("span", {
            className: "text-[0.58rem] text-muted-foreground/65",
            children: "core = oldest · outer = newer · layers clockwise from L1 (top)"
          }, undefined, false, undefined, this),
          /* @__PURE__ */ jsxDEV9(RevealLabel, {
            axis: timeAxis,
            revealStore
          }, undefined, false, undefined, this)
        ]
      }, undefined, true, undefined, this)
    ]
  }, undefined, true, undefined, this);
}

// entry.tsx
var _rest = null;
function fetchStarmapGraph() {
  return _rest("/learning/graph?n=5000").then((d) => ({
    clusters: [],
    ...d,
    edges: d.edges ?? [],
    memory: d.memory ?? [],
    nodes: (d.nodes ?? []).map((n) => ({
      ...n,
      useCount: n.use_count ?? 0,
      createdBy: n.created_by ?? null
    }))
  }));
}
function StarmapGraphView() {
  const [graph, setGraph] = useState5(null);
  const [error, setError] = useState5(null);
  const [loading, setLoading] = useState5(true);
  const [tick, setTick] = useState5(0);
  useEffect4(() => {
    if (!_rest)
      return;
    let alive = true;
    setLoading(true);
    setError(null);
    fetchStarmapGraph().then((d) => {
      if (alive) {
        setGraph(d);
        setLoading(false);
      }
    }).catch((e) => {
      if (alive) {
        setError(e);
        setLoading(false);
      }
    });
    return () => {
      alive = false;
    };
  }, [tick]);
  if (loading && !graph) {
    return jsx("div", { className: "flex flex-1 items-center justify-center text-xs text-(--ui-text-tertiary)", children: "Loading map…" });
  }
  if (error) {
    return jsx(ErrorState, {
      title: "Could not load memory graph",
      description: String(error && error.message || error),
      children: jsx(Button2, { type: "button", variant: "outline", size: "sm", onClick: () => setTick((t) => t + 1), children: "Retry" })
    });
  }
  if (graph && graph.nodes.length === 0) {
    return jsx(EmptyState, { title: "Nothing learned yet", description: "As Hermes builds skills and memories for your work, they appear here." });
  }
  if (!graph)
    return null;
  return jsx("div", {
    className: "relative flex min-h-0 flex-1 flex-col",
    children: jsx(StarMap, { graph })
  });
}
var LAYER_META = {
  l1_profile: { color: "#60a5fa", label: "L1 Profile" },
  l2_raw: { color: "#f97316", label: "L2 Raw" },
  l3_fact: { color: "#a78bfa", label: "L3 Fact" },
  l4_summary: { color: "#f472b6", label: "L4 Summary" },
  l5_knowledge: { color: "#34d399", label: "L5 Knowledge" },
  l6_schema: { color: "#facc15", label: "L6 Schema" },
  l7_intention: { color: "#f87171", label: "L7 Intention" }
};
function Health({ ok, label }) {
  return jsxs("span", { className: "inline-flex items-center gap-1.5 text-xs text-(--ui-text-secondary)", children: [
    jsx(StatusDot, { tone: ok ? "good" : "bad" }),
    jsx("span", { children: label })
  ] });
}
function LayerBar({ label, value, max: max2, color }) {
  const n = Number(value) || 0;
  const pct = Math.max(0, Math.min(100, n / Math.max(Number(max2) || 1, 1) * 100));
  return jsxs("div", { className: "flex flex-col gap-1", children: [
    jsxs("div", { className: "flex justify-between text-xs text-(--ui-text-secondary)", children: [
      jsx("span", { children: label }),
      jsx("span", { className: "tabular-nums", children: String(n) })
    ] }),
    jsx("div", { className: "h-1.5 overflow-hidden rounded-full bg-(--ui-border-subtle)", children: jsx("div", {
      className: "h-full rounded-full",
      style: { width: `${pct}%`, backgroundColor: color }
    }) })
  ] });
}
function Stat({ label, value, hint }) {
  return jsxs("div", { className: "rounded-md border border-(--ui-border-subtle) p-2.5", children: [
    jsx("div", { className: "text-[0.65rem] text-(--ui-text-tertiary)", children: label }),
    jsx("div", { className: "mt-0.5 text-lg tabular-nums", children: value ?? "—" }),
    hint ? jsx("div", { className: "mt-1 text-[0.65rem] text-(--ui-text-quaternary)", children: hint }) : null
  ] });
}
function Overview({ status, layers, maxLayer, connecting }) {
  if (connecting) {
    return jsx("div", { className: "text-xs text-(--ui-text-tertiary)", children: "Loading status…" });
  }
  return jsxs("div", { className: "flex flex-col gap-4", children: [
    jsxs("div", { className: "flex flex-wrap gap-4", children: [
      jsx(Health, { ok: Boolean(status && status.vdb === "ok"), label: `VDB (${status && status.vdb_provider || "—"})` }),
      jsx(Health, { ok: Boolean(status && status.embed === "ok"), label: `Embedder ${status ? `${status.embed_dims}d` : ""}` }),
      jsx(Health, { ok: Boolean(status && status.llm === "ok"), label: "LLM extraction" }),
      jsx(Health, { ok: Boolean(status && status.write_pipeline === "ok"), label: "Write pipeline" })
    ] }),
    jsx("div", {
      className: "flex flex-col gap-2 rounded-md border border-(--ui-border-subtle) p-2.5",
      children: Object.entries(LAYER_META).map(([key, meta]) => jsx(LayerBar, { label: meta.label, value: layers[key] || 0, max: maxLayer, color: meta.color }, key))
    }),
    jsxs("div", { className: "grid grid-cols-2 gap-3 md:grid-cols-4", children: [
      jsx(Stat, { label: "Used — writes", value: status && status.writes, hint: "memories added (all-time)" }),
      jsx(Stat, { label: "Used — searches", value: status && status.searches, hint: "recalls by agents (all-time)" }),
      jsx(Stat, { label: "VDB points", value: status && status.vdb_points }),
      jsx(Stat, { label: "Embed dims", value: status && status.embed_dims })
    ] })
  ] });
}
function when(item) {
  const sec = Number(item && item.gmt_created);
  if (Number.isFinite(sec) && sec > 0)
    return relativeTime(sec * 1000);
  return item && item.ts || "";
}
function MemoryCard({ item, meta }) {
  return jsxs("div", { className: "rounded-md border border-(--ui-border-subtle) p-2.5", children: [
    jsxs("div", { className: "flex items-center justify-between gap-2 text-[0.65rem] text-(--ui-text-tertiary)", children: [
      jsx(Badge, { variant: "muted", size: "xs", children: meta || item.layer || "memory" }),
      jsx("span", { children: when(item) })
    ] }),
    jsx("div", { className: "mt-1.5 text-sm text-(--ui-text-primary) whitespace-pre-wrap break-words", children: (item.content || item.text || "").slice(0, 600) || "(empty)" })
  ] });
}
function Memories({ layer, setLayer, list, loading, refresh }) {
  return jsxs("div", { className: "flex flex-col gap-3", children: [
    jsxs("div", { className: "flex items-center gap-2", children: [
      jsx("span", { className: "text-xs text-(--ui-text-tertiary)", children: "Layer:" }),
      jsx(Select, { value: layer, onValueChange: setLayer, children: [
        jsx(SelectTrigger, { className: "w-56", children: jsx(SelectValue, { placeholder: "All layers" }) }),
        jsx(SelectContent, { children: [
          jsx(SelectItem, { value: "all", children: "All layers" }),
          ...Object.entries(LAYER_META).map(([k, m2]) => jsx(SelectItem, { value: k, children: m2.label }, k))
        ] })
      ] }),
      jsx(Button2, { type: "button", variant: "outline", size: "sm", onClick: refresh, children: "Refresh" })
    ] }),
    loading ? jsx("div", { className: "text-xs text-(--ui-text-tertiary)", children: "Loading memories…" }) : list.length ? jsx("div", {
      className: "flex flex-col gap-2 overflow-y-auto",
      children: list.map((item, i2) => jsx(MemoryCard, { item, meta: LAYER_META[item.layer]?.label || item.layer }, item && (item.memory_id || item.ts) || `row-${i2}`))
    }) : jsx(EmptyState, { title: "No memories", description: layer === "all" ? "Nothing stored yet." : "Nothing in this layer." })
  ] });
}
function SearchView({ query, setQuery, submitted, setSubmitted, hits, loading }) {
  return jsxs("div", { className: "flex flex-col gap-3", children: [
    jsxs("div", { className: "flex items-center gap-2", children: [
      jsx(SearchField, {
        placeholder: "Semantic search…",
        value: query,
        onChange: setQuery,
        loading,
        containerClassName: "flex-1",
        "aria-label": "Search memories"
      }),
      jsx(Button2, { type: "button", size: "sm", disabled: !query.trim() || loading, onClick: () => setSubmitted(query.trim()), children: "Search" })
    ] }),
    !submitted ? jsx(EmptyState, { title: "Search memories", description: "Type a query and press Search." }) : loading ? jsx("div", { className: "text-xs text-(--ui-text-tertiary)", children: "Searching…" }) : hits.length ? jsx("div", {
      className: "flex flex-col gap-2 overflow-y-auto",
      children: hits.map(([channel, m2], i2) => jsx(MemoryCard, {
        item: m2,
        meta: `${channel} · ${m2 && m2.layer || ""} · ${Number(m2 && m2.score || 0).toFixed(3)}`
      }, m2 && m2.memory_id || `hit-${i2}`))
    }) : jsx(EmptyState, { title: "No hits", description: "Try a broader query." })
  ] });
}
function AddView({ draft, setDraft, addPending, addError, onSave }) {
  return jsxs("div", { className: "flex flex-col gap-3", children: [
    jsx(Textarea, {
      value: draft,
      onChange: (e) => setDraft(e.target.value),
      rows: 6,
      placeholder: "Write a memory… v4 will extract facts, summary, graph nodes, and intention."
    }),
    jsxs("div", { className: "flex items-center gap-2", children: [
      jsx(Button2, { type: "button", disabled: addPending || !draft.trim(), onClick: onSave, children: addPending ? "Saving…" : "Save memory" }),
      addError ? jsx("span", { className: "text-xs text-destructive", children: String(addError.message || addError) }) : null
    ] })
  ] });
}
var TABS = [
  { id: "overview", label: "Overview" },
  { id: "graph", label: "Graph" },
  { id: "memories", label: "Memories" },
  { id: "search", label: "Search" },
  { id: "add", label: "Add" }
];
function HyAtlasPage() {
  const [tab, setTab] = useState5("overview");
  const [layer, setLayer] = useState5("all");
  const [query, setQuery] = useState5("");
  const [submitted, setSubmitted] = useState5("");
  const [draft, setDraft] = useState5("");
  const [tick, setTick] = useState5(0);
  const [status, setStatus] = useState5(null);
  useEffect4(() => {
    if (!_rest)
      return;
    let alive = true;
    const fetchStatus = () => _rest("/status").then((s) => {
      if (alive)
        setStatus(s);
    }).catch(() => {});
    fetchStatus();
    const id = setInterval(fetchStatus, 8000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [tick]);
  const ver = status && status.version ? `v${status.version}` : "v4";
  const [list, setList] = useState5([]);
  const [listLoading, setListLoading] = useState5(true);
  useEffect4(() => {
    if (!_rest)
      return;
    let alive = true;
    setListLoading(true);
    const path = `/memories?limit=30${layer === "all" ? "" : `&layer=${layer}`}`;
    _rest(path).then((r) => {
      if (alive) {
        const m2 = r && r.memories;
        setList(Array.isArray(m2) ? m2 : m2 && m2.normal || []);
        setListLoading(false);
      }
    }).catch(() => {
      if (alive)
        setListLoading(false);
    });
    return () => {
      alive = false;
    };
  }, [layer, tick]);
  const [hits, setHits] = useState5([]);
  const [searchLoading, setSearchLoading] = useState5(false);
  useEffect4(() => {
    if (!_rest || !submitted) {
      setHits([]);
      return;
    }
    let alive = true;
    setSearchLoading(true);
    _rest(`/search?q=${encodeURIComponent(submitted)}&limit=10`).then((r) => {
      if (!alive)
        return;
      const c2 = r && r.memories || {};
      const merged = [
        ...(c2.profile || []).map((m2) => ["profile", m2]),
        ...(c2.proactive || []).map((m2) => ["proactive", m2]),
        ...(c2.normal || []).map((m2) => ["normal", m2])
      ];
      setHits(merged);
      setSearchLoading(false);
    }).catch(() => {
      if (alive)
        setSearchLoading(false);
    });
    return () => {
      alive = false;
    };
  }, [submitted, tick]);
  const [addPending, setAddPending] = useState5(false);
  const [addError, setAddError] = useState5(null);
  const onSave = () => {
    if (!draft.trim())
      return;
    setAddPending(true);
    setAddError(null);
    _rest("/add", { method: "POST", body: { text: draft.trim() } }).then(() => {
      setDraft("");
      setTick((t) => t + 1);
      host.notify({ kind: "success", message: `Memory saved to HyAtlas ${ver}` });
    }).catch((e) => setAddError(e)).finally(() => setAddPending(false));
  };
  const layers = status && status.layers || {};
  const maxLayer = Math.max(1, ...Object.values(layers).map((v) => Number(v) || 0));
  const connecting = !status;
  const body = tab === "overview" ? jsx(Overview, { status, layers, maxLayer, connecting }) : tab === "graph" ? jsx(StarmapGraphView, {}) : tab === "memories" ? jsx(Memories, { layer, setLayer, list, loading: listLoading, refresh: () => setTick((t) => t + 1) }) : tab === "search" ? jsx(SearchView, { query, setQuery, submitted, setSubmitted, hits, loading: searchLoading }) : jsx(AddView, { draft, setDraft, addPending, addError, onSave });
  return jsxs("div", { className: "flex h-full flex-col gap-4 overflow-auto p-5 text-sm", children: [
    jsxs("div", { className: "flex items-start justify-between gap-3", children: [
      jsxs("div", { children: [
        jsx("h1", { className: "text-xl font-semibold", children: "HyAtlas Memory" }),
        jsx("p", { className: "text-xs text-(--ui-text-tertiary)", children: connecting ? `${ver} · chromem-go · connecting…` : status ? `${ver} · ${status.vdb_points || 0} memories · ${status.writes || 0} writes · ${status.searches || 0} recalls` : `${ver} · chromem-go` })
      ] }),
      jsx(Button2, { type: "button", variant: "outline", size: "sm", onClick: () => setTick((t) => t + 1), children: "Refresh" })
    ] }),
    jsx(SegmentedControl, { value: tab, onChange: setTab, options: TABS }),
    jsx("div", { className: "flex-1 min-h-0 flex flex-col", children: body })
  ] });
}
var entry_default = {
  id: "hyatlas",
  name: "HyAtlas Memory",
  register(ctx) {
    _rest = ctx.rest;
    ctx.registerMany([
      {
        id: "page",
        area: ROUTES_AREA,
        data: { path: "/hyatlas" },
        render: () => jsx(HyAtlasPage, {})
      },
      {
        id: "nav",
        area: SIDEBAR_NAV_AREA,
        data: { path: "/hyatlas", label: "HyAtlas Memory", codicon: "database" }
      },
      {
        id: "open",
        area: PALETTE_AREA,
        data: {
          id: "hyatlas.open",
          label: "Open HyAtlas Memory",
          keywords: ["hyatlas", "memory", "starmap"],
          run: () => host.navigate("/hyatlas")
        }
      },
      {
        id: "shortcut",
        area: KEYBINDS_AREA,
        data: {
          id: "hyatlas.open",
          label: "Open HyAtlas Memory",
          category: "HyAtlas",
          defaults: ["mod+shift+h"],
          run: () => host.navigate("/hyatlas")
        }
      }
    ]);
  }
};
export {
  entry_default as default
};
