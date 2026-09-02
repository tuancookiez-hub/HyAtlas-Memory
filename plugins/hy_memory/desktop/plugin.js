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
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

let api = null

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'graph', label: 'Graph' },
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

function Observatory({ graphQ, picked, setPicked }) {
  const hostRef = useRef(null)
  const data = graphQ.data
  const nodes = (data && data.nodes) || []
  const rels = (data && data.relations) || []

  useEffect(() => {
    const el = hostRef.current
    if (!el) return undefined
    let stop = false
    let frame = 0
    let renderer = null
    let onMove = null
    let onLeave = null
    let onClick = null
    let onResize = null

    const run = async () => {
      const THREE = await loadThree()
      if (stop || !hostRef.current) return
      const box = hostRef.current
      const w = Math.max(box.clientWidth, 320)
      const h = Math.max(box.clientHeight, 280)
      const scene = new THREE.Scene()
      const camera = new THREE.PerspectiveCamera(42, w / h, 0.1, 4000)
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true })
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
      renderer.setSize(w, h)
      box.replaceChildren(renderer.domElement)

      const placed = layout(nodes)
      const byId = Object.fromEntries(placed.map((n) => [n.id, n]))
      const groups = []
      placed.forEach((n) => {
        const color = new THREE.Color(n.color)
        const grp = new THREE.Group()
        const size = 3.4
        ;[
          { mul: 1, op: 0.95, side: THREE.FrontSide, depth: true },
          { mul: 1.8, op: 0.22, side: THREE.BackSide, depth: true },
          { mul: 3.1, op: 0.08, side: THREE.BackSide, depth: false },
        ].forEach((layer) => {
          grp.add(new THREE.Mesh(
            new THREE.SphereGeometry(size * layer.mul, 18, 18),
            new THREE.MeshBasicMaterial({
              color,
              transparent: true,
              opacity: layer.op,
              side: layer.side,
              depthWrite: layer.depth,
            }),
          ))
        })
        grp.position.set(n.x, n.y, n.z)
        grp.userData.node = n
        scene.add(grp)
        groups.push(grp)
      })

      const edges = []
      rels.forEach((rel) => {
        const a = byId[rel.from]
        const b = byId[rel.to]
        if (!a || !b) return
        const mid = new THREE.Vector3((a.x + b.x) / 2, (a.y + b.y) / 2 + 10, (a.z + b.z) / 2 - 6)
        const curve = new THREE.QuadraticBezierCurve3(
          new THREE.Vector3(a.x, a.y, a.z),
          mid,
          new THREE.Vector3(b.x, b.y, b.z),
        )
        const line = new THREE.Line(
          new THREE.BufferGeometry().setFromPoints(curve.getPoints(16)),
          new THREE.LineBasicMaterial({ color: 0x8a8274, transparent: true, opacity: 0.18, depthWrite: false }),
        )
        line.userData = { from: rel.from, to: rel.to, relation: rel.relation }
        scene.add(line)
        edges.push(line)
      })

      const starGeo = new THREE.BufferGeometry()
      const starPos = new Float32Array(480 * 3)
      for (let i = 0; i < 480; i += 1) {
        starPos[i * 3] = (hash(`${i}x`) - 0.5) * 520
        starPos[i * 3 + 1] = (hash(`${i}y`) - 0.5) * 360
        starPos[i * 3 + 2] = -220 + (hash(`${i}z`) - 0.5) * 180
      }
      starGeo.setAttribute('position', new THREE.BufferAttribute(starPos, 3))
      const stars = new THREE.Points(starGeo, new THREE.PointsMaterial({
        color: 0x9a927f, size: 0.7, transparent: true, opacity: 0.18, depthWrite: false,
      }))
      scene.add(stars)

      let pan = { x: 0, y: 0 }
      let zoom = 1.15
      let hover = null
      const raycaster = new THREE.Raycaster()
      const pointer = new THREE.Vector2()

      const updateCam = () => {
        camera.aspect = box.clientWidth / Math.max(box.clientHeight, 1)
        camera.updateProjectionMatrix()
        camera.position.set(pan.x, pan.y + 28 * zoom, 360 * zoom)
        camera.lookAt(new THREE.Vector3(pan.x, pan.y, 0))
      }

      const pick = (event) => {
        const r = box.getBoundingClientRect()
        pointer.x = ((event.clientX - r.left) / r.width) * 2 - 1
        pointer.y = -((event.clientY - r.top) / r.height) * 2 + 1
        raycaster.setFromCamera(pointer, camera)
        const hits = raycaster.intersectObjects(groups, true)
        if (!hits.length) return null
        let obj = hits[0].object
        while (obj && !obj.userData.node) obj = obj.parent
        return obj && obj.userData.node ? obj.userData.node : null
      }

      const paint = (focus) => {
        const connected = new Set()
        if (focus) {
          connected.add(focus.id)
          edges.forEach((e) => {
            if (e.userData.from === focus.id) connected.add(e.userData.to)
            if (e.userData.to === focus.id) connected.add(e.userData.from)
          })
        }
        groups.forEach((grp) => {
          const on = !focus || connected.has(grp.userData.node.id)
          grp.traverse((ch) => {
            if (ch.material) ch.material.opacity = on ? (ch.userData.base || ch.material.opacity) : 0.08
          })
        })
        edges.forEach((e) => {
          const hit = focus && (e.userData.from === focus.id || e.userData.to === focus.id)
          e.material.opacity = focus ? (hit ? 0.55 : 0.03) : 0.18
        })
      }

      groups.forEach((grp) => {
        grp.traverse((ch) => {
          if (ch.material) ch.userData.base = ch.material.opacity
        })
      })

      onMove = (event) => {
        hover = pick(event)
        paint(hover || picked)
      }
      onLeave = () => {
        hover = null
        paint(picked)
      }
      onClick = (event) => {
        const node = pick(event)
        setPicked((cur) => (node && cur && cur.id === node.id ? null : node))
      }
      onResize = () => {
        if (!renderer) return
        renderer.setSize(box.clientWidth, Math.max(box.clientHeight, 1))
        updateCam()
      }

      box.addEventListener('pointermove', onMove)
      box.addEventListener('pointerleave', onLeave)
      box.addEventListener('click', onClick)
      window.addEventListener('resize', onResize)
      updateCam()

      const tick = () => {
        if (stop) return
        stars.rotation.y += 0.00008
        paint(hover || picked)
        renderer.render(scene, camera)
        frame = requestAnimationFrame(tick)
      }
      tick()
    }

    run()
    return () => {
      stop = true
      cancelAnimationFrame(frame)
      if (onMove) hostRef.current?.removeEventListener('pointermove', onMove)
      if (onLeave) hostRef.current?.removeEventListener('pointerleave', onLeave)
      if (onClick) hostRef.current?.removeEventListener('click', onClick)
      if (onResize) window.removeEventListener('resize', onResize)
      renderer?.dispose()
      if (hostRef.current) hostRef.current.replaceChildren()
    }
  }, [nodes, rels, picked, setPicked])

  if (graphQ.isLoading) {
    return jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: 'Loading observatory…' })
  }
  if (!nodes.length) {
    return jsx(EmptyState, { title: 'Empty graph', description: 'No L5 entities yet. Add memories and wait for extraction.' })
  }

  return jsxs('div', {
    className: 'grid gap-3 lg:grid-cols-[1fr_240px]',
    children: [
      jsx('div', {
        ref: hostRef,
        className: 'h-[420px] overflow-hidden rounded-md border border-(--ui-stroke-secondary) bg-black/20',
      }),
      jsxs('div', {
        className: 'rounded-md border border-(--ui-stroke-secondary) p-3 text-xs',
        children: [
          jsx('div', { className: 'text-[0.65rem] tracking-wide text-(--ui-text-tertiary)', children: 'FIELD NOTE' }),
          picked
            ? jsxs('div', {
                className: 'mt-2 flex flex-col gap-2',
                children: [
                  jsx('div', { className: 'text-sm text-(--ui-text-primary)', children: picked.label }),
                  jsx(Badge, { variant: 'muted', size: 'xs', children: picked.type || 'entity' }),
                  jsx('div', { className: 'text-(--ui-text-tertiary)', children: `${nodes.length} nodes · ${rels.length} relations in view` }),
                ],
              })
            : jsx('div', { className: 'mt-2 text-(--ui-text-tertiary)', children: 'Click a node. Drag is pan-free — hover lights its neighborhood.' }),
        ],
      }),
    ],
  })
}

function hash(s) {
  let h = 2166136261
  for (let i = 0; i < s.length; i += 1) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return ((h >>> 0) % 100000) / 100000
}

function layout(nodes) {
  const n = Math.max(nodes.length, 1)
  return nodes.map((node, i) => {
    const ang = (i / n) * Math.PI * 2
    const ring = 1 + (hash(node.id + 'r') * 2)
    const rx = 70 * ring
    const rz = 42 * ring
    const y = (hash(node.id + 'y') - 0.5) * 70
    return {
      ...node,
      color: COLOR[node.type] || COLOR.default,
      x: Math.cos(ang) * rx,
      y,
      z: Math.sin(ang) * rz,
    }
  })
}

const COLOR = {
  default: '#5e9b8a',
  entity: '#5e9b8a',
  domain: '#8b7aa0',
  artifact: '#5e7b9b',
  person: '#d4c5a3',
}

let threePromise = null
function loadThree() {
  if (window.THREE) return Promise.resolve(window.THREE)
  if (threePromise) return threePromise
  threePromise = new Promise((resolve, reject) => {
    const script = document.createElement('script')
    script.src = 'https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.min.js'
    script.onload = () => resolve(window.THREE)
    script.onerror = () => reject(new Error('Failed to load Three.js'))
    document.head.appendChild(script)
  })
  return threePromise
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
          jsx(Stat, { label: 'Graph nodes', value: status && status.graph_nodes, hint: 'L5 store, not layer docs' }),
          jsx(Stat, { label: 'Graph edges', value: status && status.graph_edges }),
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
  const [picked, setPicked] = useState(null)

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

  const graphQ = useQuery({
    queryKey: ['hyatlas', 'graph'],
    queryFn: () => api.rest('/graph?n=120'),
    refetchInterval: 15000,
    enabled: tab === 'graph' || tab === 'overview',
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
    : tab === 'graph'
      ? jsx(Observatory, { graphQ, picked, setPicked })
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
                    ? `v4 · chromem-go · ${status.vdb_points} memories · ${status.graph_nodes} graph nodes`
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
