/** Optional host-controlled visual tokens; they never carry session or payment state. */
export interface ClientTheme {
  accent?: string;
  accentHover?: string;
  nav?: string;
  navAlt?: string;
  canvas?: string;
}

declare global {
  interface Window { __CANONICAL_STAFF_THEME__?: ClientTheme; }
}

const tokenNames: Record<keyof ClientTheme, string> = {
  accent: "--accent", accentHover: "--accent-hover", nav: "--nav", navAlt: "--nav-alt", canvas: "--canvas",
};

export function applyClientTheme(theme = window.__CANONICAL_STAFF_THEME__) {
  if (!theme) return;
  for (const [key, value] of Object.entries(theme) as [keyof ClientTheme, string | undefined][]) {
    if (value && CSS.supports("color", value)) document.documentElement.style.setProperty(tokenNames[key], value);
  }
}

export function resetClientTheme() {
  for (const token of Object.values(tokenNames)) document.documentElement.style.removeProperty(token);
}
