// The six views. Each function mounts into a section element and receives the
// shared state plus an actions object, so nothing reaches into globals.
//
// The NAMI Optimizer lab lives in its own module (lab.js) because it is a
// distinct surface with its own polling, and it re-uses the primitives exported
// below rather than duplicating them.

import { api, artifactUrl, benchmarkArtifactUrl, originalUrl, uploadFile, poll } from './api.js';
import { pairBars, ring, miniBars } from './charts.js';
import { h, frag, fmt, mount, statusBadge, severityBadge, toast, confirmAction, option, multiSelectOptions, readableList } from './util.js';

const FORMAT_EXTENSIONS = '.pdf,.xlsx,.xlsm,.xls,.csv,.tsv,.docx,.json,.txt,.log,.md';

// ---------------------------------------------------------------- primitives

export function statCard({ label, value, sub, kind = '', title = '' }) {
  return h('div', { class: 'card stat', title },
    h('span', { class: 'stat-label' }, label),
    h('span', { class: `stat-value ${kind}` }, value),
    sub ? h('span', { class: 'stat-sub' }, sub) : null
  );
}

export function card({ title, subtitle, actions, className = '' }, ...body) {
  return h('div', { class: `card ${className}` },
    (title || subtitle || actions) ? h('div', { class: 'card-head' },
      h('div', {}, title ? h('h3', {}, title) : null, subtitle ? h('div', { class: 'card-sub' }, subtitle) : null),
      actions || null
    ) : null,
    ...body
  );
}

export function kvList(entries) {
  return h('dl', { class: 'kv' }, ...entries.flatMap(([key, value]) =>
    [h('dt', {}, key), h('dd', {}, value ?? 'n/a')]));
}

function findingsList(findings) {
  if (!findings || !findings.length) return h('p', { class: 'muted small' }, 'No findings were recorded.');
  return h('div', {}, ...findings.map((finding) =>
    h('div', { class: `finding ${finding.severity || 'info'}` },
      h('span', { class: 'dot' }),
      h('div', {},
        h('strong', {}, finding.title || ''),
        finding.detail ? h('p', {}, finding.detail) : null
      )
    )
  ));
}

export function verificationChecks(checks) {
  if (!checks || !checks.length) return h('p', { class: 'muted small' }, 'No checks were recorded.');
  return h('div', {}, ...checks.map((check) =>
    h('div', { class: `check ${check.status}` },
      h('span', { class: 'mark' }, check.status === 'pass' ? '✓' : check.status === 'fail' ? '✕' : '!'),
      h('div', { class: 'body' },
        h('div', { class: 'id' }, check.id),
        h('p', {}, check.detail || '')
      )
    )
  ));
}

function artifactButton(jobId, artifact) {
  const url = artifactUrl(jobId, artifact.id);
  return h('a', { class: 'btn btn-sm', href: url, download: artifact.filename },
    artifact.kind === 'output' ? '↓ Download output'
      : artifact.kind === 'baseline' ? '↓ Baseline output'
        : `↓ ${artifact.label || artifact.filename}`);
}

function verificationBadge(status) {
  const map = { passed: 'ok', failed: 'err', not_applicable: 'neutral', 'not run': 'neutral' };
  return h('span', { class: `badge ${map[status] || 'neutral'}` }, status || 'not run');
}

// ---------------------------------------------------------------- dashboard

export async function renderDashboard(root, state, actions) {
  const overview = await api('/api/overview');
  state.overview = overview;

  const statuses = overview.jobs || {};
  const total = Object.values(statuses).reduce((sum, value) => sum + value, 0);

  mount(root,
    h('div', { class: 'grid cols-4' },
      statCard({ label: 'Files stored', value: fmt.int(overview.files.total), sub: fmt.bytes(overview.files.bytes) + ' on disk' }),
      statCard({ label: 'Optimization jobs', value: fmt.int(total), sub: `${fmt.int(statuses.succeeded || 0)} succeeded, ${fmt.int(statuses.failed || 0)} failed` }),
      statCard({
        label: 'Recent space reclaimed',
        value: fmt.bytes(overview.saved_bytes_recent),
        sub: 'sum of the last 8 jobs',
        kind: overview.saved_bytes_recent > 0 ? 'good' : '',
      }),
      statCard({
        label: 'Average size reduction',
        value: overview.average_reduction_pct_recent === null ? 'n/a' : `${overview.average_reduction_pct_recent}%`,
        sub: 'recent jobs with a measured gain',
        kind: (overview.average_reduction_pct_recent || 0) > 0 ? 'good' : '',
      })
    ),

    h('div', { class: 'grid side', style: 'margin-top:.9rem' },
      card({ title: 'Recent processing runs', subtitle: 'Measured results from this account' },
        overview.recent_jobs.length
          ? h('div', { class: 'scroll' }, h('table', { class: 'data' },
            h('thead', {}, h('tr', {},
              h('th', {}, 'File'), h('th', {}, 'Format'), h('th', {}, 'Status'),
              h('th', { class: 'num' }, 'Size change'), h('th', { class: 'num' }, 'Time change'),
              h('th', {}, 'Verification'), h('th', {}, '')
            )),
            h('tbody', {}, ...overview.recent_jobs.map((job) => h('tr', {},
              h('td', {}, h('a', { href: `#/results/${job.id}` }, job.file_name)),
              h('td', {}, h('span', { class: 'badge info' }, job.file_format || '?')),
              h('td', {}, statusBadge(job.status)),
              h('td', { class: 'num' }, job.result && job.result.size ? fmt.pct(job.result.size.reduction_pct) : 'n/a'),
              h('td', { class: 'num' }, job.result && job.result.time ? fmt.pct(job.result.time.reduction_pct) : 'n/a'),
              h('td', {}, verificationBadge(job.result ? job.result.verification : null)),
              h('td', {}, h('a', { class: 'btn btn-sm btn-ghost', href: `#/results/${job.id}` }, 'Open'))
            )))
          ))
          : h('div', { class: 'empty' }, h('p', {}, 'No optimization runs yet.'),
            h('p', { class: 'small' }, 'Upload a file to analyse it, then run an optimization.'))
      ),

      h('div', { class: 'stack' },
        card({ title: 'Supported formats', subtitle: 'Detected from content, not the extension' },
          h('div', { class: 'chips' }, ...(overview.supported_formats || []).map((format) =>
            h('span', { class: 'chip' }, format.toUpperCase())))
        ),
        card({ title: 'Execution engines', subtitle: 'What actually runs' },
          kvList([
            ['Active', (state.backends && state.backends.implemented || []).join(', ')],
            ['Declared only', (state.backends && state.backends.declared || []).join(', ')],
          ]),
          h('p', { class: 'hint' }, 'Spark and Flink adapters are interface declarations. No job in this build is attributed to them.')
        ),
        card({ title: 'Account activity', subtitle: 'Recent actions, no document contents' },
          h('div', {}, ...overview.activity.map((entry) =>
            h('div', { class: 'row between tiny', style: 'padding:.2rem 0;border-bottom:1px solid var(--line-soft)' },
              h('span', { class: 'mono-sm' }, entry.action),
              h('span', { class: 'muted' }, fmt.rel(entry.created_at))
            )))
        )
      )
    )
  );
}

// ------------------------------------------------------------------- upload

export async function renderUpload(root, state, actions) {
  const settings = state.settings || {};
  const limit = settings.max_upload_bytes || 0;

  const progressBar = h('div', { class: 'progress', hidden: true }, h('span'));
  const progressLabel = h('p', { class: 'small muted', hidden: true });
  const fileInput = h('input', { type: 'file', hidden: true, accept: FORMAT_EXTENSIONS, multiple: true });

  const zone = h('div', { class: 'dropzone', id: 'dropzone' },
    h('div', { class: 'big' }, '⇪'),
    h('h3', {}, 'Drop a file here, or click to browse'),
    h('p', { class: 'muted small' }, `PDF, XLSX, XLS, CSV, DOCX, JSON and TXT. Up to ${fmt.bytes(limit)} per file.`),
    h('p', { class: 'hint' }, 'The file is parsed by an isolated worker process. Nothing is executed and nothing leaves this server.')
  );

  const fileList = h('div', { id: 'file-list' });

  const startUpload = async (files) => {
    if (!files || !files.length) return;
    progressBar.hidden = false;
    progressLabel.hidden = false;
    for (const file of files) {
      if (file.size > limit) {
        toast(`${file.name} is ${fmt.bytes(file.size)}, above your ${fmt.bytes(limit)} limit.`, 'err');
        continue;
      }
      progressLabel.textContent = `Uploading ${file.name} (${fmt.bytes(file.size)})…`;
      progressBar.firstChild.style.width = '0%';
      try {
        const payload = await uploadFile(file, {
          onProgress: (fraction) => { progressBar.firstChild.style.width = `${Math.round(fraction * 100)}%`; },
        });
        toast(`${file.name} uploaded as ${payload.format.toUpperCase()}. Analysing…`, 'ok');
        progressLabel.textContent = `Analysing ${file.name}…`;
        progressBar.classList.add('is-indeterminate');
        await poll(async () => {
          const detail = await api(`/api/files/${payload.id}`);
          if (detail.analysis_ready) return { terminal: true, detail };
          if (detail.status === 'analysis_failed') return { terminal: true, failed: true, detail };
          return { terminal: false };
        }, { intervalMs: 500, timeoutMs: 180000 }).then((outcome) => {
          if (outcome.failed) toast(`Analysis failed: ${outcome.detail.error}`, 'err');
          else toast(`${file.name} analysed.`, 'ok');
          actions.setCurrentFile(payload.id);
          actions.navigate('#/analysis/' + payload.id);
        });
      } catch (error) {
        toast(error.message, 'err');
      } finally {
        progressBar.classList.remove('is-indeterminate');
        progressBar.firstChild.style.width = '0%';
      }
    }
    progressBar.hidden = true;
    progressLabel.hidden = true;
    actions.refreshFiles();
  };

  zone.addEventListener('click', () => fileInput.click());
  zone.addEventListener('dragover', (event) => { event.preventDefault(); zone.classList.add('is-over'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('is-over'));
  zone.addEventListener('drop', (event) => {
    event.preventDefault();
    zone.classList.remove('is-over');
    if (event.dataTransfer && event.dataTransfer.files) startUpload([...event.dataTransfer.files]);
  });
  fileInput.addEventListener('change', () => { startUpload([...fileInput.files]); fileInput.value = ''; });

  mount(root,
    h('div', { class: 'grid side' },
      h('div', { class: 'stack' },
        zone,
        fileInput,
        h('div', {}, progressBar, progressLabel)
      ),
      h('div', { class: 'stack' },
        card({ title: 'Rules the uploader enforces' },
          h('ul', { class: 'small muted', style: 'margin:.2rem 0 0 1rem;padding:0' },
            h('li', {}, 'The file type comes from the bytes, not the name: a renamed file is reported, not trusted.'),
            h('li', {}, 'Archives are checked for decompression bombs before any part is read.'),
            h('li', {}, 'Content is stored under a random name inside your own directory.'),
            h('li', {}, 'A file that is not a supported document is rejected with the reason why.')
          )
        ),
        card({ title: 'Your limits', subtitle: 'Change these in Settings' },
          kvList([
            ['Max upload', fmt.bytes(settings.max_upload_bytes)],
            ['Processing timeout', `${settings.processing_timeout_seconds || 0} s`],
            ['Chunk size', `${fmt.int(settings.chunk_rows)} rows`],
            ['Analysis row limit', fmt.int(settings.max_analysis_rows)],
          ])
        )
      )
    ),
    h('div', { style: 'margin-top:1rem' }, fileList)
  );

  await renderFileList(fileList, state, actions);
}

async function renderFileList(container, state, actions) {
  const payload = await api('/api/files');
  state.files = payload.files;
  const rows = payload.files.map((file) => h('tr', {},
    h('td', {}, h('a', { href: `#/analysis/${file.id}` }, file.original_name)),
    h('td', {}, h('span', { class: 'badge info' }, (file.detected_format || '?').toUpperCase())),
    h('td', { class: 'num' }, fmt.bytes(file.size_bytes)),
    h('td', {}, statusBadge(file.status)),
    h('td', { class: 'muted tiny' }, file.error ? file.error.slice(0, 60) : fmt.rel(file.created_at)),
    h('td', {},
      h('div', { class: 'row' },
        h('a', { class: 'btn btn-sm', href: `#/analysis/${file.id}` }, 'Analyse'),
        h('a', { class: 'btn btn-sm btn-ghost', href: originalUrl(file.id), download: file.original_name }, 'Original'),
        h('button', {
          class: 'btn btn-sm btn-danger',
          onclick: async () => {
            if (!confirmAction(`Delete ${file.original_name} and all of its results? This cannot be undone.`)) return;
            try {
              await api(`/api/files/${file.id}`, { method: 'DELETE' });
              toast('File and results deleted.', 'ok');
              await renderFileList(container, state, actions);
              actions.refreshFiles();
            } catch (error) { toast(error.message, 'err'); }
          },
        }, 'Delete')
      )
    )
  ));

  mount(container,
    card({
      title: 'Uploaded files',
      subtitle: `${fmt.int(payload.total)} file(s), ${fmt.bytes(payload.total_bytes)} stored`,
    },
      payload.files.length
        ? h('div', { class: 'scroll' }, h('table', { class: 'data' },
          h('thead', {}, h('tr', {},
            h('th', {}, 'Name'), h('th', {}, 'Detected'), h('th', { class: 'num' }, 'Size'),
            h('th', {}, 'Status'), h('th', {}, 'Added'), h('th', {}, 'Actions'))),
          h('tbody', {}, ...rows)))
        : h('div', { class: 'empty' }, 'No files uploaded yet.')
    )
  );
}

// ----------------------------------------------------------------- analysis

async function loadAnalysis(fileId) {
  const detail = await api(`/api/files/${fileId}`);
  let operations = [];
  let recommended = [];
  if (detail.analysis && detail.analysis.operations) {
    operations = detail.analysis.operations.available || [];
    recommended = detail.analysis.operations.recommended_ids || [];
  } else {
    // Analysis job results keep the operation catalogue in the job payload.
    const jobs = await api('/api/jobs?kind=analyze&limit=50');
    const match = jobs.jobs.find((job) => job.file_id === fileId);
    if (match) {
      const job = await api(`/api/jobs/${match.id}`);
      const result = job.job.result || {};
      operations = (result.operations && result.operations.available) || [];
      recommended = (result.operations && result.operations.recommended_ids) || [];
      if (!detail.analysis && result.analysis) detail.analysis = result.analysis;
    }
  }
  return { detail, operations, recommended };
}

export async function renderAnalysis(root, state, actions) {
  const files = (await api('/api/files')).files;
  state.files = files;
  const fileId = state.currentFileId || (files[0] && files[0].id);

  const picker = h('select', {
    onchange: (event) => actions.navigate(`#/analysis/${event.target.value}`),
  }, ...files.map((file) => option(file.id, `${file.original_name} — ${(file.detected_format || '?').toUpperCase()} (${fmt.bytes(file.size_bytes)})`, file.id === fileId)));

  if (!files.length) {
    mount(root, h('div', { class: 'empty' },
      h('p', {}, 'Nothing to analyse yet.'),
      h('p', {}, h('a', { class: 'btn btn-primary', href: '#/upload' }, 'Upload a file'))));
    return;
  }

  const { detail, operations, recommended } = await loadAnalysis(fileId);
  const analysis = detail.analysis || {};

  if (!analysis.summary) {
    mount(root,
      h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h3', {}, 'Select a file'), picker)),
      h('div', { class: 'empty', style: 'margin-top:1rem' },
        detail.status === 'analysis_failed'
          ? h('p', {}, `Analysis failed: ${detail.error}`)
          : h('p', {}, 'Analysis is still running for this file. Reload in a moment.'))
    );
    return;
  }

  const summaryCards = (analysis.summary || []).map((item) =>
    statCard({ label: item.label, value: item.value, sub: item.hint || '', title: item.hint || '' }));

  // Operation selection state lives here so the run button knows what to send.
  const selected = new Set(recommended);
  const optionValues = {};
  const runButton = h('button', { class: 'btn btn-primary' }, '▶ Run optimization');
  const selectionSummary = h('p', { class: 'small muted' }, `${selected.size} of ${operations.length} operations selected`);

  const operationNodes = operations.map((op) => {
    const checkbox = h('input', {
      type: 'checkbox',
      checked: selected.has(op.id) ? 'checked' : null,
      onchange: (event) => {
        if (event.target.checked) selected.add(op.id); else selected.delete(op.id);
        selectionSummary.textContent = `${selected.size} of ${operations.length} operations selected`;
      },
    });
    const categoryBadge = op.category === 'lossless'
      ? h('span', { class: 'badge ok' }, 'lossless')
      : h('span', { class: 'badge warn' }, 'changes data');
    const approvalBadge = op.requires_approval ? h('span', { class: 'badge violet' }, 'needs explicit approval') : null;

    const effectKeys = Object.keys(op.effect || {}).filter((key) => !Array.isArray(op.effect[key]));
    const effectText = effectKeys
      .filter((key) => typeof op.effect[key] !== 'boolean')
      .map((key) => `${key.replace(/_/g, ' ')}: ${fmt.int(op.effect[key])}`)
      .join(' · ');

    const fields = [];
    const schema = op.options_schema || {};
    if (schema.columns) {
      fields.push(h('label', {}, 'Columns to drop',
        h('select', { multiple: true, size: 6, onchange: (event) => { optionValues.columns = [...event.target.selectedOptions].map((o) => o.value); } },
          ...(op.effect.available_columns || []).map((name) => option(name, name, false)))));
    }
    if (schema.key_columns) {
      fields.push(h('label', {}, 'Key columns for deduplication',
        h('select', { multiple: true, size: 6, onchange: (event) => { optionValues.key_columns = [...event.target.selectedOptions].map((o) => o.value); } },
          ...(op.effect.candidate_key_columns || []).map((name) => option(name, name, false)))));
    }
    if (schema.fields) {
      fields.push(h('label', {}, 'Fields to keep',
        h('select', { multiple: true, size: 6, onchange: (event) => { optionValues.fields = [...event.target.selectedOptions].map((o) => o.value); } },
          ...(op.effect.available_fields || []).map((name) => option(name, name, false)))));
    }
    if (schema.image_quality) {
      fields.push(h('label', {}, `JPEG quality (${schema.image_quality.min}–${schema.image_quality.max})`,
        h('input', { type: 'number', value: schema.image_quality.default, min: schema.image_quality.min, max: schema.image_quality.max,
          onchange: (event) => { optionValues.image_quality = Number(event.target.value); } })));
    }
    if (schema.image_max_dimension) {
      fields.push(h('label', {}, 'Maximum image dimension in pixels (0 keeps the original size)',
        h('input', { type: 'number', value: 0, min: 0, max: 10000,
          onchange: (event) => { optionValues.image_max_dimension = Number(event.target.value); } })));
    }
    if (schema.style) {
      fields.push(h('label', {}, 'Output formatting',
        h('select', { onchange: (event) => { optionValues.style = event.target.value; } },
          option('compact', 'Compact (smallest)', true), option('pretty', 'Pretty-printed (indented)', false))));
    }

    return h('div', { class: 'op' },
      checkbox,
      h('div', { class: 'op-body' },
        h('div', { class: 'op-title' }, op.label, categoryBadge, approvalBadge),
        h('p', {}, op.description),
        op.rationale ? h('div', { class: 'op-meta' }, `why: ${op.rationale}`) : null,
        effectText ? h('div', { class: 'op-meta' }, `measured in analysis: ${effectText}`) : null,
        fields.length ? h('div', { class: 'op-fields' }, ...fields) : null
      )
    );
  });

  runButton.addEventListener('click', async () => {
    if (selected.size === 0) {
      toast('Select at least one operation, or SPADE would only rewrite the file.', 'err');
      return;
    }
    runButton.disabled = true;
    runButton.textContent = 'Starting…';
    try {
      const payload = await api('/api/jobs', {
        method: 'POST',
        body: { file_id: fileId, operations: [...selected], options: optionValues },
      });
      toast('Optimization started. Processing runs in an isolated worker.', 'ok');
      actions.navigate(`#/results/${payload.job.id}`);
    } catch (error) {
      toast(error.message, 'err');
      runButton.disabled = false;
      runButton.textContent = '▶ Run optimization';
    }
  });

  const structure = analysis.structure || {};
  const columnRows = (analysis.columns || []).map((column) => h('tr', {},
    h('td', {}, h('span', { class: 'mono-sm' }, column.name)),
    h('td', {}, h('span', { class: 'badge neutral' }, column.inferred_type)),
    h('td', { class: 'num' }, fmt.int(column.non_empty)),
    h('td', { class: 'num' }, fmt.int(column.missing)),
    h('td', { class: 'num' }, column.missing_pct === undefined ? 'n/a' : `${column.missing_pct}%`),
    h('td', { class: 'num' }, column.distinct === null || column.distinct === undefined ? 'n/a' : fmt.int(column.distinct)),
    h('td', {}, (column.flags || []).join(', ') || '—')
  ));

  const containerInfo = structure.container || {};
  const sampleRows = structure.sample_values || [];

  mount(root,
    h('div', { class: 'card' }, h('div', { class: 'card-head' },
      h('div', {}, h('h3', {}, 'Select a file'), h('div', { class: 'card-sub' }, `${(analysis.label) || detail.format} · ${fmt.bytes(detail.size_bytes)} · detected by content`)),
      picker)),

    h('div', { class: 'grid cols-4', style: 'margin-top:.9rem' }, ...summaryCards),

    h('div', { class: 'grid side', style: 'margin-top:.9rem' },
      card({ title: 'Analysis findings', subtitle: 'What the profiler found before any change' }, findingsList(analysis.findings)),
      h('div', { class: 'stack' },
        card({ title: 'Detected format' },
          kvList([
            ['Detected', (analysis.format || detail.format || '').toUpperCase()],
            ['Engine', analysis.engine || '—'],
            ['Stated type', detail.declared_ext ? detail.declared_ext.toUpperCase() : 'none'],
            ['Content type', detail.content_type],
            ['SHA-256', (detail.sha256 || '').slice(0, 24) + '…'],
            ['Structure', structure.rows !== undefined ? `${fmt.int(structure.rows)} rows × ${structure.columns} columns` : `${structure.pages ?? '—'} pages`],
          ]),
          (analysis.format === 'pdf' || detail.format === 'pdf')
            ? h('p', { class: 'hint' }, structure.scanned ? 'This PDF has no text layer: OCR would be required for text work.' : 'Text layer detected, so text preservation can be verified after processing.')
            : null
        ),
        (containerInfo.sheets)
          ? card({ title: 'Sheets' }, kvList(Object.entries(containerInfo.sheet_dimensions || {}).map(([name, dims]) => [name + (name === containerInfo.selected_sheet ? ' (selected)' : ''), `${fmt.int(dims[0])} × ${dims[1]}`])))
          : null,
        (analysis.notes && analysis.notes.length) ? card({ title: 'Method notes' },
          h('ul', { class: 'small muted', style: 'margin:.2rem 0 0 1rem;padding:0' }, ...analysis.notes.map((note) => h('li', {}, note)))) : null
      )
    ),

    h('div', { style: 'margin-top:.9rem' },
      card({ title: 'Choose what to apply', subtitle: 'Review the plan before anything is transformed' },
        h('p', { class: 'small muted' }, 'Operations marked “changes data” are never applied automatically. The original file is always kept, and every run is verified afterwards.'),
        selectionSummary,
        h('div', { style: 'margin-top:.6rem' }, ...operationNodes),
        h('div', { class: 'row', style: 'margin-top:.6rem' }, runButton)
      )
    ),

    columnTable(analysis, columnRows),
    sampleTable(structure, sampleRows)
  );
}

function columnTable(analysis, columnRows) {
  if (!columnRows.length) return null;
  const head = h('tr', {},
    h('th', {}, 'Column'),
    h('th', {}, 'Type'),
    h('th', { class: 'num' }, 'Values'),
    h('th', { class: 'num' }, 'Missing'),
    h('th', { class: 'num' }, 'Missing %'),
    h('th', { class: 'num' }, 'Distinct'),
    h('th', {}, 'Flags'));
  const table = h('table', { class: 'data' },
    h('thead', {}, head),
    h('tbody', {}, ...columnRows));
  return h('div', { style: 'margin-top:.9rem' },
    card({ title: 'Column profile', subtitle: `${analysis.columns.length} column(s) after the header row` },
      h('div', { class: 'scroll' }, table)));
}

function sampleTable(structure, sampleRows) {
  if (!sampleRows.length) return null;
  const head = h('tr', {}, ...(structure.header || []).map((name) => h('th', {}, name)));
  const body = h('tbody', {}, ...sampleRows.map((row) => {
    const cells = row.map((cell) => h('td', { class: 'mono-sm' }, cell));
    return h('tr', {}, ...cells);
  }));
  const table = h('table', { class: 'data' }, h('thead', {}, head), body);
  return h('div', { style: 'margin-top:.9rem' },
    card({ title: 'Sample rows', subtitle: 'The first rows exactly as parsed' },
      h('div', { class: 'scroll' }, table)));
}

// ------------------------------------------------------------------ results

export async function renderResults(root, state, actions) {
  const jobs = (await api('/api/jobs?kind=optimize&limit=50')).jobs;
  const jobId = state.currentJobId || (jobs[0] && jobs[0].id);

  const picker = h('select', { onchange: (event) => actions.navigate(`#/results/${event.target.value}`) },
    ...jobs.map((job) => option(job.id, `${job.file_name} — ${job.status} (${fmt.rel(job.created_at)})`, job.id === jobId)));

  if (!jobs.length) {
    mount(root, h('div', { class: 'empty' },
      h('p', {}, 'No optimization runs yet.'),
      h('p', {}, h('a', { class: 'btn btn-primary', href: '#/upload' }, 'Upload a file'),
        ' ', h('a', { class: 'btn btn-ghost', href: '#/analysis' }, 'Open analysis'))));
    return;
  }

  const job = (await api(`/api/jobs/${jobId}`)).job;

  const header = h('div', { class: 'card' }, h('div', { class: 'card-head' },
    h('div', {}, h('h3', {}, job.file_name),
      h('div', { class: 'card-sub' }, `${(job.file_format || '?').toUpperCase()} · ${job.kind} · created ${fmt.date(job.created_at)}`)),
    h('div', { class: 'row' }, statusBadge(job.status), picker)));

  if (job.status === 'queued' || job.status === 'running') {
    mount(root, header, h('div', { class: 'card', style: 'margin-top:.9rem' },
      h('h3', {}, 'Processing…'),
      h('p', { class: 'muted small' }, 'The analysis, both execution paths and the verification run in an isolated worker process. This page updates automatically.'),
      h('div', { class: 'progress is-indeterminate' }, h('span'))));
    actions.watchJob(job.id);
    return;
  }

  if (job.status === 'failed') {
    const result = job.result || {};
    mount(root, header, h('div', { class: 'alert err', style: 'margin-top:.9rem' },
      h('strong', {}, 'Processing failed: '), job.error || 'unknown error'),
      card({ title: 'Diagnostics', subtitle: `Error kind: ${result.error_kind || 'unknown'}` },
        result.stage_timings
          ? kvList(Object.entries(result.stage_timings).map(([key, value]) => [key, `${value} ms`]))
          : h('p', { class: 'muted small' }, 'No stage data was produced.'),
        h('p', { class: 'hint' }, 'The uploaded file was left untouched and is still available from Upload.'))
    );
    return;
  }

  const result = job.result || {};
  const comparison = result.comparison || {};
  const size = comparison.file_size || {};
  const time = comparison.execution_time || {};
  const cpu = comparison.cpu_time || {};
  const memory = comparison.memory || {};
  const throughput = comparison.throughput || {};
  const records = comparison.records || {};
  const quality = comparison.quality || {};
  const outcome = result.outcome || {};
  const optimized = result.optimized || {};
  const baseline = result.baseline || {};

  const outcomeClass = outcome.kind === 'optimized' ? 'ok' : outcome.kind === 'no_improvement' ? 'warn' : 'err';
  const regressions = (comparison.regressions || []).filter((entry) => entry.kind === 'regression');
  const unavailable = (comparison.regressions || []).filter((entry) => entry.kind === 'unavailable');

  mount(root,
    header,

    h('div', { class: 'alert ' + (outcomeClass === 'ok' ? 'info' : outcomeClass), style: 'margin-top:.9rem' },
      h('strong', {}, outcome.headline || ''), ' ', outcome.explanation || ''),

    h('div', { class: 'grid cols-4', style: 'margin-top:.9rem' },
      statCard({
        label: 'File size reduction',
        value: size.reduction_pct === null ? 'n/a' : `${size.reduction_pct}%`,
        sub: `${fmt.bytes(size.original_bytes)} → ${fmt.bytes(size.output_bytes)}`,
        kind: (size.reduction_pct || 0) > 0 ? 'good' : (size.reduction_pct || 0) < 0 ? 'bad' : '',
        title: '((original − output) ÷ original) × 100',
      }),
      statCard({
        label: 'Execution time reduction',
        value: time.reduction_pct === null ? 'n/a' : `${time.reduction_pct}%`,
        sub: `${fmt.seconds(time.baseline_seconds)} → ${fmt.seconds(time.optimized_seconds)}`,
        kind: (time.reduction_pct || 0) > 0 ? 'good' : (time.reduction_pct || 0) < 0 ? 'bad' : '',
        title: '((baseline − optimized) ÷ baseline) × 100',
      }),
      statCard({
        label: 'Throughput change',
        value: throughput.improvement_pct === null || throughput.improvement_pct === undefined ? 'n/a' : fmt.pct(throughput.improvement_pct),
        sub: throughput.baseline_per_second ? `${fmt.int(throughput.baseline_per_second)}/s → ${fmt.int(throughput.optimized_per_second)}/s` : 'not meaningful here',
        kind: (throughput.improvement_pct || 0) > 0 ? 'good' : (throughput.improvement_pct || 0) < 0 ? 'bad' : '',
        title: 'Throughput is higher-is-better, so it is computed separately from the reduction formula.',
      }),
      statCard({
        label: 'Records processed',
        value: fmt.int(records.input),
        sub: `${fmt.int(records.optimized_output)} written · ${fmt.int(records.input - records.optimized_output)} removed`,
      })
    ),

    h('div', { class: 'grid cols-4', style: 'margin-top:.9rem' },
      card({ title: 'Size reduction', className: 'stat' }, ring({ value: size.reduction_pct, label: 'smaller', good: (size.reduction_pct || 0) >= 0 })),
      card({ title: 'Time reduction', className: 'stat' }, ring({ value: time.reduction_pct, label: 'faster', good: (time.reduction_pct || 0) >= 0 })),
      card({ title: 'Peak heap reduction', className: 'stat' }, ring({ value: memory.reduction_pct, label: 'less memory', good: (memory.reduction_pct || 0) >= 0 })),
      card({ title: 'Verification', className: 'stat' },
        h('div', { class: 'center' }, verificationBadge(quality.verification_status),
          h('p', { class: 'small muted', style: 'margin-top:.6rem' }, quality.verification_summary || '')),
        h('p', { class: 'hint center' }, quality.exact_comparison ? 'Exact record-level comparison' : 'Bounded record-level comparison'))
    ),

    h('div', { class: 'grid cols-2', style: 'margin-top:.9rem' },
      card({ title: 'File size', subtitle: 'Original vs optimized output' },
        pairBars({ title: 'bytes', before: size.original_bytes, after: size.output_bytes, format: fmt.bytes, reduction: size.reduction_pct })),
      card({ title: 'Execution time', subtitle: 'Baseline path vs optimized path, same output contract' },
        pairBars({
          title: 'wall clock', before: time.baseline_seconds, after: time.optimized_seconds,
          format: fmt.seconds, reduction: time.reduction_pct,
          note: time.reduction_pct === null || time.reduction_pct === undefined
            ? `Both paths finished inside the ${((comparison.resolution_seconds || 0.05) * 1000).toFixed(0)} ms measurement resolution, so no percentage is claimed.`
            : '',
        }))
    ),

    h('div', { style: 'margin-top:.9rem' },
      card({ title: 'Full comparison', subtitle: comparison.formula || '' },
        h('div', { class: 'scroll' }, h('table', { class: 'data' },
          h('thead', {}, h('tr', {}, h('th', {}, 'Metric'), h('th', { class: 'num' }, 'Baseline / original'), h('th', { class: 'num' }, 'Optimized'), h('th', { class: 'num' }, 'Change'))),
          h('tbody', {},
            h('tr', {}, h('td', {}, 'File size'), h('td', { class: 'num' }, fmt.bytes(size.original_bytes)), h('td', { class: 'num' }, fmt.bytes(size.output_bytes)), h('td', { class: 'num' }, fmt.pct(size.reduction_pct))),
            h('tr', {}, h('td', {}, 'Wall-clock execution time'), h('td', { class: 'num' }, fmt.seconds(time.baseline_seconds)), h('td', { class: 'num' }, fmt.seconds(time.optimized_seconds)), h('td', { class: 'num' }, fmt.pct(time.reduction_pct))),
            h('tr', {}, h('td', {}, 'CPU time'), h('td', { class: 'num' }, fmt.seconds(cpu.baseline_seconds)), h('td', { class: 'num' }, fmt.seconds(cpu.optimized_seconds)), h('td', { class: 'num' }, fmt.pct(cpu.reduction_pct))),
            h('tr', {}, h('td', {}, 'Peak Python heap'), h('td', { class: 'num' }, fmt.bytes(memory.baseline_peak_python_heap_bytes)), h('td', { class: 'num' }, fmt.bytes(memory.optimized_peak_python_heap_bytes)), h('td', { class: 'num' }, fmt.pct(memory.reduction_pct))),
            h('tr', {}, h('td', {}, 'Peak process RSS'), h('td', { class: 'num' }, memory.baseline_peak_rss_bytes ? fmt.bytes(memory.baseline_peak_rss_bytes) : 'not available'), h('td', { class: 'num' }, memory.optimized_peak_rss_bytes ? fmt.bytes(memory.optimized_peak_rss_bytes) : 'not available'), h('td', { class: 'num' }, 'n/a')),
            h('tr', {}, h('td', {}, 'Records per second'), h('td', { class: 'num' }, throughput.baseline_per_second ? fmt.int(throughput.baseline_per_second) : 'n/a'), h('td', { class: 'num' }, throughput.optimized_per_second ? fmt.int(throughput.optimized_per_second) : 'n/a'), h('td', { class: 'num' }, throughput.improvement_pct === undefined || throughput.improvement_pct === null ? 'n/a' : fmt.pct(throughput.improvement_pct))),
            h('tr', {}, h('td', {}, `Records (${quality.verification_status})`), h('td', { class: 'num' }, fmt.int(records.input)), h('td', { class: 'num' }, fmt.int(records.optimized_output)), h('td', { class: 'num' }, `${fmt.int(records.input - records.optimized_output)} removed`))
          )
        )),
        h('p', { class: 'hint' }, throughput.note || '')
      )
    ),

    h('div', { class: 'grid side', style: 'margin-top:.9rem' },
      card({ title: 'Verification checks', subtitle: quality.verification_summary || '' },
        verificationChecks(result.verification && result.verification.checks)),
      h('div', { class: 'stack' },
        card({ title: 'Data-quality findings' }, findingsList(quality.findings)),
        card({ title: 'Regressions and trade-offs' },
          regressions.length
            ? h('div', {}, ...regressions.map((entry) => h('div', { class: 'finding warn' },
              h('span', { class: 'dot' }),
              h('div', {}, h('strong', {}, `${entry.label || entry.metric}: ${fmt.pct(entry.pct)} (worse)`)))))
            : h('p', { class: 'small muted' }, 'No metric got worse in this run.'),
          unavailable.length
            ? h('div', {}, ...unavailable.map((entry) => h('div', { class: 'finding info' },
              h('span', { class: 'dot' }), h('div', {}, h('strong', {}, `Not measured: ${entry.metric}`), h('p', {}, entry.detail || '')))))
            : null,
          (comparison.trade_offs || []).length
            ? h('ul', { class: 'small muted', style: 'margin:.4rem 0 0 1rem;padding:0' }, ...comparison.trade_offs.map((entry) => h('li', {}, entry)))
            : null
        ),
        (result.warnings || []).length ? card({ title: 'Warnings' },
          h('ul', { class: 'small muted', style: 'margin:.2rem 0 0 1rem;padding:0' }, ...result.warnings.map((warning) => h('li', {}, warning)))) : null
      )
    ),

    h('div', { class: 'grid side', style: 'margin-top:.9rem' },
      card({ title: 'Operations applied', subtitle: `${(result.operations && result.operations.selected || []).length} operation(s)` },
        h('div', {}, ...((result.operations && result.operations.selected) || []).map((op) =>
          h('div', { class: 'op' },
            h('div', { class: 'op-body' },
              h('div', { class: 'op-title' }, op.label,
                op.category === 'lossless' ? h('span', { class: 'badge ok' }, 'lossless') : h('span', { class: 'badge warn' }, 'changes data')),
              h('p', {}, op.description))))),
        h('h4', { style: 'margin-top:.8rem' }, 'Measured effects'),
        (() => {
          const keys = new Set([...Object.keys(baseline.operation_stats || {}), ...Object.keys(optimized.operation_stats || {})]);
          if (!keys.size) return h('p', { class: 'small muted' }, 'No operation-level counters were produced.');
          return h('table', { class: 'data' },
            h('thead', {}, h('tr', {}, h('th', {}, 'Effect'), h('th', { class: 'num' }, 'Baseline run'), h('th', { class: 'num' }, 'Optimized run'))),
            h('tbody', {}, ...[...keys].map((key) => h('tr', {},
              h('td', {}, key.replace(/_/g, ' ')),
              h('td', { class: 'num' }, fmt.int((baseline.operation_stats || {})[key] || 0)),
              h('td', { class: 'num' }, fmt.int((optimized.operation_stats || {})[key] || 0))))));
        })()
      ),
      card({ title: 'Downloads', subtitle: 'The original upload is never modified' },
        h('div', { class: 'stack' }, ...job.artifacts.map((artifact) =>
          h('div', { class: 'row between' },
            h('div', {}, h('div', {}, artifact.label), h('div', { class: 'mono-sm' }, `${artifact.filename} · ${fmt.bytes(artifact.size_bytes)}`)),
            artifactButton(job.id, artifact)))),
        h('h4', { style: 'margin-top:.8rem' }, 'Engine detail'),
        kvList([
          ['Baseline engine', baseline.engine || baseline.backend || '—'],
          ['Optimized engine', optimized.engine || optimized.backend || '—'],
          ['Chunk size', optimized.chunk_rows ? `${fmt.int(optimized.chunk_rows)} rows` : 'n/a'],
          ['Total pipeline time', fmt.seconds(result.total_seconds)],
        ])
      )
    ),

    h('details', { style: 'margin-top:.9rem' },
      h('summary', { class: 'muted small', style: 'cursor:pointer' }, 'Stage timings, environment and measurement scope'),
      h('div', { class: 'grid cols-2', style: 'margin-top:.6rem' },
        card({ title: 'Stage timings' },
          kvList(Object.entries(result.stage_timings || {}).map(([key, value]) => [key, `${value} ms`]))),
        card({ title: 'Measurement scope' },
          kvList(Object.entries(result.measurement_scope || {}).map(([key, value]) => [key, String(value)])),
          h('p', { class: 'hint' }, Object.entries(result.environment || {}).map(([key, value]) => `${key}: ${typeof value === 'object' ? JSON.stringify(value) : value}`).join(' · ')))
      )
    )
  );
}

// ------------------------------------------------------------------ history

export async function renderHistory(root, state, actions) {
  const payload = await api('/api/jobs?kind=optimize&limit=100');
  const jobs = payload.jobs;
  state.jobs = jobs;

  const rows = [];
  for (const job of jobs) {
    let detail = null;
    if (job.status === 'succeeded') {
      const full = await api(`/api/jobs/${job.id}`);
      detail = (full.job.result || {}).comparison || null;
    }

    const viewLink = h('a', { class: 'btn btn-sm btn-ghost', href: `#/results/${job.id}` }, 'View');
    const deleteButton = h('button', {
      class: 'btn btn-sm btn-danger',
      onclick: async () => {
        if (!confirmAction('Delete this run and its generated files?')) return;
        try {
          await api(`/api/jobs/${job.id}`, { method: 'DELETE' });
          toast('Run deleted.', 'ok');
          actions.navigate('#/history');
          await renderHistory(root, state, actions);
        } catch (error) { toast(error.message, 'err'); }
      },
    }, 'Delete');

    const cells = [
      h('td', {}, h('a', { href: `#/results/${job.id}` }, job.file_name)),
      h('td', {}, h('span', { class: 'badge info' }, (job.file_format || '?').toUpperCase())),
      h('td', {}, statusBadge(job.status)),
      h('td', { class: 'num' }, detail ? fmt.pct((detail.file_size || {}).reduction_pct) : '—'),
      h('td', { class: 'num' }, detail ? fmt.pct((detail.execution_time || {}).reduction_pct) : '—'),
      h('td', {}, detail ? verificationBadge((detail.quality || {}).verification_status) : '—'),
      h('td', { class: 'muted tiny' }, job.error ? String(job.error).slice(0, 70) : fmt.rel(job.created_at)),
      h('td', {}, h('div', { class: 'row' }, viewLink, deleteButton)),
    ];
    rows.push(h('tr', {}, ...cells));
  }

  mount(root,
    card({ title: 'Processing history', subtitle: `${jobs.length} optimization run(s)` },
      jobs.length
        ? h('div', { class: 'scroll' }, h('table', { class: 'data' },
          h('thead', {}, h('tr', {},
            h('th', {}, 'File'), h('th', {}, 'Format'), h('th', {}, 'Status'),
            h('th', { class: 'num' }, 'Size change'), h('th', { class: 'num' }, 'Time change'),
            h('th', {}, 'Verification'), h('th', {}, 'Notes'), h('th', {}, ''))),
          h('tbody', {}, ...rows)))
        : h('div', { class: 'empty' },
          h('p', {}, 'Nothing has been processed yet.'),
          h('p', {}, h('a', { class: 'btn btn-primary', href: '#/upload' }, 'Upload a file'))))
  );
}

// ----------------------------------------------------------------- settings

export async function renderSettings(root, state, actions) {
  const payload = await api('/api/settings');
  const engines = await api('/api/engines');
  state.settings = payload.settings;
  state.backends = engines.backends;
  const bounds = payload.bounds;

  const inputs = {};
  const field = (key, label, hint) => {
    const bound = bounds[key] || {};
    inputs[key] = h('input', { type: 'number', value: payload.settings[key], min: bound.min, max: bound.max });
    return h('label', { class: 'field' },
      h('span', {}, label),
      inputs[key],
      h('span', { class: 'hint' }, `${hint} Allowed range ${fmt.int(bound.min)}–${fmt.int(bound.max)}.`));
  };

  const save = h('button', { class: 'btn btn-primary' }, 'Save settings');
  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      const body = {};
      for (const [key, node] of Object.entries(inputs)) body[key] = Number(node.value);
      const updated = await api('/api/settings', { method: 'PUT', body });
      state.settings = updated.settings;
      toast('Settings saved.', 'ok');
      actions.refreshMeta();
    } catch (error) {
      toast(error.message, 'err');
    } finally {
      save.disabled = false;
    }
  });

  const backendRows = (engines.backends.backends || []).map((backend) => h('tr', {},
    h('td', {}, backend.name),
    h('td', {}, backend.implemented ? h('span', { class: 'badge ok' }, 'active') : h('span', { class: 'badge violet' }, 'declared')),
    h('td', {}, backend.role),
    h('td', { class: 'small muted' }, backend.implemented ? backend.description : backend.reason_unavailable)
  ));

  mount(root,
    h('div', { class: 'grid side' },
      h('div', { class: 'stack' },
        card({ title: 'Processing limits', subtitle: 'These bound every upload and every run for your account' },
          field('max_upload_bytes', 'Maximum upload size (bytes)', 'Larger uploads are rejected before they are parsed.'),
          field('chunk_rows', 'Chunk size (rows)', 'The optimized path reads delimited text in blocks of this size.'),
          field('processing_timeout_seconds', 'Processing timeout (seconds)', 'A worker that overruns is terminated.'),
          field('max_analysis_rows', 'Analysis row limit', 'Profiling samples at most this many rows; execution always covers all of them.'),
          h('div', { class: 'row' }, save)
        ),
        card({ title: 'Security posture' },
          h('ul', { class: 'small muted', style: 'margin:.2rem 0 0 1rem;padding:0' },
            h('li', {}, 'Server-side sessions with scrypt-hashed passwords and HTTP-only cookies.'),
            h('li', {}, 'Every file, job and artifact lookup is scoped to your user id in SQL.'),
            h('li', {}, 'Uploads are parsed only inside an isolated worker process with a hard timeout.'),
            h('li', {}, 'Filenames are sanitised and stored paths are confined to your own directory.'),
            h('li', {}, 'Nothing is sent to an external AI or cloud service, and document contents are never logged.')
          )
        )
      ),
      h('div', { class: 'stack' },
        card({ title: 'Storage' },
          kvList([
            ['Files stored', fmt.int((state.overview && state.overview.files.total) || 0)],
            ['Disk used by your account', fmt.bytes(payload.disk_usage_bytes)],
            ['Hard upload ceiling', fmt.bytes(payload.hard_limits.max_upload_bytes)],
            ['Hard timeout ceiling', `${payload.hard_limits.max_processing_seconds} s`],
          ]),
          h('p', { class: 'hint' }, 'Deleting a file or a run removes its stored bytes immediately.')
        ),
        card({ title: 'Execution engines', subtitle: 'Implemented versus declared' },
          h('table', { class: 'data' },
            h('thead', {}, h('tr', {}, h('th', {}, 'Engine'), h('th', {}, 'State'), h('th', {}, 'Role'), h('th', {}, 'Notes'))),
            h('tbody', {}, ...backendRows)),
          h('p', { class: 'hint' }, engines.backends.note)
        ),
        card({ title: 'Measurement scope' },
          kvList(Object.entries(engines.measurement_scope).map(([key, value]) => [key, String(value)])))
      )
    )
  );
}
