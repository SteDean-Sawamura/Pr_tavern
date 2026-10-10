import * as state from '../state.js';
import * as api from '../api.js';
import { esc, npcColor, svgEl, shadeColor, guessShapeFromId, LOC_NAMES, PALETTE } from '../utils.js';

// ===== Map rendering =====

function renderMap() {
  const gameState = state.get('gameState');
  if (!gameState) return;
  const svg = document.getElementById('map-svg');
  svg.innerHTML = '';

  // Add defs for pixel patterns
  const defs = svgEl('defs', {});
  svg.appendChild(defs);

  const locs = gameState.locations || [];
  const npcs = gameState.npcs || [];
  const W = 800, H = 600, CX = W / 2, CY = H / 2, R = 210;

  const currentLocId = gameState.location;
  const nodes = [];
  const otherLocs = locs.filter(l => l.id !== currentLocId);

  if (currentLocId) {
    const loc = locs.find(l => l.id === currentLocId) || { id: currentLocId, name: currentLocId };
    nodes.push({ ...loc, x: CX, y: CY, current: true });
  }

  otherLocs.forEach((loc, i) => {
    const angle = (2 * Math.PI * i / otherLocs.length) - Math.PI / 2;
    nodes.push({ ...loc, x: CX + R * Math.cos(angle), y: CY + R * Math.sin(angle), current: false });
  });

  // Edges (dashed)
  const centerNode = nodes.find(n => n.current) || nodes[0];
  if (centerNode) {
    nodes.forEach(n => {
      if (n === centerNode) return;
      svg.appendChild(svgEl('line', {
        x1: centerNode.x, y1: centerNode.y,
        x2: n.x, y2: n.y,
        class: 'map-edge'
      }));
    });
  }

  // Location nodes with pixel-style icons
  nodes.forEach(n => {
    const r = n.current ? 48 : 36;
    const g = svgEl('g', { class: 'map-node-group', 'data-loc-id': n.id, style: 'cursor:pointer' });
    g.addEventListener('click', () => window.selectLocation(n.id));

    // Pulsing ring for current location
    if (n.current) {
      const pulseRing = svgEl('circle', {
        cx: n.x, cy: n.y, r: r + 4,
        fill: 'none', stroke: 'var(--gold)', 'stroke-width': '1.5', opacity: '0.3',
      });
      pulseRing.innerHTML = '<animate attributeName="r" values="' + (r+2) + ';' + (r+8) + ';' + (r+2) + '" dur="3s" repeatCount="indefinite"/><animate attributeName="opacity" values="0.3;0.1;0.3" dur="3s" repeatCount="indefinite"/>';
      g.appendChild(pulseRing);
    }

    // Base circle
    g.appendChild(svgEl('circle', {
      cx: n.x, cy: n.y, r,
      fill: n.current ? 'var(--gold)' : 'var(--text-muted)',
      stroke: n.current ? 'var(--gold)' : 'var(--border)',
      'stroke-width': n.current ? '2' : '1.5',
      class: 'map-node' + (n.current ? ' map-node-current' : ''),
      opacity: n.current ? '1' : '0.6',
    }));

    // Pixel-style location icon (small building/scene)
    drawLocationPixelIcon(g, n.x, n.y, n.id, n.current, n.visual_profile);

    // Draw player character in center of current location
    if (n.current) {
      drawPixelPlayerCharacter(g, n.x, n.y);
    }

    // Label
    const ly = n.y > CY + 20 ? n.y + r + 18 : n.y < CY - 20 ? n.y - r - 8 : n.y + r + 18;
    const label = svgEl('text', {
      x: n.x, y: ly,
      class: 'map-label' + (n.current ? ' map-label-current' : ''),
    });
    label.textContent = n.name || n.id;
    g.appendChild(label);

    svg.appendChild(g);

    // NPC pixel sprites around location
    const locNpcs = npcs.filter(npc => npc.default_location === n.id);

    // NPC count badge (non-current locations)
    if (locNpcs.length > 0 && !n.current) {
      const badgeX = n.x + r - 2, badgeY = n.y - r + 2;
      svg.appendChild(svgEl('circle', {
        cx: badgeX, cy: badgeY, r: 8,
        fill: 'var(--info)', stroke: 'var(--bg-primary)', 'stroke-width': '2',
      }));
      const badgeText = svgEl('text', {
        x: badgeX, y: badgeY + 3,
        fill: 'white', 'font-size': '9', 'text-anchor': 'middle', 'pointer-events': 'none', 'font-weight': '600',
      });
      badgeText.textContent = locNpcs.length;
      svg.appendChild(badgeText);
    }

    // NPC pixel sprites
    locNpcs.forEach((npc, ni) => {
      const npcAngle = (2 * Math.PI * ni / Math.max(locNpcs.length, 1)) + (n.current ? 0 : -Math.PI / 2);
      const npcR = r + 22;
      const nx = n.x + npcR * Math.cos(npcAngle);
      const ny = n.y + npcR * Math.sin(npcAngle);

      const spriteG = svgEl('g', {
        class: 'pixel-sprite',
        'data-npc-id': npc.id,
        transform: `translate(${nx},${ny})`,
      });
      spriteG.addEventListener('click', (e) => { e.stopPropagation(); window.selectNpc(npc.id); });
      spriteG.addEventListener('mouseenter', (e) => {
        showTooltip(e, npc);
        spriteG.querySelector('.sprite-body')?.classList.add('sprite-hover');
        spriteG.querySelector('.sprite-body')?.classList.remove('sprite-idle');
      });
      spriteG.addEventListener('mouseleave', () => {
        hideTooltip();
        const body = spriteG.querySelector('.sprite-body');
        if (body) { body.classList.remove('sprite-hover'); body.classList.add('sprite-idle'); }
      });

      // Draw pixel person
      const color = npcColor(npc.id);
      const bodyG = svgEl('g', { class: 'sprite-body sprite-idle' });
      drawPixelPerson(bodyG, color, npc.visual_profile);

      spriteG.appendChild(bodyG);

      // Name below sprite - adaptive to name length
      const fullName = npc.name || '?';
      const fontSize = fullName.length <= 4 ? '8' : fullName.length <= 6 ? '7' : '6';
      const nameLabel = svgEl('text', {
        x: 0, y: 12,
        fill: color, 'font-size': fontSize, 'text-anchor': 'middle',
        'pointer-events': 'none', 'font-weight': '600',
      });
      nameLabel.textContent = fullName;
      spriteG.appendChild(nameLabel);

      svg.appendChild(spriteG);
    });
  });
}

// Pixel-style location icons - using isometric cube style
function drawLocationPixelIcon(parent, cx, cy, locId, isCurrent, visualProfile) {
  const outline = isCurrent ? '#ffd700' : 'var(--text-muted)';

  // If AI-generated visual profile exists, use it
  if (visualProfile && visualProfile.svg) {
    const wrapper = svgEl('g', { transform: `translate(${cx},${cy})`, 'pointer-events': 'none' });
    wrapper.innerHTML = visualProfile.svg;
    parent.appendChild(wrapper);
    return;
  }

  // Fallback: use visual_profile colors if available, else defaults
  const vp = visualProfile || {};
  const baseColor = vp.base_color || (isCurrent ? '#ffed4e' : '#bbb');
  const darkColor = vp.dark_color || shadeColor(baseColor, -30);
  const lightColor = vp.light_color || shadeColor(baseColor, 30);
  const accentColor = vp.accent_color || '#ffff99';
  const shape = vp.shape || guessShapeFromId(locId);

  const g = svgEl('g', { 'pointer-events': 'none' });

  if (shape === 'cube') {
    drawIsoCube(g, cx, cy, 14, 16, baseColor, darkColor, lightColor, outline);
  } else if (shape === 'tall') {
    drawIsoCube(g, cx, cy, 10, 24, baseColor, darkColor, lightColor, outline);
    // Windows
    for (let row = 0; row < 3; row++) {
      g.appendChild(svgEl('rect', { x: cx - 3, y: cy - 8 + row * 6, width: 2.5, height: 2.5, fill: accentColor, opacity: '0.7', rx: 0.3 }));
      g.appendChild(svgEl('rect', { x: cx + 1, y: cy - 8 + row * 6, width: 2.5, height: 2.5, fill: accentColor, opacity: '0.7', rx: 0.3 }));
    }
  } else if (shape === 'wide') {
    drawIsoCube(g, cx, cy, 18, 12, baseColor, darkColor, lightColor, outline);
    // Door
    g.appendChild(svgEl('rect', { x: cx - 2, y: cy + 1, width: 4, height: 5, fill: darkColor, rx: 1 }));
  } else if (shape === 'pyramid') {
    // Pointed top
    g.appendChild(svgEl('polygon', {
      points: `${cx},${cy-16} ${cx-14},${cy+6} ${cx},${cy+12} ${cx+14},${cy+6}`,
      fill: baseColor, stroke: outline, 'stroke-width': '1'
    }));
    g.appendChild(svgEl('polygon', {
      points: `${cx},${cy-16} ${cx+14},${cy+6} ${cx},${cy+12}`,
      fill: darkColor, stroke: outline, 'stroke-width': '1'
    }));
  } else if (shape === 'dome') {
    g.appendChild(svgEl('ellipse', { cx, cy: cy - 2, rx: 12, ry: 8, fill: baseColor, stroke: outline, 'stroke-width': '1' }));
    g.appendChild(svgEl('rect', { x: cx - 12, y: cy, width: 24, height: 10, fill: darkColor, stroke: outline, 'stroke-width': '1' }));
    g.appendChild(svgEl('rect', { x: cx - 2, y: cy + 2, width: 4, height: 6, fill: accentColor, rx: 1 }));
  } else if (shape === 'multi') {
    // Multi-building cluster
    drawIsoCube(g, cx - 8, cy + 2, 10, 14, baseColor, darkColor, lightColor, outline);
    drawIsoCube(g, cx + 6, cy, 10, 20, shadeColor(baseColor, 10), shadeColor(darkColor, 10), shadeColor(lightColor, 10), outline);
  } else {
    // Default single cube
    drawIsoCube(g, cx, cy, 14, 16, baseColor, darkColor, lightColor, outline);
  }

  parent.appendChild(g);
}

function drawIsoCube(g, cx, cy, w, h, front, side, top, stroke) {
  const hw = w / 2;
  const hh = h / 2;
  const d = hw * 0.6;
  // Front face (diamond)
  g.appendChild(svgEl('polygon', {
    points: `${cx-hw},${cy} ${cx},${cy-hh} ${cx+hw},${cy} ${cx},${cy+hh}`,
    fill: front, stroke, 'stroke-width': '1'
  }));
  // Right face
  g.appendChild(svgEl('polygon', {
    points: `${cx+hw},${cy} ${cx+hw+d},${cy-d} ${cx+hw+d},${cy+hh-d} ${cx+hw},${cy+hh}`,
    fill: side, stroke, 'stroke-width': '1'
  }));
  // Top face
  g.appendChild(svgEl('polygon', {
    points: `${cx},${cy-hh} ${cx+d},${cy-hh-d} ${cx+hw+d},${cy-d} ${cx+hw},${cy}`,
    fill: top, stroke, 'stroke-width': '1'
  }));
}

// Pixel person with NPC-specific features
function drawPixelPerson(g, shirtColor, visualProfile) {
  const vp = visualProfile || {};
  const skin = vp.skin_color || '#f0d0a0';
  const legColor = vp.pant_color || '#444';
  const hairColor = vp.hair_color || '#555';
  const actualShirtColor = vp.shirt_color || shirtColor;
  const hair = { color: hairColor, type: vp.hair_style || 'short' };

  // Shadow
  g.appendChild(svgEl('ellipse', { cx: 0, cy: 5, rx: 5, ry: 1.5, fill: 'rgba(0,0,0,0.15)' }));
  // Legs
  g.appendChild(svgEl('rect', { x: -3.5, y: 0, width: 3, height: 5, rx: 0.5, fill: legColor }));
  g.appendChild(svgEl('rect', { x: 0.5, y: 0, width: 3, height: 5, rx: 0.5, fill: legColor }));
  // Shoes
  g.appendChild(svgEl('rect', { x: -4, y: 4, width: 3.5, height: 1.5, rx: 0.5, fill: '#222' }));
  g.appendChild(svgEl('rect', { x: 0.5, y: 4, width: 3.5, height: 1.5, rx: 0.5, fill: '#222' }));
  // Body
  g.appendChild(svgEl('rect', { x: -4.5, y: -8, width: 9, height: 9, rx: 1.5, fill: actualShirtColor }));
  // Arms
  g.appendChild(svgEl('rect', { x: -6.5, y: -7, width: 2.5, height: 6, rx: 1, fill: actualShirtColor }));
  g.appendChild(svgEl('rect', { x: 4, y: -7, width: 2.5, height: 6, rx: 1, fill: actualShirtColor }));
  // Hands
  g.appendChild(svgEl('circle', { cx: -5.5, cy: 0, r: 1.2, fill: skin }));
  g.appendChild(svgEl('circle', { cx: 5.5, cy: 0, r: 1.2, fill: skin }));
  // Head
  g.appendChild(svgEl('rect', { x: -3.5, y: -16, width: 7, height: 7, rx: 2, fill: skin }));
  // Eyes
  g.appendChild(svgEl('rect', { x: -2, y: -13, width: 1.5, height: 1.5, rx: 0.3, fill: '#333' }));
  g.appendChild(svgEl('rect', { x: 0.5, y: -13, width: 1.5, height: 1.5, rx: 0.3, fill: '#333' }));
  // Mouth
  g.appendChild(svgEl('rect', { x: -1, y: -10.5, width: 2, height: 0.8, rx: 0.4, fill: '#b07060' }));

  // Hair
  switch (hair.type) {
    case 'slick':
      g.appendChild(svgEl('rect', { x: -4, y: -17.5, width: 8, height: 3, rx: 1.5, fill: hair.color }));
      g.appendChild(svgEl('rect', { x: 3, y: -16, width: 1.5, height: 3, rx: 0.5, fill: hair.color }));
      break;
    case 'long':
      g.appendChild(svgEl('rect', { x: -4.5, y: -17.5, width: 9, height: 3, rx: 1.5, fill: hair.color }));
      g.appendChild(svgEl('rect', { x: -4.5, y: -15, width: 2, height: 7, rx: 0.8, fill: hair.color }));
      g.appendChild(svgEl('rect', { x: 2.5, y: -15, width: 2, height: 7, rx: 0.8, fill: hair.color }));
      break;
    case 'bun':
      g.appendChild(svgEl('rect', { x: -4, y: -17.5, width: 8, height: 3, rx: 1.5, fill: hair.color }));
      g.appendChild(svgEl('circle', { cx: 0, cy: -18.5, r: 2.5, fill: hair.color }));
      break;
    case 'buzz':
      g.appendChild(svgEl('rect', { x: -3.5, y: -17, width: 7, height: 2.5, rx: 1, fill: hair.color }));
      break;
    default: // short
      g.appendChild(svgEl('rect', { x: -4, y: -17, width: 8, height: 3, rx: 1.5, fill: hair.color }));
      break;
  }

  // Accessories per NPC type
  if (vp.accessory === 'tie') {
    // Tie
    g.appendChild(svgEl('rect', { x: -0.5, y: -7, width: 1, height: 5, fill: '#c22' }));
    g.appendChild(svgEl('polygon', { points: '-1.5,-7 1.5,-7 0,-5.5', fill: '#c22' }));
  } else if (vp.accessory === 'whistle') {
    // Whistle
    g.appendChild(svgEl('circle', { cx: 2, cy: -4, r: 1.2, fill: '#ccc', stroke: '#999', 'stroke-width': '0.4' }));
    g.appendChild(svgEl('line', { x1: 0, y1: -7, x2: 2, y2: -4, stroke: '#999', 'stroke-width': '0.5' }));
  } else if (vp.accessory === 'earrings') {
    // Earrings
    g.appendChild(svgEl('circle', { cx: -3.5, cy: -11, r: 0.8, fill: '#e6a817' }));
    g.appendChild(svgEl('circle', { cx: 3.5, cy: -11, r: 0.8, fill: '#e6a817' }));
  }
}

// Player character pixel sprite - drawn at current location center
function drawPixelPlayerCharacter(parent, cx, cy) {
  const g = svgEl('g', { transform: `translate(${cx},${cy + 16})`, 'pointer-events': 'none' });
  const bodyG = svgEl('g', { class: 'sprite-body sprite-idle' });

  const skin = '#f0d0a0';
  const shirtColor = '#2196F3';
  const pantsColor = '#1a1a2e';
  const hairColor = '#222';
  const capeColor = '#e53935';

  // Shadow
  bodyG.appendChild(svgEl('ellipse', { cx: 0, cy: 6, rx: 6, ry: 2, fill: 'rgba(0,0,0,0.2)' }));
  // Legs
  bodyG.appendChild(svgEl('rect', { x: -4, y: 0, width: 3.5, height: 6, rx: 0.5, fill: pantsColor }));
  bodyG.appendChild(svgEl('rect', { x: 0.5, y: 0, width: 3.5, height: 6, rx: 0.5, fill: pantsColor }));
  // Shoes
  bodyG.appendChild(svgEl('rect', { x: -4.5, y: 5, width: 4, height: 2, rx: 0.5, fill: '#8B4513' }));
  bodyG.appendChild(svgEl('rect', { x: 0.5, y: 5, width: 4, height: 2, rx: 0.5, fill: '#8B4513' }));
  // Body
  bodyG.appendChild(svgEl('rect', { x: -5, y: -9, width: 10, height: 10, rx: 1.5, fill: shirtColor }));
  // Cape (behind body)
  bodyG.appendChild(svgEl('polygon', { points: '-5,-8 -7,-1 -6,5 -5,1', fill: capeColor, opacity: '0.8' }));
  bodyG.appendChild(svgEl('polygon', { points: '5,-8 7,-1 6,5 5,1', fill: capeColor, opacity: '0.6' }));
  // Arms
  bodyG.appendChild(svgEl('rect', { x: -7.5, y: -8, width: 3, height: 7, rx: 1, fill: shirtColor }));
  bodyG.appendChild(svgEl('rect', { x: 4.5, y: -8, width: 3, height: 7, rx: 1, fill: shirtColor }));
  // Hands
  bodyG.appendChild(svgEl('circle', { cx: -6, cy: 0, r: 1.5, fill: skin }));
  bodyG.appendChild(svgEl('circle', { cx: 6, cy: 0, r: 1.5, fill: skin }));
  // Head
  bodyG.appendChild(svgEl('rect', { x: -4, y: -17, width: 8, height: 8, rx: 2, fill: skin }));
  // Eyes
  bodyG.appendChild(svgEl('rect', { x: -2.5, y: -14, width: 2, height: 2, rx: 0.5, fill: '#1a237e' }));
  bodyG.appendChild(svgEl('rect', { x: 0.5, y: -14, width: 2, height: 2, rx: 0.5, fill: '#1a237e' }));
  // Eye highlights
  bodyG.appendChild(svgEl('rect', { x: -2, y: -14, width: 0.8, height: 0.8, fill: '#fff' }));
  bodyG.appendChild(svgEl('rect', { x: 1, y: -14, width: 0.8, height: 0.8, fill: '#fff' }));
  // Mouth (smile)
  bodyG.appendChild(svgEl('path', { d: 'M-1,-10.5 Q0,-9 1,-10.5', fill: 'none', stroke: '#b07060', 'stroke-width': '0.8' }));
  // Hair
  bodyG.appendChild(svgEl('rect', { x: -4.5, y: -18.5, width: 9, height: 4, rx: 2, fill: hairColor }));
  bodyG.appendChild(svgEl('rect', { x: -5, y: -17, width: 2, height: 3, rx: 0.5, fill: hairColor }));
  bodyG.appendChild(svgEl('rect', { x: 3, y: -17, width: 2, height: 3, rx: 0.5, fill: hairColor }));
  // Belt
  bodyG.appendChild(svgEl('rect', { x: -5, y: -1, width: 10, height: 2, rx: 0.5, fill: '#5D4037' }));
  bodyG.appendChild(svgEl('rect', { x: -1, y: -1.5, width: 2, height: 2.5, rx: 0.5, fill: '#FFD700' }));

  g.appendChild(bodyG);

  // "你" label
  const nameLabel = svgEl('text', {
    x: 0, y: 14,
    fill: '#2196F3', 'font-size': '9', 'text-anchor': 'middle',
    'font-weight': '700',
  });
  nameLabel.textContent = '你';
  g.appendChild(nameLabel);

  parent.appendChild(g);
}

function showTooltip(e, npc) {
  const tt = document.getElementById('map-tooltip');
  const area = document.getElementById('map-area');
  const rect = area.getBoundingClientRect();
  tt.querySelector('#tooltip-name').textContent = npc.name;
  tt.querySelector('#tooltip-title').textContent = npc.title || '';
  tt.style.left = (e.clientX - rect.left + 12) + 'px';
  tt.style.top = (e.clientY - rect.top - 10) + 'px';
  tt.classList.add('show');
}

function hideTooltip() {
  document.getElementById('map-tooltip').classList.remove('show');
}

// ===== Init =====

export function initMapRenderer() {
  window.renderMap = renderMap;
  window.showTooltip = showTooltip;
  window.hideTooltip = hideTooltip;
}
