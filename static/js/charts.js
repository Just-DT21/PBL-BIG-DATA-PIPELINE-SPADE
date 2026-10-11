// Hand-drawn SVG charts. No charting dependency: the marks are simple enough to
// generate directly, and that keeps the dashboard working offline.

import { h, fmt } from './util.js';

const NS = 'http://www.w3.org/2000/svg';

function svg(width, height, ...children) {
  const node = document.createElementNS(NS, 'svg');
  node.setAttribute('viewBox', `0 0 ${width} ${height}`);
  node.setAttribute('class', 'chart');
  node.setAttribute('role', 'img');
  node.setAttribute('preserveAspectRatio', 'xMidYMid meet');
  for (const child of children) if (child) node.appendChild(child);
  return node;
}

function rect(x, y, width, height, className) {
  const node = document.createElementNS(NS, 'rect');
  node.setAttribute('x', x);
  node.setAttribute('y', y);
  node.setAttribute('width', Math.max(0, width));
  node.setAttribute('height', Math.max(0, height));
  node.setAttribute('rx', 3);
  node.setAttribute('class', className);
  return node;
}

function text(x, y, content, className = 'label', anchor = 'start') {
  const node = document.createElementNS(NS, 'text');
  node.setAttribute('x', x);
  node.setAttribute('y', y);
  node.setAttribute('text-anchor', anchor);
  node.setAttribute('class', className);
  node.textContent = content;
  return node;
}

function line(x1, y1, x2, y2) {
  const node = document.createElementNS(NS, 'line');
  node.setAttribute('x1', x1);
  node.setAttribute('y1', y1);
  node.setAttribute('x2', x2);
  node.setAttribute('y2', y2);
  node.setAttribute('class', 'axis');
  return node;
}

/**
 * Grouped before/after bars with the delta called out.
 * `lowerIsBetter` colours the change green when it shrinks.
 */
export function pairBars({ title, before, after, format = fmt.int, lowerIsBetter = true, note = '', reduction = undefined }) {
  const width = 460;
  const height = 168;
  const chartTop = 34;
  const chartBottom = 118;
  const max = Math.max(Number(before) || 0, Number(after) || 0, 1);
  const barWidth = 92;
  const beforeHeight = ((Number(before) || 0) / max) * (chartBottom - chartTop);
  const afterHeight = ((Number(after) || 0) / max) * (chartBottom - chartTop);
  const delta = Number(before) - Number(after);
  // When the pipeline supplies the percentage it is authoritative: it is null
  // for differences below the measurement resolution, and the chart must not
  // invent a figure the report deliberately withheld.
  const pct = reduction === undefined
    ? (Number(before) ? (delta / Number(before)) * 100 : null)
    : reduction;
  const improved = lowerIsBetter ? delta > 0 : delta < 0;
  const deltaLabel = pct === null || pct === undefined
    ? 'n/a'
    : `${pct > 0 ? '' : '+'}${Number(pct).toFixed(2)}%`;

  return svg(
    width,
    height,
    text(0, 12, title || '', 'label'),
    text(width, 12, deltaLabel, 'label', 'end'),
    line(0, chartBottom, width, chartBottom),
    rect(58, chartBottom - beforeHeight, barWidth, beforeHeight, 'bar-base'),
    rect(58 + barWidth + 34, chartBottom - afterHeight, barWidth, afterHeight, improved ? 'bar-good' : 'bar-warn'),
    text(58 + barWidth / 2, chartBottom + 16, 'baseline', 'label', 'middle'),
    text(58 + barWidth * 1.5 + 34, chartBottom + 16, 'optimized', 'label', 'middle'),
    text(58 + barWidth / 2, chartBottom - beforeHeight - 6, format(before), 'label', 'middle'),
    text(58 + barWidth * 1.5 + 34, chartBottom - afterHeight - 6, format(after), 'label', 'middle'),
    note ? text(0, chartBottom + 34, note, '', 'start') : null
  );
}

/** Donut showing a percentage. */
export function ring({ value, label, caption = '', good = true, size = 132 }) {
  const radius = 46;
  const circumference = 2 * Math.PI * radius;
  const clamped = value === null || value === undefined ? 0 : Math.max(0, Math.min(100, Math.abs(Number(value))));
  const dash = (clamped / 100) * circumference;
  const center = size / 2;

  const track = document.createElementNS(NS, 'circle');
  track.setAttribute('cx', center);
  track.setAttribute('cy', center);
  track.setAttribute('r', radius);
  track.setAttribute('fill', 'none');
  track.setAttribute('stroke-width', 10);
  track.setAttribute('class', 'ring-track');

  const arc = document.createElementNS(NS, 'circle');
  arc.setAttribute('cx', center);
  arc.setAttribute('cy', center);
  arc.setAttribute('r', radius);
  arc.setAttribute('fill', 'none');
  arc.setAttribute('stroke-width', 10);
  arc.setAttribute('stroke-dasharray', `${dash} ${circumference - dash}`);
  arc.setAttribute('transform', `rotate(-90 ${center} ${center})`);
  arc.setAttribute('class', `ring-value ${good ? 'good' : 'bad'}`);

  const displayed = value === null || value === undefined ? 'n/a' : `${Number(value).toFixed(1)}%`;
  return svg(
    size,
    size,
    track,
    arc,
    text(center, center + 4, displayed, 'ring-text', 'middle'),
    text(center, center + 20, label || '', 'label', 'middle'),
    caption ? text(center, size - 4, caption, 'label', 'middle') : null
  );
}

/** Horizontal mini bars, used for per-column or per-record distributions. */
export function miniBars({ rows, format = fmt.int, width = 420, rowHeight = 26 }) {
  const height = Math.max(rowHeight, rows.length * rowHeight + 8);
  const max = Math.max(...rows.map((row) => Number(row.value) || 0), 1);
  const barLeft = 132;
  const barWidth = width - barLeft - 78;
  const children = [];
  rows.forEach((row, index) => {
    const y = index * rowHeight + 6;
    const width2 = (Math.max(0, Number(row.value) || 0) / max) * barWidth;
    children.push(text(0, y + 12, String(row.label).slice(0, 20), 'label', 'start'));
    children.push(rect(barLeft, y + 2, barWidth, 12, 'bar-base'));
    children.push(rect(barLeft, y + 2, width2, 12, row.className || 'bar-opt'));
    children.push(text(width, y + 12, format(row.value), 'label', 'end'));
  });
  return svg(width, height, ...children);
}
