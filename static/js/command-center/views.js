// Views for the AI Command Center: Overview, Projects, Activity and Settings.
//
// Views are plain functions that build DOM into the given container, following
// the same style as static/js/views.js. Every control rendered here is wired up:
// the chart controls, the task list, the model picker and the settings toggles.

import { h, clear, mount, toast, confirmAction } from '../util.js';
import {
  ACTIVITY_SERIES,
  DEMO_NOTICE,
  PROJECTS,
  PROJECT_STATUSES,
  activityTypes,
  compactNumber,
  modelById,
  modelCatalog,
  peakDay,
  seriesOverview,
  timeAgo,
} from './data.js';
import { createActivityChart, sparkline } from './chart.js';
import * as store from './store.js';

// Local, non-persisted view state. Keeping it here means filters and the chart
// range survive a re-render without writing to storage on every keystroke.
const viewState = {
  range: 14,
  visible: { requests: true, tasks: true, errors: false },
  selectedDay: null,
  projectQuery: '',
  projectStatus: 'all',
  activityQuery: '',
  activityType: 'all',
  taskFilter: 'all',
};

let chartHandle = null;

/** Release the chart's ResizeObserver before a view is replaced. */
export function teardownView() {
  chartHandle?.destroy();
  chartHandle = null;
}

/** Used by the Projects view to jump into a filtered activity log. */
export function setActivityQuery(query) {
  viewState.activityQuery = query;
  viewState.activityType = 'all';
}

// ---------------------------------------------------------------------------
// Shared building blocks
// ---------------------------------------------------------------------------

function panel({ title, subtitle, actions, body, className = '', id = null }) {
  const head =
    title || actions
      ? h(
          'header',
          { class: 'cc-panel-head' },
          h(
            'div',
            {},
            title ? h('h3', { class: 'cc-panel-title' }, title) : null,
            subtitle ? h('p', { class: 'cc-panel-sub' }, subtitle) : null
          ),
          actions ? h('div', { class: 'cc-panel-actions' }, actions) : null
        )
      : null;
  return h('section', { class: `cc-panel glass ${className}`.trim(), id }, head, h('div', { class: 'cc-panel-body' }, body));
}

function chips({ options, value, onPick, label, multi = false }) {
  return h(
    'div',
    { class: 'cc-chips', role: 'group', 'aria-label': label },
    options.map((option) => {
      const active = multi ? Boolean(value[option.id]) : option.id === value;
      return h(
        'button',
        {
          type: 'button',
          class: `cc-chip${active ? ' is-active' : ''}`,
          'aria-pressed': active ? 'true' : 'false',
          onclick: () => onPick(option.id),
        },
        option.label
      );
    })
  );
}

function deltaBadge(delta, { unit = '%', invert = false } = {}) {
  if (delta === null || delta === undefined) return h('span', { class: 'cc-delta is-flat' }, 'no prior window');
  const flat = Math.abs(delta) < 0.05;
  const good = invert ? delta < 0 : delta > 0;
  const arrow = flat ? '▪' : delta > 0 ? '▲' : '▼';
  return h(
    'span',
    { class: `cc-delta ${flat ? 'is-flat' : good ? 'is-good' : 'is-bad'}` },
    `${arrow} ${Math.abs(delta).toFixed(1)}${unit}`
  );
}

function countUp(node, target, format, { duration = 900 } = {}) {
  const reduced = store.state.preferences.motion === 'reduced';
  if (reduced || typeof requestAnimationFrame !== 'function') {
    node.textContent = format(target);
    return;
  }
  const start = performance.now();
  const step = (now) => {
    const progress = Math.min(1, (now - start) / duration);
    const eased = 1 - (1 - progress) ** 3;
    node.textContent = format(target * eased);
    if (progress < 1) requestAnimationFrame(step);
    else node.textContent = format(target);
  };
  node.textContent = format(0);
  requestAnimationFrame(step);
}

function statusBadge(status) {
  const map = { active: 'is-good', paused: 'is-warn', done: 'is-info' };
  const label = status === 'done' ? 'complete' : status;
  return h('span', { class: `cc-badge ${map[status] || 'is-info'}` }, label);
}

function icon(name) {
  const paths = {
    overview: 'M3 3h8v8H3zM13 3h8v5h-8zM13 10h8v11h-8zM3 13h8v8H3z',
    projects: 'M3 6h7l1.6 2H21v11H3z',
    activity: 'M3 16l5-6 4 4 4-7 5 5',
    settings:
      'M12 8.4A3.6 3.6 0 1 0 12 15.6A3.6 3.6 0 0 0 12 8.4zM4.5 12a7.5 7.5 0 0 1 .1-1.3l-1.6-1.2 1.7-2.9 1.9.7A7.4 7.4 0 0 1 9 5.9l.3-2h3.4l.3 2c.9.3 1.7.8 2.4 1.4l1.9-.7 1.7 2.9-1.6 1.2a7.5 7.5 0 0 1 0 2.6l1.6 1.2-1.7 2.9-1.9-.7c-.7.6-1.5 1.1-2.4 1.4l-.3 2H9.3l-.3-2a7.4 7.4 0 0 1-2.4-1.4l-1.9.7-1.7-2.9 1.6-1.2A7.5 7.5 0 0 1 4.5 12z',
  };
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', 'cc-ico');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', paths[name] || paths.overview);
  svg.append(path);
  return svg;
}

function modelChip(model, { compact = false } = {}) {
  return h(
    'span',
    { class: `cc-model-chip tone-${model.tone}`, title: `${model.name} (DEMO placeholder)` },
    h('i', { class: 'cc-dot-mark' }),
    compact ? model.family : model.name,
    h('em', {}, 'DEMO')
  );
}

// ---------------------------------------------------------------------------
// Overview
// ---------------------------------------------------------------------------

/** Keep the task-derived numbers live without re-rendering (and re-animating) the page. */
function syncTaskDerivedNumbers() {
  const valueNode = document.getElementById('cc-stat-tasks-value');
  if (!valueNode) return;
  const { done, open } = store.taskSummary();
  valueNode.textContent = (seriesOverview().tasks + done).toLocaleString();
  const noteNode = document.getElementById('cc-stat-tasks-note');
  if (noteNode) noteNode.textContent = done ? `includes ${done} you completed here` : 'vs previous 15 days';
  const openNode = document.getElementById('cc-fact-open-tasks');
  if (openNode) openNode.textContent = String(open);
}

function statCard({ label, iconName, value, format, series, footnote, footnoteId = null, valueId = null, extra = null, tone = 'blue', delta = null, deltaUnit = '%', invertDelta = false }) {
  const valueNode = h('div', { class: 'cc-stat-value', id: valueId }, format ? format(0) : String(value));
  const card = h(
    'article',
    { class: `cc-stat glass tone-${tone}` },
    h(
      'header',
      { class: 'cc-stat-head' },
      h('span', { class: 'cc-stat-icon' }, icon(iconName)),
      h('span', { class: 'cc-stat-label' }, label)
    ),
    valueNode,
    h(
      'div',
      { class: 'cc-stat-meta' },
      delta === null ? null : deltaBadge(delta, { unit: deltaUnit, invert: invertDelta }),
      h('span', { class: 'cc-stat-note', id: footnoteId }, footnote)
    ),
    extra || (series ? h('div', { class: 'cc-stat-spark' }, sparkline(series, { up: true })) : null)
  );
  if (typeof value === 'number') countUp(valueNode, value, format || ((n) => String(Math.round(n))));
  return card;
}

function projectsStat() {
  const counts = PROJECTS.reduce(
    (acc, project) => ({ ...acc, [project.status]: (acc[project.status] || 0) + 1 }),
    {}
  );
  const total = PROJECTS.length;
  const segments = [
    { id: 'active', label: 'Active', className: 'is-good' },
    { id: 'paused', label: 'Paused', className: 'is-warn' },
    { id: 'done', label: 'Complete', className: 'is-info' },
  ];
  return h(
    'div',
    { class: 'cc-status-bar' },
    segments.map((segment) =>
      h('span', {
        class: `cc-status-seg ${segment.className}`,
        style: `flex:${counts[segment.id] || 0}`,
        title: `${counts[segment.id] || 0} ${segment.label.toLowerCase()}`,
      })
    ),
    h(
      'div',
      { class: 'cc-status-legend' },
      segments.map((segment) =>
        h('span', {}, h('i', { class: `cc-dot-mark ${segment.className}` }), `${counts[segment.id] || 0} ${segment.label.toLowerCase()}`)
      )
    )
  );
}

function taskPanel(app) {
  const input = h('input', {
    type: 'text',
    id: 'cc-task-input',
    class: 'cc-input',
    placeholder: 'Add a task, e.g. “Review prompt regressions”',
    maxlength: String(store.MAX_TASK_LENGTH),
    autocomplete: 'off',
    'aria-describedby': 'cc-task-error',
  });
  const error = h('p', { class: 'cc-task-error', id: 'cc-task-error', role: 'alert' });
  const counter = h('span', { class: 'cc-hint' }, `0 / ${store.MAX_TASK_LENGTH}`);
  const listHost = h('ul', { class: 'cc-task-list' });
  const summaryNode = h('p', { class: 'cc-panel-sub cc-task-summary' });
  const filterHost = h('div', { class: 'cc-task-filters' });
  const clearButton = h('button', { class: 'cc-btn cc-btn-ghost cc-btn-sm', type: 'button' }, 'Clear completed');

  function refresh() {
    const filter = viewState.taskFilter;
    const { total, done, open } = store.taskSummary();
    syncTaskDerivedNumbers();
    summaryNode.textContent = total
      ? `${open} open · ${done} completed · ${total} total`
      : 'No tasks yet — the list is stored in this browser.';

    mount(
      filterHost,
      chips({
        label: 'Filter tasks',
        value: filter,
        options: [
          { id: 'all', label: `All ${total ? `(${total})` : ''}`.trim() },
          { id: 'open', label: `Open ${open ? `(${open})` : ''}`.trim() },
          { id: 'done', label: `Done ${done ? `(${done})` : ''}`.trim() },
        ],
        onPick: (id) => {
          viewState.taskFilter = id;
          refresh();
        },
      })
    );

    clearButton.disabled = done === 0;
    clearButton.textContent = done ? `Clear completed (${done})` : 'Clear completed';

    const visible = store.state.tasks.filter((task) =>
      filter === 'open' ? !task.done : filter === 'done' ? task.done : true
    );

    if (!visible.length) {
      clear(listHost);
      listHost.append(
        h(
          'li',
          { class: 'cc-empty cc-empty-sm' },
          h('span', { class: 'cc-empty-mark', 'aria-hidden': 'true' }, total ? '◌' : '✧'),
          h('strong', {}, total ? 'Nothing in this filter' : 'No tasks yet'),
          h(
            'span',
            {},
            total
              ? 'Switch the filter above to see the rest of the list.'
              : 'Add your first task above. It is saved in this browser and survives a reload.'
          ),
          total
            ? h(
                'button',
                {
                  class: 'cc-btn cc-btn-ghost cc-btn-sm',
                  type: 'button',
                  onclick: () => {
                    viewState.taskFilter = 'all';
                    refresh();
                  },
                },
                'Show all tasks'
              )
            : null
        )
      );
      return;
    }

    mount(
      listHost,
      visible.map((task) => {
        const model = modelById(task.model);
        return h(
          'li',
          { class: `cc-task${task.done ? ' is-done' : ''}` },
          h('label', { class: 'cc-checkbox' },
            h('input', {
              type: 'checkbox',
              checked: task.done,
              'aria-label': `Mark “${task.title}” ${task.done ? 'as open' : 'as completed'}`,
              onchange: () => {
                store.toggleTask(task.id);
                refresh();
                app.refreshChrome();
              },
            }),
            h('span', { class: 'cc-checkbox-box', 'aria-hidden': 'true' })
          ),
          h(
            'div',
            { class: 'cc-task-body' },
            h('span', { class: 'cc-task-title' }, task.title),
            h(
              'span',
              { class: 'cc-task-meta' },
              modelChip(model, { compact: true }),
              h('span', {}, task.done && task.completedAt ? `completed ${timeAgo(task.completedAt)}` : `added ${timeAgo(task.createdAt)}`)
            )
          ),
          h(
            'button',
            {
              class: 'cc-icon-btn cc-icon-btn-sm cc-task-delete',
              type: 'button',
              'aria-label': `Delete “${task.title}”`,
              title: 'Delete task',
              onclick: () => {
                store.deleteTask(task.id);
                refresh();
                app.refreshChrome();
                toast('Task deleted.', '');
              },
            },
            '✕'
          )
        );
      })
    );
  }

  function submit() {
    const result = store.addTask(input.value);
    if (!result.ok) {
      error.textContent = result.error;
      input.classList.add('has-error');
      input.setAttribute('aria-invalid', 'true');
      input.focus();
      return;
    }
    error.textContent = '';
    input.classList.remove('has-error');
    input.removeAttribute('aria-invalid');
    input.value = '';
    counter.textContent = `0 / ${store.MAX_TASK_LENGTH}`;
    viewState.taskFilter = 'all';
    refresh();
    app.refreshChrome();
    toast('Task added.', 'ok');
  }

  const form = h(
    'form',
    {
      class: 'cc-task-form',
      onsubmit: (event) => {
        event.preventDefault();
        submit();
      },
    },
    h('div', { class: 'cc-task-row' },
      input,
      h('button', { class: 'cc-btn cc-btn-primary', type: 'submit' }, 'Add task')
    ),
    h('div', { class: 'cc-task-below' }, error, counter)
  );

  input.addEventListener('input', () => {
    counter.textContent = `${input.value.length} / ${store.MAX_TASK_LENGTH}`;
    if (input.classList.contains('has-error') && input.value.trim()) {
      error.textContent = '';
      input.classList.remove('has-error');
      input.removeAttribute('aria-invalid');
    }
  });

  clearButton.addEventListener('click', () => {
    const removed = store.clearCompletedTasks();
    if (removed) {
      refresh();
      app.refreshChrome();
      toast(`${removed} completed task${removed === 1 ? '' : 's'} cleared.`, 'ok');
    }
  });

  refresh();
  return panel({
    title: 'Task panel',
    subtitle: 'Add, complete and delete tasks — the list is kept in this browser.',
    className: 'cc-panel-tasks',
    actions: null,
    body: h('div', {}, summaryNode, form, filterHost, listHost, h('div', { class: 'cc-task-foot' }, clearButton)),
  });
}

function chartPanel(app) {
  const chartHost = h('div', { class: 'cc-chart' });
  const detail = h('div', { class: 'cc-chart-detail' });
  const legendHost = h('div', { class: 'cc-legend' });
  const rangeHost = h('div', { class: 'cc-range' });

  const series = ACTIVITY_SERIES;

  function renderDetail() {
    if (viewState.selectedDay === null) {
      const peak = peakDay(series);
      mount(
        detail,
        h('span', { class: 'cc-hint' }, `Hover, tap or use ← → to inspect a day. Busiest day: ${peak.weekday} ${peak.label} with ${peak.requests.toLocaleString()} requests.`)
      );
      return;
    }
    const rows = series.slice(Math.max(0, series.length - viewState.range));
    const row = rows[viewState.selectedDay];
    if (!row) {
      viewState.selectedDay = null;
      renderDetail();
      return;
    }
    mount(
      detail,
      h('span', { class: 'cc-detail-pin' }, 'Pinned'),
      h('strong', {}, `${row.weekday} ${row.label}`),
      h('span', {}, `${row.requests.toLocaleString()} requests`),
      h('span', {}, `${row.tasks} tasks`),
      h('span', {}, `${row.errors} errors`),
      h('span', {}, `${row.latency} ms avg`),
      h(
        'button',
        {
          class: 'cc-btn cc-btn-ghost cc-btn-sm',
          type: 'button',
          onclick: () => {
            viewState.selectedDay = null;
            chartHandle?.update({ selected: null });
            renderDetail();
          },
        },
        'Clear'
      )
    );
  }

  function draw() {
    chartHandle?.destroy();
    chartHandle = createActivityChart(chartHost, {
      series,
      range: viewState.range,
      visible: viewState.visible,
      selected: viewState.selectedDay,
      onSelect: (index) => {
        viewState.selectedDay = index;
        chartHandle.update({ selected: index });
        renderDetail();
      },
    });
    renderDetail();
  }

  function rangeChips() {
    return chips({
      label: 'Chart range',
      value: viewState.range,
      options: [
        { id: 7, label: '7D' },
        { id: 14, label: '14D' },
        { id: 30, label: '30D' },
      ],
      onPick: (id) => {
        viewState.range = Number(id);
        viewState.selectedDay = null;
        mount(rangeHost, [rangeChips()]);
        draw();
      },
    });
  }

  mount(rangeHost, [rangeChips()]);

  function legendChips() {
    return chips({
      label: 'Visible series',
      multi: true,
      value: viewState.visible,
      options: [
        { id: 'requests', label: 'API requests (simulated)' },
        { id: 'tasks', label: 'Tasks completed' },
        { id: 'errors', label: 'Errors' },
      ],
      onPick: (id) => {
        const next = { ...viewState.visible, [id]: !viewState.visible[id] };
        if (!next.requests && !next.tasks && !next.errors) {
          toast('Keep at least one series visible.', 'warn');
          return;
        }
        viewState.visible = next;
        mount(legendHost, [legendChips()]);
        chartHandle?.update({ visible: next });
      },
    });
  }

  mount(legendHost, [legendChips()]);

  draw();

  return panel({
    title: 'Activity',
    subtitle: 'Thirty days of simulated traffic. Hover, click or use the arrow keys to inspect a day.',
    actions: h('div', { class: 'cc-panel-controls' }, rangeHost),
    body: h('div', {}, chartHost, legendHost, detail),
  });
}

function routingPanel(app) {
  const detailHost = h('div', { class: 'cc-routing-detail' });
  const listHost = h('div', { class: 'cc-routing-list', role: 'radiogroup', 'aria-label': 'Demo model catalogue' });

  function refresh() {
    const selected = modelById(store.state.preferences.model);
    mount(
      detailHost,
      h('div', { class: 'cc-routing-selected' },
        modelChip(selected),
        h('h4', {}, selected.name),
        h('dl', { class: 'cc-specs' },
          h('div', {}, h('dt', {}, 'Context'), h('dd', {}, selected.context)),
          h('div', {}, h('dt', {}, 'Throughput'), h('dd', {}, selected.throughput)),
          h('div', {}, h('dt', {}, 'Suited to'), h('dd', {}, selected.best_for))
        ),
        h('p', { class: 'cc-note' }, 'Placeholder entry. Selecting a model only changes this page — no request leaves the browser.')
      ),
      h('button', { class: 'cc-btn cc-btn-ghost cc-btn-sm', type: 'button', onclick: () => toast('Demo check complete: no network call was made.', 'ok') }, 'Run demo check')
    );
    mount(
      listHost,
      modelCatalog.map((model) =>
        h('button', {
          type: 'button',
          role: 'radio',
          'aria-checked': model.id === store.state.preferences.model ? 'true' : 'false',
          class: `cc-routing-item${model.id === store.state.preferences.model ? ' is-active' : ''}`,
          onclick: () => app.selectModel(model.id),
        },
          modelChip(model),
          h('span', { class: 'cc-routing-name' }, model.name),
          h('span', { class: 'cc-routing-meta' }, `${model.context} · ${model.throughput}`)
        )
      )
    );
  }

  refresh();
  return panel({
    title: 'Model routing',
    subtitle: 'Demo catalogue — every entry is a placeholder, none is connected to a provider.',
    className: 'cc-panel-routing',
    body: h('div', { class: 'cc-routing' }, detailHost, listHost),
  });
}

export function renderOverview(container, app) {
  const summary = seriesOverview();
  const { total, done } = store.taskSummary();
  const tasksTotal = summary.tasks + done;
  const peak = peakDay();

  const stats = h(
    'div',
    { class: 'cc-stats' },
    statCard({
      label: 'Total projects',
      iconName: 'projects',
      value: PROJECTS.length,
      format: (n) => String(Math.round(n)),
      footnote: 'in this demo workspace',
      extra: projectsStat(),
      tone: 'blue',
    }),
    statCard({
      label: 'Tasks completed',
      iconName: 'overview',
      value: tasksTotal,
      format: (n) => Math.round(n).toLocaleString(),
      valueId: 'cc-stat-tasks-value',
      footnoteId: 'cc-stat-tasks-note',
      delta: summary.deltas.tasks,
      footnote: done ? `includes ${done} you completed here` : 'vs previous 15 days',
      series: ACTIVITY_SERIES.map((row) => row.tasks),
      tone: 'violet',
    }),
    statCard({
      label: 'Success rate',
      iconName: 'activity',
      value: summary.successRate,
      format: (n) => `${n.toFixed(2)}%`,
      delta: summary.deltas.successRatePp,
      deltaUnit: 'pp',
      footnote: `${summary.errors.toLocaleString()} simulated errors`,
      series: ACTIVITY_SERIES.map((row) => ((row.requests - row.errors) / row.requests) * 100),
      tone: 'green',
    }),
    statCard({
      label: 'API requests',
      iconName: 'settings',
      value: summary.requests,
      format: (n) => compactNumber(n),
      delta: summary.deltas.requests,
      footnote: 'simulated · no live API',
      series: ACTIVITY_SERIES.map((row) => row.requests),
      tone: 'cyan',
    })
  );

  container.append(
    h('div', { class: 'cc-notice' },
      h('span', { class: 'cc-notice-tag' }, 'Demo'),
      h('p', {}, DEMO_NOTICE)
    ),
    stats,
    h('div', { class: 'cc-columns' },
      h('div', { class: 'cc-col-main' }, chartPanel(app), routingPanel(app)),
      h('div', { class: 'cc-col-side' }, taskPanel(app),
        panel({
          title: 'At a glance',
          subtitle: 'Derived from the simulated series',
          body: h('dl', { class: 'cc-facts' },
            h('div', {}, h('dt', {}, 'Peak day'), h('dd', {}, `${peak.weekday} ${peak.label} · ${peak.requests.toLocaleString()}`)),
            h('div', {}, h('dt', {}, 'Avg latency'), h('dd', {}, `${summary.latency} ms`)),
            h('div', {}, h('dt', {}, 'Simulated spend'), h('dd', {}, `$${summary.spend.toFixed(2)}`)),
            h('div', {}, h('dt', {}, 'Open tasks'), h('dd', { id: 'cc-fact-open-tasks' }, String(store.taskSummary().open)))
          ),
        })
      )
    )
  );
}

// ---------------------------------------------------------------------------
// Projects
// ---------------------------------------------------------------------------

function projectCard(project, app) {
  const model = modelById(project.model);
  const progress = Math.max(0, Math.min(100, project.progress));
  return h(
    'article',
    { class: 'cc-project glass' },
    h(
      'header',
      { class: 'cc-project-head' },
      h('div', {}, h('h3', {}, project.name), h('p', { class: 'cc-muted' }, project.description)),
      statusBadge(project.status)
    ),
    h(
      'div',
      { class: 'cc-progress', role: 'progressbar', 'aria-valuenow': String(progress), 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-label': `${project.name} progress` },
      h('span', { class: 'cc-progress-fill', style: `width:${progress}%` })
    ),
    h(
      'div',
      { class: 'cc-project-stats' },
      h('span', {}, h('strong', {}, `${progress}%`), ' complete'),
      h('span', {}, h('strong', {}, `${project.tasks.done}/${project.tasks.total}`), ' tasks'),
      h('span', {}, h('strong', {}, compactNumber(project.requests)), ' requests')
    ),
    h(
      'div',
      { class: 'cc-project-foot' },
      h('div', { class: 'cc-project-tags' }, modelChip(model), h('span', { class: 'cc-tag' }, project.environment), h('span', { class: 'cc-tag' }, project.owner), h('span', { class: 'cc-tag' }, `updated ${project.updated}`)),
      h(
        'button',
        {
          class: 'cc-btn cc-btn-ghost cc-btn-sm',
          type: 'button',
          onclick: () => app.focusActivity(project.name),
        },
        'Find in activity'
      )
    )
  );
}

function projectFilters(grid, app) {
  const query = h('input', {
    type: 'search',
    class: 'cc-input cc-input-search',
    placeholder: 'Search projects, owners or models…',
    value: viewState.projectQuery,
    'aria-label': 'Search projects',
    autocomplete: 'off',
  });
  const count = h('span', { class: 'cc-hint' });
  const chipsHost = h('div', {});

  function filtered() {
    const needle = viewState.projectQuery.trim().toLowerCase();
    return PROJECTS.filter((project) => {
      const matchesStatus = viewState.projectStatus === 'all' || project.status === viewState.projectStatus;
      if (!matchesStatus) return false;
      if (!needle) return true;
      return [project.name, project.description, project.owner, modelById(project.model).name, project.environment]
        .join(' ')
        .toLowerCase()
        .includes(needle);
    });
  }

  function renderGrid() {
    const rows = filtered();
    count.textContent = `${rows.length} of ${PROJECTS.length} projects`;
    if (!rows.length) {
      mount(grid, h('div', { class: 'cc-empty' },
        h('span', { class: 'cc-empty-mark', 'aria-hidden': 'true' }, '◌'),
        h('strong', {}, 'No projects match these filters'),
        h('span', {}, 'Try a different search term or switch the status filter back to All.'),
        h('button', { class: 'cc-btn cc-btn-ghost cc-btn-sm', type: 'button', onclick: () => {
          viewState.projectQuery = '';
          viewState.projectStatus = 'all';
          query.value = '';
          renderChips();
          renderGrid();
        } }, 'Reset filters')
      ));
      return;
    }
    mount(grid, rows.map((project) => projectCard(project, app)));
  }

  function renderChips() {
    mount(chipsHost, [chips({
      label: 'Filter projects by status',
      value: viewState.projectStatus,
      options: PROJECT_STATUSES,
      onPick: (id) => {
        viewState.projectStatus = id;
        renderChips();
        renderGrid();
      },
    })]);
  }

  query.addEventListener('input', () => {
    viewState.projectQuery = query.value;
    renderGrid();
  });
  query.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      query.value = '';
      viewState.projectQuery = '';
      renderGrid();
    }
  });

  renderChips();
  renderGrid();
  return h('div', { class: 'cc-projects-bar' }, query, chipsHost, count);
}

export function renderProjects(container, app) {
  const grid = h('div', { class: 'cc-projects' });
  container.append(
    panel({
      title: 'Projects',
      subtitle: 'Ten sample programmes, their demo model routing and progress.',
      body: h('div', {}, projectFilters(grid, app)),
    }),
    grid
  );
}

// ---------------------------------------------------------------------------
// Activity
// ---------------------------------------------------------------------------

function downloadJSON(filename, payload) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = h('a', { href: url, download: filename });
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function dayLabel(timestamp, now) {
  const today = new Date(now);
  const startOfToday = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();
  if (timestamp >= startOfToday) return 'Today';
  if (timestamp >= startOfToday - 86400000) return 'Yesterday';
  return new Date(timestamp).toLocaleDateString(undefined, { weekday: 'long', month: 'short', day: 'numeric' });
}

const ACTIVITY_GLYPH = { task: '✓', model: '◈', run: '▶', system: '⚙' };

function activityRow(entry) {
  return h(
    'li',
    { class: `cc-event type-${entry.type}` },
    h('span', { class: 'cc-event-ico', 'aria-hidden': 'true' }, ACTIVITY_GLYPH[entry.type] || '•'),
    h('div', { class: 'cc-event-body' },
      h('span', { class: 'cc-event-title' }, entry.title),
      entry.detail ? h('span', { class: 'cc-event-detail' }, entry.detail) : null
    ),
    h('time', { class: 'cc-event-time', datetime: new Date(entry.at).toISOString() }, timeAgo(entry.at))
  );
}

export function renderActivity(container, app) {
  const listHost = h('div', { class: 'cc-timeline' });
  const countNode = h('span', { class: 'cc-hint' });
  const filterHost = h('div', {});
  const query = h('input', {
    type: 'search',
    class: 'cc-input cc-input-search',
    placeholder: 'Search the local log…',
    value: viewState.activityQuery,
    'aria-label': 'Search activity',
    autocomplete: 'off',
  });

  function matches(entry) {
    const typeOk = viewState.activityType === 'all' || entry.type === viewState.activityType;
    if (!typeOk) return false;
    const needle = viewState.activityQuery.trim().toLowerCase();
    if (!needle) return true;
    return `${entry.title} ${entry.detail}`.toLowerCase().includes(needle);
  }

  function renderList() {
    const rows = store.state.activity.filter(matches);
    countNode.textContent = `${rows.length} of ${store.state.activity.length} entries · ${store.persistenceMode === 'localStorage' ? 'stored in this browser' : 'in memory (storage unavailable)'}`;
    if (!rows.length) {
      mount(listHost, h('div', { class: 'cc-empty' },
        h('span', { class: 'cc-empty-mark', 'aria-hidden': 'true' }, '◌'),
        h('strong', {}, store.state.activity.length ? 'Nothing matches that filter' : 'The log is empty'),
        h('span', {}, store.state.activity.length
          ? 'Loosen the search or pick a different category.'
          : 'Add, complete or delete tasks and the events will show up here.'),
        h('button', { class: 'cc-btn cc-btn-ghost cc-btn-sm', type: 'button', onclick: () => {
          viewState.activityType = 'all';
          viewState.activityQuery = '';
          query.value = '';
          renderChips();
          renderList();
        } }, 'Reset filters')
      ));
      return;
    }

    const groups = [];
    const now = Date.now();
    for (const entry of rows) {
      const label = dayLabel(entry.at, now);
      const last = groups[groups.length - 1];
      if (last && last.label === label) last.entries.push(entry);
      else groups.push({ label, entries: [entry] });
    }

    mount(listHost, groups.map((group) =>
      h('section', { class: 'cc-day' },
        h('h4', { class: 'cc-day-label' }, group.label, h('span', {}, `${group.entries.length}`)),
        h('ul', { class: 'cc-event-list' }, group.entries.map(activityRow))
      )
    ));
  }

  function renderChips() {
    mount(filterHost, [chips({
      label: 'Filter activity by category',
      value: viewState.activityType,
      options: activityTypes,
      onPick: (id) => {
        viewState.activityType = id;
        renderChips();
        renderList();
      },
    })]);
  }

  query.addEventListener('input', () => {
    viewState.activityQuery = query.value;
    renderList();
  });
  query.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      query.value = '';
      viewState.activityQuery = '';
      renderList();
    }
  });

  const exportButton = h('button', { class: 'cc-btn cc-btn-ghost cc-btn-sm', type: 'button' }, 'Export log');
  exportButton.addEventListener('click', () => {
    downloadJSON('ai-command-center-activity.json', { exportedAt: new Date().toISOString(), demo: true, activity: store.state.activity });
    toast('Activity log exported as JSON.', 'ok');
  });

  const clearButton = h('button', { class: 'cc-btn cc-btn-ghost cc-btn-sm cc-btn-danger', type: 'button' }, 'Clear log');
  clearButton.addEventListener('click', () => {
    if (!store.state.activity.length) return;
    if (!confirmAction('Clear the local activity log? Tasks and preferences are kept.')) return;
    store.state.activity = [];
    store.persistActivity();
    renderList();
    toast('Activity log cleared.', '');
  });

  renderChips();
  renderList();

  container.append(
    panel({
      title: 'Activity',
      subtitle: 'Everything that happened in this browser session, newest first.',
      actions: h('div', { class: 'cc-panel-actions' }, exportButton, clearButton),
      body: h('div', {},
        h('div', { class: 'cc-projects-bar' }, query, filterHost, countNode),
        listHost
      ),
    })
  );
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

export function renderSettings(container, app) {
  const { theme, motion, model } = store.state.preferences;
  const { total, done, open } = store.taskSummary();
  const selected = modelById(model);

  const themePanel = panel({
    title: 'Appearance',
    subtitle: 'Theme and motion. Both are stored in this browser.',
    body: h('div', { class: 'cc-settings-row' },
      h('div', { class: 'cc-setting' },
        h('h4', {}, 'Theme'),
        h('p', { class: 'cc-muted' }, 'Follow the operating system, or pin a theme'),
        chips({
          label: 'Theme',
          value: theme,
          options: [{ id: 'dark', label: 'Dark' }, { id: 'light', label: 'Light' }, { id: 'system', label: 'System' }],
          onPick: (id) => app.setPreference('theme', id),
        })
      ),
      h('div', { class: 'cc-setting' },
        h('h4', {}, 'Motion'),
        h('p', { class: 'cc-muted' }, 'Reduced motion removes card transitions and chart animation'),
        chips({
          label: 'Motion',
          value: motion,
          options: [{ id: 'full', label: 'Full' }, { id: 'reduced', label: 'Reduced' }],
          onPick: (id) => app.setPreference('motion', id),
        })
      )
    ),
  });

  const modelSelect = h('select', { class: 'cc-select', id: 'cc-settings-model', 'aria-label': 'Default demo model' },
    modelCatalog.map((entry) => h('option', { value: entry.id, selected: entry.id === model ? 'selected' : null }, `${entry.name} (${entry.tag})`))
  );
  modelSelect.addEventListener('change', () => app.selectModel(modelSelect.value));

  const modelPanel = panel({
    title: 'Default model',
    subtitle: 'Placeholder catalogue — nothing here talks to a provider.',
    body: h('div', { class: 'cc-setting' },
      h('label', { class: 'cc-field' }, h('span', {}, 'Active demo model'), modelSelect),
      h('div', { class: 'cc-routing-selected cc-routing-inline' },
        modelChip(selected),
        h('dl', { class: 'cc-specs' },
          h('div', {}, h('dt', {}, 'Context'), h('dd', {}, selected.context)),
          h('div', {}, h('dt', {}, 'Throughput'), h('dd', {}, selected.throughput)),
          h('div', {}, h('dt', {}, 'Suited to'), h('dd', {}, selected.best_for))
        )
      ),
      h('p', { class: 'cc-note' }, 'No API key is configured and this page never calls an external service. New tasks record whichever model is selected here.')
    ),
  });

  const exportButton = h('button', { class: 'cc-btn cc-btn-primary cc-btn-sm', type: 'button' }, 'Export state as JSON');
  exportButton.addEventListener('click', () => {
    downloadJSON('ai-command-center-state.json', store.exportPayload());
    toast('Demo state exported.', 'ok');
  });

  const clearButton = h('button', { class: 'cc-btn cc-btn-ghost cc-btn-sm', type: 'button' }, done ? `Clear ${done} completed task${done === 1 ? '' : 's'}` : 'Clear completed tasks');
  clearButton.disabled = done === 0;
  clearButton.addEventListener('click', () => {
    const removed = store.clearCompletedTasks();
    app.rerender();
    if (removed) toast(`${removed} task${removed === 1 ? '' : 's'} cleared.`, 'ok');
  });

  const resetButton = h('button', { class: 'cc-btn cc-btn-ghost cc-btn-sm cc-btn-danger', type: 'button' }, 'Reset demo workspace');
  resetButton.addEventListener('click', () => {
    if (!confirmAction('Reset tasks, preferences and the activity log back to the demo defaults?')) return;
    store.resetEverything();
    viewState.taskFilter = 'all';
    viewState.selectedDay = null;
    app.applyPreferences();
    app.rerender();
    toast('Demo workspace reset.', 'ok');
  });

  const dataPanel = panel({
    title: 'Local data',
    subtitle: store.persistenceMode === 'localStorage'
      ? 'Tasks, preferences and the activity log live in this browser only.'
      : 'Browser storage is unavailable here, so state is kept in memory for this tab only.',
    body: h('div', {},
      h('dl', { class: 'cc-facts cc-facts-inline' },
        h('div', {}, h('dt', {}, 'Persistence'), h('dd', {}, store.persistenceMode === 'localStorage' ? 'localStorage' : 'in memory')),
        h('div', {}, h('dt', {}, 'Tasks'), h('dd', {}, `${total} total · ${open} open · ${done} done`)),
        h('div', {}, h('dt', {}, 'Log entries'), h('dd', {}, String(store.state.activity.length))),
        h('div', {}, h('dt', {}, 'Network calls'), h('dd', {}, 'none'))
      ),
      h('div', { class: 'cc-actions' }, exportButton, clearButton, resetButton)
    ),
  });

  const aboutPanel = panel({
    title: 'About this build',
    subtitle: 'AI Command Center demo',
    body: h('dl', { class: 'cc-facts cc-facts-inline' },
      h('div', {}, h('dt', {}, 'Version'), h('dd', {}, '1.0.0 (demo)')),
      h('div', {}, h('dt', {}, 'Served by'), h('dd', {}, 'The existing FastAPI app, as a static page')),
      h('div', {}, h('dt', {}, 'Charts'), h('dd', {}, 'Hand-built SVG, no charting dependency')),
      h('div', {}, h('dt', {}, 'Backend'), h('dd', {}, 'Mock data only — no live model API'))
    ),
  });

  container.append(
    h('div', { class: 'cc-settings' },
      themePanel,
      modelPanel,
      dataPanel,
      aboutPanel,
      h('p', { class: 'cc-note cc-note-wide' },
        DEMO_NOTICE, ' ', h('a', { href: '/' }, 'Back to the SPADE workspace'), '.'
      )
    )
  );
}
