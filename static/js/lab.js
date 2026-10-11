// NAMI OPTIMIZER | Pipeline Intelligence Lab.
//
// This is the benchmarking surface of the project: the SPADE reference pipeline
// and the NAMI Optimizer, compared on the same input with the same resolved plan.
//
// Two rules hold everywhere on this page:
//
//   1. Every number is rendered from a backend response. Nothing is a sample or
//      a placeholder, and a figure the backend refused to claim is shown as
//      "not claimed" rather than as zero.
//   2. Where a figure is an estimate (memory) the page says so next to it.

import { api, benchmarkArtifactUrl } from './api.js';
import { pairBars } from './charts.js';
import { card, kvList, statCard, verificationChecks } from './views.js';
import { h, frag, fmt, mount, statusBadge, toast, confirmAction, option, readableList } from './util.js';

const POLL_MS = 1500;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// --------------------------------------------------------------------------- header

function comparisonHeader(policies) {
  const reference = policies.reference_system || {};
  const nami = policies.nami_system || {};
  const formats = ((policies.support || {}).supported_formats || []).join(', ').toUpperCase();
  return card({ className: 'lab-head' },
    h('div', { class: 'lab-banner' },
      h('span', { class: 'pill violet' }, 'Pipeline Intelligence Lab'),
      h('h2', { class: 'lab-title' }, 'NAMI Optimizer'),
      h('p', { class: 'muted' }, nami.meaning
        ? `${nami.meaning} — adaptive, memory-aware pipeline optimisation, benchmarked against the SPADE reference pipeline.`
        : 'Adaptive, memory-aware pipeline optimisation, benchmarked against the SPADE reference pipeline.'),
      h('p', { class: 'muted small' }, nami.description || '')
    ),
    h('div', { class: 'lab-versus' },
      h('div', { class: 'vs-card reference' },
        h('span', { class: 'vs-tag' }, 'A · reference'),
        h('h3', {}, reference.name || 'SPADE reference pipeline'),
        h('p', { class: 'muted small' }, reference.description || '')
      ),
      h('div', { class: 'vs-mid' }, 'vs'),
      h('div', { class: 'vs-card nami' },
        h('span', { class: 'vs-tag' }, 'B · candidate'),
        h('h3', {}, nami.name || 'NAMI Optimizer'),
        h('p', { class: 'muted small' }, nami.description || '')
      )
    ),
    h('p', { class: 'hint' },
      'Both systems consume the same resolved plan and write through the same container writer, so an output '
      + 'difference can only come from the transformation. Every figure below is measured in this deployment at '
      + `run time; nothing is simulated. Benchmarkable by NAMI today: ${formats}. Other formats are still handled `
      + "by SPADE's standard pipeline.")
  );
}

function policyBadge(policy) {
  if (!policy || !policy.id) return h('span', { class: 'badge neutral' }, 'no policy chosen yet');
  return h('span', { class: 'badge violet' }, `${policy.name}${policy.forced ? ' · forced' : ''}`);
}

// --------------------------------------------------------------------------- profile panel

function profilePanel(file, job) {
  if (!file) {
    return card({ title: 'Dataset profiling and preview', subtitle: 'Select a dataset to profile it' },
      h('div', { class: 'empty' }, h('p', {}, 'Upload a CSV, TSV or Excel file on the Upload File page, then choose it here.')));
  }
  if (!job) {
    return card({ title: 'Dataset profiling and preview', subtitle: file.original_name },
      h('div', { class: 'empty' }, h('p', {}, 'Profiling starts automatically when a dataset is selected.')));
  }
  if (job.status === 'failed') {
    return card({ title: 'Dataset profiling and preview', subtitle: file.original_name },
      h('div', { class: 'alert err' }, job.error || 'Profiling failed.'));
  }
  if (job.status !== 'succeeded') {
    return card({ title: 'Dataset profiling and preview', subtitle: file.original_name },
      h('p', { class: 'muted small' }, `Profiling the workload: ${job.status}…`));
  }

  const result = job.result || {};
  const profile = result.workload_profile || {};
  const policy = result.policy || {};
  const schema = profile.schema || [];
  const available = (result.operations && result.operations.available) || [];

  return card({
    title: 'Dataset profiling and preview',
    subtitle: `${file.original_name} — measured by NAMI before any transformation`,
    actions: policyBadge(policy),
  },
    h('div', { class: 'grid cols-4' },
      statCard({
        label: 'Rows',
        value: fmt.int(profile.rows),
        sub: profile.rows_exact ? 'every row scanned' : 'estimated from bytes per row',
      }),
      statCard({
        label: 'Columns',
        value: fmt.int(profile.columns),
        sub: `${(profile.numeric_columns || []).length} numeric · ${(profile.text_columns || []).length} text`,
      }),
      statCard({
        label: 'Missing cells',
        value: profile.missing_pct === null || profile.missing_pct === undefined ? 'n/a' : `${profile.missing_pct}%`,
        sub: profile.missing_cells_exact ? 'measured over the scan' : 'sampled window',
      }),
      statCard({
        label: 'Duplicates (sample)',
        value: profile.duplicate_pct_in_sample === null || profile.duplicate_pct_in_sample === undefined
          ? 'n/a'
          : `${profile.duplicate_pct_in_sample}%`,
        sub: 'indicator used for policy selection',
        kind: (profile.duplicate_pct_in_sample || 0) > 0 ? 'warn' : '',
      })
    ),
    h('div', { class: 'grid side', style: 'margin-top:.9rem' },
      h('div', {},
        h('h4', { class: 'lab-sub' }, 'Why this policy was selected'),
        h('p', { class: 'muted small' }, policy.strategy || ''),
        h('ul', { class: 'lab-reasons' }, ...(policy.reasons || []).map((reason) => h('li', {}, reason))),
        h('h4', { class: 'lab-sub' }, 'Column schema (sampled values)'),
        h('div', { class: 'scroll' }, h('table', { class: 'data' },
          h('thead', {}, h('tr', {},
            h('th', {}, 'Column'), h('th', {}, 'Type'), h('th', { class: 'num' }, 'Missing'),
            h('th', { class: 'num' }, 'Max len'), h('th', {}, 'Examples'))),
          h('tbody', {}, ...schema.map((column) => h('tr', {},
            h('td', {}, column.name),
            h('td', {}, h('span', { class: 'badge neutral' }, column.type)),
            h('td', { class: 'num' }, fmt.int(column.missing)),
            h('td', { class: 'num' }, fmt.int(column.max_length)),
            h('td', { class: 'muted small' }, readableList(column.examples || [], 2))
          )))
        ))
      ),
      h('div', {},
        h('h4', { class: 'lab-sub' }, 'Footprint (estimates, used to choose the policy)'),
        kvList([
          ['Table in memory', fmt.bytes(profile.estimated_table_bytes)],
          ['Dedupe state', fmt.bytes(profile.estimated_digest_state_bytes)],
          ['Memory budget', fmt.bytes((result.thresholds || {}).memory_budget_bytes)],
          ['Chunk rows', fmt.int((result.thresholds || {}).chunk_rows)],
          ['Profile scan time', fmt.seconds(profile.scan_seconds)],
          ['Cells scanned', fmt.int((profile.rows_scanned || 0) * (profile.columns || 0))],
        ]),
        h('p', { class: 'hint' }, ((profile.estimates || {}).note) || 'Memory figures are estimates used to choose a policy, never reported as measurements.'),
        h('h4', { class: 'lab-sub' }, 'Plan — identical for both systems'),
        kvList([
          ['Operation ids', (result.plan && result.plan.operation_ids || []).join(', ') || 'none selected'],
          ['Columns in → out', `${fmt.int((result.plan || {}).columns_in)} → ${fmt.int((result.plan || {}).columns_out)}`],
          ['Deduplicate records', String(Boolean((result.plan || {}).deduplicate))],
          ['Trim whitespace', String(Boolean((result.plan || {}).trim_whitespace))],
          ['Canonicalise numbers', String(Boolean((result.plan || {}).canonicalize_numbers))],
          ['Drop blank rows', String(Boolean((result.plan || {}).drop_empty_rows))],
        ]),
        h('p', { class: 'hint' }, `${available.length} operation(s) available for this format; the selection above is what both systems will execute.`)
      )
    )
  );
}

// --------------------------------------------------------------------------- results

function verdictBadge(pct) {
  if (pct === null || pct === undefined) return h('span', { class: 'muted small' }, 'not claimed');
  if (pct > 0.5) return h('span', { class: 'badge ok' }, `improvement ${fmt.pct(pct)}`);
  if (pct < -0.5) return h('span', { class: 'badge warn' }, `regression ${fmt.pct(pct)}`);
  return h('span', { class: 'badge neutral' }, `within threshold ${fmt.pct(pct)}`);
}

function metricRow(label, baseline, optimized, format, pct) {
  return h('tr', {},
    h('td', {}, label),
    h('td', { class: 'num' }, format(baseline)),
    h('td', { class: 'num' }, format(optimized)),
    h('td', { class: 'num' }, pct === null || pct === undefined ? 'n/a' : fmt.pct(pct)),
    h('td', {}, verdictBadge(pct))
  );
}

const median = (block, key) => (block && block[key] ? block[key].median : null);

function resultsPanel(benchmark) {
  if (!benchmark) {
    return card({ title: 'Comparison results' },
      h('div', { class: 'empty' }, h('p', {}, 'Run a comparison to see measured before-and-after figures.')));
  }
  if (benchmark.status !== 'succeeded' || !benchmark.result || !benchmark.result.ok) {
    return card({ title: 'Comparison results', subtitle: benchmark.file_name, actions: h('span', { class: 'badge err' }, benchmark.status) },
      h('div', { class: 'alert err' }, benchmark.error || 'This run has no result document.'),
      h('p', { class: 'hint' }, 'Failures are stored with their error instead of being replaced by a partial or optimistic result.'));
  }

  const result = benchmark.result;
  const comparison = result.comparison || {};
  const end = comparison.end_to_end || {};
  const exec = comparison.execution_only || {};
  const memory = comparison.memory || {};
  const throughput = comparison.throughput || {};
  const size = comparison.file_size || {};
  const correctness = result.correctness || {};
  const policy = result.policy || {};
  const trials = result.trials || {};
  const aggregation = trials.aggregation || {};
  const records = comparison.records || {};

  const verified = correctness.status === 'passed'
    ? h('span', { class: 'badge ok' }, 'output verified')
    : correctness.status === 'passed_with_notes'
      ? h('span', { class: 'badge warn' }, 'verified with notes')
      : h('span', { class: 'badge err' }, 'verification failed');

  const findings = [
    ...(comparison.improvements || []).map((item) => ({ kind: 'ok', label: item.label, detail: `Measured ${fmt.pct(item.pct)} better.` })),
    ...(comparison.regressions || []).map((item) => ({ kind: 'warn', label: item.label, detail: `Measured ${fmt.pct(item.pct)} worse.` })),
  ];

  return frag(
    card({
      title: 'SPADE vs NAMI',
      subtitle: `${benchmark.file_name} · policy ${policy.id} · trials ${trials.completed}/${trials.requested} measured, ${trials.warmup_runs} warm-up pair(s) discarded`,
      actions: verified,
    },
      h('div', { class: 'grid cols-2 vs-metrics' },
        h('div', { class: 'vs-card reference' },
          h('span', { class: 'vs-tag' }, 'A · SPADE reference'),
          kvList([
            ['Execution (median)', fmt.seconds(median(aggregation.spade, 'wall_seconds'))],
            ['CPU (median)', fmt.seconds(median(aggregation.spade, 'cpu_seconds'))],
            ['Peak Python heap', fmt.bytes(memory.baseline_peak_python_heap_bytes)],
            ['Records in / out', `${fmt.int(records.input)} / ${fmt.int(records.spade_output)}`],
            ['Throughput', `${fmt.int(throughput.baseline_per_second)} rec/s`],
          ])
        ),
        h('div', { class: 'vs-card nami' },
          h('span', { class: 'vs-tag' }, 'B · NAMI Optimizer'),
          kvList([
            ['Execution (median)', fmt.seconds(median(aggregation.nami_stage_execute, 'median'))],
            ['End-to-end (median)', fmt.seconds(median(aggregation.nami, 'wall_seconds'))],
            ['CPU (median)', fmt.seconds(median(aggregation.nami, 'cpu_seconds'))],
            ['Peak Python heap', fmt.bytes(memory.optimized_peak_python_heap_bytes)],
            ['Records in / out', `${fmt.int(records.input)} / ${fmt.int(records.nami_output)}`],
            ['Throughput', `${fmt.int(throughput.optimized_per_second)} rec/s`],
          ])
        )
      ),
      h('p', { class: 'hint' },
        'NAMI re-derives its policy from a fresh profile on every run, so its end-to-end figure includes profiling and policy selection. '
        + 'The execution-only comparison excludes that cost from both systems, which is the like-for-like figure for the transformation work.')
    ),

    h('div', { class: 'grid cols-2', style: 'margin-top:.9rem' },
      card({ title: 'Reduction table', subtitle: comparison.formula },
        h('table', { class: 'data' },
          h('thead', {}, h('tr', {},
            h('th', {}, 'Measure'), h('th', { class: 'num' }, 'Baseline'), h('th', { class: 'num' }, 'Optimized'),
            h('th', { class: 'num' }, 'Reduction'), h('th', {}, 'Verdict'))),
          h('tbody', {},
            metricRow('Execution-only wall clock', exec.baseline_seconds, exec.optimized_seconds, fmt.seconds, exec.reduction_pct),
            metricRow('End-to-end wall clock', end.baseline_seconds, end.optimized_seconds, fmt.seconds, end.reduction_pct),
            metricRow('CPU time', (comparison.cpu_time || {}).baseline_seconds, (comparison.cpu_time || {}).optimized_seconds, fmt.seconds, (comparison.cpu_time || {}).reduction_pct),
            metricRow('Peak Python heap', memory.baseline_peak_python_heap_bytes, memory.optimized_peak_python_heap_bytes, fmt.bytes, memory.reduction_pct),
            metricRow('Output size vs original', size.original_bytes, size.optimized_bytes, fmt.bytes, size.reduction_pct),
            metricRow('Records per second (higher is better)', throughput.baseline_per_second, throughput.optimized_per_second, fmt.int, throughput.change_pct)
          )
        ),
        end.within_noise
          ? h('p', { class: 'hint' },
            `The end-to-end difference is smaller than the combined trial spread (${fmt.seconds(end.trial_spread_seconds)}), so it is reported as within measurement noise rather than as a result.`)
          : null,
        ...(comparison.notes || []).map((note) => h('p', { class: 'hint' }, note))
      ),
      card({ title: 'Before and after', subtitle: 'Median values from the measured trials' },
        pairBars({ title: 'Execution-only wall clock', before: exec.baseline_seconds, after: exec.optimized_seconds, format: fmt.seconds, lowerIsBetter: true, note: 'profiling excluded from both systems' }),
        pairBars({ title: 'End-to-end wall clock', before: end.baseline_seconds, after: end.optimized_seconds, format: fmt.seconds, lowerIsBetter: true, note: 'NAMI end-to-end includes its profiling pass' }),
        pairBars({ title: 'Peak Python heap', before: memory.baseline_peak_python_heap_bytes, after: memory.optimized_peak_python_heap_bytes, format: fmt.bytes, lowerIsBetter: true, note: 'tracemalloc peak: a lower bound on true memory' }),
        pairBars({ title: 'Records per second (higher is better)', before: throughput.baseline_per_second, after: throughput.optimized_per_second, format: fmt.int, lowerIsBetter: false, note: 'not a reduction percentage' }),
        pairBars({ title: 'Output bytes', before: size.original_bytes, after: size.optimized_bytes, format: fmt.bytes, lowerIsBetter: true, note: size.reduction_claimed === false ? 'no size claim: re-encoding only' : 'compared with the original upload' })
      )
    ),

    h('div', { class: 'grid side', style: 'margin-top:.9rem' },
      card({ title: 'Correctness and validation', subtitle: correctness.summary, actions: verified },
        h('div', {},
          verificationChecks(correctness.checks),
          h('p', { class: 'hint' },
            `Rows compared: ${fmt.int(correctness.records_compared)} · reference artifact `
            + `${(correctness.reference || {}).rows === undefined ? 'n/a' : fmt.int(correctness.reference.rows)} rows · `
            + `NAMI artifact ${(correctness.candidate || {}).rows === undefined ? 'n/a' : fmt.int(correctness.candidate.rows)} rows. `
            + 'Both artifacts are re-read from disk for this check.')
        )
      ),
      h('div', {},
        card({ title: 'Selected policy', subtitle: policy.memory_behaviour, actions: policyBadge(policy) },
          h('p', { class: 'muted small' }, policy.strategy || ''),
          h('ul', { class: 'lab-reasons' }, ...(policy.reasons || []).map((reason) => h('li', {}, reason))),
          h('h4', { class: 'lab-sub' }, 'Thresholds in force'),
          kvList(Object.entries(result.thresholds || {}).map(([key, value]) => [
            key,
            typeof value === 'number' && /bytes|budget/.test(key) ? fmt.bytes(value) : fmt.int(value),
          ]))
        ),
        card({ title: 'Measured outcome', subtitle: 'Only what was measured on this input' },
          findings.length
            ? h('div', {}, ...findings.map((finding) => h('div', { class: `finding ${finding.kind}` },
              h('span', { class: 'dot' }),
              h('div', {}, h('strong', {}, finding.label), h('p', {}, finding.detail)))))
            : h('div', { class: 'finding info' },
              h('span', { class: 'dot' }),
              h('div', {},
                h('strong', {}, 'No significant change'),
                h('p', {}, `Every measured change stayed inside the ±${comparison.significance_threshold_pct}% significance threshold, so none is claimed as an improvement.`))),
          h('h4', { class: 'lab-sub' }, 'Trade-offs recorded'),
          h('ul', { class: 'lab-reasons' }, ...(comparison.trade_offs || []).map((item) => h('li', {}, item))),
          h('h4', { class: 'lab-sub' }, 'Stage timings of the last measured run'),
          kvList([
            ['Profile', fmt.seconds((trials.stage_timings || {}).nami_profile_seconds)],
            ['Policy decision', fmt.seconds((trials.stage_timings || {}).nami_policy_seconds)],
            ['Transform', fmt.seconds((trials.stage_timings || {}).nami_execute_seconds)],
          ]),
          h('p', { class: 'hint' }, trials.warmup_note || '')
        )
      )
    ),

    card({ title: 'Downloads', subtitle: 'Output, reference artifact and the machine-readable report' },
      benchmark.artifacts.length
        ? h('div', { class: 'artifact-list' }, ...benchmark.artifacts.map((artifact) => h('a', {
          class: 'btn btn-sm',
          href: benchmarkArtifactUrl(benchmark.id, artifact.id),
          download: artifact.filename,
        }, `↓ ${artifact.label || artifact.filename} (${fmt.bytes(artifact.size_bytes)})`)))
        : h('p', { class: 'muted small' }, 'No artifacts were produced.'),
      h('p', { class: 'hint' }, 'Your original upload is never modified: it stays in your account and can be downloaded or deleted at any time.')
    )
  );
}

// --------------------------------------------------------------------------- history

function historyPanel(benchmarks, actions) {
  if (!benchmarks.length) {
    return card({ title: 'Benchmark history' }, h('div', { class: 'empty' }, h('p', {}, 'No benchmark has been run yet.')));
  }
  const value = (entry, key) => {
    const summary = entry.summary || {};
    return summary[key] === undefined || summary[key] === null ? '—' : fmt.pct(summary[key]);
  };
  return card({ title: 'Benchmark history', subtitle: `${benchmarks.length} run(s) stored for this account` },
    h('div', { class: 'scroll' }, h('table', { class: 'data' },
      h('thead', {}, h('tr', {},
        h('th', {}, 'When'), h('th', {}, 'File'), h('th', {}, 'Policy'), h('th', {}, 'Status'),
        h('th', { class: 'num' }, 'Exec-only'), h('th', { class: 'num' }, 'End-to-end'),
        h('th', { class: 'num' }, 'Size'), h('th', {}, 'Correctness'), h('th', {}, ''))),
      h('tbody', {}, ...benchmarks.map((entry) => h('tr', {},
        h('td', { class: 'muted small' }, fmt.rel(entry.created_at)),
        h('td', {}, entry.file_name),
        h('td', { class: 'muted small' }, entry.force_policy ? `${entry.policy_id || '—'} (forced)` : (entry.policy_id || '—')),
        h('td', {}, statusBadge(entry.status)),
        h('td', { class: 'num' }, value(entry, 'execution_reduction_pct')),
        h('td', { class: 'num' }, value(entry, 'end_to_end_reduction_pct')),
        h('td', { class: 'num' }, value(entry, 'file_size_reduction_pct')),
        h('td', {}, (entry.summary || {}).correctness
          ? h('span', { class: `badge ${(entry.summary.correctness === 'passed' ? 'ok' : 'warn')}` }, entry.summary.correctness)
          : '—'),
        h('td', {}, h('div', { class: 'row-actions' },
          h('button', { class: 'btn btn-sm', onclick: () => actions.viewBenchmark(entry.id) }, 'View'),
          h('button', { class: 'btn btn-sm btn-ghost', onclick: () => actions.removeBenchmark(entry.id, entry.file_name) }, 'Delete')))
      )))
    ))
  );
}

function methodologyPanel(policies) {
  return card({ title: 'Methodology and limitations', subtitle: 'What this comparison does and does not show' },
    h('div', { class: 'grid cols-2' },
      h('div', {},
        h('h4', { class: 'lab-sub' }, 'Method'),
        h('ul', { class: 'lab-reasons' }, ...(policies.methodology || []).map((item) => h('li', {}, item))),
        h('h4', { class: 'lab-sub' }, 'Policy decision order'),
        h('ol', { class: 'lab-reasons' }, ...(policies.decision_order || []).map((item) => h('li', {}, item)))
      ),
      h('div', {},
        h('h4', { class: 'lab-sub' }, 'Limitations'),
        h('ul', { class: 'lab-reasons' }, ...(policies.limitations || []).map((item) => h('li', {}, item))),
        h('h4', { class: 'lab-sub' }, 'Measurement scope'),
        kvList(Object.entries(policies.measurement_scope || {}).map(([key, value]) => [key, String(value)])),
        h('p', { class: 'hint' }, policies.scope_note || '')
      )
    )
  );
}

// --------------------------------------------------------------------------- view

export async function renderNami(root, state, actions) {
  const [policies, fileList, history] = await Promise.all([
    api('/api/nami/policies'),
    api('/api/files'),
    api('/api/nami/benchmarks'),
  ]);
  state.namiPolicies = policies;
  state.files = fileList.files;
  state.namiHistory = history.benchmarks;

  const supported = (policies.support || {}).supported_formats || [];
  // The file list reports `detected_format`; a single-file payload reports `format`.
  const formatOf = (file) => file.detected_format || file.format || '';
  const analysable = fileList.files.filter((file) => supported.includes(formatOf(file)) && file.status !== 'analysis_failed');
  let selectedFileId = state.namiFileId && analysable.some((file) => file.id === state.namiFileId)
    ? state.namiFileId
    : (analysable[0] ? analysable[0].id : null);
  let fileDetail = null;
  let preflightJob = state.namiPreflight || null;
  let busy = false;

  const profileNode = h('div', {});
  const statusNode = h('div', {});
  const resultNode = h('div', {});
  const historyNode = h('div', {});
  const operationsNode = h('div', { class: 'ops-box' });

  const fileSelect = h('select', {},
    ...(analysable.length
      ? analysable.map((file) => option(file.id, `${file.original_name} · ${formatOf(file).toUpperCase()} · ${fmt.bytes(file.size_bytes)}`, file.id === selectedFileId))
      : [option('', 'No benchmarkable file uploaded yet', true)]));

  const trialsInput = h('input', {
    type: 'number',
    min: '1',
    max: String((policies.defaults || {}).max_trials || 7),
    value: String(state.namiTrials || (policies.defaults || {}).trials || 3),
  });
  const warmupInput = h('input', {
    type: 'checkbox',
    checked: state.namiWarmup === undefined ? Boolean((policies.defaults || {}).warmup) : state.namiWarmup,
  });
  const forceSelect = h('select', {},
    option('', 'Automatic — chosen from the measured profile', !state.namiForcePolicy),
    ...(policies.policies || []).map((policy) => option(policy.id, policy.name, state.namiForcePolicy === policy.id)));

  const selectedOperations = () =>
    [...operationsNode.querySelectorAll('input[type=checkbox]:checked')].map((node) => node.value);

  async function loadFileDetail(fileId) {
    if (!fileId) return null;
    try {
      return await api(`/api/files/${encodeURIComponent(fileId)}`);
    } catch (_) {
      return null;
    }
  }

  function renderOperations() {
    const catalogue = (fileDetail && fileDetail.analysis && fileDetail.analysis.operations) || {};
    const operations = catalogue.available || [];
    const recommended = new Set(catalogue.recommended_ids || []);
    mount(operationsNode,
      operations.length
        ? frag(
          h('p', { class: 'hint' }, 'Both systems receive exactly this selection. Operations that rewrite data keep their approval requirement, so nothing is applied silently.'),
          h('div', { class: 'ops-grid' }, ...operations.map((operation) => h('label', { class: 'op-item', title: operation.description },
            h('input', { type: 'checkbox', value: operation.id, checked: recommended.has(operation.id) }),
            h('span', { class: 'op-copy' },
              h('strong', {}, operation.label),
              h('span', { class: `badge ${operation.category === 'lossless' ? 'info' : 'warn'}` }, operation.category),
              operation.requires_approval ? h('span', { class: 'badge err' }, 'approval') : null,
              h('p', { class: 'muted small' }, operation.description)
            )
          )))
        )
        : h('p', { class: 'muted small' }, 'The analysis for this file exposes no operation catalogue yet. Open Upload File and wait for the analysis to finish, then reselect the file.')
    );
  }

  function renderStatus(lines, kind = 'info') {
    mount(statusNode, lines.length ? h('div', { class: `alert ${kind}` }, ...lines.map((line) => h('div', {}, line))) : frag());
  }

  async function runPreflight() {
    if (!selectedFileId) return;
    mount(profileNode, card({}, h('p', { class: 'muted small' }, 'Profiling the workload in the isolated worker…')));
    try {
      const response = await api('/api/nami/preflight', {
        method: 'POST',
        body: { file_id: selectedFileId, operations: selectedOperations() },
      });
      const jobId = response.job.id;
      for (let attempt = 0; attempt < 900; attempt += 1) {
        const payload = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
        preflightJob = payload.job;
        state.namiPreflight = payload.job;
        if (preflightJob.status === 'succeeded' || preflightJob.status === 'failed') break;
        await sleep(700);
      }
    } catch (error) {
      preflightJob = { status: 'failed', error: error.message };
    }
    mount(profileNode, profilePanel(fileDetail || { original_name: 'selected file' }, preflightJob));
  }

  async function openBenchmark(id) {
    mount(resultNode, card({}, h('p', { class: 'muted small' }, 'Loading the stored benchmark…')));
    const payload = await api(`/api/nami/benchmarks/${encodeURIComponent(id)}`);
    mount(resultNode, resultsPanel(payload.benchmark));
  }

  async function refreshHistory() {
    const payload = await api('/api/nami/benchmarks');
    state.namiHistory = payload.benchmarks;
    mount(historyNode, historyPanel(payload.benchmarks, actions));
  }

  const runButton = h('button', { class: 'btn btn-primary', onclick: () => runComparison() }, 'Run comparison');
  const preflightButton = h('button', { class: 'btn', onclick: () => runPreflight() }, 'Profiling & plan');

  async function runComparison() {
    if (busy) return;
    if (!selectedFileId) {
      renderStatus(['Select a benchmarkable dataset first: NAMI benchmarks CSV, TSV and Excel workloads.'], 'warn');
      return;
    }
    const trials = Math.max(1, Math.min(Number(trialsInput.value) || 1, (policies.defaults || {}).max_trials || 7));
    state.namiTrials = trials;
    state.namiWarmup = warmupInput.checked;
    state.namiForcePolicy = forceSelect.value || null;
    busy = true;
    runButton.disabled = true;
    mount(resultNode, card({}, h('p', { class: 'muted small' }, 'Submitting the comparison…')));
    const startedAt = Date.now();
    try {
      const response = await api('/api/nami/benchmarks', {
        method: 'POST',
        body: {
          file_id: selectedFileId,
          operations: selectedOperations(),
          trials,
          warmup: warmupInput.checked,
          force_policy: forceSelect.value || null,
        },
      });
      const benchmarkId = response.benchmark.id;
      state.currentBenchmarkId = benchmarkId;
      let payload = response.benchmark;
      while (payload.status === 'queued' || payload.status === 'running') {
        renderStatus([
          h('strong', {}, 'Comparison running'),
          `Status ${payload.status} · ${Math.round((Date.now() - startedAt) / 1000)}s elapsed · live polling every ${POLL_MS / 1000}s.`,
          `Trials requested: ${payload.trials_requested}. The worker runs SPADE and NAMI on the same plan with alternating order, then verifies the outputs row by row.`,
        ]);
        await sleep(POLL_MS);
        payload = (await api(`/api/nami/benchmarks/${encodeURIComponent(benchmarkId)}`)).benchmark;
      }
      renderStatus(
        payload.status === 'succeeded'
          ? [
            h('strong', {}, `Comparison finished in ${Math.round((Date.now() - startedAt) / 1000)}s`),
            'Measured in this deployment, not simulated. The reduction table, correctness checks and downloads are below.',
          ]
          : [h('strong', {}, 'The comparison failed'), payload.error || 'Unknown error.'],
        payload.status === 'succeeded' ? 'ok' : 'err'
      );
      mount(resultNode, resultsPanel(payload));
      await refreshHistory();
    } catch (error) {
      renderStatus([h('strong', {}, 'The comparison could not be started'), error.message], 'err');
      mount(resultNode, resultsPanel(null));
    } finally {
      busy = false;
      runButton.disabled = false;
    }
  }

  const configCard = card({ title: 'Benchmark configuration', subtitle: 'Same input, same plan, two systems' },
    h('div', { class: 'grid cols-2' },
      h('label', { class: 'field' }, h('span', {}, 'Dataset'), fileSelect),
      h('label', { class: 'field' }, h('span', {}, 'Execution policy'), forceSelect)
    ),
    h('div', { class: 'grid cols-2' },
      h('label', { class: 'field' }, h('span', {}, 'Measured trials (1–7)'), trialsInput),
      h('label', { class: 'field check-inline' },
        h('input', { type: 'checkbox', checked: warmupInput.checked }),
        h('span', {}, 'Discard a warm-up pair first (recommended: removes first-run cache and import effects)'))
    ),
    operationsNode,
    h('div', { class: 'row-actions', style: 'margin-top:.6rem' }, preflightButton, runButton),
    h('p', { class: 'hint' },
      'Profiling is read-only: it measures the workload and selects a policy without transforming anything. '
      + 'A comparison then runs both systems in the isolated worker, on the same resolved plan, and stores the artifacts for download.')
  );

  mount(root,
    comparisonHeader(policies),
    h('div', { class: 'grid side', style: 'margin-top:.9rem' }, configCard, profileNode),
    statusNode,
    resultNode,
    historyNode,
    methodologyPanel(policies)
  );

  actions.viewBenchmark = openBenchmark;
  actions.removeBenchmark = async (id, fileName) => {
    if (!confirmAction(`Delete this benchmark of ${fileName}? Its outputs and report are removed from storage.`)) return;
    await api(`/api/nami/benchmarks/${encodeURIComponent(id)}`, { method: 'DELETE' });
    toast('Benchmark deleted.', 'ok');
    await refreshHistory();
  };

  fileSelect.addEventListener('change', async () => {
    selectedFileId = fileSelect.value || null;
    state.namiFileId = selectedFileId;
    fileDetail = await loadFileDetail(selectedFileId);
    renderOperations();
    preflightJob = null;
    state.namiPreflight = null;
    mount(profileNode, profilePanel(fileDetail, null));
    await runPreflight();
  });

  fileDetail = await loadFileDetail(selectedFileId);
  renderOperations();
  mount(profileNode, profilePanel(fileDetail, preflightJob));
  mount(resultNode, resultsPanel(null));
  mount(historyNode, historyPanel(state.namiHistory, actions));

  if (state.currentBenchmarkId) await openBenchmark(state.currentBenchmarkId);
  if (fileDetail) await runPreflight();
}
