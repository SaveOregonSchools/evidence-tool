const TERMINAL_STATUSES = new Set(['done', 'failed', 'cancelled', 'interrupted']);

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function showJobPanel() {
  document.getElementById('job-panel')?.classList.remove('hidden');
}

function selectedAiJobId() {
  const container = document.getElementById('ai-jobs-container');
  return container?.dataset.selectedAiJobId || new URLSearchParams(window.location.search).get('ai_job_id') || '';
}

function configureJobLogLink(job) {
  const link = document.getElementById('job-log-link') || document.getElementById('job-error-link');
  const wrap = document.getElementById('job-error-link-wrap');
  if (!link) return;
  const logUrl = job.extra && (job.extra.log_url || job.extra.error_log_url);
  if (job.kind === 'ai' && logUrl) {
    link.href = logUrl;
    link.classList.remove('hidden');
    wrap?.classList.remove('hidden');
  } else {
    link.href = '#';
    link.classList.add('hidden');
    wrap?.classList.add('hidden');
  }
}

function configureCancelButton(job) {
  const btn = document.getElementById('job-cancel');
  if (!btn) return;
  const cancellable = job.kind === 'ai' && !TERMINAL_STATUSES.has(job.status) && job.db_id;
  if (!cancellable) {
    btn.classList.add('hidden');
    btn.onclick = null;
    return;
  }
  btn.classList.remove('hidden');
  btn.disabled = false;
  btn.textContent = 'Cancel categorization job';
  btn.onclick = async () => {
    if (!confirm('Cancel this categorization job? If Ollama is currently processing a file, the job will stop after that request returns.')) return;
    btn.disabled = true;
    btn.textContent = 'Cancelling...';
    try {
      await cancelAiJob(job.db_id);
      document.getElementById('job-message').textContent = 'cancel_requested: Cancel request sent. Waiting for the current step to stop.';
      await refreshAiJobs();
    } catch (e) {
      document.getElementById('job-message').textContent = `error: ${e.message}`;
      btn.disabled = false;
      btn.textContent = 'Cancel categorization job';
    }
  };
}

function setJob(job) {
  showJobPanel();
  const total = job.total || 0;
  const current = job.current || 0;
  let pct = total > 0 ? Math.min(100, Math.round((current / total) * 100)) : 10;
  if (TERMINAL_STATUSES.has(job.status) && total === 0) pct = 100;

  const bar = document.getElementById('job-bar');
  const msg = document.getElementById('job-message');
  if (bar) bar.style.width = pct + '%';
  if (msg) msg.textContent = `${job.status}: ${job.message || ''} ${total ? `(${current}/${total})` : ''}`;

  const err = document.getElementById('job-errors');
  if (err) {
    if (job.errors && job.errors.length) {
      err.style.display = 'block';
      err.textContent = job.errors.map(e => `${e.path || ''}${e.path ? '\n' : ''}${e.error || e}`).join('\n\n');
    } else {
      err.style.display = 'none';
    }
  }
  configureJobLogLink(job);
  configureCancelButton(job);
}

function renderAiJobRow(job) {
  const selectedClass = String(job.id) === String(selectedAiJobId()) ? ' class="selected"' : '';
  const settingsUrl = job.settings_url || job.load_url || `/?scan_id=${encodeURIComponent(job.scan_id)}&ai_job_id=${encodeURIComponent(job.id)}`;
  const scanUrl = job.scan_url || `/?scan_id=${encodeURIComponent(job.scan_id)}`;
  const logUrl = job.log_url || job.error_url || `/ai_job/${encodeURIComponent(job.id)}/errors`;
  const isTerminal = job.terminal === true || job.cancellable === false || TERMINAL_STATUSES.has(job.status);
  const errors = Number(job.error_count || 0) > 0
    ? `<a href="${escapeHtml(logUrl)}" target="_blank" rel="noopener">${escapeHtml(job.error_count)}</a>`
    : '0';
  const actions = [
    `<a class="button secondary tiny" href="${escapeHtml(logUrl)}" target="_blank" rel="noopener">Log</a>`,
    isTerminal ? '' : `<button class="danger tiny" type="button" data-cancel-ai-job="${escapeHtml(job.id)}">Cancel</button>`
  ].filter(Boolean).join(' ');
  return `
    <tr${selectedClass}>
      <td><a href="${escapeHtml(settingsUrl)}" title="Load this job's saved Phase 2 settings">${escapeHtml(job.id)}</a></td>
      <td><a href="${escapeHtml(scanUrl)}">${escapeHtml(job.scan_label || `Scan ${job.scan_id}`)}</a></td>
      <td>${escapeHtml(job.status)}</td>
      <td>${escapeHtml(job.current)} / ${escapeHtml(job.total)}</td>
      <td>${escapeHtml(job.model)}</td>
      <td title="${escapeHtml(job.last_error)}">${errors}</td>
      <td>${escapeHtml(job.created_at)}</td>
      <td>${escapeHtml(job.started_at)}</td>
      <td>${escapeHtml(job.finished_at)}</td>
      <td>${actions}</td>
    </tr>`;
}

function renderAiJobs(jobs) {
  const container = document.getElementById('ai-jobs-container');
  if (!container) return;
  if (!jobs || jobs.length === 0) {
    container.innerHTML = '<p class="muted">No categorization jobs yet.</p>';
    return;
  }
  container.innerHTML = `
    <div class="table-wrap loose">
      <table class="compact">
        <thead>
          <tr>
            <th>ID</th><th>Scan</th><th>Status</th><th>Progress</th><th>Model</th><th>Errors</th><th>Created</th><th>Started</th><th>Finished</th><th></th>
          </tr>
        </thead>
        <tbody>${jobs.map(renderAiJobRow).join('')}</tbody>
      </table>
    </div>`;
}

async function refreshAiJobs() {
  const container = document.getElementById('ai-jobs-container');
  if (!container) return;
  try {
    const resp = await fetch('/api/ai_jobs');
    const data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || 'Could not refresh categorization jobs');
    renderAiJobs(data.jobs || []);
  } catch (e) {
    console.warn(e);
  }
}

async function cancelAiJob(aiJobId) {
  const resp = await fetch(`/cancel_ai/${aiJobId}`, {
    method: 'POST',
    headers: { 'X-Requested-With': 'fetch' }
  });
  const data = await resp.json();
  if (!resp.ok || !data.ok) throw new Error(data.error || 'Cancel request failed');
  return data;
}

async function pollJob(jobId, scanId) {
  while (true) {
    const resp = await fetch(`/job/${jobId}`);
    const data = await resp.json();
    if (!data.ok) throw new Error(data.error || 'Job lookup failed');
    const job = data.job;
    setJob(job);
    if (job.kind === 'ai') await refreshAiJobs();

    if (job.status === 'done') {
      if (job.kind === 'ai' && job.db_id) {
        window.location.href = `/?scan_id=${job.scan_id || scanId}&ai_job_id=${job.db_id}`;
      } else {
        window.location.href = `/?scan_id=${job.scan_id || scanId}`;
      }
      return;
    }
    if (['failed', 'cancelled', 'interrupted'].includes(job.status)) {
      // Keep the status panel visible so the last error and persistent log link remain copyable.
      if (job.kind === 'ai') await refreshAiJobs();
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 1200));
  }
}

async function submitJobForm(form, buttonText) {
  const buttons = [...form.querySelectorAll('button')];
  buttons.forEach(b => { b.disabled = true; b.dataset.oldText = b.textContent; b.textContent = buttonText; });
  try {
    const resp = await fetch(form.action, { method: 'POST', body: new FormData(form) });
    const data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || 'Request failed');
    if (String(data.job_id || '').startsWith('ai-')) await refreshAiJobs();
    await pollJob(data.job_id, data.scan_id);
  } catch (e) {
    showJobPanel();
    document.getElementById('job-message').textContent = `error: ${e.message}`;
    await refreshAiJobs();
  } finally {
    buttons.forEach(b => { b.disabled = false; b.textContent = b.dataset.oldText || 'Submit'; });
  }
}

for (const id of ['scan-form', 'upload-form', 'ai-form']) {
  const form = document.getElementById(id);
  if (form) {
    form.addEventListener('submit', ev => {
      ev.preventDefault();
      const text = id === 'ai-form' ? 'Categorizing...' : 'Scanning...';
      submitJobForm(form, text);
    });
  }
}

document.addEventListener('click', async ev => {
  const button = ev.target.closest('[data-cancel-ai-job], [data-cancel-ai-job-id]');
  if (!button) return;
  ev.preventDefault();
  const id = button.getAttribute('data-cancel-ai-job') || button.getAttribute('data-cancel-ai-job-id');
  if (!id) return;
  if (!confirm('Cancel this categorization job? If Ollama is currently processing a file, the job will stop after that request returns.')) return;
  button.disabled = true;
  button.textContent = 'Cancelling...';
  try {
    await cancelAiJob(id);
    await refreshAiJobs();
  } catch (e) {
    alert(e.message);
    button.disabled = false;
    button.textContent = 'Cancel';
  }
});

document.getElementById('refresh-ai-jobs')?.addEventListener('click', refreshAiJobs);
refreshAiJobs();
setInterval(refreshAiJobs, 3000);
