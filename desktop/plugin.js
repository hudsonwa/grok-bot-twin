// Grok Bot Twin: Hermes Desktop half.
// Plain ESM, no JSX. Imports only the Hermes plugin SDK and React.
// Talks to this plugin's own REST backend (dashboard/plugin_api.py) through ctx.rest.
// Every string that comes from Grok or botport is rendered as a React text node.

import {
  Badge,
  Button,
  Checkbox,
  cn,
  Codicon,
  ConfirmDialog,
  CopyButton,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DisclosureCaret,
  ErrorState,
  fmtDateTime,
  host,
  Input,
  PALETTE_AREA,
  queryClient,
  relativeTime,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  Skeleton,
  STATUSBAR_AREAS,
  StatusDot,
  Tip,
  useQuery,
  useQueryClient
} from '@hermes/plugin-sdk'
import { useEffect, useMemo, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'grok-bot-twin'
const ROUTE = '/twins'
const STATUS_KEY = [ID, 'status']
const ENABLE_CMD = 'hermes plugins enable grok-bot-twin'
const EVENT_TYPES = ['synced', 'secrets_withheld', 'conflict', 'changed']

// ctx.rest is captured at register time; components read it lazily.
let rest = null

// ── small element helper: el(type, props, ...children) ─────────────────────

function el(type, props, ...children) {
  const { key, ...rest_ } = props || {}
  const kids = children.filter(c => c !== undefined && c !== null && c !== false && c !== '')

  if (kids.length === 0) {
    return jsx(type, rest_, key)
  }

  if (kids.length === 1) {
    return jsx(type, { ...rest_, children: kids[0] }, key)
  }

  return jsxs(type, { ...rest_, children: kids }, key)
}

// ── untrusted text ──────────────────────────────────────────────────────────

// Control characters out, whitespace collapsed, capped. React escapes the rest.
function clean(value, max = 120) {
  const text = String(value ?? '')
    .replace(/[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2066-\u2069]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()

  return text.length > max ? `${text.slice(0, max - 1)}…` : text
}

// One line, ellipsised, full value on hover.
function Truncate({ text, max = 120, className }) {
  const value = clean(text, max)

  return el('span', { className: cn('min-w-0 truncate', className), title: value }, value)
}

const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`

function ago(iso) {
  const ms = Date.parse(iso ?? '')

  return Number.isFinite(ms) ? relativeTime(Math.min(ms, Date.now())) : null
}

function exact(iso) {
  const ms = Date.parse(iso ?? '')

  return Number.isFinite(ms) ? fmtDateTime.format(new Date(ms)) : ''
}

// ── REST ────────────────────────────────────────────────────────────────────

class TwinError extends Error {
  constructor(message, code) {
    super(message)
    this.code = code
  }
}

// Every backend call goes through here. A `{error}` body (HTTP 200) becomes a throw
// so callers have one failure path.
async function api(path, opts) {
  if (!rest) {
    throw new TwinError('The plugin is still starting.', 'not_ready')
  }

  let res

  try {
    res = await rest(path, opts)
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err)

    throw new TwinError(message || 'The twin service did not answer.', 'unreachable')
  }

  if (res && typeof res === 'object' && res.error) {
    const message = clean(res.error.message || res.error.code || 'Something went wrong', 300)

    throw new TwinError(res.error.code === 'locked' ? 'Busy, try again in a moment' : message, res.error.code)
  }

  return res ?? {}
}

const post = (path, body = {}) => api(path, { method: 'POST', body })
const pairPath = (pid, tail) => `/pairs/${encodeURIComponent(pid)}/${tail}`

function refreshAll() {
  void queryClient.invalidateQueries({ queryKey: [ID] })
}

// Read /status now (not from cache), so a sync's result toast is judged
// against the twins' real state and every card shows it at once.
async function freshStatus() {
  try {
    if (typeof queryClient.fetchQuery === 'function') {
      await queryClient.fetchQuery({ queryFn: () => api('/status'), queryKey: STATUS_KEY, staleTime: 0 })
    } else {
      await queryClient.invalidateQueries({ queryKey: STATUS_KEY })
    }
  } catch {
    // the toast falls back to the cached status
  }
}

// Sync now (palette, page header, card): post, read the fresh status, toast, refresh the rest.
async function syncAndToast(body, opts) {
  const res = await post('/sync', body)

  await freshStatus()
  toastSyncResult(res, opts)
  refreshAll()

  return res
}

function errorText(err) {
  return err instanceof Error ? err.message : String(err ?? 'Something went wrong')
}

// One busy flag per card/dialog: buttons disable while anything runs, the
// pressed one shows the spinner.
function useBusy() {
  const [busy, setBusy] = useState(null)

  async function run(name, fn, failTitle) {
    if (busy) {
      return undefined
    }

    setBusy(name)

    try {
      return await fn()
    } catch (err) {
      host.notifyError(err, failTitle)

      return undefined
    } finally {
      setBusy(null)
    }
  }

  return [busy, run]
}

// ── vocabulary ──────────────────────────────────────────────────────────────

const STATE = {
  in_step: { label: 'In step', tone: 'good', rank: 0 },
  changes_waiting: { label: 'Changes waiting', tone: 'warn', rank: 1 },
  grok_missing: { label: 'Grok bot missing', tone: 'muted', rank: 2 },
  needs_you: { label: 'Needs you', tone: 'warn', rank: 3 },
  error: { label: 'Error', tone: 'bad', rank: 4 }
}

const stateOf = s => STATE[s] ?? STATE.error

const COVERAGE_GROUPS = [
  {
    id: 'syncs',
    label: 'Syncs',
    tip: { g2h: 'Changes in Grok reach Hermes at the next check (every 60 s by default).', h2g: 'Carried to Grok at the next sync.' }
  },
  {
    id: 'syncs_via_mirror',
    label: 'Via Mirror routine',
    tip: {
      g2h: 'Arrives when your Mirror bot’s routine runs in Grok (or when you press its Test button).',
      h2g: 'Packed by Send to Grok. Your Mirror bot’s routine applies it in Grok.'
    }
  },
  {
    id: 'by_hand',
    label: 'By hand',
    tip: {
      g2h: 'Grok gives no way to move this. Recreate it in Hermes; it is on the Ready to switch checklist.',
      h2g: 'Grok gives no way to change this from outside. Set it up in Grok yourself.'
    }
  },
  {
    id: 'not_carried',
    label: 'Not carried',
    tip: { g2h: 'Not carried from Grok to Hermes.', h2g: 'Not carried from Hermes to Grok.' }
  }
]

// Older engine values map onto the v1 groups.
const COVERAGE_ALIAS = { on_export: 'syncs_via_mirror', 'n/a': 'not_carried' }
const coverageValue = v => COVERAGE_ALIAS[v] ?? v

const KIND_LABEL = {
  'persona.name': 'name',
  'persona.description': 'description',
  'persona.instructions': 'instructions',
  name: 'name',
  description: 'description',
  instructions: 'instructions',
  skill: 'skill',
  skills: 'skills',
  routine: 'routine',
  routines: 'routines',
  connector: 'connector',
  connectors: 'connectors',
  activity: 'activity digest'
}

function kindLabel(kindOrItem) {
  const raw = clean(kindOrItem, 60)

  return KIND_LABEL[raw] ?? KIND_LABEL[raw.split(':')[0]] ?? raw.replace(/[._]/g, ' ')
}

// "skill:greet-visitors" -> "greet-visitors (skill)"; "persona.description" -> "description".
function itemLabel(item) {
  const raw = clean(item, 120).split('@')[0].slice(0, 80)
  const at = raw.indexOf(':')

  return at > 0 ? `${raw.slice(at + 1)} (${kindLabel(raw.slice(0, at))})` : kindLabel(raw)
}

function joinWords(words) {
  if (words.length <= 1) {
    return words[0] ?? ''
  }

  return `${words.slice(0, -1).join(', ')} & ${words[words.length - 1]}`
}

const KIND_ORDER = ['name', 'description', 'instructions', 'skill', 'skills', 'routine', 'routines', 'connector', 'connectors', 'activity digest']
const kindRank = label => (KIND_ORDER.includes(label) ? KIND_ORDER.indexOf(label) : KIND_ORDER.length)

// [{kind,name,direction}] -> "name & description updated from Grok"
function describeApplied(applied) {
  const counts = new Map()

  for (const a of applied) {
    const label = kindLabel(a?.kind || a?.item || 'item')
    counts.set(label, (counts.get(label) ?? 0) + 1)
  }

  const words = [...counts]
    .sort(([a], [b]) => kindRank(a) - kindRank(b))
    .map(([label, n]) => (n > 1 ? `${n} ${label.endsWith('s') ? label : `${label}s`}` : label))
  const dirs = new Set(applied.map(a => a?.direction))
  const tail = dirs.size === 1 && dirs.has('g2h') ? 'updated from Grok' : dirs.size === 1 && dirs.has('h2g') ? 'sent to Grok' : 'synced'

  return `${joinWords(words)} ${tail}`
}

// ── status cache helpers (used outside React too) ───────────────────────────

function cachedPairs() {
  return queryClient.getQueryData(STATUS_KEY)?.pairs ?? []
}

function pairName(pairId, payload) {
  const fromPayload = payload?.name ?? payload?.grok?.name
  const fromCache = cachedPairs().find(p => p?.pair_id === pairId)?.grok?.name

  return clean(fromPayload || fromCache || pairId || 'Your twin', 60)
}

// ── toasts (shared by events, polling and button results) ───────────────────
// Ids are per pair + kind, so an event toast and a button-result toast for the
// same thing replace each other instead of stacking.

function toastSynced(pairId, applied, payload) {
  // Events may carry only `kinds` (+ a direction flag) instead of the full list.
  if ((!Array.isArray(applied) || applied.length === 0) && Array.isArray(payload?.kinds) && payload.kinds.length) {
    const direction = payload.direction ?? (/from Hermes/.test(String(payload.message ?? '')) ? 'h2g' : 'g2h')
    applied = payload.kinds.slice(0, 20).map(kind => ({ direction, kind }))
  }

  if (!Array.isArray(applied) || applied.length === 0) {
    return
  }

  host.notify({
    id: `${ID}:synced:${pairId}`,
    kind: 'success',
    message: `${pairName(pairId, payload)}: ${describeApplied(applied)}`
  })
}

// Pair ids that just got a "this needs you" toast (secret held back, conflict),
// so a Sync now right after does not answer it with "Already in step".
const attentionToasts = new Map() // pairId -> ms

function noteAttention(pairId) {
  attentionToasts.set(pairId, Date.now())
}

const recentAttention = (pairId, windowMs = 10_000) => Date.now() - (attentionToasts.get(pairId) ?? 0) < windowMs

function toastSecrets(pairId, withheld, count, payload) {
  const list = Array.isArray(withheld) ? withheld : []
  const n = Number(count) || list.length || 1
  const name = pairName(pairId, payload)
  const kinds = [...new Set(list.map(w => kindLabel(typeof w === 'string' ? w : w?.kind || w?.item || 'description')))]
  const where = kinds.length ? joinWords(kinds) : 'description'
  const toGrok = (payload?.direction ?? list[0]?.direction) === 'h2g'
  const target = toGrok ? 'Grok' : 'Hermes'

  noteAttention(pairId)
  // The whole sentence is in the visible message; meta is the always-shown
  // second line (detail would sit behind a collapsed "Details").
  host.notify({
    id: `${ID}:secrets:${pairId}`,
    kind: 'warning',
    message: `${secretsHeldText(n)} — a check, not a guarantee.`,
    meta: `${name}'s ${where} ${n === 1 && kinds.length <= 1 ? 'was' : 'were'} not copied to ${target}.`
  })
}

const secretsHeldText = n => (n === 1 ? '1 suspected secret held back' : `${n} suspected secrets held back`)

// The "needs you" conflict toast stays up (warnings do not time out), so it is
// tracked by its stable id per pair and taken down once the conflict is gone.
const conflictToasts = new Map() // pairId -> { n: count shown, at: ms }
const conflictToastId = pairId => `${ID}:conflict:${pairId}`

// The SDK has no dismiss door: a toast with the same id replaces it, and a
// tiny duration takes that replacement down at once.
function dismissToast(id, message) {
  host.notify({ durationMs: 1, id, kind: 'success', message })
}

function settleConflictToast(pairId) {
  if (conflictToasts.delete(pairId)) {
    dismissToast(conflictToastId(pairId), `${pairName(pairId)}: nothing left to pick`)
  }
}

// Called with every fresh status: a twin with no conflicts left (resolved, or
// the twin/demo removed) loses its toast; a changed count updates it in place.
// A status that may have been read before the toast went up (`fetchedAt`
// within a few seconds of it) is not trusted to take it down.
function reconcileConflictToasts(pairs, fetchedAt = Date.now()) {
  if (!Array.isArray(pairs)) {
    return
  }

  for (const [pid, shown] of [...conflictToasts]) {
    if (!(fetchedAt - shown.at > 3_000)) {
      continue
    }

    const p = pairs.find(x => x?.pair_id === pid)
    const n = Number(p?.conflicts) || 0

    if (n === 0) {
      settleConflictToast(pid)
    } else if (n !== shown.n) {
      toastConflict(pid, n, p, { quiet: true })
    }
  }
}

function toastConflict(pairId, count, payload, { quiet = false } = {}) {
  const n = Number(count) || 1

  if (!quiet) {
    noteAttention(pairId)
  }

  conflictToasts.set(pairId, { at: Date.now(), n })
  host.notify({
    id: conflictToastId(pairId),
    kind: 'warning',
    message: `${pairName(pairId, payload)} needs you: ${plural(n, 'change')} edited on both sides`,
    action: { label: 'Pick one', onClick: () => host.navigate(ROUTE) }
  })
}

const ATTENTION_STATES = ['needs_you', 'changes_waiting']

// Why a pair that synced nothing is still not in step, in plain words.
function attentionReason(entry, cached) {
  const state = entry?.state && entry.state !== 'in_step' ? entry.state : cached?.state
  const fromCache = cached?.state === state ? clean(cached?.state_reason, 120) : ''

  if (fromCache) {
    return fromCache
  }

  if (state === 'needs_you') {
    const n = Number(cached?.conflicts) || (Array.isArray(entry?.conflicts) ? entry.conflicts.length : 0) || 1

    return `${plural(n, 'change')} edited on both sides`
  }

  const toG = Number(entry?.to_grok) || Number(cached?.pending?.to_grok) || 0

  return toG ? `${plural(toG, 'change')} for Grok` : stateOf(state).label
}

// A /sync or /send-to-grok response -> the same toasts the events would raise.
// "Already in step" only when every twin really is: a twin that synced nothing
// but still needs you gets its reason instead (or nothing, when a secret or
// conflict toast for it went up in the last ~10 s).
function toastSyncResult(res, { quietWhenNothing = false } = {}) {
  const pairs = Array.isArray(res?.pairs) ? res.pairs : []
  let applied = 0
  let failed = 0
  const attention = []

  for (const p of pairs) {
    const pid = p?.pair_id
    applied += Array.isArray(p?.applied) ? p.applied.length : 0

    // One twin that failed is reported in its place; the others still synced.
    if (p?.state === 'error') {
      failed += 1
      host.notify({
        id: `${ID}:sync-error:${pid}`,
        kind: 'error',
        message: `${pairName(pid)}: could not sync`,
        detail: clean(p?.error?.message, 300) || 'botport reported an error'
      })
      continue
    }

    toastSynced(pid, p?.applied)

    if (Array.isArray(p?.secrets_withheld) && p.secrets_withheld.length) {
      toastSecrets(pid, p.secrets_withheld, p.secrets_withheld.length)
    }

    if (Array.isArray(p?.conflicts) && p.conflicts.length) {
      toastConflict(pid, p.conflicts.length)
    }

    const cached = cachedPairs().find(c => c?.pair_id === pid)

    // Not "in step" while the twin's (fresh) status says anything else.
    const cachedOff = Boolean(cached?.state) && cached.state !== 'in_step'

    if (ATTENTION_STATES.includes(p?.state) || cachedOff || recentAttention(pid)) {
      attention.push({ cached, entry: p, pid })
    }
  }

  if (applied > 0 || failed > 0 || quietWhenNothing) {
    return
  }

  if (attention.length === 0) {
    host.notify({
      id: `${ID}:sync-result`,
      kind: 'info',
      message: pairs.length === 0 ? 'No twins to sync yet' : pairs.length === 1 ? 'Already in step' : `All ${pairs.length} twins already in step`
    })

    return
  }

  for (const { cached, entry, pid } of attention) {
    if (!recentAttention(pid)) {
      host.notify({ id: `${ID}:sync-result:${pid}`, kind: 'info', message: `${pairName(pid)}: ${attentionReason(entry, cached)}` })
    }
  }
}

// ── events: gateway stream + /events polling, deduped ───────────────────────

const seenKeys = new Map() // key -> expiry (ms)

// keys: [[key, ttlMs]]. Exact keys (event id / time) last 10 min; content keys
// only 30 s, so a genuinely repeated change later still toasts.
function rememberEvent(keys) {
  const now = Date.now()

  for (const [k, until] of seenKeys) {
    if (until < now) {
      seenKeys.delete(k)
    }
  }

  if (keys.some(([k]) => seenKeys.has(k))) {
    return false
  }

  for (const [k, ttl] of keys) {
    seenKeys.set(k, now + ttl)
  }

  return true
}

function stableJson(value) {
  if (Array.isArray(value)) {
    return `[${value.map(stableJson).join(',')}]`
  }

  if (value && typeof value === 'object') {
    return `{${Object.keys(value)
      .filter(k => k !== 'at' && k !== 'pair_id')
      .sort()
      .map(k => `${JSON.stringify(k)}:${stableJson(value[k])}`)
      .join(',')}}`
  }

  return JSON.stringify(value ?? null)
}

let refreshTimer = null

function handleEvent(raw, ctx) {
  const type = String(raw?.type ?? '')

  if (!EVENT_TYPES.includes(type)) {
    return
  }

  const payload = raw?.payload && typeof raw.payload === 'object' ? raw.payload : {}
  const pairId = clean(raw?.pair_id ?? payload.pair_id ?? '', 80)
  const at = raw?.at ?? payload.at ?? ''
  // Two keys: exact (id/at) and content. The socket and the poll deliver the
  // same event; either key already seen means it was handled.
  const keys = [[`c|${type}|${pairId}|${stableJson(payload)}`, 30_000]]

  if (payload.event_id || raw?.id || at) {
    keys.push([`x|${type}|${pairId}|${payload.event_id ?? raw?.id ?? at}`, 10 * 60_000])
  }

  if (!rememberEvent(keys)) {
    return
  }

  // Coalesce refreshes when several events land together.
  if (!refreshTimer) {
    refreshTimer = ctx.setTimeout(() => {
      refreshTimer = null
      refreshAll()
    }, 250)
  }

  if (type === 'synced') {
    toastSynced(pairId, payload.applied, payload)
  } else if (type === 'secrets_withheld') {
    toastSecrets(pairId, payload.secrets_withheld ?? payload.items, payload.count, payload)
  } else if (type === 'conflict') {
    toastConflict(pairId, payload.count ?? (Array.isArray(payload.conflicts) ? payload.conflicts.length : 1), payload)
  }
}

function startEventPolling(ctx) {
  let since = new Date().toISOString()
  let inflight = false
  let failures = 0
  let resumeAt = 0

  ctx.setInterval(() => {
    if (inflight || Date.now() < resumeAt) {
      return
    }

    inflight = true

    api(`/events?since=${encodeURIComponent(since)}`)
      .then(res => {
        failures = 0

        for (const ev of Array.isArray(res?.events) ? res.events : []) {
          if (typeof ev?.at === 'string' && ev.at > since) {
            since = ev.at
          }

          handleEvent(ev, ctx)
        }
      })
      .catch(() => {
        // Backend off or restarting: back off quietly (5 s doubling, max 60 s).
        failures += 1
        resumeAt = Date.now() + Math.min(60_000, 5_000 * 2 ** Math.min(failures, 4))
      })
      .finally(() => {
        inflight = false
      })
  }, 5_000)
}

// ── shared bits of UI ───────────────────────────────────────────────────────

const SECTION_LABEL = 'text-[0.625rem] font-medium uppercase tracking-wider text-(--ui-text-quaternary)'
const CARD = 'rounded-lg border border-(--ui-stroke-tertiary) px-4 py-3'
const NOTE = 'rounded-lg bg-(--ui-bg-quinary) px-3 py-2.5 text-[0.75rem] leading-relaxed text-(--ui-text-secondary)'

function Icon({ name, className, size = '0.8rem' }) {
  return jsx(Codicon, { className, name, size })
}

function Avatar({ name }) {
  const initial = Array.from(clean(name, 60) || '?')[0].toUpperCase()

  return el(
    'span',
    {
      'aria-hidden': true,
      className:
        'grid size-8 shrink-0 place-items-center rounded-md bg-(--ui-bg-quaternary) text-[0.8125rem] font-semibold text-(--ui-text-secondary)'
    },
    initial
  )
}

function StateLabel({ state, reason }) {
  const s = stateOf(state)

  return el(
    Tip,
    { label: reason && norm(reason) !== norm(s.label) ? `${s.label}: ${clean(reason, 120)}` : s.label },
    el(
      'span',
      { className: 'inline-flex shrink-0 items-center gap-1.5 text-[0.75rem] font-medium text-(--ui-text-secondary)', role: 'status' },
      jsx(StatusDot, { tone: s.tone }),
      s.label
    )
  )
}

function InlineError({ error, onRetry }) {
  return el(
    'div',
    { className: cn(NOTE, 'flex items-center gap-2'), role: 'alert' },
    jsx(Icon, { className: 'text-(--ui-text-tertiary)', name: 'warning' }),
    el('span', { className: 'min-w-0 flex-1' }, clean(errorText(error), 300)),
    onRetry && el(Button, { onClick: onRetry, size: 'xs', variant: 'ghost' }, 'Retry')
  )
}

function LinesSkeleton({ rows = 3 }) {
  return el(
    'div',
    { className: 'flex flex-col gap-2 py-1' },
    Array.from({ length: rows }, (_, i) => jsx(Skeleton, { className: 'h-3.5 w-full max-w-md' }, i))
  )
}

function CommandLine({ command, label }) {
  return el(
    'div',
    { className: 'flex min-w-0 items-center gap-1 rounded-md bg-(--ui-bg-quaternary) py-0.5 pl-2.5 pr-0.5' },
    el('code', { className: 'min-w-0 flex-1 truncate font-mono text-[0.7rem] text-(--ui-text-primary)', title: command }, command),
    jsx(CopyButton, { appearance: 'icon', buttonSize: 'icon-xs', label: label ?? 'Copy command', text: command })
  )
}

// ── coverage chips ──────────────────────────────────────────────────────────

const COVERAGE_DIRS = [
  { id: 'g2h', label: 'Grok → Hermes', aria: 'From Grok to Hermes' },
  { id: 'h2g', label: 'Hermes → Grok', aria: 'From Hermes to Grok' }
]

// [{item,g2h,h2g}] -> one row per direction, one chip per item with its value.
function coverageRows(coverage) {
  const groups = new Map(COVERAGE_GROUPS.map(g => [g.id, g]))
  const rows = COVERAGE_DIRS.map(d => ({ ...d, chips: [] }))

  for (const row of Array.isArray(coverage) ? coverage.slice(0, 20) : []) {
    const item = kindLabel(row?.item)

    for (const r of rows) {
      const group = groups.get(coverageValue(row?.[r.id]))

      if (item && group) {
        r.chips.push({ group, item })
      }
    }
  }

  return rows.filter(r => r.chips.length > 0)
}

function CoverageChip({ chip, dir, mirrorConnected }) {
  // "Via Mirror routine" next to "Mirror bot not set up" would promise too much: say it is not set up.
  const unset = chip.group.id === 'syncs_via_mirror' && !mirrorConnected
  const strong = chip.group.id === 'syncs' || (chip.group.id === 'syncs_via_mirror' && !unset)
  const label = unset ? `${chip.group.label} (not set up)` : chip.group.label

  return el(
    Tip,
    { label: `${chip.item[0].toUpperCase()}${chip.item.slice(1)}, ${dir.label}: ${label}. ${chip.group.tip[dir.id]}` },
    el(
      'span',
      {
        className:
          'inline-flex cursor-default items-center gap-1 rounded-[3px] bg-(--ui-bg-quaternary) px-1.5 py-0.5 text-[0.6875rem] text-(--ui-text-secondary)',
        tabIndex: 0
      },
      chip.item,
      el('span', { className: strong ? 'text-(--ui-text-tertiary)' : 'text-(--ui-text-quaternary)' }, label)
    )
  )
}

function Coverage({ coverage, mirrorConnected }) {
  const rows = coverageRows(coverage)

  if (rows.length === 0) {
    return null
  }

  return el(
    'div',
    { 'aria-label': 'What carries over', className: 'grid grid-cols-[auto_minmax(0,1fr)] items-baseline gap-x-3 gap-y-1.5', role: 'group' },
    rows.flatMap(row => [
      el('span', { 'aria-label': row.aria, className: cn(SECTION_LABEL, 'whitespace-nowrap normal-case tracking-normal text-[0.6875rem]'), key: `${row.id}-l` }, row.label),
      el(
        'div',
        { className: 'flex flex-wrap gap-1', key: `${row.id}-c` },
        row.chips.map(chip => jsx(CoverageChip, { chip, dir: row, mirrorConnected }, `${chip.item}-${row.id}`))
      )
    ])
  )
}

// ── conflicts ───────────────────────────────────────────────────────────────

// Unified-diff file/hunk header lines are noise for a person: dropped.
const DIFF_HEADER = /^(---|\+\+\+|@@|diff |index )/

function diffBodyLines(diff) {
  return String(diff ?? '')
    .split('\n')
    .slice(0, 400)
    .filter(line => !DIFF_HEADER.test(line))
}

function DiffView({ diff }) {
  const lines = diffBodyLines(diff)

  if (lines.length === 0) {
    return null
  }

  return el(
    'pre',
    {
      'aria-label': 'Differences',
      className:
        'max-h-56 overflow-auto rounded-md border border-(--ui-stroke-tertiary) py-1.5 font-mono text-[0.6875rem] leading-[1.45] text-(--ui-text-secondary)',
      tabIndex: 0
    },
    lines.map((line, i) => {
      const add = line.startsWith('+')
      const del = line.startsWith('-')

      return el(
        'div',
        {
          className: cn(
            'min-w-max whitespace-pre px-2.5',
            add && 'bg-(--ui-diff-add-background) text-(--ui-diff-add-foreground)',
            del && 'bg-(--ui-diff-remove-background) text-(--ui-diff-remove-foreground) line-through decoration-current/40'
          ),
          key: i
        },
        line || ' '
      )
    })
  )
}

// Longest common subsequence over two short token lists -> keep flags per side.
function lcsKeep(a, b) {
  const n = a.length
  const m = b.length
  const dp = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1))

  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }

  const keepA = new Array(n).fill(false)
  const keepB = new Array(m).fill(false)
  let i = 0
  let j = 0

  while (i < n && j < m) {
    if (a[i] === b[j]) {
      keepA[i] = true
      keepB[j] = true
      i += 1
      j += 1
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      i += 1
    } else {
      j += 1
    }
  }

  return [keepA, keepB]
}

const MAX_MARK_LINES = 200
const MAX_MARK_WORDS = 300

// Two texts -> per side, lines of [{text, changed}] segments: whole lines that
// exist on one side only are marked, and within lines that were edited only
// the changed words are.
function markDifferences(left, right) {
  const la = String(left).split('\n')
  const lb = String(right).split('\n')

  if (la.length > MAX_MARK_LINES || lb.length > MAX_MARK_LINES) {
    return [la.map(t => [{ changed: false, text: t }]), lb.map(t => [{ changed: false, text: t }])]
  }

  const [keepA, keepB] = lcsKeep(la, lb)
  const outA = la.map(t => [{ changed: false, text: t }])
  const outB = lb.map(t => [{ changed: false, text: t }])
  let i = 0
  let j = 0

  // Walk both sides; a run of unkept lines on each side is paired up line by line.
  while (i < la.length || j < lb.length) {
    const runA = []
    const runB = []

    while (i < la.length && !keepA[i]) runA.push(i++)
    while (j < lb.length && !keepB[j]) runB.push(j++)

    for (let k = 0; k < Math.max(runA.length, runB.length); k++) {
      const ia = runA[k]
      const ib = runB[k]

      if (ia !== undefined && ib !== undefined) {
        const wa = la[ia].split(/(\s+)/)
        const wb = lb[ib].split(/(\s+)/)

        if (wa.length > MAX_MARK_WORDS || wb.length > MAX_MARK_WORDS) {
          outA[ia] = [{ changed: true, text: la[ia] }]
          outB[ib] = [{ changed: true, text: lb[ib] }]
          continue
        }

        const [ka, kb] = lcsKeep(wa, wb)
        outA[ia] = wa.map((w, x) => ({ changed: !ka[x] && /\S/.test(w), text: w }))
        outB[ib] = wb.map((w, x) => ({ changed: !kb[x] && /\S/.test(w), text: w }))
      } else if (ia !== undefined) {
        outA[ia] = [{ changed: true, text: la[ia] }]
      } else if (ib !== undefined) {
        outB[ib] = [{ changed: true, text: lb[ib] }]
      }
    }

    if (i < la.length && j < lb.length) {
      i += 1
      j += 1
    } else if (runA.length === 0 && runB.length === 0) {
      break
    }
  }

  return [outA, outB]
}

const MARK = 'rounded-[2px] bg-(--ui-bg-tertiary) font-semibold text-(--ui-text-primary)'

function ConflictSide({ label, side, marked }) {
  const text = side?.text

  return el(
    'div',
    { className: 'flex min-w-0 flex-col gap-1' },
    el('span', { className: SECTION_LABEL }, label),
    text === null || text === undefined
      ? el('div', { className: cn(NOTE, 'italic') }, 'Binary file, no preview')
      : el(
          'pre',
          {
            className:
              'max-h-36 overflow-auto whitespace-pre-wrap break-words rounded-md bg-(--ui-bg-quinary) px-2.5 py-1.5 font-mono text-[0.6875rem] leading-[1.45] text-(--ui-text-secondary)',
            tabIndex: 0
          },
          marked
            ? marked.map((segments, i) =>
                el(
                  'div',
                  { key: i },
                  segments.length === 1 && !segments[0].text ? ' ' : segments.map((seg, k) => (seg.changed ? el('mark', { className: MARK, key: k }, seg.text) : seg.text))
                )
              )
            : String(text).slice(0, 20_000)
        )
  )
}

const RESOLVE_NOTE =
  'Keep Hermes: the Hermes copy stays and Grok gets it on the by-hand list. Keep Grok: Grok’s copy replaces Hermes (the old Hermes copy becomes a restore point).'

function ConflictItem({ pairId, conflict, initialShowDiff = false }) {
  const [busy, run] = useBusy()
  const [showDiff, setShowDiff] = useState(initialShowDiff)
  const since = ago(conflict?.since ?? conflict?.hermes?.changed_at)
  const hermesText = conflict?.hermes?.text
  const grokText = conflict?.grok?.text
  const bothText = typeof hermesText === 'string' && typeof grokText === 'string'
  const marked = useMemo(
    () => (bothText ? markDifferences(hermesText.slice(0, 20_000), grokText.slice(0, 20_000)) : [null, null]),
    [bothText, hermesText, grokText]
  )
  const hasDiff = diffBodyLines(conflict?.diff).length > 0

  async function keep(side) {
    const res = await run(side, () => post(pairPath(pairId, 'resolve'), { conflict_id: conflict.conflict_id, keep: side }), 'Could not keep that version')

    if (res) {
      host.notify({
        id: `${ID}:resolved:${pairId}`,
        kind: 'success',
        message: `Kept the ${side === 'hermes' ? 'Hermes' : 'Grok'} copy of ${clean(conflict?.name, 60) || kindLabel(conflict?.kind)}`,
        meta:
          side === 'hermes'
            ? 'Grok gets it on the by-hand list.'
            : 'The old Hermes copy is a restore point now.'
      })
      refreshAll()
    }
  }

  return el(
    'div',
    { className: 'flex flex-col gap-2.5' },
    el(
      'div',
      { className: 'flex min-w-0 items-center gap-2' },
      jsx(Badge, { variant: 'outline', children: kindLabel(conflict?.kind) }),
      jsx(Truncate, { className: 'text-[0.8125rem] font-medium text-(--ui-text-primary)', max: 80, text: conflict?.name }),
      since && el('span', { className: 'ml-auto shrink-0 text-[0.6875rem] text-(--ui-text-tertiary)', title: exact(conflict?.since) }, `Edited on both sides since ${since}`)
    ),
    el(
      'div',
      { className: 'grid gap-3 sm:grid-cols-2' },
      jsx(ConflictSide, { label: 'Hermes', marked: marked[0], side: conflict?.hermes }),
      jsx(ConflictSide, { label: 'Grok', marked: marked[1], side: conflict?.grok })
    ),
    conflict?.text_truncated && el('p', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)' }, 'Long file: the preview shows the first 64 KB of each side.'),
    hasDiff &&
      el(
        'div',
        { className: 'flex flex-col gap-1.5' },
        el(
          Button,
          { 'aria-expanded': showDiff, className: 'self-start', onClick: () => setShowDiff(!showDiff), size: 'xs', variant: 'ghost' },
          jsx(DisclosureCaret, { open: showDiff }),
          showDiff ? 'Hide diff' : 'Show diff'
        ),
        showDiff && jsx(DiffView, { diff: conflict?.diff })
      ),
    el(
      'div',
      { className: 'flex flex-wrap items-center gap-2' },
      // Equal weight: neither choice is pre-selected.
      el(Button, { disabled: Boolean(busy), loading: busy === 'hermes', onClick: () => void keep('hermes'), size: 'sm', variant: 'outline' }, 'Keep Hermes'),
      el(Button, { disabled: Boolean(busy), loading: busy === 'grok', onClick: () => void keep('grok'), size: 'sm', variant: 'outline' }, 'Keep Grok')
    ),
    el('p', { className: 'text-[0.6875rem] leading-snug text-(--ui-text-tertiary)' }, RESOLVE_NOTE)
  )
}

function ConflictCard({ pair }) {
  const [mountedAt] = useState(() => Date.now())
  const q = useQuery({
    queryFn: () => api(pairPath(pair.pair_id, 'conflicts')),
    queryKey: [ID, 'conflicts', pair.pair_id, pair.conflicts],
    // an earlier answer for the same key may be "none": always ask again when the card appears, and keep asking
    // while the answer is empty (the conflict may be a moment ahead of this route; the status refresh removes the
    // card when the conflict really is gone)
    refetchInterval: query => (Array.isArray(query?.state?.data?.conflicts) && query.state.data.conflicts.length === 0 ? 1_500 : false),
    refetchOnMount: 'always'
  })

  const conflicts = Array.isArray(q.data?.conflicts) ? q.data.conflicts : []
  // "Already settled" only from an answer fetched after this card appeared;
  // while that fetch runs (or an older, empty answer is all there is) the
  // line stays neutral.
  const settled = conflicts.length === 0 && !q.isFetching && !(Number(q.dataUpdatedAt) < mountedAt)

  useEffect(() => {
    if (settled) {
      void queryClient.invalidateQueries({ queryKey: STATUS_KEY })
    }
  }, [settled])

  return el(
    'section',
    { 'aria-label': 'Pick one', className: 'flex flex-col gap-3 rounded-md border border-(--ui-stroke-secondary) p-3' },
    el(
      'div',
      { className: 'flex items-center gap-2' },
      jsx(Icon, { className: 'text-(--ui-text-secondary)', name: 'git-compare' }),
      el('h3', { className: 'text-[0.8125rem] font-semibold text-(--ui-text-primary)' }, 'Pick one'),
      el('span', { className: 'text-[0.75rem] text-(--ui-text-tertiary)' }, `${plural(pair.conflicts, 'change')} edited in both Grok and Hermes`)
    ),
    q.isPending
      ? jsx(LinesSkeleton, { rows: 4 })
      : q.isError
        ? jsx(InlineError, { error: q.error, onRetry: () => void q.refetch() })
        : conflicts.length === 0
          ? el('p', { className: 'text-[0.75rem] text-(--ui-text-tertiary)', role: 'status' }, 'Loading both copies…')
          : conflicts.map((c, i) =>
              el(
                'div',
                { className: cn(i > 0 && 'border-t border-(--ui-stroke-tertiary) pt-3'), key: c?.conflict_id ?? i },
                jsx(ConflictItem, { conflict: c, pairId: pair.pair_id })
              )
            )
  )
}

// ── parity ──────────────────────────────────────────────────────────────────

function ParityPanel({ pair }) {
  const q = useQuery({ queryFn: () => api(pairPath(pair.pair_id, 'parity')), queryKey: [ID, 'parity', pair.pair_id] })
  const checks = Array.isArray(q.data?.checks) ? [...q.data.checks].slice(0, 100).sort((a, b) => Number(Boolean(a?.pass)) - Number(Boolean(b?.pass))) : []
  const total = Number(q.data?.total) || checks.length
  const passed = Number.isFinite(Number(q.data?.passed)) ? Number(q.data.passed) : checks.filter(c => c?.pass).length

  return el(
    'section',
    { 'aria-label': 'Setup parity', className: 'flex flex-col gap-2 rounded-md bg-(--ui-bg-quinary) p-3' },
    el(
      'div',
      { className: 'flex flex-wrap items-baseline gap-x-2 gap-y-0.5' },
      el('h3', { className: 'text-[0.8125rem] font-semibold text-(--ui-text-primary)' }, 'Setup parity'),
      q.data && total > 0 && el('span', { className: 'text-[0.75rem] font-medium tabular-nums text-(--ui-text-secondary)' }, `${passed}/${total} checks pass`),
      el('span', { className: 'text-[0.75rem] text-(--ui-text-tertiary)' }, 'Is each item the same on both sides? Read from files; no model is called.')
    ),
    q.isPending
      ? jsx(LinesSkeleton, { rows: 4 })
      : q.isError
        ? jsx(InlineError, { error: q.error, onRetry: () => void q.refetch() })
        : el(
            'ul',
            { className: 'flex flex-col gap-1.5' },
            checks.map((c, i) =>
              el(
                'li',
                { className: 'flex flex-col gap-1', key: c?.id ?? i },
                el(
                  'div',
                  { className: 'flex min-w-0 items-center gap-2 text-[0.75rem]' },
                  jsx(Icon, { className: c?.pass ? 'text-(--ui-text-tertiary)' : 'text-(--ui-text-primary)', name: c?.pass ? 'pass' : 'circle-slash' }),
                  el('span', { className: 'sr-only' }, c?.pass ? 'Passed:' : 'Failed:'),
                  jsx(Truncate, { className: c?.pass ? 'text-(--ui-text-secondary)' : 'font-medium text-(--ui-text-primary)', max: 100, text: c?.name })
                ),
                !c?.pass && c?.detail && el('p', { className: 'ml-5 text-[0.6875rem] leading-snug text-(--ui-text-tertiary)' }, clean(c.detail, 200)),
                !c?.pass &&
                  (c?.grok !== undefined || c?.hermes !== undefined) &&
                  el(
                    'div',
                    { className: 'ml-5 grid gap-2 sm:grid-cols-2' },
                    jsx(ConflictSide, { label: 'Grok', side: { text: clean(c.grok, 600) } }),
                    jsx(ConflictSide, { label: 'Hermes', side: { text: clean(c.hermes, 600) } })
                  )
              )
            )
          )
  )
}

// ── recent activity ─────────────────────────────────────────────────────────

function RecentList({ pair }) {
  const q = useQuery({ queryFn: () => api(pairPath(pair.pair_id, 'recent')), queryKey: [ID, 'recent', pair.pair_id], refetchInterval: 60_000 })
  const entries = Array.isArray(q.data?.entries) ? q.data.entries.slice(0, 20) : []

  if (q.isPending) {
    return jsx(LinesSkeleton, { rows: 3 })
  }

  if (q.isError) {
    return jsx(InlineError, { error: q.error, onRetry: () => void q.refetch() })
  }

  if (entries.length === 0) {
    return el('p', { className: 'text-[0.75rem] text-(--ui-text-tertiary)' }, 'Nothing yet. Changes on either side show up here.')
  }

  return el(
    'ul',
    { className: 'flex flex-col' },
    entries.map((e, i) =>
      el(
        'li',
        { className: 'flex min-w-0 items-center gap-2 py-1 text-[0.75rem]', key: `${e?.at}-${i}` },
        jsx(Badge, { className: 'w-14 justify-center', variant: 'muted', children: e?.side === 'grok' ? 'Grok' : 'Hermes' }),
        jsx(Truncate, { className: 'flex-1 text-(--ui-text-secondary)', text: e?.summary }),
        el('span', { className: 'shrink-0 text-(--ui-text-quaternary)', title: exact(e?.at) }, ago(e?.at) ?? '')
      )
    )
  )
}

// ── restore points dialog ───────────────────────────────────────────────────

// The item(s) a restore point brings back, in words a person recognises.
// Item ids look like "skill:greet-visitors" and point ids like ITEM@DIGEST12;
// the digest is never shown.
function restoreItems(p) {
  if (Array.isArray(p?.items)) {
    return p.items.slice(0, 50).map(i => itemLabel(typeof i === 'string' ? i : i?.item)).filter(Boolean)
  }

  const item = p?.item || (String(p?.id ?? '').includes('@') ? String(p.id).split('@')[0] : '')

  return item ? [itemLabel(item)] : []
}

function listWords(words, max = 3) {
  return words.length > max ? `${words.slice(0, max).join(', ')} and ${words.length - max} more` : joinWords(words)
}

function restoreLabel(p) {
  const items = restoreItems(p)

  if (items.length) {
    return listWords(items)
  }

  const n = Number(p?.items) || 0

  return n ? `Whole twin (${plural(n, 'item')})` : 'Whole twin'
}

function restoreWhen(p) {
  const reason = clean(p?.reason || '', 60).replace(/^kept copy$/i, '')
  const when = ago(p?.at)
  const head = reason ? `${reason[0].toUpperCase()}${reason.slice(1)}` : 'Kept'

  return when ? `${head} · ${when}` : head
}

function toastRestored(res) {
  const restored = Array.isArray(res?.restored) ? res.restored : []

  if (restored.length === 0) {
    host.notify({ kind: 'info', message: 'Nothing to restore', meta: 'The twin already matches that copy.' })

    return
  }

  const names = listWords(restored.map(r => itemLabel(typeof r === 'string' ? r : r?.item)))
  const waiting = Array.isArray(res?.waiting_for_ok) ? res.waiting_for_ok.length : 0

  // The restored copy now differs from Grok's: say why the card turns to "Waiting for your OK".
  host.notify(
    waiting > 0
      ? { kind: 'success', message: 'Restored the Hermes copy; it now waits for your OK before going to Grok', meta: `Restored ${names}.` }
      : { kind: 'success', message: `Restored ${names}` }
  )
}

function RestoreRow({ point, first, confirming, busy, onAsk, onCancel, onRestore }) {
  const label = restoreLabel(point)

  return el(
    'li',
    { className: cn('flex min-w-0 flex-col gap-1.5 py-2', !first && 'border-t border-(--ui-stroke-tertiary)') },
    el(
      'div',
      { className: 'flex min-w-0 items-center gap-3' },
      jsx(Icon, { className: 'text-(--ui-text-tertiary)', name: point?.scope === 'profile' ? 'history' : 'file' }),
      el(
        'div',
        { className: 'flex min-w-0 flex-1 flex-col' },
        el('span', { className: 'break-words text-[0.8125rem] text-(--ui-text-primary)' }, label),
        el('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', title: exact(point?.at) }, restoreWhen(point))
      ),
      !confirming && el(Button, { 'aria-label': `Restore ${label}`, disabled: Boolean(busy), onClick: onAsk, size: 'xs', variant: 'outline' }, 'Restore')
    ),
    confirming &&
      el(
        'div',
        { className: 'ml-7 flex flex-wrap items-center gap-2 rounded-md bg-(--ui-bg-quinary) px-2.5 py-1.5', role: 'group' },
        el('span', { className: 'flex-1 text-[0.75rem] text-(--ui-text-secondary)' }, 'Restore this copy?'),
        el(Button, { disabled: Boolean(busy), loading: busy === 'restore', onClick: onRestore, size: 'xs' }, 'Restore'),
        el(Button, { disabled: Boolean(busy), onClick: onCancel, size: 'xs', variant: 'ghost' }, 'Cancel')
      )
  )
}

// `initialConfirm` lets the smoke test render the inline confirm.
function RestoreDialog({ pair, open, onClose, initialConfirm = null }) {
  const q = useQuery({ enabled: open, queryFn: () => api(pairPath(pair.pair_id, 'restore-points')), queryKey: [ID, 'restore-points', pair.pair_id] })
  const [confirm, setConfirm] = useState(initialConfirm)
  const [busy, run] = useBusy()
  const points = Array.isArray(q.data?.points) ? q.data.points : []

  async function restore(point) {
    const res = await run('restore', () => post(pairPath(pair.pair_id, 'restore'), { point_id: point.id }), 'Could not restore')

    if (res) {
      setConfirm(null)
      toastRestored(res)
      refreshAll()
    }
  }

  return el(
    Dialog,
    { onOpenChange: v => !v && !busy && onClose(), open },
    el(
      DialogContent,
      { className: 'max-w-lg' },
      el(
        DialogHeader,
        {},
        el(DialogTitle, {}, 'Restore points'),
        el(DialogDescription, {}, `Before a sync replaces something in ${clean(pair?.grok?.name, 60)}'s Hermes twin, the old copy is kept here, newest first.`)
      ),
      q.isPending
        ? jsx(LinesSkeleton, { rows: 4 })
        : q.isError
          ? jsx(InlineError, { error: q.error, onRetry: () => void q.refetch() })
          : points.length === 0
            ? el('p', { className: 'py-4 text-center text-[0.75rem] text-(--ui-text-tertiary)' }, 'No restore points yet.')
            : el(
                'ul',
                { className: 'flex max-h-80 flex-col overflow-y-auto' },
                points.slice(0, 100).map((p, i) =>
                  jsx(
                    RestoreRow,
                    {
                      busy,
                      confirming: confirm !== null && confirm === p?.id,
                      first: i === 0,
                      onAsk: () => setConfirm(p?.id ?? null),
                      onCancel: () => setConfirm(null),
                      onRestore: () => void restore(p),
                      point: p
                    },
                    p?.id ?? i
                  )
                )
              )
    )
  )
}

// ── ready to switch dialog ──────────────────────────────────────────────────

// Unticked boxes need a stronger edge than the default to read on dark themes.
const CHECKBOX = 'mt-0.5 border-(--ui-stroke-primary)'

function CheckRow({ id, checked, onChange, title, detail, disabled, strike = false }) {
  return el(
    'div',
    { className: cn('flex min-w-0 items-start gap-2.5 py-1.5', disabled && 'opacity-60') },
    jsx(Checkbox, { checked, className: CHECKBOX, disabled, id, onCheckedChange: v => onChange(v === true) }),
    el(
      'label',
      { className: cn('flex min-w-0 flex-1 flex-col', !disabled && 'cursor-pointer'), htmlFor: id },
      el(
        'span',
        { className: cn('break-words text-[0.8125rem] leading-snug text-(--ui-text-primary)', strike && checked && 'text-(--ui-text-tertiary) line-through decoration-current/40') },
        clean(title, 600)
      ),
      detail && el('span', { className: 'break-words text-[0.6875rem] text-(--ui-text-tertiary)' }, clean(detail, 300))
    )
  )
}

const DAY_NAMES = ['Sundays', 'Mondays', 'Tuesdays', 'Wednesdays', 'Thursdays', 'Fridays', 'Saturdays']
const pad2 = n => String(n).padStart(2, '0')

// Five-field cron -> plain words for the common shapes; anything else stays as written.
function cronText(schedule) {
  const raw = clean(schedule, 120)
  const f = raw.split(' ')

  if (f.length !== 5) {
    return raw
  }

  const [min, hour, dom, mon, dow] = f
  const num = (v, max) => /^\d{1,2}$/.test(v) && Number(v) <= max
  const step = /^\*\/(\d{1,2})$/

  if (mon !== '*') {
    return raw
  }

  if (dom === '*' && dow === '*' && hour === '*') {
    if (min === '*') return 'every minute'
    if (step.test(min)) return `every ${Number(min.match(step)[1])} minutes`
    if (num(min, 59)) return Number(min) === 0 ? 'every hour, on the hour' : `every hour at :${pad2(min)}`
    return raw
  }

  if (dom === '*' && dow === '*' && num(min, 59) && step.test(hour)) {
    return `every ${Number(hour.match(step)[1])} hours at :${pad2(min)}`
  }

  if (!num(min, 59) || !num(hour, 23)) {
    return raw
  }

  const at = `${pad2(hour)}:${pad2(min)}`

  if (dom === '*' && dow === '*') {
    return `every day at ${at}`
  }

  if (dom === '*') {
    if (dow === '1-5') return `weekdays at ${at}`
    if (dow === '0,6' || dow === '6,0' || dow === '6-7' || dow === '6,7') return `weekends at ${at}`

    const days = dow.split(',')

    if (days.every(d => num(d, 7)) && days.length <= 6) {
      return `${joinWords([...new Set(days.map(d => DAY_NAMES[Number(d) % 7]))])} at ${at}`
    }

    return raw
  }

  if (dow === '*' && num(dom, 31) && Number(dom) >= 1) {
    return `day ${Number(dom)} of every month at ${at}`
  }

  return raw
}

function SwitchDialog({ pair, open, onClose, storage }) {
  const q = useQuery({ enabled: open, queryFn: () => api(pairPath(pair.pair_id, 'checklist')), queryKey: [ID, 'checklist', pair.pair_id] })
  const storeKey = `ticks.${pair.pair_id}`
  const [ticks, setTicks] = useState(() => storage?.get(storeKey, {}) ?? {})
  const [jobs, setJobs] = useState(null)
  const [busy, run] = useBusy()

  const routines = Array.isArray(q.data?.switch?.grok_routines_to_turn_off) ? q.data.switch.grok_routines_to_turn_off : []
  const paused = Array.isArray(q.data?.switch?.hermes_jobs_paused) ? q.data.switch.hermes_jobs_paused : []
  // A routine already shows under "Turn off in Grok" and, copied paused, under "Resume in Hermes":
  // it is not listed a third time under "Set up by hand".
  const routineNames = new Set(routines.map(r => r?.name))
  const byHand = (Array.isArray(q.data?.by_hand) ? q.data.by_hand : []).filter(
    b => !(paused.length > 0 && String(b?.item ?? '').startsWith('routine:') && routineNames.has(b?.name ?? String(b.item).slice(8)))
  )
  // Nothing is preselected: the owner ticks each job that should run from now on.
  const selected = jobs ?? {}
  const chosen = paused.filter(j => selected[j?.job_id]).map(j => j.job_id)
  const allJobs = paused.length > 0 && chosen.length === paused.length
  const routinesDone = routines.every((r, i) => ticks[`r:${r?.name ?? i}`])

  function tick(key, value) {
    const next = { ...ticks, [key]: value }
    setTicks(next)
    storage?.set(storeKey, next)
  }

  async function resume() {
    const res = await run('resume', () => post(pairPath(pair.pair_id, 'switch'), { job_ids: chosen }), 'Could not resume jobs')

    if (!res) {
      return
    }

    const ok = Array.isArray(res.resumed) ? res.resumed.length : 0
    const failed = Array.isArray(res.failed) ? res.failed : []

    if (failed.length) {
      host.notify({
        kind: 'warning',
        message: `Resumed ${plural(ok, 'job')}, ${failed.length} failed`,
        detail: clean(failed.map(f => `${f?.job_id}: ${f?.error}`).join('; '), 300)
      })
    } else {
      host.notify({ kind: 'success', message: ok ? `Resumed ${plural(ok, 'job')} in Hermes` : 'Switch checklist saved' })
      onClose()
    }

    refreshAll()
  }

  const section = (title, hint, body) =>
    el('section', { className: 'flex flex-col gap-1' }, el('h3', { className: SECTION_LABEL }, title), hint && el('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)' }, hint), body)

  return el(
    Dialog,
    { onOpenChange: v => !v && onClose(), open },
    el(
      DialogContent,
      { className: 'max-w-lg' },
      el(
        DialogHeader,
        {},
        el(DialogTitle, {}, 'Ready to switch'),
        el(
          DialogDescription,
          {},
          `Move ${clean(pair?.grok?.name, 60)} over to Hermes. Turn off each Grok routine first, so the same job never runs twice.`
        )
      ),
      q.isPending
        ? jsx(LinesSkeleton, { rows: 5 })
        : q.isError
          ? jsx(InlineError, { error: q.error, onRetry: () => void q.refetch() })
          : el(
              'div',
              { className: 'flex max-h-[55vh] flex-col gap-4 overflow-y-auto pr-1' },
              section(
                'Turn off in Grok',
                routines.length ? 'Tick each one after you turn it off in the Grok app.' : null,
                routines.length
                  ? routines.map((r, i) =>
                      jsx(
                        CheckRow,
                        {
                          checked: Boolean(ticks[`r:${r?.name ?? i}`]),
                          id: `${ID}-r-${pair.pair_id}-${i}`,
                          onChange: v => tick(`r:${r?.name ?? i}`, v),
                          detail: r?.schedule ? cronText(r.schedule) : null,
                          strike: true,
                          title: r?.name
                        },
                        `r${i}`
                      )
                    )
                  : el('p', { className: 'text-[0.75rem] text-(--ui-text-tertiary)' }, 'No Grok routines to turn off.')
              ),
              byHand.length > 0 &&
                section(
                  'Set up by hand',
                  null,
                  byHand.map((b, i) =>
                    jsx(
                      CheckRow,
                      {
                        checked: Boolean(ticks[`b:${b?.item ?? b?.id ?? i}`]),
                        detail: `${b?.side === 'grok' ? 'In Grok' : 'In Hermes'} · ${itemLabel(b?.item)}`,
                        id: `${ID}-b-${pair.pair_id}-${i}`,
                        onChange: v => tick(`b:${b?.item ?? b?.id ?? i}`, v),
                        strike: true,
                        title: b?.what ?? itemLabel(b?.item)
                      },
                      `b${i}`
                    )
                  )
                ),
              section(
                'Resume in Hermes',
                paused.length
                  ? el(
                      'span',
                      { className: 'flex items-center gap-2' },
                      el('span', { className: 'flex-1' }, 'These arrived paused. Tick the ones that should run from now on.'),
                      paused.length > 1 &&
                        el(
                          Button,
                          {
                            onClick: () => setJobs(allJobs ? {} : Object.fromEntries(paused.map(j => [j?.job_id, true]))),
                            size: 'xs',
                            variant: 'ghost'
                          },
                          allJobs ? 'Clear' : 'Select all'
                        )
                    )
                  : null,
                paused.length
                  ? paused.map((j, i) =>
                      jsx(
                        CheckRow,
                        {
                          checked: Boolean(selected[j?.job_id]),
                          detail: j?.schedule ? cronText(j.schedule) : null,
                          id: `${ID}-j-${pair.pair_id}-${i}`,
                          onChange: v => setJobs({ ...selected, [j?.job_id]: v }),
                          title: j?.name ?? j?.job_id
                        },
                        j?.job_id ?? i
                      )
                    )
                  : el('p', { className: 'text-[0.75rem] text-(--ui-text-tertiary)' }, 'No paused Hermes jobs.')
              )
            ),
      el(
        DialogFooter,
        {},
        !routinesDone && routines.length > 0 && el('span', { className: 'mr-auto self-center text-[0.6875rem] text-(--ui-text-tertiary)' }, 'Tick the Grok routines first'),
        el(Button, { onClick: onClose, variant: 'ghost' }, 'Cancel'),
        el(
          Button,
          { disabled: q.isPending || q.isError || !routinesDone || chosen.length === 0, loading: busy === 'resume', onClick: () => void resume() },
          chosen.length > 1 ? `Resume ${chosen.length} selected` : 'Resume selected'
        )
      )
    )
  )
}

// ── pair card ───────────────────────────────────────────────────────────────

function MetaDot() {
  return el('span', { 'aria-hidden': true, className: 'text-(--ui-text-quaternary)' }, '·')
}

function PairMeta({ pair }) {
  const parts = []
  const last = ago(pair?.last_sync_at)
  const inFlight = Number(pair?.pending?.in_flight) || 0
  const approval = Number(pair?.pending?.waiting_approval) || 0

  parts.push(el('span', { key: 'last', title: exact(pair?.last_sync_at) }, last ? `Last sync ${last}` : 'Not synced yet'))

  metaTexts(pair).forEach((t, i) => parts.push(el('span', { key: `p${i}` }, t)))

  if (inFlight) {
    parts.push(el('span', { key: 'fly' }, `${inFlight} with the Mirror bot`))
  }

  if (approval) {
    parts.push(el('span', { key: 'appr' }, `${approval} waiting for your OK`))
  }

  return el(
    'div',
    { className: 'flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[0.6875rem] text-(--ui-text-tertiary)' },
    parts.flatMap((p, i) => (i === 0 ? [p] : [jsx(MetaDot, {}, `d${i}`), p]))
  )
}

// The meta line already says "N changes for Hermes/Grok"; the state column
// repeats the reason only when it adds something.
function metaTexts(pair) {
  const out = []
  const toG = Number(pair?.pending?.to_grok) || 0
  const toH = Number(pair?.pending?.to_hermes) || 0

  if (toH) out.push(`${plural(toH, 'change')} for Hermes`)
  if (toG) out.push(`${plural(toG, 'change')} for Grok`)

  return out
}

const norm = t => clean(t, 200).toLowerCase().replace(/[.\s]+$/, '')

function distinctReason(pair) {
  const reason = clean(pair?.state_reason, 120)

  if (!reason || pair?.state === 'error') {
    return ''
  }

  const r = norm(reason)
  const same = [stateOf(pair?.state).label, ...metaTexts(pair)].some(t => norm(t) === r)

  return same ? '' : reason
}

// Connected wins; older engines only report when the Mirror bot was last seen.
function mirrorState(pair) {
  const m = pair?.mirror_bot

  if (!m || typeof m !== 'object') {
    return { connected: false, label: 'Mirror bot not set up' }
  }

  const seen = ago(m.last_seen_at)

  if (m.connected === true || (m.connected === undefined && seen)) {
    // The demo's Mirror bot is pretend: say so wherever its state shows.
    const label = pair?.demo ? 'Mirror bot connected (demo)' : seen ? `Mirror bot connected · seen ${seen}` : 'Mirror bot connected'

    return { connected: true, label }
  }

  return { connected: false, label: 'Mirror bot not set up' }
}

// ── waiting for approval ────────────────────────────────────────────────────

const WAITING_REASON = {
  needs_approval: 'Needs your OK before it is applied',
  secret_held: 'Held back: looks like it holds a secret'
}

function WaitingList({ pair }) {
  const [busy, run] = useBusy()
  const items = Array.isArray(pair?.waiting) ? pair.waiting.slice(0, 30) : []

  if (items.length === 0) {
    return null
  }

  async function approve(item) {
    const res = await run(item, () => post(pairPath(pair.pair_id, 'approve'), { item }), `Could not approve ${itemLabel(item)}`)

    if (res) {
      const sync = res.sync && typeof res.sync === 'object' ? res.sync : null
      toastSyncResult({ pairs: sync ? [{ pair_id: pair.pair_id, ...sync }] : [] }, { quietWhenNothing: true })
      host.notify({ id: `${ID}:approved:${pair.pair_id}`, kind: 'success', message: `Approved ${itemLabel(item)}` })
      refreshAll()
    }
  }

  return el(
    'section',
    { 'aria-label': 'Waiting for your OK', className: 'flex flex-col gap-1.5 rounded-md border border-(--ui-stroke-secondary) p-3' },
    el(
      'div',
      { className: 'flex items-center gap-2' },
      jsx(Icon, { className: 'text-(--ui-text-secondary)', name: 'pass' }),
      el('h3', { className: 'text-[0.8125rem] font-semibold text-(--ui-text-primary)' }, 'Waiting for your OK'),
      el('span', { className: 'text-[0.75rem] text-(--ui-text-tertiary)' }, 'Nothing is applied until you approve it.')
    ),
    el(
      'ul',
      { className: 'flex flex-col' },
      items.map((w, i) => {
        const item = clean(w?.item, 120)

        return el(
          'li',
          { className: cn('flex min-w-0 items-center gap-2 py-1.5', i > 0 && 'border-t border-(--ui-stroke-tertiary)'), key: `${item}-${i}` },
          el(
            'div',
            { className: 'flex min-w-0 flex-1 flex-col' },
            jsx(Truncate, { className: 'text-[0.8125rem] text-(--ui-text-primary)', max: 80, text: itemLabel(item) }),
            jsx(Truncate, {
              className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
              max: 200,
              text: w?.detail || WAITING_REASON[w?.reason] || clean(w?.reason, 80) || 'Waiting'
            })
          ),
          el(
            Button,
            {
              'aria-label': `Approve ${itemLabel(item)}`,
              disabled: Boolean(busy) || !item,
              loading: busy === item,
              onClick: () => void approve(item),
              size: 'xs',
              variant: 'secondary'
            },
            'Approve'
          )
        )
      })
    )
  )
}

// ── Mirror bot setup ────────────────────────────────────────────────────────

function MirrorDialog({ pair, open, onClose, storage }) {
  const key = `mirror.${pair.pair_id}`
  const saved = storage?.get(key, null) ?? null
  const [folder, setFolder] = useState(() => (typeof saved?.pair_folder === 'string' ? saved.pair_folder : ''))
  const [guide, setGuide] = useState(() => (saved && Array.isArray(saved.steps) ? saved : null))
  const [ticks, setTicks] = useState(() => storage?.get(`${key}.ticks`, {}) ?? {})
  const [busy, run] = useBusy()
  const mirror = mirrorState(pair)
  const demo = Boolean(pair?.demo)
  const name = clean(pair?.grok?.name || pair?.pair_id, 60)
  const folderText = folder.trim()
  const folderOk = folderText.length > 1 && folderText.length <= 1024 && !/[\u0000-\u001f]/.test(folderText)

  // Demo twins have no real Mirror bot: no folder is asked for, the demo engine
  // returns the same checklist and charter so the steps can be shown.
  async function connect(quiet = false) {
    const body = demo ? {} : { pair_folder: folderText }
    const res = await run('connect', () => post(pairPath(pair.pair_id, 'mirror'), body), 'Could not connect the Mirror bot')

    if (res) {
      const next = {
        charter: String(res.charter ?? '').slice(0, 40_000),
        connected: res.connected !== false,
        pair_folder: demo ? '' : folderText,
        steps: (Array.isArray(res.steps) ? res.steps : []).slice(0, 12).map((s, i) => ({ id: clean(s?.id ?? `s${i}`, 40), what: clean(s?.what, 600) }))
      }
      setGuide(next)
      storage?.set(key, next)

      if (quiet) {
        return
      }

      host.notify({
        id: `${ID}:mirror:${pair.pair_id}`,
        kind: 'success',
        message: demo ? `${name}: demo Mirror checklist ready` : `${name}: pair folder ready`,
        meta: 'Follow the steps to finish in Grok.'
      })
      refreshAll()
    }
  }

  async function disconnect() {
    const res = await run('off', () => post(pairPath(pair.pair_id, 'mirror'), { off: true }), 'Could not disconnect the Mirror bot')

    if (res) {
      setGuide(null)
      storage?.remove?.(key)
      host.notify({ id: `${ID}:mirror:${pair.pair_id}`, kind: 'info', message: `${name}: Mirror bot disconnected`, meta: 'Skills go back on the by-hand list.' })
      refreshAll()
    }
  }

  function tick(id, value) {
    const next = { ...ticks, [id]: value }
    setTicks(next)
    storage?.set(`${key}.ticks`, next)
  }

  // The demo twin starts with its pretend Mirror bot connected: show its checklist straight away
  // (the demo engine changes nothing when it is already connected).
  useEffect(() => {
    if (demo && mirror.connected && !guide) {
      void connect(true)
    }
  }, [])

  const steps = guide?.steps ?? []
  const done = steps.filter(s => ticks[s.id]).length

  return el(
    Dialog,
    { onOpenChange: v => !v && !busy && onClose(), open },
    el(
      DialogContent,
      { className: 'max-w-xl' },
      el(
        DialogHeader,
        {},
        el(DialogTitle, {}, 'Mirror bot'),
        el(
          DialogDescription,
          {},
          `A small bot in your Grok account whose routine carries skills between ${name} and its Hermes twin, through a folder your sync app (for example Google Drive for desktop) keeps in step. botport only reads and writes that folder; it makes no network calls.`
        )
      ),
      el(
        'div',
        { className: 'flex max-h-[60vh] flex-col gap-4 overflow-y-auto pr-1' },
        demo
          ? el(
              'section',
              { className: cn(NOTE, 'flex items-center gap-2') },
              jsx(Icon, { className: 'text-(--ui-text-tertiary)', name: 'beaker' }),
              el(
                'span',
                { className: 'flex-1' },
                mirror.connected
                  ? 'Demo twin: a pretend Mirror bot is connected, no folder needed. These are the steps you would follow in Grok.'
                  : 'Demo twin: no folder needed. See the steps you would follow in Grok.'
              ),
              !guide && el(Button, { disabled: Boolean(busy), loading: busy === 'connect', onClick: () => void connect(), size: 'sm' }, 'Show the steps')
            )
          : el(
              'section',
              { className: 'flex flex-col gap-1.5' },
              el('label', { className: SECTION_LABEL, htmlFor: `${ID}-folder-${pair.pair_id}` }, 'Pair folder'),
              el(
                'div',
                { className: 'flex items-center gap-2' },
                jsx(Input, {
                  'aria-invalid': (folderText && !folderOk) || undefined,
                  className: 'font-mono',
                  id: `${ID}-folder-${pair.pair_id}`,
                  maxLength: 1024,
                  onChange: e => setFolder(e.target.value),
                  placeholder: 'Full path of a folder your sync app shares',
                  size: 'sm',
                  spellCheck: false,
                  value: folder
                }),
                el(
                  Button,
                  { disabled: Boolean(busy) || !folderOk, loading: busy === 'connect', onClick: () => void connect(), size: 'sm' },
                  guide ? 'Update' : 'Connect'
                )
              ),
              el('p', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)' }, 'A real folder outside the Hermes home. Its contents never leave this machine except through your own sync app.')
            ),
        steps.length > 0 &&
          el(
            'section',
            { 'aria-label': 'Setup steps', className: 'flex flex-col gap-1' },
            el('h3', { className: SECTION_LABEL }, `Steps in Grok · ${done}/${steps.length} done`),
            steps.map((s, i) =>
              jsx(
                CheckRow,
                {
                  checked: Boolean(ticks[s.id]),
                  id: `${ID}-ms-${pair.pair_id}-${i}`,
                  onChange: v => tick(s.id, v),
                  strike: true,
                  title: `${i + 1}. ${s.what}`
                },
                s.id
              )
            )
          ),
        guide?.charter &&
          el(
            'section',
            { 'aria-label': 'Mirror bot charter', className: 'flex flex-col gap-1.5' },
            el(
              'div',
              { className: 'flex items-center gap-2' },
              el('h3', { className: cn(SECTION_LABEL, 'flex-1') }, 'Charter: paste as the Mirror bot’s instructions'),
              jsx(CopyButton, { appearance: 'button', buttonSize: 'xs', buttonVariant: 'secondary', label: 'Copy charter', showLabel: true, text: guide.charter })
            ),
            el(
              'pre',
              {
                className:
                  'max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md bg-(--ui-bg-quinary) px-2.5 py-2 font-mono text-[0.6875rem] leading-[1.45] text-(--ui-text-secondary)',
                tabIndex: 0
              },
              guide.charter
            )
          )
      ),
      // The dialog's own X is the one Close; the footer only holds Disconnect.
      (mirror.connected || guide) &&
        el(
          DialogFooter,
          {},
          el(Button, { disabled: Boolean(busy), loading: busy === 'off', onClick: () => void disconnect(), variant: 'ghost' }, 'Disconnect')
        )
    )
  )
}

// ── remove twin ─────────────────────────────────────────────────────────────

function RemoveTwinDialog({ pair, open, onClose }) {
  const name = clean(pair?.grok?.name || pair?.pair_id, 60)
  const profile = clean(pair?.hermes?.profile || 'default', 60)
  const plan = useQuery({
    enabled: open,
    gcTime: 0,
    queryFn: () => post(pairPath(pair.pair_id, 'remove'), { dry_run: true }),
    queryKey: [ID, 'remove-plan', pair.pair_id],
    retry: false
  })
  const removed = Array.isArray(plan.data?.removed) ? plan.data.removed : []

  return jsx(ConfirmDialog, {
    busyLabel: 'Removing…',
    cancelLabel: 'Cancel',
    children: plan.isPending
      ? jsx(LinesSkeleton, { rows: 3 })
      : plan.isError
        ? jsx(InlineError, { error: plan.error, onRetry: () => void plan.refetch() })
        : el(
            'div',
            { className: 'flex flex-col gap-1.5' },
            el('p', { className: SECTION_LABEL }, `Removed (${removed.length})`),
            el(
              'ul',
              { className: 'max-h-40 overflow-y-auto rounded-md bg-(--ui-bg-quinary) px-2.5 py-1.5 font-mono text-[0.6875rem] leading-[1.5] text-(--ui-text-secondary)' },
              removed.slice(0, 40).map((r, i) => el('li', { className: 'truncate', key: i, title: clean(r, 300) }, clean(r, 160))),
              removed.length > 40 && el('li', { key: 'more' }, `and ${removed.length - 40} more`)
            )
          ),
    confirmLabel: 'Remove twin',
    description: `${name} stops being twinned. The Hermes profile “${profile}” stays as a normal profile, and nothing changes in Grok.`,
    destructive: true,
    onClose,
    onConfirm: async () => {
      if (!plan.data) {
        throw new Error('Wait for the list of what will be removed.')
      }

      await post(pairPath(pair.pair_id, 'remove'), {})
      host.notify({ id: `${ID}:removed:${pair.pair_id}`, kind: 'success', message: `${name} is no longer twinned`, meta: `Profile “${profile}” kept.` })
      refreshAll()
    },
    open,
    title: `Remove the ${name} twin?`
  })
}

// ── pair card ───────────────────────────────────────────────────────────────

function SecretsBadge({ pair }) {
  const list = Array.isArray(pair?.secrets_withheld) ? pair.secrets_withheld.slice(0, 5) : []
  const n = Number(pair?.secrets_withheld_total) || list.length

  if (n <= 0) {
    return null
  }

  const where = list.map(w => clean(w?.location || itemLabel(w?.item), 200)).filter(Boolean)
  const label = `${where.length ? `${where.join(' · ')}. ` : ''}Items that look like they hold a key are not copied; the value is never stored or shown. A check, not a guarantee.`

  return el(
    Tip,
    { label },
    el(Badge, { className: 'ml-1', tabIndex: 0, variant: 'warn' }, jsx(Icon, { name: 'key', size: '0.65rem' }), secretsHeldText(n))
  )
}

function PairCard({ pair, storage }) {
  const [busy, run] = useBusy()
  const [panel, setPanel] = useState(null) // 'parity' | 'recent' | null
  const [dialog, setDialog] = useState(null) // 'restore' | 'switch' | 'mirror' | 'remove' | null
  const name = clean(pair?.grok?.name || pair?.pair_id, 60)
  const profile = clean(pair?.hermes?.profile || 'default', 60)
  const parity = pair?.parity && Number(pair.parity.total) > 0 ? pair.parity : null
  const missing = pair?.state === 'grok_missing' || pair?.grok?.present === false
  const mirror = mirrorState(pair)
  const restoreN = Math.max(0, Math.floor(Number(pair?.restore_points) || 0))
  const pairError = pair?.state === 'error' ? clean(pair?.error?.message ?? pair?.error ?? pair?.state_reason ?? '', 300) : ''

  const togglePanel = id => setPanel(panel === id ? null : id)
  const extraReason = distinctReason(pair)

  async function syncNow() {
    await run('sync', () => syncAndToast({ pair_id: pair.pair_id }), `Could not sync ${name}`)
  }

  async function sendToGrok() {
    const res = await run('send', () => post(pairPath(pair.pair_id, 'send-to-grok')), `Could not pack ${name} for Grok`)

    if (res) {
      const items = Number(res?.pack?.items ?? (Number(res?.skills) || 0) + (Number(res?.digest_entries) || 0)) || 0
      toastSyncResult(res, { quietWhenNothing: true })
      host.notify({
        id: `${ID}:send:${pair.pair_id}`,
        kind: 'success',
        message: items ? `${plural(items, 'item')} ready for Grok` : 'Packed for Grok',
        meta: clean(res?.note || 'Run the Mirror routine (Test) in Grok to apply.', 200)
      })
      refreshAll()
    }
  }

  return el(
    'article',
    { 'aria-label': `${name} twin`, className: cn(CARD, 'flex flex-col gap-3') },
    // Head: who is twinned with what, and how it's doing.
    el(
      'header',
      { className: 'flex min-w-0 items-start gap-3' },
      jsx(Avatar, { name }),
      el(
        'div',
        { className: 'flex min-w-0 flex-1 flex-col gap-0.5' },
        el(
          'div',
          { className: 'flex min-w-0 items-center gap-1.5' },
          jsx(Truncate, { className: 'text-[0.875rem] font-semibold text-(--ui-text-primary)', max: 60, text: name }),
          el(Tip, { label: 'Grok bot and Hermes profile are twins' }, el('span', { className: 'shrink-0 text-(--ui-text-quaternary)', tabIndex: 0 }, jsx(Icon, { name: 'arrow-swap' }), el('span', { className: 'sr-only' }, 'twinned with'))),
          jsx(Truncate, { className: 'font-mono text-[0.75rem] text-(--ui-text-secondary)', max: 60, text: profile }),
          pair?.demo && jsx(Badge, { className: 'ml-1', variant: 'muted', children: 'Demo' }),
          jsx(SecretsBadge, { pair })
        ),
        jsx(PairMeta, { pair })
      ),
      el(
        'div',
        { className: 'flex shrink-0 flex-col items-end gap-0.5' },
        jsx(StateLabel, { reason: pair?.state_reason, state: pair?.state }),
        extraReason && jsx(Truncate, { className: 'max-w-56 text-right text-[0.6875rem] text-(--ui-text-tertiary)', max: 120, text: extraReason })
      )
    ),
    missing &&
      el(
        'div',
        { className: NOTE },
        'This bot is no longer in the Grok app. The Hermes twin keeps working; nothing is deleted. If you removed the bot on purpose, the twin is now just a Hermes profile.'
      ),
    pairError && jsx(InlineError, { error: new Error(pairError), onRetry: () => void syncNow() }),
    jsx(Coverage, { coverage: pair?.coverage, mirrorConnected: mirror.connected }),
    jsx(WaitingList, { pair }),
    Number(pair?.conflicts) > 0 && jsx(ConflictCard, { pair }),
    // Actions
    el(
      'div',
      { className: 'flex flex-wrap items-center gap-1.5' },
      el(Button, { 'aria-label': `Sync ${name} now`, disabled: Boolean(busy), loading: busy === 'sync', onClick: () => void syncNow(), size: 'sm', variant: 'secondary' }, jsx(Icon, { name: 'sync' }), 'Sync now'),
      el(
        Tip,
        { label: mirror.connected ? 'Packs Hermes-side skills for your Mirror bot. Its routine applies them in Grok.' : 'Set up the Mirror bot first, or apply changes in Grok by hand.' },
        el(Button, { 'aria-label': `Send ${name} changes to Grok`, disabled: Boolean(busy) || missing, loading: busy === 'send', onClick: () => void sendToGrok(), size: 'sm', variant: 'outline' }, jsx(Icon, { name: 'cloud-upload' }), 'Send to Grok')
      ),
      el(Button, { 'aria-label': `Ready to switch ${name}`, onClick: () => setDialog('switch'), size: 'sm', variant: 'outline' }, jsx(Icon, { name: 'checklist' }), 'Ready to switch'),
      el(
        Button,
        { 'aria-expanded': panel === 'parity', 'aria-label': `Setup parity for ${name}`, onClick: () => togglePanel('parity'), size: 'sm', variant: 'ghost' },
        parity ? jsx(StatusDot, { tone: Number(parity.passed) >= Number(parity.total) ? 'good' : 'warn' }) : jsx(Icon, { name: 'checklist' }),
        parity ? `Setup parity ${Number(parity.passed) || 0}/${Number(parity.total)}` : 'Setup parity'
      ),
      el(
        Button,
        { 'aria-label': `Restore points for ${name}${restoreN > 0 ? ` (${restoreN})` : ''}`, onClick: () => setDialog('restore'), size: 'sm', variant: 'ghost' },
        jsx(Icon, { name: 'history' }),
        'Restore points',
        // The count sits in a fixed-width slot that is always there (invisible at 0),
        // so the row keeps its width when the first restore point appears.
        el(
          'span',
          {
            'aria-hidden': true,
            className: cn(
              'inline-flex h-4 min-w-5 items-center justify-center rounded-full bg-(--ui-bg-tertiary) px-1 text-[0.625rem] tabular-nums text-(--ui-text-secondary)',
              restoreN === 0 && 'invisible'
            ),
            'data-count': restoreN
          },
          restoreN > 99 ? '99+' : String(restoreN)
        )
      ),
      el(
        Button,
        { 'aria-expanded': panel === 'recent', className: 'ml-auto', onClick: () => togglePanel('recent'), size: 'sm', variant: 'ghost' },
        jsx(DisclosureCaret, { open: panel === 'recent' }),
        'Recent activity'
      )
    ),
    panel === 'parity' && jsx(ParityPanel, { pair }),
    panel === 'recent' && el('div', { className: 'border-t border-(--ui-stroke-tertiary) pt-2' }, jsx(RecentList, { pair })),
    // Footer: the Mirror bot link and the way out.
    el(
      'footer',
      { className: 'flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-(--ui-stroke-tertiary) pt-2 text-[0.6875rem] text-(--ui-text-tertiary)' },
      el(
        'span',
        { className: 'inline-flex items-center gap-1.5' },
        jsx(StatusDot, { tone: mirror.connected ? 'good' : 'muted' }),
        mirror.label
      ),
      el(
        Button,
        { 'aria-label': `${mirror.connected ? 'Manage' : 'Set up'} the Mirror bot for ${name}`, onClick: () => setDialog('mirror'), size: 'micro', variant: 'textStrong' },
        mirror.connected ? 'Manage' : 'Set up'
      ),
      el(
        Button,
        { 'aria-label': `Remove the ${name} twin`, className: 'ml-auto', onClick: () => setDialog('remove'), size: 'micro', variant: 'text' },
        'Remove twin'
      )
    ),
    dialog === 'restore' && jsx(RestoreDialog, { onClose: () => setDialog(null), open: true, pair }),
    dialog === 'switch' && jsx(SwitchDialog, { onClose: () => setDialog(null), open: true, pair, storage }),
    dialog === 'mirror' && jsx(MirrorDialog, { onClose: () => setDialog(null), open: true, pair, storage }),
    dialog === 'remove' && jsx(RemoveTwinDialog, { onClose: () => setDialog(null), open: true, pair })
  )
}

// ── add twins dialog ────────────────────────────────────────────────────────

const PROFILE_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/
const SCAN_BADGE = { caution: 'warn', dangerous: 'destructive', safe: 'success' }
const ACTION_WORDS = { by_hand: 'by hand', held_back: 'held back', via_mirror: 'via Mirror bot' }
const SKIP_REASON = { already_twinned: 'already a twin', profile_already_paired: 'that profile already has a twin' }

// One row of a create preview. The preview text is botport's, with lines that
// look like secrets already masked; it is shown as plain text, capped.
function PreviewItem({ item }) {
  const [open, setOpen] = useState(false)
  const preview = typeof item?.preview === 'string' ? item.preview.slice(0, 4_000) : ''
  const byHand = item?.action === 'by_hand'

  return el(
    'li',
    { className: 'flex flex-col py-0.5' },
    el(
      'div',
      { className: 'flex min-w-0 items-center gap-2 text-[0.75rem]' },
      el('span', { className: 'w-20 shrink-0 text-(--ui-text-tertiary)' }, kindLabel(item?.kind)),
      jsx(Truncate, {
        className: 'flex-1 text-(--ui-text-secondary)',
        max: 80,
        // Persona rows (name, description, instructions) repeat their kind as the name: show the masked first line instead.
        text: clean(item?.name, 80) === kindLabel(item?.kind) && preview ? preview.split('\n').find(l => l.trim()) ?? '' : item?.name
      }),
      item?.scan &&
        el(Tip, { label: 'Hermes skill safety scan' }, el(Badge, { tabIndex: 0, variant: SCAN_BADGE[item.scan] ?? 'muted' }, `scan: ${clean(item.scan, 12)}`)),
      jsx(Badge, { variant: byHand ? 'outline' : 'muted', children: ACTION_WORDS[item?.action] ?? clean(item?.action || 'create', 16) }),
      preview &&
        el(
          Button,
          { 'aria-expanded': open, 'aria-label': `${open ? 'Hide' : 'Show'} preview of ${clean(item?.name, 60)}`, onClick: () => setOpen(!open), size: 'icon-xs', variant: 'ghost' },
          jsx(DisclosureCaret, { open })
        )
    ),
    open &&
      el(
        'pre',
        {
          className:
            'mt-1 ml-22 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-md bg-(--ui-bg-quinary) px-2.5 py-1.5 font-mono text-[0.6875rem] leading-[1.45] text-(--ui-text-secondary)',
          tabIndex: 0
        },
        preview
      )
  )
}

// grok id -> why its profile cannot be used: it already backs a twin, or
// another bot in this preview uses it too (one profile backs one twin).
function profileClash(preview, profiles) {
  const inUse = new Set((Array.isArray(preview?.profiles_in_use) ? preview.profiles_in_use : []).map(p => clean(p, 64)))
  const pairs = Array.isArray(preview?.pairs) ? preview.pairs : []
  const out = {}

  for (const p of pairs) {
    const gid = p?.grok?.id
    const value = profiles[gid] ?? ''

    if (!value) {
      continue
    }

    if (inUse.has(value)) {
      out[gid] = 'This profile already backs a twin. Pick another name.'
    } else if (pairs.some(o => o?.grok?.id !== gid && profiles[o?.grok?.id] === value)) {
      out[gid] = 'Another bot here uses this profile too. Give each twin its own.'
    }
  }

  return out
}

function profilesOf(preview) {
  const names = {}

  for (const p of Array.isArray(preview?.pairs) ? preview.pairs : []) {
    names[p?.grok?.id] = clean(p?.hermes?.profile ?? '', 64)
  }

  return names
}

// `initialPreview` lets the smoke test render the preview step directly.
function AddTwinsDialog({ open, onClose, initialPreview = null }) {
  const bots = useQuery({ enabled: open && !initialPreview, queryFn: () => api('/grok/bots'), queryKey: [ID, 'grok-bots'] })
  const [picked, setPicked] = useState({})
  const [preview, setPreview] = useState(initialPreview)
  const [profiles, setProfiles] = useState(() => profilesOf(initialPreview))
  const [busy, run] = useBusy()

  useEffect(() => {
    if (open) {
      setPicked({})
      setPreview(initialPreview)
      setProfiles(profilesOf(initialPreview))
    }
  }, [open, initialPreview])

  const list = Array.isArray(bots.data?.bots) ? bots.data.bots : []
  const ids = list.filter(b => picked[b?.grok_id] && !b?.twinned_pair_id).map(b => b.grok_id)
  const open_ = list.filter(b => b?.grok_id && !b?.twinned_pair_id)
  const allPicked = open_.length > 0 && open_.every(b => picked[b.grok_id])
  const previewPairs = Array.isArray(preview?.pairs) ? preview.pairs : []
  const taken = profileClash(preview, profiles)
  const badProfile = previewPairs.some(p => !PROFILE_RE.test(profiles[p?.grok?.id] ?? '') || taken[p?.grok?.id])

  async function doPreview() {
    const res = await run('preview', () => post('/twins/preview', { grok_ids: ids }), 'Could not preview')

    if (res) {
      setProfiles(profilesOf(res))
      setPreview(res)
    }
  }

  async function create() {
    const grokIds = previewPairs.map(p => p?.grok?.id).filter(Boolean)
    const res = await run('create', () => post('/twins/create', { grok_ids: grokIds, profiles }), 'Could not create twins')

    if (res) {
      const n = Array.isArray(res.pairs) ? res.pairs.length : grokIds.length
      const skippedN = Array.isArray(res.skipped) ? res.skipped.length : 0
      host.notify({
        kind: skippedN ? 'warning' : 'success',
        message: n === 1 ? 'Twin created' : `${n} twins created`,
        meta: skippedN ? `${plural(skippedN, 'bot')} skipped: that profile already has a twin.` : 'They now stay in step with Grok.'
      })
      refreshAll()
      onClose()
    }
  }

  let body

  if (!preview) {
    body = bots.isPending
      ? jsx(LinesSkeleton, { rows: 4 })
      : bots.isError
        ? jsx(InlineError, { error: bots.error, onRetry: () => void bots.refetch() })
        : list.length === 0
          ? bots.data?.app_found === false
            ? el('div', { className: NOTE }, 'The Grok desktop app was not found on this computer. Install it and sign in, open your bots once, then try again. Or try the demo.')
            : el('p', { className: 'py-4 text-center text-[0.75rem] text-(--ui-text-tertiary)' }, 'No bots found in the Grok app yet.')
          : el(
                'div',
                { className: 'flex flex-col gap-1' },
                el(
                  'div',
                  { className: 'flex items-center gap-2 text-[0.6875rem] text-(--ui-text-tertiary)' },
                  el('span', { className: 'flex-1' }, `${list.length} ${list.length === 1 ? 'bot' : 'bots'} in the Grok app. Pick the ones to twin.`),
                  open_.length > 0 &&
                    el(
                      Button,
                      {
                        'aria-label': allPicked ? 'Clear the selection' : 'Select every bot that is not a twin yet',
                        onClick: () => setPicked(allPicked ? {} : Object.fromEntries(open_.map(b => [b.grok_id, true]))),
                        size: 'sm',
                        variant: 'ghost'
                      },
                      allPicked ? 'Clear' : 'Select all'
                    )
                ),
                el(
                'ul',
                { className: 'flex max-h-80 flex-col overflow-y-auto' },
                list.map((b, i) => {
                  const twinned = Boolean(b?.twinned_pair_id)
                  const cid = `${ID}-bot-${i}`

                  return el(
                    'li',
                    { className: cn('flex min-w-0 items-start gap-2.5 py-2', i > 0 && 'border-t border-(--ui-stroke-tertiary)'), key: b?.grok_id ?? i },
                    jsx(Checkbox, {
                      'aria-label': clean(b?.name, 60),
                      checked: twinned || Boolean(picked[b?.grok_id]),
                      className: CHECKBOX,
                      disabled: twinned,
                      id: cid,
                      onCheckedChange: v => setPicked({ ...picked, [b?.grok_id]: v === true })
                    }),
                    el(
                      'label',
                      { className: cn('flex min-w-0 flex-1 flex-col', twinned ? 'opacity-60' : 'cursor-pointer'), htmlFor: cid },
                      el(
                        'span',
                        { className: 'flex min-w-0 items-center gap-1.5' },
                        jsx(Truncate, { className: 'text-[0.8125rem] font-medium text-(--ui-text-primary)', max: 60, text: b?.name }),
                        twinned && jsx(Badge, { variant: 'muted', children: 'Already a twin' })
                      ),
                      b?.description && jsx(Truncate, { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', max: 120, text: b.description })
                    ),
                    b?.updated_at && el('span', { className: 'shrink-0 text-[0.6875rem] text-(--ui-text-quaternary)', title: exact(b.updated_at) }, ago(b.updated_at))
                  )
                })
              )
              )
  } else {
    const skipped = Array.isArray(preview?.skipped) ? preview.skipped.slice(0, 20) : []
    body = el(
      'div',
      { className: 'flex max-h-[55vh] flex-col gap-3 overflow-y-auto pr-1' },
      skipped.length > 0 &&
        el(
          'div',
          { className: NOTE },
          `Skipped: ${skipped.map(k => `${clean(k?.name ?? list.find(b => b?.grok_id === k?.grok_id)?.name ?? k?.grok_id ?? (typeof k === 'string' ? k : 'a bot'), 60)} (${SKIP_REASON[k?.reason] ?? clean(k?.reason || 'skipped', 40)})`).join(', ')}`
        ),
      previewPairs.map((p, i) => {
        const gid = p?.grok?.id
        const items = Array.isArray(p?.items) ? p.items : []
        const held = Array.isArray(p?.secrets_withheld) ? p.secrets_withheld.length : 0
        const pausedN = Number(p?.schedules_paused) || 0
        const value = profiles[gid] ?? ''
        const invalid = !PROFILE_RE.test(value)
        const clash = !invalid && taken[gid]
        const inputId = `${ID}-profile-${i}`

        return el(
          'section',
          { className: cn(CARD, 'flex flex-col gap-2'), key: gid ?? i },
          el(
            'div',
            { className: 'flex min-w-0 items-center gap-2' },
            jsx(Avatar, { name: p?.grok?.name }),
            jsx(Truncate, { className: 'flex-1 text-[0.8125rem] font-semibold text-(--ui-text-primary)', max: 60, text: p?.grok?.name })
          ),
          el(
            'div',
            { className: 'flex items-center gap-2' },
            el('label', { className: 'shrink-0 text-[0.75rem] text-(--ui-text-secondary)', htmlFor: inputId }, 'Hermes profile'),
            jsx(Input, {
              'aria-invalid': invalid || Boolean(clash) || undefined,
              className: 'font-mono',
              id: inputId,
              maxLength: 64,
              onChange: e => setProfiles({ ...profiles, [gid]: e.target.value.toLowerCase() }),
              size: 'sm',
              spellCheck: false,
              value
            })
          ),
          invalid && el('p', { className: 'text-[0.6875rem] text-(--ui-text-secondary)', role: 'alert' }, 'Use lower-case letters, digits, - and _.'),
          clash && el('p', { className: 'text-[0.6875rem] text-(--ui-text-secondary)', role: 'alert' }, clash),
          items.length > 0 &&
            el(
              'ul',
              { 'aria-label': `What ${clean(p?.grok?.name, 60)} brings`, className: 'flex flex-col' },
              items.slice(0, 60).map((it, j) => jsx(PreviewItem, { item: it }, `${it?.item ?? it?.name}-${j}`)),
              items.length > 60 && el('li', { className: 'py-0.5 text-[0.6875rem] text-(--ui-text-tertiary)', key: 'more' }, `and ${items.length - 60} more`)
            ),
          (held > 0 || pausedN > 0) &&
            el(
              'div',
              { className: 'flex flex-wrap gap-1.5' },
              held > 0 &&
                el(
                  Tip,
                  { label: 'Items that look like they hold a key are not copied at all. A check, not a guarantee.' },
                  el(Badge, { tabIndex: 0, variant: 'warn' }, jsx(Icon, { name: 'key', size: '0.65rem' }), held === 1 ? '1 suspected secret will be held back' : `${held} suspected secrets will be held back`)
                ),
              pausedN > 0 && el(Badge, { variant: 'muted' }, jsx(Icon, { name: 'debug-pause', size: '0.65rem' }), pausedN === 1 ? '1 schedule arrives paused' : `${pausedN} schedules arrive paused`)
            ),
          held > 0 &&
            el(
              'ul',
              { className: 'flex flex-col gap-0.5 text-[0.6875rem] text-(--ui-text-tertiary)' },
              p.secrets_withheld.slice(0, 8).map((w, j) => el('li', { key: j }, `${itemLabel(w?.item)}: ${clean(w?.location || w?.what || 'held back', 200)}`))
            )
        )
      })
    )
  }

  return el(
    Dialog,
    { onOpenChange: v => !v && !busy && onClose(), open },
    el(
      DialogContent,
      { className: 'max-w-xl' },
      el(
        DialogHeader,
        {},
        el(DialogTitle, {}, preview ? 'Preview twins' : 'Add twins from Grok'),
        el(DialogDescription, {}, preview ? 'Nothing has been written yet.' : 'Bots are read from the Grok app on this computer (name and description). Nothing is twinned until you pick it and check the preview.')
      ),
      body,
      el(
        DialogFooter,
        {},
        preview
          ? el(Button, { disabled: Boolean(busy), onClick: () => setPreview(null), variant: 'ghost' }, 'Back')
          : el(Button, { onClick: onClose, variant: 'ghost' }, 'Cancel'),
        preview
          ? el(Button, { disabled: Boolean(busy) || badProfile || previewPairs.length === 0, loading: busy === 'create', onClick: () => void create() }, previewPairs.length > 1 ? `Create ${previewPairs.length} twins` : 'Create twins')
          : el(Button, { disabled: ids.length === 0 || Boolean(busy), loading: busy === 'preview', onClick: () => void doPreview() }, ids.length > 1 ? `Preview ${ids.length}` : 'Preview')
      )
    )
  )
}

// ── page sections ───────────────────────────────────────────────────────────

async function startDemo(run) {
  const res = await run('demo', () => post('/demo', { action: 'start' }), 'Could not start the demo')

  if (res) {
    host.notify({ kind: 'success', message: 'Demo twins ready', meta: 'Invented bots only. Use Demo controls to make the Grok side change.' })
    refreshAll()
  }

  return res
}

// "Install it and run `gh attestation verify`" -> text with <code> spans.
function HintText({ text }) {
  return clean(text, 400)
    .split('`')
    .map((part, i) =>
      i % 2 === 1
        ? el('code', { className: 'rounded-[3px] bg-(--ui-bg-quaternary) px-1 font-mono text-[0.6875rem] text-(--ui-text-primary)', key: i }, part)
        : el('span', { key: i }, part)
    )
}

// showDemo: false when the empty state below already offers "Try demo".
function SetupBanner({ status, hasDemo, busy, onDemo, showDemo = !hasDemo }) {
  const engine = status?.engine ?? {}
  const hint = clean(engine.install_hint || '', 400)
  const problem = clean(engine.problem || '', 300)
  const found = Boolean(engine.version) || /trust|attest|owner|permission|digest|version/i.test(problem)
  const title = hasDemo ? 'Demo mode' : found ? 'botport needs attention before real twins' : 'One step left to twin your real Grok bots'
  const commands = [...hint.matchAll(/`([^`]{2,160})`/g)].map(m => m[1]).filter(c => /^(gh|botport|brew|cargo|hermes)\s/.test(c))

  return el(
    'section',
    { 'aria-label': 'Setup', className: cn(NOTE, 'flex flex-col gap-2') },
    el(
      'div',
      { className: 'flex items-start gap-2' },
      jsx(Icon, { className: 'mt-0.5 text-(--ui-text-tertiary)', name: found && !hasDemo ? 'warning' : 'info' }),
      el(
        'div',
        { className: 'flex min-w-0 flex-1 flex-col gap-1' },
        el('p', { className: 'font-medium text-(--ui-text-primary)' }, title),
        el(
          'p',
          {},
          hasDemo
            ? 'These twins use invented bots, so you can watch syncing without a Grok account. Real twins need the botport command-line tool.'
            : found
              ? 'Twins sync through botport, and the copy Hermes found cannot be used yet. Your bots are untouched. Meanwhile, try everything with invented demo bots.'
              : 'Twins sync through botport, a small command-line tool that is not installed yet. You can try everything right now with invented demo bots.'
        ),
        problem && el('p', { className: 'text-(--ui-text-tertiary)' }, `Problem: ${problem}`),
        hint && el('p', { className: 'text-(--ui-text-tertiary)' }, jsx(HintText, { text: hint }))
      )
    ),
    el(
      'div',
      { className: 'flex flex-wrap items-center gap-2 pl-6' },
      commands.slice(0, 2).map(c => el('div', { className: 'min-w-0 max-w-md flex-1', key: c }, jsx(CommandLine, { command: c, label: 'Copy command' }))),
      showDemo && el(Button, { disabled: Boolean(busy), loading: busy === 'demo', onClick: onDemo, size: 'sm' }, jsx(Icon, { name: 'beaker' }), 'Try demo')
    )
  )
}

// What botport needs before real twins: shown in a dialog from the demo line.
function BotportDialog({ status, open, onClose }) {
  const engine = status?.engine ?? {}
  const hint = clean(engine.install_hint || '', 400)
  const problem = clean(engine.problem || '', 300)
  const commands = [...hint.matchAll(/`([^`]{2,160})`/g)].map(m => m[1]).filter(c => /^(gh|botport|brew|cargo|hermes)\s/.test(c))

  return el(
    Dialog,
    { onOpenChange: v => !v && onClose(), open },
    el(
      DialogContent,
      { className: 'max-w-lg' },
      el(
        DialogHeader,
        {},
        el(DialogTitle, {}, 'Set up botport'),
        el(DialogDescription, {}, 'Real twins sync through botport, a small command-line tool. The demo twins do not need it.')
      ),
      el(
        'div',
        { className: 'flex flex-col gap-2 text-[0.75rem] leading-relaxed text-(--ui-text-secondary)' },
        problem && el('p', {}, el('span', { className: 'font-medium text-(--ui-text-primary)' }, 'Problem: '), problem),
        hint && el('p', {}, jsx(HintText, { text: hint })),
        commands.slice(0, 3).map(c => el('div', { key: c }, jsx(CommandLine, { command: c, label: 'Copy command' })))
      )
    )
  )
}

const DEMO_NOTE =
  'The demo twin started from a bot export and has a Mirror bot connected. A twin made only from the Grok app’s cache starts with its name and description.'

// Demo mode: one calm line of fixed height, so nothing below it moves when
// status refreshes (for example after Sync now).
function DemoLine({ status }) {
  const [open, setOpen] = useState(false)
  const needsBotport = status?.engine?.available === false || Boolean(status?.engine?.problem)

  return el(
    'div',
    { 'aria-label': 'Demo mode', className: 'flex h-7 shrink-0 items-center gap-2 px-1 text-[0.75rem] text-(--ui-text-tertiary)', role: 'note' },
    jsx(Icon, { className: 'text-(--ui-text-quaternary)', name: 'beaker' }),
    el(
      Tip,
      { label: DEMO_NOTE },
      el('span', { className: 'min-w-0 cursor-help truncate', tabIndex: 0 }, 'Demo mode: invented bots')
    ),
    needsBotport &&
      el(Button, { className: 'ml-auto', onClick: () => setOpen(true), size: 'micro', variant: 'textStrong' }, 'Set up botport'),
    open && jsx(BotportDialog, { onClose: () => setOpen(false), open: true, status })
  )
}

function EmptyTwins({ busy, onDemo, onAdd }) {
  return el(
    'div',
    { className: 'grid min-h-64 place-items-center px-4 text-center' },
    el(
      'div',
      { className: 'flex max-w-sm flex-col items-center gap-2' },
      jsx(Icon, { className: 'text-(--ui-text-quaternary)', name: 'mirror', size: '1.5rem' }),
      el('h2', { className: 'text-sm font-medium text-(--ui-text-primary)' }, 'No twins yet'),
      el(
        'p',
        { className: 'text-xs leading-relaxed text-(--ui-text-tertiary)' },
        'A twin is a Hermes profile that stays in step with one of your Grok bots. Keep using Grok; switch when the twin is ready.'
      ),
      el(
        'div',
        { className: 'mt-1 flex flex-wrap justify-center gap-2' },
        el(Button, { disabled: Boolean(busy), loading: busy === 'demo', onClick: onDemo, size: 'sm' }, jsx(Icon, { name: 'beaker' }), 'Try demo'),
        el(Button, { disabled: Boolean(busy), onClick: onAdd, size: 'sm', variant: 'outline' }, jsx(Icon, { name: 'add' }), 'Add twins from Grok')
      )
    )
  )
}

function DemoControls() {
  const [busy, run] = useBusy()
  const actions = [
    { id: 'grok-edit', icon: 'edit', label: 'Simulate Grok edit', done: 'Grok side edited: the description changed' },
    { id: 'conflict', icon: 'git-compare', label: 'Simulate conflict', done: 'Same skill edited on both sides' },
    { id: 'secret', icon: 'key', label: 'Simulate leaked key', done: 'A fake key was pasted into the Grok side' }
  ]

  async function go(a) {
    const res = await run(a.id, () => post('/demo', { action: a.id }), `${a.label} failed`)

    if (res) {
      host.notify({ id: `${ID}:demo`, kind: 'info', message: a.done, meta: 'The twin picks it up at its next check, or press Sync now.' })
      refreshAll()
    }
  }

  return el(
    'section',
    { 'aria-label': 'Demo controls', className: cn(CARD, 'flex flex-col gap-2 border-dashed') },
    el(
      'div',
      { className: 'flex items-baseline gap-2' },
      el('h2', { className: 'text-[0.8125rem] font-semibold text-(--ui-text-primary)' }, 'Demo controls'),
      el('span', { className: 'text-[0.75rem] text-(--ui-text-tertiary)' }, 'Change the invented Grok side, then watch the twin catch up.')
    ),
    el(
      'div',
      { className: 'flex flex-wrap gap-1.5' },
      actions.map(a =>
        el(Button, { disabled: Boolean(busy), key: a.id, loading: busy === a.id, onClick: () => void go(a), size: 'sm', variant: 'secondary' }, jsx(Icon, { name: a.icon }), a.label)
      )
    )
  )
}

function Disclosure() {
  return el(
    'footer',
    { className: 'px-1 pt-2 text-[0.6875rem] leading-relaxed text-(--ui-text-quaternary)' },
    "Runs inside Hermes. Reads the Grok app's bot list (names, descriptions) and routine-change events from selected bots' local transcripts; never login or credential files. Calls the botport CLI. No network calls. No telemetry. ",
    el(
      Tip,
      {
        label:
          'Unofficial; not affiliated with xAI. Items that look like they hold a key are held back, not copied (a check, not a guarantee). Replaced items are kept as restore points. Built on botport (MIT).'
      },
      el('span', { className: 'cursor-help underline decoration-dotted underline-offset-2', tabIndex: 0 }, 'Learn more')
    )
  )
}

function PageSkeleton() {
  return el(
    'div',
    { 'aria-busy': true, 'aria-label': 'Loading twins', className: 'flex flex-col gap-3' },
    [0, 1].map(i =>
      el(
        'div',
        { className: cn(CARD, 'flex flex-col gap-3'), key: i },
        el('div', { className: 'flex items-center gap-3' }, jsx(Skeleton, { className: 'size-8' }), el('div', { className: 'flex flex-1 flex-col gap-1.5' }, jsx(Skeleton, { className: 'h-3.5 w-48' }), jsx(Skeleton, { className: 'h-3 w-72 max-w-full' }))),
        jsx(Skeleton, { className: 'h-3 w-full max-w-lg' }),
        jsx(Skeleton, { className: 'h-6 w-80 max-w-full' })
      )
    )
  )
}

function BackendOff({ error, onRetry, retrying }) {
  return el(
    'div',
    { className: 'grid min-h-72 place-items-center px-4' },
    el(
      ErrorState,
      {
        description: "Enable the plugin's Python half: hermes plugins enable grok-bot-twin, then restart the gateway.",
        title: 'The twin service is not running'
      },
      el(
        'div',
        { className: 'mx-auto flex w-full max-w-sm flex-col items-stretch gap-2' },
        jsx(CommandLine, { command: ENABLE_CMD }),
        el(Button, { loading: retrying, onClick: onRetry, size: 'sm', variant: 'secondary' }, 'Retry'),
        error && el('p', { className: 'text-center text-[0.6875rem] text-(--ui-text-quaternary)', title: errorText(error) }, clean(errorText(error), 140))
      )
    )
  )
}

function TwinsPage({ storage }) {
  const qc = useQueryClient()
  const status = useQuery({ queryFn: () => api('/status'), queryKey: STATUS_KEY, refetchInterval: 30_000, retry: 1 })
  const [busy, run] = useBusy()
  const [adding, setAdding] = useState(false)

  const data = status.data
  const pairs = useMemo(() => (Array.isArray(data?.pairs) ? data.pairs : []), [data])
  const hasDemo = pairs.some(p => p?.demo)
  const demoMode = Boolean(data) && (data.mode === 'demo' || (pairs.length > 0 && pairs.every(p => p?.demo)))
  const needsSetup = Boolean(data) && !demoMode && (data.mode === 'setup' || data.engine?.available === false)

  async function syncAll() {
    await run('sync', () => syncAndToast({}), 'Could not sync')
  }

  async function removeDemo() {
    const res = await run('demo', () => post('/demo', { action: 'remove' }), 'Could not remove the demo')

    if (res) {
      pairs.filter(p => p?.demo).forEach(p => settleConflictToast(p.pair_id))
      host.notify({ kind: 'info', message: 'Demo removed', meta: 'The invented twins and their demo profiles are gone.' })
      void qc.invalidateQueries({ queryKey: [ID] })
    }
  }

  const header = el(
    'header',
    { className: 'flex shrink-0 flex-wrap items-start gap-x-4 gap-y-2 px-4 pb-3 pt-2' },
    el(
      'div',
      { className: 'flex min-w-0 flex-1 flex-col gap-0.5' },
      el(
        'div',
        { className: 'flex items-center gap-2' },
        el('h1', { className: 'text-sm font-semibold text-(--ui-text-primary)' }, 'Twins'),
        pairs.length > 0 && el('span', { className: 'rounded-full bg-(--ui-bg-quaternary) px-1.5 py-px text-[0.625rem] tabular-nums text-(--ui-text-tertiary)' }, String(pairs.length))
      ),
      el('p', { className: 'text-xs text-(--ui-text-tertiary)' }, 'Try Hermes without leaving Grok. Switch when your twin is ready.')
    ),
    data &&
      el(
        'div',
        { className: 'flex flex-wrap items-center gap-1.5' },
        hasDemo
          ? el(Button, { disabled: Boolean(busy), loading: busy === 'demo', onClick: () => void removeDemo(), size: 'sm', variant: 'ghost' }, 'Remove demo')
          : pairs.length > 0 && !needsSetup && el(Button, { disabled: Boolean(busy), loading: busy === 'demo', onClick: () => void startDemo(run), size: 'sm', variant: 'ghost' }, jsx(Icon, { name: 'beaker' }), 'Try demo'),
        el(Button, { disabled: Boolean(busy) || pairs.length === 0, loading: busy === 'sync', onClick: () => void syncAll(), size: 'sm', variant: 'outline' }, jsx(Icon, { name: 'sync' }), 'Sync now'),
        el(Button, { disabled: Boolean(busy), onClick: () => setAdding(true), size: 'sm' }, jsx(Icon, { name: 'add' }), 'Add twins')
      )
  )

  let body

  if (status.isPending) {
    body = jsx(PageSkeleton, {})
  } else if (status.isError && !data) {
    body = jsx(BackendOff, { error: status.error, onRetry: () => void status.refetch(), retrying: status.isFetching })
  } else {
    body = el(
      'div',
      { className: 'flex flex-col gap-3' },
      demoMode
        ? jsx(DemoLine, { status: data })
        : needsSetup && jsx(SetupBanner, { busy, hasDemo, onDemo: () => void startDemo(run), showDemo: !hasDemo && pairs.length > 0, status: data }),
      status.isError && jsx(InlineError, { error: status.error, onRetry: () => void status.refetch() }),
      pairs.length === 0
        ? jsx(EmptyTwins, { busy, onAdd: () => setAdding(true), onDemo: () => void startDemo(run) })
        : pairs.map((p, i) => jsx(PairCard, { pair: p, storage }, p?.pair_id ?? i)),
      hasDemo && jsx(DemoControls, {}),
      data?.last_check_at &&
        el('p', { className: 'px-1 text-[0.6875rem] text-(--ui-text-quaternary)', title: exact(data.last_check_at) }, `Checked Grok ${ago(data.last_check_at)}${data.interval_s ? ` · every ${Number(data.interval_s) >= 120 ? `${Math.round(Number(data.interval_s) / 60)} min` : `${Number(data.interval_s)} s`}` : ''}`)
    )
  }

  return el(
    'div',
    { className: 'relative flex h-full min-h-0 flex-col overflow-hidden' },
    header,
    el(
      'div',
      { className: 'min-h-0 flex-1 overflow-y-auto px-4 pb-6' },
      el('div', { className: 'mx-auto flex w-full max-w-3xl flex-col gap-3' }, body, jsx(Disclosure, {}))
    ),
    jsx(AddTwinsDialog, { onClose: () => setAdding(false), open: adding })
  )
}

// ── status bar chip ─────────────────────────────────────────────────────────

function summarize(pairs) {
  if (pairs.length === 0) {
    return { label: 'No twins — click to try the demo', tone: 'muted' }
  }

  const worst = pairs.reduce((w, p) => (stateOf(p?.state).rank > stateOf(w?.state).rank ? p : w), pairs[0])
  const s = stateOf(worst?.state)
  const count = pairs.filter(p => p?.state === worst?.state).length
  const what = worst?.state === 'in_step' ? 'in step' : `${count} ${s.label.toLowerCase()}`

  return { label: `${plural(pairs.length, 'twin')} · ${what}`, tone: s.tone }
}

function StatusChip() {
  const q = useQuery({ queryFn: () => api('/status'), queryKey: STATUS_KEY, refetchInterval: 30_000, retry: false })
  const pairs = Array.isArray(q.data?.pairs) ? q.data.pairs : []

  // The status chip is always mounted: every fresh status takes down conflict toasts that no longer apply.
  useEffect(() => {
    if (q.data) {
      reconcileConflictToasts(q.data.pairs, q.dataUpdatedAt || Date.now())
    }
  }, [q.data, q.dataUpdatedAt])
  const info = q.isError && !q.data ? { label: 'Twin service is off — click for setup', tone: 'muted' } : summarize(pairs)

  return el(
    Tip,
    { label: info.label },
    el(
      'button',
      {
        'aria-label': `Twins: ${info.label}`,
        className: cn(
          'inline-flex h-full items-center gap-1.5 rounded-none px-1.5 text-[0.6875rem] transition-colors',
          'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-(--ui-text-primary)'
        ),
        onClick: () => host.navigate(ROUTE),
        type: 'button'
      },
      jsx(StatusDot, { tone: info.tone }),
      el('span', {}, 'Twin')
    )
  )
}

// ── palette actions ─────────────────────────────────────────────────────────

async function paletteSync() {
  try {
    await syncAndToast({})
  } catch (err) {
    host.notifyError(err, 'Could not sync twins')
  }
}

async function paletteDemo() {
  host.navigate(ROUTE)

  if (cachedPairs().some(p => p?.demo)) {
    return
  }

  try {
    await post('/demo', { action: 'start' })
    host.notify({ kind: 'success', message: 'Demo twins ready', meta: 'Invented bots only. Use Demo controls to make the Grok side change.' })
    refreshAll()
  } catch (err) {
    host.notifyError(err, 'Could not start the demo')
  }
}

// ── plugin ──────────────────────────────────────────────────────────────────

export default {
  id: ID,
  name: 'Grok Bot Twin',
  description: 'Twins page, sidebar row, status-bar dot and ⌘K commands for Grok Bot Twin.',
  register(ctx) {
    rest = ctx.rest
    const storage = ctx.storage

    ctx.registerMany([
      { area: ROUTES_AREA, data: { path: ROUTE }, id: 'page', render: () => jsx(TwinsPage, { storage }) },
      { area: SIDEBAR_NAV_AREA, data: { codicon: 'mirror', label: 'Twins', path: ROUTE }, id: 'nav', order: 60 },
      { area: STATUSBAR_AREAS.right, id: 'status', order: 115, render: () => jsx(StatusChip, {}) },
      {
        area: PALETTE_AREA,
        data: { id: `${ID}.sync`, keywords: ['twin', 'grok', 'sync', 'bot'], label: 'Twin: sync now', run: () => void paletteSync() },
        id: 'palette-sync'
      },
      {
        area: PALETTE_AREA,
        data: { id: `${ID}.open`, keywords: ['twin', 'twins', 'grok', 'bot'], label: 'Twin: open Twins', run: () => host.navigate(ROUTE) },
        id: 'palette-open'
      },
      {
        area: PALETTE_AREA,
        data: { id: `${ID}.demo`, keywords: ['twin', 'grok', 'demo', 'try'], label: 'Twin: try demo', run: () => void paletteDemo() },
        id: 'palette-demo'
      }
    ])

    for (const type of EVENT_TYPES) {
      ctx.onEvent(`plugin.${ID}.${type}`, event => handleEvent({ payload: event?.payload, type }, ctx))
    }

    startEventPolling(ctx)

    ctx.onDispose(() => {
      rest = null
      refreshTimer = null
      seenKeys.clear()
      attentionToasts.clear()
    })
  }
}
