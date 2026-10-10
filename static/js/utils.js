/* utils.js -- shared helpers for RPG map UI modules */

import { get } from './state.js';

export function esc(s) {
  if (s === null || s === undefined) return "";
  const div = document.createElement("div");
  div.textContent = String(s);
  return div.innerHTML;
}

export function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

// ===== Location display names (mutable, populated at runtime) =====
export const LOC_NAMES = {};

// ===== NPC color map =====
export const PALETTE = ['#c9935a','#b86b9e','#6a9ab5','#6dab6d','#c27070','#a87fc4','#c9a44a','#6a9ab5','#b85c5c','#6dab6d'];

export function npcColor(id) {
  const profile = get('gameState')?.npcs?.find(npc => npc.id === id)?.visual_profile;
  if (profile?.map_color) return profile.map_color;
  let hash = 0;
  for (let i = 0; i < id.length; i++) hash = ((hash << 5) - hash + id.charCodeAt(i)) | 0;
  return PALETTE[Math.abs(hash) % PALETTE.length];
}

// ===== SVG helpers =====
export function svgEl(tag, attrs) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, String(v)));
  return el;
}

export function shadeColor(color, percent) {
  if (color.startsWith('var(')) return color;
  let c = color;
  if (c.length === 4) c = '#' + c[1]+c[1] + c[2]+c[2] + c[3]+c[3];
  const num = parseInt(c.replace('#',''), 16);
  let r = (num >> 16) + percent, g = ((num >> 8) & 0xff) + percent, b = (num & 0xff) + percent;
  r = Math.max(0, Math.min(255, r));
  g = Math.max(0, Math.min(255, g));
  b = Math.max(0, Math.min(255, b));
  return '#' + ((1 << 24) + (r << 16) + (g << 8) + b).toString(16).slice(1);
}

export function guessShapeFromId(locId) {
  if (/training|field|park|garden|forest|lake|river/.test(locId)) return 'wide';
  if (/stadium|arena|theater|colosseum/.test(locId)) return 'dome';
  if (/office|tower|skyscraper|school|hospital/.test(locId)) return 'tall';
  if (/city|town|village|street|market|square|district/.test(locId)) return 'multi';
  if (/temple|church|castle|palace|shrine/.test(locId)) return 'pyramid';
  if (/home|apartment|house|inn|tavern|shop|store|room|locker/.test(locId)) return 'cube';
  return 'cube';
}
