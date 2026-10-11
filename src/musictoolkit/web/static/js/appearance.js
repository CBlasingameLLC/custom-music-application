// The accent colour: a handful of presets and any colour of the person's choosing. Whatever is chosen, the colours the
// app derives from it are made to be readable: the accent used as text or an icon on the app's surfaces, and the solid
// fill under the text of a primary button, each reach at least 4.5:1 (WCAG AA for normal text). Pure functions, no DOM.

export const DEFAULT_ACCENT = 'violet';

export const ACCENTS = [
  { id: 'violet', name: 'Violet', hex: '#8b7bff' },
  { id: 'blue', name: 'Blue', hex: '#4f8dff' },
  { id: 'teal', name: 'Teal', hex: '#2bb8a8' },
  { id: 'green', name: 'Green', hex: '#3fb950' },
  { id: 'amber', name: 'Amber', hex: '#e8a82e' },
  { id: 'orange', name: 'Orange', hex: '#f0743e' },
  { id: 'rose', name: 'Rose', hex: '#ec5d8d' },
  { id: 'graphite', name: 'Graphite', hex: '#8a94a8' },
];

// The lightest dark surface and the darkest light surface text sits on (css/app.css: --bg-elev-2 and --bg-sidebar). An
// accent that reads on these reads on every surface in between.
const HARDEST_SURFACE = { dark: '#1b1f29', light: '#eceef7' };
const WHITE = '#ffffff';
const INK = '#0b0d12'; // text on a light accent
export const MIN_CONTRAST = 4.5;
const TARGET = 4.6; // a little over, so rounding to 8-bit colours never lands under the minimum
const WHITE_TEXT_MIN_LIGHTNESS = 0.42; // darker than this a colour is mud: use dark text on the lighter colour instead
const MAX_DARKENING = 0.3; // and so is one that must lose more than this much lightness to carry white text

// ------------------------------------------------------------ colour arithmetic

export function parseHex(text) {
  const match = /^#?([0-9a-f]{6})$/i.exec(String(text || '').trim());
  if (!match) return null;
  const value = parseInt(match[1], 16);
  return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
}

export const toHex = (rgb) => '#' + rgb.map((c) => Math.round(Math.min(255, Math.max(0, c))).toString(16).padStart(2, '0')).join('');

export function luminance([r, g, b]) {
  const linear = (c) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
}

/** The contrast ratio of two colours, 1 to 21 (hex strings or [r, g, b]). */
export function contrast(a, b) {
  const [x, y] = [a, b].map((c) => luminance(typeof c === 'string' ? parseHex(c) : c));
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}

export function rgbToHsl([r, g, b]) {
  const [R, G, B] = [r, g, b].map((c) => c / 255);
  const max = Math.max(R, G, B);
  const min = Math.min(R, G, B);
  const l = (max + min) / 2;
  if (max === min) return [0, 0, l];
  const d = max - min;
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  const h = max === R ? (G - B) / d + (G < B ? 6 : 0) : max === G ? (B - R) / d + 2 : (R - G) / d + 4;
  return [h * 60, s, l];
}

export function hslToRgb([h, s, l]) {
  const hue = ((h % 360) + 360) % 360;
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs(((hue / 60) % 2) - 1));
  const m = l - c / 2;
  const [r, g, b] = hue < 60 ? [c, x, 0] : hue < 120 ? [x, c, 0] : hue < 180 ? [0, c, x] : hue < 240 ? [0, x, c] : hue < 300 ? [x, 0, c] : [c, 0, x];
  return [(r + m) * 255, (g + m) * 255, (b + m) * 255];
}

/** Move a colour's lightness (hue and saturation kept) in `direction` (+1 lighter, -1 darker) until it reaches `target` against `against`. */
function adjustUntil(rgb, against, target, direction) {
  const [h, s, l0] = rgbToHsl(rgb);
  let l = l0;
  let current = rgb;
  while (contrast(current, against) < target && (direction > 0 ? l < 0.97 : l > 0.03)) {
    l += direction * 0.01;
    current = hslToRgb([h, s, l]).map(Math.round);
  }
  return current;
}

// ------------------------------------------------------------ what an accent becomes

/** An accent given as a preset id or a #rrggbb colour, as its #rrggbb (the default for anything unusable). */
export function accentHex(accent) {
  const preset = ACCENTS.find((a) => a.id === accent);
  if (preset) return preset.hex;
  const custom = parseHex(accent);
  return custom ? toHex(custom) : ACCENTS.find((a) => a.id === DEFAULT_ACCENT).hex;
}

/** What to keep of a saved accent: a preset id, or a #rrggbb; the default for anything else. */
export function normalizeAccent(accent) {
  if (ACCENTS.some((a) => a.id === accent)) return accent;
  const custom = parseHex(accent);
  return custom ? toHex(custom) : DEFAULT_ACCENT;
}

/**
 * The CSS variables an accent sets for a theme ("dark" or "light"):
 *   --accent        for text and icons on the app's surfaces (>= 4.5:1 on the hardest of them)
 *   --accent-fill   the solid colour of a primary button, with --on-accent text on it (>= 4.5:1)
 *   --accent-strong the deeper end of a gradient; --accent-2 its other end; --accent-soft a tint for selected rows
 */
export function accentVars(accent, theme) {
  const base = parseHex(accentHex(accent));
  const surface = HARDEST_SURFACE[theme === 'light' ? 'light' : 'dark'];
  const text = contrast(base, surface) >= TARGET ? base : adjustUntil(base, surface, TARGET, theme === 'light' ? -1 : 1);

  // The button fill. White text when the colour carries it, or can be darkened a little to (and still look like itself);
  // otherwise dark text on the colour, lightened if it must be.
  let fill;
  let onAccent;
  if (contrast(base, WHITE) >= TARGET) {
    [fill, onAccent] = [base, WHITE];
  } else {
    const darkened = adjustUntil(base, WHITE, TARGET, -1);
    const [baseLight, darkLight] = [rgbToHsl(base)[2], rgbToHsl(darkened)[2]];
    if (darkLight >= WHITE_TEXT_MIN_LIGHTNESS && baseLight - darkLight <= MAX_DARKENING) [fill, onAccent] = [darkened, WHITE];
    else [fill, onAccent] = [contrast(base, INK) >= TARGET ? base : adjustUntil(base, INK, TARGET, 1), INK];
  }
  const whiteWorks = onAccent === WHITE;
  const [h, s, l] = rgbToHsl(fill);
  const hover = hslToRgb([h, s, Math.min(0.92, Math.max(0.08, l + (whiteWorks ? 0.05 : -0.05)))]);
  const strong = hslToRgb([h, s, Math.max(0.1, l - 0.06)]);
  const [th, ts, tl] = rgbToHsl(text);
  const second = hslToRgb([th - 45, ts, tl]);
  const [r, g, b] = base.map(Math.round);
  return {
    '--accent': toHex(text),
    '--accent-fill': toHex(fill),
    '--accent-fill-hover': toHex(hover),
    '--accent-strong': toHex(strong),
    '--accent-2': toHex(second),
    '--accent-soft': `rgba(${r}, ${g}, ${b}, ${theme === 'light' ? 0.13 : 0.17})`,
    '--on-accent': onAccent,
  };
}

/** Black or white, whichever reads better on this colour (the tick drawn on a swatch). */
export const readableOn = (hex) => (contrast(hex, WHITE) >= contrast(hex, INK) ? WHITE : INK);

/** For the tests and the Settings preview: how readable each derived colour is. */
export function accentContrasts(accent, theme) {
  const vars = accentVars(accent, theme);
  return {
    text: contrast(vars['--accent'], HARDEST_SURFACE[theme === 'light' ? 'light' : 'dark']),
    button: contrast(vars['--accent-fill'], vars['--on-accent']),
  };
}
