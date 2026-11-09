// Desktop half smoke test: loads desktop/plugin.js outside Hermes Desktop.
//
//   node tests/desktop/check.mjs [--modules DIR]
//
// DIR is a node_modules folder holding react + react-dom (19.x) for the render
// pass; without it the script still checks imports and registrations and skips
// rendering. A temp copy of plugin.js gets its three bare imports rewritten to
// local shims: a recording stub of @hermes/plugin-sdk and the real React.
// Set HERMES_DESKTOP_SRC to a Hermes Desktop `src` folder to also check every
// SDK name the plugin imports against the real export list.

import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync, writeFileSync, existsSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const pluginPath = resolve(here, '../../desktop/plugin.js')
const source = readFileSync(pluginPath, 'utf8')
const argModules = process.argv.indexOf('--modules')
const modulesDir = argModules > 0 ? resolve(process.argv[argModules + 1]) : resolve(here, '../../node_modules')

let passed = 0
const ok = (cond, msg) => {
  assert.ok(cond, msg)
  passed += 1
}

// ── 1. static rules ─────────────────────────────────────────────────────────

const specifiers = [...source.matchAll(/^\s*import\s[^'"]*?from\s*['"]([^'"]+)['"]/gm)].map(m => m[1])
ok(specifiers.every(s => ['@hermes/plugin-sdk', 'react', 'react/jsx-runtime'].includes(s)), `only SDK/react imports (${specifiers})`)
ok(!/\beval\(|new\s+Function\(|\bimport\(/.test(source), 'no eval / new Function / dynamic import')
ok(!/querySelector|MutationObserver|createElement\(\s*['"]script/.test(source), 'no DOM probing or script injection')
ok(!/(?<![\w.])(?:window\.)?set(?:Interval|Timeout)\(/.test(source.replace(/ctx\.set(Interval|Timeout)\(/g, '')), 'timers only via ctx')
ok(!/#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(|oklch\(/.test(source), 'no hardcoded colours')
ok(!/github\.com|githubusercontent/i.test(source), 'no GitHub URLs')
ok(!/dangerouslySetInnerHTML|innerHTML/.test(source), 'no raw HTML injection')
// Honest wording: never these overclaiming or alarming phrases in anything a person sees.
ok(!/secrets? refused|\blive\b|one[- ]click|two-way sync of instructions/i.test(source), 'banned wording absent')
ok(!/\/Users\/|\/home\/|C:\\\\/.test(source), 'no absolute home paths')
// After "Keep Hermes" the replaced Grok copy is not restorable: never claim it is.
ok(!/replaced copy is kept/i.test(source), 'no claim that the replaced copy is restorable')

const sdkNames = (() => {
  const m = source.match(/import\s*\{([^}]+)\}\s*from\s*'@hermes\/plugin-sdk'/)
  return m[1].split(',').map(s => s.trim()).filter(Boolean)
})()

if (process.env.HERMES_DESKTOP_SRC) {
  const index = readFileSync(join(process.env.HERMES_DESKTOP_SRC, 'sdk/index.ts'), 'utf8')
  const areas = readFileSync(join(process.env.HERMES_DESKTOP_SRC, 'sdk/areas.ts'), 'utf8')
  const all = index + areas
  for (const name of sdkNames) {
    const re = new RegExp(`(?:export\\s+(?:const|function|class)\\s+${name}\\b|[{,\\s]${name}\\s*[,}]|\\bas\\s+${name}\\b|^\\s*${name},?$|^export const ${name}\\b)`, 'm')
    ok(re.test(all) || (name === 'host' && /export const host\b/.test(index)), `SDK exports ${name}`)
  }
  // Every ctx.* / host.* member the plugin touches exists in the real contract.
  const contract = readFileSync(join(process.env.HERMES_DESKTOP_SRC, 'contrib/plugin.ts'), 'utf8')
  const ctxMembers = new Set([...source.matchAll(/\bctx\.([a-zA-Z]+)/g)].map(m => m[1]))
  for (const m of ctxMembers) {
    ok(new RegExp(`^\\s*(?:readonly\\s+)?${m}\\??:`, 'm').test(contract), `PluginContext has ${m}`)
  }
  const hostBlock = index.slice(index.indexOf('export const host = {'))
  const hostMembers = new Set([...source.matchAll(/\bhost\.([a-zA-Z]+)/g)].map(m => m[1]))
  for (const m of hostMembers) {
    ok(new RegExp(`^\\s*${m}(?:,|:|\\()`, 'm').test(hostBlock), `host has ${m}`)
  }
  ok(/get<T>\(key: string, fallback: T\)/.test(contract) && /remove\(key: string\)/.test(contract), 'PluginStorage get/set/remove')
}

// ── 2. stubs ────────────────────────────────────────────────────────────────

const work = mkdtempSync(join(tmpdir(), 'twin-desktop-'))
const haveReact = existsSync(join(modulesDir, 'react/package.json')) && existsSync(join(modulesDir, 'react-dom/package.json'))
const req = haveReact ? createRequire(join(modulesDir, 'x.js')) : null

function shim(file, mod) {
  const keys = Object.keys(req(mod)).filter(k => /^[A-Za-z_$][\w$]*$/.test(k) && k !== 'default')
  writeFileSync(
    join(work, file),
    `import { createRequire } from 'node:module'\nconst m = createRequire(${JSON.stringify(join(modulesDir, 'x.js'))})(${JSON.stringify(mod)})\nexport default m\nexport const { ${keys.join(', ')} } = m\n`
  )
}

if (haveReact) {
  shim('react.mjs', 'react')
  shim('jsx.mjs', 'react/jsx-runtime')
} else {
  // Minimal stand-ins so the module still evaluates and registers.
  writeFileSync(join(work, 'react.mjs'), 'export const useState = v => [typeof v === "function" ? v() : v, () => {}]\nexport const useEffect = () => {}\nexport const useMemo = f => f()\n')
  writeFileSync(join(work, 'jsx.mjs'), 'export const jsx = (t, p) => ({ t, p })\nexport const jsxs = jsx\n')
}

// SDK stub: components are thin pass-throughs that keep text and a few props
// visible in the SSR output; hooks read fixtures from globalThis.__h.
const sdkStub = `
import { jsx, jsxs } from './jsx.mjs'
const H = globalThis.__h
const pass = tag => ({ children, className, ...p }) => jsx(tag, { className, 'data-props': Object.keys(p).filter(k => typeof p[k] !== 'function' && k !== 'loading').map(k => k + '=' + String(p[k])).join(' '), children })
export const Badge = pass('span')
export const Button = ({ children, disabled, loading, onClick, ...p }) => { H.buttons.push({ text: H.text(children), onClick, disabled: Boolean(disabled || loading), variant: p.variant ?? 'default' }); return jsx('button', { disabled: Boolean(disabled || loading), 'aria-label': p['aria-label'], children }) }
export const Checkbox = p => jsx('input', { type: 'checkbox', readOnly: true, checked: Boolean(p.checked), disabled: Boolean(p.disabled), id: p.id })
export const cn = (...a) => a.flat(Infinity).filter(Boolean).join(' ')
export const Codicon = ({ name }) => jsx('i', { 'data-codicon': name })
export const ConfirmDialog = p => { if (p.open) H.confirms.push(p); return p.open ? jsxs('div', { role: 'alertdialog', children: [jsx('h2', { children: p.title }), jsx('p', { children: p.description }), p.children, jsx('button', { children: p.confirmLabel })] }) : null }
export const CopyButton = p => jsx('button', { 'aria-label': p.label, 'data-copy': typeof p.text === 'string' ? p.text.length : 0, children: p.showLabel ? p.label : 'copy' })
export const Dialog = ({ open, children }) => open ? jsx('div', { role: 'dialog', children }) : null
export const DialogContent = pass('div')
export const DialogDescription = pass('p')
export const DialogFooter = pass('footer')
export const DialogHeader = pass('header')
export const DialogTitle = pass('h2')
export const DisclosureCaret = () => jsx('i', {})
export const ErrorState = ({ title, description, children }) => jsxs('div', { children: [jsx('h2', { children: title }), jsx('p', { children: description }), children] })
export const fmtDateTime = new Intl.DateTimeFormat('en', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC' })
export const host = H.host
export const Input = p => jsx('input', { id: p.id, value: p.value, placeholder: p.placeholder, readOnly: true })
export const PALETTE_AREA = 'palette'
export const queryClient = H.queryClient
export const relativeTime = ms => { const m = Math.round((Date.now() - ms) / 60000); return m < 1 ? 'just now' : m + ' min ago' }
export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar.nav'
export const Skeleton = () => jsx('div', { 'data-skeleton': true })
export const STATUSBAR_AREAS = { left: 'statusBar.left', right: 'statusBar.right' }
export const StatusDot = ({ tone }) => jsx('span', { 'data-tone': tone })
export const Tip = ({ label, children }) => jsxs('span', { 'data-tip': typeof label === 'string' ? label : '', children: [children] })
export const useQuery = opts => H.useQuery(opts)
export const useQueryClient = () => H.queryClient
`
writeFileSync(join(work, 'sdk.mjs'), sdkStub)

const testable = source
  .replace(/from\s*'@hermes\/plugin-sdk'/, `from './sdk.mjs'`)
  .replace(/from\s*'react\/jsx-runtime'/, `from './jsx.mjs'`)
  .replace(/from\s*'react'/, `from './react.mjs'`)
  // Harness-only: expose inner components of the temp copy for direct renders.
  .concat('\nexport const __parts = { AddTwinsDialog, RestoreDialog, SwitchDialog, ParityPanel, RecentList, PairCard, StatusChip, TwinsPage, MirrorDialog, RemoveTwinDialog, WaitingList, ConflictItem, ConflictCard, cronText, markDifferences, profileClash, reconcileConflictToasts }\n')
writeFileSync(join(work, 'plugin.mjs'), testable)

// ── 3. fixtures (invented data only) ────────────────────────────────────────

const iso = minsAgo => new Date(Date.now() - minsAgo * 60_000).toISOString()
const coverage = [
  { item: 'name', g2h: 'syncs', h2g: 'by_hand' },
  { item: 'description', g2h: 'syncs', h2g: 'by_hand' },
  { item: 'instructions', g2h: 'syncs_via_mirror', h2g: 'by_hand' },
  { item: 'skills', g2h: 'syncs_via_mirror', h2g: 'syncs_via_mirror' },
  { item: 'routines', g2h: 'by_hand', h2g: 'by_hand' },
  { item: 'connectors', g2h: 'not_carried', h2g: 'not_carried' },
  { item: 'activity', g2h: 'syncs_via_mirror', h2g: 'syncs_via_mirror' }
]
const acme = {
  pair_id: 'acme-helper', demo: true, grok: { id: 'b-acme-0001', name: 'Acme Helper', present: true }, hermes: { profile: 'acme-helper' },
  state: 'needs_you', state_reason: '1 change edited on both sides', last_sync_at: iso(2), pending: { to_hermes: 0, to_grok: 1, waiting_approval: 0 },
  conflicts: 1, coverage, by_hand: [{ item: 'routine:Morning summary', side: 'hermes', what: 'Recreate routine "Morning summary" in Hermes (created paused)' }],
  restore_points: 3, secrets_withheld_total: 1, parity: { passed: 9, total: 10 }, mirror_bot: { connected: true, last_seen_at: iso(4) },
  secrets_withheld: [{ item: 'persona.description', kind: 'description', direction: 'g2h', location: 'suspected secret in the description: token-shaped value (a check, not a guarantee)' }],
  waiting: []
}
const recipe = {
  pair_id: 'recipe-planner', demo: true, grok: { id: 'b-recipe-0002', name: 'Recipe Planner <b>bold</b>\u0007', present: true }, hermes: { profile: 'recipe-planner' },
  state: 'in_step', state_reason: '', last_sync_at: iso(1), pending: { to_hermes: 0, to_grok: 0 }, conflicts: 0, coverage, by_hand: [],
  restore_points: 1, secrets_withheld_total: 0, parity: null, mirror_bot: { connected: false }
}
const garden = {
  pair_id: 'garden-buddy', demo: true, grok: { id: 'b-garden-0003', name: 'Garden Buddy', present: true }, hermes: { profile: 'garden-buddy' },
  state: 'changes_waiting', state_reason: '1 change for Grok', last_sync_at: iso(7), pending: { to_hermes: 0, to_grok: 1, in_flight: 1, waiting_approval: 1 },
  conflicts: 0, coverage, by_hand: [], restore_points: 0, secrets_withheld_total: 0, mirror_bot: { connected: false },
  waiting: [{ item: 'skill:seed-calendar', reason: 'needs_approval', detail: 'New skill from Grok: approve before it is installed' }]
}
const HINT = 'Install botport (MIT) from its GitHub releases and verify it with `gh attestation verify`, then set botport_path in the plugin settings if it is not on PATH. Without botport you can still try the demo: `hermes twin demo`.'
const statusDemo = { mode: 'demo', engine: { available: false, version: null, problem: 'botport not found on PATH', install_hint: HINT }, pairs: [acme, recipe, garden], last_check_at: iso(0.5), interval_s: 60, profile: 'default' }
const statusSetup = { ...statusDemo, mode: 'setup', pairs: [] }
const statusUntrusted = { ...statusSetup, engine: { available: false, version: '0.9.0', problem: 'botport is writable by another user; refusing to run it', install_hint: HINT } }
const fixtures = {
  conflicts: { conflicts: [{ conflict_id: 'skill:greet-visitors', kind: 'skill', name: 'greet-visitors', since: iso(30), hermes: { digest: '3f2a9c1b7e40', text: 'Ask for their name.\nSay a cheerful hello.' }, grok: { digest: '9b1c00aa4411', text: null }, diff: '--- grok\n+++ hermes\n@@ -1 +1 @@\n-Say a warm hello.\n+Say a cheerful hello.' }] },
  parity: { passed: 9, total: 10, checks: [{ id: 'p1', name: 'Knows its name', pass: true, detail: '' }, { id: 'p2', name: 'Greets by name', pass: false, detail: 'Grok says warm, Hermes says cheerful' }] },
  recent: { entries: [{ at: iso(3), side: 'grok', kind: 'name_change', summary: 'Description updated' }] },
  'restore-points': { points: [{ id: 'persona.name@8ef22c6d10ba', item: 'persona.name', kind: 'name', name: 'name', at: iso(10), reason: 'kept copy', scope: 'item' }, { id: 'skill:greet-visitors@3f2a9c1b7e40', at: iso(5), reason: 'kept copy', scope: 'item', item: 'skill:greet-visitors' }] },
  'remove-plan': { pair_id: 'recipe-planner', dry_run: true, profile_kept: true, removed: ['state:pairs/recipe-planner.toml', 'state:pairs/recipe-planner.key', 'hermes:profiles/recipe-planner/botport'] },
  checklist: { by_hand: acme.by_hand, switch: { grok_routines_to_turn_off: [{ name: 'Morning summary' }], hermes_jobs_paused: [{ job_id: 'job-1', name: 'Morning summary', schedule: 'every day 08:00' }, { job_id: 'job-2', name: 'Acme Helper: Water reminder with a rather long name that must wrap, not be cut', schedule: '0 8 * * 1-5' }, { job_id: 'job-3', name: 'Weekly tidy', schedule: '0 11 * * 5' }] } },
  'grok-bots': { app_found: true, bots: [{ grok_id: 'b-acme-0001', name: 'Acme Helper', description: 'Answers questions about Acme widgets.', updated_at: iso(60), twinned_pair_id: 'acme-helper' }, { grok_id: 'b-garden-0003', name: 'Garden Buddy', description: 'Plans the vegetable patch.', updated_at: iso(90), twinned_pair_id: null }] }
}

// ── 4. recording host / ctx ─────────────────────────────────────────────────

const H = (globalThis.__h = {
  mode: 'pending',
  status: null,
  buttons: [],
  confirms: [],
  notes: [],
  nav: [],
  restCalls: [],
  restReply: async () => ({}),
  text: c => (Array.isArray(c) ? c.map(H.text).join('') : typeof c === 'string' || typeof c === 'number' ? String(c) : c?.props?.children ? H.text(c.props.children) : ''),
  host: {
    notify: n => H.notes.push(n),
    notifyError: (e, title) => H.notes.push({ kind: 'error', title, message: String(e?.message ?? e) }),
    navigate: p => H.nav.push(p)
  },
  queryClient: {
    getQueryData: () => H.status,
    invalidateQueries: async () => { H.invalidations = (H.invalidations ?? 0) + 1 },
    // fresh /status read after a sync: the reply (if any) becomes the cached status
    fetchQuery: async ({ queryKey }) => { H.fetches = [...(H.fetches ?? []), queryKey[1]]; if (H.freshStatus) H.status = H.freshStatus; return H.status }
  },
  override: {},
  useQuery: ({ queryKey, enabled = true }) => {
    const base = { refetch: async () => {}, isFetching: false, error: null }
    const which = queryKey[1]
    if (H.override[which]) return { ...base, isPending: false, isError: false, ...H.override[which] }
    if (!enabled) return { ...base, isPending: true, isError: false, data: undefined }
    if (which === 'status') {
      if (H.mode === 'pending') return { ...base, isPending: true, isError: false, data: undefined }
      if (H.mode === 'error') return { ...base, isPending: false, isError: true, error: new Error('404 Not Found'), data: undefined }
      return { ...base, isPending: false, isError: false, data: H.status }
    }
    return { ...base, isPending: false, isError: false, data: fixtures[which] }
  }
})

const contributions = []
const listeners = {}
const intervals = []
const ctx = {
  rest: async (path, opts) => { H.restCalls.push({ path, opts }); return H.restReply(path, opts) },
  register: c => { contributions.push(c); return () => {} },
  registerMany: cs => { contributions.push(...cs); return () => {} },
  onEvent: (type, fn) => { listeners[type] = fn; return () => {} },
  onDispose: () => {},
  setInterval: (fn, ms) => { intervals.push({ fn, ms }); return () => {} },
  setTimeout: (fn) => { fn(); return () => {} },
  storage: { get: (_k, f) => f, set: () => {}, remove: () => {} }
}

// ── 5. load + register ──────────────────────────────────────────────────────

const mod = await import(pathToFileURL(join(work, 'plugin.mjs')).href)
const plugin = mod.default
ok(plugin.id === 'grok-bot-twin' && plugin.name === 'Grok Bot Twin' && typeof plugin.register === 'function', 'default export shape')
plugin.register(ctx)

const byArea = a => contributions.filter(c => c.area === a)
ok(byArea('routes').length === 1 && byArea('routes')[0].data.path === '/twins' && typeof byArea('routes')[0].render === 'function', 'route /twins')
ok(byArea('sidebar.nav').length === 1 && byArea('sidebar.nav')[0].data.label === 'Twins' && byArea('sidebar.nav')[0].data.codicon === 'mirror', 'sidebar row')
ok(byArea('statusBar.right').length === 1 && typeof byArea('statusBar.right')[0].render === 'function', 'status-bar item')
const palette = byArea('palette').map(c => c.data.label)
ok(JSON.stringify(palette) === JSON.stringify(['Twin: sync now', 'Twin: open Twins', 'Twin: try demo']), `palette rows ${palette}`)
ok(new Set(contributions.map(c => c.id)).size === contributions.length, 'unique contribution ids')
ok(['synced', 'secrets_withheld', 'conflict', 'changed'].every(t => typeof listeners[`plugin.grok-bot-twin.${t}`] === 'function'), 'event listeners')
ok(intervals.length === 1 && intervals[0].ms === 5000, 'events poll every 5 s via ctx.setInterval')

// ── 6. events → toasts, deduped across socket + poll ────────────────────────

H.status = statusDemo
H.notes = []
listeners['plugin.grok-bot-twin.synced']({ type: 'plugin.grok-bot-twin.synced', payload: { pair_id: 'acme-helper', at: '2000-01-05T09:30:00Z', applied: [{ item: 'persona.name', kind: 'name', direction: 'g2h' }, { item: 'persona.description', kind: 'description', direction: 'g2h' }] } })
ok(H.notes.at(-1)?.message === 'Acme Helper: name & description updated from Grok', `synced toast: ${H.notes.at(-1)?.message}`)
listeners['plugin.grok-bot-twin.secrets_withheld']({ payload: { pair_id: 'acme-helper', at: '2000-01-05T09:30:01Z', count: 1, secrets_withheld: [{ item: 'persona.description', kind: 'description', location: 'line 1', what: 'api key' }] } })
// The "not a guarantee" note is in the visible message itself, not behind Details.
ok(H.notes.at(-1)?.message === '1 suspected secret held back — a check, not a guarantee.' && H.notes.at(-1)?.meta === "Acme Helper's description was not copied to Hermes." && !H.notes.at(-1)?.detail, `secret toast: ${JSON.stringify(H.notes.at(-1))}`)
// Python event shape: kinds + items only, pair_id inside the payload.
listeners['plugin.grok-bot-twin.secrets_withheld']({ payload: { pair_id: 'recipe-planner', count: 2, message: '2 suspected secrets held back', items: ['persona.description', 'persona.instructions'] } })
ok(H.notes.at(-1)?.message === '2 suspected secrets held back — a check, not a guarantee.' && /description & instructions were not copied/.test(H.notes.at(-1)?.meta), `secrets toast (python shape): ${JSON.stringify(H.notes.at(-1))}`)
listeners['plugin.grok-bot-twin.synced']({ payload: { pair_id: 'garden-buddy', name: 'Garden Buddy', kinds: ['description', 'name'], count: 2, message: 'x' } })
ok(H.notes.at(-1)?.message === 'Garden Buddy: name & description updated from Grok', `synced toast (kinds shape): ${H.notes.at(-1)?.message}`)
listeners['plugin.grok-bot-twin.conflict']({ payload: { pair_id: 'acme-helper', at: '2000-01-05T09:30:02Z', count: 1 } })
ok(H.notes.at(-1)?.message === 'Acme Helper needs you: 1 change edited on both sides', 'conflict toast')
const before = H.notes.length
// The poll returns the same three events: no new toasts.
H.restReply = async path => path.startsWith('/events') ? { events: [
  { at: '2000-01-05T09:30:00Z', type: 'synced', pair_id: 'acme-helper', payload: { applied: [{ item: 'persona.name', kind: 'name', direction: 'g2h' }] } },
  { at: '2000-01-05T09:30:02Z', type: 'conflict', pair_id: 'acme-helper', payload: { count: 1 } },
  { at: '2000-01-05T09:31:00Z', type: 'changed', pair_id: 'acme-helper', payload: {} }
] } : {}
intervals[0].fn()
await new Promise(r => setImmediate(r))
await new Promise(r => setImmediate(r))
ok(H.notes.length === before, `poll duplicates suppressed (${H.notes.length - before} new)`)
ok(H.restCalls.some(c => c.path.startsWith('/events?since=')), 'polls /events?since=')
// the conflict toast has a stable id per twin; a fresh status without the conflict takes it down.
{
  const R = mod.__parts.reconcileConflictToasts
  const cid = 'grok-bot-twin:conflict:acme-helper'
  ok(H.notes.some(n => n.id === cid && n.kind === 'warning' && n.action?.label === 'Pick one'), 'conflict toast: stable id per twin + Pick one')
  let m = H.notes.length
  R([{ ...acme, conflicts: 1 }], Date.now() + 10_000)
  ok(H.notes.length === m, 'conflict toast: same count, untouched')
  R([{ ...acme, conflicts: 2 }], Date.now() + 10_000)
  ok(H.notes.length === m + 1 && H.notes.at(-1)?.id === cid && H.notes.at(-1)?.kind === 'warning' && /2 changes edited on both sides/.test(H.notes.at(-1)?.message), `conflict toast updated in place: ${JSON.stringify(H.notes.at(-1))}`)
  m = H.notes.length
  R([{ ...acme, conflicts: 0 }], Date.now() + 1_000)
  ok(H.notes.length === m, 'a status read right after the toast does not take it down')
  R([{ ...recipe }], Date.now() + 10_000)
  ok(H.notes.length === m + 1 && H.notes.at(-1)?.id === cid && H.notes.at(-1)?.durationMs === 1 && H.notes.at(-1)?.kind !== 'warning', `conflict toast dismissed when the twin is gone: ${JSON.stringify(H.notes.at(-1))}`)
  R([], Date.now() + 10_000)
  ok(H.notes.length === m + 1, 'dismissed once only')
}
ok(H.notes.every(n => !/sk-|secret value/i.test(JSON.stringify(n))), 'no secret values in toasts')

// Palette: sync now → POST /sync and a result toast; try demo → navigates.
H.restCalls = []
H.restReply = async path => path === '/sync' ? { pairs: [{ pair_id: 'calm-bot', applied: [], secrets_withheld: [], conflicts: [], state: 'in_step' }] } : { ok: true, pairs: [] }
byArea('palette')[0].data.run()
await new Promise(r => setImmediate(r))
ok(H.restCalls[0]?.path === '/sync' && H.restCalls[0]?.opts?.method === 'POST', 'palette sync posts /sync')
ok(H.notes.at(-1)?.message === 'Already in step', 'palette sync toast')
// Sync now right after the background check toasted a secret/conflict for that twin:
// no "Already in step" next to it, and no second toast either.
let mark = H.notes.length
H.restReply = async path => path === '/sync' ? { pairs: [{ pair_id: 'acme-helper', applied: [], secrets_withheld: [], conflicts: [], state: 'needs_you' }] } : {}
byArea('palette')[0].data.run()
await new Promise(r => setImmediate(r))
ok(H.notes.length === mark, `no toast after a recent needs-you toast (${JSON.stringify(H.notes.slice(mark))})`)
// A twin that still has changes waiting (no recent event toast): its reason, not "Already in step".
H.restReply = async path => path === '/sync' ? { pairs: [{ pair_id: 'garden-buddy', applied: [], secrets_withheld: [], conflicts: [], state: 'changes_waiting', to_grok: 1 }] } : {}
byArea('palette')[0].data.run()
await new Promise(r => setImmediate(r))
ok(H.notes.length === mark + 1 && H.notes.at(-1)?.message === 'Garden Buddy: 1 change for Grok', `changes-waiting reason instead of in step: ${H.notes.at(-1)?.message}`)
// the palette sync reads /status fresh before it judges (engine said in_step, the fresh status says a
// change is waiting -> its reason, never "Already in step"), then refreshes the cards.
H.fetches = []
H.invalidations = 0
const plants = { ...recipe, pair_id: 'garden-buddy-2', grok: { id: 'b-garden-0004', name: 'Window Plants', present: true } }
H.freshStatus = { ...statusDemo, pairs: [{ ...plants, state: 'changes_waiting', state_reason: '1 change for Grok' }] }
H.restReply = async path => path === '/sync' ? { pairs: [{ pair_id: 'garden-buddy-2', applied: [], secrets_withheld: [], conflicts: [], state: 'in_step' }] } : {}
mark = H.notes.length
byArea('palette')[0].data.run()
await new Promise(r => setImmediate(r))
await new Promise(r => setImmediate(r))
ok(H.fetches.includes('status') && H.invalidations > 0, `palette sync: fresh status read + cards refreshed (${H.fetches} / ${H.invalidations})`)
ok(H.notes.length === mark + 1 && H.notes.at(-1)?.message === 'Window Plants: 1 change for Grok' && !/in step/i.test(H.notes.at(-1)?.message), `palette sync judged on the fresh status: ${H.notes.at(-1)?.message}`)
H.freshStatus = { ...statusDemo, pairs: [{ ...plants, state: 'in_step' }] }
mark = H.notes.length
byArea('palette')[0].data.run()
await new Promise(r => setImmediate(r))
await new Promise(r => setImmediate(r))
ok(H.notes.at(-1)?.message === 'Already in step', `every twin in step -> "Already in step": ${H.notes.at(-1)?.message}`)
H.freshStatus = null
H.status = statusDemo
// The engine reports in_step but the twin is needs_you in the status cache: still no "Already in step".
H.restReply = async path => path === '/sync' ? { pairs: [{ pair_id: 'garden-buddy', applied: [], secrets_withheld: [{ item: 'persona.description', kind: 'description', direction: 'g2h' }], conflicts: [], state: 'in_step' }] } : {}
mark = H.notes.length
byArea('palette')[0].data.run()
await new Promise(r => setImmediate(r))
ok(H.notes.slice(mark).every(n => !/in step/i.test(n.message)) && /suspected secret held back/.test(H.notes.at(-1)?.message), `secret in the sync result: no in-step claim (${JSON.stringify(H.notes.slice(mark).map(n => n.message))})`)
byArea('palette')[1].data.run()
ok(H.nav.at(-1) === '/twins', 'palette open navigates')
// sync --all: one twin failed in place, the other synced -> an error toast for that twin only, no "in step" claim.
H.restReply = async path => path === '/sync' ? { pairs: [
  { pair_id: 'recipe-planner', applied: [], secrets_withheld: [], conflicts: [], state: 'in_step', error: null },
  { pair_id: 'garden-buddy', applied: [], secrets_withheld: [], conflicts: [], state: 'error', error: { code: 'internal', message: "this twin's state file could not be read" } }] } : {}
byArea('palette')[0].data.run()
await new Promise(r => setImmediate(r))
ok(H.notes.at(-1)?.kind === 'error' && H.notes.at(-1)?.message === 'Garden Buddy: could not sync' && H.notes.at(-1)?.detail === "this twin's state file could not be read" && !H.notes.some(n => /twins already in step/.test(n.message)), `per-twin sync error toast: ${JSON.stringify(H.notes.at(-1))}`)
// Backend {error} body becomes a toast, not a crash.
H.restReply = async () => ({ error: { code: 'locked', message: 'busy' } })
byArea('palette')[0].data.run()
await new Promise(r => setImmediate(r))
await new Promise(r => setImmediate(r))
ok(H.notes.at(-1)?.kind === 'error' && H.notes.at(-1)?.message === 'Busy, try again in a moment', `locked -> busy toast: ${H.notes.at(-1)?.message}`)
// Backend off: rest throws → notifyError.
H.restReply = async () => { throw new Error('404 Not Found') }
byArea('palette')[0].data.run()
await new Promise(r => setImmediate(r))
await new Promise(r => setImmediate(r))
ok(H.notes.at(-1)?.kind === 'error', 'transport failure -> notifyError')

// ── 7. render (needs react-dom) ─────────────────────────────────────────────

if (!haveReact) {
  console.log(`SKIP render: no react/react-dom under ${modulesDir} (pass --modules DIR)`)
} else {
  const { renderToStaticMarkup } = req('react-dom/server')
  const { jsx } = req('react/jsx-runtime')
  const render = (node) => { H.buttons = []; return renderToStaticMarkup(node) }
  const page = () => render(byArea('routes')[0].render())
  const chip = () => render(byArea('statusBar.right')[0].render())
  const text = html => html.replace(/<[^>]+>/g, ' ').replace(/&#x27;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, '&').replace(/\s+/g, ' ')

  H.mode = 'pending'
  let html = page()
  ok(html.includes('data-skeleton') && text(html).includes('Twins') && text(html).includes('Try Hermes without leaving Grok. Switch when your twin is ready.'), 'loading: header + skeletons')

  H.mode = 'error'
  html = page()
  ok(text(html).includes("Enable the plugin's Python half: hermes plugins enable grok-bot-twin, then restart the gateway."), 'backend off message')
  ok(H.buttons.some(b => b.text === 'Retry'), 'backend off retry')
  ok(/Twin service is off/.test(chip()), 'status chip when backend off')

  H.mode = 'ok'
  H.status = statusSetup
  html = text(page())
  for (const s of ['No twins yet', 'Try demo', 'Add twins from Grok', 'One step left', 'Problem: botport not found on PATH', 'gh attestation verify', 'Runs inside Hermes.', 'No network calls.', 'Learn more']) {
    ok(html.includes(s), `setup/empty shows "${s}"`)
  }
  ok(/<code[^>]*>gh attestation verify<\/code>/.test(page()), 'install hint renders commands as code')
  ok(H.buttons.filter(b => b.text === 'Try demo').length === 1 && H.buttons.some(b => b.text === 'Add twins from Grok'), 'empty state: one Try demo + Add twins from Grok')

  // Engine found but not trusted: a clear setup card, demo still offered.
  H.status = statusUntrusted
  html = text(page())
  for (const s of ['botport needs attention before real twins', 'cannot be used yet', 'Problem: botport is writable by another user', 'gh attestation verify', 'Try demo', 'No twins yet']) {
    ok(html.includes(s), `untrusted engine shows "${s}"`)
  }
  ok(/No twins — click to try the demo/.test(chip()), 'status chip with no twins')

  H.status = statusDemo
  let raw = page()
  html = text(raw)
  for (const s of ['Acme Helper', 'acme-helper', 'Demo', 'Needs you', 'In step', 'Changes waiting', 'Last sync 2 min ago', '1 change for Grok', 'Mirror bot connected (demo)', 'Mirror bot not set up', 'skills Via Mirror routine (not set up)',
    '1 suspected secret held back', 'Grok → Hermes', 'Hermes → Grok', 'name Syncs', 'name By hand', 'instructions Via Mirror routine', 'connectors Not carried', 'Setup parity 9/10', 'Pick one', 'Keep Hermes', 'Keep Grok',
    'Binary file, no preview', 'Say a cheerful hello.', 'Demo controls', 'Simulate Grok edit', 'Simulate conflict', 'Simulate leaked key', 'Remove demo', 'Sync now', 'Add twins',
    'Send to Grok', 'Ready to switch', 'Restore points 3', 'Recent activity', 'Demo mode: invented bots', 'Set up botport', 'Waiting for your OK', 'seed-calendar (skill)', 'New skill from Grok',
    'Approve', '1 with the Mirror bot', '1 waiting for your OK', 'Remove twin', 'Manage', 'Set up']) {
    ok(html.includes(s), `demo page shows "${s}"`)
  }
  ok(!/\bParity 9/.test(html.replace('Setup parity 9', '')), 'parity is labelled Setup parity')
  ok(raw.includes('aria-label="Approve seed-calendar (skill)"') && raw.includes('aria-label="Sync Acme Helper now"'), 'action buttons carry aria labels')
  ok(/suspected secret in the description/.test(raw), 'secret location in badge tooltip')
  ok(!raw.includes('<b>bold</b>') && raw.includes('&lt;b&gt;bold&lt;/b&gt;'), 'untrusted name rendered as text')
  ok(!/\u0007/.test(raw), 'control chars stripped')
  // Demo mode: one calm line of fixed height, botport details only behind "Set up botport".
  ok(/aria-label="Demo mode"[^>]*class="[^"]*\bh-7\b/.test(raw) && !html.includes('Problem:') && !html.includes('gh attestation') && !html.includes('These twins use invented bots'), 'demo mode: single fixed-height line, no banner')
  ok((html.match(/Demo mode/g) ?? []).length === 1, 'demo mode said once')
  // Coverage: arrows only in the two row labels per card, never inside chips.
  ok((html.match(/→/g) ?? []).length === 6 && !/⇄/.test(html) && (html.match(/Grok → Hermes/g) ?? []).length === 3, `coverage rows by direction (${(html.match(/→/g) ?? []).length} arrows)`)
  // Conflict card: no digests, no unified-diff headers, diff behind a toggle.
  ok(!/3f2a9c1b7e40|9b1c00aa4411/.test(raw) && !/\+\+\+ hermes|--- grok|@@ -1/.test(raw) && H.buttons.some(b => b.text === 'Show diff'), 'conflict card: no hashes, no diff headers, Show diff toggle')
  ok(html.includes('Keep Hermes: the Hermes copy stays and Grok gets it on the by-hand list. Keep Grok: Grok’s copy replaces Hermes (the old Hermes copy becomes a restore point).'), 'conflict card: honest keep wording')
  ok(/data-tone="warn"/.test(raw) && /data-tone="good"/.test(raw), 'state dots')
  ok(/3 twins · 1 needs you/.test(chip()) && /data-tone="warn"/.test(chip()) && /aria-label="Twins: 3 twins/.test(chip()) && />Twin</.test(chip()), 'status chip summary')

  // the restore-point count sits in a fixed-width slot that is there even at 0 (invisible), so the
  // action row keeps its width when the first restore point appears.
  ok(raw.includes('aria-label="Restore points for Acme Helper (3)"') && /min-w-5[^"]*"[^>]*data-count="3"[^>]*>3</.test(raw), 'restore count: fixed-width badge')
  {
    const zero = render(jsx(mod.__parts.PairCard, { pair: { ...garden, restore_points: 0 }, storage: ctx.storage }))
    ok(/class="[^"]*min-w-5[^"]*invisible[^"]*"[^>]*data-count="0"/.test(zero) && zero.includes('aria-label="Restore points for Garden Buddy"'), 'restore count slot reserved (invisible) at 0')
  }

  // Single states.
  for (const [pair, label, tone] of [[recipe, 'In step', 'good'], [garden, 'Changes waiting', 'warn'], [acme, 'Needs you', 'warn'], [{ ...recipe, state: 'error', error: { code: 'internal', message: 'Engine said no' } }, 'Error', 'bad']]) {
    const one = render(jsx(mod.__parts.PairCard, { pair, storage: ctx.storage }))
    ok(text(one).includes(label) && one.includes(`data-tone="${tone}"`), `pair card state ${label}`)
  }
  ok(text(render(jsx(mod.__parts.PairCard, { pair: { ...recipe, state: 'error', error: { code: 'internal', message: 'Engine said no' } }, storage: ctx.storage }))).includes('Engine said no'), 'pair error message shown')
  // Status text once: the reason is shown only when it says something the label/meta line does not.
  const count = (h, s) => h.split(s).length - 1
  let card = text(render(jsx(mod.__parts.PairCard, { pair: { ...recipe, state_reason: 'In step' }, storage: ctx.storage })))
  ok(count(card, 'In step') === 1, `"In step" once (${count(card, 'In step')})`)
  card = text(render(jsx(mod.__parts.PairCard, { pair: garden, storage: ctx.storage })))
  ok(count(card, '1 change for Grok') === 1 && count(card, 'Changes waiting') === 1, 'changes-waiting reason not repeated')
  card = text(render(jsx(mod.__parts.PairCard, { pair: acme, storage: ctx.storage })))
  ok(card.includes('1 change edited on both sides'), 'distinct reason still shown')

  // Approve posts the item and toasts.
  H.restCalls = []
  H.restReply = async path => (path.endsWith('/approve') ? { approved: [{ item: 'skill:seed-calendar', to: 'hermes' }], sync: { applied: [{ item: 'skill:seed-calendar', kind: 'skill', direction: 'g2h' }], secrets_withheld: [], conflicts: [], state: 'in_step' } } : {})
  render(jsx(mod.__parts.WaitingList, { pair: garden }))
  await H.buttons.find(b => b.text === 'Approve').onClick()
  await new Promise(r => setImmediate(r))
  ok(H.restCalls[0]?.path === '/pairs/garden-buddy/approve' && H.restCalls[0]?.opts?.body?.item === 'skill:seed-calendar', 'approve posts {item}')
  ok(H.notes.some(n => n.message === 'Garden Buddy: skill updated from Grok') && H.notes.at(-1)?.message === 'Approved seed-calendar (skill)', 'approve toasts')

  const P = mod.__parts
  let add = render(jsx(P.AddTwinsDialog, { open: true, onClose: () => {} }))
  html = text(add)
  ok(html.includes('Add twins from Grok') && html.includes('Nothing is twinned until you pick it') && html.includes('Select all') && !html.includes('agent-importers') && html.includes('Already a twin') && html.includes('Garden Buddy'), 'add twins dialog')
  ok(H.buttons.find(b => b.text === 'Preview')?.disabled === true, 'preview disabled until a bot is picked')
  ok((add.match(/type="checkbox"[^>]*checked/g) ?? []).length === 1, 'no bot preselected (only the existing twin is ticked)')
  ok(html.includes('90 min ago'), 'add twins list: relative time')
  // demo bots listed while app_found is false (botport skipped) -> the list, not "app not found".
  H.override['grok-bots'] = { data: { app_found: false, bots: [{ grok_id: 'b-recipe-0002', name: 'Recipe Planner', description: 'Plans meals.', updated_at: iso(12), twinned_pair_id: null, demo: true }] } }
  html = text(render(jsx(P.AddTwinsDialog, { open: true, onClose: () => {} })))
  ok(html.includes('Recipe Planner') && html.includes('12 min ago') && !html.includes('was not found'), `demo bots listed, no app-not-found note: ${html.slice(0, 200)}`)
  H.override['grok-bots'] = { data: { app_found: false, bots: [] } }
  html = text(render(jsx(P.AddTwinsDialog, { open: true, onClose: () => {} })))
  ok(html.includes('The Grok desktop app was not found'), 'empty list + no app -> the note')
  H.override = {}
  // a profile that already backs a twin (or is used twice in the preview) is flagged inline; Create is off.
  {
    const two = { profiles_in_use: ['default'], skipped: [], pairs: [
      { grok: { id: 'b-recipe-0002', name: 'Recipe Planner' }, hermes: { profile: 'default' }, items: [], secrets_withheld: [], schedules_paused: 0 },
      { grok: { id: 'b-garden-0003', name: 'Garden Buddy' }, hermes: { profile: 'garden-buddy' }, items: [], secrets_withheld: [], schedules_paused: 0 }] }
    html = text(render(jsx(P.AddTwinsDialog, { open: true, onClose: () => {}, initialPreview: two })))
    ok(html.includes('This profile already backs a twin. Pick another name.') && H.buttons.find(b => /^Create/.test(b.text))?.disabled === true, 'taken profile warned inline, create disabled')
    const c = P.profileClash(two, { 'b-recipe-0002': 'garden-buddy', 'b-garden-0003': 'garden-buddy' })
    ok(/Another bot here uses this profile/.test(c['b-recipe-0002']) && /Another bot/.test(c['b-garden-0003']), 'same profile twice in one preview is flagged')
    ok(Object.keys(P.profileClash(two, { 'b-recipe-0002': 'recipe-planner', 'b-garden-0003': 'garden-buddy' })).length === 0, 'distinct new profiles pass')
  }
  const preview = { skipped: [{ grok_id: 'b-acme-0001', name: 'Acme Helper', reason: 'already_twinned' }], pairs: [{
    grok: { id: 'b-garden-0003', name: 'Garden Buddy' }, hermes: { profile: 'garden-buddy' }, schedules_paused: 2,
    items: [
      { item: 'persona.name', kind: 'name', name: 'name', action: 'create', owner: 'grok', preview: 'Garden Buddy' },
      { item: 'skill:seed-calendar', kind: 'skill', name: 'seed-calendar', action: 'create', owner: 'grok', scan: 'caution', preview: '--- SKILL.md\nkey: [line hidden by botport: suspected secret]' },
      { item: 'routine:Water reminder', kind: 'routine', name: 'Water reminder', action: 'by_hand', owner: 'grok' }
    ],
    secrets_withheld: [{ item: 'persona.instructions', kind: 'instructions', location: 'suspected secret in the instructions: line 3 (a check, not a guarantee)' }]
  }] }
  add = render(jsx(P.AddTwinsDialog, { open: true, onClose: () => {}, initialPreview: preview }))
  html = text(add)
  for (const s of ['Preview twins', 'Nothing has been written yet', 'Skipped: Acme Helper (already a twin)', 'Hermes profile', 'seed-calendar', 'scan: caution', 'by hand',
    '1 suspected secret will be held back', 'instructions: suspected secret in the instructions: line 3', '2 schedules arrive paused', 'Create twins']) {
    ok(html.includes(s), `preview shows "${s}"`)
  }
  ok(add.includes('aria-label="Show preview of seed-calendar"') && !add.includes('SKILL.md'), 'masked previews collapsed behind a toggle')
  ok(H.buttons.find(b => b.text === 'Create twins')?.disabled === false, 'create enabled after preview')
  H.confirms = []
  raw = render(jsx(P.RestoreDialog, { open: true, onClose: () => {}, pair: acme }))
  html = text(raw)
  ok(html.includes('greet-visitors (skill)') && /\bname\b/.test(html) && html.includes('min ago') && !html.includes('8ef22c6d10ba') && !html.includes('3f2a9c1b7e40') && H.buttons.filter(b => b.text === 'Restore').length === 2, 'restore points: item names + when, no digests')
  ok(raw.includes('aria-label="Restore greet-visitors (skill)"'), 'restore button names the item')
  raw = render(jsx(P.RestoreDialog, { open: true, onClose: () => {}, pair: acme, initialConfirm: 'skill:greet-visitors@3f2a9c1b7e40' }))
  html = text(raw)
  ok(html.includes('Restore this copy?') && H.buttons.some(b => b.text === 'Cancel') && H.confirms.length === 0 && !raw.includes('alertdialog'), 'restore confirm is inline, no stacked modal')
  H.restCalls = []
  H.restReply = async () => ({ restored: ['skill:greet-visitors'], restore_point: null })
  await H.buttons[H.buttons.findIndex(b => b.text === 'Cancel') - 1].onClick()
  await new Promise(r => setImmediate(r))
  ok(H.restCalls[0]?.path === '/pairs/acme-helper/restore' && H.restCalls[0]?.opts?.body?.point_id === 'skill:greet-visitors@3f2a9c1b7e40' && H.notes.at(-1)?.message === 'Restored greet-visitors (skill)', `restore toast names the item: ${H.notes.at(-1)?.message}`)
  render(jsx(P.RestoreDialog, { open: true, onClose: () => {}, pair: acme, initialConfirm: 'persona.name@8ef22c6d10ba' }))
  H.restReply = async () => ({ restored: [], restore_point: null })
  await H.buttons[H.buttons.findIndex(b => b.text === 'Cancel') - 1].onClick()
  await new Promise(r => setImmediate(r))
  ok(H.notes.at(-1)?.message === 'Nothing to restore', `restore of nothing says so: ${H.notes.at(-1)?.message}`)
  // the restored Hermes copy now waits for approval to go to Grok: the toast says so.
  render(jsx(P.RestoreDialog, { open: true, onClose: () => {}, pair: acme, initialConfirm: 'skill:greet-visitors@3f2a9c1b7e40' }))
  H.restReply = async () => ({ restored: ['skill:greet-visitors'], restore_point: 'skill:greet-visitors@aaaaaaaaaaaa', waiting_for_ok: ['skill:greet-visitors'] })
  await H.buttons[H.buttons.findIndex(b => b.text === 'Cancel') - 1].onClick()
  await new Promise(r => setImmediate(r))
  ok(H.notes.at(-1)?.message === 'Restored the Hermes copy; it now waits for your OK before going to Grok' && /greet-visitors/.test(H.notes.at(-1)?.meta), `restore toast explains the wait: ${JSON.stringify(H.notes.at(-1))}`)
  raw = render(jsx(P.SwitchDialog, { open: true, onClose: () => {}, pair: acme, storage: ctx.storage }))
  html = text(raw)
  ok(html.includes('Turn off in Grok') && html.includes('Morning summary') && html.includes('Resume in Hermes') && html.includes('every day 08:00') && html.includes('weekdays at 08:00') && html.includes('Fridays at 11:00'), 'switch dialog with plain-words schedules')
  ok(!/type="checkbox"[^>]*checked/.test(raw) && (raw.match(/type="checkbox"/g) ?? []).length === 4, 'ready to switch: nothing preselected')
  ok(!html.includes('Set up by hand') && !html.includes('Recreate routine'), 'ready to switch: a routine is not repeated under Set up by hand')
  ok(H.buttons.some(b => b.text === 'Select all') && !raw.includes('truncate'), 'ready to switch: Select all helper, rows wrap')
  ok(!/0 8 \* \* 1-5|0 11 \* \* 5/.test(html), 'no raw cron shown when it has words')
  const cronCases = [['0 8 * * *', 'every day at 08:00'], ['0 8 * * 1-5', 'weekdays at 08:00'], ['0 11 * * 5', 'Fridays at 11:00'], ['30 7 * * 1,3', 'Mondays & Wednesdays at 07:30'],
    ['0 9 * * 0,6', 'weekends at 09:00'], ['*/15 * * * *', 'every 15 minutes'], ['0 9 1 * *', 'day 1 of every month at 09:00'], ['0 9 1 1 *', '0 9 1 1 *'], ['every day 08:00', 'every day 08:00'], ['61 8 * * *', '61 8 * * *']]
  for (const [cron, want] of cronCases) ok(P.cronText(cron) === want, `cron "${cron}" -> "${P.cronText(cron)}"`)
  ok(H.buttons.find(b => b.text === 'Resume selected')?.disabled === true, 'resume disabled until Grok routines are ticked')
  html = text(render(jsx(P.ParityPanel, { pair: acme })))
  ok(html.indexOf('Greets by name') < html.indexOf('Knows its name') && html.includes('Grok says warm'), 'parity: failing check first with detail')
  ok(html.includes('Setup parity') && html.includes('9/10 checks pass') && html.includes('no model is called'), 'setup parity header')

  // Conflict: side by side with word marks; the full diff (no headers) behind the toggle.
  const both = { conflict_id: 'skill:greet-visitors', kind: 'skill', name: 'greet-visitors', since: iso(30), hermes: { digest: 'c9176fde1247', text: '# Greet visitors\nSay a cheerful hello to each visitor.\nKeep it short.' }, grok: { digest: 'c354dae9eaf5', text: '# Greet visitors\nSay a warm hello to each visitor.\nKeep it short.' }, diff: '--- grok\n+++ hermes\n@@ -1,3 +1,3 @@\n # Greet visitors\n-Say a warm hello to each visitor.\n+Say a cheerful hello to each visitor.\n Keep it short.' }
  raw = render(jsx(P.ConflictItem, { pairId: 'acme-helper', conflict: both }))
  ok(/<mark[^>]*>cheerful<\/mark>/.test(raw) && /<mark[^>]*>warm<\/mark>/.test(raw) && (raw.match(/<mark/g) ?? []).length === 2, 'conflict: changed words highlighted, nothing else')
  ok(!/c9176fde1247|c354dae9eaf5|@@|\+\+\+|--- grok/.test(raw) && !raw.includes('aria-label="Differences"'), 'conflict: no digests, diff hidden by default')
  raw = render(jsx(P.ConflictItem, { pairId: 'acme-helper', conflict: both, initialShowDiff: true }))
  ok(raw.includes('aria-label="Differences"') && !/@@|\+\+\+ hermes|--- grok/.test(raw) && /ui-diff-add/.test(raw) && /ui-diff-remove/.test(raw) && H.buttons.some(b => b.text === 'Hide diff'), 'show diff: themed lines without headers')
  {
    const kh = H.buttons.find(b => b.text === 'Keep Hermes')
    const kg = H.buttons.find(b => b.text === 'Keep Grok')
    ok(kh && kg && kh.variant === kg.variant && kh.variant === 'outline', `keep buttons have equal weight (${kh?.variant} / ${kg?.variant})`)
  }
  // while the conflicts query (re)loads, or only an older empty answer exists, a neutral line;
  // "Already settled" only from a fetch made after the card appeared.
  for (const [q, want, label] of [
    [{ data: { conflicts: [] }, isFetching: true, dataUpdatedAt: Date.now() + 60_000 }, 'Loading both copies…', 'refetching'],
    [{ data: { conflicts: [] }, isFetching: false, dataUpdatedAt: 1 }, 'Loading both copies…', 'stale empty answer'],
    [{ data: { conflicts: [] }, isFetching: false, dataUpdatedAt: Date.now() + 60_000 }, 'Already settled', 'fresh empty answer']
  ]) {
    H.override.conflicts = q
    const h = text(render(jsx(P.ConflictCard, { pair: acme })))
    ok(h.includes(want) && (want === 'Already settled' || !h.includes('Already settled')), `pick-one card ${label}: ${want}`)
  }
  H.override = {}
  render(jsx(P.ConflictItem, { pairId: 'acme-helper', conflict: both }))
  H.restReply = async () => ({ resolved: 'skill:greet-visitors' })
  await H.buttons.find(b => b.text === 'Keep Hermes').onClick()
  await new Promise(r => setImmediate(r))
  ok(H.notes.at(-1)?.message === 'Kept the Hermes copy of greet-visitors' && /by-hand list/.test(H.notes.at(-1)?.meta) && !/restore/i.test(JSON.stringify(H.notes.at(-1))), `keep Hermes toast makes no restore claim: ${JSON.stringify(H.notes.at(-1))}`)
  await H.buttons.find(b => b.text === 'Keep Grok').onClick()
  await new Promise(r => setImmediate(r))
  ok(/restore point/.test(H.notes.at(-1)?.meta), 'keep Grok toast: old Hermes copy is a restore point')

  // Mirror bot (real twin): empty, then with saved steps + charter.
  html = text(render(jsx(P.MirrorDialog, { open: true, onClose: () => {}, pair: { ...recipe, demo: false }, storage: ctx.storage })))
  ok(html.includes('Mirror bot') && html.includes('Pair folder') && html.includes('makes no network calls') && H.buttons.find(b => b.text === 'Connect')?.disabled === true && !H.buttons.some(b => b.text === 'Disconnect'), 'mirror dialog: folder first')
  const guide = { pair_folder: '/tmp/example-pair-folder', connected: true, charter: '# botport — Mirror bot charter\nKeep skills in step.', steps: [{ id: 'mirror_bot', what: 'Create a Mirror bot in Grok and paste the charter.' }, { id: 'share_folder', what: 'Share the pair folder with it.' }, { id: 'add_pair', what: 'Ask it to add the pair.' }, { id: 'test', what: 'Press Test on its routine, then sync.' }] }
  const store = { get: (k, f) => (k === 'mirror.acme-helper' ? guide : k === 'mirror.acme-helper.ticks' ? { mirror_bot: true } : f), set: () => {}, remove: () => {} }
  raw = render(jsx(P.MirrorDialog, { open: true, onClose: () => {}, pair: { ...acme, demo: false }, storage: store }))
  html = text(raw)
  ok(!H.buttons.some(b => b.text === 'Close'), 'mirror dialog: no second Close in the footer')
  ok(html.includes('Steps in Grok · 1/4 done') && html.includes('4. Press Test on its routine, then sync.') && html.includes('Charter: paste') && html.includes('Copy charter') && H.buttons.some(b => b.text === 'Disconnect'), 'mirror dialog: checklist + charter + copy + disconnect')
  H.restCalls = []
  H.restReply = async () => ({ pair_id: 'acme-helper', connected: false })
  await H.buttons.find(b => b.text === 'Disconnect').onClick()
  await H.buttons.find(b => b.text === 'Update').onClick()
  ok(H.restCalls[0]?.path === '/pairs/acme-helper/mirror' && H.restCalls[0]?.opts?.body?.off === true && H.restCalls[1]?.opts?.body?.pair_folder === '/tmp/example-pair-folder', 'mirror posts {off:true} and {pair_folder}')
  // Demo twin: no folder asked; "Show the steps" posts {} and the checklist renders.
  html = text(render(jsx(P.MirrorDialog, { open: true, onClose: () => {}, pair: acme, storage: ctx.storage })))
  ok(html.includes('a pretend Mirror bot is connected, no folder needed') && !html.includes('Pair folder') && H.buttons.some(b => b.text === 'Show the steps') && !H.buttons.some(b => b.text === 'Close'), 'demo mirror: no folder field')
  H.restCalls = []
  H.restReply = async () => ({ pair_id: 'acme-helper', connected: true, steps: guide.steps, charter: guide.charter })
  await H.buttons.find(b => b.text === 'Show the steps').onClick()
  await new Promise(r => setImmediate(r))
  ok(H.restCalls[0]?.path === '/pairs/acme-helper/mirror' && JSON.stringify(H.restCalls[0]?.opts?.body) === '{}' && /demo Mirror checklist ready/.test(H.notes.at(-1)?.message), 'demo mirror posts {} (no folder)')
  html = text(render(jsx(P.MirrorDialog, { open: true, onClose: () => {}, pair: acme, storage: store })))
  ok(html.includes('Steps in Grok · 1/4 done') && html.includes('Charter: paste') && !html.includes('Pair folder'), 'demo mirror: steps + charter')

  // Remove twin: dry-run list in a confirm dialog, then the real call.
  H.confirms = []
  html = text(render(jsx(P.RemoveTwinDialog, { open: true, onClose: () => {}, pair: recipe })))
  ok(html.includes('Remove the Recipe Planner') && html.includes('stays as a normal profile') && html.includes('Removed (3)') && html.includes('state:pairs/recipe-planner.key'), 'remove twin: dry-run list')
  ok(H.confirms[0]?.destructive === true && H.confirms[0]?.confirmLabel === 'Remove twin', 'remove twin: destructive confirm')
  H.restCalls = []
  H.restReply = async () => ({ pair_id: 'recipe-planner', dry_run: false, profile_kept: true, removed: [] })
  await H.confirms[0].onConfirm()
  ok(H.restCalls[0]?.path === '/pairs/recipe-planner/remove' && !H.restCalls[0]?.opts?.body?.dry_run && /profile/i.test(H.notes.at(-1)?.meta ?? ''), 'remove twin: posts and toasts profile kept')
  html = text(render(jsx(P.RecentList, { pair: acme })))
  ok(html.includes('Description updated') && html.includes('Grok'), 'recent activity')
}

rmSync(work, { recursive: true, force: true })
console.log(`desktop check: ${passed} assertions passed`)
