let selectedFile = null;
let currentJobId = null;

const SPONSOR_COLORS = {
  civic: '#059669', apify: '#f59e0b',
  contextual: '#6366f1', redis: '#dc2626', openclaw: '#7c3aed',
};

// ── File handling ──────────────────────────────────────────────────────────────

document.getElementById('fileInput').addEventListener('change', (e) => {
  const file = e.target.files[0];
  if (file) setFile(file);
});

const zone = document.getElementById('uploadZone');
zone.addEventListener('dragover', (e) => { e.preventDefault(); zone.classList.add('dragging'); });
zone.addEventListener('dragleave', () => zone.classList.remove('dragging'));
zone.addEventListener('drop', (e) => {
  e.preventDefault();
  zone.classList.remove('dragging');
  const file = e.dataTransfer.files[0];
  if (file) setFile(file);
});
zone.addEventListener('click', () => document.getElementById('fileInput').click());

function setFile(file) {
  selectedFile = file;
  document.getElementById('selectedFileName').textContent = file.name;
  document.getElementById('fileSelected').classList.remove('hidden');
}

function clearFile() {
  selectedFile = null;
  document.getElementById('fileSelected').classList.add('hidden');
  document.getElementById('fileInput').value = '';
}

function restart() {
  clearFile();
  document.getElementById('analysisSection').classList.add('hidden');
  document.getElementById('uploadSection').classList.remove('hidden');
  document.getElementById('activityFeed').innerHTML = '';
  document.getElementById('resultsContent').classList.add('hidden');
  document.getElementById('loadingPlaceholder').classList.remove('hidden');
  resetBadges();
  currentJobId = null;
}

// ── Analysis ───────────────────────────────────────────────────────────────────

async function startAnalysis() {
  if (!selectedFile) return;

  document.getElementById('uploadSection').classList.add('hidden');
  document.getElementById('analysisSection').classList.remove('hidden');

  // Upload file
  const formData = new FormData();
  formData.append('file', selectedFile);

  const resp = await fetch('/analyze', { method: 'POST', body: formData });
  const { job_id } = await resp.json();
  currentJobId = job_id;

  // Start SSE stream
  streamActivity(job_id);
}

function streamActivity(jobId) {
  const es = new EventSource(`/stream/${jobId}`);
  const feed = document.getElementById('activityFeed');

  es.onmessage = (e) => {
    const data = JSON.parse(e.data);
    const { sponsor, status, message } = data;

    if (sponsor === 'done') {
      es.close();
      fetchAndRenderResult(jobId);
      return;
    }

    // Add activity item
    const item = document.createElement('div');
    item.className = `activity-item ${status}`;
    item.dataset.sponsor = sponsor;

    const icon = status === 'success' ? '✓' : status === 'active' ? '→' : '·';
    item.innerHTML = `
      <span class="activity-dot"></span>
      <span>
        <strong style="color:${SPONSOR_COLORS[sponsor] || '#6b6b85'};font-size:10px;text-transform:uppercase;letter-spacing:0.06em">
          ${sponsor.toUpperCase()}
        </strong><br>
        ${message}
      </span>`;

    feed.appendChild(item);
    feed.scrollTop = feed.scrollHeight;

    // Update header badge
    updateBadge(sponsor, status);
  };

  es.onerror = () => es.close();
}

function updateBadge(sponsor, status) {
  const badge = document.getElementById(`badge-${sponsor}`);
  if (!badge) return;
  badge.classList.remove('active', 'done');
  if (status === 'active') badge.classList.add('active');
  if (status === 'success') badge.classList.add('done');
}

function resetBadges() {
  document.querySelectorAll('.sponsor-badge').forEach(b => b.classList.remove('active', 'done'));
}

async function fetchAndRenderResult(jobId) {
  const resp = await fetch(`/result/${jobId}`);
  const data = await resp.json();
  renderResults(data);
}

// ── Render Results ─────────────────────────────────────────────────────────────

function fmt(n) {
  return '$' + Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function renderResults(data) {
  document.getElementById('loadingPlaceholder').classList.add('hidden');
  document.getElementById('resultsContent').classList.remove('hidden');

  const s = data.summary;

  // Summary cards
  document.getElementById('totalBilled').textContent = fmt(s.total_billed);
  document.getElementById('fairValue').textContent = fmt(s.total_fair_price);
  document.getElementById('overchargeAmount').textContent = fmt(s.overcharge_amount);
  document.getElementById('overchargePct').textContent = `${s.overcharge_percent}% above fair market`;

  // Provider
  document.getElementById('providerName').textContent = data.provider || '';

  // Flags
  const flagsRow = document.getElementById('flagsRow');
  flagsRow.innerHTML = '';
  (s.flags_found || []).forEach(f => {
    const chip = document.createElement('span');
    chip.className = `flag-chip ${f}`;
    chip.textContent = f;
    flagsRow.appendChild(chip);
  });
  if (s.flags_found && s.flags_found.length === 0) {
    const chip = document.createElement('span');
    chip.className = 'flag-chip ok';
    chip.textContent = 'All charges within range';
    flagsRow.appendChild(chip);
  }

  // Line items
  const lineItemsEl = document.getElementById('lineItems');
  lineItemsEl.innerHTML = `
    <div class="line-item header">
      <span>CDT Code</span>
      <span>Description</span>
      <span>Billed</span>
      <span>Fair (80th%)</span>
      <span>Difference</span>
      <span>Status</span>
    </div>`;

  (data.line_items || []).forEach(item => {
    const isOver = item.flag === 'overcharge' || item.flag === 'duplicate';
    const row = document.createElement('div');
    row.className = 'line-item';
    row.innerHTML = `
      <span class="li-code">${item.cdt_code}${item.tooth ? '<br><small style="color:var(--muted);font-size:10px">Tooth ' + item.tooth + '</small>' : ''}</span>
      <span>${item.description}</span>
      <span>${fmt(item.billed)}</span>
      <span>${fmt(item.fair_price_p80)}</span>
      <span class="${isOver ? 'li-overcharge' : 'li-ok'}">${isOver ? '+' : ''}${fmt(item.difference)}</span>
      <span><span class="flag-pill ${item.flag}">${item.flag}</span></span>`;
    lineItemsEl.appendChild(row);
  });

  // Phone script
  document.getElementById('scriptContent').textContent = data.phone_script || '';

  // Appeal letter download
  document.getElementById('downloadBtn').href = `/appeal/${data.appeal_pdf_id}`;

  // Tab switching
  document.querySelectorAll('.output-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.output-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const which = tab.dataset.tab;
      document.getElementById('scriptContent').classList.toggle('hidden', which !== 'script');
      document.getElementById('letterContent').classList.toggle('hidden', which !== 'letter');
    });
  });
}
