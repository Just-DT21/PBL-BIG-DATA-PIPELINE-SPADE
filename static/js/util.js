// Small DOM and formatting helpers. No framework: the interface is built from
// plain elements so there is no build step and nothing to hydrate.

export function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key === 'html') node.innerHTML = value;
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
    else if (key === 'value') node.value = value;
    else if (value === true) node.setAttribute(key, '');
    else node.setAttribute(key, value);
  }
  append(node, children);
  return node;
}

function append(node, children) {
  for (const child of children.flat(4)) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function frag(...children) {
  const fragment = document.createDocumentFragment();
  append(fragment, children);
  return fragment;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function mount(node, ...children) {
  clear(node);
  append(node, children);
  return node;
}

export const fmt = {
  bytes(value) {
    if (value === null || value === undefined || Number.isNaN(value)) return 'n/a';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let size = Number(value);
    let index = 0;
    while (Math.abs(size) >= 1024 && index < units.length - 1) {
      size /= 1024;
      index += 1;
    }
    return `${index === 0 ? Math.round(size) : size.toFixed(2)} ${units[index]}`;
  },
  seconds(value) {
    if (value === null || value === undefined || Number.isNaN(value)) return 'n/a';
    const seconds = Number(value);
    if (seconds < 1) return `${(seconds * 1000).toFixed(1)} ms`;
    if (seconds < 60) return `${seconds.toFixed(3)} s`;
    return `${Math.floor(seconds / 60)}m ${(seconds % 60).toFixed(1)}s`;
  },
  pct(value, digits = 2) {
    if (value === null || value === undefined || Number.isNaN(value)) return 'n/a';
    const sign = value > 0 ? '+' : '';
    return `${sign}${Number(value).toFixed(digits)}%`;
  },
  int(value) {
    if (value === null || value === undefined || Number.isNaN(value)) return 'n/a';
    return Number(value).toLocaleString();
  },
  date(value) {
    if (!value) return 'n/a';
    return new Date(value * 1000).toLocaleString();
  },
  rel(value) {
    if (!value) return 'n/a';
    const delta = Date.now() / 1000 - value;
    if (delta < 60) return 'just now';
    if (delta < 3600) return `${Math.floor(delta / 60)} min ago`;
    if (delta < 86400) return `${Math.floor(delta / 3600)} h ago`;
    return `${Math.floor(delta / 86400)} d ago`;
  },
  percentOf(part, whole) {
    if (!whole) return 0;
    return Math.min(100, Math.max(0, (part / whole) * 100));
  },
};

export function statusBadge(status) {
  const map = {
    succeeded: 'ok',
    analyzed: 'ok',
    failed: 'err',
    analysis_failed: 'err',
    running: 'info',
    queued: 'neutral',
    uploaded: 'neutral',
  };
  return h('span', { class: `badge ${map[status] || 'neutral'}` }, status || 'unknown');
}

export function severityBadge(severity) {
  const map = { ok: 'ok', info: 'info', warn: 'warn', error: 'err' };
  return h('span', { class: `badge ${map[severity] || 'neutral'}` }, severity || 'info');
}

export function toast(message, kind = '') {
  const holder = document.getElementById('toasts');
  if (!holder) return;
  const node = h('div', { class: `toast ${kind}` }, message);
  holder.appendChild(node);
  setTimeout(() => {
    node.style.opacity = '0';
    node.style.transition = 'opacity .3s';
    setTimeout(() => node.remove(), 320);
  }, 4200);
}

export function confirmAction(message) {
  return window.confirm(message);
}

export function option(value, label, selected) {
  return h('option', { value, selected: selected ? 'selected' : null }, label);
}

export function multiSelectOptions(values, selectedValues, size = 8) {
  const selected = new Set(selectedValues || []);
  return values.map((value) => h('option', { value, selected: selected.has(value) ? 'selected' : null }, value));
}

export function readableList(values, max = 6) {
  if (!values || !values.length) return 'none';
  const shown = values.slice(0, max).join(', ');
  return values.length > max ? `${shown} and ${values.length - max} more` : shown;
}
