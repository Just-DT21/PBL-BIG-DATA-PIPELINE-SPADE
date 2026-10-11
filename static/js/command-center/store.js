// Local state for the AI Command Center.
//
// The page is a static demo, so persistence uses the browser's own storage:
// localStorage, mirrored in memory when storage is unavailable (private mode or
// a hardened context). Nothing is ever sent to a server.

import { DEFAULT_MODEL_ID, seedActivity } from './data.js';

const KEYS = {
  tasks: 'cc.tasks.v1',
  activity: 'cc.activity.v1',
  preferences: 'cc.preferences.v1',
};

export const MAX_TASK_LENGTH = 120;

const DEFAULTS = {
  theme: 'dark', // 'dark' | 'light' | 'system'
  model: DEFAULT_MODEL_ID,
  motion: 'full', // 'full' | 'reduced'
};

const memory = new Map();

function available() {
  try {
    const probe = '__cc_probe__';
    window.localStorage.setItem(probe, '1');
    window.localStorage.removeItem(probe);
    return true;
  } catch (_) {
    return false;
  }
}

const CAN_STORE = typeof window !== 'undefined' && available();
export const persistenceMode = CAN_STORE ? 'localStorage' : 'memory';

function readRaw(key) {
  if (!CAN_STORE) return memory.has(key) ? memory.get(key) : null;
  try {
    return window.localStorage.getItem(key);
  } catch (_) {
    return null;
  }
}

function writeRaw(key, value) {
  if (!CAN_STORE) {
    memory.set(key, value);
    return;
  }
  try {
    window.localStorage.setItem(key, value);
  } catch (_) {
    memory.set(key, value);
  }
}

function removeRaw(key) {
  memory.delete(key);
  if (!CAN_STORE) return;
  try {
    window.localStorage.removeItem(key);
  } catch (_) {
    /* nothing else to do */
  }
}

function readJSON(key, fallback) {
  const raw = readRaw(key);
  if (!raw) return fallback;
  try {
    const parsed = JSON.parse(raw);
    return parsed === null || parsed === undefined ? fallback : parsed;
  } catch (_) {
    return fallback;
  }
}

function makeId(prefix) {
  const random = Math.random().toString(36).slice(2, 8);
  return `${prefix}_${Date.now().toString(36)}${random}`;
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const listeners = new Set();

export const state = {
  tasks: [],
  activity: [],
  preferences: { ...DEFAULTS },
  view: 'overview',
};

function normalizeTask(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const title = typeof raw.title === 'string' ? raw.title.trim().slice(0, MAX_TASK_LENGTH) : '';
  if (!title) return null;
  return {
    id: typeof raw.id === 'string' && raw.id ? raw.id : makeId('task'),
    title,
    done: Boolean(raw.done),
    model: typeof raw.model === 'string' ? raw.model : DEFAULT_MODEL_ID,
    createdAt: Number(raw.createdAt) || Date.now(),
    completedAt: Number(raw.completedAt) || null,
  };
}

function normalizeActivity(raw) {
  if (!raw || typeof raw !== 'object') return null;
  if (!raw.title || !raw.type) return null;
  return {
    id: typeof raw.id === 'string' && raw.id ? raw.id : makeId('evt'),
    type: raw.type,
    title: String(raw.title).slice(0, 80),
    detail: String(raw.detail || '').slice(0, 160),
    at: Number(raw.at) || Date.now(),
  };
}

/** Read persisted state, falling back to the seeded demo values. */
export function load() {
  const storedPreferences = readJSON(KEYS.preferences, {});
  state.preferences = { ...DEFAULTS, ...(storedPreferences && typeof storedPreferences === 'object' ? storedPreferences : {}) };
  if (!['dark', 'light', 'system'].includes(state.preferences.theme)) state.preferences.theme = DEFAULTS.theme;
  if (!['full', 'reduced'].includes(state.preferences.motion)) state.preferences.motion = DEFAULTS.motion;

  const storedTasks = readJSON(KEYS.tasks, null);
  state.tasks = Array.isArray(storedTasks) ? storedTasks.map(normalizeTask).filter(Boolean) : [];

  const storedActivity = readJSON(KEYS.activity, null);
  state.activity = Array.isArray(storedActivity) && storedActivity.length
    ? storedActivity.map(normalizeActivity).filter(Boolean)
    : seedActivity();

  state.view = 'overview';
  return state;
}

export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function notify(reason) {
  for (const listener of listeners) listener(reason, state);
}

// ---------------------------------------------------------------------------
// Persistence
// ---------------------------------------------------------------------------

export function persistTasks() {
  writeRaw(KEYS.tasks, JSON.stringify(state.tasks));
}

export function persistActivity() {
  // Keep the log bounded; it is a local session history, not an archive.
  state.activity = state.activity.slice(0, 120);
  writeRaw(KEYS.activity, JSON.stringify(state.activity));
}

export function persistPreferences() {
  writeRaw(KEYS.preferences, JSON.stringify(state.preferences));
}

export function setPreference(key, value) {
  if (!(key in DEFAULTS)) return;
  state.preferences[key] = value;
  persistPreferences();
  notify(`preference:${key}`);
}

export function setView(view) {
  state.view = view;
  notify('view');
}

// ---------------------------------------------------------------------------
// Activity log
// ---------------------------------------------------------------------------

export function logActivity(type, title, detail) {
  const entry = { id: makeId('evt'), type, title, detail, at: Date.now() };
  state.activity = [entry, ...state.activity].slice(0, 120);
  persistActivity();
  notify('activity');
  return entry;
}

// ---------------------------------------------------------------------------
// Tasks
// ---------------------------------------------------------------------------

export const TaskValidation = {
  empty: 'Enter a task before adding it.',
  tooLong: `Keep tasks under ${MAX_TASK_LENGTH} characters.`,
  duplicate: 'That task is already on the list.',
};

/** Validate a candidate task title. Returns null when the title is acceptable. */
export function validateTaskTitle(title) {
  const trimmed = String(title || '').trim();
  if (!trimmed) return TaskValidation.empty;
  if (trimmed.length > MAX_TASK_LENGTH) return TaskValidation.tooLong;
  const clash = state.tasks.some((task) => task.title.toLowerCase() === trimmed.toLowerCase());
  if (clash) return TaskValidation.duplicate;
  return null;
}

export function addTask(title) {
  const error = validateTaskTitle(title);
  if (error) return { ok: false, error };
  const task = {
    id: makeId('task'),
    title: String(title).trim(),
    done: false,
    model: state.preferences.model,
    createdAt: Date.now(),
    completedAt: null,
  };
  state.tasks = [task, ...state.tasks];
  persistTasks();
  logActivity('task', 'Task added', task.title);
  notify('tasks');
  return { ok: true, task };
}

export function toggleTask(id) {
  const task = state.tasks.find((item) => item.id === id);
  if (!task) return null;
  task.done = !task.done;
  task.completedAt = task.done ? Date.now() : null;
  persistTasks();
  logActivity('task', task.done ? 'Task completed' : 'Task reopened', task.title);
  notify('tasks');
  return task;
}

export function deleteTask(id) {
  const task = state.tasks.find((item) => item.id === id);
  if (!task) return null;
  state.tasks = state.tasks.filter((item) => item.id !== id);
  persistTasks();
  logActivity('task', 'Task removed', task.title);
  notify('tasks');
  return task;
}

export function clearCompletedTasks() {
  const removed = state.tasks.filter((task) => task.done).length;
  if (!removed) return 0;
  state.tasks = state.tasks.filter((task) => !task.done);
  persistTasks();
  logActivity('task', 'Completed tasks cleared', `${removed} task${removed === 1 ? '' : 's'} removed`);
  notify('tasks');
  return removed;
}

export function taskSummary() {
  const total = state.tasks.length;
  const done = state.tasks.filter((task) => task.done).length;
  return { total, done, open: total - done };
}

// ---------------------------------------------------------------------------
// Reset
// ---------------------------------------------------------------------------

export function resetEverything() {
  removeRaw(KEYS.tasks);
  removeRaw(KEYS.activity);
  removeRaw(KEYS.preferences);
  state.tasks = [];
  state.preferences = { ...DEFAULTS };
  state.activity = seedActivity();
  // Re-write the defaults so the next load (and the pre-paint theme script) agrees
  // with what is on screen right now.
  persistTasks();
  persistPreferences();
  persistActivity();
  notify('reset');
}

export function exportPayload() {
  return {
    exportedAt: new Date().toISOString(),
    demo: true,
    note: 'AI Command Center demo state. No live API is configured.',
    preferences: { ...state.preferences },
    tasks: state.tasks,
    activity: state.activity,
  };
}

export const storageKeys = KEYS;
