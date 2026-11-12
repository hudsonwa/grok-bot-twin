// Grok Bot Twin: Hermes Desktop half.
// Plain ESM, no JSX. Imports only the Hermes plugin SDK and React.
// Talks to this plugin's own REST backend (dashboard/plugin_api.py) through ctx.rest.
// Every string that comes from Grok or from botport is rendered as a React text node.
//
// One rule for the whole page: at every moment it shows one sentence about the state and at most
// one button, and only one button on the whole page is the primary one. Everything else sits behind
// a "…" menu or inside a sheet that explains itself and ends in Done.
//
// Words: a "twin" is a bot's second home in Hermes while it follows Grok; what stays when the following
// stops is a "Hermes bot". The program is always "botport" (it is "verified", never "checked"); "Check now"
// is only ever about twins. The Grok-side bot used for skill sync is "the sync bot"; its small file is
// "its pass". A terminal command is never the remedy for anything on this page.

import {
  Button,
  Checkbox,
  cn,
  Codicon,
  CopyButton,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DisclosureCaret,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
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
  Switch,
  Tabs,
  TabsList,
  TabsTrigger,
  Textarea,
  Tip,
  useQuery,
  useQueryClient
} from '@hermes/plugin-sdk'
import { useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'grok-bot-twin'
const ROUTE = '/twins'
// Capabilities → Plugins, at this plugin's own card (Hermes scrolls to it and marks it; an older Hermes opens the tab)
const PLUGINS_SETTINGS = `/capabilities?tab=plugins&plugin=${ID}`
const STATUS_KEY = [ID, 'status']
const SETUP_KEY = [ID, 'setup']
const ENABLE_CMD = 'hermes plugins enable grok-bot-twin'
const EVENT_TYPES = ['synced', 'secrets_withheld', 'conflict', 'changed', 'fleet', 'tasks']

// Doors captured at register time; components read them lazily.
let rest = null
let os = null // ctx.os: open a web page, pick a file, show a file in the file manager
let files = null // ctx.files: the app's own folder picker
let timers = null // ctx: tracked setTimeout / setInterval

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


// ── REST ────────────────────────────────────────────────────────────────────

// message: what happened, in plain words. hint: the one thing to do next.
// detail: technical text, only ever shown folded away.
class TwinError extends Error {
  constructor(message, code, hint = '', detail = '', lines = []) {
    super(message)
    this.code = code
    this.hint = hint
    this.detail = detail
    // 1-based line numbers the backend points at in a text it refused (never the text itself)
    this.lines = lines
  }
}

const FALLBACK_PROBLEM = 'Something went wrong inside Grok Bot Twin.'
const plainText = (value, max = 300) => clean(String(value ?? '').replace(/`/g, ''), max)

// A backend {code, message, hint} -> a TwinError. The backend writes plain sentences;
// the few codes below must read the same wherever they surface.
function problemOf(e) {
  const code = clean(e?.code, 40) || 'error'

  if (code === 'locked') {
    return new TwinError('Still working on the last change…', code, 'Try again in a moment.')
  }

  if (code === 'internal') {
    return new TwinError(FALLBACK_PROBLEM, code, 'Try again.')
  }

  const lines = (Array.isArray(e?.lines) ? e.lines : []).map(n => Math.floor(Number(n))).filter(n => n >= 1 && n <= 1_000_000).slice(0, 20)

  return new TwinError(plainText(e?.message) || FALLBACK_PROBLEM, code, plainText(e?.hint), '', lines)
}

// The transport threw (plugin off, gateway restarting, or an HTTP error status).
// An error body the backend wrote is unpacked; anything else becomes one plain
// sentence, with the raw text kept as folded detail only.
function transportError(err) {
  const raw = err instanceof Error ? err.message : String(err ?? '')
  const at = raw.indexOf('{')

  if (at >= 0) {
    try {
      const body = JSON.parse(raw.slice(at))
      const inner = body?.detail ?? body?.error

      if (inner && typeof inner === 'object' && (inner.message || inner.code)) {
        return problemOf(inner)
      }
    } catch {
      // not JSON: fall through to the plain sentence
    }
  }

  const lost = new TwinError('Hermes could not reach Grok Bot Twin.', 'unreachable', 'Check that the plugin is turned on, then try again.', clean(raw, 200))

  // the call never got to the plugin: whoever shows this must not take it for the backend's own "unreachable"
  lost.transport = true

  return lost
}

const pause = ms => new Promise(resolve => (timers ? timers.setTimeout(resolve, ms) : resolve()))

// Text onto the clipboard through the app's own door. False when that did not work: the caller then
// shows the text with a Copy button instead of claiming it was copied.
async function copyText(text) {
  try {
    return typeof os?.writeClipboard === 'function' ? (await os.writeClipboard(String(text))) === true : false
  } catch {
    return false
  }
}

// Every backend call goes through here. A `{error}` body (HTTP 200) becomes a throw
// so callers have one failure path. "Busy" is retried once, quietly.
async function api(path, opts, attempt = 0) {
  if (!rest) {
    throw new TwinError('Grok Bot Twin is still starting.', 'not_ready', 'Try again in a moment.')
  }

  let res

  try {
    res = await rest(path, opts)
  } catch (err) {
    throw transportError(err)
  }

  if (res && typeof res === 'object' && res.error) {
    if (res.error.code === 'locked' && attempt === 0) {
      await pause(1500)

      return api(path, opts, 1)
    }

    throw problemOf(res.error)
  }

  return res ?? {}
}

const post = (path, body = {}) => api(path, { method: 'POST', body })
const pairPath = (pid, tail) => `/pairs/${encodeURIComponent(pid)}/${tail}`

function refreshAll() {
  void queryClient.invalidateQueries({ queryKey: [ID] })
}

// Read /status now (not from cache), so a check's result is judged against
// the twins' real state and every row shows it at once.
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

// "Check now" (palette, row menu, attention boxes): post, read the fresh status, toast, refresh the rest.
async function checkAndToast(body, opts) {
  const res = await post('/sync', body)

  await freshStatus()
  toastCheckResult(res, opts)
  refreshAll()

  return res
}

function asProblem(err) {
  if (err instanceof TwinError) {
    return err
  }

  return new TwinError(FALLBACK_PROBLEM, 'internal', 'Try again.', clean(err instanceof Error ? err.message : String(err ?? ''), 200))
}

// Every failure toast has the same shape: what was being done, what happened, what to do next.
function notifyProblem(err, title) {
  const e = asProblem(err)

  host.notify({ detail: e.detail || undefined, kind: 'error', message: e.message, meta: e.hint || undefined, title: title || undefined })
}

// One busy flag per row/dialog: buttons disable while anything runs, the
// pressed one shows the spinner.
// The page's title, for the keyboard to land on when the thing it was on has left the page.
let pageTitle = null

// The keyboard is never left nowhere. `el` is where it was (the button that was pressed, or what opened a
// sheet): the focus goes back there, and when that has left the page (its row or box went with the act) to
// the page's title, from where Tab reaches the first thing on the page. Only when the focus is on nothing
// (or on the box around `el`, where a dialog parks it): a dialog that took it, or the owner who moved on,
// keeps it. Looked at again twice, because a button that waits is switched off for a moment (the browser
// lets go of a switched-off button) and a row often leaves a little later, with the backend's next answer.
// `home` is the sheet or dialog the control sat in: when the control is gone and the sheet is still open,
// the focus stays inside the sheet (the page behind it is not to be reached).
function landFocus(el, home = null, again = [400, 1500]) {
  if (typeof document === 'undefined' || !el || el === document.body) {
    return
  }

  const now = document.activeElement
  const nowhere = !now || now === document.body || (now !== el && now.contains?.(el) === true)

  if (!nowhere && now !== el) {
    return
  }

  if (nowhere && !el.isConnected) {
    const to = home && home.isConnected ? home : pageTitle

    to?.focus?.()

    return
  }

  if (nowhere && !el.disabled && typeof el.focus === 'function') {
    el.focus()
  }

  if (timers && again.length > 0) {
    timers.setTimeout(() => landFocus(el, home, again.slice(1)), again[0])
  }
}

function useBusy() {
  const [busy, setBusy] = useState(null)

  async function run(name, fn, failTitle) {
    if (busy) {
      return undefined
    }

    // where the keyboard is when the act starts: it is there again when the act is over (landFocus)
    const held = typeof document === 'undefined' ? null : document.activeElement
    const home = held?.closest?.('[role="dialog"],[role="alertdialog"]') ?? null

    setBusy(name)

    try {
      return await fn()
    } catch (err) {
      notifyProblem(err, failTitle)

      return undefined
    } finally {
      setBusy(null)

      if (held && timers) {
        timers.setTimeout(() => landFocus(held, home), 0)
      }
    }
  }

  return [busy, run]
}

// ── words ───────────────────────────────────────────────────────────────────

const STATE = {
  in_step: { label: 'Up to date', tone: 'good', rank: 0 },
  changes_waiting: { label: 'Updating…', tone: 'warn', rank: 1 },
  grok_missing: { label: 'Not in Grok any more', tone: 'muted', rank: 2 },
  needs_you: { label: 'Needs your attention', tone: 'warn', rank: 3 },
  error: { label: "Can't check right now", tone: 'bad', rank: 4 }
}

const stateOf = s => STATE[s] ?? STATE.error

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
  connector: 'connection',
  connectors: 'connections',
  model: 'model',
  activity: 'activity summary'
}

function kindLabel(kindOrItem) {
  const raw = clean(kindOrItem, 60)

  return KIND_LABEL[raw] ?? KIND_LABEL[raw.split(':')[0]] ?? raw.replace(/[._]/g, ' ')
}

// "skill:greet-visitors@3f2a…" -> { kind: 'skill', name: 'greet-visitors' }; the digest is never shown.
function itemParts(item) {
  const raw = clean(item, 120).split('@')[0].slice(0, 80)
  const at = raw.indexOf(':')

  return at > 0 ? { kind: kindLabel(raw.slice(0, at)), name: raw.slice(at + 1) } : { kind: kindLabel(raw), name: '' }
}

// For lists: "greet-visitors (skill)", "description".
function itemLabel(item) {
  const p = itemParts(item)

  return p.name ? `${p.name} (${p.kind})` : p.kind
}

// For sentences: 'the skill "greet-visitors"', "the description". A bare name is never shown as a word.
function itemPhrase(item) {
  const p = itemParts(item)

  return p.name ? `the ${p.kind} "${p.name}"` : `the ${p.kind}`
}

// "Trail Guide's", "Garden Notes'"
const owned = name => (/s$/i.test(name) ? `${name}'` : `${name}'s`)
const capital = text => `${text.charAt(0).toUpperCase()}${text.slice(1)}`

function joinWords(words) {
  if (words.length <= 1) {
    return words[0] ?? ''
  }

  return `${words.slice(0, -1).join(', ')} and ${words[words.length - 1]}`
}

function listWords(words, max = 3) {
  return words.length > max ? `${words.slice(0, max).join(', ')} and ${words.length - max} more` : joinWords(words)
}

const KIND_ORDER = ['name', 'description', 'instructions', 'skill', 'skills', 'routine', 'routines', 'connection', 'connections', 'activity summary']
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
  const tail = dirs.size === 1 && dirs.has('g2h') ? 'updated from Grok' : dirs.size === 1 && dirs.has('h2g') ? 'sent to Grok' : 'updated'

  return `${joinWords(words)} ${tail}`
}

// ── one twin, read for people ───────────────────────────────────────────────

const isSample = pair => Boolean(pair?.sample ?? pair?.demo)
const skillSyncOn = pair => pair?.mirror_bot?.connected === true
const nameOf = pair => clean(pair?.grok?.name || 'Your twin', 60)

// What is still to do before a new twin behaves like its bot: bring the instructions over, choose a model.
// null: the backend did not say (an older one), so the page claims nothing either way.
const SETUP_STEPS = ['instructions', 'model']
const leftOf = pair => (Array.isArray(pair?.setup_left) ? SETUP_STEPS.filter(k => pair.setup_left.includes(k)) : null)
const thingsLeft = pair => leftOf(pair)?.length ?? 0
// The sample that can show both of its sides: it is there to be watched, so its set-up chores are not
// counted as something left for the owner (they stay in its menu, to try).
const sampleShown = pair => isSample(pair) && typeof pair?.grok?.description === 'string' && typeof pair?.hermes?.description === 'string'
const choresLeft = pair => (sampleShown(pair) ? 0 : thingsLeft(pair))

// Suspected secrets, one entry per place ("description", "skill greet-visitors").
function secretPlaces(pair) {
  const list = Array.isArray(pair?.secrets_withheld) ? pair.secrets_withheld.slice(0, 20) : []
  const seen = new Map()

  for (const w of list) {
    const item = clean(w?.item || w?.kind || 'description', 140)

    if (!seen.has(item)) {
      seen.set(item, { direction: w?.direction === 'h2g' ? 'h2g' : 'g2h', item, lines: [] })
    }

    // Where it is: only the line number is taken from the backend's sentence, never its other words
    // (and the value itself is never sent).
    const line = Number(/\bline (\d{1,6})\b/.exec(String(w?.location ?? ''))?.[1])
    const place = seen.get(item)

    if (line >= 1 && !place.lines.includes(line) && place.lines.length < 5) {
      place.lines.push(line)
    }
  }

  // The count can be known while the list is not (an older engine): still say so once.
  if (seen.size === 0 && (Number(pair?.secrets_held) || 0) > 0) {
    seen.set('', { direction: 'g2h', item: '', lines: [] })
  }

  return [...seen.values()]
}

// Which changes `pending.to_grok` counts, when the backend names them; and which of them are skills (the
// only things skill sync can carry: a changed name or description always goes over by the owner's hand).
const toGrokItems = pair => (Array.isArray(pair?.pending?.to_grok_items) ? pair.pending.to_grok_items : []).map(i => clean(i, 120)).filter(Boolean).slice(0, 20)
const isSkillItem = item => /^skills?:/.test(clean(item, 120))

// Every open matter of a twin, in the order a person should deal with them: what blocks everything,
// then a choice between two versions, then what is ready to go, then the possible password (which
// clears by itself once it is taken out in Grok). Each matter has one sentence for the row and a
// short title for its header; only one is open at a time.
function mattersOf(pair) {
  const out = []
  const pending = pair?.pending ?? {}
  const conflicts = Number(pair?.conflicts) || 0

  if (pair?.state === 'error') {
    out.push({ id: 'error', kind: 'error', short: "Can't check right now", title: "Can't check right now" })
  }

  if (pair?.state === 'grok_missing' || pair?.grok?.present === false) {
    out.push({ id: 'missing', kind: 'missing', short: 'Not in Grok any more', title: 'Not in Grok any more' })
  }

  if (conflicts > 0) {
    const title = conflicts === 1 ? 'Something changed in both places' : `${conflicts} things changed in both places`

    out.push({ id: 'conflict', kind: 'conflict', short: conflicts === 1 ? 'Changed in both places' : title, title })
  }

  for (const w of Array.isArray(pair?.waiting) ? pair.waiting.slice(0, 30) : []) {
    if (!w?.item) {
      continue
    }

    const kind = itemParts(w.item).kind
    const title = w.to === 'grok' ? `A ${kind} changed in Hermes` : w.to === 'hermes' ? `A ${kind} from Grok is ready` : 'Something is waiting for your go-ahead'

    out.push({ id: `waiting-${clean(w.item, 120)}`, kind: 'waiting', short: w.to === 'grok' ? '1 change to send to Grok' : title, title, waiting: w })
  }

  if ((Number(pending.to_grok) || 0) > 0) {
    const n = Number(pending.to_grok)

    // (named for what it is: a description changed in Hermes is not "a skill change")
    const items = toGrokItems(pair)
    const other = items.filter(i => !isSkillItem(i))
    const title = other.length === 0 ? `${plural(n, 'skill change')} ready for Grok` : n === 1 ? `${capital(itemPhrase(other[0]))} changed in Hermes` : `${plural(n, 'change')} ready for Grok`

    out.push({ count: n, id: 'unsent', kind: 'unsent', short: `${plural(n, 'change')} to send to Grok`, title })
  }

  for (const place of secretPlaces(pair)) {
    out.push({ id: `secret-${place.item}`, kind: 'secret', short: 'A password or key was not copied', title: 'A password or key was not copied', ...place })
  }

  return out
}

// A matter that only asks "send this to Grok?" is not an alarm.
const isToSend = m => m.kind === 'unsent' || (m.kind === 'waiting' && m.waiting?.to === 'grok')

// Does this matter's box hold a main button the owner can press right now? (A real possible password is
// fixed in Grok: no button.) Two versions to choose between have one too: keeping the Hermes version, the
// side the owner is on; it replaces nothing, and the other choice sits right beside it. A bot deleted in
// Grok has "OK, stop following". Changes to send have one when they can be sent, or when the backend
// names them (then each has its Copy button).
const hasMainAction = (m, pair) =>
  m.kind === 'error' ||
  m.kind === 'waiting' ||
  m.kind === 'conflict' ||
  m.kind === 'missing' ||
  (m.kind === 'unsent' && (skillSyncOn(pair) || (Array.isArray(pair?.pending?.to_grok_items) && pair.pending.to_grok_items.length > 0))) ||
  (m.kind === 'secret' && isSample(pair))

// "10 Nov"
const shortDay = iso => {
  const ms = Date.parse(iso ?? '')

  if (!Number.isFinite(ms)) {
    return ''
  }

  const d = new Date(ms)

  return `${d.getDate()} ${new Intl.DateTimeFormat('en', { month: 'short' }).format(d)}`
}

const FOLLOWS = 'Follows Grok'
const NOT_FOLLOWING = 'Not following Grok right now'
const WAITING_FOR_MODEL = 'Waiting for you to choose a model in Hermes'

// The one status sentence of a row. `held`: twins cannot follow Grok at all right now, and the banner
// above the rows says why once; a row then says only that. `asked`: the owner was sent to Hermes to
// choose this twin's model and has not done it yet. A twin with an open matter and set-up left says both.
function statusSentence(pair, { held = false, asked = false } = {}) {
  const matters = mattersOf(pair)
  const pending = pair?.pending ?? {}
  // (the sample shows the point first, a twin following its bot: its set-up chores wait in its menu)
  const left = sampleShown(pair) ? [] : leftOf(pair)
  const leftText = left?.length ? `${plural(left.length, 'thing')} left` : ''
  const andLeft = text => (leftText ? `${text} · ${leftText}` : text)

  if (held && !isSample(pair)) {
    return NOT_FOLLOWING
  }

  if (matters.some(m => m.kind === 'error')) {
    return STATE.error.label
  }

  if (matters.some(m => m.kind === 'missing')) {
    return STATE.grok_missing.label
  }

  if (matters.length > 1) {
    const toSend = matters.filter(isToSend).reduce((n, m) => n + (m.count ?? 1), 0)

    return andLeft(matters.every(isToSend) ? `${plural(toSend, 'change')} to send to Grok` : `${matters.length} things need you`)
  }

  if (matters.length === 1) {
    return andLeft(matters[0].short)
  }

  if ((Number(pending.to_hermes) || 0) > 0) {
    return STATE.changes_waiting.label
  }

  if ((Number(pending.in_flight) || 0) > 0) {
    return `${plural(Number(pending.in_flight), 'change')} on the way to Grok`
  }

  if (left?.length) {
    return asked && left.length === 1 && left[0] === 'model' ? WAITING_FOR_MODEL : `Almost ready · ${leftText}`
  }

  if (pair?.switched?.at) {
    const n = Number(pair.switched.routines_on) || 0

    return n ? `${FOLLOWS} · ${plural(n, 'routine')} ${n === 1 ? 'runs' : 'run'} in Hermes` : FOLLOWS
  }

  // the plain calm state says the word a new owner waits for after setting up
  return isSample(pair) ? FOLLOWS : `${FOLLOWS} · ready`
}

// The shape next to the sentence: trouble is a triangle, a to-do is a half-filled circle, something to
// send is an arrow. A to-do is not a warning.
function rowTone(pair, { held = false } = {}) {
  const matters = mattersOf(pair)

  if (held && !isSample(pair)) return 'muted'
  if (matters.some(m => m.kind === 'error')) return 'bad'
  if (matters.some(m => m.kind === 'missing')) return 'muted'
  if (matters.length > 0) return matters.every(isToSend) ? 'send' : matters.every(m => m.kind === 'waiting') ? 'todo' : 'warn'
  if ((Number(pair?.pending?.to_hermes) || 0) > 0 || (Number(pair?.pending?.in_flight) || 0) > 0) return 'busy'
  if (choresLeft(pair) > 0) return 'todo'

  return 'good'
}

// When this twin was last looked at: the newest of the background round, the twin's own record and a
// check pressed on this page a moment ago (so "checked 37 sec ago" never follows a check by seconds).
const manualChecks = new Map() // pairId -> ms

function noteChecked(pairIds) {
  for (const pid of pairIds) {
    if (pid) {
      manualChecks.set(pid, Date.now())
    }
  }
}

function checkedIso(pair, lastCheckAt) {
  const times = [pair?.checked_at, pair?.last_sync_at, lastCheckAt].map(t => Date.parse(t ?? '')).filter(Number.isFinite)
  const manual = manualChecks.get(pair?.pair_id)

  if (manual) {
    times.push(manual)
  }

  return times.length ? new Date(Math.min(Math.max(...times), Date.now())).toISOString() : null
}

// Is there anything to move, or anything moved that could be paused again? Unknown counts as yes
// only when the twin's own list names a routine or a connection.
function canMove(pair) {
  if (pair?.switched?.at) {
    return true
  }

  const r = pair?.routines

  if (r && typeof r === 'object') {
    return (Number(r.in_grok) || 0) + (Number(r.paused_in_hermes) || 0) + (Number(r.on_in_hermes) || 0) > 0
  }

  return (Array.isArray(pair?.by_hand) ? pair.by_hand : []).some(b => /^(routine|connector):/.test(String(b?.item ?? '')))
}

// ── status cache helpers (used outside React too) ───────────────────────────

function cachedPairs() {
  return queryClient.getQueryData(STATUS_KEY)?.pairs ?? []
}

function pairName(pairId, payload) {
  const fromPayload = payload?.name ?? payload?.grok?.name
  const fromCache = cachedPairs().find(p => p?.pair_id === pairId)?.grok?.name

  return clean(fromPayload || fromCache || 'Your twin', 60)
}

// ── toasts (shared by events, polling and button results) ───────────────────
// Ids are per twin + kind, so an event toast and a button-result toast for the
// same thing replace each other instead of stacking.
//
// A toast never repeats what the Twins page is already showing: while the page is open, a matter
// that has its own box in a row (possible password, changed in both places, a twin that needs
// something) raises no toast at all. Elsewhere in Hermes it is raised once per item, leaves by
// itself, and its button opens Twins.

let pageOpen = 0 // Twins pages on screen right now

function toastSynced(pairId, applied, payload) {
  // Events may carry only `kinds` (+ a direction flag) instead of the full list.
  if ((!Array.isArray(applied) || applied.length === 0) && Array.isArray(payload?.kinds) && payload.kinds.length) {
    const direction = payload.direction ?? (/from Hermes/.test(String(payload.message ?? '')) ? 'h2g' : 'g2h')
    applied = payload.kinds.slice(0, 20).map(kind => ({ direction, kind }))
  }

  if (!Array.isArray(applied) || applied.length === 0) {
    return
  }

  // With the Twins page open the row itself shows a new name: a notice would say it twice. (A new description
  // is not on the row, so that one is still announced.)
  if (pageOpen > 0 && applied.every(a => kindLabel(a?.kind || a?.item || 'item') === 'name')) {
    return
  }

  host.notify({
    id: `${ID}:synced:${pairId}`,
    kind: 'success',
    message: `${pairName(pairId, payload)}: ${describeApplied(applied)}`
  })
}

// Twins that just got an attention toast (possible password, changed in both places),
// so a "Check now" right after does not answer it with "Up to date".
const attentionToasts = new Map() // pairId -> ms

function noteAttention(pairId) {
  attentionToasts.set(pairId, Date.now())
}

const recentAttention = (pairId, windowMs = 10_000) => Date.now() - (attentionToasts.get(pairId) ?? 0) < windowMs

const SECRET_SENTENCE = 'Something that looks like a password or key was not copied.'
const WARNING_TOAST_MS = 12_000
// no toast is gone before it can be read
const TOAST_MS = 5_000
const OPEN_TWINS = { label: 'Open Twins', onClick: () => host.navigate(ROUTE) }

// The SDK has no dismiss door: a toast with the same id replaces it, and a
// tiny duration takes that replacement down at once.
function dismissToast(id, message) {
  host.notify({ durationMs: 1, id, kind: 'success', message })
}

// Possible-password toasts already raised, per twin, by the place they are in: one toast per item.
const secretToasts = new Map() // pairId -> Set of places
const secretToastId = pairId => `${ID}:secrets:${pairId}`

function toastSecrets(pairId, withheld, count, payload) {
  const list = Array.isArray(withheld) ? withheld : []
  const places = list.map(w => clean(typeof w === 'string' ? w : w?.item || w?.kind || 'description', 140))
  const kinds = [...new Set(list.map(w => kindLabel(typeof w === 'string' ? w : w?.kind || w?.item || 'description')))]

  noteAttention(pairId)

  if (pageOpen > 0) {
    return
  }

  const shown = secretToasts.get(pairId) ?? new Set()
  const fresh = (places.length ? places : ['']).filter(p => !shown.has(p))

  if (fresh.length === 0) {
    return
  }

  fresh.forEach(p => shown.add(p))
  secretToasts.set(pairId, shown)

  const where = kinds.length ? joinWords(kinds) : 'description'
  const toGrok = (payload?.direction ?? list[0]?.direction) === 'h2g'

  // The whole sentence is in the visible message; meta is the always-shown second line.
  host.notify({
    action: OPEN_TWINS,
    durationMs: WARNING_TOAST_MS,
    id: secretToastId(pairId),
    kind: 'warning',
    message: SECRET_SENTENCE,
    meta: `It is in ${owned(pairName(pairId, payload))} ${where}. Remove it in ${toGrok ? 'Hermes' : 'the Grok app'}; Hermes notices by itself.`
  })
}

// The "changed in both places" toast stays up until the choice is made, so it is
// tracked by its stable id per twin and taken down once there is nothing left to choose.
const conflictToasts = new Map() // pairId -> { n: count shown, at: ms }
const conflictToastId = pairId => `${ID}:conflict:${pairId}`
const bothPlaces = n => (n === 1 ? 'something changed in both places' : `${n} things changed in both places`)

function settleConflictToast(pairId) {
  if (conflictToasts.delete(pairId)) {
    dismissToast(conflictToastId(pairId), `${pairName(pairId)}: nothing left to choose`)
  }
}

// Called with every fresh status: a twin with nothing left to choose (chosen, or
// the twin removed) loses its toast; a changed count updates it in place.
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

  // A possible password that is gone may be announced again if it ever comes back.
  for (const pid of [...secretToasts.keys()]) {
    const p = pairs.find(x => x?.pair_id === pid)

    if (!p || secretPlaces(p).length === 0) {
      secretToasts.delete(pid)
    }
  }
}

function toastConflict(pairId, count, payload, { quiet = false } = {}) {
  const n = Number(count) || 1

  if (!quiet) {
    noteAttention(pairId)
  }

  if (pageOpen > 0) {
    return
  }

  conflictToasts.set(pairId, { at: Date.now(), n })
  host.notify({
    action: OPEN_TWINS,
    id: conflictToastId(pairId),
    kind: 'warning',
    message: `${pairName(pairId, payload)}: ${bothPlaces(n)}`
  })
}

// The Twins page came on screen: its rows now say everything these toasts said.
function clearPageToasts() {
  for (const pid of [...conflictToasts.keys()]) {
    conflictToasts.delete(pid)
    dismissToast(conflictToastId(pid), 'Shown on the Twins page')
  }

  for (const pid of [...secretToasts.keys()]) {
    dismissToast(secretToastId(pid), 'Shown on the Twins page')
  }
}

const ATTENTION_STATES = ['needs_you', 'changes_waiting']

// Why a twin that had nothing new is still not up to date, in plain words.
function attentionReason(entry, cached) {
  const matters = cached ? mattersOf(cached) : []

  if (matters.length > 1) {
    return statusSentence(cached).replace(/^[A-Z]/, c => c.toLowerCase())
  }

  if (matters.length === 1) {
    return matters[0].short.replace(/^[A-Z]/, c => c.toLowerCase())
  }

  if (cached && thingsLeft(cached) > 0) {
    return `${plural(thingsLeft(cached), 'thing')} left to set up`
  }

  const state = entry?.state && entry.state !== 'in_step' ? entry.state : cached?.state
  const given = cached?.state === state ? clean(cached?.state_reason, 120) : ''

  if (given && !/^up to date$/i.test(given)) {
    return given
  }

  if (state === 'needs_you') {
    const n = Number(cached?.conflicts) || (Array.isArray(entry?.conflicts) ? entry.conflicts.length : 0) || 1

    return bothPlaces(n)
  }

  const toG = Number(entry?.to_grok) || Number(cached?.pending?.to_grok) || 0

  return toG ? `${plural(toG, 'change')} to send to Grok` : stateOf(state).label
}

// A /sync response -> the same toasts the events would raise.
// "Up to date" only when every twin really is: a twin that had nothing new
// but still needs something gets its reason instead (or nothing, when the page is
// showing it already or a toast for it went up in the last ~10 s).
function toastCheckResult(res, { quietWhenNothing = false } = {}) {
  const pairs = Array.isArray(res?.pairs) ? res.pairs : []
  let applied = 0
  let failed = 0
  const attention = []

  noteChecked(pairs.map(p => p?.pair_id))

  for (const p of pairs) {
    const pid = p?.pair_id
    applied += Array.isArray(p?.applied) ? p.applied.length : 0

    // One twin that failed is reported in its place; the others were still checked.
    if (p?.state === 'error') {
      failed += 1

      if (pageOpen === 0) {
        host.notify({
          id: `${ID}:check-error:${pid}`,
          kind: 'error',
          message: `${pairName(pid)}: can't check right now`,
          meta: plainText(p?.error?.message) || 'Hermes could not read your Grok bots.'
        })
      }

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

    // Not "up to date" while the twin's (fresh) status says anything else.
    const cachedOff = (Boolean(cached?.state) && cached.state !== 'in_step') || (cached ? mattersOf(cached).length > 0 || thingsLeft(cached) > 0 : false)

    if (ATTENTION_STATES.includes(p?.state) || cachedOff || recentAttention(pid)) {
      attention.push({ cached, entry: p, pid })
    }
  }

  if (applied > 0 || failed > 0 || quietWhenNothing) {
    return
  }

  if (attention.length === 0) {
    host.notify({
      id: `${ID}:check-result`,
      kind: 'info',
      message: pairs.length === 0 ? 'No twins to check yet' : pairs.length === 1 ? 'Up to date' : `All ${pairs.length} twins are up to date`
    })

    return
  }

  // The rows already say it while the page is open.
  if (pageOpen > 0) {
    host.notify({ durationMs: TOAST_MS, id: `${ID}:check-result`, kind: 'info', message: 'Checked just now' })

    return
  }

  for (const { cached, entry, pid } of attention) {
    if (!recentAttention(pid)) {
      host.notify({ action: OPEN_TWINS, id: `${ID}:check-result:${pid}`, kind: 'info', message: `${pairName(pid)}: ${attentionReason(entry, cached)}` })
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
  } else if (type === 'fleet') {
    toastFleet(payload)
  } else if (type === 'tasks') {
    toastTasks(payload)
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
// Type sizes: 13 px for body text, 12 px for secondary text, nothing smaller; 14 px for the first
// sentence a new owner reads, 15 px for a twin's name, 18-20 px for the page title and the welcome.
// Colours: words are never fainter than the secondary text colour. The tertiary one is 3.5 to 1 on a light
// window (measured: 12 px notes and the quiet text buttons), under the 4.5 to 1 that small text needs; it is
// kept for shapes only (icons, 3 to 1). Quiet text is told apart by its size, not by fading.

const LEAD = 'text-sm leading-relaxed text-(--ui-text-secondary)'
const BODY = 'text-[0.8125rem] leading-relaxed text-(--ui-text-secondary)'
const SMALL = 'text-xs leading-relaxed text-(--ui-text-secondary)'
const HEADING = 'text-[0.8125rem] font-semibold text-(--ui-text-primary)'
const CARD = 'rounded-lg border border-(--ui-stroke-tertiary) px-4 py-3'
const BOX = 'flex flex-col gap-2 rounded-md border border-(--ui-stroke-secondary) p-3'
const NOTE = 'rounded-lg bg-(--ui-bg-quinary) px-3 py-2.5 text-xs leading-relaxed text-(--ui-text-secondary)'
const PRE = 'overflow-auto whitespace-pre-wrap break-words rounded-md bg-(--ui-bg-quinary) px-2.5 py-1.5 font-mono text-xs leading-[1.45] text-(--ui-text-secondary)'

// Keyboard focus that can be seen: the Desktop's own buttons mark it with a border colour only. One rule, in
// the page's own style element (a plugin page can only use utility classes the Desktop already compiled, and
// none of those draws an outline). `gbt` is on the page and on every sheet and dialog it opens.
// (A field that is typed into shows it the same way: the link code, a pasted text, a computer's name.)
const FOCUS_CSS = '.gbt button:focus-visible,.gbt [role="switch"]:focus-visible,.gbt a:focus-visible,.gbt [tabindex="0"]:focus-visible,.gbt input:focus-visible,.gbt textarea:focus-visible{outline:2px solid var(--ui-text-secondary,Highlight);outline-offset:2px}'

// The Desktop's "text" button (Privacy, Try now, Don't, New code …) rests in its faintest text colour: 3.5 to 1
// on a light window, under the 4.5 to 1 that 12 px words need. On this page it rests in the secondary colour
// (6 to 1 or better, light and dark) and still darkens and underlines under the pointer, as the Desktop has it.
const QUIET_CSS = '.gbt button[data-variant="text"]:not(:hover){color:var(--ui-text-secondary)}'
// The Desktop draws a sheet's Close (the X in its corner) 24 px square; on this page's sheets and dialogs it
// is 28 px, the height of every other control. (`gbt` is on the sheet's own shell, whose child the X is.)
const TARGET_CSS = '.gbt[data-slot="dialog-content"]>button,.gbt [data-slot="dialog-close-button"]{width:1.75rem;height:1.75rem}'
// Lines stay readable on a wide window: a paragraph or a banner's sentence is at most about 80 characters long
// (60 widths of "0" in the page's own type: the letters of a sentence are narrower than that digit, and measured
// in Hermes Desktop a literal 80ch still gave lines of 102 to 105 characters, 60ch gives 71 to 83). Only the
// text is held back: its box, a button beside it and anything centred stay where they are. A narrow window is
// narrower than this already, so nothing changes there.
const MEASURE_CSS = '.gbt p:not(.flex-1):not(.text-center):not(.sr-only),.gbt .gbt-line{max-width:60ch}'
// The page's own style element: the rules no utility class of the Desktop can give.
const PAGE_CSS = FOCUS_CSS + QUIET_CSS + TARGET_CSS + MEASURE_CSS

// Someone who asked their computer for less motion gets none from the page either: the Desktop's stylesheet
// already stills every animation and transition then; a scroll the page starts itself is the one thing left.
const lessMotion = () => typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches

// The sentence under a sheet's title is something the owner reads before acting: the Desktop draws it in its
// faintest text colour (3.5 to 1 on a light window); here it is in the secondary one.
const SAID_UNDER_TITLE = 'text-(--ui-text-secondary)'

// A children prop as a flat list, so it can be spread next to siblings.
const many = c => (Array.isArray(c) ? c : [c])

function Icon({ name, className, size = '0.85rem' }) {
  return jsx(Codicon, { className, name, size })
}

// A twin's state as a shape, never as a colour alone. The sentence next to it says the same in words.
const TONE_ICON = { bad: 'error', busy: 'sync', good: 'pass', muted: 'circle-slash', send: 'arrow-up', todo: 'color-mode', warn: 'warning' }

function StateIcon({ tone }) {
  return el(
    'span',
    { 'aria-hidden': true, className: 'inline-flex shrink-0 items-center', 'data-tone': tone },
    jsx(Icon, { className: tone === 'good' ? 'text-(--ui-text-tertiary)' : 'text-(--ui-text-primary)', name: TONE_ICON[tone] ?? TONE_ICON.muted })
  )
}

// An address the page could not open by itself: shown, with a Copy button.
function AddressLine({ address }) {
  return el(
    'div',
    { className: 'flex min-w-0 items-center gap-1 rounded-md bg-(--ui-bg-quaternary) py-0.5 pl-2.5 pr-0.5' },
    el('span', { className: 'min-w-0 flex-1 truncate text-xs text-(--ui-text-primary)' }, address),
    jsx(CopyButton, { appearance: 'icon', buttonSize: 'icon-xs', label: 'Copy the address', text: address })
  )
}

function Avatar({ name }) {
  const initial = Array.from(clean(name, 60) || '?')[0].toUpperCase()

  return el(
    'span',
    {
      'aria-hidden': true,
      className:
        'grid size-9 shrink-0 place-items-center rounded-md bg-(--ui-bg-quaternary) text-sm font-semibold text-(--ui-text-secondary)'
    },
    initial
  )
}

function Tag({ children }) {
  return el('span', { className: 'shrink-0 rounded-[3px] bg-(--ui-bg-quaternary) px-1.5 py-px text-xs text-(--ui-text-secondary)' }, children)
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
    el('code', { className: 'min-w-0 flex-1 truncate font-mono text-xs text-(--ui-text-primary)', title: command }, command),
    // (a full-size target: 32 px, like the row menus)
    jsx(CopyButton, { appearance: 'icon', buttonSize: 'icon-sm', label: label ?? 'Copy', text: command })
  )
}

// A switch is 20 px high as the Desktop draws it. The label around it takes the press too: 28 px high, the
// height of every button on the page. (Its spoken name stays the switch's own.)
function SwitchTarget({ htmlFor, children }) {
  return el('label', { className: 'inline-flex min-h-7 shrink-0 cursor-pointer items-center', htmlFor }, children)
}

// A quiet "show more" line with its content folded under it.
function Fold({ label, openLabel, children, initialOpen = false }) {
  const [open, setOpen] = useState(initialOpen)

  return el(
    'div',
    { className: 'flex flex-col gap-1.5' },
    el(
      Button,
      { 'aria-expanded': open, className: 'self-start', onClick: () => setOpen(!open), variant: 'ghost' },
      jsx(DisclosureCaret, { open }),
      open ? (openLabel ?? label) : label
    ),
    open && children
  )
}

// What happened, what to do, one button; technical text stays folded.
function InlineProblem({ error, onRetry, retryLabel = 'Try again' }) {
  const e = asProblem(error)

  return el(
    'div',
    { className: cn(NOTE, 'flex flex-col gap-1.5'), role: 'alert' },
    el(
      'div',
      { className: 'flex items-start gap-2' },
      jsx(Icon, { className: 'mt-0.5 text-(--ui-text-tertiary)', name: 'warning' }),
      el('div', { className: 'flex min-w-0 flex-1 flex-col' }, el('span', { className: 'text-(--ui-text-primary)' }, e.message), e.hint && el('span', {}, e.hint)),
      onRetry && el(Button, { onClick: onRetry, variant: 'secondary' }, retryLabel)
    ),
    e.detail && el(Fold, { label: 'Technical details' }, el('pre', { className: cn(PRE, 'max-h-24') }, e.detail))
  )
}

// Escape, Cancel or Done: the keyboard goes back to what opened the sheet or dialog. (The Desktop's own
// dialog hands the focus back only to a trigger it was given; these are opened from state, so the page
// remembers the opener itself. Nothing is looked up: only the element that had the focus is kept.)
function useReturnFocus(open) {
  useEffect(() => {
    if (!open || typeof document === 'undefined') {
      return undefined
    }

    const active = document.activeElement
    // A menu entry is gone by the time the dialog closes: the button of the menu it sat in is what opened it.
    const menuButton = active?.closest?.('[role="menu"]')?.getAttribute?.('aria-labelledby')
    const opener = (menuButton && document.getElementById(menuButton)) || active

    return () => {
      // only when the focus was left nowhere: to the opener while opener.isConnected, and to the page's
      // title when the opener went with the act (a twin that was stopped, a computer that was unlinked)
      const back = () => landFocus(opener)

      if (timers) {
        timers.setTimeout(back, 0)
      } else {
        back()
      }
    }
  }, [open])
}

// A confirmation: the page's own, so that the keyboard is safe. It opens with the focus on Cancel, and a
// key presses only the button that has the focus (the Desktop's ready-made confirmation carries out the
// act on Enter or Space whatever is focused, Cancel and folds included). While the act runs both buttons
// wait and the dialog cannot be closed; a failure is said inside it and nothing closes.
function Confirm({ open, onClose, onConfirm, title, description, children, confirmLabel, busyLabel, cancelLabel = 'Cancel', destructive = false }) {
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState('')
  const cancel = useRef(null)

  useReturnFocus(open)

  async function confirm() {
    if (busy) {
      return
    }

    setBusy(true)
    setProblem('')

    try {
      await onConfirm()
      onClose()
    } catch (err) {
      setProblem(err instanceof TwinError ? [err.message, err.hint].filter(Boolean).join(' ') : err instanceof Error && err.message ? clean(err.message, 300) : FALLBACK_PROBLEM)
    } finally {
      setBusy(false)
    }
  }

  return el(
    Dialog,
    { onOpenChange: v => !v && !busy && onClose(), open },
    el(
      DialogContent,
      {
        className: 'gbt max-w-md',
        onOpenAutoFocus: e => {
          e.preventDefault()
          cancel.current?.focus()
        },
        role: 'alertdialog'
      },
      el(DialogHeader, {}, el(DialogTitle, {}, title), description && el(DialogDescription, { className: SAID_UNDER_TITLE }, description)),
      ...many(children ?? []),
      problem && el('p', { className: cn(NOTE, 'text-(--ui-text-primary)'), role: 'alert' }, problem),
      el(
        DialogFooter,
        {},
        el(Button, { disabled: busy, onClick: onClose, ref: cancel, type: 'button', variant: 'ghost' }, cancelLabel),
        el(Button, { disabled: busy, loading: busy, onClick: () => void confirm(), type: 'button', variant: destructive ? 'destructive' : 'default' }, busy && busyLabel ? busyLabel : confirmLabel)
      )
    )
  )
}

// Every sheet has the same frame: a plain title, one sentence, the content, its buttons.
function Sheet({ open, onClose, title, description, children, footer, wide = false, locked = false }) {
  useReturnFocus(open)
  // Every sheet ends in a button: Done, unless the sheet brings its own.
  const buttons = footer === undefined ? [el(Button, { disabled: locked, key: 'done', onClick: onClose }, 'Done')] : footer

  return el(
    Dialog,
    { onOpenChange: v => !v && !locked && onClose(), open },
    el(
      DialogContent,
      { className: wide ? 'gbt max-w-xl' : 'gbt max-w-lg' },
      el(DialogHeader, {}, el(DialogTitle, {}, title), description && el(DialogDescription, { className: SAID_UNDER_TITLE }, description)),
      ...many(children),
      buttons && el(DialogFooter, {}, ...many(buttons))
    )
  )
}

// "Stays" and "Removed", two short lists: the same shape for every way of leaving.
function StaysGoes({ stays, goes }) {
  const list = (label, lines) =>
    lines.length > 0 &&
    el(
      'div',
      { className: 'flex flex-col gap-0.5' },
      el('span', { className: HEADING }, label),
      el('ul', { className: cn(BODY, 'flex flex-col') }, lines.map((line, i) => el('li', { key: i }, clean(line, 200))))
    )

  return el('div', { className: 'flex flex-col gap-2.5' }, list('Stays', stays), list('Removed', goes))
}

// ── two versions of one thing ───────────────────────────────────────────────

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
      'aria-label': 'Line-by-line changes',
      // (a name needs a role to be read out; the box scrolls, so the keyboard can stop on it)
      role: 'group',
      className:
        'max-h-56 overflow-auto rounded-md border border-(--ui-stroke-tertiary) py-1.5 font-mono text-xs leading-[1.45] text-(--ui-text-secondary)',
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

// Changed words stand on the same ground the line-by-line view uses for an added line.
const MARK = 'rounded-[2px] bg-(--ui-diff-add-background) px-0.5 font-semibold text-(--ui-diff-add-foreground)'
const HEADING_LINE = /^#{1,6}\s+/

// One version, in the page's own type (a skill is prose, not code). A "# Heading" line is shown as a
// heading without its marks; the line-by-line view keeps the text exactly as written.
function VersionText({ side, marked }) {
  const text = side?.text

  if (text === null || text === undefined) {
    return el('div', { className: cn(NOTE, 'italic') }, 'This one is not text, so it cannot be shown here.')
  }

  const rows = marked ?? String(text).slice(0, 20_000).split('\n').map(t => [{ changed: false, text: t }])

  return el(
    'div',
    { className: cn(NOTE, 'max-h-40 overflow-auto whitespace-pre-wrap break-words text-[0.8125rem]'), tabIndex: 0 },
    rows.map((segments, i) => {
      const heading = HEADING_LINE.test(segments[0]?.text ?? '')
      const parts = heading ? [{ ...segments[0], text: segments[0].text.replace(HEADING_LINE, '') }, ...segments.slice(1)] : segments

      return el(
        'div',
        { className: heading ? 'font-semibold text-(--ui-text-primary)' : undefined, key: i },
        parts.length === 1 && !parts[0].text ? ' ' : parts.map((seg, k) => (seg.changed ? el('mark', { className: MARK, key: k }, seg.text) : seg.text))
      )
    })
  )
}

// `consequence`: the whole sentence (the result toast and the button's hover text). `short`: what the card says.
const KEEP = {
  hermes: {
    button: 'Keep the Hermes version',
    consequence: 'Grok still has its own version. You can send this one to Grok afterwards.',
    // (when the kept text is on the clipboard and the page says where to paste it: a thing to do, not a maybe)
    carried: 'Grok still has its own version until you paste this one there.',
    short: 'Grok keeps its own version.',
    title: 'Hermes version'
  },
  grok: {
    button: 'Keep the Grok version',
    consequence: 'Replaces the one in Hermes. You can undo this under Earlier versions.',
    short: 'Replaces the Hermes one. To undo, see',
    title: 'Grok version'
  }
}

// Only the lines that differ are shown, with "…" where the same lines were left out (every line is still
// there, under "Show line-by-line changes"). A text of one line, or one with no marked difference, is shown whole.
function onlyChanged(rows) {
  if (!Array.isArray(rows) || rows.length < 2 || !rows.some(r => r.some(g => g.changed))) {
    return rows
  }

  const out = []
  let skipped = false

  for (const row of rows) {
    // a heading stays: it says which part of the text the lines belong to
    if (row.some(g => g.changed) || HEADING_LINE.test(row[0]?.text ?? '')) {
      out.push(row)
      skipped = false
    } else if (!skipped) {
      out.push([{ changed: false, text: '… unchanged lines' }])
      skipped = true
    }
  }

  return out
}

// One thing that changed in both places: two cards, each one a choice that says what happens.
// `onVersions` opens the list of earlier versions (the words "Earlier versions" are the link).
function ConflictItem({ pairId, conflict, onVersions, lead = false, initialShowDiff = false, carry = false }) {
  const [busy, run] = useBusy()
  const [showDiff, setShowDiff] = useState(initialShowDiff)
  const hermesText = conflict?.hermes?.text
  const grokText = conflict?.grok?.text
  const bothText = typeof hermesText === 'string' && typeof grokText === 'string'
  const marked = useMemo(
    () => (bothText ? markDifferences(hermesText.slice(0, 20_000), grokText.slice(0, 20_000)) : [null, null]),
    [bothText, hermesText, grokText]
  )
  const hasDiff = diffBodyLines(conflict?.diff).length > 0
  const kind = kindLabel(conflict?.kind)
  const named = clean(conflict?.name, 60) && clean(conflict?.name, 60) !== kind
  const what = named ? `the ${kind} "${clean(conflict.name, 60)}"` : `the ${kind}`
  const since = ago(conflict?.since ?? conflict?.hermes?.changed_at)

  async function undo(pointId) {
    try {
      await post(pairPath(pairId, 'restore'), { point_id: pointId })
      host.notify({ id: `${ID}:resolved:${pairId}`, kind: 'success', message: `Put back the Hermes version of ${what}` })
      refreshAll()
    } catch (err) {
      notifyProblem(err, 'Could not undo that')
    }
  }

  async function keep(side) {
    const res = await run(
      side,
      async () => {
        const out = await post(pairPath(pairId, 'resolve'), { conflict_id: conflict.conflict_id, keep: side })

        // the kept text goes on the clipboard in the same click, when the owner carries it into Grok by hand
        if (side === 'hermes' && carry) {
          await handOver(pairId, conflict.conflict_id, conflict?.text_truncated ? null : hermesText)
        }

        return out
      },
      'Could not keep that version'
    )

    if (res) {
      const point = side === 'grok' && typeof res.restore_point === 'string' ? res.restore_point : null

      host.notify({
        action: point ? { label: 'Undo', onClick: () => void undo(point) } : undefined,
        // (a notice that carries Undo stays long enough to be read and reached)
        durationMs: point ? WARNING_TOAST_MS : undefined,
        id: `${ID}:resolved:${pairId}`,
        kind: 'success',
        message: `Kept the ${side === 'hermes' ? 'Hermes' : 'Grok'} version of ${what}`,
        meta: side === 'hermes' ? (carry ? KEEP.hermes.carried : KEEP.hermes.consequence) : 'The Hermes version it replaced is kept under Earlier versions.'
      })
      refreshAll()
    }
  }

  const card = (side, sideData, marks) =>
    el(
      'div',
      { className: 'flex min-w-0 flex-col gap-2 rounded-md border border-(--ui-stroke-tertiary) p-2.5' },
      el('span', { className: HEADING }, KEEP[side].title),
      jsx(VersionText, { marked: onlyChanged(marks), side: sideData }),
      side === 'grok'
        ? el(
            'div',
            { className: cn(SMALL, 'flex flex-wrap items-center gap-x-1') },
            // (two lines by design: the button never ends up alone on a line of its own)
            el('span', { className: 'basis-full' }, KEEP.grok.short.replace(/ To undo, see$/, '')),
            el('span', {}, onVersions ? 'To undo, see' : 'To undo, see Earlier versions.'),
            // a full-size target, not a word inside a sentence
            onVersions && el(Button, { onClick: onVersions, variant: 'text' }, 'Earlier versions')
          )
        : el('p', { className: SMALL }, KEEP.hermes.short),
      // The page's one main button, when this is the matter it leads with, is the choice that replaces nothing
      // (the Hermes version stays; Grok keeps its own). The whole consequence is the button's hover text.
      el(Button, { className: 'max-w-full self-start whitespace-normal text-left', disabled: Boolean(busy), loading: busy === side, onClick: () => void keep(side), title: KEEP[side].consequence, variant: lead && side === 'hermes' ? 'default' : 'outline' }, KEEP[side].button)
    )

  return el(
    'div',
    { className: 'flex flex-col gap-2.5' },
    el(
      'div',
      { className: 'flex flex-col' },
      // (the two buttons below are the question: no sentence asks it again)
      el('h3', { className: HEADING }, `${capital(what)} changed in both places.`),
      since && el('span', { className: SMALL, title: 'Changed words are highlighted.' }, `Different since ${since}.`)
    ),
    el('div', { className: 'grid gap-3 sm:grid-cols-2' }, card('hermes', conflict?.hermes, marked[0]), card('grok', conflict?.grok, marked[1])),
    conflict?.text_truncated && el('p', { className: SMALL }, 'Long text: only the first part of each version is shown.'),
    hasDiff &&
      el(
        'div',
        { className: 'flex flex-col gap-1.5' },
        el(
          Button,
          { 'aria-expanded': showDiff, className: 'self-start', onClick: () => setShowDiff(!showDiff), variant: 'ghost' },
          jsx(DisclosureCaret, { open: showDiff }),
          showDiff ? 'Hide line-by-line changes' : 'Show line-by-line changes'
        ),
        showDiff && jsx(DiffView, { diff: conflict?.diff })
      )
  )
}

function ConflictBox({ pair, onVersions, lead = false }) {
  const q = useQuery({
    queryFn: () => api(pairPath(pair.pair_id, 'conflicts')),
    queryKey: [ID, 'conflicts', pair.pair_id, pair.conflicts],
    // an earlier answer for the same key may be "none": always ask again when the box appears, and keep asking
    // while the answer is empty (the change may be a moment ahead of this route; the status refresh removes the
    // box when there really is nothing left to choose)
    refetchInterval: query => (Array.isArray(query?.state?.data?.conflicts) && query.state.data.conflicts.length === 0 ? 1_500 : false),
    refetchOnMount: 'always'
  })
  const [mountedAt] = useState(() => Date.now())
  const conflicts = Array.isArray(q.data?.conflicts) ? q.data.conflicts : []
  const settled = conflicts.length === 0 && !q.isFetching && !(Number(q.dataUpdatedAt) < mountedAt)

  useEffect(() => {
    if (settled) {
      void queryClient.invalidateQueries({ queryKey: STATUS_KEY })
    }
  }, [settled])

  return el(
    'section',
    { 'aria-label': 'Changed in both places', className: BOX },
    q.isPending
      ? jsx(LinesSkeleton, { rows: 4 })
      : q.isError
        ? jsx(InlineProblem, { error: q.error, onRetry: () => void q.refetch() })
        : conflicts.length === 0
          ? el('p', { className: SMALL }, 'Loading both versions…')
          : conflicts.map((c, i) =>
              el(
                'div',
                { className: cn(i > 0 && 'border-t border-(--ui-stroke-tertiary) pt-3'), key: c?.conflict_id ?? i },
                jsx(ConflictItem, { carry: !isSkillItem(c?.conflict_id) || !skillSyncOn(pair), conflict: c, lead: lead && i === 0, onVersions, pairId: pair.pair_id })
              )
            )
  )
}

// ── attention boxes: what happened, what to do, one action ──────────────────
// A row shows its first open matter as a box and the others as one-line headers that open on a click.
// `lead` is true for the one box on the page whose action may be the primary button.

const ICON_OF = { conflict: 'git-compare', error: 'warning', missing: 'question', secret: 'key', unsent: 'cloud-upload', waiting: 'cloud-upload' }
const mainVariant = lead => (lead ? 'default' : 'secondary')

function Matter({ label, icon, title, children, actions }) {
  return el(
    'section',
    { 'aria-label': label, className: BOX },
    el(
      'div',
      { className: 'flex items-start gap-2' },
      jsx(Icon, { className: 'mt-0.5 shrink-0 text-(--ui-text-secondary)', name: icon }),
      el('div', { className: 'flex min-w-0 flex-1 flex-col gap-0.5' }, el('h3', { className: HEADING }, title), ...many(children))
    ),
    actions && el('div', { className: 'flex flex-wrap items-center gap-2 pl-6' }, ...many(actions))
  )
}

// A matter that is waiting its turn: one line, opens on a click.
function MatterHeader({ matter, onOpen }) {
  return el(
    Button,
    { 'aria-label': `Show: ${matter.title}`, className: 'h-auto w-full min-w-0 justify-start gap-2 px-3 py-2 text-left text-[0.8125rem] font-normal', onClick: onOpen, variant: 'outline' },
    jsx(Icon, { className: 'shrink-0 text-(--ui-text-secondary)', name: ICON_OF[matter.kind] ?? 'circle-outline' }),
    el('span', { className: 'min-w-0 flex-1 truncate' }, matter.title),
    jsx(Icon, { className: 'shrink-0 text-(--ui-text-tertiary)', name: 'chevron-right' })
  )
}

// One twin cannot be checked (the others can). The reason, then the one thing to do, in the backend's words.
function ErrorBox({ pair, lead }) {
  const [busy, run] = useBusy()
  const name = nameOf(pair)
  const sentence = text => (text ? `${capital(text)}${/[.!?…]$/.test(text) ? '' : '.'}` : '')
  const reason = sentence(plainText(pair?.error?.message ?? '', 200))
  const hint = sentence(plainText(pair?.error?.hint ?? '', 200))

  return el(
    Matter,
    {
      actions: el(
        Button,
        { loading: busy === 'check', onClick: () => void run('check', () => checkAndToast({ pair_id: pair.pair_id }), `Could not check ${name}`), variant: mainVariant(lead) },
        'Try again'
      ),
      icon: ICON_OF.error,
      label: "Can't check right now",
      title: `Can't check ${name} right now`
    },
    el('p', { className: BODY }, [reason, hint || 'Your twin and your Grok bot are untouched.'].filter(Boolean).join(' '))
  )
}

// The bot is gone from the Grok app: one sentence, one decision, and a quiet way to wait.
function MissingBox({ pair, onStop, onWait, lead = false }) {
  const name = nameOf(pair)

  return el(
    Matter,
    {
      actions: [
        el(Button, { key: 'stop', onClick: onStop, variant: mainVariant(lead) }, 'OK, stop following'),
        el(Button, { key: 'wait', onClick: onWait, variant: 'ghost' }, 'It will be back')
      ],
      icon: ICON_OF.missing,
      label: 'Not in Grok any more',
      title: `${name} is not in your Grok app any more.`
    },
    el('p', { className: BODY }, 'Its twin stays here as an ordinary Hermes bot. Nothing was deleted.')
  )
}

// The sample's fake password is taken out by its own button, right in the box.
async function removeFakePassword(pair) {
  try {
    await post('/demo', { action: 'remove-secret' })
  } catch (err) {
    // an older backend knows this as part of "change the bot in Grok"
    if (!(err instanceof TwinError) || err.code === 'unreachable') {
      throw err
    }

    await post('/demo', { action: 'grok-edit' })
  }

  refreshAll()
  await pause(1_500)
  await checkAndToast({ pair_id: pair.pair_id }, { quietWhenNothing: true })
}

function SecretBox({ pair, place, lead }) {
  const [busy, run] = useBusy()
  const name = nameOf(pair)
  const sample = isSample(pair)
  const parts = place.item ? itemParts(place.item) : { kind: 'text', name: '' }
  const where = parts.name ? `${parts.kind} "${parts.name}"` : parts.kind
  const toGrok = place.direction === 'h2g'
  // "line 3 of the description" when the backend says which line; else the part alone
  const spot = Array.isArray(place.lines) && place.lines.length > 0 ? `${lineWords(place.lines)} of the ${where}` : `the ${where}`
  const check = el(
    Button,
    { disabled: Boolean(busy), key: 'check', loading: busy === 'check', onClick: () => void run('check', () => checkAndToast({ pair_id: pair.pair_id }, { quietWhenNothing: true }), `Could not check ${name}`), variant: 'text' },
    'Check now'
  )

  return el(
    Matter,
    {
      actions: sample
        ? [
            el(
              Button,
              { disabled: Boolean(busy), key: 'fake', loading: busy === 'fake', onClick: () => void run('fake', () => removeFakePassword(pair), 'Could not take the fake password out'), variant: mainVariant(lead) },
              'Remove the fake password'
            ),
            check
          ]
        : check,
      icon: ICON_OF.secret,
      label: 'A password or key was not copied',
      title: `A password or key in ${owned(name)} ${where} was not ${toGrok ? 'sent to Grok' : 'copied'}.`
    },
    el(
      'p',
      { className: BODY },
      sample ? 'This one is fake. With a real bot you would remove it in the Grok app, and Hermes would notice by itself.' : `Remove it from ${spot} in ${toGrok ? 'Hermes' : 'the Grok app'}. Hermes notices by itself.`
    ),
    el(Fold, { label: 'Why?' }, el('p', { className: SMALL }, "Hermes never stores or shows it. This check can miss things, so keep passwords out of a bot's text."))
  )
}

// When something sent to Grok gets there: the sync bot's routine picks it up at its next run.
function arrivesText(pair) {
  const every = Math.floor(Number(pair?.mirror_bot?.every_minutes) || 0)

  return every > 0
    ? `It arrives when the sync bot's routine next runs in Grok, usually within ${plural(every, 'minute')}.`
    : "It arrives the next time the sync bot's routine runs in Grok."
}

async function sendToGrok(pair) {
  const res = await post(pairPath(pair.pair_id, 'send-to-grok'))

  // One toast for the send itself; anything else the send turned up (a possible password, a change in
  // both places) still gets its own.
  toastCheckResult({ pairs: (Array.isArray(res?.pairs) ? res.pairs : []).map(p => ({ ...p, applied: [] })) }, { quietWhenNothing: true })
  host.notify({
    id: `${ID}:send:${pair.pair_id}`,
    kind: 'success',
    message: 'Sent to Grok',
    meta: isSample(pair) ? "The sample's pretend Grok took it at once." : arrivesText(pair)
  })
  refreshAll()
}

async function skipItem(pair, item, undo = false) {
  await post(pairPath(pair.pair_id, 'skip'), { item, undo })
  refreshAll()
}

// "Done, I pasted it": this version is in Grok now, by the owner's own hand. A backend that does not
// know that yet files it as "kept in Hermes only", which also stops the reminder.
async function markCopied(pair, item) {
  try {
    await post(pairPath(pair.pair_id, 'skip'), { copied: true, item })
  } catch (err) {
    if (!(err instanceof TwinError) || !['bad_input', 'unreachable'].includes(err.code)) {
      throw err
    }

    await post(pairPath(pair.pair_id, 'skip'), { item })
  }

  refreshAll()
}

// Grok already has this text (or the item is not waiting any more): the backend's "not waiting to be copied"
// answer to "Done, I pasted it". Nothing went wrong, so it is not shown as a failure.
function settledAlready(err) {
  return err instanceof TwinError && ['usage', 'not_found', 'not_waiting'].includes(err.code) && /not waiting/i.test(err.message)
}

// "Keep the Hermes version" also puts the kept text on the clipboard, in the same click: it is what the owner
// carries into Grok next. The box that follows then opens at "Copied. In the Grok app…" (Copy again is there),
// so the chore is keep, go to Grok, paste. Only when the clipboard really took it; otherwise the box is as
// before, with its own Copy button. Remembered per twin for three minutes, for the one item that was kept.
const handed = new Map() // pair id -> { at, key, text }
const HANDED_MS = 3 * 60_000

function itemKey(item) {
  const p = itemParts(item)

  return `${p.kind}:${p.name}`
}

// `shown`: the text on the page (a name or description), used when the backend's door hands out skills only.
// A possible password is never worked around.
async function handOver(pairId, item, shown = null) {
  let got = ''

  try {
    const res = await api(`${pairPath(pairId, 'skill-text')}?item=${encodeURIComponent(item)}`)

    got = typeof res?.text === 'string' ? res.text.slice(0, 200_000) : ''
  } catch (err) {
    if (asProblem(err).code === 'looks_like_secret' || isSkillItem(item)) {
      return false
    }
  }

  got = got || (isSkillItem(item) ? '' : clean(shown, 4000))

  if (!got || !(await copyText(got))) {
    return false
  }

  handed.set(pairId, { at: Date.now(), key: itemKey(item), text: got })

  return true
}

function handedText(pair, item) {
  const h = handed.get(pair?.pair_id)

  if (!h || Date.now() - h.at > HANDED_MS || h.key !== itemKey(item)) {
    return null
  }

  return h.text
}

// While a change waits to be pasted into Grok the page looks every 5 s (and not every 30) whether Grok has it:
// the box then leaves by itself, with no "Done" to press. Only while such a box is on screen and the page is
// visible; one timer however many boxes.
let grokWatchers = 0
let stopGrokWatch = null

function useWatchGrok() {
  useEffect(() => {
    if (typeof timers?.setInterval !== 'function') {
      return undefined
    }

    grokWatchers += 1

    if (grokWatchers === 1) {
      stopGrokWatch = timers.setInterval(() => {
        if (typeof document === 'undefined' || document.visibilityState !== 'hidden') {
          void queryClient.invalidateQueries({ queryKey: STATUS_KEY })
        }
      }, 5_000)
    }

    return () => {
      grokWatchers -= 1

      if (grokWatchers === 0 && typeof stopGrokWatch === 'function') {
        stopGrokWatch()
        stopGrokWatch = null
      }
    }
  }, [])
}

// One skill the owner carries into Grok by hand: its text on the clipboard, then where to paste it.
// Nothing is claimed that did not happen: "Copied." only when the clipboard took it; otherwise the text
// is shown with its own Copy button. `initialText` lets the smoke test render the state after the copy.
// The words for one change carried into Grok by hand, from what the change is: a skill is pasted into that
// skill, a description into the bot's description. The sentence that says where to paste names the real place.
function byHandWords(pair, item) {
  const name = nameOf(pair)
  const parts = itemParts(item)

  if (isSkillItem(item)) {
    return {
      copy: "Copy the skill's text",
      first: 'Grok does not have this change yet. Copy the text, then paste it into the skill in the Grok app.',
      local: null,
      noun: 'skill',
      where: parts.name ? `In the Grok app, open ${owned(name)} skill "${parts.name}" and paste it over the old text.` : `In the Grok app, open ${name} and paste it over the old text.`
    }
  }

  if (parts.kind === 'description' || parts.kind === 'name') {
    const local = clean(parts.kind === 'name' ? pair?.hermes?.name : pair?.hermes?.description, 4000)

    return {
      copy: `Copy the ${parts.kind}`,
      first: `Grok does not have this change yet. Copy the ${parts.kind}, then paste it into the bot's ${parts.kind} in the Grok app.`,
      local,
      noun: parts.kind,
      where: `In the Grok app, open ${owned(name)} settings and paste it over the ${parts.kind}.`
    }
  }

  return {
    copy: 'Copy the text',
    first: `Grok does not have this change yet. Copy the text, then paste it over ${itemPhrase(item)} in the Grok app.`,
    local: null,
    noun: parts.kind,
    where: `In the Grok app, open ${name} and paste it over ${itemPhrase(item)}.`
  }
}

// `brief`: one of several in a box that has already said what to do; only its own button and result show.
function SkillByHand({ pair, item, lead, onSkillSync, brief = false, initialText = null, initialCopied = false, initialProblem = null }) {
  const [busy, run] = useBusy()
  // (already on the clipboard when "Keep the Hermes version" put it there a moment ago: handOver)
  const [given] = useState(() => (initialText === null ? handedText(pair, item) : null))
  const [text, setText] = useState(initialText ?? given)
  const [copied, setCopied] = useState(initialCopied || given !== null)
  const [problem, setProblem] = useState(initialProblem)
  const name = nameOf(pair)
  const phrase = itemPhrase(item)
  const words = byHandWords(pair, item)

  useWatchGrok()
  const main = mainVariant(lead)

  async function copy() {
    setProblem(null)

    await run('copy', async () => {
      let got = text

      if (got === null) {
        try {
          const res = await api(`${pairPath(pair.pair_id, 'skill-text')}?item=${encodeURIComponent(item)}`)

          got = typeof res?.text === 'string' ? res.text.slice(0, 200_000) : ''
        } catch (err) {
          const e = asProblem(err)

          // A name or description is on the page already (the twin's own, as Hermes has it): a backend that
          // hands out skills only does not stop the copy. A possible password still does.
          if (words.local && e.code !== 'looks_like_secret') {
            got = words.local
          } else {
            // said in place, next to the button; a backend without this door gets the honest way round
            setProblem(e.code === 'looks_like_secret' || e.code === 'not_found' ? e : new TwinError(`Hermes cannot fetch this ${words.noun}'s text yet.`, 'no_text', `Open ${name} in Hermes and copy the ${words.noun} from there.`))

            return
          }
        }
      }

      if (!got) {
        setProblem(new TwinError(`This ${words.noun} has no text to copy.`, 'no_text', `Open ${name} in Hermes to look at it.`))

        return
      }

      // (both at once: the text's fold opens only when the clipboard refused, so a copy that worked shows one
      // sentence and one button, not the text and a second Copy)
      const took = await copyText(got)

      setCopied(took)
      setText(got)
    })
  }

  // "Done, I pasted it" is never answered with an error when nothing failed: the plugin often sees Grok's new
  // text before the owner is back to press it, and then there is nothing left to note. Either way the status
  // is read again at once, so the row goes calm within a second and not at the next timed look.
  async function done() {
    const ok = await run(
      'done',
      async () => {
        try {
          await markCopied(pair, item)

          return 'noted'
        } catch (err) {
          if (!settledAlready(err)) {
            throw err
          }

          return 'settled'
        } finally {
          handed.delete(pair.pair_id)
          refreshAll()
          void freshStatus()
        }
      },
      `Could not note that for ${itemLabel(item)}`
    )

    if (ok === 'noted') {
      host.notify({
        action: { label: 'Undo', onClick: () => void skipItem(pair, item, true).catch(err => notifyProblem(err, 'Could not undo that')) },
        durationMs: WARNING_TOAST_MS,
        id: `${ID}:skipped:${pair.pair_id}`,
        kind: 'info',
        message: `Noted: ${phrase} is in Grok too`,
        meta: 'Hermes asks again the next time you change it here.'
      })
    }
  }

  const where = words.where
  const has = text !== null

  return el(
    'div',
    { className: 'flex flex-col gap-2' },
    (has || !brief) && el('p', { className: BODY, role: has ? 'status' : undefined }, has ? `${copied ? 'Copied.' : 'Here is the text. Copy it.'} ${where}` : words.first),
    problem && el('p', { className: 'text-xs text-(--ui-text-primary)', role: 'alert' }, [problem.message, problem.hint].filter(Boolean).join(' ')),
    has &&
      el(
        Fold,
        { initialOpen: !copied, label: 'Show the text', openLabel: 'Hide the text' },
        el('pre', { className: cn(PRE, 'max-h-40 select-all'), tabIndex: 0 }, text),
        el('div', {}, jsx(CopyButton, { appearance: 'button', buttonVariant: 'secondary', label: 'Copy', showLabel: true, text }))
      ),
    el(
      'div',
      { className: 'flex flex-wrap items-center gap-2' },
      has
        ? el(Button, { 'aria-label': `Done, I pasted ${itemLabel(item)} into Grok`, disabled: Boolean(busy), key: 'done', loading: busy === 'done', onClick: () => void done(), variant: main }, 'Done, I pasted it')
        : el(Button, { 'aria-label': `Copy the text of ${itemLabel(item)}`, disabled: Boolean(busy), key: 'copy', loading: busy === 'copy', onClick: () => void copy(), variant: main }, words.copy),
      has
        ? copied && el(Button, { disabled: Boolean(busy), key: 'again', loading: busy === 'copy', onClick: () => void copy(), variant: 'ghost' }, 'Copy again')
        : el(Button, { disabled: Boolean(busy), key: 'done', loading: busy === 'done', onClick: () => void done(), variant: 'ghost' }, 'Done, I pasted it'),
      // (only skills can be sent for the owner: a name or description is always carried by hand)
      onSkillSync && isSkillItem(item) && el(Button, { disabled: Boolean(busy), key: 'sync', onClick: onSkillSync, title: 'Let Hermes send skill changes to Grok for you', variant: 'text' }, 'Skill sync…')
    )
  )
}

// Something is ready and waits for a go-ahead: a skill changed in Hermes (send it to Grok, or keep it
// in Hermes only; without skill sync, carry it over by hand), or something from Grok that is ready to
// be added to Hermes.
function WaitingBox({ pair, waiting, onSkillSync, lead }) {
  const [busy, run] = useBusy()
  const item = clean(waiting?.item, 120)
  const parts = itemParts(item)
  const phrase = itemPhrase(item)
  const Phrase = capital(phrase)
  const to = waiting?.to === 'grok' || waiting?.to === 'hermes' ? waiting.to : null
  const syncOn = skillSyncOn(pair)
  const main = mainVariant(lead)

  async function approve(thenSend) {
    const res = await run(
      'go',
      async () => {
        const out = await post(pairPath(pair.pair_id, 'approve'), { item })

        if (thenSend) {
          await sendToGrok(pair)
        }

        return out
      },
      `Could not go ahead with ${itemLabel(item)}`
    )

    if (res) {
      const sync = res.sync && typeof res.sync === 'object' ? res.sync : null

      toastCheckResult({ pairs: sync ? [{ pair_id: pair.pair_id, ...sync }] : [] }, { quietWhenNothing: true })

      if (!thenSend) {
        host.notify({ id: `${ID}:approved:${pair.pair_id}`, kind: 'success', message: to === 'hermes' ? `Added ${itemLabel(item)} to Hermes` : `Went ahead with ${itemLabel(item)}` })
      }

      refreshAll()
    }
  }

  async function skip(message) {
    const ok = await run('skip', () => skipItem(pair, item).then(() => true), `Could not set ${itemLabel(item)} aside`)

    if (ok) {
      host.notify({
        action: { label: 'Undo', onClick: () => void skipItem(pair, item, true).catch(err => notifyProblem(err, 'Could not undo that')) },
        durationMs: WARNING_TOAST_MS,
        id: `${ID}:skipped:${pair.pair_id}`,
        kind: 'info',
        message,
        meta: 'You can offer it again under Details… in the twin\'s "…" menu.'
      })
    }
  }

  const quiet = (label, message) => el(Button, { disabled: Boolean(busy), key: 'skip', loading: busy === 'skip', onClick: () => void skip(message), variant: 'ghost' }, label)

  if (to === 'grok' && (!syncOn || !isSkillItem(item))) {
    return el(Matter, { icon: ICON_OF.waiting, label: 'A change to copy into Grok', title: `You changed ${phrase} in Hermes.` }, jsx(SkillByHand, { item, lead, onSkillSync, pair }))
  }

  if (to === 'grok') {
    return el(
      Matter,
      {
        actions: [
          el(Button, { 'aria-label': `Send ${itemLabel(item)} to Grok`, disabled: Boolean(busy), key: 'go', loading: busy === 'go', onClick: () => void approve(true), variant: main }, 'Send to Grok'),
          quiet('Keep it in Hermes only', `${Phrase} stays in Hermes only`)
        ],
        icon: ICON_OF.waiting,
        label: 'Ready to send to Grok',
        title: `You changed ${phrase} in Hermes.`
      },
      el('p', { className: BODY }, 'Send it to Grok, or keep this change in Hermes only.')
    )
  }

  if (to === 'hermes') {
    return el(
      Matter,
      {
        actions: [
          el(Button, { 'aria-label': `Add ${itemLabel(item)} to Hermes`, disabled: Boolean(busy), key: 'go', loading: busy === 'go', onClick: () => void approve(false), variant: main }, 'Add to Hermes'),
          quiet('Not now', `${Phrase} was not added to Hermes`)
        ],
        icon: 'cloud-download',
        label: 'Ready to add to Hermes',
        title: `${Phrase} from Grok is ready to add to Hermes.`
      },
      el('p', { className: BODY }, parts.kind === 'routine' ? 'It arrives switched off, so nothing runs twice.' : 'Nothing changes until you say so.')
    )
  }

  // The engine did not say which way this goes: no guess, a neutral go-ahead.
  return el(
    Matter,
    {
      actions: [
        el(Button, { 'aria-label': `Go ahead with ${itemLabel(item)}`, disabled: Boolean(busy), key: 'go', loading: busy === 'go', onClick: () => void approve(false), variant: main }, 'Go ahead'),
        quiet('Not now', `${Phrase} was set aside`)
      ],
      icon: 'pass',
      label: 'Waiting for your go-ahead',
      title: `${Phrase} is waiting for your go-ahead.`
    },
    el('p', { className: BODY }, plainText(waiting?.detail, 200) || 'Nothing changes until you say so.')
  )
}

// Which changes `pending.to_grok` counts, when the backend names them.
const unsentItems = toGrokItems

// Skill changes that were given the go-ahead but have not left for Grok yet. With skill sync on they go
// with one button; without it the owner carries each one over, with its text to hand.
function UnsentBox({ pair, count, onSkillSync, lead }) {
  const [busy, run] = useBusy()
  const syncOn = skillSyncOn(pair)
  const name = nameOf(pair)
  const all = unsentItems(pair)
  // what skill sync cannot carry is the owner's to paste, with skill sync on or off
  const other = all.filter(i => !isSkillItem(i))
  const items = syncOn ? other : all
  const skills = Math.max(0, count - other.length)
  const sameKind = items.length > 1 && items.every(isSkillItem)
  const send = variant =>
    el(
      Button,
      { 'aria-label': `Send ${owned(name)} skill changes to Grok`, loading: busy === 'send', onClick: () => void run('send', () => sendToGrok(pair), `Could not send ${owned(name)} changes to Grok`), variant },
      'Send to Grok'
    )

  if (syncOn && other.length === 0) {
    return el(
      Matter,
      {
        actions: send(mainVariant(lead)),
        icon: ICON_OF.unsent,
        label: 'Skill changes for Grok',
        title: `${plural(count, 'skill change')} ${count === 1 ? 'is' : 'are'} ready for Grok.`
      },
      el('p', { className: BODY }, arrivesText(pair))
    )
  }

  return el(
    Matter,
    {
      icon: ICON_OF.unsent,
      label: other.length === 0 ? 'Skill changes to copy into Grok' : 'Changes to copy into Grok',
      // the title says what changed: a description is never counted as a skill
      title: other.length === 0 ? `${plural(count, 'skill')} changed in Hermes.` : items.length === 1 && count === 1 ? `You changed ${itemPhrase(items[0])} in Hermes.` : `${plural(count, 'thing')} changed in Hermes.`
    },
    items.length > 0
      ? [
          // several of one kind: what to do is said once, above them (each keeps its own Copy and Done)
          sameKind && el('p', { className: BODY, key: 'once' }, 'Grok does not have these changes yet. Copy each text, then paste it into that skill in the Grok app.'),
          ...items.map((item, i) =>
            el(
              'div',
              { className: cn('flex flex-col gap-1', i > 0 && 'border-t border-(--ui-stroke-tertiary) pt-2'), key: item },
              (items.length > 1 || count > 1) && el('span', { className: 'text-[0.8125rem] text-(--ui-text-primary)' }, capital(itemPhrase(item))),
              // "Skill sync…" once, on the first skill
              jsx(SkillByHand, { brief: sameKind, item, lead: lead && i === 0, onSkillSync: item === items.find(isSkillItem) ? onSkillSync : undefined, pair })
            )
          ),
          // skill sync is on: the skills among them still go with one button
          syncOn && skills > 0 && el('div', { className: 'flex flex-wrap items-center gap-2 border-t border-(--ui-stroke-tertiary) pt-2', key: 'send' }, el('span', { className: cn(BODY, 'min-w-0 flex-1') }, `${plural(skills, 'skill change')} ${skills === 1 ? 'is' : 'are'} ready for Grok.`), send('secondary'))
        ]
      : [
          // an older backend gives the number only: the names are under Details
          el('p', { className: BODY, key: 'p' }, `Grok does not have ${count === 1 ? 'this change' : 'these changes'} yet. Details… in the "…" menu names ${count === 1 ? 'the skill' : 'each skill'}; copy ${count === 1 ? 'it' : 'them'} into the Grok app, or set up skill sync.`),
          el('div', { key: 'b' }, el(Button, { onClick: onSkillSync, title: 'Let Hermes send skill changes to Grok for you', variant: 'secondary' }, 'Skill sync…'))
        ]
  )
}

// ── details: what copies over, earlier versions, recent activity ────────────
// One sheet with three sections, instead of four separate read-only sheets in the menu.

// Older engine values map onto the four groups.
const COVERAGE_ALIAS = { on_export: 'syncs_via_mirror', 'n/a': 'not_carried' }
const coverageValue = v => COVERAGE_ALIAS[v] ?? v

// What every twin does when the backend sent no list of its own (the product's fixed table).
const DEFAULT_COVERAGE = [
  { g2h: 'syncs', h2g: 'by_hand', item: 'name' },
  { g2h: 'syncs', h2g: 'by_hand', item: 'description' },
  { g2h: 'by_hand', h2g: 'by_hand', item: 'instructions' },
  { g2h: 'syncs_via_mirror', h2g: 'syncs_via_mirror', item: 'skills' },
  { g2h: 'by_hand', h2g: 'by_hand', item: 'routines' },
  { g2h: 'by_hand', h2g: 'by_hand', item: 'connectors' }
]

// The whole table: what copies over and how, for every twin. Its rows are not typed here: twin/copies.py holds
// them and writes them between the two marks below (a test compares the two), so the page, the README and the
// disclosure always say the same.
// what-copies:begin (written by twin/copies.py: change the table there)
const WHAT_COPIES = [
  {
    title: 'From Grok to Hermes',
    rows: [
      { what: 'Name and description', how: 'By itself, in seconds, while Hermes is open.' },
      { what: 'Instructions, routines and connections', how: "With Import setup export: the bot writes a file about itself when you ask it in Grok. Instructions are filled in, routines arrive switched off, and connections arrive as a list of names. Logins are never copied. With skill sync set up, instructions also follow Grok's every 15 minutes by themselves, unless you switch that off. When it is unsure which to keep, you are asked." },
      { what: 'Skills', how: 'With skill sync, which is optional: a sync bot in your own Grok account carries them. Without it you copy them yourself.' },
      { what: 'Conversations', how: 'Off until you switch full transcripts on for a twin. Then conversations from a Grok export can become Hermes chats of the twin; full transcripts are set up in the terminal.' },
      { what: 'Grok memory', how: 'Does not copy: memory is the most private thing a bot holds, so none is taken unless you switch on memory facts, which need skill sync.' },
      { what: 'Grok model setting', how: "Does not copy: nothing Hermes reads from Grok names the bot's model, so the twin uses the model you choose in Hermes." }
    ]
  },
  {
    title: 'From Hermes back to Grok',
    rows: [
      { what: 'Name, description and instructions', how: 'You make the same change in Grok yourself.' },
      { what: 'Skills', how: 'With skill sync, after you press Send to Grok: a skill Grok does not have yet is installed there. A skill Grok already has under that name is not replaced; paste the change into Grok yourself. Without skill sync you paste them all yourself.' },
      { what: 'Routines and connections', how: 'You change them in Grok yourself.' }
    ]
  },
  {
    title: 'Between your own computers',
    rows: [
      { what: 'Name and description', how: 'By itself, in seconds, once the other computer is linked and Hermes is open on both.' },
      { what: 'Skills', how: "Off until you switch it on. Then the text of each twin's skills follows the Mac with the Grok app. A skill changed on both computers asks you which one to keep." },
      { what: 'Routines', how: "Off until you switch it on. Then each twin's routines copy to every linked computer, and each routine runs on one computer that you choose, never on two." },
      { what: 'Memory', how: 'Off until you switch it on for a twin. Then it travels encrypted, between your linked computers only.' },
      { what: 'Settings', how: 'By itself, item by item: tools, preferences and instructions follow the Mac with the Grok app. Each computer keeps its own model unless you switch that on.' },
      { what: 'Instructions', how: "By itself, as one of the twin's settings. You can switch that off on each computer." },
      { what: 'Chats, files and logins', how: 'Not copied. A request you send to another computer and its answer are the only chat text that travels, and only with requests switched on.' }
    ]
  }
]
// what-copies:end

// One table, two columns, three headed parts. Laid out with inline styles: a plugin page can only use the
// utility classes the Desktop's own stylesheet happens to hold, and none of them is for a table.
const TABLE_CELL = { padding: '0.25rem 0.75rem 0.25rem 0', textAlign: 'left', verticalAlign: 'top' }

function WholeTable() {
  return el(
    'table',
    { className: BODY, style: { borderCollapse: 'collapse', width: '100%' } },
    el('caption', { className: 'sr-only' }, 'What copies over, and how'),
    el('thead', { className: 'sr-only' }, el('tr', {}, el('th', { scope: 'col' }, 'What'), el('th', { scope: 'col' }, 'How'))),
    WHAT_COPIES.map(group =>
      el(
        'tbody',
        { key: group.title },
        el('tr', {}, el('th', { className: HEADING, colSpan: 2, scope: 'colgroup', style: { ...TABLE_CELL, paddingTop: '0.5rem' } }, group.title)),
        group.rows.map(r =>
          el(
            'tr',
            { 'data-pending': r.pending ? 'true' : undefined, key: r.what },
            el('th', { className: 'text-(--ui-text-primary)', scope: 'row', style: { ...TABLE_CELL, fontWeight: 400, width: '34%' } }, r.what),
            el('td', { style: { ...TABLE_CELL, paddingRight: 0 } }, r.how)
          )
        )
      )
    )
  )
}

function coverageGroups(coverage, dir) {
  const groups = { by_hand: [], not_carried: [], syncs: [], syncs_via_mirror: [] }
  const rows = Array.isArray(coverage) && coverage.length ? coverage.slice(0, 20) : DEFAULT_COVERAGE

  for (const row of rows) {
    const group = groups[coverageValue(row?.[dir])]
    const item = kindLabel(row?.item)

    if (group && item) {
      group.push(item)
    }
  }

  return groups
}

// How fast a changed name or description in Grok shows up, said from what the backend reports and nothing
// else: "in seconds" while its fast check is on (it is woken the moment the Grok app saves), otherwise the
// timed check's real interval as a number. The same words everywhere.
function speedText(intervalS, fastCheck) {
  const s = Math.floor(Number(intervalS))

  if (fastCheck?.on === true || (s > 0 && s <= 15)) {
    return 'in seconds'
  }

  if (!(s > 0)) {
    return 'at the next check'
  }

  if (s < 60) {
    return `within ${s} seconds`
  }

  return s === 60 ? 'within a minute' : `within ${Math.ceil(s / 60)} minutes`
}

// 'skill "greet-visitors"' for a compared part that has a name, else the backend's own words.
function partName(check) {
  const id = clean(check?.id, 140)

  if (id.includes(':')) {
    const p = itemParts(id)

    return p.name ? `${p.kind} "${p.name}"` : p.kind
  }

  return clean(check?.name, 100) || kindLabel(id)
}

// Why a part differs between Grok and Hermes, and the one thing to press about it.
// action: 'show' (the row has a box for it; `matter` says which) | 'finish' (set-up step) | null
function differsBecause(check, pair, conflictIds) {
  const id = clean(check?.id, 140)
  const left = leftOf(pair) ?? []

  if (check?.action === 'choose_model' || id === 'model') {
    return { action: left.includes('model') ? 'finish' : null, name: 'model', text: 'none chosen yet' }
  }

  if (conflictIds.has(id)) {
    return { action: 'show', label: 'Choose', matter: 'conflict', text: 'changed in both places' }
  }

  const secret = secretPlaces(pair).find(p => p.item && (p.item === id || (!id.includes(':') && kindLabel(p.item) === kindLabel(id))))

  if (secret) {
    return { action: 'show', label: 'Show me', matter: `secret-${secret.item}`, text: `waiting for the password to be removed in ${secret.direction === 'h2g' ? 'Hermes' : 'Grok'}` }
  }

  if ((Array.isArray(pair?.skipped) ? pair.skipped : []).includes(id)) {
    return { action: null, text: 'kept in Hermes only; you can offer it again below' }
  }

  if ((Array.isArray(pair?.waiting) ? pair.waiting : []).some(w => w?.item === id)) {
    return { action: 'show', label: 'Show me', matter: `waiting-${id}`, text: 'changed in Hermes, not sent to Grok yet' }
  }

  if (kindLabel(id) === 'instructions' && !id.includes(':')) {
    return { action: left.includes('instructions') ? 'finish' : null, text: left.includes('instructions') ? 'not brought over yet' : 'you look after these yourself' }
  }

  // Changed in Hermes and still to be carried to Grok: the row has the box for it.
  const toGrok = toGrokItems(pair).find(i => i === id || (!id.includes(':') && !i.includes(':') && kindLabel(i) === kindLabel(id)))

  if (toGrok) {
    return { action: 'show', label: 'Show me', matter: 'unsent', text: 'changed in Hermes, not in Grok yet' }
  }

  // The engine's own note is written for its log (its words, its commands): it is never a sentence of this
  // list. A backend that sends its reason in plain words (`why`) is quoted; otherwise the page says what it
  // knows from the kind of part. The raw note waits under "Technical details".
  // (it follows "Routine "Morning summary" · ", so it starts in lower case unless it starts with a name)
  const given = plainText(check?.why, 160)
  const why = /^(Grok|Hermes|botport)\b/.test(given) ? given : `${given.charAt(0).toLowerCase()}${given.slice(1)}`
  const kind = id.includes(':') ? itemParts(id).kind : kindLabel(id)

  return { action: null, raw: clean(check?.detail, 200), text: why || (['routine', 'connection'].includes(kind) ? 'not set up in Hermes yet' : 'differs between Grok and Hermes') }
}

// Passing checks -> "name, description and 2 skills"
function matchWords(passing) {
  const counts = new Map()

  for (const c of passing) {
    const id = clean(c?.id, 140)
    const label = id.includes(':') ? itemParts(id).kind : kindLabel(id) || clean(c?.name, 60)

    if (label && label !== 'model') {
      counts.set(label, (counts.get(label) ?? 0) + 1)
    }
  }

  const words = [...counts].sort(([a], [b]) => kindRank(a) - kindRank(b)).map(([label, n]) => (n > 1 || ['skill', 'routine', 'connection'].includes(label) ? plural(n, label) : label))

  return words.length > 1 ? `${words.slice(0, -1).join(', ')} and ${words[words.length - 1]}` : (words[0] ?? '')
}

// `more`: sentences that are true of every twin and need not be read to use one (what goes back to Grok):
// they wait under "Technical details" with what could not be compared and the engine's own notes.
const firstSentence = text => /^.*?[.!?](?=\s|$)/.exec(String(text ?? ''))?.[0] ?? String(text ?? '')

function SameOrNot({ pair, open, onShow, onFinish, more = [], initialOpen = false }) {
  const q = useQuery({ enabled: open, queryFn: () => api(pairPath(pair.pair_id, 'parity')), queryKey: [ID, 'parity', pair.pair_id] })
  const conflictsOpen = (Number(pair?.conflicts) || 0) > 0
  const cq = useQuery({ enabled: open && conflictsOpen, queryFn: () => api(pairPath(pair.pair_id, 'conflicts')), queryKey: [ID, 'conflicts', pair.pair_id, pair.conflicts] })
  const conflictIds = new Set((conflictsOpen && Array.isArray(cq.data?.conflicts) ? cq.data.conflicts : []).map(c => clean(c?.conflict_id, 140)))
  const checks = Array.isArray(q.data?.checks) ? q.data.checks.slice(0, 100) : []
  const differing = checks.filter(c => !c?.pass)
  const total = Number(q.data?.total) || checks.length

  if (q.isPending) {
    return jsx(LinesSkeleton, { rows: 2 })
  }

  if (q.isError) {
    return jsx(InlineProblem, { error: q.error, onRetry: () => void q.refetch() })
  }

  if (total === 0) {
    return more.length > 0 ? el(Fold, { initialOpen, label: 'Technical details' }, el('div', { className: cn(SMALL, 'flex flex-col gap-1') }, more.map((m, i) => el('p', { key: i }, m)))) : null
  }

  // Instructions never count as "the same": Grok does not keep them on this computer, so Hermes cannot compare them.
  const isInstructions = c => kindLabel(clean(c?.id, 140)) === 'instructions' && !clean(c?.id, 140).includes(':')
  const matching = matchWords(checks.filter(c => c?.pass && !isInstructions(c)))
  const uncompared = checks.some(c => c?.pass && isInstructions(c))

  const reasons = differing.map(c => differsBecause(c, pair, conflictIds))
  // the engine's own notes, word for word, for whoever wants them
  const rawNotes = differing.map((c, i) => (reasons[i].raw ? `${reasons[i].name ?? partName(c)}: ${reasons[i].raw}` : '')).filter(Boolean)

  return el(
    'div',
    { className: 'flex flex-col gap-1' },
    el('span', { className: HEADING }, matching ? `${capital(matching)} ${matching.includes(' and ') || /s$/.test(matching) ? 'match' : 'matches'} Grok.` : 'Nothing could be compared with Grok yet.'),
    differing.length > 0 &&
      el(
        'ul',
        { className: 'flex flex-col gap-1' },
        differing.map((c, i) => {
          const why = reasons[i]
          const what = why.name ?? partName(c)
          const press =
            why.action === 'show'
              ? () => onShow?.(why.matter)
              : why.action === 'finish'
                ? () => onFinish?.()
                : null

          return el(
            'li',
            { className: cn('flex min-w-0 flex-wrap items-center gap-x-2', BODY), key: c?.id ?? i },
            // (the first sentence is the state; a second one, the reason, is the hover text)
            el('span', { className: 'min-w-0 flex-1', title: firstSentence(why.text) === why.text ? undefined : why.text }, el('span', { className: 'text-(--ui-text-primary)' }, capital(what)), ` · ${firstSentence(why.text).replace(/\.$/, '')}`),
            press &&
              el(
                Button,
                { 'aria-label': `${why.label ?? 'Finish setting up'}: ${what}`, onClick: press, variant: 'secondary' },
                why.label ?? 'Finish setting up'
              )
          )
        })
      ),
    (rawNotes.length > 0 || uncompared || more.length > 0) &&
      el(
        Fold,
        { initialOpen, label: 'Technical details' },
        el(
          'div',
          { className: cn(SMALL, 'flex flex-col gap-1') },
          more.map((m, i) => el('p', { key: i }, m)),
          uncompared && el('p', {}, 'Not compared: instructions. Grok does not keep them on this computer, so you look after them yourself.'),
          rawNotes.length > 0 && el('pre', { className: PRE }, rawNotes.join('\n'))
        )
      )
  )
}

// ── What syncs: one twin's eight things, and which way each goes ────────────
//
// The owner's choice, per twin and per thing: from Grok to Hermes, back, both ways, or off. The backend owns
// whether a way can work yet and when something last moved; the page shows what it last said (never ahead of
// it) and sends one choice at a time. One read of GET /sync/panel serves every twin. Folded inside a twin's
// Details, under the whole table, so the calm page does not grow. Not on the sample, and nothing at all on a
// backend that does not answer in this shape.

const SYNC_PANEL_KEY = [ID, 'sync-panel']
const SYNC_SCHEMA = 'grok-bot-twin.syncpanel.v1'
// the eight things, always in this order, with the page's own label for each
const SYNC_ITEMS = [
  ['name', 'Name'],
  ['description', 'Description'],
  ['instructions', 'Instructions'],
  ['skills', 'Skills'],
  ['routines', 'Routines'],
  ['memory_facts', 'Memory facts'],
  ['session_briefs', 'Session briefs'],
  ['transcripts', 'Conversations (full)']
]
const SYNC_WAYS = { grok_to_hermes: 'Grok → Hermes', hermes_to_grok: 'Hermes → Grok', both: 'Both ways', off: 'Off' }
const SYNC_ROW_STATES = ['ok', 'needs_mirror', 'not_yet', 'error']
const SYNC_MIRROR_STATES = ['ready', 'not_set_up', 'no_permission', 'unknown']
const SYNC_NEEDS_MIRROR = 'Needs the Mirror'
const SYNC_NOT_YET = 'Not in this version yet'
const SYNC_PRIVATE = 'Memory, session briefs and conversations start off for every twin, and only you switch them on.'
const syncGroupLabel = (label, twin) => `${label} for ${twin}: which way it syncs`

// GET (or POST) /sync/panel, tidied once: every text clean and capped, every key, way and state one of the
// page's own. null: not this answer (an older backend, a problem), and the page shows none of it.
function syncPanelOf(raw) {
  if (!raw || typeof raw !== 'object' || raw.schema !== SYNC_SCHEMA || !Array.isArray(raw.twins)) {
    return null
  }

  const text = (v, max = 300) => clean(typeof v === 'string' ? v : '', max)
  const way = v => (Object.hasOwn(SYNC_WAYS, v) ? v : null)
  const mirror = raw.mirror && typeof raw.mirror === 'object' ? raw.mirror : {}
  const twins = raw.twins
    // (a twin's id goes back to the backend exactly as it came: anything that is not plain short text is left out)
    .filter(t => t && typeof t === 'object' && typeof t.id === 'string' && t.id !== '' && t.id.length <= 120 && t.id === clean(t.id, 120))
    .slice(0, 100)
    .map(t => {
      const given = listOf(t.items).filter(i => i && typeof i === 'object').slice(0, 20)
      const items = SYNC_ITEMS.flatMap(([key, label]) => {
        const i = given.find(g => g.key === key)

        if (!i) {
          return []
        }

        const state = SYNC_ROW_STATES.includes(i.state) ? i.state : 'error'
        const offered = listOf(i.choices).filter(c => c && typeof c === 'object').slice(0, 8)
        // the four ways, always in this order; a way the backend did not offer as it should cannot be chosen
        const choices = Object.keys(SYNC_WAYS).map(direction => {
          const c = offered.find(o => o.direction === direction)
          const can = ['ok', 'needs_mirror', 'not_yet'].includes(c?.state) ? c.state : 'not_yet'

          return { direction, sentence: text(c?.sentence), state: can, why: can === 'ok' ? '' : text(c?.why) || (can === 'needs_mirror' ? `${SYNC_NEEDS_MIRROR}.` : `${SYNC_NOT_YET}.`) }
        })
        const err = i.error && typeof i.error === 'object' ? i.error : null

        return [
          {
            choices,
            direction: way(i.direction),
            error: state === 'error' ? { code: text(err?.code, 40) || 'error', message: text(err?.message) } : null,
            key,
            label: text(i.label, 40) || label,
            lastAt: isoOf(i.last_synced_at),
            lastWay: ['grok_to_hermes', 'hermes_to_grok'].includes(i.last_direction) ? i.last_direction : null,
            note: text(i.note),
            private: i.private === true,
            sentence: text(i.sentence),
            state
          }
        ]
      })

      return { id: t.id, items, mirror: ['ready', 'not_set_up', 'no_permission'].includes(t.mirror) ? t.mirror : 'not_set_up', name: text(t.name, 60) || 'This twin' }
    })

  return {
    mirror: { sentence: text(mirror.sentence, 400), state: SYNC_MIRROR_STATES.includes(mirror.state) ? mirror.state : 'unknown' },
    said: raw.said && typeof raw.said === 'object' ? text(raw.said.sentence, 400) : '',
    twins
  }
}

// An answer in hand is shown at once; the next read confirms it.
function showSyncPanel(res) {
  if (syncPanelOf(res)) {
    queryClient.setQueryData?.(SYNC_PANEL_KEY, res)
  } else {
    void queryClient.invalidateQueries({ queryKey: SYNC_PANEL_KEY })
  }
}

// "Last synced 3 min ago, Grok → Hermes" / "Not synced yet"
function syncedText(item) {
  const when = item.lastAt ? ago(item.lastAt) : null

  return when ? `Last synced ${when}${item.lastWay ? `, ${SYNC_WAYS[item.lastWay]}` : ''}` : 'Not synced yet'
}

// One of the four ways: a plain radio button with its words. Its sentence (what choosing it does) is the
// hover text; a way that cannot be chosen is described by the line under the group that says why.
function SyncOption({ id, name, choice, checked, disabled, why, onChoose }) {
  return el(
    'label',
    { className: cn('inline-flex min-h-7 items-center gap-1.5', BODY, checked && 'text-(--ui-text-primary)', !disabled && 'cursor-pointer'), htmlFor: id, title: choice.sentence || undefined },
    el('input', { 'aria-describedby': why || undefined, checked, className: 'shrink-0', disabled, id, name, onChange: () => onChoose(choice.direction), type: 'radio', value: choice.direction }),
    SYNC_WAYS[choice.direction]
  )
}

// One thing that can sync: its name, the four ways, what the way in effect means, and when it last moved.
// `said`: how the last act on this row ended ({ problem } or { said }), until the next act.
function SyncRow({ twin, item, busy, said, act }) {
  const id = `gbt-sync-${item.key}`
  const waiting = Boolean(busy)
  const shut = item.choices.filter(c => c.state !== 'ok')
  // the ways that cannot be chosen, grouped by their reason: "Hermes → Grok, Both ways: Needs the Mirror, …"
  const whys = [...new Set(shut.map(c => c.why))].map(why => ({ text: `${shut.filter(c => c.why === why).map(c => SYNC_WAYS[c.direction]).join(', ')}: ${why}`, why }))
  const whyId = why => `${id}-why-${whys.findIndex(w => w.why === why)}`
  // (a tag only on a row where nothing moves: a row that syncs one way is not "waiting", its why lines say the rest)
  const tag = item.direction !== 'off' && item.direction !== null ? '' : item.state === 'needs_mirror' ? SYNC_NEEDS_MIRROR : item.state === 'not_yet' ? SYNC_NOT_YET : ''

  return el(
    'li',
    { className: BOX },
    el('div', { className: 'flex flex-wrap items-center gap-x-2 gap-y-1' }, el('h3', { className: HEADING, id: `${id}-label` }, item.label), tag && jsx(Tag, { children: tag })),
    // (never ahead of the answer: the checked way is the one the backend last said, so a refusal leaves it where it was)
    el(
      'div',
      { 'aria-label': syncGroupLabel(item.label, twin.name), className: 'flex flex-wrap items-center gap-x-3', role: 'radiogroup' },
      item.choices.map(c =>
        jsx(
          SyncOption,
          { checked: item.direction === c.direction, choice: c, disabled: waiting || c.state !== 'ok', id: `${id}-${c.direction}`, name: id, onChoose: d => act(item, d), why: c.state === 'ok' ? '' : whyId(c.why) },
          c.direction
        )
      )
    ),
    item.sentence && el('p', { className: cn(BODY, 'text-(--ui-text-primary)') }, item.sentence),
    el('p', { className: SMALL }, syncedText(item)),
    item.note && el('p', { className: SMALL }, item.note),
    whys.map((w, i) => el('p', { className: SMALL, id: `${id}-why-${i}`, key: i }, w.text)),
    item.state === 'error' && jsx(InlineProblem, { error: new TwinError(item.error?.message || FALLBACK_PROBLEM, item.error?.code || 'error') }),
    said?.problem && jsx(InlineProblem, { error: said.problem }),
    said?.said && el('p', { className: cn(BODY, 'text-(--ui-text-primary)'), role: 'status' }, said.said)
  )
}

// The panel for one twin. `twin`: its part of the tidied answer. `onMirror`: opens the twin's skill sync set-up,
// where the Mirror is set up (offered once, for every row that needs it).
function SyncsPanel({ twin, mirror, onMirror, initialSaid = null }) {
  const [busy, run] = useBusy()
  const [said, setSaid] = useState(initialSaid) // { item, problem } | { item, said } | null

  // One choice at a time for the whole panel: every way waits while it runs. What comes back is shown as it is;
  // a refusal is said in that row, in the backend's words, and nothing else changes.
  // (a way that cannot be chosen is never sent, whatever let the press through)
  const act = (item, direction) =>
    item.choices.some(c => c.direction === direction && c.state === 'ok') &&
    run(`${item.key}:${direction}`, async () => {
      let next

      setSaid(null)

      try {
        const res = await post('/sync/panel', { twin: twin.id, item: item.key, direction })

        showSyncPanel(res)
        next = { item: item.key, said: syncPanelOf(res)?.said ?? '' }
      } catch (err) {
        next = { item: item.key, problem: asProblem(err) }
      }

      setSaid(next)

      return next
    })

  const needsMirror = twin.mirror !== 'ready' && twin.items.some(i => i.choices.some(c => c.state === 'needs_mirror'))
  const row = item => jsx(SyncRow, { act, busy, item, said: said?.item === item.key ? said : null, twin }, item.key)
  const open = twin.items.filter(i => !i.private)
  const kept = twin.items.filter(i => i.private)

  return el(
    'div',
    { className: 'flex flex-col gap-2' },
    needsMirror &&
      el(
        'div',
        { className: cn(NOTE, 'flex flex-wrap items-center gap-2') },
        el('span', { className: 'min-w-0 flex-1', id: 'gbt-sync-mirror' }, mirror.sentence || 'Some of these need the Mirror: a small bot in your Grok account that carries changes.'),
        onMirror ? el(Button, { 'aria-describedby': 'gbt-sync-mirror', onClick: onMirror, variant: 'secondary' }, 'Skill sync…') : el('span', {}, "Set it up with Skill sync… in this twin's menu.")
      ),
    open.length > 0 && el('ul', { className: 'flex flex-col gap-1.5' }, open.map(row)),
    kept.length > 0 && el('p', { className: SMALL }, SYNC_PRIVATE),
    kept.length > 0 && el('ul', { className: 'flex flex-col gap-1.5' }, kept.map(row))
  )
}

// The fold in a twin's Details: there only when the backend answers about this very twin.
function SyncsFold({ pair, onMirror, initialOpen = false, initialSaid = null }) {
  const sample = isSample(pair)
  const q = useQuery({ enabled: !sample, queryFn: () => api('/sync/panel'), queryKey: SYNC_PANEL_KEY, refetchInterval: 10_000, retry: 1 })
  const panel = sample ? null : syncPanelOf(q.data)
  const twin = panel?.twins.find(t => t.id === pair?.pair_id)

  if (!twin || twin.items.length === 0) {
    return null
  }

  return el(Fold, { initialOpen, label: 'What syncs' }, jsx(SyncsPanel, { initialSaid, mirror: panel.mirror, onMirror, twin }))
}

function CopiesPanel({ pair, open, intervalS, fastCheck, onSkillSync, onShow, onFinish, initialTechnical = false, initialTable = false, initialSyncs = false }) {
  const [busy, run] = useBusy()
  const g2h = coverageGroups(pair?.coverage, 'g2h')
  const h2g = coverageGroups(pair?.coverage, 'h2g')
  const on = skillSyncOn(pair)
  const skipped = Array.isArray(pair?.skipped) ? pair.skipped.slice(0, 30) : []
  const back = [...h2g.syncs, ...h2g.syncs_via_mirror]

  // (a sentence that is true of every twin is the row's hover text, `hover`; one about this twin, `note`, is shown)
  const row = (title, words, note, action, hover) =>
    words.length > 0 &&
    el(
      'div',
      { className: 'flex flex-col gap-0.5', title: hover },
      el('div', { className: 'flex flex-wrap items-baseline gap-x-2' }, el('span', { className: HEADING }, `${title}:`), el('span', { className: BODY }, joinWords(words))),
      note && el('p', { className: SMALL }, note),
      action
    )

  return el(
    'div',
    { className: 'flex flex-col gap-3' },
    row('Copies by itself', g2h.syncs, '', null, `A change in Grok reaches Hermes ${speedText(intervalS, fastCheck)}.`),
    // (off: the button that turns it on says so, and the whole sentence is the hover text)
    row(
      'With skill sync on',
      g2h.syncs_via_mirror,
      on ? 'On for this twin.' : '',
      !on && onSkillSync && el(Button, { className: 'self-start', onClick: onSkillSync, variant: 'secondary' }, 'Skill sync…'),
      on ? undefined : 'Skill sync is off for this twin, so for now you copy these yourself.'
    ),
    // (what a routine and a connection are is the hover text: the sheet stays within its words)
    row('You bring these over yourself', g2h.by_hand, '', null, 'The Grok app keeps none of these on this computer, so Hermes cannot read them. Routines are the things your bot does on a schedule; connections are its links to other services.'),
    row('Does not copy', g2h.not_carried),
    // (the lines above are this twin's; the table is every twin's, with the reasons: one press away, so the
    // sheet stays within its words)
    el(Fold, { initialOpen: initialTable, label: 'The whole table' }, jsx(WholeTable, {})),
    // (which way each of this twin's things goes, the owner's choice: folded too, and only on a backend that knows it)
    jsx(SyncsFold, { initialOpen: initialSyncs, onMirror: onSkillSync, pair }),
    jsx(SameOrNot, {
      initialOpen: initialTechnical,
      more: [back.length > 0 ? `Back to Grok: ${joinWords(back)} you change in Hermes go to Grok when you send them. Everything else you change in Grok yourself.` : 'Back to Grok: nothing goes by itself.'],
      onFinish,
      onShow,
      open,
      pair
    }),
    skipped.length > 0 &&
      el(
        'div',
        { className: 'flex flex-col gap-1' },
        el('span', { className: HEADING }, 'Kept in Hermes only'),
        skipped.map(item =>
          el(
            'div',
            { className: cn('flex items-center gap-2', BODY), key: item },
            el('span', { className: 'min-w-0 flex-1' }, itemLabel(item)),
            el(
              Button,
              { 'aria-label': `Offer ${itemLabel(item)} again`, disabled: Boolean(busy), loading: busy === item, onClick: () => void run(item, () => skipItem(pair, item, true), 'Could not change that'), variant: 'ghost' },
              'Offer again'
            )
          )
        )
      )
  )
}

// A line of the activity list in people's words, whatever the backend sends: a part is named as the page names
// it ("name", 'skill "greet-visitors"'), never by its key, and a version comes back "from an earlier version".
function plainActivity(summary) {
  return clean(summary, 160)
    .replace(/\bpersona\.(name|description|instructions)\b/g, '$1')
    .replace(/\bskill:([\w.-]{1,80})/g, 'skill "$1"')
    .replace(/\brestored from a kept copy\b/g, 'put back from an earlier version')
    .slice(0, 120)
}

// What changed lately, and where. The side is not repeated when the sentence already names it.
function RecentPanel({ pair, open }) {
  const q = useQuery({ enabled: open, queryFn: () => api(pairPath(pair.pair_id, 'recent')), queryKey: [ID, 'recent', pair.pair_id], refetchInterval: 60_000 })
  const entries = Array.isArray(q.data?.entries) ? q.data.entries.slice(0, 20) : []

  if (q.isPending) {
    return jsx(LinesSkeleton, { rows: 3 })
  }

  if (q.isError) {
    return jsx(InlineProblem, { error: q.error, onRetry: () => void q.refetch() })
  }

  if (entries.length === 0) {
    return el('p', { className: BODY }, 'Nothing yet. Changes in Grok or Hermes show up here.')
  }

  return el(
    'div',
    { className: 'flex flex-col gap-1' },
    el('p', { className: SMALL }, 'Times and kinds only, never what was said.'),
    el(
      'ul',
      { className: 'flex flex-col' },
      entries.map((e, i) => {
        const summary = plainActivity(e?.summary)
        const side = e?.side === 'grok' ? 'In Grok' : e?.side === 'both' ? 'In both' : 'In Hermes'

        return el(
          'li',
          { className: cn('flex min-w-0 items-center gap-2 py-1', BODY), key: `${e?.at}-${i}` },
          el('span', { className: 'min-w-0 flex-1 truncate' }, /\b(Grok|Hermes)\b/.test(summary) ? summary : `${side}: ${summary}`),
          el('span', { className: 'shrink-0 text-xs text-(--ui-text-secondary)' }, ago(e?.at) ?? '')
        )
      })
    )
  )
}

// The thing(s) an earlier version brings back, in words a person recognises.
// Item ids look like "skill:greet-visitors" and version ids like ITEM@DIGEST12;
// the digest is never shown.
function versionItems(p) {
  if (Array.isArray(p?.items)) {
    return p.items.slice(0, 50).map(i => itemLabel(typeof i === 'string' ? i : i?.item)).filter(Boolean)
  }

  const item = p?.item || (String(p?.id ?? '').includes('@') ? String(p.id).split('@')[0] : '')

  return item ? [itemLabel(item)] : []
}

function versionLabel(p) {
  const items = versionItems(p)

  if (items.length) {
    return listWords(items)
  }

  const n = Number(p?.items) || 0

  return n ? `The whole twin (${plural(n, 'item')})` : 'The whole twin'
}

// Putting a version back is done at once: what it replaced is kept too, and the notice offers Undo for as
// long as a warning stays (`undo` gets the id of the version that was just replaced, when the backend names it).
function toastPutBack(res, undo = null) {
  const restored = Array.isArray(res?.restored) ? res.restored : []

  if (restored.length === 0) {
    host.notify({ kind: 'info', message: 'Nothing to put back', meta: 'Hermes already has that version.' })

    return
  }

  const names = listWords(restored.map(r => itemLabel(typeof r === 'string' ? r : r?.item)))
  const waiting = Array.isArray(res?.waiting_for_ok) ? res.waiting_for_ok.length : 0
  const point = undo && typeof res?.restore_point === 'string' && res.restore_point ? res.restore_point : null

  host.notify({
    action: point ? { label: 'Undo', onClick: () => void undo(point, names) } : undefined,
    durationMs: WARNING_TOAST_MS,
    id: `${ID}:put-back`,
    kind: 'success',
    message: `Put back ${names}`,
    meta: waiting > 0 ? 'It now differs from Grok. Send it to Grok when you are ready.' : point ? undefined : 'The version it replaced is kept under Earlier versions.'
  })
}

// What took this version's place, when the backend says.
const REPLACED_BY = { choice: 'Replaced when you kept the Grok version', grok: 'Replaced by a change from Grok', import: 'Replaced by a setup export you imported', paste: 'Replaced by your paste', put_back: 'Replaced when you put an earlier version back', send: 'Replaced when you sent it to Grok' }

// One earlier version: what it is, when it was replaced, Show (whenever its text is known) and then Put back.
// Put back asks nothing: the current version is kept as well, and the notice that follows carries Undo.
function VersionRow({ point, first, busy, onRestore, initialShown = false }) {
  const [shown, setShown] = useState(initialShown)
  // "Description", "Instructions"; a skill keeps its own name as it is written
  const raw = versionLabel(point)
  const label = /^(name|description|instructions|the whole twin)\b/i.test(raw) ? capital(raw) : raw
  const day = shortDay(point?.at)
  const when = ago(point?.at)
  const why = REPLACED_BY[point?.replaced_by]
  const text = typeof point?.text === 'string' && point.text ? point.text.slice(0, 20_000) : ''

  return el(
    'li',
    { className: cn('flex min-w-0 flex-col gap-1.5 py-2', !first && 'border-t border-(--ui-stroke-tertiary)') },
    el(
      'div',
      { className: 'flex min-w-0 items-center gap-3' },
      jsx(Icon, { className: 'text-(--ui-text-tertiary)', name: 'history' }),
      el(
        'div',
        { className: 'flex min-w-0 flex-1 flex-col' },
        el('span', { className: 'break-words text-[0.8125rem] text-(--ui-text-primary)' }, label),
        el('span', { className: SMALL, title: when ?? undefined }, why ? `${why}${day ? ` on ${day}` : ''}` : when ? `Kept ${when}` : 'Kept')
      ),
      text && el(Button, { 'aria-expanded': shown, 'aria-label': `${shown ? 'Hide' : 'Show'} this version of ${label}`, onClick: () => setShown(!shown), variant: 'ghost' }, shown ? 'Hide' : 'Show'),
      el(Button, { 'aria-label': `Put back ${label}`, disabled: Boolean(busy), loading: busy === point?.id, onClick: onRestore, title: 'The current version is kept, so you can undo this.', variant: 'outline' }, 'Put back')
    ),
    shown && text && el('pre', { className: cn(PRE, 'max-h-40'), tabIndex: 0 }, text)
  )
}

// A version that holds nothing (the blank state from before a twin had that text) is not a version to go
// back to: it is left out, whatever the backend lists.
const worthShowing = p => p?.text !== ''

function VersionsPanel({ pair, open }) {
  const q = useQuery({ enabled: open, queryFn: () => api(pairPath(pair.pair_id, 'restore-points')), queryKey: [ID, 'restore-points', pair.pair_id] })
  const [busy, run] = useBusy()
  const points = (Array.isArray(q.data?.points) ? q.data.points : []).filter(worthShowing)

  // Undo: the version that Put back replaced goes back in its turn (it was kept, like every other one).
  async function undo(pointId, names) {
    try {
      await post(pairPath(pair.pair_id, 'restore'), { point_id: pointId })
      host.notify({ id: `${ID}:put-back`, kind: 'success', message: `Undone: ${names} ${/ and |, /.test(names) ? 'are' : 'is'} as before` })
      refreshAll()
    } catch (err) {
      notifyProblem(err, 'Could not undo that')
    }
  }

  async function restore(point) {
    const res = await run(point.id, () => post(pairPath(pair.pair_id, 'restore'), { point_id: point.id }), 'Could not put that version back')

    if (res) {
      toastPutBack(res, undo)
      refreshAll()
    }
  }

  if (q.isPending) {
    return jsx(LinesSkeleton, { rows: 4 })
  }

  if (q.isError) {
    return jsx(InlineProblem, { error: q.error, onRetry: () => void q.refetch() })
  }

  return el(
    'div',
    { className: 'flex flex-col gap-1' },
    el('p', { className: SMALL }, `Whenever something in ${owned(nameOf(pair))} twin is replaced, the version before is kept here, newest first.`),
    points.length === 0
      ? el('p', { className: cn(BODY, 'py-2') }, 'No earlier versions yet. One appears here the first time something is replaced.')
      : el(
          'ul',
          { className: 'flex flex-col' },
          points.slice(0, 100).map((p, i) => jsx(VersionRow, { busy, first: i === 0, onRestore: () => void restore(p), point: p }, p?.id ?? i))
        )
  )
}

const DETAIL_TABS = ['copies', 'versions', 'recent']

// `initialTab` lets the smoke test render each section directly; `initialTechnical` opens the fold of the first one.
function DetailsSheet({ pair, open, onClose, intervalS, fastCheck, checkedAt = null, onSkillSync, onShow, onFinish, initialTab = 'copies', initialTechnical = false, initialTable = false }) {
  const [tab, setTab] = useState(DETAIL_TABS.includes(initialTab) ? initialTab : 'copies')
  // The number on the tab is the number of versions its list shows (the same answer the list is drawn from:
  // one request, shared). The count in the status may include versions that hold nothing, which are not listed.
  const listed = useQuery({ enabled: open, queryFn: () => api(pairPath(pair.pair_id, 'restore-points')), queryKey: [ID, 'restore-points', pair.pair_id] })
  const versions = Array.isArray(listed.data?.points) ? listed.data.points.filter(worthShowing).length : 0
  const trigger = (value, label) => el(TabsTrigger, { key: value, value }, label)
  const checked = ago(checkedAt)

  return el(
    Sheet,
    {
      // (the three sections are named by their tabs: the line under the title is only how fresh this is)
      description: checked ? `Checked ${checked}.` : 'What follows Grok, and what was replaced.',
      onClose,
      open,
      title: `${nameOf(pair)}: details`,
      wide: true
    },
    el(
      Tabs,
      { onValueChange: setTab, value: tab },
      el(
        TabsList,
        // (the names of the sections that are not open are read too: the secondary colour, not the Desktop's faint one)
        { 'aria-label': 'Sections', className: 'self-start text-(--ui-text-secondary)' },
        trigger('copies', 'What copies over'),
        trigger('versions', versions > 0 ? `Earlier versions (${versions > 99 ? '99+' : versions})` : 'Earlier versions'),
        trigger('recent', 'Recent activity')
      )
    ),
    el(
      'div',
      { className: 'max-h-[55vh] min-h-48 overflow-y-auto pr-1' },
      tab === 'copies' && jsx(CopiesPanel, { fastCheck, initialTable, initialTechnical, intervalS, onFinish, onShow, onSkillSync, open, pair }),
      tab === 'versions' && jsx(VersionsPanel, { open, pair }),
      tab === 'recent' && jsx(RecentPanel, { open, pair })
    )
  )
}

// ── finish setting up: the two things a new twin still needs ────────────────
// A new twin has the bot's name and description. The Grok app does not keep a bot's instructions on
// this computer, so the owner pastes them once; and the twin's Hermes bot needs a model.

// One thing a new twin needs. A sheet step stays within fifty words, so only the thing to do now (`current`)
// shows its sentence and its controls. One that is done is a single line ("2. Model · same as your other
// chats"); one whose turn comes later is its title and, at most, its one button (`brief`) on the same line.
function FinishStep({ n, title, done, doneTitle = title, doneText = '', current = true, brief = null, children }) {
  return el(
    'li',
    { className: cn(CARD, 'flex flex-col gap-2') },
    el(
      'div',
      { className: 'flex min-w-0 flex-wrap items-center gap-2' },
      jsx(Icon, { className: done ? 'text-(--ui-text-secondary)' : 'text-(--ui-text-tertiary)', name: done ? 'pass-filled' : 'circle-large-outline' }),
      el('h3', { className: cn(HEADING, 'min-w-0 flex-1') }, done ? `${n}. ${doneTitle}${doneText ? ` · ${doneText}` : ''}` : `${n}. ${title}`),
      el('span', { className: 'sr-only' }, done ? '(done)' : '(to do)'),
      !done && !current && brief
    ),
    !done && current && el('div', { className: 'flex flex-col gap-2 pl-6' }, ...many(children))
  )
}

// ── what the page remembers ─────────────────────────────────────────────────
// Kept in Hermes Desktop's own storage for this plugin, not in the plugin's data folder, so leaving has to
// clear it from here. Per twin: the skill sync guide and its step, the ticked boxes, "waiting for it to come
// back" and "sent to choose a model". That storage cannot be listed, so NOTED_KEY names the twins something
// is kept for: all of it can then be cleared, also for a twin whose row is gone.
const TWIN_NOTES = ['mirror', 'mirror-step', 'ticks', 'kept', 'asked']
const NOTED_KEY = 'noted'

function notedTwins(storage) {
  const ids = storage?.get(NOTED_KEY, null)

  return (Array.isArray(ids) ? ids : []).filter(id => typeof id === 'string' && id).slice(0, 500)
}

function noteTwin(storage, pairId) {
  const ids = notedTwins(storage)

  if (pairId && !ids.includes(pairId)) {
    storage?.set(NOTED_KEY, [...ids, pairId])
  }
}

function forgetTwin(storage, pairId) {
  TWIN_NOTES.forEach(k => storage?.remove?.(`${k}.${pairId}`))
  // (and a text that was typed for it and never saved)
  unsavedText.delete(pairId)

  const ids = notedTwins(storage)

  if (ids.includes(pairId)) {
    const rest = ids.filter(id => id !== pairId)

    if (rest.length) {
      storage?.set(NOTED_KEY, rest)
    } else {
      storage?.remove?.(NOTED_KEY)
    }
  }
}

// Remove Grok Bot Twin: every twin's notes (`shown`: the twins on the page when it was pressed), where the
// twins are looked after, and when this computer was unlinked. (The one thing kept is "everything is
// removed", so the last screen is still there after a look elsewhere; it goes when the owner stays or
// twins again.)
function forgetEverything(storage, shown = []) {
  const ids = new Set([...notedTwins(storage), ...shown])

  ids.forEach(id => typeof id === 'string' && id && forgetTwin(storage, id))
  ;[NOTED_KEY, HOME_KEY, UNLINKED_KEY, PLACE_KEY, BATCH_KEY].forEach(k => storage?.remove?.(k))
}

// Something of the page's own is left although the plugin's data folder is empty (Grok Bot Twin was removed
// in the terminal, which cannot reach this storage): the page then still offers Remove Grok Bot Twin.
function notesLeft(storage) {
  return notedTwins(storage).length > 0 || Boolean(storage?.get(UNLINKED_KEY, null))
}

// The owner was sent to Hermes to choose this twin's model. Remembered on this computer, so the row and
// the sheet can say "waiting for you" until the model is chosen.
const askedKey = pairId => `asked.${pairId}`
const wasAsked = (storage, pairId) => Boolean(storage?.get(askedKey(pairId), null))

// Where a twin's model is chosen, in Hermes' own menu words (the backend's sentence when it sends one).
const modelWords = pair => plainText(pair?.model_hint, 240) || `In Hermes, open Settings, then Model. Under "Applies to" choose ${nameOf(pair)} and choose a model. Then click Twins in the sidebar.`

function askForModel(storage, pair) {
  storage?.set(askedKey(pair.pair_id), { at: new Date().toISOString() })
  noteTwin(storage, pair.pair_id)
  openInHermes(clean(pair?.hermes?.profile || '', 64))
  // The button lands on the twin's chat, where nothing repeats what to do next: the same sentence goes along,
  // and stays long enough to follow it.
  host.notify({ durationMs: 30_000, id: `${ID}:model:${pair.pair_id}`, kind: 'info', message: `Choose a model for ${nameOf(pair)}`, meta: modelWords(pair) })
}

// "line 14" / "lines 14 and 20" / "lines 14, 20 and 2 more"
function lineWords(nums) {
  if (nums.length === 1) {
    return `line ${nums[0]}`
  }

  return nums.length <= 3 ? `lines ${nums.slice(0, -1).join(', ')} and ${nums[nums.length - 1]}` : `lines ${nums.slice(0, 2).join(', ')} and ${nums.length - 2} more`
}

// `initialText` / `initialProblem` / `initialConfirmAll` / `initialAsked` let the smoke test render a
// pasted text, a refused one, the "save it all" question and the wait for a model.
// What was pasted or typed and not saved yet, per twin: Escape, a click outside or Later closes the sheet and
// loses nothing; it is in the box again when the sheet is opened from the row. Kept in memory only, while
// Hermes is open: instructions are never written anywhere before the owner saves them (and the check for
// passwords runs on saving).
const unsavedText = new Map()

function FinishSheet({ pair, open, onClose, onImport, storage, example = '', initialText = '', initialProblem = null, initialConfirmAll = false, initialAsked = null }) {
  const [busy, run] = useBusy()
  const [text, setText] = useState(() => initialText || unsavedText.get(pair.pair_id) || '')
  const [problem, setProblem] = useState(initialProblem)
  const [confirmAll, setConfirmAll] = useState(initialConfirmAll)
  // lines of this very text the owner already said are not a password (a second refusal names only new ones)
  const [accepted, setAccepted] = useState([])
  const [asked, setAsked] = useState(() => initialAsked ?? wasAsked(storage, pair.pair_id))
  const name = nameOf(pair)
  const sample = isSample(pair)
  const profile = clean(pair?.hermes?.profile || '', 64)
  const left = leftOf(pair) ?? []
  const needInstructions = left.includes('instructions')
  const needModel = left.includes('model')
  const sampleText = typeof example === 'string' ? example.slice(0, 20_000) : ''
  const box = useRef(null)
  // The lines the backend pointed at in the text it refused (numbers only; it never sends the text back).
  const flagged = problem?.code === 'looks_like_secret' ? (Array.isArray(problem.lines) ? problem.lines : []).filter(n => n <= text.split('\n').length) : []

  // (saving empties the box, and with it what is kept)
  useEffect(() => {
    if (text) {
      unsavedText.set(pair.pair_id, text)
    } else {
      unsavedText.delete(pair.pair_id)
    }
  }, [text])

  // Show the owner where: the first doubtful line is selected in the box.
  useEffect(() => {
    if (flagged.length === 0) {
      return
    }

    try {
      const rows = text.split('\n')
      const start = rows.slice(0, flagged[0] - 1).reduce((n, row) => n + row.length + 1, 0)

      box.current?.focus?.()
      box.current?.setSelectionRange?.(start, start + rows[flagged[0] - 1].length)
    } catch {
      // the sentence still names the line
    }
  }, [flagged.join(',')])

  async function save(body, said, note = '', kept = 'They stay on this computer. ') {
    setProblem(null)
    setConfirmAll(false)

    const res = await run('save', async () => {
      try {
        return await post(pairPath(pair.pair_id, 'instructions'), body)
      } catch (err) {
        // said in place, next to the text it is about
        setProblem(asProblem(err))

        return null
      }
    })

    if (res) {
      setText('')
      host.notify({ id: `${ID}:instructions:${pair.pair_id}`, kind: 'success', message: said, meta: `${note}${kept}Nothing was sent to Grok.` })
      refreshAll()
    }
  }

  // "Save without that line": the page takes the doubtful lines out; the rest goes through the same check.
  function saveWithout() {
    const drop = new Set(flagged)
    const rest = text.split('\n').filter((_, i) => !drop.has(i + 1)).join('\n')

    if (!rest.trim()) {
      setText('')
      setProblem(new TwinError('Nothing was left after taking that out.', 'empty', 'Paste the instructions again without the password or key.'))

      return
    }

    setText(rest)
    setAccepted([])
    void save({ text: rest }, `${owned(name)} instructions are saved`, `Without ${lineWords(flagged)}. `)
  }

  // "It is not a password. Save it all": the owner vouches for exactly the lines that were pointed out.
  function saveAll() {
    const lines = [...new Set([...accepted, ...flagged])].sort((a, b) => a - b)

    setAccepted(lines)
    void save({ accept_lines: lines, text }, `${owned(name)} instructions are saved`)
  }

  function chooseModel() {
    askForModel(storage, pair)
    setAsked(true)
  }

  const allDone = left.length === 0
  // (when a line is pointed at, the two buttons below are the ways on: the hint that names them is not said again)
  const said = problem && el('p', { className: 'text-xs text-(--ui-text-primary)', key: 'said', role: 'alert' }, [problem.message, flagged.length === 0 && problem.hint].filter(Boolean).join(' '))
  const one = flagged.length === 1

  return el(
    Sheet,
    {
      description: allDone ? 'It follows Grok. Everything else is in its "…" menu.' : `${capital(plural(left.length, 'thing'))} left, about a minute.`,
      footer: [
        el(Button, { disabled: Boolean(busy), key: 'done', onClick: onClose, title: !allDone && text ? 'What you typed is kept until Hermes closes.' : undefined, variant: allDone && (sample || !profile) ? 'default' : 'ghost' }, allDone ? 'Done' : 'Later'),
        allDone && !sample && profile ? el(Button, { key: 'open', onClick: () => { openInHermes(profile); onClose() } }, 'Open in Hermes') : null
      ].filter(Boolean),
      locked: Boolean(busy),
      onClose,
      open,
      title: allDone ? `${owned(name)} twin is ready` : `Finish setting up ${name}`,
      wide: true
    },
    el(
      'ol',
      { 'aria-label': 'Things left to do', className: 'flex max-h-[60vh] flex-col gap-2 overflow-y-auto pr-1' },
      el(
        FinishStep,
        { done: !needInstructions, doneText: 'in Hermes', doneTitle: 'Instructions', n: 1, title: 'Paste its instructions' },
        // (where to copy them from; not repeated once a pasted text is being looked at for a password)
        flagged.length === 0 && el('p', { className: BODY }, `In the Grok app, edit ${name} and copy its instructions.`),
        jsx(Textarea, {
          'aria-label': `Instructions for ${name}`,
          className: 'max-h-56 min-h-28 text-[0.8125rem]',
          disabled: Boolean(busy),
          maxLength: 200_000,
          onChange: e => {
            setText(e.target.value)
            setProblem(null)
            setConfirmAll(false)
            setAccepted([])
          },
          placeholder: 'Paste the instructions here',
          ref: box,
          value: text
        }),
        // (the question takes the place of the sentence it is about, and names the line again)
        !(confirmAll && flagged.length > 0) && said,
        flagged.length > 0
          ? confirmAll
            ? el(
                'div',
                { className: cn(NOTE, 'flex flex-col gap-2'), role: 'group' },
                el('span', { className: 'text-(--ui-text-primary)' }, `Save ${lineWords(flagged)} too? ${one ? 'It stays' : 'They stay'} on this computer.`),
                el(
                  'div',
                  { className: 'flex flex-wrap gap-2' },
                  el(Button, { disabled: Boolean(busy), loading: busy === 'save', onClick: saveAll, variant: 'secondary' }, 'Save it all'),
                  el(Button, { disabled: Boolean(busy), onClick: () => setConfirmAll(false), variant: 'ghost' }, 'Cancel')
                )
              )
            : el(
                'div',
                { className: 'flex flex-wrap items-center gap-2' },
                el(Button, { disabled: Boolean(busy), loading: busy === 'save', onClick: saveWithout }, one ? 'Save without that line' : 'Save without those lines'),
                el(Button, { disabled: Boolean(busy), onClick: () => setConfirmAll(true), variant: 'text' }, 'Save it all…')
              )
          : el(
              'div',
              { className: 'flex flex-wrap items-center gap-2' },
              el(Button, { disabled: Boolean(busy) || text.trim().length === 0, loading: busy === 'save', onClick: () => void save({ text }, `${owned(name)} instructions are saved`) }, 'Save instructions'),
              sample && sampleText && !text && el(Button, { disabled: Boolean(busy), onClick: () => setText(sampleText), variant: 'secondary' }, "Paste the sample's instructions"),
              el(Button, { disabled: Boolean(busy), onClick: () => void save({ none: true }, `Noted: ${name} has no instructions`, '', ''), variant: 'ghost' }, 'It has no instructions'),
              // (the bot can also write its instructions, routines and connector names into one file)
              onImport && !sample && el(Button, { disabled: Boolean(busy), onClick: onImport, variant: 'text' }, 'Import them instead…')
            ),
        flagged.length === 0 &&
          el(
            Fold,
            { label: 'Why paste them?' },
            el('p', { className: SMALL }, "The Grok app does not keep a bot's instructions on this computer, so there is nothing for Hermes to read. What you paste is saved on this computer only, and a line that looks like a password or key is pointed out before anything is saved. Text you have not saved is kept until Hermes closes.")
          )
      ),
      !sample &&
        el(
          FinishStep,
          {
            // (while the instructions are still to do, this one waits as one line with its button)
            brief: el(Button, { disabled: Boolean(busy) || !profile, onClick: chooseModel, variant: 'text' }, asked ? 'Open it again' : 'Choose in Hermes'),
            current: !needInstructions,
            done: !needModel,
            doneText: pair?.hermes?.model_from === 'default' ? 'same as your other chats' : 'chosen',
            doneTitle: 'Model',
            n: 2,
            title: 'Choose a model'
          },
          asked
            ? el('p', { className: cn(BODY, 'text-(--ui-text-primary)'), role: 'status' }, `Waiting for you to choose a model for ${name} in Hermes. This step ticks by itself.`)
            : // the backend's own sentence when it knows better where this Hermes keeps the choice
              el('p', { className: BODY }, modelWords(pair)),
          el(
            'div',
            {},
            el(Button, { disabled: Boolean(busy) || !profile, onClick: chooseModel, variant: asked ? 'secondary' : 'default' }, asked ? 'Open it again' : 'Choose in Hermes')
          )
        )
    ),
    sample && allDone && el('p', { className: NOTE }, 'The sample has no chat. Connect a real bot to talk to its twin.')
  )
}

// ── import a setup export ───────────────────────────────────────────────────
// The owner asks the bot itself for its setup (a text to paste into the bot's own chat) and hands Hermes the
// file it answers with: its instructions, its routines and the names of its connectors. The file's text is
// held by this sheet only, while it is open: never in the page's storage, never in a list that outlasts the
// sheet. The backend reads it twice (to show what would happen, then to do it) and keeps none of it.
// Memories and facts about the owner are left out before anything else looks at the file.
const EXPORT_MAX = 2_000_000
const EXPORT_PRIVACY = 'Its lists of memories and facts about you are left out: Hermes never stores them.'
const EXPORT_TOO_LARGE = 'That file is too large to be a setup export.'
const INSTRUCTIONS_HELD = 'Not copied: too much of it looks like a password or key.'
const ROUTINE_SAID = { exists: 'a routine of this name is already in Hermes', held: 'not copied: looks like a password or key' }
const ROUTINE_FALLBACK = { failed: 'could not be added', manual: 'make this one in Hermes yourself' }

// "Left out: 3 memories, 2 facts about you, 1 skill. Hermes never stores them." ('' when nothing was)
function leftOutWords(left) {
  const n = k => countOf(left?.[k]) ?? 0
  const parts = [
    n('memories') > 0 && plural(n('memories'), 'memory', 'memories'),
    n('user_facts') > 0 && `${plural(n('user_facts'), 'fact')} about you`,
    n('skills') > 0 && plural(n('skills'), 'skill'),
    n('routines') > 0 && `${n('routines')} more ${n('routines') === 1 ? 'routine' : 'routines'}`
  ].filter(Boolean)

  return parts.length > 0 ? `Left out: ${parts.join(', ')}. Hermes never stores them.` : ''
}

// The backend's answer about one file, as the sheet shows it. Every string in it began in a file somebody
// handed over: plain text, capped, and only ever rendered as text.
function exportOf(res) {
  const ins = res?.instructions ?? {}

  return {
    added: countOf(res?.added) ?? 0,
    applied: res?.applied === true,
    bot: { matches: res?.bot?.matches !== false, name: clean(res?.bot?.name, 60) },
    connectors: listOf(res?.connectors).map(c => clean(c?.name, 80)).filter(Boolean).slice(0, 50),
    instructions: {
      leftOut: listOf(ins.left_out).map(n => Math.floor(Number(n))).filter(n => n >= 1 && n <= 1_000_000).slice(0, 200),
      lines: countOf(ins.lines) ?? 0,
      replaces: ins.replaces === true,
      saved: ins.saved === true,
      state: clean(ins.state, 20),
      // (all of it: what is shown here is what an import saves, to the last character)
      text: typeof ins.text === 'string' ? ins.text.slice(0, 210_000) : '',
      why: plainText(ins.why, 240)
    },
    leftOut: leftOutWords(res?.left_out),
    routines: listOf(res?.routines).slice(0, 50).map(r => ({
      name: clean(r?.name, 120) || 'A routine',
      prompt: typeof r?.prompt === 'string' ? r.prompt.slice(0, 17_000) : '',
      schedule: clean(r?.schedule, 60),
      state: clean(r?.state, 20),
      timezone: clean(r?.timezone, 64),
      why: plainText(r?.why, 200)
    }))
  }
}

// What an import did, for its notice: counts and kinds, never a word of the file.
function importedWords(got) {
  return [
    got.instructions.saved && 'Instructions saved.',
    got.added > 0 && `${plural(got.added, 'routine')} added, switched off.`,
    got.connectors.length > 0 && `${plural(got.connectors.length, 'connector')} to set up on this computer.`
  ].filter(Boolean).join(' ')
}

// The text of a file the owner chose or dropped. Refused in plain words when it cannot be a setup export:
// too large (looked at before it is read, and again after), unreadable, or empty.
async function exportTextOf(file) {
  const pick = 'Choose the file the bot sent.'

  if (Number(file?.size) > EXPORT_MAX) {
    throw new TwinError(EXPORT_TOO_LARGE, 'too_large', pick)
  }

  let body = ''

  try {
    body = await file.text()
  } catch {
    throw new TwinError('Hermes could not read that file.', 'unreadable', 'Choose it again.')
  }

  if (typeof body !== 'string' || !body.trim()) {
    throw new TwinError('That file is empty.', 'empty', pick)
  }

  if (body.length > EXPORT_MAX) {
    throw new TwinError(EXPORT_TOO_LARGE, 'too_large', pick)
  }

  return body
}

// `initialText` / `initialShown` / `initialProblem` / `initialOver` / `initialCopied` / `initialPasted` let the
// smoke test render a file that was read, a refusal, a file held over the box, a copy the clipboard would not
// take and an answer pasted as text.
function ImportSheet({ pair, open, onClose, onMove, initialText = '', initialShown = null, initialProblem = null, initialOver = false, initialCopied = null, initialPasted = '' }) {
  const q = useQuery({ enabled: open, queryFn: () => api(pairPath(pair.pair_id, 'setup-export')), queryKey: [ID, 'setup-export', pair.pair_id] })
  const [busy, run] = useBusy()
  // the file's text: here, and nowhere else on the page
  const [text, setText] = useState(initialText)
  const [pasted, setPasted] = useState(initialPasted)
  // the backend's answer: what an import would do, and after Import what it did
  const [shown, setShown] = useState(initialShown)
  const [problem, setProblem] = useState(initialProblem)
  const [over, setOver] = useState(initialOver)
  // null: not pressed yet; true: on the clipboard; false: the clipboard would not take it, so the text is shown
  const [copied, setCopied] = useState(initialCopied)
  const picker = useRef(null)
  const name = nameOf(pair)
  const request = typeof q.data?.request === 'string' ? q.data.request.slice(0, 8000) : ''
  const privacy = plainText(q.data?.privacy, 200) || EXPORT_PRIVACY
  const last = q.data?.last && isoOf(q.data.last.at) ? { at: q.data.last.at, connectors: listOf(q.data.last.connectors).map(c => clean(c, 80)).filter(Boolean).slice(0, 50) } : null
  const view = shown ? exportOf(shown) : null
  const path = pairPath(pair.pair_id, 'setup-export')
  async function copyRequest() {
    setCopied(await copyText(request))
  }

  // Ask what an import of this text would do. Nothing is changed by asking.
  async function read(body) {
    if (body.length > EXPORT_MAX) {
      setProblem(new TwinError(EXPORT_TOO_LARGE, 'too_large', 'Paste only what the bot sent.'))

      return
    }

    setProblem(null)

    const res = await run('read', async () => {
      try {
        return await post(path, { apply: false, text: body })
      } catch (err) {
        // said in place, next to the box the file was dropped on
        setProblem(asProblem(err))

        return null
      }
    })

    if (res) {
      setText(body)
      setPasted('')
      setShown(res)
    }
  }

  async function readFile(file) {
    if (!file) {
      return
    }

    let body = ''

    try {
      body = await exportTextOf(file)
    } catch (err) {
      setProblem(asProblem(err))

      return
    }

    await read(body)
  }

  async function apply() {
    setProblem(null)

    const res = await run('apply', async () => {
      try {
        // (the bot's name goes back as the backend sent it: the import is of the bot that was shown)
        return await post(path, { apply: true, bot: typeof shown?.bot?.name === 'string' ? shown.bot.name : '', text })
      } catch (err) {
        setProblem(asProblem(err))

        return null
      }
    })

    if (res) {
      // the file has done its work: its text is let go
      setText('')
      setShown(res)
      host.notify({ id: `${ID}:import:${pair.pair_id}`, kind: 'success', message: `${owned(name)} setup export is imported`, meta: [importedWords(exportOf(res)), 'Nothing was sent to Grok.'].filter(Boolean).join(' ') })
      refreshAll()
    }
  }

  function another() {
    setText('')
    setShown(null)
    setProblem(null)
  }

  const hover = e => {
    e.preventDefault()
    setOver(true)
  }
  const said = problem && el('p', { className: 'text-xs text-(--ui-text-primary)', key: 'said', role: 'alert' }, [problem.message, problem.hint].filter(Boolean).join(' '))
  const connectorList = (names, key) =>
    names.length > 0 &&
    el(
      'section',
      { className: 'flex flex-col gap-1', key },
      el('h3', { className: HEADING }, 'Set up on this computer'),
      el('p', { className: cn(BODY, 'break-words text-(--ui-text-primary)') }, names.join(', ')),
      el('p', { className: SMALL }, 'No logins are copied: connect these in Hermes yourself.')
    )

  let body
  let footer
  let title = `Import ${owned(name)} setup export`

  if (!view) {
    // ── ask the bot, then hand over what it sent ──
    const askSaid = copied === true ? 'Copied. Paste it into its Grok chat.' : copied === false ? 'Copy this, then paste it into its chat in the Grok app.' : 'Paste this into its chat in the Grok app.'

    body = el(
      'div',
      {
        className: 'flex max-h-[60vh] flex-col gap-4 overflow-y-auto pr-1',
        // A file let go beside the box is read like one let go on it: it never falls through to whatever
        // else in the app takes dropped files.
        onDragOver: e => e.preventDefault(),
        onDrop: e => {
          e.preventDefault()
          setOver(false)
          void readFile(e.dataTransfer?.files?.[0])
        }
      },
      el(
        'section',
        { className: 'flex flex-col gap-2' },
        el('h3', { className: HEADING }, 'Ask the bot'),
        // (while a refusal is on the sheet, only what is needed for another try stays next to it)
        !problem && el('p', { className: BODY, role: copied === null ? undefined : 'status' }, askSaid),
        q.isError
          ? jsx(InlineProblem, { error: q.error, onRetry: () => void q.refetch() })
          : el(
              'div',
              {},
              el(Button, { disabled: !request || Boolean(busy), onClick: () => void copyRequest(), variant: copied === true ? 'secondary' : 'default' }, 'Copy the export request')
            ),
        request &&
          (!problem || copied === false) &&
          el(
            Fold,
            { initialOpen: copied === false, key: copied === false ? 'shown' : 'folded', label: 'Show the request', openLabel: 'Hide the request' },
            el('pre', { className: cn(PRE, 'max-h-40 select-all'), tabIndex: 0 }, request),
            el('div', {}, jsx(CopyButton, { appearance: 'button', buttonVariant: 'secondary', label: 'Copy', showLabel: true, text: request }))
          )
      ),
      el(
        'section',
        { className: 'flex flex-col gap-2' },
        el(
          'div',
          {
            'aria-label': 'Drop the setup export here',
            className: cn('flex flex-col items-center gap-2 rounded-lg border-2 border-dashed px-4 py-5 text-center', over ? 'border-(--ui-text-secondary) bg-(--ui-bg-quinary)' : 'border-(--ui-stroke-secondary)'),
            'data-over': over ? 'yes' : 'no',
            onDragEnter: hover,
            onDragLeave: e => {
              if (!e.currentTarget?.contains?.(e.relatedTarget)) {
                setOver(false)
              }
            },
            onDragOver: hover,
            onDrop: e => {
              // (the file stays in this box: it is never handed on to whatever else listens for a drop)
              e.preventDefault()
              e.stopPropagation?.()
              setOver(false)
              void readFile(e.dataTransfer?.files?.[0])
            },
            role: 'group'
          },
          el('h3', { className: HEADING, role: over ? 'status' : undefined }, over ? 'Drop it here' : 'Drop its answer here'),
          el('input', {
            accept: '.json,.txt,application/json,text/plain',
            'aria-hidden': true,
            className: 'hidden',
            onChange: e => {
              const file = e.target.files?.[0]

              // (so that choosing the same file again is a change too)
              e.target.value = ''
              void readFile(file)
            },
            ref: picker,
            tabIndex: -1,
            type: 'file'
          }),
          el(Button, { disabled: Boolean(busy), loading: busy === 'read' && !pasted, onClick: () => picker.current?.click?.(), variant: copied === true ? 'default' : 'secondary' }, 'Choose the file…')
        ),
        el(
          Fold,
          { initialOpen: Boolean(initialPasted), label: 'Or paste it' },
          jsx(Textarea, {
            'aria-label': `The setup export ${name} sent`,
            className: 'max-h-40 min-h-20 text-[0.8125rem]',
            disabled: Boolean(busy),
            maxLength: EXPORT_MAX,
            onChange: e => {
              setPasted(e.target.value)
              setProblem(null)
            },
            placeholder: 'Paste its answer here',
            value: pasted
          }),
          el('div', {}, el(Button, { disabled: Boolean(busy) || pasted.trim().length === 0, loading: busy === 'read' && Boolean(pasted), onClick: () => void read(pasted), variant: 'secondary' }, 'Read it'))
        ),
        said,
        el('p', { className: SMALL }, privacy)
      ),
      last &&
        !problem &&
        el(
          Fold,
          { label: 'Last import' },
          el('p', { className: SMALL }, `${capital(ago(last.at) ?? 'earlier')}.`),
          connectorList(last.connectors, 'last')
        )
    )
    footer = [el(Button, { disabled: Boolean(busy), key: 'close', onClick: onClose, variant: 'ghost' }, 'Close')]
  } else {
    // ── what the file holds: before Import what would happen, after it what did ──
    const ins = view.instructions
    const done = view.applied
    const one = ins.leftOut.length === 1
    const insSaid = ins.saved
      ? `${plural(ins.lines, 'line')} saved`
      : ins.state === 'new'
        ? plural(ins.lines, 'line')
        : ins.state === 'same'
          ? 'Already in Hermes.'
          : ins.state === 'held'
            ? ins.why || INSTRUCTIONS_HELD
            : 'The file has no instructions.'
    const taken = ins.saved || ins.state === 'new'
    const fresh = view.routines.filter(r => r.state === 'new').length
    const routineSaid = r => ROUTINE_SAID[r.state] ?? (r.state in ROUTINE_FALLBACK ? r.why || ROUTINE_FALLBACK[r.state] : '')
    const texts = [taken && ins.text ? { name: 'Instructions', text: ins.text } : null, ...view.routines.filter(r => r.prompt && r.state !== 'held').map(r => ({ name: r.name, text: r.prompt }))].filter(Boolean)
    const anything = !done && (ins.state === 'new' || fresh > 0 || view.connectors.length > 0)
    const other = !view.bot.matches && view.bot.name

    if (done) {
      title = `${owned(name)} setup export is imported`
    }

    body = el(
      'div',
      // (a file let go here is not read, and goes nowhere else either)
      { className: 'flex max-h-[60vh] flex-col gap-4 overflow-y-auto pr-1', onDragOver: e => e.preventDefault(), onDrop: e => e.preventDefault() },
      other && !done && el('p', { className: cn(NOTE, 'text-(--ui-text-primary)'), role: 'status' }, `This export is from "${view.bot.name}", not from "${name}".`),
      el(
        'section',
        { className: 'flex flex-col gap-1' },
        el('div', { className: 'flex min-w-0 flex-wrap items-baseline gap-x-2' }, el('h3', { className: HEADING }, 'Instructions'), el('span', { className: BODY }, insSaid)),
        taken && ins.leftOut.length > 0 && el('p', { className: BODY }, `${capital(lineWords(ins.leftOut))} ${one ? 'is' : 'are'} left out: ${one ? 'it looks' : 'they look'} like a password or key.`),
        taken && ins.replaces && el('p', { className: SMALL }, done ? 'The version before is kept under Earlier versions.' : 'Replaces the instructions this twin has now. The version before is kept under Earlier versions.')
      ),
      view.routines.length > 0 &&
        el(
          'section',
          { className: 'flex flex-col gap-1' },
          el('h3', { className: HEADING }, 'Routines'),
          el(
            'ul',
            { 'aria-label': 'Routines in the file', className: cn(BODY, 'flex flex-col gap-0.5') },
            view.routines.map((r, i) =>
              el(
                'li',
                { className: 'break-words', key: i },
                el('span', { className: 'text-(--ui-text-primary)' }, r.name),
                [r.schedule && `${cronText(r.schedule)}${r.timezone ? ` (${r.timezone})` : ''}`, routineSaid(r)].filter(Boolean).map(part => ` · ${part}`).join('')
              )
            )
          ),
          !done && fresh > 0 && el('p', { className: SMALL }, `${fresh} new ${fresh === 1 ? 'routine stays' : 'routines stay'} switched off until you choose Move routines to Hermes.`),
          done && view.added > 0 && el('p', { className: SMALL, role: 'status' }, `${plural(view.added, 'routine')} added, switched off.`)
        ),
      connectorList(view.connectors, 'connectors'),
      !done && !anything && el('p', { className: BODY, role: 'status' }, 'Nothing new in this file.'),
      view.leftOut && el('p', { className: SMALL }, view.leftOut),
      texts.length > 0 &&
        el(
          Fold,
          { label: 'Show the text', openLabel: 'Hide the text' },
          texts.map((t, i) => el('div', { className: 'flex flex-col gap-1', key: i }, el('span', { className: SMALL }, t.name), el('pre', { className: cn(PRE, 'max-h-40'), tabIndex: 0 }, t.text)))
        ),
      said
    )
    footer = done
      ? [
          view.added > 0 && onMove ? el(Button, { key: 'move', onClick: onMove, variant: 'secondary' }, 'Move routines to Hermes…') : null,
          el(Button, { key: 'done', onClick: onClose }, 'Done')
        ].filter(Boolean)
      : [
          el(Button, { disabled: Boolean(busy), key: 'another', onClick: another, variant: 'ghost' }, 'Choose another file'),
          anything
            ? el(Button, { disabled: Boolean(busy) || !text, key: 'go', loading: busy === 'apply', onClick: () => void apply() }, other ? `Import from ${view.bot.name} anyway` : 'Import')
            : el(Button, { key: 'done', onClick: onClose }, 'Done')
        ]
  }

  return el(Sheet, { footer, locked: Boolean(busy), onClose, open, title, wide: true }, body)
}

// ── move to Hermes ──────────────────────────────────────────────────────────

// Unticked boxes need a stronger edge than the default to read on dark themes.
// The edge of an empty box is 3 to 1 or better against the sheet, light and dark (the stroke colours are 1.8 to 1).
// The box is 16 px as the Desktop draws it; the words next to it take the press too, on 26 px (TICK_LABEL:
// the padding that used to be the row's is the label's, so the row is as high as before).
const CHECKBOX = 'mt-1 border-(--ui-text-tertiary)'
const TICK_LABEL = 'flex min-w-0 flex-1 flex-col py-1'

// A ticked row stays as it is written: a tick, never a strike-through (that reads as deleted).
function CheckRow({ id, checked, onChange, title, detail, disabled, label }) {
  return el(
    'div',
    // (a row that cannot be ticked is quieter by its colour, never faded: faded 12 px words are 3 to 1)
    { className: 'flex min-w-0 items-start gap-2.5 py-0.5' },
    jsx(Checkbox, { 'aria-label': label ? clean(label, 120) : undefined, checked, className: CHECKBOX, disabled, id, onCheckedChange: v => onChange(v === true) }),
    el(
      'label',
      { className: cn(TICK_LABEL, !disabled && 'cursor-pointer'), htmlFor: id },
      el(
        'span',
        { className: cn('break-words text-[0.8125rem] leading-snug', disabled ? 'text-(--ui-text-secondary)' : 'text-(--ui-text-primary)') },
        clean(title, 600)
      ),
      detail && el('span', { className: 'break-words text-xs text-(--ui-text-secondary)' }, clean(detail, 300))
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

// What has to be settled before a twin's routines can move, as one plain sentence. Only two things hold
// it up: a choice between two versions, and a twin that cannot be checked. A skill change waiting to be
// sent has nothing to do with routines; if the backend still insists, the page names the real thing.
function blockedSentence(pair, fromBackend) {
  const matters = mattersOf(pair)
  const first = matters.find(m => m.kind === 'error' || m.kind === 'conflict')
  const given = plainText(fromBackend, 200)

  if (first) {
    return given || (first.kind === 'conflict' ? 'First choose which version to keep.' : 'Hermes has to be able to check this twin first.')
  }

  // nothing is open on this twin: an old sentence from the backend would be stale
  if (!given || matters.length === 0) {
    return ''
  }

  const waiting = matters.find(m => m.kind === 'waiting' && m.waiting?.item)

  return waiting ? `First decide what to do with your change to ${itemPhrase(waiting.waiting.item)}.` : given
}

async function undoMove(pair) {
  const res = await post(pairPath(pair.pair_id, 'switch'), { undo: true })
  const n = Array.isArray(res?.paused) ? res.paused.length : 0
  const failed = Array.isArray(res?.failed) ? res.failed.length : 0

  host.notify(
    failed
      ? { kind: 'warning', message: `Paused ${plural(n, 'routine')} in Hermes, ${failed} could not be paused`, meta: 'Open Routines in Hermes… in the twin\'s "…" menu to see which ones are still on.' }
      : { kind: 'success', message: `Paused ${plural(n, 'routine')} in Hermes again`, meta: 'Switch them back on in Grok if you want them to keep running there.' }
  )
  refreshAll()

  return res
}

const ROUTINES_ARE = 'Routines are the things your bot does on a schedule.'

// `onShow` closes the sheet at the box that has to be dealt with first.
function MoveSheet({ pair, open, onClose, onShow, storage }) {
  const q = useQuery({ enabled: open, queryFn: () => api(pairPath(pair.pair_id, 'checklist')), queryKey: [ID, 'checklist', pair.pair_id] })
  const storeKey = `ticks.${pair.pair_id}`
  const [ticks, setTicks] = useState(() => storage?.get(storeKey, {}) ?? {})
  const [jobs, setJobs] = useState(null)
  const [busy, run] = useBusy()
  const name = nameOf(pair)

  const routines = Array.isArray(q.data?.switch?.grok_routines_to_turn_off) ? q.data.switch.grok_routines_to_turn_off : []
  const paused = Array.isArray(q.data?.switch?.hermes_jobs_paused) ? q.data.switch.hermes_jobs_paused : []
  const running = Array.isArray(q.data?.switch?.hermes_jobs_resumed) ? q.data.switch.hermes_jobs_resumed : []
  // A routine already shows under step 1 and, copied switched off, under the last step:
  // it is not listed a third time in between. Instructions have their own place (Finish setting up).
  // When no copy arrived at all (Grok keeps a routine's schedule to itself, so the real botport makes none),
  // the routine is the owner's to make again and belongs in their checklist: there is no last step then.
  const yourself = (Array.isArray(q.data?.by_hand) ? q.data.by_hand : []).filter(b => {
    const item = String(b?.item ?? '')

    if (item.startsWith('routine:')) {
      return paused.length + running.length === 0
    }

    return kindLabel(item) !== 'instructions'
  })
  // Inside this twin's own sheet a routine needs no "Acme Support: " in front of its name.
  const jobName = j => {
    const raw = clean(j?.name ?? 'A routine', 200)

    return (raw.startsWith(`${name}: `) ? raw.slice(name.length + 2) : raw) || 'A routine'
  }
  // A routine the owner just ticked off in Grok is ticked here already; nothing else is preselected,
  // and nothing is switched on before the owner presses the button.
  const tickedOff = new Set(routines.filter((r, i) => ticks[`r:${r?.name ?? i}`]).map(r => clean(r?.name, 200)))
  const selected = jobs ?? Object.fromEntries(paused.filter(j => tickedOff.has(jobName(j))).map(j => [j?.job_id, true]))
  const chosen = paused.filter(j => selected[j?.job_id]).map(j => j.job_id)
  const allJobs = paused.length > 0 && chosen.length === paused.length
  const routinesDone = routines.every((r, i) => ticks[`r:${r?.name ?? i}`])
  const movedOn = pair?.switched?.at ? shortDay(pair.switched.at) : ''
  const moved = paused.length === 0 && (running.length > 0 || Boolean(movedOn))
  const blocked = moved ? '' : blockedSentence(pair, q.data?.blocked_by)
  const nothing = !moved && routines.length === 0 && paused.length === 0
  const loaded = !q.isPending && !q.isError

  function tick(key, value) {
    // from the latest ticks, so two quick ticks never lose one
    setTicks(prev => {
      const next = { ...prev, [key]: value }
      storage?.set(storeKey, next)
      noteTwin(storage, pair.pair_id)

      return next
    })
  }

  async function turnOn() {
    const res = await run('on', () => post(pairPath(pair.pair_id, 'switch'), { job_ids: chosen }), 'Could not switch those routines on')

    if (!res) {
      return
    }

    const ok = Array.isArray(res.resumed) ? res.resumed.length : 0
    const failed = Array.isArray(res.failed) ? res.failed : []

    if (failed.length) {
      host.notify({
        detail: clean(failed.map(f => f?.error).filter(Boolean).join('; '), 300) || undefined,
        kind: 'warning',
        message: `Switched on ${plural(ok, 'routine')}, ${failed.length} could not be switched on`,
        meta: 'Nothing else was changed. Try the rest again.'
      })
    } else {
      host.notify({
        action: { label: 'Pause routines', onClick: () => void undoMove(pair).catch(err => notifyProblem(err, 'Could not pause those routines')) },
        id: `${ID}:moved:${pair.pair_id}`,
        kind: 'success',
        message: `${owned(name)} ${ok === 1 ? 'routine now runs' : 'routines now run'} in Hermes`,
        meta: `${plural(ok, 'routine')} on. You can pause ${ok === 1 ? 'it' : 'them'} again from the twin's row or its "…" menu.`
      })
      onClose()
    }

    refreshAll()
  }

  async function pauseAgain() {
    const res = await run('undo', () => undoMove(pair), 'Could not pause those routines')

    if (res) {
      void q.refetch()
    }
  }

  const step = (n, title, hint, body) =>
    el(
      'section',
      { className: 'flex flex-col gap-1' },
      el('h3', { className: HEADING }, n ? `${n}. ${title}` : title),
      hint && el('div', { className: SMALL }, hint),
      body
    )

  const yourselfList = yourself.map((b, i) =>
    jsx(
      CheckRow,
      {
        checked: Boolean(ticks[`b:${b?.item ?? b?.id ?? i}`]),
        // where it is done, in Hermes' own words for the place
        detail: b?.side === 'grok' ? 'You do this in the Grok app.' : String(b?.item ?? '').startsWith('connector:') ? `You do this in Hermes: open ${name} there, then Capabilities → Connectors.` : 'You do this in Hermes.',
        id: `${ID}-b-${pair.pair_id}-${i}`,
        onChange: v => tick(`b:${b?.item ?? b?.id ?? i}`, v),
        title: b?.what || capital(itemPhrase(b?.item))
      },
      `b${i}`
    )
  )

  let body
  let footer

  if (!loaded) {
    body = q.isPending ? jsx(LinesSkeleton, { rows: 5 }) : jsx(InlineProblem, { error: q.error, onRetry: () => void q.refetch() })
    footer = [el(Button, { key: 'done', onClick: onClose, variant: 'ghost' }, 'Close')]
  } else if (blocked) {
    // Only what has to happen first, and the way to it: no steps to tick that cannot be finished yet.
    body = el('p', { className: cn(BODY, 'text-(--ui-text-primary)'), role: 'status' }, `${blocked} Then come back here.`)
    footer = [
      el(Button, { key: 'later', onClick: onClose, variant: 'ghost' }, 'Not now'),
      el(Button, { key: 'show', onClick: () => (onShow ? onShow() : onClose()) }, 'Show me')
    ]
  } else if (moved) {
    const n = running.length || Number(pair?.switched?.routines_on) || 0

    body = el(
      'div',
      { className: 'flex max-h-[55vh] flex-col gap-4 overflow-y-auto pr-1' },
      el(
        'div',
        { className: cn(NOTE, 'flex flex-wrap items-center gap-2') },
        el('span', { className: 'min-w-0 flex-1 text-(--ui-text-primary)' }, `${plural(n, 'routine')} on${movedOn ? ` since ${movedOn}` : ''}`),
        el(Button, { disabled: Boolean(busy), loading: busy === 'undo', onClick: () => void pauseAgain(), variant: 'secondary' }, 'Pause routines')
      ),
      running.length > 0 &&
        el(
          'ul',
          { 'aria-label': 'Routines running in Hermes', className: cn(BODY, 'flex flex-col gap-0.5') },
          running.map((j, i) => el('li', { key: j?.job_id ?? i }, jobName(j), j?.schedule ? ` · ${cronText(j.schedule)}` : ''))
        ),
      el('p', { className: SMALL }, 'Pausing switches these off in Hermes again. Switch them back on in Grok yourself if you want them to run there.'),
      yourself.length > 0 && step(0, 'Still yours to set up in Hermes', 'Grok cannot hand these over. Your own checklist.', yourselfList)
    )
    footer = [el(Button, { disabled: Boolean(busy), key: 'done', onClick: onClose }, 'Done')]
  } else if (nothing) {
    body = el(
      'div',
      { className: 'flex max-h-[55vh] flex-col gap-4 overflow-y-auto pr-1' },
      el('p', { className: BODY }, `${name} has no routines, so there is nothing to move. Its twin already works in Hermes.`),
      yourself.length > 0 && step(0, 'Set these up again in Hermes', 'Grok cannot hand these over. Your own checklist.', yourselfList)
    )
    footer = [el(Button, { key: 'done', onClick: onClose }, 'Done')]
  } else {
    const last = yourself.length > 0 ? 3 : 2
    const locked = routines.length > 0 && !routinesDone
    // Nothing arrived to switch on: no last step with an empty list, no button that can never be pressed.
    const none = paused.length === 0

    body = el(
      'div',
      { className: 'flex max-h-[55vh] flex-col gap-4 overflow-y-auto pr-1' },
      step(
        1,
        'Switch these off in Grok',
        routines.length ? `In the Grok app, open ${owned(name)} routines and switch each one off. Tick it here when it is off.` : null,
        routines.length
          ? routines.map((r, i) =>
              jsx(
                CheckRow,
                {
                  checked: Boolean(ticks[`r:${r?.name ?? i}`]),
                  id: `${ID}-r-${pair.pair_id}-${i}`,
                  onChange: v => tick(`r:${r?.name ?? i}`, v),
                  detail: r?.schedule ? cronText(r.schedule) : null,
                  title: r?.name
                },
                `r${i}`
              )
            )
          : el('p', { className: BODY }, 'Nothing is running in Grok for this bot.')
      ),
      yourself.length > 0 &&
        step(
          2,
          'Set these up again in Hermes',
          none ? 'No routine arrived to switch on: Grok keeps their schedules. Make each routine again in Hermes.' : 'Your own checklist. It does not hold up step 3.',
          yourselfList
        ),
      !none && step(
        last,
        'Switch these on in Hermes',
        locked
          ? el('span', { className: 'flex items-center gap-1.5 text-(--ui-text-primary)' }, jsx(Icon, { name: 'lock' }), 'Opens when step 1 is ticked.')
          : el(
              'span',
              { className: 'flex items-center gap-2' },
              el('span', { className: 'flex-1' }, 'These arrived switched off. The ones you switched off in Grok are ticked; untick any that should not run.'),
              paused.length > 1 &&
                el(
                  Button,
                  { onClick: () => setJobs(allJobs ? {} : Object.fromEntries(paused.map(j => [j?.job_id, true]))), variant: 'ghost' },
                  allJobs ? 'Clear' : 'Select all'
                )
            ),
        paused.map((j, i) =>
          jsx(
            CheckRow,
            {
              checked: !locked && Boolean(selected[j?.job_id]),
              detail: j?.schedule ? cronText(j.schedule) : null,
              disabled: locked,
              id: `${ID}-j-${pair.pair_id}-${i}`,
              onChange: v => setJobs(prev => ({ ...(prev ?? {}), [j?.job_id]: v })),
              title: jobName(j)
            },
            j?.job_id ?? i
          )
        )
      )
    )
    footer = none
      ? [el(Button, { key: 'done', onClick: onClose }, 'Done')]
      : [
          el(Button, { disabled: Boolean(busy), key: 'cancel', onClick: onClose, variant: 'ghost' }, 'Not now'),
          el(
            Button,
            { disabled: locked || chosen.length === 0, key: 'go', loading: busy === 'on', onClick: () => void turnOn() },
            chosen.length > 0 ? `Switch on ${plural(chosen.length, 'routine')}` : 'Switch on routines'
          )
        ]
  }

  return el(
    Sheet,
    {
      description: loaded && !blocked && !moved && !nothing ? `${ROUTINES_ARE} These steps, in this order, so none runs in both places.` : loaded && moved ? ROUTINES_ARE : undefined,
      footer,
      locked: Boolean(busy),
      onClose,
      open,
      title: loaded && moved ? `${owned(name)} routines run in Hermes` : loaded && nothing ? `${owned(name)} routines` : `Move ${owned(name)} routines to Hermes`
    },
    body
  )
}

// ── skill sync (optional) ───────────────────────────────────────────────────
// Words: the Grok-side bot that carries skills is "the sync bot"; the small file it needs is "its pass".

// The four steps, by name. Only one is on screen at a time while skill sync is being set up.
const SYNC_STEPS = ['Choose a shared folder', 'Give the sync bot its instructions', 'Send it the set-up folder', 'Give it a routine, then check']
// Said wherever a routine is asked for, and again once it is on: a run is never free.
const SYNC_CREDIT = "Every run of the sync bot's routine spends Grok credit, also when nothing changed."
const SYNC_TEXT_CAP = 40_000

const sentence = (value, max = 200) => {
  const t = clean(value, max)

  return t && !/[.!?]$/.test(t) ? `${t}.` : t
}

// What the backend says about skill sync for one twin, rebuilt from known fields only.
function syncStatusOf(raw) {
  if (!raw || typeof raw !== 'object') {
    return null
  }

  const box = raw.box && typeof raw.box === 'object' ? raw.box : {}

  return {
    box_note: sentence(box.note, 300),
    connected: raw.connected === true ? true : raw.connected === false ? false : null,
    credit_note: sentence(raw.credit_note),
    folder_name: clean(raw.folder_name, 80),
    heartbeat: sentence(raw.heartbeat),
    ran: typeof raw.last_run_at === 'string' && raw.last_run_at !== '',
    served: raw.served === true,
    set_up: raw.set_up === true
  }
}

// What this computer remembers of a set-up in progress: the three texts to paste, where the set-up folder
// is, which file is still missing. Never a pass, and nothing of what is in a file.
function syncGuideOf(raw) {
  if (!raw || typeof raw !== 'object' || !raw.texts || typeof raw.texts !== 'object') {
    return null
  }

  const t = key => (typeof raw.texts[key] === 'string' ? raw.texts[key].slice(0, SYNC_TEXT_CAP) : '')

  return {
    done: raw.done === true,
    folder_name: clean(raw.folder_name, 80),
    kit_folder: typeof raw.kit_folder === 'string' ? raw.kit_folder.slice(0, 1024) : '',
    kit_ready: raw.kit_ready !== false,
    missing: (Array.isArray(raw.missing) ? raw.missing : [])
      .slice(0, 4)
      .filter(m => m && typeof m === 'object')
      .map(m => ({ file_name: clean(m.file_name, 80), url: typeof m.url === 'string' && m.url.startsWith('https://') ? m.url.slice(0, 500) : '' })),
    texts: { install_message: t('install_message'), instructions: t('instructions'), routine_prompt: t('routine_prompt') },
    via: raw.via === 'inbox' ? 'inbox' : 'kit'
  }
}

// `initialConfirmOff` / `initialShowSteps` / `initialStep` / `initialGuide` / `initialStatus` / `initialAnswer` /
// `initialShown` let the smoke test render each state.
function SkillSyncSheet({ pair, open, onClose, storage, initialConfirmOff = false, initialShowSteps = false, initialStep = null, initialGuide = undefined, initialStatus = undefined, initialAnswer = '', initialShown = '' }) {
  const key = `mirror.${pair.pair_id}`
  const stepKey = `mirror-step.${pair.pair_id}`
  const sample = isSample(pair)
  const name = nameOf(pair)
  const on = skillSyncOn(pair)
  const [guide, setGuide] = useState(() => syncGuideOf(initialGuide !== undefined ? initialGuide : (storage?.get(key, null) ?? null)))
  const [at, setAt] = useState(() => Math.floor(Number(initialStep ?? storage?.get(stepKey, 1)) || 1))
  const [typing, setTyping] = useState(false)
  const [typed, setTyped] = useState('')
  const [shown, setShown] = useState(initialShown) // a text shown on the page, when the clipboard did not take it
  const [answer, setAnswer] = useState(initialAnswer) // what the last "Check" found
  const [address, setAddress] = useState('') // where the missing file is, when the browser did not open
  const [routineCopied, setRoutineCopied] = useState(false)
  const [stuck, setStuck] = useState(false) // adding through the shared folder did not work: choose a folder instead
  const [fresh, setFresh] = useState(null)
  const [confirmOff, setConfirmOff] = useState(initialConfirmOff)
  const [busy, run] = useBusy()
  // What the sync bot last reported, read from the shared folder by the backend (never asked of Grok).
  const q = useQuery({ enabled: open && !sample && initialStatus === undefined, queryFn: () => api(pairPath(pair.pair_id, 'skill-sync')), queryKey: [ID, 'skill-sync', pair.pair_id], refetchInterval: 60_000, retry: false })
  // (an answer this sheet fetched itself counts until the poll brings a newer one)
  const newest = fresh && !(Number(q.dataUpdatedAt) > fresh.at) ? fresh.data : q.data
  const status = syncStatusOf(initialStatus !== undefined ? initialStatus : newest)
  const typedText = typed.trim()
  // A finished note about a twin whose skill sync was since turned off elsewhere is not believed.
  const stale = Boolean(guide?.done) && !on && status?.connected === false
  const kept = stale ? null : guide
  const hasGuide = Boolean(kept)
  // botport calls a twin connected as soon as a folder is chosen: the steps stay until the sync bot has run.
  const finished = hasGuide ? kept.done : on
  // A shared folder is remembered on this computer (another twin uses it): this twin joins through it.
  const join = !sample && !hasGuide && !on && status?.set_up === true && !stuck
  // One step at a time: nothing past step 1 can be done before a folder is chosen.
  const current = hasGuide ? Math.min(4, Math.max(1, at)) : 1
  const credit = status?.credit_note || SYNC_CREDIT

  function go(n) {
    setAt(n)
    setAnswer('')
    setShown('')
    setAddress('')
    storage?.set(stepKey, n)
    noteTwin(storage, pair.pair_id)
  }

  function keep(next) {
    setGuide(next)
    storage?.set(key, next)
    noteTwin(storage, pair.pair_id)
  }

  // One call makes everything: the folder is used, the set-up folder is written, the texts come back. Sample
  // twins have no real sync bot: no folder is asked for, and the sample's texts stand in.
  async function setUp(body, failTitle, folderName = '') {
    const res = await run('connect', () => post(pairPath(pair.pair_id, 'skill-sync'), body), failTitle)

    if (!res) {
      return null
    }

    const kit = res.kit && typeof res.kit === 'object' ? res.kit : {}
    const inbox = res.via === 'inbox'
    const next = syncGuideOf({
      done: inbox,
      folder_name: sample ? '' : res.folder_name || folderName || guide?.folder_name || '',
      kit_folder: kit.folder,
      kit_ready: kit.ready !== false,
      missing: kit.missing,
      texts: res.texts && typeof res.texts === 'object' ? res.texts : {},
      via: res.via
    })

    keep(next)
    setTyping(false)
    setStuck(false)

    if (res.status && typeof res.status === 'object') {
      setFresh({ at: Date.now(), data: res.status })
    }

    refreshAll()

    return next
  }

  async function connect(folderPath, folderName) {
    const next = await setUp(sample ? {} : { folder: folderPath }, 'Could not use that folder', folderName || String(folderPath).split(/[\\/]/).filter(Boolean).pop() || '')

    if (next && !next.done) {
      go(2)
    }
  }

  // A later twin: the folder is known and the sync bot is running, so Hermes adds the twin through the folder.
  async function addThroughFolder() {
    const next = await setUp({}, `Could not add ${name}`)

    if (!next) {
      setStuck(true)
    } else if (next.done) {
      host.notify({ id: `${ID}:mirror:${pair.pair_id}`, kind: 'success', message: `${name} is added to skill sync`, meta: 'The sync bot picks it up on its next run; nothing to do in Grok.' })
    } else {
      go(2)
    }
  }

  async function chooseFolder() {
    if (typeof files?.requestFolder !== 'function') {
      setTyping(true)

      return
    }

    const got = await files.requestFolder()

    if (got?.ok === false) {
      if (got.error === 'unavailable') {
        setTyping(true)
      } else if (got.error !== 'protected') {
        // ('protected': the app itself already told the owner why that folder cannot be used)
        notifyProblem(new TwinError(plainText(got.message) || 'The folder picker could not open.', 'picker', 'Try again.'))
      }

      return
    }

    if (got?.folder?.path) {
      await connect(got.folder.path, got.folder.name)
    }
  }

  // The three texts go to the clipboard through the app's own door; one that did not get there is shown instead.
  async function copy(which, said, then) {
    const value = kept?.texts?.[which] ?? ''

    if (value && (await copyText(value))) {
      host.notify({ durationMs: TOAST_MS, id: `${ID}:mirror-copy`, kind: 'success', message: said })
      setShown('')

      if (which === 'routine_prompt') {
        setRoutineCopied(true)
      }

      if (then) {
        go(then)
      }
    } else {
      setShown(which)
    }
  }

  async function showFolder() {
    const ok = kept?.kit_folder && typeof os?.revealPath === 'function' ? await os.revealPath(kept.kit_folder) : false

    if (!ok) {
      notifyProblem(new TwinError('Hermes could not show that folder.', 'reveal', 'Choose the shared folder again; Hermes then makes the folder again and shows it.'))
    }
  }

  async function download() {
    const url = kept?.missing?.[0]?.url ?? ''
    const ok = url && typeof os?.openExternal === 'function' ? await os.openExternal(url) : false

    // The browser did not open: the address is shown instead.
    setAddress(ok ? '' : url)
  }

  async function lookAgain() {
    // (the set-up folder is made again: also for a twin that joined through the shared folder)
    const next = await setUp({ remake: true }, 'Could not look for the file')

    if (next) {
      keep({ ...next, done: false, via: 'kit' })
      setAddress('')
      setAnswer(next.kit_ready ? '' : 'Not there yet. Leave the download in your Downloads folder, then look again.')
    }
  }

  // The sync bot runs an older botport: the set-up folder is made again and step 3 opens with it.
  async function sendAgain() {
    const next = await setUp({ remake: true }, 'Could not make the set-up folder again')

    if (next) {
      keep({ ...next, done: false, via: 'kit' })
      go(3)
    }
  }

  async function check() {
    setAnswer('')

    await run(
      'check',
      async () => {
        await post('/sync', { pair_id: pair.pair_id })
        await freshStatus()

        let ran
        let waits = false

        if (sample) {
          // (the sample has no sync bot: it is on as soon as the sample says so)
          ran = skillSyncOn(cachedPairs().find(p => p?.pair_id === pair.pair_id) ?? pair)
        } else {
          const got = await api(pairPath(pair.pair_id, 'skill-sync'))

          setFresh({ at: Date.now(), data: got })
          // (a run counts for this twin once the sync bot serves it, not when it only ran for another twin)
          ran = syncStatusOf(got)?.served === true
          waits = !ran && syncStatusOf(got)?.ran === true
        }

        if (ran) {
          if (kept) {
            keep({ ...kept, done: true })
          }

          host.notify({ id: `${ID}:mirror:${pair.pair_id}`, kind: 'success', message: `Skill sync is on for ${name}`, meta: 'The sync bot has run. Skills now travel by themselves.' })
        } else {
          // said in the step, next to the button
          setAnswer(waits ? `The sync bot ran, but has not picked ${name} up yet. Press Check after its next run.` : "The sync bot has not run yet. Press Check after its routine's first run.")
        }

        refreshAll()
      },
      'Could not check'
    )
  }

  async function turnOff() {
    const res = await run('off', () => post(pairPath(pair.pair_id, 'mirror'), { off: true }), 'Could not turn off skill sync')

    if (res) {
      setGuide(null)
      setConfirmOff(false)
      setFresh(null)
      setAnswer('')
      setAddress('')
      storage?.remove?.(key)
      storage?.remove?.(stepKey)
      setAt(1)
      host.notify({ id: `${ID}:mirror:${pair.pair_id}`, kind: 'info', message: `Skill sync is off for ${name}`, meta: 'Skills are back to copy-yourself.' })
      refreshAll()
    }
  }

  const stateLine = finished ? `Skill sync is on for ${name}.` : hasGuide ? 'Started, but the sync bot has not run yet.' : 'Not set up yet.'
  const next = (n, variant = 'ghost') => el(Button, { disabled: Boolean(busy), key: 'next', onClick: () => go(n), variant }, 'Next step')
  let heading = SYNC_STEPS[current - 1]
  let text
  let actions

  if (join) {
    heading = `Add ${name}`
    text = `Skill sync already uses ${status.folder_name ? `"${status.folder_name}"` : 'a shared folder'} on this computer. Hermes adds ${name} through that folder; the sync bot picks it up on its next run.`
    actions = [el(Button, { disabled: Boolean(busy), key: 'add', loading: busy === 'connect', onClick: () => void addThroughFolder() }, `Add ${name} to skill sync`)]
  } else if (current === 1) {
    text = sample
      ? 'The sample needs no folder.'
      : hasGuide && kept.folder_name
        ? `Using "${kept.folder_name}". It stays on this computer; only your own sync app carries it anywhere.`
        : 'Pick a folder the sync bot can reach, such as one that Google Drive (or a similar app) keeps on this computer and in the cloud. Skills travel through it.'
    actions = sample
      ? [el(Button, { disabled: Boolean(busy), key: 'pick', loading: busy === 'connect', onClick: () => void connect('', '') }, 'Turn it on for the sample')]
      : [
          // a folder is chosen already: going on is the main thing to do here
          hasGuide && next(2, 'default'),
          el(Button, { disabled: Boolean(busy), key: 'pick', loading: busy === 'connect', onClick: () => void chooseFolder(), variant: hasGuide ? 'ghost' : 'default' }, hasGuide ? 'Choose another folder…' : 'Choose folder…'),
          typing &&
            el(
              'div',
              { className: 'flex min-w-0 flex-1 basis-full items-center gap-2', key: 'type' },
              jsx(Input, { 'aria-label': 'Where the folder is', maxLength: 1024, onChange: e => setTyped(e.target.value), placeholder: 'Paste where the folder is', size: 'sm', spellCheck: false, value: typed }),
              el(Button, { disabled: Boolean(busy) || typedText.length < 2, onClick: () => void connect(typedText, ''), variant: 'secondary' }, 'Use this folder')
            )
        ]
  } else if (current === 2) {
    text = 'In the Grok app, make a new bot to be the sync bot and paste these as its instructions. They tell it to look in the shared folder on a schedule and carry skills across.'
    actions = [
      el(Button, { disabled: Boolean(busy) || !kept.texts.instructions, key: 'copy', onClick: () => void copy('instructions', "The sync bot's instructions are copied", 3) }, 'Copy instructions'),
      shown === 'instructions' && next(3)
    ]
  } else if (current === 3) {
    if (sample) {
      text = "With a real twin you attach one folder of files in the sync bot's chat, the twin's pass among them. The sample has none."
      actions = [el(Button, { key: 'next', onClick: () => go(4) }, 'Next step')]
    } else if (!kept.kit_ready) {
      text = "One file is missing: botport for the sync bot's box. Download it, leave it in your Downloads folder, then look again."
      actions = [
        el(Button, { disabled: Boolean(busy) || !kept.missing[0]?.url, key: 'get', onClick: () => void download() }, 'Download it'),
        el(Button, { disabled: Boolean(busy), key: 'look', loading: busy === 'connect', onClick: () => void lookAgain(), variant: 'secondary' }, 'Look again')
      ]
    } else if (!kept.kit_folder) {
      // a step that cannot be done says why, and its button is the way out
      text = 'The set-up folder is not here any more. Choose the shared folder again to make a new one.'
      actions = [el(Button, { key: 'again', onClick: () => go(1) }, 'Choose the folder again')]
    } else {
      text = `Hermes made one folder with all the sync bot needs: its guide, botport for its box with checksums, and ${name}'s pass as a file. Attach every file in it to one message in the sync bot's chat, with the message Hermes wrote. Never paste what is in a file.`
      actions = [
        el(Button, { key: 'show', onClick: () => void showFolder() }, 'Show the folder'),
        el(Button, { disabled: Boolean(busy) || !kept.texts.install_message, key: 'copy', onClick: () => void copy('install_message', 'The message for the sync bot is copied', 4), variant: 'secondary' }, 'Copy the message'),
        shown === 'install_message' && next(4)
      ]
    }
  } else {
    text = sample
      ? 'With a real twin you give the sync bot a routine, and every run of it spends Grok credit. Then you press Check here.'
      : 'In the Grok app, give the sync bot a routine with this as its prompt. Every run spends Grok credit, also when nothing changed, so once an hour is plenty. After its first run, press Check.'
    actions = [
      !sample && el(Button, { disabled: Boolean(busy) || !kept.texts.routine_prompt, key: 'copy', onClick: () => void copy('routine_prompt', 'The routine prompt is copied'), variant: routineCopied ? 'ghost' : 'default' }, 'Copy routine prompt'),
      el(Button, { disabled: Boolean(busy), key: 'check', loading: busy === 'check', onClick: () => void check(), variant: sample || routineCopied ? 'default' : 'secondary' }, 'Check')
    ]
  }

  const stepper = el(
    'section',
    { 'aria-label': join ? 'Add to skill sync' : `Step ${current} of 4`, className: cn(CARD, 'flex flex-col gap-1.5') },
    !join && el('span', { className: SMALL }, `Step ${current} of 4`),
    el('h3', { className: HEADING }, heading),
    el('p', { className: BODY }, text),
    !join && shown && kept?.texts?.[shown] && el('pre', { className: cn(PRE, 'max-h-40 select-all'), tabIndex: 0 }, kept.texts[shown]),
    !join && shown && kept?.texts?.[shown] && el('p', { className: SMALL }, 'Hermes could not reach the clipboard. Select the text above and copy it.'),
    address && el('pre', { className: cn(PRE, 'select-all'), tabIndex: 0 }, address),
    address && el('p', { className: SMALL }, 'Your browser did not open. Copy this address into it.'),
    answer && el('p', { className: 'text-xs text-(--ui-text-primary)', role: 'status' }, answer),
    el(
      'div',
      { className: 'flex flex-wrap items-center gap-2' },
      ...actions.filter(Boolean),
      !join && current > 1 && el(Button, { disabled: Boolean(busy), key: 'back', onClick: () => go(current - 1), variant: 'text' }, 'Back')
    )
  )

  // Once it is on, the steps are history: four ticked lines, folded away.
  const history = el(
    'ul',
    { 'aria-label': 'Set-up steps', className: cn(BODY, 'flex flex-col gap-1') },
    (sample
      ? // the sample is switched on from the start: what a real twin's four steps are, one line each
        [
          'You choose a folder the sync bot can reach, such as one Google Drive keeps on this computer and in the cloud.',
          'A second bot in Grok, the sync bot, gets instructions that Hermes writes for you.',
          "You attach one folder of files in the sync bot's chat, the twin's pass among them.",
          'You give the sync bot a routine (every run spends Grok credit), then press Check here.'
        ]
      : [
          (hasGuide && kept.folder_name) || status?.folder_name ? `Shared folder: "${(hasGuide && kept.folder_name) || status.folder_name}".` : 'A shared folder is chosen.',
          'The sync bot has its instructions.',
          'The sync bot has the set-up folder.',
          'The sync bot has its routine.'
        ]
    ).map((line, i) => el('li', { className: 'flex items-center gap-2', key: i }, jsx(Icon, { className: 'text-(--ui-text-secondary)', name: 'pass-filled' }), el('span', {}, line), el('span', { className: 'sr-only' }, '(done)')))
  )

  // On: when the sync bot last ran, what it needs, what a run costs; the steps folded away.
  const added = hasGuide && kept.via === 'inbox' && !status?.served
  const running = [
    added && el('p', { className: BODY, key: 'added', role: 'status' }, `Added. The sync bot picks ${name} up on its next run; nothing to do in Grok.`),
    !sample && !added && status?.heartbeat && el('p', { className: BODY, key: 'beat', role: 'status' }, status.heartbeat),
    !sample &&
      status?.box_note &&
      el(
        'div',
        { className: cn(NOTE, 'flex flex-col gap-2'), key: 'box', role: 'alert' },
        el('span', {}, status.box_note),
        el('div', {}, el(Button, { disabled: Boolean(busy), loading: busy === 'connect', onClick: () => void sendAgain(), variant: 'secondary' }, 'Send the set-up folder again…'))
      ),
    // on, but no sync bot has ever run (set up in the terminal, or this computer forgot the steps): the way back in
    !sample && status && !status.box_note && !status.ran && !added && el('div', { key: 'again' }, el(Button, { disabled: Boolean(busy), loading: busy === 'connect', onClick: () => void sendAgain(), variant: 'secondary' }, 'Send the set-up folder again…')),
    !sample && el('p', { className: SMALL, key: 'credit' }, credit),
    el(Fold, { initialOpen: initialShowSteps, key: 'steps', label: sample ? 'The four steps for a real twin' : 'Set-up steps', openLabel: sample ? 'Hide the steps' : 'Hide the set-up steps' }, history)
  ]

  return el(
    Sheet,
    {
      description: 'Optional. Skills then travel between Grok and Hermes by themselves, through a small sync bot in your own Grok account. Every run of that bot spends Grok credit.',
      footer: [
        (on || hasGuide) &&
          !confirmOff &&
          el(Button, { className: 'mr-auto', disabled: Boolean(busy), key: 'off', onClick: () => setConfirmOff(true), variant: 'text' }, finished ? 'Turn off skill sync…' : 'Cancel the set-up…'),
        // "Done" only when it is done
        el(Button, { disabled: Boolean(busy), key: 'done', onClick: onClose, variant: finished ? 'default' : 'ghost' }, finished ? 'Done' : 'Close')
      ],
      locked: Boolean(busy),
      onClose,
      open,
      title: finished ? 'Skill sync' : 'Set up skill sync',
      wide: true
    },
    el(
      'div',
      { className: 'flex max-h-[60vh] flex-col gap-2.5 overflow-y-auto pr-1' },
      el(
        'p',
        { className: cn(BODY, 'flex items-center gap-1.5 text-(--ui-text-primary)'), role: 'status' },
        jsx(Icon, { className: finished ? 'text-(--ui-text-secondary)' : 'text-(--ui-text-tertiary)', name: finished ? 'pass-filled' : 'circle-large-outline' }),
        stateLine
      ),
      sample && el('div', { className: NOTE }, finished ? 'This is the sample, so nothing is really connected. The steps below are the ones you would follow with a real twin.' : 'This is the sample, so nothing is really connected and it needs no folder.'),
      confirmOff &&
        el(
          'div',
          { className: cn(NOTE, 'flex flex-col gap-2'), role: 'alert' },
          el('span', { className: 'text-(--ui-text-primary)' }, finished ? 'Turn off skill sync?' : 'Cancel the set-up?'),
          el('span', {}, 'Skills go back to copy-yourself. The pass is replaced, so the sync bot must be set up again to turn this back on.'),
          el(
            'div',
            { className: 'flex flex-wrap gap-2' },
            el(Button, { disabled: Boolean(busy), loading: busy === 'off', onClick: () => void turnOff(), variant: 'secondary' }, finished ? 'Turn off skill sync' : 'Cancel the set-up'),
            el(Button, { disabled: Boolean(busy), onClick: () => setConfirmOff(false), variant: 'ghost' }, finished ? 'Cancel' : 'Keep going')
          )
        ),
      ...(finished ? running.filter(Boolean) : [stepper])
    )
  )
}

// ── stop twinning ───────────────────────────────────────────────────────────

const STOP_STAYS = ['Your bot in Grok, unchanged.', 'Its Hermes bot and all its chats.']
const STOP_GOES = ['Following Grok.', 'The earlier versions kept for undo.']

// One noun for what stays ("Hermes bot"), whatever an older backend still calls it, and the plain
// truth about botport: Hermes removes its own copy, never the owner's download.
// "Link" is the word for computers: what ends between a Grok bot and its twin is "following Grok", and what
// Remove counts are twins.
const reword = line =>
  line
    .replace(/^The link between them\.$/, 'Following Grok.')
    .replace(/^Every twin link\.$/, 'Every twin.')
    .replace(/^(\d+) twin links?\b/, (_, n) => plural(Number(n), 'twin'))
    .replace(/\bHermes copies\b/g, 'Hermes bots')
    .replace(/\bHermes copy\b/g, 'Hermes bot')
    .replace(/^The botport (helper )?copy\.$|^Hermes' copy of botport \(your download stays in Downloads\)\.$/, "Hermes' copy of botport, not your download.")
    .replace(/\bhelper(?= bot\b)/g, 'sync')

// The lines a leaving dialog shows. A line about small internal files ("key files") is not for this
// list: they are still removed, and still listed behind "Technical details".
const lines = (given, fallback) => (Array.isArray(given) ? given.slice(0, 8).map(l => reword(clean(l, 200))).filter(l => l && !/key files?\b/i.test(l)) : fallback)

// `onStopped`: the page shows the result as a line above the rows (where the Hermes bot is now, the button
// that opens it, and what is left to do by hand in Grok); without it a toast says the same.
function StopTwinningDialog({ pair, open, onClose, onStopped, storage }) {
  const name = nameOf(pair)
  const profile = clean(pair?.hermes?.profile || '', 64)
  const plan = useQuery({
    enabled: open,
    gcTime: 0,
    queryFn: () => post(pairPath(pair.pair_id, 'remove'), { dry_run: true }),
    queryKey: [ID, 'remove-plan', pair.pair_id],
    retry: false
  })
  const removed = Array.isArray(plan.data?.removed) ? plan.data.removed : []
  const goes = lines(plan.data?.goes, STOP_GOES)
  const given = lines(plan.data?.stays, STOP_STAYS)
  // What keeps running is said, when something does: routines switched on in Hermes are Hermes' own from then on.
  const routinesOn = Number(pair?.switched?.routines_on) || 0
  const stays = routinesOn > 0 && !given.some(l => /routine/i.test(l)) ? [...given, `Its ${plural(routinesOn, 'routine')} ${routinesOn === 1 ? 'keeps' : 'keep'} running in Hermes.`] : given
  const withSync = plan.data?.skill_sync === true || (plan.data?.skill_sync === undefined && skillSyncOn(pair))
  // Shared with other computers: its copies there go too (kept there, as everywhere), and the list says so
  // unless the backend's own lines already do.
  // (only computers that have a copy; "when it is next on" only for one that is not in touch right now)
  const there = copiesOf(pair?.fleet?.machines).filter(c => c.state !== 'pending' && !c.error)
  const copies = there.map(c => c.name)
  const off = there.filter(c => c.state === 'offline' || c.state === 'never_synced').map(c => c.name)
  const later = off.length === 0 ? '' : off.length === copies.length ? `, when ${off.length === 1 ? 'it is' : 'each is'} next on` : `; on ${listWords(off)} when ${off.length === 1 ? 'it is' : 'each is'} next on`
  const away = copies.length > 0 && !goes.some(l => /computer|copies on/i.test(l)) ? [`Its copies on ${listWords(copies)}${later}. Kept there ${KEPT_DAYS} days, with chats.`] : []
  // The backend's own line says "when it is next on" of every linked computer: not of one that is on now.
  const timed = l => (off.length === 0 && there.length > 0 ? l.replace(/^(Its copies on [^.]{1,200}?), when (?:it|each) is next on\./, '$1.') : l)
  // Following and what was kept for undo go together: one line, not two.
  const gone = (goes.length ? goes : STOP_GOES).map(timed)
  const merged = gone.some(l => /^Following Grok\.$/.test(l)) && gone.some(l => /^The earlier versions kept for undo\.$/.test(l)) ? ['Following Grok, and its earlier versions.', ...gone.filter(l => !/^Following Grok\.$|^The earlier versions kept for undo\.$/.test(l))] : gone
  // What twinning again does (it picks up the same Hermes bot) is said in the result line, where there is room.
  const again = plainText(plan.data?.again, 200)

  return jsx(Confirm, {
    busyLabel: 'Stopping…',
    cancelLabel: 'Cancel',
    children: plan.isPending
      ? jsx(LinesSkeleton, { rows: 3 })
      : plan.isError
        ? jsx(InlineProblem, { error: hereProblem(plan.error), onRetry: () => void plan.refetch() })
        : el(
            'div',
            { className: 'flex flex-col gap-2.5' },
            jsx(StaysGoes, { goes: [...merged, ...away], stays }),
            // (with skill sync on, one thing is left to do in Grok: the result line brings it complete, with Copy)
            el('p', { className: BODY }, 'You can twin it again later.'),
            // why it cannot be done just now, when the backend says so (botport is missing: its button is on the page)
            plainText(plan.data?.note, 200) && el('p', { className: cn(BODY, 'text-(--ui-text-primary)'), role: 'status' }, plainText(plan.data.note, 200)),
            removed.length > 0 &&
              el(
                Fold,
                { label: 'Technical details', openLabel: 'Hide technical details' },
                el(
                  'ul',
                  { className: cn(PRE, 'max-h-40') },
                  removed.slice(0, 40).map((r, i) => el('li', { className: 'truncate', key: i, title: clean(r, 300) }, clean(r, 160))),
                  removed.length > 40 && el('li', { key: 'more' }, `and ${removed.length - 40} more`)
                )
              )
          ),
    confirmLabel: `Stop twinning ${name}`,
    destructive: true,
    onClose,
    onConfirm: async () => {
      if (!plan.data) {
        // (never "one moment" for a plan that failed: nothing is on its way then)
        throw new Error(plan.isError ? 'Hermes could not work out what will be removed, so nothing was stopped. Press Cancel and see the note at the top of the page.' : 'One moment: still working out what will be removed.')
      }

      await post(pairPath(pair.pair_id, 'remove'), {})
      // what the page remembered about this twin goes with it
      forgetTwin(storage, pair.pair_id)
      settleConflictToast(pair.pair_id)
      // The row is gone after this, and it was the way into the bot: the result says where it is now.
      const message = `${name} is no longer twinned.`
      // (the toast names where Hermes lists the bot; the line on the page has the button that opens it instead)
      const where = onStopped ? 'Its Hermes bot is still there, with all its chats.' : 'Its Hermes bot is still there, with all its chats: you find it among your profiles in Hermes.'
      const meta = [where, copies.length > 0 && `Its copies on your other computers are kept there for ${KEPT_DAYS} days.`, again && !/starts fresh/i.test(again) && reword(again) !== 'You can twin it again later.' && reword(again)].filter(Boolean).join(' ')

      if (onStopped) {
        // what to say to the sync bot, when skill sync was on: by-hand work comes complete
        onStopped({ forget: withSync ? `Forget ${name}. Stop carrying its skills.` : '', message, meta, name, profile })
      } else {
        host.notify({ action: profile ? { label: 'Open in Hermes', onClick: () => openInHermes(profile) } : undefined, durationMs: WARNING_TOAST_MS, id: `${ID}:removed:${pair.pair_id}`, kind: 'success', message, meta })
      }

      refreshAll()
    },
    open,
    title: `Stop twinning ${name}?`
  })
}

// The sample has one exit, under one name, wherever it is pressed. Nothing is asked first: only invented
// data goes, and the result line offers the sample again.
async function endSample(pairs, again, storage) {
  await post('/demo', { action: 'remove' })
  listOf(pairs)
    .filter(isSample)
    .forEach(p => {
      // what the page remembered about the sample goes with it
      forgetTwin(storage, p.pair_id)
      settleConflictToast(p.pair_id)
    })
  host.notify({ action: again ? { label: 'Show it again', onClick: again } : undefined, durationMs: WARNING_TOAST_MS, id: `${ID}:sample`, kind: 'info', message: 'Sample removed', meta: 'Nothing of yours was changed.' })
  refreshAll()
}

// What became of a twin that was stopped, as a line on the page (not a toast over the header): where its
// Hermes bot is, the button that opens it, and, when skill sync was on, the one thing left to do in Grok
// with its text ready to copy. It stays until the owner closes it.
function StoppedLine({ result, onClose, initialCopied = false, initialShown = false }) {
  const [copied, setCopied] = useState(initialCopied)
  const [shown, setShown] = useState(initialShown) // the clipboard refused: the text itself, with its own Copy

  async function copy() {
    if (await copyText(result.forget)) {
      setCopied(true)
    } else {
      setShown(true)
    }
  }

  return el(
    'div',
    { 'aria-label': result.message, className: cn(NOTE, 'flex flex-col gap-2'), role: 'status' },
    el(
      'div',
      { className: 'flex flex-wrap items-center gap-2' },
      jsx(Icon, { className: 'text-(--ui-text-tertiary)', name: 'pass' }),
      el('span', { className: 'min-w-0 flex-1' }, el('span', { className: 'text-(--ui-text-primary)' }, result.message), ' ', result.meta),
      result.profile && el(Button, { 'aria-label': `Open ${result.name} in Hermes`, onClick: () => openInHermes(result.profile), variant: 'secondary' }, 'Open in Hermes'),
      !result.forget && el(Button, { onClick: onClose, variant: 'text' }, 'OK')
    ),
    result.forget &&
      el(
        'div',
        { className: 'flex flex-wrap items-center gap-2' },
        el('span', { className: 'min-w-0 flex-1' }, copied ? "Copied. Paste it into the sync bot's chat in the Grok app." : "One thing is left in Grok: tell the sync bot to forget this bot. Copy what to say, then paste it into the sync bot's chat in the Grok app."),
        shown ? jsx(CopyButton, { appearance: 'button', buttonVariant: 'secondary', label: 'Copy', showLabel: true, text: result.forget }) : el(Button, { onClick: () => void copy(), variant: 'secondary' }, 'Copy what to say'),
        el(Button, { onClick: onClose, variant: 'text' }, 'Done')
      ),
    result.forget && shown && el('pre', { className: PRE }, result.forget)
  )
}

// ── one twin: one sentence, at most one button ──────────────────────────────

function openInHermes(profile) {
  if (typeof host.newChat === 'function') {
    host.newChat(profile)
  } else {
    host.navigate('/')
  }
}

const DAY_MS = 24 * 60 * 60 * 1000

// The point of a twin, shown on the sample: the bot as Grok has it, the twin as Hermes has it, and one
// button that changes the Grok side so the Hermes side can be watched following. Read-only otherwise.
// `initialPhase` lets the smoke test render the moment after the twin followed.
function SeeTwinSheet({ pair, open, onClose, onEndSample, intervalS, fastCheck, initialPhase = 'idle' }) {
  const [busy, run] = useBusy()
  const [phase, setPhase] = useState(initialPhase) // 'idle' | 'changed' | 'followed'
  const name = nameOf(pair)
  const grokText = clean(pair?.grok?.description, 400)
  const hermesName = clean(pair?.hermes?.name || name, 60)
  const hermesText = clean(pair?.hermes?.description, 400)
  const same = hermesName === name && hermesText === grokText

  async function change() {
    await run(
      'change',
      async () => {
        await post('/demo', { action: 'grok-edit' })
        await freshStatus()
        setPhase('changed')
        // The twin notices by itself; the pause lets the difference be seen first.
        await pause(2_500)

        try {
          await post('/sync', { pair_id: pair.pair_id })
          noteChecked([pair.pair_id])
        } catch {
          // the background check picks it up at its next round
        }

        await freshStatus()
        setPhase('followed')
        refreshAll()
      },
      'That did not work'
    )
  }

  const card = (title, who, text, tag) =>
    el(
      'div',
      { 'aria-label': title, className: 'flex min-w-0 flex-col gap-2 rounded-md border border-(--ui-stroke-tertiary) p-3', role: 'group' },
      el('div', { className: 'flex items-center gap-2' }, el('span', { className: cn(HEADING, 'flex-1') }, title), tag && jsx(Tag, { children: tag })),
      el('div', { className: 'flex min-w-0 items-center gap-2' }, jsx(Avatar, { name: who }), jsx(Truncate, { className: 'text-sm font-semibold text-(--ui-text-primary)', max: 60, text: who })),
      el('p', { className: cn(BODY, 'break-words') }, text || 'No description.')
    )

  // The first view is the two cards and the one button, nothing to read first (the cards' titles say which side
  // is which, the button says what to do). What it meant is said once the change has been played.
  const said = !same
    ? 'The bot changed in Grok. Its twin notices by itself…'
    : phase === 'followed'
      ? `The twin followed. With your own bots this happens by itself, ${speedText(intervalS, fastCheck)}.`
      : ''

  const seen = same && phase === 'followed'

  return el(
    Sheet,
    {
      description: 'Invented data.',
      footer: [
        onEndSample &&
          el(
            Button,
            {
              disabled: Boolean(busy),
              key: 'end',
              onClick: () => {
                onClose()
                onEndSample()
              },
              // once the twin has followed, the sample has shown what it is for: leaving is the next act
              variant: seen ? 'default' : 'ghost'
            },
            'End sample'
          ),
        el(Button, { disabled: Boolean(busy), key: 'done', onClick: onClose, variant: seen && !onEndSample ? 'default' : 'ghost' }, 'Done')
      ].filter(Boolean),
      locked: Boolean(busy),
      onClose,
      open,
      title: `${name} and its twin`,
      wide: true
    },
    el('div', { className: 'grid gap-3 sm:grid-cols-2' }, card('In Grok', name, grokText, !same && 'Changed'), card('In Hermes', hermesName, hermesText, same && phase === 'followed' && 'Followed')),
    // (the place where the result is said is there from the start, so a screen reader hears it arrive)
    el('p', { className: said ? cn(BODY, 'text-(--ui-text-primary)') : 'sr-only', role: 'status' }, said),
    el('div', {}, el(Button, { disabled: Boolean(busy), loading: busy === 'change', onClick: () => void change(), variant: seen ? 'outline' : 'default' }, phase === 'idle' ? 'Change the bot in Grok' : 'Change it again'))
  )
}

// botport is gone and a dialog of this page cannot load: the backend's advice for that is written for someone
// who is not on the Twins page ("Open the Twins page … press Connect my Grok bots … or run a command"). Here
// the page's own banner has the button, under its own name; the dialog names that one.
function hereProblem(err) {
  const e = asProblem(err)

  return e.code === 'botport_missing' ? new TwinError(e.message, e.code, 'Press Cancel, then Get botport again… at the top of the page.', e.detail) : e
}

// `lead`: this row carries the page's one primary button (in its first box when that box has a main
// action, else on its own button). `held`: twins cannot follow Grok right now (the banner above says why,
// once). `request`: the page asks this row to open one of its sheets.
// `folded`: another row above already shows its open matter as a box; this row's matters wait as one line
// each until one is opened (the page shows one box at a time unless the owner opens more).
// `plain`: something above the rows needs the owner (a request waits for an answer): a row then carries its
// state only, and where its copies are stays in the list of computers and in the hover text.
// `showWhere`: false when every twin is on every linked computer (the list of computers says so once).
// `fleetNow`: whether sharing is on and each linked computer's state, for a copy that is still to come.
function TwinRow({ pair, storage, lastCheckAt, intervalS, fastCheck, held: pageHeld = false, grouped = false, lead = false, folded = false, plain = false, showWhere = true, fleetNow = null, request = null, onRequestDone, onStopped, example = '', onEndSample }) {
  const [busy, run] = useBusy()
  const [sheet, setSheet] = useState(null) // 'details' | 'sync' | 'move' | 'stop' | 'finish' | 'import' | 'see' | null
  const [tab, setTab] = useState('copies')
  const [openId, setOpenId] = useState(null)
  const waitKey = `kept.${pair.pair_id}`
  const [waitingBack, setWaitingBack] = useState(() => storage?.get(waitKey, false) === true)
  const name = nameOf(pair)
  const sample = isSample(pair)
  const held = pageHeld && !sample
  const profile = clean(pair?.hermes?.profile || '', 64)
  const gone = mattersOf(pair).some(m => m.kind === 'missing')
  // While nothing can follow Grok, a row does not repeat the banner in a box of its own.
  // `grouped`: several twins cannot be checked for one reason, and the banner says it once.
  const matters = mattersOf(pair).filter(m => !(m.kind === 'missing' && waitingBack) && !(m.kind === 'error' && (held || grouped)))
  const openMatter = matters.find(m => m.id === openId) ?? (folded ? null : matters[0]) ?? null
  const left = leftOf(pair) ?? []
  const needModel = left.includes('model')
  const asked = !sample && needModel && wasAsked(storage, pair.pair_id)
  const close = () => setSheet(null)
  const moved = Boolean(pair?.switched?.at)
  const movedLately = moved && Date.now() - Date.parse(pair.switched.at) < DAY_MS
  const quietRow = matters.length === 0
  const checkedAt = checkedIso(pair, lastCheckAt)
  const when = quietRow && !held ? ago(checkedAt) : null
  // The sample can show both sides of itself once the backend sends them.
  const canSee = sample && typeof pair?.grok?.description === 'string' && typeof pair?.hermes?.description === 'string'
  const details = which => {
    setTab(which)
    setSheet('details')
  }

  useEffect(() => {
    if (request && request.pairId === pair.pair_id) {
      // (the sample's own sheet needs both sides of the sample: without them the row is the answer)
      if (request.sheet !== 'see' || canSee) {
        setSheet(request.sheet)
      }

      onRequestDone?.()
    }
  }, [request])

  // The model was chosen, or the bot is back in Grok: what was remembered about waiting has had its day.
  useEffect(() => {
    if (!needModel && wasAsked(storage, pair.pair_id)) {
      storage?.remove?.(askedKey(pair.pair_id))
    }

    if (!gone && waitingBack) {
      storage?.remove?.(waitKey)
      setWaitingBack(false)
    }
  }, [needModel, gone])

  const check = () => void run('check', () => checkAndToast({ pair_id: pair.pair_id }), `Could not check ${name}`)
  const pauseRoutines = () => void run('undo', () => undoMove(pair), 'Could not pause those routines')
  const item = (id, label, onSelect) => el(DropdownMenuItem, { key: id, onSelect }, label)
  const sentence = busy === 'check' ? 'Checking…' : gone && waitingBack && !held ? `${STATE.grok_missing.label} · waiting for it to come back` : statusSentence(pair, { asked, held })

  const first = matters[0] ?? null
  const leadInBox = lead && first !== null && hasMainAction(first, pair)
  const leadHere = lead && !leadInBox
  const finishLabel = `Finish setting up ${name}`

  // The row's one button: only when no matter is open (an open matter has its action in its own box).
  let action = null
  let finishInRow = false

  if (quietRow && canSee) {
    action = el(Button, { 'aria-label': `See ${name} and its twin`, onClick: () => setSheet('see'), variant: mainVariant(leadHere) }, 'See the twin')
  } else if (quietRow && asked && left.length === 1) {
    finishInRow = true
    action = el(Button, { 'aria-label': `Open ${name} in Hermes again to choose a model`, onClick: () => askForModel(storage, pair), variant: mainVariant(leadHere) }, 'Open it again')
  } else if (left.length > 0 && (quietRow || !sampleShown(pair))) {
    // (also with a matter open below: the button slot always holds the row's own next act, and the twin is
    // opened from the line under its name, as on every row with set-up left)
    finishInRow = true
    action = el(Button, { 'aria-label': finishLabel, onClick: () => setSheet('finish'), variant: quietRow ? mainVariant(leadHere) : leadHere ? 'default' : 'secondary' }, 'Finish setting up')
  } else if (!sample && profile) {
    // A twin can always be opened: calm, not following, or with something open (its fix is in the box below).
    action = el(Button, { 'aria-label': `Open ${name} in Hermes`, onClick: () => openInHermes(profile), variant: 'secondary' }, 'Open in Hermes')
  }

  const box = m => {
    const mine = leadInBox && m === first

    if (m.kind === 'error') return jsx(ErrorBox, { lead: mine, pair }, m.id)
    if (m.kind === 'missing') {
      return jsx(
        MissingBox,
        {
          lead: mine,
          onStop: () => setSheet('stop'),
          onWait: () => {
            setWaitingBack(true)
            storage?.set(waitKey, true)
            noteTwin(storage, pair.pair_id)
          },
          pair
        },
        m.id
      )
    }
    if (m.kind === 'conflict') return jsx(ConflictBox, { lead: mine, onVersions: () => details('versions'), pair }, m.id)
    if (m.kind === 'secret') return jsx(SecretBox, { lead: mine, pair, place: m }, m.id)
    if (m.kind === 'waiting') return jsx(WaitingBox, { lead: mine, onSkillSync: () => setSheet('sync'), pair, waiting: m.waiting }, m.id)
    if (m.kind === 'unsent') return jsx(UnsentBox, { count: m.count, lead: mine, onSkillSync: () => setSheet('sync'), pair }, m.id)

    return el('span', { key: m.id })
  }

  return el(
    'article',
    { 'aria-label': `${name} twin`, className: cn(CARD, 'flex flex-col gap-3') },
    el(
      'header',
      { className: 'flex min-w-0 items-center gap-3' },
      jsx(Avatar, { name }),
      el(
        'div',
        { className: 'flex min-w-0 flex-1 flex-col gap-0.5' },
        el(
          'div',
          { className: 'flex min-w-0 items-center gap-2' },
          jsx(Truncate, { className: 'text-[0.9375rem] font-semibold text-(--ui-text-primary)', max: 60, text: name }),
          sample && jsx(Tag, { children: 'Sample' })
        ),
        el(
          'div',
          { className: cn('flex min-w-0 flex-wrap items-center gap-x-3', BODY) },
          // The shape and its sentence stay together; only what follows may go to the next line.
          el('span', { className: 'inline-flex min-w-0 items-center gap-1.5', title: when ? `Checked ${when}` : undefined }, jsx(StateIcon, { tone: grouped && !held ? 'muted' : rowTone(pair, { held }) }), el('span', { className: 'min-w-0' }, sentence)),
          // where its other copies are, as the Mac's own list has it
          // (not while a banner above the rows has the owner's attention, unless a copy is in trouble)
          // (and not when every twin is on every linked computer: the list of computers below says so once)
          !sample && ((showWhere && !(held || plain)) || WHERE_NEWS.test(alsoOn(pair?.fleet?.machines, '', fleetNow))) && jsx(WhereLine, { detail: copiesDetail(pair?.fleet?.machines), text: alsoOn(pair?.fleet?.machines, '', fleetNow) }),
          // A twin with set-up left can be opened in one step too: quietly, on the line that wraps in a narrow
          // window (next to the name it would squeeze the name).
          finishInRow && !sample && profile && el(Button, { 'aria-label': `Open ${name} in Hermes`, onClick: () => openInHermes(profile), variant: 'text' }, 'Open in Hermes'),
          movedLately &&
            quietRow &&
            !held &&
            el(
              Button,
              { 'aria-label': `Pause ${owned(name)} routines in Hermes again`, disabled: Boolean(busy), loading: busy === 'undo', onClick: pauseRoutines, variant: 'text' },
              'Pause routines'
            )
        )
      ),
      action,
      el(
        DropdownMenu,
        {},
        el(DropdownMenuTrigger, { asChild: true }, el(Button, { 'aria-label': `More for ${name}`, size: 'icon-sm', variant: 'ghost' }, jsx(Icon, { name: 'ellipsis' }))),
        el(
          DropdownMenuContent,
          { align: 'end' },
          // also here, for the rows whose one button is something else (set-up to finish)
          !sample && profile && item('open', 'Open in Hermes', () => openInHermes(profile)),
          item('check', 'Check now', check),
          item('details', 'Details…', () => details('copies')),
          // (the way back from any replacement, two clicks from the row, once there is a version to go back to)
          !sample && Number(pair?.restore_points) > 0 && item('versions', 'Earlier versions…', () => details('versions')),
          left.length > 0 && !(finishInRow && !asked) && item('finish', sampleShown(pair) ? 'Try finishing its set-up…' : 'Finish setting up…', () => setSheet('finish')),
          // (a real bot's own file: its instructions, routines and connector names in one go)
          !sample && item('import', 'Import setup export…', () => setSheet('import')),
          jsx(DropdownMenuSeparator, {}, 's1'),
          item('sync', skillSyncOn(pair) ? 'Skill sync… (on)' : 'Skill sync…', () => setSheet('sync')),
          canMove(pair) && item('move', moved ? 'Routines in Hermes…' : 'Move routines to Hermes…', () => setSheet('move')),
          moved && !movedLately && item('pause', 'Pause routines', pauseRoutines),
          jsx(DropdownMenuSeparator, {}, 's2'),
          sample ? item('stop', 'End sample', () => onEndSample?.()) : item('stop', 'Stop twinning…', () => setSheet('stop'))
        )
      )
    ),
    // The first open matter is a box; the others wait as one-line headers.
    matters.map(m => (m === openMatter ? box(m) : jsx(MatterHeader, { matter: m, onOpen: () => setOpenId(m.id) }, m.id))),
    sheet === 'details' &&
      jsx(DetailsSheet, {
        checkedAt,
        fastCheck,
        initialTab: tab,
        intervalS,
        onClose: close,
        onFinish: () => setSheet('finish'),
        // closes the sheet at the box it is about
        onShow: id => {
          close()

          if (id) {
            setOpenId(id)
          }
        },
        onSkillSync: () => setSheet('sync'),
        open: true,
        pair
      }),
    sheet === 'see' && jsx(SeeTwinSheet, { fastCheck, intervalS, onClose: close, onEndSample, open: true, pair }),
    sheet === 'finish' && jsx(FinishSheet, { example, onClose: close, onImport: () => setSheet('import'), open: true, pair, storage }),
    sheet === 'import' && !sample && jsx(ImportSheet, { onClose: close, onMove: () => setSheet('move'), open: true, pair }),
    sheet === 'sync' && jsx(SkillSyncSheet, { onClose: close, open: true, pair, storage }),
    sheet === 'move' && jsx(MoveSheet, { onClose: close, onShow: close, open: true, pair, storage }),
    sheet === 'stop' && !sample && jsx(StopTwinningDialog, { onClose: close, onStopped, open: true, pair, storage })
  )
}

// ── connect, choose, confirm: one sheet that moves on by itself ─────────────

const PROFILE_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/
const ACTION_WORDS = {
  by_hand: 'later, with the page\'s help',
  create: 'copies now',
  held_back: 'not copied: looks like a password or key',
  via_mirror: 'optional, set up later'
}
const SCAN_WORDS = { caution: 'Safety check: take a look', dangerous: 'Safety check: risky' }
const SKIP_REASON = { already_twinned: 'already has a twin', profile_already_paired: 'that short name is taken' }

// A row keeps its number and its title whether it is done or not; a done row shows one line instead of its body.
function SetupRow({ n, done, title, summary, children, actions }) {
  return el(
    'li',
    { className: cn(CARD, 'flex flex-col gap-1.5') },
    el(
      'div',
      { className: 'flex items-center gap-2' },
      jsx(Icon, { className: done ? 'text-(--ui-text-secondary)' : 'text-(--ui-text-tertiary)', name: done ? 'pass-filled' : 'circle-large-outline' }),
      el('span', { className: HEADING }, `${n}. ${title}`),
      el('span', { className: 'sr-only' }, done ? '(done)' : '(to do)')
    ),
    done ? summary && el('div', { className: cn(BODY, 'pl-6') }, summary) : children && el('div', { className: cn(BODY, 'flex flex-col gap-1.5 pl-6') }, ...many(children)),
    !done && actions && el('div', { className: 'flex flex-wrap items-center gap-2 pl-6' }, ...many(actions))
  )
}

// A problem whose fix is a fresh download (the wrong release, a damaged file, a download that is gone).
const NEEDS_DOWNLOAD = ['check_failed', 'not_found', 'not_genuine']

// Two rows that tick themselves while the sheet is open. The plugin never downloads botport itself:
// the owner's browser fetches it, Hermes finds the download (or the owner shows Hermes the file), and
// Hermes verifies it on the spot. One label per action, wherever it appears: "Download botport",
// "Verify botport". When something failed, the row says what, and the fix is the primary button.
// `initialDownloaded` / `initialElsewhere` / `initialOther` let the smoke test render those states.
function ConnectStep({ setup, onSetup, onRetry, onSample, onLink, initialDownloaded = false, initialWaiting = false, initialElsewhere = false, initialOther = false, initialAddress = '', initialProblem = null, initialTaken = false, initialSlow = false, initialVerified = false }) {
  const [busy, run] = useBusy()
  const [downloaded, setDownloaded] = useState(initialDownloaded || initialWaiting)
  // Right after Download the row waits: Hermes finds the file in Downloads by itself. The file picker is for
  // whoever asks for it, or for a download that has not shown up after a minute.
  const [byHand, setByHand] = useState(initialDownloaded && !initialWaiting)
  const [elsewhere, setElsewhere] = useState(initialElsewhere)
  const [address, setAddress] = useState(initialAddress)
  const [problem, setProblem] = useState(initialProblem)
  const [typing, setTyping] = useState(false)
  const [typed, setTyped] = useState('')
  // The file was taken (Use it, or chosen) but could not be verified on the spot: the row then says why and
  // what to do next. It never goes back to "Found botport in Downloads" as if nothing had been pressed.
  const [taken, setTaken] = useState(initialTaken)
  const [slow, setSlow] = useState(initialSlow)
  // verified a moment ago, and the sheet has not moved on yet: the row says so (never a stale line in between)
  const [verified, setVerified] = useState(initialVerified)
  const data = setup.data
  const appFound = data?.grok_app?.found === true
  const found = data?.helper?.found === true
  const checked = data?.check?.done === true
  // Accepted in the terminal as a local build: in use, but never called verified.
  const local = checked && (data?.check?.unattested === true || data?.check?.method === 'local')
  const ghFound = data?.check?.gh_found !== false
  const version = clean(data?.helper?.version, 20)
  const safe = value => (typeof value === 'string' && value.startsWith('https://') ? value : '')
  const direct = safe(data?.download?.url)
  const pageUrl = safe(data?.download_url)
  const target = direct || pageUrl
  const appUrl = safe(data?.grok_app?.download_url)
  // Hermes saw the download arrive in Downloads (by its exact name): no file to hunt for.
  const arrived = !checked && clean(data?.download?.found?.name, 80) !== ''
  const folder = typeof data?.download?.dir === 'string' ? data.download.dir.slice(0, 1024) : ''
  const ghUrl = safe(data?.gh_help?.url)
  const ghCommands = (Array.isArray(data?.gh_help?.commands) ? data.gh_help.commands : []).slice(0, 3).map(c => clean(c, 80)).filter(Boolean)
  const named = version ? `botport ${version}` : 'botport'

  // A failure here is said in the row it belongs to, not in a toast over the sheet.
  const quietly = (name, fn) =>
    run(name, async () => {
      setProblem(null)

      try {
        return await fn()
      } catch (err) {
        setProblem(asProblem(err))

        return null
      }
    })

  async function download() {
    const ok = target && typeof os?.openExternal === 'function' ? await os.openExternal(target) : false

    // The browser did not open: the address is shown instead, with a Copy button.
    setAddress(ok ? '' : target)
    setProblem(null)
    setByHand(!ok)
    setDownloaded(true)
  }

  useEffect(() => {
    if (!downloaded || byHand || !timers) {
      return undefined
    }

    return timers.setTimeout(() => setByHand(true), 60_000)
  }, [downloaded, byHand])

  const verifying = busy === 'file' || busy === 'check'

  useEffect(() => {
    if (!verifying) {
      setSlow(false)

      return undefined
    }

    return timers ? timers.setTimeout(() => setSlow(true), 400) : undefined
  }, [verifying])

  async function useFile(body) {
    const res = await quietly('file', async () => {
      let out = await post('/engine/path', body)

      // Kept but not verified yet (no pinned record for this release on this build): the one press goes on to
      // verify it, so "Use it" always ends in a tick or in the reason. A failure is this row's message.
      if (out?.helper?.found === true && out?.check?.done !== true) {
        // This second half of the one press is named from its first moment ("Verifying botport…"), and the
        // line stays for at least 0.3 s: the row never flashes "Hermes has the file" with a button in between.
        const began = Date.now()

        setTaken(true)
        setSlow(true)
        onSetup(out)

        const trusted = await post('/engine/trust', {}).catch(err => {
          throw plainTrust(err)
        })
        const rest = 300 - (Date.now() - began)

        if (rest > 0 && timers) {
          await new Promise(done => timers.setTimeout(done, rest))
        }

        out = trusted?.setup ?? out
      }

      return out
    })

    if (res) {
      setTyping(false)
      setVerified(res?.check?.done === true)
      onSetup(res)
    } else {
      onRetry()
    }
  }

  async function chooseFile() {
    if (typeof os?.pickOpenPath !== 'function') {
      setTyping(true)

      return
    }

    // opens in Downloads when the backend says where that is
    const path = await os.pickOpenPath({ title: 'Choose the botport download', ...(folder ? { defaultPath: folder } : {}) })

    if (path) {
      await useFile({ path })
    }
  }

  async function verify(method) {
    if (busy) {
      return
    }

    try {
      const res = await quietly(method ? 'gh' : 'check', () =>
        post('/engine/trust', method ? { method } : {}).catch(err => {
          // (asked for GitHub's tool on purpose, under "For developers": its own words are right there)
          throw method ? err : plainTrust(err)
        })
      )

      if (res) {
        onSetup(res.setup)
      }
    } finally {
      onRetry()
    }
  }

  if (setup.isPending) {
    return jsx(LinesSkeleton, { rows: 5 })
  }

  if (setup.isError && !data) {
    return jsx(InlineProblem, { error: setup.error, onRetry })
  }

  const said = problem && el('span', { className: 'text-(--ui-text-primary)', key: 'problem', role: 'alert' }, [problem.message, problem.hint].filter(Boolean).join(' '))
  const waiting = !found && helperMessage(data)
  const refetch = Boolean(problem) && NEEDS_DOWNLOAD.includes(problem.code)
  const getLabel = direct ? 'Download botport' : 'Open the download page'
  const get = variant => target && el(Button, { disabled: Boolean(busy), key: 'get', onClick: () => void download(), variant }, getLabel)
  const haveFile = variant =>
    el(
      Button,
      {
        disabled: Boolean(busy),
        key: 'have',
        onClick: () => {
          setByHand(true)
          setDownloaded(true)
        },
        variant
      },
      'I have the file'
    )
  let body
  let actions

  // a file that was taken and is on this computer now: the row is about verifying it, not about finding it
  const kept = taken && found

  if (arrived && !refetch && !kept) {
    body = [el('span', { className: 'text-(--ui-text-primary)', key: 'a', role: 'status' }, 'Found botport in Downloads.'), el('span', { key: 'b' }, 'Hermes verifies it is the real botport before using it.'), said]
    actions = [
      el(Button, { disabled: Boolean(busy), key: 'use', loading: busy === 'file', onClick: () => void useFile({ from_downloads: true }) }, 'Use it'),
      el(Button, { disabled: Boolean(busy), key: 'pick', onClick: () => void chooseFile(), variant: 'ghost' }, 'Choose another file…')
    ]
  } else if (downloaded && !byHand && !problem && !address && !refetch && !kept) {
    body = [el('span', { className: 'text-(--ui-text-primary)', key: 'a', role: 'status' }, 'Waiting for the download. This row moves on by itself.')]
    actions = [el(Button, { disabled: Boolean(busy), key: 'hand', onClick: () => setByHand(true), variant: 'text' }, 'Choose the file myself…')]
  } else if ((downloaded || (arrived && refetch)) && !kept) {
    body = [
      // what went wrong takes the place of the instruction it makes stale
      said ??
        (address
          ? el('span', { key: 'a' }, 'Hermes could not open your browser. Copy this address into it, then choose the file it downloads.')
          : el('span', { key: 'a' }, 'Downloaded? Choose it: it is the newest file in Downloads and its name starts with botport. If your browser unpacked it, choose the file named botport inside the new folder.')),
      address && jsx(AddressLine, { address }, 'address')
    ]
    actions = [
      refetch && get('default'),
      el(Button, { disabled: Boolean(busy), key: 'pick', loading: busy === 'file', onClick: () => void chooseFile(), variant: refetch ? 'ghost' : 'default' }, 'Choose the file…'),
      !refetch && get('ghost'),
      typing &&
        el(
          'div',
          { className: 'flex min-w-0 flex-1 basis-full items-center gap-2', key: 'type' },
          jsx(Input, { 'aria-label': 'Where the downloaded file is', maxLength: 1024, onChange: e => setTyped(e.target.value), placeholder: 'Paste where the downloaded file is', size: 'sm', spellCheck: false, value: typed }),
          el(Button, { disabled: Boolean(busy) || typed.trim().length < 2, onClick: () => void useFile({ path: typed.trim() }), variant: 'secondary' }, 'Use this file')
        )
    ]
  } else if (found) {
    // botport is on this computer (chosen earlier, or installed some other way) but not verified.
    // A verification that just failed is the whole message; the stale "not verified yet" line goes.
    body = [
      said ??
        (data?.check?.problem
          ? el('span', { className: 'text-(--ui-text-primary)', key: 'b', role: 'status' }, `${plainText(data.check.problem, 240)} Hermes runs it only after that.`)
          : el('span', { className: kept ? 'text-(--ui-text-primary)' : undefined, key: 'a', role: kept ? 'status' : undefined }, kept ? 'Hermes has the file. It runs botport only after verifying it.' : 'botport is on this computer but has not been verified yet. Hermes runs it only after that.'))
    ]
    // This version of the plugin has no record of the release: a fresh download cannot help, an update of the
    // plugin can. The backend's sentence says so, and the main button goes where it sends the owner.
    const needsUpdate = problem?.code === 'not_pinned'

    actions = problem
      ? [
          needsUpdate ? el(Button, { key: 'plugins', onClick: () => host.navigate(PLUGINS_SETTINGS) }, 'Open Plugins') : get('default'),
          el(Button, { disabled: Boolean(busy), key: 'check', loading: busy === 'check', onClick: () => void verify(), variant: target || needsUpdate ? 'text' : 'default' }, 'Verify again')
        ]
      : [el(Button, { disabled: Boolean(busy), key: 'check', loading: busy === 'check', onClick: () => void verify() }, 'Verify botport'), get('ghost')]
  } else {
    // (with no botport for this computer, what botport is does not help: the sentence that says so stands alone)
    body = [(target || !waiting) && el('span', { key: 'a' }, 'botport is a small free app that reads your bot list from Grok. Hermes verifies it is the real botport before using it.'), waiting && el('span', { className: 'text-(--ui-text-primary)', key: 'w' }, waiting), said]
    actions = [
      get('default'),
      // (with nothing to download for this computer, "I have the file" is for developers: it waits in their fold)
      target && haveFile('ghost'),
      // nothing to download for this computer: the two ways on that need no botport here, linking first
      !target && onLink && el(Button, { key: 'link', onClick: onLink }, LINK_LABEL),
      !target && onSample && el(Button, { key: 'sample', onClick: onSample, variant: onLink ? 'ghost' : 'default' }, 'See the sample')
    ]
  }

  // Anything longer than a moment is named (never a bare spinner, and never a line that only flashes).
  if (slow) {
    body = [el('span', { className: 'text-(--ui-text-primary)', key: 'v', role: 'status' }, 'Verifying botport…')]
  } else if (verified && !checked && !problem) {
    // the answer is in hand, the sheet's own facts follow in a moment
    body = [el('span', { className: 'text-(--ui-text-primary)', key: 'v', role: 'status' }, 'botport is verified.')]
    actions = []
  }

  return el(
    'div',
    { className: 'flex flex-col gap-2.5' },
    el(
      'ol',
      { 'aria-label': 'Steps to connect', className: 'flex flex-col gap-2' },
      el(
        SetupRow,
        {
          actions: [
            // the one thing to press here; it is the sheet's main button once botport is done
            appUrl && el(Button, { key: 'get', onClick: () => void os?.openExternal?.(appUrl), variant: checked ? 'default' : 'secondary' }, 'Get the Grok app'),
            !elsewhere && el(Button, { key: 'else', onClick: () => setElsewhere(true), variant: 'text' }, 'My bots are elsewhere'),
            elsewhere && onLink && el(Button, { key: 'link', onClick: onLink, variant: 'secondary' }, LINK_LABEL),
            elsewhere && onSample && el(Button, { key: 'sample', onClick: onSample, variant: 'secondary' }, 'See the sample')
          ],
          done: appFound,
          n: 1,
          summary: 'Found on this computer.',
          title: 'The Grok desktop app'
        },
        el('span', { key: 'a' }, 'Twins needs the Grok desktop app on this computer. Open it and sign in; this ticks by itself.'),
        elsewhere && el('span', { className: 'text-(--ui-text-primary)', key: 'b' }, `${onLink ? 'Bots on another computer? Link this one to it. ' : ''}Twins cannot reach bots that exist only in the browser or on a phone yet.${onSample ? ' You can still see the sample.' : ''}`)
      ),
      el(
        SetupRow,
        {
          actions,
          done: checked,
          n: 2,
          summary: local ? `${named} is in use but has not been verified.` : `${named} · verified`,
          title: 'Get botport'
        },
        ...body.filter(Boolean)
      )
    ),
    !checked &&
      el(
        Fold,
        { initialOpen: initialOther, label: 'For developers' },
        el(
          'div',
          { className: 'flex flex-col gap-2' },
          found &&
            el(
              'div',
              { className: 'flex flex-col gap-1.5' },
              el('p', { className: SMALL }, "Have GitHub's command-line tool (gh)? Hermes can also verify botport against the build record GitHub keeps for it."),
              !ghFound && ghCommands.length > 0 && el('p', { className: SMALL }, 'It is not on this computer yet. In the Terminal app:'),
              !ghFound && ghCommands.map(c => el('div', { key: c }, jsx(CommandLine, { command: c, label: 'Copy' }))),
              el(
                'div',
                { className: 'flex flex-wrap items-center gap-2' },
                el(Button, { disabled: Boolean(busy), loading: busy === 'gh', onClick: () => void verify('gh'), variant: 'secondary' }, "Verify with GitHub's tool"),
                !ghFound && ghUrl && el(Button, { onClick: () => void os?.openExternal?.(ghUrl), variant: 'ghost' }, "Open the tool's own page")
              )
            ),
          // no botport is offered for this computer: a build of one's own can still be chosen here
          !target && !found && !downloaded && el('div', { className: 'flex flex-wrap items-center gap-2' }, el('span', { className: cn(SMALL, 'min-w-0 flex-1') }, 'Built botport yourself?'), haveFile('ghost')),
          el('p', { className: SMALL }, 'Prefer the terminal? The same steps as one command, once botport is installed:'),
          jsx(CommandLine, { command: 'hermes twin setup' })
        )
      )
  )
}

// Verifying without naming a way: when the plugin has no record of this release it falls back to a developer's
// tool, and says so in that tool's words. On the owner's path that is one plain sentence and the next act; the
// tool stays under "For developers".
function plainTrust(err) {
  const e = asProblem(err)

  return ['gh_missing', 'gh_login', 'gh_unsafe'].includes(e.code)
    ? new TwinError('Hermes could not verify this file, so it was not run.', 'check_failed', 'Download botport again. If that does not help, open "For developers" below.')
    : e
}

// Why there is nothing to download (no build for this kind of computer), in the backend's words.
function helperMessage(data) {
  const none = data && 'download' in data && data.download === null

  return none ? plainText(data?.problem, 200) || 'There is no botport for this kind of computer yet.' : ''
}

// One row of the full item list behind "See every item". The preview text is the helper's,
// with lines that look like passwords or keys already hidden; it is shown as plain text, capped.
function PreviewItem({ item }) {
  const [open, setOpen] = useState(false)
  const preview = typeof item?.preview === 'string' ? item.preview.slice(0, 4_000) : ''
  const kind = kindLabel(item?.kind)
  // Name, description and instructions repeat their kind as the name: the first line of the text
  // stands there instead, or nothing; never the same word twice.
  const repeats = clean(item?.name, 80).toLowerCase() === kind.toLowerCase()
  const note = clean(item?.note, 160)

  return el(
    'li',
    { className: 'flex flex-col py-0.5' },
    el(
      'div',
      { className: cn('flex min-w-0 items-center gap-2', BODY) },
      el('span', { className: 'w-24 shrink-0 text-(--ui-text-secondary)' }, capital(kind)),
      jsx(Truncate, { className: 'flex-1', max: 80, text: repeats ? (preview.split('\n').find(l => l.trim()) ?? '') : item?.name }),
      SCAN_WORDS[item?.scan] && jsx(Tag, { children: SCAN_WORDS[item.scan] }),
      el('span', { className: 'shrink-0 text-xs text-(--ui-text-secondary)' }, ACTION_WORDS[item?.action] ?? ACTION_WORDS.create),
      preview &&
        el(
          Button,
          { 'aria-expanded': open, 'aria-label': `${open ? 'Hide' : 'Show'} ${clean(item?.name, 60)}`, onClick: () => setOpen(!open), size: 'icon-xs', variant: 'ghost' },
          jsx(DisclosureCaret, { open })
        )
    ),
    note && el('span', { className: cn(SMALL, 'ml-26') }, capital(note)),
    open && el('pre', { className: cn(PRE, 'mt-1 ml-26 max-h-40'), tabIndex: 0 }, preview)
  )
}

// The three summary lines of a bot about to be twinned. The backend writes them (the terminal prints
// the same words); the fallback only covers an older backend.
function previewLines(p) {
  if (Array.isArray(p?.lines) && p.lines.length) {
    // (a routine is moved from the twin's menu whenever the owner likes: it is not part of what the row then
    // counts as left, so it is not listed under "later" as if it were)
    return p.lines
      .slice(0, 5)
      .map(l => clean(l, 240))
      .filter(Boolean)
      .flatMap(l => {
        const m = /^Later, with the page's help: (.+)\.$/.exec(l)
        const parts = m ? m[1].split(', ') : []
        const routines = parts.filter(x => /^\d+ routines?$/.test(x))

        if (routines.length === 0) {
          return [l]
        }

        const rest = parts.filter(x => !routines.includes(x))

        return [rest.length ? `Later, with the page's help: ${rest.join(', ')}.` : '', `Optional: ${routines[0]}, from the twin's "…" menu.`].filter(Boolean)
      })
  }

  const items = Array.isArray(p?.items) ? p.items : []
  const kinds = action => [...new Set(items.filter(i => (i?.action || 'create') === action).map(i => kindLabel(i?.kind)))]
  const now = kinds('create').filter(k => k !== 'skill')
  const count = kind => items.filter(i => (i?.action || 'create') === 'by_hand' && kindLabel(i?.kind) === kind).length
  // (routines are moved from the twin's menu whenever the owner likes: they are not part of what is "left")
  const later = kinds('by_hand').filter(k => k !== 'routine').map(k => (k === 'connection' ? plural(count(k), k) : k))
  const routines = count('routine')
  const skills = items.filter(i => i?.kind === 'skill').length
  const held = Array.isArray(p?.secrets_withheld) ? p.secrets_withheld.length : 0

  return [
    now.length ? `Copies now: ${joinWords(now).replace(' & ', ' and ')}.` : 'Copies now: nothing yet.',
    later.length ? `Later, with the page's help: ${later.join(', ')}.` : 'Nothing is left for later.',
    routines ? `Optional: ${plural(routines, 'routine')}, from the twin's "…" menu.` : '',
    skills ? `${plural(skills, 'skill')}: optional, set up later.` : '',
    held ? `${plural(held, 'thing')} that look${held === 1 ? 's' : ''} like a password or key will not be copied.` : ''
  ].filter(Boolean)
}

// grok id -> why its Hermes name cannot be used: another twin has it, or
// another bot in this batch uses it too (one Hermes bot backs one twin).
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
      out[gid] = 'Another twin already uses that name. Try another.'
    } else if (pairs.some(o => o?.grok?.id !== gid && profiles[o?.grok?.id] === value)) {
      out[gid] = 'Another bot here uses that name too. Give each twin its own.'
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

// `initialStep` / `initialPreview` / `initialCreated` let the smoke test render each step directly.
// `onFinish(pairId)` closes the sheet and opens "Finish setting up" for the twin that was just made.
// `reconnect`: opened from the "not following Grok" banner; once both rows are ticked again the sheet closes by itself.
// Where the owner was in the sheet (the bots ticked, and whether "Ready to twin" was reached) is noted until a
// twin is made: Escape, a click outside, Connect pressed again or a reload come back to the same place.
// Only the bots' ids are noted; what "Ready to twin" shows is asked for again.
const PLACE_KEY = 'connect-place'

function placeOf(storage) {
  const kept = storage?.get(PLACE_KEY, null)
  const ids = (Array.isArray(kept?.ids) ? kept.ids : []).filter(i => typeof i === 'string' && i).slice(0, 50)

  return ids.length > 0 ? { confirm: kept?.step === 'confirm', ids } : null
}

function AddFlow({ open, onClose, onFinish, onSample, onLink, ready, storage = null, reconnect = false, problem = null, initialStep = null, initialPreview = null, initialCreated = null }) {
  const [step, setStep] = useState(initialStep ?? (initialPreview ? 'confirm' : ready ? 'choose' : 'connect'))
  const place = useRef(initialStep || initialPreview || reconnect ? null : placeOf(storage))
  const [picked, setPicked] = useState(() => (place.current ? Object.fromEntries(place.current.ids.map(i => [i, true])) : null))
  const [preview, setPreview] = useState(initialPreview)
  const [profiles, setProfiles] = useState(() => profilesOf(initialPreview))
  const [created, setCreated] = useState(initialCreated)
  const [busy, run] = useBusy()
  const qc = useQueryClient()
  const setup = useQuery({ enabled: open && step === 'connect', queryFn: () => api('/setup'), queryKey: SETUP_KEY, refetchInterval: 3_000 })
  // The list is looked at again every few seconds while it is open: a bot made in Grok meanwhile appears by
  // itself, and one deleted there goes.
  const bots = useQuery({ enabled: open && step === 'choose', queryFn: () => api('/grok/bots'), queryKey: [ID, 'grok-bots'], refetchInterval: 5_000 })
  // Both rows have to be ticked: botport verified and the Grok desktop app found.
  const setupReady = setup.data?.ready === true && setup.data?.grok_app?.found !== false
  const botsNotReady = bots.data?.ready === false || bots.data?.app_found === false
  const bounced = useRef(0)

  // Both rows ticked: move on without a click. A bot list that says botport is
  // not ready sends the owner back to the two rows, never to a false "Grok not found".
  useEffect(() => {
    if (step === 'connect' && setupReady) {
      void qc.invalidateQueries({ queryKey: STATUS_KEY })

      if (reconnect) {
        // The owner came to fix the connection, not to add a bot.
        host.notify({ id: `${ID}:reconnected`, kind: 'success', message: 'Connected again', meta: 'Your twins are being checked again.' })
        refreshAll()
        onClose()

        return
      }

      setStep('choose')
    } else if (step === 'choose' && botsNotReady && bounced.current < 2) {
      // back to the two rows, where the missing thing has its own line (not for ever, should the two
      // answers disagree: then the bot list says it itself)
      bounced.current += 1
      setStep('connect')
    }
  }, [step, setupReady, botsNotReady])

  const list = Array.isArray(bots.data?.bots) ? bots.data.bots.filter(b => !b?.demo) : []
  const free = list.filter(b => b?.grok_id && !b?.twinned_pair_id)
  // The only bot without a twin is ticked already; with several, the owner chooses.
  const ticks = picked ?? (free.length === 1 ? { [free[0].grok_id]: true } : {})
  const ids = free.filter(b => ticks[b.grok_id]).map(b => b.grok_id)
  const allPicked = free.length > 0 && free.every(b => ticks[b.grok_id])
  const previewPairs = Array.isArray(preview?.pairs) ? preview.pairs : []
  const taken = profileClash(preview, profiles)
  const badProfile = previewPairs.some(p => !PROFILE_RE.test(profiles[p?.grok?.id] ?? '') || taken[p?.grok?.id])
  const skipped = Array.isArray(preview?.skipped) ? preview.skipped.slice(0, 20) : []
  const skippedText = skipped.length
    ? `Left out: ${skipped.map(k => `${clean(k?.name ?? list.find(b => b?.grok_id === k?.grok_id)?.name ?? 'one bot', 60)} (${SKIP_REASON[k?.reason] ?? 'cannot be twinned right now'})`).join(', ')}`
    : ''

  // Note the place (see PLACE_KEY); once a twin is made, or nothing is ticked, there is nothing to come back to.
  useEffect(() => {
    if (reconnect || step === 'connect' || (step === 'choose' && !bots.data)) {
      return
    }

    if (step === 'done' || ids.length === 0) {
      storage?.remove?.(PLACE_KEY)
    } else {
      storage?.set?.(PLACE_KEY, { ids, step: step === 'confirm' ? 'confirm' : 'choose' })
    }
  }, [step, ids.join('|'), Boolean(bots.data)])

  // Came back to a sheet that was at "Ready to twin": go there again, once the bot list is in hand.
  useEffect(() => {
    if (step === 'choose' && place.current?.confirm && bots.data && !busy) {
      place.current = null

      if (ids.length > 0) {
        void doPreview()
      }
    }
  }, [step, Boolean(bots.data)])

  async function doPreview() {
    const res = await run('preview', () => post('/twins/preview', { grok_ids: ids }), 'Could not get those bots ready')

    if (res) {
      setProfiles(profilesOf(res))
      setPreview(res)
      setStep('confirm')
    }
  }

  async function create() {
    const grokIds = previewPairs.map(p => p?.grok?.id).filter(Boolean)
    const res = await run('create', () => post('/twins/create', { grok_ids: grokIds, profiles }), 'Could not make the twin')

    if (res) {
      setCreated(Array.isArray(res.pairs) ? res.pairs : [])
      setStep('done')
      refreshAll()
    }
  }

  let title
  let description
  let body
  let footer

  if (step === 'connect') {
    title = reconnect ? 'Reconnect your Grok bots' : 'Connect your Grok bots'
    description = reconnect ? 'Your twins are checked again as soon as both rows are ticked.' : 'Two steps, once. About a minute.'
    body = jsx(ConnectStep, {
      initialProblem: problem,
      onLink,
      onRetry: () => void setup.refetch(),
      onSample,
      onSetup: next => {
        if (next && typeof next === 'object') {
          qc.setQueryData?.(SETUP_KEY, next)
        }

        void setup.refetch()
      },
      setup
    })
    // No "Continue": the sheet moves on by itself the moment both rows are ticked.
    footer = [el(Button, { key: 'cancel', onClick: onClose, variant: 'ghost' }, 'Not now')]
  } else if (step === 'choose') {
    title = 'Choose your bots'
    description = list.length > 0 && free.length === 0 ? 'Nothing changes in Grok.' : 'These are the bots in your Grok app. Nothing changes in Grok.'
    body = bots.isPending
      ? jsx(LinesSkeleton, { rows: 4 })
      : bots.isError
        ? jsx(InlineProblem, { error: bots.error, onRetry: () => void bots.refetch() })
        : bots.data?.ready === false
          ? el(
              'div',
              { className: cn(NOTE, 'flex flex-wrap items-center gap-2') },
              el('span', { className: 'min-w-0 flex-1' }, 'The connection to your Grok app is not finished yet.'),
              el(Button, { onClick: () => setStep('connect'), variant: 'secondary' }, 'Finish connecting')
            )
          : bots.data?.real_hidden
            ? el('div', { className: NOTE }, 'Your real Grok bots are hidden while the sample is on, because you chose that. Choose "Show my real bots again" in the "…" menu of the sample banner, or end the sample, to see them here.')
            : list.length === 0
              ? el(
                  'div',
                  { className: cn(NOTE, 'flex flex-wrap items-center gap-2') },
                  el(
                    'span',
                    { className: 'min-w-0 flex-1' },
                    bots.data?.app_found === false
                      ? 'The Grok desktop app is not on this computer. Install it and sign in, then press Try again.'
                      : 'No bots in your Grok app yet. Make one in Grok; it appears here by itself.'
                  ),
                  // nothing to press when the list fills by itself
                  bots.data?.app_found === false && el(Button, { onClick: () => void bots.refetch(), variant: 'secondary' }, 'Try again')
                )
              : el(
                  'div',
                  { className: 'flex flex-col gap-1' },
                  // nothing to choose: the sentence says so, and the way to more (no Continue that can never be pressed,
                  // and no list of bots that cannot be ticked: the page behind has them)
                  free.length === 0 && el('p', { className: NOTE, role: 'status' }, 'Every bot in Grok already has a twin. Make a new one in Grok; it appears here by itself.'),
                  free.length > 0 && el(
                    'div',
                    { className: 'flex items-center gap-2' },
                    el('h3', { className: cn(HEADING, 'flex-1') }, 'Your Grok bots'),
                    free.length > 1 &&
                      el(
                        Button,
                        {
                          'aria-label': allPicked ? 'Clear the selection' : 'Select every bot that has no twin yet',
                          onClick: () => setPicked(allPicked ? {} : Object.fromEntries(free.map(b => [b.grok_id, true]))),
                          variant: 'ghost'
                        },
                        allPicked ? 'Clear' : 'Select all'
                      )
                  ),
                  free.length > 0 && el(
                    'ul',
                    { className: 'flex max-h-80 flex-col overflow-y-auto' },
                    list.map((b, i) => {
                      const twinned = Boolean(b?.twinned_pair_id)
                      const cid = `${ID}-bot-${i}`

                      return el(
                        'li',
                        { className: cn('flex min-w-0 items-start gap-2.5 py-1', i > 0 && 'border-t border-(--ui-stroke-tertiary)'), key: b?.grok_id ?? i },
                        jsx(Checkbox, {
                          'aria-label': clean(b?.name, 60),
                          checked: twinned || Boolean(ticks[b?.grok_id]),
                          className: CHECKBOX,
                          disabled: twinned,
                          id: cid,
                          onCheckedChange: v => setPicked({ ...ticks, [b?.grok_id]: v === true })
                        }),
                        el(
                          'label',
                          { className: cn(TICK_LABEL, !twinned && 'cursor-pointer'), htmlFor: cid },
                          el(
                            'span',
                            { className: 'flex min-w-0 items-center gap-1.5' },
                            jsx(Truncate, { className: cn('text-[0.8125rem] font-medium', twinned ? 'text-(--ui-text-secondary)' : 'text-(--ui-text-primary)'), max: 60, text: b?.name }),
                            twinned && jsx(Tag, { children: 'Already has a twin' })
                          ),
                          b?.description && jsx(Truncate, { className: SMALL, max: 120, text: b.description })
                        )
                      )
                    })
                  )
                )
    const nothingToChoose = !bots.isPending && !bots.isError && list.length > 0 && free.length === 0

    footer = nothingToChoose
      ? [el(Button, { key: 'cancel', onClick: onClose }, 'Done')]
      : [
          el(Button, { key: 'cancel', onClick: onClose, variant: 'ghost' }, 'Cancel'),
          el(Button, { disabled: ids.length === 0 || Boolean(busy), key: 'go', loading: busy === 'preview', onClick: () => void doPreview() }, 'Continue')
        ]
  } else if (step === 'confirm') {
    const one = previewPairs.length === 1
    title = one ? `Ready to twin ${clean(previewPairs[0]?.grok?.name, 60)}` : previewPairs.length ? `Ready to twin ${previewPairs.length} bots` : 'Nothing new to twin'
    description = previewPairs.length ? 'Nothing changes in Grok.' : 'Nothing was changed.'
    body = el(
      'div',
      { className: 'flex max-h-[55vh] flex-col gap-3 overflow-y-auto pr-1' },
      skippedText && el('div', { className: NOTE }, skippedText),
      previewPairs.length === 0 && el('p', { className: BODY }, 'Each bot has one twin. Go back and choose a bot that has none yet.'),
      previewPairs.map((p, i) => {
        const gid = p?.grok?.id
        const items = Array.isArray(p?.items) ? p.items : []
        const value = profiles[gid] ?? ''
        const invalid = !PROFILE_RE.test(value)
        const clash = !invalid && taken[gid]
        const inputId = `${ID}-profile-${i}`

        return el(
          'section',
          { className: cn(!one && CARD, 'flex flex-col gap-2'), key: gid ?? i },
          !one &&
            el(
              'div',
              { className: 'flex min-w-0 items-center gap-2' },
              jsx(Avatar, { name: p?.grok?.name }),
              jsx(Truncate, { className: 'flex-1 text-sm font-semibold text-(--ui-text-primary)', max: 60, text: p?.grok?.name })
            ),
          el('ul', { 'aria-label': `What happens for ${clean(p?.grok?.name, 60)}`, className: cn(BODY, 'flex flex-col gap-0.5') }, previewLines(p).map((line, j) => el('li', { key: j }, line))),
          (invalid || clash) && el('p', { className: 'text-xs text-(--ui-text-primary)', role: 'alert' }, invalid ? 'The short name in Hermes can use lower-case letters, digits, - and _. Fix it under Options.' : clash),
          items.length > 0 &&
            el(
              Fold,
              { label: 'See every item', openLabel: 'Hide the items' },
              el(
                'ul',
                { className: 'flex flex-col' },
                items.slice(0, 60).map((it, j) => jsx(PreviewItem, { item: it }, `${it?.item ?? it?.name}-${j}`)),
                items.length > 60 && el('li', { className: cn(SMALL, 'py-0.5'), key: 'more' }, `and ${items.length - 60} more`)
              )
            ),
          el(
            Fold,
            { initialOpen: invalid || Boolean(clash), label: 'Options' },
            el(
              'div',
              { className: 'flex flex-col gap-1' },
              el(
                'div',
                { className: 'flex items-center gap-2' },
                el('label', { className: cn(BODY, 'shrink-0'), htmlFor: inputId }, 'Short name in Hermes'),
                jsx(Input, {
                  'aria-invalid': invalid || Boolean(clash) || undefined,
                  id: inputId,
                  maxLength: 64,
                  onChange: e => setProfiles({ ...profiles, [gid]: e.target.value.toLowerCase() }),
                  spellCheck: false,
                  value
                })
              ),
              el('p', { className: SMALL }, 'Hermes picked a free name for you. Change it only if you want a different one.')
            )
          )
        )
      })
    )
    footer = [
      el(Button, { disabled: Boolean(busy), key: 'back', onClick: () => setStep('choose'), variant: previewPairs.length ? 'ghost' : 'default' }, 'Back'),
      previewPairs.length > 0 &&
        el(
          Button,
          { disabled: Boolean(busy) || badProfile, key: 'go', loading: busy === 'create', onClick: () => void create() },
          previewPairs.length > 1 ? `Twin ${previewPairs.length} bots` : 'Twin this bot'
        )
    ]
  } else {
    // A new twin has the name and the description. What is still left (its instructions, a model) is done for
    // every new twin at once in the batch sheet; a twin with nothing left is ready.
    const made = Array.isArray(created) ? created : []
    const only = made.length === 1 ? made[0] : null
    const left = only ? leftOf(only) : null
    const openTwin = p => {
      openInHermes(clean(p.hermes.profile, 64))
      onClose()
    }

    if (made.some(p => !isSample(p) && thingsLeft(p) > 0)) {
      return jsx(BatchSheet, { onClose, onOneAtATime: onFinish, open, pairs: made, storage })
    }

    if (made.length === 0) {
      title = 'No twin was made'
      description = 'Nothing was changed.'
    } else if (only && left && left.length === 0) {
      title = `${owned(nameOf(only))} twin is ready`
      description = 'It follows Grok from now on.'
    } else if (only) {
      title = `${owned(nameOf(only))} twin is made`
      description = "It has the bot's name and description, and follows them."
    } else {
      title = made.every(p => leftOf(p) !== null) ? `${made.length} twins are ready` : `${made.length} twins made`
      description = made.every(p => leftOf(p) !== null) ? 'They follow Grok from now on.' : 'Each has its name and description.'
    }

    // One twin: the title already names it. Several: the list.
    body = only
      ? null
      : el(
          'div',
          { className: 'flex flex-col gap-2' },
          made.map((p, i) =>
            el(
              'div',
              { className: 'flex min-w-0 items-center gap-3', key: p?.pair_id ?? i },
              jsx(Avatar, { name: nameOf(p) }),
              jsx(Truncate, { className: 'flex-1 text-sm font-medium text-(--ui-text-primary)', max: 60, text: nameOf(p) })
            )
          )
        )

    const canOpen = only && !isSample(only) && only?.hermes?.profile

    if (canOpen && left) {
      footer = [
        el(Button, { key: 'done', onClick: onClose, variant: 'ghost' }, 'Done'),
        el(Button, { 'aria-label': `Open ${nameOf(only)} in Hermes`, key: 'open', onClick: () => openTwin(only) }, 'Open in Hermes')
      ]
    } else {
      footer = [
        canOpen && el(Button, { 'aria-label': `Open ${nameOf(only)} in Hermes`, key: 'open', onClick: () => openTwin(only), variant: 'secondary' }, 'Open in Hermes'),
        el(Button, { key: 'done', onClick: onClose }, 'Done')
      ]
    }
  }

  return el(Sheet, { description, footer, locked: Boolean(busy), onClose, open, title, wide: true }, body)
}

// ── several new twins at once ───────────────────────────────────────────────
// After twinning, one sheet for every new twin: tick the twins, one model for all of them, then one request
// to paste into each bot's chat in Grok. The page looks in the Downloads folder every few seconds while the
// sheet is open and brings in each answer file as it lands (or the owner drops several files at once). The
// files' text is held only while it is sent; the backend keeps none of it and never keeps memories.
const BATCH_EVERY_MS = 4_000
const BATCH_WATCH_MS = 15 * 60 * 1000
const BATCH_FILES = 20
// While Hermes does the Grok part in another chat the page has gone: the open batch is remembered on this
// computer (ids, the time watching began, when it was handed over) for half an hour, and the Twins page opens
// the sheet again at the watching step, so the files Hermes saved meanwhile are brought in.
const BATCH_KEY = 'batch-open'
const BATCH_KEEP_MS = 30 * 60 * 1000

function rememberBatch(storage, ids, since) {
  try {
    storage?.set?.(BATCH_KEY, { at: Date.now(), ids: ids.filter(i => typeof i === 'string' && i).slice(0, 50), since })
  } catch {
    // the sheet still works; it is only not opened again by itself
  }
}

function forgetBatch(storage) {
  try {
    storage?.remove?.(BATCH_KEY)
  } catch {
    // nothing kept, nothing to clear
  }
}

// The remembered batch, when it is younger than half an hour (an older one is cleared).
function openBatchOf(storage, now = Date.now()) {
  let kept = null

  try {
    kept = storage?.get?.(BATCH_KEY, null) ?? null
  } catch {
    return null
  }

  if (!kept || typeof kept !== 'object') {
    return null
  }

  const ids = (Array.isArray(kept.ids) ? kept.ids : []).filter(i => typeof i === 'string' && i).slice(0, 50)
  const at = Number(kept.at)
  const since = Number(kept.since)

  if (ids.length === 0 || !Number.isFinite(at) || !Number.isFinite(since) || now - at >= BATCH_KEEP_MS || now < at - 60_000) {
    forgetBatch(storage)

    return null
  }

  return { at, ids, since }
}

// "its instructions and a model" / "its instructions" / "a model"
function needWords(left) {
  return joinWords([left.includes('instructions') && 'its instructions', left.includes('model') && 'a model'].filter(Boolean))
}

// One twin as the batch sheet sees it: what the backend says is left (or, from an older backend, what the
// twin itself says), and its name in plain text.
function batchTwinOf(raw, pair) {
  const own = leftOf(pair) ?? []
  const left = Array.isArray(raw?.left) ? SETUP_STEPS.filter(k => raw.left.includes(k)) : own

  return { left, name: clean(raw?.name, 60) || nameOf(pair), pair, pairId: pair.pair_id, sample: raw?.sample === true || isSample(pair) }
}

// What bringing in one twin's file did: counts and plain words only, never a word of the file.
function batchResultOf(r) {
  return {
    connectors: countOf(r?.connectors) ?? 0,
    file: clean(r?.file, 120),
    instructions: r?.instructions === true,
    leftOut: countOf(r?.lines_left_out) ?? 0,
    routines: countOf(r?.routines) ?? 0,
    state: clean(r?.state, 20),
    why: plainText(r?.why, 240)
  }
}

// A twin that is done stays done; "no file yet" never takes the place of anything.
function mergeBatch(prev, results) {
  const next = { ...prev }

  for (const r of listOf(results).slice(0, 200)) {
    const pid = typeof r?.pair_id === 'string' ? r.pair_id : ''
    const got = batchResultOf(r)

    if (!pid || got.state === 'no_file' || (next[pid]?.state === 'done' && got.state !== 'done')) {
      continue
    }

    next[pid] = got
  }

  return next
}

// The lines under one twin: the first says where it is, the others what came with its file.
function batchLines(name, got, already) {
  if (got?.state === 'done') {
    return [
      got.instructions ? `${name}: instructions in ✓` : `${name}: its file is in; it has no instructions ✓`,
      got.routines > 0 && `${plural(got.routines, 'routine')} added, switched off`,
      got.connectors > 0 && `${plural(got.connectors, 'connector')} to set up here`,
      got.leftOut > 0 && `${plural(got.leftOut, 'line')} left out: ${got.leftOut === 1 ? 'it looks' : 'they look'} like a password or key`
    ].filter(Boolean)
  }

  if (already) {
    return [`${name}: instructions in ✓`]
  }

  if (got?.state === 'failed') {
    return [`${name}: its file could not be brought in. ${got.why || 'Hermes could not read it.'}`]
  }

  if (got?.state === 'sample') {
    return [`${name}: the sample has nothing to bring in`]
  }

  return [`${name}: waiting for its file…`]
}

// `initial…` let the smoke test render each state: the instructions step, a stopped watch, files brought in,
// the permission check ticked, a model that was set.
function BatchSheet({ pairs, open, onClose, onOneAtATime, storage, initialPhase = 'choose', initialTicked = null, initialModel = null, initialGot = {}, initialWatch = 'on', initialCopied = null, initialConsent = false, initialModelSaid = null, initialUnmatched = 0, resumed = null }) {
  const made = listOf(pairs).filter(p => typeof p?.pair_id === 'string' && p.pair_id)
  const ids = made.map(p => p.pair_id)
  const q = useQuery({ enabled: open && ids.length > 0, queryFn: () => api(`/batch?pair_ids=${ids.map(encodeURIComponent).join(',')}`), queryKey: [ID, 'batch', ids.join(',')], retry: false })
  // the twins as the page has them now: a twin whose instructions were pasted meanwhile counts as done
  const statusQ = useQuery({ enabled: open, queryFn: () => api('/status'), queryKey: STATUS_KEY })
  // `resumed`: the batch remembered while Hermes does the Grok part (see BATCH_KEY): straight to the watching step
  const [phase, setPhase] = useState(resumed ? 'instructions' : initialPhase) // 'choose' | 'instructions' | 'grok'
  const [ticked, setTicked] = useState(() => initialTicked ?? Object.fromEntries(ids.map(i => [i, !resumed || resumed.ids.includes(i)])))
  const [handedOff, setHandedOff] = useState(Boolean(resumed))
  const [typedModel, setTypedModel] = useState(initialModel) // null: the model Hermes uses, untouched
  const [endpointPick, setEndpointPick] = useState(null) // { name, model }: a model server already set up in Hermes
  const [got, setGot] = useState(initialGot)
  const [modelSaid, setModelSaid] = useState(initialModelSaid)
  const [watch, setWatch] = useState(initialWatch) // 'on' | 'stopped'
  const [since, setSince] = useState(() => (resumed ? resumed.since : Math.floor(Date.now() / 1000) - 5))
  const [scanProblem, setScanProblem] = useState(null)
  const [copied, setCopied] = useState(initialCopied)
  const [consent, setConsent] = useState(initialConsent)
  const [cuNow, setCuNow] = useState(null) // what the hand-over said, when it refused
  const [handed, setHanded] = useState('') // the chat message, when neither the chat nor the clipboard took it
  const [over, setOver] = useState(false)
  const [problem, setProblem] = useState('')
  const [unmatched, setUnmatched] = useState(initialUnmatched)
  const [aside, setAside] = useState(null) // { kind: 'finish' | 'sync', pairId }: a twin's own sheet over this one
  const [busy, run] = useBusy()
  const picker = useRef(null)
  const scanning = useRef(false)
  const cuQ = useQuery({ enabled: open && phase === 'grok', queryFn: () => api('/batch/computer-use'), queryKey: [ID, 'computer-use'], retry: false })

  const info = q.data && typeof q.data === 'object' ? q.data : null
  const twins = made.map(p => batchTwinOf(listOf(info?.twins).find(t => t?.pair_id === p.pair_id), p))
  const needing = twins.filter(t => t.left.length > 0 && !t.sample)
  const chosen = needing.filter(t => ticked[t.pairId] !== false)
  const chosenIds = chosen.map(t => t.pairId)
  const one = needing.length === 1 ? needing[0] : null
  const request = typeof info?.request === 'string' ? info.request.slice(0, 8000) : ''
  const privacy = plainText(info?.privacy, 200) || EXPORT_PRIVACY
  const defaultModel = clean(info?.default_model, 120)
  const models = [...new Set([defaultModel, ...listOf(info?.models).map(m => clean(m, 120))].filter(Boolean))].slice(0, 20)
  const endpoints = listOf(info?.endpoints)
    .map(e => ({ local: e?.local === true, models: listOf(e?.models).map(m => clean(m, 120)).filter(Boolean).slice(0, 20), name: clean(e?.name, 60), reachable: e?.reachable === true }))
    .filter(e => e.name && e.models.length > 0)
    .slice(0, 6)
  const model = (typedModel ?? defaultModel).trim()
  const endpointName = endpointPick && endpointPick.model === model ? endpointPick.name : ''
  const changedModel = typedModel !== null && model !== defaultModel
  const anyModelLeft = chosen.some(t => t.left.includes('model'))
  const downloadsHere = info?.downloads?.available !== false
  // with skill sync set up, a bot also saves its file in the Grok box and the sync bot carries it to the shared folder
  const viaMirror = info?.pickup?.via === 'mirror'
  const syncIds = info?.skill_sync?.set_up === true ? listOf(info.skill_sync.pair_ids).filter(i => typeof i === 'string') : []
  const freshPairs = listOf(statusQ.data?.pairs)
  const pairNow = pid => freshPairs.find(p => p?.pair_id === pid) ?? made.find(p => p.pair_id === pid)
  // in Hermes already: before (nothing to bring over) or since (pasted in its own sheet meanwhile)
  const already = t => {
    const now = leftOf(freshPairs.find(p => p?.pair_id === t.pairId))

    return !t.left.includes('instructions') || (now !== null && !now.includes('instructions'))
  }
  const doneFor = t => got[t.pairId]?.state === 'done' || already(t)
  const allDone = phase !== 'choose' && chosen.length > 0 && chosen.every(doneFor)
  const watching = open && phase !== 'choose' && watch === 'on' && !allDone && chosenIds.length > 0

  // every twin is in: nothing left to come back to
  useEffect(() => {
    if (allDone && handedOff) {
      forgetBatch(storage)
    }
  }, [allDone, handedOff])

  // Done or Later: the batch is not opened again by itself
  const close = () => {
    if (handedOff) {
      forgetBatch(storage)
    }

    onClose()
  }

  async function stopHermes() {
    const res = await run('stop', () => post('/batch/grok-part', { stop: true }), 'Could not stop Hermes')

    if (res) {
      forgetBatch(storage)
      setHandedOff(false)
      host.notify({ id: `${ID}:grok-part`, kind: 'success', message: 'Hermes stopped the Grok part', meta: 'Nothing more is done in Grok. Paste the request yourself for the twins still waiting.' })
    }
  }

  function took(results) {
    const list = listOf(results)

    if (list.some(r => r?.state === 'done')) {
      refreshAll()
    }

    setGot(prev => mergeBatch(prev, list))
  }

  // One look in Downloads: what matches a ticked twin is brought in at once.
  async function scan() {
    if (scanning.current) {
      return
    }

    scanning.current = true

    try {
      const res = await post('/batch/downloads', { apply: true, pair_ids: chosenIds, since })

      setScanProblem(null)
      took(res?.results)
    } catch (err) {
      // (said once, with Watch again; never a toast every four seconds)
      setScanProblem(asProblem(err))
      setWatch('stopped')
    } finally {
      scanning.current = false
    }
  }

  // Every 4 s while the sheet is open, for at most 15 minutes.
  useEffect(() => {
    if (!watching || !timers) {
      return undefined
    }

    const began = Date.now()

    void scan()

    const stop = timers.setInterval(() => {
      if (Date.now() - began >= BATCH_WATCH_MS) {
        setWatch('stopped')

        return
      }

      void scan()
    }, BATCH_EVERY_MS)

    return () => stop?.()
  }, [watching, since])

  function watchAgain() {
    setScanProblem(null)
    setSince(Math.floor(Date.now() / 1000) - 5)
    setWatch('on')
  }

  async function doAll() {
    // the model goes to the twins that have none, or to every ticked twin when the owner chose another one
    const targets = chosen.filter(t => changedModel || t.left.includes('model')).map(t => t.pairId)

    if (model && targets.length > 0) {
      const res = await run('model', () => post('/batch/model', endpointName ? { endpoint: endpointName, model, pair_ids: targets } : { model, pair_ids: targets }), 'Could not set the model')

      if (!res) {
        return
      }

      const results = listOf(res.results)
      const nameFor = r => clean(r?.name, 60) || twins.find(t => t.pairId === r?.pair_id)?.name || 'A twin'

      setModelSaid({
        failed: results.filter(r => r?.state === 'failed').map(r => ({ name: nameFor(r), why: plainText(r?.why, 200) })),
        model,
        set: results.filter(r => r?.state === 'set' || r?.state === 'same').map(nameFor)
      })
      refreshAll()
    }

    setSince(Math.floor(Date.now() / 1000) - 5)
    setWatch('on')
    setPhase('instructions')
  }

  async function copyRequest() {
    setCopied(await copyText(request))
  }

  async function readFiles(list) {
    const picked = Array.from(list ?? []).slice(0, BATCH_FILES)

    if (picked.length === 0) {
      return
    }

    const sent = []
    const bad = []

    for (const f of picked) {
      const name = clean(f?.name, 120) || 'A file'

      try {
        sent.push({ name, text: await exportTextOf(f) })
      } catch (err) {
        bad.push(`${name}: ${asProblem(err).message}`)
      }
    }

    setProblem(bad.join(' '))

    if (sent.length === 0) {
      return
    }

    const res = await run('import', () => post('/batch/import', { files: sent, pair_ids: chosenIds }), 'Could not bring in those files')

    if (res) {
      const results = listOf(res.results)
      const used = new Set(results.map(r => clean(r?.file, 120)).filter(Boolean))

      setUnmatched(sent.filter(f => !used.has(f.name)).length)
      took(results)
    }
  }

  // Hermes does the Grok part in a chat of its own: only after the owner has read what it will do, ticked that
  // they approve each step, and pressed Start. The message goes into the new chat's box; Send is the owner's.
  async function startGrokPart() {
    const res = await run('grok', () => post('/batch/grok-part', { pair_ids: chosenIds }), 'Could not hand the Grok part to Hermes')

    if (!res) {
      return
    }

    if (res.ok !== true) {
      setCuNow(res.status && typeof res.status === 'object' ? res.status : { available: false, enable: [] })

      return
    }

    const message = typeof res.message === 'string' ? res.message.slice(0, 8000) : ''
    const canDraft = typeof host.composer?.setDraft === 'function'
    const onClipboard = await copyText(message)

    if (!onClipboard && !canDraft) {
      setHanded(message)

      return
    }

    // (the page goes with the new chat: the Twins page opens this sheet again at the watching step)
    rememberBatch(storage, chosenIds, since)
    setHandedOff(true)
    setPhase('instructions')

    if (typeof host.newChat === 'function') {
      host.newChat()
    } else {
      host.navigate('/')
    }

    let put = false

    if (canDraft) {
      await pause(400)

      try {
        put = (await host.composer.setDraft(null, message)) === true
      } catch {
        put = false
      }
    }

    host.notify({
      durationMs: 30_000,
      id: `${ID}:grok-part`,
      kind: 'info',
      message: put ? 'The request is in the new chat' : 'Paste it into the new chat',
      meta: `${put ? 'Read it, then press Send.' : 'It is copied.'} Every click in Grok asks you first: answer Once each time. Stop it with Stop or /stop.`
    })
  }

  const hover = e => {
    e.preventDefault()
    setOver(true)
  }
  const nameList = listWords(chosen.map(t => t.name))
  const said = problem && el('p', { className: 'text-xs text-(--ui-text-primary)', key: 'said', role: 'alert' }, problem)

  let title
  let description
  let body
  let footer

  if (phase === 'choose') {
    // ── which twins, and one model for them ──
    if (one && made.length === 1) {
      title = `${owned(one.name)} twin is made.`
      description = `It needs ${needWords(one.left)}.`
    } else {
      const same = needing.every(t => t.left.join() === needing[0]?.left.join())

      title = `${made.length} twins made.`
      description =
        needing.length === made.length && same
          ? `Each needs ${needWords(needing[0]?.left ?? [])}.`
          : one
            ? `${one.name} needs ${needWords(one.left)}; the others have everything.`
            : 'Each line says what that twin still needs.'
    }

    const solo = needing.length === 1

    body = el(
      'div',
      { className: 'flex max-h-[60vh] flex-col gap-4 overflow-y-auto pr-1' },
      q.isError && jsx(InlineProblem, { error: q.error, onRetry: () => void q.refetch() }),
      !solo &&
        el(
          'div',
          { 'aria-label': 'Twins to set up now', className: 'flex flex-col', role: 'group' },
          needing.map(t =>
            jsx(
              CheckRow,
              {
                checked: ticked[t.pairId] !== false,
                detail: `Needs ${needWords(t.left)}`,
                disabled: Boolean(busy),
                id: `batch-${t.pairId}`,
                label: t.name,
                onChange: v => setTicked({ ...ticked, [t.pairId]: v }),
                title: t.name
              },
              t.pairId
            )
          )
        ),
      (anyModelLeft || models.length > 0 || endpoints.length > 0) &&
        el(
          'section',
          { className: 'flex flex-col gap-1' },
          el('label', { className: HEADING, htmlFor: 'batch-model' }, 'Model'),
          (endpoints.length > 0 || models.length > 0) &&
            el(
              'select',
              {
                'aria-label': 'Choose a model',
                className: 'h-8 rounded-md border border-(--ui-border) bg-transparent px-2 text-sm text-(--ui-text-primary)',
                disabled: Boolean(busy),
                id: 'batch-model-pick',
                onChange: e => {
                  const v = String(e.target.value || '')
                  const [kind, a, b] = v.split('\u0000')

                  if (kind === 'e') {
                    setEndpointPick({ model: b, name: a })
                    setTypedModel(b)
                  } else if (kind === 'm') {
                    setEndpointPick(null)
                    setTypedModel(a)
                  }
                },
                value: endpointName ? `e\u0000${endpointName}\u0000${model}` : models.includes(model) ? `m\u0000${model}` : ''
              },
              el('option', { value: '' }, 'Type a model name below'),
              endpoints.length > 0 &&
                el(
                  'optgroup',
                  { label: 'Model servers set up in Hermes' },
                  endpoints.flatMap(e =>
                    e.models.map(m =>
                      el(
                        'option',
                        { key: `${e.name}\u0000${m}`, value: `e\u0000${e.name}\u0000${m}` },
                        `${e.name}${e.local ? ' (local)' : ''} - ${m}${e.reachable ? '' : ' (not answering now)'}`
                      )
                    )
                  )
                ),
              models.length > 0 &&
                el('optgroup', { label: 'Models already in use' }, models.map(m => el('option', { key: m, value: `m\u0000${m}` }, m)))
            ),
          jsx(Input, {
            'aria-label': solo ? `Model for ${one?.name ?? 'this twin'}` : 'Model for the ticked twins',
            disabled: Boolean(busy),
            id: 'batch-model',
            list: 'batch-models',
            maxLength: 120,
            onChange: e => {
              setEndpointPick(null)
              setTypedModel(e.target.value)
            },
            placeholder: 'Type a model name',
            spellCheck: false,
            value: typedModel ?? defaultModel
          }),
          el('datalist', { id: 'batch-models' }, models.map(m => el('option', { key: m, value: m }))),
          el(
            'p',
            { className: SMALL },
            anyModelLeft
              ? solo
                ? 'Its model can be changed later.'
                : "Used for every ticked twin. Each twin's model can be changed later."
              : solo
                ? 'It has a model already; change it here only to use another. It can be changed later too.'
                : "They have a model already; change it here only to use another. Each twin's model can be changed later."
          ),
          el(
            'p',
            { className: SMALL },
            'Grok Bot Twin never downloads a model. To use a model server you already run (for example on your own network), add it as a custom endpoint: run ',
            el('code', null, 'hermes model'),
            ' or set it under Settings, then Model, in Hermes Desktop.'
          )
        )
    )

    const none = chosen.length === 0

    footer = [
      el(Button, { disabled: Boolean(busy), key: 'later', onClick: onClose, variant: 'ghost' }, 'Later'),
      el(
        Button,
        { disabled: Boolean(busy) || none, key: 'one', onClick: () => (onOneAtATime ? onOneAtATime(chosen[0].pairId) : onClose()), variant: 'secondary' },
        solo ? 'Step by step' : 'One at a time'
      ),
      el(Button, { disabled: Boolean(busy) || none || !info, key: 'all', loading: busy === 'model', onClick: () => void doAll() }, solo ? 'Do it now' : 'Do all ticked now')
    ]
  } else if (phase === 'grok') {
    // ── Hermes does the Grok part: what it will do, and the owner's word first ──
    const cu = cuNow ?? (cuQ.data && typeof cuQ.data === 'object' ? cuQ.data : null)
    const enable = listOf(cu?.enable).map(s => plainText(s, 240)).filter(Boolean).slice(0, 8)
    const each = chosen.length === 1 ? `${chosen[0].name}'s chat` : 'the chat of each ticked bot'

    title = 'Let Hermes do the Grok part'

    if (!cu && cuQ.isError) {
      description = 'Hermes could not tell whether it can use the Grok app here.'
      body = jsx(InlineProblem, { error: cuQ.error, onRetry: () => void cuQ.refetch() })
    } else if (!cu) {
      description = 'Looking at what Hermes may use on this computer.'
      body = jsx(LinesSkeleton, { rows: 3 })
    } else if (cu.available !== true) {
      description = 'Hermes needs a few things switched on first.'
      body = el(
        'div',
        { className: 'flex flex-col gap-2' },
        el('ul', { 'aria-label': 'What to switch on', className: cn(BODY, 'flex list-disc flex-col gap-1 pl-5') }, (enable.length > 0 ? enable : ['Hermes cannot use other apps on this computer yet.']).map((s, i) => el('li', { key: i }, s))),
        el('p', { className: SMALL }, 'Nothing is lost meanwhile: go back and paste the request yourself.')
      )
    } else if (handed) {
      description = 'Copy this message into a new Hermes chat.'
      body = el(
        'div',
        { className: 'flex flex-col gap-2' },
        el('pre', { className: cn(PRE, 'max-h-40 select-all'), tabIndex: 0 }, handed),
        el('div', {}, jsx(CopyButton, { appearance: 'button', buttonVariant: 'secondary', label: 'Copy', showLabel: true, text: handed }))
      )
    } else {
      description = 'Read this before anything starts.'
      body = el(
        'div',
        { className: 'flex flex-col gap-3' },
        el(
          'section',
          { 'aria-label': 'What Hermes will do', className: cn(NOTE, 'flex flex-col gap-1.5') },
          el('h3', { className: HEADING }, 'Before Hermes starts'),
          el(
            'ul',
            { className: 'flex list-disc flex-col gap-1 pl-5' },
            el('li', {}, `In a new Hermes chat, Hermes opens ${each} in the Grok app, pastes the request and saves the answer to your Downloads folder.`),
            el('li', {}, 'Every click asks you first: answer Once each time.'),
            el('li', {}, "Screenshots of the Grok window go to this chat's model provider."),
            el('li', {}, 'You can stop it at any time with Stop, or by typing /stop.')
          )
        ),
        jsx(CheckRow, { checked: consent, disabled: Boolean(busy), id: 'batch-consent', label: 'I will approve each step myself', onChange: setConsent, title: 'I will approve each step myself' })
      )
    }

    footer = [
      el(Button, { disabled: Boolean(busy), key: 'back', onClick: () => { setPhase('instructions'); setCuNow(null); setHanded('') }, variant: cu?.available === true && !handed ? 'ghost' : 'default' }, 'Back'),
      cu && cu.available !== true && el(Button, { disabled: Boolean(busy) || cuQ.isFetching, key: 'again', onClick: () => { setCuNow(null); void cuQ.refetch() }, variant: 'secondary' }, 'Look again'),
      cu?.available === true && !handed && el(Button, { disabled: Boolean(busy) || !consent, key: 'start', loading: busy === 'grok', onClick: () => void startGrokPart() }, 'Start in a new Hermes chat')
    ].filter(Boolean)
  } else {
    // ── the instructions: one request, pasted into each bot's chat; the files come back by themselves ──
    const solo = chosen.length === 1
    const routinesIn = chosen.reduce((n, t) => n + (got[t.pairId]?.state === 'done' ? got[t.pairId].routines : 0), 0)
    const modelLeft = chosen.filter(t => t.left.includes('model') && !(modelSaid?.set ?? []).includes(t.name))

    if (allDone) {
      title = solo ? `${chosen[0].name} has its instructions.` : `All ${chosen.length} twins have their instructions.`
      description = modelLeft.length > 0 ? `${listWords(modelLeft.map(t => t.name))} still ${modelLeft.length === 1 ? 'needs' : 'need'} a model: choose it in Hermes.` : solo ? 'It follows Grok from now on.' : 'They follow Grok from now on.'
    } else {
      title = solo ? `Instructions for ${chosen[0].name}` : `Instructions for ${chosen.length} twins`
      description = solo ? 'Ask the bot in Grok once; Hermes brings in its answer.' : 'Ask each bot in Grok once; Hermes brings in their answers.'
    }

    const progress = el(
      'ul',
      { 'aria-label': 'Progress', className: 'flex flex-col gap-1.5', role: 'status' },
      chosen.map(t => {
        const lines = batchLines(t.name, got[t.pairId], already(t))
        const waiting = !doneFor(t) && got[t.pairId]?.state !== 'sample'

        return el(
          'li',
          { className: 'flex min-w-0 items-start gap-2', key: t.pairId },
          jsx(Icon, { className: 'mt-1 text-(--ui-text-secondary)', name: doneFor(t) ? 'pass-filled' : got[t.pairId]?.state === 'failed' ? 'warning' : 'circle-large-outline' }),
          el(
            'div',
            { className: 'flex min-w-0 flex-1 flex-col' },
            el('span', { className: 'break-words text-[0.8125rem] text-(--ui-text-primary)' }, lines[0]),
            lines.slice(1).map((line, i) => el('span', { className: SMALL, key: i }, line))
          ),
          waiting && el(Button, { 'aria-label': `Paste ${owned(t.name)} instructions yourself instead`, disabled: Boolean(busy), onClick: () => setAside({ kind: 'finish', pairId: t.pairId }), variant: 'text' }, 'Paste it yourself instead')
        )
      })
    )
    const modelLine =
      modelSaid &&
      el(
        'div',
        { className: 'flex flex-col gap-0.5' },
        modelSaid.set.length > 0 && el('p', { className: BODY }, `Model ${clean(modelSaid.model, 120)} set for ${listWords(modelSaid.set)}.`),
        modelSaid.failed.map((f, i) => el('p', { className: cn(BODY, 'text-(--ui-text-primary)'), key: i }, `${f.name}: the model was not set. ${f.why || 'Choose it in Hermes.'}`))
      )
    // (for a ticked twin that has no skill sync yet: it joins through the shared folder that is set up already)
    const syncFor = syncIds.length === 0 ? null : (chosen.find(t => !syncIds.includes(t.pairId)) ?? chosen.find(t => syncIds.includes(t.pairId)))?.pairId ?? null

    if (allDone) {
      body = el(
        'div',
        { className: 'flex max-h-[60vh] flex-col gap-3 overflow-y-auto pr-1' },
        modelLine,
        progress,
        routinesIn > 0 && el('p', { className: SMALL }, 'Routines stay switched off until you choose Move routines to Hermes in a twin\'s "…" menu.')
      )
      footer = [el(Button, { key: 'done', onClick: close }, 'Done')]
    } else {
      const askSaid = copied === true ? (solo ? `Copied. Paste it into ${owned(chosen[0].name)} chat in Grok.` : "Copied. Paste it into each bot's chat in Grok.") : copied === false ? 'Copy this yourself:' : null

      body = el(
        'div',
        {
          className: 'flex max-h-[60vh] flex-col gap-4 overflow-y-auto pr-1',
          // a file let go anywhere on the sheet is read like one let go on the box
          onDragOver: e => e.preventDefault(),
          onDrop: e => {
            e.preventDefault()
            setOver(false)
            void readFiles(e.dataTransfer?.files)
          }
        },
        modelLine,
        handedOff &&
          el(
            'div',
            { className: cn(NOTE, 'flex flex-wrap items-center gap-2') },
            el('span', { className: 'min-w-0 flex-1 text-(--ui-text-primary)', role: 'status' }, 'Hermes is doing the Grok part in another chat. Files it saves to Downloads are picked up here.'),
            el(Button, { disabled: Boolean(busy), loading: busy === 'stop', onClick: () => void stopHermes(), variant: 'text' }, 'Stop Hermes')
          ),
        el(
          'ol',
          { 'aria-label': 'What to do in Grok', className: cn(BODY, 'flex list-decimal flex-col gap-2 pl-5') },
          el(
            'li',
            {},
            el(
              'div',
              { className: 'flex flex-wrap items-center gap-2' },
              el(Button, { disabled: !request || Boolean(busy), onClick: () => void copyRequest(), variant: copied === true ? 'secondary' : 'default' }, 'Copy the request'),
              askSaid && el('span', { className: SMALL, role: 'status' }, askSaid)
            ),
            copied === false && request && el('div', { className: 'mt-2 flex flex-col gap-1' }, el('pre', { className: cn(PRE, 'max-h-40 select-all'), tabIndex: 0 }, request), el('div', {}, jsx(CopyButton, { appearance: 'button', buttonVariant: 'secondary', label: 'Copy', showLabel: true, text: request })))
          ),
          el('li', {}, solo ? `Paste it into ${owned(chosen[0].name)} chat in Grok.` : 'Paste it into the chat of each ticked bot in Grok.'),
          el('li', {}, viaMirror
            ? (solo ? 'The bot also saves its file in the shared box folder, and skill sync brings it here. Or save its answer file to your Downloads folder.' : 'Each bot also saves its file in the shared box folder, and skill sync brings it here. Or save each answer file to your Downloads folder.')
            : (solo ? 'Save its answer file to your Downloads folder.' : 'Save each answer file to your Downloads folder.'))
        ),
        el(
          'div',
          { className: cn(NOTE, 'flex flex-wrap items-center gap-2') },
          el(
            'span',
            { className: 'min-w-0 flex-1', role: 'status' },
            watch === 'on'
              ? viaMirror
                ? "Watching the shared folder and Downloads. A file saved in the box arrives after the sync bot's next run, which can take a while."
                : downloadsHere
                ? 'Watching your Downloads folder: each answer file is brought in as it lands.'
                : 'Hermes cannot see a Downloads folder here: drop the files below instead.'
              : scanProblem
                ? `Stopped watching. ${scanProblem.message}`
                : 'Stopped watching after 15 minutes.'
          ),
          watch !== 'on' && el(Button, { disabled: Boolean(busy), onClick: watchAgain, variant: 'secondary' }, 'Watch again')
        ),
        progress,
        unmatched > 0 && el('p', { className: SMALL, role: 'status' }, `${plural(unmatched, 'file')} ${unmatched === 1 ? 'was' : 'were'} not from a ticked bot.`),
        el(
          'div',
          {
            'aria-label': 'Drop the answer files here',
            className: cn('flex flex-col items-center gap-2 rounded-lg border-2 border-dashed px-4 py-4 text-center', over ? 'border-(--ui-text-secondary) bg-(--ui-bg-quinary)' : 'border-(--ui-stroke-secondary)'),
            onDragEnter: hover,
            onDragLeave: e => {
              if (!e.currentTarget?.contains?.(e.relatedTarget)) {
                setOver(false)
              }
            },
            onDragOver: hover,
            onDrop: e => {
              e.preventDefault()
              e.stopPropagation?.()
              setOver(false)
              void readFiles(e.dataTransfer?.files)
            },
            role: 'group'
          },
          el('h3', { className: HEADING, role: over ? 'status' : undefined }, over ? 'Drop them here' : 'Or drop the answer files here'),
          el('input', {
            accept: '.json,.txt,.md,application/json,text/plain,text/markdown',
            'aria-hidden': true,
            className: 'hidden',
            multiple: true,
            onChange: e => {
              const list = Array.from(e.target.files ?? [])

              // (so that choosing the same files again is a change too)
              e.target.value = ''
              void readFiles(list)
            },
            ref: picker,
            tabIndex: -1,
            type: 'file'
          }),
          el(Button, { disabled: Boolean(busy), loading: busy === 'import', onClick: () => picker.current?.click?.(), variant: 'secondary' }, 'Choose files…')
        ),
        said,
        el('p', { className: SMALL }, privacy),
        el(
          'div',
          { className: 'flex flex-wrap gap-2' },
          el(Button, { disabled: Boolean(busy) || chosenIds.length === 0, onClick: () => setPhase('grok'), variant: 'text' }, 'Let Hermes do the Grok part'),
          syncFor && el(Button, { disabled: Boolean(busy), onClick: () => setAside({ kind: 'sync', pairId: syncFor }), variant: 'text' }, 'Keep their skills the same with Skill sync…')
        )
      )
      footer = [el(Button, { disabled: Boolean(busy), key: 'later', onClick: close, title: 'Watching stops when this closes. Each twin keeps what is left in its row.', variant: 'ghost' }, 'Later')]
    }
  }

  const asidePair = aside ? pairNow(aside.pairId) : null
  const asideSheet =
    asidePair &&
    (aside.kind === 'sync'
      ? jsx(SkillSyncSheet, { onClose: () => setAside(null), open: true, pair: asidePair, storage }, 'aside')
      : jsx(FinishSheet, { onClose: () => setAside(null), open: true, pair: asidePair, storage }, 'aside'))

  return el(Sheet, { description, footer, locked: Boolean(busy), onClose: close, open, title, wide: true }, ...many(body), asideSheet)
}

// ── sample ──────────────────────────────────────────────────────────────────

// What can go wrong, to try on the sample. (What goes right, a twin following its bot, is the sample
// row's own button: See the twin.)
const TRY_IT = [
  { id: 'conflict', label: 'Change it in both places', done: 'A skill was changed in Grok and in Hermes.' },
  { id: 'secret', label: 'Paste a fake password', done: "A fake password was pasted into the sample bot's description." },
  { id: 'hermes-edit', label: 'Change a skill in Hermes', done: 'A skill was changed in Hermes.' }
]

// `quiet`: the page opens the sample's own sheet next, which says the same; no toast on top of it.
async function startSample(run, { quiet = false } = {}) {
  const res = await run('sample', () => post('/demo', { action: 'start' }), 'Could not start the sample')

  // Said only when a sample twin really is on screen.
  if (res && Array.isArray(res.pairs) && res.pairs.length > 0) {
    if (!quiet) {
      host.notify({ durationMs: TOAST_MS, id: `${ID}:sample`, kind: 'success', message: 'Sample twin ready', meta: 'Invented data only. Nothing of yours is touched.' })
    }
  } else if (res) {
    notifyProblem(new TwinError('The sample did not start.', 'sample', 'Try again.'))
  }

  refreshAll()

  return res
}

// One line and one way out. What a try-out did is said here, in the banner, not in a toast over the row
// that is about to show it. `initialSaid` / `initialOpen` let the smoke test render those states.
function SampleBanner({ pairs, hideReal, onEnd, initialSaid = '', initialOpen = false }) {
  const [busy, run] = useBusy()
  const [said, setSaid] = useState(initialSaid)
  const samplePair = pairs.find(isSample)

  async function tryIt(a) {
    const res = await run(a.id, () => post('/demo', { action: a.id }), 'That did not work')

    if (!res) {
      return
    }

    setSaid(`${a.done} Watch the sample twin: it notices in a moment.`)
    refreshAll()
    // The twin notices by itself: no "now press check".
    await pause(2_000)

    try {
      await checkAndToast(samplePair ? { pair_id: samplePair.pair_id } : {}, { quietWhenNothing: true })
    } catch {
      // the background check picks it up at its next round
    }

    await pause(2_000)
    setSaid('')
  }

  async function setHide(value) {
    await run('hide', () => post('/demo', { action: value ? 'hide-real' : 'show-real' }), 'Could not change that')
    refreshAll()
  }

  return el(
    'section',
    { 'aria-label': 'Sample', className: cn(CARD, 'flex flex-col gap-2 border-dashed') },
    el(
      'div',
      { className: 'flex flex-wrap items-center gap-2' },
      jsx(Icon, { className: 'text-(--ui-text-tertiary)', name: 'beaker' }),
      el('p', { className: cn(BODY, 'min-w-0 flex-1 text-(--ui-text-primary)'), role: said ? 'status' : undefined }, said || 'Sample twin with invented data. Nothing of yours is touched.'),
      el(Button, { disabled: Boolean(busy), onClick: onEnd, variant: 'secondary' }, 'End sample'),
      // Rarely needed (handy when recording the screen): tucked into the banner's own menu.
      el(
        DropdownMenu,
        {},
        el(DropdownMenuTrigger, { asChild: true }, el(Button, { 'aria-label': 'More for the sample', size: 'icon-sm', variant: 'ghost' }, jsx(Icon, { name: 'ellipsis' }))),
        el(
          DropdownMenuContent,
          { align: 'end' },
          el(DropdownMenuItem, { onSelect: () => void setHide(!hideReal) }, hideReal ? 'Show my real bots again' : 'Hide my real bots while the sample is on')
        )
      )
    ),
    el(
      Fold,
      { initialOpen, label: 'What can go wrong?', openLabel: 'What can go wrong? Try it on the sample:' },
      el(
        'div',
        { className: 'flex flex-wrap items-center gap-1.5' },
        TRY_IT.map(a => el(Button, { disabled: Boolean(busy), key: a.id, loading: busy === a.id, onClick: () => void tryIt(a), variant: 'outline' }, a.label))
      )
    )
  )
}

// ── other computers ─────────────────────────────────────────────────────────
// The computer with the Grok app shares its list of twins; every other computer links to it once, with a
// link code, and then keeps a twin of each bot by itself. Words on screen: "computer" (never a second
// noun for it), a computer's own name, "your Mac" for the one with the Grok app ("your other Mac" when this
// computer is a Mac too), Share / Stop sharing,
// Link / Unlink, "link code", "in sync". The wire's own words never reach the page, and nobody is asked
// for an address: the link code carries it.

const FLEET_KEY = [ID, 'fleet']
const SHARE_LABEL = 'Share my twins with my other computers'
const LINK_LABEL = 'Link this computer'
const CODE_EXAMPLE = 'ABCD-EFGH-JKMN' // no letter in it that a code never has
// The computer with the Grok app, seen from this one before its name is known.
const macWord = platform => (platform === 'macos' ? 'your other Mac' : 'your Mac')
// What crosses, and what never does: one sentence, shown wherever sharing is switched on or off.
const SHARED_SENTENCE = "Sharing sends your twins' names, descriptions and the settings you let follow, and this computer's name; never chats or passwords, and skills only if switched on."
const WHERE_TO_TYPE = `On your other computer, open Twins in Hermes, press ${LINK_LABEL} and type this code.`
const PLATFORM = { linux: 'Linux', macos: 'Mac', windows: 'Windows' }
const FLEET_ROLES = ['none', 'source', 'follower']
// (`pending`: only in a twin's own list of computers, for a computer that has not got that twin yet)
const COMPUTER_STATES = ['in_sync', 'behind', 'offline', 'never_synced', 'pending', 'unknown']
const KEPT_DAYS = 30

const listOf = v => (Array.isArray(v) ? v : [])
const countOf = v => (Number.isFinite(Number(v)) && v !== null ? Math.max(0, Math.floor(Number(v))) : null)
const isoOf = v => (typeof v === 'string' && Number.isFinite(Date.parse(v)) ? v : null)

// "14:02" for today, "yesterday 14:02", "3 Nov" before that; a clock running ahead never gives a future time.
function sinceWords(iso, now = Date.now()) {
  const ms = Date.parse(iso ?? '')

  if (!Number.isFinite(ms)) {
    return ''
  }

  const d = new Date(Math.min(ms, now))
  const day = t => new Date(t).setHours(0, 0, 0, 0)
  const days = Math.round((day(now) - day(d.getTime())) / DAY_MS)
  const time = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`

  return days <= 0 ? time : days === 1 ? `yesterday ${time}` : shortDay(d.toISOString())
}

// "since 14:02" / "since yesterday 14:02" / "since 3 Nov"
const sinceText = iso => (sinceWords(iso) ? `since ${sinceWords(iso)}` : '')
// "at 14:02" / "yesterday at 14:02" / "on 3 Nov"
function whenText(iso) {
  const words = sinceWords(iso)

  return !words ? '' : /^\d\d:\d\d$/.test(words) ? `at ${words}` : words.startsWith('yesterday') ? words.replace('yesterday ', 'yesterday at ') : `on ${words}`
}

// The newest of several times (any of them may be missing).
function latestOf(...isos) {
  const times = isos.map(t => Date.parse(t ?? '')).filter(Number.isFinite)

  return times.length ? new Date(Math.max(...times)).toISOString() : null
}

// How fresh "in sync" is, truthfully. "in sync now" only while the newest note is under half a minute old;
// after that the age is said ("in sync 45 sec ago", "in sync 3 min ago"), so a computer last heard from a
// minute ago is never "now". Several times may be given (last contact, last change): the newest counts.
const FRESH_EXACT_S = 30

function inSync(...isos) {
  const iso = latestOf(...isos)
  const ms = Date.parse(iso ?? '')

  return !Number.isFinite(ms) ? 'in sync' : Date.now() - ms < FRESH_EXACT_S * 1000 ? 'in sync now' : `in sync ${ago(iso)}`
}

// Relative times on the page move by themselves: one small tick, nothing is asked of the backend.
function useTick(ms = 5_000) {
  const [, setTick] = useState(0)

  useEffect(() => {
    if (typeof timers?.setInterval !== 'function') {
      return undefined
    }

    // the Desktop's own tracked timer: it hands back the way to stop it
    const stop = timers.setInterval(() => setTick(n => n + 1), ms)

    return () => {
      if (typeof stop === 'function') {
        stop()
      }
    }
  }, [ms])
}

function computerOf(m) {
  return {
    error: m?.error && typeof m.error === 'object' ? m.error : null,
    failed: countOf(m?.failed) ?? 0,
    id: clean(m?.id, 40),
    lastSeenAt: isoOf(m?.last_seen_at),
    lastSyncAt: isoOf(m?.last_sync_at),
    name: clean(m?.name, 40) || 'A computer',
    platform: Object.hasOwn(PLATFORM, m?.platform) ? m.platform : 'other',
    source: m?.role === 'source',
    state: COMPUTER_STATES.includes(m?.state) ? m.state : 'unknown',
    this: m?.this === true,
    twins: countOf(m?.twins)
  }
}

// Two computers with the same name are told apart by themselves: "Surface" and "Surface (2)".
function distinctNames(computers) {
  const seen = new Map()

  return computers.map(c => {
    const key = c.name.toLowerCase()
    const n = (seen.get(key) ?? 0) + 1

    seen.set(key, n)

    return n === 1 ? c : { ...c, name: `${c.name} (${n})` }
  })
}

// GET /fleet, tidied once: every list is a list, every name is clean text, every time is a time or null.
// null: this backend has no sharing between computers (an older one), and the page shows none of it.
function fleetOf(raw) {
  if (!raw || typeof raw !== 'object' || !FLEET_ROLES.includes(raw.role)) {
    return null
  }

  const computers = distinctNames(listOf(raw.machines).slice(0, 20).map(computerOf))
  const label = new Map(computers.map(c => [c.id, c.name]))
  const named = m => ({ ...computerOf(m), name: label.get(clean(m?.id, 40)) ?? computerOf(m).name })
  const share = raw.share && typeof raw.share === 'object' ? raw.share : {}
  const code = share.code && typeof share.code === 'object' && clean(share.code.code, 24) ? { expiresAt: isoOf(share.code.expires_at), text: clean(share.code.code, 24).toUpperCase() } : null
  const src = raw.source && typeof raw.source === 'object' ? raw.source : null
  const me = computers.find(c => c.this) ?? computerOf({ ...raw.this_machine, this: true })
  const unlink = raw.last_unlink && typeof raw.last_unlink === 'object' ? raw.last_unlink : null

  return {
    canShare: raw.can_share === true,
    computers,
    me,
    others: computers.filter(c => !c.this),
    removed: listOf(raw.removed)
      .slice(0, 50)
      .map(r => ({ keptUntil: isoOf(r?.kept_until), name: clean(r?.name, 60) || 'A twin', profile: clean(r?.profile, 64), reason: clean(r?.reason, 40), removedAt: isoOf(r?.removed_at), rid: clean(r?.rid, 60) }))
      .filter(r => r.rid),
    role: raw.role,
    share: {
      address: clean(share.address, 80),
      code,
      network: clean(share.network, 20),
      on: share.on === true,
      port: countOf(share.port),
      problem: share.problem && typeof share.problem === 'object' ? share.problem : null,
      state: ['off', 'on', 'starting', 'error'].includes(share.state) ? share.state : share.on === true ? 'on' : 'off'
    },
    source: src
      ? {
          lastContactAt: isoOf(src.last_contact_at),
          lastSyncAt: isoOf(src.last_sync_at),
          name: label.get(clean(src.id, 40)) ?? (clean(src.name, 40) || 'your Mac'),
          offlineSince: isoOf(src.offline_since),
          problem: src.problem && typeof src.problem === 'object' ? src.problem : null,
          state: ['syncing', 'in_sync', 'offline', 'sharing_off', 'error'].includes(src.state) ? src.state : 'syncing'
        }
      : null,
    twins: listOf(raw.twins)
      .slice(0, 500)
      .map(t => ({
        computers: listOf(t?.machines).slice(0, 20).map(named),
        description: clean(t?.description, 400),
        error: t?.error && typeof t.error === 'object' ? t.error : null,
        id: clean(t?.id, 40),
        name: clean(t?.name, 60) || 'A twin',
        // optional: one sentence from the backend about this twin here ("…so this twin is test-quill-2")
        note: plainText(t?.note, 200),
        profile: clean(t?.profile, 64),
        // optional: when a new name or description last arrived here, and which of the two
        // ("new": its first copy arrived then)
        changed: ['name', 'description', 'new'].includes(t?.changed) ? t.changed : '',
        changedAt: isoOf(t?.changed_at),
        state: ['in_sync', 'error', 'not_listed'].includes(t?.state) ? t.state : 'in_sync',
        syncedAt: isoOf(t?.synced_at)
      })),
    unlinked: unlink ? { at: isoOf(unlink.at), bySource: unlink.by === 'source', name: clean(unlink.source_name, 40) || 'your Mac' } : null
  }
}

const cachedFleet = () => fleetOf(queryClient.getQueryData?.(FLEET_KEY))

// A fleet answer in hand is shown at once (the code after the switch, the twins after linking); the
// next poll confirms it.
function showFleet(res) {
  if (res && typeof res === 'object' && FLEET_ROLES.includes(res.role)) {
    queryClient.setQueryData?.(FLEET_KEY, res)
  }

  refreshAll()
}

// What a computer on the Mac's list is doing, in the list's own short words.
function computerSentence(c) {
  const kind = PLATFORM[c.platform] ?? ''
  let doing = ''

  if (c.state === 'in_sync') doing = inSync(c.lastSyncAt, c.lastSeenAt)
  else if (c.state === 'behind') doing = c.failed > 0 ? `${plural(c.failed, 'twin')} could not be made there` : 'catching up'
  else if (c.state === 'offline') doing = ago(c.lastSeenAt) ? `last seen ${ago(c.lastSeenAt)}` : 'not seen yet'
  else if (c.state === 'never_synced') doing = 'linked, getting its first twins'

  return [kind, doing].filter(Boolean).join(' · ')
}

const computerTone = c => (c.state === 'in_sync' ? 'good' : c.state === 'behind' ? (c.failed > 0 ? 'warn' : 'busy') : c.state === 'never_synced' ? 'busy' : 'muted')

// Where a twin's other copies are, for its row: "also on Surface & Orchard", "also on 3 computers".
// How fresh each one is belongs to the list of computers (said once there) and to the hover text here;
// only trouble that is this twin's own is said on its row ("could not be made on Surface").
// `skipId`: a computer the row's own sentence already names.
function copiesOf(computers, skipId = '') {
  return listOf(computers)
    .map(c => (typeof c?.source === 'boolean' ? c : computerOf(c)))
    .filter(c => !c.this && c.id !== skipId)
}

// A copy counts as being on a computer only once that computer has made it: one it has not got yet
// (sharing was off, or that computer was) is "not on Studio yet", never "also on Studio".
const copyThere = c => !c.error && c.state !== 'pending'
const namesOf = list => (list.length <= 2 ? joinWords(list.map(c => c.name)) : `${list.length} computers`)

// When a copy that is still to come gets there, in the terminal's own words (`hermes twin fleet` says the same).
// `now`: what the page knows of sharing right now ({ on, states: computer id -> its state }); without it only
// what is certain is said.
function comingWords(coming, now = null) {
  const to = namesOf(coming)

  if (!now) {
    return `not on ${to} yet`
  }

  if (!now.on) {
    return `goes to ${to} when sharing is on again`
  }

  return coming.every(c => ['in_sync', 'behind', 'never_synced'].includes(now.states.get(c.id))) ? `on its way to ${to}` : `goes to ${to} when ${coming.length === 1 ? 'it is' : 'each is'} next on`
}

// Words of a where-line that are this twin's own news (a copy still to come, or one that failed): shown on its
// row even when rows otherwise leave "where" to the list of computers.
const WHERE_NEWS = /could not|not on |goes to |on its way/

function alsoOn(computers, skipId = '', now = null) {
  const others = copiesOf(computers, skipId)
  const there = others.filter(copyThere)
  const coming = others.filter(c => !c.error && c.state === 'pending')
  const failed = others.filter(c => c.error)

  return [
    there.length > 0 && `also on ${namesOf(there)}`,
    coming.length > 0 && comingWords(coming, now),
    failed.length > 0 && `could not be made on ${failed.length === 1 ? failed[0].name : `${failed.length} computers`}`
  ]
    .filter(Boolean)
    .join(' · ')
}

// Is this twin on every one of these computers (ids), with nothing waiting and nothing failed? Then the
// list of computers says where the twins are, once, and its row need not.
function onAll(computers, ids, skipId = '') {
  const others = copiesOf(computers, skipId)

  return others.every(copyThere) && others.map(c => c.id).sort().join('|') === ids
}

// The same, one computer at a time, for the hover text: "Surface: in sync 30 sec. ago. Orchard: last in sync 2 hr. ago."
function copiesDetail(computers, skipId = '') {
  return copiesOf(computers, skipId)
    .map(c => {
      const when = ago(c.lastSyncAt ?? c.lastSeenAt)
      const doing = c.error ? 'could not be made there' : c.state === 'pending' ? 'gets this twin when it is next in touch' : c.state === 'in_sync' ? inSync(c.lastSyncAt, c.lastSeenAt) : c.state === 'offline' ? (when ? `last in sync ${when}` : 'not seen yet') : c.state === 'behind' ? 'catching up' : c.state === 'never_synced' ? 'getting its first twins' : ''

      return doing ? `${c.name}: ${doing}.` : ''
    })
    .filter(Boolean)
    .join(' ')
}

// The "where it is" part of a row: its own small sign and words, after the state sentence.
function WhereLine({ text, detail = '' }) {
  return text ? el('span', { className: 'inline-flex min-w-0 items-center gap-1.5 text-(--ui-text-secondary)', title: detail || undefined }, jsx(Icon, { name: 'device-desktop' }), el('span', { className: 'min-w-0 whitespace-nowrap' }, text)) : null
}

// A sentence for each thing that can stop a link, in the page's own words: none names a button that is
// not on this sheet, none asks for an address. `clear`: the code can never work again, so the field empties.
function linkProblem(err, mac = 'your Mac', wellFormed = false) {
  const e = err instanceof TwinError || typeof err?.code !== 'string' ? asProblem(err) : { code: clean(err.code, 40), hint: plainText(err.hint), message: plainText(err.message) || FALLBACK_PROBLEM }
  const fromMac = `Make a new code on ${mac} and type it here.`

  // The call never reached the plugin on this computer: nothing was tried, and the other computer is not to blame.
  if (e.transport === true) {
    return { clear: false, code: 'no_answer', hint: 'Nothing was changed. Press Link again in a moment.', message: 'Hermes could not reach Grok Bot Twin on this computer.' }
  }

  // Twelve good characters that are refused look like a code: they are the wrong code, not "not a code".
  const code = e.code === 'bad_code' && wellFormed ? 'code_wrong' : e.code
  const known = {
    bad_code: [`That is not a link code. A link code looks like ${CODE_EXAMPLE}.`, `Copy it from the Twins page on ${mac}.`],
    code_expired: ['That code has expired. A code lasts 10 minutes.', fromMac, true],
    code_used: ['That code was already used. Each code links one computer.', fromMac, true],
    code_wrong: [`That code does not match the one on ${mac}. Nothing was changed.`, 'Correct it and press Link again.'],
    own_code: ['That code is from this computer.', 'Type it on your other computer.'],
    too_many_tries: ['Too many wrong codes, so that code no longer works.', fromMac, true],
    unreachable: [`Hermes could not reach ${mac}.`, 'Check it is awake with sharing on, on the same network as this computer.']
  }[code]

  return known ? { clear: known[2] === true, code, hint: known[1], message: known[0] } : { clear: false, code, hint: e.hint, message: e.message }
}

// Sharing cannot listen. `wentOff`: the switch was put back to off (it failed at the moment it was switched
// on); otherwise it stays on and starts by itself once the cause is gone. Tailscale is named only here,
// where starting it is the fix.
function shareProblem(problem, wentOff) {
  const code = clean(problem?.code, 40)
  const then = wentOff ? ', then switch sharing on again.' : '. Sharing starts by itself after that.'
  // (a computer is never restarted for this: Hermes tries again by itself, and the owner may try at once)
  const busyHint = wentOff ? 'Switch sharing on again in a minute. Nothing was changed.' : 'Hermes tries again by itself every minute.'

  if (code === 'no_network') {
    return { detail: plainText(problem?.message, 200), hint: `Put this computer on your home network or start Tailscale${then}`, message: 'Hermes found no home network to share on.' }
  }

  // (the backend names the program when it can tell which: its two sentences are then shown as sent)
  const said = plainText(problem?.message, 200)

  if (code === 'port_busy' && said && !/^Another program\b/.test(said) && plainText(problem?.hint, 200)) {
    return { detail: '', hint: wentOff ? plainText(problem.hint, 200).replace(/; sharing then starts by itself\.?$/, ', then switch sharing on again.') : plainText(problem.hint, 200), message: said }
  }

  if (code === 'port_busy') {
    // (one thing to do that needs no program named; the backend's longer advice, with its number, is under
    // "Technical details")
    return { detail: [plainText(problem?.message, 200), plainText(problem?.hint, 300)].filter(Boolean).join(' '), again: !wentOff, hint: busyHint, message: 'Another program on this computer is using the place Hermes shares from.' }
  }

  return { detail: '', hint: plainText(problem?.hint, 200), message: plainText(problem?.message, 200) || 'Sharing could not start.' }
}

// The life of a link code in words; null once it has run out.
function codeLife(expiresAt, now = Date.now()) {
  const ms = Date.parse(expiresAt ?? '') - now

  if (!Number.isFinite(ms)) {
    return 'Good for 10 minutes'
  }

  if (ms <= 0) {
    return null
  }

  const mins = Math.round(ms / 60_000)

  return mins >= 2 ? `Good for ${mins} minutes` : 'Good for under a minute'
}

// The link code, made for a person: large, in groups of four, with Copy, its life in words and New code.
function LinkCode({ code, busy, onNew }) {
  const life = codeLife(code.expiresAt)
  const box = useRef(null)

  // A new code is what the owner switched sharing on for: it comes into view by itself, once.
  useEffect(() => {
    box.current?.scrollIntoView?.({ behavior: lessMotion() ? 'auto' : 'smooth', block: 'nearest' })
  }, [code.text])

  if (!life) {
    return el(
      'div',
      { className: cn(NOTE, 'flex flex-wrap items-center gap-2'), role: 'status' },
      el('span', { className: 'min-w-0 flex-1' }, 'That link code has expired.'),
      el(Button, { disabled: Boolean(busy), loading: busy === 'code', onClick: onNew, variant: 'secondary' }, 'New code')
    )
  }

  return el(
    'div',
    { className: 'flex flex-col items-center gap-2 rounded-lg bg-(--ui-bg-quinary) px-4 py-4 text-center', ref: box },
    el('span', { className: SMALL }, 'Link code'),
    el(
      'div',
      { className: 'flex flex-wrap items-center justify-center gap-3' },
      el('output', { 'aria-label': `Link code: ${code.text.replace(/-/g, '').split('').join(' ')}`, className: 'select-all font-mono text-2xl font-semibold tracking-widest text-(--ui-text-primary)' }, code.text),
      jsx(CopyButton, { appearance: 'button', buttonVariant: 'secondary', label: 'Copy', showLabel: true, text: code.text })
    ),
    el('p', { className: cn(BODY, 'max-w-md') }, WHERE_TO_TYPE),
    el(
      'div',
      { className: cn(SMALL, 'flex flex-wrap items-center justify-center gap-x-2') },
      el('span', {}, `${life} · links one computer`),
      el(Button, { disabled: Boolean(busy), loading: busy === 'code', onClick: onNew, variant: 'text' }, 'New code')
    )
  )
}

// One computer, in the same row grammar as a twin: picture, name, one sentence, a menu.
function ComputerRow({ computer, sentence, tone, tags = [], menu = null }) {
  return el(
    'article',
    { 'aria-label': `${computer.name}, a computer`, className: cn(CARD, 'flex min-w-0 items-center gap-3') },
    el('span', { 'aria-hidden': true, className: 'grid size-9 shrink-0 place-items-center rounded-md bg-(--ui-bg-quaternary) text-(--ui-text-secondary)' }, jsx(Icon, { name: 'device-desktop', size: '1rem' })),
    el(
      'div',
      { className: 'flex min-w-0 flex-1 flex-col gap-0.5' },
      el('div', { className: 'flex min-w-0 items-center gap-2' }, jsx(Truncate, { className: 'text-[0.9375rem] font-semibold text-(--ui-text-primary)', max: 40, text: computer.name }), tags.map(t => jsx(Tag, { children: t }, t))),
      sentence && el('div', { className: cn('flex min-w-0 flex-wrap items-center gap-x-1.5', BODY) }, el('span', { className: 'inline-flex min-w-0 items-center gap-1.5' }, tone && jsx(StateIcon, { tone }), el('span', { className: 'min-w-0' }, sentence)))
    ),
    menu
  )
}

// The one sentence and the two lists of a leaving, as the backend planned it (nothing is changed by asking).
function LeavingBody({ plan, fallback }) {
  if (plan.isPending) {
    return jsx(LinesSkeleton, { rows: 3 })
  }

  if (plan.isError) {
    return jsx(InlineProblem, { error: plan.error, onRetry: () => void plan.refetch() })
  }

  const goes = lines(plan.data?.goes, [])
  // The way back is said once: in the list when it says the twins are kept, else in its own line.
  // (After confirming, the result line carries it again, with the button.)
  const undo = goes.some(l => /\bkept\b/i.test(l)) ? '' : plainText(plan.data?.undo, 200)

  return el(
    'div',
    { className: 'flex flex-col gap-2.5' },
    el('p', { className: cn(BODY, 'text-(--ui-text-primary)') }, plainText(plan.data?.sentence, 240) || fallback),
    jsx(StaysGoes, { goes, stays: lines(plan.data?.stays, []) }),
    undo && el('p', { className: BODY }, undo)
  )
}

// On the Mac: stop sharing with one computer. Its twins leave that computer only, and are kept there.
function UnlinkComputerDialog({ computer, open, onClose, onAgain }) {
  const path = `/fleet/machines/${encodeURIComponent(computer.id)}/remove`
  const plan = useQuery({ enabled: open, gcTime: 0, queryFn: () => post(path, { dry_run: true }), queryKey: [ID, 'unlink-computer-plan', computer.id], retry: false })

  return jsx(Confirm, {
    busyLabel: 'Unlinking…',
    cancelLabel: 'Cancel',
    children: jsx(LeavingBody, { fallback: `${computer.name} stops getting your twins. Its copies leave it the next time it is on, and are kept there for ${KEPT_DAYS} days.`, plan }),
    confirmLabel: `Unlink ${computer.name}`,
    destructive: true,
    onClose,
    onConfirm: async () => {
      if (!plan.data) {
        throw new Error('One moment: still working out what will change.')
      }

      const res = await post(path, { dry_run: false })

      host.notify({
        action: onAgain ? { label: 'Link it again', onClick: onAgain } : undefined,
        durationMs: WARNING_TOAST_MS,
        id: `${ID}:fleet:unlinked`,
        kind: 'success',
        message: `${computer.name} is unlinked.`,
        meta: [plainText(res?.undo ?? plan.data?.undo, 200) || `Link it again within ${KEPT_DAYS} days and its twins come back with their chats.`, !onAgain && 'Switch sharing on to link it again.'].filter(Boolean).join(' ')
      })
      refreshAll()
    },
    open,
    title: `Unlink ${computer.name}?`
  })
}

// On the other computer: leave the Mac's twins. Only this computer's copies go, and they are kept.
// No toast afterwards: the page itself becomes the result (what happened, what became of the twins, and
// Link as its filled button).
function UnlinkDialog({ fleet, open, onClose, storage }) {
  const from = fleet.source?.name ?? 'your Mac'
  const plan = useQuery({ enabled: open, gcTime: 0, queryFn: () => post('/fleet/unlink', { dry_run: true }), queryKey: [ID, 'unlink-plan'], retry: false })

  return jsx(Confirm, {
    busyLabel: 'Unlinking…',
    cancelLabel: 'Cancel',
    children: jsx(LeavingBody, { fallback: `This computer stops following ${from}. Its twins leave this computer only, and are kept for ${KEPT_DAYS} days.`, plan }),
    confirmLabel: 'Unlink this computer',
    destructive: true,
    onClose,
    onConfirm: async () => {
      if (!plan.data) {
        throw new Error('One moment: still working out what will change.')
      }

      // Hermes may be inside one of the twins that are about to leave: step out first
      await stepOutOfTwin(storage, twinProfilesOf([], fleet))
      await post('/fleet/unlink', { dry_run: false })
      // remembered, so the first screen can say truthfully who ended the link (see unlinkedHere)
      storage?.set(UNLINKED_KEY, new Date().toISOString())
      refreshAll()
    },
    open,
    title: `Unlink this computer from ${from}?`
  })
}

// A computer's shown name. Never asked for: the computer's own name is the default.
function RenameSheet({ computer, open, onClose, initialName, initialProblem = null }) {
  const [busy, run] = useBusy()
  const [name, setName] = useState(initialName ?? computer.name)
  const [problem, setProblem] = useState(initialProblem)

  const save = () =>
    void run('save', async () => {
      try {
        showFleet(await post('/fleet/name', { name: name.trim() }))
        onClose()
      } catch (err) {
        setProblem(asProblem(err))
      }
    })

  return el(
    Sheet,
    {
      description: 'Your other computers show this one under this name.',
      footer: [el(Button, { key: 'cancel', onClick: onClose, variant: 'ghost' }, 'Cancel'), el(Button, { disabled: !name.trim() || Boolean(busy), key: 'save', loading: busy === 'save', onClick: save }, 'Save name')],
      onClose,
      open,
      title: 'Rename this computer'
    },
    jsx(Input, {
      'aria-describedby': problem ? 'gbt-name-problem' : undefined,
      'aria-invalid': problem ? true : undefined,
      'aria-label': "This computer's name",
      maxLength: 40,
      onChange: e => {
        setName(e.target.value)
        setProblem(null)
      },
      onKeyDown: e => e.key === 'Enter' && name.trim() && save(),
      value: name
    }),
    problem && el('p', { className: cn(BODY, 'text-(--ui-text-primary)'), id: 'gbt-name-problem', role: 'alert' }, [problem.message, problem.hint].filter(Boolean).join(' '))
  )
}

// On the Mac: the one switch, the link code, and the computers that follow it. `initial…` let the smoke
// test render a failed switch, an open dialog and the sheet for a name.
function ShareSection({ fleet, initialProblem = null, initialConfirm = null, initialRename = false, initialDetails = false }) {
  const [busy, run] = useBusy()
  const [problem, setProblem] = useState(initialProblem) // why the switch went back to off
  const [confirm, setConfirm] = useState(initialConfirm) // null | 'off' | a computer's id
  const [rename, setRename] = useState(initialRename)
  const { others, share } = fleet
  const listening = share.on && share.state !== 'error'
  const trouble = share.on && share.state === 'error' ? shareProblem(share.problem, false) : problem
  const leaving = others.find(c => c.id === confirm) ?? null
  const names = joinWords(others.map(c => c.name))

  async function turn(on) {
    try {
      showFleet(await post('/fleet/share', { on }))
    } catch (err) {
      const e = asProblem(err)

      if (on && (e.code === 'no_network' || e.code === 'port_busy')) {
        // Nothing can listen: the switch does not stay on pretending. One thing to do, then switch it on again.
        try {
          await post('/fleet/share', { on: false })
        } catch {
          // it stays on then, and the card says it starts by itself
        }

        setProblem(shareProblem(e, true))
        refreshAll()

        return
      }

      throw err
    }
  }

  function flip(on) {
    setProblem(null)

    // Computers follow this one: say what happens to them before sharing stops.
    if (!on && others.length > 0) {
      setConfirm('off')

      return
    }

    void run('share', () => turn(on), on ? 'Could not start sharing' : 'Could not stop sharing')
  }

  const newCode = () =>
    void run(
      'code',
      async () => {
        await post('/fleet/code', {})
        refreshAll()
      },
      'Could not make a link code'
    )

  return el(
    'section',
    { 'aria-label': 'Your other computers', className: 'flex flex-col gap-3 pt-2' },
    // (for a screen reader only: a computer that links or leaves while the page is open changes the list
    // without a word; this line is always there, so its change is read out, politely)
    // (while sharing cannot start it does not claim to be sharing)
    el('p', { className: 'sr-only', role: 'status' }, share.on && share.state === 'error' ? 'Sharing is not working right now.' : share.on && others.length > 0 ? `Sharing with ${names}.` : ''),
    el(
      'div',
      { className: cn(CARD, 'flex flex-col gap-3') },
      el(
        'div',
        { className: 'flex items-start gap-3' },
        el(
          'div',
          { className: 'flex min-w-0 flex-1 flex-col gap-0.5' },
          el('label', { className: HEADING, htmlFor: 'gbt-share' }, SHARE_LABEL),
          // said where sharing is decided or a code is given out; a page with computers already linked stays calm
          // (the same sentence is in Privacy)
          // (not while sharing is off with nothing linked: the switch's own label says what it does, and a calm
          // page stays within its sixty words)
          // (and not again once a computer is linked, with each new code or while sharing is switched off: the
          // owner read it at the first link, and it stays in Privacy)
          others.length === 0 && share.on && el('p', { className: SMALL }, SHARED_SENTENCE)
        ),
        jsx(SwitchTarget, { children: jsx(Switch, { 'aria-label': SHARE_LABEL, checked: share.on, disabled: Boolean(busy), id: 'gbt-share', onCheckedChange: v => flip(v === true) }), htmlFor: 'gbt-share' })
      ),
      busy === 'share' && !share.on && el('p', { className: BODY, role: 'status' }, 'Starting to share…'),
      listening && share.state === 'starting' && !share.code && el('p', { className: BODY, role: 'status' }, 'Starting to share…'),
      trouble &&
        el(
          'div',
          { className: cn(NOTE, 'flex flex-col gap-1'), role: 'alert' },
          el('span', { className: 'text-(--ui-text-primary)' }, trouble.message),
          trouble.hint && el('span', {}, trouble.hint),
          // (asking again is always allowed; nothing is restarted for it)
          trouble.again && el('div', {}, el(Button, { disabled: Boolean(busy), loading: busy === 'share', onClick: () => void run('share', () => turn(true), 'Could not start sharing'), variant: 'secondary' }, 'Try again'))
        ),
      listening && share.code && jsx(LinkCode, { busy, code: share.code, onNew: newCode }),
      // Sharing is on, no code is showing and no computer is linked: one sentence, one button.
      listening &&
        !share.code &&
        share.state === 'on' &&
        others.length === 0 &&
        el(
          'div',
          { className: 'flex flex-wrap items-center gap-2' },
          el('p', { className: cn(BODY, 'min-w-0 flex-1') }, 'No other computer is linked.'),
          el(Button, { disabled: Boolean(busy), loading: busy === 'code', onClick: newCode, variant: 'secondary' }, 'New code')
        ),
      !share.on && others.length > 0 && el('p', { className: BODY, role: 'status' }, `Sharing is off. ${names} ${others.length === 1 ? 'keeps its twins and catches' : 'keep their twins and catch'} up when you switch it on again.`),
      // What nobody has to know: the name the others see (changing it is possible, never asked for) and where they reach this computer.
      (share.on || others.length > 0 || trouble?.detail) &&
        el(
          Fold,
          { initialOpen: initialDetails, label: 'Technical details', openLabel: 'Hide technical details' },
          el(
            'div',
            { className: cn(SMALL, 'flex flex-col gap-1') },
            // the backend's own words for why sharing cannot start (they may name what the page does not)
            trouble?.detail && el('p', {}, trouble.detail),
            el('div', { className: 'flex flex-wrap items-center gap-x-2' }, el('span', {}, `Your other computers see this one as ${fleet.me.name}.`), el(Button, { onClick: () => setRename(true), variant: 'text' }, 'Rename…')),
            listening &&
              share.address &&
              el(
                'p',
                {},
                `They reach it at ${share.address}${share.port ? `, port ${share.port}` : ''}${share.network === 'tailscale' ? ', over Tailscale (encrypted)' : share.network === 'lan' ? ', over your home network (not encrypted: others on that network could read the names and descriptions)' : ''}. Each linked computer has its own pass, and nothing is reachable without one.`
              )
          )
        )
    ),
    others.map(c =>
      jsx(
        ComputerRow,
        {
          computer: c,
          menu: el(
            DropdownMenu,
            {},
            el(DropdownMenuTrigger, { asChild: true }, el(Button, { 'aria-label': `More for ${c.name}`, size: 'icon-sm', variant: 'ghost' }, jsx(Icon, { name: 'ellipsis' }))),
            el(DropdownMenuContent, { align: 'end' }, el(DropdownMenuItem, { onSelect: () => setConfirm(c.id) }, `Unlink ${c.name}…`))
          ),
          // (while sharing is off nothing is heard from it: when it was last seen, not a promise that it waits)
          sentence: share.on ? computerSentence(c) : [PLATFORM[c.platform], ago(c.lastSeenAt ?? c.lastSyncAt) ? `last seen ${ago(c.lastSeenAt ?? c.lastSyncAt)}` : 'not seen while sharing is off'].filter(Boolean).join(' · '),
          tone: share.on ? computerTone(c) : 'muted'
        },
        c.id
      )
    ),
    // One more computer: the same place and shape as "Add a bot" under the twins.
    listening &&
      !share.code &&
      share.state === 'on' &&
      others.length > 0 &&
      el('div', { className: 'flex justify-center' }, el(Button, { disabled: Boolean(busy), loading: busy === 'code', onClick: newCode, variant: 'outline' }, jsx(Icon, { name: 'add' }), 'Link another computer')),
    jsx(FleetSkills, {}), // the twins' skills for the linked computers (its own switch; off until the owner says so)
    confirm === 'off' &&
      jsx(Confirm, {
        busyLabel: 'Stopping…',
        cancelLabel: 'Cancel',
        children: el(
          'div',
          { className: 'flex flex-col gap-2.5' },
          el('p', { className: cn(BODY, 'text-(--ui-text-primary)') }, `${names} ${others.length === 1 ? 'keeps the twins it has and stops' : 'keep the twins they have and stop'} following until you share again.`),
          el('p', { className: BODY }, `Nothing is removed anywhere. Switch sharing on again and ${others.length === 1 ? 'it catches up by itself' : 'they catch up by themselves'}; no new link code is needed.`)
        ),
        confirmLabel: 'Stop sharing',
        onClose: () => setConfirm(null),
        onConfirm: async () => {
          await turn(false)
        },
        open: true,
        title: 'Stop sharing your twins?'
      }),
    // "Link it again" makes a new code, which needs sharing to be on
    leaving && jsx(UnlinkComputerDialog, { computer: leaving, onAgain: share.on ? newCode : undefined, onClose: () => setConfirm(null), open: true }),
    rename && jsx(RenameSheet, { computer: fleet.me, onClose: () => setRename(false), open: true })
  )
}

// The twelve characters of a link code out of whatever was typed or pasted: capitals, no spaces or dashes.
// A pasted sentence that holds a code gives the code. Look-alikes (O for 0, I or L for 1) are left as typed:
// the backend reads them as the code means them, whatever letters its codes are made of.
function codeChars(raw) {
  const text = String(raw ?? '').toUpperCase()
  const found = /(?<![A-Z0-9])[A-Z0-9]{4}[-\s]?[A-Z0-9]{4}[-\s]?[A-Z0-9]{4}(?![A-Z0-9])/.exec(text)

  return (found ? found[0] : text)
    .replace(/[^A-Z0-9]/g, '')
    .slice(0, 12)
}

const codeShown = chars => (chars.match(/.{1,4}/g) ?? []).join('-')

// Computers this Hermes already knows by name (its own list of connections): asked for only when the Mac
// could not be reached with the code alone. Only the name is shown; the address is never typed or seen.
async function knownComputers() {
  try {
    const rows = typeof host.connections === 'function' ? await host.connections() : []

    return listOf(rows)
      .map(c => {
        let where = clean(c?.host, 255)

        if (!where && typeof c?.url === 'string') {
          try {
            where = new URL(c.url).hostname
          } catch {
            where = ''
          }
        }

        return { id: clean(c?.id, 80), kind: c?.kind, label: clean(c?.label, 40), where }
      })
      .filter(c => (c.kind === 'remote' || c.kind === 'ssh') && c.label && c.where && c.where !== 'localhost' && c.where !== '127.0.0.1')
      .slice(0, 5)
  } catch {
    return []
  }
}

// On the other computer: one string links it. `initial…` let the smoke test render a typed code, each
// refusal and the list of computers Hermes already knows.
// `platform`: this computer's kind (a Mac says "your other Mac"). `onLinked`: the page takes the keyboard
// once the sheet has closed onto the twins.
function LinkSheet({ open, onClose, onLinked, platform = '', initialCode = '', initialProblem = null, initialKnown = [] }) {
  const mac = macWord(platform)
  const [busy, run] = useBusy()
  const [chars, setChars] = useState(() => codeChars(initialCode))
  const [problem, setProblem] = useState(initialProblem)
  const [known, setKnown] = useState(initialKnown)
  const pasted = useRef(false)
  const complete = chars.length === 12
  const lost = problem?.code === 'unreachable'

  const link = (code = chars, at = null) =>
    void run('link', async () => {
      setProblem(null)

      try {
        // No toast: the page the sheet closes onto says where the twins come from and how fresh they are.
        showFleet(await post('/fleet/link', at ? { code: codeShown(code), host: at.where } : { code: codeShown(code) }))
        onClose()
        onLinked?.()
      } catch (err) {
        const p = linkProblem(err, mac, codeChars(code).length === 12)

        setProblem(p)

        if (p.clear) {
          setChars('')
        }

        if (p.code === 'unreachable' && known.length === 0) {
          setKnown(await knownComputers())
        }
      }
    })

  function typed(value) {
    const next = codeChars(value)

    setChars(next)
    setProblem(null)

    // A whole code arriving at once was pasted: there is nothing left to do but link. (A code corrected
    // letter by letter waits for the button: a half-corrected one must not use up a try.)
    const atOnce = pasted.current || next.length - chars.length >= 4

    pasted.current = false

    if (next.length === 12 && next !== chars && atOnce) {
      link(next)
    }
  }

  return el(
    Sheet,
    {
      // (left out while a refusal is on screen: the sheet stays within fifty words)
      description: problem ? undefined : `Your Grok bots stay on ${mac}; this computer gets a twin of each.`,
      footer: [
        el(Button, { disabled: busy === 'link', key: 'cancel', onClick: onClose, variant: 'ghost' }, 'Not now'),
        el(Button, { disabled: !complete || Boolean(busy), key: 'link', loading: busy === 'link', onClick: () => link() }, busy === 'link' ? 'Linking…' : lost ? 'Try again' : 'Link')
      ],
      locked: busy === 'link',
      onClose,
      open,
      title: LINK_LABEL
    },
    el('p', { className: BODY }, `On ${mac}, open Twins and switch on “${SHARE_LABEL}”. Type the code it shows:`),
    jsx(Input, {
      'aria-describedby': problem ? 'gbt-link-problem' : undefined,
      'aria-invalid': problem && !lost ? true : undefined,
      'aria-label': 'Link code',
      autoFocus: true,
      className: 'h-11 text-center font-mono text-xl tracking-widest',
      inputMode: 'text',
      maxLength: 40,
      onChange: e => typed(e.target.value),
      onKeyDown: e => e.key === 'Enter' && complete && !busy && link(),
      onPaste: () => {
        pasted.current = true
      },
      placeholder: CODE_EXAMPLE,
      // not disabled while linking: the field keeps the keyboard, so a refused code can be corrected at once
      readOnly: busy === 'link',
      value: codeShown(chars)
    }),
    problem &&
      el(
        'div',
        { className: cn(NOTE, 'flex flex-col gap-1'), id: 'gbt-link-problem', role: 'alert' },
        el('span', { className: 'text-(--ui-text-primary)' }, problem.message),
        problem.hint && el('span', {}, problem.hint)
      ),
    lost &&
      known.length > 0 &&
      el(
        'div',
        { className: 'flex flex-col gap-1.5' },
        el('p', { className: BODY }, `Or pick ${mac}, if Hermes already knows it:`),
        el(
          'div',
          { className: 'flex flex-wrap gap-1.5' },
          known.map(k => el(Button, { disabled: !complete || Boolean(busy), key: k.id || k.label, onClick: () => link(chars, k), variant: 'outline' }, k.label))
        )
      )
  )
}

// each starts with an ordinary word, so its capital letter never lands on a computer's name
const KEPT_REASON = { removed_on_source: name => `removed on ${name}`, unlinked: () => 'this computer was unlinked', unlinked_by_source: name => `unlinked by ${name}` }

// Hermes bots that were taken from this computer and are still kept: folded. Keeping one makes it an ordinary
// Hermes bot that follows nothing. Linking again brings twins back only on a computer that is not linked now,
// and only those that were not removed on the Mac itself: the fold says "link again" only where that is true.
const KEPT_LABEL = `Hermes bots kept for ${KEPT_DAYS} days`

function KeptBots({ fleet, initialOpen = false, here = false }) {
  const [busy, run] = useBusy()
  const from = fleet.source?.name ?? fleet.unlinked?.name ?? 'your Mac'
  // `here`: the link was ended on this computer, whatever the record says of each bot
  const why = r => (here && r.reason === 'unlinked_by_source' ? 'unlinked' : r.reason)
  const comesBack = !fleet.source && fleet.removed.some(r => r.reason !== 'removed_on_source')

  if (fleet.removed.length === 0) {
    return null
  }

  const keep = r =>
    void run(
      r.rid,
      async () => {
        showFleet(await post(`/fleet/removed/${encodeURIComponent(r.rid)}/restore`, {}))
        host.notify({
          action: r.profile ? { label: 'Open in Hermes', onClick: () => openInHermes(r.profile) } : undefined,
          durationMs: WARNING_TOAST_MS,
          id: `${ID}:fleet:back:${r.rid}`,
          kind: 'success',
          message: `${r.name} is an ordinary Hermes bot now`,
          meta: 'It has its chats and follows nothing.'
        })
      },
      `Could not keep ${r.name}`
    )

  return el(
    Fold,
    { initialOpen, label: `${KEPT_LABEL} (${fleet.removed.length})`, openLabel: KEPT_LABEL },
    el(
      'ul',
      { className: 'flex flex-col gap-1.5' },
      el('li', { className: SMALL, key: 'note' }, comesBack ? 'To get your twins back, link again. Keeping a bot here instead makes it an ordinary Hermes bot that follows nothing.' : 'Keeping a bot makes it an ordinary Hermes bot that follows nothing. Otherwise it leaves this computer on the date shown.'),
      fleet.removed.slice(0, 20).map(r =>
        el(
          'li',
          { className: cn(BOX, 'flex-row flex-wrap items-center'), key: r.rid },
          el(
            'div',
            { className: 'flex min-w-0 flex-1 flex-col' },
            el('span', { className: HEADING }, r.name),
            el('span', { className: SMALL }, [capital((KEPT_REASON[why(r)] ?? (() => 'removed'))(from)), r.keptUntil && `kept with its chats until ${shortDay(r.keptUntil)}`].filter(Boolean).join(' · '))
          ),
          el(Button, { 'aria-label': `Keep ${r.name} as an ordinary Hermes bot`, disabled: Boolean(busy), loading: busy === r.rid, onClick: () => keep(r), variant: 'secondary' }, 'Keep this bot')
        )
      )
    )
  )
}

// "Update Grok Bot Twin on mini: Capabilities → Plugins → Update." when the backend knows which computer is behind.
const UPDATE_BOTH = 'Update Grok Bot Twin on both computers: Capabilities → Plugins.'

function updateHint(hint) {
  // the backend's first sentence, when it names a computer and the way ("…on mini: Capabilities → Plugins → Update.")
  const first = /^Update Grok Bot Twin on [^.:<>]{1,60}: [^.<>]{1,60}\./.exec(plainText(hint, 240))?.[0] ?? ''

  return first || UPDATE_BOTH
}

// Why the twins here are not following right now: one cause, said once above the rows. null: they are.
function followerTrouble(fleet) {
  const s = fleet.source

  if (!s || s.state === 'in_sync' || s.state === 'syncing') {
    return null
  }

  const stay = 'Your twins here stay as they are'

  if (s.state === 'offline') {
    return { after: `${s.name} offline ${sinceText(s.offlineSince)}`.trim(), button: true, row: `${s.name} offline ${sinceText(s.offlineSince)}`.trim(), short: `offline ${sinceText(s.offlineSince)}`.trim(), sentence: `${s.name} has not answered ${sinceText(s.offlineSince)}`.trim() + '. Your twins here still work, and catch up by themselves when it is back.' }
  }

  if (s.state === 'sharing_off') {
    // the header and the rows say since when, as they do for a Mac that is off
    return { after: `sharing off on ${s.name} ${sinceText(s.offlineSince)}`.trim(), button: false, row: `Waiting for ${s.name}`, short: `sharing off ${sinceText(s.offlineSince)}`.trim(), sentence: `Sharing was switched off on ${s.name} ${whenText(s.offlineSince)}`.trim() + `. ${stay} and catch up when it is on again.` }
  }

  const code = clean(s.problem?.code, 40)
  // what to do about it, in the backend's words (they say that the twins stay); else that they stay
  const way = plainText(s.problem?.hint, 240)

  return {
    after: `not following ${s.name} right now`,
    button: false,
    row: `Not following ${s.name} right now`,
    short: 'not followed right now',
    // which computer to update, and where: the backend's sentence when it names one, else both
    sentence: code === 'version_mismatch' ? `${s.name} and this computer run versions of Grok Bot Twin that cannot work together. ${updateHint(s.problem?.hint)} ${stay} meanwhile.` : `${plainText(s.problem?.message, 200) || `Hermes cannot follow ${s.name} right now.`} ${way || `${stay}.`}`
  }
}

// Hermes cannot ask the plugin right now: what is on screen is the last answer, and no row calls it fresh.
// (The cause and Try again are said once, above the rows.)
const NOT_CHECKED = { after: 'not checked just now', button: false, row: 'Not checked just now', sentence: '', short: 'not checked just now' }

// "2 twins from mini · in sync 30 sec. ago": where the twins come from and how fresh, for the header
// and the status bar. A twin that could not be made is counted, not hidden.
function followerSummary(fleet, { stale = false } = {}) {
  const s = fleet.source
  const from = s?.name ?? 'your Mac'
  const failed = fleet.twins.filter(t => t.state === 'error').length
  const here = fleet.twins.length - failed
  const who = failed > 0 ? `${here} of ${plural(fleet.twins.length, 'twin')} ${here === 1 ? 'is' : 'are'} here · from ${from}` : fleet.twins.length === 0 ? `Linked to ${from}` : `${plural(fleet.twins.length, 'twin')} from ${from}`
  const trouble = stale ? NOT_CHECKED : followerTrouble(fleet)

  if (trouble) {
    return { count: failed, label: `${who} · ${trouble.after}`, tone: failed > 0 ? 'warn' : 'muted' }
  }

  if (s?.state === 'syncing') {
    return { count: failed, label: `${who} · getting your twins…`, tone: 'warn' }
  }

  return { count: failed, label: `${who} · ${inSync(s?.lastContactAt, s?.lastSyncAt)}`, tone: failed > 0 ? 'warn' : 'good' }
}

// The same from /status alone (the status bar has no fleet answer in hand).
function briefSummary(brief) {
  const s = brief?.source
  const name = clean(s?.name, 40) || 'your Mac'

  if (s?.state === 'offline') return { count: 0, label: `Twins from ${name} · ${name} offline ${sinceText(s.offline_since)}`.trim(), tone: 'muted' }
  if (s?.state === 'sharing_off') return { count: 0, label: `Twins from ${name} · sharing off on ${name} ${sinceText(s.offline_since)}`.trim(), tone: 'muted' }
  if (s?.state === 'error') return { count: 0, label: `Twins from ${name} · not following right now`, tone: 'warn' }

  return { count: 0, label: `Twins from ${name} · ${s?.state === 'syncing' ? 'getting your twins…' : 'in sync'}`, tone: 'good' }
}

// One twin on a computer that follows the Mac: the same row grammar as on the Mac itself. `lead`: this
// row carries the page's one primary button.
// How long a row says that a change just arrived ("changed 20 sec ago") before it goes back to "in sync".
const ARRIVED_S = 120

function FollowerRow({ twin, fleet, trouble, lead = false, busy, onCheck, showWhere = true, initialDetails = false }) {
  const [details, setDetails] = useState(initialDetails)
  const from = fleet.source?.name ?? 'your Mac'
  const failed = twin.state === 'error'
  const sourceId = fleet.computers.find(c => c.source)?.id ?? ''
  const where = failed ? '' : alsoOn(twin.computers, sourceId)
  // The box's title already says what happened; its one sentence is the cause, and what was left alone.
  // (a file's name in brackets is for a developer: it is never part of the sentence)
  const cause = plainText(twin.error?.hint, 200).replace(/\bpress Check now\b/, 'press Try again').replace(/\s*\([\w.-]+\.md\)/g, '')
  // A Hermes bot of that name was already here: trying again cannot help, so no button offers it.
  const blocked = clean(twin.error?.code, 40) === 'profile_blocked'
  const reason = blocked && cause ? `${cause} Hermes left it as it is.` : cause || plainText(twin.error?.message, 200)
  let sentence
  let tone
  let fresh = ''
  // a description that arrived a moment ago is shown under the row for as long as the row says so
  let newText = ''

  if (failed) {
    sentence = 'Could not be made here'
    tone = 'warn'
  } else if (trouble) {
    sentence = trouble.row
    tone = 'muted'
  } else if (twin.state === 'not_listed') {
    sentence = `No longer on ${owned(from)} list · kept as it is`
    tone = 'muted'
  } else if (fleet.source?.state === 'syncing') {
    sentence = `Follows ${from} · getting it now…`
    tone = 'busy'
  } else {
    // the header carries the time once; a row says the fact, and the time on hover. A new name or description
    // that arrived a moment ago is said on the row for two minutes: a change shows without a click.
    // (the backend's own note of the change when it sends one; else the time this twin was last written here)
    const at = twin.changedAt ?? twin.syncedAt
    const arrived = Date.now() - Date.parse(at ?? '')

    // (a copy that has only just arrived has not "changed": it arrived, or, when the backend does not say which, was updated)
    const what = twin.changedAt && twin.changed === 'new' ? 'arrived' : twin.changedAt ? `${twin.changed ? `${twin.changed} ` : ''}changed` : 'updated'

    sentence = arrived >= 0 && arrived < ARRIVED_S * 1000 ? `Follows ${from} · ${what} ${ago(at)}` : `Follows ${from} · in sync`
    newText = arrived >= 0 && arrived < ARRIVED_S * 1000 && twin.changedAt && twin.changed === 'description' ? twin.description : ''
    tone = 'good'
    fresh = capital(inSync(fleet.source?.lastContactAt, fleet.source?.lastSyncAt))
  }

  return el(
    'article',
    { 'aria-label': `${twin.name} twin`, className: cn(CARD, 'flex flex-col gap-3') },
    el(
      'header',
      { className: 'flex min-w-0 items-center gap-3' },
      jsx(Avatar, { name: twin.name }),
      el(
        'div',
        { className: 'flex min-w-0 flex-1 flex-col gap-0.5' },
        // (its description, as it arrived, is the name's hover text; Details… has it in full)
        el('span', { className: 'min-w-0', title: twin.description || undefined }, jsx(Truncate, { className: 'text-[0.9375rem] font-semibold text-(--ui-text-primary)', max: 60, text: twin.name })),
        el(
          'div',
          { className: cn('flex min-w-0 flex-wrap items-center gap-x-3', BODY) },
          el('span', { className: 'inline-flex min-w-0 items-center gap-1.5', title: fresh || undefined }, jsx(StateIcon, { tone }), el('span', { className: 'min-w-0' }, busy === 'check' ? 'Checking…' : sentence)),
          // (when every twin is on every computer, the line of computers below says so once)
          (showWhere || WHERE_NEWS.test(where)) && jsx(WhereLine, { text: where })
        )
      ),
      // A twin that is here can always be opened, whatever its Mac is doing.
      !failed && twin.profile && el(Button, { 'aria-label': `Open ${twin.name} in Hermes`, onClick: () => openInHermes(twin.profile), variant: 'secondary' }, 'Open in Hermes'),
      el(
        DropdownMenu,
        {},
        el(DropdownMenuTrigger, { asChild: true }, el(Button, { 'aria-label': `More for ${twin.name}`, size: 'icon-sm', variant: 'ghost' }, jsx(Icon, { name: 'ellipsis' }))),
        el(DropdownMenuContent, { align: 'end' }, // (not "Check now", which on the Mac means comparing with Grok; and it names what is asked)
          el(DropdownMenuItem, { onSelect: onCheck }, `Check ${from} now`), el(DropdownMenuItem, { onSelect: () => setDetails(true) }, 'Details…'))
      )
    ),
    failed &&
      el(
        Matter,
        {
          actions: blocked ? null : el(Button, { disabled: Boolean(busy), loading: busy === 'check', onClick: onCheck, variant: mainVariant(lead) }, 'Try again'),
          icon: ICON_OF.error,
          label: 'Could not be made here',
          title: `${twin.name} could not be made on this computer`
        },
        el('p', { className: BODY }, reason || `Hermes tries again by itself. Your other twins are in sync, and nothing changed on ${from}.`)
      ),
    // what the backend has to say about this twin here, once, quietly
    !failed && twin.note && el('p', { className: SMALL }, twin.note),
    // the change itself, without a click, while the row says that it arrived
    newText && el('p', { className: cn(SMALL, 'min-w-0') }, jsx(Truncate, { max: 160, text: newText })),
    // what this twin is, as it arrived: its description, where it comes from and where else it lives
    details &&
      el(
        Sheet,
        { description: `A twin from ${from}. Its name and description change here when they change there.`, onClose: () => setDetails(false), open: true, title: twin.name },
        el('div', { className: 'flex flex-col gap-0.5' }, el('span', { className: HEADING }, 'Description'), el('p', { className: cn(BODY, 'break-words') }, twin.description || 'No description yet.')),
        el('p', { className: SMALL }, [twin.syncedAt && ago(twin.syncedAt) ? `Last changed here ${ago(twin.syncedAt)}` : '', capital(where)].filter(Boolean).join(' · ') || `On this computer and ${from}.`)
      )
  )
}

// A computer that follows the Mac: where its twins come from and how fresh, one row per twin, the
// computers that hold them, and the one way out.
// `noLead`: the page's one main button is elsewhere right now (a request waits for the owner's answer).
function FollowerView({ fleet, onUnlink, initialRename = false, stale = false, noLead = false }) {
  const [busy, run] = useBusy()
  const [rename, setRename] = useState(initialRename)
  const trouble = stale ? NOT_CHECKED : followerTrouble(fleet)
  const from = fleet.source?.name ?? 'your Mac'
  const firstFailed = noLead ? null : (fleet.twins.find(t => t.state === 'error' && clean(t.error?.code, 40) !== 'profile_blocked')?.id ?? null)
  // Is every twin on every computer of the list? Then the list says where they are, once, and no row repeats it.
  const sourceId = fleet.computers.find(c => c.source)?.id ?? ''
  const elsewhere = fleet.computers.filter(c => !c.this && c.id !== sourceId).map(c => c.id).sort().join('|')
  const everywhere = fleet.twins.every(t => t.state === 'error' || onAll(t.computers, elsewhere, sourceId))
  const sourceSaid = trouble ? trouble.short : 'your twins come from here'
  const meSaid = stale ? NOT_CHECKED.short : trouble ? `waiting for ${from}` : inSync(fleet.source?.lastContactAt, fleet.source?.lastSyncAt)
  const check = () =>
    void run(
      'check',
      async () => {
        showFleet(await post('/fleet/sync', {}))
      },
      `Could not reach ${from}`
    )

  return el(
    'div',
    { className: 'flex flex-col gap-3' },
    trouble?.sentence &&
      el(
        'div',
        { 'aria-label': `Twins are not following ${from}`, className: cn(NOTE, 'flex flex-wrap items-center gap-2'), role: 'region' },
        jsx(Icon, { className: 'text-(--ui-text-primary)', name: 'circle-slash' }),
        el('span', { className: 'gbt-line min-w-0 flex-1 text-(--ui-text-primary)' }, trouble.sentence)
        // (no button: the sentence says it clears by itself, and a button right after that made people stop
        // and wonder. Whoever wants to save the wait has "Check mini now" in each twin's menu.)
      ),
    fleet.twins.length === 0 &&
      el(
        'p',
        { className: cn(CARD, LEAD), role: 'status' },
        fleet.source?.state === 'syncing' ? `Linked to ${from}. Your twins are on their way and appear here by themselves.` : `Linked to ${from}. It shares no twins yet: twin a Grok bot there and it appears here by itself.`
      ),
    fleet.twins.map(t => jsx(FollowerRow, { busy, fleet, lead: t.id === firstFailed, onCheck: check, showWhere: !everywhere, trouble, twin: t }, t.id)),
    jsx(KeptBots, { fleet }),
    jsx(FleetSkills, {}), // the twins' skills from the computer with the Grok app (its own switch; nothing while the backend says nothing)
    // The computers that hold these twins, in one line of the same row grammar (picture, names, a menu): which
    // one has the Grok app and which one this is. What kind each is and how fresh is its hover text; the header
    // and the rows already say where the twins come from and how fresh they are.
    fleet.computers.length > 0 &&
      el(
        'section',
        { 'aria-label': 'Computers that hold these twins', className: cn(CARD, 'flex min-w-0 items-center gap-3') },
        el('span', { 'aria-hidden': true, className: 'grid size-9 shrink-0 place-items-center rounded-md bg-(--ui-bg-quaternary) text-(--ui-text-secondary)' }, jsx(Icon, { name: 'device-desktop', size: '1rem' })),
        el(
          'ul',
          { className: cn('flex min-w-0 flex-1 flex-wrap items-center gap-x-4 gap-y-1', BODY) },
          el('li', { 'aria-hidden': true, className: 'list-none' }, 'On'),
          fleet.computers.map(c =>
            el(
              'li',
              { className: 'inline-flex min-w-0 list-none items-center gap-1.5', key: c.id, title: [PLATFORM[c.platform], c.source ? sourceSaid : c.this ? meSaid : ''].filter(Boolean).join(' · ') || undefined },
              jsx(Truncate, { className: 'font-semibold text-(--ui-text-primary)', max: 40, text: c.name }),
              // (a Mac that does not answer is marked here too, where the computers are listed)
              c.source && jsx(Tag, { children: fleet.source?.state === 'offline' ? 'Has the Grok app · offline' : 'Has the Grok app' }),
              c.this && jsx(Tag, { children: 'This computer' })
            )
          )
        ),
        el(
          DropdownMenu,
          {},
          el(DropdownMenuTrigger, { asChild: true }, el(Button, { 'aria-label': 'More for this computer', size: 'icon-sm', variant: 'ghost' }, jsx(Icon, { name: 'ellipsis' }))),
          // the same place the Mac has "Unlink Surface…": one place only
          el(DropdownMenuContent, { align: 'end' }, el(DropdownMenuItem, { onSelect: () => setRename(true) }, 'Rename this computer…'), onUnlink && jsx(DropdownMenuSeparator, {}), onUnlink && el(DropdownMenuItem, { onSelect: onUnlink }, 'Unlink this computer…'))
        )
      ),
    rename && jsx(RenameSheet, { computer: fleet.me, onClose: () => setRename(false), open: true })
  )
}

const UNLINKED_KEY = 'unlinked'

// Did the owner end the link on this computer, on this page? The backend's own record wins unless the page
// did it itself within two minutes of that record: a link ended here must never be blamed on the Mac.
function unlinkedHere(fleet, storage) {
  const u = fleet?.unlinked
  const mine = Date.parse(storage?.get(UNLINKED_KEY, null) ?? '')

  return Boolean(u) && (!u.bySource || (Number.isFinite(mine) && Math.abs(Date.parse(u.at ?? '') - mine) < 120_000))
}

// A link ended: said once on the first screen, with what became of the twins and the way back.
// `here`: it was ended on this computer (unlinkedHere).
function unlinkedSentence(fleet, here = !fleet?.unlinked?.bySource) {
  const u = fleet?.unlinked

  if (!u) {
    return ''
  }

  // only what linking again brings back: a twin removed on the Mac itself stays removed
  const kept = fleet.removed.filter(r => r.reason !== 'removed_on_source').length
  const what = kept > 0 ? `Its ${plural(kept, 'twin')} ${kept === 1 ? 'is' : 'are'} kept for ${KEPT_DAYS} days; link again to bring ${kept === 1 ? 'it' : 'them'} back.` : 'Link again to bring its twins back.'

  return `${here ? `This computer was unlinked from ${u.name}` : `${u.name} stopped sharing with this computer`} ${whenText(u.at)}`.trim() + `. ${what}`
}

// What happened between computers while the Twins page was closed. Names never ride in these events.
// ── requests between your computers ("on Surface: …") ───────────────────────
//
// A twin on one computer can ask its copy on another to do something there. This is the asked computer's side:
// the switch (off until the owner turns it on, here, for this computer only), the steps that wait for the
// owner's approval on this computer, and the list of what was asked. Everything shown is text from the
// plugin's own backend, cleaned again here and rendered as text.

const TASKS_KEY = [ID, 'tasks']
const TASKS_LABEL = 'Let my other computers ask this one to do things'
// Beside the switch: one word. The label is the sentence that says what the switch allows; who asks whom,
// and that approvals stay with the owner, is the first line of the fold, on or off.
const TASKS_OFF = 'Off.'
const TASKS_WHEN_ON = 'When on, your twins on your other computers can ask their copies here to do things. Hermes asks for your approval here, by the same rules as in your own chats.'
const TASKS_ON_SHORT = 'On.'
const TASKS_ON = 'Your twins on your other computers can ask their copies here to do things. Hermes asks for your approval here, by the same rules as in your own chats.'
// What happens here when a step needs approval is the owner's choice on this computer (GET /tasks `mode`):
// the last sentence of the line above says which of the three it is.
const TASK_MODES = ['here', 'linked', 'safe']
const TASKS_APPROVAL = {
  here: 'Hermes asks for your approval here, by the same rules as in your own chats.',
  linked: 'Hermes asks for your approval here or on the computer that asked, by the same rules as in your own chats.',
  safe: 'A step that would need your approval is refused at once.'
}
const tasksSaid = (on, mode) => (mode === 'here' ? (on ? TASKS_ON : TASKS_WHEN_ON) : `${on ? 'Your' : 'When on, your'} twins on your other computers can ask their copies here to do things. ${TASKS_APPROVAL[mode]}`)
const TASKS_MODE_TITLE = 'Let your linked computers approve steps here?'
const TASKS_HOME_NETWORK = 'Your computers are linked over your home network: what is asked and what comes back can be read by others on it.'
const TASK_OPEN = ['queued', 'running', 'waiting_approval']

// GET /tasks, tidied once: every list is a list, every text is clean text.
function tasksOf(raw) {
  if (!raw || typeof raw !== 'object' || !raw.switch || typeof raw.switch !== 'object') {
    return null
  }

  const text = (v, max = 300) => clean(typeof v === 'string' ? v : '', max)
  const log = (Array.isArray(raw.log) ? raw.log : [])
    .filter(r => r && typeof r === 'object')
    .slice(0, 50)
    .map(r => ({
      askedAt: text(r.asked_at, 40),
      dir: r.dir === 'out' || r.dir === 'relay' ? r.dir : 'in',
      id: text(r.id, 40),
      label: text(r.label, 120),
      peer: text(r.peer, 40),
      peerTo: text(r.peer_to, 40),
      state: text(r.state, 30),
      text: text(r.text),
      twin: text(r.twin, 100)
    }))
  const waiting = (Array.isArray(raw.waiting) ? raw.waiting : [])
    .filter(w => w && typeof w === 'object' && /^k-[0-9a-f]{16}$/.test(String(w.id ?? '')))
    .slice(0, 5)
    .map(w => ({ command: text(w.command, 400), description: text(w.description), id: w.id, peer: text(w.peer, 40), q: /^[0-9a-f]{8}$/.test(String(w.q ?? '')) ? w.q : '', text: text(w.text), twin: text(w.twin, 100) }))
  // steps that wait on another computer for a request asked from here, which that computer lets this one answer:
  // the answer always names the very step shown (`q`), so an entry without that mark is not shown
  const asking = (Array.isArray(raw.asking) ? raw.asking : [])
    .filter(w => w && typeof w === 'object' && /^k-[0-9a-f]{16}$/.test(String(w.id ?? '')) && /^[0-9a-f]{8}$/.test(String(w.q ?? '')))
    .slice(0, 5)
    .map(w => ({ command: text(w.command, 400), description: text(w.description), id: w.id, peer: text(w.peer, 40) || 'another computer', q: w.q, text: text(w.text), twin: text(w.twin, 100) }))
  // what happens here when a step needs approval; an older backend says nothing, and then nothing is offered
  const m = raw.mode && typeof raw.mode === 'object' ? raw.mode : {}
  const warning = text(m.warning, 300)
  const value = TASK_MODES.includes(m.value) ? m.value : 'here'
  const choices = (Array.isArray(m.choices) ? m.choices : [])
    .filter((c, i, all) => c && typeof c === 'object' && TASK_MODES.includes(c.value) && all.findIndex(o => o?.value === c.value) === i)
    .map(c => ({ label: text(c.label, 80), note: text(c.note, 200), value: c.value }))
    // (letting other computers approve is never offered without the sentence that says what it means)
    .filter(c => c.label && (c.value !== 'linked' || warning || value === 'linked'))

  return {
    asking,
    computers: (Array.isArray(raw.machines) ? raw.machines : []).map(m => text(m?.name, 40)).filter(Boolean),
    // which of them take requests, when the backend says (optional): the example then names one that does
    taking: (Array.isArray(raw.machines) ? raw.machines : []).filter(m => m?.takes_requests === true).map(m => text(m?.name, 40)).filter(Boolean),
    // linked over a plain home network: requests and answers are signed there, but can be read
    homeNetwork: raw.switch.network === 'lan',
    linked: raw.linked === true,
    log,
    mode: { choices, label: text(m.label, 80) || 'When a step needs approval', value, warning },
    on: raw.switch.on === true,
    open: log.some(r => TASK_OPEN.includes(r.state)),
    waiting
  }
}

// Who asked whom, in the owner's words.
function requestTitle(r) {
  const twin = r.twin || 'a twin'

  if (r.dir === 'out') {
    return `You asked ${twin} on ${r.peer}`
  }

  if (r.dir === 'relay') {
    return `${r.peer} asked ${r.peerTo || 'another computer'}`
  }

  return `${r.peer} asked ${twin} here`
}

// One read of GET /tasks for both parts below (the same query key: asked once, shown twice).
function useRequests(enabled = true) {
  const q = useQuery({ enabled, queryFn: () => api('/tasks'), queryKey: TASKS_KEY, refetchInterval: query => (tasksOf(query?.state?.data)?.open ? 2_000 : 10_000), retry: 1 })
  const tasks = tasksOf(q.data)

  // Nothing to say on a computer that is not linked to another one.
  return tasks && tasks.linked && tasks.computers.length > 0 ? tasks : null
}

const showRequests = res => queryClient.setQueryData(TASKS_KEY, res)

// A step that waits for the owner, here: it is the one thing on the page that needs them, so it comes first.
// What was asked, the step as Hermes describes it, two answers.
// `lead`: the first waiting step holds the page's one main button (nothing else on the page is filled then).
function RequestsWaiting({ lead = false }) {
  const [busy, run] = useBusy()
  const tasks = useRequests()

  if (!tasks || tasks.waiting.length + tasks.asking.length === 0) {
    return null
  }

  // A step that waits on another computer for a request sent from here, which that computer lets this one answer.
  const answerThere = (w, choice) =>
    void run(
      `${choice}:there:${w.id}`,
      async () => {
        try {
          // `q` names the very step shown here, always: the answer cannot count for a later step
          showRequests(await post(`/tasks/${encodeURIComponent(w.id)}/answer`, { choice, q: w.q }))
        } catch (err) {
          // another step waits by now: show that one, then say so
          if (asProblem(err).code === 'step_changed') {
            void queryClient.invalidateQueries({ queryKey: TASKS_KEY })
          }

          throw err
        }

        host.notify({ id: `${ID}:tasks:answered`, kind: choice === 'once' ? 'success' : 'info', message: choice === 'once' ? `Sent to ${w.peer}: do that one step once.` : `Sent to ${w.peer}: don't do that step.` })
      },
      'Could not pass your answer on'
    )
  const answer = (w, choice) =>
    void run(
      `${choice}:${w.id}`,
      async () => {
        try {
          // `q` names the very step shown here: the answer cannot count for a later step of the same request
          showRequests(await post(`/tasks/${encodeURIComponent(w.id)}/approve`, w.q ? { choice, q: w.q } : { choice }))
        } catch (err) {
          // another step waits by now: show that one, then say so
          if (asProblem(err).code === 'step_changed') {
            void queryClient.invalidateQueries({ queryKey: TASKS_KEY })
          }

          throw err
        }

        host.notify({ id: `${ID}:tasks:answered`, kind: choice === 'once' ? 'success' : 'info', message: choice === 'once' ? 'Approved. That one step goes ahead.' : 'Refused. That step is not done.' })
      },
      'Could not pass your answer on'
    )

  return el(
    'div',
    { className: 'flex flex-col gap-3' },
    tasks.waiting.map((w, i) =>
      el(
        'div',
        { 'aria-label': `A request from ${w.peer} waits for your approval`, className: cn(NOTE, 'flex flex-col gap-2'), key: w.id, role: 'alert' },
        el('span', { className: 'text-(--ui-text-primary)' }, `${w.peer} asked ${w.twin || 'a twin'} here: ${w.text}`),
        el('span', {}, w.description ? `One step needs your approval: ${w.description}.` : 'One step needs your approval.'),
        w.command && el('code', { className: 'min-w-0 break-all rounded-md bg-(--ui-bg-quaternary) px-2.5 py-1 font-mono text-xs text-(--ui-text-primary)' }, w.command),
        el(
          'div',
          { className: 'flex flex-wrap items-center gap-2' },
          // (each spoken name begins with the words on the button: someone who talks to the computer says what they see)
          el(Button, { 'aria-label': `Do it once: the step ${w.peer} asked for`, disabled: Boolean(busy), loading: busy === `once:${w.id}`, onClick: () => answer(w, 'once'), variant: mainVariant(lead && i === 0) }, 'Do it once'),
          el(Button, { 'aria-label': `Don't do the step ${w.peer} asked for`, disabled: Boolean(busy), loading: busy === `deny:${w.id}`, onClick: () => answer(w, 'deny'), variant: 'text' }, "Don't"),
          el('span', { className: 'min-w-0 flex-1' }, 'Nothing happens until then.')
        )
      )
    ),
    // (after what waits here: the page's main button goes to the first step of all)
    tasks.asking.map((w, i) =>
      el(
        'div',
        { 'aria-label': `Your request to ${w.peer} waits for your approval`, className: cn(NOTE, 'flex flex-col gap-2'), key: `there:${w.id}`, role: 'alert' },
        el('span', { className: 'text-(--ui-text-primary)' }, `${w.peer} asks before it does one step for your request to ${w.twin || 'a twin'}: ${w.text}`),
        el('span', {}, w.description ? `One step needs your approval: ${w.description}.` : 'One step needs your approval.'),
        w.command && el('code', { className: 'min-w-0 break-all rounded-md bg-(--ui-bg-quaternary) px-2.5 py-1 font-mono text-xs text-(--ui-text-primary)' }, w.command),
        el(
          'div',
          { className: 'flex flex-wrap items-center gap-2' },
          el(Button, { 'aria-label': `Do it once: the step on ${w.peer}`, disabled: Boolean(busy), loading: busy === `once:there:${w.id}`, onClick: () => answerThere(w, 'once'), variant: mainVariant(lead && tasks.waiting.length === 0 && i === 0) }, 'Do it once'),
          el(Button, { 'aria-label': `Don't do the step on ${w.peer}`, disabled: Boolean(busy), loading: busy === `deny:there:${w.id}`, onClick: () => answerThere(w, 'deny'), variant: 'text' }, "Don't"),
          el('span', { className: 'min-w-0 flex-1' }, `Nothing happens on ${w.peer} until then. You can also answer on ${w.peer}.`)
        )
      )
    )
  )
}

// What happens on this computer when a step of a request needs approval: three choices, one line each, as the
// backend words them. Plain radio buttons under one name: Tab reaches the group, the arrow keys move in it.
function ModeChoice({ choice, checked, disabled, onChoose }) {
  const id = `gbt-tasks-mode-${choice.value}`

  return el(
    'div',
    { className: 'flex min-w-0 items-start gap-2.5 py-0.5' },
    // (its spoken name is the words of the choice; the line under them is its description)
    el('input', { 'aria-describedby': choice.note ? `${id}-note` : undefined, 'aria-labelledby': `${id}-label`, checked, className: 'mt-1 shrink-0', disabled, id, name: 'gbt-tasks-mode', onChange: () => onChoose(choice.value), type: 'radio', value: choice.value }),
    el(
      'label',
      { className: cn('flex min-w-0 flex-1 flex-col', !disabled && 'cursor-pointer'), htmlFor: id },
      el('span', { className: 'break-words text-[0.8125rem] leading-snug text-(--ui-text-primary)', id: `${id}-label` }, choice.label),
      choice.note && el('span', { className: SMALL, id: `${id}-note` }, choice.note)
    )
  )
}

function RequestsSection({ initialOpen = false, initialHow = false, initialConfirm = false }) {
  const [busy, run] = useBusy()
  const [confirm, setConfirm] = useState(initialConfirm)
  const tasks = useRequests()

  if (!tasks) {
    return null
  }

  const flip = on => void run('tasks', async () => showRequests(await post('/tasks/switch', { on })), on ? 'Could not switch this on' : 'Could not switch this off')
  const { mode } = tasks
  const setMode = value => run('mode', async () => showRequests(await post('/tasks/mode', { mode: value })), 'Could not change this')
  // Asking here, or refusing such steps, takes nothing from the owner: at once. Letting other computers approve
  // is said in one sentence first (the backend's own), and happens only when the owner then says so.
  const choose = value => {
    if (busy || value === mode.value) {
      return
    }

    if (value === 'linked') {
      setConfirm(true)
    } else {
      void setMode(value)
    }
  }
  const offered = tasks.on && mode.choices.length > 0
  // The example names a computer that takes requests when one is known: the backend says so, or one answered
  // a request from here before. Only when none is known does the line add that it needs its own switch on.
  const answered = tasks.log.find(r => r.dir === 'out' && r.state === 'done' && tasks.computers.includes(r.peer))?.peer
  const known = tasks.taking[0] ?? answered ?? ''
  const example = known || tasks.computers[0]

  return el(
    'section',
    { 'aria-label': 'Requests from your other computers', className: 'flex flex-col gap-3 pt-2' },
    el(
      'div',
      { className: cn(CARD, 'flex flex-col gap-3') },
      el(
        'div',
        { className: 'flex items-start gap-3' },
        el('div', { className: 'flex min-w-0 flex-1 flex-col gap-0.5' }, el('label', { className: HEADING, htmlFor: 'gbt-tasks' }, TASKS_LABEL), // (on: one word beside the switch; what it allows and how approvals work is the first line of the fold below)
          // (what the switch allows, and that approvals stay with the owner, is the word's hover text and the fold's first line)
          el('p', { className: SMALL, title: tasks.on ? TASKS_ON : TASKS_WHEN_ON }, tasks.on ? TASKS_ON_SHORT : TASKS_OFF), tasks.on && tasks.homeNetwork && el('p', { className: SMALL }, TASKS_HOME_NETWORK)),
        jsx(SwitchTarget, { children: jsx(Switch, { 'aria-label': TASKS_LABEL, checked: tasks.on, disabled: Boolean(busy), id: 'gbt-tasks', onCheckedChange: v => flip(v === true) }), htmlFor: 'gbt-tasks' })
      ),
      // only while requests are let in: what happens here when a step of one needs approval
      offered &&
        el(
          'div',
          { 'aria-label': mode.label, className: 'flex flex-col gap-1', role: 'radiogroup' },
          el('span', { 'aria-hidden': true, className: HEADING }, mode.label),
          mode.choices.map(c => jsx(ModeChoice, { checked: c.value === mode.value, choice: c, disabled: Boolean(busy), onChoose: choose }, c.value))
        ),
      offered &&
        confirm === true &&
        Boolean(mode.warning) &&
        jsx(Confirm, {
          cancelLabel: 'Cancel',
          confirmLabel: 'Let them approve',
          description: mode.warning,
          onClose: () => setConfirm(false),
          onConfirm: () => setMode('linked'),
          open: true,
          title: TASKS_MODE_TITLE
        }),
      // how to ask from this computer: the one line a first request needs, folded so the card stays short
      // (asking needs no switch here; the computer that is asked needs its own switch on)
      el(
        Fold,
        { initialOpen: initialHow, label: 'How to ask from here' },
        el(
          'div',
          { className: cn(SMALL, 'flex flex-col gap-1.5') },
          el('p', {}, tasksSaid(tasks.on, mode.value)),
          el('p', {}, known ? 'In any Hermes chat on this computer, type this.' : `In any Hermes chat on this computer, type this. ${example} needs its own switch on.`),
          jsx(CommandLine, { command: `/twin ask ${example}: what system are you on?`, label: 'Copy the request' }),
          el('p', {}, 'The answer comes back into that chat, with the name of the computer that did it.')
        )
      ),
      tasks.log.length > 0 &&
        el(
          Fold,
          { initialOpen, label: `Requests (${tasks.log.length})`, openLabel: 'Requests' },
          el(
            'ul',
            { className: 'flex flex-col gap-1.5' },
            tasks.log.slice(0, 20).map(r =>
              el(
                'li',
                { className: cn(BOX, 'gap-0.5'), key: `${r.dir}:${r.id}` },
                el('span', { className: HEADING }, requestTitle(r)),
                el('span', { className: SMALL }, [capital(r.label), r.askedAt && ago(r.askedAt)].filter(Boolean).join(' · ')),
                r.text && el('span', { className: cn(BODY, 'break-words') }, r.text)
              )
            )
          )
        )
    )
  )
}

// ── Twin memory between your computers (xs-memory) ──
//
// A twin's memory (its two files of notes) stays on the computer it was written on until the owner switches
// that twin on here; it is then kept the same on the linked computers that switch it on too. This is all of
// it on the page: one switch per twin (off until it is switched on), what the backend says about each twin,
// a note that was changed on two computers, the earlier versions kept here, and "Forget on this computer".
// Every sentence comes from the plugin's own backend, ready to show; it is cleaned again here and rendered
// as text. Nothing is shown before the first answer, and nothing on a backend that does not know these routes.

const MEMORY_KEY = [ID, 'memory']
const MEMORY_TITLE = 'Twin memory'
const MEMORY_WHAT = 'What follows'
// (the page's own words for what is kept to go back to, here as for a twin's other texts)
const MEMORY_POINTS = 'Earlier versions'
const MEMORY_COMPUTERS = 'Computers that take part'
const MEMORY_LEFT = n => `Earlier versions of a twin that is no longer on this computer are still kept here (${n}).`
const MEMORY_FORGET = 'Forget on this computer'
const MEMORY_FORGET_WHAT = 'Takes out the notes that came from your other computers. Your own notes here stay.'
const memoryLabel = name => `Keep ${name}'s memory the same on my computers`
const memoryComputerLabel = (computer, twin) => `Let ${computer} take part in ${twin}'s memory`

// A name the backend gave (a twin, a note changed twice, an earlier version, a version): it goes back exactly as
// it came, so anything that is not plain short text is not shown at all.
const memoryId = (v, max) => (typeof v === 'string' && v.length <= max && v === clean(v, max) ? v : '')
// A note as it was written: its line breaks stay, what cannot be seen goes, capped. React escapes the rest.
const memoryNote = v => (typeof v === 'string' ? v : '').replace(/[\u0000-\u0009\u000b-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2066-\u2069]/g, ' ').trim().slice(0, 2_000)

// GET /fleet/memory, tidied once: every list is a list, every text is clean text.
// null: not an answer about memory (an older backend, or a problem), and the page shows none of this.
function memoryOf(raw) {
  if (!raw || typeof raw !== 'object' || !FLEET_ROLES.includes(raw.role)) {
    return null
  }

  const text = (v, max = 300) => clean(typeof v === 'string' ? v : '', max)
  const twins = listOf(raw.twins)
    .filter(t => t && typeof t === 'object' && memoryId(t.id, 120))
    .slice(0, 100)
    .map(t => ({
      canForget: t.can_forget === true,
      // which linked computers take part in this twin's memory: only the computer that shares is told, and
      // only for a twin that is on (anything else has no such list, and no such rows)
      computers: listOf(t.computers)
        .filter(c => c && typeof c === 'object' && typeof c.id === 'string' && /^m-[0-9a-f]{12}$/.test(c.id) && typeof c.name === 'string')
        .slice(0, 20)
        .map(c => ({ id: c.id, name: text(c.name, 40) || 'A computer', on: c.on === true })),
      conflicts: listOf(t.conflicts)
        .filter(c => c && typeof c === 'object' && memoryId(c.id, 40))
        .slice(0, 5)
        .map(c => ({
          id: c.id,
          message: text(c.message),
          versions: listOf(c.versions)
            .filter(v => v && typeof v === 'object' && memoryId(v.key, 40) && v.key !== 'both')
            .slice(0, 4)
            .map(v => ({ key: v.key, text: memoryNote(v.text) }))
        }))
        .filter(c => c.versions.length > 0),
      id: t.id,
      message: text(t.message),
      name: text(t.name, 60) || 'A twin',
      notes: listOf(t.notes).map(n => text(n)).filter(Boolean).slice(0, 5),
      on: t.on === true,
      points: listOf(t.restore)
        .filter(p => p && typeof p === 'object' && memoryId(p.point, 20))
        .slice(0, 10)
        .map(p => ({ at: isoOf(p.at), id: p.point, label: text(p.label, 200) }))
    }))

  return {
    // earlier versions still kept here for a twin that has left this computer (also on a computer linked to no other)
    left: listOf(raw.left)
      .filter(l => l && typeof l === 'object' && typeof l.id === 'string' && /^t-[0-9a-f]{16}$/.test(l.id) && Number.isInteger(l.points) && l.points > 0)
      .slice(0, 20)
      .map(l => ({ id: l.id, points: l.points })),
    role: raw.role,
    sentence: text(raw.sentence, 400),
    // a computer that is linked to no other has the heading and its sentence, which says why: no rows, no fold
    twins: raw.role === 'none' ? [] : twins,
    what: raw.role === 'none' ? [] : listOf(raw.what).map(w => text(w, 400)).filter(Boolean).slice(0, 8)
  }
}

// An answer about memory in hand is shown at once (never a guess before it); the next read confirms it.
function showMemory(res) {
  if (memoryOf(res)) {
    queryClient.setQueryData?.(MEMORY_KEY, res)
  } else {
    void queryClient.invalidateQueries({ queryKey: MEMORY_KEY })
  }
}

// One twin: its name and switch, what the backend says about it, and what can be done with its memory here.
// `said`: what the last act on this row came to ({ problem } or { said }), until the next act.
function MemoryRow({ twin, n, busy, said, act, initialPoints = false }) {
  const id = `gbt-memory-${n}`
  const waiting = Boolean(busy)
  const doing = what => busy === `${what}:${twin.id}`

  return el(
    'li',
    { className: BOX },
    el(
      'div',
      { className: 'flex items-start gap-3' },
      el(
        'div',
        { className: 'flex min-w-0 flex-1 flex-col gap-0.5' },
        el('label', { className: HEADING, htmlFor: id }, twin.name),
        twin.message && el('p', { className: SMALL }, twin.message),
        twin.notes.map((line, i) => el('p', { className: SMALL, key: i }, line))
      ),
      // (never ahead of the answer: the switch shows what the backend last said, so a refusal leaves it where it was)
      jsx(SwitchTarget, { children: jsx(Switch, { 'aria-label': memoryLabel(twin.name), checked: twin.on, disabled: waiting, id, onCheckedChange: v => act(twin, 'switch', '/fleet/memory', { on: v === true }) }), htmlFor: id })
    ),
    // on the computer that shares, for a twin that is on: which of the linked computers take part
    twin.computers.length > 0 &&
      el(
        'div',
        { className: 'flex flex-col gap-0.5' },
        el('p', { className: SMALL }, MEMORY_COMPUTERS),
        el(
          'ul',
          { className: 'flex flex-col' },
          twin.computers.map((c, i) =>
            el(
              'li',
              { className: 'flex items-center gap-3', key: c.id },
              el('label', { className: cn(BODY, 'min-w-0 flex-1 break-words'), htmlFor: `${id}-c${i}` }, c.name),
              jsx(SwitchTarget, { children: jsx(Switch, { 'aria-label': memoryComputerLabel(c.name, twin.name), checked: c.on, disabled: waiting, id: `${id}-c${i}`, onCheckedChange: v => act(twin, `computer:${c.id}`, '/fleet/memory/computer', { machine: c.id, on: v === true }) }), htmlFor: `${id}-c${i}` })
            )
          )
        )
      ),
    said?.problem && jsx(InlineProblem, { error: said.problem }),
    said?.said && el('p', { className: cn(BODY, 'text-(--ui-text-primary)'), role: 'status' }, said.said),
    // a note that was changed on two computers: each version as it is written, and which to keep
    twin.conflicts.map(c =>
      el(
        'div',
        { className: 'flex flex-col gap-2', key: c.id },
        c.message && el('p', { className: cn(BODY, 'text-(--ui-text-primary)') }, c.message),
        c.versions.map(v =>
          el(
            'div',
            { className: 'flex flex-wrap items-start gap-2', key: v.key },
            el('blockquote', { className: cn(NOTE, 'max-h-40 min-w-0 flex-1 overflow-auto whitespace-pre-wrap break-words text-(--ui-text-primary)'), tabIndex: 0 }, v.text),
            // (each spoken name begins with the words on the button, then says which note)
            el(Button, { 'aria-label': `Keep this one: ${clean(v.text, 80)}`, disabled: waiting, loading: doing(`keep:${c.id}:${v.key}`), onClick: () => act(twin, `keep:${c.id}:${v.key}`, '/fleet/memory/resolve', { conflict: c.id, keep: v.key }), variant: 'secondary' }, 'Keep this one')
          )
        ),
        // (only where there are two to keep: a note that was removed on one computer has one version)
        c.versions.length > 1 &&
          el(Button, { 'aria-label': `Keep both: ${c.versions.map(v => clean(v.text, 40)).join(' / ')}`, className: 'self-start', disabled: waiting, loading: doing(`keep:${c.id}:both`), onClick: () => act(twin, `keep:${c.id}:both`, '/fleet/memory/resolve', { conflict: c.id, keep: 'both' }), variant: 'secondary' }, 'Keep both')
      )
    ),
    twin.points.length > 0 &&
      el(
        Fold,
        { initialOpen: initialPoints, label: `${MEMORY_POINTS} (${twin.points.length})` },
        el(
          'ul',
          { className: 'flex flex-col gap-1.5' },
          twin.points.map(p => {
            const what = [p.label, whenText(p.at)].filter(Boolean).join(' · ')

            return el(
              'li',
              { className: 'flex min-w-0 flex-wrap items-center gap-2', key: p.id },
              el('span', { className: cn(BODY, 'min-w-0 flex-1 break-words') }, what),
              el(Button, { 'aria-label': `Put back: ${what}`, disabled: waiting, loading: doing(`restore:${p.id}`), onClick: () => act(twin, `restore:${p.id}`, '/fleet/memory/restore', { point: p.id }, 'restored'), variant: 'outline' }, 'Put back')
            )
          })
        )
      ),
    // asks nothing first: the backend's own sentence then says what went and what stayed
    twin.canForget &&
      el(
        'div',
        { className: 'flex flex-wrap items-center gap-2' },
        el(Button, { 'aria-describedby': `${id}-forget`, 'aria-label': `${MEMORY_FORGET}: ${twin.name}`, disabled: waiting, loading: doing('forget'), onClick: () => act(twin, 'forget', '/fleet/memory/forget', {}, 'forgot'), variant: 'outline' }, MEMORY_FORGET),
        el('span', { className: cn(SMALL, 'min-w-0 flex-1'), id: `${id}-forget` }, MEMORY_FORGET_WHAT)
      )
  )
}

function MemorySection({ initialWhat = false, initialPoints = false, initialSaid = null }) {
  const [busy, run] = useBusy()
  const [said, setSaid] = useState(initialSaid) // { twin, problem } | { twin, said } | null: how the last act ended
  // read when the page opens and every 5 s while it is on screen, as the list of computers is; an event
  // about the computers reads it again too (see handleEvent)
  const q = useQuery({ queryFn: () => api('/fleet/memory'), queryKey: MEMORY_KEY, refetchInterval: 5_000, retry: 1 })
  const memory = memoryOf(q.data)

  if (!memory) {
    return null
  }

  // One call at a time for the whole section: every control waits while it runs. What comes back is shown as
  // it is; a refusal is said in the twin's own row, in the backend's words, and nothing else changes.
  // `answer`: the part of the reply that holds the sentence to say afterwards ("forgot", "restored").
  const act = (twin, what, path, body, answer = '') =>
    run(`${what}:${twin.id}`, async () => {
      let next

      setSaid(null)

      try {
        const res = await post(path, { twin: twin.id, ...body })

        showMemory(res)
        next = { said: answer ? plainText(res?.[answer]?.sentence, 400) : '', twin: twin.id }
      } catch (err) {
        next = { problem: asProblem(err), twin: twin.id }
      }

      setSaid(next)

      return next
    })

  // how an act ended on something that has no row (any more): what was kept for a twin that left, once forgotten
  const elsewhere = said && !memory.twins.some(t => t.id === said.twin) ? said : null

  return el(
    'section',
    { 'aria-label': MEMORY_TITLE, className: 'flex flex-col gap-3 pt-2' },
    el(
      'div',
      { className: cn(CARD, 'flex flex-col gap-3') },
      el('div', { className: 'flex flex-col gap-0.5' }, el('h2', { className: HEADING }, MEMORY_TITLE), memory.sentence && el('p', { className: SMALL }, memory.sentence)),
      memory.what.length > 0 &&
        el(
          Fold,
          { initialOpen: initialWhat, label: MEMORY_WHAT },
          el('ul', { className: cn(SMALL, 'flex flex-col gap-1.5') }, memory.what.map((line, i) => el('li', { className: 'gbt-line', key: i }, line)))
        ),
      memory.twins.length > 0 &&
        el(
          'ul',
          { className: 'flex flex-col gap-1.5' },
          memory.twins.map((twin, i) => jsx(MemoryRow, { act, busy, initialPoints, n: i, said: said?.twin === twin.id ? said : null, twin }, twin.id))
        ),
      // kept for a twin that is no longer here: one line each, and the way to let go of them (asks nothing first)
      memory.left.map((l, i) =>
        el(
          'div',
          { className: 'flex flex-wrap items-center gap-2', key: l.id },
          el('span', { className: cn(SMALL, 'min-w-0 flex-1'), id: `gbt-memory-left-${i}` }, MEMORY_LEFT(l.points)),
          el(Button, { 'aria-describedby': `gbt-memory-left-${i}`, disabled: Boolean(busy), loading: busy === `forget:${l.id}`, onClick: () => act(l, 'forget', '/fleet/memory/forget', {}, 'forgot'), variant: 'outline' }, 'Forget them')
        )
      ),
      elsewhere?.problem && jsx(InlineProblem, { error: elsewhere.problem }),
      elsewhere?.said && el('p', { className: cn(BODY, 'text-(--ui-text-primary)'), role: 'status' }, elsewhere.said)
    )
  )
}

// ── skills on the owner's computers ──────────────────────────────────────────

// The twins' skills, sent from the computer with the Grok app to the linked ones: off until the owner switches
// it on. One read of GET /fleet/skills; every sentence about a skill's state comes ready from the backend.
const SKILLS_KEY = [ID, 'fleet-skills']
const SKILL_STATES = ['in_sync', 'off', 'only_here', 'changed_here', 'conflict', 'waiting', 'too_big', 'removed_there', 'error']
const SKILL_KEY_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}(?:\/[A-Za-z0-9][A-Za-z0-9._-]{0,63})?$/
const SKILL_VERSION_RE = /^(?:k-[0-9a-f]{12}|v[0-9]{1,9})$/
const SKILLS_SHARE_LABEL = "Share my twins' skills with my other computers"

// GET /fleet/skills, tidied once: every list is a list, every text is clean text. null: nothing to show (an
// older backend, a computer that is not linked and does not share).
function skillsOf(raw) {
  if (!raw || typeof raw !== 'object' || raw.error || !['source', 'follower'].includes(raw.role) || !Array.isArray(raw.twins)) {
    return null
  }

  const text = (v, max = 200) => clean(typeof v === 'string' ? v : '', max)
  const other = raw.other && typeof raw.other === 'object' ? text(raw.other.name, 40) : ''
  const twins = raw.twins
    .filter(t => t && typeof t === 'object' && /^t-[0-9a-f]{16}$/.test(String(t.id ?? '')))
    .slice(0, 100)
    .map(t => ({
      id: t.id,
      name: text(t.name, 100) || 'Twin',
      skills: (Array.isArray(t.skills) ? t.skills : [])
        .filter(s => s && typeof s === 'object' && SKILL_KEY_RE.test(String(s.key ?? '')))
        .slice(0, 200)
        .map(s => ({
          canSwitch: s.can_switch === true,
          choices: (Array.isArray(s.choices) ? s.choices : []).filter(c => c && (c.keep === 'mine' || c.keep === 'theirs') && text(c.label, 60)).slice(0, 2).map(c => ({ keep: c.keep, label: text(c.label, 60) })),
          group: text(s.group, 64),
          key: s.key,
          label: text(s.label, 160),
          name: text(s.name, 64) || s.key,
          note: text(s.note, 200),
          on: s.on === true,
          state: SKILL_STATES.includes(s.state) ? s.state : 'error',
          switchLabel: text(s.switch_label, 120),
          versions: (Array.isArray(s.versions) ? s.versions : []).filter(v => v && typeof v === 'object' && SKILL_VERSION_RE.test(String(v.vid ?? ''))).slice(0, 10).map(v => ({ at: text(v.at, 40), label: text(v.label, 160), vid: v.vid }))
        }))
    }))

  return { message: text(raw.message, 300), on: raw.on === true, other, role: raw.role, twins, what: text(raw.what, 300) }
}

const showSkills = res => queryClient.setQueryData(SKILLS_KEY, res)

// After a choice or a "Put back": what was done, and Undo while the backend names what it replaced.
function toastSkill(res, message, name) {
  const u = res?.undo && typeof res.undo === 'object' && SKILL_VERSION_RE.test(String(res.undo.vid ?? '')) ? res.undo : null

  async function undo() {
    try {
      showSkills(await post('/fleet/skills/restore', { skill: u.skill, twin: u.twin, vid: u.vid }))
      host.notify({ id: `${ID}:skills:done`, kind: 'success', message: `${name} is as it was before` })
    } catch (err) {
      notifyProblem(err, 'Could not undo that')
    }
  }

  host.notify({
    action: u ? { label: 'Undo', onClick: () => void undo() } : undefined,
    // (a notice that carries Undo stays long enough to be read and reached)
    durationMs: u ? WARNING_TOAST_MS : undefined,
    id: `${ID}:skills:done`,
    kind: 'success',
    message,
    meta: u ? 'What it replaced is kept under Earlier versions.' : undefined
  })
}

// `initialVersions`: a skill's key whose earlier versions are shown from the start (for the page's own tests).
function FleetSkills({ initialVersions = '' }) {
  const [busy, run] = useBusy()
  const q = useQuery({ queryFn: () => api('/fleet/skills'), queryKey: SKILLS_KEY, refetchInterval: 5_000, retry: 1 })
  const view = skillsOf(q.data)

  if (!view) {
    return null
  }

  const sharing = view.role === 'source'
  const from = view.other || 'your Mac'
  const label = sharing ? SKILLS_SHARE_LABEL : `Take skills from ${from}`
  const flip = on => void run('skills', async () => showSkills(await post('/fleet/skills/switch', { on })), on ? 'Could not switch this on' : 'Could not switch this off')
  const setItem = (t, s, on) => void run(`item:${t.id}:${s.key}`, async () => showSkills(await post('/fleet/skills/item', { on, skill: s.key, twin: t.id })), 'Could not change that skill')
  const keep = (t, s, c) =>
    void run(
      `keep:${c.keep}:${t.id}:${s.key}`,
      async () => {
        const res = await post('/fleet/skills/keep', { keep: c.keep, skill: s.key, twin: t.id })

        showSkills(res)
        toastSkill(res, c.keep === 'mine' ? `Kept this computer's version of ${s.name}` : `Kept ${sharing ? "the other computer's" : `${from}'s`} version of ${s.name}`, s.name)
      },
      'Could not keep that version'
    )
  const putBack = (t, s, v) =>
    void run(
      `back:${v.vid}:${t.id}:${s.key}`,
      async () => {
        const res = await post('/fleet/skills/restore', { skill: s.key, twin: t.id, vid: v.vid })

        showSkills(res)
        toastSkill(res, `Put back an earlier version of ${s.name}`, s.name)
      },
      'Could not put that version back'
    )
  const listed = view.twins.filter(t => t.skills.length > 0)

  return el(
    'section',
    { 'aria-label': 'Skills', className: 'flex flex-col gap-3' },
    el(
      'div',
      { className: cn(CARD, 'flex flex-col gap-3') },
      el(
        'div',
        { className: 'flex items-start gap-3' },
        el(
          'div',
          { className: 'flex min-w-0 flex-1 flex-col gap-0.5' },
          el('label', { className: HEADING, htmlFor: 'gbt-skills' }, label),
          // what goes, and what never does: said on or off, in the backend's one sentence
          view.what && el('p', { className: SMALL }, view.what),
          view.message && el('p', { className: SMALL, role: 'status' }, view.message)
        ),
        jsx(SwitchTarget, { children: jsx(Switch, { 'aria-label': label, checked: view.on, disabled: Boolean(busy), id: 'gbt-skills', onCheckedChange: v => flip(v === true) }), htmlFor: 'gbt-skills' })
      ),
      view.on && listed.length === 0 && !view.message && el('p', { className: BODY, role: 'status' }, sharing ? 'Your twins have no skills yet.' : 'No skills have arrived yet.'),
      view.on &&
        listed.map((t, ti) =>
          el(
            'div',
            { 'aria-label': `Skills of ${t.name}`, className: 'flex flex-col gap-1.5', key: t.id, role: 'group' },
            jsx(Truncate, { className: SMALL, max: 100, text: t.name }),
            el(
              'ul',
              { className: 'flex flex-col gap-1.5' },
              t.skills.map((s, si) => {
                const id = `gbt-skill-${ti}-${si}`
                // (the spoken name begins with the skill's own name, the words in front of the switch)
                const spoken = s.switchLabel || (sharing ? `Share ${s.name}` : `Follow ${s.name} from ${from}`)

                return el(
                  'li',
                  { className: cn(BOX, 'list-none'), key: s.key },
                  el(
                    'div',
                    { className: 'flex items-start gap-3' },
                    el(
                      'div',
                      { className: 'flex min-w-0 flex-1 flex-col gap-0.5' },
                      el('label', { className: 'flex min-w-0 flex-wrap items-baseline gap-x-2', htmlFor: id }, el('span', { className: cn(HEADING, 'min-w-0 break-words') }, s.name), s.group && el('span', { className: SMALL }, s.group)),
                      s.label && el('span', { className: cn(SMALL, s.state === 'conflict' || s.state === 'error' ? 'text-(--ui-text-primary)' : '') }, capital(s.label)),
                      s.note && el('span', { className: SMALL }, s.note)
                    ),
                    jsx(SwitchTarget, { children: jsx(Switch, { 'aria-label': spoken, checked: s.on, disabled: Boolean(busy) || !s.canSwitch, id, onCheckedChange: v => setItem(t, s, v === true) }), htmlFor: id })
                  ),
                  s.choices.length > 0 &&
                    el(
                      'div',
                      { className: 'flex flex-wrap items-center gap-2' },
                      s.choices.map(c => el(Button, { 'aria-label': `${c.label} version of ${s.name}`, disabled: Boolean(busy), key: c.keep, loading: busy === `keep:${c.keep}:${t.id}:${s.key}`, onClick: () => keep(t, s, c), variant: 'outline' }, c.label))
                    ),
                  s.versions.length > 0 &&
                    el(
                      Fold,
                      { initialOpen: initialVersions === s.key, label: `Earlier versions (${s.versions.length})`, openLabel: 'Earlier versions' },
                      el(
                        'ul',
                        { className: 'flex flex-col gap-1' },
                        s.versions.map(v =>
                          el(
                            'li',
                            { className: 'flex list-none flex-wrap items-center gap-x-2', key: v.vid },
                            el('span', { className: cn(SMALL, 'min-w-0 flex-1') }, [capital(v.label), ago(v.at)].filter(Boolean).join(' · ')),
                            el(Button, { 'aria-label': `Put back ${s.name}: ${v.label || 'an earlier version'}`, disabled: Boolean(busy), loading: busy === `back:${v.vid}:${t.id}:${s.key}`, onClick: () => putBack(t, s, v), variant: 'text' }, 'Put back')
                          )
                        )
                      )
                    )
                )
              })
            )
          )
        )
    )
  )
}

// ── settings that follow between your computers ─────────────────────────────
//
// Besides a twin's name and description, some of its settings can follow from the computer with the Grok app
// to the linked ones: its tools, a few preferences, its persona and (off unless switched on) its model. One
// switch per kind, on each computer for itself. Nothing secret is ever among them; the backend says so in
// `never`, and that line is shown as it is. Everything shown is the backend's text, cleaned again here.

const SETTINGS_KEY = [ID, 'fleetSettings']
const SETTING_KINDS = ['tools', 'preferences', 'persona', 'model']
const SETTING_STATES = ['in_sync', 'waiting', 'held', 'set_up', 'error', 'off']
const SETTINGS_OWN_MODEL = 'Each computer keeps its own model.'
const SET_UP_HERE = 'Set up on this computer'

const andWords = list => (list.length <= 1 ? (list[0] ?? '') : `${list.slice(0, -1).join(', ')} and ${list[list.length - 1]}`)

// GET /fleet/settings, tidied once. null: nothing to show (not linked and not sharing, or an older backend).
function settingsOf(raw) {
  if (!raw || typeof raw !== 'object' || !Array.isArray(raw.items) || (raw.role !== 'source' && raw.role !== 'follower')) {
    return null
  }

  const text = (v, max = 200) => clean(typeof v === 'string' ? v : '', max)
  const items = SETTING_KINDS.map(id => raw.items.find(i => i && typeof i === 'object' && i.id === id))
    .filter(Boolean)
    .map(i => ({ id: i.id, label: text(i.label, 40) || capital(i.id), note: text(i.note), on: i.on === true, what: text(i.what) }))

  if (items.length === 0) {
    return null
  }

  const need = n => {
    const name = text(n?.name, 60)

    return text(n?.message) || (n?.kind === 'connector' ? `Set up the connector ${name} on this computer.` : n?.kind === 'model' ? 'Choose a model for it on this computer.' : name ? `The tools called ${name} are not on this computer.` : '')
  }
  const twins = (raw.role === 'follower' && Array.isArray(raw.twins) ? raw.twins : [])
    .filter(t => t && typeof t === 'object' && /^t-[0-9a-f]{16}$/.test(String(t.id ?? '')))
    .slice(0, 100)
    .map(t => ({
      changed: (Array.isArray(t.changed) ? t.changed : []).filter(c => SETTING_KINDS.includes(c)),
      id: t.id,
      name: text(t.name, 100) || 'This twin',
      needs: (Array.isArray(t.needs) ? t.needs : []).filter(n => n && typeof n === 'object').slice(0, 10).map(need).filter(Boolean),
      problem: text(t.error?.message),
      restore: (Array.isArray(t.restore) ? t.restore : [])
        .filter(p => p && typeof p === 'object' && /^p-[A-Za-z0-9_-]{1,60}$/.test(String(p.point ?? '')))
        .slice(0, 10)
        .map(p => ({ label: text(p.label, 120), point: p.point })),
      state: t.held === true ? 'held' : SETTING_STATES.includes(t.state) ? t.state : 'waiting',
      syncedAt: text(t.synced_at, 40)
    }))

  return { items, never: text(raw.never, 300), other: text(raw.other, 40), role: raw.role, sentence: text(raw.sentence, 300), state: raw.state === 'older' || raw.state === 'waiting' ? raw.state : 'on', twins }
}

// The one line the calm page carries: which kinds follow, and that the model stays each computer's own.
function settingsLine(s) {
  const other = s.other || 'the other computer'

  if (s.state === 'older') {
    return `${other} needs a newer Grok Bot Twin before settings can follow.`
  }

  const on = s.items.filter(i => i.on).map(i => i.label.toLowerCase())
  const model = s.items.find(i => i.id === 'model')
  const said = [on.length > 0 ? `Settings follow too: ${on.join(', ')}.` : 'No settings follow.', model && !model.on && on.length > 0 ? SETTINGS_OWN_MODEL : '', s.state === 'waiting' && s.role === 'follower' ? `Waiting for ${other}.` : '']

  return said.filter(Boolean).join(' ')
}

// How one twin's settings stand on this (linked) computer, in a few words.
function settingsTwinWords(t, other) {
  const from = other || 'the other computer'

  if (t.state === 'held') {
    return 'Kept as they are here'
  }

  if (t.state === 'set_up') {
    return SET_UP_HERE
  }

  if (t.state === 'error') {
    return t.problem || 'Its settings could not be saved here'
  }

  if (t.state === 'off') {
    return 'No settings follow here'
  }

  if (t.state === 'waiting') {
    return `Waiting for settings from ${from}`
  }

  return ['Settings in sync', t.syncedAt && ago(t.syncedAt)].filter(Boolean).join(', ')
}

const showSettings = res => queryClient.setQueryData(SETTINGS_KEY, res)

function SettingsSection({ initialOpen = false }) {
  const [busy, run] = useBusy()
  const [open, setOpen] = useState(initialOpen)
  // (read again with every event from the backend: refreshAll covers this key; and now and then by itself)
  const q = useQuery({ queryFn: () => api('/fleet/settings'), queryKey: SETTINGS_KEY, refetchInterval: 30_000, retry: 1 })
  const s = settingsOf(q.data)

  if (!s) {
    return null
  }

  const other = s.other || 'the other computer'
  const flip = (item, on) => void run(`item:${item.id}`, async () => showSettings(await post('/fleet/settings', { item: item.id, on })), on ? 'Could not switch this on' : 'Could not switch this off')
  const putBack = t =>
    void run(
      `back:${t.id}`,
      async () => {
        showSettings(await post('/fleet/settings/restore', { point: t.restore[0].point, twin: t.id }))
        host.notify({ id: `${ID}:settings:back`, kind: 'success', message: `${t.name} has its earlier settings again`, meta: `They stay as they are here until they next change on ${other}.` })
      },
      'Could not put the settings back'
    )
  const follow = t => void run(`follow:${t.id}`, async () => showSettings(await post('/fleet/settings/follow', { twin: t.id })), `Could not follow ${other} again`)
  const toSetUp = s.twins.filter(t => t.state === 'set_up' || t.needs.length > 0)

  return el(
    'section',
    { 'aria-label': 'Settings that follow between your computers', className: 'flex flex-col gap-3 pt-2' },
    el(
      'div',
      { className: cn(CARD, 'flex flex-col gap-3') },
      el(
        'div',
        { className: 'flex flex-wrap items-center gap-2' },
        el('p', { className: cn(BODY, 'min-w-0 flex-1') }, settingsLine(s)),
        // (the spoken name begins with the word on the button)
        el(Button, { 'aria-expanded': open, 'aria-label': 'Change which settings follow', onClick: () => setOpen(!open), variant: 'ghost' }, jsx(DisclosureCaret, { open }), 'Change')
      ),
      // something for the owner to do here is said on the calm page too, in one line; what exactly is in the fold
      !open && toSetUp.length > 0 && el('p', { className: SMALL, role: 'note' }, `${SET_UP_HERE}: ${andWords(toSetUp.slice(0, 3).map(t => t.name))}${toSetUp.length > 3 ? ' and more' : ''}.`),
      open &&
        el(
          'div',
          { className: 'flex flex-col gap-3' },
          s.sentence && el('p', { className: SMALL }, s.sentence),
          el(
            'ul',
            { className: 'flex flex-col gap-2' },
            s.items.map(i =>
              el(
                'li',
                { className: 'flex items-start gap-3', key: i.id },
                el('div', { className: 'flex min-w-0 flex-1 flex-col gap-0.5' }, el('label', { className: HEADING, htmlFor: `gbt-settings-${i.id}` }, i.label), i.what && el('p', { className: SMALL }, i.what), i.note && el('p', { className: SMALL }, i.note)),
                jsx(SwitchTarget, { children: jsx(Switch, { 'aria-label': i.label, checked: i.on, disabled: Boolean(busy), id: `gbt-settings-${i.id}`, onCheckedChange: v => flip(i, v === true) }), htmlFor: `gbt-settings-${i.id}` })
              )
            )
          ),
          s.never && el('p', { className: SMALL }, s.never),
          s.twins.length > 0 &&
            el(
              'ul',
              { 'aria-label': 'Settings of each twin on this computer', className: 'flex flex-col gap-1.5' },
              s.twins.map(t =>
                el(
                  'li',
                  { className: cn(BOX, 'gap-1'), key: t.id },
                  el('span', { className: HEADING }, t.name),
                  el('span', { className: SMALL }, `${settingsTwinWords(t, s.other)}${t.state === 'in_sync' && t.changed.length > 0 ? ` · last changed: ${t.changed.map(c => s.items.find(i => i.id === c)?.label.toLowerCase() ?? c).join(', ')}` : ''}`),
                  t.needs.length > 0 && el('ul', { 'aria-label': `${SET_UP_HERE}, for ${t.name}`, className: cn(SMALL, 'flex list-disc flex-col gap-0.5 pl-4'), role: 'list' }, t.state !== 'set_up' && el('li', { className: 'list-none -ml-4' }, `${SET_UP_HERE}:`), t.needs.map((n, k) => el('li', { key: k }, n))),
                  (t.state === 'held' || t.restore.length > 0) &&
                    el(
                      'div',
                      { className: 'flex flex-wrap items-center gap-2' },
                      t.state === 'held' && el(Button, { 'aria-label': `Follow ${other} again with the settings of ${t.name}`, disabled: Boolean(busy), loading: busy === `follow:${t.id}`, onClick: () => follow(t), variant: 'secondary' }, `Follow ${other} again`),
                      t.state !== 'held' && t.restore.length > 0 && el(Button, { 'aria-label': `Put back the settings ${t.name} had here`, disabled: Boolean(busy), loading: busy === `back:${t.id}`, onClick: () => putBack(t), variant: 'secondary' }, 'Put back'),
                      t.state !== 'held' && t.restore.length > 0 && t.restore[0].label && el('span', { className: cn(SMALL, 'min-w-0 flex-1') }, capital(t.restore[0].label))
                    )
                )
              )
            )
        )
    )
  )
}

// A step waits for the owner on this computer while the Twins page is not on screen: say so once.
function toastTasks(payload) {
  const what = clean(payload?.what, 40)

  if (pageOpen > 0 || (what !== 'needs_approval' && what !== 'needs_answer')) {
    return
  }

  // a step on another computer, for a request sent from here, which that computer lets this one answer
  if (what === 'needs_answer') {
    host.notify({ action: OPEN_TWINS, durationMs: WARNING_TOAST_MS, id: `${ID}:tasks:asking`, kind: 'warning', message: 'A step on another computer waits for your approval', meta: 'A request you sent needs one step approved. Open Twins to answer.' })

    return
  }

  host.notify({ action: OPEN_TWINS, durationMs: WARNING_TOAST_MS, id: `${ID}:tasks:waiting`, kind: 'warning', message: 'A request waits for your approval', meta: 'One of your other computers asked a twin here to do something. Open Twins to answer.' })
}

function toastFleet(payload) {
  if (pageOpen > 0) {
    return
  }

  const what = clean(payload?.what, 40)
  const n = Math.max(1, Math.floor(Number(payload?.count) || 1))

  if (what === 'machine_linked') {
    host.notify({ action: OPEN_TWINS, durationMs: WARNING_TOAST_MS, id: `${ID}:fleet:linked`, kind: 'info', message: 'Another computer now follows your twins', meta: 'Open Twins to see which, or to unlink it.' })
  } else if (what === 'unlinked') {
    host.notify({ action: OPEN_TWINS, durationMs: WARNING_TOAST_MS, id: `${ID}:fleet:unlinked`, kind: 'info', message: 'This computer was unlinked', meta: `Its twins are kept for ${KEPT_DAYS} days. Open Twins to link again.` })
  } else if (what === 'twin_removed') {
    host.notify({ action: OPEN_TWINS, durationMs: WARNING_TOAST_MS, id: `${ID}:fleet:removed`, kind: 'info', message: `${capital(plural(n, 'twin'))} left this computer`, meta: `Removed on the computer they come from. Kept here for ${KEPT_DAYS} days.` })
  }
}

// ── routines on your computers ──────────────────────────────────────────────
//
// What a twin's Hermes bot does on a schedule is copied to the owner's other computers, and each routine runs
// on exactly one of them. This is the page's part: the switch, where each routine runs, the button that
// moves it, and the way back. Every sentence about a routine is the backend's own (it owns the wording),
// cleaned again here and rendered as text.

const XS_ROUTINES_KEY = [ID, 'fleet-routines']
const XS_ROUTINES_LABEL = 'Routines'
const XS_RUN_HERE_LABEL = 'Routines may run on this computer'
// how many routines are listed before the rest folds away
const XS_ROUTINES_SHOWN = 12
// what may stand in an address the page builds (a routine, a computer, a restore point)
const XS_NAME_IN_PATH = /^[A-Za-z0-9_-]{1,64}$/
const XS_FORCE_LABEL = 'Start it here anyway'
// (a routine in Hermes as it was before a copy replaced or removed it; the page's word for it, as for a twin's
// earlier versions)
const XS_EARLIER_LABEL = 'Routines as they were before'

// GET /fleet/routines, tidied once: every list is a list, every text is clean text.
// null: this computer shares with no other one, or the backend is an older one; the page then shows none of it.
function routinesOf(raw) {
  if (!raw || typeof raw !== 'object' || (raw.role !== 'source' && raw.role !== 'follower')) {
    return null
  }

  const text = (v, max = 300) => plainText(typeof v === 'string' ? v : '', max)
  const computer = m => ({ id: XS_NAME_IN_PATH.test(String(m?.id ?? '')) ? String(m.id) : '', name: text(m?.name, 40) || 'another computer', this: m?.this === true })
  const plainLines = v => listOf(v).map(s => text(s, 200).replace(/[.\s]+$/, '')).filter(Boolean).slice(0, 12)
  const problem = raw.problem && typeof raw.problem === 'object' ? { hint: text(raw.problem.hint), message: text(raw.problem.message) || FALLBACK_PROBLEM } : null
  const supported = raw.supported !== false

  return {
    canRun: raw.can_run === true,
    computers: listOf(raw.machines).slice(0, 20).map(computer).filter(c => c.id),
    linked: raw.role === 'follower',
    localOnly: countOf(raw.local_only) ?? 0,
    never: plainLines(raw.never),
    on: raw.on === true,
    problem,
    // an older Grok Bot Twin on the other side, or a problem: the sentence about it is shown, and no list
    listed: supported && !problem,
    restore: listOf(raw.restore)
      .filter(p => p && typeof p === 'object' && XS_NAME_IN_PATH.test(String(p.id ?? '')))
      .slice(0, 20)
      .map(p => ({ at: isoOf(p.at), id: String(p.id), label: text(p.label, 160) || 'A routine as it was' })),
    routines: listOf(raw.routines)
      .filter(r => r && typeof r === 'object' && XS_NAME_IN_PATH.test(String(r.id ?? '')))
      .slice(0, 200)
      .map(r => ({
        canForce: r.can_force === true,
        id: String(r.id),
        label: text(r.label, 160),
        name: text(r.name, 120) || 'A routine',
        runsOn: computer(r.runs_on),
        // (a schedule that arrives as five bare fields is said in words, like everywhere on the page)
        schedule: cronText(typeof r.schedule === 'string' ? r.schedule : ''),
        // (the one state the page acts on; what a routine is doing is said by the backend's own label)
        moving: r.state === 'moving',
        targets: listOf(r.can_move_to).slice(0, 20).map(computer).filter(t => t.id),
        twinName: text(r.twin_name, 60),
        waitingFor: text(r.waiting_for, 40)
      })),
    sentence: text(raw.sentence),
    whatSyncs: plainLines(raw.what_syncs)
  }
}

// One read of GET /fleet/routines. It is read again on every `fleet` event (refreshAll) and otherwise no
// faster than the list of computers itself: every 5 s while a routine is moving, every 10 s when all is still.
function useRoutines(enabled = true) {
  const q = useQuery({ enabled, queryFn: () => api('/fleet/routines'), queryKey: XS_ROUTINES_KEY, refetchInterval: query => (routinesOf(query?.state?.data)?.routines.some(r => r.moving) ? 5_000 : 10_000), retry: 1 })

  return routinesOf(q.data)
}

// An answer in hand is shown at once; the next read confirms it.
function showRoutines(res) {
  if (routinesOf(res)) {
    queryClient.setQueryData?.(XS_ROUTINES_KEY, res)
  }
}

// "1 routine stays on this computer because it runs a script."
const staysHereWords = n => (n === 1 ? '1 routine stays on this computer because it runs a script.' : `${n} routines stay on this computer because they run a script.`)
// The sentence of the second, explicit step: said before a routine is started here without the other computer's word.
const forceWords = computer => `${computer || 'The other computer'} has not confirmed that it stopped this routine. If it is still on, the routine could run on both until it next reaches this computer.`

// One routine: its name, whose it is and when it runs, where it runs (the backend's own label), and one
// button per computer it can move to. "Start it here anyway" stands apart, and asks first.
function RoutineRow({ routine: r, busy, here, onMove, onForce }) {
  // while it is moving, the computer it is moving to is not offered again; any other one still is
  const offered = t => !(r.moving && t.id === r.runsOn.id)

  return el(
    'li',
    { className: cn(BOX, 'gap-1.5') },
    el(
      'div',
      { className: 'flex min-w-0 flex-col gap-0.5' },
      el('span', { className: cn(HEADING, 'break-words') }, r.name),
      (r.twinName || r.schedule) && el('span', { className: cn(SMALL, 'break-words') }, [r.twinName, r.schedule].filter(Boolean).join(' · ')),
      r.label && el('span', { className: cn(BODY, 'break-words', r.moving && 'text-(--ui-text-primary)') }, capital(r.label))
    ),
    r.targets.length > 0 &&
      el(
        'div',
        { className: 'flex flex-wrap items-center gap-2' },
        r.targets.map(t => {
          const words = t.this ? 'Run here' : `Run on ${t.name}`

          // (each spoken name begins with the words on the button, then says which routine)
          return el(Button, { 'aria-label': `${words}: ${r.name}`, disabled: Boolean(busy) || !offered(t), key: t.id, loading: busy === `move:${r.id}:${t.id}`, onClick: () => onMove(r, t), variant: 'secondary' }, words)
        })
      ),
    r.canForce &&
      here &&
      el('div', { className: 'flex flex-wrap items-center gap-2' }, el(Button, { 'aria-haspopup': 'dialog', 'aria-label': `${XS_FORCE_LABEL}: ${r.name}`, disabled: Boolean(busy), onClick: () => onForce(r), variant: 'outline' }, XS_FORCE_LABEL))
  )
}

// `initialOpen` / `initialForce` let the smoke test render the folds open and the question before a forced start.
function RoutinesSection({ initialOpen = false, initialForce = null }) {
  const [busy, run] = useBusy()
  const [force, setForce] = useState(initialForce) // null | the routine to start here anyway (its id)
  const view = useRoutines()

  if (!view) {
    return null
  }

  const here = view.computers.find(c => c.this) ?? null
  const flip = (name, body, on) => void run(name, async () => showRoutines(await post('/fleet/routines', body)), on ? 'Could not switch this on' : 'Could not switch this off')
  const movePath = r => `/fleet/routines/${encodeURIComponent(r.id)}/move`
  const move = (r, t) => void run(`move:${r.id}:${t.id}`, async () => showRoutines(await post(movePath(r), { to: t.id })), `Could not move ${r.name}`)
  const putBack = p =>
    void run(
      `back:${p.id}`,
      async () => {
        showRoutines(await post(`/fleet/routines/restore/${encodeURIComponent(p.id)}`, {}))
        host.notify({ durationMs: TOAST_MS, id: `${ID}:routines:back`, kind: 'success', message: 'Put back in Hermes, paused', meta: p.label })
      },
      'Could not put that back'
    )
  const forcing = force && here ? (view.routines.find(r => r.id === force && r.canForce) ?? null) : null
  const rows = view.listed ? view.routines : []
  const row = r => jsx(RoutineRow, { busy, here, onForce: x => setForce(x.id), onMove: move, routine: r }, r.id)

  return el(
    'section',
    { 'aria-label': 'Routines on your computers', className: 'flex flex-col gap-3 pt-2' },
    el(
      'div',
      { className: cn(CARD, 'flex flex-col gap-3') },
      el(
        'div',
        { className: 'flex items-start gap-3' },
        el('div', { className: 'flex min-w-0 flex-1 flex-col gap-0.5' }, el('label', { className: HEADING, htmlFor: 'gbt-routines' }, XS_ROUTINES_LABEL), view.sentence && el('p', { className: SMALL, id: 'gbt-routines-says' }, view.sentence)),
        jsx(SwitchTarget, { children: jsx(Switch, { 'aria-describedby': view.sentence ? 'gbt-routines-says' : undefined, 'aria-label': XS_ROUTINES_LABEL, checked: view.on, disabled: Boolean(busy), id: 'gbt-routines', onCheckedChange: v => flip('routines', { on: v === true }, v === true) }), htmlFor: 'gbt-routines' })
      ),
      // on a linked computer: copies are kept here either way; whether one may run here is the owner's own switch
      view.linked &&
        el(
          'div',
          { className: 'flex items-start gap-3' },
          el('label', { className: cn(HEADING, 'min-w-0 flex-1'), htmlFor: 'gbt-routines-run' }, XS_RUN_HERE_LABEL),
          jsx(SwitchTarget, { children: jsx(Switch, { 'aria-label': XS_RUN_HERE_LABEL, checked: view.canRun, disabled: Boolean(busy), id: 'gbt-routines-run', onCheckedChange: v => flip('run', { can_run: v === true }, v === true) }), htmlFor: 'gbt-routines-run' })
        ),
      view.problem &&
        el(
          'div',
          { className: cn(NOTE, 'flex items-start gap-2'), role: 'note' },
          jsx(Icon, { className: 'mt-0.5 text-(--ui-text-tertiary)', name: 'warning' }),
          el('div', { className: 'flex min-w-0 flex-1 flex-col' }, el('span', { className: 'text-(--ui-text-primary)' }, view.problem.message), view.problem.hint && el('span', {}, view.problem.hint))
        ),
      rows.length > 0 && el('ul', { 'aria-label': 'Routines and where each one runs', className: 'flex flex-col gap-1.5' }, rows.slice(0, XS_ROUTINES_SHOWN).map(row)),
      rows.length > XS_ROUTINES_SHOWN &&
        el(Fold, { initialOpen, label: `${rows.length - XS_ROUTINES_SHOWN} more routines`, openLabel: 'More routines' }, el('ul', { 'aria-label': 'More routines', className: 'flex flex-col gap-1.5' }, rows.slice(XS_ROUTINES_SHOWN).map(row))),
      view.listed && view.localOnly > 0 && el('p', { className: SMALL }, staysHereWords(view.localOnly)),
      (view.whatSyncs.length > 0 || view.never.length > 0) &&
        el(
          Fold,
          { initialOpen, label: 'What is copied' },
          el(
            'div',
            { className: cn(SMALL, 'flex flex-col gap-1') },
            view.whatSyncs.length > 0 && el('p', {}, `Copied: ${view.whatSyncs.join(', ')}.`),
            view.never.length > 0 && el('p', {}, `Never copied: ${view.never.join(', ')}.`)
          )
        ),
      view.restore.length > 0 &&
        el(
          Fold,
          { initialOpen, label: `${XS_EARLIER_LABEL} (${view.restore.length})`, openLabel: XS_EARLIER_LABEL },
          el(
            'ul',
            { className: 'flex flex-col gap-1.5' },
            view.restore.map(p =>
              el(
                'li',
                { className: cn(BOX, 'flex-row flex-wrap items-center'), key: p.id },
                el('div', { className: 'flex min-w-0 flex-1 flex-col' }, el('span', { className: cn(HEADING, 'break-words') }, p.label), ago(p.at) && el('span', { className: SMALL }, capital(ago(p.at)))),
                el(Button, { 'aria-label': `Put back: ${p.label}`, disabled: Boolean(busy), loading: busy === `back:${p.id}`, onClick: () => putBack(p), variant: 'secondary' }, 'Put back')
              )
            )
          )
        )
    ),
    forcing &&
      jsx(Confirm, {
        busyLabel: 'Starting…',
        cancelLabel: 'Cancel',
        confirmLabel: XS_FORCE_LABEL,
        description: forceWords(forcing.waitingFor),
        onClose: () => setForce(null),
        onConfirm: async () => showRoutines(await post(movePath(forcing), { force: true, to: here.id })),
        open: true,
        title: `Start ${forcing.name} here anyway?`
      })
  )
}

// ── page sections ───────────────────────────────────────────────────────────

// One centred block: an icon, a headline, a sentence or two, and what to press.
function Centred({ icon, title, children }) {
  return el(
    'div',
    { className: 'grid flex-1 place-items-center px-4 pb-20 pt-8 text-center' },
    el(
      'div',
      { className: 'flex max-w-md flex-col items-center gap-3' },
      jsx(Icon, { className: 'text-(--ui-text-quaternary)', name: icon, size: '2rem' }),
      el('h2', { className: 'text-xl font-semibold text-(--ui-text-primary)' }, title),
      ...many(children)
    )
  )
}

// Both ways in are on the first screen: the bots are on this computer (Connect) or on another one (Link).
// Which button is the filled one follows the one fact Hermes has: is the Grok app on this computer?
// `note`: a link that ended, said once. `onLink` is absent on a backend that cannot link computers.
// `returning`: something of Grok Bot Twin is on this computer already, so the page also offers to remove it;
// the line that explains Link is for a first visit, and the screen stays within its 45 words either way.
function Welcome({ busy, onConnect, onSample, onLink, grokHere = true, note = '', kept = null, returning = false }) {
  const linkFirst = Boolean(onLink) && (!grokHere || Boolean(note))
  const connect = el(Button, { disabled: Boolean(busy), key: 'connect', onClick: onConnect, variant: linkFirst ? 'outline' : 'default' }, 'Connect my Grok bots')
  const link = onLink && el(Button, { disabled: Boolean(busy), key: 'link', onClick: onLink, variant: linkFirst ? 'default' : 'outline' }, LINK_LABEL)
  const buttons = el('div', { className: 'mt-1 flex flex-wrap items-center justify-center gap-2', key: 'b' }, ...(linkFirst ? [link, connect] : [connect, link]))

  // A link just ended: the screen is about that and nothing else (what happened, what became of the twins,
  // the way back).
  if (note) {
    return el(Centred, { icon: 'mirror', title: 'This computer is unlinked' }, el('p', { className: LEAD, key: 'n', role: 'status' }, note), buttons, kept && el('div', { className: 'w-full text-left', key: 'k' }, kept))
  }

  return el(
    Centred,
    { icon: 'mirror', title: 'Bring your Grok bots to Hermes' },
    el('p', { className: LEAD, key: 'p' }, 'Each bot gets a twin in Hermes that follows its name and description. Nothing changes in Grok.'),
    buttons,
    onLink && !returning && el('p', { className: SMALL, key: 'h' }, 'Bots on another computer? Link this one to it.'),
    el(Button, { disabled: Boolean(busy), key: 'sample', loading: busy === 'sample', onClick: onSample, variant: 'text' }, 'See a sample first')
  )
}

function PageSkeleton() {
  return el(
    'div',
    { 'aria-busy': true, 'aria-label': 'Loading twins', className: 'flex flex-col gap-3' },
    [0, 1].map(i =>
      el(
        'div',
        { className: cn(CARD, 'flex items-center gap-3'), key: i },
        jsx(Skeleton, { className: 'size-9' }),
        el('div', { className: 'flex flex-1 flex-col gap-1.5' }, jsx(Skeleton, { className: 'h-3.5 w-48' }), jsx(Skeleton, { className: 'h-3 w-72 max-w-full' }))
      )
    )
  )
}

// Hermes itself is not up yet (just launched, or its backend is reconnecting): nothing is wrong with
// the plugin, and nothing for the owner to do. The page asks again by itself.
function Starting() {
  return el(
    'div',
    { className: 'flex flex-col gap-3', role: 'status' },
    el('p', { className: cn(LEAD, 'text-center') }, 'Hermes is still starting. Your twins will appear in a moment.'),
    jsx(PageSkeleton, {})
  )
}

function gatewayUp() {
  try {
    const value = host.state?.gateway?.get?.()

    return typeof value !== 'string' || value === 'open'
  } catch {
    return true
  }
}

// Only "this plugin's address does not exist" means the plugin's switch is off.
const pluginMissing = error => /\b404\b|not found/i.test(`${asProblem(error).detail} ${asProblem(error).message}`)

// The plugin's other switch is off. No command first, no error code: where to click, and one way to retry.
function BackendOff({ error }) {
  const detail = asProblem(error).detail

  return el(
    Centred,
    { icon: 'plug', title: 'Twins needs one more switch turned on' },
    el(
      'p',
      { className: LEAD, key: 'p' },
      'In Hermes, open Capabilities → Plugins and turn on ',
      el('strong', { className: 'font-semibold text-(--ui-text-primary)' }, 'Grok Bot Twin'),
      '. This page carries on by itself.'
    ),
    // one button; the page asks again every few seconds, so nothing has to be pressed afterwards
    el(Button, { key: 'b', onClick: () => host.navigate(PLUGINS_SETTINGS) }, 'Open Plugins'),
    el(
      Fold,
      { key: 'f', label: 'Use the terminal instead' },
      el(
        'div',
        { className: 'flex w-full max-w-sm flex-col gap-1.5 text-left' },
        jsx(CommandLine, { command: ENABLE_CMD }),
        el('p', { className: SMALL }, 'Then restart Hermes.'),
        detail && el('p', { className: SMALL }, `Technical details: ${detail}`)
      )
    )
  )
}

// Hermes is running and the plugin's address exists, but it has not answered for a while. Not the switch.
function Unreachable({ error, onRetry, retrying }) {
  const detail = asProblem(error).detail

  return el(
    Centred,
    { icon: 'debug-disconnect', title: 'Twins is not answering right now' },
    el('p', { className: LEAD, key: 'p' }, 'Hermes is running, but Grok Bot Twin has not answered for a minute. Your twins and your Grok bots are untouched.'),
    el(Button, { key: 'b', loading: retrying, onClick: onRetry }, 'Try again'),
    detail && el(Fold, { key: 'f', label: 'Technical details' }, el('p', { className: SMALL }, detail))
  )
}

// Hermes sends a plugin's calls to whichever of its copies is active. After "Open in Hermes" that is the
// twin's own Hermes bot, where this plugin is not running. The page does not explain that to anyone:
// it goes back by itself to where the twins are looked after and shows them.
const HOME_KEY = 'home'

function activeProfile() {
  try {
    for (const atom of [host.state?.profile, host.state?.focusedSessionProfile]) {
      const value = typeof atom?.get === 'function' ? atom.get() : null

      if (typeof value === 'string' && value) {
        return clean(value, 64)
      }
    }
  } catch {
    // an older Desktop without these atoms: the ordinary states apply
  }

  return ''
}

const DEFAULT_HOME = 'default'

// The Hermes bots that are twins on this computer (made here, arrived over a link, or kept after leaving).
// The page never looks after twins from inside one of them: such a bot can be moved away under it.
function twinProfilesOf(pairs, fleet) {
  const names = new Set()
  const add = v => {
    const name = clean(v, 64)

    if (name) {
      names.add(name)
    }
  }

  listOf(pairs).forEach(p => !isSample(p) && add(p?.hermes?.profile))
  listOf(fleet?.twins).forEach(t => add(t?.profile))
  listOf(fleet?.removed).forEach(r => add(r?.profile))

  return names
}

// Where the twins are looked after: the place Twins was last seen working in that is not a twin itself.
function homeOf(storage, twins = null) {
  const stored = clean(storage?.get(HOME_KEY, null)?.profile ?? '', 64)

  return stored && !twins?.has(stored) ? stored : DEFAULT_HOME
}

const canGoHome = () => typeof host.ensureAgent === 'function' || typeof host.newChat === 'function'

// The Desktop is pointed at another Hermes bot than the one the twins are looked after from.
//   the plugin does not answer (`serving` empty): any other place than the remembered one, or a twin's own bot
//   the plugin answers from `serving`: only when that is a twin's own Hermes bot, which unlinking, removing or
//   an update of the plugin can pull away under the page
function awayFromHome(storage, { serving = '', twins = null } = {}) {
  const stored = clean(storage?.get(HOME_KEY, null)?.profile ?? '', 64)
  const here = serving || activeProfile()
  const inTwin = Boolean(here) && twins?.has(here) === true
  const home = homeOf(storage, twins)

  if (!here || here === home || !canGoHome()) {
    return null
  }

  if (serving ? !inTwin : !inTwin && !stored) {
    return null
  }

  return { here, home }
}

// Unlinking or removing moves a twin's Hermes bot away. If Hermes is inside one of them at that moment, it
// steps out first, so the page is still there to say what happened.
async function stepOutOfTwin(storage, twins) {
  const here = activeProfile()

  if (here && twins.has(here) && canGoHome()) {
    await goHome(homeOf(storage, twins))
  }
}

// Quietly: point Hermes back at the place the twins are kept, without opening anything.
// Loudly (the fallback, and the button): start a fresh chat there, then come back to Twins.
async function goHome(home, { quiet = true } = {}) {
  if (quiet && typeof host.ensureAgent === 'function') {
    try {
      await host.ensureAgent(null, home)

      return true
    } catch {
      // fall through to the visible way
    }
  }

  if (typeof host.newChat !== 'function') {
    return false
  }

  host.newChat(home)
  // Switching opens a fresh chat first; come back to Twins once that has settled.
  await pause(400)
  host.navigate(ROUTE)

  return true
}

// Only when going back by itself did not work: one sentence, one button, no names of anything.
function ElsewhereNow({ away, onRetry }) {
  const [busy, run] = useBusy()

  return el(
    Centred,
    { icon: 'mirror', title: 'Your twins are one step away' },
    el('p', { className: LEAD, key: 'p' }, "You are inside one twin's Hermes bot right now. Nothing is wrong with your twins."),
    el(
      Button,
      {
        key: 'b',
        loading: busy === 'go',
        onClick: () =>
          void run('go', async () => {
            await goHome(away.home, { quiet: false })
            onRetry()
          })
      },
      'Show my twins'
    )
  )
}

const PRIVACY = [
  "Grok Bot Twin runs inside Hermes on this computer. It reads your Grok app's bot list (names and descriptions) and, for the bots you twin, when their routines change. It never reads login or password files.",
  'It runs botport, the small app that you downloaded. Hermes verifies it first, unless you chose in the terminal to use a build of your own. The plugin itself downloads nothing and sends no usage data.',
  'Anything that looks like a password or key is not copied (this check can miss things). Whatever a change replaces is kept under Earlier versions.',
  "It uses the network only between your own computers, over your own network. Sharing or linking sends twin names, descriptions and your computers' names, and the settings you let follow (tools, preferences, persona; the model only if you switch it on), to keep your twins the same everywhere. With skills switched on, it also sends your twins' skills (text files only) to your linked computers. With routines switched on, it also sends your twins' routines. With memory switched on for a twin, it also sends that twin's memory, encrypted. Asking a twin on another computer sends your request there and its answer back. Nothing else of your chats, and no other files, passwords or keys, ever leaves a computer.",
  'Unofficial; not affiliated with xAI. Built on botport (MIT licence).'
]

function PrivacySheet({ open, onClose }) {
  return el(Sheet, { onClose, open, title: 'Privacy' }, el('div', { className: cn(BODY, 'flex flex-col gap-2') }, PRIVACY.map((line, i) => el('p', { key: i }, line))))
}

// ── remove Grok Bot Twin (everything) ───────────────────────────────────────

const ALL_STAYS = ['Your Grok bots.', 'Your Hermes bots and their chats.']
const REMOVED_KEY = 'removed'
// (the sync bot is the second bot the owner made in Grok when setting up skill sync: said, not assumed)
const SYNC_BOT_REMINDER = "One thing is left in Grok: the sync bot you made for skill sync. Delete its routine in the Grok app; Hermes cannot reach it."

// Only what exists is listed. The backend filters; the page also drops what it can tell is not there.
function removedLines(plan) {
  const given = lines(plan?.goes, [])
  const noTwins = typeof plan?.twins === 'number' && plan.twins === 0
  const noSample = plan?.sample === false
  const kept = given.filter(l => !(noTwins && /^(every|\d+) twins?\b|earlier versions/i.test(l)) && !(noSample && /\bsample\b/i.test(l)))
  // "2 twins (Trail Guide, Book Club)." and "Earlier versions of 2 twins." are one line: the dialog stays within sixty words
  const twins = kept.find(l => /^(Every|\d+) twins?\b.*\.$/.test(l))
  const versions = kept.find(l => /^Earlier versions\b/.test(l))
  const out = twins && versions ? kept.filter(l => l !== versions).map(l => (l === twins ? `${l.slice(0, -1)}, with ${/^1 twin\b/.test(l) ? 'its' : 'their'} earlier versions.` : l)) : kept

  return out.length ? out : ["Grok Bot Twin's own records on this computer."]
}

// `routinesOn`: routines the owner switched on in Hermes, over all twins (they are Hermes' own by then).
// `fleet`: on a Mac that shares, the dialog says what becomes of the twins on the linked computers.
function UninstallDialog({ open, onClose, onRemoved, routinesOn = 0, fleet = null, storage }) {
  const plan = useQuery({ enabled: open, gcTime: 0, queryFn: () => post('/uninstall', { dry_run: true }), queryKey: [ID, 'uninstall-plan'], retry: false })
  const withSync = (Array.isArray(plan.data?.skill_sync) ? plan.data.skill_sync : []).slice(0, 10).map(n => clean(n, 60)).filter(Boolean)
  const running = Math.max(0, Math.floor(Number(plan.data?.routines_on ?? routinesOn) || 0))
  const given = lines(plan.data?.stays, ALL_STAYS)
  const linked = fleet?.role === 'source' ? fleet.others.map(c => c.name) : []
  const stays = [
    ...given,
    running > 0 && !given.some(l => /routine/i.test(l)) && `${capital(plural(running, 'routine'))} you switched on in Hermes: ${running === 1 ? 'it keeps' : 'they keep'} running there.`,
    // nothing reaches them any more, and nothing is taken from them
    linked.length > 0 && !given.some(l => /other computers|stop following/i.test(l)) && `The twins on ${listWords(linked)}; they stop following.`
  ].filter(Boolean)

  return jsx(Confirm, {
    busyLabel: 'Removing…',
    cancelLabel: 'Cancel',
    children: plan.isPending
      ? jsx(LinesSkeleton, { rows: 4 })
      : plan.isError
        ? jsx(InlineProblem, { error: plan.error, onRetry: () => void plan.refetch() })
        : el(
            'div',
            { className: 'flex flex-col gap-2.5' },
            jsx(StaysGoes, { goes: removedLines(plan.data), stays }),
            // (the sync bot's routine is said on the last screen, where it is the next thing to do)
            // (the one step that is left afterwards is the next screen's own sentence, with its button)
            el('p', { className: cn(BODY, 'text-(--ui-text-primary)') }, 'This cannot be undone. Connecting again starts fresh.')
          ),
    confirmLabel: 'Remove Grok Bot Twin',
    destructive: true,
    onClose,
    onConfirm: async () => {
      if (!plan.data) {
        throw new Error('One moment: still working out what will be removed.')
      }

      // the twins on the page now: a refresh that lands while the backend works must not lose them
      const shown = cachedPairs().map(p => p?.pair_id)

      // the word the backend asks for before it removes anything: this press, after the list above was read
      await post('/uninstall', { confirm: 'remove-everything' })
      // and what the page itself remembered, which is not in the plugin's data folder
      forgetEverything(storage, shown)
      cachedPairs().forEach(p => settleConflictToast(p?.pair_id))
      // No toast: the page itself becomes the last screen, with the one step that is left.
      onRemoved?.({ skill_sync: withSync.length > 0 })
      refreshAll()
    },
    open,
    title: 'Remove Grok Bot Twin?'
  })
}

// The last screen: everything is gone, one step is left, and its button is here.
function RemovedScreen({ syncBot, onStay }) {
  return el(
    Centred,
    { icon: 'pass', title: 'Everything is removed' },
    el(
      'p',
      { className: LEAD, key: 'p' },
      'Your Grok bots and your Hermes chats are untouched. One step left: in Capabilities → Plugins, switch ',
      el('strong', { className: 'font-semibold text-(--ui-text-primary)' }, 'Grok Bot Twin'),
      ' off. Open Plugins shows its card.'
    ),
    syncBot && el('p', { className: BODY, key: 's' }, SYNC_BOT_REMINDER),
    el(Button, { key: 'b', onClick: () => host.navigate(PLUGINS_SETTINGS) }, 'Open Plugins'),
    el(Button, { key: 'stay', onClick: onStay, variant: 'text' }, 'Set up Twins again')
  )
}

// "3 twins and the sample" / "1 twin" / "The sample twin"
function whoWords(pairs) {
  const real = pairs.filter(p => !isSample(p)).length
  const sample = pairs.length > real

  return real === 0 ? 'The sample twin' : `${plural(real, 'twin')}${sample ? ' and the sample' : ''}`
}

// "3 twins · 1 needs your attention · 3 to finish setting up": every kind of open thing is counted, and
// the header and the status bar say the same. `held`: twins cannot follow Grok right now.
function summarize(pairs, { held = false } = {}) {
  if (pairs.length === 0) {
    return { count: 0, label: 'No twins yet. Click to get started.', tone: 'muted' }
  }

  const who = whoWords(pairs)

  if (held) {
    return { count: 1, label: `${who} · not following Grok right now`, tone: 'warn' }
  }

  // A twin whose only open matter is a skill change to send is "to send", not an alarm (as in its row).
  const open = pairs.filter(p => mattersOf(p).length > 0)
  const attention = open.filter(p => !mattersOf(p).every(isToSend))
  const sending = open.length - attention.length
  const finishing = pairs.filter(p => choresLeft(p) > 0).length
  const failing = attention.some(p => p?.state === 'error')
  const updating = pairs.some(p => (Number(p?.pending?.to_hermes) || 0) > 0)
  const parts = [
    attention.length > 0 && `${attention.length} ${attention.length === 1 ? 'needs' : 'need'} your attention`,
    sending > 0 && `${sending} with a change to send to Grok`,
    finishing > 0 && `${finishing} to finish setting up`
  ].filter(Boolean)
  const what = parts.length ? parts.join(' · ') : updating ? 'updating' : 'nothing needs you'

  return { count: attention.length, label: `${who} · ${what}`, tone: failing ? 'bad' : open.length || updating || finishing ? 'warn' : 'good' }
}

// Why the owner's twins cannot follow Grok right now, as one cause for the whole page: said once, in a
// banner, with the button that fixes it when a button can. null: they can.
//   grok_app            the Grok desktop app is gone: nothing here can fix it; it clears by itself
//   botport_unverified  botport changed: a single press verifies it again
//   botport_missing     botport is gone: the Connect sheet gets it back
//   same_error          every twin fails in the same way: said once, with Try again
//   other               the backend's own sentence
const CAUSES = ['botport_missing', 'botport_unverified', 'grok_app', 'no_build']

function followProblem(data, realPairs) {
  if (realPairs.length === 0) {
    return null
  }

  const setup = data?.setup
  const ready = setup ? setup.ready === true : data?.engine?.available !== false
  const given = plainText(setup?.problem, 200)
  let cause = CAUSES.includes(setup?.cause) ? setup.cause : null

  // A backend that does not name the cause: botport first while it says "not ready"; the Grok desktop
  // app either way (today's "ready" does not look at it).
  if (!cause && setup) {
    if (!ready && setup.helper_found === false) cause = 'botport_missing'
    else if (!ready && setup.helper_checked === false) cause = 'botport_unverified'
    else if (setup.grok_app_found === false) cause = 'grok_app'
  }

  if (cause === 'grok_app') {
    return { cause, sentence: 'The Grok desktop app is not on this computer any more. Your twins stop following Grok until it is back. Nothing is lost.' }
  }

  if (cause === 'botport_unverified') {
    return { cause, sentence: 'botport has to be verified again before Hermes runs it. Until then your twins do not follow Grok. Nothing is lost.' }
  }

  if (cause === 'botport_missing') {
    return { cause, sentence: 'botport is no longer on this computer. Until it is back your twins do not follow Grok. Nothing is lost.' }
  }

  if (cause || !ready || given) {
    return { cause: 'other', sentence: `${given || 'Hermes cannot read your Grok bots right now.'} Until that is fixed your twins do not follow Grok. Nothing is lost.` }
  }

  // Two or more twins failing in the same way are one problem, said once: not one box per twin.
  const failing = realPairs.filter(p => p?.state === 'error' && clean(p?.error?.code, 40))
  const byCode = new Map()

  for (const p of failing) {
    const code = clean(p.error.code, 40)

    byCode.set(code, [...(byCode.get(code) ?? []), p])
  }

  const group = [...byCode.values()].sort((a, b) => b.length - a.length)[0] ?? []

  if (group.length < 2) {
    return null
  }

  const first = group[0].error
  const all = key => (group.every(p => p.error?.[key] === first?.[key]) ? plainText(first?.[key], 200) : '')
  const ids = new Set(group.map(p => p.pair_id))

  // every twin: they are not following Grok at all right now
  if (group.length === realPairs.length) {
    return { cause: 'same_error', ids, partial: false, sentence: [all('message') || 'Hermes could not check your twins.', all('hint'), 'Your twins and your Grok bots are untouched.'].filter(Boolean).join(' ') }
  }

  // some of them: the reason once, with their number where each sentence names its own twin
  const NAME = '\u0000'
  const shapes = group.map(p => plainText(p.error?.message, 200).replace(nameOf(p), NAME))
  const shared = shapes[0].includes(NAME) && shapes.every(m => m === shapes[0]) ? shapes[0].replace(NAME, `${group.length} of your twins`) : all('message')

  return { cause: 'same_error', ids, partial: true, sentence: [shared || `${group.length} of your ${realPairs.length} twins cannot be checked right now.`, all('hint')].filter(Boolean).join(' ') }
}

// How often the page reads /status. A banner that clears by itself has to go soon after its cause has
// (Grok is back, botport is back): every 4 s while one shows or the last reading failed, every 30 s when calm.
function statusEvery(q) {
  if (q?.state?.status === 'error') {
    return 4_000
  }

  const data = q?.state?.data
  const real = Array.isArray(data?.pairs) ? data.pairs.filter(p => !isSample(p)) : []

  return followProblem(data, real) !== null ? 4_000 : 30_000
}

// `initialBack` lets the smoke test render the screen shown when going back by itself did not work;
// `initialMountedAt` the one shown when the plugin has not answered for a while.
function TwinsPage({ storage, initialBack = 'idle', initialMountedAt = null }) {
  const status = useQuery({ queryFn: () => api('/status'), queryKey: STATUS_KEY, refetchInterval: statusEvery, retry: 1 })
  const [busy, run] = useBusy()
  const [flow, setFlow] = useState(false) // false | true | 'reconnect'
  const [flowProblem, setFlowProblem] = useState(null) // why Verify botport failed: said in the Connect sheet
  const [sheet, setSheet] = useState(null) // 'privacy' | 'uninstall' | 'link' | 'unlink' | null
  const [request, setRequest] = useState(null) // { pairId, sheet }: a row is asked to open one of its sheets
  const [stopped, setStopped] = useState(null) // what became of a twin that was just stopped (see StoppedLine)
  const [removed, setRemoved] = useState(() => storage?.get(REMOVED_KEY, null) ?? null)
  const [back, setBack] = useState(initialBack) // 'idle' | 'going' | 'done' | 'failed'
  // a batch handed to Hermes for the Grok part less than half an hour ago: its sheet opens again (BATCH_KEY)
  const [openBatch, setOpenBatch] = useState(() => openBatchOf(storage))
  const mountedAt = useRef(initialMountedAt ?? Date.now())
  const titleRef = useRef(null)

  const data = status.data
  const pairs = useMemo(() => (Array.isArray(data?.pairs) ? data.pairs : []), [data])
  // Other computers: /status says in brief whether this backend can share or follow at all; the whole
  // picture is its own cheap answer, asked for more often while a link code is on screen (the computer
  // that uses it shows up by itself).
  const brief = data?.fleet && typeof data.fleet === 'object' ? data.fleet : null
  const fleetQ = useQuery({ enabled: Boolean(brief), queryFn: () => api('/fleet'), queryKey: FLEET_KEY, refetchInterval: q => (q?.state?.data?.share?.code ? 2_000 : 5_000), retry: 1 })
  const fleet = brief ? fleetOf(fleetQ.data) : null
  const following = (fleet?.role ?? brief?.role) === 'follower'
  const sampleOn = data?.sample?.on === true || pairs.some(isSample)
  const realPairs = pairs.filter(p => !isSample(p))
  const setupReady = data?.setup ? data.setup.ready === true : data?.engine?.available !== false
  const trouble = followProblem(data, realPairs)
  // A step of a request from another computer waits for the owner's answer here: it is first on the page, it
  // is counted in the header, and its "Do it once" is the page's one main button.
  const requests = useRequests(following || fleet?.role === 'source')
  // (a step on another computer that this one may answer needs the owner just the same)
  const asking = requests ? requests.waiting.length + requests.asking.length : 0
  // `held`: no twin follows Grok right now. A trouble that some twins share (and others do not) has its banner too,
  // but the page as a whole is not held.
  const held = trouble !== null && trouble.partial !== true
  const grouped = trouble?.partial === true ? trouble.ids : null
  const info = summarize(pairs, { held })
  const hasData = typeof data?.has_data === 'boolean' ? data.has_data : pairs.length > 0 || sampleOn || data?.setup?.helper_found === true
  const quiet = (key, label, onClick, extra) => el(Button, { key, onClick, variant: 'text', ...extra }, label)
  // Looked at from inside a twin's own Hermes bot (after Open in Hermes): the page goes back by itself to the
  // place the twins are looked after from, whether or not the plugin happens to answer in there.
  const twinNames = twinProfilesOf(pairs.length > 0 ? pairs : cachedPairs(), fleet ?? cachedFleet())
  const serving = !status.isError && data ? clean(data.profile ?? '', 64) : ''
  const fleetKnown = !brief || Boolean(fleet)
  // (when the plugin does not answer: only if its address is really gone, which is what being inside another
  // Hermes bot looks like. A Hermes that is still starting, or a call that timed out, moves nothing.)
  const away = status.isError ? (gatewayUp() && pluginMissing(status.error) ? awayFromHome(storage, { twins: twinNames }) : null) : serving && fleetKnown ? awayFromHome(storage, { serving, twins: twinNames }) : null
  // The plugin or the fleet answer is not coming through: nothing on screen may claim to be fresh.
  const stale = following && (status.isError || fleetQ.isError)
  const starting = status.isError && !data && !away && (!gatewayUp() || (!pluginMissing(status.error) && Date.now() - mountedAt.current < 60_000))
  // The banner's own button is the page's primary one when a click there can fix things.
  const bannerButton = trouble !== null && trouble.cause !== 'grok_app'
  // Otherwise the one row that carries it: the first twin with a button the owner can press (a matter
  // whose box has a main action, else set-up that is left to finish).
  // (a bot gone from Grok that the owner chose to wait for is no open matter: its row says so in a line)
  const openOf = p => mattersOf(p).filter(m => !(m.kind === 'error' && held && !isSample(p)) && !(m.kind === 'missing' && storage?.get(`kept.${p?.pair_id}`, false) === true))
  const leadId = bannerButton || asking > 0
    ? null
    : (pairs.find(p => {
        const open = openOf(p)

        return (open.length > 0 && hasMainAction(open[0], p)) || choresLeft(p) > 0
      })?.pair_id ?? null)

  const firstOpenId = pairs.find(p => openOf(p).some(m => !(m.kind === 'error' && grouped?.has(p?.pair_id))))?.pair_id ?? null

  // While this page is on screen its rows say what a toast would say.
  useEffect(() => {
    pageOpen += 1
    clearPageToasts()

    return () => {
      pageOpen = Math.max(0, pageOpen - 1)
    }
  }, [])

  // Remember where Twins works, so a later visit from a twin's own copy can come back here by itself.
  // A twin's own Hermes bot is never remembered as that place. (And nothing is remembered again after
  // Remove Grok Bot Twin, which clears what the page kept.)
  const twinKey = [...twinNames].sort().join('|')

  useEffect(() => {
    if (!removed && serving && fleetKnown && !twinNames.has(serving)) {
      storage?.set(HOME_KEY, { profile: serving })
    }
  }, [serving, fleetKnown, twinKey, Boolean(removed)])

  // Opened from a twin's own Hermes bot: go back by itself, once for each such visit, then show the twins.
  const wentFor = useRef(initialBack === 'idle' ? '' : 'given')

  useEffect(() => {
    if (!away) {
      wentFor.current = ''

      return
    }

    if (wentFor.current) {
      return
    }

    wentFor.current = `${away.here}>${away.home}`
    setBack('going')
    void goHome(away.home)
      .then(async ok => {
        // judged on a fresh answer: still away after that means going back did not work
        await status.refetch()

        // what the twin's own copy answered is not shown for a moment longer
        try {
          await queryClient.invalidateQueries({ queryKey: [ID] })
        } catch {
          // the next poll brings it
        }

        setBack(ok ? 'done' : 'failed')
      })
      .catch(() => setBack('failed'))
  }, [away?.home, away?.here])

  useTick()

  // A twin or the sample came back: the "everything is removed" screen has had its day. (Right after
  // the removal the old list is still in hand for a moment; that is not "came back".)
  const justRemoved = useRef(false)

  useEffect(() => {
    if (!removed || !data) {
      return
    }

    if (pairs.length === 0) {
      justRemoved.current = false
    } else if (!justRemoved.current) {
      storage?.remove?.(REMOVED_KEY)
      setRemoved(null)
    }
  }, [removed, pairs.length, Boolean(data)])

  function stay() {
    storage?.remove?.(REMOVED_KEY)
    setRemoved(null)
    // the button that was pressed is gone with its screen: the new first screen's title takes the keyboard
    timers?.setTimeout(() => titleRef.current?.focus?.(), 80)
  }

  // A twin that was stopped and is twinned again: the line saying it is "no longer twinned" goes by itself.
  // (Right after the stop the old list is still in hand for a moment: it has to be seen gone first.)
  const stoppedHere = Boolean(stopped?.profile) && realPairs.some(p => clean(p?.hermes?.profile || '', 64) === stopped.profile)
  const stoppedGone = useRef(false)

  useEffect(() => {
    if (!stopped) {
      stoppedGone.current = false
    } else if (!stoppedHere) {
      stoppedGone.current = true
    } else if (stoppedGone.current) {
      setStopped(null)
    }
  }, [Boolean(stopped), stoppedHere, pairs])

  // A single press. If it fails, the Connect sheet opens with what went wrong and the download as the fix.
  async function verifyBotport() {
    const res = await run('trust', async () => {
      try {
        return await post('/engine/trust', {})
      } catch (err) {
        setFlowProblem(asProblem(err))
        setFlow('reconnect')

        return null
      }
    })

    if (res) {
      host.notify({ id: `${ID}:trust`, kind: 'success', message: 'botport is verified', meta: realPairs.length > 0 ? 'Your twins follow Grok again.' : undefined })
      refreshAll()
    }
  }

  function connect(kind = true) {
    setFlowProblem(null)
    setFlow(kind)
  }

  // The sample opens on its point: the bot and its twin side by side, one press away from watching it follow.
  async function beginSample() {
    const res = await startSample(run, { quiet: true })
    const made = listOf(res?.pairs).find(isSample)

    if (made?.pair_id) {
      setRequest({ pairId: made.pair_id, sheet: 'see' })
    }
  }

  const stopSample = () => void run('end-sample', () => endSample(pairs, () => void beginSample(), storage), 'Could not end the sample')

  const tryAll = () => void run('all', () => checkAndToast({}), 'Could not check your twins')
  const bannerAction =
    trouble === null || trouble.cause === 'grok_app'
      ? null
      : trouble.cause === 'botport_unverified'
        ? el(Button, { disabled: Boolean(busy), loading: busy === 'trust', onClick: () => void verifyBotport() }, 'Verify botport')
        : trouble.cause === 'same_error'
          ? el(Button, { disabled: Boolean(busy), loading: busy === 'all', onClick: tryAll }, 'Try again')
          : el(Button, { disabled: Boolean(busy), onClick: () => connect('reconnect') }, trouble.cause === 'botport_missing' ? 'Get botport again…' : 'Reconnect…')

  const connectLabel = realPairs.length > 0 ? 'Add a bot' : 'Connect my Grok bots'
  const title = el(
    'h1',
    {
      className: 'text-lg font-semibold text-(--ui-text-primary) outline-none',
      ref: node => {
        titleRef.current = node
        pageTitle = node
      },
      tabIndex: -1
    },
    'Twins'
  )
  const gone = Boolean(removed) && (pairs.length === 0 || justRemoved.current)
  const showList = !status.isPending && !away && !(status.isError && !data) && (pairs.length > 0 || following) && !gone
  // On a computer that follows the Mac the header says where its twins come from and how fresh they are.
  // (a request that waits is counted like every other open thing; the page never says "nothing needs you" beside it)
  const waits = asking > 0 ? ` · ${plural(asking, 'request')} ${asking === 1 ? 'waits' : 'wait'} for you` : ''
  const headline = `${(following ? (fleet ? followerSummary(fleet, { stale }) : briefSummary(brief)).label : info.label).replace(asking > 0 ? ' · nothing needs you' : '\u0000', '')}${waits}`
  // Sharing is offered where there is something to share, and stays in reach while a computer is still linked.
  const sharing = fleet && !following && (fleet.role === 'source' || (fleet.canShare && realPairs.length > 0)) ? fleet : null
  // Is every twin on every linked computer, with sharing on? Then the list of computers says where the twins
  // are, once, and no row repeats it (a calm page with three twins stays within its sixty words).
  const linkedIds = sharing?.share.on ? sharing.others.map(c => c.id).sort().join('|') : ''
  const everywhere = linkedIds !== '' && realPairs.every(p => onAll(p?.fleet?.machines, linkedIds))
  const sampleRows = pairs.filter(isSample)
  const calmChecked = !following && !status.isError && realPairs.length > 0 && info.tone === 'good' && !trouble && asking === 0 ? ago(data?.last_check_at) : null
  const header = el(
    'header',
    { className: 'flex flex-col gap-0.5 pb-1 pt-3' },
    title,
    // (a calm page also says when it last looked, so "is everything all right?" needs no hover)
    showList && el('p', { className: BODY }, calmChecked ? `${headline} · checked ${calmChecked}` : headline),
    // (for a screen reader only: the page came back from a twin's own Hermes bot by itself)
    back === 'done' && showList && el('p', { className: 'sr-only', role: 'status' }, 'Showing your twins.')
  )

  let body

  if (status.isPending) {
    body = jsx(PageSkeleton, {})
  } else if (away) {
    body = (back === 'failed' || back === 'done') && wentFor.current ? jsx(ElsewhereNow, { away, onRetry: () => void status.refetch() }) : el('div', { className: 'flex flex-col gap-3', role: 'status' }, el('p', { className: cn(LEAD, 'text-center') }, 'Going back to your twins…'), jsx(PageSkeleton, {}))
  } else if (starting) {
    body = jsx(Starting, {})
  } else if (status.isError && !data) {
    // the switch screen only when the plugin's address really does not exist
    body = jsx(pluginMissing(status.error) ? BackendOff : Unreachable, { error: status.error, onRetry: () => void status.refetch(), retrying: status.isFetching })
  } else if (gone) {
    body = jsx(RemovedScreen, { onStay: stay, syncBot: removed?.skill_sync === true })
  } else if (following) {
    body = el(
      'div',
      { className: 'flex flex-col gap-3' },
      el('p', { className: 'sr-only', role: 'status' }, headline),
      // one cause, said once: the plugin, or its answer about the other computers, is not coming through
      (status.isError || (fleet && fleetQ.isError)) &&
        jsx(InlineProblem, {
          error: status.isError ? status.error : fleetQ.error,
          onRetry: () => {
            void status.refetch()
            void fleetQ.refetch()
          }
        }),
      // a step of a request from another computer waits for the owner here: first on the page
      fleet && !stale && jsx(RequestsWaiting, { lead: true }),
      fleet ? jsx(FollowerView, { fleet, noLead: asking > 0, onUnlink: stale ? undefined : () => setSheet('unlink'), stale }) : fleetQ.isError ? (status.isError ? null : jsx(InlineProblem, { error: fleetQ.error, onRetry: () => void fleetQ.refetch() })) : jsx(PageSkeleton, {}),
      // which of a twin's settings follow from the other computer, and the way back
      fleet && !stale && jsx(SettingsSection, {}),
      // requests from the owner's other computers: the switch and what was asked
      // what the twins do on a schedule, and on which computer each routine runs
      fleet && !stale && jsx(RoutinesSection, {}),
      fleet && jsx(RequestsSection, {}),
      fleet && jsx(MemorySection, {}),
      // the sample can be looked at here too; it is never one of the Mac's twins
      sampleOn && jsx(SampleBanner, { hideReal: false, onEnd: stopSample, pairs }),
      sampleRows.map(p => jsx(TwinRow, { example: data?.sample?.instructions_example, fastCheck: data?.fast_check, intervalS: data?.interval_s, lastCheckAt: data?.last_check_at, lead: p?.pair_id === leadId, onEndSample: stopSample, pair: p, storage }, p.pair_id))
    )
  } else if (pairs.length === 0 && !sharing) {
    body = jsx(Welcome, {
      busy,
      grokHere: data?.setup?.grok_app_found !== false,
      kept: fleet && fleet.removed.length > 0 ? jsx(KeptBots, { fleet, here: unlinkedHere(fleet, storage) }) : null,
      note: unlinkedSentence(fleet, unlinkedHere(fleet, storage)),
      onConnect: () => connect(),
      onLink: brief ? () => setSheet('link') : undefined,
      onSample: () => void beginSample(),
      returning: hasData || notesLeft(storage)
    })
  } else {
    body = el(
      'div',
      { className: 'flex flex-col gap-3' },
      // Every attention state is announced through this one region.
      el('p', { className: 'sr-only', role: 'status' }, headline),
      status.isError && jsx(InlineProblem, { error: status.error, onRetry: () => void status.refetch() }),
      trouble &&
        el(
          'div',
          { 'aria-label': held ? 'Twins are not following Grok' : 'Some twins cannot be checked', className: cn(NOTE, 'flex flex-wrap items-center gap-2'), role: 'region' },
          jsx(Icon, { className: 'text-(--ui-text-primary)', name: trouble.cause === 'grok_app' ? 'circle-slash' : 'warning' }),
          el(
            'div',
            { className: 'flex min-w-0 flex-1 flex-col' },
            el('span', { className: 'gbt-line text-(--ui-text-primary)' }, trouble.sentence),
            // nothing on this page can fix it, and nothing has to be pressed afterwards
            trouble.cause === 'grok_app' && el('span', { className: 'gbt-line' }, 'This clears by itself when Grok is back.')
          ),
          bannerAction
        ),
      data?.engine?.allow_unattested === true &&
        realPairs.length > 0 &&
        !held &&
        el(
          'div',
          { className: cn(NOTE, 'flex flex-wrap items-center gap-2'), role: 'note' },
          el('span', { className: 'min-w-0 flex-1' }, 'botport is in use but has not been verified.'),
          el(Button, { disabled: Boolean(busy), loading: busy === 'trust', onClick: () => void verifyBotport(), variant: 'secondary' }, 'Verify botport')
        ),
      sharing && jsx(RequestsWaiting, { lead: !bannerButton }),
      sampleOn && jsx(SampleBanner, { hideReal: data?.sample?.hide_real === true, onEnd: stopSample, pairs }),
      pairs.map((p, i) =>
        jsx(
          TwinRow,
          {
            example: data?.sample?.instructions_example,
            fastCheck: data?.fast_check,
            // its trouble is said once, in the banner above: no box of its own
            grouped: grouped?.has(p?.pair_id) === true,
            held,
            intervalS: data?.interval_s,
            lastCheckAt: data?.last_check_at,
            // one box at a time: the row with the page's main button shows its matter (else the first row with
            // something open); the others say theirs in one line until opened
            folded: p?.pair_id !== (leadId ?? firstOpenId),
            lead: p?.pair_id === leadId,
            onEndSample: stopSample,
            onRequestDone: () => setRequest(null),
            onStopped: setStopped,
            pair: p,
            // (also while a link code is on screen: for those minutes the page is about linking)
            plain: asking > 0 || Boolean(sharing?.share?.code),
            request,
            // (what the page knows of sharing right now, for the words of a copy that is still to come)
            fleetNow: sharing ? { on: sharing.share.on, states: new Map(sharing.others.map(c => [c.id, c.state])) } : null,
            showWhere: !everywhere,
            storage
          },
          p?.pair_id ?? i
        )
      ),
      // The way to more twins sits at the end of the list, in the same column; it is the primary
      // button only while nothing above it needs the owner and no bot of theirs is connected yet.
      // While twins cannot follow Grok there is no bot list to add from.
      !held &&
        el(
          'div',
          { className: 'flex flex-wrap items-center justify-center gap-2 pt-1' },
          // (not while a row has something open: the page stays within its words)
          realPairs.length === 0 && !firstOpenId && el('span', { className: BODY }, 'Ready for your own bots?'),
          // (never while a row has something open: its box holds what to press, even when that is a choice of two)
          el(Button, { disabled: Boolean(busy), onClick: () => connect(), variant: realPairs.length === 0 && !leadId && asking === 0 && !pairs.some(p => mattersOf(p).length > 0) ? 'default' : 'outline' }, jsx(Icon, { name: 'add' }), connectLabel)
        ),
      sharing && jsx(ShareSection, { fleet: sharing }),
      // which settings of the twins follow to the linked computers (only once this computer shares)
      sharing?.role === 'source' && jsx(SettingsSection, {}),
      // what the twins do on a schedule, and on which computer each routine runs
      sharing && sharing.role === 'source' && jsx(RoutinesSection, {}),
      sharing && jsx(RequestsSection, {}),
      sharing && jsx(MemorySection, {})
    )
  }

  const routinesOn = realPairs.reduce((n, p) => n + (Number(p?.switched?.routines_on) || 0), 0)

  return el(
    'div',
    { className: 'gbt relative flex h-full min-h-0 flex-col overflow-hidden' },
    el('style', {}, PAGE_CSS),
    el(
      'div',
      { className: 'min-h-0 flex-1 overflow-y-auto px-4 pb-6' },
      el(
        'div',
        { className: 'mx-auto flex min-h-full w-full max-w-3xl flex-col gap-3' },
        header,
        // a twin was just stopped: where its Hermes bot is now, said on the page whatever the page shows next
        stopped && !away && !gone && jsx(StoppedLine, { onClose: () => setStopped(null), result: stopped }),
        body,
        data &&
          !away &&
          !gone &&
          el(
            'footer',
            { className: 'flex flex-wrap items-center justify-center gap-x-3 gap-y-1 pt-2' },
            quiet('privacy', 'Privacy', () => setSheet('privacy')),
            (pairs.length > 0 || following) && !sampleOn && quiet('sample', 'See a sample', () => void beginSample(), { disabled: Boolean(busy), loading: busy === 'sample' }),
            (hasData || notesLeft(storage)) && quiet('uninstall', 'Remove Grok Bot Twin…', () => setSheet('uninstall'))
          )
      )
    ),
    flow &&
      jsx(AddFlow, {
        onClose: () => setFlow(false),
        onFinish: pairId => {
          setFlow(false)
          setRequest({ pairId, sheet: 'finish' })
        },
        // "my bots are on another computer": the other way in
        onLink:
          brief && !following && fleet?.role !== 'source'
            ? () => {
                setFlow(false)
                setSheet('link')
              }
            : undefined,
        // "My bots are elsewhere": the sample is still there to look at
        onSample: sampleOn
          ? undefined
          : () => {
              setFlow(false)
              void beginSample()
            },
        open: true,
        problem: flowProblem,
        ready: setupReady && data?.setup?.ready === true && data?.setup?.grok_app_found !== false,
        reconnect: flow === 'reconnect',
        storage
      }),
    openBatch &&
      !flow &&
      pairs.some(p => openBatch.ids.includes(p?.pair_id)) &&
      jsx(BatchSheet, {
        onClose: () => setOpenBatch(null),
        onOneAtATime: pairId => {
          setOpenBatch(null)
          setRequest({ pairId, sheet: 'finish' })
        },
        open: true,
        pairs: pairs.filter(p => openBatch.ids.includes(p?.pair_id)),
        resumed: openBatch,
        storage
      }),
    sheet === 'privacy' && jsx(PrivacySheet, { onClose: () => setSheet(null), open: true }),
    sheet === 'link' &&
      jsx(LinkSheet, {
        onClose: () => setSheet(null),
        // the button that opened the sheet is gone by then: the page's title takes the keyboard
        onLinked: () => (timers ? timers.setTimeout(() => titleRef.current?.focus?.(), 80) : undefined),
        open: true,
        platform: fleet?.me.platform
      }),
    sheet === 'unlink' && fleet && jsx(UnlinkDialog, { fleet, onClose: () => setSheet(null), open: true, storage }),
    sheet === 'uninstall' &&
      jsx(UninstallDialog, {
        onClose: () => setSheet(null),
        onRemoved: what => {
          justRemoved.current = true
          storage?.set(REMOVED_KEY, what)
          setRemoved(what)
        },
        fleet,
        open: true,
        routinesOn,
        storage
      })
  )
}

// ── status bar ──────────────────────────────────────────────────────────────

let chipStorage = null // ctx.storage, for the status bar item (set at register)

function StatusChip() {
  const q = useQuery({ queryFn: () => api('/status'), queryKey: STATUS_KEY, refetchInterval: 30_000, retry: false })
  const pairs = Array.isArray(q.data?.pairs) ? q.data.pairs : []

  // Always mounted: every fresh status takes down "changed in both places" toasts that no longer apply.
  useEffect(() => {
    if (q.data) {
      reconcileConflictToasts(q.data.pairs, q.dataUpdatedAt || Date.now())
    }
  }, [q.data, q.dataUpdatedAt])

  const away = q.isError ? awayFromHome(chipStorage) : null
  const real = pairs.filter(p => !isSample(p))
  const info = away
    ? { count: 0, label: 'Open Twins to see your twins', tone: 'muted' }
    : q.isError && !q.data
      ? !gatewayUp() || !pluginMissing(q.error)
        ? { count: 0, label: 'Hermes is still starting', tone: 'muted' }
        : { count: 0, label: 'Twins is not turned on yet', tone: 'muted' }
      : q.data?.fleet?.role === 'follower'
        ? briefSummary(q.data.fleet)
        : summarize(pairs, { held: followProblem(q.data, real) !== null && followProblem(q.data, real).partial !== true })

  return el(
    Tip,
    { label: info.label },
    el(
      'button',
      {
        'aria-label': `Twins: ${info.label}`,
        className: cn(
          'inline-flex h-full items-center gap-1.5 rounded-none px-1.5 text-xs outline-none transition-colors',
          'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-(--ui-text-primary)',
          'focus-visible:ring-2 focus-visible:ring-ring/50'
        ),
        onClick: () => host.navigate(ROUTE),
        type: 'button'
      },
      jsx(StatusDot, { tone: info.tone }),
      // The count shows only when something needs attention.
      el('span', {}, info.count > 0 ? `Twins · ${info.count}` : 'Twins')
    )
  )
}

// ── palette actions ─────────────────────────────────────────────────────────

async function paletteCheck() {
  try {
    await checkAndToast({})
  } catch (err) {
    notifyProblem(err, 'Could not check your twins')
  }
}

async function paletteSample() {
  host.navigate(ROUTE)

  if (cachedPairs().some(isSample)) {
    return
  }

  await startSample(async (_name, fn, title) => {
    try {
      return await fn()
    } catch (err) {
      notifyProblem(err, title)

      return undefined
    }
  })
}

// ── plugin ──────────────────────────────────────────────────────────────────

export default {
  id: ID,
  name: 'Grok Bot Twin',
  description: 'Twins page, sidebar row, status-bar item and ⌘K commands for Grok Bot Twin.',
  register(ctx) {
    rest = ctx.rest
    os = ctx.os ?? null
    files = ctx.files ?? null
    timers = ctx
    const storage = ctx.storage
    chipStorage = storage

    ctx.registerMany([
      { area: ROUTES_AREA, data: { path: ROUTE }, id: 'page', render: () => jsx(TwinsPage, { storage }) },
      { area: SIDEBAR_NAV_AREA, data: { codicon: 'mirror', label: 'Twins', path: ROUTE }, id: 'nav', order: 60 },
      { area: STATUSBAR_AREAS.right, id: 'status', order: 115, render: () => jsx(StatusChip, {}) },
      {
        area: PALETTE_AREA,
        data: { id: `${ID}.sync`, keywords: ['twin', 'twins', 'grok', 'check', 'sync', 'bot'], label: 'Twins: Check now', run: () => void paletteCheck() },
        id: 'palette-sync'
      },
      {
        area: PALETTE_AREA,
        data: { id: `${ID}.open`, keywords: ['twin', 'twins', 'grok', 'bot'], label: 'Twins: Open', run: () => host.navigate(ROUTE) },
        id: 'palette-open'
      },
      {
        area: PALETTE_AREA,
        data: { id: `${ID}.demo`, keywords: ['twin', 'twins', 'grok', 'sample', 'demo', 'try'], label: 'Twins: See a sample', run: () => void paletteSample() },
        id: 'palette-demo'
      }
    ])

    for (const type of EVENT_TYPES) {
      ctx.onEvent(`plugin.${ID}.${type}`, event => handleEvent({ payload: event?.payload, type }, ctx))
    }

    startEventPolling(ctx)

    ctx.onDispose(() => {
      rest = null
      os = null
      files = null
      timers = null
      chipStorage = null
      refreshTimer = null
      seenKeys.clear()
      attentionToasts.clear()
      conflictToasts.clear()
      secretToasts.clear()
      manualChecks.clear()
      pageOpen = 0
    })
  }
}
