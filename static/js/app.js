// Application bootstrap. Holds the small shared state, resolves the hash route
// and mounts the matching view.

import { api, ApiError } from './api.js';
import { h, mount, toast, clear } from './util.js';
import { renderNami } from './lab.js';
import * as views from './views.js';

const state = {
  user: null,
  settings: null,
  files: [],
  jobs: [],
  overview: null,
  backends: null,
  hardLimits: null,
  formats: [],
  currentFileId: null,
  currentJobId: null,
  // NAMI Optimizer lab
  namiPolicies: null,
  namiHistory: [],
  namiPreflight: null,
  namiFileId: null,
  namiTrials: null,
  namiWarmup: null,
  namiForcePolicy: null,
  currentBenchmarkId: null,
};

const VIEWS = {
  dashboard: { title: 'Dashboard', subtitle: 'Measured activity for this account', render: views.renderDashboard },
  upload: { title: 'Upload File', subtitle: 'Drag and drop, or browse. The type is detected from the content.', render: views.renderUpload },
  analysis: { title: 'File Analysis', subtitle: 'Structure, data quality and the operation plan', render: views.renderAnalysis },
  results: { title: 'Optimization Results', subtitle: 'Before-and-after measurements and verification', render: views.renderResults },
  nami: { title: 'NAMI Optimizer', subtitle: 'SPADE vs NAMI | Pipeline Intelligence Lab', render: renderNami },
  history: { title: 'Processing History', subtitle: 'Every run, with status and outcomes', render: views.renderHistory },
  settings: { title: 'Settings', subtitle: 'Limits, engines and security posture', render: views.renderSettings },
};

const elements = {
  authScreen: document.getElementById('auth-screen'),
  appShell: document.getElementById('app-shell'),
  nav: document.getElementById('nav'),
  title: document.getElementById('view-title'),
  subtitle: document.getElementById('view-subtitle'),
  alert: document.getElementById('global-alert'),
  enginePill: document.getElementById('engine-pill'),
};

let currentView = null;
let jobWatcher = null;

// --------------------------------------------------------------- navigation

function parseHash() {
  const raw = (window.location.hash || '#/dashboard').replace(/^#\/?/, '');
  const [view, param] = raw.split('/');
  if (!VIEWS[view]) return { view: 'dashboard', param: null };
  return { view, param: param || null };
}

function actions() {
  return {
    navigate(hash) {
      if (window.location.hash === hash) route();
      else window.location.hash = hash;
    },
    setCurrentFile(id) { state.currentFileId = id; },
    refreshFiles: () => api('/api/files').then((payload) => { state.files = payload.files; }).catch(() => {}),
    refreshMeta: refreshMeta,
    watchJob: watchJob,
  };
}

async function route() {
  const { view, param } = parseHash();
  if (!state.user) return;

  if (jobWatcher) { clearInterval(jobWatcher); jobWatcher = null; }

  if (view === 'analysis') state.currentFileId = param || state.currentFileId;
  if (view === 'results') state.currentJobId = param || state.currentJobId;

  for (const link of elements.nav.querySelectorAll('.nav-item')) {
    link.classList.toggle('is-active', link.dataset.view === view);
  }
  elements.title.textContent = VIEWS[view].title;
  elements.subtitle.textContent = VIEWS[view].subtitle;
  for (const section of document.querySelectorAll('.view')) section.hidden = true;
  const section = document.getElementById(`view-${view}`);
  section.hidden = false;
  currentView = view;
  hideAlert();

  try {
    clear(section);
    await VIEWS[view].render(section, state, actions());
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      await signOut(true);
      return;
    }
    showAlert('err', `Could not load this view: ${error.message}`);
    clear(section);
    mount(section, h('div', { class: 'empty' }, h('p', {}, error.message)));
  }
}

function showAlert(kind, message, extra) {
  elements.alert.hidden = false;
  elements.alert.className = `alert ${kind}`;
  mount(elements.alert, h('strong', {}, message), extra ? ` ${extra}` : '');
}

function hideAlert() {
  elements.alert.hidden = true;
}

function watchJob(jobId) {
  if (jobWatcher) clearInterval(jobWatcher);
  const tick = async () => {
    try {
      const payload = await api(`/api/jobs/${jobId}`);
      if (payload.job.status === 'succeeded' || payload.job.status === 'failed') {
        clearInterval(jobWatcher);
        jobWatcher = null;
        if (currentView === 'results') route();
      }
    } catch (error) {
      clearInterval(jobWatcher);
      jobWatcher = null;
    }
  };
  jobWatcher = setInterval(tick, 1500);
  tick();
}

// -------------------------------------------------------------------- auth

function authMode(mode) {
  const form = document.getElementById('auth-form');
  const submit = document.getElementById('auth-submit');
  const hint = document.getElementById('auth-hint');
  form.dataset.mode = mode;
  submit.textContent = mode === 'register' ? 'Create account and continue' : 'Sign in';
  hint.textContent = mode === 'register'
    ? 'Passwords are hashed with scrypt before storage. Only a hash of your session token is kept server-side.'
    : 'Sign in to continue. Sessions use HTTP-only SameSite cookies.';
  document.querySelectorAll('.tab').forEach((tab) => tab.classList.toggle('is-active', tab.dataset.authTab === mode));
  if (mode === 'register') form.password.autocomplete = 'new-password';
  else form.password.autocomplete = 'current-password';
}

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => authMode(tab.dataset.authTab));
});

document.getElementById('auth-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.target;
  const errorNode = document.getElementById('auth-error');
  const submit = document.getElementById('auth-submit');
  errorNode.textContent = '';
  submit.disabled = true;
  const mode = form.dataset.mode === 'register' ? 'register' : 'login';
  try {
    const payload = await api(`/api/auth/${mode}`, {
      method: 'POST',
      body: { email: form.email.value.trim(), password: form.password.value },
    });
    state.user = payload.user;
    form.reset();
    await startSession();
  } catch (error) {
    errorNode.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

document.getElementById('sign-out').addEventListener('click', () => signOut(false));

async function signOut(silent) {
  try { await api('/api/auth/logout', { method: 'POST' }); } catch (_) { /* ignore */ }
  state.user = null;
  elements.authScreen.hidden = false;
  elements.appShell.hidden = true;
  if (!silent) toast('Signed out.', 'ok');
}

// ------------------------------------------------------------------ session

async function refreshMeta() {
  try {
    const [me, engines] = await Promise.all([api('/api/auth/me'), api('/api/engines')]);
    state.user = me.user;
    state.settings = me.settings;
    state.backends = engines.backends;
    state.hardLimits = engines.limits;
    state.formats = engines.formats;
    document.getElementById('sidebar-user').textContent = state.user.email;
    const implemented = (engines.backends.implemented || []).length;
    const declared = (engines.backends.declared || []).length;
    elements.enginePill.textContent = `${implemented} active engines · ${declared} declared`;
    elements.enginePill.title = engines.backends.note;
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      state.user = null;
      throw error;
    }
    throw error;
  }
}

async function startSession() {
  await refreshMeta();
  elements.authScreen.hidden = true;
  elements.appShell.hidden = false;
  if (!window.location.hash) window.location.hash = '#/dashboard';
  await route();
}

window.addEventListener('hashchange', route);

(async function boot() {
  try {
    await refreshMeta();
    await startSession();
  } catch (_) {
    elements.authScreen.hidden = false;
    elements.appShell.hidden = true;
    authMode('login');
  }
})();
