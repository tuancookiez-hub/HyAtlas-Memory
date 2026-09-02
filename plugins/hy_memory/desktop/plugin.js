import {
  KEYBINDS_AREA,
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  host,
} from '@hermes/plugin-sdk'
import { useCallback, useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

let api = null

const cardStyle = {
  border: '1px solid var(--ui-stroke-secondary)',
  borderRadius: '8px',
  padding: '10px',
}

const buttonStyle = {
  border: '1px solid var(--ui-stroke-secondary)',
  borderRadius: '6px',
  padding: '6px 11px',
  background: 'transparent',
  color: 'var(--ui-text-primary)',
  cursor: 'pointer',
}

const LAYERS = [
  ['l1_profile', 'L1 Profile'],
  ['l2_raw', 'L2 Raw'],
  ['l3_fact', 'L3 Fact'],
  ['l4_summary', 'L4 Summary'],
  ['l5_knowledge', 'L5 Knowledge'],
  ['l6_schema', 'L6 Schema'],
  ['l7_intention', 'L7 Intention'],
]

function StatusDot({ ok, label }) {
  const color = ok ? 'var(--ui-accent, #3fb950)' : 'var(--ui-error, #f85149)'
  return jsxs('span', {
    className: 'flex items-center gap-1.5 text-xs',
    children: [
      jsx('span', {
        style: {
          width: '8px',
          height: '8px',
          borderRadius: '99px',
          background: color,
          display: 'inline-block',
        },
      }),
      jsx('span', { style: { color: 'var(--ui-text-secondary)' }, children: label }),
    ],
  })
}

function LayerBar({ label, value, max }) {
  const numeric = Number(value) || 0
  const ceiling = Math.max(Number(max) || 1, 1)
  const pct = Math.max(0, Math.min(100, (numeric / ceiling) * 100))
  return jsxs('div', {
    className: 'flex flex-col gap-1',
    children: [
      jsxs('div', {
        className: 'flex justify-between text-xs',
        style: { color: 'var(--ui-text-secondary)' },
        children: [
          jsx('span', { children: label }),
          jsx('span', { children: String(numeric) }),
        ],
      }),
      jsx('div', {
        style: {
          height: '6px',
          borderRadius: '99px',
          background: 'var(--ui-stroke-secondary)',
          overflow: 'hidden',
        },
        children: jsx('div', {
          style: {
            width: `${pct}%`,
            height: '100%',
            borderRadius: '99px',
            background: 'var(--ui-accent)',
          },
        }),
      }),
    ],
  })
}

function MemoryRow({ item }) {
  const layer = item.layer || ''
  return jsxs('div', {
    style: { ...cardStyle, padding: '8px' },
    children: [
      jsxs('div', {
        className: 'flex justify-between text-xs',
        style: { color: 'var(--ui-text-tertiary)' },
        children: [
          jsx('span', { children: layer }),
          jsx('span', {
            children: item.ts || (item.gmt_created ? new Date(item.gmt_created * 1000).toISOString().slice(0, 16).replace('T', ' ') : ''),
          }),
        ],
      }),
      jsx('div', {
        className: 'text-sm mt-1',
        style: { color: 'var(--ui-text-primary)' },
        children: (item.content || item.text || '').slice(0, 240) || '(empty)',
      }),
    ],
  })
}

function HyAtlasPage() {
  const [status, setStatus] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [tab, setTab] = useState('overview')
  const [memories, setMemories] = useState([])
  const [layerFilter, setLayerFilter] = useState('')
  const [query, setQuery] = useState('')
  const [searchResults, setSearchResults] = useState(null)
  const [draft, setDraft] = useState('')

  const refresh = useCallback(async () => {
    setBusy(true)
    setError('')
    try {
      const nextStatus = await api.rest('/status')
      setStatus(nextStatus)
      const list = await api.rest(`/memories?limit=30${layerFilter ? `&layer=${layerFilter}` : ''}`)
      setMemories((list && list.memories) || [])
    } catch (err) {
      setError(String((err && err.message) || err))
    } finally {
      setBusy(false)
    }
  }, [layerFilter])

  useEffect(() => { refresh() }, [refresh])

  async function runSearch() {
    if (!query.trim()) return
    setBusy(true)
    setError('')
    try {
      const res = await api.rest(`/search?q=${encodeURIComponent(query)}&limit=10`)
      setSearchResults(res)
    } catch (err) {
      setError(String((err && err.message) || err))
    } finally {
      setBusy(false)
    }
  }

  async function addMemory() {
    if (!draft.trim()) return
    setBusy(true)
    setError('')
    try {
      await api.rest('/add', { method: 'POST', body: { text: draft } })
      setDraft('')
      await host.notify({ kind: 'success', message: 'Memory saved to HyAtlas v4' })
      await refresh()
    } catch (err) {
      setError(String((err && err.message) || err))
    } finally {
      setBusy(false)
    }
  }

  const layers = (status && status.layers) || {}
  const maxLayer = Math.max(1, ...Object.values(layers).map((v) => Number(v) || 0))
  const vdbOk = status && status.vdb === 'ok'
  const embedOk = status && status.embed === 'ok'
  const llmOk = status && status.llm === 'ok'
  const pipelineOk = status && status.write_pipeline === 'ok'

  return jsxs('div', {
    className: 'flex h-full flex-col gap-4 overflow-auto p-5 text-sm',
    children: [
      jsxs('div', { className: 'flex items-center justify-between', children: [
        jsxs('div', { children: [
          jsx('h1', { className: 'text-xl font-semibold', children: 'HyAtlas Memory' }),
          jsx('p', {
            style: { color: 'var(--ui-text-tertiary)' },
            children: `v4 · chromem-go · ${status ? `${status.vdb_points} memories · ${status.graph_nodes} graph nodes` : 'connecting…'}`,
          }),
        ] }),
        jsx('button', { type: 'button', disabled: busy, style: buttonStyle, onClick: refresh, children: busy ? '…' : 'Refresh' }),
      ] }),

      error ? jsx('div', {
        style: { ...cardStyle, borderColor: 'var(--ui-error, #f85149)', color: 'var(--ui-error, #f85149)' },
        children: error,
      }) : null,

      jsxs('div', {
        className: 'flex gap-2 text-xs',
        children: [
          jsx('button', { type: 'button', style: { ...buttonStyle, ...(tab === 'overview' ? { borderColor: 'var(--ui-accent)' } : {}) }, onClick: () => setTab('overview'), children: 'Overview' }),
          jsx('button', { type: 'button', style: { ...buttonStyle, ...(tab === 'memories' ? { borderColor: 'var(--ui-accent)' } : {}) }, onClick: () => setTab('memories'), children: 'Memories' }),
          jsx('button', { type: 'button', style: { ...buttonStyle, ...(tab === 'search' ? { borderColor: 'var(--ui-accent)' } : {}) }, onClick: () => setTab('search'), children: 'Search' }),
          jsx('button', { type: 'button', style: { ...buttonStyle, ...(tab === 'add' ? { borderColor: 'var(--ui-accent)' } : {}) }, onClick: () => setTab('add'), children: 'Add' }),
        ],
      }),

      tab === 'overview' ? jsxs('div', { className: 'flex flex-col gap-4', children: [
        jsxs('div', { className: 'flex flex-wrap gap-4', children: [
          jsx(StatusDot, { ok: Boolean(vdbOk), label: `VDB (${status ? status.vdb_provider : '—'})` }),
          jsx(StatusDot, { ok: Boolean(embedOk), label: `Embedder ${status ? `${status.embed_dims}d` : ''}` }),
          jsx(StatusDot, { ok: Boolean(llmOk), label: 'LLM extraction' }),
          jsx(StatusDot, { ok: Boolean(pipelineOk), label: 'Write pipeline' }),
        ] }),
        jsx('div', {
          style: cardStyle,
          className: 'flex flex-col gap-2',
          children: LAYERS.map(([key, label]) => jsx(LayerBar, {
            key,
            label,
            value: layers[key] || 0,
            max: maxLayer,
          })),
        }),
        jsxs('div', {
          className: 'grid grid-cols-2 gap-3 md:grid-cols-4',
          children: [
            jsx('div', { style: cardStyle, children: jsxs('div', { className: 'text-xs', children: [
              jsx('div', { style: { color: 'var(--ui-text-tertiary)' }, children: 'Graph nodes' }),
              jsx('div', { className: 'text-lg', children: status ? status.graph_nodes : '—' }),
            ] }) }),
            jsx('div', { style: cardStyle, children: jsxs('div', { className: 'text-xs', children: [
              jsx('div', { style: { color: 'var(--ui-text-tertiary)' }, children: 'Graph edges' }),
              jsx('div', { className: 'text-lg', children: status ? status.graph_edges : '—' }),
            ] }) }),
            jsx('div', { style: cardStyle, children: jsxs('div', { className: 'text-xs', children: [
              jsx('div', { style: { color: 'var(--ui-text-tertiary)' }, children: 'VDB points' }),
              jsx('div', { className: 'text-lg', children: status ? status.vdb_points : '—' }),
            ] }) }),
            jsx('div', { style: cardStyle, children: jsxs('div', { className: 'text-xs', children: [
              jsx('div', { style: { color: 'var(--ui-text-tertiary)' }, children: 'Embed dims' }),
              jsx('div', { className: 'text-lg', children: status ? status.embed_dims : '—' }),
            ] }) }),
          ],
        }),
      ] }) : null,

      tab === 'memories' ? jsxs('div', { className: 'flex flex-col gap-3', children: [
        jsxs('select', {
          value: layerFilter,
          onChange: (e) => setLayerFilter(e.target.value),
          style: {
            border: '1px solid var(--ui-stroke-secondary)',
            borderRadius: '6px',
            padding: '6px 9px',
            background: 'transparent',
            color: 'var(--ui-text-primary)',
            maxWidth: '240px',
          },
          children: [
            jsx('option', { value: '', children: 'All layers' }),
            ...LAYERS.map(([key, label]) => jsx('option', { value: key, children: label }, key)),
          ],
        }),
        memories.length
          ? memories.map((item) => jsx(MemoryRow, { key: item.memory_id || item.ts, item }))
          : jsx('div', { style: { color: 'var(--ui-text-tertiary)' }, children: 'No memories match this filter.' }),
      ] }) : null,

      tab === 'search' ? jsxs('div', { className: 'flex flex-col gap-3', children: [
        jsxs('div', { className: 'flex gap-2', children: [
          jsx('input', {
            value: query,
            onChange: (e) => setQuery(e.target.value),
            onKeyDown: (e) => { if (e.key === 'Enter') runSearch() },
            placeholder: 'Semantic search…',
            style: {
              flex: 1,
              border: '1px solid var(--ui-stroke-secondary)',
              borderRadius: '6px',
              padding: '7px 9px',
              background: 'transparent',
              color: 'var(--ui-text-primary)',
            },
          }),
          jsx('button', { type: 'button', disabled: busy, style: buttonStyle, onClick: runSearch, children: 'Search' }),
        ] }),
        searchResults ? (() => {
          const channels = (searchResults && searchResults.memories) || {}
          const rows = [
            ...(channels.profile || []).map((m) => ['profile', m]),
            ...(channels.proactive || []).map((m) => ['proactive', m]),
            ...(channels.normal || []).map((m) => ['normal', m]),
          ]
          return rows.length
            ? rows.map(([channel, m]) => jsxs('div', {
                style: { ...cardStyle, padding: '8px' },
                children: [
                  jsxs('div', {
                    className: 'flex justify-between text-xs',
                    style: { color: 'var(--ui-text-tertiary)' },
                    children: [
                      jsx('span', { children: `${channel} · ${m.layer}` }),
                      jsx('span', { children: `score ${Number(m.score || 0).toFixed(3)}` }),
                    ],
                  }),
                  jsx('div', { className: 'text-sm mt-1', children: (m.content || '').slice(0, 240) }),
                ],
              }, m.memory_id))
            : jsx('div', { style: { color: 'var(--ui-text-tertiary)' }, children: 'No hits.' })
        })() : jsx('div', { style: { color: 'var(--ui-text-tertiary)' }, children: 'Type a query and hit Enter.' }),
      ] }) : null,

      tab === 'add' ? jsxs('div', { className: 'flex flex-col gap-3', children: [
        jsx('textarea', {
          value: draft,
          onChange: (e) => setDraft(e.target.value),
          rows: 5,
          placeholder: 'Write a memory… the v4 LLM will extract facts, summary, graph nodes, and intention.',
          style: {
            border: '1px solid var(--ui-stroke-secondary)',
            borderRadius: '6px',
            padding: '8px 10px',
            background: 'transparent',
            color: 'var(--ui-text-primary)',
            resize: 'vertical',
            fontFamily: 'var(--ui-font-mono)',
          },
        }),
        jsx('button', { type: 'button', disabled: busy || !draft.trim(), style: buttonStyle, onClick: addMemory, children: busy ? 'Saving…' : 'Save memory' }),
      ] }) : null,
    ],
  })
}

export default {
  id: 'hy_memory',
  name: 'HyAtlas Memory',
  register(ctx) {
    api = ctx
    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: '/hyatlas' },
        render: () => jsx(HyAtlasPage, {}),
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        data: { path: '/hyatlas', label: 'HyAtlas Memory', codicon: 'database' },
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'hyatlas.open',
          label: 'Open HyAtlas Memory',
          keywords: ['hyatlas', 'memory'],
          run: () => host.navigate('/hyatlas'),
        },
      },
      {
        id: 'shortcut',
        area: KEYBINDS_AREA,
        data: {
          id: 'hyatlas.open',
          label: 'Open HyAtlas Memory',
          category: 'HyAtlas',
          defaults: ['mod+shift+h'],
          run: () => host.navigate('/hyatlas'),
        },
      },
    ])
  },
}
