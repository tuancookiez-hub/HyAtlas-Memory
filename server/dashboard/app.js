// Global state
const REFRESH_S = window.REFRESH_S || 30;
const USER_IDS = window.USER_IDS || [];
let currentPage = 'overview';
let allMemories = [];
let layerCountsData = null;  // actual Qdrant counts per layer (used by Memory Composition bar)
let statusData = null;
let infoData = null;
let storageData = null;
let metricsData = null;
let codingCountData = null;
let l5Graph = null;  // full response from /api/l5/graph
let activityChart = null;
let radarChart = null;

// Layer definitions
const LAYERS = {
  'l0_basic_info': { name: 'Basic Info', desc: 'Foundational data points and identifiers', color: '#4a6fa5' },
  'l1_raw': { name: 'Raw', desc: 'Unprocessed sensory and contextual inputs', color: '#3d8b8b' },
  'l2_fact': { name: 'Facts', desc: 'Discrete, verifiable pieces of information', color: '#6b4c9a' },
  'l3_summary': { name: 'Summaries', desc: 'Syntheses of multiple facts, events, and observations', color: '#4a6fa5' },
  'l4_identity': { name: 'Identity', desc: 'Self-concept, roles, preferences, and defining characteristics', color: '#d4af37' },
  'l5_knowledge': { name: 'Knowledge', desc: 'Consolidated understanding and expertise', color: '#3d8b8b' },
  'l6_schema': { name: 'Schemas', desc: 'Structural patterns and organizational frameworks', color: '#6b4c9a' },
  'l7_intention': { name: 'Meta Principles', desc: 'Core values, timeless truths, and highest-order principles', color: '#d4af37' }
};

// Navigation
document.querySelectorAll('.nav-item').forEach(item => {
  item.addEventListener('click', () => {
    const page = item.dataset.page;
    navigateTo(page);
  });
});

function hideBootScreen(){const b=document.getElementById('boot-screen');if(!b)return;b.classList.add('gone');setTimeout(function(){b.style.display='none'},500)}
function hideObsSeed(){const s=document.getElementById('obs-seed');if(!s)return;s.classList.add('seed-fade-out');setTimeout(function(){s.style.display='none'},400)}
function showObsSeed(){const s=document.getElementById('obs-seed');if(!s)return;s.classList.remove('seed-fade-out');s.style.display='flex';s.style.opacity=''}
function enterPage(page){const el=document.getElementById('page-'+page);if(!el)return;el.classList.remove('entered');el.offsetHeight;el.classList.add('entered')}
function navigateTo(page) {
  currentPage = page;
  const prev=document.querySelector('.page-section.entered');

  // Update nav
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.querySelector(`[data-page="${page}"]`).classList.add('active');

  // Update page sections
  document.querySelectorAll('.page-section').forEach(s => s.classList.remove('active','entered'));
  const newEl=document.getElementById(`page-${page}`);
  newEl.classList.add('active');

  // Update right sidebar
  updateRightSidebar(page);

  // Transition: fade out old, fade in new
  if(prev&&prev!==newEl){prev.classList.remove('entered');setTimeout(function(){enterPage(page)},150)}
  else{enterPage(page)}

  // Page-specific init
  if (page === 'observatory') {
    showObsSeed();
    if (!sceneInitialized) {
      setTimeout(initGraph, 100);
    } else {
      setTimeout(() => {
        obsAnim = null;
        obsPan = {x: 0, y: 0};
        obsZoom = computeObservatoryFitZoom();
        if (typeof updateCamera === 'function') updateCamera(false);
        setTimeout(hideObsSeed, 800);
      }, 50);
    }
  }
  if (page === 'l5') {
    initL5Page();
  }
}

// Compute the obsZoom value that fits the full graph into the viewport.
// Without this, fixed zoom values leave L6/L7 nodes off-screen.
function computeObservatoryFitZoom() {
  const c = document.getElementById('graph-container');
  if (!c || !obsScene) return 1.0;
  let maxRadius = 0;
  let minY = Infinity, maxY = -Infinity;
  if (window.__obsDebug && window.__obsDebug.nodes) {
    for (const n of window.__obsDebug.nodes) {
      if (n.placeholder) continue;
      const r = Math.sqrt((n.x || 0) ** 2 + (n.z || 0) ** 2);
      if (r > maxRadius) maxRadius = r;
      if (typeof n.y === 'number') {
        if (n.y < minY) minY = n.y;
        if (n.y > maxY) maxY = n.y;
      }
    }
  }
  if (maxRadius < 1) maxRadius = 200;
  maxRadius = Math.max(maxRadius, 80);
  const galH = (maxY - minY) || 600;
  const galCY = (maxY + minY) / 2 || 0;
  const fovHalfRad = (42 / 2) * Math.PI / 180;
  const tanFovHalf = Math.tan(fovHalfRad);
  const viewW = c.clientWidth || 1;
  const viewH = c.clientHeight || 1;
  const aspect = viewW / viewH;
  const baseZ = 420;
  const fitByX = (maxRadius + 80) / (baseZ * tanFovHalf * aspect);
  const fitByY = (galH / 2 + 140) / (baseZ * tanFovHalf);
  const fit = Math.max(fitByX, fitByY);
  window.__obsFitCenterY = galCY;
  return Math.max(0.3, Math.min(5.0, fit));
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[c]);
}
function escapeAttr(s) {
  return escapeHtml(s);
}

// API calls
async function fetchJSON(url, options = {}) {
  const resp = await fetch(url, options);
  return resp.json();
}

async function loadAllData() {
  try {
    const [status, info, memories, storage, metrics, codingCount, codingMemories, layerCounts, graphCounts, l5] = await Promise.all([
      fetchJSON('/api/status'),
      fetchJSON('/api/info'),
      fetchJSON('/api/memories?limit=500'),
      fetchJSON('/api/storage'),
      fetchJSON('/api/metrics?minutes=10080'),
      fetchJSON('/api/coding-count'),
      fetchJSON('/api/coding-memories?limit=500'),
      fetchJSON('/api/layer-counts'),
      fetchJSON('/api/graph-counts'),
      fetchJSON('/api/l5/graph').catch(() => null),  // L5 may not exist yet; ignore failure
    ]);
    l5Graph = l5;

    statusData = status;
    infoData = info;
    // Merge Qdrant L0-L4 + Kuzu L5/L6/L7 into one layer-counts view
    // (don't reassign const layerCounts; build a new mergedCounts object)
    if (graphCounts && typeof graphCounts === 'object') {
      const mergedCounts = (layerCounts && layerCounts.counts) ? {...layerCounts.counts} : {};
      if (graphCounts.l5_knowledge) mergedCounts.l5_knowledge = graphCounts.l5_knowledge;
      if (graphCounts.l6_schema)   mergedCounts.l6_schema   = graphCounts.l6_schema;
      if (graphCounts.l7_intention) mergedCounts.l7_intention = graphCounts.l7_intention;
      layerCountsData = {
        ...(layerCounts || {}),
        counts: mergedCounts,
        graph_total: graphCounts.total || 0,
      };
    } else {
      layerCountsData = layerCounts || null;
    }

    // Normalize coding memories to the VDB memory shape so the existing
    // Today-page tabs / filters / sort work without further changes.
    const codingMems = (codingMemories.memories || []).map(cm => ({
      memory_id:        cm.memory_id,
      user_id:          'coding',
      agent_id:         cm.agent_id || 'default',
      layer:            'coding',
      content:          cm.task || cm.solution || 'coding memory',
      // gmt_created is the CREATION time; gmt_updated is the last
      // modification time. The dashboard uses gmt_updated for "ago" when
      // available, so a coding memory that was just UPDATED shows as
      // recent even if its initial creation was hours ago.
      gmt_created:      Math.floor(new Date(cm.created_at).getTime() / 1000),
      gmt_updated:      Math.floor(new Date(cm.updated_at || cm.created_at).getTime() / 1000),
      score:            null,
      workspace_id:     cm.workspace_id,
      branch:           cm.branch,
      session_id:       cm.session_id,
      confidence:       cm.confidence,
      _source:          'coding',
    }));

    // Normalize L5 entities to the VDB memory shape so the Observatory
    // graph plots them in the L5 ring alongside L0-L4. L5 entities are
    // extracted from L2_facts; their mention_count proxies the importance
    // (high-mention entities get larger graph nodes; their synthetic
    // timestamp scales with mention_count so they're spread out across
    // the timeline instead of all bunched at "today").
    const l5Mems = (l5Graph && l5Graph.nodes) ? l5Graph.nodes.map(n => {
      const daysAgo = Math.min(180, Math.max(1, n.mention_count || 0));
      const ts = Math.floor(Date.now() / 1000) - (86400 * daysAgo);
      const rawLayer = (n.layer || '').toLowerCase();
      const layerMap = {
        'l5_knowledge': 'l5_knowledge',
        'l6_schema': 'l6_schema',
        'l7_intention': 'l7_intention',
      };
      return {
        memory_id:        'l5_' + n.node_id,
        user_id:          'l5_knowledge',
        agent_id:         'default',
        layer:            layerMap[rawLayer] || 'l5_knowledge',
        content:          n.name,
        gmt_created:      ts,
        gmt_updated:      ts,
        score:            null,
        session_id:       'l5',
        confidence:       n.confidence || 0.95,
        entity_type:      n.entity_type,
        mention_count:    n.mention_count || 1,
        aliases:          n.aliases || [],
        _source:          'l5_graph',
      };
    }) : [];

    allMemories = [
      ...(memories.memories || []),
      ...codingMems,
      ...l5Mems,
    ];

    storageData = storage;
    metricsData = metrics;
    codingCountData = codingCount;

    renderAll();
    updateGlobalStatus();
    return true;
  } catch (err) {
    console.error('Failed to load data:', err);
    return false;
  }
}

function updateGlobalStatus() {
  const dot = document.getElementById('global-status-dot');
  const text = document.getElementById('global-status-text');
  const meta = document.getElementById('global-status-meta');
  
  if (!statusData) {
    dot.className = 'status-dot error';
    text.textContent = 'OFFLINE';
    text.style.color = 'var(--red)';
    return;
  }
  
  const allOk = statusData.vdb === 'ok' && statusData.embed === 'ok' && statusData.llm === 'ok';
  
  if (allOk) {
    dot.className = 'status-dot';
    text.textContent = 'OPERATIONAL';
    text.style.color = 'var(--green)';
  } else {
    dot.className = 'status-dot degraded';
    text.textContent = 'DEGRADED';
    text.style.color = 'var(--accent)';
  }
  
  const lastMemory = allMemories.length > 0
    ? allMemories.reduce((latest, m) => {
        const ts = m.gmt_created || 0;
        return ts > (latest.gmt_created || 0) ? m : latest;
      }, allMemories[0])
    : null;
  if (lastMemory && lastMemory.gmt_created) {
    const ago = Math.floor((Date.now() / 1000 - lastMemory.gmt_created) / 60);
    if (ago < 60) {
      meta.textContent = `Last memory: ${ago}m ago`;
    } else if (ago < 1440) {
      const hours = Math.floor(ago / 60);
      const mins = ago % 60;
      meta.textContent = `Last memory: ${hours}h ${mins}m ago`;
    } else {
      const days = Math.floor(ago / 1440);
      const hours = Math.floor((ago % 1440) / 60);
      meta.textContent = `Last memory: ${days}d ${hours}h ago`;
    }
  } else {
    meta.textContent = 'Last memory: —';
  }
}

function renderAll() {
  renderOverview();
  renderLayers();
  renderToday();
  renderSystem();
  updateRightSidebar(currentPage);
}

// Overview Page
function renderOverview() {
  // Compute total memories across all sources: VDB active + Coding
  const vdbActive = (statusData?.vdb_points || 0);
  const codingTotal = codingCountData?.total || 0;
  const totalMemories = vdbActive + codingTotal;

  // Compute total L5 graph links (edges + relationships)
  const totalLinks = (l5Graph && l5Graph.relations) ? l5Graph.relations.length
                    : (l5Graph && l5Graph.edges) ? l5Graph.edges.length
                    : 0;

  // Compute active layers from layer-counts (includes Kuzu L5/L6/L7, not just VDB)
  const lc = (layerCountsData && layerCountsData.counts) ? layerCountsData.counts : {};
  const activeLayers = Object.values(lc).filter(c => c > 0).length;
  const totalLayers = 8;

  // Stat cards
  const statsHtml = `
    <div class="stat-card">
      <div class="stat-label">MEMORIES STORED</div>
      <div class="stat-value">${totalMemories}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">LINKS</div>
      <div class="stat-value">${totalLinks}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">LAYER COVERAGE</div>
      <div class="stat-value">${activeLayers}<span style="color: var(--muted); font-size: 20px;">/${totalLayers}</span></div>
    </div>
  `;
  document.getElementById('overview-stats').innerHTML = statsHtml;
  
  // Composition bar
  renderCompositionBar();

  // Memory store
  renderMemoryStore();
  
  // Activity chart
  renderActivityChart();
  
  // Operations
  renderOperations();
}

function getActiveLayerCount() {
  const layers = new Set(allMemories.map(m => m.layer));
  return layers.size;
}

function renderCompositionBar() {
  // The bar uses ACTUAL Qdrant counts (queried via /api/layer-counts),
  // NOT the allMemories sample — the sample is biased by recent
  // gmt_created activity (e.g., a backfill of L1_RAW + L4_identity would
  // make L2 look much smaller than it really is).
  const lc = (typeof layerCountsData === 'object' && layerCountsData) ? layerCountsData : null;

  let layerCounts, total;
  if (lc && lc.counts && typeof lc.total === 'number' && lc.total > 0) {
    layerCounts = lc.counts;
    total = lc.total;
  } else {
    // Fallback to the sample if the endpoint failed for any reason.
    layerCounts = {};
    allMemories.forEach(m => { layerCounts[m.layer] = (layerCounts[m.layer] || 0) + 1; });
    total = allMemories.length;
  }

  let html = '<div class="tag-bar">';

  // L5 is now implemented (66 entities / 120 relations in Kuzu, surfaced
  // through /api/layer-counts overlay). L6/L7 are populated by the
  // System2 digest (live in Kuzu, not Qdrant).
  const layerOrder = ['l0_basic_info', 'l1_raw', 'l2_fact', 'l3_summary', 'l4_identity', 'l5_knowledge', 'l6_schema', 'l7_intention'];
  layerOrder.forEach(layer => {
    const count = layerCounts[layer] || 0;
    if (count > 0) {
      const pct = (count / total * 100).toFixed(1);
      const layerInfo = LAYERS[layer] || { name: layer, color: '#666' };
      // Bar segment: clean colored block, no text inside.
      html += `<div class="tag-segment layer-${layer}" style="width: ${pct}%" title="${escapeHtml(layerInfo.name)}: ${count.toLocaleString()} (${pct}%)"></div>`;
    }
  });

  html += '</div>';

  // Legend under the bar — swatch + layer code + count + percentage
  html += '<div class="composition-legend">';
  layerOrder.forEach(layer => {
    const count = layerCounts[layer] || 0;
    if (count > 0) {
      const pct = (count / total * 100).toFixed(1);
      const layerLabel = layer.split('_')[0].toUpperCase();
      html += `<span class="legend-item">`
           +  `<span class="legend-swatch layer-${layer}"></span>`
           +  `<span class="legend-name">${layerLabel}</span>`
           +  `<span class="legend-count">${count.toLocaleString()}</span>`
           +  `<span class="legend-pct">${pct}%</span>`
           +  `</span>`;
    }
  });
  html += '</div>';

  document.getElementById('composition-bar').innerHTML = html;
}

function renderMemoryStore() {
  const vdbPoints = statusData?.vdb_points || 0;
  const codingTotal = codingCountData?.total || 0;
  const total = vdbPoints + codingTotal;
  
  const allOk = statusData?.vdb === 'ok' && statusData?.embed === 'ok' && statusData?.llm === 'ok';
  const statusText = allOk ? 'System ready (all services online)' : 'System degraded';
  
  const html = `
    <div class="flex items-center gap-3 mb-3">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2">
        <path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>
      </svg>
      <div>
        <div class="text-sm font-mono">${total} memories stored</div>
        <div class="text-xs text-muted">${statusText}</div>
      </div>
    </div>
    <div class="text-xs text-muted mt-2">VDB: ${vdbPoints} • Coding: ${codingTotal}</div>
  `;
  
  document.getElementById('memory-store').innerHTML = html;
}

function renderActivityChart() {
  const ctx = document.getElementById('activity-chart').getContext('2d');
  
  // Group memories by day (last 7 days)
  const days = {};
  const now = Date.now() / 1000;
  for (let i = 6; i >= 0; i--) {
    const date = new Date((now - i * 86400) * 1000);
    const key = date.toISOString().split('T')[0];
    days[key] = 0;
  }
  
  allMemories.forEach(m => {
    if (m.gmt_created) {
      const date = new Date(m.gmt_created * 1000).toISOString().split('T')[0];
      if (days.hasOwnProperty(date)) {
        days[date]++;
      }
    }
  });
  
  const labels = Object.keys(days).map(d => {
    const date = new Date(d);
    return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  });
  const data = Object.values(days);
  
  if (activityChart) {
    activityChart.destroy();
  }
  
  activityChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: 'Memories Ingested',
        data,
        borderColor: '#d4af37',
        backgroundColor: 'rgba(212, 175, 55, 0.1)',
        fill: true,
        tension: 0.4,
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 6,
        pointHoverBackgroundColor: '#d4af37'
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#0a0a0a',
          borderColor: '#1a1a1a',
          borderWidth: 1,
          titleColor: '#e8e8e8',
          bodyColor: '#e8e8e8',
          padding: 12
        }
      },
      scales: {
        x: {
          grid: { color: '#1a1a1a', drawBorder: false },
          ticks: { color: '#666666', font: { size: 11 } }
        },
        y: {
          grid: { color: '#1a1a1a', drawBorder: false },
          ticks: { color: '#666666', font: { size: 11 } },
          beginAtZero: true
        }
      }
    }
  });
}

function renderOperations() {
  const vdbPoints = statusData?.vdb_points || 0;
  const activeLayers = getActiveLayerCount();
  const codingTotal = codingCountData?.total || 0;
  
  const html = `
    <div class="stat-card">
      <div class="stat-label">TOTAL MEMORIES</div>
      <div class="stat-value">${vdbPoints}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">LAYERS ACTIVE</div>
      <div class="stat-value">${activeLayers}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">CODING MEMORIES</div>
      <div class="stat-value">${codingTotal}</div>
    </div>
  `;
  
  document.getElementById('operations-stats').innerHTML = html;
}


async function performSearch() {
  const query = document.getElementById('search-input').value.trim();
  
  if (!query) {
    searchResults = [];
    renderSearchResults();
    return;
  }
  
  try {
    const body = {
      query,
      user_ids: USER_IDS,
      limit: 20
    };
    
    const resp = await fetchJSON('/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    
    // Merge all categories
    searchResults = [
      ...(resp.memories?.profile || []),
      ...(resp.memories?.proactive || []),
      ...(resp.memories?.normal || [])
    ];
    
    renderSearchResults();
  } catch (err) {
    console.error('Search failed:', err);
  }
}

function renderSearchResults() {
  document.getElementById('results-count').textContent = `RESULTS (${searchResults.length})`;
  
  const html = searchResults.map((m, i) => {
    const title = (m.content || '').substring(0, 60) + '...';
    const snippet = (m.content || '').substring(0, 100) + '...';
    const score = m.score?.toFixed(2) || '—';
    const tagCount = (m.tags || []).length;
    
    return `
      <div class="search-result" data-index="${i}">
        <div class="flex justify-between items-start mb-2">
          <span class="badge badge-layer layer-${m.layer}">${m.layer}</span>
          <span class="font-mono text-xs text-muted">${score}</span>
        </div>
        <div class="text-sm font-semibold mb-2">${title}</div>
        <div class="text-xs text-muted mb-2">${snippet}</div>
        <div class="text-xs text-muted">${tagCount} tags</div>
      </div>
    `;
  }).join('');
  
  document.getElementById('search-results').innerHTML = html;
  
  document.querySelectorAll('.search-result').forEach(el => {
    el.addEventListener('click', () => {
      const idx = parseInt(el.dataset.index);
      showMemoryDetail(searchResults[idx]);
      document.querySelectorAll('.search-result').forEach(r => r.classList.remove('selected'));
      el.classList.add('selected');
    });
  });
}

function showMemoryDetail(memory) {
  const title = (memory.content || '').substring(0, 60) + '...';
  const tagCounts = {};
  (memory.tags || []).forEach(tag => {
    tagCounts[tag] = allMemories.filter(m => (m.tags || []).includes(tag)).length;
  });
  
  const html = `
    <div class="memory-detail">
      <div class="text-xs text-muted mb-2">MEMORY DETAIL</div>
      <span class="badge badge-layer layer-${memory.layer} mb-3">${memory.layer}</span>
      
      <div class="text-sm font-semibold mb-2">${title}</div>
      <div class="text-xs text-muted font-mono mb-3">id: ${memory.memory_id}</div>
      <div class="text-xs text-muted mb-4">${new Date(memory.gmt_created * 1000).toLocaleString()}</div>
      
      <div class="text-sm mb-4">${memory.content || '—'}</div>
      
      <div class="mb-4">
        <div class="text-xs text-muted mb-2">TAGS</div>
        ${(memory.tags || []).map(t => `<span class="badge badge-tag">${t}</span>`).join(' ')}
      </div>
      
      <div class="mb-4">
        <div class="text-xs text-muted mb-2">TAG FREQUENCY</div>
        ${Object.entries(tagCounts).map(([tag, count]) => `
          <div class="text-xs mb-1">${tag}: <span class="font-mono">${count}</span></div>
        `).join('')}
      </div>
      
      <div class="mb-4">
        <div class="text-xs text-muted mb-2">SESSION</div>
        <div class="text-xs font-mono">${memory.session_id || '—'}</div>
      </div>
      
      <button class="btn" onclick="navigateTo('observatory')">Open in Observatory</button>
    </div>
  `;
  
  document.getElementById('right-sidebar').innerHTML = html;
}

// Memory Layers
function renderLayers() {
  const layerCounts = {};
  const layerTagCounts = {};
  
  allMemories.forEach(m => {
    layerCounts[m.layer] = (layerCounts[m.layer] || 0) + 1;
    const tagCount = (m.tags || []).length;
    layerTagCounts[m.layer] = (layerTagCounts[m.layer] || 0) + tagCount;
  });
  
  const total = allMemories.length;
  
  const rows = Object.entries(LAYERS).map(([key, info]) => {
    const count = layerCounts[key] || 0;
    const pct = total > 0 ? (count / total * 100).toFixed(1) : '0.0';
    const avgTags = count > 0 ? (layerTagCounts[key] / count).toFixed(1) : '0.0';
    
    return `
      <tr>
        <td>
          <div class="flex items-center gap-3">
            <div class="layer-indicator layer-${key}" style="background: ${info.color}">${key.split('_')[0].toUpperCase()}</div>
            <div>
              <div class="font-semibold">${info.name}</div>
              <div class="text-xs text-muted">${info.desc}</div>
            </div>
          </div>
        </td>
        <td class="font-mono">${count}</td>
        <td class="font-mono">${pct}%</td>
        <td class="font-mono">${avgTags}</td>
      </tr>
    `;
  }).join('');
  
  document.getElementById('layers-tbody').innerHTML = rows;
  document.getElementById('layers-total').textContent = `Total: ${total}`;
  document.getElementById('layers-active').textContent = `Layers Active: ${getActiveLayerCount()}`;
  
  // Right sidebar - layer hierarchy
  renderLayerHierarchy(layerCounts);
}

function renderLayerHierarchy(layerCounts) {
  const html = Object.entries(LAYERS).map(([key, info]) => {
    const count = layerCounts[key] || 0;
    const active = count > 0;
    
    return `
      <div class="layer-node" style="opacity: ${active ? 1 : 0.4}">
        <div class="layer-indicator" style="background: ${info.color}">${key.split('_')[0].toUpperCase()}</div>
        <div class="layer-info">
          <div class="layer-name">${info.name}</div>
          <div class="layer-count">${count} memories</div>
        </div>
      </div>
    `;
  }).reverse().join('');
  
  const sidebarHtml = `
    <div class="right-section">
      <div class="right-section-title">LAYER HIERARCHY</div>
      <div class="text-xs text-muted mb-4">Information flows upward through abstraction.</div>
      <div class="layer-hierarchy">${html}</div>
      <div class="text-xs text-muted mt-4">
        <div class="flex items-center gap-2 mb-2">
          <div style="width: 12px; height: 2px; background: var(--accent)"></div>
          <span>Active Layer</span>
        </div>
        <div class="flex items-center gap-2">
          <div style="width: 12px; height: 2px; background: var(--border); border-style: dashed"></div>
          <span>Empty Layer</span>
        </div>
      </div>
      <button class="export-btn mt-4" onclick="navigateTo('observatory')">Explore in Observatory</button>
    </div>
  `;
  
  if (currentPage === 'layers') {
    document.getElementById('right-sidebar').innerHTML = sidebarHtml;
  }
}

// Today / Activity
let todayFilter = 'all';

document.querySelectorAll('#page-today .tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('#page-today .tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    todayFilter = tab.dataset.filter;
    renderToday();
  });
});

// Recent Ingestion tabs (Overview page right sidebar) — event-delegated
// because the tabs are re-rendered every time renderOverviewSidebar() runs.
document.addEventListener('click', (e) => {
  const tab = e.target.closest('.ingest-tab');
  if (!tab) return;
  const newTab = tab.dataset.tab;
  if (!newTab || newTab === recentIngestionTab) return;
  recentIngestionTab = newTab;
  renderOverviewSidebar();
});

document.getElementById('export-json').addEventListener('click', () => {
  const today = allMemories.filter(m => {
    const created = new Date(m.gmt_created * 1000);
    const now = new Date();
    return created.toDateString() === now.toDateString();
  });
  
  const blob = new Blob([JSON.stringify(today, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `memories-${new Date().toISOString().split('T')[0]}.json`;
  a.click();
  URL.revokeObjectURL(url);
});

function renderToday() {
  const since = Date.now() - 24 * 60 * 60 * 1000;

  let filtered = allMemories.filter(m => {
    const created = new Date(m.gmt_created * 1000);
    return created.getTime() >= since;
  });
  
  if (todayFilter === 'vdb') {
    filtered = filtered.filter(m => m.user_id !== 'coding');
  } else if (todayFilter === 'coding') {
    filtered = filtered.filter(m => m.user_id === 'coding');
  }
  
  filtered.sort((a, b) => b.gmt_created - a.gmt_created);
  
  const html = filtered.slice(0, 20).map(m => {
    const title = (m.content || '').substring(0, 60) + '...';
    const time = new Date(m.gmt_created * 1000).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });
    const ago = Math.floor((Date.now() / 1000 - m.gmt_created) / 60);
    const agoText = ago < 60 ? `${ago}m ago` : `${Math.floor(ago / 60)}h ago`;
    
    return `
      <div class="timeline-item">
        <div class="timeline-dot"></div>
        <div class="timeline-content">
          <div class="timeline-time">${time} • ${agoText}</div>
          <div class="timeline-title">${title}</div>
          <div class="flex gap-2 mt-2">
            <span class="badge badge-layer layer-${m.layer}">${m.layer}</span>
            ${(m.tags || []).slice(0, 3).map(t => `<span class="badge badge-tag">${t}</span>`).join('')}
          </div>
        </div>
      </div>
    `;
  }).join('');
  
  document.getElementById('timeline').innerHTML = html || '<div class="text-muted">No events today</div>';
  
  // Right sidebar - summary
  renderTodaySummary(filtered);
}

function renderTodaySummary(todayMemories) {
  const uniqueSessions = new Set(todayMemories.map(m => m.session_id)).size;
  const uniqueTags = new Set(todayMemories.flatMap(m => m.tags || [])).size;
  
  const weekAgo = Date.now() / 1000 - 7 * 86400;
  const weekCount = allMemories.filter(m => m.gmt_created >= weekAgo).length;
  
  const html = `
    <div class="right-section">
      <div class="right-section-title">TODAY'S SUMMARY</div>
      <div class="summary-grid">
        <div class="summary-card">
          <div class="summary-label">MEMORIES INGESTED</div>
          <div class="summary-value">${todayMemories.length}</div>
        </div>
        <div class="summary-card">
          <div class="summary-label">UNIQUE SESSIONS</div>
          <div class="summary-value">${uniqueSessions}</div>
        </div>
        <div class="summary-card">
          <div class="summary-label">UNIQUE TAGS</div>
          <div class="summary-value">${uniqueTags}</div>
        </div>
        <div class="summary-card">
          <div class="summary-label">THIS WEEK</div>
          <div class="summary-value">${weekCount}</div>
        </div>
      </div>
    </div>
  `;
  
  if (currentPage === 'today') {
    document.getElementById('right-sidebar').innerHTML = html;
  }
}

// Settings / System
document.querySelectorAll('#page-system .tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('#page-system .tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    
    document.querySelectorAll('.tab-content').forEach(c => c.style.display = 'none');
    document.getElementById(`tab-${tab.dataset.tab}`).style.display = 'block';
  });
});

function renderSystem() {
  // System info
  const uptime = metricsData?.uptime_seconds ? formatUptime(metricsData.uptime_seconds) : '—';
  const lastMemory = allMemories.length > 0 ? new Date(allMemories[0].gmt_created * 1000).toLocaleString() : '—';
  
  const infoHtml = `
    <div class="kv-item">
      <div class="kv-label">System Name</div>
      <div class="kv-value">${infoData?.name || '—'}</div>
    </div>
    <div class="kv-item">
      <div class="kv-label">Version</div>
      <div class="kv-value">${infoData?.version || '—'}</div>
    </div>
    <div class="kv-item">
      <div class="kv-label">Status</div>
      <div class="kv-value">${infoData?.status || '—'}</div>
    </div>
    <div class="kv-item">
      <div class="kv-label">Uptime</div>
      <div class="kv-value">${uptime}</div>
    </div>
    <div class="kv-item">
      <div class="kv-label">Server Time</div>
      <div class="kv-value">${new Date().toLocaleString()}</div>
    </div>
    <div class="kv-item">
      <div class="kv-label">Platform</div>
      <div class="kv-value">Windows (local)</div>
    </div>
    <div class="kv-item">
      <div class="kv-label">VDB Provider</div>
      <div class="kv-value">${statusData?.vdb_provider || '—'}</div>
    </div>
    <div class="kv-item">
      <div class="kv-label">VDB Collection</div>
      <div class="kv-value">${statusData?.vdb_collection || '—'}</div>
    </div>
    <div class="kv-item">
      <div class="kv-label">Last Memory</div>
      <div class="kv-value">${lastMemory}</div>
    </div>
  `;
  
  document.getElementById('system-info').innerHTML = infoHtml;
  
  // Storage
  const vdbPoints = statusData?.vdb_points || 0;
  const codingTotal = codingCountData?.total || 0;
  const total = vdbPoints + codingTotal;
  
  const storageHtml = `
    <div class="mb-4">
      <div class="flex justify-between mb-2">
        <div class="text-sm">VDB Points</div>
        <div class="text-sm font-mono">${vdbPoints.toLocaleString()}</div>
      </div>
    </div>
    <div class="mb-4">
      <div class="flex justify-between mb-2">
        <div class="text-sm">Coding Memories</div>
        <div class="text-sm font-mono">${codingTotal.toLocaleString()}</div>
      </div>
    </div>
    <div class="mb-4">
      <div class="flex justify-between mb-2">
        <div class="text-sm">Total Memories</div>
        <div class="text-sm font-mono">${total.toLocaleString()}</div>
      </div>
    </div>
    <div>
      <div class="flex justify-between mb-2">
        <div class="text-sm">Disk Usage</div>
        <div class="text-sm font-mono">${storageData?.files ? Object.values(storageData.files).join(', ') : '—'}</div>
      </div>
    </div>
  `;
  
  document.getElementById('storage-info').innerHTML = storageHtml;
  
  // Components health
  const components = [
    { name: 'Vector Database', status: statusData?.vdb },
    { name: 'Embedding Service', status: statusData?.embed },
    { name: 'LLM Service', status: statusData?.llm }
  ];
  
  const healthHtml = components.map(c => `
    <div class="health-item">
      <div class="text-sm">${c.name}</div>
      <div class="health-status">
        <div class="status-dot ${c.status === 'ok' ? '' : 'error'}"></div>
        <span style="color: ${c.status === 'ok' ? 'var(--green)' : 'var(--red)'}">${c.status === 'ok' ? 'Healthy' : 'Error'}</span>
      </div>
    </div>
  `).join('');
  
  document.getElementById('components-health').innerHTML = healthHtml;
  
  // System status
  const allOk = components.every(c => c.status === 'ok');
  const statusHtml = `
    <div class="flex items-center gap-3 mb-3">
      <div class="status-dot ${allOk ? '' : 'degraded'}"></div>
      <div class="text-lg font-semibold" style="color: ${allOk ? 'var(--green)' : 'var(--accent)'}">
        ${allOk ? 'OPERATIONAL' : 'DEGRADED'}
      </div>
    </div>
    <div class="text-sm text-muted">${allOk ? 'All systems are running normally.' : 'Some components are experiencing issues.'}</div>
  `;
  
  document.getElementById('system-status').innerHTML = statusHtml;
  
  // Radar chart
  renderRadarChart(components);
  
  // Component details
  const detailsHtml = components.map(c => `
    <div class="panel mb-4">
      <div class="panel-title mb-3">${c.name.toUpperCase()}</div>
      <div class="kv-list">
        <div class="kv-item">
          <div class="kv-label">Status</div>
          <div class="kv-value" style="color: ${c.status === 'ok' ? 'var(--green)' : 'var(--red)'}">${c.status}</div>
        </div>
        ${c.name === 'Vector Database' ? `
          <div class="kv-item">
            <div class="kv-label">Provider</div>
            <div class="kv-value">${statusData?.vdb_provider || '—'}</div>
          </div>
          <div class="kv-item">
            <div class="kv-label">Collection</div>
            <div class="kv-value">${statusData?.vdb_collection || '—'}</div>
          </div>
          <div class="kv-item">
            <div class="kv-label">Points</div>
            <div class="kv-value">${statusData?.vdb_points || '—'}</div>
          </div>
        ` : ''}
        ${c.name === 'Embedding Service' ? `
          <div class="kv-item">
            <div class="kv-label">Dimensions</div>
            <div class="kv-value">${statusData?.embed_dims || '—'}</div>
          </div>
        ` : ''}
      </div>
    </div>
  `).join('');
  
  document.getElementById('component-details').innerHTML = detailsHtml;
  
  // Configuration
  const configHtml = `
    <div class="kv-list">
      <div class="kv-item">
        <div class="kv-label">HY_MEMORY_BASE</div>
        <div class="kv-value">http://127.0.0.1:19527</div>
      </div>
      <div class="kv-item">
        <div class="kv-label">BIND_HOST</div>
        <div class="kv-value">127.0.0.1</div>
      </div>
      <div class="kv-item">
        <div class="kv-label">BIND_PORT</div>
        <div class="kv-value">8765</div>
      </div>
      <div class="kv-item">
        <div class="kv-label">REFRESH_S</div>
        <div class="kv-value">${REFRESH_S}</div>
      </div>
      <div class="kv-item">
        <div class="kv-label">User IDs</div>
        <div class="kv-value">${USER_IDS.join(', ')}</div>
      </div>
    </div>
  `;
  
  document.getElementById('config-info').innerHTML = configHtml;
  
  // Diagnostics
  const diagnostics = {
    status: statusData,
    info: infoData,
    storage: storageData,
    metrics: metricsData,
    codingCount: codingCountData
  };
  
  document.getElementById('diagnostics-json').textContent = JSON.stringify(diagnostics, null, 2);
}

function renderRadarChart(components) {
  const ctx = document.getElementById('radar-chart').getContext('2d');
  
  const activeLayers = getActiveLayerCount();
  const layerScore = (activeLayers / 8) * 100; // 8 total layers
  const uptimeHours = (metricsData?.uptime_seconds || 0) / 3600;
  const uptimeScore = Math.min(uptimeHours / 24 * 100, 100);
  
  const data = {
    labels: ['VDB Health', 'Embed Health', 'LLM Health', 'Layer Coverage', 'Uptime'],
    datasets: [{
      label: 'System Health',
      data: [
        components[0].status === 'ok' ? 100 : 0,
        components[1].status === 'ok' ? 100 : 0,
        components[2].status === 'ok' ? 100 : 0,
        layerScore,
        uptimeScore
      ],
      backgroundColor: 'rgba(74, 222, 128, 0.2)',
      borderColor: '#4ade80',
      borderWidth: 2,
      pointBackgroundColor: '#4ade80',
      pointBorderColor: '#fff',
      pointHoverBackgroundColor: '#fff',
      pointHoverBorderColor: '#4ade80'
    }]
  };
  
  if (radarChart) {
    radarChart.destroy();
  }
  
  radarChart = new Chart(ctx, {
    type: 'radar',
    data,
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false }
      },
      scales: {
        r: {
          beginAtZero: true,
          max: 100,
          grid: { color: '#1a1a1a' },
          angleLines: { color: '#1a1a1a' },
          pointLabels: { color: '#666666', font: { size: 10 } },
          ticks: { display: false }
        }
      }
    }
  });
}

function formatUptime(seconds) {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const mins = Math.floor((seconds % 3600) / 60);
  return `${days}d ${hours}h ${mins}m`;
}

// Right Sidebar
function updateRightSidebar(page) {
  if (page === 'overview') {
    renderOverviewSidebar();
  } else if (page === 'observatory') {
    // Field note shown on node click
  } else if (page === 'explore') {
    // Memory detail shown on result click
  } else if (page === 'layers') {
    const layerCounts = {};
    allMemories.forEach(m => {
      layerCounts[m.layer] = (layerCounts[m.layer] || 0) + 1;
    });
    renderLayerHierarchy(layerCounts);
  } else if (page === 'today') {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const todayMemories = allMemories.filter(m => new Date(m.gmt_created * 1000) >= today);
    renderTodaySummary(todayMemories);
  } else if (page === 'system') {
    // System page has its own layout
    document.getElementById('right-sidebar').innerHTML = '';
  }
}

// Recent Ingestion tab state: 'all' | 'vdb' | 'coding' | 'l1_raw'
let recentIngestionTab = 'all';

function renderOverviewSidebar() {
  // Apply the active Recent Ingestion tab filter.
  // 'vdb'      = durable L2/L3/L4/L0 (the user-facing "memory" store)
  // 'coding'   = coding pipeline extracts (user_id === 'coding')
  // 'l1_raw'   = raw conversation snippets (the input layer)
  // 'l5'       = L5 knowledge graph entities (derivations, not real ingestions)
  // 'all'      = everything merged, sorted newest first
  let recent;
  if (recentIngestionTab === 'vdb') {
    recent = allMemories.filter(m => m.user_id !== 'coding' && m.layer !== 'l5_knowledge');
  } else if (recentIngestionTab === 'coding') {
    recent = allMemories.filter(m => m.user_id === 'coding');
  } else if (recentIngestionTab === 'l1_raw') {
    recent = allMemories.filter(m => m.layer === 'l1_raw');
  } else if (recentIngestionTab === 'l5') {
    recent = allMemories.filter(m => m.layer === 'l5_knowledge');
  } else {
    // 'all' — VDB + coding + l1_raw, but NOT l5 (L5 is a derivation, not an ingestion)
    recent = allMemories.filter(m => m.layer !== 'l5_knowledge');
  }
  // Prefer gmt_updated over gmt_created for the "ago" text — recent
  // UPDATEs of older memories should show as recent (otherwise the
  // "Last ingestion" timer looks stale when only UPDATEs are happening).
  const sortKey = m => m.gmt_updated || m.gmt_created || 0;
  recent.sort((a, b) => sortKey(b) - sortKey(a));
  recent = recent.slice(0, 10);

  // Counts for each tab so the user can see what's available.
  const tabCounts = {
    all:    allMemories.length,
    vdb:    allMemories.filter(m => m.user_id !== 'coding' && m.layer !== 'l5_knowledge').length,
    coding: allMemories.filter(m => m.user_id === 'coding').length,
    l1_raw: allMemories.filter(m => m.layer === 'l1_raw').length,
  };
  const tabBtn = (id, label) => {
    const isActive = recentIngestionTab === id;
    const cnt = tabCounts[id] || 0;
    return `<button class="ingest-tab ${isActive ? 'active' : ''}" data-tab="${id}">${label} <span class="ingest-tab-count">${cnt.toLocaleString()}</span></button>`;
  };

  const html = `
    <div class="right-section">
      <div class="right-section-title">RECENT INGESTION</div>
      <div class="ingest-tabs">
        ${tabBtn('all',    'All')}
        ${tabBtn('vdb',    'VDB')}
        ${tabBtn('coding', 'Coding')}
        ${tabBtn('l1_raw', 'L1_RAW')}
      </div>
      ${recent.length === 0 ? '<div class="text-xs text-muted" style="margin-top: 12px;">No recent memories in this filter</div>' : recent.map(m => {
        const title = (m.content || '').substring(0, 50) + '...';
        const ts = sortKey(m);
        const ago = Math.floor((Date.now() / 1000 - ts) / 60);
        const agoText = ago < 1 ? 'just now' : (ago < 60 ? `${ago}m ago` : `${Math.floor(ago / 60)}h ago`);
        const wasUpdated = m.gmt_updated && m.gmt_created && (m.gmt_updated - m.gmt_created > 60);
        const titleAttr = wasUpdated
          ? `Created ${new Date(m.gmt_created * 1000).toLocaleString()} • Updated ${new Date(m.gmt_updated * 1000).toLocaleString()}`
          : `Created ${new Date(m.gmt_created * 1000).toLocaleString()}`;

        return `
          <div class="ingestion-item">
            <div class="ingestion-title" title="${escapeHtml(titleAttr)}">${escapeHtml(title)}</div>
            <div class="ingestion-meta">
              <span class="badge badge-layer layer-${m.layer || 'l2_fact'}" style="font-size: 9px; padding: 2px 6px;">${m.layer || '—'}</span>
              <span>${agoText}${wasUpdated ? ' <span style="color:#888;" title="Memory was updated, not created">⟳</span>' : ''}</span>
            </div>
          </div>
        `;
      }).join('')}
    </div>
    
    <div class="right-section">
      <div class="right-section-title">MEMORY INSIGHT</div>
      <div class="text-sm">
        <div class="mb-2">Most active layer: <span class="font-mono">${getMostActiveLayer()}</span></div>
        <div class="mb-2">Total memories: <span class="font-mono">${allMemories.length}</span></div>
        <div class="mb-2">Active layers: <span class="font-mono">${getActiveLayerCount()}</span></div>
      </div>
    </div>
  `;
  
  document.getElementById('right-sidebar').innerHTML = html;
}

function getMostActiveLayer() {
  const counts = {};
  allMemories.forEach(m => {
    counts[m.layer] = (counts[m.layer] || 0) + 1;
  });
  return Object.entries(counts).sort((a, b) => b[1] - a[1])[0]?.[0] || '—';
}

function getMostCommonTag() {
  const counts = {};
  allMemories.forEach(m => {
    (m.tags || []).forEach(t => {
      counts[t] = (counts[t] || 0) + 1;
    });
  });
  return Object.entries(counts).sort((a, b) => b[1] - a[1])[0]?.[0] || '—';
}

// Utilities
function debounce(fn, delay) {
  let timeout;
  return (...args) => {
    clearTimeout(timeout);
    timeout = setTimeout(() => fn(...args), delay);
  };
}

// Init
loadAllData().then(() => {
  updateRightSidebar('overview');
  enterPage('overview');
  hideBootScreen();
});

// Auto-refresh
setInterval(loadAllData, REFRESH_S * 1000);

