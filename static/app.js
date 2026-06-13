const TERMINAL_STATUSES = new Set(['done', 'failed', 'cancelled', 'interrupted']);

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function parseCsvRows(text) {
  const rows = [];
  let row = [];
  let cell = '';
  let inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    const next = text[i + 1];
    if (inQuotes) {
      if (ch === '"' && next === '"') {
        cell += '"';
        i++;
      } else if (ch === '"') {
        inQuotes = false;
      } else {
        cell += ch;
      }
    } else if (ch === '"') {
      inQuotes = true;
    } else if (ch === ',') {
      row.push(cell);
      cell = '';
    } else if (ch === '\n') {
      row.push(cell);
      rows.push(row);
      row = [];
      cell = '';
    } else if (ch !== '\r') {
      cell += ch;
    }
  }
  row.push(cell);
  if (row.some(v => String(v).trim() !== '')) rows.push(row);
  return rows;
}

function importCategoryCsv(fileInput, textarea) {
  const file = fileInput.files && fileInput.files[0];
  if (!file || !textarea) return;
  const reader = new FileReader();
  reader.onload = () => {
    const rows = parseCsvRows(String(reader.result || ''));
    const lines = [];
    for (const row of rows) {
      const name = String(row[0] || '').trim();
      const definition = String(row[1] || '').trim();
      if (!name) continue;
      const lower = name.toLowerCase();
      if (['category', 'category name', 'primary category', 'name'].includes(lower)) continue;
      lines.push(definition ? `${name}: ${definition}` : name);
    }
    if (!lines.length) {
      alert('No category rows found in that CSV. Use first column = category name, second column = definition.');
      return;
    }
    textarea.value = lines.join('\n');
    // The CSV has been imported into the saved settings textbox. Clearing the
    // file input lets you edit the imported lines before submission.
    fileInput.value = '';
  };
  reader.onerror = () => alert('Could not read the selected category CSV.');
  reader.readAsText(file);
}

function showJobPanel() {
  document.getElementById('job-panel').classList.remove('hidden');
}

async function requestAiCancel(dbId, messageTarget) {
  const resp = await fetch(`/cancel_ai/${dbId}`, {
    method: 'POST',
    headers: { 'X-Requested-With': 'fetch' }
  });
  const data = await resp.json();
  if (!resp.ok || !data.ok) throw new Error(data.error || 'Cancel request failed');
  if (messageTarget) messageTarget.textContent = 'cancel_requested: Cancel request sent. Waiting for the current step to stop.';
  await refreshAiJobs();
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
      await requestAiCancel(job.db_id, document.getElementById('job-message'));
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
  document.getElementById('job-bar').style.width = pct + '%';
  document.getElementById('job-message').textContent = `${job.status}: ${job.message || ''} ${total ? `(${current}/${total})` : ''}`;

  const log = document.getElementById('job-log-link');
  if (log && job.kind === 'ai' && job.extra && job.extra.log_url) {
    log.classList.remove('hidden');
    log.innerHTML = `<a href="${escapeHtml(job.extra.log_url)}">Open categorization error log</a>`;
  } else if (log) {
    log.classList.add('hidden');
    log.textContent = '';
  }

  const err = document.getElementById('job-errors');
  if (job.errors && job.errors.length) {
    err.style.display = 'block';
    err.textContent = job.errors.map(e => `${e.path || ''} ${e.error || e}`).join('\n');
  } else {
    err.style.display = 'none';
  }
  configureCancelButton(job);
}

function renderAiJobRow(job) {
  const cancelCell = job.cancellable
    ? `<button class="danger tiny ai-cancel-row" type="button" data-ai-job-id="${job.id}">Cancel</button>`
    : '';
  return `
    <tr>
      <td><a href="${escapeHtml(job.load_url)}">${escapeHtml(job.id)}</a></td>
      <td><a href="${escapeHtml(job.scan_url)}">${escapeHtml(job.scan_label)}</a></td>
      <td>${escapeHtml(job.status)}</td>
      <td>${escapeHtml(job.current)} / ${escapeHtml(job.total)}</td>
      <td>${escapeHtml(job.model)}</td>
      <td title="${escapeHtml(job.last_error)}">${escapeHtml(job.error_count)}</td>
      <td>${escapeHtml(job.created_at)}</td>
      <td>${escapeHtml(job.started_at)}</td>
      <td>${escapeHtml(job.finished_at)}</td>
      <td><a href="${escapeHtml(job.log_url)}">Open</a></td>
      <td>${cancelCell}</td>
    </tr>`;
}

function attachAiCancelHandlers() {
  document.querySelectorAll('.ai-cancel-row').forEach(btn => {
    btn.addEventListener('click', async () => {
      const id = btn.dataset.aiJobId;
      if (!id) return;
      if (!confirm('Cancel this categorization job? If Ollama is currently processing a file, the job will stop after that request returns.')) return;
      btn.disabled = true;
      btn.textContent = 'Cancelling...';
      try {
        await requestAiCancel(id, document.getElementById('job-message'));
      } catch (e) {
        alert(e.message);
        btn.disabled = false;
        btn.textContent = 'Cancel';
      }
    });
  });
}

async function refreshAiJobs() {
  const body = document.getElementById('ai-jobs-body');
  if (!body) return;
  try {
    const resp = await fetch('/api/ai_jobs');
    const data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || 'AI job lookup failed');
    body.innerHTML = data.jobs.map(renderAiJobRow).join('') || '<tr><td colspan="11" class="muted">No categorization jobs yet.</td></tr>';
    attachAiCancelHandlers();
  } catch (e) {
    // Keep the existing server-rendered table if the refresh fails.
    console.warn('Could not refresh categorization jobs', e);
  }
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
      window.location.href = `/?scan_id=${job.scan_id || scanId}`;
      return;
    }
    if (['failed', 'cancelled', 'interrupted'].includes(job.status)) {
      if (job.kind === 'ai') {
        await refreshAiJobs();
      }
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
    if (form.id === 'ai-form') await refreshAiJobs();
    await pollJob(data.job_id, data.scan_id);
  } catch (e) {
    showJobPanel();
    document.getElementById('job-message').textContent = `error: ${e.message}`;
  } finally {
    buttons.forEach(b => { b.disabled = false; b.textContent = b.dataset.oldText || 'Submit'; });
  }
}


const categoryCsvInput = document.getElementById('category-csv-input');
const categoriesTextarea = document.getElementById('categories-textarea');
if (categoryCsvInput && categoriesTextarea) {
  categoryCsvInput.addEventListener('change', () => importCategoryCsv(categoryCsvInput, categoriesTextarea));
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

refreshAiJobs();
setInterval(refreshAiJobs, 4000);
