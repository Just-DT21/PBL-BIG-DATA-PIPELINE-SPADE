// API access. Every call is same-origin, credentials included, and errors are
// surfaced as Error objects carrying the server's message.

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function readError(response) {
  let detail = `${response.status} ${response.statusText}`;
  try {
    const payload = await response.json();
    if (payload && typeof payload.detail === 'string') detail = payload.detail;
    else if (payload && Array.isArray(payload.detail)) detail = payload.detail.map((d) => d.msg || d).join('; ');
  } catch (_) {
    /* keep the status line */
  }
  return detail;
}

export async function api(path, { method = 'GET', body, signal } = {}) {
  const options = { method, credentials: 'same-origin', signal, headers: {} };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  if (!response.ok) throw new ApiError(await readError(response), response.status);
  if (response.status === 204) return null;
  const type = response.headers.get('content-type') || '';
  return type.includes('application/json') ? response.json() : response.text();
}

export function uploadFile(file, { onProgress, signal } = {}) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append('file', file, file.name);
    const request = new XMLHttpRequest();
    request.open('POST', '/api/files', true);
    request.withCredentials = true;
    request.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable && onProgress) onProgress(event.loaded / event.total);
    });
    request.addEventListener('load', () => {
      let payload = null;
      try {
        payload = JSON.parse(request.responseText);
      } catch (_) {
        payload = null;
      }
      if (request.status >= 200 && request.status < 300) {
        resolve(payload);
      } else {
        const detail = payload && typeof payload.detail === 'string' ? payload.detail : `Upload failed (${request.status})`;
        reject(new ApiError(detail, request.status));
      }
    });
    request.addEventListener('error', () => reject(new ApiError('The upload could not be completed.', 0)));
    request.addEventListener('abort', () => reject(new ApiError('Upload cancelled.', 0)));
    if (signal) signal.addEventListener('abort', () => request.abort());
    request.send(form);
  });
}

export function artifactUrl(jobId, artifactId) {
  return `/api/jobs/${encodeURIComponent(jobId)}/artifacts/${encodeURIComponent(artifactId)}`;
}

export function originalUrl(fileId) {
  return `/api/files/${encodeURIComponent(fileId)}/download`;
}

export async function poll(fn, { intervalMs = 400, timeoutMs = 120000, onTick } = {}) {
  const started = Date.now();
  for (;;) {
    const value = await fn();
    if (onTick) onTick(value);
    if (value && (value.terminal || value.done)) return value;
    if (Date.now() - started > timeoutMs) throw new ApiError('Timed out waiting for the server.', 0);
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
}
