// Demo dataset for the AI Command Center.
//
// Everything here is generated locally from a fixed seed: no network request is
// made and no model, key or provider is configured. The series is seeded so the
// numbers stay identical across reloads instead of jittering on every render.

const DAY_MS = 86400000;

/** Deterministic PRNG (mulberry32) so demo figures are stable. */
function rng(seed) {
  let a = seed >>> 0;
  return function next() {
    a += 0x6d2b79f5;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export const DEMO_NOTICE =
  'Demo workspace. Every figure is sample data generated in the browser, and every model below is a ' +
  'placeholder: nothing is connected to a live API and no key is stored anywhere.';

export const STORAGE_NOTE = 'Tasks, preferences and the local activity log persist in this browser only.';

// ---------------------------------------------------------------------------
// Model catalogue (explicitly demo-only placeholders)
// ---------------------------------------------------------------------------

export const modelCatalog = [
  {
    id: 'nova-3-mini-demo',
    name: 'Nova 3 Mini',
    tag: 'DEMO',
    family: 'Nova',
    context: '32K tokens',
    throughput: '~180 tok/s',
    best_for: 'Routing and classification',
    tone: 'blue',
  },
  {
    id: 'atlas-2-pro-demo',
    name: 'Atlas 2 Pro',
    tag: 'DEMO',
    family: 'Atlas',
    context: '128K tokens',
    throughput: '~72 tok/s',
    best_for: 'Long-form reasoning and reports',
    tone: 'violet',
  },
  {
    id: 'helios-vision-demo',
    name: 'Helios Vision',
    tag: 'DEMO',
    family: 'Helios',
    context: '64K tokens',
    throughput: '~40 tok/s',
    best_for: 'Document and screenshot analysis',
    tone: 'cyan',
  },
  {
    id: 'orbit-edge-1b-demo',
    name: 'Orbit Edge 1B',
    tag: 'DEMO',
    family: 'Orbit',
    context: '8K tokens',
    throughput: '~310 tok/s',
    best_for: 'On-device style batch jobs',
    tone: 'green',
  },
];

export const DEFAULT_MODEL_ID = 'nova-3-mini-demo';

export function modelById(id) {
  return modelCatalog.find((model) => model.id === id) || modelCatalog[0];
}

// ---------------------------------------------------------------------------
// 30-day activity series
// ---------------------------------------------------------------------------

function dayKey(date) {
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${date.getFullYear()}-${month}-${day}`;
}

/**
 * Thirty days of plausible traffic: weekday peaks, weekend troughs, a slow
 * upward trend and a little noise.
 */
export function buildSeries(days = 30) {
  const random = rng(20261012);
  const series = [];
  const today = new Date();
  today.setHours(0, 0, 0, 0);

  for (let offset = days - 1; offset >= 0; offset -= 1) {
    const date = new Date(today.getTime() - offset * DAY_MS);
    const weekday = date.getDay();
    const weekend = weekday === 0 || weekday === 6;
    const trend = 1 + (days - offset) * 0.006;
    const noise = 0.9 + random() * 0.2;
    const shape = weekend ? 0.42 : 0.88 + (weekday % 3) * 0.075;
    const requests = Math.round(7600 * shape * trend * noise);
    const tasks = Math.max(3, Math.round(requests / (157 + random() * 26)));
    const errorRate = weekend ? 0.004 + random() * 0.006 : 0.006 + random() * 0.011;
    const errors = Math.round(requests * errorRate);
    const latency = Math.round(430 + random() * 260 + (weekend ? -40 : 60));
    series.push({
      key: dayKey(date),
      date,
      label: date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }),
      weekday: date.toLocaleDateString(undefined, { weekday: 'short' }),
      requests,
      tasks,
      errors,
      latency,
      spend: Number((requests * 0.00042).toFixed(2)),
    });
  }
  return series;
}

export const ACTIVITY_SERIES = buildSeries(30);

function sum(rows, key) {
  return rows.reduce((total, row) => total + row[key], 0);
}

function deltaPct(current, previous) {
  if (!previous) return null;
  return ((current - previous) / previous) * 100;
}

/** Split a series in half and compare the two windows. */
export function seriesOverview(series = ACTIVITY_SERIES) {
  const half = Math.floor(series.length / 2);
  const previous = series.slice(0, half);
  const current = series.slice(half);
  const requests = sum(series, 'requests');
  const tasks = sum(series, 'tasks');
  const errors = sum(series, 'errors');
  const successRate = requests ? ((requests - errors) / requests) * 100 : 0;
  const rateOf = (rows) => {
    const total = sum(rows, 'requests');
    return total ? ((total - sum(rows, 'errors')) / total) * 100 : 0;
  };
  return {
    requests,
    tasks,
    errors,
    successRate,
    latency: Math.round(sum(series, 'latency') / series.length),
    spend: Number(sum(series, 'spend').toFixed(2)),
    deltas: {
      requests: deltaPct(sum(current, 'requests'), sum(previous, 'requests')),
      tasks: deltaPct(sum(current, 'tasks'), sum(previous, 'tasks')),
      errors: deltaPct(sum(current, 'errors'), sum(previous, 'errors')),
      latency: deltaPct(
        sum(current, 'latency') / current.length,
        sum(previous, 'latency') / previous.length
      ),
      // Success rate moves in percentage points, not percent.
      successRatePp: previous.length && current.length ? rateOf(current) - rateOf(previous) : null,
    },
  };
}

export function peakDay(series = ACTIVITY_SERIES) {
  return series.reduce((best, row) => (row.requests > best.requests ? row : best), series[0]);
}

// ---------------------------------------------------------------------------
// Projects
// ---------------------------------------------------------------------------

export const PROJECTS = [
  {
    id: 'prj-atlas-rag',
    name: 'Atlas RAG pipeline',
    description: 'Retrieval index refresh and answer quality scoring for the internal knowledge base.',
    status: 'active',
    progress: 82,
    tasks: { done: 41, total: 48 },
    requests: 28450,
    model: 'atlas-2-pro-demo',
    environment: 'Production',
    owner: 'M. Okafor',
    updated: '2 h ago',
  },
  {
    id: 'prj-doc-summarizer',
    name: 'Document summarizer',
    description: 'Two-pass summarisation for long PDFs with citation anchoring.',
    status: 'active',
    progress: 64,
    tasks: { done: 23, total: 39 },
    requests: 19230,
    model: 'atlas-2-pro-demo',
    environment: 'Staging',
    owner: 'L. Bennett',
    updated: '5 h ago',
  },
  {
    id: 'prj-support-copilot',
    name: 'Support copilot',
    description: 'Draft replies grounded in the last twelve months of resolved tickets.',
    status: 'active',
    progress: 47,
    tasks: { done: 18, total: 44 },
    requests: 14980,
    model: 'nova-3-mini-demo',
    environment: 'Production',
    owner: 'S. Harrington',
    updated: 'yesterday',
  },
  {
    id: 'prj-vision-qa',
    name: 'Vision QA sweep',
    description: 'Screenshot regression checks against the component library.',
    status: 'paused',
    progress: 38,
    tasks: { done: 12, total: 32 },
    requests: 8640,
    model: 'helios-vision-demo',
    environment: 'Preview',
    owner: 'A. Duarte',
    updated: '3 d ago',
  },
  {
    id: 'prj-invoice-extractor',
    name: 'Invoice extractor',
    description: 'Line-item extraction with confidence gating and a human review queue.',
    status: 'active',
    progress: 91,
    tasks: { done: 52, total: 57 },
    requests: 33110,
    model: 'nova-3-mini-demo',
    environment: 'Production',
    owner: 'K. Lindqvist',
    updated: '45 min ago',
  },
  {
    id: 'prj-search-relevance',
    name: 'Search relevance lab',
    description: 'Offline ranking experiments with pairwise preference scoring.',
    status: 'active',
    progress: 55,
    tasks: { done: 27, total: 49 },
    requests: 12760,
    model: 'orbit-edge-1b-demo',
    environment: 'Staging',
    owner: 'R. Mensah',
    updated: '8 h ago',
  },
  {
    id: 'prj-voice-triage',
    name: 'Voice triage',
    description: 'Transcript cleanup and intent tagging for the support phone line.',
    status: 'paused',
    progress: 22,
    tasks: { done: 7, total: 31 },
    requests: 4180,
    model: 'nova-3-mini-demo',
    environment: 'Preview',
    owner: 'T. Nakamura',
    updated: '6 d ago',
  },
  {
    id: 'prj-churn-signal',
    name: 'Churn signal',
    description: 'Weekly risk briefing assembled from product and billing events.',
    status: 'active',
    progress: 73,
    tasks: { done: 33, total: 45 },
    requests: 9120,
    model: 'atlas-2-pro-demo',
    environment: 'Production',
    owner: 'P. Alvarez',
    updated: 'yesterday',
  },
  {
    id: 'prj-meeting-graph',
    name: 'Meeting notes graph',
    description: 'Entity and decision extraction into a queryable graph.',
    status: 'done',
    progress: 100,
    tasks: { done: 38, total: 38 },
    requests: 6740,
    model: 'atlas-2-pro-demo',
    environment: 'Production',
    owner: 'L. Bennett',
    updated: '2 w ago',
  },
  {
    id: 'prj-anomaly-watch',
    name: 'Anomaly watch',
    description: 'Nightly scan of metric drift with plain-language explanations.',
    status: 'done',
    progress: 100,
    tasks: { done: 29, total: 29 },
    requests: 15320,
    model: 'orbit-edge-1b-demo',
    environment: 'Production',
    owner: 'M. Okafor',
    updated: '3 w ago',
  },
];

export const PROJECT_STATUSES = [
  { id: 'all', label: 'All' },
  { id: 'active', label: 'Active' },
  { id: 'paused', label: 'Paused' },
  { id: 'done', label: 'Complete' },
];

// ---------------------------------------------------------------------------
// Activity feed seed
// ---------------------------------------------------------------------------

const ACTIVITY_TYPES = [
  { id: 'all', label: 'Everything' },
  { id: 'task', label: 'Tasks' },
  { id: 'model', label: 'Models' },
  { id: 'run', label: 'Runs' },
  { id: 'system', label: 'System' },
];

export const activityTypes = ACTIVITY_TYPES;

/** Offsets are seconds ago so the feed always reads as "recent". */
const SEED_ACTIVITY = [
  { type: 'run', minutes: 6, title: 'Batch run finished', detail: 'Invoice extractor · 412 documents scored' },
  { type: 'task', minutes: 24, title: 'Task completed', detail: 'Invoice extractor · review extraction confidence thresholds' },
  { type: 'model', minutes: 41, title: 'Model switched', detail: 'Atlas 2 Pro (DEMO) selected for Document summarizer' },
  { type: 'run', minutes: 78, title: 'Batch run started', detail: 'Support copilot · 1,240 replies drafted' },
  { type: 'system', minutes: 132, title: 'Index refreshed', detail: 'Atlas RAG pipeline · knowledge base snapshot rebuilt in 4 m 12 s' },
  { type: 'task', minutes: 190, title: 'Task completed', detail: 'Atlas RAG pipeline · tune retrieval window to 8 chunks' },
  { type: 'run', minutes: 260, title: 'Evaluation pass', detail: 'Search relevance lab · nDCG 0.71' },
  { type: 'system', minutes: 320, title: 'Nightly snapshot', detail: 'Workspace state archived locally' },
  { type: 'model', minutes: 405, title: 'Model switched', detail: 'Orbit Edge 1B (DEMO) selected for ranking jobs' },
  { type: 'task', minutes: 520, title: 'Task added', detail: 'Support copilot · document prompt patterns for replies' },
  { type: 'run', minutes: 640, title: 'Batch run finished', detail: 'Vision QA sweep · 240 screenshots compared' },
  { type: 'run', minutes: 810, title: 'Batch run finished', detail: 'Anomaly watch · 38 metrics reviewed' },
  { type: 'task', minutes: 960, title: 'Task added', detail: 'Churn signal · draft the Friday risk briefing' },
  { type: 'system', minutes: 900, title: 'Session started', detail: 'Signed in from a local browser' },
  { type: 'run', minutes: 1140, title: 'Evaluation pass', detail: 'Document summarizer · ROUGE-L 0.44' },
  { type: 'task', minutes: 1500, title: 'Task completed', detail: 'Archive the June experiment results' },
];

export function seedActivity(now = Date.now()) {
  return SEED_ACTIVITY.map((entry, index) => ({
    id: `seed-${index}`,
    type: entry.type,
    title: entry.title,
    detail: entry.detail,
    at: now - entry.minutes * 60000,
  }));
}

// ---------------------------------------------------------------------------
// Formatting helpers used by more than one view
// ---------------------------------------------------------------------------

export function compactNumber(value) {
  const number = Number(value) || 0;
  if (Math.abs(number) >= 1000000) return `${(number / 1000000).toFixed(2)}M`;
  if (Math.abs(number) >= 1000) return `${(number / 1000).toFixed(number >= 10000 ? 1 : 2)}K`;
  return String(Math.round(number));
}

export function timeAgo(timestamp, now = Date.now()) {
  const seconds = Math.max(0, Math.round((now - timestamp) / 1000));
  if (seconds < 45) return 'just now';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days} d ago`;
  return `${Math.round(days / 7)} w ago`;
}
