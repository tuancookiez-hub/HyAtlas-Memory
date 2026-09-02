import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  KEYBINDS_AREA,
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  SearchField,
  SegmentedControl,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  StatusDot,
  Textarea,
  host,
  relativeTime,
  useMutation,
  useQuery,
  useQueryClient,
} from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

let api = null

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'memories', label: 'Memories' },
  { id: 'search', label: 'Search' },
  { id: 'add', label: 'Add' },
]

const LAYERS = [
  ['l1_profile', 'L1 Profile'],
  ['l2_raw', 'L2 Raw'],
  ['l3_fact', 'L3 Fact'],
  ['l4_summary', 'L4 Summary'],
  ['l5_knowledge', 'L5 Knowledge'],
  ['l6_schema', 'L6 Schema'],
  ['l7_intention', 'L7 Intention'],
]

function tone(ok) {
  return ok ? 'good' : 'bad'
}

function describe(err) {
  const raw = String((err && err.message) || err || '')
  if (/Headless backend|web UI disabled/i.test(raw)) {
    return 'Plugin backend is not mounted. Restart the Desktop backend after enabling hy_memory (⌘K → Restart backend).'
  }
  if (/No such API endpoint/i.test(raw)) {
    return 'Plugin API route missing. Confirm hy_memory is in plugins.enabled, then restart the Desktop backend.'
  }
  if (/unreachable|503/i.test(raw)) {
    return 'HyAtlas v4 is not running on 127.0.0.1:19528.'
  }
  return raw
    .replace(/^Error invoking remote method 'hermes:api':\s*/i, '')
    .replace(/^Error:\s*/i, '')
}

function when(item) {
  const sec = Number(item && item.gmt_created)
  if (Number.isFinite(sec) && sec > 0) return relativeTime(sec * 1000)
  return (item && item.ts) || ''
}

function LayerBar({ label, value, max }) {
  const n = Number(value) || 0
  const pct = Math.max(0, Math.min(100, (n / Math.max(Number(max) || 1, 1)) * 100))
  return jsxs('div', {
    className: 'flex flex-col gap-1',
    children: [
      jsxs('div', {
        className: 'flex justify-between text-xs text-(--ui-text-secondary)',
        children: [
          jsx('span', { children: label }),
          jsx('span', { className: 'tabular-nums', children: String(n) }),
        ],
      }),
      jsx('div', {
        className: 'h-1.5 overflow-hidden rounded-full bg-(--ui-stroke-secondary)',
        children: jsx('div', {
          className: 'h-full rounded-full bg-primary',
          style: { width: `${pct}%` },
        }),
      }),
    ],
  })
}

function Stat({ label, value, hint }) {
  return jsxs('div', {
    className: 'rounded-md border border-(--ui-stroke-secondary) p-2.5',
    children: [
      jsx('div', { className: 'text-[0.65rem] text-(--ui-text-tertiary)', children: label }),
      jsx('div', { className: 'mt-0.5 text-lg tabular-nums', children: value ?? '—' }),
      hint ? jsx('div', { className: 'mt-1 text-[0.65rem] text-(--ui-text-quaternary)', children: hint }) : null,
    ],
  })
}

function Health({ ok, label }) {
  return jsxs('span', {
    className: 'inline-flex items-center gap-1.5 text-xs text-(--ui-text-secondary)',
    children: [
      jsx(StatusDot, { tone: tone(ok) }),
      jsx('span', { children: label }),
    ],
  })
}

function MemoryCard({ item, meta }) {
  return jsxs('div', {
    className: 'rounded-md border border-(--ui-stroke-secondary) p-2.5',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between gap-2 text-[0.65rem] text-(--ui-text-tertiary)',
        children: [
          jsx(Badge, { variant: 'muted', size: 'xs', children: meta || item.layer || 'memory' }),
          jsx('span', { children: when(item) }),
        ],
      }),
      jsx('div', {
        className: 'mt-1.5 text-sm text-(--ui-text-primary)',
        children: (item.content || item.text || '').slice(0, 280) || '(empty)',
      }),
    ],
  })
}

function Overview({ status, layers, maxLayer, connecting }) {
  if (connecting) {
    return jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: 'Loading status…' })
  }
  return jsxs('div', {
    className: 'flex flex-col gap-4',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap gap-4',
        children: [
          jsx(Health, { ok: Boolean(status && status.vdb === 'ok'), label: `VDB (${(status && status.vdb_provider) || '—'})` }),
          jsx(Health, { ok: Boolean(status && status.embed === 'ok'), label: `Embedder ${status ? `${status.embed_dims}d` : ''}` }),
          jsx(Health, { ok: Boolean(status && status.llm === 'ok'), label: 'LLM extraction' }),
          jsx(Health, { ok: Boolean(status && status.write_pipeline === 'ok'), label: 'Write pipeline' }),
        ],
      }),
      jsx('div', {
        className: 'flex flex-col gap-2 rounded-md border border-(--ui-stroke-secondary) p-2.5',
        children: LAYERS.map(([key, label]) =>
          jsx(LayerBar, { label, value: layers[key] || 0, max: maxLayer }, key),
        ),
      }),
      jsxs('div', {
        className: 'grid grid-cols-2 gap-3 md:grid-cols-4',
        children: [
          jsx(Stat, { label: 'Used — writes', value: status && status.writes, hint: 'memories added (all-time)' }),
          jsx(Stat, { label: 'Used — searches', value: status && status.searches, hint: 'recalls by agents (all-time)' }),
          jsx(Stat, { label: 'VDB points', value: status && status.vdb_points }),
          jsx(Stat, { label: 'Embed dims', value: status && status.embed_dims }),
        ],
      }),
    ],
  })
}

function Memories({ layer, setLayer, listQ, rows }) {
  return jsxs('div', {
    className: 'flex flex-col gap-3',
    children: [
      jsxs(Select, {
        value: layer,
        onValueChange: setLayer,
        children: [
          jsx(SelectTrigger, { className: 'w-56', children: jsx(SelectValue, { placeholder: 'All layers' }) }),
          jsxs(SelectContent, {
            children: [
              jsx(SelectItem, { value: 'all', children: 'All layers' }),
              ...LAYERS.map(([key, label]) => jsx(SelectItem, { value: key, children: label }, key)),
            ],
          }),
        ],
      }),
      listQ.isLoading
        ? jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: 'Loading memories…' })
        : rows.length
          ? rows.map((item) => jsx(MemoryCard, { item }, item.memory_id || item.ts))
          : jsx(EmptyState, {
              title: 'No memories',
              description: layer === 'all' ? 'Nothing stored yet.' : 'Nothing in this layer.',
            }),
    ],
  })
}

function Search({ query, setQuery, submitted, setSubmitted, searchQ, hits }) {
  return jsxs('div', {
    className: 'flex flex-col gap-3',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx(SearchField, {
            placeholder: 'Semantic search…',
            value: query,
            onChange: setQuery,
            loading: searchQ.isFetching,
            containerClassName: 'flex-1',
            'aria-label': 'Search memories',
          }),
          jsx(Button, {
            type: 'button',
            size: 'sm',
            disabled: !query.trim() || searchQ.isFetching,
            onClick: () => setSubmitted(query.trim()),
            children: 'Search',
          }),
        ],
      }),
      !submitted
        ? jsx(EmptyState, { title: 'Search memories', description: 'Type a query and press Search.' })
        : searchQ.isLoading
          ? jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: 'Searching…' })
          : hits.length
            ? hits.map(([channel, m]) =>
                jsx(
                  MemoryCard,
                  {
                    item: m,
                    meta: `${channel} · ${m.layer || ''} · ${Number(m.score || 0).toFixed(3)}`,
                  },
                  m.memory_id,
                ),
              )
            : jsx(EmptyState, { title: 'No hits', description: 'Try a broader query.' }),
    ],
  })
}

function Add({ draft, setDraft, addM }) {
  return jsxs('div', {
    className: 'flex flex-col gap-3',
    children: [
      jsx(Textarea, {
        value: draft,
        onChange: (e) => setDraft(e.target.value),
        rows: 6,
        placeholder: 'Write a memory… v4 will extract facts, summary, graph nodes, and intention.',
      }),
      jsx(Button, {
        type: 'button',
        disabled: addM.isPending || !draft.trim(),
        onClick: () => addM.mutate(draft.trim()),
        children: addM.isPending ? 'Saving…' : 'Save memory',
      }),
    ],
  })
}

function HyAtlasPage() {
  const client = useQueryClient()
  const [tab, setTab] = useState('overview')
  const [layer, setLayer] = useState('all')
  const [query, setQuery] = useState('')
  const [draft, setDraft] = useState('')
  const [submitted, setSubmitted] = useState('')
  const statusQ = useQuery({
    queryKey: ['hyatlas', 'status'],
    queryFn: () => api.rest('/status'),
    refetchInterval: 8000,
  })

  const listQ = useQuery({
    queryKey: ['hyatlas', 'memories', layer],
    queryFn: () => api.rest(`/memories?limit=30${layer === 'all' ? '' : `&layer=${layer}`}`),
    refetchInterval: 8000,
    enabled: tab === 'memories' || tab === 'overview',
  })

  const searchQ = useQuery({
    queryKey: ['hyatlas', 'search', submitted],
    queryFn: () => api.rest(`/search?q=${encodeURIComponent(submitted)}&limit=10`),
    enabled: submitted.length > 0 && tab === 'search',
  })

  const addM = useMutation({
    mutationFn: (text) => api.rest('/add', { method: 'POST', body: { text } }),
    onSuccess: () => {
      setDraft('')
      client.invalidateQueries({ queryKey: ['hyatlas'] })
      host.notify({ kind: 'success', message: 'Memory saved to HyAtlas v4' })
    },
  })

  const status = statusQ.data
  const layers = (status && status.layers) || {}
  const maxLayer = Math.max(1, ...Object.values(layers).map((v) => Number(v) || 0))
  const rows = (listQ.data && listQ.data.memories) || []
  const channels = (searchQ.data && searchQ.data.memories) || {}
  const hits = [
    ...(channels.profile || []).map((m) => ['profile', m]),
    ...(channels.proactive || []).map((m) => ['proactive', m]),
    ...(channels.normal || []).map((m) => ['normal', m]),
  ]
  const err = statusQ.error || (tab === 'memories' && listQ.error) || (tab === 'search' && searchQ.error) || addM.error
  const connecting = statusQ.isLoading && !status

  const body = tab === 'overview'
    ? jsx(Overview, { status, layers, maxLayer, connecting })
    : tab === 'memories'
      ? jsx(Memories, { layer, setLayer, listQ, rows })
      : tab === 'search'
        ? jsx(Search, { query, setQuery, submitted, setSubmitted, searchQ, hits })
        : jsx(Add, { draft, setDraft, addM })

  return jsxs('div', {
    className: 'flex h-full flex-col gap-4 overflow-auto p-5 text-sm',
    children: [
      jsxs('div', {
        className: 'flex items-start justify-between gap-3',
        children: [
          jsxs('div', {
            children: [
              jsx('h1', { className: 'text-xl font-semibold', children: 'HyAtlas Memory' }),
              jsx('p', {
                className: 'text-xs text-(--ui-text-tertiary)',
                children: connecting
                  ? 'v4 · chromem-go · connecting…'
                  : status
                    ? `v4 · ${status.vdb_points} memories · ${status.writes || 0} writes · ${status.searches || 0} recalls`
                    : 'v4 · chromem-go',
              }),
            ],
          }),
          jsx(Button, {
            type: 'button',
            variant: 'outline',
            size: 'sm',
            disabled: statusQ.isFetching,
            onClick: () => client.invalidateQueries({ queryKey: ['hyatlas'] }),
            children: statusQ.isFetching ? 'Refreshing…' : 'Refresh',
          }),
        ],
      }),
      statusQ.isError && !status
        ? jsx(ErrorState, {
            title: 'Cannot reach HyAtlas',
            description: describe(statusQ.error),
            children: jsx(Button, {
              type: 'button',
              variant: 'outline',
              size: 'sm',
              onClick: () => client.invalidateQueries({ queryKey: ['hyatlas'] }),
              children: 'Retry',
            }),
          })
        : jsxs('div', {
            className: 'flex flex-col gap-4',
            children: [
              err && status
                ? jsxs('div', {
                    className: 'rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive',
                    children: [
                      describe(err),
                      ' ',
                      jsx(Button, {
                        type: 'button',
                        variant: 'text',
                        size: 'inline',
                        onClick: () => client.invalidateQueries({ queryKey: ['hyatlas'] }),
                        children: 'Retry',
                      }),
                    ],
                  })
                : null,
              jsx(SegmentedControl, { value: tab, onChange: setTab, options: TABS }),
              body,
            ],
          }),
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
          keywords: ['hyatlas', 'memory', 'chromem'],
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
