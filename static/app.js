const TERMINAL_STATUSES = new Set(['done', 'failed', 'cancelled', 'interrupted']);

function showJobPanel() {
  document.getElementById('job-panel').classList.remove('hidden');
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
      const resp = await fetch(`/cancel_ai/${job.db_id}`, {
        method: 'POST',
        headers: { 'X-Requested-With': 'fetch' }
      });
      const data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.error || 'Cancel request failed');
      document.getElementById('job-message').textContent = 'cancel_requested: Cancel request sent. Waiting for the current step to stop.';
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
  const err = document.getElementById('job-errors');
  if (job.errors && job.errors.length) {
    err.style.display = 'block';
    err.textContent = job.errors.map(e => `${e.path || ''} ${e.error || e}`).join('\n');
  } else {
    err.style.display = 'none';
  }
  configureCancelButton(job);
}

async function pollJob(jobId, scanId) {
  while (true) {
    const resp = await fetch(`/job/${jobId}`);
    const data = await resp.json();
    if (!data.ok) throw new Error(data.error || 'Job lookup failed');
    const job = data.job;
    setJob(job);
    if (job.status === 'done') {
      window.location.href = `/?scan_id=${job.scan_id || scanId}`;
      return;
    }
    if (['failed', 'cancelled', 'interrupted'].includes(job.status)) {
      if (job.kind === 'ai') {
        window.location.href = `/?scan_id=${job.scan_id || scanId}`;
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
    await pollJob(data.job_id, data.scan_id);
  } catch (e) {
    showJobPanel();
    document.getElementById('job-message').textContent = `error: ${e.message}`;
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
