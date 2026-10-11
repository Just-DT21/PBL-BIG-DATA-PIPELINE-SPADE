// Hand-drawn SVG charts for the AI Command Center.
//
// Same approach as static/js/charts.js: no charting dependency, the marks are
// simple enough to generate directly and the page then works fully offline.
// The activity chart is interactive: pointer, keyboard and touch all move a
// guide line, clicking (or Enter) pins a day and reports it back to the caller.

import { compactNumber } from './data.js';

const NS = 'http://www.w3.org/2000/svg';

let uid = 0;

function el(tag, attrs = {}) {
  const node = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    node.setAttribute(key, String(value));
  }
  return node;
}

function textEl(x, y, content, className, anchor = 'start') {
  const node = el('text', { x, y, class: className, 'text-anchor': anchor });
  node.textContent = content;
  return node;
}

/** Round a maximum up to a readable axis value: 1, 2, 2.5 or 5 × 10ⁿ. */
function niceMax(value) {
  if (!value || value <= 0) return 1;
  const exponent = Math.floor(Math.log10(value));
  const base = 10 ** exponent;
  const scaled = value / base;
  const step = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 2.5 ? 2.5 : scaled <= 5 ? 5 : 10;
  return step * base;
}

function smoothPath(points) {
  if (points.length === 0) return '';
  if (points.length === 1) return `M ${points[0].x} ${points[0].y}`;
  let d = `M ${points[0].x} ${points[0].y}`;
  for (let i = 1; i < points.length; i += 1) {
    const previous = points[i - 1];
    const current = points[i];
    const midX = (previous.x + current.x) / 2;
    d += ` C ${midX} ${previous.y} ${midX} ${current.y} ${current.x} ${current.y}`;
  }
  return d;
}

/**
 * Mini trend line for a stat card.
 * `values` is a plain list of numbers.
 */
export function sparkline(values, { width = 132, height = 38, up = true } = {}) {
  const list = Array.isArray(values) && values.length ? values : [0];
  const min = Math.min(...list);
  const max = Math.max(...list);
  const span = max - min || 1;
  const points = list.map((value, index) => ({
    x: list.length === 1 ? width / 2 : (index / (list.length - 1)) * width,
    y: height - 4 - ((value - min) / span) * (height - 10),
  }));
  const line = smoothPath(points);
  const gradientId = `spark-${(uid += 1)}`;
  const svg = el('svg', {
    viewBox: `0 0 ${width} ${height}`,
    class: `cc-spark ${up ? 'is-up' : 'is-down'}`,
    preserveAspectRatio: 'none',
    role: 'presentation',
    'aria-hidden': 'true',
  });
  const defs = el('defs');
  const gradient = el('linearGradient', { id: gradientId, x1: '0', y1: '0', x2: '0', y2: '1' });
  gradient.append(
    el('stop', { offset: '0', 'stop-color': 'currentColor', 'stop-opacity': '0.34' }),
    el('stop', { offset: '1', 'stop-color': 'currentColor', 'stop-opacity': '0' })
  );
  defs.append(gradient);
  svg.append(
    defs,
    el('path', {
      d: `${line} L ${points[points.length - 1].x} ${height} L ${points[0].x} ${height} Z`,
      fill: `url(#${gradientId})`,
      stroke: 'none',
    }),
    el('path', { d: line, fill: 'none', class: 'cc-spark-line' })
  );
  return svg;
}

const SERIES_LABELS = {
  requests: 'API requests (simulated)',
  tasks: 'Tasks completed',
  errors: 'Errors',
};

/**
 * Interactive activity chart.
 *
 * @param {HTMLElement} container positioned element the chart is drawn into.
 * @param {object} options
 *   series    full 30-day series (already generated in data.js)
 *   range     number of trailing days to plot
 *   visible   { requests, tasks, errors }
 *   selected  index of the pinned day, or null
 *   onSelect  (index|null) => void
 */
export function createActivityChart(container, options) {
  let current = { ...options };
  let hoverIndex = null;
  let frame = null;
  let observer = null;

  const tooltip = document.createElement('div');
  tooltip.className = 'cc-chart-tip glass';
  tooltip.hidden = true;
  container.append(tooltip);

  function sliceSeries() {
    const series = current.series || [];
    const range = Math.min(current.range || series.length, series.length);
    return series.slice(series.length - range);
  }

  function draw() {
    for (const node of Array.from(container.querySelectorAll('svg'))) node.remove();

    const rows = sliceSeries();
    if (rows.length === 0) return;

    const width = Math.max(320, Math.round(container.clientWidth || 720));
    const height = 304;
    const pad = { top: 20, right: 56, bottom: 36, left: 58 };
    const plotWidth = Math.max(40, width - pad.left - pad.right);
    const plotHeight = Math.max(40, height - pad.top - pad.bottom);

    const shown = current.visible || { requests: true, tasks: true, errors: false };
    const requestMax = niceMax(Math.max(...rows.map((row) => row.requests), 1) * 1.12);
    const taskMax = niceMax(Math.max(...rows.map((row) => row.tasks), 1) * 1.35);
    const errorMax = niceMax(Math.max(...rows.map((row) => row.errors), 1) * 1.6);

    const stepX = rows.length > 1 ? plotWidth / (rows.length - 1) : 0;
    const xAt = (index) => pad.left + (rows.length > 1 ? index * stepX : plotWidth / 2);
    const yRequests = (value) => pad.top + plotHeight - (value / requestMax) * plotHeight;
    const yTasks = (value) => pad.top + plotHeight - (value / taskMax) * plotHeight;
    const yErrors = (value) => pad.top + plotHeight - (value / errorMax) * plotHeight;

    const chartId = `cc-chart-${(uid += 1)}`;
    const svg = el('svg', {
      viewBox: `0 0 ${width} ${height}`,
      width,
      height,
      class: 'cc-chart-svg',
      role: 'img',
      tabindex: '0',
      'aria-label': `Activity for the last ${rows.length} days. Use the left and right arrow keys to inspect a day.`,
    });

    const defs = el('defs');
    const areaGradient = el('linearGradient', { id: `${chartId}-area`, x1: '0', y1: '0', x2: '0', y2: '1' });
    areaGradient.append(
      el('stop', { offset: '0', 'stop-color': 'var(--cc-blue)', 'stop-opacity': '0.42' }),
      el('stop', { offset: '1', 'stop-color': 'var(--cc-blue)', 'stop-opacity': '0' })
    );
    defs.append(areaGradient);
    svg.append(defs);

    // --- grid + axes -------------------------------------------------------
    const ticks = 4;
    for (let i = 0; i <= ticks; i += 1) {
      const y = pad.top + (plotHeight / ticks) * i;
      svg.append(el('line', { x1: pad.left, y1: y, x2: pad.left + plotWidth, y2: y, class: 'cc-grid' }));
      svg.append(textEl(pad.left - 10, y + 4, compactNumber(requestMax * (1 - i / ticks)), 'cc-axis', 'end'));
      if (shown.tasks) {
        svg.append(textEl(pad.left + plotWidth + 10, y + 4, compactNumber(taskMax * (1 - i / ticks)), 'cc-axis cc-axis-right', 'start'));
      }
    }

    // --- tasks (bars, right axis) -----------------------------------------
    if (shown.tasks) {
      const barWidth = Math.max(3, Math.min(22, stepX * 0.42));
      rows.forEach((row, index) => {
        const x = xAt(index) - barWidth / 2;
        const y = yTasks(row.tasks);
        svg.append(
          el('rect', {
            x,
            y,
            width: barWidth,
            height: Math.max(1, pad.top + plotHeight - y),
            rx: Math.min(4, barWidth / 2),
            class: 'cc-bar',
            style: `animation-delay:${index * 14}ms`,
          })
        );
      });
    }

    // --- requests (area + line, left axis) --------------------------------
    if (shown.requests) {
      const points = rows.map((row, index) => ({ x: xAt(index), y: yRequests(row.requests) }));
      const line = smoothPath(points);
      svg.append(
        el('path', {
          d: `${line} L ${points[points.length - 1].x} ${pad.top + plotHeight} L ${points[0].x} ${pad.top + plotHeight} Z`,
          fill: `url(#${chartId}-area)`,
          stroke: 'none',
        })
      );
      const path = el('path', { d: line, fill: 'none', class: 'cc-line' });
      svg.append(path);
      const length = typeof path.getTotalLength === 'function' ? path.getTotalLength() : 0;
      if (length) {
        path.style.strokeDasharray = String(length);
        path.style.strokeDashoffset = String(length);
        path.style.animation = 'cc-draw 900ms cubic-bezier(.4,0,.2,1) forwards';
      }
    }

    // --- errors (thin line, own scale) ------------------------------------
    if (shown.errors) {
      const points = rows.map((row, index) => ({ x: xAt(index), y: yErrors(row.errors) }));
      svg.append(el('path', { d: smoothPath(points), fill: 'none', class: 'cc-line-errors' }));
    }

    // --- x labels ----------------------------------------------------------
    const labelStep = Math.max(1, Math.ceil(rows.length / (width < 560 ? 4 : 7)));
    rows.forEach((row, index) => {
      if (index % labelStep !== 0 && index !== rows.length - 1) return;
      svg.append(textEl(xAt(index), pad.top + plotHeight + 22, row.label, 'cc-axis', 'middle'));
    });

    // --- interactive layer -------------------------------------------------
    const hoverLine = el('line', {
      x1: 0,
      y1: pad.top,
      x2: 0,
      y2: pad.top + plotHeight,
      class: 'cc-guide',
      opacity: 0,
    });
    const requestDot = el('circle', { r: 5, class: 'cc-dot', opacity: 0 });
    const taskDot = el('circle', { r: 4.5, class: 'cc-dot cc-dot-task', opacity: 0 });
    const pinDot = el('circle', { r: 9, class: 'cc-pin', opacity: 0 });
    const overlay = el('rect', {
      x: pad.left,
      y: pad.top,
      width: plotWidth,
      height: plotHeight,
      fill: 'transparent',
      class: 'cc-overlay',
    });
    svg.append(hoverLine, pinDot, requestDot, taskDot, overlay);

    container.append(svg);

    // --- helpers over the drawn chart -------------------------------------
    const pointFor = (index) => {
      const row = rows[index];
      return {
        row,
        index,
        x: xAt(index),
        requestY: yRequests(row.requests),
        taskY: yTasks(row.tasks),
      };
    };

    function placeTooltip(index) {
      const point = pointFor(index);
      tooltip.hidden = false;
      tooltip.innerHTML =
        `<strong>${point.row.weekday}, ${point.row.label}</strong>` +
        `<span><i class="cc-k cc-k-blue"></i>${point.row.requests.toLocaleString()} requests</span>` +
        `<span><i class="cc-k cc-k-violet"></i>${point.row.tasks} tasks completed</span>` +
        `<span><i class="cc-k cc-k-amber"></i>${point.row.errors} errors · ${point.row.latency} ms avg</span>`;
      const scale = (container.clientWidth || width) / width;
      const left = Math.min(Math.max(point.x * scale, 96), (container.clientWidth || width) - 96);
      tooltip.style.left = `${left}px`;
      tooltip.style.top = `${Math.max(6, point.requestY * scale - 12)}px`;
      tooltip.classList.toggle('is-below', point.requestY * scale < 120);
    }

    function paint(index, { pinned = false } = {}) {
      if (index === null || index < 0 || index >= rows.length) {
        hoverLine.setAttribute('opacity', 0);
        requestDot.setAttribute('opacity', 0);
        taskDot.setAttribute('opacity', 0);
        pinDot.setAttribute('opacity', 0);
        tooltip.hidden = true;
        return;
      }
      const point = pointFor(index);
      hoverLine.setAttribute('x1', point.x);
      hoverLine.setAttribute('x2', point.x);
      hoverLine.setAttribute('opacity', 1);
      requestDot.setAttribute('cx', point.x);
      requestDot.setAttribute('cy', point.requestY);
      requestDot.setAttribute('opacity', shown.requests ? 1 : 0);
      taskDot.setAttribute('cx', point.x);
      taskDot.setAttribute('cy', point.taskY);
      taskDot.setAttribute('opacity', shown.tasks ? 1 : 0);
      pinDot.setAttribute('cx', point.x);
      pinDot.setAttribute('cy', point.requestY);
      pinDot.setAttribute('opacity', pinned ? 1 : 0);
      placeTooltip(index);
    }

    function indexFromEvent(event) {
      const rect = svg.getBoundingClientRect();
      const scale = width / rect.width;
      const x = (event.clientX - rect.left) * scale;
      const raw = rows.length > 1 ? (x - pad.left) / stepX : 0;
      return Math.max(0, Math.min(rows.length - 1, Math.round(raw)));
    }

    overlay.addEventListener('pointermove', (event) => {
      hoverIndex = indexFromEvent(event);
      paint(hoverIndex);
    });
    overlay.addEventListener('pointerleave', () => {
      hoverIndex = null;
      paint(current.selected === null || current.selected === undefined ? null : current.selected, { pinned: true });
    });
    overlay.addEventListener('click', (event) => {
      const index = indexFromEvent(event);
      current.onSelect?.(current.selected === index ? null : index);
    });

    svg.addEventListener('keydown', (event) => {
      const start = hoverIndex ?? current.selected ?? rows.length - 1;
      if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
        event.preventDefault();
        hoverIndex = Math.max(0, Math.min(rows.length - 1, start + (event.key === 'ArrowRight' ? 1 : -1)));
        paint(hoverIndex);
      } else if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        current.onSelect?.(current.selected === hoverIndex ? null : hoverIndex);
      } else if (event.key === 'Escape') {
        event.preventDefault();
        hoverIndex = null;
        current.onSelect?.(null);
      }
    });
    svg.addEventListener('blur', () => {
      hoverIndex = null;
      paint(current.selected === null || current.selected === undefined ? null : current.selected, { pinned: true });
    });

    // Restore the pinned day (if any) after a redraw.
    if (typeof current.selected === 'number' && current.selected < rows.length) {
      paint(current.selected, { pinned: true });
    }
  }

  function schedule() {
    if (frame) cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => {
      frame = null;
      draw();
    });
  }

  draw();
  if (typeof ResizeObserver === 'function') {
    observer = new ResizeObserver(schedule);
    observer.observe(container);
  }

  return {
    update(next) {
      current = { ...current, ...next };
      draw();
    },
    destroy() {
      if (observer) observer.disconnect();
      if (frame) cancelAnimationFrame(frame);
      container.replaceChildren();
    },
  };
}

export const seriesLabels = SERIES_LABELS;
