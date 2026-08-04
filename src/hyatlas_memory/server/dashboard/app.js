// Global state
const REFRESH_S = window.REFRESH_S || 30;
const USER_IDS = window.USER_IDS || [];

// Safely convert a possibly-string/null epoch-seconds value to a valid Date
// or null. Migrated L1_RAW entries can have non-numeric gmt_created, which
// produces Invalid Date and crashes .toISOString() / .getTime().
function tsToDate(ts) {
  const n = Number(ts);
  return n > 0 ? new Date(n * 1000) : null;
}
let currentPage = 'overview';
const PROFILE_IDS = ['all', 'default', 'research', 'sentinel', 'work-backend', 'work-frontend', 'trading', 'hestia'];
let currentAgentId = PROFILE_IDS.includes(localStorage.getItem('hyatlas-agent-id'))
  ? localStorage.getItem('hyatlas-agent-id')
  : 'all';
let loadSeq = 0;
let vdbMemories = [];
let codingMemories = [];
let graphNodes = [];
let graphRelations = [];
let activityMemories = [];
let observatoryMemories = [];

let layerCountsData = null;  // display counts: VDB L0-L4 + graph L5-L7 (Memory Composition bar)
let layerHealthData = null;  // per-user/agent counts from /api/layer-health
let l6SchemasData = null;    // sample L6 schemas from /api/l6-schemas
let statusData = null;
let infoData = null;
let storageData = null;
let metricsData = null;
let qualityData = null;
let codingCountData = null;
let loadErrors = [];
let l5Graph = null;  // full response from /api/l5/graph
let activityChart = null;
let radarChart = null;

// Layer definitions
const LAYERS = {
  'l0_basic_info': { name: 'Basic Info', desc: 'Foundational data points and identifiers', color: '#4a6fa5' },
  'l1_raw': { name: 'Raw', desc: 'Unprocessed sensory and contextual inputs', color: '#3d8b8b' },
  'l2_fact': { name: 'Facts', desc: 'Discrete, verifiable pieces of information', color: '#6b4c9a' },
  'l3_summary': { name: 'Summaries', desc: 'Syntheses of multiple facts, events, and observations', color: '#4a6fa5' },
  'l4_identity': { name: 'Identity (retired)', desc: 'Legacy layer — migrated to L2 facts; archive only', color: '#888888' },
  'l5_knowledge': { name: 'Knowledge', desc: 'Consolidated understanding and expertise', color: '#3d8b8b' },
  'l6_schema': { name: 'Schemas', desc: 'Structural patterns and organizational frameworks', color: '#6b4c9a' },
  'l7_intention': { name: 'Intentions', desc: 'Forward-looking goals and commitments (L7)', color: '#d4af37' }
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
  const navEl = document.querySelector(`[data-page="${page}"]`);
  if (navEl) navEl.classList.add('active');

  // Update page sections
  document.querySelectorAll('.page-section').forEach(s => s.classList.remove('active','entered'));
  const newEl=document.getElementById(`page-${page}`);
  if (!newEl) return;
  newEl.classList.add('active');

  // Body class for memory-detail full-width styling (hides right sidebar).
  document.body.classList.toggle('memory-detail-active', page === 'memory-detail');

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
  renderLoadErrors();
}

function renderLoadErrors() {
  document.querySelectorAll('.domain-error').forEach(el => el.remove());
  const domains = {
    overview: ['operations', 'graph'],
    observatory: ['graph'],
    layers: ['graph'],
    today: ['operations'],
    system: ['operations'],
    quality: ['quality'],
    l5: ['graph'],
  };
  const errors = loadErrors.filter(error => (domains[currentPage] || []).includes(error.name));
  const page = document.getElementById(`page-${currentPage}`);
  if (!page || !errors.length) return;
  const banner = document.createElement('div');
  banner.className = 'domain-error';
  banner.style.cssText = 'margin:12px 0;padding:10px 12px;border:1px solid rgba(248,113,113,.45);color:var(--red);background:rgba(248,113,113,.06);';
  banner.textContent = `Live ${errors.map(error => error.name).join(', ')} data is unavailable. Showing the last known values.`;
  page.prepend(banner);
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
  let data = null;
  try {
    data = await resp.json();
  } catch {
    data = null;
  }
  if (!resp.ok) {
    const error = new Error(data?.error || `${resp.status} ${resp.statusText}`);
    error.status = resp.status;
    error.payload = data;
    throw error;
  }
  return data;
}

async function fetchResult(name, task) {
  try {
    return {name, ok: true, data: await task};
  } catch (error) {
    return {name, ok: false, error};
  }
}

function scopedPath(path, agentId = currentAgentId) {
  return scopeQuery(path, agentId);
}

function scopeQuery(path, agentId = currentAgentId) {
  const join = path.includes('?') ? '&' : '?';
  if (!agentId || agentId === 'all') return `${path}${join}agent_id=all`;
  return `${path}${join}agent_id=${encodeURIComponent(agentId)}`;
}

function scopeAgents(agentId = currentAgentId) {
  return agentId === 'all' ? [] : [agentId];
}

function setScopeStatus(text) {
  const el = document.getElementById('scope-status');
  if (el) el.textContent = text;
}

function initAgentSelector() {
  const el = document.getElementById('agent-selector');
  if (!el) return;
  el.value = currentAgentId;
  el.addEventListener('change', async () => {
    const next = PROFILE_IDS.includes(el.value) ? el.value : 'all';
    currentAgentId = next;
    localStorage.setItem('hyatlas-agent-id', next);
    l5State.data = null;
    l5State.scope = null;
    setScopeStatus('Loading…');
    await loadAllData();
  });
  const label = document.getElementById('scope-label');
  if (label) label.textContent = currentAgentId === 'all' ? 'All profiles' : currentAgentId;
}

async function loadAllData() {
  const seq = ++loadSeq;
  const agentId = currentAgentId;
  setScopeStatus(`Loading ${agentId === 'all' ? 'all profiles' : agentId}…`);
  try {
    const [coreResult, opsResult, graphResult, qualityResult] = await Promise.all([
      fetchResult('core', Promise.all([
        fetchJSON('/api/status'),
        fetchJSON('/api/info'),
        fetchJSON(scopedPath('/api/memories?limit=100', agentId)),
        fetchJSON(scopedPath('/api/layer-counts', agentId)),
      ])),
      fetchResult('operations', Promise.all([
        fetchJSON('/api/storage'),
        fetchJSON('/api/metrics?minutes=10080'),
        fetchJSON('/api/coding-count'),
        fetchJSON('/api/coding-memories?limit=500'),
      ])),
      fetchResult('graph', Promise.all([
        fetchJSON(scopedPath('/api/graph-counts', agentId)),
        fetchJSON(scopedPath('/api/layer-health', agentId)),
        fetchJSON(scopedPath('/api/l6-schemas?n=6', agentId)),
        fetchJSON(scopedPath('/api/l5/graph', agentId)),
        fetchJSON(scopedPath('/api/l5/graph?layer=l6_schema&n=500&rels=false', agentId)),
        fetchJSON(scopedPath('/api/l5/graph?layer=l7_intention&n=500&rels=false', agentId)),
      ])),
      fetchResult('quality', fetchJSON(scopedPath('/api/quality-metrics', agentId))),
    ]);
    if (!coreResult.ok) throw coreResult.error;
    const [status, info, memories, layerCounts] = coreResult.data;
    const [storage, metrics, codingCount, codingPayload] = opsResult.ok
      ? opsResult.data
      : [storageData, metricsData, codingCountData, {memories: codingMemories}];
    const [graphCounts, layerHealth, l6Schemas, l5, l6, l7] = graphResult.ok
      ? graphResult.data
      : [layerCountsData?.graph_counts, layerHealthData, l6SchemasData, l5Graph, null, null];
    const quality = qualityResult.ok ? qualityResult.data : qualityData;
    const failed = [opsResult, graphResult, qualityResult].filter(result => !result.ok);

    if (seq !== loadSeq || agentId !== currentAgentId) return false;
    loadErrors = failed;
    l5Graph = l5;
    layerHealthData = layerHealth;
    l6SchemasData = l6Schemas;

    statusData = status;
    infoData = info;
    // Prefer the split payload from /api/layer-counts (vdb/graph/display).
    // Fall back to graph-counts only if the new fields are missing.
    if (layerCounts && typeof layerCounts === 'object') {
      const displayCounts = layerCounts.display_counts || layerCounts.counts || {};
      const graphFromLayer = layerCounts.graph_counts || {};
      const graphFallback = (graphCounts && typeof graphCounts === 'object') ? {
        l5_knowledge: graphCounts.l5_knowledge || 0,
        l6_schema: graphCounts.l6_schema || 0,
        l7_intention: graphCounts.l7_intention || 0,
      } : {};
      const graph = Object.keys(graphFromLayer).length ? graphFromLayer : graphFallback;
      const counts = {...displayCounts};
      // Ensure L5-L7 display uses graph canonical values.
      ['l5_knowledge', 'l6_schema', 'l7_intention'].forEach(k => {
        if (graph[k] != null) counts[k] = graph[k];
      });
      const displayTotal = Object.values(counts).reduce((a, b) => a + (Number(b) || 0), 0);
      layerCountsData = {
        ...layerCounts,
        counts,
        display_counts: counts,
        display_total: displayTotal,
        total: displayTotal,
        vdb_counts: layerCounts.vdb_counts || null,
        vdb_total: layerCounts.vdb_total != null ? layerCounts.vdb_total : null,
        graph_counts: graph,
        graph_total: Object.values(graph).reduce((a, b) => a + (Number(b) || 0), 0),
        relation_count: layerCounts.relation_count != null
          ? layerCounts.relation_count
          : (graphCounts && graphCounts.relation_count),
      };
    } else if (graphCounts && typeof graphCounts === 'object') {
      const counts = {
        l5_knowledge: graphCounts.l5_knowledge || 0,
        l6_schema: graphCounts.l6_schema || 0,
        l7_intention: graphCounts.l7_intention || 0,
      };
      const total = Object.values(counts).reduce((a, b) => a + (Number(b) || 0), 0);
      layerCountsData = {
        counts,
        display_counts: counts,
        display_total: total,
        total,
        graph_counts: counts,
        graph_total: total,
        relation_count: graphCounts.relation_count,
      };
    } else {
      layerCountsData = null;
    }

    // Normalize coding memories to the VDB memory shape so the existing
    // Today-page tabs / filters / sort work without further changes.
    const codingMems = opsResult.ok ? (codingPayload.memories || []).map(cm => ({
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
    })) : codingMemories;

    // Normalize graph nodes only for Observatory rendering. Their timestamps
    // remain the real Kuzu creation time and never enter ingestion/activity.
    const graphMems = [
      ...(l5Graph?.nodes || []),
      ...(l6?.nodes || []),
      ...(l7?.nodes || []),
    ].map(n => {
      const ts = Math.floor(new Date(n.created_at || 0).getTime() / 1000) || 0;
      const rawLayer = (n.layer || '').toLowerCase();
      const layerMap = {
        'l5_knowledge': 'l5_knowledge',
        'l6_schema': 'l6_schema',
        'l7_intention': 'l7_intention',
      };
      return {
        memory_id:        'graph_' + n.node_id,
        user_id:          'graph',
        agent_id:         n.agent_id || agentId || 'default',
        layer:            layerMap[rawLayer] || 'l5_knowledge',
        content:          n.name,
        gmt_created:      ts,
        gmt_updated:      ts,
        score:            null,
        session_id:       'graph',
        confidence:       n.confidence || 0.95,
        entity_type:      n.entity_type,
        mention_count:    n.mention_count || 1,
        aliases:          n.aliases || [],
        _source:          'kuzu_graph',
      };
    });

    vdbMemories = memories.memories || [];
    codingMemories = codingMems;
    graphNodes = graphMems;
    graphRelations = l5Graph?.relations || [];
    activityMemories = [...vdbMemories, ...codingMemories];
    observatoryMemories = [...vdbMemories, ...graphNodes];


    storageData = storage;
    metricsData = metrics;
    qualityData = quality;
    codingCountData = codingCount;

    renderAll();
    renderLoadErrors();
    if (sceneInitialized && currentPage === 'observatory') updateGraph(obsCurrentScope);
    if (currentPage === 'explore') performSearch();
    if (currentPage === 'l5') initL5Page();
    updateGlobalStatus();
    const label = document.getElementById('scope-label');
    if (label) label.textContent = agentId === 'all' ? 'All profiles' : agentId;
    setScopeStatus(failed.length
      ? `Updated with stale ${failed.map(result => result.name).join(', ')} data`
      : `Updated ${new Date().toLocaleTimeString()}`);
    return true;
  } catch (err) {
    console.error('Failed to load data:', err);
    if (seq === loadSeq) {
      loadErrors = [{name: 'core', error: err}];
      setScopeStatus('Core refresh failed');
    }
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
  
  const coreOk = statusData.vdb === 'ok' && statusData.embed === 'ok';
  const allOk = coreOk && statusData.llm === 'ok';

  if (allOk) {
    dot.className = 'status-dot';
    text.textContent = 'OPERATIONAL';
    text.style.color = 'var(--green)';
  } else if (coreOk) {
    dot.className = 'status-dot degraded';
    text.textContent = 'LIMITED';
    text.style.color = 'var(--accent)';
  } else {
    dot.className = 'status-dot error';
    text.textContent = 'BROKEN';
    text.style.color = 'var(--red)';
  }
  
  const lastMemory = vdbMemories.length > 0
    ? vdbMemories.reduce((latest, m) => {
        const ts = m.gmt_created || 0;
        return ts > (latest.gmt_created || 0) ? m : latest;
      }, vdbMemories[0])
    : null;

  if (lastMemory && lastMemory.gmt_created) {
    const ts = Number(lastMemory.gmt_created);
    if (!Number.isFinite(ts) || ts <= 0) {
      meta.textContent = 'Last memory: —';
    } else {
      const ago = Math.floor((Date.now() / 1000 - ts) / 60);
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
  renderQuality();
  updateRightSidebar(currentPage);
}

// Overview Page
function renderOverview() {
  // Named metrics — never mix VDB points, display total, and graph nodes silently.
  const vdbPoints = getVdbPoints();
  const displayTotal = getLayerTotal();
  const totalLinks = getGraphRelationCount();
  const lc = (layerCountsData && (layerCountsData.display_counts || layerCountsData.counts))
    ? (layerCountsData.display_counts || layerCountsData.counts)
    : {};
  // L4 is retired; don't count it as healthy coverage.
  const coverageKeys = Object.keys(lc).filter(k => k !== 'l4_identity');
  const activeLayers = coverageKeys.filter(k => Number(lc[k]) > 0).length;
  const totalLayers = 7;

  // Stat cards
  const statsHtml = `
    <div class="stat-card">
      <div class="stat-label">VDB POINTS</div>
      <div class="stat-value">${fmtCount(vdbPoints)}</div>
      <div class="text-xs text-muted mt-1">zvec raw points</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">GRAPH RELATIONS</div>
      <div class="stat-value">${fmtCount(totalLinks)}</div>
      <div class="text-xs text-muted mt-1">Kuzu edges</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">DISPLAY TOTAL</div>
      <div class="stat-value">${fmtCount(displayTotal)}</div>
      <div class="text-xs text-muted mt-1">L0-L4 VDB + L5-L7 graph</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">LAYER COVERAGE</div>
      <div class="stat-value">${activeLayers}<span style="color: var(--muted); font-size: 20px;">/${totalLayers}</span></div>
      <div class="text-xs text-muted mt-1">L4 retired</div>
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
  const counts = layerCountsData?.counts;
  if (counts) return Object.values(counts).filter(c => Number(c) > 0).length;
  const layers = new Set(observatoryMemories.map(m => m.layer).filter(Boolean));
  return layers.size;
}

function getLayerTotal() {
  const total = Number(layerCountsData?.display_total ?? layerCountsData?.total);
  if (Number.isFinite(total) && total > 0) return total;
  return observatoryMemories.length;
}

function getVdbPoints() {
  const fromLayer = Number(layerCountsData?.vdb_total);
  if (currentAgentId !== 'all' && Number.isFinite(fromLayer) && fromLayer >= 0) return fromLayer;
  const fromStatus = Number(statusData?.vdb_points);
  if (Number.isFinite(fromStatus) && fromStatus > 0) return fromStatus;
  const fromStorage = Number(storageData?.vdb?.points);
  if (Number.isFinite(fromStorage) && fromStorage > 0) return fromStorage;
  if (Number.isFinite(fromLayer) && fromLayer >= 0) return fromLayer;
  return 0;
}

function getGraphRelationCount() {
  const fromLayer = Number(layerCountsData?.relation_count);
  if (Number.isFinite(fromLayer) && fromLayer > 0) return fromLayer;
  if (l5Graph && Array.isArray(l5Graph.relations)) return l5Graph.relations.length;
  if (l5Graph && Array.isArray(l5Graph.edges)) return l5Graph.edges.length;
  return 0;
}

function fmtCount(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  return n.toLocaleString();
}

function renderCompositionBar() {
  // Prefer split display counts from /api/layer-counts (VDB L0-L4 + graph L5-L7).
  // Always recompute total from the counts object so percentages cannot drift.
  const lc = (typeof layerCountsData === 'object' && layerCountsData) ? layerCountsData : null;

  let layerCounts, total;
  if (lc && (lc.display_counts || lc.counts)) {
    layerCounts = lc.display_counts || lc.counts;
    total = Object.values(layerCounts).reduce((a, b) => a + (Number(b) || 0), 0);
  } else {
    // Fallback to the sample if the endpoint failed for any reason.
    layerCounts = {};
    observatoryMemories.forEach(m => { layerCounts[m.layer] = (layerCounts[m.layer] || 0) + 1; });
    total = observatoryMemories.length;
  }

  let html = '<div class="tag-bar">';

  // L5-L7 are graph-canonical (Kuzu). L0-L4 are VDB-canonical (zvec).
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
  const vdbPoints = getVdbPoints();
  const displayTotal = getLayerTotal();
  const codingTotal = Number(codingCountData?.total) || 0;
  const vdbOk = vdbPoints > 0 || statusData?.vdb === 'ok';
  const embedOk = statusData?.embed === 'ok';
  const llmOk = statusData?.llm === 'ok';
  const statusText = vdbOk
    ? (embedOk && llmOk ? 'System ready (all services online)' : 'Memory readable; capture/digest limited')
    : 'Memory store unavailable';

  const html = `
    <div class="flex items-center gap-3 mb-3">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2">
        <path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>
      </svg>
      <div>
        <div class="text-sm font-mono">${fmtCount(vdbPoints)} VDB · ${fmtCount(displayTotal)} display</div>
        <div class="text-xs text-muted">${statusText}</div>
      </div>
    </div>
    <div class="text-xs text-muted mt-2">VDB: ${fmtCount(vdbPoints)} • Coding: ${fmtCount(codingTotal)}</div>
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
  
  activityMemories.forEach(m => {
    const ts = Number(m.gmt_created);
    if (ts > 0) {
      const date = new Date(ts * 1000).toISOString().split('T')[0];
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
  const vdbPoints = getVdbPoints();
  const displayTotal = getLayerTotal();
  const activeLayers = getActiveLayerCount();
  const codingTotal = Number(codingCountData?.total) || 0;

  const html = `
    <div class="stat-card">
      <div class="stat-label">VDB POINTS</div>
      <div class="stat-value">${fmtCount(vdbPoints)}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">DISPLAY TOTAL</div>
      <div class="stat-value">${fmtCount(displayTotal)}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">LAYERS ACTIVE</div>
      <div class="stat-value">${activeLayers}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">CODING MEMORIES</div>
      <div class="stat-value">${fmtCount(codingTotal)}</div>
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
    document.getElementById('results-count').textContent = 'Searching…';
    document.getElementById('search-results').innerHTML = '<div class="text-muted">Searching memories…</div>';
    const layer = document.getElementById('filter-layer').value;
    const days = Number(document.getElementById('filter-time').value) || 0;
    const sort = document.getElementById('sort-by').value;
    const readers = {
      semantic: 'legacy',
      keyword: 'hybrid_tag',
      hybrid: 'hybrid_v2',
    };
    const body = {
      query,
      user_ids: USER_IDS,
      agent_ids: scopeAgents(),
      reader: readers[searchMode] || 'legacy',
      limit: 20,
    };
    if (days) body.created_after = Date.now() / 1000 - days * 86400;

    const resp = await fetchJSON('/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });

    searchResults = [
      ...(resp.memories?.profile || []),
      ...(resp.memories?.proactive || []),
      ...(resp.memories?.normal || [])
    ];
    if (layer) searchResults = searchResults.filter(m => m.layer === layer);
    if (days) {
      const since = body.created_after;
      searchResults = searchResults.filter(m => Number(m.gmt_created || 0) >= since);
    }
    if (sort === 'recent') {
      searchResults.sort((a, b) => Number(b.gmt_created || 0) - Number(a.gmt_created || 0));
    } else {
      searchResults.sort((a, b) => Number(b.score || 0) - Number(a.score || 0));
    }

    renderSearchResults();
  } catch (err) {
    console.error('Search failed:', err);
    searchResults = [];
    renderSearchResults();
    document.getElementById('search-results').innerHTML =
      `<div class="text-muted">Search failed: ${escapeHtml(err.message || String(err))}</div>`;
  }
}

function renderSearchResults() {
  document.getElementById('results-count').textContent = `RESULTS (${searchResults.length})`;
  if (!searchResults.length) {
    document.getElementById('search-results').innerHTML = '<div class="text-muted">No memories matched the current query and filters.</div>';
    return;
  }

  const html = searchResults.map((m, i) => {
    const title = (m.content || '').substring(0, 60) + '...';
    const snippet = (m.content || '').substring(0, 100) + '...';
    const score = m.score?.toFixed(2) || '—';
    const scoreLabel = searchMode === 'keyword'
      ? 'keyword/hybrid score'
      : searchMode === 'hybrid'
        ? 'hybrid retrieval score'
        : 'semantic similarity score';
    const tagCount = (m.tags || []).length;

    return `
      <div class="search-result" data-index="${i}">
        <div class="flex justify-between items-start mb-2">
          <span class="badge badge-layer layer-${m.layer}">${m.layer}</span>
          <span class="font-mono text-xs text-muted" title="${scoreLabel}">${score}</span>
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
      enterMemoryDetail(searchResults[idx].memory_id);
      document.querySelectorAll('.search-result').forEach(r => r.classList.remove('selected'));
      el.classList.add('selected');
    });
  });
}

function showMemoryDetail(memory) {
  // Legacy panel renderer — kept so any out-of-tree code that still calls it
  // (e.g. tests, embedded previews) renders into the right sidebar as before.
  // New click flows route through enterMemoryDetail() which navigates to the
  // dedicated memory-detail page instead.
  const title = (memory.content || '');
  const tagCounts = {};
  (memory.tags || []).forEach(tag => {
    tagCounts[tag] = vdbMemories.filter(m => (m.tags || []).includes(tag)).length;
  });

  const imp = typeof memory.importance === 'number' ? memory.importance : null;
  const impCls = imp === null ? '' : imp >= 0.7 ? 'importance-high' : imp >= 0.4 ? 'importance-mid' : 'importance-low';
  const impBadge = imp === null ? '—' : `<span class="badge badge-importance ${impCls}">★ ${imp.toFixed(2)}</span>`;
  const acc = typeof memory.access_count === 'number' ? memory.access_count : '—';

  const html = `
    <div class="memory-detail">
      <div class="text-xs text-muted mb-2">MEMORY DETAIL</div>
      <div class="flex gap-2 mb-3" style="flex-wrap: wrap; align-items: center;">
        <span class="badge badge-layer layer-${memory.layer}">${memory.layer || '—'}</span>
        ${impBadge}
        <span class="badge badge-importance" title="Times this memory has been recalled">↻ ${acc}</span>
      </div>

      <div class="text-xs text-muted font-mono mb-3">id: ${memory.memory_id}</div>
      <div class="text-xs text-muted mb-4">${tsToDate(memory.gmt_created)?.toLocaleString() ?? '—'}</div>

      <div class="text-sm mb-4" style="white-space: pre-wrap; word-break: break-word;">${escapeHtml(title)}</div>

      <div class="mb-3">
        <div class="text-xs text-muted mb-1">SCORING (4-factor)</div>
        <div class="text-xs font-mono">semantic 0.50 · recency 0.30 · importance ${imp === null ? '—' : imp.toFixed(2) + ' × 0.15'} · access ${acc} × 0.05</div>
      </div>

      ${(memory.user_id || memory.session_id) ? `
      <div class="mb-3">
        <div class="text-xs text-muted mb-1">PROVENANCE</div>
        ${memory.user_id ? `<div class="text-xs font-mono">user: ${memory.user_id}</div>` : ''}
        ${memory.session_id ? `<div class="text-xs font-mono">session: ${memory.session_id}</div>` : ''}
      </div>` : ''}

      ${(memory.tags || []).length > 0 ? `
      <div class="mb-3">
        <div class="text-xs text-muted mb-1">TAGS</div>
        ${(memory.tags || []).map(t => `<span class="badge badge-tag">${t}</span>`).join(' ')}
      </div>` : ''}
    </div>
  `;

  document.getElementById('right-sidebar').innerHTML = html;
}

// ---------------------------------------------------------------------------
// Memory Detail page
//
// Clicking a memory card anywhere in the dashboard (recent ingestion, today
// timeline, explore search, L5 graph, etc.) navigates to a dedicated page
// rather than replacing the right sidebar. The URL gets a `?memory=<id>`
// query param so refresh / shared links preserve state.
//
// Back behavior:
//   - The in-page "Back" button returns to whichever page the user came
//     from (Overview / Today / Explore / L5 / …) and clears the query.
//   - The browser Back button does the same via the popstate listener.
// ---------------------------------------------------------------------------

// Page we came from when entering the memory-detail page; used by exitMemoryDetail().
let memoryDetailReturnPage = 'overview';

// Render the full memory detail into the dedicated page container.
// Includes layer, importance, access_count, full (untruncated) content,
// 4-factor scoring breakdown, provenance, tags, timestamps.
function renderMemoryDetailPage(memory) {
  const content = document.getElementById('memory-detail-content');
  const titleEl = document.getElementById('memory-detail-title');
  const subEl   = document.getElementById('memory-detail-subtitle');
  if (!content || !titleEl || !subEl) return;

  const fullContent = memory.content || '';
  const preview = fullContent.length > 80 ? fullContent.substring(0, 80) + '…' : fullContent;
  titleEl.textContent = preview || '(empty content)';
  titleEl.title = fullContent;
  const layerInfo = (memory.layer && LAYERS[memory.layer]) ? LAYERS[memory.layer] : null;
  const layerLabel = layerInfo ? layerInfo.name : (memory.layer || 'unknown');
  subEl.textContent  = `${layerLabel}${memory.user_id ? ' · ' + memory.user_id : ''}`;

  const imp = typeof memory.importance === 'number' ? memory.importance : null;
  const impCls = imp === null ? '' : imp >= 0.7 ? 'importance-high' : imp >= 0.4 ? 'importance-mid' : 'importance-low';
  const impBadge = imp === null
    ? `<span class="badge badge-importance">—</span>`
    : `<span class="badge badge-importance ${impCls}" title="Importance (4-factor scorer, weight 0.15)">★ ${imp.toFixed(2)}</span>`;
  const acc = typeof memory.access_count === 'number' ? memory.access_count : null;
  const accBadge = acc === null
    ? `<span class="badge badge-importance" title="Access count not available">↻ —</span>`
    : `<span class="badge badge-importance" title="Times this memory has been recalled (4-factor scorer, weight 0.05)">↻ ${acc.toLocaleString()}</span>`;

  // Compute the four 4-factor components from available fields. This is the
  // best-effort explanation surfaced to the user; the backend may use a
  // different runtime scorer, but the *weights* (0.50/0.30/0.15/0.05) and the
  // *shape* are stable.
  const semantic = 0.50;
  const recency = 0.30;
  const importanceWeight = 0.15;
  const accessWeight = 0.05;
  const impVal = imp === null ? null : imp.toFixed(2);
  const accVal = acc === null ? '—' : String(acc);

  const createdTs = tsToDate(memory.gmt_created);
  const updatedTs = memory.gmt_updated && (!memory.gmt_created || (Number(memory.gmt_updated) - Number(memory.gmt_created) > 60))
    ? tsToDate(memory.gmt_updated) : null;
  const createdStr = createdTs ? createdTs.toLocaleString() : '—';
  const updatedStr = updatedTs ? updatedTs.toLocaleString() : null;

  const tagsHtml = (memory.tags && memory.tags.length > 0)
    ? `
      <div class="memory-detail-section">
        <div class="memory-detail-section-title">TAGS</div>
        <div class="flex gap-2" style="flex-wrap: wrap;">${(memory.tags || []).map(t => `<span class="badge badge-tag">${escapeHtml(t)}</span>`).join('')}</div>
      </div>`
    : '';

  const provenanceHtml = (memory.user_id || memory.session_id || memory.agent_id || memory.workspace_id || memory.branch)
    ? `
      <div class="memory-detail-section">
        <div class="memory-detail-section-title">PROVENANCE</div>
        ${memory.user_id     ? `<div class="kv-row"><div class="kv-label">user</div><div class="kv-value font-mono">${escapeHtml(memory.user_id)}</div></div>` : ''}
        ${memory.session_id  ? `<div class="kv-row"><div class="kv-label">session</div><div class="kv-value font-mono">${escapeHtml(memory.session_id)}</div></div>` : ''}
        ${memory.agent_id    ? `<div class="kv-row"><div class="kv-label">agent</div><div class="kv-value font-mono">${escapeHtml(memory.agent_id)}</div></div>` : ''}
        ${memory.workspace_id ? `<div class="kv-row"><div class="kv-label">workspace</div><div class="kv-value font-mono">${escapeHtml(memory.workspace_id)}</div></div>` : ''}
        ${memory.branch      ? `<div class="kv-row"><div class="kv-label">branch</div><div class="kv-value font-mono">${escapeHtml(memory.branch)}</div></div>` : ''}
      </div>`
    : '';

  const l5Extras = (memory.entity_type || memory.mention_count || memory.aliases)
    ? `
      <div class="memory-detail-section">
        <div class="memory-detail-section-title">L5 ENTITY</div>
        ${memory.entity_type   ? `<div class="kv-row"><div class="kv-label">type</div><div class="kv-value font-mono">${escapeHtml(memory.entity_type)}</div></div>` : ''}
        ${memory.mention_count ? `<div class="kv-row"><div class="kv-label">mention_count</div><div class="kv-value font-mono">${escapeHtml(String(memory.mention_count))}</div></div>` : ''}
        ${(memory.aliases && memory.aliases.length) ? `<div class="kv-row"><div class="kv-label">aliases</div><div class="kv-value font-mono">${escapeHtml(memory.aliases.join(', '))}</div></div>` : ''}
      </div>`
    : '';

  content.innerHTML = `
    <div class="memory-detail-page">
      <div class="memory-detail-meta-row">
        <span class="badge badge-layer layer-${memory.layer || ''}">${escapeHtml(memory.layer || '—')}</span>
        ${impBadge}
        ${accBadge}
        <span class="text-xs text-muted font-mono" title="Memory identifier">id: ${escapeHtml(memory.memory_id)}</span>
      </div>

      <div class="panel memory-detail-content-panel">
        <div class="memory-detail-section-title">CONTENT</div>
        <div class="memory-detail-content-text">${escapeHtml(fullContent)}</div>
      </div>

      <div class="panel">
        <div class="memory-detail-section-title">SCORING (4-factor)</div>
        <div class="scoring-grid">
          <div class="scoring-row">
            <div class="scoring-label">semantic</div>
            <div class="scoring-bar"><div class="scoring-bar-fill" style="width: ${semantic*100}%"></div></div>
            <div class="scoring-value font-mono">${semantic.toFixed(2)} (weight)</div>
          </div>
          <div class="scoring-row">
            <div class="scoring-label">recency</div>
            <div class="scoring-bar"><div class="scoring-bar-fill" style="width: ${recency*100}%"></div></div>
            <div class="scoring-value font-mono">${recency.toFixed(2)} (weight)</div>
          </div>
          <div class="scoring-row">
            <div class="scoring-label">importance</div>
            <div class="scoring-bar"><div class="scoring-bar-fill" style="width: ${(imp === null ? 0 : imp)*100}%"></div></div>
            <div class="scoring-value font-mono">${impVal === null ? '—' : impVal} × ${importanceWeight.toFixed(2)}</div>
          </div>
          <div class="scoring-row">
            <div class="scoring-label">access</div>
            <div class="scoring-bar"><div class="scoring-bar-fill" style="width: ${Math.min(100, (acc === null ? 0 : acc)*10)}%"></div></div>
            <div class="scoring-value font-mono">${accVal} × ${accessWeight.toFixed(2)}</div>
          </div>
        </div>
      </div>

      <div class="panel">
        <div class="memory-detail-section-title">TIMESTAMPS</div>
        <div class="kv-row"><div class="kv-label">created</div><div class="kv-value font-mono">${escapeHtml(createdStr)}</div></div>
        ${updatedStr ? `<div class="kv-row"><div class="kv-label">updated</div><div class="kv-value font-mono">${escapeHtml(updatedStr)}</div></div>` : ''}
      </div>

      ${provenanceHtml}
      ${l5Extras}
      ${tagsHtml}
    </div>
  `;
}

// Navigate to the dedicated memory-detail page for `memoryId`.
// Pushes URL state so refresh / shared links preserve the open memory,
// and remembers the page we came from for the in-page Back button.
function enterMemoryDetail(memoryId) {
  if (!memoryId) return;
  const mem = observatoryMemories.find(m => m.memory_id === memoryId);
  if (!mem) {
    console.warn('enterMemoryDetail: memory not found', memoryId);
    return;
  }

  // Remember where we came from so the Back button returns there
  // (only record if we are not already on the memory-detail page,
  // otherwise we'd overwrite the original return page).
  if (currentPage !== 'memory-detail') {
    memoryDetailReturnPage = currentPage || 'overview';
  }

  // Update URL: ?memory=<id>  (keeps the path the same; refreshable / shareable)
  try {
    const newUrl = new URL(window.location.href);
    newUrl.searchParams.set('memory', memoryId);
    history.pushState(
      { page: 'memory-detail', memoryId, returnPage: memoryDetailReturnPage },
      '',
      newUrl.toString()
    );
  } catch (e) {
    // Older browsers / file:// — fall back to hash
    history.pushState(
      { page: 'memory-detail', memoryId, returnPage: memoryDetailReturnPage },
      '',
      '#memory=' + encodeURIComponent(memoryId)
    );
  }

  // Render the page content BEFORE navigateTo so it is ready when the
  // page-section becomes visible (navigateTo triggers a fade transition).
  renderMemoryDetailPage(mem);

  // Switch to the memory-detail page-section (hides other pages + right sidebar)
  navigateTo('memory-detail');

  // Scroll the content area to top so the back button + title are visible
  const ca = document.querySelector('.content-area');
  if (ca) ca.scrollTop = 0;
}

// Return from the memory-detail page to wherever the user came from.
// Uses replaceState (not pushState) so we don't grow the back stack
// when the in-page Back button is clicked. The browser's native Back
// button still works via the popstate listener below.
function exitMemoryDetail() {
  const returnPage = memoryDetailReturnPage || 'overview';
  try {
    const newUrl = new URL(window.location.href);
    newUrl.searchParams.delete('memory');
    history.replaceState({ page: returnPage }, '', newUrl.toString());
  } catch (e) {
    history.replaceState({ page: returnPage }, '', window.location.pathname);
  }
  navigateTo(returnPage);
}

// Wire the in-page Back button
document.getElementById('memory-detail-back-btn').addEventListener('click', exitMemoryDetail);

// Browser Back / Forward: keep the page in sync with history state.
window.addEventListener('popstate', (e) => {
  const state = e.state || {};
  const params = new URL(window.location.href).searchParams;
  const memId = params.get('memory');

  if (memId) {
    // Restoring a memory-detail entry from history
    const mem = observatoryMemories.find(m => m.memory_id === memId);
    if (mem) {
      memoryDetailReturnPage = state.returnPage || memoryDetailReturnPage || 'overview';
      renderMemoryDetailPage(mem);
      navigateTo('memory-detail');
      return;
    }
  }
  // Otherwise restore whichever top-level page the state points at.
  const target = state.page && state.page !== 'memory-detail' ? state.page : (memoryDetailReturnPage || 'overview');
  if (target !== currentPage) {
    navigateTo(target);
  }
});

// Event delegation for timeline-item and ingestion-item clicks.
// One listener attached at document level handles all dynamically-rendered
// timeline/ingestion items without needing inline onclick attributes.
document.addEventListener('click', (e) => {
  const item = e.target.closest('.timeline-item[data-memory-id], .ingestion-item[data-memory-id]');
  if (!item) return;
  const mid = item.getAttribute('data-memory-id');
  if (mid) enterMemoryDetail(mid);
});

window.__openMemoryDetail = function(memoryId) {
  enterMemoryDetail(memoryId);
};

function showMemoryDetailById(memoryId) {
  // Kept as a thin alias for backwards-compat (tests, external links).
  enterMemoryDetail(memoryId);
}

// On boot, honor ?memory=<id> so a refresh / shared link keeps the
// memory-detail page open. Called by the init code at the bottom of
// app.js after loadAllData() finishes.
function restoreMemoryDetailFromUrl() {
  try {
    const memId = new URL(window.location.href).searchParams.get('memory');
    if (!memId) return;
    const mem = observatoryMemories.find(m => m.memory_id === memId);
    if (!mem) return;
    // Don't push a new history entry — replace current so Back works cleanly.
    history.replaceState(
      { page: 'memory-detail', memoryId: memId, returnPage: memoryDetailReturnPage },
      '',
      window.location.href
    );
    renderMemoryDetailPage(mem);
    navigateTo('memory-detail');
  } catch (e) {
    console.warn('restoreMemoryDetailFromUrl failed', e);
  }
}

// Memory Layers
function renderLayers() {
  // Display counts: VDB L0-L4 + graph L5-L7. Also show dual VDB|Graph for L5-L7.
  const layerCounts = {};
  const layerTagCounts = {};
  const vdbCounts = (layerCountsData && layerCountsData.vdb_counts) || {};
  const graphCountsLocal = (layerCountsData && layerCountsData.graph_counts) || {};
  const liveCounts = (layerCountsData && (layerCountsData.display_counts || layerCountsData.counts)) || null;
  if (liveCounts) {
    Object.entries(liveCounts).forEach(([k, v]) => {
      if (typeof v === 'number') layerCounts[k] = v;
    });
  }
  observatoryMemories.forEach(m => {
    if (!(m.layer in layerCounts)) {
      layerCounts[m.layer] = (layerCounts[m.layer] || 0) + 1;
    }
    const tagCount = (m.tags || []).length;
    layerTagCounts[m.layer] = (layerTagCounts[m.layer] || 0) + tagCount;
  });

  const total = Object.values(layerCounts).reduce((a, b) => a + (Number(b) || 0), 0);
  const graphLayerKeys = new Set(['l5_knowledge', 'l6_schema', 'l7_intention']);

  const rows = Object.entries(LAYERS).map(([key, info]) => {
    const count = Number(layerCounts[key]) || 0;
    const pct = total > 0 ? (count / total * 100).toFixed(1) : '0.0';
    const avgTags = count > 0 ? ((Number(layerTagCounts[key]) || 0) / count).toFixed(1) : '0.0';
    const dual = graphLayerKeys.has(key)
      ? `<div class="text-xs text-muted">VDB ${fmtCount(vdbCounts[key] || 0)} · Graph ${fmtCount(graphCountsLocal[key] || count)}</div>`
      : (key === 'l4_identity' ? `<div class="text-xs text-muted">retired · archive only</div>` : '');
    const source = graphLayerKeys.has(key) ? 'graph' : 'vdb';

    return `
      <tr>
        <td>
          <div class="flex items-center gap-3">
            <div class="layer-indicator layer-${key}" style="background: ${info.color}">${key.split('_')[0].toUpperCase()}</div>
            <div>
              <div class="font-semibold">${info.name}</div>
              <div class="text-xs text-muted">${info.desc} · ${source}</div>
              ${dual}
            </div>
          </div>
        </td>
        <td class="font-mono">${fmtCount(count)}</td>
        <td class="font-mono">${pct}%</td>
        <td class="font-mono">${avgTags}</td>
      </tr>
    `;
  }).join('');

  document.getElementById('layers-tbody').innerHTML = rows;
  document.getElementById('layers-total').textContent = `Display total: ${fmtCount(total)} (L0-L4 VDB + L5-L7 graph)`;
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
  const today = activityMemories.filter(m => {
    const created = tsToDate(m.gmt_created);
    const now = new Date();
    return created && created.toDateString() === now.toDateString();
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

  let filtered = activityMemories.filter(m => {
    const created = tsToDate(m.gmt_created);
    return created && created.getTime() >= since;
  });
  
  if (todayFilter === 'vdb') {
    filtered = filtered.filter(m => m.user_id !== 'coding');
  } else if (todayFilter === 'coding') {
    filtered = filtered.filter(m => m.user_id === 'coding');
  }
  
  filtered.sort((a, b) => b.gmt_created - a.gmt_created);
  
  const html = filtered.slice(0, 20).map(m => {
      const title = (m.content || '');
      const preview = title.length > 100 ? title.substring(0, 100) + '…' : title;
      const ts = Number(m.gmt_created) || 0;
      const time = ts ? new Date(ts * 1000).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' }) : '—';
      const ago = ts ? Math.floor((Date.now() / 1000 - ts) / 60) : 0;
      const agoText = !ts ? '—' : ago < 60 ? `${ago}m ago` : `${Math.floor(ago / 60)}h ago`;

      const imp = typeof m.importance === 'number' ? m.importance : null;
      const impCls = imp === null ? '' : imp >= 0.7 ? 'importance-high' : imp >= 0.4 ? 'importance-mid' : 'importance-low';
      const impBadge = imp === null ? '' : `<span class="badge badge-importance ${impCls}" title="Importance score (4-factor scorer)">★ ${imp.toFixed(2)}</span>`;

      return `
        <div class="timeline-item" data-memory-id="${m.memory_id}" onclick="window.__openMemoryDetail && window.__openMemoryDetail('${m.memory_id}')">
          <div class="timeline-dot"></div>
          <div class="timeline-content">
            <div class="timeline-time">${time} • ${agoText}</div>
            <div class="timeline-title">${escapeHtml(preview)}</div>
            <div class="flex gap-2 mt-2" style="flex-wrap: wrap; align-items: center;">
              <span class="badge badge-layer layer-${m.layer}">${m.layer}</span>
              ${impBadge}
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
  const weekCount = activityMemories.filter(m => m.gmt_created >= weekAgo).length;
  
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
  const lastMemory = vdbMemories.length > 0 ? (tsToDate(vdbMemories[0].gmt_created)?.toLocaleString() ?? '—') : '—';
  
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
    ${layerHealthData ? `
    <div class="kv-item">
      <div class="kv-label">Digest namespace</div>
      <div class="kv-value font-mono text-sm">${escapeHtml(layerHealthData.user_id)} / ${escapeHtml(layerHealthData.agent_id)}</div>
    </div>
    <div class="kv-item">
      <div class="kv-label">Fresh L2 (digest fuel)</div>
      <div class="kv-value">${layerHealthData.fresh_l2_for_digest ?? '—'}</div>
    </div>
    <div class="kv-item">
    <div class="kv-label">Graph L5 / L6 / relations (per agent)</div>
    <div class="kv-value font-mono text-sm">${layerHealthData.graph_layer_counts ? `${layerHealthData.graph_layer_counts.l5_knowledge ?? '—'} / ${layerHealthData.graph_layer_counts.l6_schema ?? '—'} / ${layerHealthData.graph_relation_count ?? '—'}` : '—'}</div>
    </div>
    <div class="kv-item">
    <div class="kv-label">Graph L5 / L6 / relations (global)</div>
    <div class="kv-value font-mono text-sm">${layerHealthData.graph_layer_counts_global ? `${layerHealthData.graph_layer_counts_global.l5_knowledge ?? '—'} / ${layerHealthData.graph_layer_counts_global.l6_schema ?? '—'} / ${layerHealthData.graph_relation_count_global ?? '—'}` : '—'}</div>
    </div>
    <div class="kv-item">
      <div class="kv-label">Digest log</div>
      <div class="kv-value">${escapeHtml(layerHealthData.digest_log_status || '—')}${layerHealthData.digest_log_mtime ? ` · ${new Date(layerHealthData.digest_log_mtime * 1000).toLocaleString()}` : ''}</div>
    </div>
    <div class="kv-item">
      <div class="kv-label">Manual digest</div>
      <div class="kv-value font-mono text-xs break-all">${escapeHtml(layerHealthData.digest_command || '—')}</div>
    </div>
    ` : ''}
    ${l6SchemasData && l6SchemasData.schemas && l6SchemasData.schemas.length ? `
    <div class="kv-item" style="grid-column:1/-1">
      <div class="kv-label">L6 schemas (sample ${l6SchemasData.count} / ${l6SchemasData.graph_l6_total ?? '—'} in graph)</div>
      <ul class="text-sm" style="margin:8px 0 0;padding-left:18px;line-height:1.45">
        ${l6SchemasData.schemas.map(s => `<li style="margin-bottom:8px"><span class="font-mono text-xs text-muted">${escapeHtml((s.node_id || '').slice(0,8))}</span> ${escapeHtml((s.name || '').slice(0,220))}${(s.name || '').length > 220 ? '…' : ''}</li>`).join('')}
      </ul>
    </div>
    ` : ''}
  `;
  
  document.getElementById('system-info').innerHTML = infoHtml;
  
  // Storage — named metrics only
  const vdbPoints = getVdbPoints();
  const displayTotal = getLayerTotal();
  const codingTotal = Number(codingCountData?.total) || 0;
  const graphTotal = Number(layerCountsData?.graph_total) || 0;

  const storageHtml = `
    <div class="mb-4">
      <div class="flex justify-between mb-2">
        <div class="text-sm">VDB Points (zvec raw)</div>
        <div class="text-sm font-mono">${fmtCount(vdbPoints)}</div>
      </div>
    </div>
    <div class="mb-4">
      <div class="flex justify-between mb-2">
        <div class="text-sm">Display total (L0-L4 VDB + L5-L7 graph)</div>
        <div class="text-sm font-mono">${fmtCount(displayTotal)}</div>
      </div>
    </div>
    <div class="mb-4">
      <div class="flex justify-between mb-2">
        <div class="text-sm">Graph nodes (L5-L7)</div>
        <div class="text-sm font-mono">${fmtCount(graphTotal)}</div>
      </div>
    </div>
    <div class="mb-4">
      <div class="flex justify-between mb-2">
        <div class="text-sm">Coding Memories</div>
        <div class="text-sm font-mono">${fmtCount(codingTotal)}</div>
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
  
  const state = c => c.status === 'ok'
    ? { dot: '', color: 'var(--green)', label: 'Healthy' }
    : (c.name === 'LLM Service' && String(c.status || '').match(/rate_limited|warning/i))
      ? { dot: 'degraded', color: 'var(--accent)', label: 'Limited' }
      : { dot: 'error', color: 'var(--red)', label: 'Error' };
  const healthHtml = components.map(c => {
    const s = state(c);
    return `
      <div class="health-item">
        <div class="text-sm">${c.name}</div>
        <div class="health-status">
          <div class="status-dot ${s.dot}"></div>
          <span style="color: ${s.color}">${s.label}</span>
        </div>
      </div>
    `;
  }).join('');

  document.getElementById('components-health').innerHTML = healthHtml;

  // System status
  const coreOk = statusData?.vdb === 'ok' && statusData?.embed === 'ok';
  const allOk = coreOk && statusData?.llm === 'ok';
  const statusHtml = `
    <div class="flex items-center gap-3 mb-3">
      <div class="status-dot ${allOk ? '' : (coreOk ? 'degraded' : 'error')}"></div>
      <div class="text-lg font-semibold" style="color: ${allOk ? 'var(--green)' : (coreOk ? 'var(--accent)' : 'var(--red)')}">
        ${allOk ? 'OPERATIONAL' : (coreOk ? 'LIMITED' : 'BROKEN')}
      </div>
    </div>
    <div class="text-sm text-muted">${allOk ? 'All systems are running normally.' : (coreOk ? 'Memory is readable; capture/digest may be provider-limited.' : 'Core memory components need attention.')}</div>
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
            <div class="kv-value">${fmtCount(getVdbPoints())}</div>
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
  const runtime = storageData?.runtime || {};
  const configHtml = `
    <div class="kv-list">
      <div class="kv-item">
        <div class="kv-label">HY_MEMORY_BASE</div>
        <div class="kv-value">${escapeHtml(runtime.backend || '—')}</div>
      </div>
      <div class="kv-item">
        <div class="kv-label">BIND_HOST</div>
        <div class="kv-value">${escapeHtml(runtime.bind_host || '—')}</div>
      </div>
      <div class="kv-item">
        <div class="kv-label">BIND_PORT</div>
        <div class="kv-value">${runtime.bind_port ?? '—'}</div>
      </div>
      <div class="kv-item">
        <div class="kv-label">REFRESH_S</div>
        <div class="kv-value">${runtime.refresh_seconds ?? REFRESH_S}</div>
      </div>
      <div class="kv-item">
        <div class="kv-label">PLATFORM</div>
        <div class="kv-value">${escapeHtml(runtime.platform || '—')}</div>
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

function renderQuality() {
  const root = qualityData || {};
  const snap = root.snapshot || {};
  const scores = snap.scores || {};
  const guides = root.guides || {};
  const breakdown = snap.score_breakdown || {};
  const llm = snap.llm_tokens_7d || {};
  const graph = snap.graph || {};
  const tips = root.tips || [];
  const glance = root.at_a_glance || {};

  const barRow = (label, value, maxVal = 100, sub = '') => {
    const available = value != null;
    const v = available ? Number(value) : 0;
    const shown = available ? v : 'N/A';
    const pct = available ? Math.max(0, Math.min(100, maxVal ? (v / maxVal) * 100 : v)) : 0;
    return `
      <div class="quality-bar-row">
        <div class="quality-bar-meta">
          <span class="quality-bar-label">${escapeHtml(label)}</span>
          <span class="quality-bar-value">${shown}${available && maxVal && maxVal !== 100 ? ` / ${maxVal}` : ''}</span>
        </div>
        <div class="quality-bar-track"><div class="quality-bar-fill" style="width:${pct}%"></div></div>
        ${sub ? `<div class="quality-bar-sub">${escapeHtml(sub)}</div>` : ''}
      </div>`;
  };

  const trendIcon = (t) => (t === 'up' ? '↑' : t === 'down' ? '↓' : '→');
  const trendClass = (t) => (t === 'up' ? 'trend-up' : t === 'down' ? 'trend-down' : 'trend-flat');

  const vitalsEl = document.getElementById('quality-vitals');
  if (vitalsEl) {
    const overall = scores.composite != null ? scores.composite : '—';
    const grade = glance.grade || '—';
    const health = glance.health_label || '';
    const tone = glance.tone || 'neutral';
    const headline = glance.headline || '';
    const pulse = glance.pulse || [];
    const highlights = glance.highlights || [];
    const visit = glance.since_last_visit;

    const pulseHtml = pulse.map(p => {
      const d = p.delta;
      const deltaStr = d == null ? '' : (d > 0 ? `+${d}` : `${d}`);
      return `<div class="quality-pulse-chip ${trendClass(p.trend)}">
        <div class="quality-pulse-label">${escapeHtml(p.label)}</div>
        <div class="quality-pulse-value">${p.value ?? '—'}${escapeHtml(p.suffix || '')}</div>
        <div class="quality-pulse-meta">${trendIcon(p.trend)} ${escapeHtml(deltaStr)} <span class="text-muted">${escapeHtml(p.context || '')}</span></div>
      </div>`;
    }).join('');

    const hiHtml = highlights.map(h => `
      <li class="quality-highlight"><span class="quality-hi-icon">${escapeHtml(h.icon || '✓')}</span>${escapeHtml(h.text || '')}</li>`).join('');

    let visitLine = '';
    if (visit && visit.composite_delta != null && visit.composite_delta !== 0) {
      const sign = visit.composite_delta > 0 ? '+' : '';
      visitLine = `<p class="quality-visit-note">${sign}${visit.composite_delta} overall ${escapeHtml(visit.label || '')}</p>`;
    }

    vitalsEl.innerHTML = `
      <div class="quality-vitals-grid tone-${tone}">
        <div class="quality-grade-ring">
          <div class="quality-grade-letter">${escapeHtml(grade)}</div>
          <div class="quality-grade-sub">${escapeHtml(health)}</div>
        </div>
        <div class="quality-vitals-main">
          <div class="quality-vitals-score">${overall}<span class="quality-vitals-denom">/100</span></div>
          <p class="quality-vitals-headline">${escapeHtml(headline)}</p>
          ${visitLine}
          <ul class="quality-highlight-list">${hiHtml}</ul>
        </div>
      </div>
      <div class="quality-pulse-row">${pulseHtml}</div>
      <div class="quality-hero-bars quality-hero-bars-compact">
        ${barRow('Evolution', scores.evolution, 100)}
        ${barRow('Activity', scores.activity, 100)}
        ${barRow('Latency', scores.latency, 100)}
      </div>`;
  }

  const weightsEl = document.getElementById('quality-weights');
  if (weightsEl) {
    weightsEl.textContent = breakdown.composite_weights || guides.composite || '';
  }

  const bdEl = document.getElementById('quality-breakdown');
  if (bdEl) {
    const sections = [
      ['Evolution score adds up to', breakdown.evolution],
      ['Activity score adds up to', breakdown.activity],
      ['Latency score', breakdown.latency],
    ];
    bdEl.innerHTML = sections.map(([title, items]) => {
      if (!items || !items.length) return '';
      const rows = items.map(it => barRow(it.label, it.points, it.max, it.detail)).join('');
      return `<div class="quality-bd-section"><div class="panel-title mb-2" style="font-size:11px">${escapeHtml(title)}</div>${rows}</div>`;
    }).join('');
  }

  const glossEl = document.getElementById('quality-glossary');
  if (glossEl) {
    const keys = [
      ['composite', 'Overall'],
      ['fresh_l2', 'Fresh L2 queue'],
      ['l6', 'L6 schemas'],
      ['relations', 'Graph relations'],
      ['llm_tokens', 'LLM tokens (7d)'],
    ];
    glossEl.innerHTML = keys.map(([k, title]) => `
      <div class="kv-item"><div class="kv-label">${escapeHtml(title)}</div>
        <div class="kv-value text-sm">${escapeHtml(guides[k] || '')}</div></div>`).join('');
  }

  const liveEl = document.getElementById('quality-live');
  if (liveEl) {
    const tpm = snap.tokens_per_memory_index;
    liveEl.innerHTML = `
      <div class="kv-item"><div class="kv-label">LLM tokens on memory writes (7d)</div>
        <div class="kv-value font-mono">${llm.total != null ? llm.total.toLocaleString() : '—'}</div></div>
      <div class="kv-item"><div class="kv-label">Tokens per VDB point</div>
        <div class="kv-value">${tpm != null ? tpm : '—'}</div></div>
      <div class="kv-item"><div class="kv-label">Writes / digests (7d)</div>
        <div class="kv-value">${snap.sys1_writes_7d ?? '—'} / ${snap.sys2_digests_7d ?? '—'}</div></div>
      <div class="kv-item"><div class="kv-label">Fresh L2 · digest log</div>
        <div class="kv-value">${snap.fresh_l2_for_digest ?? '—'} · <strong>${escapeHtml(snap.digest_log_status || '—')}</strong></div></div>
      <div class="kv-item"><div class="kv-label">L5 / L6 / L7 · relations</div>
        <div class="kv-value font-mono">${graph.l5 ?? '—'} / ${graph.l6 ?? '—'} / ${graph.l7 ?? '—'} · ${graph.relations ?? '—'}</div></div>
    `;
  }

  const nudgePanel = document.getElementById('quality-nudge-panel');
  const tipsEl = document.getElementById('quality-tips');
  if (tipsEl) {
    if (!tips.length) {
      if (nudgePanel) nudgePanel.style.display = 'none';
      tipsEl.innerHTML = '';
    } else {
      if (nudgePanel) nudgePanel.style.display = '';
      tipsEl.innerHTML = tips.map(t => `
        <div class="quality-tip priority-${escapeHtml(t.priority || 'low')}">
          <div class="quality-tip-title">${escapeHtml(t.title || '')}</div>
          <p>${escapeHtml(t.body || '')}</p>
          <p class="text-muted text-sm"><strong>Do:</strong> ${escapeHtml(t.action || '')}</p>
        </div>`).join('');
    }
  }

  const jsonEl = document.getElementById('quality-json');
  if (jsonEl) {
    jsonEl.textContent = JSON.stringify(root, null, 2);
  }
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
    // Memory detail is now a dedicated page; no in-sidebar panel needed.
  } else if (page === 'layers') {
    renderLayerHierarchy(layerCountsData?.counts || {});
  } else if (page === 'today') {
    const since = Date.now() - 24 * 60 * 60 * 1000;
    const todayMemories = activityMemories.filter(m => {
      const created = tsToDate(m.gmt_created);
      return created && created.getTime() >= since;
    });
    renderTodaySummary(todayMemories);
  } else if (page === 'system') {
    // System page has its own layout
    document.getElementById('right-sidebar').innerHTML = '';
  } else if (page === 'memory-detail') {
    // The dedicated Memory Detail page replaces the right sidebar entirely.
    // Clear it and let CSS hide the column for a full-width view.
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
    recent = activityMemories.filter(m => m.user_id !== 'coding');
  } else if (recentIngestionTab === 'coding') {
    recent = activityMemories.filter(m => m.user_id === 'coding');
  } else if (recentIngestionTab === 'l1_raw') {
    recent = activityMemories.filter(m => m.layer === 'l1_raw');
  } else {
    recent = activityMemories.filter(() => true);
  }
  // Prefer gmt_updated over gmt_created for the "ago" text — recent
  // UPDATEs of older memories should show as recent (otherwise the
  // "Last ingestion" timer looks stale when only UPDATEs are happening).
  const sortKey = m => Number(m.gmt_updated || m.gmt_created) || 0;
  recent.sort((a, b) => sortKey(b) - sortKey(a));
  recent = recent.slice(0, 10);

  // Counts for each tab so the user can see what's available.
  const tabCounts = {
    all:    activityMemories.length,
    vdb:    activityMemories.filter(m => m.user_id !== 'coding').length,
    coding: activityMemories.filter(m => m.user_id === 'coding').length,
    l1_raw: activityMemories.filter(m => m.layer === 'l1_raw').length,
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
        const ts = Number(sortKey(m)) || 0;
        const ago = ts ? Math.floor((Date.now() / 1000 - ts) / 60) : 0;
        const agoText = !ts ? '—' : (ago < 1 ? 'just now' : (ago < 60 ? `${ago}m ago` : `${Math.floor(ago / 60)}h ago`));
        const wasUpdated = m.gmt_updated && m.gmt_created && (Number(m.gmt_updated) - Number(m.gmt_created) > 60);
        const titleAttr = wasUpdated
          ? `Created ${ts ? new Date(Number(m.gmt_created) * 1000).toLocaleString() : '—'} • Updated ${ts ? new Date(Number(m.gmt_updated) * 1000).toLocaleString() : '—'}`
          : `Created ${ts ? new Date(Number(m.gmt_created) * 1000).toLocaleString() : '—'}`;

        return `
                  <div class="ingestion-item" data-memory-id="${m.memory_id}" onclick="window.__openMemoryDetail && window.__openMemoryDetail('${m.memory_id}')">
                    <div class="ingestion-title" title="${escapeHtml(titleAttr)}">${escapeHtml(title)}</div>
                    <div class="ingestion-meta">
                      <span class="badge badge-layer layer-${m.layer || 'l2_fact'}" style="font-size: 9px; padding: 2px 6px;">${m.layer || '—'}</span>
                      ${typeof m.importance === 'number' ? `<span class="badge badge-importance ${m.importance >= 0.7 ? 'importance-high' : m.importance >= 0.4 ? 'importance-mid' : 'importance-low'}" style="font-size: 9px; padding: 2px 6px;" title="Importance">★ ${m.importance.toFixed(2)}</span>` : ''}
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
        <div class="mb-2">VDB points: <span class="font-mono">${fmtCount(getVdbPoints())}</span> · display: <span class="font-mono">${fmtCount(getLayerTotal())}</span></div>
        <div class="mb-2">Active layers: <span class="font-mono">${getActiveLayerCount()}</span></div>
      </div>
    </div>
  `;
  
  document.getElementById('right-sidebar').innerHTML = html;
}

function getMostActiveLayer() {
  const counts = layerCountsData?.counts || {};
  return Object.entries(counts).sort((a, b) => Number(b[1]) - Number(a[1]))[0]?.[0] || '—';
}

function getMostCommonTag() {
  const counts = {};
  vdbMemories.forEach(m => {
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
initAgentSelector();
loadAllData().then(() => {
  updateRightSidebar('overview');
  enterPage('overview');
  hideBootScreen();
  // If the URL has ?memory=<id>, open the dedicated Memory Detail page
  // (after data has loaded so observatoryMemories is populated). Falls through
  // silently if the memory is not in the current dataset.
  restoreMemoryDetailFromUrl();
  setTimeout(refreshLoop, REFRESH_S * 1000);
});

async function refreshLoop() {
  await loadAllData();
  setTimeout(refreshLoop, REFRESH_S * 1000);
}

