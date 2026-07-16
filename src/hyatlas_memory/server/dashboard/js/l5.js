// L5 Knowledge Graph page
let l5State = {
  data: null,            // full response from /api/l5/graph
  selectedType: null,    // null = all
  search: '',
  selectedEntity: null,  // null = show all
};

async function initL5Page() {
  if (l5State.data) {
    renderL5();
    return;
  }
  try {
    const data = await fetchJSON('/api/l5/graph');
    l5State.data = data;
    renderL5();
  } catch (e) {
    document.getElementById('l5-stats').innerHTML =
      '<div class="text-muted">Failed to load L5 graph. Run <code>bin/l5_export_json.py</code> and refresh.</div>';
  }
}

function renderL5() {
  if (!l5State.data) return;
  const d = l5State.data;

  // Stats panel
  const typeDistHtml = Object.entries(d.type_distribution || {})
    .sort((a, b) => b[1] - a[1])
    .map(([t, n]) => `<span class="l5-type-badge" data-type="${t}">${escapeHtml(t)}: ${n}</span>`)
    .join('  ');
  const relDistHtml = Object.entries(d.relation_type_distribution || {})
    .sort((a, b) => b[1] - a[1])
    .map(([t, n]) => `${escapeHtml(t)}: ${n}`)
    .join('  ');
  document.getElementById('l5-stats').innerHTML = `
    <div class="kv"><span class="kv-k">NODES</span><span class="kv-v">${d.node_count}</span></div>
    <div class="kv"><span class="kv-k">RELATIONS</span><span class="kv-v">${d.relation_count}</span></div>
    <div class="kv"><span class="kv-k">EXPORTED AT</span><span class="kv-v">${escapeHtml(d.exported_at || new Date().toISOString().slice(0, 19).replace('T', ' '))}</span></div>
    <div class="kv"><span class="kv-k">ENTITY TYPES</span><span class="kv-v">${typeDistHtml}</span></div>
    <div class="kv"><span class="kv-k">RELATION TYPES</span><span class="kv-v">${relDistHtml}</span></div>
  `;

  // Type chips (All + each type)
  const allTypes = ['TOOL', 'PROJECT', 'MODEL', 'PERSON', 'CONCEPT'].filter(t => (d.type_distribution || {})[t]);
  const chips = ['<span class="l5-chip ' + (l5State.selectedType === null ? 'active' : '') + '" data-type="">ALL</span>']
    .concat(allTypes.map(t => `<span class="l5-chip ${l5State.selectedType === t ? 'active' : ''}" data-type="${t}">${t}</span>`));
  document.getElementById('l5-type-chips').innerHTML = chips.join('');

  // Wire up chip click handlers
  document.querySelectorAll('#l5-type-chips .l5-chip').forEach(chip => {
    chip.onclick = () => {
      const t = chip.getAttribute('data-type') || null;
      l5State.selectedType = t;
      l5State.selectedEntity = null;  // reset selection on filter change
      renderL5EntitiesAndRelations();
    };
  });

  // Wire up search
  const searchEl = document.getElementById('l5-search');
  searchEl.value = l5State.search;
  searchEl.oninput = () => {
    l5State.search = searchEl.value;
    l5State.selectedEntity = null;
    renderL5EntitiesAndRelations();
  };

  renderL5EntitiesAndRelations();
}

function renderL5EntitiesAndRelations() {
  if (!l5State.data) return;
  let nodes = l5State.data.nodes || [];
  let rels = l5State.data.relations || [];

  // Apply type filter
  if (l5State.selectedType) {
    const names = new Set(nodes.filter(n => n.entity_type === l5State.selectedType).map(n => n.name));
    nodes = nodes.filter(n => names.has(n.name));
    rels = rels.filter(r => names.has(r.a) && names.has(r.b));
  }

  // Apply search filter
  if (l5State.search) {
    const sl = l5State.search.toLowerCase();
    const matched = nodes.filter(n =>
      n.name.toLowerCase().includes(sl) ||
      (n.aliases || []).some(a => a.toLowerCase().includes(sl))
    );
    const names = new Set(matched.map(n => n.name));
    // If user searched, expand to show 1-hop neighbors too
    const expanded = new Set(names);
    for (const r of rels) {
      if (names.has(r.a)) expanded.add(r.b);
      if (names.has(r.b)) expanded.add(r.a);
    }
    nodes = nodes.filter(n => expanded.has(n.name));
    rels = rels.filter(r => expanded.has(r.a) && expanded.has(r.b));
  }

  // Apply entity selection (only show relations involving that entity)
  if (l5State.selectedEntity) {
    const e = l5State.selectedEntity;
    rels = rels.filter(r => r.a === e || r.b === e);
  }

  // Sort nodes by mention_count desc
  nodes = nodes.slice().sort((a, b) => (b.mention_count || 0) - (a.mention_count || 0));

  // Render entities
  document.getElementById('l5-entities-title').textContent =
    `ENTITIES (${nodes.length}${l5State.selectedType ? ' of ' + l5State.data.nodes.length : ''})`;
  const entHtml = nodes.length === 0
    ? '<div class="text-muted">No entities match the current filter.</div>'
    : nodes.slice(0, 200).map(n => {
        const selected = l5State.selectedEntity === n.name ? ' selected' : '';
        const aliasStr = n.aliases && n.aliases.length
          ? `<div class="l5-aliases">aka: ${n.aliases.map(a => escapeHtml(a)).join(', ')}</div>`
          : '';
        return `<div class="l5-entity${selected}" data-name="${escapeAttr(n.name)}">
          <span class="l5-type-badge l5-type-${n.entity_type}">${escapeHtml(n.entity_type)}</span>
          <span class="l5-name">${escapeHtml(n.name)}</span>
          <span class="l5-mentions">×${n.mention_count || 1}</span>
          ${aliasStr}
        </div>`;
      }).join('');
  document.getElementById('l5-entities-list').innerHTML = entHtml +
    (nodes.length > 200 ? `<div class="text-muted mt-2">…and ${nodes.length - 200} more (refine your filter to see them)</div>` : '');

  // Wire up entity click handlers
  document.querySelectorAll('#l5-entities-list .l5-entity').forEach(el => {
    el.onclick = () => {
      const name = el.getAttribute('data-name');
      l5State.selectedEntity = (l5State.selectedEntity === name) ? null : name;
      renderL5EntitiesAndRelations();
    };
  });

  // Render relations
  document.getElementById('l5-relations-title').textContent =
    `RELATIONS (${rels.length}${l5State.selectedEntity ? ' involving ' + l5State.selectedEntity : ''})`;
  const relHtml = rels.length === 0
    ? '<div class="text-muted">No relations match the current filter.</div>'
    : rels.slice(0, 200).map(r => `
        <div class="l5-relation">
          <span class="l5-rel-name">${escapeHtml(r.a)}</span>
          <span class="l5-rel-type">${escapeHtml(r.relation_type)}</span>
          <span class="l5-rel-arrow">→</span>
          <span class="l5-rel-name">${escapeHtml(r.b)}</span>
          <span class="l5-rel-conf">${(r.confidence || 0).toFixed(2)}</span>
        </div>
      `).join('');
  document.getElementById('l5-relations-list').innerHTML = relHtml +
    (rels.length > 200 ? `<div class="text-muted mt-2">…and ${rels.length - 200} more (refine your filter to see them)</div>` : '');
}

