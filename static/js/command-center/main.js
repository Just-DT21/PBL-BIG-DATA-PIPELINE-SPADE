// Bootstrap for the AI Command Center: preferences, routing, chrome and the
// shared handles that the views call back into.
//
// The page is static: it loads the demo data module, keeps its state in the
// browser and never calls a backend.

import { toast } from '../util.js';
import { modelById, modelCatalog } from './data.js';
import * as store from './store.js';
import {
  renderOverview,
  renderProjects,
  renderActivity,
  renderSettings,
  setActivityQuery,
  teardownView,
} from './views.js';

const VIEWS = {
  overview: { title: 'Overview', subtitle: 'Workspace health at a glance', render: renderOverview },
  projects: { title: 'Projects', subtitle: 'Demo programmes and their model routing', render: renderProjects },
  activity: { title: 'Activity', subtitle: 'Local event log for this browser', render: renderActivity },
  settings: { title: 'Settings', subtitle: 'Appearance, defaults and local data', render: renderSettings },
};

const elements = {
  view: document.getElementById('cc-view'),
  nav: document.getElementById('cc-nav'),
  title: document.getElementById('cc-view-title'),
  subtitle: document.getElementById('cc-view-sub'),
  sidebar: document.getElementById('cc-sidebar'),
  scrim: document.getElementById('cc-scrim'),
  burger: document.getElementById('cc-burger'),
  themeButton: document.getElementById('cc-theme'),
  themeIcon: document.getElementById('cc-theme-icon'),
  modelSelect: document.getElementById('cc-model'),
  modelLabel: document.getElementById('cc-active-model'),
  taskCount: document.getElementById('cc-side-tasks'),
  storageNote: document.getElementById('cc-storage-note'),
};

const systemQuery = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null;

// ---------------------------------------------------------------------------
// Preferences
// ---------------------------------------------------------------------------

function resolvedTheme() {
  const preference = store.state.preferences.theme;
  if (preference === 'system') return systemQuery?.matches ? 'light' : 'dark';
  return preference;
}

function applyPreferences() {
  const root = document.documentElement;
  root.dataset.theme = resolvedTheme();
  root.dataset.motion = store.state.preferences.motion;
  const isDark = resolvedTheme() === 'dark';
  if (elements.themeIcon) elements.themeIcon.textContent = isDark ? '☾' : '☀';
  if (elements.themeButton) {
    const label = store.state.preferences.theme === 'system'
      ? `Theme follows the system (currently ${resolvedTheme()}). Click for ${isDark ? 'light' : 'dark'}.`
      : `Switch to ${isDark ? 'light' : 'dark'} theme`;
    elements.themeButton.setAttribute('aria-label', label);
    elements.themeButton.title = label;
  }
}

if (systemQuery) {
  const onSystemChange = () => {
    if (store.state.preferences.theme === 'system') {
      applyPreferences();
      rerender();
    }
  };
  if (typeof systemQuery.addEventListener === 'function') systemQuery.addEventListener('change', onSystemChange);
  else if (typeof systemQuery.addListener === 'function') systemQuery.addListener(onSystemChange);
}

// ---------------------------------------------------------------------------
// Chrome (piece that sits outside the routed view)
// ---------------------------------------------------------------------------

function refreshChrome() {
  const { open } = store.taskSummary();
  if (elements.taskCount) elements.taskCount.textContent = open ? `${open} open` : 'no open tasks';
  const model = modelById(store.state.preferences.model);
  if (elements.modelLabel) elements.modelLabel.textContent = `${model.name} (DEMO)`;
  if (elements.modelSelect && elements.modelSelect.value !== model.id) elements.modelSelect.value = model.id;
  if (elements.storageNote) {
    elements.storageNote.textContent = store.persistenceMode === 'localStorage'
      ? 'Demo mode · state saved in this browser'
      : 'Demo mode · state kept in memory for this tab';
  }
}

// ---------------------------------------------------------------------------
// Routing
// ---------------------------------------------------------------------------

function parseHash() {
  const raw = (window.location.hash || '#/overview').replace(/^#\/?/, '');
  const [view] = raw.split('/');
  return VIEWS[view] ? view : 'overview';
}

function closeSidebar() {
  elements.sidebar.classList.remove('is-open');
  elements.scrim.hidden = true;
  elements.burger.setAttribute('aria-expanded', 'false');
}

function rerender() {
  const view = parseHash();
  teardownView();
  store.setView(view);

  for (const link of elements.nav.querySelectorAll('a[data-view]')) {
    const active = link.dataset.view === view;
    link.classList.toggle('is-active', active);
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }

  elements.title.textContent = VIEWS[view].title;
  elements.subtitle.textContent = VIEWS[view].subtitle;
  elements.view.replaceChildren();
  elements.view.classList.remove('is-entering');
  // Restart the entrance animation for every navigation.
  void elements.view.offsetWidth;
  elements.view.classList.add('is-entering');
  elements.view.scrollTop = 0;

  try {
    VIEWS[view].render(elements.view, app);
  } catch (error) {
    elements.view.append(
      Object.assign(document.createElement('div'), {
        className: 'cc-empty',
        textContent: `This view could not be rendered: ${error.message}`,
      })
    );
  }
  closeSidebar();
  refreshChrome();
}

const app = {
  rerender,
  refreshChrome,
  applyPreferences,
  navigate(view) {
    if (!VIEWS[view]) return;
    if (window.location.hash === `#/${view}`) rerender();
    else window.location.hash = `#/${view}`;
  },
  selectModel(id) {
    const model = modelById(id);
    if (!model || model.id === store.state.preferences.model) {
      refreshChrome();
      return;
    }
    store.setPreference('model', model.id);
    store.logActivity('model', 'Model switched', `${model.name} (${model.tag}) selected`);
    toast(`${model.name} (DEMO) selected for new tasks.`, 'ok');
    applyPreferences();
    rerender();
  },
  setPreference(key, value) {
    store.setPreference(key, value);
    applyPreferences();
    rerender();
  },
  focusActivity(query) {
    setActivityQuery(query);
    app.navigate('activity');
  },
};

// ---------------------------------------------------------------- wiring

elements.nav.addEventListener('click', (event) => {
  const link = event.target.closest('a[data-view]');
  if (!link) return;
  event.preventDefault();
  app.navigate(link.dataset.view);
});

elements.burger.addEventListener('click', () => {
  const open = !elements.sidebar.classList.contains('is-open');
  elements.sidebar.classList.toggle('is-open', open);
  elements.scrim.hidden = !open;
  elements.burger.setAttribute('aria-expanded', open ? 'true' : 'false');
});

elements.scrim.addEventListener('click', closeSidebar);

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closeSidebar();
});

elements.themeButton.addEventListener('click', () => {
  const next = resolvedTheme() === 'dark' ? 'light' : 'dark';
  app.setPreference('theme', next);
  toast(`Switched to the ${next} theme.`, '');
});

for (const model of modelCatalog) {
  elements.modelSelect.append(
    Object.assign(document.createElement('option'), {
      value: model.id,
      textContent: `${model.name} (${model.tag})`,
    })
  );
}

elements.modelSelect.addEventListener('change', () => app.selectModel(elements.modelSelect.value));

window.addEventListener('hashchange', rerender);

store.subscribe((reason) => {
  refreshChrome();
  if (reason === 'reset') rerender();
});

// --------------------------------------------------------------------- boot

store.load();
applyPreferences();
refreshChrome();
if (!window.location.hash) window.location.hash = '#/overview';
rerender();
