'use strict';

const $ = id => document.getElementById(id);

let _pollTimer = null;
let _activeRunId = null;
let _adaptersCache = [];

async function loadAdapters() {
  try {
    const resp = await fetch('/eval/adapters');
    _adaptersCache = await resp.json();
    const sel = $('adapter-sel');
    sel.innerHTML = '';
    _adaptersCache.forEach(a => {
      const opt = document.createElement('option');
      opt.value = a.name;
      opt.textContent = `${a.name} ${a.version} [${a.status}]`;
      sel.appendChild(opt);
    });
    onAdapterChange();
  } catch (e) {
    $('run-status').textContent = 'Failed to load adapters: ' + e.message;
  }
}

function onAdapterChange() {
  const sel = $('adapter-sel');
  const opt = sel.options[sel.selectedIndex];
  if (!opt) return;
  const name = opt.value;
  const info = _adaptersCache.find(a => a.name === name);
  if (info) {
    const badgeEl = $('adapter-badge');
    const notesEl = $('adapter-notes');
    if (info.status === 'ready') {
      badgeEl.textContent = 'READY';
      badgeEl.className = 'badge ready';
    } else if (info.status === 'synthetic_only') {
      badgeEl.textContent = 'SYNTHETIC';
      badgeEl.className = 'badge synthetic';
    } else {
      badgeEl.textContent = info.status.toUpperCase();
      badgeEl.className = 'badge disabled';
    }
    notesEl.textContent = info.notes || '';
  }
}

async function startRun() {
  const adapter = $('adapter-sel').value;
  if (!adapter) return;
  $('run-btn').disabled = true;
  $('run-status').textContent = 'Launching...';
  try {
    const resp = await fetch('/eval/runs', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({adapter}),
    });
    const data = await resp.json();
    if (!resp.ok) {
      $('run-status').textContent = 'Error: ' + (data.error || resp.statusText);
      $('run-btn').disabled = false;
      return;
    }
    _activeRunId = data.run_id;
    $('run-status').textContent = `run_id=${data.run_id} — polling...`;
    startPolling(data.run_id);
    loadRuns();
  } catch (e) {
    $('run-status').textContent = 'Error: ' + e.message;
    $('run-btn').disabled = false;
  }
}

function startPolling(runId) {
  clearInterval(_pollTimer);
  _pollTimer = setInterval(async () => {
    try {
      const resp = await fetch(`/eval/runs/${runId}`);
      const data = await resp.json();
      const status = data.status || 'unknown';
      $('run-status').textContent = `run_id=${runId} — ${status}`;
      loadRuns();
      if (status === 'completed' || status === 'failed' || status === 'cancelled') {
        clearInterval(_pollTimer);
        _pollTimer = null;
        _activeRunId = null;
        $('run-btn').disabled = false;
      }
    } catch (e) {
      // network hiccup — keep polling
    }
  }, 2000);
}

async function loadRuns() {
  try {
    const resp = await fetch('/eval/runs');
    const runs = await resp.json();
    const tbody = $('runs-tbody');
    tbody.innerHTML = '';
    runs.slice().reverse().forEach(run => {
      const tr = document.createElement('tr');
      const statusClass = `status-${run.status || 'unknown'}`;
      tr.innerHTML = `
        <td>${run.run_id}</td>
        <td>${run.adapter || ''}</td>
        <td>${run.started_at ? run.started_at.substring(0, 19).replace('T', ' ') : ''}</td>
        <td class="${statusClass}">${run.status || ''}</td>
        <td>${run.pass_count != null ? run.pass_count : ''}/${run.case_count != null ? run.case_count : ''}</td>
      `;
      tr.addEventListener('click', () => loadRunDetail(run.run_id));
      tbody.appendChild(tr);
    });
  } catch (e) {
    // ignore
  }
}

async function loadRunDetail(runId) {
  const area = $('detail-area');
  try {
    const resp = await fetch(`/eval/runs/${runId}`);
    const data = await resp.json();
    const cases = data.cases || [];
    const passCount = cases.filter(c => c.final_status === 'completed' || c.final_status === 'skipped').length;
    area.innerHTML = `
      <div class="summary-box">
        <dl>
          <dt>run_id</dt><dd>${data.run_id || runId}</dd>
          <dt>adapter</dt><dd>${data.adapter || ''}</dd>
          <dt>status</dt><dd class="status-${data.status || ''}">${data.status || ''}</dd>
          <dt>cases</dt><dd>${passCount}/${cases.length} passed</dd>
        </dl>
      </div>
      <div id="case-list"></div>
      <div class="artifact-links">
        <a href="/eval/runs/${runId}" target="_blank">run.json</a>
      </div>
      <div class="failure-placeholder">
        Failure inspector — coming in PR2; requires CausalFailureSliceExtractor wiring.
      </div>
    `;
    const caseList = $('case-list');
    const adapterInfo = _adaptersCache.find(a => a.name === data.adapter);
    cases.forEach(c => {
      const row = document.createElement('div');
      row.className = 'case-row';
      const isSynthetic = adapterInfo ? !adapterInfo.supports_real_mode : true;
      row.innerHTML = `
        <span class="case-id">${c.case_id}</span>
        ${isSynthetic ? '<span class="badge synthetic">SYNTHETIC</span>' : ''}
        <span class="status-${c.final_status || ''}">${c.final_status || ''}</span>
      `;
      caseList.appendChild(row);
    });
  } catch (e) {
    area.textContent = 'Failed to load run: ' + e.message;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  $('adapter-sel').addEventListener('change', onAdapterChange);
  $('run-btn').addEventListener('click', startRun);
  loadAdapters();
  loadRuns();
});
