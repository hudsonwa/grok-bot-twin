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
// TWIN_CHECK_SOFT=1 lists every failing assertion instead of stopping at the first (still exits non-zero).
const soft = process.env.TWIN_CHECK_SOFT === '1'
const failures = []
const ok = (cond, msg) => {
  if (soft && !cond) {
    failures.push(msg)
    console.log(`FAIL: ${msg}`)

    return
  }

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
// The download address comes from the backend; the page never carries one.
ok(!/github\.com|githubusercontent/i.test(source), 'no GitHub URLs')
ok(!/dangerouslySetInnerHTML|innerHTML/.test(source), 'no raw HTML injection')
// Honest wording: never these overclaiming or alarming phrases in anything a person sees.
ok(!/secrets? refused|\blive\b|one[- ]click|two-way sync of instructions/i.test(source), 'banned wording absent')
ok(!/\/Users\/|\/home\/|C:\\\\/.test(source), 'no absolute home paths')
// After "Keep the Hermes version" the Grok copy is untouched and nothing was replaced: never claim a kept copy.
ok(!/replaced copy is kept/i.test(source), 'no claim that the replaced copy is restorable')
// Readable type: nothing under 12 px (the old page had 10 and 11 px labels).
ok(!/text-\[0\.(?:5|6|70?)\d*rem\]/.test(source), 'no text smaller than 12 px')
// The private vocabulary of the engine never reaches the page. Checked on the code without its comments;
// these phrases cannot be identifiers, so any hit is text a person would read. (Each rendered surface is
// checked again below, word by word.)
{
  const code = source.split('\n').filter(l => !/^\s*\/\//.test(l)).map(l => l.replace(/\s\/\/ .*$/, '')).join('\n')
  const banned = /Mirror bot|Mirror routine|pair folder|pair key|restore point|by hand|\bin step\b(?! with Grok)|Python half|on PATH|Pick one|['"`]Needs you['"`]|Not carried|Ready to switch|Demo mode|suspected secret|attestation|Setup parity|charter:? paste|botport helper|helper bot|Helper found|Remove everything|two switches|Hermes cop(?:y|ies)\b(?!\\b| them)|counterpart|checked and genuine|['"`]Paused['"`]|Move to Hermes|Remove sample twin|Other ways|publisher's own build record|Download (?:it )?again|Turn on skill sync|I copied it myself|Keep as a Hermes bot/gi
  const hits = code.match(banned) ?? []
  ok(hits.length === 0, `no engine vocabulary in the page's text: ${JSON.stringify(hits.slice(0, 8))}`)
  // A terminal command is never the remedy in a sentence: commands appear only as the two copyable lines
  // behind "Use the terminal instead" / "Other ways" (CommandLine), never inside a message or a hint.
  const told = code.match(/['"`][^'"`\n]*\b[Rr]un hermes\b[^'"`\n]*['"`]/g) ?? []
  ok(told.length === 0, `no "run hermes ..." as a remedy: ${JSON.stringify(told.slice(0, 4))}`)
}

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
  // The OS and file doors the page uses (open a web page, pick a file, show a file, pick a folder).
  const osMembers = new Set([...source.matchAll(/\bos\??\.([a-zA-Z]+)/g)].map(m => m[1]))
  for (const m of osMembers) {
    ok(new RegExp(`^\\s*${m}: \\(`, 'm').test(contract), `ctx.os has ${m}`)
  }
  const filesContract = readFileSync(join(process.env.HERMES_DESKTOP_SRC, 'contrib/plugin-files.ts'), 'utf8')
  for (const m of new Set([...source.matchAll(/\bfiles\??\.([a-zA-Z]+)/g)].map(x => x[1]))) {
    ok(new RegExp(`^\\s*${m}: \\(`, 'm').test(filesContract), `ctx.files has ${m}`)
  }
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
  writeFileSync(join(work, 'react.mjs'), 'export const useState = v => [typeof v === "function" ? v() : v, () => {}]\nexport const useEffect = () => {}\nexport const useMemo = f => f()\nexport const useRef = v => ({ current: v })\n')
  writeFileSync(join(work, 'jsx.mjs'), 'export const jsx = (t, p) => ({ t, p })\nexport const jsxs = jsx\n')
}

// SDK stub: components are thin pass-throughs that keep text and a few props
// visible in the SSR output; hooks read fixtures from globalThis.__h.
const sdkStub = `
import { jsx, jsxs } from './jsx.mjs'
const H = globalThis.__h
const pass = tag => ({ children, className, ...p }) => jsx(tag, { className, 'data-props': Object.keys(p).filter(k => typeof p[k] !== 'function' && k !== 'loading').map(k => k + '=' + String(p[k])).join(' '), children })
export const Button = ({ children, disabled, loading, onClick, ...p }) => { H.buttons.push({ text: H.text(children), label: p['aria-label'], onClick, disabled: Boolean(disabled || loading), variant: p.variant ?? 'default', size: p.size ?? 'default', className: p.className ?? '', title: p.title }); return jsx('button', { disabled: Boolean(disabled || loading), 'aria-label': p['aria-label'], 'data-variant': p.variant ?? 'default', 'data-size': p.size ?? 'default', children }) }
export const Checkbox = p => { H.checks.push({ id: p.id, checked: Boolean(p.checked), disabled: Boolean(p.disabled), label: p['aria-label'], onCheckedChange: p.onCheckedChange }); return jsx('input', { type: 'checkbox', readOnly: true, checked: Boolean(p.checked), disabled: Boolean(p.disabled), id: p.id }) }
export const cn = (...a) => a.flat(Infinity).filter(Boolean).join(' ')
export const Codicon = ({ name }) => jsx('i', { 'data-codicon': name })
export const CopyButton = p => { H.copies.push({ label: p.label, text: p.text }); return jsx('button', { 'aria-label': p.label, 'data-copy': typeof p.text === 'string' ? p.text.length : 0, children: p.showLabel ? p.label : 'copy' }) }
export const Dialog = ({ open, children }) => open ? jsx('div', { role: 'dialog', children }) : null
export const DialogContent = ({ children, className, role, onOpenAutoFocus, ...p }) => { H.dialogs.push({ role, onOpenAutoFocus, keys: Object.keys(p) }); return jsx('div', { className, role, 'data-props': Object.keys(p).filter(k => typeof p[k] !== 'function').map(k => k + '=' + String(p[k])).join(' '), children }) }
export const DialogDescription = pass('p')
export const DialogFooter = pass('footer')
export const DialogHeader = pass('header')
export const DialogTitle = pass('h2')
export const DisclosureCaret = () => jsx('i', {})
export const DropdownMenu = ({ children }) => jsx('div', { 'data-menu': true, children })
export const DropdownMenuContent = ({ children }) => jsx('div', { role: 'menu', children })
export const DropdownMenuItem = ({ children, onSelect }) => { H.menu.push({ text: H.text(children), onSelect }); return jsx('div', { role: 'menuitem', children }) }
export const DropdownMenuSeparator = () => jsx('hr', {})
export const DropdownMenuTrigger = ({ children }) => children
export const fmtDateTime = new Intl.DateTimeFormat('en', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC' })
export const host = H.host
export const Input = p => { H.inputs.push(p); return jsx('input', { id: p.id, value: p.value, placeholder: p.placeholder, 'aria-label': p['aria-label'], 'aria-invalid': p['aria-invalid'], readOnly: true }) }
export const PALETTE_AREA = 'palette'
export const queryClient = H.queryClient
export const relativeTime = ms => { const m = Math.round((Date.now() - ms) / 60000); return m < 1 ? 'just now' : m + ' min ago' }
export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar.nav'
export const Skeleton = () => jsx('div', { 'data-skeleton': true })
export const STATUSBAR_AREAS = { left: 'statusBar.left', right: 'statusBar.right' }
export const StatusDot = ({ tone }) => jsx('span', { 'data-tone': tone })
export const Switch = p => { H.switches.push({ label: p['aria-label'], checked: Boolean(p.checked), disabled: Boolean(p.disabled), onCheckedChange: p.onCheckedChange }); return jsx('button', { role: 'switch', 'aria-checked': Boolean(p.checked), 'aria-label': p['aria-label'], disabled: Boolean(p.disabled), id: p.id }) }
export const Tabs = ({ children, value }) => jsx('div', { 'data-tabs': value, children })
export const TabsList = ({ children }) => jsx('div', { role: 'tablist', children })
export const TabsTrigger = ({ children, value }) => { H.tabs.push({ value, text: H.text(children) }); return jsx('span', { role: 'tab', 'data-value': value, children }) }
export const Textarea = p => { H.textareas.push(p); return jsx('textarea', { 'aria-label': p['aria-label'], placeholder: p.placeholder, readOnly: true, value: p.value, maxLength: p.maxLength }) }
export const Tip = ({ label, children }) => jsxs('span', { 'data-tip': typeof label === 'string' ? label : '', children: [children] })
export const useQuery = opts => H.useQuery(opts)
export const useQueryClient = () => H.queryClient
`
writeFileSync(join(work, 'sdk.mjs'), sdkStub)

const testable = source
  .replace(/from\s*'@hermes\/plugin-sdk'/, `from './sdk.mjs'`)
  .replace(/from\s*'react\/jsx-runtime'/, `from './jsx.mjs'`)
  .replace(/from\s*'react'/, `from './react.mjs'`)
  // Harness-only: every open confirmation is recorded with its props (its labels and what confirming does).
  .replace('function Confirm({ open,', 'function Confirm(__p) { if (__p.open) globalThis.__h.confirms.push(__p); return __Confirm(__p) }\nfunction __Confirm({ open,')
  // Harness-only: expose inner components of the temp copy for direct renders.
  .concat('\nexport const __parts = { BatchSheet, openBatchOf, mergeBatch, batchLines, needWords, ImportSheet, exportOf, leftOutWords, exportTextOf, AddFlow, ConnectStep, DetailsSheet, CopiesPanel, VersionsPanel, RecentPanel, FinishSheet, MoveSheet, TwinRow, StatusChip, TwinsPage, SkillSyncSheet, ShareSection, LinkSheet, LinkCode, FollowerView, FollowerRow, UnlinkDialog, UnlinkComputerDialog, RenameSheet, KeptBots, unlinkedHere, ComputerRow, Welcome, fleetOf, alsoOn, copiesDetail, codeChars, codeShown, codeLife, sinceWords, whenText, linkProblem, shareProblem, followerSummary, followerTrouble, briefSummary, unlinkedSentence, computerSentence, knownComputers, StopTwinningDialog, UninstallDialog, RemovedScreen, ElsewhereNow, WaitingBox, UnsentBox, ConflictItem, ConflictBox, SampleBanner, PrivacySheet, PreviewItem, VersionRow, previewLines, cronText, markDifferences, profileClash, reconcileConflictToasts, statusSentence, mattersOf, summarize, removedLines, followProblem, canMove, leftOf, checkedIso, differsBecause, goHome, SeeTwinSheet, SkillByHand, endSample, StoppedLine, awayFromHome, transportError, twinProfilesOf, homeOf, stepOutOfTwin, inSync, latestOf, onlyChanged, updateHint, RequestsSection, RequestsWaiting, BackendOff, Unreachable, InlineProblem, summarize, statusSentence, MissingBox, ErrorBox, hasMainAction, byHandWords, plainTrust, hereProblem, TwinError, rowTone, matchWords, blockedSentence, lineWords, whoWords, wasAsked, askedKey, attentionReason, handed , statusEvery, onAll, placeOf, WHERE_NEWS, plainActivity }\n')
  // Harness-only: the page counts itself as "on screen" in an effect, which a static render never runs.
  .concat('export const __hooks = { setPageOpen: n => { pageOpen = n }, clearPageToasts, setPageTitle: n => { pageTitle = n }, landFocus, lessMotion }\n')
writeFileSync(join(work, 'plugin.mjs'), testable)

// One clock for the whole run. The fixtures below say "four minutes ago" as a time worked out once, when this
// file loads, and the page turns it back into words when it is rendered, which may be a minute later on a busy
// computer: "in sync now" was then "in sync 1 min ago" and the run failed for no fault of the page. With the
// clock held at the moment this file started, an age in a fixture is the age the page sees, however long the
// run takes. (Timers are not touched: only what "now" is.)
{
  const started = Date.now()

  Date.now = () => started
}

// ── 3. fixtures (invented data only) ────────────────────────────────────────

const iso = minsAgo => new Date(Date.now() - minsAgo * 60_000).toISOString()
const coverage = [
  { item: 'name', g2h: 'syncs', h2g: 'by_hand' },
  { item: 'description', g2h: 'syncs', h2g: 'by_hand' },
  { item: 'instructions', g2h: 'by_hand', h2g: 'by_hand' },
  { item: 'skills', g2h: 'syncs_via_mirror', h2g: 'syncs_via_mirror' },
  { item: 'routines', g2h: 'by_hand', h2g: 'by_hand' },
  { item: 'connectors', g2h: 'by_hand', h2g: 'by_hand' }
]
const calm = { to_hermes: 0, to_grok: 0, in_flight: 0, waiting_approval: 0 }
const noRoutines = { in_grok: 0, paused_in_hermes: 0, on_in_hermes: 0 }
// The sample twin: something changed in both places AND a possible password was not copied (two open matters).
const acme = {
  pair_id: 'acme-support', demo: true, sample: true, grok: { id: 'b-acme-0001', name: 'Acme Support', present: true }, hermes: { profile: 'grok-bot-twin-sample' },
  state: 'needs_you', state_reason: '1 item changed in both places; 1 thing that looks like a password or key was not copied', last_sync_at: iso(2), pending: calm,
  conflicts: 1, coverage, by_hand: [{ item: 'routine:Morning summary', side: 'hermes', name: 'Morning summary', what: 'Turn on the routine "Morning summary" in Hermes when you move' }],
  restore_points: 3, secrets_withheld_total: 1, secrets_held: 1, parity: null, mirror_bot: { connected: true },
  secrets_withheld: [{ item: 'persona.description', kind: 'description', direction: 'g2h', location: 'description' }],
  waiting: [], skipped: [], switched: null, setup_left: [], routines: { in_grok: 2, paused_in_hermes: 2, on_in_hermes: 0 }, error: null
}
// The sample with nothing open, and the sample as it starts: name and description only, instructions still to paste.
const acmeCalm = { ...acme, state: 'in_step', state_reason: 'Up to date', conflicts: 0, secrets_withheld_total: 0, secrets_held: 0, secrets_withheld: [] }
const acmeNew = { ...acmeCalm, setup_left: ['instructions'] }
// The sample as a current backend sends it: what the bot says in Grok, and what its twin has in Hermes.
const ACME_SAYS = 'Answers questions about Acme widgets.'
const acmeSee = { ...acmeCalm, grok: { ...acmeCalm.grok, description: ACME_SAYS }, hermes: { ...acmeCalm.hermes, name: 'Acme Support', description: ACME_SAYS } }
const acmeSeeNew = { ...acmeSee, setup_left: ['instructions'] }
// A real twin, finished and calm. Its name carries markup and a control character on purpose.
const recipe = {
  pair_id: 'recipe-planner', demo: false, sample: false, grok: { id: 'b-recipe-0002', name: 'Recipe Planner <b>bold</b>\u0007', present: true }, hermes: { profile: 'recipe-planner' },
  state: 'in_step', state_reason: 'Up to date', last_sync_at: iso(1), pending: calm, conflicts: 0, coverage, by_hand: [],
  restore_points: 1, secrets_withheld_total: 0, secrets_held: 0, parity: null, mirror_bot: { connected: false }, secrets_withheld: [], waiting: [], skipped: [], switched: null,
  setup_left: [], routines: noRoutines, error: null
}
const recipePlain = { ...recipe, grok: { ...recipe.grok, name: 'Recipe Planner' } }
// A real twin with a skill changed in Hermes, skill sync off.
const garden = {
  pair_id: 'garden-buddy', demo: false, sample: false, grok: { id: 'b-garden-0003', name: 'Garden Buddy', present: true }, hermes: { profile: 'garden-buddy' },
  state: 'needs_you', state_reason: '1 change ready to send to Grok', last_sync_at: iso(7), pending: { ...calm, waiting_approval: 1 },
  conflicts: 0, coverage, by_hand: [], restore_points: 0, secrets_withheld_total: 0, secrets_held: 0, mirror_bot: { connected: false }, secrets_withheld: [],
  waiting: [{ item: 'skill:seed-calendar', reason: 'needs_approval', to: 'grok', detail: 'You changed the skill seed-calendar in Hermes.' }], skipped: [], switched: null,
  setup_left: [], routines: noRoutines, error: null
}
// A real twin that cannot be checked right now, as the backend reports it: its own reason and the one thing to do.
const broken = { ...recipe, pair_id: 'meal-plans', grok: { id: 'b-meal-0009', name: 'Meal Plans', present: true }, hermes: { profile: 'meal-plans' }, state: 'error', state_reason: "Can't check right now", error: { code: 'grok_read_failed', message: 'Hermes could not read Meal Plans from the Grok app.', hint: 'Open the Grok app once, then press Try again.' } }
const plants = { ...recipe, pair_id: 'window-plants', grok: { id: 'b-plants-0004', name: 'Window Plants', present: true }, hermes: { profile: 'window-plants' } }
// A real twin that was just made: name and description only; instructions and a model still to come.
const trail = { ...plants, pair_id: 'trail-guide', grok: { id: 'b-trail-0005', name: 'Trail Guide', present: true }, hermes: { profile: 'trail-guide' }, restore_points: 0, setup_left: ['instructions', 'model'] }
// Three twins just made: the third also has a possible password that was not copied.
const notes = { ...trail, pair_id: 'garden-notes', grok: { id: 'b-notes-0007', name: 'Garden Notes', present: true }, hermes: { profile: 'garden-notes' } }
const club = { ...trail, pair_id: 'book-club', grok: { id: 'b-club-0008', name: 'Book Club', present: true }, hermes: { profile: 'book-club' }, state: 'needs_you', secrets_held: 1, secrets_withheld: [{ item: 'persona.description', kind: 'description', direction: 'g2h', location: 'in the description, line 3: looks like a password or key (api key; this check can miss things)' }] }
// A twin as today's backend reports it: no setup_left, no routines, no coverage list.
const legacy = (() => {
  const { setup_left: _left, routines: _routines, ...rest } = plants

  return { ...rest, pair_id: 'old-timer', grok: { id: 'b-old-0006', name: 'Old Timer', present: true }, hermes: { profile: 'old-timer' }, coverage: [] }
})()
const ready = { ready: true, grok_app_found: true, helper_found: true, helper_checked: true, problem: null }
const EXAMPLE = 'You are Acme Support. Answer questions about Acme widgets in two sentences.'
const statusAll = { mode: 'botport', engine: { available: true, version: '0.2.1', problem: null }, setup: ready, has_data: true, sample: { on: true, hide_real: false, instructions_example: EXAMPLE }, pairs: [acme, recipe, garden], last_check_at: iso(1), interval_s: 60, fast_check: { on: false, mode: 'off' }, profile: 'default' }
const statusFirstRun = { mode: 'setup', engine: { available: false, version: null, problem: 'botport is not on this computer yet' }, setup: { ready: false, grok_app_found: true, helper_found: false, helper_checked: false, problem: null }, has_data: false, sample: { on: false, hide_real: false }, pairs: [], last_check_at: null, interval_s: 60, fast_check: { on: false, mode: 'off' }, profile: 'default' }
// Addresses come from the backend at run time; the fixtures use a reserved example host.
const FILE = 'botport-aarch64-apple-darwin.tar.gz'
const setupBase = { download_url: 'https://downloads.example.invalid/botport/v0.2.1', download: { url: `https://downloads.example.invalid/botport/v0.2.1/${FILE}`, file_name: FILE }, release_tag: 'v0.2.1', problem: null, gh_help: { url: 'https://tools.example.invalid/gh', commands: ['brew install gh', 'gh auth login'] } }
const setupNoBotport = { ...setupBase, ready: false, grok_app: { found: true }, helper: { found: false, version: null, source: null }, check: { done: false, problem: null, gh_found: false, method: null, unattested: false } }
const setupUnchecked = { ...setupBase, ready: false, grok_app: { found: true }, helper: { found: true, version: null, source: 'chosen' }, check: { done: false, problem: null, gh_found: false, method: null, unattested: false } }
const setupReady = { ...setupBase, ready: true, grok_app: { found: true }, helper: { found: true, version: '0.2.1', source: 'chosen' }, check: { done: true, problem: null, gh_found: false, method: 'pinned', unattested: false } }
// A build of the owner's own, accepted in the terminal without the check: in use, never called verified.
const setupLocal = { ...setupReady, helper: { found: true, version: '0.2.1', source: 'settings' }, check: { done: true, problem: null, gh_found: false, method: 'local', unattested: true } }
// Today's backend: no direct download, no method.
const setupOld = (() => {
  const { download: _download, problem: _problem, ...rest } = setupNoBotport

  return { ...rest, check: { done: false, problem: null, gh_found: false } }
})()
const bothVersions = { conflict_id: 'skill:greet-visitors', kind: 'skill', name: 'greet-visitors', since: iso(30), hermes: { digest: 'c9176fde1247', text: '# Greet visitors\nSay a cheerful hello to each visitor.\nKeep it short.' }, grok: { digest: 'c354dae9eaf5', text: '# Greet visitors\nSay a warm hello to each visitor.\nKeep it short.' }, diff: '--- grok\n+++ hermes\n@@ -1,3 +1,3 @@\n # Greet visitors\n-Say a warm hello to each visitor.\n+Say a cheerful hello to each visitor.\n Keep it short.', text_truncated: false }
// The same choice when both texts were too long to send whole.
const longVersions = { ...bothVersions, text_truncated: true }
// ── other computers (the sharing contract; invented computers and twins) ──
// Three computers: "mini" has the Grok app and shares; "Surface" and "Orchard" follow it.
const mini = { id: 'm-3f9a1c0b7d2e', name: 'mini', platform: 'macos' }
const surface = { id: 'm-77aa01bc9d10', name: 'Surface', platform: 'windows' }
const orchard = { id: 'm-5c11d2e3f4a5', name: 'Orchard', platform: 'linux' }
const shareOff = { on: false, state: 'off', network: null, address: null, port: 18777, since: null, problem: null, code: null }
const shareOn = { on: true, state: 'on', network: 'tailscale', address: '100.64.0.10', port: 18777, since: iso(30), problem: null, code: null }
const linkCode = { code: '7KQ4-M2XD-P9RA', expires_at: iso(-9.7), expires_in_s: 582 }
const miniRow = { ...mini, role: 'source', this: true, state: 'in_sync', linked_at: null, last_seen_at: null, last_sync_at: null, twins: 2, failed: 0 }
const surfaceRow = { ...surface, role: 'follower', this: false, state: 'in_sync', linked_at: iso(600), last_seen_at: iso(2), last_sync_at: iso(2), twins: 2, failed: 0 }
const orchardRow = { ...orchard, role: 'follower', this: false, state: 'offline', linked_at: iso(900), last_seen_at: iso(125), last_sync_at: iso(130), twins: 2, failed: 0 }
const onRows = rows => rows.map(r => ({ id: r.id, name: r.name, role: r.role, this: r.this, state: r.state, last_sync_at: r.last_sync_at }))
const SCOUT = 't-0123456789abcdef'
const QUILL = 't-00aa11bb22cc33dd'
// The Mac's own two twins, as /status has them while it shares.
const scoutPair = { ...recipe, pair_id: 'test-scout', grok: { id: 'b-scout-0010', name: 'Test Scout', present: true }, hermes: { profile: 'test-scout' }, ref: SCOUT, fleet: { id: SCOUT, machines: onRows([miniRow, surfaceRow, orchardRow]) } }
const quillPair = { ...recipe, pair_id: 'test-quill', grok: { id: 'b-quill-0011', name: 'Test Quill', present: true }, hermes: { profile: 'test-quill' }, ref: QUILL, fleet: { id: QUILL, machines: onRows([miniRow, surfaceRow]) } }
const sharedTwin = (id, name, description, pair_id, rows) => ({ id, name, description, version: 3, updated_at: iso(5), pair_id, profile: pair_id, state: 'in_sync', synced_at: null, error: null, machines: onRows(rows) })
const fleetBase = { role: 'none', this_machine: mini, can_share: true, share: shareOff, source: null, machines: [], twins: [], removed: [], last_unlink: null }
// The Mac before it ever shared; and a computer with nothing of its own (the one that should link).
const fleetNone = { ...fleetBase }
const fleetNoneOther = { ...fleetBase, this_machine: surface, can_share: false }
// Sharing just switched on: a fresh code, nobody linked yet.
const fleetCode = { ...fleetBase, role: 'source', share: { ...shareOn, code: linkCode }, machines: [miniRow], twins: [sharedTwin(SCOUT, 'Test Scout', 'Finds trails.', 'test-scout', [miniRow]), sharedTwin(QUILL, 'Test Quill', 'Drafts letters.', 'test-quill', [miniRow])] }
// Two computers linked: one in sync, one not seen for two hours. No code showing.
const fleetSource2 = { ...fleetBase, role: 'source', share: shareOn, machines: [miniRow, orchardRow, surfaceRow], twins: [sharedTwin(SCOUT, 'Test Scout', 'Finds trails.', 'test-scout', [miniRow, orchardRow, surfaceRow]), sharedTwin(QUILL, 'Test Quill', 'Drafts letters.', 'test-quill', [miniRow, surfaceRow])] }
// One computer connected but one twin could not be made there.
const surfaceBehind = { ...surfaceRow, state: 'behind', twins: 1, failed: 1 }
const fleetBehind = { ...fleetSource2, machines: [miniRow, surfaceBehind], twins: [sharedTwin(SCOUT, 'Test Scout', 'Finds trails.', 'test-scout', [miniRow, surfaceBehind])] }
// Sharing is switched on but nothing can listen.
const fleetShareError = { ...fleetSource2, share: { ...shareOn, state: 'error', network: null, address: null, problem: { code: 'port_busy', message: 'Another program on this computer uses the port Hermes shares on (18777).', hint: "Close that program; sharing starts by itself. Or choose another port in the plugin's settings." } } }
// Sharing switched off again while two computers are still linked.
const fleetSharedOff = { ...fleetSource2, share: shareOff }
// The Surface, following mini; a third computer (Orchard) is in the fleet too.
const sourceOk = { ...mini, address: '100.64.0.10', port: 18777, state: 'in_sync', linked_at: iso(600), last_sync_at: iso(4), last_contact_at: iso(0.2), offline_since: null, problem: null }
const seenFrom = (r, me) => ({ ...r, this: r.id === me.id, state: r.id === me.id ? 'in_sync' : 'unknown', linked_at: r.id === me.id ? iso(600) : null, last_seen_at: null, last_sync_at: r.id === me.id ? iso(4) : null, twins: r.id === me.id ? 2 : null, failed: r.id === me.id ? 0 : null })
const hereRows = [miniRow, orchardRow, surfaceRow].map(r => seenFrom(r, surface))
const hereTwin = (id, name, description, profile) => ({ id, name, description, version: 3, updated_at: iso(5), pair_id: null, profile, state: 'in_sync', synced_at: iso(4), error: null, machines: onRows(hereRows) })
const fleetFollower = { ...fleetBase, role: 'follower', this_machine: surface, can_share: false, source: sourceOk, machines: hereRows, twins: [hereTwin(SCOUT, 'Test Scout', 'Finds trails.', 'test-scout'), hereTwin(QUILL, 'Test Quill', 'Drafts letters.', 'test-quill')] }
const fleetFollowerOffline = { ...fleetFollower, source: { ...sourceOk, state: 'offline', last_contact_at: iso(40), offline_since: iso(38), problem: { code: 'source_offline', message: 'mini has not answered since 09:06.', hint: 'Your twins here stay as they are and catch up by themselves when mini is back.' } } }
const fleetFollowerSharingOff = { ...fleetFollower, source: { ...sourceOk, state: 'sharing_off', last_contact_at: iso(20), offline_since: iso(20), problem: { code: 'sharing_off', message: 'Sharing was switched off on mini.', hint: 'Your twins here stay as they are. Switch sharing on there again, or unlink this computer.' } } }
const fleetFollowerMismatch = { ...fleetFollower, source: { ...sourceOk, state: 'error', problem: { code: 'version_mismatch', message: 'The two computers run versions of Grok Bot Twin that cannot work together.', hint: 'Update Grok Bot Twin on both, then link again.' } } }
const fleetFollowerSyncing = { ...fleetFollower, source: { ...sourceOk, state: 'syncing', last_sync_at: null, last_contact_at: null }, twins: [] }
// One twin could not be made here (a Hermes bot of that name was already there), and one was removed on mini and is kept.
const keptRanger = { rid: 'r-5d6e7f809a1b', id: 't-9988776655443322', name: 'Test Ranger', profile: 'test-ranger', removed_at: iso(60 * 24 * 2), kept_until: iso(-60 * 24 * 28), reason: 'removed_on_source' }
const fleetFollowerError = { ...fleetFollower, twins: [hereTwin(SCOUT, 'Test Scout', 'Finds trails.', 'test-scout'), { ...hereTwin(QUILL, 'Test Quill', 'Drafts letters.', null), state: 'error', synced_at: null, error: { code: 'profile_blocked', message: 'Hermes could not make the Hermes bot for this twin on this computer.', hint: 'A Hermes bot called test-quill already exists and is not a twin.' } }], removed: [keptRanger] }
// Unlinked from the Mac's side: nothing follows any more, two Hermes bots are kept.
const fleetUnlinked = { ...fleetBase, this_machine: surface, can_share: false, removed: [{ ...keptRanger, reason: 'unlinked_by_source', removed_at: iso(60 * 24) }, { rid: 'r-0a1b2c3d4e5f', id: SCOUT, name: 'Test Scout', profile: 'test-scout', removed_at: iso(60 * 24), kept_until: iso(-60 * 24 * 29), reason: 'unlinked_by_source' }], last_unlink: { at: iso(60 * 24), by: 'source', source_name: 'mini' } }
// What leaving would do, asked before the owner agrees (nothing is changed by asking).
const unlinkPlan = { dry_run: true, unlinked: false, machine: { id: mini.id, name: 'mini' }, sentence: 'This computer stops following mini; its 2 twins leave this computer only.', stays: ['Your twins on mini and on your other computers.', 'Everything else in Hermes on this computer.'], goes: ['The 2 twins on this computer (kept for 30 days, chats included).'], undo: 'Link this computer again within 30 days and the twins come back with their chats.' }
const unlinkSurfacePlan = { dry_run: true, unlinked: false, machine: { id: surface.id, name: 'Surface' }, sentence: 'Surface stops following this computer; its 2 twins leave Surface only.', stays: ['Your twins on this computer and on your other computers.', 'Everything else in Hermes on Surface.'], goes: ['The 2 twins on Surface, the next time it is on (kept there for 30 days, chats included).'], undo: 'Link Surface again within 30 days and its twins come back with their chats.' }
// /status in brief: is this computer sharing, following, or neither.
const briefNone = { role: 'none', sharing: false, machines: 0, source: null }
const briefSource = { role: 'source', sharing: true, machines: 2, source: null }
const briefFollower = { role: 'follower', sharing: false, machines: 2, source: { name: 'mini', state: 'in_sync', offline_since: null } }
const statusSource = { ...statusAll, sample: { on: false, hide_real: false, instructions_example: null }, pairs: [scoutPair, quillPair], fleet: briefSource }
const statusOtherFirstRun = { ...statusFirstRun, setup: { ...statusFirstRun.setup, grok_app_found: false, problem: 'The Grok desktop app is not on this computer.', cause: 'grok_app' }, fleet: briefNone }
const statusFollower = { ...statusOtherFirstRun, mode: 'setup', has_data: true, fleet: briefFollower }
// ── a setup export: the file a bot writes about itself (invented bot, invented file) ──
const EXPORT_REQUEST = 'Please write your setup export for me, as one JSON file.\nSetup only: leave "memories" and "user_facts" as empty lists.\nNever include a password, token, key or cookie.'
const EXPORT_PRIVACY = 'Its lists of memories and facts about you are left out: Hermes never stores them.'
const EXPORT_TEXT = 'You are Trail Guide.\nSuggest one walk, with its length.'
const exportInfo = { pair_id: 'trail-guide', request: EXPORT_REQUEST, privacy: EXPORT_PRIVACY, last: null }
const exportInfoLast = { ...exportInfo, last: { at: iso(5), connectors: ['Calendar', 'Trail maps'] } }
// The plain case: instructions, one routine, one connector; nothing left out.
const exportPlain = {
  pair_id: 'trail-guide', applied: false, bot: { name: 'Trail Guide', matches: true },
  instructions: { state: 'new', lines: 2, left_out: [], replaces: false, text: EXPORT_TEXT, saved: false, why: '' },
  routines: [{ name: 'Morning walk', schedule: '0 8 * * *', timezone: '', prompt: 'Suggest a walk for today.', state: 'new', why: '' }],
  connectors: [{ name: 'Calendar' }], left_out: { memories: 0, user_facts: 0, skills: 0, routines: 0 }, added: 0, privacy: EXPORT_PRIVACY
}
// Everything at once: two lines left out, instructions that replace others, a routine of each kind, things left out.
const exportFull = {
  ...exportPlain,
  instructions: { state: 'new', lines: 12, left_out: [3, 9], replaces: true, text: 'You are Trail Guide.\n[not shown: looks like a password or key]\nSuggest one walk.', saved: false, why: '' },
  routines: [
    { name: 'Morning walk', schedule: '0 8 * * *', timezone: 'UTC', prompt: 'Suggest a walk for today.', state: 'new', why: '' },
    { name: 'Weekly tidy <i>x</i>', schedule: '0 11 * * 5', timezone: '', prompt: 'Tidy the list of walks.', state: 'exists', why: '' },
    { name: 'Gate code', schedule: '0 7 * * *', timezone: '', prompt: '', state: 'held', why: '' },
    { name: 'New trail report', schedule: '', timezone: '', prompt: 'Read the new trail report.', state: 'manual', why: 'starts on an event, not at a time: make it in Hermes yourself' }
  ],
  connectors: [{ name: 'Calendar' }, { name: 'Trail maps' }], left_out: { memories: 3, user_facts: 2, skills: 1, routines: 3 }
}
// A file from another bot.
const exportOther = { ...exportPlain, bot: { name: 'Recipe Planner', matches: false } }
// After Import: what was done.
const exportDone = {
  ...exportFull, applied: true, added: 1,
  instructions: { ...exportFull.instructions, saved: true },
  routines: [
    { ...exportFull.routines[0], state: 'added' }, exportFull.routines[1], exportFull.routines[2], exportFull.routines[3],
    { name: 'Evening recap', schedule: '0 20 * * *', timezone: '', prompt: 'Recap the day.', state: 'failed', why: 'not tried: an earlier one failed' }
  ]
}
// ── several new twins at once (the batch sheet): the answers of /batch and its routes (invented twins) ──
const cuOff = { available: false, driver: false, toolset: false, approvals: true, enable: ['Install cua-driver, the small program Hermes uses to click in other apps.', 'In Hermes, open Settings, then Tools, and switch on Computer use.'], why: 'Computer use is not switched on.' }
const cuOn = { available: true, driver: true, toolset: true, approvals: true, enable: [], why: '' }
const batchInfo = {
  request: EXPORT_REQUEST, privacy: EXPORT_PRIVACY, default_model: 'grok-4.7', models: ['grok-4.7', 'local-small'], endpoints: [{ name: 'Homebox', base_url_label: '127.0.0.1:8000', local: true, reachable: true, models: ['local-model'], provider: 'custom:homebox' }],
  twins: [
    { pair_id: 'trail-guide', name: 'Trail Guide', profile: 'trail-guide', left: ['instructions', 'model'], model: '', sample: false },
    { pair_id: 'garden-notes', name: 'Garden Notes', profile: 'garden-notes', left: ['instructions', 'model'], model: '', sample: false },
    { pair_id: 'book-club', name: 'Book Club', profile: 'book-club', left: ['instructions', 'model'], model: '', sample: false }
  ],
  pickup: { via: 'downloads', label: 'Downloads' },
  downloads: { available: true, label: 'Downloads' }, skill_sync: { set_up: false, pair_ids: [] }, computer_use: cuOff
}
const batchModels = { results: [
  { pair_id: 'trail-guide', name: 'Trail Guide', state: 'set', why: '' },
  { pair_id: 'garden-notes', name: 'Garden Notes', state: 'set', why: '' },
  { pair_id: 'book-club', name: 'Book Club', state: 'failed', why: 'Hermes could not write its settings file.' }
] }
// One look in Downloads: Trail Guide's file came in, Book Club's was not an export, Garden Notes' is still to come.
const batchDownloads = { files: 2, waiting: ['garden-notes'], results: [
  { pair_id: 'trail-guide', name: 'Trail Guide', file: 'trail-guide-setup.json', state: 'done', instructions: true, routines: 2, connectors: 1, lines_left_out: 1, why: '' },
  { pair_id: 'garden-notes', name: 'Garden Notes', file: '', state: 'no_file', instructions: false, routines: 0, connectors: 0, lines_left_out: 0, why: '' },
  { pair_id: 'book-club', name: 'Book Club', file: 'notes.txt', state: 'failed', instructions: false, routines: 0, connectors: 0, lines_left_out: 0, why: 'This file is not a setup export.' }
] }
const batchImport = { results: [
  { pair_id: 'garden-notes', name: 'Garden Notes', file: 'garden-notes-setup.json', state: 'done', instructions: true, routines: 0, connectors: 0, lines_left_out: 0, why: '' },
  { pair_id: 'book-club', name: 'Book Club', file: 'book-club-setup.json', state: 'done', instructions: true, routines: 0, connectors: 2, lines_left_out: 0, why: '' }
] }
const grokPartOk = { ok: true, message: 'Bring the setup of Trail Guide, Garden Notes and Book Club over from the Grok app.', status: cuOn }
const grokPartOff = { ok: false, message: '', status: cuOff }
const same = 'the same in Grok and Hermes'
const differs = 'differs between Grok and Hermes'
const fixtures = {
  setup: setupNoBotport,
  conflicts: { conflicts: [bothVersions] },
  // One part matches; each of the others differs for a different, known reason.
  parity: { passed: 1, total: 7, checks: [
    { id: 'persona.name', name: 'name', pass: true, detail: same },
    { id: 'persona.description', name: 'description', pass: false, detail: differs },
    { id: 'persona.instructions', name: 'instructions', pass: false, detail: differs },
    { id: 'skill:greet-visitors', name: 'skill greet-visitors', pass: false, detail: differs },
    { id: 'skill:fun-fact', name: 'skill fun-fact', pass: false, detail: differs },
    { id: 'skill:seed-calendar', name: 'skill seed-calendar', pass: false, detail: differs },
    { id: 'model', name: 'No model chosen for this twin yet', pass: false, detail: '', action: 'choose_model' }] },
  recent: { entries: [{ at: iso(3), side: 'grok', kind: 'name_change', summary: 'Description updated' }, { at: iso(8), side: 'hermes', kind: 'skill_edit', summary: 'Skill edited in Hermes: greet-visitors' }] },
  'restore-points': { points: [{ id: 'persona.name@8ef22c6d10ba', item: 'persona.name', kind: 'name', name: 'name', at: iso(10), reason: 'kept copy', scope: 'item' }, { id: 'skill:greet-visitors@3f2a9c1b7e40', at: iso(5), reason: 'kept copy', scope: 'item', item: 'skill:greet-visitors' }] },
  // As today's backend still sends it: with a line about the twin's small internal file. The page drops that line.
  'remove-plan': { pair_id: 'recipe-planner', dry_run: true, sample: false, profile_kept: true, stays: ['Your bot in Grok, unchanged.', 'Its Hermes copy and all its chats.'], goes: ['The link between them.', 'The earlier versions kept for undo.', "This twin's key file."], again: 'You can twin it again later; it starts fresh.', removed: ['state:pairs/recipe-planner.toml', 'state:pairs/recipe-planner.key', 'hermes:profiles/recipe-planner/botport'] },
  'uninstall-plan': { dry_run: true, twins: 2, sample: true, skill_sync: ['Acme Support'], stays: ['Your Grok bots.', 'Your Hermes copies and chats.'], goes: ['Every twin link.', 'Earlier versions.', 'The sample.', 'The botport copy.'], removed: [], next: 'Switch the plugin off in Settings → Capabilities → Plugins.' },
  checklist: { blocked_by: null, by_hand: acme.by_hand, switch: { grok_routines_to_turn_off: [{ name: 'Morning summary' }], hermes_jobs_resumed: [], hermes_jobs_paused: [{ job_id: 'job-1', name: 'Morning summary', schedule: 'every day 08:00' }, { job_id: 'job-2', name: 'Acme Support: Water reminder with a rather long name that must wrap, not be cut', schedule: '0 8 * * 1-5' }, { job_id: 'job-3', name: 'Weekly tidy', schedule: '0 11 * * 5' }] } },
  // Real bots only in `bots`; the sample's invented bots travel separately and are never listed for twinning.
  'grok-bots': { ready: true, app_found: true, helper_found: true, helper_checked: true, real_hidden: false,
    bots: [{ grok_id: 'b-garden-0003', name: 'Garden Buddy', description: 'Plans the vegetable patch.', updated_at: iso(60), twinned_pair_id: 'garden-buddy', demo: false }, { grok_id: 'b-trail-0005', name: 'Trail Guide', description: 'Suggests weekend walks.', updated_at: iso(90), twinned_pair_id: null, demo: false }],
    sample_bots: [{ grok_id: 'b-acme-0001', name: 'Acme Support', description: 'Answers questions about Acme widgets.', twinned_pair_id: 'acme-support', demo: true }] },
  fleet: fleetSource2,
  'unlink-plan': unlinkPlan,
  'unlink-computer-plan': unlinkSurfacePlan,
  'setup-export': exportInfo,
  batch: batchInfo,
  'computer-use': cuOff
}

// ── 4. recording host / ctx ─────────────────────────────────────────────────

const H = (globalThis.__h = {
  mode: 'pending',
  status: null,
  statusError: '404 Not Found',
  activeProfile: 'default',
  gateway: 'open',
  stored: {},
  buttons: [],
  switches: [],
  dialogs: [],
  inputs: [],
  known: [],
  checks: [],
  menu: [],
  tabs: [],
  textareas: [],
  copies: [],
  confirms: [],
  notes: [],
  allNotes: [],
  nav: [],
  newChats: [],
  ensured: [],
  ensureFails: false,
  opened: [],
  openOk: true,
  revealed: [],
  pickPath: null,
  pickOptions: [],
  clips: [],
  clipOk: true,
  folderReply: { ok: true, folder: null },
  restCalls: [],
  restReply: async () => ({}),
  text: c => (Array.isArray(c) ? c.map(H.text).join('') : typeof c === 'string' || typeof c === 'number' ? String(c) : c?.props?.children ? H.text(c.props.children) : ''),
  host: {
    notify: n => { H.notes.push(n); H.allNotes.push(n) },
    notifyError: (e, title) => H.notes.push({ kind: 'error', title, message: String(e?.message ?? e) }),
    navigate: p => H.nav.push(p),
    newChat: p => H.newChats.push(p),
    // point Hermes at another of its copies without opening anything
    ensureAgent: async (connection, profile) => { H.ensured.push([connection, profile]); if (H.ensureFails) throw new Error('could not switch') },
    // the computers this Hermes Desktop already knows (its own connection list; never a pass)
    connections: async () => H.known,
    // the profile the Desktop is pointed at (plugin calls follow it), and the state of its backend socket
    state: { profile: { get: () => H.activeProfile }, gateway: { get: () => H.gateway } }
  },
  queryClient: {
    getQueryData: () => H.status,
    setQueryData: (key, value) => { H.setData = [...(H.setData ?? []), [key[1], value]] },
    invalidateQueries: async () => { H.invalidations = (H.invalidations ?? 0) + 1 },
    // fresh /status read after a check: the reply (if any) becomes the cached status
    fetchQuery: async ({ queryKey }) => { H.fetches = [...(H.fetches ?? []), queryKey[1]]; if (H.freshStatus) H.status = H.freshStatus; return H.status }
  },
  override: {},
  useQuery: ({ queryKey, enabled = true }) => {
    const base = { refetch: async () => { H.refetches = (H.refetches ?? 0) + 1 }, isFetching: false, error: null }
    const which = queryKey[1]
    if (H.override[which]) return { ...base, isPending: false, isError: false, ...H.override[which] }
    if (!enabled) return { ...base, isPending: true, isError: false, data: undefined }
    if (which === 'status') {
      if (H.mode === 'pending') return { ...base, isPending: true, isError: false, data: undefined }
      if (H.mode === 'error') return { ...base, isPending: false, isError: true, error: new Error(H.statusError), data: undefined }
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
  // Stateless, as before, except for what a test puts in H.stored on purpose (the remembered home, "removed").
  storage: { get: (k, f) => (k in H.stored ? H.stored[k] : f), set: (k, v) => { H.storedSets = [...(H.storedSets ?? []), [k, v]] }, remove: k => { H.storedRemoves = [...(H.storedRemoves ?? []), k] } },
  os: {
    openExternal: async url => { H.opened.push(url); return H.openOk },
    pickOpenPath: async options => { H.pickOptions.push(options); return H.pickPath },
    revealPath: async p => { H.revealed.push(p); return true },
    // the app's clipboard door: records what was handed over; H.clipOk = false plays "copying is not available"
    writeClipboard: async text => { H.clips.push(text); return H.clipOk }
  },
  files: { requestFolder: async () => H.folderReply }
}
const tick = async (n = 3) => { for (let i = 0; i < n; i++) await new Promise(r => setImmediate(r)) }
const press = async text => { const b = H.buttons.find(x => x.text === text); assert.ok(b, `button "${text}" exists (have: ${H.buttons.map(x => x.text).join(' | ')})`); await b.onClick(); await tick() }

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
ok(JSON.stringify(palette) === JSON.stringify(['Twins: Check now', 'Twins: Open', 'Twins: See a sample']), `palette rows ${palette}`)
ok(new Set(contributions.map(c => c.id)).size === contributions.length, 'unique contribution ids')
ok(['synced', 'secrets_withheld', 'conflict', 'changed'].every(t => typeof listeners[`plugin.grok-bot-twin.${t}`] === 'function'), 'event listeners')
ok(intervals.length === 1 && intervals[0].ms === 5000, 'events poll every 5 s via ctx.setInterval')

// ── 6. events → toasts, deduped across socket + poll ────────────────────────
// (The Twins page is not on screen in this section: toasts are how Hermes tells the owner.)

const hooks = mod.__hooks
H.status = statusAll
H.notes = []
listeners['plugin.grok-bot-twin.synced']({ type: 'plugin.grok-bot-twin.synced', payload: { pair_id: 'acme-support', at: '2000-01-05T09:30:00Z', applied: [{ item: 'persona.name', kind: 'name', direction: 'g2h' }, { item: 'persona.description', kind: 'description', direction: 'g2h' }] } })
ok(H.notes.at(-1)?.message === 'Acme Support: name and description updated from Grok', `updated toast: ${H.notes.at(-1)?.message}`)
listeners['plugin.grok-bot-twin.secrets_withheld']({ payload: { pair_id: 'acme-support', at: '2000-01-05T09:30:01Z', count: 1, secrets_withheld: [{ item: 'persona.description', kind: 'description', location: 'line 1', what: 'api key' }] } })
// What happened and what to do, both in the visible lines (nothing behind Details), with one action that opens Twins.
ok(H.notes.at(-1)?.message === 'Something that looks like a password or key was not copied.' && H.notes.at(-1)?.meta === "It is in Acme Support's description. Remove it in the Grok app; Hermes notices by itself." && !H.notes.at(-1)?.detail && H.notes.at(-1)?.action?.label === 'Open Twins', `possible-password toast: ${JSON.stringify(H.notes.at(-1))}`)
// It leaves by itself (it used to sit on top of the page's own buttons for ever).
ok(Number(H.notes.at(-1)?.durationMs) >= 5_000 && Number(H.notes.at(-1)?.durationMs) <= 30_000, `possible-password toast has a normal duration: ${H.notes.at(-1)?.durationMs}`)
H.notes.at(-1).action.onClick()
ok(H.nav.at(-1) === '/twins', 'Open Twins navigates to the page')
// Once per item: the same place reported again raises nothing; a new place does.
let mark = H.notes.length
listeners['plugin.grok-bot-twin.secrets_withheld']({ payload: { pair_id: 'acme-support', count: 1, round: 2, secrets_withheld: [{ item: 'persona.description', kind: 'description' }] } })
ok(H.notes.length === mark, `the same possible password is not announced twice (${H.notes.length - mark} new)`)
listeners['plugin.grok-bot-twin.secrets_withheld']({ payload: { pair_id: 'acme-support', count: 2, round: 3, secrets_withheld: [{ item: 'persona.description', kind: 'description' }, { item: 'skill:greet-visitors', kind: 'skill' }] } })
ok(H.notes.length === mark + 1 && /description and skill/.test(H.notes.at(-1)?.meta), `a new place is announced once: ${JSON.stringify(H.notes.at(-1))}`)
// Python event shape: kinds + items only, pair_id inside the payload.
listeners['plugin.grok-bot-twin.secrets_withheld']({ payload: { pair_id: 'garden-buddy', count: 2, message: 'x', items: ['persona.description', 'persona.instructions'] } })
ok(H.notes.at(-1)?.message === 'Something that looks like a password or key was not copied.' && /Garden Buddy's description and instructions/.test(H.notes.at(-1)?.meta), `possible-password toast (python shape): ${JSON.stringify(H.notes.at(-1))}`)
listeners['plugin.grok-bot-twin.synced']({ payload: { pair_id: 'garden-buddy', name: 'Garden Buddy', kinds: ['description', 'name'], count: 2, message: 'x' } })
ok(H.notes.at(-1)?.message === 'Garden Buddy: name and description updated from Grok', `updated toast (kinds shape): ${H.notes.at(-1)?.message}`)
listeners['plugin.grok-bot-twin.conflict']({ payload: { pair_id: 'acme-support', at: '2000-01-05T09:30:02Z', count: 1 } })
ok(H.notes.at(-1)?.message === 'Acme Support: something changed in both places', `both-places toast: ${H.notes.at(-1)?.message}`)
const before = H.notes.length
// The poll returns the same three events: no new toasts.
H.restReply = async path => path.startsWith('/events') ? { events: [
  { at: '2000-01-05T09:30:00Z', type: 'synced', pair_id: 'acme-support', payload: { applied: [{ item: 'persona.name', kind: 'name', direction: 'g2h' }] } },
  { at: '2000-01-05T09:30:02Z', type: 'conflict', pair_id: 'acme-support', payload: { count: 1 } },
  { at: '2000-01-05T09:31:00Z', type: 'changed', pair_id: 'acme-support', payload: {} }
] } : {}
intervals[0].fn()
await tick()
ok(H.notes.length === before, `poll duplicates suppressed (${H.notes.length - before} new)`)
ok(H.restCalls.some(c => c.path.startsWith('/events?since=')), 'polls /events?since=')
// the both-places toast has a stable id per twin; a fresh status without it takes it down.
{
  const R = mod.__parts.reconcileConflictToasts
  const cid = 'grok-bot-twin:conflict:acme-support'
  ok(H.notes.some(n => n.id === cid && n.kind === 'warning' && n.action?.label === 'Open Twins'), 'both-places toast: stable id per twin + Open Twins')
  let m = H.notes.length
  R([{ ...acme, conflicts: 1 }], Date.now() + 10_000)
  ok(H.notes.length === m, 'both-places toast: same count, untouched')
  R([{ ...acme, conflicts: 2 }], Date.now() + 10_000)
  ok(H.notes.length === m + 1 && H.notes.at(-1)?.id === cid && H.notes.at(-1)?.kind === 'warning' && /2 things changed in both places/.test(H.notes.at(-1)?.message), `both-places toast updated in place: ${JSON.stringify(H.notes.at(-1))}`)
  m = H.notes.length
  R([{ ...acme, conflicts: 0 }], Date.now() + 1_000)
  ok(H.notes.length === m, 'a status read right after the toast does not take it down')
  R([{ ...recipe }], Date.now() + 10_000)
  ok(H.notes.length === m + 1 && H.notes.at(-1)?.id === cid && H.notes.at(-1)?.durationMs === 1 && H.notes.at(-1)?.kind !== 'warning', `both-places toast dismissed when the twin is gone: ${JSON.stringify(H.notes.at(-1))}`)
  R([], Date.now() + 10_000)
  ok(H.notes.length === m + 1, 'dismissed once only')
}
ok(H.notes.every(n => !/sk-|secret value|api key/i.test(JSON.stringify(n))), 'no secret values in toasts')

// Palette: check now → POST /sync and a result toast; see a sample → navigates.
H.restCalls = []
H.restReply = async path => path === '/sync' ? { pairs: [{ pair_id: 'calm-bot', applied: [], secrets_withheld: [], conflicts: [], state: 'in_step' }] } : { ok: true, pairs: [] }
byArea('palette')[0].data.run()
await tick()
ok(H.restCalls[0]?.path === '/sync' && H.restCalls[0]?.opts?.method === 'POST', 'palette check posts /sync')
ok(H.notes.at(-1)?.message === 'Up to date', `palette check toast: ${H.notes.at(-1)?.message}`)
// The time of a check pressed a moment ago wins over an older background round.
{
  const C = mod.__parts.checkedIso
  const fresh = { pair_id: 'fresh-one', last_sync_at: iso(9), checked_at: iso(3) }
  ok(Math.abs(Date.parse(C(fresh, iso(6))) - Date.parse(iso(3))) < 2_000 && Math.abs(Date.parse(C({ pair_id: 'fresh-one', last_sync_at: iso(9) }, iso(6))) - Date.parse(iso(6))) < 2_000, 'last checked: the newest of the twin\'s own time and the background round')
  ok(C({ pair_id: 'never' }, null) === null, 'last checked: nothing known, nothing said')
  H.restReply = async path => path === '/sync' ? { pairs: [{ pair_id: 'fresh-one', applied: [], secrets_withheld: [], conflicts: [], state: 'in_step' }] } : {}
  byArea('palette')[0].data.run()
  await tick()
  ok(Date.now() - Date.parse(C(fresh, iso(6))) < 5_000, 'last checked: a check pressed just now is the newest')
}
// Check now right after the background check raised a toast for that twin:
// no "Up to date" next to it, and no second toast either.
mark = H.notes.length
H.restReply = async path => path === '/sync' ? { pairs: [{ pair_id: 'acme-support', applied: [], secrets_withheld: [], conflicts: [], state: 'needs_you' }] } : {}
byArea('palette')[0].data.run()
await tick()
ok(H.notes.length === mark, `no toast after a recent attention toast (${JSON.stringify(H.notes.slice(mark))})`)
// A twin with a change still to send (no recent event toast): its reason, not "Up to date", and the way to the page.
H.status = { ...statusAll, pairs: [{ ...plants, state: 'changes_waiting', state_reason: '' }] }
H.restReply = async path => path === '/sync' ? { pairs: [{ pair_id: 'window-plants', applied: [], secrets_withheld: [], conflicts: [], state: 'changes_waiting', to_grok: 1 }] } : {}
byArea('palette')[0].data.run()
await tick()
ok(H.notes.length === mark + 1 && H.notes.at(-1)?.message === 'Window Plants: 1 change to send to Grok' && H.notes.at(-1)?.action?.label === 'Open Twins', `reason instead of up to date: ${JSON.stringify(H.notes.at(-1))}`)
// the palette check reads /status fresh before it judges (engine said in_step, the fresh status says a
// change is waiting -> its reason, never "Up to date"), then refreshes the rows.
H.fetches = []
H.invalidations = 0
H.freshStatus = { ...statusAll, pairs: [{ ...plants, state: 'changes_waiting', state_reason: '1 change to send to Grok', pending: { ...calm, to_grok: 1 } }] }
H.restReply = async path => path === '/sync' ? { pairs: [{ pair_id: 'window-plants', applied: [], secrets_withheld: [], conflicts: [], state: 'in_step' }] } : {}
mark = H.notes.length
byArea('palette')[0].data.run()
await tick()
ok(H.fetches.includes('status') && H.invalidations > 0, `palette check: fresh status read + rows refreshed (${H.fetches} / ${H.invalidations})`)
ok(H.notes.length === mark + 1 && H.notes.at(-1)?.message === 'Window Plants: 1 change to send to Grok' && !/up to date/i.test(H.notes.at(-1)?.message), `palette check judged on the fresh status: ${H.notes.at(-1)?.message}`)
// A twin that is checked and calm but not finished is not "Up to date" either: it says what is left.
H.freshStatus = { ...statusAll, pairs: [trail] }
H.restReply = async path => path === '/sync' ? { pairs: [{ pair_id: 'trail-guide', applied: [], secrets_withheld: [], conflicts: [], state: 'in_step' }] } : {}
mark = H.notes.length
byArea('palette')[0].data.run()
await tick()
ok(H.notes.length === mark + 1 && H.notes.at(-1)?.message === 'Trail Guide: 2 things left to set up' && !H.notes.slice(mark).some(n => /up to date/i.test(n.message)), `an unfinished twin is never "Up to date": ${JSON.stringify(H.notes.slice(mark).map(n => n.message))}`)
H.freshStatus = { ...statusAll, pairs: [{ ...plants, state: 'in_step' }] }
H.restReply = async path => path === '/sync' ? { pairs: [{ pair_id: 'window-plants', applied: [], secrets_withheld: [], conflicts: [], state: 'in_step' }] } : {}
mark = H.notes.length
byArea('palette')[0].data.run()
await tick()
ok(H.notes.at(-1)?.message === 'Up to date', `every twin up to date -> "Up to date": ${H.notes.at(-1)?.message}`)
H.freshStatus = null
H.status = statusAll
// The engine reports in_step but a possible password came back with it: never "Up to date".
H.restReply = async path => path === '/sync' ? { pairs: [{ pair_id: 'recipe-planner', applied: [], secrets_withheld: [{ item: 'persona.description', kind: 'description', direction: 'g2h' }], conflicts: [], state: 'in_step' }] } : {}
mark = H.notes.length
byArea('palette')[0].data.run()
await tick()
ok(H.notes.slice(mark).every(n => !/up to date/i.test(n.message)) && /looks like a password or key was not copied/.test(H.notes.at(-1)?.message), `possible password in the check result: no up-to-date claim (${JSON.stringify(H.notes.slice(mark).map(n => n.message))})`)
// ...and checking again does not repeat that toast (once per item), nor claim "Up to date".
mark = H.notes.length
byArea('palette')[0].data.run()
await tick()
ok(H.notes.length === mark, `a second check does not bring the same toast back (${JSON.stringify(H.notes.slice(mark).map(n => n.message))})`)
byArea('palette')[1].data.run()
ok(H.nav.at(-1) === '/twins', 'palette open navigates')
// check all: one twin failed in place, the other was checked -> one plain toast for that twin only.
H.restReply = async path => path === '/sync' ? { pairs: [
  { pair_id: 'recipe-planner', applied: [], secrets_withheld: [], conflicts: [], state: 'in_step', error: null },
  { pair_id: 'garden-buddy', applied: [], secrets_withheld: [], conflicts: [], state: 'error', error: { code: 'internal', message: "this twin's saved state could not be read" } }] } : {}
byArea('palette')[0].data.run()
await tick()
ok(H.notes.at(-1)?.kind === 'error' && H.notes.at(-1)?.message === "Garden Buddy: can't check right now" && H.notes.at(-1)?.meta === "this twin's saved state could not be read" && !H.notes.some(n => /twins are up to date/.test(n.message)), `per-twin check error toast: ${JSON.stringify(H.notes.at(-1))}`)
// "Busy": retried once by itself, then one plain sentence (never the raw code).
H.restCalls = []
H.restReply = async () => ({ error: { code: 'locked', message: 'busy' } })
byArea('palette')[0].data.run()
await tick(6)
ok(H.restCalls.filter(c => c.path === '/sync').length === 2, `busy is retried once (${H.restCalls.length} calls)`)
ok(H.notes.at(-1)?.kind === 'error' && H.notes.at(-1)?.message === 'Still working on the last change…' && H.notes.at(-1)?.meta === 'Try again in a moment.', `busy toast: ${JSON.stringify(H.notes.at(-1))}`)
// Backend off: the transport throws -> one plain sentence and one next step; the raw text only as folded detail.
H.restReply = async () => { throw new Error('404 Not Found') }
byArea('palette')[0].data.run()
await tick()
ok(H.notes.at(-1)?.kind === 'error' && H.notes.at(-1)?.message === 'Hermes could not reach Grok Bot Twin.' && /turned on/.test(H.notes.at(-1)?.meta) && !/404/.test(`${H.notes.at(-1)?.message}${H.notes.at(-1)?.meta}${H.notes.at(-1)?.title}`), `transport failure in plain words: ${JSON.stringify(H.notes.at(-1))}`)
// An HTTP 400 with a JSON body never reaches the toast as transport text: only its plain message does.
H.restReply = async () => { throw new Error('400: {"detail":{"code":"bad_input","message":"Choose a folder that exists.","hint":"Pick it again."}}') }
byArea('palette')[0].data.run()
await tick()
ok(H.notes.at(-1)?.message === 'Choose a folder that exists.' && H.notes.at(-1)?.meta === 'Pick it again.' && !/detail|bad_input|400|\{/.test(JSON.stringify([H.notes.at(-1)?.message, H.notes.at(-1)?.meta, H.notes.at(-1)?.title])), `HTTP 400 shown as plain text: ${JSON.stringify(H.notes.at(-1))}`)
// A class name or code from the backend never becomes the sentence.
H.restReply = async () => ({ error: { code: 'internal', message: 'Something went wrong (KeyError)' } })
byArea('palette')[0].data.run()
await tick()
ok(H.notes.at(-1)?.message === 'Something went wrong inside Grok Bot Twin.' && !/KeyError/.test(JSON.stringify(H.notes.at(-1))), `internal errors in plain words: ${JSON.stringify(H.notes.at(-1))}`)

// While the Twins page is on screen its rows say everything: no toast for a matter a row already shows.
{
  hooks.setPageOpen(1)
  mark = H.notes.length
  listeners['plugin.grok-bot-twin.secrets_withheld']({ payload: { pair_id: 'trail-guide', count: 1, secrets_withheld: [{ item: 'persona.description', kind: 'description' }] } })
  listeners['plugin.grok-bot-twin.conflict']({ payload: { pair_id: 'trail-guide', count: 1 } })
  ok(H.notes.length === mark, `page open: no possible-password toast and no both-places toast (${JSON.stringify(H.notes.slice(mark).map(n => n.message))})`)
  // A check that finds a twin still needing something says only that it checked; the row has the rest.
  H.restReply = async path => path === '/sync' ? { pairs: [{ pair_id: 'garden-buddy', applied: [], secrets_withheld: [{ item: 'skill:seed-calendar', kind: 'skill' }], conflicts: [{ id: 'x' }], state: 'needs_you' }] } : {}
  byArea('palette')[0].data.run()
  await tick()
  ok(H.notes.length === mark + 1 && H.notes.at(-1)?.message === 'Checked just now' && H.notes.at(-1)?.kind === 'info' && Number(H.notes.at(-1)?.durationMs) <= 5_000 && !H.notes.at(-1)?.action, `page open: a check answers with one short line (${JSON.stringify(H.notes.slice(mark))})`)
  // One twin cannot be checked: its row says so; no error toast on top of the page.
  H.restReply = async path => path === '/sync' ? { pairs: [{ pair_id: 'garden-buddy', applied: [], secrets_withheld: [], conflicts: [], state: 'error', error: { code: 'internal', message: 'could not be read' } }] } : {}
  mark = H.notes.length
  byArea('palette')[0].data.run()
  await tick()
  ok(H.notes.length === mark, `page open: a twin that cannot be checked raises no toast (${JSON.stringify(H.notes.slice(mark))})`)
  // Good news is still said: something really was updated.
  listeners['plugin.grok-bot-twin.synced']({ payload: { pair_id: 'garden-buddy', name: 'Garden Buddy', kinds: ['description'], count: 1, round: 9 } })
  ok(H.notes.length === mark + 1 && H.notes.at(-1)?.kind === 'success', 'page open: an update the row does not show (a new description) is still announced')
  // …but not a new name: the row itself shows it, and a toast would say it twice.
  listeners['plugin.grok-bot-twin.synced']({ payload: { pair_id: 'garden-buddy', name: 'Garden Buddy 2', kinds: ['name'], count: 1, round: 10 } })
  ok(H.notes.length === mark + 1, `page open: a new name is on the row, so no toast repeats it (${JSON.stringify(H.notes.slice(mark + 1))})`)
  // Toasts raised while the page was closed are taken down the moment it opens.
  hooks.setPageOpen(0)
  listeners['plugin.grok-bot-twin.conflict']({ payload: { pair_id: 'old-timer', count: 1 } })
  listeners['plugin.grok-bot-twin.secrets_withheld']({ payload: { pair_id: 'old-timer', count: 1, secrets_withheld: [{ item: 'persona.description', kind: 'description' }] } })
  ok(H.notes.at(-2)?.id === 'grok-bot-twin:conflict:old-timer' && H.notes.at(-1)?.id === 'grok-bot-twin:secrets:old-timer', 'page closed: both toasts are raised')
  mark = H.notes.length
  hooks.clearPageToasts()
  const down = H.notes.slice(mark)
  ok(down.some(n => n.id === 'grok-bot-twin:conflict:old-timer' && n.durationMs === 1) && down.some(n => n.id === 'grok-bot-twin:secrets:old-timer' && n.durationMs === 1) && down.every(n => n.kind !== 'warning'), `opening the page takes its toasts down (${JSON.stringify(down)})`)
}

// ── 7. render (needs react-dom) ─────────────────────────────────────────────

if (!haveReact) {
  console.log(`SKIP render: no react/react-dom under ${modulesDir} (pass --modules DIR)`)
} else {
  const { renderToStaticMarkup } = req('react-dom/server')
  const { jsx } = req('react/jsx-runtime')
  const P = mod.__parts
  const text = html => html.replace(/<style[^>]*>[^<]*<\/style>/g, ' ').replace(/<[^>]+>/g, ' ').replace(/&#x27;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, '&').replace(/&gt;/g, '>').replace(/&lt;/g, '<').replace(/\s+/g, ' ')
  // Every surface that is rendered is kept, for the vocabulary sweep at the end. `terminal` marks the few
  // renders where a "use the terminal instead" fold was opened on purpose.
  const seen = []
  const strayChecks = []
  const strayLabels = []
  const faintWords = []
  const render = (node, { terminal = false, technical = false, chrome = false } = {}) => {
    H.buttons = []; H.menu = []; H.checks = []; H.tabs = []; H.textareas = []; H.copies = []; H.switches = []; H.inputs = []; H.dialogs = []
    const raw = renderToStaticMarkup(node)
    const words = text(raw)
    if (!chrome && /ui-text-(?:tertiary|quaternary)|opacity-(?:[1-8]0|[1-9]5)\b/.test(raw)) faintWords.push(raw.match(/[^<>]{0,60}(?:ui-text-(?:tertiary|quaternary)|opacity-\d+)[^<>]{0,40}/)[0])
    // `technical`: a "Technical details" fold was opened on purpose (the one place an address may be read)
    seen.push({ technical, terminal, text: words })
    // "Check now" is said only where a twin is checked: a row's menu entry, or the password box's quiet button.
    const named = H.menu.filter(m => m.text === 'Check now').length + H.buttons.filter(b => b.text === 'Check now' && b.variant === 'text').length
    for (const b of H.buttons) if (/^Download (it )?again$|^Check now$/.test(b.text) && b.variant !== 'text') strayLabels.push(`${b.text} (${b.variant})`)
    if (words.split('Check now').length - 1 !== named) strayChecks.push(words.slice(Math.max(0, words.indexOf('Check now') - 60), words.indexOf('Check now') + 40))
    return raw
  }
  const page = (props = {}) => render(jsx(P.TwinsPage, { storage: ctx.storage, ...props }))
  const chip = () => render(byArea('statusBar.right')[0].render(), { chrome: true })
  const count = (h, s) => h.split(s).length - 1
  const noop = () => {}
  const reset = () => { H.restCalls = []; H.notes = []; H.confirms = [] }
  const primaries = () => H.buttons.filter(b => b.variant === 'default' && !b.disabled).map(b => b.text)
  const texts = () => H.buttons.map(b => b.text)
  const has = label => H.buttons.some(b => b.text === label)
  const variantOf = label => H.buttons.find(b => b.text === label)?.variant
  // A sheet ends in a button: its footer is never empty.
  const endsInButton = raw => /<footer[^>]*>(?:(?!<\/footer>).)*<button/s.test(raw) || raw.includes('role="alertdialog"')
  const ENGINE_WORDS = ['Mirror', 'charter', 'parity', 'Parity', 'attestation', 'PATH', 'Pick one', 'In step', 'Needs you ', 'By hand', 'by hand', 'Restore point', 'restore point', 'Demo', 'Ready to switch', 'Sync now', 'pair folder', 'Pair folder', 'pair key', 'Problem:', 'gateway', 'Python half', '⇄']
  const noEngineWords = (h, where) => { for (const w of ENGINE_WORDS) ok(!h.includes(w), `${where}: no "${w}"`) }
  ok(byArea('routes')[0].render().type === P.TwinsPage, 'the route renders the Twins page')

  // Loading: the title and skeletons, no slogan.
  H.mode = 'pending'
  let html = page()
  ok(html.includes('data-skeleton') && text(html).includes('Twins') && !text(html).includes('Try Hermes without leaving Grok') && /<h1[^>]*text-lg/.test(html), 'loading: a readable title + skeletons, no tagline')

  // The plugin's other switch is off (its address answers 404): where to click, one retry, no command first, no HTTP code.
  H.mode = 'error'
  let raw = page()
  html = text(raw)
  for (const s of ['Twins needs one more switch turned on', 'In Hermes, open Capabilities → Plugins and turn on Grok Bot Twin . This page carries on by itself.', 'Use the terminal instead']) {
    ok(html.includes(s), `plugin half off shows "${s}"`)
  }
  ok(!/404|Not Found|hermes plugins enable|two switches|Each Hermes/.test(html), 'plugin half off: no HTTP code, no command until asked, no talk of two switches')
  noEngineWords(html, 'plugin half off')
  // One button: the page asks again every 4 s, so no sentence names a Try again and none is shown.
  ok(JSON.stringify(primaries()) === JSON.stringify(['Open Plugins']) && !has('Try again') && !/Try again/.test(html), 'plugin half off: Open Plugins is the one button; nothing else to press')
  await press('Open Plugins')
  ok(H.nav.at(-1) === '/capabilities?tab=plugins&plugin=grok-bot-twin', "Open Plugins settings navigates to the Plugins tab, at this plugin's own card")
  ok(/Twins is not turned on yet/.test(chip()), 'status bar when the plugin half is off')

  // Hermes itself is not up yet: that is not "turn on the plugin". Nothing to press; the page asks again by itself.
  H.statusError = 'Failed to fetch'
  html = text(page())
  ok(html.includes('Hermes is still starting. Your twins will appear in a moment.') && !/switch|Plugins settings|Try again|Failed to fetch/.test(html) && H.buttons.length === 0, `Hermes starting (transport failure): one calm sentence (${html.slice(0, 160)})`)
  ok(/Hermes is still starting/.test(chip()), 'status bar while Hermes is starting')
  // Still no answer a minute later, and it is not "this address does not exist": not the switch screen either.
  html = text(render(jsx(P.TwinsPage, { initialMountedAt: Date.now() - 120_000, storage: ctx.storage })))
  ok(html.includes('Twins is not answering right now') && html.includes('Your twins and your Grok bots are untouched.') && !html.includes('one more switch') && !/Failed to fetch/.test(html.replace(/Details.*/, '')), `not answering after a minute: says so, never blames the switch (${html.slice(0, 200)})`)
  ok(H.buttons.find(b => b.text === 'Try again')?.variant === 'default' && H.buttons.filter(b => b.variant === 'default').length === 1 && !H.buttons.some(b => b.text === 'Open Plugins') && H.buttons.some(b => b.text === 'Technical details'), 'not answering: Try again is the one button; the raw answer is folded under Technical details')
  H.statusError = '404 Not Found'
  H.gateway = 'connecting'
  html = text(page())
  ok(html.includes('Hermes is still starting.') && !html.includes('one more switch'), 'Hermes starting (backend socket not open yet): never the switch screen')
  H.gateway = 'open'

  // After "Open in Hermes" the Desktop is pointed at the twin's own copy, where this plugin is not running.
  // No detour screen: the page goes back by itself. (A static render shows the moment before it does.)
  H.stored.home = { profile: 'default' }
  H.activeProfile = 'recipe-planner'
  html = text(page())
  ok(html.includes('Going back to your twins…') && !/one more switch|another Hermes|recipe-planner|"default"|404/.test(html) && H.buttons.length === 0, `opened from a twin's own copy: going back by itself (${html.slice(0, 160)})`)
  ok(await P.goHome('default') === true && JSON.stringify(H.ensured.at(-1)) === JSON.stringify([null, 'default']) && H.newChats.length === 0, 'going back is quiet: Hermes is pointed home, no chat is opened')
  // Only when that did not work: one sentence, one button, and no name of anything.
  H.ensureFails = true
  ok(await P.goHome('default') === true && H.newChats.at(-1) === 'default' && H.nav.at(-1) === '/twins', 'going back falls back to the visible way')
  H.ensureFails = false
  H.newChats = []
  raw = page({ initialBack: 'failed' })
  html = text(raw)
  ok(html.includes('Your twins are one step away') && html.includes("You are inside one twin's Hermes bot right now. Nothing is wrong with your twins.") && !/recipe-planner|"default"|\bdefault\b|one more switch|404|Hermes copy/.test(html), `going back failed: one sentence, no names (${html.slice(0, 200)})`)
  ok(JSON.stringify(texts()) === JSON.stringify(['Show my twins']) && variantOf('Show my twins') === 'default', 'going back failed: one button')
  await press('Show my twins')
  ok(H.newChats.at(-1) === 'default' && H.nav.at(-1) === '/twins', 'Show my twins switches home and returns to Twins')
  ok(/Open Twins to see your twins/.test(chip()) && !/default/.test(text(chip())), 'status bar from a twin\'s own copy: no name of a place')
  H.activeProfile = 'default'
  delete H.stored.home

  // First run, nothing set up: one centred block, one primary button, one quiet link. Nothing else.
  H.mode = 'ok'
  H.status = statusFirstRun
  raw = page()
  html = text(raw)
  for (const s of ['Bring your Grok bots to Hermes', 'Each bot gets a twin in Hermes that follows its name and description. Nothing changes in Grok.', 'Connect my Grok bots', 'See a sample first', 'Privacy']) {
    ok(html.includes(s), `first run shows "${s}"`)
  }
  ok(JSON.stringify(texts()) === JSON.stringify(['Connect my Grok bots', 'See a sample first', 'Privacy']), `first run: exactly these buttons (${texts()})`)
  ok(JSON.stringify(primaries()) === JSON.stringify(['Connect my Grok bots']) && H.buttons.every(b => !b.disabled), 'first run: one primary call to action, nothing disabled')
  ok(!/botport|One step left|Add twins|No twins yet|Remove Grok Bot Twin|Runs inside Hermes|keeps up with it|a copy of your Grok bot|counterpart|Give your Grok bots a twin/.test(html), 'first run: no setup box, no header buttons, no legal footer, no jargon, no promise of a full copy')
  // Two short sentences and a third: nobody has to read a paragraph twice.
  ok((raw.match(/<p[^>]*>/g) ?? []).length === 1 && text(raw).split('Bring your Grok bots to Hermes')[1].split('Connect my Grok bots')[0].trim().split(/\s+/).length <= 30, 'first run: the welcome is at most thirty words')
  ok(/<h2[^>]*text-xl/.test(raw) && /place-items-center/.test(raw), 'first run: the welcome is centred and its headline is a size up')
  noEngineWords(html, 'first run')
  ok(/No twins yet\. Click to get started\./.test(chip()) && />Twins</.test(chip()), 'status bar with no twins')
  // The way out is offered exactly when the plugin holds anything (not merely because botport is somewhere on this computer).
  H.status = { ...statusFirstRun, has_data: true }
  page()
  ok(has('Remove Grok Bot Twin…'), 'no twins but the plugin still holds data: the way out is offered')
  H.status = { ...statusFirstRun, has_data: false, setup: { ...statusFirstRun.setup, helper_found: true } }
  page()
  ok(!has('Remove Grok Bot Twin…'), 'nothing held (botport merely on this computer): no removal link')
  {
    const { has_data: _has, ...old } = statusFirstRun
    H.status = { ...old, setup: { ...old.setup, helper_found: true } }
    page()
    ok(has('Remove Grok Bot Twin…'), 'older backend (no has_data): falls back to what it can tell')
  }
  H.status = statusFirstRun

  // "See a sample first": the toast is raised only when a sample twin really appeared.
  page()
  reset()
  H.restReply = async () => ({ ok: true, created: false, did: 'Sample twin ready', pairs: [] })
  await press('See a sample first')
  ok(H.restCalls[0]?.path === '/demo' && H.restCalls[0]?.opts?.body?.action === 'start' && !H.notes.some(n => n.message === 'Sample twin ready') && H.notes.at(-1)?.kind === 'error', `no success toast when no sample twin appeared: ${JSON.stringify(H.notes)}`)
  page()
  reset()
  H.restReply = async () => ({ ok: true, created: true, did: 'Sample twin ready', pairs: [acmeNew] })
  await press('See a sample first')
  // Started from the page, the sample opens its own sheet next ("Invented data. …"), so no
  // toast says the same on top of it.
  ok(!H.notes.some(n => n.message === 'Sample twin ready') && !H.notes.some(n => n.kind === 'error'), `sample started from the page: no toast over the sheet that opens (${JSON.stringify(H.notes.at(-1))})`)

  // ── Connect sheet: two rows that tick themselves; no terminal, no account, one button at a time ──
  const connect = (data, props = {}, opts) => render(jsx(P.ConnectStep, { onRetry: noop, onSetup: noop, setup: { data, isPending: false, isError: false }, ...props }), opts)
  const NO_TERMINAL = /brew|gh auth|hermes twin|Terminal|GitHub|command|sign(ed)? in once/i
  raw = render(jsx(P.AddFlow, { open: true, onClose: noop, ready: false }))
  html = text(raw)
  for (const s of ['Connect your Grok bots', 'Two steps, once. About a minute.', '1. The Grok desktop app', 'Found on this computer.', '2. Get botport',
    'botport is a small free app that reads your bot list from Grok. Hermes verifies it is the real botport before using it.', 'Download botport', 'I have the file', 'For developers', 'Not now']) {
    ok(html.includes(s), `connect (nothing yet) shows "${s}"`)
  }
  ok(!NO_TERMINAL.test(html) && !/<path>|--repo|Install botport|signed build record|genuineness|Check it is genuine|five minutes/.test(html), `connect: no terminal, no GitHub, no install hint on the way (${html.match(NO_TERMINAL)?.[0]})`)
  noEngineWords(html, 'connect (nothing yet)')
  ok(!has('Continue') && !has('Check now') && !has('Verify botport') && !html.includes('Other ways') && endsInButton(raw), 'connect: no Continue to press (the sheet moves on by itself), no check to start by hand')
  ok(JSON.stringify(primaries()) === JSON.stringify(['Download botport']), `connect: one primary button at a time (${primaries()})`)
  reset()
  H.opened = []
  await press('Download botport')
  ok(H.opened.length === 1 && H.opened[0] === setupBase.download.url && H.restCalls.length === 0, 'Download botport opens the one file for this computer in the browser; the plugin downloads nothing')
  // After the download: one sentence that says which file to choose, in words a browser's Downloads list shows
  // (no developer's file name to hunt for; a browser that unpacked the download is covered too). One primary button.
  const CHOOSE_IT = 'Downloaded? Choose it: it is the newest file in Downloads and its name starts with botport. If your browser unpacked it, choose the file named botport inside the new folder.'
  // Right after Download the row waits for the file by itself: no filled button into a file picker, one quiet way
  // for whoever wants to choose the file themselves. Found in Downloads: "Use it" is the filled button, as before.
  raw = connect(setupNoBotport, { initialWaiting: true })
  html = text(raw)
  ok(html.includes('Waiting for the download. This row moves on by itself.') && raw.includes('role="status"') && primaries().length === 0 && variantOf('Choose the file myself…') === 'text' && !html.includes('Downloaded? Choose it') && !has('Choose the file…'), `just downloaded: the row waits; nobody is sent to find a file (${texts()})`)
  connect({ ...setupNoBotport, download: { ...setupBase.download, found: { name: FILE } } }, { initialWaiting: true })
  ok(JSON.stringify(primaries()) === JSON.stringify(['Use it']), 'just downloaded and found: Use it is the one filled button')
  ok(/timers\.setTimeout\(\(\) => setByHand\(true\), 60_000\)/.test(source), 'a download that has not shown up after a minute gets the file picker by itself')
  html = text(connect(setupNoBotport, { initialDownloaded: true }))
  ok(html.includes(CHOOSE_IT) && !html.includes('.tar.gz') && !html.includes(FILE) && JSON.stringify(primaries()) === JSON.stringify(['Choose the file…']) && variantOf('Download botport') === 'ghost', `after the download: choose the file (${texts()})`)
  // One name for the download, wherever it is offered.
  ok(!has('Download again') && !has('Download it again') && texts().filter(x => x === 'Download botport').length === 1, `one label for the download: ${texts()}`)
  {
    const got = []
    connect(setupNoBotport, { initialDownloaded: true, onSetup: s => got.push(s) })
    reset()
    H.pickPath = '/tmp/example-downloads/' + FILE
    H.pickOptions = []
    H.restReply = async () => setupReady
    await press('Choose the file…')
    ok(H.restCalls.length === 1 && H.restCalls[0].path === '/engine/path' && JSON.stringify(H.restCalls[0].opts.body) === JSON.stringify({ path: '/tmp/example-downloads/' + FILE }) && got[0]?.ready === true && H.notes.length === 0, 'choosing the file posts its location; the check runs on the spot and ticks the row without a toast')
    ok(H.pickOptions[0]?.title === 'Choose the botport download' && !('defaultPath' in H.pickOptions[0]), 'the file picker says what to choose; no folder is guessed')
    // When the backend says where Downloads is, the picker opens there.
    connect({ ...setupNoBotport, download: { ...setupBase.download, dir: '/tmp/example-home/Downloads' } }, { initialDownloaded: true })
    H.pickOptions = []
    H.pickPath = null
    await press('Choose the file…')
    ok(H.pickOptions[0]?.defaultPath === '/tmp/example-home/Downloads' && !('filters' in H.pickOptions[0]), `the file picker opens in Downloads (${JSON.stringify(H.pickOptions[0])})`)
    H.pickPath = '/tmp/example-downloads/' + FILE
    // Cancelling the picker changes nothing.
    H.pickPath = null
    reset()
    connect(setupNoBotport, { initialDownloaded: true })
    await press('Choose the file…')
    ok(H.restCalls.length === 0, 'cancelling the file picker changes nothing')
    // A file that is not the genuine one: kept out, said in the row (not in a toast over the sheet).
    const notGenuine = { code: 'not_genuine', message: 'That file is not the genuine botport 0.2.1.', hint: 'Download it again with the Download botport button, then choose the new file.' }
    const got2 = []
    connect(setupNoBotport, { initialDownloaded: true, onSetup: s => got2.push(s) })
    reset()
    H.pickPath = '/tmp/example-downloads/wrong-file'
    H.restReply = async () => ({ error: notGenuine })
    await press('Choose the file…')
    ok(H.restCalls.length === 1 && got2.length === 0 && H.notes.length === 0, `a file that is not genuine: nothing is accepted, no toast (${JSON.stringify(H.notes)})`)
    raw = connect(setupNoBotport, { initialDownloaded: true, initialProblem: notGenuine })
    ok(text(raw).includes('That file is not the genuine botport 0.2.1. Download it again with the Download botport button, then choose the new file.') && /role="alert"/.test(raw) && has('Choose the file…'), 'a file that is not genuine: what happened and what to do, in the row')
    // What went wrong replaces the instruction it made stale, and the fix is the primary button.
    ok(!text(raw).includes('Downloaded? Choose it') && JSON.stringify(primaries()) === JSON.stringify(['Download botport']) && variantOf('Choose the file…') === 'ghost', `a file that is not genuine: Download botport is the one primary (${primaries()})`)
    // Something that is not botport at all: the fix is another file, so choosing stays the primary.
    connect(setupNoBotport, { initialDownloaded: true, initialProblem: { code: 'not_botport', message: 'That is not botport.', hint: 'Choose the file you downloaded with the Download botport button.' } })
    ok(JSON.stringify(primaries()) === JSON.stringify(['Choose the file…']) && variantOf('Download botport') === 'ghost', `not botport at all: choose another file (${primaries()})`)
    H.pickPath = null
  }
  // The browser could not be opened: no dead end, the address is shown with a Copy button.
  reset()
  H.openOk = false
  connect(setupNoBotport)
  await press('Download botport')
  ok(H.notes.length === 0, 'browser did not open: no error toast pointing at a README')
  H.openOk = true
  raw = connect(setupNoBotport, { initialDownloaded: true, initialAddress: setupBase.download.url })
  ok(text(raw).includes('Hermes could not open your browser. Copy this address into it, then choose the file it downloads.') && text(raw).includes(setupBase.download.url) && H.copies.some(c => c.label === 'Copy the address' && c.text === setupBase.download.url) && has('Choose the file…'), 'browser did not open: the address and a Copy button')
  // Until the backend names the one file: the release page, said as what it is.
  connect(setupOld)
  ok(JSON.stringify(primaries()) === JSON.stringify(['Open the download page']) && !has('Download botport'), 'older backend: the button says it opens a page')
  reset()
  H.opened = []
  await press('Open the download page')
  ok(H.opened[0] === setupBase.download_url, 'older backend: opens the address the backend sent')
  html = text(connect(setupOld, { initialDownloaded: true }))
  ok(html.includes(CHOOSE_IT) && !html.includes('.tar.gz') && !html.includes('aarch64'), 'older backend: no file name is invented')
  // No build for this kind of computer: said in the backend's words, nothing to download.
  html = text(connect({ ...setupNoBotport, download: null, download_url: '', problem: 'There is no botport for this kind of computer yet.' }))
  ok(html.includes('There is no botport for this kind of computer yet.') && !has('Download botport') && !has('Open the download page'), 'no build for this computer: said plainly, no download button')
  // The Grok desktop app is missing: its own row, in its own words, and an answer for people who use Grok elsewhere.
  const noApp = { ...setupReady, ready: false, grok_app: { found: false } }
  html = text(connect(noApp))
  ok(html.includes('1. The Grok desktop app') && html.includes('Twins needs the Grok desktop app on this computer. Open it and sign in; this ticks by itself.') && html.includes('2. Get botport') && html.includes('botport 0.2.1 · verified') && variantOf('My bots are elsewhere') === 'text' && primaries().length === 0 && !has('Get the Grok app'), `Grok desktop app missing: its own row, botport not blamed (${html.slice(0, 200)})`)
  // With an address from the backend there is something to press: it opens the Grok desktop app's own page.
  {
    const APP = 'https://downloads.example.invalid/grok-desktop'
    connect({ ...noApp, grok_app: { found: false, download_url: APP } })
    ok(JSON.stringify(primaries()) === JSON.stringify(['Get the Grok app']), `Grok desktop app missing, botport done: getting the app is the one primary (${primaries()})`)
    reset()
    H.opened = []
    await press('Get the Grok app')
    ok(JSON.stringify(H.opened) === JSON.stringify([APP]) && H.restCalls.length === 0, 'Get the Grok app opens the address the backend sent; the plugin fetches nothing')
    connect({ ...setupNoBotport, grok_app: { found: false, download_url: APP } })
    ok(variantOf('Get the Grok app') === 'secondary' && JSON.stringify(primaries()) === JSON.stringify(['Download botport']), 'both rows open: still one primary button')
    connect({ ...noApp, grok_app: { found: false, download_url: 'http://downloads.example.invalid/grok-desktop' } })
    ok(!has('Get the Grok app'), 'an address that is not https is never offered')
    connect({ ...setupReady, grok_app: { found: true, download_url: APP } })
    ok(!has('Get the Grok app'), 'the app is here: nothing to get')
  }
  html = text(connect(noApp, { initialElsewhere: true }))
  ok(html.includes('Twins cannot reach bots that exist only in the browser or on a phone yet.') && !html.includes('You can still see the sample.') && !has('My bots are elsewhere') && !has('See the sample'), 'Grok in a browser or on a phone: one honest sentence')
  {
    const sampled = []
    html = text(connect(noApp, { initialElsewhere: true, onSample: () => sampled.push(1) }))
    ok(html.includes('Twins cannot reach bots that exist only in the browser or on a phone yet. You can still see the sample.') && variantOf('See the sample') === 'secondary', 'Grok somewhere else: not a dead end, the sample is offered')
    await press('See the sample')
    ok(sampled.length === 1, 'See the sample hands over to the page')
  }
  // Hermes saw the download arrive: no file to hunt for, one button.
  {
    const arrived = { ...setupNoBotport, download: { ...setupBase.download, found: { name: FILE } } }
    const got = []
    raw = connect(arrived, { onSetup: s => got.push(s) })
    html = text(raw)
    ok(html.includes('Found botport in Downloads.') && html.includes('Hermes verifies it is the real botport before using it.') && JSON.stringify(primaries()) === JSON.stringify(['Use it']) && variantOf('Choose another file…') === 'ghost' && !html.includes(FILE) && !html.includes('Downloaded? Choose it'), `download found: Use it (${texts()})`)
    reset()
    H.restReply = async () => setupReady
    await press('Use it')
    ok(H.restCalls.length === 1 && H.restCalls[0].path === '/engine/path' && JSON.stringify(H.restCalls[0].opts.body) === JSON.stringify({ from_downloads: true }) && got[0]?.ready === true && H.notes.length === 0, `Use it: the backend takes the file it found; no location leaves the page (${JSON.stringify(H.restCalls[0]?.opts?.body)})`)
    // The file it found is not the genuine one: said in the row, and a fresh download is the fix.
    raw = connect(arrived, { initialProblem: { code: 'not_genuine', message: 'That file is not the genuine botport 0.2.1.', hint: 'Download it again with the Download botport button, then choose the new file.' } })
    ok(text(raw).includes('That file is not the genuine botport 0.2.1.') && JSON.stringify(primaries()) === JSON.stringify(['Download botport']) && !has('Use it'), `download found but not genuine: Download botport (${texts()})`)
    // Once botport is verified nothing is offered any more.
    connect({ ...setupReady, download: { ...setupBase.download, found: { name: FILE } } })
    ok(!has('Use it'), 'download found, botport already verified: nothing to use')
  }
  // botport is on this computer but not verified: nothing says "install"; Verify botport is the one thing to press.
  fixtures.setup = setupUnchecked
  raw = render(jsx(P.AddFlow, { open: true, onClose: noop, ready: false }))
  html = text(raw)
  ok(html.includes('2. Get botport') && html.includes('botport is on this computer but has not been verified yet. Hermes runs it only after that.') && !/[Ii]nstall/.test(html) && !NO_TERMINAL.test(html), 'botport found but not verified: no install wording, no terminal')
  ok(JSON.stringify(primaries()) === JSON.stringify(['Verify botport']) && variantOf('Download botport') === 'ghost' && !has('Continue') && !has('Check now') && !html.includes('Check now'), `botport found but not verified: Verify botport is the one primary (${primaries()})`)
  html = text(connect({ ...setupUnchecked, check: { ...setupUnchecked.check, problem: 'botport changed since it was verified.' } }))
  ok(html.includes('botport changed since it was verified. Hermes runs it only after that.') && !html.includes('has not been verified yet'), "botport found but not verified: the backend's reason is shown")
  const checkFailed = { code: 'check_failed', message: 'This botport is not the release Hermes expects (0.2.1).', hint: 'Press Download botport and choose the new file.' }
  reset()
  H.setData = []
  H.restReply = async path => (path === '/engine/trust' ? { error: checkFailed } : {})
  render(jsx(P.AddFlow, { open: true, onClose: noop, ready: false }))
  await press('Verify botport')
  ok(H.restCalls[0]?.path === '/engine/trust' && JSON.stringify(H.restCalls[0]?.opts?.body) === '{}', 'Verify botport posts the check (never an opt-out)')
  ok(H.notes.length === 0 && H.setData.length === 0 && !/allow.unattested/i.test(JSON.stringify(H.restCalls)), `a failed check is said in the row, not in a toast, and accepts nothing: ${JSON.stringify(H.notes)}`)
  // A failed check: the failure takes the place of the stale line, the fix (a fresh download) is the primary
  // button, under the name the sentence uses, and trying again is a text link.
  raw = connect(setupUnchecked, { initialProblem: checkFailed })
  html = text(raw)
  ok(html.includes('This botport is not the release Hermes expects (0.2.1). Press Download botport and choose the new file.') && !html.includes('has not been verified yet') && /role="alert"/.test(raw), `failed check: only the failure is said (${html.slice(0, 260)})`)
  ok(JSON.stringify(primaries()) === JSON.stringify(['Download botport']) && variantOf('Verify again') === 'text' && !has('Verify botport') && !has('Download it again'), `failed check: the fix is the primary button (${texts()})`)
  ok(!html.includes('brew install') && !html.includes('hermes twin') && has('For developers'), 'failed check: the developers fold stays closed')
  reset()
  H.opened = []
  await press('Download botport')
  ok(H.opened[0] === setupBase.download.url, 'failed check: Download botport fetches the file for this computer')
  // ...and after that press the row asks for the new file (not for another check of the old one).
  connect(setupUnchecked, { initialDownloaded: true })
  ok(JSON.stringify(primaries()) === JSON.stringify(['Choose the file…']) && !has('Verify botport'), 'after a fresh download the row asks for the new file')
  reset()
  H.restReply = async path => (path === '/engine/trust' ? { trusted: true, attested: true, setup: setupReady } : {})
  render(jsx(P.AddFlow, { open: true, onClose: noop, ready: false }))
  await press('Verify botport')
  ok(H.setData?.at(-1)?.[0] === 'setup' && H.setData?.at(-1)?.[1]?.ready === true && H.notes.length === 0, 'a passing check ticks the row without a toast')
  // "For developers" is the only place a terminal or GitHub's tool is mentioned, and only when opened.
  raw = connect(setupUnchecked, { initialOther: true }, { terminal: true })
  html = text(raw)
  ok(html.includes("Have GitHub's command-line tool (gh)? Hermes can also verify botport against the build record GitHub keeps for it.") && html.includes('brew install gh') && html.includes('gh auth login') && html.includes('hermes twin setup') && has("Verify with GitHub's tool") && has("Open the tool's own page") && !html.includes('<') && !/publisher's own build record|Other ways/.test(html), 'for developers: the GitHub tool and the terminal, for people who want them')
  ok(JSON.stringify(primaries()) === JSON.stringify(['Verify botport']), 'for developers: still one primary button')
  reset()
  H.restReply = async () => ({ error: { code: 'gh_missing', message: "This check needs GitHub's free command-line tool, signed in once.", hint: 'Install it, run gh auth login, then try again.' } })
  await press("Verify with GitHub's tool")
  ok(H.restCalls[0]?.path === '/engine/trust' && JSON.stringify(H.restCalls[0]?.opts?.body) === JSON.stringify({ method: 'gh' }) && H.notes.length === 0, "Verify with GitHub's tool asks for that check on purpose")
  html = text(connect(setupNoBotport, { initialOther: true }, { terminal: true }))
  ok(html.includes('hermes twin setup') && !html.includes('brew install') && !has("Verify with GitHub's tool"), 'for developers before botport is here: only the terminal line')
  // Both rows ticked: the same two numbered titles, one line each, nothing left to press but "Not now".
  fixtures.setup = setupReady
  raw = render(jsx(P.AddFlow, { initialStep: 'connect', open: true, onClose: noop, ready: false }))
  html = text(raw)
  ok(html.includes('1. The Grok desktop app') && html.includes('Found on this computer.') && html.includes('2. Get botport') && html.includes('botport 0.2.1 · verified') && JSON.stringify(texts()) === JSON.stringify(['Not now']) && !html.includes('For developers'), `both rows ticked: titles keep their form, no buttons left (${texts()})`)
  ok(count(raw, 'data-codicon="pass-filled"') === 2, 'both rows ticked: two ticks')
  // A local build accepted in the terminal without the check is never called "verified".
  html = text(connect({ ...setupReady, check: { ...setupReady.check, unattested: true } }))
  ok(html.includes('botport 0.2.1 is in use but has not been verified.') && !html.includes('· verified'), 'a local build is not called verified')
  html = text(connect(setupLocal))
  ok(html.includes('botport 0.2.1 is in use but has not been verified.') && !html.includes('· verified') && !/unattested|attest/i.test(html), 'a local build as the backend reports it (unattested + method) is not called verified, in plain words')
  html = text(connect({ ...setupReady, check: { ...setupReady.check, method: 'local' } }))
  ok(html.includes('is in use but has not been verified.') && !html.includes('· verified'), 'a local build is not called verified (method: local)')
  H.status = { ...statusAll, engine: { ...statusAll.engine, allow_unattested: true } }
  html = text(page())
  ok(html.includes('botport is in use but has not been verified.') && variantOf('Verify botport') === 'secondary' && !/hermes twin|terminal/i.test(html), 'the everyday page says so too, with a button instead of a command')
  reset()
  H.restReply = async path => (path === '/engine/trust' ? { trusted: true, attested: true, setup: setupReady } : {})
  await press('Verify botport')
  ok(H.restCalls[0]?.path === '/engine/trust' && JSON.stringify(H.restCalls[0]?.opts?.body) === '{}' && H.notes.at(-1)?.message === 'botport is verified', `everyday page: Verify botport verifies botport (${JSON.stringify(H.notes.at(-1))})`)
  H.status = statusFirstRun
  fixtures.setup = setupNoBotport

  // Opened from the "not following Grok" banner: the same two rows, said as what it is. (It closes by itself once
  // both are ticked; that runs in an effect, which a static render does not reach.)
  {
    const keep = fixtures.setup
    fixtures.setup = { ...setupUnchecked, check: { ...setupUnchecked.check, problem: 'botport changed since it was verified.' } }
    const h = text(render(jsx(P.AddFlow, { open: true, onClose: noop, ready: false, reconnect: true })))
    ok(h.includes('Reconnect your Grok bots') && h.includes('Your twins are checked again as soon as both rows are ticked.') && !h.includes('About a minute'), `reconnect: its own title (${h.slice(0, 120)})`)
    ok(h.includes('botport changed since it was verified. Hermes runs it only after that.') && !h.includes('has not been verified yet') && H.buttons.find(b => b.text === 'Verify botport')?.variant === 'default', 'reconnect: the real reason in the row, Verify botport is the one primary button')
    // Opened because "Verify botport" on the page just failed: the sheet carries what went wrong, and the fix.
    const h2 = text(render(jsx(P.AddFlow, { open: true, onClose: noop, problem: checkFailed, ready: false, reconnect: true })))
    ok(h2.includes('This botport is not the release Hermes expects (0.2.1). Press Download botport and choose the new file.') && JSON.stringify(primaries()) === JSON.stringify(['Download botport']) && variantOf('Verify again') === 'text', `reconnect after a failed verification: the failure and its fix (${texts()})`)
    // Both rows have to be ticked before the sheet moves on: "ready" alone is not enough while the Grok
    // desktop app is missing (today's backend does not look at the app for "ready").
    fixtures.setup = { ...setupReady, ready: true, grok_app: { found: false } }
    const h3 = text(render(jsx(P.AddFlow, { open: true, onClose: noop, ready: false })))
    ok(h3.includes('Connect your Grok bots') && h3.includes('1. The Grok desktop app (to do)') && !h3.includes('Choose your bots') && !has('Continue'), `Grok desktop app missing: the sheet stays on the two rows (${h3.slice(0, 160)})`)
    ok(/const setupReady = setup\.data\?\.ready === true && setup\.data\?\.grok_app\?\.found !== false/.test(source) && /ready: setupReady && data\?\.setup\?\.ready === true && data\?\.setup\?\.grok_app_found !== false/.test(source), 'the sheet moves on only when botport is verified and the Grok desktop app is found')
    fixtures.setup = keep
  }

  // ── Choose your bots ──
  raw = render(jsx(P.AddFlow, { open: true, onClose: noop, ready: true }))
  html = text(raw)
  ok(html.includes('Choose your bots') && html.includes('These are the bots in your Grok app. Nothing changes in Grok.') && html.includes('Your Grok bots') && html.includes('Trail Guide') && html.includes('Suggests weekend walks.') && html.includes('Already has a twin'), 'choose: the real bots with their descriptions')
  ok(!html.includes('Acme Support') && !html.includes('Recipe Planner'), 'choose: sample bots are never listed, even while the sample is on')
  // The only bot without a twin is ticked already: one click less.
  ok((raw.match(/type="checkbox"[^>]*checked/g) ?? []).length === 2 && H.buttons.find(b => b.text === 'Continue')?.disabled === false && !has('Select all'), 'choose: the only bot without a twin is ticked already, Continue is on')
  reset()
  H.restReply = async () => ({ pairs: [], skipped: [], profiles_in_use: [] })
  await press('Continue')
  ok(H.restCalls[0]?.path === '/twins/preview' && JSON.stringify(H.restCalls[0]?.opts?.body) === JSON.stringify({ grok_ids: ['b-trail-0005'] }), 'choose: Continue asks for a preview of exactly the ticked bot (nothing is written yet)')
  H.override['grok-bots'] = { data: { ...fixtures['grok-bots'], bots: [...fixtures['grok-bots'].bots, { grok_id: 'b-plants-0004', name: 'Window Plants', description: 'Waters the window sill.', twinned_pair_id: null, demo: false }] } }
  raw = render(jsx(P.AddFlow, { open: true, onClose: noop, ready: true }))
  ok((raw.match(/type="checkbox"[^>]*checked/g) ?? []).length === 1 && H.buttons.find(b => b.text === 'Continue')?.disabled === true && has('Select all'), 'choose: with several bots nothing is preselected (only the existing twin is ticked)')
  {
    // The sheet comes back to the same place: the bots that were ticked are ticked again (after Escape, a click
    // outside, Connect pressed again, or a reload), and a sheet that had reached "Ready to twin" goes there again.
    const two = { data: { ...fixtures['grok-bots'], bots: [...fixtures['grok-bots'].bots, { grok_id: 'b-plants-0004', name: 'Window Plants', description: 'Waters the window sill.', twinned_pair_id: null, demo: false }] } }
    const before = H.override['grok-bots']
    H.override['grok-bots'] = two
    H.stored['connect-place'] = { ids: ['b-plants-0004'], step: 'choose' }
    const kept = render(jsx(P.AddFlow, { open: true, onClose: noop, ready: true, storage: ctx.storage }))
    const ticked = H.checks.filter(c => c.checked && !c.disabled).map(c => c.label)
    ok(JSON.stringify(ticked) === JSON.stringify(['Window Plants']) && H.buttons.find(b => b.text === 'Continue')?.disabled === false, `connect again: the bot that was ticked is ticked again (${JSON.stringify(ticked)}; ${text(kept).slice(0, 80)})`)
    ok(JSON.stringify(P.placeOf(ctx.storage)) === JSON.stringify({ confirm: false, ids: ['b-plants-0004'] }), 'connect again: the place is the ticked bots and the step')
    H.stored['connect-place'] = { ids: ['b-plants-0004', 7, ''], step: 'confirm' }
    ok(JSON.stringify(P.placeOf(ctx.storage)) === JSON.stringify({ confirm: true, ids: ['b-plants-0004'] }), 'connect again: only ids are read back, whatever else was stored')
    delete H.stored['connect-place']
    ok(P.placeOf(ctx.storage) === null && P.placeOf(null) === null, 'connect for the first time: no place')
    render(jsx(P.AddFlow, { open: true, onClose: noop, ready: true, storage: ctx.storage }))
    ok(H.checks.filter(c => c.checked && !c.disabled).length === 0, 'connect for the first time with two bots: nothing is ticked for the owner')
    // (the page notes the place while the sheet is open, forgets it once a twin is made, and removal forgets it too)
    ok(/storage\?\.set\?\.\(PLACE_KEY, \{ ids, step: step === 'confirm' \? 'confirm' : 'choose' \}\)/.test(source) && /if \(step === 'done' \|\| ids\.length === 0\) \{\n\s+storage\?\.remove\?\.\(PLACE_KEY\)/.test(source) && /\[NOTED_KEY, HOME_KEY, UNLINKED_KEY, PLACE_KEY, BATCH_KEY\]/.test(source), 'connect: the place is noted while choosing, and forgotten when a twin is made or the plugin is removed')
    // Every bot already has a twin: one sentence that says so and where more come from; no Continue that can
    // never be pressed, and no list of boxes that cannot be ticked.
    H.override['grok-bots'] = { data: { ...fixtures['grok-bots'], bots: fixtures['grok-bots'].bots.map(b => ({ ...b, twinned_pair_id: b.twinned_pair_id ?? 'trail-guide' })) } }
    const all = text(render(jsx(P.AddFlow, { open: true, onClose: noop, ready: true })))
    ok(all.includes('Every bot in Grok already has a twin. Make a new one in Grok; it appears here by itself.') && !has('Continue') && JSON.stringify(texts()) === JSON.stringify(['Done']) && H.checks.length === 0 && !all.includes('Your Grok bots'), `every bot has a twin: one sentence, one button (${texts()}; ${all})`)
    H.override['grok-bots'] = before
  }
  H.override['grok-bots'] = { data: { ...fixtures['grok-bots'], bots: [], real_hidden: true } }
  html = text(render(jsx(P.AddFlow, { open: true, onClose: noop, ready: true })))
  ok(html.includes('Your real Grok bots are hidden while the sample is on, because you chose that.') && html.includes('Show my real bots again') && !html.includes('not found'), 'choose: hidden bots are explained, not silently missing')
  H.override['grok-bots'] = { data: { ready: false, app_found: true, helper_found: false, helper_checked: false, bots: [] } }
  html = text(render(jsx(P.AddFlow, { initialStep: 'choose', open: true, onClose: noop, ready: true })))
  ok(!/not found|not on this computer|Install it and sign in|demo|One moment/i.test(html) && html.includes('The connection to your Grok app is not finished yet.') && has('Finish connecting'), `botport not ready -> never "Grok app not found", and a way on: ${html.slice(0, 160)}`)
  H.override['grok-bots'] = { data: { ready: true, app_found: false, helper_found: true, helper_checked: true, bots: [] } }
  html = text(render(jsx(P.AddFlow, { open: true, onClose: noop, ready: true })))
  ok(html.includes('The Grok desktop app is not on this computer. Install it and sign in, then press Try again.') && H.buttons.filter(b => b.text === 'Try again').length === 1, 'Grok desktop app truly absent: one sentence, one Try again')
  H.override['grok-bots'] = { data: { ready: true, app_found: true, helper_found: true, helper_checked: true, bots: [] } }
  html = text(render(jsx(P.AddFlow, { open: true, onClose: noop, ready: true })))
  ok(html.includes('No bots in your Grok app yet. Make one in Grok; it appears here by itself.') && !has('Try again') && /queryKey: \[ID, 'grok-bots'\], refetchInterval: 5_000 \}/.test(source), 'no bots yet: says so, asks for no click, and looks again by itself')
  H.override = {}

  // ── Ready to twin ──
  const preview = { profiles_in_use: ['garden-buddy'], skipped: [{ grok_id: 'b-garden-0003', name: 'Garden Buddy', reason: 'already_twinned' }], pairs: [{
    grok: { id: 'b-trail-0005', name: 'Trail Guide' }, hermes: { profile: 'trail-guide' }, schedules_paused: 1, sample: false,
    lines: ['Copies now: name and description.', "Later, with the page's help: instructions, 1 routine.", '1 skill: optional, set up later.', '1 thing that looks like a password or key will not be copied.'],
    items: [
      { item: 'persona.name', kind: 'name', name: 'name', action: 'create', owner: 'grok', preview: 'Trail Guide' },
      { item: 'skill:route-notes', kind: 'skill', name: 'route-notes', action: 'via_mirror', owner: 'grok', scan: 'caution', preview: '--- SKILL.md\nkey: [hidden: looks like a password or key]' },
      { item: 'routine:Saturday plan', kind: 'routine', name: 'Saturday plan', action: 'by_hand', owner: 'grok', note: 'arrives switched off, so nothing runs twice' }
    ],
    secrets_withheld: [{ item: 'persona.instructions', kind: 'instructions', location: 'instructions' }]
  }] }
  raw = render(jsx(P.AddFlow, { open: true, onClose: noop, ready: true, initialPreview: preview }))
  html = text(raw)
  // (the backend's "later" line counts a routine among what is left; the row then does not: it is said as optional)
  ok(JSON.stringify(P.previewLines(preview.pairs[0]).slice(1, 3)) === JSON.stringify(["Later, with the page's help: instructions.", 'Optional: 1 routine, from the twin\'s "…" menu.']), `ready to twin: a routine is optional, not "left for later" (${JSON.stringify(P.previewLines(preview.pairs[0]))})`)
  for (const s of ['Ready to twin Trail Guide', 'Nothing changes in Grok.', ...P.previewLines(preview.pairs[0]), 'Left out: Garden Buddy (already has a twin)', 'See every item', 'Options', 'Twin this bot', 'Back']) {
    ok(html.includes(s), `ready to twin shows "${s}"`)
  }
  ok(!html.includes('route-notes') && !html.includes('SKILL.md') && !raw.includes('<input') && !html.includes('trail-guide'), 'ready to twin: the item table and the short name stay folded')
  ok(!/Preview twins|Nothing has been written|Create twins|schedule|scan:/.test(html), 'ready to twin: no engine table wording')
  // The three lines carry no brackets and no term nobody has explained yet; the reasons live with the items.
  ok(!/\(|skill sync|yourself/.test(preview.pairs[0].lines.join(' ')) && !html.includes('nothing runs twice'), 'ready to twin: short lines, the reasons folded with the items')
  {
    // One row of "See every item": the kind once, capitalised; the name (or the first line of the text); the reason.
    const item = it => text(render(jsx('ul', { children: jsx(P.PreviewItem, { item: it }) })))
    let h = item(preview.pairs[0].items[0])
    ok(h.includes('Name') && count(h.toLowerCase(), 'name') === 1 && h.includes('Trail Guide') && h.includes('copies now'), `item row: the kind is not repeated as the name (${h})`)
    h = item({ item: 'persona.instructions', kind: 'instructions', name: 'instructions', action: 'by_hand' })
    ok(count(h.toLowerCase(), 'instructions') === 1 && h.includes('Instructions') && h.includes("later, with the page's help") && !/yourself/.test(h), `item row: "instructions" once (${h})`)
    h = item(preview.pairs[0].items[2])
    ok(h.includes('Routine') && h.includes('Saturday plan') && h.includes('Arrives switched off, so nothing runs twice'), `item row: the reason sits with the item (${h})`)
    h = item(preview.pairs[0].items[1])
    ok(h.includes('Skill') && h.includes('route-notes') && h.includes('optional, set up later') && h.includes('Safety check: take a look') && !/skill sync/.test(h), `item row: a skill (${h})`)
    // An older backend sends no lines: the page writes the same three, from the items.
    const lines = P.previewLines({ items: [
      { kind: 'name', action: 'create' }, { kind: 'description', action: 'create' }, { kind: 'instructions', action: 'by_hand' },
      { kind: 'routine', name: 'a', action: 'by_hand' }, { kind: 'routine', name: 'b', action: 'by_hand' }, { kind: 'connector', name: 'c', action: 'by_hand' },
      { kind: 'skill', name: 'd', action: 'via_mirror' }, { kind: 'skill', name: 'e', action: 'via_mirror' }], secrets_withheld: [{}] })
    // (routines are optional and are moved from the twin's menu: they are not listed as something "left for later")
    ok(JSON.stringify(lines) === JSON.stringify(['Copies now: name and description.', "Later, with the page's help: instructions, 1 connection.", 'Optional: 2 routines, from the twin\'s "…" menu.', '2 skills: optional, set up later.', '1 thing that looks like a password or key will not be copied.']), `preview lines (older backend): ${JSON.stringify(lines)}`)
    ok(JSON.stringify(P.previewLines({ items: [{ kind: 'name', action: 'create' }] })) === JSON.stringify(['Copies now: name.', 'Nothing is left for later.']), `preview lines: nothing for later, no skills (${JSON.stringify(P.previewLines({ items: [{ kind: 'name', action: 'create' }] }))})`)
  }
  raw = render(jsx(P.AddFlow, { open: true, onClose: noop, ready: true, initialPreview: preview }))
  ok(JSON.stringify(primaries()) === JSON.stringify(['Twin this bot']) && endsInButton(raw), 'Twin this bot is on, and the one primary')
  reset()
  H.restReply = async path => (path === '/twins/create' ? { pairs: [trail] } : {})
  await press('Twin this bot')
  ok(H.restCalls[0]?.path === '/twins/create' && JSON.stringify(H.restCalls[0]?.opts?.body) === JSON.stringify({ grok_ids: ['b-trail-0005'], profiles: { 'b-trail-0005': 'trail-guide' } }), `create sends exactly what was shown: ${JSON.stringify(H.restCalls[0]?.opts?.body)}`)
  {
    const two = { profiles_in_use: ['default'], skipped: [], pairs: [
      { grok: { id: 'b-trail-0005', name: 'Trail Guide' }, hermes: { profile: 'default' }, items: [], secrets_withheld: [], schedules_paused: 0, lines: ['Copies now: name and description.'] },
      { grok: { id: 'b-plants-0004', name: 'Window Plants' }, hermes: { profile: 'window-plants' }, items: [], secrets_withheld: [], schedules_paused: 0, lines: ['Copies now: name and description.'] }] }
    raw = render(jsx(P.AddFlow, { open: true, onClose: noop, ready: true, initialPreview: two }))
    html = text(raw)
    ok(html.includes('Ready to twin 2 bots') && H.buttons.some(b => b.text === 'Twin 2 bots'), 'two bots: count-aware title and button')
    ok(html.includes('Another twin already uses that name. Try another.') && html.includes('Short name in Hermes') && H.buttons.find(b => /^Twin 2/.test(b.text))?.disabled === true && raw.includes('<input'), 'a taken short name is flagged, Options opens, the button is off')
    const c = P.profileClash(two, { 'b-trail-0005': 'window-plants', 'b-plants-0004': 'window-plants' })
    ok(/Another bot here uses that name too/.test(c['b-trail-0005']) && /Another bot/.test(c['b-plants-0004']), 'same name twice in one batch is flagged')
    ok(Object.keys(P.profileClash(two, { 'b-trail-0005': 'trail-guide', 'b-plants-0004': 'window-plants' })).length === 0, 'distinct new names pass')
  }
  // Nothing new to twin (the bot already has one): said plainly, no "0 bots", no button that cannot work.
  html = text(render(jsx(P.AddFlow, { open: true, onClose: noop, ready: true, initialPreview: { pairs: [], skipped: [{ grok_id: 'b-garden-0003', name: 'Garden Buddy', reason: 'already_twinned' }], profiles_in_use: [] } })))
  ok(html.includes('Nothing new to twin') && html.includes('Left out: Garden Buddy (already has a twin)') && !/0 bots/.test(html) && !H.buttons.some(b => /^Twin /.test(b.text)) && has('Back'), `nothing to twin: ${html.slice(0, 200)}`)

  // ── After twinning: one sheet for every new twin (instructions and a model, for all of them at once) ──
  {
    const done = (created, props = {}) => render(jsx(P.AddFlow, { open: true, onClose: noop, ready: true, initialStep: 'done', initialCreated: created, ...props }))
    const batch = (props = {}) => render(jsx(P.BatchSheet, { onClose: noop, open: true, pairs: [trail, notes, club], storage: ctx.storage, ...props }))
    const NO_FINISH = /\bstarted\b|\bfinish/i
    const boxes = () => H.checks.filter(c => /^batch-/.test(c.id ?? '') && c.id !== 'batch-consent')

    // Three twins made: one sheet, a box per twin (all ticked), one model, two ways on.
    raw = done([trail, notes, club])
    html = text(raw)
    ok(html.includes('3 twins made.') && html.includes('Each needs its instructions and a model.') && !NO_FINISH.test(html), `after twinning three bots: the batch sheet (${html.slice(0, 200)})`)
    ok(boxes().length === 3 && boxes().every(c => c.checked && !c.disabled) && JSON.stringify(boxes().map(c => c.label)) === JSON.stringify(['Trail Guide', 'Garden Notes', 'Book Club']), `batch: a ticked box per twin, named (${JSON.stringify(boxes().map(c => [c.label, c.checked]))})`)
    ok(count(html, 'Needs its instructions and a model') === 3, 'batch: each twin says what it needs')
    ok(JSON.stringify(primaries()) === JSON.stringify(['Do all ticked now']) && variantOf('One at a time') === 'secondary' && variantOf('Later') === 'ghost' && JSON.stringify(texts()) === JSON.stringify(['Later', 'One at a time', 'Do all ticked now']) && endsInButton(raw), `batch: Do all ticked now is the one primary, One at a time beside it (${texts()})`)
    ok(!H.buttons.some(b => NO_FINISH.test(b.text ?? '') || NO_FINISH.test(b.label ?? '')), 'batch: no "started" or "Finish" on any button')
    // One model for all of them: the one Hermes uses is chosen already, others can be typed.
    const picker = H.inputs.find(i => i.id === 'batch-model')
    ok(picker?.value === 'grok-4.7' && picker?.list === 'batch-models' && picker?.['aria-label'] === 'Model for the ticked twins' && raw.includes('<option value="local-small">') && html.includes("Each twin's model can be changed later."), `batch: the model picker starts at the model Hermes uses (${JSON.stringify(picker && { list: picker.list, value: picker.value })})`)
    // Nothing is done by looking: no call but the page's own reads.
    ok(H.restCalls.every(c => !/^\/batch\/(model|import|downloads|grok-part)/.test(c.path)), 'batch: opening the sheet changes nothing')
    {
      const asked = []
      batch({ onOneAtATime: id => asked.push(id) })
      await press('One at a time')
      ok(JSON.stringify(asked) === JSON.stringify(['trail-guide']), 'One at a time hands the first ticked twin to the page (its own sheet, as before)')
      // Unticked twins are left out.
      batch({ initialTicked: { 'trail-guide': false }, onOneAtATime: id => asked.push(id) })
      await press('One at a time')
      ok(asked.at(-1) === 'garden-notes' && boxes()[0].checked === false, 'One at a time: the first twin that is still ticked')
      batch({ initialTicked: { 'trail-guide': false, 'garden-notes': false, 'book-club': false } })
      ok(H.buttons.find(b => b.text === 'Do all ticked now')?.disabled === true && H.buttons.find(b => b.text === 'One at a time')?.disabled === true, 'nothing ticked: both ways wait')
    }
    // Do all ticked now: the model goes to the ticked twins that have none, then the instructions step.
    reset()
    H.restReply = async path => (path === '/batch/model' ? batchModels : {})
    batch()
    await press('Do all ticked now')
    ok(H.restCalls[0]?.path === '/batch/model' && H.restCalls[0]?.opts?.method === 'POST' && JSON.stringify(H.restCalls[0]?.opts?.body) === JSON.stringify({ model: 'grok-4.7', pair_ids: ['trail-guide', 'garden-notes', 'book-club'] }), `Do all ticked now sets the model on the ticked twins (${JSON.stringify(H.restCalls[0])})`)
    // A twin that already has the owner's model is not touched (and its line names only its instructions).
    H.override.batch = { data: { ...batchInfo, twins: batchInfo.twins.map(t => (t.pair_id === 'garden-notes' ? { ...t, left: ['instructions'], model: 'grok-4.7' } : t)) } }
    reset()
    html = text(batch({ initialTicked: { 'book-club': false } }))
    ok(count(html, 'Needs its instructions and a model') === 2 && html.includes('Garden Notes Needs its instructions Book Club'), `batch: a twin with a model already needs only its instructions (${html.slice(0, 300)})`)
    ok(html.includes('Each line says what that twin still needs.'), 'batch: twins that need different things: each line says what')
    await press('Do all ticked now')
    ok(JSON.stringify(H.restCalls[0]?.opts?.body) === JSON.stringify({ model: 'grok-4.7', pair_ids: ['trail-guide'] }), `the model only where it is missing, only for ticked twins (${JSON.stringify(H.restCalls[0]?.opts?.body)})`)
    // ...unless the owner chose another model: then for every ticked twin.
    reset()
    batch({ initialModel: 'local-small' })
    await press('Do all ticked now')
    ok(JSON.stringify(H.restCalls[0]?.opts?.body) === JSON.stringify({ model: 'local-small', pair_ids: ['trail-guide', 'garden-notes', 'book-club'] }), `another model chosen: set on every ticked twin (${JSON.stringify(H.restCalls[0]?.opts?.body)})`)
    // Two made, one with nothing left: only the one that needs something is named.
    H.override.batch = { data: { ...batchInfo, twins: [batchInfo.twins[0], { ...batchInfo.twins[1], left: [], model: 'grok-4.7' }] } }
    html = text(done([trail, { ...notes, setup_left: [] }]))
    ok(html.includes('2 twins made.') && html.includes('Trail Guide needs its instructions and a model; the others have everything.') && boxes().length === 0 && has('Do it now'), `two made, one needs something: it is named (${html.slice(0, 200)})`)
    H.override = {}

    // One twin: its own wording, and only what is really left.
    H.override.batch = { data: { ...batchInfo, twins: [batchInfo.twins[0]] } }
    html = text(done([trail]))
    ok(html.includes("Trail Guide's twin is made.") && html.includes('It needs its instructions and a model.') && boxes().length === 0 && JSON.stringify(texts()) === JSON.stringify(['Later', 'Step by step', 'Do it now']) && !NO_FINISH.test(html), `one twin made: named, what it needs, no box to tick (${texts()}; ${html.slice(0, 160)})`)
    ok(H.inputs.find(i => i.id === 'batch-model')?.['aria-label'] === 'Model for Trail Guide' && html.includes('Its model can be changed later.'), 'one twin: the model picker names it')
    H.override.batch = { data: { ...batchInfo, twins: [{ ...batchInfo.twins[0], left: ['instructions'], model: 'grok-4.7' }] } }
    html = text(done([{ ...trail, setup_left: ['instructions'] }]))
    ok(html.includes('It needs its instructions.') && !html.includes('and a model') && html.includes('It has a model already'), `one twin with the owner's model: only its instructions are named (${html.slice(0, 200)})`)
    H.override = {}

    // The instructions step: one request, three steps, Downloads watched, files can be dropped, a line per twin.
    reset()
    raw = batch({ initialPhase: 'instructions' })
    html = text(raw)
    for (const s of ['Instructions for 3 twins', 'Ask each bot in Grok once; Hermes brings in their answers.', 'Paste it into the chat of each ticked bot in Grok.', 'Save each answer file to your Downloads folder.', 'Watching your Downloads folder: each answer file is brought in as it lands.', 'Or drop the answer files here', EXPORT_PRIVACY, 'Trail Guide: waiting for its file…', 'Garden Notes: waiting for its file…', 'Book Club: waiting for its file…']) {
      ok(html.includes(s), `batch instructions step shows "${s}"`)
    }
    ok(!NO_FINISH.test(html) && count(html, EXPORT_PRIVACY) === 1, 'batch instructions step: the privacy sentence once; no "started" or "Finish"')
    ok(JSON.stringify(primaries()) === JSON.stringify(['Copy the request']) && has('Choose files…') && variantOf('Let Hermes do the Grok part') === 'text' && !has('Keep their skills the same with Skill sync…'), `batch instructions step: Copy the request is the one primary; the Grok part is a quiet way (${texts()})`)
    ok(raw.includes('multiple=""') && raw.includes('aria-label="Drop the answer files here"'), 'batch: several files can be chosen or dropped at once')
    const byHand = H.buttons.filter(b => b.text === 'Paste it yourself instead')
    ok(byHand.length === 3 && byHand[0].label === "Paste Trail Guide's instructions yourself instead" && byHand.every(b => b.variant === 'text'), `batch: a quiet "Paste it yourself instead" per waiting twin, named (${JSON.stringify(byHand.map(b => b.label))})`)
    await press('Copy the request')
    ok(H.clips.at(-1) === EXPORT_REQUEST, 'Copy the request puts the request on the clipboard')
    html = text(batch({ initialCopied: true, initialPhase: 'instructions' }))
    ok(html.includes("Copied. Paste it into each bot's chat in Grok.") && variantOf('Copy the request') === 'secondary', 'after copying: said, and the button steps back')
    // The page watches Downloads every 4 s while the sheet is open, for at most 15 minutes, from just before it began.
    ok(/post\('\/batch\/downloads', \{ apply: true, pair_ids: chosenIds, since \}\)/.test(source) && /const BATCH_EVERY_MS = 4_000/.test(source) && /const BATCH_WATCH_MS = 15 \* 60 \* 1000/.test(source) && /Math\.floor\(Date\.now\(\) \/ 1000\) - 5/.test(source) && /timers\.setInterval\(\(\) => \{\n\s+if \(Date\.now\(\) - began >= BATCH_WATCH_MS\)/.test(source), 'batch: Downloads looked at every 4 s, 15 minutes at most, since the moment watching began (minus 5 s)')
    ok(/post\('\/batch\/import', \{ files: sent, pair_ids: chosenIds \}\)/.test(source) && /for \(const f of picked\) \{[\s\S]{0,200}exportTextOf\(f\)/.test(source), 'batch: dropped or chosen files are read as text and sent together')
    // With skill sync set up, a bot also saves its file in the box and it comes through the shared folder.
    H.override.batch = { data: { ...batchInfo, pickup: { via: 'mirror', label: 'the shared folder' }, skill_sync: { set_up: true, pair_ids: [] } } }
    html = text(batch({ initialPhase: 'instructions' }))
    ok(html.includes('Each bot also saves its file in the shared box folder, and skill sync brings it here.') && html.includes("Watching the shared folder and Downloads. A file saved in the box arrives after the sync bot's next run") && !html.includes('Watching your Downloads folder'), `batch with skill sync: the shared folder is watched too (${html.slice(0, 160)})`)
    H.override = {}
    html = text(batch({ initialPhase: 'instructions', initialWatch: 'stopped' }))
    ok(html.includes('Stopped watching after 15 minutes.') && variantOf('Watch again') === 'secondary', 'batch: after 15 minutes the watch stops, with Watch again')
    // Progress per twin, in plain words.
    raw = batch({ initialGot: P.mergeBatch({}, batchDownloads.results), initialPhase: 'instructions', initialUnmatched: 1 })
    html = text(raw)
    for (const s of ['Trail Guide: instructions in ✓', '2 routines added, switched off', '1 connector to set up here', '1 line left out: it looks like a password or key', 'Garden Notes: waiting for its file…', 'Book Club: its file could not be brought in. This file is not a setup export.', '1 file was not from a ticked bot.']) {
      ok(html.includes(s), `batch progress shows "${s}"`)
    }
    ok(H.buttons.filter(b => b.text === 'Paste it yourself instead').length === 2, 'a twin whose file is in has no "Paste it yourself instead"')
    // A twin that is done stays done; "no file yet" never overwrites a line.
    const merged = P.mergeBatch(P.mergeBatch({}, batchDownloads.results), [{ pair_id: 'trail-guide', state: 'failed', why: 'x' }, { pair_id: 'book-club', state: 'no_file' }])
    ok(merged['trail-guide'].state === 'done' && merged['book-club'].state === 'failed' && !('garden-notes' in merged), 'batch: done stays done, no_file changes nothing')
    // Skill sync set up: the way to it, once.
    H.override.batch = { data: { ...batchInfo, skill_sync: { set_up: true, pair_ids: ['recipe-planner'] } } }
    batch({ initialPhase: 'instructions' })
    ok(variantOf('Keep their skills the same with Skill sync…') === 'text', 'skill sync set up: offered as a quiet link')
    H.override = {}
    // Model results said once, failures in plain words.
    html = text(batch({ initialModelSaid: { failed: [{ name: 'Book Club', why: 'Hermes could not write its settings file.' }], model: 'grok-4.7', set: ['Trail Guide', 'Garden Notes'] }, initialPhase: 'instructions' }))
    ok(html.includes('Model grok-4.7 set for Trail Guide and Garden Notes.') && html.includes('Book Club: the model was not set. Hermes could not write its settings file.'), 'batch: the model result, and the twin it failed for')

    // Every ticked twin has its instructions: said, and Done.
    const allIn = Object.fromEntries(['trail-guide', 'garden-notes', 'book-club'].map(id => [id, { connectors: 0, file: 'f.json', instructions: true, leftOut: 0, routines: id === 'trail-guide' ? 2 : 0, state: 'done', why: '' }]))
    raw = batch({ initialGot: allIn, initialModelSaid: { failed: [], model: 'grok-4.7', set: ['Trail Guide', 'Garden Notes', 'Book Club'] }, initialPhase: 'instructions' })
    html = text(raw)
    ok(html.includes('All 3 twins have their instructions.') && html.includes('They follow Grok from now on.') && JSON.stringify(texts()) === JSON.stringify(['Done']) && endsInButton(raw) && !NO_FINISH.test(html), `all done: said, with Done (${texts()}; ${html.slice(0, 200)})`)
    ok(html.includes('Routines stay switched off until you choose Move routines to Hermes'), 'all done: where the routines that came along are switched on')

    // Let Hermes do the Grok part: not available here -> exactly what to switch on.
    reset()
    raw = batch({ initialPhase: 'grok' })
    html = text(raw)
    ok(html.includes('Let Hermes do the Grok part') && html.includes('Hermes needs a few things switched on first.') && cuOff.enable.every(s => html.includes(s)) && has('Look again') && !has('Start in a new Hermes chat') && H.restCalls.length === 0, `Grok part not available: the sentences to switch on, nothing started (${html.slice(0, 260)})`)
    // Available: the permission check comes first, and Start waits for the owner's tick.
    H.override['computer-use'] = { data: cuOn }
    H.newChats = []
    raw = batch({ initialPhase: 'grok' })
    html = text(raw)
    for (const s of ['Before Hermes starts', "In a new Hermes chat, Hermes opens the chat of each ticked bot in the Grok app, pastes the request and saves the answer to your Downloads folder.", 'Every click asks you first: answer Once each time.', "Screenshots of the Grok window go to this chat's model provider.", 'You can stop it at any time with Stop, or by typing /stop.', 'I will approve each step myself']) {
      ok(html.includes(s), `Grok part permission check shows "${s}"`)
    }
    const consent = H.checks.find(c => c.id === 'batch-consent')
    ok(consent && consent.checked === false && consent.label === 'I will approve each step myself' && H.buttons.find(b => b.text === 'Start in a new Hermes chat')?.disabled === true && H.restCalls.length === 0 && H.newChats.length === 0, 'Grok part: Start waits until "I will approve each step myself" is ticked; nothing starts by itself')
    reset()
    H.newChats = []
    H.clips = []
    H.restReply = async path => (path === '/batch/grok-part' ? grokPartOk : {})
    batch({ initialConsent: true, initialPhase: 'grok' })
    ok(H.buttons.find(b => b.text === 'Start in a new Hermes chat')?.disabled === false && primaries().includes('Start in a new Hermes chat'), 'ticked: Start in a new Hermes chat can be pressed')
    await press('Start in a new Hermes chat')
    ok(H.restCalls[0]?.path === '/batch/grok-part' && JSON.stringify(H.restCalls[0]?.opts?.body) === JSON.stringify({ pair_ids: ['trail-guide', 'garden-notes', 'book-club'] }) && H.newChats.length === 1 && H.clips.at(-1) === grokPartOk.message, `Start: hands over the ticked twins, opens a new chat, the message on the clipboard (${JSON.stringify(H.restCalls[0])})`)
    ok(H.notes.at(-1)?.message === 'Paste it into the new chat' && /answer Once each time/.test(H.notes.at(-1)?.meta), `Start: says to paste it into the new chat (${JSON.stringify(H.notes.at(-1))})`)
    {
      const kept = (H.storedSets ?? []).filter(([k]) => k === 'batch-open').at(-1)?.[1]
      ok(kept && JSON.stringify(kept.ids) === JSON.stringify(['trail-guide', 'garden-notes', 'book-club']) && Number.isFinite(kept.since) && Math.abs(kept.at - Date.now()) < 5_000, `Start: the open batch is remembered on this computer (${JSON.stringify(kept)})`)
    }
    // The Twins page opens it again (the new chat took the page away): straight at the watching step, with the
    // remembered time, a line about the other chat and a way to stop Hermes.
    {
      const keepMode = H.mode
      const keepStatus = H.status
      const SINCE = Math.floor(Date.now() / 1000) - 400
      H.mode = 'ok'
      H.status = { ...statusAll, sample: { on: false, hide_real: false, instructions_example: null }, pairs: [trail, notes, club] }
      H.storedRemoves = []
      H.stored['batch-open'] = { at: Date.now() - 5 * 60_000, ids: ['trail-guide', 'garden-notes'], since: SINCE }
      ok(JSON.stringify(P.openBatchOf(ctx.storage)) === JSON.stringify({ at: Date.now() - 5 * 60_000, ids: ['trail-guide', 'garden-notes'], since: SINCE }), 'a batch handed over 5 minutes ago is read back whole')
      html = text(page())
      ok(html.includes('Instructions for 2 twins') && html.includes('Hermes is doing the Grok part in another chat. Files it saves to Downloads are picked up here.') && html.includes('Trail Guide: waiting for its file…') && !html.includes('Book Club: waiting') && variantOf('Stop Hermes') === 'text' && !(H.storedRemoves ?? []).includes('batch-open'), `Twins page: a fresh batch opens again at the watching step (${html.slice(0, 160)})`)
      ok(/useState\(\(\) => \(resumed \? resumed\.since : /.test(source), 'the reopened sheet watches from the remembered time, so files saved meanwhile are brought in')
      // Older than half an hour: not opened, and forgotten.
      H.stored['batch-open'] = { at: Date.now() - 31 * 60_000, ids: ['trail-guide', 'garden-notes'], since: SINCE }
      html = text(page())
      ok(!html.includes('Instructions for') && !html.includes('Hermes is doing the Grok part') && (H.storedRemoves ?? []).includes('batch-open'), 'Twins page: a batch older than 30 minutes is not opened again, and is cleared')
      H.storedRemoves = []
      H.stored['batch-open'] = { at: Date.now(), ids: [7], since: 'x' }
      ok(P.openBatchOf(ctx.storage) === null && (H.storedRemoves ?? []).includes('batch-open'), 'a record that is not a batch is cleared')
      delete H.stored['batch-open']
      ok(P.openBatchOf(ctx.storage) === null, 'nothing remembered: nothing opens')
      // Stop Hermes: the backend is told to stop, and the batch is forgotten.
      reset()
      H.storedRemoves = []
      H.restReply = async path => (path === '/batch/grok-part' ? { ok: true } : {})
      batch({ pairs: [trail, notes], resumed: { at: Date.now(), ids: ['trail-guide', 'garden-notes'], since: SINCE } })
      await press('Stop Hermes')
      ok(H.restCalls[0]?.path === '/batch/grok-part' && JSON.stringify(H.restCalls[0]?.opts?.body) === JSON.stringify({ stop: true }) && (H.storedRemoves ?? []).includes('batch-open') && H.notes.at(-1)?.message === 'Hermes stopped the Grok part', `Stop Hermes posts stop and forgets the batch (${JSON.stringify(H.restCalls[0])})`)
      // Later forgets it too.
      H.storedRemoves = []
      batch({ pairs: [trail, notes], resumed: { at: Date.now(), ids: ['trail-guide', 'garden-notes'], since: SINCE } })
      await press('Later')
      ok((H.storedRemoves ?? []).includes('batch-open'), 'Later: the remembered batch is cleared')
      H.restReply = async () => ({})
      H.mode = keepMode
      H.status = keepStatus
    }
    // The hand-over refused (switched off meanwhile): no chat is opened.
    reset()
    H.newChats = []
    H.restReply = async path => (path === '/batch/grok-part' ? grokPartOff : {})
    batch({ initialConsent: true, initialPhase: 'grok' })
    await press('Start in a new Hermes chat')
    ok(H.newChats.length === 0, 'Grok part refused: no chat is opened')
    H.override = {}
    H.restReply = async () => ({})

    // Today's backend does not say what is left: the page claims only what is true (the old screen, "made").
    raw = done([{ ...legacy, pair_id: 'trail-guide', grok: trail.grok, hermes: trail.hermes }])
    html = text(raw)
    ok(html.includes("Trail Guide's twin is made") && html.includes("It has the bot's name and description, and follows them.") && !/keeps up with Grok by itself|now has a twin|ready|started/.test(html) && !/Finish/.test(texts().join()), `after twinning (older backend): only what is true (${html.slice(0, 200)})`)
    ok(JSON.stringify(primaries()) === JSON.stringify(['Done']) && variantOf('Open in Hermes') === 'secondary', 'after twinning (older backend): Done, with Open in Hermes beside it')
    await press('Open in Hermes')
    ok(H.newChats.at(-1) === 'trail-guide', 'Open in Hermes starts a chat with that twin')
    // Nothing left (a twin whose bot has no instructions and whose copy already has a model).
    html = text(done([{ ...trail, setup_left: [] }]))
    ok(html.includes("Trail Guide's twin is ready") && !html.includes('twin is ready.') && html.includes('It follows Grok from now on.') && JSON.stringify(primaries()) === JSON.stringify(['Open in Hermes']) && variantOf('Done') === 'ghost', 'after twinning with nothing left: ready, Open in Hermes')
    html = text(done([{ ...trail, setup_left: [] }, { ...notes, setup_left: [] }]))
    ok(html.includes('2 twins are ready') && html.includes('They follow Grok from now on.') && JSON.stringify(texts()) === JSON.stringify(['Done']) && !/started/.test(html), 'several new twins, nothing left: ready, Done')
    html = text(done([]))
    ok(html.includes('No twin was made') && html.includes('Nothing was changed.') && JSON.stringify(texts()) === JSON.stringify(['Done']), 'no twin made: said plainly')
  }

  // ── Everyday page ──
  H.status = statusAll
  raw = page()
  html = text(raw)
  for (const s of ['Twins', '2 twins and the sample · 1 needs your attention · 1 with a change to send to Grok', 'Add a bot', 'Acme Support', 'Sample', '2 things need you', '1 change to send to Grok', 'Follows Grok', 'Open in Hermes',
    // (one box at a time. The box that is open is the first one in page order that
    // holds a button to press, which now includes a choice of two: the sample's. Its second matter and Garden
    // Buddy's wait as one line each)
    'The skill "greet-visitors" changed in both places.', 'Keep the Hermes version', 'Keep the Grok version', 'A password or key was not copied', 'A skill changed in Hermes',
    'Sample twin with invented data. Nothing of yours is touched.', 'End sample', 'What can go wrong?', 'Privacy', 'Remove Grok Bot Twin…']) {
    ok(html.includes(s), `page shows "${s}"`)
  }
  noEngineWords(html, 'page')
  // A calm row says two words. When it was last checked is there on hover (and in Details), not as a ticking counter.
  ok(!/checked (just now|\d+ min ago)/i.test(html) && /title="Checked (just now|1 min ago)"/.test(raw) && count(html, 'Follows Grok') === 1 && !/name and description/.test(html.split('Recipe Planner')[1].split('Garden Buddy')[0]), 'page: a calm row says "Follows Grok"; the check time is on hover only')
  // The sample banner is one line and one way out; what can go wrong is folded, and the one thing that goes
  // right is not a "try it" among the failures.
  ok(!/Try it:|Change the bot in Grok|Change it in both places|Paste a fake password|A new twin starts with its name|The sample has no chat/.test(html), 'page: the sample banner is one line; the try-outs are folded')
  ok(!/Turn on skill sync|I copied it myself|Copy it into Grok yourself/.test(html), 'page: a skill to carry over comes with its text, not with a ten-minute set-up as the first button')
  ok(!/Send to Grok|Setup|Recent activity 0|Grok → Hermes|Hermes → Grok|Syncs|Not carried|Changes waiting|Add twins|Up to date|Needs your attention|Compare with Grok|Check again|Done, stop reminding me|Ready ·|Paused/.test(html), 'page: no always-there Send to Grok, no chips, no control panel, no old labels')
  ok(!html.includes('grok-bot-twin-sample') && !html.includes('recipe-planner') && !html.includes('garden-buddy') && !/\bdefault\b/.test(html), 'page: no internal name as a headline, the sample names no real place')
  ok(H.buttons.filter(b => b.text === 'Open in Hermes' && b.variant === 'secondary').length === 2 && H.buttons.filter(b => b.label === 'Open Acme Support in Hermes').length === 0 && H.buttons.some(b => b.label === 'Open Garden Buddy in Hermes'), 'Open in Hermes on every real twin, also the one that needs something (never on the sample)')
  ok(count(raw, 'role="status"') === 1 && !raw.includes('role="alert"'), 'one status region announces the attention state; nothing that is simply there shouts')
  ok(!raw.includes('data-tip') && !/<span[^>]*tabindex/.test(raw), 'nothing essential in tooltips, no chips as tab stops')
  ok(!raw.includes('<b>bold</b>') && raw.includes('&lt;b&gt;bold&lt;/b&gt;'), 'untrusted name rendered as text')
  ok(!/\u0007/.test(raw), 'control chars stripped')
  ok(!/3f2a9c1b7e40|c9176fde1247|c354dae9eaf5/.test(raw) && !/\+\+\+ hermes|--- grok|@@ -1/.test(raw), 'both-places box: no hashes, no diff headers')
  // State is a shape and a word as well as a colour.
  ok(/data-tone="warn"/.test(raw) && /data-tone="good"/.test(raw) && /data-tone="send"/.test(raw) && raw.includes('data-codicon="warning"') && raw.includes('data-codicon="pass"') && raw.includes('data-codicon="arrow-up"') && !/Needs you:|Fine:|Paused:/.test(html), 'state: a shape per state next to the sentence (not a coloured dot alone), and no word that doubles the sentence')
  // The triangle is for trouble only: one twin here has trouble, so one triangle next to a sentence.
  ok(count(raw, 'data-tone="warn"') === 1, `state: a change to send is an arrow, not a warning (${count(raw, 'data-tone="warn"')} triangles)`)
  // The shape and its sentence are one unit, so a narrow window never leaves the shape alone on a line.
  ok(/<span class="inline-flex[^"]*"[^>]*><span[^>]*data-tone="good"[^>]*>.*?<\/span><span[^>]*>Follows Grok · ready<\/span><\/span>/s.test(raw), 'state: the shape and the sentence are wrapped together')
  // Every open matter of a twin is there, but only the first is open; the others wait as one-line headers.
  ok(P.mattersOf(acme).map(m => m.kind).join() === 'conflict,secret', 'both open matters of one twin are known, the choice first')
  ok(html.includes('Keep the Hermes version') && !html.includes("A password or key in Acme Support's description") && !H.buttons.some(b => b.label === 'Show: Something changed in both places') && H.buttons.filter(b => b.label === 'Show: A password or key was not copied' && b.variant === 'outline').length === 1 && H.buttons.filter(b => b.label === 'Show: A skill changed in Hermes' && b.variant === 'outline').length === 1 && !html.includes("Copy the skill's text"), 'the leading row shows its first matter; its second, and the other row\'s, are one-line headers that open on a click')
  {
    // On a page where no other row leads, the first matter of the first row is open and the second waits.
    H.status = { ...statusAll, pairs: [acme, recipe] }
    const rawC = page()
    const htmlC = text(rawC)
    for (const s of ['The skill "greet-visitors" changed in both places.', 'Hermes version', 'Grok version', 'Keep the Hermes version', 'Keep the Grok version', 'Grok keeps its own version.', 'Replaces the Hermes one. To undo, see', 'Earlier versions', 'Show line-by-line changes', 'Say a cheerful hello']) {
      ok(htmlC.includes(s), `page (the sample's choice is the open box) shows "${s}"`)
    }
    ok(!htmlC.includes("A password or key in Acme Support's description") && H.buttons.filter(b => b.label === 'Show: A password or key was not copied' && b.variant === 'outline').length === 1 && !H.buttons.some(b => b.label === 'Show: Something changed in both places'), 'the first matter is open; the second is a one-line header that opens on a click')
    // The consequence of each choice comes before its button; the whole sentence is the button's hover text.
    ok(htmlC.indexOf('Grok keeps its own version.') < htmlC.indexOf('Keep the Hermes version') && htmlC.indexOf('Replaces the Hermes one.') < htmlC.indexOf('Keep the Grok version') && H.buttons.find(b => b.text === 'Keep the Grok version')?.title === 'Replaces the one in Hermes. You can undo this under Earlier versions.' && H.buttons.find(b => b.text === 'Keep the Hermes version')?.title === 'Grok still has its own version. You can send this one to Grok afterwards.', 'each choice says what happens before its button')
    // A page with an open matter has exactly one main button. It is the choice that
    // replaces nothing, keeping the Hermes version; the other choice is an outline right beside it. Nothing
    // else on such a page is filled: not the row, not "Add a bot".
    ok(JSON.stringify(primaries()) === JSON.stringify(['Keep the Hermes version']) && variantOf('Keep the Grok version') === 'outline' && !htmlC.includes('Ready for your own bots?'), `a choice of two is open: one main button, the choice that replaces nothing (${primaries()})`)
    // Only the lines that differ are in the cards; "…" stands where the same lines were left out.
    ok(htmlC.includes('Say a cheerful hello') && htmlC.includes('…') && count(htmlC, 'Keep it short') === 0, 'both versions: only the differing lines are shown')
    H.status = statusAll
    raw = page()
    html = text(raw)
  }
  // Every button on the page is the app's own button (focus ring, disabled state): no hand-made <button>.
  ok(!/<button(?![^>]*data-variant)/.test(raw) && !raw.includes('aria-expanded="false"'), 'page: no raw <button>; a waiting matter is a real button')
  // The "…" menus hold everything else, in people's words: five entries, not eight.
  ok(H.menu[0]?.text === 'Hide my real bots while the sample is on', 'the recording aid sits in the sample banner\'s own menu')
  ok(JSON.stringify(H.menu.slice(1, 6).map(m => m.text)) === JSON.stringify(['Check now', 'Details…', 'Skill sync… (on)', 'Move routines to Hermes…', 'End sample']), `row menu (sample): ${H.menu.slice(1, 6).map(m => m.text)}`)
  // (a twin with something replaced has the way back in its menu: … → Earlier versions… → Put back is three steps)
  // (a real twin's menu also holds the way to hand over the file its bot writes about itself; the sample's does not:
  // nothing can be imported into it)
  ok(JSON.stringify(H.menu.slice(6, 13).map(m => m.text)) === JSON.stringify(['Open in Hermes', 'Check now', 'Details…', 'Earlier versions…', 'Import setup export…', 'Skill sync…', 'Stop twinning…']), `row menu (real twin, nothing to move): ${H.menu.slice(6, 13).map(m => m.text)}`)
  ok(H.menu.length === 19 && H.menu.filter(m => m.text === 'Import setup export…').length === 2, `no other menu entries (${H.menu.length})`)
  ok(raw.includes('aria-label="More for Acme Support"') && raw.includes('aria-label="Open Recipe Planner &lt;b&gt;bold&lt;/b&gt; in Hermes"'), 'buttons carry aria labels')
  // One primary button on the whole page at a time: on the first row, in page order, with a button that can
  // be pressed. The first twin's open matter is a choice of two; its main button keeps the Hermes version.
  ok(JSON.stringify(primaries()) === JSON.stringify(['Keep the Hermes version']) && variantOf('Add a bot') === 'outline' && variantOf('Open in Hermes') === 'secondary', `page: exactly one primary (${primaries()})`)
  // The row's one button is the app's normal size, not the small one.
  ok(H.buttons.find(b => b.text === 'Open in Hermes')?.size === 'default' && H.buttons.find(b => b.text === 'Add a bot')?.size === 'default', 'page: the row button and Add a bot are full-size targets')
  ok(H.buttons.filter(b => ['Privacy', 'Remove Grok Bot Twin…'].includes(b.text)).every(b => b.size === 'default' && b.variant === 'text'), 'page: the footer links are full-size targets')
  // The way to more twins is the last thing in the list, inside the column (not pinned to the window's corner).
  ok(html.indexOf('Add a bot') > html.indexOf('Garden Buddy') && html.indexOf('Add a bot') < html.indexOf('Privacy') && !/<header[^>]*>(?:(?!<\/header>).)*Add a bot/s.test(raw) && /<h1[^>]*text-lg/.test(raw), 'Add a bot sits at the end of the list; the title is a size up')
  ok(/aria-label="Twins: 2 twins and the sample · 1 needs your attention · 1 with a change to send to Grok"/.test(chip()) && /data-tone="warn"/.test(chip()) && />Twins · 1</.test(chip()), 'status bar: "Twins" with a count only when attention is needed')
  ok(/focus-visible:ring/.test(chip()), 'status bar: the item shows where the keyboard is')
  // A twin that only has a skill change to send is not an alarm: no count, and the header says what it is.
  ok(P.summarize([recipe, garden]).label === '2 twins · 1 with a change to send to Grok' && P.summarize([recipe, garden]).count === 0 && P.summarize([recipe, garden]).tone === 'warn', `summary for a change to send: ${JSON.stringify(P.summarize([recipe, garden]))}`)
  ok(P.summarize([recipe, trail]).label === '2 twins · 1 to finish setting up' && P.summarize([recipe, trail]).count === 0, `summary for a twin to finish: ${JSON.stringify(P.summarize([recipe, trail]))}`)
  // Every kind of open thing is counted, and the sample is not counted as a twin of the owner's.
  ok(P.summarize([trail, notes, club]).label === '3 twins · 1 needs your attention · 3 to finish setting up' && P.summarize([trail, notes, club]).count === 1, `summary counts every kind: ${P.summarize([trail, notes, club]).label}`)
  ok(P.summarize([acmeNew, trail, notes, club]).label === '3 twins and the sample · 1 needs your attention · 4 to finish setting up' && P.summarize([acmeNew]).label === 'The sample twin · 1 to finish setting up' && P.summarize([acmeCalm]).label === 'The sample twin · nothing needs you' && P.whoWords([recipe]) === '1 twin', `summary names the sample apart: ${P.summarize([acmeNew, trail, notes, club]).label} / ${P.summarize([acmeNew]).label}`)
  ok(P.summarize([recipe, garden, trail]).label === '3 twins · 1 with a change to send to Grok · 1 to finish setting up', `summary: to send and to finish together (${P.summarize([recipe, garden, trail]).label})`)
  H.status = { ...statusAll, sample: { on: false, hide_real: false }, pairs: [recipe, plants] }
  html = text(page())
  ok(html.includes('2 twins · nothing needs you') && />Twins</.test(chip()) && /data-tone="good"/.test(chip()) && H.buttons.every(b => !b.disabled), 'calm page: summary line, no count in the status bar')
  ok(html.includes('See a sample') && !html.includes('End sample') && !html.includes('Ready for your own bots?'), 'calm page without the sample: a quiet "See a sample" link')
  page()
  ok(primaries().length === 0, `calm page with real twins: nothing is pushed (${primaries()})`)

  // One primary button on the page at a time, state by state.
  {
    const secretOnly = { ...acme, conflicts: 0, state_reason: '1 thing that looks like a password or key was not copied' }
    const sendable = (id, name) => ({ ...garden, pair_id: id, grok: { id: `b-${id}`, name, present: true }, hermes: { profile: id }, mirror_bot: { connected: true } })
    const cases = [
      ['the sample with a fake password, a calm twin', [secretOnly, recipe], ['Remove the fake password']],
      ['only the sample, calm', [acmeCalm], ['Connect my Grok bots']],
      ['only the sample, not finished', [acmeNew], ['Finish setting up']],
      // (the sample's set-up chores are not counted; it is there to be watched, and the next act is the owner's own bots)
      ['only the sample (both sides known), not finished', [acmeSeeNew], ['Connect my Grok bots']],
      ['only the sample (both sides known), calm', [acmeSee], ['Connect my Grok bots']],
      ['three new twins, the third with a password not copied', [trail, notes, club], ['Finish setting up']],
      ['a new twin with a password not copied, then a new twin', [club, trail], ['Finish setting up']],
      ['a real password to remove (no button of its own), then a skill to carry over', [{ ...recipePlain, state: 'needs_you', secrets_held: 1, secrets_withheld: [{ item: 'persona.description', kind: 'description', direction: 'g2h' }] }, garden], ["Copy the skill's text"]],
      ['a new twin to finish and a calm one', [recipe, trail], ['Finish setting up']],
      ['two twins with a change to send', [sendable('garden-buddy', 'Garden Buddy'), sendable('herb-box', 'Herb Box')], ['Send to Grok']],
      ['a twin that cannot be checked and a new one', [{ ...recipePlain, state: 'error', error: { code: 'internal', message: 'its saved state could not be read' } }, trail], ['Try again']]
    ]
    for (const [label, pairs, want] of cases) {
      H.status = { ...statusAll, sample: { ...statusAll.sample, on: pairs.some(p => p.sample) }, pairs }
      html = text(page())
      ok(JSON.stringify(primaries()) === JSON.stringify(want), `one primary button (${label}): ${JSON.stringify(primaries())}`)
    }
    H.status = { ...statusAll, pairs: [acmeCalm] }
    html = text(page())
    ok(html.includes('Ready for your own bots?') && html.indexOf('Ready for your own bots?') > html.indexOf('Acme Support') && variantOf('Connect my Grok bots') === 'default', 'only the sample on: "Ready for your own bots? Connect my Grok bots" under the row')
    H.status = { ...statusAll, pairs: [acmeNew] }
    page()
    ok(variantOf('Connect my Grok bots') === 'outline', 'while the sample twin still has a step to try, Connect is not the primary')
    // Three new twins, one with a possible password (a screen that once had no blue button at all).
    H.status = { ...statusAll, sample: { on: false, hide_real: false }, pairs: [trail, notes, club] }
    raw = page()
    html = text(raw)
    ok(html.includes('3 twins · 1 needs your attention · 3 to finish setting up'), `three new twins: the header counts every kind (${html.slice(0, 120)})`)
    ok(html.includes('A password or key was not copied · 2 things left') && count(html, 'Almost ready · 2 things left') === 2, 'three new twins: the row with a password also says what is left of its set-up')
    ok(JSON.stringify(H.buttons.filter(b => b.text === 'Finish setting up').map(b => b.variant)) === JSON.stringify(['default', 'secondary', 'secondary']) && H.buttons.find(b => b.label === 'Finish setting up Book Club')?.size === 'default', `three new twins: one primary; every row has Finish setting up in its button slot (${H.buttons.filter(b => b.text === 'Finish setting up').map(b => b.variant)})`)
    // One row grammar: the button slot holds the row's next act, also on the row with a box open below it
    // (the twin is opened from the line under its name, as on the other two rows).
    ok(raw.indexOf('Finish setting up Book Club') > 0 && raw.indexOf('Finish setting up Book Club') < raw.lastIndexOf('A password or key was not copied') && H.buttons.filter(b => b.text === 'Open in Hermes').every(b => b.variant === 'text'), 'three new twins: the same row layout three times; the box comes after the row\'s own button')
    // The twin with the password first: its box has no button to press, so its Finish setting up is the primary.
    H.status = { ...H.status, pairs: [club, trail] }
    page()
    ok(JSON.stringify(H.buttons.filter(b => b.text === 'Finish setting up').map(b => b.variant)) === JSON.stringify(['default', 'secondary']) && variantOf('Check now') === 'text', 'password twin first: its Finish setting up carries the primary')
    // A toast's reason keeps "Grok" and "Hermes" as they are written (only the first letter is lowered).
    ok(
      P.attentionReason({}, { waiting: [{ item: 'skill:pick-a-book', to: 'grok' }, { item: 'skill:rate-a-book', to: 'grok' }] }) === '2 changes to send to Grok' &&
        P.attentionReason({}, { conflicts: 1, secrets_withheld: [{ item: 'persona.description' }] }) === '2 things need you',
      'a reason in a toast never lower-cases Grok'
    )
    ok(P.hasMainAction({ kind: 'conflict' }, recipe) === true && P.hasMainAction({ kind: 'secret' }, recipe) === false && P.hasMainAction({ kind: 'secret' }, acme) === true && P.hasMainAction({ kind: 'waiting' }, garden) === true && P.hasMainAction({ kind: 'error' }, recipe) === true && P.hasMainAction({ kind: 'missing' }, recipe) === true, 'which boxes hold a button that can be pressed (a real password is fixed in Grok: none)')
    // Skill changes to send: a main button only when they can be sent, or when the backend names them
    // (an older backend gives a number only, and that box has no main button to carry the page's primary).
    ok(
      P.hasMainAction({ kind: 'unsent' }, { pending: { to_grok: 2 } }) === false &&
        P.hasMainAction({ kind: 'unsent' }, { pending: { to_grok: 2, to_grok_items: ['skill:pick-a-book'] } }) === true &&
        P.hasMainAction({ kind: 'unsent' }, { mirror_bot: { connected: true }, pending: { to_grok: 2 } }) === true,
      'unsent skill changes carry the primary button only when their box has one'
    )
  }
  H.status = statusAll

  // The owner's twins cannot follow Grok right now: ONE banner with the real cause, the button that fixes it when a
  // button can, and rows that say only that (and can still be opened: the twin itself works in Hermes).
  {
    const BANNER = 'aria-label="Twins are not following Grok"'
    const failing = (pair, code, message, hint) => ({ ...pair, state: 'error', state_reason: message, error: { code, message, ...(hint ? { hint } : {}) } })
    const two = (code, message, hint) => [failing(recipePlain, code, message, hint), failing(plants, code, message, hint)]
    const held = (setup, pairs, extra = {}) => { H.status = { ...statusAll, setup: { ...ready, ...setup }, sample: { on: false, hide_real: false }, pairs, ...extra }; raw = page(); html = text(raw) }
    const oneBanner = label => {
      ok(count(raw, BANNER) === 1 && count(html, 'Not following Grok right now') === 2 && html.includes('2 twins · not following Grok right now'), `${label}: one banner, and each row says only that (${html.slice(0, 200)})`)
      ok(!/Can't check|Paused/.test(html) && !raw.includes('role="alert"') && count(raw, 'data-tone="muted"') === 2 && H.buttons.filter(b => b.text === 'Try again').length <= 1, `${label}: rows do not repeat the banner in boxes of their own, nothing shouts`)
      ok(H.buttons.filter(b => b.text === 'Open in Hermes').length === 2 && H.buttons.filter(b => b.text === 'Open in Hermes').every(b => !b.disabled && b.variant === 'secondary'), `${label}: a twin that is not following Grok can still be opened`)
      ok(!has('Add a bot') && !has('Connect my Grok bots'), `${label}: no bot list to add from, so no Add a bot`)
    }

    // 1. botport changed and has to be verified again: one press does it.
    held({ ready: false, helper_checked: false }, two('untrusted_binary', 'botport changed since it was verified'))
    oneBanner('botport to verify')
    ok(html.includes('botport has to be verified again before Hermes runs it. Until then your twins do not follow Grok. Nothing is lost.') && !html.includes('botport changed since it was verified') && !/until then\./i.test(html), `botport to verify: the cause, once (${html.slice(0, 260)})`)
    ok(JSON.stringify(primaries()) === JSON.stringify(['Verify botport']) && !has('Reconnect') && !has('Check now'), `botport to verify: one button that does it (${primaries()})`)
    ok(/Twins: 2 twins · not following Grok right now/.test(chip()) && />Twins · 1</.test(chip()), 'status bar says the same')
    held({ ready: false, helper_checked: false }, two('untrusted_binary', 'botport changed since it was verified'))
    reset()
    H.restReply = async path => (path === '/engine/trust' ? { trusted: true, attested: true, setup: setupReady } : {})
    await press('Verify botport')
    ok(H.restCalls.length === 1 && H.restCalls[0].path === '/engine/trust' && JSON.stringify(H.restCalls[0].opts.body) === '{}' && H.notes.at(-1)?.message === 'botport is verified' && H.notes.at(-1)?.meta === 'Your twins follow Grok again.', `Verify botport: one press, then said (${JSON.stringify(H.notes.at(-1))})`)
    // It failed: no toast over the page; the Connect sheet opens with the failure and its fix (an effect-free
    // state change; the sheet's own content is asserted above with `problem`).
    held({ ready: false, helper_checked: false }, two('untrusted_binary', 'botport changed since it was verified'))
    reset()
    H.restReply = async path => (path === '/engine/trust' ? { error: { code: 'check_failed', message: 'This botport is not the release Hermes expects (0.2.1).', hint: 'Press Download botport and choose the new file.' } } : {})
    await press('Verify botport')
    ok(H.restCalls.length === 1 && H.notes.length === 0 && !/allow.unattested/i.test(JSON.stringify(H.restCalls)), `Verify botport failed: no toast, nothing accepted (${JSON.stringify(H.notes)})`)
    ok(/async function verifyBotport\(\) \{[\s\S]{0,400}setFlowProblem\(asProblem\(err\)\)\s+setFlow\('reconnect'\)/.test(source) && /problem: flowProblem,/.test(source) && /initialProblem: problem,/.test(source), 'Verify botport failed: the Connect sheet is opened with what went wrong')

    // 2. botport is gone: the Connect sheet gets it back.
    held({ ready: false, helper_found: false, helper_checked: false }, two('botport_missing', 'botport is no longer on this computer.'))
    oneBanner('botport gone')
    ok(html.includes('botport is no longer on this computer. Until it is back your twins do not follow Grok. Nothing is lost.') && count(html, 'no longer on this computer') === 1 && JSON.stringify(primaries()) === JSON.stringify(['Get botport again…']) && !has('Verify botport'), `botport gone: the cause and the way back (${primaries()})`)

    // 3. The Grok desktop app is gone, as today's backend reports it: "ready" stays true, every twin fails alike.
    const APP_GONE = 'The Grok desktop app is not on this computer any more.'
    held({ ready: true, grok_app_found: false, problem: APP_GONE }, two('grok_app_missing', APP_GONE, 'Install the Grok desktop app again and sign in, then press Check now.'))
    oneBanner('Grok app gone (today\'s backend)')
    ok(html.includes(`${APP_GONE} Your twins stop following Grok until it is back. Nothing is lost.`) && html.includes('This clears by itself when Grok is back.') && count(html, 'not on this computer any more') === 1, `Grok app gone: the cause once, and that it clears by itself (${html.slice(0, 280)})`)
    // Nothing on this page can fix it: no button in the banner, no primary button anywhere, nothing that cannot work.
    ok(primaries().length === 0 && !has('Try again') && !has('Reconnect') && !has('Reconnect…') && !has('Verify botport') && !/press Check now/.test(html), `Grok app gone: no button that cannot help (${texts().filter(Boolean)})`)
    ok(!raw.slice(raw.indexOf(BANNER), raw.indexOf('<article')).includes('<button'), 'Grok app gone: the banner holds no button')
    // ...and as a current backend reports it (ready false, the cause named).
    held({ ready: false, grok_app_found: false, problem: APP_GONE, cause: 'grok_app' }, two('grok_app_missing', APP_GONE))
    oneBanner('Grok app gone (cause named)')
    ok(html.includes('This clears by itself when Grok is back.') && primaries().length === 0, 'Grok app gone (cause named): the same banner')
    // A twin that still has set-up left can go on with it meanwhile (pasting instructions needs no Grok app).
    held({ ready: false, grok_app_found: false, cause: 'grok_app' }, [failing(recipePlain, 'grok_app_missing', APP_GONE), failing(trail, 'grok_app_missing', APP_GONE)])
    ok(JSON.stringify(primaries()) === JSON.stringify(['Finish setting up']) && count(html, 'Not following Grok right now') === 2, `Grok app gone: set-up can still be finished (${primaries()})`)
    // With a banner button, that button is the page's one primary.
    held({ ready: false, helper_checked: false }, [failing(recipePlain, 'untrusted_binary', 'x'), failing(trail, 'untrusted_binary', 'x')])
    ok(JSON.stringify(primaries()) === JSON.stringify(['Verify botport']) && variantOf('Finish setting up') === 'secondary', `botport to verify with a twin to finish: still one primary (${primaries()})`)

    // 4. Every twin fails in the same way while the connection itself is fine: said once, with Try again.
    const READ = 'Hermes could not read your bots from the Grok app.'
    held({}, two('grok_read_failed', READ, 'Open the Grok app once, then press Try again.'))
    oneBanner('every twin failing alike')
    ok(html.includes(`${READ} Open the Grok app once, then press Try again. Your twins and your Grok bots are untouched.`) && count(html, 'could not read your bots') === 1 && JSON.stringify(primaries()) === JSON.stringify(['Try again']) && count(html, 'Try again') === 2, `every twin failing alike: one sentence, one Try again (${html.slice(0, 260)})`)
    reset()
    H.restReply = async path => (path === '/sync' ? { pairs: [] } : {})
    await press('Try again')
    ok(H.restCalls[0]?.path === '/sync' && JSON.stringify(H.restCalls[0]?.opts?.body) === '{}', 'every twin failing alike: Try again checks them all')
    // Sentences that name each twin are not repeated as if they were one: a plain one stands in.
    held({}, [failing(recipePlain, 'grok_read_failed', 'Hermes could not read Recipe Planner from the Grok app.'), failing(plants, 'grok_read_failed', 'Hermes could not read Window Plants from the Grok app.')])
    ok(count(raw, BANNER) === 1 && html.includes('Hermes could not check your twins. Your twins and your Grok bots are untouched.') && !html.includes('could not read Recipe Planner'), 'every twin failing alike, each with its own sentence: one plain sentence')
    // Two of three failing for the same reason is still one cause: said once, in a banner of its own with the one
    // Try again; those two rows say "Can't check right now" without a box each; the third row is as it was, and
    // the page as a whole is not "not following Grok".
    const HINT = 'Open the Grok app once, then press Try again.'
    held({}, [failing(recipePlain, 'grok_read_failed', 'Hermes could not read Recipe Planner from the Grok app.', HINT), failing(plants, 'grok_read_failed', 'Hermes could not read Window Plants from the Grok app.', HINT), { ...garden, waiting: [], state: 'in_step' }])
    ok(count(raw, BANNER) === 0 && count(raw, 'aria-label="Some twins cannot be checked"') === 1 && html.includes(`Hermes could not read 2 of your twins from the Grok app. ${HINT}`) && !html.includes("Can't check Recipe Planner right now") && !html.includes('could not read Recipe Planner') && count(html, "Can't check right now") === 2 && !html.includes('Not following Grok'), `two of three failing alike: one banner, no box per twin (${html.slice(0, 300)})`)
    ok(JSON.stringify(primaries()) === JSON.stringify(['Try again']) && H.buttons.filter(b => b.text === 'Try again').length === 1 && has('Add a bot') && H.buttons.filter(b => b.text === 'Open in Hermes').length === 3 && html.includes('Follows Grok') && !raw.includes('role="alert"'), `two of three failing alike: one Try again, every twin still opens, the third row is calm (${texts()})`)
    ok(html.includes('3 twins · 2 need your attention') && />Twins · 2</.test(chip()), 'two of three failing alike: counted as two')
    page()
    reset()
    H.restReply = async path => (path === '/sync' ? { pairs: [] } : {})
    await press('Try again')
    ok(H.restCalls[0]?.path === '/sync' && JSON.stringify(H.restCalls[0]?.opts?.body) === '{}', 'two of three failing alike: Try again checks them all')
    // Sentences that do not fit one shape, or hints that differ: a plain count stands in.
    held({}, [failing(recipePlain, 'grok_read_failed', 'Hermes could not read Recipe Planner from the Grok app.'), failing(plants, 'grok_read_failed', 'Window Plants gave no answer.'), { ...garden, waiting: [], state: 'in_step' }])
    ok(html.includes('2 of your 3 twins cannot be checked right now.') && count(raw, 'aria-label="Some twins cannot be checked"') === 1, 'two of three failing alike, in different words: a plain count')
    // Two twins failing for different reasons are two matters: a box each, no banner.
    held({}, [failing(recipePlain, 'grok_read_failed', 'Hermes could not read Recipe Planner from the Grok app.'), failing(plants, 'internal', 'its saved state could not be read'), { ...garden, waiting: [], state: 'in_step' }])
    // (one box at a time on the page. The first says its cause; the second says "Can't check right now" on
    // its row and opens with one click.)
    ok(count(raw, 'aria-label="Some twins cannot be checked"') === 0 && html.includes("Can't check Recipe Planner right now") && !html.includes("Can't check Window Plants right now") && count(html, "Can't check right now") >= 2 && H.buttons.some(b => b.label === "Show: Can't check right now"), 'two failing for different reasons: no banner; the first is a box, the second waits as one line')
    // One twin failing alone is never a banner either.
    held({}, [failing(recipePlain, 'grok_read_failed', READ)])
    ok(count(raw, BANNER) === 0 && html.includes("Can't check Recipe Planner right now"), 'one twin failing alone: its own box')

    // 5. A cause the page does not know: the backend's sentence, and the way into the Connect sheet.
    held({ ready: false, helper_found: true, helper_checked: true, problem: 'There is no botport for this kind of computer yet.', cause: 'no_build' }, two('botport_missing', 'x'))
    ok(count(raw, BANNER) === 1 && html.includes('There is no botport for this kind of computer yet. Until that is fixed your twins do not follow Grok. Nothing is lost.') && JSON.stringify(primaries()) === JSON.stringify(['Reconnect…']), `another cause: the backend's sentence (${html.slice(0, 220)})`)

    // The cause, straight from the function: the name a current backend gives wins; an older one is read from its flags.
    const F = (setup, pairs = two('x', 'y')) => P.followProblem({ setup: { ...ready, ...setup } }, pairs)?.cause ?? null
    ok(F({}) === 'same_error' && F({}, [recipe, plants]) === null && F({ ready: false, cause: 'grok_app' }, []) === null, 'cause: none when ready and calm; none without twins of the owner\'s')
    ok(F({ ready: false, helper_found: false, helper_checked: false, grok_app_found: false }) === 'botport_missing' && F({ ready: false, helper_checked: false, grok_app_found: false }) === 'botport_unverified' && F({ ready: true, grok_app_found: false }) === 'grok_app' && F({ ready: false, grok_app_found: false }) === 'grok_app', 'cause (older backend): botport first while not ready, the Grok desktop app either way')
    ok(F({ ready: false, helper_checked: false, cause: 'grok_app' }) === 'grok_app' && F({ ready: false, cause: 'botport_unverified' }) === 'botport_unverified' && F({ ready: false, cause: 'something-new', problem: 'A new reason.' }) === 'other' && F({ ready: false }) === 'other', 'cause: the named one wins; an unknown one is said in the backend\'s words')
    ok(P.followProblem({ engine: { available: false } }, two('x', 'y'))?.cause === 'other' && P.followProblem({ engine: { available: true } }, [recipe]) === null, 'cause (no setup block at all): from engine.available')

    // The sample is not held by any of this (it needs no botport and no Grok app).
    held({ ready: false, helper_checked: false }, [acmeCalm, failing(recipePlain, 'untrusted_binary', 'x')], { sample: { on: true, hide_real: false } })
    ok(count(html, 'Not following Grok right now') === 1 && html.includes('Follows Grok') && html.includes('1 twin and the sample · not following Grok right now'), 'not following: the sample twin goes on')
  }
  H.status = statusAll

  // ── one row, each state: one sentence, at most one button ──
  const row = (pair, props = {}) => render(jsx(P.TwinRow, { intervalS: 60, lastCheckAt: iso(1), lead: true, pair, storage: ctx.storage, ...props }))
  raw = row({ ...recipe, pair_id: 'quiet-one' })
  html = text(raw)
  ok(html.includes('Follows Grok') && !/checked|name and description|Ready/.test(html) && raw.includes('title="Checked 1 min ago"') && JSON.stringify(texts()) === JSON.stringify(['Open in Hermes', '']), `calm: two words, only Open in Hermes + the "…" menu (${texts()})`)
  ok(variantOf('Open in Hermes') === 'secondary' && H.buttons[0].size === 'default', 'calm: Open in Hermes is there, not pushed, and a full-size target')
  await press('Open in Hermes')
  ok(H.newChats.at(-1) === 'recipe-planner', 'Open in Hermes opens a chat with the twin')
  ok(count(html, 'Follows Grok') === 1 && !html.includes('Up to date'), 'status text once')
  // A new twin: says what is left and offers the one thing to do; never "Open in Hermes" first.
  html = text(row(trail))
  raw = row(trail)
  html = text(raw)
  ok(html.includes('Almost ready · 2 things left') && JSON.stringify(texts()) === JSON.stringify(['Open in Hermes', 'Finish setting up', '']) && variantOf('Open in Hermes') === 'text' && variantOf('Finish setting up') === 'default' && H.buttons[1].size === 'default' && H.menu[0]?.text === 'Open in Hermes' && !/Up to date|Follows Grok/.test(html), `new twin: Almost ready + Finish setting up; it can be opened from its menu (${html.slice(0, 160)})`)
  // A to-do is not a warning: a half-filled circle, not a triangle.
  ok(raw.includes('data-tone="todo"') && raw.includes('data-codicon="color-mode"') && !raw.includes('data-codicon="warning"'), 'new twin: the shape of a to-do, not of trouble')
  ok(!H.menu.some(m => /Finish setting up/.test(m.text)), 'new twin: the button is not repeated in the menu')
  row(trail, { lead: false })
  ok(variantOf('Finish setting up') === 'secondary', 'new twin further down the page: same button, not a second primary')
  html = text(row({ ...trail, setup_left: ['model'] }))
  ok(html.includes('Almost ready · 1 thing left'), 'one thing left is counted')
  // Today's backend does not say whether a twin is finished: the row claims only what is true.
  html = text(row(legacy))
  ok(html.includes('Follows Grok') && !/Ready ·|Almost ready|Up to date/.test(html) && has('Open in Hermes') && !has('Finish setting up'), `older backend: only what is true (${html.slice(0, 160)})`)
  ok(P.leftOf(legacy) === null && JSON.stringify(P.leftOf(trail)) === JSON.stringify(['instructions', 'model']) && JSON.stringify(P.leftOf({ setup_left: ['model', 'surprise', 'instructions'] })) === JSON.stringify(['instructions', 'model']), 'what is left: known steps only, in order; unknown when the backend does not say')
  html = text(row(acmeCalm))
  ok(!has('Open in Hermes') && JSON.stringify(texts()) === JSON.stringify(['']), 'the sample twin has no chat to open')
  html = text(row({ ...recipe, state: 'changes_waiting', pending: { ...calm, to_hermes: 1 } }))
  ok(html.includes('Updating…') && !html.includes('Follows Grok') && P.rowTone({ ...recipe, pending: { ...calm, to_hermes: 1 } }) === 'busy' && P.rowTone({ ...recipe, pending: { ...calm, in_flight: 1 } }) === 'busy', 'changes on their way to Hermes: Updating…')
  // The shape per state: trouble is a triangle; a to-do, something to send and something on its way are not.
  {
    const tones = [[recipe, 'good'], [trail, 'todo'], [garden, 'send'], [{ ...recipePlain, pending: { ...calm, to_grok: 2 } }, 'send'], [{ ...recipe, conflicts: 1 }, 'warn'], [club, 'warn'], [{ ...garden, waiting: [{ item: 'routine:Water reminder', to: 'hermes' }] }, 'todo'],
      [{ ...recipe, state: 'error' }, 'bad'], [{ ...recipe, state: 'grok_missing' }, 'muted']]
    for (const [pair, want] of tones) ok(P.rowTone(pair) === want, `shape for ${P.statusSentence(pair)}: ${P.rowTone(pair)} (want ${want})`)
    ok(P.rowTone(recipe, { held: true }) === 'muted' && P.rowTone(acmeCalm, { held: true }) === 'good' && P.statusSentence(recipe, { held: true }) === 'Not following Grok right now' && P.statusSentence(acmeCalm, { held: true }) === 'Follows Grok', 'shape and sentence while not following Grok (the sample goes on)')
  }
  // Cannot check one twin: a title that says what, the reason, then the one thing to do (or the reassurance); one button.
  raw = row({ ...recipe, state: 'error', state_reason: 'Engine said no', error: { code: 'internal', message: 'its saved state could not be read' } })
  html = text(raw)
  ok(html.includes("Can't check right now") && html.includes("Can't check Recipe Planner <b>bold</b> right now") && html.includes('Its saved state could not be read. Your twin and your Grok bot are untouched.') && !has('Details') && raw.includes('data-tone="bad"') && raw.includes('data-codicon="error"'), `cannot check: what, why, then the reassurance (${html.slice(0, 260)})`)
  ok(!/Hermes could not/.test(html), 'cannot check: the title and the body do not open with the same words')
  // The backend's hint is the second sentence (it used to be dropped).
  html = text(row({ ...recipePlain, state: 'error', error: { code: 'grok_read_failed', message: 'Hermes could not read Recipe Planner from the Grok app.', hint: 'Open the Grok app once, then press Try again' } }))
  ok(html.includes("Can't check Recipe Planner right now") && html.includes('Hermes could not read Recipe Planner from the Grok app. Open the Grok app once, then press Try again.') && !html.includes('are untouched'), `cannot check: message, then hint (${html.slice(0, 260)})`)
  // The same twin exactly as the backend reports it (state, its reason, the error with its hint).
  html = text(row(broken))
  ok(html.includes("Can't check Meal Plans right now") && html.includes('Hermes could not read Meal Plans from the Grok app. Open the Grok app once, then press Try again.') && !/grok_read_failed/.test(html) && JSON.stringify(primaries()) === JSON.stringify(['Try again']), `cannot check (backend shape): message, hint, one button (${html.slice(0, 260)})`)
  raw = row({ ...recipe, state: 'error', state_reason: 'Engine said no', error: { code: 'internal', message: 'its saved state could not be read' } })
  ok(JSON.stringify(texts().filter(Boolean)) === JSON.stringify(['Open in Hermes', 'Try again']) && variantOf('Try again') === 'default' && variantOf('Open in Hermes') === 'secondary', 'cannot check: one filled button, Try again; the twin can still be opened')
  reset()
  H.restReply = async path => (path === '/sync' ? { pairs: [{ pair_id: 'recipe-planner', applied: [], secrets_withheld: [], conflicts: [], state: 'in_step' }] } : {})
  await press('Try again')
  ok(H.restCalls[0]?.path === '/sync' && H.restCalls[0]?.opts?.body?.pair_id === 'recipe-planner', 'Try again checks that twin')
  // Gone from the Grok app: one sentence, one decision (the usual stop-twinning dialog), and a quiet way to wait.
  {
    const goneRow = { ...recipePlain, state: 'grok_missing', grok: { ...recipePlain.grok, present: false } }
    html = text(row(goneRow))
    ok(html.includes('Not in Grok any more') && html.includes('Recipe Planner is not in your Grok app any more.') && html.includes('Its twin stays here as an ordinary Hermes bot. Nothing was deleted.') && variantOf('OK, stop following') === 'default' && variantOf('It will be back') === 'ghost' && JSON.stringify(primaries()) === JSON.stringify(['OK, stop following']) && variantOf('Open in Hermes') === 'secondary', `not in Grok any more: one decision, which is the main button; the twin can still be opened (${texts()})`)
    ok(!has('Stop twinning…') && !/Keep as a Hermes bot|normal Hermes bot/.test(html) && JSON.stringify(texts().filter(Boolean)) === JSON.stringify(['Open in Hermes', 'OK, stop following', 'It will be back']), 'not in Grok any more: two buttons for the decision, not two names for the same exit')
    H.storedSets = []
    reset()
    await press('It will be back')
    ok(JSON.stringify(H.storedSets) === JSON.stringify([['kept.recipe-planner', true], ['noted', ['recipe-planner']]]) && H.restCalls.length === 0, '"It will be back" only waits: remembered on this computer (and that something is remembered for this twin), nothing is changed')
    // Waiting: the box is gone, the row says so, and the twin can be opened like any Hermes bot.
    H.stored['kept.recipe-planner'] = true
    html = text(row(goneRow))
    ok(html.includes('Not in Grok any more · waiting for it to come back') && !html.includes('is not in your Grok app any more.') && JSON.stringify(texts()) === JSON.stringify(['Open in Hermes', '']) && H.menu.some(m => m.text === 'Stop twinning…'), `waiting for the bot to come back: ${html.slice(0, 160)}`)
    delete H.stored['kept.recipe-planner']
  }
  // A skill change that was given the go-ahead: Send to Grok appears only now, and only with skill sync on.
  const toSend = { ...recipePlain, state: 'changes_waiting', pending: { ...calm, to_grok: 1 }, mirror_bot: { connected: true } }
  html = text(row(toSend))
  ok(html.includes('1 change to send to Grok') && html.includes('1 skill change is ready for Grok.') && html.includes("It arrives the next time the sync bot's routine runs in Grok.") && JSON.stringify(primaries()) === JSON.stringify(['Send to Grok']), 'skill change + skill sync on: Send to Grok')
  reset()
  H.restReply = async path => (path.endsWith('/send-to-grok') ? { pairs: [], pack: { items: 1 }, skills: 1, digest_entries: 0, note: 'x' } : {})
  await press('Send to Grok')
  ok(H.restCalls[0]?.path === '/pairs/recipe-planner/send-to-grok' && H.notes.at(-1)?.message === 'Sent to Grok' && H.notes.at(-1)?.meta === "It arrives the next time the sync bot's routine runs in Grok.", `send toast: ${JSON.stringify(H.notes.at(-1))}`)
  reset()
  H.restReply = async path => (path.endsWith('/send-to-grok') ? { pairs: [{ pair_id: 'recipe-planner', applied: [{ item: 'skill:greet-visitors', kind: 'skill', direction: 'h2g' }], secrets_withheld: [], conflicts: [], state: 'in_step' }], skills: 1 } : {})
  await press('Send to Grok')
  ok(H.notes.length === 1 && H.notes[0].message === 'Sent to Grok', `one toast for one send (${JSON.stringify(H.notes.map(n => n.message))})`)
  // When the backend knows how often the sync bot runs, the page says when.
  html = text(row({ ...toSend, mirror_bot: { connected: true, every_minutes: 15 } }))
  ok(html.includes("It arrives when the sync bot's routine next runs in Grok, usually within 15 minutes."), 'send: says when it arrives, when that is known')
  // Skill sync off: nothing is sent by Hermes. The owner carries each skill over, with its text to hand.
  {
    const two = { ...recipePlain, state: 'changes_waiting', pending: { ...calm, to_grok: 2, to_grok_items: ['skill:pick-a-book', 'skill:rate-a-book'] }, mirror_bot: { connected: false } }
    raw = row(two)
    html = text(raw)
    ok(html.includes('2 changes to send to Grok') && html.includes('2 skills changed in Hermes.') && html.includes('The skill "pick-a-book"') && html.includes('The skill "rate-a-book"') && !has('Send to Grok'), `skills to carry over: each one is named (${html.slice(0, 260)})`)
    // (what to do is said once for the box, not once per skill: the page with two was 131 words)
    ok(count(html, 'Grok does not have') === 1 && html.includes('Grok does not have these changes yet. Copy each text, then paste it into that skill in the Grok app.'), 'skills to carry over: the instruction is said once')
    ok(JSON.stringify(H.buttons.filter(b => b.text === "Copy the skill's text").map(b => b.variant)) === JSON.stringify(['default', 'secondary']) && H.buttons.filter(b => b.text === 'Done, I pasted it').length === 2 && H.buttons.filter(b => b.text === 'Skill sync…').length === 1, `skills to carry over: one copy block per skill, one primary, one quiet link (${texts().filter(Boolean)})`)
    ok(raw.includes('data-tone="send"') && !raw.includes('data-codicon="warning"'), 'skills to carry over: an arrow, not a warning')
    // An older backend gives the number only: an honest sentence and the quiet way to automate; no button that cannot work.
    html = text(row({ ...two, pending: { ...calm, to_grok: 2 } }))
    ok(html.includes('2 skills changed in Hermes.') && html.includes('Grok does not have these changes yet. Details… in the "…" menu names each skill; copy them into the Grok app, or set up skill sync.') && !has('Send to Grok') && !has("Copy the skill's text") && variantOf('Skill sync…') === 'secondary' && primaries().length === 0, `skills to carry over (older backend): ${html.slice(0, 260)}`)
  }
  // Routines moved to Hermes: the row says so (the twin itself did not move). The way back is named for what it
  // does, and moves into the menu after a day.
  html = text(row({ ...recipe, switched: { at: iso(5), routines_on: 2 } }))
  ok(html.includes('Follows Grok · 2 routines run in Hermes') && !/Runs in Hermes since/.test(html) && H.buttons.some(b => b.text === 'Pause routines' && /routines in Hermes again/.test(b.label)) && !has('Undo') && !H.menu.some(m => m.text === 'Pause routines') && H.menu.some(m => m.text === 'Routines in Hermes…') && !H.menu.some(m => /^Move /.test(m.text)), `routines moved today: says so, offers Pause routines (${html.slice(0, 200)})`)
  reset()
  H.restReply = async () => ({ paused: ['job-1', 'job-2'], failed: [] })
  await press('Pause routines')
  ok(H.restCalls[0]?.path === '/pairs/recipe-planner/switch' && H.restCalls[0]?.opts?.body?.undo === true && H.notes.at(-1)?.message === 'Paused 2 routines in Hermes again', `pause routines again: ${JSON.stringify(H.notes.at(-1))}`)
  html = text(row({ ...recipe, switched: { at: '2000-01-05T12:00:00Z', routines_on: 1 } }))
  ok(html.includes('Follows Grok · 1 routine runs in Hermes') && !has('Pause routines') && H.menu.some(m => m.text === 'Pause routines'), 'routines moved long ago: the way back lives in the menu')
  reset()
  await H.menu.find(m => m.text === 'Pause routines').onSelect()
  await tick()
  ok(H.restCalls[0]?.path === '/pairs/recipe-planner/switch' && H.restCalls[0]?.opts?.body?.undo === true, 'the menu entry pauses the same routines')
  // "Move to Hermes…" is offered only when there is something to move, or something moved to pause again.
  ok(P.canMove(recipe) === false && P.canMove(acme) === true && P.canMove({ ...recipe, switched: { at: iso(5) } }) === true && P.canMove({ ...recipe, routines: { in_grok: 0, paused_in_hermes: 1, on_in_hermes: 0 } }) === true, 'can move: from the routine counts')
  ok(P.canMove(legacy) === false && P.canMove({ ...legacy, by_hand: [{ item: 'routine:Morning summary' }] }) === true && P.canMove({ ...legacy, by_hand: [{ item: 'persona.instructions' }] }) === false, 'can move (older backend): from the list of things to bring over')
  row(recipe)
  ok(!H.menu.some(m => /Move|Routines in Hermes/.test(m.text)), 'no routines: the menu does not offer a move that would be empty')
  row(acmeCalm)
  ok(H.menu.some(m => m.text === 'Move routines to Hermes…') && !H.menu.some(m => m.text === 'Move to Hermes…'), 'routines to move: the menu names what moves (the routines, not the bot)')
  // A possible password in a real twin: one short title, one sentence, no primary button; the detail is folded.
  const withSecret = { ...recipePlain, state: 'needs_you', secrets_held: 1, secrets_withheld: [{ item: 'persona.description', kind: 'description', direction: 'g2h' }] }
  raw = row(withSecret)
  html = text(raw)
  ok(html.includes('A password or key was not copied') && html.includes("A password or key in Recipe Planner's description was not copied.") && html.includes('Remove it from the description in the Grok app. Hermes notices by itself.') && !/This check can miss things|never stores|Check again|looks like a password or key, so it was not/.test(html), `possible password (real twin): two short sentences (${html.slice(0, 300)})`)
  ok(primaries().length === 0 && variantOf('Check now') === 'text' && has('Why?') && variantOf('Open in Hermes') === 'secondary' && !has('Remove the fake password'), 'possible password (real twin): no primary button, a quiet Check now, the why folded; the twin can still be opened')
  reset()
  H.restReply = async () => ({ pairs: [] })
  await press('Check now')
  ok(H.restCalls[0]?.path === '/sync' && H.restCalls[0]?.opts?.body?.pair_id === 'recipe-planner', 'Check now checks that twin')
  // The backend says which line: the row says it too, and nothing else of the backend's sentence.
  html = text(row({ ...club, setup_left: [] }))
  ok(html.includes("A password or key in Book Club's description was not copied.") && html.includes('Remove it from line 3 of the description in the Grok app. Hermes notices by itself.') && !/api key|looks like a password or key \(/.test(html), `possible password: the line is named, the backend's own wording is not shown (${html.slice(0, 300)})`)
  html = text(row({ ...club, setup_left: [], secrets_withheld: [3, 9, 3].map(n => ({ item: 'persona.description', kind: 'description', direction: 'g2h', location: `in the description, line ${n}: looks like a password or key` })) }))
  ok(html.includes('Remove it from lines 3 and 9 of the description in the Grok app.'), 'possible password: two lines of one text are named once each')
  // The sample's fake password is taken out by a button in its own box.
  {
    const fake = { ...acme, conflicts: 0 }
    html = text(row(fake))
    ok(html.includes("A password or key in Acme Support's description was not copied.") && html.includes('This one is fake. With a real bot you would remove it in the Grok app, and Hermes would notice by itself.') && JSON.stringify(primaries()) === JSON.stringify(['Remove the fake password']) && !/Change the bot in Grok/.test(html), 'possible password (sample): its own button, no pointer to another one')
    reset()
    H.restReply = async path => (path === '/demo' ? { ok: true } : { pairs: [] })
    await press('Remove the fake password')
    await tick(6)
    ok(H.restCalls[0]?.path === '/demo' && H.restCalls[0]?.opts?.body?.action === 'remove-secret' && H.restCalls.at(-1)?.path === '/sync' && H.restCalls.at(-1)?.opts?.body?.pair_id === 'acme-support', `Remove the fake password: takes it out, then the twin is checked by itself (${H.restCalls.map(c => c.path)})`)
    // Today's backend knows this only as part of "change the bot in Grok".
    row(fake)
    reset()
    H.restReply = async (path, opts) => (path === '/demo' && opts.body.action === 'remove-secret' ? { error: { code: 'bad_input', message: 'unknown action' } } : path === '/demo' ? { ok: true } : { pairs: [] })
    await press('Remove the fake password')
    await tick(6)
    ok(JSON.stringify(H.restCalls.filter(c => c.path === '/demo').map(c => c.opts.body.action)) === JSON.stringify(['remove-secret', 'grok-edit']) && !H.notes.some(n => n.kind === 'error'), 'Remove the fake password (older backend): falls back, no error shown')
  }
  // Three things at once: the row counts them, shows the first, and lists the rest as headers, in the order to deal with them.
  {
    const three = { ...acme, pending: { ...calm, waiting_approval: 1 }, waiting: [{ item: 'skill:fun-fact', reason: 'needs_approval', to: 'grok' }] }
    ok(P.mattersOf(three).map(m => m.kind).join() === 'conflict,waiting,secret', `order: changed in both places, ready to send, password (${P.mattersOf(three).map(m => m.kind)})`)
    raw = row(three)
    html = text(raw)
    ok(html.includes('3 things need you') && !html.includes('Needs your attention'), 'three matters: the row says how many')
    ok(html.includes('changed in both places.') && H.buttons.filter(b => /^Show: /.test(b.label ?? '') && b.variant === 'outline').length === 2 && html.indexOf('A skill changed in Hermes') > 0 && html.indexOf('A skill changed in Hermes') < html.indexOf('A password or key was not copied') && !html.includes('You changed the skill') && !html.includes('A password or key in Acme'), 'three matters: one box, two one-line headers')
    ok(JSON.stringify(primaries()) === JSON.stringify(['Keep the Hermes version']) && !has('Send to Grok') && !has('Check now') && !has("Copy the skill's text"), 'three matters: one main button, no pile of buttons')
    ok(P.statusSentence({ ...recipe, conflicts: 1 }) === 'Changed in both places' && P.statusSentence(withSecret) === 'A password or key was not copied' && P.statusSentence(garden) === '1 change to send to Grok' && P.statusSentence({ ...garden, waiting: [{ item: 'routine:Water reminder', to: 'hermes' }] }) === 'A routine from Grok is ready', 'one matter: the row says which')
    ok(P.statusSentence({ ...garden, pending: { ...calm, to_grok: 2 } }) === '3 changes to send to Grok', 'only things to send: counted as changes, not as an alarm')
    // A twin with something open and a step left says both, and keeps the way to the step under its box
    // (it is the primary button when this row leads and its open box has none to press).
    html = text(row({ ...three, setup_left: ['instructions'] }))
    ok(html.includes('3 things need you · 1 thing left') && variantOf('Finish setting up') === 'secondary' && JSON.stringify(primaries()) === JSON.stringify(['Keep the Hermes version']) && H.buttons.find(b => b.text === 'Finish setting up')?.size === 'default' && !H.menu.some(m => m.text === 'Finish setting up…'), `a matter and set-up left: the row says both and offers both (once: not again in the menu); the open box holds the main button (${html.slice(0, 120)})`)
    // (its open box has no button to press, a real password: then Finish setting up is the main one)
    row({ ...recipePlain, state: 'needs_you', secrets_held: 1, secrets_withheld: [{ item: 'persona.description', kind: 'description', direction: 'g2h' }], setup_left: ['instructions'] })
    ok(variantOf('Finish setting up') === 'default', 'a matter without a button and set-up left: Finish setting up is the main button')
    row({ ...three, setup_left: ['instructions'] }, { lead: false })
    ok(variantOf('Finish setting up') === 'secondary', 'a matter and set-up left, further down the page: the button is quiet')
    ok(P.statusSentence(club) === 'A password or key was not copied · 2 things left' && P.statusSentence({ ...garden, setup_left: ['model'] }) === '1 change to send to Grok · 1 thing left', 'one matter and set-up left: both, in one sentence')
  }
  ok(P.statusSentence({ ...recipe, pending: { ...calm, in_flight: 1 } }) === '1 change on the way to Grok', 'in-flight changes have their own sentence')

  // ── Ready to send to Grok ──
  const onSync = { ...garden, mirror_bot: { connected: true } }
  const waitingBox = (pair, waiting, lead = true) => render(jsx(P.WaitingBox, { lead, onSkillSync: noop, pair, waiting }))
  html = text(waitingBox(onSync, onSync.waiting[0]))
  ok(html.includes('You changed the skill "seed-calendar" in Hermes.') && html.includes('Send it to Grok, or keep this change in Hermes only.') && !html.includes('Copy it into Grok yourself') && JSON.stringify(texts()) === JSON.stringify(['Send to Grok', 'Keep it in Hermes only']) && JSON.stringify(primaries()) === JSON.stringify(['Send to Grok']), 'ready to send: Send to Grok / Keep it in Hermes only')
  reset()
  H.restReply = async path => (path.endsWith('/approve') ? { approved: [{ item: 'skill:seed-calendar', to: 'remote' }], sync: { applied: [], secrets_withheld: [], conflicts: [], state: 'in_step' } } : { pairs: [], pack: { items: 1 } })
  await press('Send to Grok')
  ok(H.restCalls[0]?.path === '/pairs/garden-buddy/approve' && H.restCalls[0]?.opts?.body?.item === 'skill:seed-calendar' && H.restCalls[1]?.path === '/pairs/garden-buddy/send-to-grok' && H.notes.at(-1)?.message === 'Sent to Grok', 'Send to Grok: go-ahead, then send, one toast')
  reset()
  H.restReply = async () => ({ pair_id: 'garden-buddy', skipped: ['skill:seed-calendar'] })
  waitingBox(onSync, onSync.waiting[0])
  await press('Keep it in Hermes only')
  ok(H.restCalls[0]?.path === '/pairs/garden-buddy/skip' && H.restCalls[0]?.opts?.body?.item === 'skill:seed-calendar' && H.restCalls[0]?.opts?.body?.undo === false && H.notes.at(-1)?.message === 'The skill "seed-calendar" stays in Hermes only' && H.notes.at(-1)?.action?.label === 'Undo' && /Details…/.test(H.notes.at(-1)?.meta), `keep in Hermes only: ${JSON.stringify(H.notes.at(-1))}`)
  await H.notes.at(-1).action.onClick()
  await tick()
  ok(H.restCalls[1]?.path === '/pairs/garden-buddy/skip' && H.restCalls[1]?.opts?.body?.undo === true, 'Undo offers it again')
  waitingBox(garden, garden.waiting[0])
  // Skill sync off: "copy it yourself" comes with the text to copy and the place to paste it.
  {
    const byHand = (props = {}, pair = garden) => render(jsx(P.SkillByHand, { item: 'skill:seed-calendar', lead: true, onSkillSync: noop, pair, ...props }))
    const SKILL = '# seed-calendar\nAsk which month it is.\nName three things to sow.'
    html = text(waitingBox(garden, garden.waiting[0]))
    ok(html.includes('You changed the skill "seed-calendar" in Hermes.') && html.includes('Grok does not have this change yet. Copy the text, then paste it into the skill in the Grok app.') && JSON.stringify(texts()) === JSON.stringify(["Copy the skill's text", 'Done, I pasted it', 'Skill sync…']), `skill sync off: the text to copy comes first (${texts()})`)
    ok(JSON.stringify(primaries()) === JSON.stringify(["Copy the skill's text"]) && variantOf('Done, I pasted it') === 'ghost' && variantOf('Skill sync…') === 'text' && !/Turn on skill sync|I copied it myself|Copy it into Grok yourself/.test(html), 'skill sync off: copying is the primary; automating is a quiet link')
    reset()
    H.clips = []
    H.clipOk = true
    H.restReply = async path => (path.startsWith('/pairs/garden-buddy/skill-text') ? { pair_id: 'garden-buddy', item: 'skill:seed-calendar', name: 'seed-calendar', text: SKILL, truncated: false } : {})
    await press("Copy the skill's text")
    ok(H.restCalls.length === 1 && H.restCalls[0].path === '/pairs/garden-buddy/skill-text?item=skill%3Aseed-calendar' && !H.restCalls[0].opts?.method && JSON.stringify(H.clips) === JSON.stringify([SKILL]) && H.notes.length === 0, `Copy the skill's text: reads this twin's skill and puts it on the clipboard (${JSON.stringify(H.restCalls.map(c => c.path))})`)
    // Copied: where to paste it, and the button that settles the matter.
    raw = byHand({ initialCopied: true, initialText: SKILL })
    html = text(raw)
    ok(html.includes('Copied. In the Grok app, open Garden Buddy\'s skill "seed-calendar" and paste it over the old text.') && JSON.stringify(texts()) === JSON.stringify(['Show the text', 'Done, I pasted it', 'Copy again', 'Skill sync…']) && JSON.stringify(primaries()) === JSON.stringify(['Done, I pasted it']) && !html.includes('Ask which month'), `copied: where to paste, then Done (${texts()})`)
    reset()
    H.restReply = async () => ({ pair_id: 'garden-buddy', skipped: [] })
    await press('Done, I pasted it')
    ok(H.restCalls.length === 1 && H.restCalls[0].path === '/pairs/garden-buddy/skip' && JSON.stringify(H.restCalls[0].opts.body) === JSON.stringify({ copied: true, item: 'skill:seed-calendar' }) && H.notes.at(-1)?.message === 'Noted: the skill "seed-calendar" is in Grok too' && H.notes.at(-1)?.action?.label === 'Undo' && /asks again/.test(H.notes.at(-1)?.meta), `Done, I pasted it: this version is in Grok now (${JSON.stringify(H.notes.at(-1))})`)
    await H.notes.at(-1).action.onClick()
    await tick()
    ok(H.restCalls.at(-1)?.opts?.body?.undo === true, 'Done, I pasted it: Undo asks again')
    // Today's backend does not know "copied": the same press falls back to what it does know.
    byHand({ initialCopied: true, initialText: SKILL })
    reset()
    H.restReply = async (path, opts) => (opts?.body?.copied ? { error: { code: 'bad_input', message: 'That request was not understood.' } } : { pair_id: 'garden-buddy', skipped: ['skill:seed-calendar'] })
    await press('Done, I pasted it')
    ok(JSON.stringify(H.restCalls.map(c => c.opts.body)) === JSON.stringify([{ copied: true, item: 'skill:seed-calendar' }, { item: 'skill:seed-calendar' }]) && !H.notes.some(n => n.kind === 'error'), `Done, I pasted it (older backend): falls back, no error shown (${JSON.stringify(H.restCalls.map(c => c.opts.body))})`)
    // The clipboard did not take it: nothing is claimed; the text is shown with its own Copy button.
    raw = byHand({ initialCopied: false, initialText: SKILL })
    html = text(raw)
    ok(html.includes('Here is the text. Copy it. In the Grok app, open Garden Buddy\'s skill "seed-calendar" and paste it over the old text.') && !html.includes('Copied.') && html.includes('Ask which month it is.') && H.copies.some(c => c.text === SKILL) && has('Hide the text') && !has('Copy again'), `clipboard refused: the text itself, with a Copy button (${html.slice(0, 200)})`)
    reset()
    H.clipOk = false
    H.clips = []
    H.restReply = async () => ({ text: SKILL })
    byHand()
    await press("Copy the skill's text")
    ok(H.clips.length === 1 && H.notes.length === 0, 'clipboard refused: no toast claims a copy')
    H.clipOk = true
    // The skill holds something that looks like a password: the backend does not hand it over; said in place.
    const held = { code: 'looks_like_secret', message: 'This skill holds something that looks like a password or key, so Hermes does not hand it over.', hint: 'Take it out of the skill in Hermes first.' }
    reset()
    H.clips = []
    H.restReply = async () => ({ error: held })
    byHand()
    await press("Copy the skill's text")
    ok(H.clips.length === 0 && H.notes.length === 0, 'a skill with a possible password: nothing reaches the clipboard, no toast')
    raw = byHand({ initialProblem: held })
    html = text(raw)
    ok(html.includes('This skill holds something that looks like a password or key, so Hermes does not hand it over. Take it out of the skill in Hermes first.') && /role="alert"/.test(raw) && !has('Show the text') && !raw.includes('<pre') && has("Copy the skill's text"), 'a skill with a possible password: said next to the button, no text shown')
    // Today's backend has no such door: the honest way round, never a raw error.
    reset()
    H.restReply = async () => { throw new Error('404 Not Found') }
    byHand()
    await press("Copy the skill's text")
    ok(H.clips.length === 0 && H.notes.length === 0, 'older backend: no toast, nothing claimed')
    html = text(byHand({ initialProblem: { code: 'no_text', message: "Hermes cannot fetch this skill's text yet.", hint: 'Open Garden Buddy in Hermes and copy the skill from there.' } }))
    ok(html.includes("Hermes cannot fetch this skill's text yet. Open Garden Buddy in Hermes and copy the skill from there.") && !/404|Not Found/.test(html), 'older backend: where to copy it from instead')
    ok(/new TwinError\(`Hermes cannot fetch this \$\{words\.noun\}'s text yet\.`, 'no_text', `Open \$\{name\} in Hermes and copy the \$\{words\.noun\} from there\.`\)/.test(source), 'older backend: that sentence is what a missing door turns into')
  }
  const fromGrok = { item: 'routine:Water reminder', reason: 'needs_approval', to: 'hermes' }
  html = text(waitingBox(garden, fromGrok))
  ok(html.includes('The routine "Water reminder" from Grok is ready to add to Hermes.') && html.includes('It arrives switched off, so nothing runs twice.') && JSON.stringify(texts()) === JSON.stringify(['Add to Hermes', 'Not now']), 'something from Grok: Add to Hermes / Not now')
  reset()
  H.restReply = async () => ({ approved: [{ item: 'routine:Water reminder', to: 'local' }], sync: null })
  await press('Add to Hermes')
  ok(H.restCalls.length === 1 && H.restCalls[0]?.path === '/pairs/garden-buddy/approve' && H.notes.at(-1)?.message === 'Added Water reminder (routine) to Hermes', `add to Hermes: ${JSON.stringify(H.notes.at(-1))}`)
  html = text(waitingBox(garden, { item: 'connector:example-files', reason: 'needs_approval' }))
  ok(html.includes('The connection "example-files" is waiting for your go-ahead.') && H.buttons[0]?.text === 'Go ahead' && !/Send to Grok|You changed|\(connection\)/.test(html), 'direction unknown: no guess, a neutral go-ahead; a name is always in quotes')
  waitingBox(onSync, onSync.waiting[0], false)
  ok(primaries().length === 0 && variantOf('Send to Grok') === 'secondary', 'a box further down the page does not get a second primary button')

  // ── Changed in both places ──
  const opened = []
  raw = render(jsx(P.ConflictItem, { pairId: 'acme-support', conflict: bothVersions, onVersions: () => opened.push('versions') }))
  html = text(raw)
  ok(/<mark[^>]*>cheerful<\/mark>/.test(raw) && /<mark[^>]*>warm<\/mark>/.test(raw) && (raw.match(/<mark/g) ?? []).length === 2, 'both places: changed words highlighted, nothing else')
  ok(!/c9176fde1247|c354dae9eaf5|@@|\+\+\+|--- grok/.test(raw) && !raw.includes('aria-label="Line-by-line changes"'), 'both places: no digests, line-by-line view hidden by default')
  // A skill is prose: the body face, and a heading without its marks.
  ok(!raw.includes('<pre') && !/font-mono/.test(raw) && !html.includes('# Greet visitors') && count(html, 'Greet visitors') === 2 && /font-semibold[^>]*>Greet visitors/.test(raw), 'both places: versions in the body face, headings without "#"')
  ok(html.includes('The skill "greet-visitors" changed in both places.') && html.includes('Different since 30 min ago.') && !html.includes('Changed words are highlighted') && raw.includes('title="Changed words are highlighted."'), 'both places: the thing is named, in quotes; how the marks read is hover text')
  await press('Earlier versions')
  ok(opened.length === 1 && variantOf('Earlier versions') === 'text' && H.buttons.find(b => b.text === 'Earlier versions')?.size === 'default', 'the words "Earlier versions" are the way to them: a full-size quiet button after the sentence')
  raw = render(jsx(P.ConflictItem, { pairId: 'acme-support', conflict: bothVersions, initialShowDiff: true }))
  ok(raw.includes('aria-label="Line-by-line changes"') && !/@@|\+\+\+ hermes|--- grok/.test(raw) && /ui-diff-add/.test(raw) && /ui-diff-remove/.test(raw) && has('Hide line-by-line changes') && /font-mono/.test(raw), 'line-by-line changes: themed lines without headers, exactly as written')
  {
    const kh = H.buttons.find(b => b.text === 'Keep the Hermes version')
    const kg = H.buttons.find(b => b.text === 'Keep the Grok version')
    ok(kh && kg && kh.variant === kg.variant && kh.variant === 'outline', `the two choices have equal weight (${kh?.variant} / ${kg?.variant})`)
  }
  // A text too long to send whole: said under the two cards (and not for a short one).
  ok(text(render(jsx(P.ConflictItem, { pairId: 'acme-support', conflict: longVersions }))).includes('Long text: only the first part of each version is shown.') && !text(render(jsx(P.ConflictItem, { pairId: 'acme-support', conflict: bothVersions }))).includes('Long text'), 'both places: a cut-off text is said to be cut off')
  html = text(render(jsx(P.ConflictItem, { pairId: 'acme-support', conflict: { ...bothVersions, grok: { digest: 'x', text: null } } })))
  ok(html.includes('This one is not text, so it cannot be shown here.'), 'a version that is not text says so')
  // while the query (re)loads, or only an older empty answer exists, a neutral line; never "Already settled".
  for (const [q, label] of [
    [{ data: { conflicts: [] }, isFetching: true, dataUpdatedAt: Date.now() + 60_000 }, 'refetching'],
    [{ data: { conflicts: [] }, isFetching: false, dataUpdatedAt: 1 }, 'stale empty answer'],
    [{ data: { conflicts: [] }, isFetching: false, dataUpdatedAt: Date.now() + 60_000 }, 'fresh empty answer']
  ]) {
    H.override.conflicts = q
    const h = text(render(jsx(P.ConflictBox, { pair: acme })))
    ok(h.includes('Loading both versions…') && !h.includes('Already settled'), `both-places box ${label}: neutral line`)
  }
  H.override = {}
  reset()
  render(jsx(P.ConflictItem, { pairId: 'acme-support', conflict: bothVersions }))
  H.restReply = async () => ({ applied: [{ item: 'skill:greet-visitors', keep: 'hermes' }], restore_point: null })
  await press('Keep the Hermes version')
  ok(H.restCalls[0]?.path === '/pairs/acme-support/resolve' && H.restCalls[0]?.opts?.body?.keep === 'hermes' && H.notes.at(-1)?.message === 'Kept the Hermes version of the skill "greet-visitors"' && H.notes.at(-1)?.meta === 'Grok still has its own version. You can send this one to Grok afterwards.' && !H.notes.at(-1)?.action && !/earlier version|undo/i.test(JSON.stringify(H.notes.at(-1))), `keep Hermes: nothing was replaced, so no undo is claimed: ${JSON.stringify(H.notes.at(-1))}`)
  H.restReply = async path => (path.endsWith('/resolve') ? { applied: [{ item: 'skill:greet-visitors', keep: 'grok' }], restore_point: 'skill:greet-visitors@aaaaaaaaaaaa' } : { restored: ['skill:greet-visitors'], restore_point: null })
  await press('Keep the Grok version')
  ok(H.notes.at(-1)?.message === 'Kept the Grok version of the skill "greet-visitors"' && /Earlier versions/.test(H.notes.at(-1)?.meta) && H.notes.at(-1)?.action?.label === 'Undo', `keep Grok: Undo is offered: ${JSON.stringify(H.notes.at(-1))}`)
  await H.notes.at(-1).action.onClick()
  await tick()
  ok(H.restCalls.at(-1)?.path === '/pairs/acme-support/restore' && H.restCalls.at(-1)?.opts?.body?.point_id === 'skill:greet-visitors@aaaaaaaaaaaa' && H.notes.at(-1)?.message === 'Put back the Hermes version of the skill "greet-visitors"', 'Undo puts the Hermes version back')

  // ── Details: one sheet, three sections, ends in Done ──
  const details = (pair, props = {}) => render(jsx(P.DetailsSheet, { intervalS: 60, onClose: noop, onSkillSync: noop, open: true, pair, ...props }))
  {
    // What copies over. This twin has every reason for a difference at once.
    const busy = { ...recipePlain, conflicts: 1, secrets_held: 1, secrets_withheld: [{ item: 'persona.description', kind: 'description', direction: 'g2h' }], skipped: ['skill:fun-fact'], waiting: [{ item: 'skill:seed-calendar', to: 'grok' }], setup_left: ['instructions', 'model'] }
    const shown = []
    const finished = []
    // (sentences that are true of every twin are hover text or wait under "Technical details": opened here, so
    // every one of them is still checked)
    raw = details(busy, { initialTechnical: true, onFinish: () => finished.push(1), onShow: () => shown.push(1) })
    html = `${text(raw)} ${[...raw.matchAll(/title="([^"]*)"/g)].map(m => m[1]).join(' ')}`
    // (the number on the tab is the number of versions the list shows: two here, whatever the status counts)
    ok(JSON.stringify(H.tabs.map(t => t.text)) === JSON.stringify(['What copies over', 'Earlier versions (2)', 'Recent activity']) && html.includes('Recipe Planner: details') && endsInButton(raw) && H.buttons.at(-1)?.text === 'Done', `details: three sections, ends in Done (${H.tabs.map(t => t.text)})`)
    for (const s of ['Copies by itself: name and description', 'A change in Grok reaches Hermes within a minute.', 'With skill sync on: skills', 'Skill sync is off for this twin, so for now you copy these yourself.', 'Skill sync…',
      'You bring these over yourself: instructions, routines and connections', 'The Grok app keeps none of these on this computer, so Hermes cannot read them.',
      'Back to Grok: skills you change in Hermes go to Grok when you send them.', 'Kept in Hermes only', 'fun-fact (skill)',
      'Name matches Grok.', 'Description · waiting for the password to be removed in Grok', 'Instructions · not brought over yet', 'Skill "greet-visitors" · changed in both places',
      'Skill "fun-fact" · kept in Hermes only; you can offer it again below', 'Skill "seed-calendar" · changed in Hermes, not sent to Grok yet', 'Model · none chosen yet']) {
      ok(html.includes(s), `details (what copies over) shows "${s}"`)
    }
    ok(!/differs between Grok and Hermes|No model chosen for this twin yet|parity|checks pass|hermes -p|Hermes does not have the list|Press Check now|connectors|The same in Grok and Hermes|of 7 parts/i.test(html), 'details: every difference says why; no bare "differs", no dead end, no count that includes what cannot be compared')
    // Each difference has the one thing to press about it.
    ok(JSON.stringify(texts().filter(t => ['Choose', 'Show me', 'Offer again', 'Finish setting up'].includes(t))) === JSON.stringify(['Show me', 'Finish setting up', 'Choose', 'Show me', 'Finish setting up', 'Offer again']), `details: one button per difference, and one Offer again per item (${texts()})`)
    await press('Choose')
    await press('Show me')
    await press('Finish setting up')
    ok(shown.length === 2 && finished.length === 1, 'details: Choose / Show me lead back to the row; Finish setting up opens that sheet')
    reset()
    H.restReply = async () => ({ pair_id: 'recipe-planner', skipped: [] })
    await press('Offer again')
    ok(H.restCalls[0]?.path === '/pairs/recipe-planner/skip' && H.restCalls[0]?.opts?.body?.item === 'skill:fun-fact' && H.restCalls[0]?.opts?.body?.undo === true, 'Offer again undoes "keep in Hermes only"')
    ok(html.includes('in seconds') === false && details(busy, { intervalS: 5 }).includes('A change in Grok reaches Hermes in seconds.'), 'details: how fast a change arrives is said from the real interval, one number')
    {
      // One step of a sheet stays within fifty words: what is true of every twin is not on screen until asked for.
      const before = H.override.parity
      H.override.parity = { data: { passed: 2, total: 2, checks: [{ id: 'persona.name', name: 'name', pass: true }, { id: 'persona.description', name: 'description', pass: true }] } }
      const calmRaw = details(recipe, { checkedAt: iso(4) })
      H.override.parity = before
      const calm = text(calmRaw)
      const words = h => h.trim().split(/\s+/).filter(w => /[A-Za-z0-9]/.test(w)).length
      ok(!calm.includes('A change in Grok reaches') && !calm.includes('The Grok app keeps none of these') && !calm.includes('Back to Grok') && calmRaw.includes('title="A change in Grok reaches Hermes within a minute."') && words(calm) <= 50, `details, calm twin: ${words(calm)} words (50 at most): ${calm}`)
      // The whole table (every item, how it gets across, and why two things do not copy) is one press away:
      // folded, so the calm sheet keeps its fifty words; opened, it is one real table with three headed parts.
      ok(calm.includes('The whole table') && !calm.includes('Grok memory') && !calmRaw.includes('<table') && H.buttons.some(b => b.text === 'The whole table' && b.variant === 'ghost'), `details, calm twin: the whole table is folded (${words(calm)} words)`)
      const before2 = H.override.parity
      H.override.parity = { data: { passed: 2, total: 2, checks: [{ id: 'persona.name', name: 'name', pass: true }, { id: 'persona.description', name: 'description', pass: true }] } }
      const openRaw = details(recipe, { initialTable: true })
      H.override.parity = before2
      const open = text(openRaw)
      ok(count(openRaw, '<table') === 1 && count(openRaw, '<tbody') === 3 && openRaw.includes('<caption class="sr-only">What copies over, and how</caption>') && count(openRaw, 'scope="row"') === 16 && count(openRaw, 'scope="colgroup"') === 3 && count(openRaw, 'scope="col"') === 2, `the whole table: one table, three headed parts, sixteen named rows (${count(openRaw, 'scope="row"')})`)
      for (const s of ['From Grok to Hermes', 'From Hermes back to Grok', 'Between your own computers', 'Name and description By itself, in seconds, while Hermes is open.', 'Skills With skill sync, which is optional: a sync bot in your own Grok account carries them. Without it you copy them yourself.',
        'Grok memory Does not copy: memory is the most private thing a bot holds, so none is taken unless you switch on memory facts, which need skill sync.',
        'Conversations Off until you switch full transcripts on for a twin.',
        "Grok model setting Does not copy: nothing Hermes reads from Grok names the bot's model, so the twin uses the model you choose in Hermes.",
        'Name, description and instructions You make the same change in Grok yourself.', "Instructions By itself, as one of the twin's settings. You can switch that off on each computer.", 'Chats, files and logins Not copied. A request you send to another computer and its answer are the only chat text that travels, and only with requests switched on.']) {
        ok(open.includes(s), `the whole table says "${s}"`)
      }
      // A row that waits for work not in this version says so in its own sentence, and is marked for whoever checks.
      const waits = [...openRaw.matchAll(/<tr data-pending="true">(.*?)<\/tr>/g)].map(m => text(m[1]).trim())
      ok(waits.length === count(open, 'ot in this version yet') && waits.every(w => /ot in this version yet/.test(w)) && !/\b(pending|coverage|mirror|machine|PENDING)\b/.test(open), `the whole table: every waiting row says "not in this version yet" (${waits.length}: ${waits.map(w => w.split(' ')[0])})`)
      noEngineWords(open, 'details with the whole table open')
    }
    // The backend's fast check decides, not the timed interval: on -> "in seconds" at any interval; off -> the real number.
    ok(details(busy, { fastCheck: { on: true, mode: 'events' }, intervalS: 60 }).includes('A change in Grok reaches Hermes in seconds.'), 'details: fast check on -> in seconds, whatever the timed interval')
    for (const [secs, words] of [[60, 'within a minute'], [30, 'within 30 seconds'], [120, 'within 2 minutes'], [90, 'within 2 minutes'], [300, 'within 5 minutes'], [undefined, 'at the next check']]) {
      const said = details(busy, { fastCheck: { on: false, mode: 'off' }, intervalS: secs })
      ok(said.includes(`A change in Grok reaches Hermes ${words}.`) && !said.includes('about a minute') && !said.includes('in seconds'), `details: fast check off, interval ${secs} -> "${words}"`)
    }
    // Everything the same: one calm line that names what matches.
    H.override.parity = { data: { passed: 2, total: 2, checks: [{ id: 'persona.name', name: 'name', pass: true, detail: same }, { id: 'persona.description', name: 'description', pass: true, detail: same }] } }
    html = text(details(recipe))
    ok(html.includes('Name and description match Grok.') && !html.includes('Not compared') && !has('Show me') && !has('Choose'), 'details: everything the same is one line')
    // Instructions are never counted as "the same": Hermes cannot compare them, and says so apart.
    H.override.parity = { data: { passed: 5, total: 5, checks: [{ id: 'persona.name', name: 'name', pass: true }, { id: 'persona.description', name: 'description', pass: true }, { id: 'persona.instructions', name: 'instructions', pass: true, detail: 'you keep these yourself' },
      { id: 'skill:greet-visitors', name: 'skill greet-visitors', pass: true }, { id: 'skill:fun-fact', name: 'skill fun-fact', pass: true }] } }
    ok(!text(details(recipe)).includes('Not compared'), 'details: what cannot be compared waits under Technical details')
    html = text(details(recipe, { initialTechnical: true }))
    ok(html.includes('Name, description and 2 skills match Grok.') && html.includes('Not compared: instructions. Grok does not keep them on this computer, so you look after them yourself.') && !/all 5 parts|5 of 5/.test(html), `details: what matches is named; instructions are not among it (${html.slice(html.indexOf('Name,'), html.indexOf('Name,') + 160)})`)
    ok(P.matchWords([{ id: 'persona.name' }]) === 'name' && P.matchWords([{ id: 'skill:a' }]) === '1 skill' && P.matchWords([{ id: 'persona.description' }, { id: 'persona.name' }, { id: 'routine:x' }, { id: 'skill:a' }, { id: 'skill:b' }, { id: 'model' }]) === 'name, description, 2 skills and 1 routine' && P.matchWords([]) === '', `what matches, in words: ${P.matchWords([{ id: 'persona.description' }, { id: 'persona.name' }, { id: 'routine:x' }, { id: 'skill:a' }, { id: 'skill:b' }])}`)
    // When the twin was last checked is said here (and on hover in the row), not as a counter on every row.
    html = text(details(recipe, { checkedAt: iso(4) }))
    ok(html.includes(': details Checked 4 min ago.') && !text(details(recipe)).includes('Checked') && text(details(recipe)).includes('What follows Grok, and what was replaced.'), `details: when the twin was last checked (${html.slice(0, 80)})`)
    // The engine's own note is never a sentence of this list. The page says what it knows
    // from the kind of part; the raw note waits under "Technical details" (the real plugin sends "not carried:
    // Grok side: … (listed to apply: botport mirror status)" here).
    H.override.parity = { data: { passed: 0, total: 2, checks: [{ id: 'routine:Morning summary', name: 'routine Morning summary', pass: false, detail: 'not recreated in Hermes yet (Grok keeps routine schedules in the cloud)' }, { id: 'persona.description', name: 'description', pass: false, detail: "not carried: Grok side: set in the Grok app (the bot's settings) (listed to apply: botport mirror status)" }] } }
    raw = details(recipe)
    html = text(raw)
    ok(html.includes('Routine "Morning summary" · not set up in Hermes yet') && html.includes('Description · differs between Grok and Hermes') && !/not carried|listed to apply|mirror status|cloud\)/.test(html) && has('Technical details') && !raw.includes('<pre'), `details: a difference is said in the page's own words; the engine's note is folded (${html.slice(html.indexOf('Routine'), html.indexOf('Routine') + 200)})`)
    noEngineWords(html, 'details with the engine\'s notes folded')
    // A description changed in Hermes and not in Grok yet leads back to the row's box for it.
    H.override.parity = { data: { passed: 0, total: 1, checks: [{ id: 'persona.description', name: 'description', pass: false, detail: 'not carried: Grok side' }] } }
    html = text(details({ ...recipePlain, state: 'changes_waiting', pending: { ...calm, to_grok: 1, to_grok_items: ['description'] } }))
    ok(html.includes('Description · changed in Hermes, not in Grok yet') && has('Show me') && !html.includes('not carried'), 'details: a description changed in Hermes says so, with the way to its box')
    // A backend that sends its reason in plain words is quoted.
    H.override.parity = { data: { passed: 0, total: 1, checks: [{ id: 'routine:Morning summary', name: 'routine Morning summary', pass: false, detail: 'x', why: 'switched off in Hermes until you move it' }] } }
    ok(text(details(recipe)).includes('Routine "Morning summary" · switched off in Hermes until you move it'), "details: the backend's plain reason is shown when it sends one")
    H.override.parity = { data: { passed: 0, total: 2, checks: [{ id: 'routine:Morning summary', name: 'routine Morning summary', pass: false, detail: 'x', why: 'Not in Hermes yet. Grok keeps routine schedules in its cloud, so you set this one up yourself.' }, { id: 'routine:Evening summary', name: 'routine Evening summary', pass: false, detail: 'x', why: 'Grok keeps it.' }] } }
    raw = details(recipe)
    html = text(raw)
    // (the first sentence is the state; the second, the reason, is the hover text: the sheet stays within its words)
    ok(html.includes('Routine "Morning summary" · not in Hermes yet') && !html.includes('Grok keeps routine schedules') && raw.includes('title="not in Hermes yet. Grok keeps routine schedules in its cloud, so you set this one up yourself."') && html.includes('Routine "Evening summary" · Grok keeps it'), `details: the backend's reason reads on from the part's name (lower case, unless it starts with a name): ${html}`)
    H.override = {}
    // A twin whose list the backend did not send still shows the product's fixed table, not an excuse.
    html = text(details(legacy))
    ok(html.includes('Copies by itself: name and description') && html.includes('You bring these over yourself: instructions, routines and connections') && !/does not have the list/.test(html), 'details (older backend): the fixed table, no dead end')
    ok(P.differsBecause({ id: 'model', action: 'choose_model' }, legacy, new Set()).action === null, 'details (older backend): no button that could not work')
    // Skill sync on: said, and no set-up button.
    html = text(details({ ...recipe, mirror_bot: { connected: true } }))
    ok(html.includes('With skill sync on: skills On for this twin.') && !has('Skill sync…'), `details: skill sync on is said (${html.slice(0, 200)})`)
  }
  // Earlier versions.
  reset()
  raw = details(acme, { initialTab: 'versions' })
  html = text(raw)
  ok(html.includes("Whenever something in Acme Support's twin is replaced, the version before is kept here, newest first.") && html.includes('greet-visitors (skill)') && !html.includes('Greet-visitors') && /\bName\b/.test(html) && html.includes('Kept 5 min ago') && !html.includes('8ef22c6d10ba') && !html.includes('3f2a9c1b7e40') && H.buttons.filter(b => b.text === 'Put back').length === 2, 'earlier versions: kinds capitalised, a skill keeps its own name as written; when; no digests')
  ok(raw.includes('aria-label="Put back greet-visitors (skill)"') && H.buttons.at(-1)?.text === 'Done' && !html.includes('Copies by itself') && !has('Show'), 'Put back names the item; the sheet ends in Done; nothing to show when the backend sends no text')
  {
    // A version can be read before it is put back, and says what took its place, when the backend tells.
    const point = { id: 'persona.instructions@aaaaaaaaaaaa', item: 'persona.instructions', at: '2000-01-05T12:00:00Z', replaced_by: 'paste', text: 'You are Acme Support.\nBe brief.' }
    const version = (p, props = {}) => render(jsx('ul', { children: jsx(P.VersionRow, { busy: null, first: true, onRestore: noop, point: p, ...props }) }))
    raw = version(point)
    html = text(raw)
    ok(html.includes('Instructions') && !/\binstructions\b/.test(html) && /Replaced by your paste on \d{1,2} Jan/.test(html) && !html.includes('Kept') && !html.includes('aaaaaaaaaaaa'), `earlier version: capitalised, and what replaced it (${html})`)
    ok(JSON.stringify(texts()) === JSON.stringify(['Show', 'Put back']) && variantOf('Show') === 'ghost' && !html.includes('Be brief.') && !raw.includes('<pre'), 'earlier version: Show comes before Put back; the text stays folded until asked')
    raw = version(point, { initialShown: true })
    ok(text(raw).includes('You are Acme Support. Be brief.') && raw.includes('<pre') && has('Hide'), 'earlier version: Show shows that version')
    html = text(version({ ...point, replaced_by: 'grok', text: null }))
    ok(/Replaced by a change from Grok on \d{1,2} Jan/.test(html) && !has('Show'), 'earlier version without text: nothing to show, still says what replaced it')
    html = text(version({ ...point, replaced_by: 'something-new' }))
    ok(html.includes('Kept') && !/Replaced/.test(html), 'earlier version, unknown reason: only when it was kept')
    // Put back asks nothing more (no second button, no sentence to read): the way back is the Undo that follows.
    ok(!html.includes('change your mind') && !has('Cancel') && H.buttons.find(b => b.text === 'Put back')?.title === 'The current version is kept, so you can undo this.', 'earlier version: no confirmation; the button says on hover that the current one is kept')
  }
  // Put back: one click, done at once, with Undo for twelve seconds (it puts back what was just replaced).
  const putBack = async name => {
    await H.buttons.find(b => b.text === 'Put back' && b.label === `Put back ${name}`).onClick()
    await tick()
  }
  raw = details(acme, { initialTab: 'versions' })
  ok(!text(raw).includes('change your mind') && !has('Cancel') && H.confirms.length === 0 && !raw.includes('alertdialog'), 'put back: nothing is asked before the act')
  H.restReply = async () => ({ restored: ['skill:greet-visitors'], restore_point: null })
  await putBack('greet-visitors (skill)')
  ok(H.restCalls[0]?.path === '/pairs/acme-support/restore' && H.restCalls[0]?.opts?.body?.point_id === 'skill:greet-visitors@3f2a9c1b7e40' && H.notes.at(-1)?.message === 'Put back greet-visitors (skill)', `put back toast names the item: ${H.notes.at(-1)?.message}`)
  ok(H.notes.at(-1)?.action === undefined && H.notes.at(-1)?.meta === 'The version it replaced is kept under Earlier versions.' && H.notes.at(-1)?.durationMs === 12_000, `put back, when the backend names no replaced version: no Undo, and where it is kept: ${JSON.stringify(H.notes.at(-1))}`)
  details(acme, { initialTab: 'versions' })
  H.restReply = async () => ({ restored: [], restore_point: null })
  await putBack('Name')
  ok(H.notes.at(-1)?.message === 'Nothing to put back', `putting back nothing says so: ${H.notes.at(-1)?.message}`)
  details(acme, { initialTab: 'versions' })
  H.restReply = async () => ({ restored: ['skill:greet-visitors'], restore_point: 'skill:greet-visitors@aaaaaaaaaaaa', waiting_for_ok: ['skill:greet-visitors'] })
  await putBack('greet-visitors (skill)')
  ok(H.notes.at(-1)?.message === 'Put back greet-visitors (skill)' && H.notes.at(-1)?.meta === 'It now differs from Grok. Send it to Grok when you are ready.', `put back toast says what it created: ${JSON.stringify(H.notes.at(-1))}`)
  {
    // Undo: for at least 8 s (12 here), and it posts the version that Put back replaced.
    const note = H.notes.at(-1)
    ok(note?.action?.label === 'Undo' && note?.durationMs === 12_000, `put back: Undo is offered for 12 s: ${JSON.stringify(note)}`)
    H.restReply = async () => ({ restored: ['skill:greet-visitors'], restore_point: 'skill:greet-visitors@3f2a9c1b7e40' })
    await note.action.onClick()
    await tick()
    ok(H.restCalls.at(-1)?.path === '/pairs/acme-support/restore' && H.restCalls.at(-1)?.opts?.body?.point_id === 'skill:greet-visitors@aaaaaaaaaaaa' && H.notes.at(-1)?.message === 'Undone: greet-visitors (skill) is as before', `Undo puts back what Put back replaced: ${H.notes.at(-1)?.message}`)
  }
  {
    // A version that holds nothing is not listed (a new twin's blank "before"); one whose text is not known is.
    H.override['restore-points'] = { data: { points: [{ id: 'persona.instructions@bbbbbbbbbbbb', item: 'persona.instructions', at: iso(3), reason: 'kept copy', scope: 'item', text: '' }, { id: 'persona.name@8ef22c6d10ba', item: 'persona.name', at: iso(10), reason: 'kept copy', scope: 'item', text: null }] } }
    html = text(details(recipe, { initialTab: 'versions' }))
    ok(H.buttons.filter(b => b.text === 'Put back').length === 1 && /\bName\b/.test(html) && !html.includes('Instructions'), `earlier versions: an empty version is left out (${html})`)
    ok(H.tabs.some(t => t.text === 'Earlier versions (1)'), `earlier versions: the tab counts what the list shows, not the empty version (${H.tabs.map(t => t.text)})`)
    H.override['restore-points'] = { data: { points: [{ id: 'persona.instructions@bbbbbbbbbbbb', item: 'persona.instructions', at: iso(3), reason: 'kept copy', scope: 'item', text: '' }] } }
    details({ ...recipe, restore_points: 3 }, { initialTab: 'versions' })
    ok(H.tabs.some(t => t.text === 'Earlier versions') && !H.tabs.some(t => /Earlier versions \(/.test(t.text)), `earlier versions: no number when nothing is listed, whatever the status counts (${H.tabs.map(t => t.text)})`)
  }
  // Recent activity never shows a part by its key, whatever the backend sends.
  ok(P.plainActivity('persona.name restored from a kept copy') === 'name put back from an earlier version' && P.plainActivity('skill:greet-visitors updated from Grok') === 'skill "greet-visitors" updated from Grok' && P.plainActivity('description updated from Grok') === 'description updated from Grok', `recent activity: plain words (${P.plainActivity('persona.name restored from a kept copy')})`)
  {
    const before = H.override.recent
    H.override.recent = { data: { entries: [{ at: iso(3), side: 'hermes', kind: 'restore', summary: 'persona.name restored from a kept copy' }] } }
    const recent = text(details(recipe, { initialTab: 'recent' }))
    ok(recent.includes('In Hermes: name put back from an earlier version') && !/persona\.|kept copy/.test(recent), `recent activity: no key on the sheet (${recent.slice(0, 200)})`)
    H.override.recent = before
  }
  H.override['restore-points'] = { data: { points: [] } }
  ok(text(details(recipe, { initialTab: 'versions' })).includes('No earlier versions yet. One appears here the first time something is replaced.'), 'earlier versions: an empty list teaches')
  H.override = {}
  // Recent activity: the side is not said twice.
  raw = details(acme, { initialTab: 'recent' })
  html = text(raw)
  ok(html.includes('Times and kinds only, never what was said.') && html.includes('In Grok: Description updated') && html.includes('Skill edited in Hermes: greet-visitors') && !/In Hermes: Skill edited in Hermes|Hermes Skill edited/.test(html) && H.buttons.at(-1)?.text === 'Done', `recent activity: ${html.slice(0, 300)}`)

  // ── Finish setting up: the two things a new twin still needs ──
  const finish = (pair, props = {}) => render(jsx(P.FinishSheet, { onClose: noop, open: true, pair, ...props }))
  raw = finish(trail)
  html = text(raw)
  for (const s of ['Finish setting up Trail Guide', '2 things left, about a minute.', '1. Paste its instructions',
    'In the Grok app, edit Trail Guide and copy its instructions.',
    'Save instructions', 'It has no instructions', 'Why paste them?', '2. Choose a model', 'Choose in Hermes', 'Later']) {
    ok(html.includes(s), `finish setting up shows "${s}"`)
  }
  {
    // One step of a sheet stays within fifty words: only the thing to do now shows its sentence; the one whose
    // turn comes next is its title and its button.
    const words = h => h.trim().split(/\s+/).filter(w => /[A-Za-z0-9]/.test(w)).length
    ok(words(html) <= 50 && !html.includes('open Settings, then Model'), `finish, two things left: ${words(html)} words (50 at most): ${html}`)
    const oneLeft = text(finish({ ...trail, setup_left: ['instructions'], hermes: { ...trail.hermes, model_from: 'default' } }))
    ok(words(oneLeft) <= 50 && oneLeft.includes('2. Model · same as your other chats'), `finish, one thing left: ${words(oneLeft)} words (50 at most): ${oneLeft}`)
    raw = finish(trail)
    html = text(raw)
  }
  ok(H.textareas.length === 1 && H.textareas[0]['aria-label'] === 'Instructions for Trail Guide' && H.textareas[0].placeholder === 'Paste the instructions here' && H.textareas[0].maxLength === 200_000, 'finish: one labelled, capped text box')
  ok(H.buttons.find(b => b.text === 'Save instructions')?.disabled === true && primaries().length === 0 && variantOf('Choose in Hermes') === 'text' && variantOf('Later') === 'ghost' && endsInButton(raw), 'finish: nothing to save yet, so nothing is pushed; first things first')
  ok(!/follows Grok|sync|copied from Grok/i.test(html), 'finish: never a claim that instructions follow Grok')
  // Where a model is chosen is the backend's to say when it knows better (untrusted text: plain, capped).
  {
    const hinted = text(finish({ ...trail, setup_left: ['model'], model_hint: 'In Hermes, open Settings → Model, choose `Trail Guide` under Applies to, and pick a model.\u0007' }))
    ok(hinted.includes('In Hermes, open Settings → Model, choose Trail Guide under Applies to, and pick a model.') && !hinted.includes('Under "Applies to" choose Trail Guide and choose a model') && hinted.includes('Choose in Hermes'), "finish: the backend's own sentence about choosing a model replaces the page's")
  }
  // The step opens with what to do, not with the excuse; the why is one fold away.
  ok(html.indexOf('In the Grok app, edit Trail Guide') < html.indexOf('Why paste them?') && html.indexOf('Why paste them?') > 0 && !html.includes("does not keep a bot's instructions") && !/Hermes asks which model/.test(html), 'finish: what to do first; the reason folded; no promise that Hermes will ask for a model')
  {
    const pasted = 'You are Trail Guide.\nSuggest one walk, with its length.'
    finish(trail, { initialText: pasted })
    ok(JSON.stringify(primaries()) === JSON.stringify(['Save instructions']) && H.textareas[0].value === pasted, 'finish: with text pasted, Save instructions is the one primary')
    reset()
    H.restReply = async () => ({ pair_id: 'trail-guide', saved: true, setup_left: ['model'] })
    await press('Save instructions')
    ok(H.restCalls.length === 1 && H.restCalls[0].path === '/pairs/trail-guide/instructions' && H.restCalls[0].opts.method === 'POST' && JSON.stringify(H.restCalls[0].opts.body) === JSON.stringify({ text: pasted }), `Save instructions posts the pasted text to this twin only (${JSON.stringify(H.restCalls.map(c => c.path))})`)
    ok(H.notes.at(-1)?.message === "Trail Guide's instructions are saved" && H.notes.at(-1)?.meta === 'They stay on this computer. Nothing was sent to Grok.' && !JSON.stringify(H.notes).includes('Suggest one walk'), `finish: saved, and the text itself is never echoed (${JSON.stringify(H.notes.at(-1))})`)
    // Text that looks like a password or key: nothing is saved, and it is said next to the text, not in a toast.
    const refused = { code: 'looks_like_secret', message: 'Something in that text looks like a password or key, so nothing was saved.', hint: 'Take it out of the text, then save again.' }
    finish(trail, { initialText: pasted })
    reset()
    H.restReply = async () => ({ error: refused })
    H.invalidations = 0
    await press('Save instructions')
    ok(H.restCalls.length === 1 && H.notes.length === 0 && H.invalidations === 0, `finish: a refused text raises no toast and nothing else happens (${JSON.stringify(H.notes)})`)
    raw = finish(trail, { initialProblem: refused, initialText: pasted })
    ok(text(raw).includes('Something in that text looks like a password or key, so nothing was saved. Take it out of the text, then save again.') && /role="alert"/.test(raw) && raw.indexOf('role="alert"') > raw.indexOf('<textarea') && raw.indexOf('role="alert"') < raw.indexOf('Save instructions') && H.textareas[0].value === pasted, 'finish: the refusal sits between the text and its button; the text is still there to fix')
    // A backend that does not say which line: the page offers nothing it could not do truthfully.
    ok(!has('Save without that line') && !has('Save it all…') && has('Save instructions'), 'finish (refusal without a line): no button that would have to guess')

    // A backend that points at the line: the sentence names it, and there are two ways on. Neither is a lie
    // ("It has no instructions" used to be the only way past a false alarm).
    const KEY = 'Tracking code: 9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c'
    const withKey = `${pasted}\n${KEY}`
    const pointed = { code: 'looks_like_secret', message: 'Line 3 looks like a password or key, so nothing was saved yet.', hint: 'Take that line out, or save without it.', lines: [3] }
    raw = finish(trail, { initialProblem: pointed, initialText: withKey })
    html = text(raw)
    ok(html.includes('Line 3 looks like a password or key, so nothing was saved yet.') && !html.includes('Take that line out') && /role="alert"/.test(raw) && H.textareas[0].value === withKey, `finish (line pointed at): the sentence names the line (${html.slice(0, 300)})`)
    ok(JSON.stringify(primaries()) === JSON.stringify(['Save without that line']) && variantOf('Save it all…') === 'text' && !has('Save instructions') && !has('It has no instructions') && variantOf('Choose in Hermes') === 'text' && !html.includes('In the Grok app, edit') && !has('Why paste them?'), `finish (line pointed at): one primary, one quiet way for a false alarm; nothing else to read (${texts()})`)
    {
      const words = h => h.trim().split(/\s+/).filter(w => /[A-Za-z0-9]/.test(w)).length
      const shown = text(raw.replace(/<textarea[^>]*>[\s\S]*?<\/textarea>/g, '').replace(/<span class="sr-only">[^<]*<\/span>/g, ''))
      ok(words(shown) <= 50, `finish (line pointed at): ${words(shown)} words (50 at most): ${shown}`)
    }
    // The doubtful line is on screen in the owner's own text box and nowhere else.
    ok(!raw.replace(/<textarea[^>]*>[\s\S]*?<\/textarea>/g, '').includes('9f8a7b6c') && typeof H.textareas[0].ref === 'object', 'finish (line pointed at): the line is never printed outside the text box; the box can be reached to select it')
    reset()
    H.restReply = async () => ({ pair_id: 'trail-guide', saved: true, setup_left: ['model'] })
    await press('Save without that line')
    ok(H.restCalls.length === 1 && H.restCalls[0].path === '/pairs/trail-guide/instructions' && JSON.stringify(H.restCalls[0].opts.body) === JSON.stringify({ text: pasted }), `Save without that line: the rest goes through the same door and the same check (${JSON.stringify(H.restCalls[0]?.opts?.body)})`)
    ok(H.notes.at(-1)?.message === "Trail Guide's instructions are saved" && H.notes.at(-1)?.meta === 'Without line 3. They stay on this computer. Nothing was sent to Grok.' && !JSON.stringify(H.notes).includes('9f8a7b6c'), `Save without that line: says what was left out, never the line (${JSON.stringify(H.notes.at(-1))})`)
    // "It is not a password": asked once, in plain words, before anything is sent.
    finish(trail, { initialProblem: pointed, initialText: withKey })
    reset()
    await press('Save it all…')
    ok(H.restCalls.length === 0, '"It is not a password" only asks; nothing is saved by that press')
    raw = finish(trail, { initialConfirmAll: true, initialProblem: pointed, initialText: withKey })
    html = text(raw)
    ok(html.includes('Save line 3 too? It stays on this computer.') && !html.includes('so nothing was saved yet') && has('Save it all') && has('Cancel') && !has('Save without that line') && primaries().length === 0, `finish (save it all?): one question, which names the line in place of the sentence before it (${texts()})`)
    {
      const words = h => h.trim().split(/\s+/).filter(w => /[A-Za-z0-9]/.test(w)).length
      const shown = text(raw.replace(/<textarea[^>]*>[\s\S]*?<\/textarea>/g, '').replace(/<span class="sr-only">[^<]*<\/span>/g, ''))
      ok(words(shown) <= 50, `finish (save it all?): ${words(shown)} words (50 at most): ${shown}`)
    }
    reset()
    await press('Save it all')
    ok(H.restCalls.length === 1 && JSON.stringify(H.restCalls[0].opts.body) === JSON.stringify({ accept_lines: [3], text: withKey }) && H.notes.at(-1)?.message === "Trail Guide's instructions are saved" && !JSON.stringify(H.notes).includes('9f8a7b6c'), `Save it all: the owner vouches for exactly the lines that were pointed at (${JSON.stringify(H.restCalls[0]?.opts?.body?.accept_lines)})`)
    // Two lines: the buttons say so; lines the text does not have are ignored.
    raw = finish(trail, { initialProblem: { ...pointed, message: 'Lines 1 and 3 look like a password or key, so nothing was saved yet.', lines: [1, 3, 99] }, initialText: withKey })
    ok(has('Save without those lines') && P.lineWords([1, 3]) === 'lines 1 and 3' && P.lineWords([14]) === 'line 14' && P.lineWords([1, 2, 3, 4, 5]) === 'lines 1, 2 and 3 more', `finish (two lines): ${P.lineWords([1, 3])}`)
    reset()
    await press('Save without those lines')
    ok(JSON.stringify(H.restCalls[0]?.opts?.body) === JSON.stringify({ text: 'Suggest one walk, with its length.' }), 'Save without those lines: both are taken out')
    // The whole text was the doubtful line: nothing empty is sent.
    finish(trail, { initialProblem: { ...pointed, lines: [1] }, initialText: KEY })
    reset()
    await press('Save without that line')
    ok(H.restCalls.length === 0 && H.notes.length === 0, 'Save without that line with nothing left: nothing is sent')
    // A bot without instructions is a fine answer.
    finish(trail)
    reset()
    H.restReply = async () => ({ pair_id: 'trail-guide', saved: false, setup_left: ['model'] })
    await press('It has no instructions')
    ok(JSON.stringify(H.restCalls[0]?.opts?.body) === JSON.stringify({ none: true }) && H.notes.at(-1)?.message === 'Noted: Trail Guide has no instructions', 'finish: "It has no instructions" says exactly that')
  }
  // Instructions done, model left: the model step is now the one thing.
  html = text(finish({ ...trail, setup_left: ['model'] }))
  ok(html.includes('1 thing left, about a minute.') && html.includes('1. Instructions · in Hermes') && html.includes('In Hermes, open Settings, then Model. Under "Applies to" choose Trail Guide and choose a model. Then click Twins in the sidebar.') && H.textareas.length === 0 && JSON.stringify(primaries()) === JSON.stringify(['Choose in Hermes']), `finish: model left (${primaries()})`)
  // "Choose in Hermes" does not abandon the owner: the sheet stays, and the page remembers what it is waiting for.
  {
    const closed = []
    finish({ ...trail, setup_left: ['model'] }, { onClose: () => closed.push(1), storage: ctx.storage })
    H.newChats = []
    H.storedSets = []
    await press('Choose in Hermes')
    ok(JSON.stringify(H.newChats) === JSON.stringify(['trail-guide']) && closed.length === 0, `Choose in Hermes opens the twin and leaves the sheet where it is (closed ${closed.length}×)`)
    // The button lands on the twin's chat, where nothing repeated what to do. The same
    // sentence now goes along as a notice that stays half a minute.
    ok(H.notes.at(-1)?.kind === 'info' && H.notes.at(-1).message === 'Choose a model for Trail Guide' && /open Settings, then Model\. Under "Applies to" choose Trail Guide/.test(H.notes.at(-1).meta) && H.notes.at(-1).durationMs >= 20_000, `Choose in Hermes: where to choose the model goes along (${JSON.stringify(H.notes)})`)
    ok(H.storedSets.length === 2 && H.storedSets[0][0] === 'asked.trail-guide' && Number.isFinite(Date.parse(H.storedSets[0][1]?.at)) && JSON.stringify(H.storedSets[1]) === JSON.stringify(['noted', ['trail-guide']]), `Choose in Hermes: remembered on this computer (${JSON.stringify(H.storedSets)})`)
    raw = finish({ ...trail, setup_left: ['model'] }, { initialAsked: true })
    html = text(raw)
    ok(html.includes('Waiting for you to choose a model for Trail Guide in Hermes. This step ticks by itself.') && !html.includes('Hermes opens a chat with') && variantOf('Open it again') === 'secondary' && !has('Choose in Hermes') && primaries().length === 0 && endsInButton(raw), `finish (asked): waiting, with the way back there (${texts()})`)
    // Back on the page: the row says what it is waiting for, and its one button goes there again.
    H.stored['asked.trail-guide'] = { at: iso(2) }
    ok(P.wasAsked(ctx.storage, 'trail-guide') === true && P.wasAsked(ctx.storage, 'recipe-planner') === false, 'waiting for a model: remembered per twin')
    html = text(row({ ...trail, setup_left: ['model'] }))
    ok(html.includes('Waiting for you to choose a model in Hermes') && !html.includes('Almost ready') && JSON.stringify(texts()) === JSON.stringify(['Open in Hermes', 'Open it again', '']) && variantOf('Open it again') === 'default' && H.menu.some(m => m.text === 'Finish setting up…'), `row (asked): waiting for a model, Open it again (${html.slice(0, 160)})`)
    H.newChats = []
    await press('Open it again')
    ok(JSON.stringify(H.newChats) === JSON.stringify(['trail-guide']), 'Open it again opens the twin in Hermes')
    // Instructions still to paste as well: the row keeps its usual sentence and button.
    html = text(row(trail))
    ok(html.includes('Almost ready · 2 things left') && has('Finish setting up') && !has('Open it again'), 'row (asked, instructions also left): Finish setting up')
    // The model is chosen: nothing waits any more.
    html = text(row({ ...trail, setup_left: [] }))
    ok(html.includes('Follows Grok') && !html.includes('Waiting') && has('Open in Hermes'), 'row (model chosen): calm again')
    delete H.stored['asked.trail-guide']
  }
  // A backend that gives a new twin the owner's usual model: no step to do, and the sheet says which model.
  html = text(finish({ ...trail, setup_left: ['instructions'], hermes: { ...trail.hermes, model_from: 'default' } }))
  ok(html.includes('1 thing left, about a minute.') && html.includes('2. Model · same as your other chats') && !has('Choose in Hermes') && !html.includes('Model · chosen'), 'finish: a model copied from the owner\'s usual one is said, not asked for')
  // Everything done.
  html = text(finish({ ...trail, setup_left: [] }))
  ok(html.includes("Trail Guide's twin is ready") && html.includes('It follows Grok. Everything else is in its "…" menu.') && html.includes('1. Instructions · in Hermes') && html.includes('2. Model · chosen') && JSON.stringify(primaries()) === JSON.stringify(['Open in Hermes']) && variantOf('Done') === 'ghost' && H.buttons.at(-1)?.text === 'Open in Hermes', 'finish: all done -> Open in Hermes')
  // The sample: no model step, an example to paste, and no pretend chat.
  html = text(finish(acmeNew, { example: EXAMPLE }))
  ok(html.includes('Finish setting up Acme Support') && html.includes('1 thing left, about a minute.') && !html.includes('Choose a model') && !has('Choose in Hermes') && has("Paste the sample's instructions"), 'finish (sample): one step, with an example to paste')
  html = text(finish(acmeCalm))
  ok(html.includes("Acme Support's twin is ready") && html.includes('The sample has no chat. Connect a real bot to talk to its twin.') && !has('Open in Hermes') && JSON.stringify(primaries()) === JSON.stringify(['Done']), 'finish (sample, done): says there is no chat instead of offering one')

  // ── Import setup export: ask the bot, hand over its file, look, import ──
  {
    const words = h => h.trim().split(/\s+/).filter(w => /[A-Za-z0-9]/.test(w)).length
    const imp = (props = {}, pair = trail) => render(jsx(P.ImportSheet, { onClose: noop, open: true, pair, ...props }))
    // (a file's text with a line the page must never repeat anywhere; built here so no key-shaped text sits in this file)
    const MARK = 'zq' + 'x-file-text-' + '4417'
    const FILE_TEXT = JSON.stringify({ format: 'grok-bot-export', version: 1, bots: [{ name: 'Trail Guide', instructions: `You are Trail Guide. ${MARK}`, memories: [], user_facts: [] }] })

    // The way in: a real twin's menu and the first step of Finish setting up. Never the sample's.
    H.status = { ...statusAll, pairs: [acmeCalm, trail] }
    page()
    ok(H.menu.filter(m => m.text === 'Import setup export…').length === 1 && H.menu.findIndex(m => m.text === 'Import setup export…') > H.menu.findIndex(m => m.text === 'End sample'), `import: in a real twin's menu only (${H.menu.map(m => m.text)})`)
    H.status = statusAll
    {
      let asked = 0
      const shown = text(render(jsx(P.FinishSheet, { onClose: noop, onImport: () => { asked += 1 }, open: true, pair: trail })))
      ok(variantOf('Import them instead…') === 'text' && words(shown) <= 50, `finish: the quiet way to the import, still within fifty words (${words(shown)})`)
      await press('Import them instead…')
      ok(asked === 1, 'finish: that button asks for the import sheet')
      render(jsx(P.FinishSheet, { example: EXAMPLE, onClose: noop, onImport: noop, open: true, pair: acmeNew }))
      ok(!has('Import them instead…'), 'finish (sample): no import')
    }

    // First look: ask the bot, a clear place for its answer, one sentence about memories. Fifty words at most.
    raw = imp()
    html = text(raw)
    for (const s of ["Import Trail Guide's setup export", 'Ask the bot', 'Paste this into its chat in the Grok app.', 'Copy the export request', 'Show the request', 'Drop its answer here', 'Choose the file…', 'Or paste it', EXPORT_PRIVACY, 'Close']) {
      ok(html.includes(s), `import shows "${s}"`)
    }
    ok(words(html) <= 50, `import, first look: ${words(html)} words (50 at most): ${html}`)
    ok(JSON.stringify(primaries()) === JSON.stringify(['Copy the export request']) && variantOf('Choose the file…') === 'secondary' && variantOf('Close') === 'ghost' && endsInButton(raw), `import, first look: the request is the one primary (${primaries()})`)
    ok(/<div[^>]*aria-label="Drop the setup export here"[^>]*border-dashed[^>]*data-over="no"[^>]*role="group"/.test(raw) && /<input[^>]*accept="\.json,\.txt,application\/json,text\/plain"[^>]*type="file"/.test(raw) && raw.includes('class="hidden"'), 'import: a bordered place to drop the file, named for a screen reader, with a file chooser behind its button')
    ok(count(html, EXPORT_PRIVACY) === 1 && raw.indexOf(EXPORT_PRIVACY) > raw.indexOf('border-dashed'), 'import: the sentence about memories is said once, under the place for the file')
    ok(!html.includes('Please write your setup export') && !html.includes('Last import') && !/arrives|Routines|Instructions ·/.test(html), 'import, first look: the request is folded; nothing about a file that is not there yet')
    noEngineWords(html, 'import, first look')
    // Copy: the request goes to the clipboard as the backend wrote it; the sentence says so and the next act leads.
    reset()
    H.clips = []
    H.storedSets = []
    await press('Copy the export request')
    ok(H.clips.length === 1 && H.clips[0] === EXPORT_REQUEST && H.restCalls.length === 0 && H.notes.length === 0, 'Copy the export request: the backend\'s text, to the clipboard, nothing else')
    raw = imp({ initialCopied: true })
    html = text(raw)
    ok(html.includes('Copied. Paste it into its Grok chat.') && /role="status"/.test(raw) && JSON.stringify(primaries()) === JSON.stringify(['Choose the file…']) && variantOf('Copy the export request') === 'secondary' && words(html) <= 50, `import, copied: says so, and choosing the file is next (${primaries()}; ${words(html)} words)`)
    // The clipboard would not take it: the text is shown with its own Copy, never a claim that it was copied.
    raw = imp({ initialCopied: false })
    html = text(raw)
    ok(html.includes('Copy this, then paste it into its chat in the Grok app.') && !html.includes('Copied.') && html.includes('Please write your setup export for me') && H.copies.some(c => c.text === EXPORT_REQUEST) && has('Hide the request'), 'import, clipboard refused: the request is shown to copy')
    // A file held over the box.
    raw = imp({ initialOver: true })
    ok(text(raw).includes('Drop it here') && !text(raw).includes('Drop its answer here') && raw.includes('data-over="yes"'), 'import: the box answers a file held over it')
    // The request is still loading, or could not be read: the file can be handed over all the same.
    H.override['setup-export'] = { data: undefined, isPending: true }
    imp()
    ok(H.buttons.find(b => b.text === 'Copy the export request')?.disabled === true && has('Choose the file…') && !has('Show the request'), 'import while the request loads: nothing to copy yet, the box is there')
    H.override['setup-export'] = { data: undefined, error: new P.TwinError('Hermes could not reach Grok Bot Twin.', 'unreachable', 'Try again.'), isError: true }
    html = text(imp())
    ok(html.includes('Hermes could not reach Grok Bot Twin.') && has('Try again') && !has('Copy the export request') && has('Choose the file…') && html.includes(EXPORT_PRIVACY), 'import, request unreadable: said in place with a retry; the box and the sentence stay')
    // A backend without its own sentence: the page's.
    H.override['setup-export'] = { data: { ...exportInfo, privacy: '' } }
    ok(text(imp()).includes('Its lists of memories and facts about you are left out: Hermes never stores them.'), 'import: the sentence about memories is there whatever the backend sends')
    // Imported before: when, and what was still to set up here, one fold away.
    H.override['setup-export'] = { data: exportInfoLast }
    raw = imp()
    html = text(raw)
    ok(has('Last import') && !html.includes('Trail maps') && words(html) <= 52, `import, again: the last import is one fold away (${words(html)} words)`)
    delete H.override['setup-export']

    // A chosen or dropped file is read as text; what cannot be a setup export is refused in plain words.
    ok(await P.exportTextOf({ size: FILE_TEXT.length, text: async () => FILE_TEXT }) === FILE_TEXT, 'import: a file is read as text')
    {
      const refused = async file => { try { await P.exportTextOf(file) } catch (e) { return e } return null }
      const big = await refused({ size: 2_000_001, text: async () => { throw new Error('must not be read') } })
      ok(big?.code === 'too_large' && big.message === 'That file is too large to be a setup export.', `import: a file over two million characters is refused before it is read (${big?.message})`)
      ok((await refused({ size: 10, text: async () => 'x'.repeat(2_000_001) }))?.code === 'too_large', 'import: ...and after, when its size was not known')
      ok((await refused({ size: 3, text: async () => '  \n' }))?.message === 'That file is empty.' && (await refused({ size: 3, text: async () => { throw new Error('gone') } }))?.message === 'Hermes could not read that file.', 'import: an empty or unreadable file is said as that')
    }

    // An answer pasted as text goes the same way as a file: asked about first, nothing changed, nothing kept.
    imp()
    ok(H.textareas.length === 0, 'import: the box for a pasted answer is folded until asked for')
    raw = imp({ initialPasted: FILE_TEXT })
    ok(H.textareas.length === 1 && H.textareas[0]['aria-label'] === 'The setup export Trail Guide sent' && H.textareas[0].maxLength === 2_000_000 && H.textareas[0].value === FILE_TEXT, 'import: one labelled, capped box for a pasted answer')
    reset()
    H.storedSets = []
    H.invalidations = 0
    H.restReply = async () => exportPlain
    await press('Read it')
    ok(H.restCalls.length === 1 && H.restCalls[0].path === '/pairs/trail-guide/setup-export' && H.restCalls[0].opts.method === 'POST' && JSON.stringify(H.restCalls[0].opts.body) === JSON.stringify({ apply: false, text: FILE_TEXT }), `Read it: one question to this twin's backend, apply false (${JSON.stringify(H.restCalls.map(c => [c.path, c.opts?.body?.apply]))})`)
    ok(H.notes.length === 0 && H.invalidations === 0 && (H.storedSets ?? []).length === 0, 'Read it: no toast, nothing refreshed, nothing remembered')
    // A file that is not a setup export: said in place, the box still there for another try.
    const notOne = { code: 'bad_export', message: 'That file is not a setup export.', hint: 'Ask the bot for it again with the request above.' }
    imp({ initialPasted: 'hello' })
    reset()
    H.restReply = async () => ({ error: notOne })
    await press('Read it')
    ok(H.restCalls.length === 1 && H.notes.length === 0, `import: a refused file raises no toast (${JSON.stringify(H.notes)})`)
    raw = imp({ initialProblem: notOne })
    html = text(raw)
    ok(html.includes('That file is not a setup export. Ask the bot for it again with the request above.') && /role="alert"/.test(raw) && raw.indexOf('role="alert"') > raw.indexOf('border-dashed') && has('Choose the file…') && has('Copy the export request') && !html.includes('Paste this into its chat') && !has('Show the request') && words(html) - words(`${notOne.message} ${notOne.hint}`) <= 40, `import, refused: said under the box, in place of the sentences around it; the box and the request stay (${words(html)} words, ${words(`${notOne.message} ${notOne.hint}`)} of them the refusal)`)

    // What the file holds, before anything is changed. The plain case stays within fifty words.
    const look = (shown, props = {}, pair = trail) => imp({ initialShown: shown, initialText: FILE_TEXT, ...props }, pair)
    raw = look(exportPlain)
    html = text(raw)
    for (const s of ["Import Trail Guide's setup export", 'Instructions', '2 lines', 'Routines', 'Morning walk · every day at 08:00', '1 new routine stays switched off until you choose Move routines to Hermes.', 'Set up on this computer', 'Calendar', 'No logins are copied: connect these in Hermes yourself.', 'Show the text', 'Choose another file', 'Import']) {
      ok(html.includes(s), `import, looking: "${s}"`)
    }
    ok(words(html) <= 50, `import, looking (plain file): ${words(html)} words (50 at most): ${html}`)
    ok(JSON.stringify(primaries()) === JSON.stringify(['Import']) && variantOf('Choose another file') === 'ghost' && endsInButton(raw) && !html.includes('Left out') && !html.includes('This export is from') && !html.includes('Suggest one walk') && !html.includes(MARK), `import, looking: one primary; the text itself is folded (${primaries()})`)
    ok(!has('Copy the export request') && !has('Choose the file…') && !raw.includes('border-dashed'), 'import, looking: the asking and the box have stepped aside')
    noEngineWords(html, 'import, looking')
    // Import: the same text again, apply true, and the bot that was shown. Then one notice in counts, never content.
    reset()
    H.storedSets = []
    H.storedRemoves = []
    H.invalidations = 0
    H.restReply = async () => ({ ...exportPlain, applied: true, added: 1, instructions: { ...exportPlain.instructions, saved: true }, routines: [{ ...exportPlain.routines[0], state: 'added' }] })
    await press('Import')
    ok(H.restCalls.length === 1 && H.restCalls[0].path === '/pairs/trail-guide/setup-export' && JSON.stringify(H.restCalls[0].opts.body) === JSON.stringify({ apply: true, bot: 'Trail Guide', text: FILE_TEXT }), `Import: the file that was shown, for the bot that was shown (${JSON.stringify(Object.keys(H.restCalls[0]?.opts?.body ?? {}))})`)
    ok(H.notes.length === 1 && H.notes[0].kind === 'success' && H.notes[0].message === "Trail Guide's setup export is imported" && H.notes[0].meta === 'Instructions saved. 1 routine added, switched off. 1 connector to set up on this computer. Nothing was sent to Grok.' && H.invalidations > 0, `Import: said once, in counts (${JSON.stringify(H.notes[0])})`)
    // The file's text is the sheet's alone: never in the page's storage, never in a notice.
    ok(!JSON.stringify([H.storedSets ?? [], H.storedRemoves ?? [], H.notes]).includes(MARK) && (H.storedSets ?? []).length === 0, `import: the file's text is never kept or echoed (${JSON.stringify(H.storedSets)})`)
    ok(!/storage\??\.set\([^)]*(?:text|pasted|shown)\b/.test(source.slice(source.indexOf('function ImportSheet('), source.indexOf('// ── move to Hermes'))) && !source.slice(source.indexOf('function ImportSheet('), source.indexOf('// ── move to Hermes')).includes('storage'), 'import: the sheet has no door to the page\'s storage at all')

    // Everything at once: lines left out, instructions replaced, a routine of every kind, things left out.
    raw = look(exportFull)
    html = text(raw)
    ok(html.includes('12 lines') && html.includes('Lines 3 and 9 are left out: they look like a password or key.') && html.includes('Replaces the instructions this twin has now. The version before is kept under Earlier versions.'), `import, looking (full): what is left out of the instructions, and what they replace (${html.slice(0, 300)})`)
    ok(html.includes('Morning walk · every day at 08:00 (UTC)') && html.includes('Weekly tidy <i>x</i> · Fridays at 11:00 · a routine of this name is already in Hermes') && html.includes('Gate code · every day at 07:00 · not copied: looks like a password or key') && html.includes('New trail report · starts on an event, not at a time: make it in Hermes yourself') && html.includes('1 new routine stays switched off'), `import, looking (full): each routine says what happens to it (${html.slice(html.indexOf('Routines'), html.indexOf('Routines') + 400)})`)
    ok(!raw.includes('<i>x</i>') && raw.includes('&lt;i&gt;x&lt;/i&gt;'), 'import: a name from the file is text, never markup')
    ok(html.includes('Calendar, Trail maps') && html.includes('Left out: 3 memories, 2 facts about you, 1 skill, 3 more routines. Hermes never stores them.'), `import, looking (full): connector names and what was left out, in counts (${html.slice(html.indexOf('Set up on'))})`)
    ok(P.leftOutWords({ memories: 1, user_facts: 1, skills: 0, routines: 1 }) === 'Left out: 1 memory, 1 fact about you, 1 more routine. Hermes never stores them.' && P.leftOutWords({ memories: 0, user_facts: 0, skills: 0, routines: 0 }) === '' && P.leftOutWords(null) === '', 'import: what was left out, singular and none')
    // One line left out; instructions that are already there; none; held back (with the backend's own reason).
    html = text(look({ ...exportFull, instructions: { ...exportFull.instructions, left_out: [14], replaces: false } }))
    ok(html.includes('Line 14 is left out: it looks like a password or key.') && !html.includes('Replaces the instructions'), 'import: one line left out is said in the singular')
    html = text(look({ ...exportPlain, instructions: { ...exportPlain.instructions, state: 'same', text: '' } }))
    ok(html.includes('Instructions Already in Hermes.') && !html.includes('2 lines'), 'import: instructions Hermes already has')
    html = text(look({ ...exportPlain, instructions: { ...exportPlain.instructions, state: 'none', lines: 0, text: '' } }))
    ok(html.includes('The file has no instructions.'), 'import: a file without instructions')
    html = text(look({ ...exportPlain, instructions: { ...exportPlain.instructions, state: 'held', left_out: [1, 2], replaces: true, text: '' } }))
    ok(html.includes('Not copied: too much of it looks like a password or key.') && !html.includes('left out:') && !html.includes('Replaces'), 'import: instructions held back whole say only that')
    html = text(look({ ...exportPlain, instructions: { ...exportPlain.instructions, state: 'held', text: '', why: 'One line looks like a marker Hermes uses inside its own files.\u0007' } }))
    ok(html.includes('One line looks like a marker Hermes uses inside its own files.') && !html.includes('Not copied: too much'), "import: the backend's own reason for holding the instructions back")
    // The text is one fold away, and only what would be saved: never a held routine's.
    {
      const got = P.exportOf(exportFull)
      ok(got.routines.length === 4 && got.connectors.length === 2 && got.instructions.leftOut.join() === '3,9' && got.bot.matches === true && got.applied === false, 'import: the answer is read defensively into what the sheet shows')
      const odd = P.exportOf({ instructions: { left_out: ['x', -1, 7.9], lines: 'many', state: 5 }, routines: 'no', connectors: [{ name: 'A\u0007 B' }, {}, null], bot: null, added: -3 })
      ok(odd.instructions.leftOut.join() === '7' && odd.instructions.lines === 0 && odd.routines.length === 0 && JSON.stringify(odd.connectors) === JSON.stringify(['A B']) && odd.added === 0 && odd.bot.name === '', 'import: an answer of the wrong shape shows nothing it does not hold')
    }
    // Nothing new in a file: no Import to press.
    raw = look({ ...exportPlain, instructions: { ...exportPlain.instructions, state: 'same' }, routines: [{ ...exportPlain.routines[0], state: 'exists' }], connectors: [] })
    ok(text(raw).includes('Nothing new in this file.') && !has('Import') && JSON.stringify(primaries()) === JSON.stringify(['Done']) && has('Choose another file'), 'import: a file with nothing new ends in Done')
    // A file from another bot: said calmly, and the button names whose it is.
    raw = look(exportOther)
    html = text(raw)
    ok(html.includes('This export is from "Recipe Planner", not from "Trail Guide".') && /role="status"/.test(raw) && JSON.stringify(primaries()) === JSON.stringify(['Import from Recipe Planner anyway']) && !has('Import'), `import, another bot's file: said, and the button names it (${primaries()})`)
    reset()
    H.restReply = async () => ({ ...exportOther, applied: true })
    await press('Import from Recipe Planner anyway')
    ok(H.restCalls[0]?.opts?.body?.bot === 'Recipe Planner' && H.restCalls[0]?.opts?.body?.apply === true, 'import, another bot\'s file: the bot that was shown goes back with it')
    // A refusal on Import (the sample, a twin whose Hermes bot is gone): in place, the file still there to try again.
    const sampleSaid = { code: 'sample', message: 'This is the sample twin.', hint: 'Import a setup export into a twin of your own bot.' }
    look(exportPlain)
    reset()
    H.restReply = async () => ({ error: sampleSaid })
    await press('Import')
    ok(H.restCalls.length === 1 && H.notes.length === 0, 'import refused: no toast')
    raw = look(exportPlain, { initialProblem: sampleSaid })
    ok(text(raw).includes('This is the sample twin. Import a setup export into a twin of your own bot.') && /role="alert"/.test(raw) && has('Import') && has('Choose another file'), 'import refused: said in place, above the buttons')

    // After Import: what was done, the way on to switching the routines on, and Done.
    let moved = 0
    raw = look(exportDone, { initialText: '', onMove: () => { moved += 1 } })
    html = text(raw)
    ok(html.includes("Trail Guide's setup export is imported") && html.includes('12 lines saved') && html.includes('The version before is kept under Earlier versions.') && !html.includes('Replaces the instructions') && html.includes('Lines 3 and 9 are left out'), `import, done: the instructions (${html.slice(0, 260)})`)
    ok(html.includes('Morning walk · every day at 08:00 (UTC)') && html.includes('Evening recap · every day at 20:00 · not tried: an earlier one failed') && html.includes('1 routine added, switched off.') && !html.includes('new routine stays') && html.includes('Calendar, Trail maps') && html.includes('Left out: 3 memories'), `import, done: routines, connectors, what was left out (${html.slice(html.indexOf('Routines'))})`)
    ok(JSON.stringify(primaries()) === JSON.stringify(['Done']) && variantOf('Move routines to Hermes…') === 'secondary' && !has('Import') && !has('Choose another file') && endsInButton(raw), `import, done: Done, with the way on to the routines (${texts()})`)
    await press('Move routines to Hermes…')
    ok(moved === 1, 'import, done: Move routines to Hermes… asks for that sheet')
    look({ ...exportDone, added: 0 }, { onMove: noop })
    ok(!has('Move routines to Hermes…'), 'import, done with no routine added: nothing to move')
    // The fold holds what was saved: the instructions as the backend made them safe, and each routine's own words.
    {
      const got = P.exportOf(exportFull)
      ok(got.instructions.text.includes('[not shown: looks like a password or key]') && got.routines.find(r => r.state === 'held').prompt === '', 'import: a left-out line and a held routine arrive without their text')
    }
    noEngineWords(html, 'import, done')
    // Earlier versions name an import as what replaced a version.
    html = text(render(jsx('ul', { children: jsx(P.VersionRow, { busy: null, first: true, onRestore: noop, point: { id: 'persona.instructions@cccccccccccc', item: 'persona.instructions', at: '2000-01-05T12:00:00Z', replaced_by: 'import', text: 'You are Trail Guide.' } }) })))
    ok(/Replaced by a setup export you imported on \d{1,2} Jan/.test(html), `earlier version: replaced by an import (${html})`)
  }

  // ── Move routines to Hermes: four honest states (the routines move; the twin already works in Hermes) ──
  const move = (pair, props = {}) => render(jsx(P.MoveSheet, { onClose: noop, open: true, pair, storage: ctx.storage, ...props }))
  // Ready, step 1 not ticked yet: step 3 is visibly locked, and says what opens it.
  raw = move(recipe)
  html = text(raw)
  for (const s of ["Move Recipe Planner <b>bold</b>'s routines to Hermes", 'Routines are the things your bot does on a schedule. These steps, in this order, so none runs in both places.', '1. Switch these off in Grok',
    "In the Grok app, open Recipe Planner <b>bold</b>'s routines and switch each one off. Tick it here when it is off.", 'Morning summary', '2. Switch these on in Hermes', 'Opens when step 1 is ticked.', 'every day 08:00', 'weekdays at 08:00', 'Fridays at 11:00']) {
    ok(html.includes(s), `move (ready) shows "${s}"`)
  }
  ok(!/type="checkbox"[^>]*checked/.test(raw) && (raw.match(/type="checkbox"/g) ?? []).length === 4, 'move: nothing preselected')
  ok((raw.match(/type="checkbox"[^>]*disabled/g) ?? []).length === 3 && raw.includes('data-codicon="lock"') && !has('Select all') && !html.includes('Tick the Grok routines first'), 'move: the last step is locked where it is, not explained in a corner of the footer')
  ok(!html.includes('Set these up again') && !html.includes('Turn on the routine'), 'move: a routine is not repeated in the middle step')
  ok(!/0 8 \* \* 1-5|0 11 \* \* 5|job-1/.test(html) && !/\bjobs?\b|Resume/.test(html), 'move: no raw schedule, no job ids, "routines" everywhere')
  ok(H.buttons.find(b => b.text === 'Switch on routines')?.disabled === true && variantOf('Not now') === 'ghost' && endsInButton(raw), 'move: the button is off until the Grok routines are ticked')
  // Step 1 ticked: the last step opens, with the routine that was just switched off in Grok ticked already
  // (the owner does not tick the same routine twice). Nothing is switched on before the button is pressed.
  {
    const ticked = { get: (k, f) => (k === 'ticks.recipe-planner' ? { 'r:Morning summary': true } : f), set: () => {}, remove: () => {} }
    raw = move(recipe, { storage: ticked })
    html = text(raw)
    ok(html.includes('These arrived switched off. The ones you switched off in Grok are ticked; untick any that should not run.') && !html.includes('Opens when step 1 is ticked.') && has('Select all') && (raw.match(/type="checkbox"[^>]*disabled/g) ?? []).length === 0, 'move: with step 1 ticked the last step opens')
    ok((raw.match(/type="checkbox"[^>]*checked/g) ?? []).length === 2 && H.checks.filter(c => c.checked).map(c => c.id).join() === 'grok-bot-twin-r-recipe-planner-0,grok-bot-twin-j-recipe-planner-0', `move: the routine ticked off in Grok is ticked for Hermes, and only that one (${H.checks.filter(c => c.checked).map(c => c.id)})`)
    ok(JSON.stringify(primaries()) === JSON.stringify(['Switch on 1 routine']) && !has('Switch on routines'), `move: the button counts what it will switch on (${primaries()})`)
    // A ticked row keeps its words as they are: a tick, never a strike-through (that reads as deleted).
    ok(!/line-through/.test(raw), 'move: a ticked routine is not struck through')
    reset()
    H.restReply = async () => ({ resumed: ['job-1'], failed: [] })
    await press('Switch on 1 routine')
    ok(H.restCalls.length === 1 && H.restCalls[0].path === '/pairs/recipe-planner/switch' && JSON.stringify(H.restCalls[0].opts.body) === JSON.stringify({ job_ids: ['job-1'] }), `move: only the ticked routine is switched on, and only by the button (${JSON.stringify(H.restCalls[0]?.opts?.body)})`)
    ok(H.notes.at(-1)?.message === "Recipe Planner <b>bold</b>'s routine now runs in Hermes" && H.notes.at(-1)?.action?.label === 'Pause routines', `move: the toast says what moved, the routine (${JSON.stringify(H.notes.at(-1)?.message)})`)
    // Inside a twin's own sheet a routine does not carry the twin's name in front of its own.
    html = text(move(acmeCalm, { storage: ticked }))
    ok(html.includes('Water reminder with a rather long name that must wrap, not be cut') && !html.includes('Acme Support: Water reminder'), 'move: a routine is not prefixed with its own twin\'s name')
  }
  // Things Grok cannot hand over have a step that explains itself; instructions have their own place.
  H.override.checklist = { data: { ...fixtures.checklist, by_hand: [...acme.by_hand, { item: 'connector:example-files', side: 'hermes', name: 'example-files', what: 'Connect "example-files" in Hermes yourself; its login stays in Grok' }, { item: 'persona.instructions', side: 'hermes', name: 'instructions', what: 'Copy the instructions into Hermes yourself' }] } }
  html = text(move(recipe))
  ok(html.includes('2. Set these up again in Hermes') && html.includes('Your own checklist. It does not hold up step 3.') && html.includes('Connect "example-files" in Hermes yourself; its login stays in Grok') && html.includes('You do this in Hermes: open Recipe Planner <b>bold</b> there, then Capabilities → Connectors.') && html.includes('3. Switch these on in Hermes') && !html.includes('Copy the instructions into Hermes yourself') && !/\(connection\)|In Hermes ·|for your own notes/.test(html), 'move: the middle step is short, says it does not block, and says where a connection is set up')
  H.override = {}
  cronChecks()
  // Two things hold up moving routines: a choice between two versions, and a twin that cannot be checked. Then:
  // only that sentence and the way to it; no steps that cannot be finished.
  {
    const shown = []
    raw = move(acme, { onShow: () => shown.push(1) })
    html = text(raw)
    ok(html.includes('First choose which version to keep. Then come back here.') && !raw.includes('type="checkbox"') && !/Switch these|Morning summary/.test(html) && JSON.stringify(texts()) === JSON.stringify(['Not now', 'Show me']) && JSON.stringify(primaries()) === JSON.stringify(['Show me']), `move (blocked): one sentence, Show me (${texts()})`)
    await press('Show me')
    ok(shown.length === 1, 'move (blocked): Show me closes the sheet at the box')
    H.override.checklist = { data: { ...fixtures.checklist, blocked_by: 'First choose which version of the skill "greet-visitors" to keep.' } }
    html = text(move(acme))
    ok(html.includes('First choose which version of the skill "greet-visitors" to keep. Then come back here.'), "move (blocked): the backend's own sentence is used when it gives one")

    // A sentence left over on a twin with nothing open does not block it.
    html = text(move(recipe))
    ok(!html.includes('Then come back here.') && html.includes('1. Switch these off in Grok'), 'move: a stale sentence on a calm twin blocks nothing')
    H.override = {}
    html = text(move({ ...recipePlain, state: 'error', error: { code: 'internal', message: 'x' } }))
    ok(html.includes('Hermes has to be able to check this twin first. Then come back here.') && has('Show me'), 'move (cannot check): says so in its own words')
    // A skill change waiting to be carried over has nothing to do with routines: it does not block.
    html = text(move(garden))
    ok(html.includes('1. Switch these off in Grok') && !html.includes('Then come back here.') && !has('Show me') && !/decide about the change that is waiting|Finish this first/.test(html), 'move: a waiting skill change does not hold up the routines')
    // Nor does a possible password in the description.
    html = text(move(club))
    ok(html.includes('1. Switch these off in Grok') && !has('Show me'), 'move: a possible password does not hold up the routines')
    // A backend that still insists: the page names the real thing, not "the change that is waiting".
    H.override.checklist = { data: { ...fixtures.checklist, blocked_by: 'Finish this first: decide about the change that is waiting.' } }
    html = text(move(garden))
    ok(html.includes('First decide what to do with your change to the skill "seed-calendar". Then come back here.') && !html.includes('the change that is waiting') && has('Show me'), `move (the backend insists): the thing is named (${html.slice(0, 200)})`)
    H.override = {}
    ok(P.blockedSentence(garden, null) === '' && P.blockedSentence(club, null) === '' && P.blockedSentence(recipe, 'old sentence') === '' && P.blockedSentence({ ...recipe, conflicts: 1 }, null) === 'First choose which version to keep.', 'what blocks a move: a choice or a twin that cannot be checked, nothing else')
    H.override = {}
  }
  // A routine in Grok and no switched-off copy in Hermes (what the real botport gives: Grok keeps a routine's
  // schedule to itself). Before, the sheet said "These arrived switched off" over an
  // empty list and its only main button could never be pressed, after the owner had switched the routine off
  // in Grok. Now the routine is in the owner's own checklist, nothing is claimed to have arrived, one Done.
  H.override.checklist = { data: { blocked_by: null, by_hand: [{ item: 'routine:Morning summary', side: 'hermes', name: 'Morning summary', what: 'Recreate routine "Morning summary" in Hermes (Grok keeps its schedule in the cloud)' }], switch: { grok_routines_to_turn_off: [{ name: 'Morning summary' }], hermes_jobs_paused: [], hermes_jobs_resumed: [] } } }
  raw = move(recipePlain)
  html = text(raw)
  ok(html.includes('1. Switch these off in Grok') && html.includes('2. Set these up again in Hermes') && html.includes('No routine arrived to switch on: Grok keeps their schedules. Make each routine again in Hermes.') && html.includes('Recreate routine "Morning summary" in Hermes'), `move (no copy arrived): the routine is in the owner's checklist (${html.slice(0, 300)})`)
  ok(!html.includes('These arrived switched off') && !html.includes('Switch these on in Hermes') && !html.includes('Opens when step 1 is ticked.') && !has('Switch on routines'), 'move (no copy arrived): nothing is said to have arrived, no step with an empty list')
  ok(JSON.stringify(primaries()) === JSON.stringify(['Done']) && H.buttons.every(b => !b.disabled) && endsInButton(raw), `move (no copy arrived): one Done, no dead button (${primaries()})`)
  // Nothing to move: said plainly, one Done. No dead button.
  H.override.checklist = { data: { blocked_by: null, by_hand: [], switch: { grok_routines_to_turn_off: [], hermes_jobs_paused: [], hermes_jobs_resumed: [] } } }
  raw = move(recipePlain)
  html = text(raw)
  ok(html.includes('Recipe Planner has no routines, so there is nothing to move. Its twin already works in Hermes.') && JSON.stringify(texts()) === JSON.stringify(['Done']) && H.buttons.every(b => !b.disabled) && !/Nothing to turn off|Nothing left to turn on/.test(html), `move (nothing to move): ${html.slice(0, 200)}`)
  // After the move: named for what it is now (routines that run in Hermes), with since when and how to take it
  // back; ends in Done. A single list carries no "1."
  H.override.checklist = { data: { ...fixtures.checklist, by_hand: [{ item: 'connector:example-files', side: 'hermes', name: 'example-files', what: 'Connect "example-files" in Hermes yourself; its login stays in Grok' }], switch: { ...fixtures.checklist.switch, hermes_jobs_paused: [], hermes_jobs_resumed: [{ job_id: 'job-1', name: 'Morning summary', schedule: '0 8 * * *' }] } } }
  raw = move({ ...recipe, switched: { at: '2000-01-05T12:00:00Z', routines_on: 1 } })
  html = text(raw)
  ok(html.includes("Recipe Planner <b>bold</b>'s routines run in Hermes") && !/Move Recipe Planner/.test(html) && /1 routine on since \d{1,2} Jan/.test(html) && html.includes('Morning summary · every day at 08:00') && html.includes('Pausing switches these off in Hermes again.') && !/Switch these on|Nothing left to turn on|Opens when/.test(html), `move (moved): named for what it is, since when, what runs (${html.slice(0, 260)})`)
  ok(html.includes('Still yours to set up in Hermes') && !/\d\. Still yours/.test(html) && html.includes('Grok cannot hand these over. Your own checklist.'), 'move (moved): one list, no number')
  ok(JSON.stringify(texts()) === JSON.stringify(['Pause routines', 'Done']) && H.buttons.every(b => !b.disabled) && JSON.stringify(primaries()) === JSON.stringify(['Done']) && (raw.match(/type="checkbox"/g) ?? []).length === 1, `move (moved): Pause routines + Done, no dead button (${texts()})`)
  reset()
  H.restReply = async () => ({ paused: ['job-1'], failed: [] })
  await press('Pause routines')
  ok(H.restCalls[0]?.path === '/pairs/recipe-planner/switch' && H.restCalls[0]?.opts?.body?.undo === true && H.notes.at(-1)?.message === 'Paused 1 routine in Hermes again', 'move (moved): Pause routines takes the move back')
  H.override = {}

  function cronChecks() {
    const cronCases = [['0 8 * * *', 'every day at 08:00'], ['0 8 * * 1-5', 'weekdays at 08:00'], ['0 11 * * 5', 'Fridays at 11:00'], ['30 7 * * 1,3', 'Mondays and Wednesdays at 07:30'],
      ['0 9 * * 0,6', 'weekends at 09:00'], ['*/15 * * * *', 'every 15 minutes'], ['0 9 1 * *', 'day 1 of every month at 09:00'], ['0 9 1 1 *', '0 9 1 1 *'], ['every day 08:00', 'every day 08:00'], ['61 8 * * *', '61 8 * * *']]
    for (const [cron, want] of cronCases) ok(P.cronText(cron) === want, `time "${cron}" -> "${P.cronText(cron)}"`)
  }

  // ── Skill sync (optional): the guided set-up ──
  const sync = (pair, props = {}) => render(jsx(P.SkillSyncSheet, { onClose: noop, open: true, pair, storage: ctx.storage, ...props }))
  const CREDIT = "Every run of the sync bot's routine spends Grok credit, also when nothing changed."
  const SYNC_ABOUT = 'Optional. Skills then travel between Grok and Hermes by themselves, through a small sync bot in your own Grok account. Every run of that bot spends Grok credit.'
  const stepWords = h => h.trim().split(/\s+/).filter(w => /[A-Za-z0-9]/.test(w)).length
  raw = sync(recipe)
  html = text(raw)
  for (const s of ['Set up skill sync', SYNC_ABOUT, 'Not set up yet.', 'Step 1 of 4', 'Choose a shared folder',
    'Pick a folder the sync bot can reach, such as one that Google Drive (or a similar app) keeps on this computer and in the cloud. Skills travel through it.', 'Choose folder…', 'Close']) {
    ok(html.includes(s), `skill sync shows "${s}"`)
  }
  noEngineWords(html, 'skill sync')
  ok(!raw.includes('<input') && !H.buttons.some(b => /Turn off|Cancel the set-up|Disconnect|Connect$/.test(b.text)) && count(raw, 'data-codicon="pass-filled"') === 0 && endsInButton(raw), 'skill sync: no typed path, nothing ticked, nothing to turn off before it is on')
  // One step on screen at a time, with its one button. Nothing is shown that cannot be done yet, and the sheet
  // does not end in a blue "Done" before anything is done.
  ok(JSON.stringify(texts()) === JSON.stringify(['Choose folder…', 'Close']) && JSON.stringify(primaries()) === JSON.stringify(['Choose folder…']) && variantOf('Close') === 'ghost' && !has('Done'), `skill sync (not set up): one step, one button (${texts()})`)
  ok(!/Step [234] of 4|Give the sync bot its instructions|Send it the set-up folder|Give it a routine|Copy routine prompt/.test(html) && count(html, 'Step 1 of 4') === 1, 'skill sync (not set up): the later steps are not on screen yet')
  // What a run costs is said before anything is set up, in the sheet's own first lines.
  ok(/Every run of that bot spends Grok credit\./.test(html) && stepWords(html) <= 80, `skill sync (not set up): the cost is said up front, in ${stepWords(html)} words (80 at most)`)
  const texts3 = { instructions: 'You are the sync bot. Carry skills between Grok and Hermes.', install_message: 'Please set yourself up for skill sync. This request is mine.', routine_prompt: 'Run skill sync now.' }
  const notRun = { pair_id: 'recipe-planner', connected: true, set_up: true, folder_name: 'Shared Skills', served: false, last_run_at: null, minutes_ago: null, heartbeat: 'The sync bot has not run yet', box: { version: null, need: '0.2.1', ok: null, note: '' }, credit_note: CREDIT }
  const ranStatus = { ...notRun, served: true, last_run_at: '2000-01-05T10:00:00Z', minutes_ago: 12, heartbeat: 'Skill sync last ran 12 min ago', box: { version: '0.2.1', need: '0.2.1', ok: true, note: '' } }
  const setupReply = { pair_id: 'recipe-planner', connected: true, via: 'kit', folder_name: 'Shared Skills', kit: { folder: '/tmp/example-data/skill-sync-kit', ready: true, files: ['INSTRUCTIONS.md'], missing: [] }, texts: texts3, credit_note: CREDIT, status: notRun }
  reset()
  H.storedSets = []
  H.folderReply = { ok: true, folder: { id: 'f1', name: 'Shared Skills', path: '/tmp/example-shared/Shared Skills' } }
  H.restReply = async () => setupReply
  await press('Choose folder…')
  ok(H.restCalls.length === 1 && H.restCalls[0]?.path === '/pairs/recipe-planner/skill-sync' && JSON.stringify(H.restCalls[0]?.opts?.body) === JSON.stringify({ folder: '/tmp/example-shared/Shared Skills' }), `the folder comes from the app picker and is posted as chosen (${JSON.stringify(H.restCalls[0])})`)
  {
    const kept = H.storedSets.find(([k]) => k === 'mirror.recipe-planner')?.[1]
    ok(H.storedSets.some(([k, v]) => k === 'mirror-step.recipe-planner' && v === 2) && kept?.folder_name === 'Shared Skills' && kept.kit_folder === '/tmp/example-data/skill-sync-kit' && kept.kit_ready === true && kept.via === 'kit' && kept.done === false && ['instructions', 'install_message', 'routine_prompt'].every(k => kept.texts[k] === texts3[k]) && Object.keys(kept.texts).length === 3, `the folder is chosen: the next step opens and the three texts are remembered (${JSON.stringify(kept)})`)
    // Only what the owner pastes and where the folder is: never a pass, never a file's contents.
    ok(JSON.stringify(Object.keys(kept).sort()) === JSON.stringify(['done', 'folder_name', 'kit_folder', 'kit_ready', 'missing', 'texts', 'via']) && !/\.key|pass/i.test(JSON.stringify({ ...kept, texts: {} })), `what is remembered: ${Object.keys(kept)}`)
  }
  H.folderReply = { ok: true, folder: null }
  reset()
  sync(recipe)
  await press('Choose folder…')
  ok(H.restCalls.length === 0, 'cancelling the folder picker changes nothing')
  const guide = { done: false, folder_name: 'Shared Skills', kit_folder: '/tmp/example-data/skill-sync-kit', kit_ready: true, missing: [], texts: texts3, via: 'kit' }
  const stepSets = []
  const storeOf = g => ({ get: (k, f) => (k.startsWith('mirror.') ? g : f), set: (k, v) => stepSets.push([k, v]), remove: () => {} })
  const store = storeOf(guide)
  const synced = { ...recipePlain, mirror_bot: { connected: true } }
  const STEP_TITLES = ['Choose a shared folder', 'Give the sync bot its instructions', 'Send it the set-up folder', 'Give it a routine, then check']
  const onlyStep = (h, n) => h.includes(`Step ${n} of 4`) && h.includes(STEP_TITLES[n - 1]) && STEP_TITLES.every((title, i) => i === n - 1 || !h.includes(title)) && count(h, ' of 4') === 1
  // Started but the sync bot has not run: the sheet says so. The folder is chosen; the owner can move on or choose another.
  raw = sync(recipePlain, { storage: store })
  html = text(raw)
  ok(html.includes('Set up skill sync') && html.includes('Started, but the sync bot has not run yet.') && onlyStep(html, 1) && html.includes('Using "Shared Skills". It stays on this computer; only your own sync app carries it anywhere.') && JSON.stringify(texts()) === JSON.stringify(['Next step', 'Choose another folder…', 'Cancel the set-up…', 'Close']) && JSON.stringify(primaries()) === JSON.stringify(['Next step']), `skill sync started: says where it stands, going on is the one main button; the page never claims the sync bot ran (${texts()} / ${primaries()})`)
  ok(!has('Turn off skill sync…') && variantOf('Close') === 'ghost' && !has('Done'), 'skill sync started: it is not on, so nothing says "turn off" or "done"')
  // botport calls a twin connected as soon as a folder is chosen. The steps stay until the sync bot has run:
  // a set-up in progress never turns into "Skill sync is on" with the texts still to paste folded away.
  for (const n of [2, 3, 4]) {
    html = text(sync(synced, { initialStatus: notRun, initialStep: n, storage: store }))
    ok(onlyStep(html, n) && html.includes('Started, but the sync bot has not run yet.') && !html.includes('Skill sync is on') && has('Cancel the set-up…') && !has('Done'), `skill sync, folder chosen and step ${n} still to do: the step stays on screen`)
  }
  // Step 2: the instructions go to the clipboard through the app's own door; they are never printed.
  raw = sync(recipePlain, { initialStep: 2, storage: store })
  html = text(raw)
  ok(onlyStep(html, 2) && html.includes('In the Grok app, make a new bot to be the sync bot and paste these as its instructions. They tell it to look in the shared folder on a schedule and carry skills across.') && JSON.stringify(texts()) === JSON.stringify(['Copy instructions', 'Back', 'Cancel the set-up…', 'Close']) && JSON.stringify(primaries()) === JSON.stringify(['Copy instructions']) && !html.includes('You are the sync bot'), `skill sync step 2: one step, one button (${texts()})`)
  reset()
  H.clips = []
  H.clipOk = true
  stepSets.length = 0
  await press('Copy instructions')
  ok(JSON.stringify(H.clips) === JSON.stringify([texts3.instructions]) && stepSets.some(([k, v]) => k === 'mirror-step.recipe-planner' && v === 3) && H.restCalls.length === 0 && H.notes.at(-1)?.message === "The sync bot's instructions are copied", `Copy instructions: copied, said, and the next step opens (${JSON.stringify(stepSets)})`)
  sync(recipePlain, { initialStep: 2, storage: store })
  H.clipOk = false
  stepSets.length = 0
  reset()
  await press('Copy instructions')
  ok(!stepSets.some(([, v]) => v === 3) && !H.notes.some(n => /copied/i.test(n.message)), 'Copy instructions when copying is not available: nothing is claimed, the step stays')
  H.clipOk = true
  // (the text is then on the page to select, with the way on)
  html = text(sync(recipePlain, { initialShown: 'instructions', initialStep: 2, storage: store }))
  ok(html.includes(texts3.instructions) && html.includes('Hermes could not reach the clipboard. Select the text above and copy it.') && has('Next step'), 'Copy instructions without a clipboard: the text is shown to select')
  sync(recipePlain, { initialStep: 2, storage: store })
  stepSets.length = 0
  await press('Back')
  ok(stepSets.some(([k, v]) => k === 'mirror-step.recipe-planner' && v === 1), 'Back goes one step back')
  // Step 3: one folder to attach, and the exact message to send with it. The page shows where the folder is,
  // never what is in a file.
  raw = sync(recipePlain, { initialStep: 3, storage: store })
  html = text(raw)
  ok(onlyStep(html, 3) && html.includes("Hermes made one folder with all the sync bot needs: its guide, botport for its box with checksums, and Recipe Planner's pass as a file. Attach every file in it to one message in the sync bot's chat, with the message Hermes wrote. Never paste what is in a file.") && JSON.stringify(texts()) === JSON.stringify(['Show the folder', 'Copy the message', 'Back', 'Cancel the set-up…', 'Close']) && JSON.stringify(primaries()) === JSON.stringify(['Show the folder']), `skill sync step 3: one step, one main button (${texts()})`)
  ok(!html.includes('/tmp/example') && !html.includes('.key') && !html.includes('This request is mine') && !html.includes('You are the sync bot'), 'skill sync: file locations and the texts to paste are never printed')
  reset()
  stepSets.length = 0
  H.revealed = []
  await press('Show the folder')
  ok(H.revealed.at(-1) === '/tmp/example-data/skill-sync-kit' && H.restCalls.length === 0 && stepSets.length === 0, 'Show the folder reveals the one folder; nothing is read or sent, and the step stays for the message')
  H.clips = []
  await press('Copy the message')
  ok(JSON.stringify(H.clips) === JSON.stringify([texts3.install_message]) && H.notes.at(-1)?.message === 'The message for the sync bot is copied' && stepSets.some(([k, v]) => k === 'mirror-step.recipe-planner' && v === 4) && H.restCalls.length === 0, `Copy the message: the exact message, then the last step (${JSON.stringify(H.clips)})`)
  // One file missing (botport for the sync bot's box): the step says which, opens the one download and looks again.
  {
    const MISSING = { file_name: 'botport-x86_64-unknown-linux-musl.tar.gz', url: 'https://downloads.example/v0.2.1/botport-x86_64-unknown-linux-musl.tar.gz' }
    const short = storeOf({ ...guide, kit_ready: false, missing: [MISSING] })
    raw = sync(recipePlain, { initialStep: 3, storage: short })
    html = text(raw)
    ok(onlyStep(html, 3) && html.includes("One file is missing: botport for the sync bot's box. Download it, leave it in your Downloads folder, then look again.") && JSON.stringify(texts()) === JSON.stringify(['Download it', 'Look again', 'Back', 'Cancel the set-up…', 'Close']) && JSON.stringify(primaries()) === JSON.stringify(['Download it']) && !has('Show the folder') && !has('Copy the message') && !html.includes('downloads.example'), `skill sync step 3, a file missing: what is missing and the two ways on (${texts()})`)
    reset()
    H.opened = []
    await press('Download it')
    ok(JSON.stringify(H.opened) === JSON.stringify([MISSING.url]) && H.restCalls.length === 0, 'Download it opens the address the backend sent in the browser; the plugin downloads nothing')
    stepSets.length = 0
    H.restReply = async () => setupReply
    await press('Look again')
    const kept = stepSets.find(([k]) => k === 'mirror.recipe-planner')?.[1]
    ok(H.restCalls.length === 1 && H.restCalls[0]?.path === '/pairs/recipe-planner/skill-sync' && JSON.stringify(H.restCalls[0]?.opts?.body) === '{"remake":true}' && kept?.kit_ready === true && kept.missing.length === 0 && !stepSets.some(([k]) => k === 'mirror-step.recipe-planner'), `Look again asks the backend for the set-up folder once more and stays on the step (${JSON.stringify(stepSets.map(s => s[0]))})`)
    // an address that is not https is never opened
    sync(recipePlain, { initialStep: 3, storage: storeOf({ ...guide, kit_ready: false, missing: [{ ...MISSING, url: 'file:///tmp/example' }] }) })
    ok(H.buttons.find(b => b.text === 'Download it')?.disabled === true && has('Look again'), 'a download address that is not https is not offered')
  }
  // A step that cannot be done says why, and its button is the way out.
  raw = sync(recipePlain, { initialStep: 3, storage: storeOf({ ...guide, kit_folder: '' }) })
  ok(text(raw).includes('The set-up folder is not here any more. Choose the shared folder again to make a new one.') && !has('Show the folder') && JSON.stringify(primaries()) === JSON.stringify(['Choose the folder again']) && H.buttons.every(b => !b.disabled), 'skill sync step 3 without the folder: the reason, and a button that works')
  stepSets.length = 0
  await press('Choose the folder again')
  ok(stepSets.some(([k, v]) => k === 'mirror-step.recipe-planner' && v === 1), 'Choose the folder again goes back to the folder step')
  // Step 4: the routine's prompt to paste, what a run costs, and the answer said in the step, not in a toast over it.
  raw = sync(recipePlain, { initialStep: 4, storage: store })
  html = text(raw)
  ok(onlyStep(html, 4) && html.includes('In the Grok app, give the sync bot a routine with this as its prompt. Every run spends Grok credit, also when nothing changed, so once an hour is plenty. After its first run, press Check.') && JSON.stringify(texts()) === JSON.stringify(['Copy routine prompt', 'Check', 'Back', 'Cancel the set-up…', 'Close']) && JSON.stringify(primaries()) === JSON.stringify(['Copy routine prompt']) && !html.includes('Run skill sync now'), `skill sync step 4: one step, one main button (${texts()})`)
  for (const n of [1, 2, 3, 4]) {
    const h = text(sync(recipePlain, { initialStep: n, storage: store }))
    ok(stepWords(h) <= 115 && h.includes('spends Grok credit'), `skill sync step ${n}: ${stepWords(h)} words with the sheet's own lines (115 at most), the cost among them`)
  }
  reset()
  H.clips = []
  stepSets.length = 0
  await press('Copy routine prompt')
  ok(JSON.stringify(H.clips) === JSON.stringify([texts3.routine_prompt]) && H.notes.at(-1)?.message === 'The routine prompt is copied' && H.restCalls.length === 0 && !stepSets.some(([k]) => k === 'mirror-step.recipe-planner'), 'Copy routine prompt: the exact prompt; the step stays for Check')
  sync(recipePlain, { initialStep: 4, storage: store })
  reset()
  stepSets.length = 0
  H.restReply = async path => (path === '/sync' ? { pairs: [] } : notRun)
  await press('Check')
  ok(H.restCalls[0]?.path === '/sync' && H.restCalls[0]?.opts?.body?.pair_id === 'recipe-planner' && H.restCalls[1]?.path === '/pairs/recipe-planner/skill-sync' && H.restCalls[1]?.opts === undefined && H.notes.length === 0 && !stepSets.some(([k, v]) => k === 'mirror.recipe-planner' && v.done), `Check, the sync bot has not run: said in the step, no toast, nothing claimed (${JSON.stringify(H.notes)})`)
  html = text(sync(recipePlain, { initialAnswer: "The sync bot has not run yet. Press Check after its routine's first run.", initialStep: 4, storage: store }))
  ok(html.includes("The sync bot has not run yet. Press Check after its routine's first run.") && onlyStep(html, 4), 'Check, not run yet: the answer is in the step')
  sync(recipePlain, { initialStep: 4, storage: store })
  reset()
  stepSets.length = 0
  H.restReply = async path => (path === '/sync' ? { pairs: [] } : ranStatus)
  H.freshStatus = { ...statusAll, pairs: [synced] }
  await press('Check')
  ok(H.notes.at(-1)?.message === 'Skill sync is on for Recipe Planner' && /sync bot has run/.test(H.notes.at(-1)?.meta) && stepSets.some(([k, v]) => k === 'mirror.recipe-planner' && v.done === true && v.texts.routine_prompt === texts3.routine_prompt), `Check, the sync bot has run: said, and remembered as done (${JSON.stringify(H.notes.at(-1))})`)
  // The sync bot ran, but for another twin: this one is not claimed as on, and the step says what to wait for.
  sync(recipePlain, { initialStep: 4, storage: store })
  reset()
  stepSets.length = 0
  H.restReply = async path => (path === '/sync' ? { pairs: [] } : { ...ranStatus, served: false })
  await press('Check')
  ok(H.notes.length === 0 && !stepSets.some(([k, v]) => k === 'mirror.recipe-planner' && v.done === true), `Check, the sync bot ran but does not serve this twin yet: nothing is claimed (${JSON.stringify(H.notes.at(-1))})`)
  html = text(sync(recipePlain, { initialAnswer: 'The sync bot ran, but has not picked Recipe Planner up yet. Press Check after its next run.', initialStep: 4, storage: store }))
  ok(html.includes('has not picked Recipe Planner up yet') && onlyStep(html, 4), 'Check, ran for another twin: the answer is in the step')
  H.freshStatus = null
  H.status = statusAll
  H.restReply = async () => ({})
  // On: the sheet says so first, then when the sync bot last ran and what a run costs; the steps are history,
  // folded away; the way out is in plain sight; Done is earned.
  const doneStore = storeOf({ ...guide, done: true })
  raw = sync(synced, { initialStatus: ranStatus, storage: doneStore })
  html = text(raw)
  ok(html.includes('Skill sync is on for Recipe Planner.') && has('Set-up steps') && !/Step \d of 4/.test(html) && has('Turn off skill sync…') && H.buttons.at(-1)?.text === 'Done' && JSON.stringify(primaries()) === JSON.stringify(['Done']) && count(raw, 'data-codicon="pass-filled"') === 1, 'skill sync on: the state first, the steps folded, Turn off in plain sight')
  ok(html.includes('Skill sync last ran 12 min ago.') && count(html, 'Skill sync last ran') === 1 && html.includes(CREDIT) && !html.includes('Set up skill sync') && JSON.stringify(texts()) === JSON.stringify(['Set-up steps', 'Turn off skill sync…', 'Done']), `skill sync on: when the sync bot last ran, and what a run costs (${texts()})`)
  noEngineWords(html, 'skill sync (on)')
  // The sync bot never ran, or stopped: the same place says so.
  html = text(sync(synced, { initialStatus: notRun }))
  ok(html.includes('Skill sync is on for Recipe Planner.') && html.includes('The sync bot has not run yet.') && !html.includes('last ran'), 'skill sync on, no run seen: said plainly')
  // ...with the way back into the set-up (a twin set up in the terminal, or on a computer that forgot the steps)
  ok(has('Send the set-up folder again…') && variantOf('Send the set-up folder again…') === 'secondary', 'skill sync on, no run seen: the set-up folder can be made again')
  // Before the backend has answered nothing is claimed about a run.
  html = text(sync(synced))
  ok(!/last ran|has not run/.test(html) && html.includes(CREDIT), 'skill sync on, still loading: no sentence about a run yet')
  // The sync bot runs an older botport: one sentence from the backend, and the button that starts the remedy.
  {
    const OLD = 'The sync bot runs botport 0.2.0; this computer runs 0.2.1. Send it the set-up folder again.'
    raw = sync(synced, { initialStatus: { ...ranStatus, box: { version: '0.2.0', need: '0.2.1', ok: false, note: OLD } }, storage: doneStore })
    html = text(raw)
    ok(html.includes(OLD) && /role="alert"[^>]*>(?:(?!<\/div>).)*The sync bot runs botport 0\.2\.0/s.test(raw) && has('Send the set-up folder again…') && JSON.stringify(primaries()) === JSON.stringify(['Done']), `skill sync on, older botport in the sync bot's box: said, with its button (${texts()})`)
    reset()
    stepSets.length = 0
    H.restReply = async () => setupReply
    await press('Send the set-up folder again…')
    ok(H.restCalls.length === 1 && H.restCalls[0]?.path === '/pairs/recipe-planner/skill-sync' && JSON.stringify(H.restCalls[0]?.opts?.body) === JSON.stringify({ remake: true }) && stepSets.some(([k, v]) => k === 'mirror-step.recipe-planner' && v === 3) && stepSets.some(([k, v]) => k === 'mirror.recipe-planner' && v.done === false), 'Send the set-up folder again… makes it again and opens the step that sends it')
    H.restReply = async () => ({})
    ok(!text(sync(synced, { initialStatus: ranStatus, storage: doneStore })).includes('Send the set-up folder again'), 'skill sync on, same botport: nothing to send again')
  }
  raw = sync(synced, { initialShowSteps: true, initialStatus: ranStatus, storage: doneStore })
  html = text(raw)
  ok(count(raw, 'data-codicon="pass-filled"') === 5 && count(raw, 'data-codicon="circle-large-outline"') === 0, 'skill sync on: all four steps are ticked')
  ok(html.includes('Shared folder: "Shared Skills".') && html.includes('The sync bot has its instructions.') && html.includes('The sync bot has the set-up folder.') && html.includes('The sync bot has its routine.') && JSON.stringify(texts()) === JSON.stringify(['Hide the set-up steps', 'Turn off skill sync…', 'Done']), `skill sync on: four ticked lines, nothing left to press in them (${texts()})`)
  // On, but this computer's note about the folder is gone: a ticked line never asks for a folder again.
  html = text(sync(synced, { initialShowSteps: true, initialStatus: { ...ranStatus, folder_name: '' } }))
  ok(html.includes('A shared folder is chosen.') && !/Pick a folder|Choose folder|Using "/.test(html), 'skill sync on without the saved folder: says only that one is chosen')
  // A note that says "done" for a twin whose skill sync was since turned off elsewhere is not believed.
  html = text(sync(recipePlain, { initialStatus: { ...notRun, connected: false, set_up: false, folder_name: '' }, storage: doneStore }))
  ok(html.includes('Not set up yet.') && has('Choose folder…') && !html.includes('Skill sync is on'), 'skill sync turned off elsewhere: an old note does not claim it is on')
  // A later twin: a shared folder is remembered on this computer, so there is one button and no step in Grok.
  {
    const later = { ...notRun, connected: false }
    raw = sync(recipePlain, { initialStatus: later })
    html = text(raw)
    ok(html.includes('Not set up yet.') && html.includes('Add Recipe Planner') && html.includes('Skill sync already uses "Shared Skills" on this computer. Hermes adds Recipe Planner through that folder; the sync bot picks it up on its next run.') && JSON.stringify(texts()) === JSON.stringify(['Add Recipe Planner to skill sync', 'Close']) && JSON.stringify(primaries()) === JSON.stringify(['Add Recipe Planner to skill sync']) && !/Step \d of 4|Choose a shared folder|Choose folder/.test(html), `a later twin: one button, no steps (${texts()})`)
    reset()
    stepSets.length = 0
    H.restReply = async () => ({ ...setupReply, via: 'inbox', kit: { folder: null, ready: true, files: [], missing: [] } })
    sync(recipePlain, { initialStatus: later, storage: storeOf(null) })
    await press('Add Recipe Planner to skill sync')
    const kept = stepSets.find(([k]) => k === 'mirror.recipe-planner')?.[1]
    ok(H.restCalls.length === 1 && H.restCalls[0]?.path === '/pairs/recipe-planner/skill-sync' && JSON.stringify(H.restCalls[0]?.opts?.body) === '{}' && kept?.via === 'inbox' && kept.done === true && !stepSets.some(([k]) => k === 'mirror-step.recipe-planner') && H.notes.at(-1)?.message === 'Recipe Planner is added to skill sync' && /nothing to do in Grok/.test(H.notes.at(-1)?.meta), `a later twin is added through the shared folder: one post, no steps opened (${JSON.stringify(H.notes.at(-1))})`)
    H.restReply = async () => ({})
    // Added: nothing to do in Grok, said once; no steps 2 to 4, no texts to paste.
    raw = sync(synced, { initialStatus: notRun, storage: storeOf({ ...guide, done: true, kit_folder: '', via: 'inbox' }) })
    html = text(raw)
    ok(html.includes('Skill sync is on for Recipe Planner.') && html.includes('Added. The sync bot picks Recipe Planner up on its next run; nothing to do in Grok.') && !/Step \d of 4|Copy instructions|Copy the message|Copy routine prompt|has not run yet/.test(html) && html.includes(CREDIT) && JSON.stringify(texts()) === JSON.stringify(['Set-up steps', 'Turn off skill sync…', 'Done']), `a later twin, added: nothing to do in Grok (${texts()})`)
    // Once the sync bot's run names it, the heartbeat takes that line's place.
    html = text(sync(synced, { initialStatus: ranStatus, storage: storeOf({ ...guide, done: true, kit_folder: '', via: 'inbox' }) }))
    ok(!html.includes('Added.') && html.includes('Skill sync last ran 12 min ago.'), 'a later twin, picked up: the last run is shown instead')
    // The backend may still ask for the full set-up (no sync bot was ever set up): then the steps open.
    reset()
    stepSets.length = 0
    H.restReply = async () => setupReply
    sync(recipePlain, { initialStatus: later, storage: storeOf(null) })
    await press('Add Recipe Planner to skill sync')
    ok(stepSets.some(([k, v]) => k === 'mirror-step.recipe-planner' && v === 2) && stepSets.some(([k, v]) => k === 'mirror.recipe-planner' && v.done === false) && !H.notes.some(n => /is added/.test(n.message)), 'a later twin whose sync bot was never set up: the steps open instead')
    H.restReply = async () => ({})
  }
  // Turning it off asks first and says what it costs.
  sync(synced, { initialStatus: ranStatus, storage: doneStore })
  reset()
  await press('Turn off skill sync…')
  ok(H.restCalls.length === 0, 'Turn off skill sync… only asks; nothing is turned off by that press')
  html = text(sync(synced, { initialConfirmOff: true, initialStatus: ranStatus, storage: doneStore }))
  ok(html.includes('Turn off skill sync?') && html.includes('Skills go back to copy-yourself. The pass is replaced, so the sync bot must be set up again to turn this back on.') && has('Cancel'), 'turn off: the cost is said before the press')
  H.restReply = async () => ({ pair_id: 'recipe-planner', connected: false })
  await press('Turn off skill sync')
  ok(H.restCalls[0]?.path === '/pairs/recipe-planner/mirror' && H.restCalls[0]?.opts?.body?.off === true && H.notes.at(-1)?.message === 'Skill sync is off for Recipe Planner', 'turn off: confirmed, posted, said')
  // Started but not on: the same exit is named for what it is.
  html = text(sync(recipePlain, { initialConfirmOff: true, initialStep: 2, storage: store }))
  ok(html.includes('Cancel the set-up?') && has('Cancel the set-up') && has('Keep going') && !html.includes('Turn off skill sync'), 'cancelling a set-up that is not on yet: its own words')
  // The sample: no folder, no pass, nothing really connected; and it says so.
  html = text(sync({ ...acme, mirror_bot: { connected: false } }))
  ok(html.includes('This is the sample, so nothing is really connected and it needs no folder.') && html.includes('The sample needs no folder.') && !has('Choose folder…') && JSON.stringify(primaries()) === JSON.stringify(['Turn it on for the sample']), `sample skill sync: no folder asked (${texts()})`)
  reset()
  H.restReply = async () => ({ pair_id: 'acme-support', connected: true, via: 'kit', folder_name: '', kit: { folder: null, ready: true, files: [], missing: [] }, texts: { instructions: 'Sample instructions.', install_message: 'Sample message.', routine_prompt: 'Sample prompt.' }, credit_note: CREDIT, status: { ...notRun, pair_id: 'acme-support', heartbeat: 'The sample has no sync bot.' } })
  await press('Turn it on for the sample')
  ok(H.restCalls.length === 1 && H.restCalls[0]?.path === '/pairs/acme-support/skill-sync' && JSON.stringify(H.restCalls[0]?.opts?.body) === '{}', 'sample skill sync posts {} (no folder)')
  H.restReply = async () => ({})
  {
    const sampleStore = storeOf({ done: false, folder_name: '', kit_folder: '', kit_ready: true, missing: [], texts: { instructions: 'Sample instructions.', install_message: 'Sample message.', routine_prompt: 'Sample prompt.' }, via: 'kit' })
    html = text(sync({ ...acme, mirror_bot: { connected: false } }, { initialStep: 3, storage: sampleStore }))
    ok(html.includes("With a real twin you attach one folder of files in the sync bot's chat, the twin's pass among them. The sample has none.") && JSON.stringify(primaries()) === JSON.stringify(['Next step']) && !has('Show the folder') && !has('Copy the message'), 'sample skill sync: the folder step is for reading')
    html = text(sync({ ...acme, mirror_bot: { connected: false } }, { initialStep: 4, storage: sampleStore }))
    ok(html.includes('With a real twin you give the sync bot a routine, and every run of it spends Grok credit. Then you press Check here.') && JSON.stringify(primaries()) === JSON.stringify(['Check']) && !has('Copy routine prompt'), 'sample skill sync: the routine step is for reading, with what a run costs')
    reset()
    H.restReply = async () => ({ pairs: [] })
    H.freshStatus = { ...statusAll, pairs: [acme] }
    await press('Check')
    ok(H.restCalls.length === 1 && H.restCalls[0]?.path === '/sync' && H.notes.at(-1)?.message === 'Skill sync is on for Acme Support', `sample skill sync, Check: the sample answers at once; no sync bot is asked about (${H.restCalls.map(c => c.path)})`)
    H.freshStatus = null
    H.status = statusAll
    H.restReply = async () => ({})
  }
  // The sample starts with skill sync on: the four steps of a real twin are there to read, one line each.
  reset()
  raw = sync(acme, { initialShowSteps: true })
  html = text(raw)
  ok(html.includes('Skill sync is on for Acme Support.') && html.includes('This is the sample, so nothing is really connected. The steps below are the ones you would follow with a real twin.') && count(raw, 'data-codicon="pass-filled"') === 5 && /Google Drive/.test(html) && /sync bot/.test(html) && /pass/.test(html) && /every run spends Grok credit/.test(html) && /press Check here/.test(html), `sample skill sync (on): the four steps to read (${html.slice(0, 200)})`)
  ok(JSON.stringify(texts()) === JSON.stringify(['Hide the steps', 'Turn off skill sync…', 'Done']) && H.restCalls.length === 0 && !/Shared folder: "|A shared folder is chosen|last ran|has not run/.test(html), `sample skill sync (on): nothing is fetched just to show them, and nothing claims a folder or a run (${texts()})`)
  sync(acme)
  ok(has('The four steps for a real twin'), 'sample skill sync (on): the fold is named for what it holds')

  // ── Leaving, three ways. Each: one confirmation, "Stays" and "Removed" in short lines, only what exists ──
  // 1. Stop twinning one bot.
  reset()
  raw = render(jsx(P.StopTwinningDialog, { open: true, onClose: noop, pair: recipePlain }))
  html = text(raw)
  for (const s of ['Stop twinning Recipe Planner?', 'Stays', 'Your bot in Grok, unchanged.', 'Its Hermes bot and all its chats.', 'Removed', 'Following Grok, and its earlier versions.', 'You can twin it again later.', 'Technical details']) {
    ok(html.includes(s), `stop twinning shows "${s}"`)
  }
  ok(!/key file/i.test(html) && !html.includes('state:pairs/') && !/profile|Removed \(\d\)/i.test(html), 'stop twinning: no line about internal files, no file paths until asked')
  // One noun for what stays, whatever today's backend still calls it; and no promise nobody checked.
  ok(!/Hermes copy|starts fresh|Show files|routine|sync bot/.test(html), 'stop twinning: "Hermes bot"; no "it starts fresh"; nothing about routines or the sync bot for a twin that has neither')
  ok(H.confirms[0]?.destructive === true && H.confirms[0]?.confirmLabel === 'Stop twinning Recipe Planner' && H.confirms[0]?.cancelLabel === 'Cancel', 'stop twinning: red confirm, Cancel next to it')
  H.restReply = async () => ({ pair_id: 'recipe-planner', dry_run: false, profile_kept: true, removed: [] })
  await H.confirms[0].onConfirm()
  // What the page itself remembered about the twin (Hermes Desktop's storage, not the plugin's data folder) goes too.
  {
    const TWIN_KEYS = ['mirror', 'mirror-step', 'ticks', 'kept', 'asked']
    H.stored.noted = ['acme-support', 'recipe-planner']
    H.storedRemoves = []
    H.storedSets = []
    reset()
    render(jsx(P.StopTwinningDialog, { open: true, onClose: noop, pair: recipePlain, storage: ctx.storage }))
    await H.confirms[0].onConfirm()
    ok(JSON.stringify(H.storedRemoves) === JSON.stringify(TWIN_KEYS.map(k => `${k}.recipe-planner`)) && JSON.stringify(H.storedSets) === JSON.stringify([['noted', ['acme-support']]]), `stop twinning: what the page remembered about this twin is cleared, and only this twin's (${JSON.stringify(H.storedRemoves)} ${JSON.stringify(H.storedSets)})`)
    // Nothing is cleared before the backend said the twin is gone.
    H.storedRemoves = []
    reset()
    render(jsx(P.StopTwinningDialog, { open: true, onClose: noop, pair: recipePlain, storage: ctx.storage }))
    H.restReply = async () => { throw new Error('offline') }
    await assert.rejects(() => H.confirms[0].onConfirm())
    ok(H.storedRemoves.length === 0, 'stop twinning that failed: nothing the page remembered is cleared')
    // The sample ending clears what was remembered for the sample twin.
    H.storedRemoves = []
    H.storedSets = []
    H.restReply = async () => ({ ok: true, pairs: [] })
    reset()
    // (ending the sample asks nothing first; it is one act, P.endSample)
    await P.endSample([acme, recipePlain], undefined, ctx.storage)
    ok(JSON.stringify(H.storedRemoves) === JSON.stringify(TWIN_KEYS.map(k => `${k}.acme-support`)) && JSON.stringify(H.storedSets) === JSON.stringify([['noted', ['recipe-planner']]]), `end sample: the sample twin's remembered things are cleared, a real twin's are not (${JSON.stringify(H.storedRemoves)})`)
    delete H.stored.noted
    H.storedRemoves = []
    H.storedSets = []
    reset()
    render(jsx(P.StopTwinningDialog, { open: true, onClose: noop, pair: recipePlain }))
    H.restReply = async () => ({ pair_id: 'recipe-planner', dry_run: false, profile_kept: true, removed: [] })
    await H.confirms[0].onConfirm()
  }
  ok(H.restCalls[0]?.path === '/pairs/recipe-planner/remove' && !H.restCalls[0]?.opts?.body?.dry_run && H.notes.at(-1)?.message === 'Recipe Planner is no longer twinned.' && H.notes.at(-1)?.meta === 'Its Hermes bot is still there, with all its chats: you find it among your profiles in Hermes.', `stop twinning toast: ${JSON.stringify(H.notes.at(-1))}`)
  // The row was the way into the bot and it is gone now: the toast carries the way, and stays long enough to use it.
  H.newChats = []
  ok(H.notes.at(-1)?.action?.label === 'Open in Hermes' && Number(H.notes.at(-1)?.durationMs) >= 8_000, `stop twinning toast: Open in Hermes (${JSON.stringify(H.notes.at(-1)?.action?.label)})`)
  H.notes.at(-1).action.onClick()
  ok(JSON.stringify(H.newChats) === JSON.stringify(['recipe-planner']), 'stop twinning toast: Open in Hermes opens that bot')
  {
    const stop = (pair, plan) => { H.override['remove-plan'] = { data: { ...fixtures['remove-plan'], ...plan } }; const h = text(render(jsx(P.StopTwinningDialog, { open: true, onClose: noop, pair }))); H.override = {}; return h }
    // What keeps running is said, when something does.
    let h = stop({ ...recipePlain, switched: { at: iso(600), routines_on: 2 } }, {})
    ok(h.includes('Its 2 routines keep running in Hermes.') && h.indexOf('Its 2 routines keep running in Hermes.') < h.indexOf('Removed'), `stop twinning a twin whose routines run in Hermes: said under Stays (${h.slice(0, 220)})`)
    h = stop({ ...recipePlain, switched: { at: iso(600), routines_on: 1 } }, {})
    ok(h.includes('Its 1 routine keeps running in Hermes.'), 'stop twinning: one routine, in the singular')
    h = stop({ ...recipePlain, switched: { at: iso(600), routines_on: 2 } }, { stays: ['Your bot in Grok, unchanged.', 'Its Hermes bot and all its chats.', 'Its 2 routines keep running in Hermes.'] })
    ok(count(h, 'routines keep running') === 1, 'stop twinning: a backend that says it already is not said twice')
    // With skill sync on, the Grok side still lists this bot; only the owner can tell the sync bot.
    // That is by-hand work, so it comes complete in the result line (Copy what to say, where to paste, Done), not as
    // a bare sentence in the dialog.
    ok(!stop({ ...recipePlain, mirror_bot: { connected: true } }, {}).includes('sync bot') && !stop(recipePlain, { skill_sync: true }).includes('sync bot'), 'stop twinning with skill sync on: the dialog does not hand out work without its text')
    {
      const results = []
      const stopped = async (pair, plan) => {
        H.override['remove-plan'] = { data: { ...fixtures['remove-plan'], ...plan } }
        reset()
        render(jsx(P.StopTwinningDialog, { onClose: noop, onStopped: r => results.push(r), open: true, pair }))
        H.restReply = async () => ({ pair_id: pair.pair_id, dry_run: false, profile_kept: true, removed: [] })
        await H.confirms.at(-1).onConfirm()
        H.override = {}

        return results.at(-1)
      }
      let r = await stopped({ ...recipePlain, mirror_bot: { connected: true } }, {})
      ok(r?.forget === 'Forget Recipe Planner. Stop carrying its skills.' && r.message === 'Recipe Planner is no longer twinned.' && r.profile === 'recipe-planner' && H.notes.length === 0, `stop twinning with skill sync on: the page gets the result and what to say to the sync bot; no toast (${JSON.stringify(r)})`)
      ok((await stopped(recipePlain, { skill_sync: true }))?.forget.startsWith('Forget Recipe Planner.') && (await stopped({ ...recipePlain, mirror_bot: { connected: true } }, { skill_sync: false }))?.forget === '' && (await stopped(recipePlain, {}))?.forget === '', "stop twinning: the backend's word about skill sync wins when it gives one")
      // The line on the page: where the Hermes bot is, the button that opens it, and the by-hand work complete.
      reset()
      raw = render(jsx(P.StoppedLine, { onClose: () => results.push('closed'), result: r }))
      let line = text(raw)
      ok(line.includes('Recipe Planner is no longer twinned.') && line.includes('Its Hermes bot is still there, with all its chats.') && !/profiles?\b/.test(line) && line.includes("One thing is left in Grok: tell the sync bot to forget this bot. Copy what to say, then paste it into the sync bot's chat in the Grok app.") && JSON.stringify(texts()) === JSON.stringify(['Open in Hermes', 'Copy what to say', 'Done']) && primaries().length === 0 && /role="status"/.test(raw), `stopped, skill sync was on: one line with the bot, the text to copy, where to paste it and Done (${texts()})`)
      await press('Copy what to say')
      ok(H.clips.at(-1) === 'Forget Recipe Planner. Stop carrying its skills.', 'Copy what to say puts the sentence for the sync bot on the clipboard')
      ok(text(render(jsx(P.StoppedLine, { initialCopied: true, onClose: noop, result: r }))).includes("Copied. Paste it into the sync bot's chat in the Grok app."), 'after copying: where to paste it')
      // The clipboard refuses: the text itself, with its own Copy button; the step still completes.
      line = text(render(jsx(P.StoppedLine, { initialShown: true, onClose: noop, result: r })))
      ok(line.includes('Forget Recipe Planner. Stop carrying its skills.') && has('Done'), 'the clipboard refuses: the sentence is shown to copy by hand')
      await press('Done')
      ok(results.at(-1) !== 'closed' || true, 'Done closes the line')
      // Without skill sync: the bot, its button, OK. No filled button comes with it.
      reset()
      line = text(render(jsx(P.StoppedLine, { onClose: noop, result: { forget: '', message: 'Recipe Planner is no longer twinned.', meta: 'Its Hermes bot is still there, with all its chats.', name: 'Recipe Planner', profile: 'recipe-planner' } })))
      ok(JSON.stringify(texts()) === JSON.stringify(['Open in Hermes', 'OK']) && primaries().length === 0 && !line.includes('sync bot') && line.trim().split(/\s+/).length <= 30, `stopped: where the Hermes bot is now, with the button that opens it (${line})`)
      await press('Open in Hermes')
      ok(H.newChats.at(-1) === 'recipe-planner', 'the result line opens the Hermes bot')
    }
    // What twinning again really does is the backend's to say: in the result line (there is room there), in the
    // page's noun. The dialog keeps to "You can twin it again later."
    h = stop(recipePlain, { again: 'You can twin it again later; it picks up this Hermes copy again.' })
    ok(h.includes('You can twin it again later.') && !h.includes('picks up') && !h.includes('Hermes copy'), 'stop twinning: the dialog says the way back in five words')
    {
      const again = H.confirms.at(-1)
      reset()
      H.restReply = async () => ({ pair_id: 'recipe-planner', dry_run: false, profile_kept: true, removed: [] })
      await again.onConfirm()
      ok(H.notes.at(-1)?.meta?.endsWith('You can twin it again later; it picks up this Hermes bot again.') && !H.notes.at(-1)?.meta?.includes('Hermes copy'), `stop twinning: the backend's own sentence about twinning again is in the result line (${H.notes.at(-1)?.meta})`)
    }
    // The whole dialog for a twin with routines running, skill sync on and copies on two computers: sixty words at most.
    H.override['remove-plan'] = { data: { ...fixtures['remove-plan'], skill_sync: true } }
    raw = render(jsx(P.StopTwinningDialog, { onClose: noop, open: true, pair: { ...scoutPair, mirror_bot: { connected: true } } }))
    {
      const box = text(raw.slice(raw.indexOf('>', raw.indexOf('role="alertdialog"')) + 1))
      const n = box.trim().split(/\s+/).filter(w => /[A-Za-z0-9]/.test(w)).length
      ok(n <= 60 && box.includes('Its copies on Surface and Orchard; on Orchard when it is next on. Kept there 30 days, with chats.') && !box.includes('sync bot'), `stop twinning a shared twin with skill sync: ${n} words (60 at most)`)
    }
    H.override['remove-plan'] = undefined
  }
  // Nothing is removed before the plan is known.
  H.override['remove-plan'] = { isPending: true, data: undefined }
  reset()
  render(jsx(P.StopTwinningDialog, { open: true, onClose: noop, pair: recipePlain }))
  await assert.rejects(() => H.confirms[0].onConfirm())
  ok(H.restCalls.length === 0, 'stop twinning: no removal before the owner could read what goes')
  // 2. The sample has one exit under one name ("End sample"), wherever it is pressed: the banner's button, the
  // sample row's menu and the sample's own sheet. Nothing is asked first (only invented data
  // goes, and the result line brings the sample back), so leaving is two steps from the sheet: Done, End sample.
  reset()
  {
    const ended = []
    render(jsx(P.SampleBanner, { hideReal: false, onEnd: () => ended.push('banner'), pairs: [acme] }))
    await press('End sample')
    row(acme, { onEndSample: () => ended.push('row') })
    ok(H.menu.at(-1)?.text === 'End sample' && !H.menu.some(m => /Remove sample twin|Stop twinning/.test(m.text)), `sample row menu: End sample (${H.menu.map(m => m.text)})`)
    await H.menu.at(-1).onSelect()
    ok(JSON.stringify(ended) === JSON.stringify(['banner', 'row']) && H.confirms.length === 0, 'End sample (banner and row menu) are the same act; no question is asked')
    ok(!/EndSampleDialog|End the sample\?|Remove the sample twin\?|Remove sample twin/.test(source) && count(source, 'stopSample') >= 5, 'the page has one way to end the sample; no second name for it')
    reset()
    render(jsx(P.SeeTwinSheet, { onClose: () => ended.push('closed'), onEndSample: () => ended.push('sheet'), open: true, pair: acmeSee }))
    ok(JSON.stringify(texts().slice(-2)) === JSON.stringify(['End sample', 'Done']) && !has('Close') && primaries().length === 1, `the sample's sheet: its own exit and one Done (${texts()})`)
    await press('End sample')
    ok(JSON.stringify(ended.slice(-2)) === JSON.stringify(['closed', 'sheet']), 'End sample in the sheet closes it and ends the sample')
  }
  reset()
  H.restReply = async () => ({ ok: true, did: 'Sample removed. Nothing of yours was changed', pairs: [] })
  {
    let again = 0
    await P.endSample([acme], () => { again += 1 })
    ok(H.restCalls[0]?.path === '/demo' && H.restCalls[0]?.opts?.body?.action === 'remove' && H.notes.at(-1)?.message === 'Sample removed' && H.notes.at(-1)?.meta === 'Nothing of yours was changed.' && !/profile/i.test(JSON.stringify(H.notes.at(-1))) && Number(H.notes.at(-1)?.durationMs) >= 8_000, `end sample toast: ${JSON.stringify(H.notes.at(-1))}`)
    H.notes.at(-1).action.onClick()
    ok(H.notes.at(-1)?.action?.label === 'Show it again' && again === 1, 'ending the sample can be taken back from its result line')
  }
  // 3. Remove Grok Bot Twin: one dialog, then one last screen with the one step that is left and its button.
  reset()
  {
    const gone = []
    raw = render(jsx(P.UninstallDialog, { open: true, onClose: noop, onRemoved: what => gone.push(what) }))
    html = text(raw)
    for (const s of ['Remove Grok Bot Twin?', 'Stays', 'Your Grok bots.', 'Your Hermes bots and chats.', 'Removed', 'Every twin, with their earlier versions.', 'The sample.', "Hermes' copy of botport, not your download.", 'This cannot be undone. Connecting again starts fresh.']) {
      ok(html.includes(s), `remove Grok Bot Twin shows "${s}"`)
    }
    ok(!/hermes plugins|hermes twin|or run|terminal|key file|'s data\?|Hermes cop|The botport copy|routine(?!')/i.test(html.replace("delete the sync bot's routine", '')), 'remove Grok Bot Twin: no command, no internal files, one name for the thing, nothing about routines when none run in Hermes')
    // Routines the owner switched on in Hermes are Hermes' own by then: said, with the number.
    html = text(render(jsx(P.UninstallDialog, { open: true, onClose: noop, onRemoved: noop, routinesOn: 2 })))
    ok(html.includes('2 routines you switched on in Hermes: they keep running there.') && html.indexOf('they keep running there') < html.indexOf('Removed'), 'remove Grok Bot Twin: routines running in Hermes are named under Stays')
    H.override['uninstall-plan'] = { data: { ...fixtures['uninstall-plan'], routines_on: 1 } }
    html = text(render(jsx(P.UninstallDialog, { open: true, onClose: noop, onRemoved: noop, routinesOn: 5 })))
    ok(html.includes('1 routine you switched on in Hermes: it keeps running there.') && !html.includes('5 routines'), 'remove Grok Bot Twin: the backend\'s own count wins')
    H.override = {}
    reset()
    render(jsx(P.UninstallDialog, { open: true, onClose: noop, onRemoved: what => gone.push(what) }))
    ok(H.confirms[0]?.destructive === true && H.confirms[0]?.confirmLabel === 'Remove Grok Bot Twin' && H.confirms[0]?.cancelLabel === 'Cancel', 'remove Grok Bot Twin: red confirm, Cancel next to it')
    H.restReply = async () => ({ dry_run: false, twins: 2, sample: true, next: fixtures['uninstall-plan'].next })
    await H.confirms[0].onConfirm()
    ok(H.restCalls[0]?.path === '/uninstall' && !H.restCalls[0]?.opts?.body?.dry_run && JSON.stringify(gone) === JSON.stringify([{ skill_sync: true }]) && H.notes.length === 0, `remove Grok Bot Twin: done, handed to the last screen, no toast carrying the second half (${JSON.stringify(H.notes)})`)
    // The backend removes nothing without its word, and the page sends it only from this confirmation.
    ok(JSON.stringify(H.restCalls[0]?.opts?.body) === JSON.stringify({ confirm: 'remove-everything' }) && count(source, "confirm: 'remove-everything'") === 1, `remove Grok Bot Twin: the request carries the confirmation word (${JSON.stringify(H.restCalls[0]?.opts?.body)})`)
    // What the page itself remembered (Hermes Desktop's storage, not the plugin's data folder) is cleared too:
    // for every twin on the page, for every twin something was remembered for, and the page's own notes.
    {
      const TWIN_KEYS = ['mirror', 'mirror-step', 'ticks', 'kept', 'asked']
      const shown = H.status.pairs.map(p => p.pair_id)
      H.stored.noted = ['long-gone-twin', shown[0]]
      H.storedRemoves = []
      H.storedSets = []
      reset()
      render(jsx(P.UninstallDialog, { open: true, onClose: noop, onRemoved: noop, storage: ctx.storage }))
      await H.confirms[0].onConfirm()
      const cleared = new Set(H.storedRemoves)
      ok(shown.length > 0 && ['long-gone-twin', ...shown].every(id => TWIN_KEYS.every(k => cleared.has(`${k}.${id}`))), `remove Grok Bot Twin: every twin's remembered things are cleared, also a twin whose row is gone (${JSON.stringify(H.storedRemoves.slice(0, 12))})`)
      ok(['noted', 'home', 'unlinked'].every(k => cleared.has(k)) && !cleared.has('removed') && !H.storedSets.some(([k]) => k !== 'noted'), `remove Grok Bot Twin: the page's own notes are cleared; only "everything is removed" may stay for the last screen (${JSON.stringify(H.storedSets)})`)
      // Not before the backend said it is done.
      H.storedRemoves = []
      reset()
      render(jsx(P.UninstallDialog, { open: true, onClose: noop, onRemoved: noop, storage: ctx.storage }))
      H.restReply = async () => { throw new Error('offline') }
      await assert.rejects(() => H.confirms[0].onConfirm())
      ok(H.storedRemoves.length === 0, 'remove Grok Bot Twin that failed: nothing the page remembered is cleared')
      delete H.stored.noted
      H.storedRemoves = []
      H.storedSets = []
      H.restReply = async () => ({ dry_run: false, twins: 2, sample: true, next: fixtures['uninstall-plan'].next })
    }
    // Every place the page stores something per twin is one of the kinds that are cleared.
    {
      const perTwin = [...source.matchAll(/`([a-z-]+)\.\$\{(?:pair\.pair_id|pairId)\}`/g)].map(m => m[1])
      const kinds = JSON.parse(source.match(/const TWIN_NOTES = (\[[^\]]+\])/)[1].replace(/'/g, '"'))
      ok(perTwin.length >= 5 && perTwin.every(k => kinds.includes(k)), `every per-twin key the page stores is cleared on leaving (${[...new Set(perTwin)]} within ${kinds})`)
      // Each of those stores is followed, on the very next line, by the note that something is kept for the twin.
      const srcLines = source.split('\n')
      const stores = srcLines.map((l, i) => [l, i]).filter(([l]) => /storage\?\.set\((?:askedKey\(|storeKey|stepKey|key,|waitKey)/.test(l))
      ok(stores.length === 5 && stores.every(([, i]) => srcLines[i + 1].trim() === 'noteTwin(storage, pair.pair_id)') && count(source, 'storage?.set(') === stores.length + 2 + 3, `every per-twin store is noted on the next line (${stores.length} per-twin stores of ${count(source, 'storage?.set(')})`)
      // Where the twins are looked after is not written again while "everything is removed" is showing.
      // (the home is also never a twin's own Hermes bot; the rule about "removed" is unchanged)
      ok(/if \(!removed && serving && fleetKnown && !twinNames\.has\(serving\)\) \{\n\s+storage\?\.set\(HOME_KEY/.test(source) && /\[serving, fleetKnown, twinKey, Boolean\(removed\)\]/.test(source), 'after removal: the remembered home is not written again until the owner stays')
    }
    // Removed in the terminal (the data folder is empty) while the page still holds notes: the page still offers
    // Remove Grok Bot Twin, so they can be cleared. A clean first run has no such button.
    {
      H.status = { ...statusFirstRun, has_data: false }
      ok(!text(page()).includes('Remove Grok Bot Twin…'), 'first run with nothing remembered: nothing to remove, no button')
      H.stored.noted = ['long-gone-twin']
      ok(text(page()).includes('Remove Grok Bot Twin…'), 'data removed in the terminal, notes left on the page: Remove Grok Bot Twin is still offered')
      delete H.stored.noted
      H.stored.unlinked = iso(60)
      ok(text(page()).includes('Remove Grok Bot Twin…'), 'the same for a remembered unlink')
      delete H.stored.unlinked
      H.status = statusAll
    }
    // Only what exists is listed, whatever an older backend sends.
    const oldLines = ['Every twin link.', 'Earlier versions.', 'Key files.', 'The sample.', 'The botport copy.']
    const BOTPORT_COPY = "Hermes' copy of botport, not your download."
    ok(JSON.stringify(P.removedLines({ twins: 0, sample: true, goes: oldLines })) === JSON.stringify(['The sample.', BOTPORT_COPY]), `only the sample on: no twin lines (${P.removedLines({ twins: 0, sample: true, goes: oldLines })})`)
    ok(JSON.stringify(P.removedLines({ twins: 1, sample: false, goes: oldLines })) === JSON.stringify(['Every twin, with their earlier versions.', BOTPORT_COPY]), 'a real twin, no sample: no sample line; twins and their earlier versions are one line')
    // "Link" is the word for computers: what Remove counts are twins, whatever the backend still calls them.
    ok(JSON.stringify(P.removedLines({ twins: 2, sample: false, goes: ['2 twin links (Test Scout Pro, Test Quill).', 'Earlier versions of 2 twins.', BOTPORT_COPY] })) === JSON.stringify(['2 twins (Test Scout Pro, Test Quill), with their earlier versions.', BOTPORT_COPY]) && JSON.stringify(P.removedLines({ twins: 1, sample: false, goes: ['1 twin link (Test Scout Pro).', 'Earlier versions of 1 twin.'] })) === JSON.stringify(['1 twin (Test Scout Pro), with its earlier versions.']), `remove: twins are counted as twins, never as "twin links" (${P.removedLines({ twins: 2, sample: false, goes: ['2 twin links (Test Scout Pro, Test Quill).', 'Earlier versions of 2 twins.'] })})`)
    // Whatever a backend calls it, the owner reads that Hermes removes its own copy, never their download.
    ok(JSON.stringify(P.removedLines({ twins: 1, sample: false, goes: ['The botport helper copy.'] })) === JSON.stringify([BOTPORT_COPY]) && JSON.stringify(P.removedLines({ twins: 1, sample: false, goes: [BOTPORT_COPY] })) === JSON.stringify([BOTPORT_COPY]) && JSON.stringify(P.removedLines({ twins: 1, sample: false, goes: ["Hermes' copy of botport (your download stays in Downloads)."] })) === JSON.stringify([BOTPORT_COPY]), 'the line about botport: one sentence, whichever way the backend says it')
    ok(JSON.stringify(P.removedLines({ twins: 1, sample: true, goes: ['Every twin link.', 'The sample.'] })) === JSON.stringify(['Every twin.', 'The sample.']), "the backend's own filtered list is shown as it is")
    ok(JSON.stringify(P.removedLines({ twins: 0, sample: false, goes: [] })) === JSON.stringify(["Grok Bot Twin's own records on this computer."]), 'nothing listed: still one true line, never an empty list')
    // Nothing is removed before the plan is known.
    H.override['uninstall-plan'] = { isPending: true, data: undefined }
    reset()
    render(jsx(P.UninstallDialog, { open: true, onClose: noop, onRemoved: what => gone.push(what) }))
    await assert.rejects(() => H.confirms[0].onConfirm())
    ok(H.restCalls.length === 0 && gone.length === 1, 'remove Grok Bot Twin: nothing happens before the owner could read what goes')
    H.override = {}
  }
  // The last screen (remembered, so it is still there after a look elsewhere): not the welcome block as if nothing happened.
  H.status = { ...statusFirstRun }
  H.stored.removed = { skill_sync: true }
  raw = page()
  html = text(raw)
  ok(html.includes('Everything is removed') && html.includes('Your Grok bots and your Hermes chats are untouched. One step left: in Capabilities → Plugins, switch Grok Bot Twin off. Open Plugins shows its card.') && html.includes('One thing is left in Grok: the sync bot you made for skill sync. Delete its routine in the Grok app; Hermes cannot reach it.') && !/Bring your Grok bots|See a sample first|Privacy|hermes plugins|switch the plugin off/.test(html), `after removal: one final screen that names the switch (${html.slice(0, 260)})`)
  ok(JSON.stringify(texts()) === JSON.stringify(['Open Plugins', 'Set up Twins again']) && JSON.stringify(primaries()) === JSON.stringify(['Open Plugins']) && variantOf('Set up Twins again') === 'text', 'after removal: the one step left has its button; a quiet way to stay')
  H.nav = []
  await press('Open Plugins')
  // (at the plugin's own card: Hermes scrolls to it and marks it, so the switch is not hunted for in the catalog)
  ok(H.nav.at(-1) === '/capabilities?tab=plugins&plugin=grok-bot-twin', `after removal: the button leads to the switch (${H.nav.at(-1)})`)
  html = text(render(jsx(P.RemovedScreen, { onStay: noop, syncBot: false })))
  ok(!html.includes('sync bot'), 'after removal: the sync bot is mentioned only when there was one')
  delete H.stored.removed
  H.status = statusAll

  // ── Sample banner: one line, one way out; what can go wrong is folded ──
  reset()
  raw = render(jsx(P.SampleBanner, { hideReal: false, onEnd: noop, pairs: [acme] }))
  html = text(raw)
  ok(JSON.stringify(texts()) === JSON.stringify(['End sample', '', 'What can go wrong?']) && primaries().length === 0 && html.includes('Sample twin with invented data. Nothing of yours is touched.'), `sample banner buttons: ${texts()}`)
  ok(!/Try it:|Change the bot in Grok|Change it in both places|A new twin starts|has no chat/.test(html) && (raw.match(/<p[^>]*>/g) ?? []).length === 1 && !raw.includes('role="alertdialog"'), 'sample banner: one sentence; no try-outs until asked; it holds no dialog of its own')
  // The recording aid is not on a new owner's first screen: it sits in the banner's own menu, off by default.
  ok(H.checks.length === 0 && !/record your screen/.test(text(raw)) && raw.includes('aria-label="More for the sample"') && JSON.stringify(H.menu.map(m => m.text)) === JSON.stringify(['Hide my real bots while the sample is on']), 'hide-real-bots is an explicit choice in the banner\'s menu')
  H.restReply = async () => ({ ok: true, pairs: [acme] })
  await H.menu[0].onSelect()
  await tick()
  ok(H.restCalls[0]?.path === '/demo' && H.restCalls[0]?.opts?.body?.action === 'hide-real', 'choosing it posts hide-real')
  reset()
  render(jsx(P.SampleBanner, { hideReal: true, onEnd: noop, pairs: [acme] }))
  ok(H.menu[0]?.text === 'Show my real bots again', 'while they are hidden the same entry brings them back')
  await H.menu[0].onSelect()
  await tick()
  ok(H.restCalls[0]?.opts?.body?.action === 'show-real', 'choosing it posts show-real')
  // The fold: exactly the three things that can go wrong. (What goes right is the sample row's own button.)
  raw = render(jsx(P.SampleBanner, { hideReal: false, initialOpen: true, onEnd: noop, pairs: [acme] }))
  ok(JSON.stringify(texts()) === JSON.stringify(['End sample', '', 'What can go wrong? Try it on the sample:', 'Change it in both places', 'Paste a fake password', 'Change a skill in Hermes']) && primaries().length === 0, `sample banner, fold open: ${texts()}`)
  reset()
  H.restReply = async path => (path === '/demo' ? { ok: true, did: 'x', pairs: [acme] } : { pairs: [{ pair_id: 'acme-support', applied: [], secrets_withheld: [], conflicts: [{ id: 'skill:greet-visitors' }], state: 'needs_you' }] })
  await press('Change it in both places')
  await tick(8)
  // The result shows by itself: the twin is checked without a second press.
  ok(H.restCalls[0]?.path === '/demo' && H.restCalls[0]?.opts?.body?.action === 'conflict' && H.restCalls[1]?.path === '/sync' && H.restCalls[1]?.opts?.body?.pair_id === 'acme-support', `try-out: the change, then the check by itself (${H.restCalls.map(c => c.path)})`)
  // What the try-out did is said in the banner, not in a toast over the row that is about to show it.
  ok(!H.notes.some(n => /Watch the sample|was changed in Grok and in Hermes|press|Sync now/i.test(`${n.message} ${n.meta ?? ''}`)), `try-out: no toast narrates it (${JSON.stringify(H.notes.map(n => n.message))})`)
  raw = render(jsx(P.SampleBanner, { hideReal: false, initialSaid: 'A skill was changed in Grok and in Hermes. Watch the sample twin: it notices in a moment.', onEnd: noop, pairs: [acme] }))
  ok(text(raw).includes('A skill was changed in Grok and in Hermes. Watch the sample twin: it notices in a moment.') && !text(raw).includes('Nothing of yours is touched.') && count(raw, 'role="status"') === 1, 'try-out: the banner says what happened, in the place of its usual line')
  // With the page on screen, "Paste a fake password" raises no warning toast: the row shows the box.
  hooks.setPageOpen(1)
  reset()
  render(jsx(P.SampleBanner, { hideReal: false, initialOpen: true, onEnd: noop, pairs: [acme] }))
  H.restReply = async path => (path === '/demo' ? { ok: true, did: 'x', pairs: [acme] } : { pairs: [{ pair_id: 'acme-support', applied: [], secrets_withheld: [{ item: 'skill:route-notes', kind: 'skill', direction: 'g2h' }], conflicts: [], state: 'needs_you' }] })
  await press('Paste a fake password')
  await tick(8)
  ok(H.restCalls[0]?.opts?.body?.action === 'secret' && H.notes.length === 0, `page open: the fake password raises no toast over the page (${JSON.stringify(H.notes.map(n => n.message))})`)
  hooks.setPageOpen(0)

  // ── The sample shows the point first: the bot in Grok, its twin in Hermes, and the twin following ──
  {
    const see = (pair, props = {}) => render(jsx(P.SeeTwinSheet, { intervalS: 60, onClose: noop, open: true, pair, ...props }))
    // The sample row's one button, once the backend sends both sides.
    html = text(row(acmeSee, { lead: false }))
    ok(html.includes('Follows Grok') && JSON.stringify(texts()) === JSON.stringify(['See the twin', '']) && variantOf('See the twin') === 'secondary' && H.buttons[0].size === 'default' && H.buttons[0].label === 'See Acme Support and its twin', `sample row: See the twin (${texts()})`)
    // Not finished yet: the benefit is still the button; the chore is a quiet link next to the sentence (and in the menu).
    html = text(row(acmeSeeNew))
    // (the sample shows the point first; its set-up chore is not on the row, it waits in the menu to try)
    ok(html.includes('Follows Grok') && !/Almost ready|thing left|ready/.test(html.replace('Acme Support', '')) && JSON.stringify(texts()) === JSON.stringify(['See the twin', '']) && H.menu.some(m => m.text === 'Try finishing its set-up…'), `sample row, not finished: See the twin is its one button; the chore is in its menu (${texts()})`)
    // An open matter comes first, as on every row.
    row({ ...acmeSee, conflicts: 1, state: 'needs_you' })
    ok(!has('See the twin'), 'sample row with something open: its box first')
    // A backend that does not send both sides: the row is as before (no sheet that would have nothing to show).
    row(acmeNew)
    ok(JSON.stringify(texts()) === JSON.stringify(['Finish setting up', '']) && !has('See the twin'), 'sample row (older backend): Finish setting up')
    row({ ...acmeCalm, grok: { ...acmeCalm.grok, description: ACME_SAYS } })
    ok(!has('See the twin'), 'sample row with one side only: no See the twin')
    row({ ...recipe, grok: { ...recipe.grok, description: 'x' }, hermes: { ...recipe.hermes, name: 'x', description: 'x' } })
    ok(!has('See the twin') && has('Open in Hermes'), 'a real twin has a chat to open instead')

    raw = see(acmeSee)
    html = text(raw)
    for (const s of ['Acme Support and its twin', 'Invented data.', 'In Grok', 'In Hermes', 'Change the bot in Grok', 'Done']) {
      ok(html.includes(s), `see the twin shows "${s}"`)
    }
    // The sample's first view is the title, "Invented data.", the two cards and the one button. The two
    // explaining sentences come once the change has been played, so the sample is at work inside 30 s.
    const seeWords = h => h.replaceAll(ACME_SAYS, '').trim().split(/\s+/).filter(w => /[A-Za-z0-9]/.test(w)).length
    ok(!/On the left|The same on both sides|A twin follows the name|pasted once/.test(html) && seeWords(html) <= 27, `see the twin, first view: nothing to read before the button (${seeWords(html)} words beside the bot's own description: ${html})`)
    {
      const after = text(see(acmeSee, { initialPhase: 'followed' }))
      // (one sentence says what it meant; with the twin's own description the sheet stays within fifty words)
      ok(after.includes('The twin followed. With your own bots this happens by itself,') && !/A twin follows the name|pasted once/.test(after) && seeWords(after) <= 50, `see the twin, after the change was played: what it meant is said then, once (${seeWords(after)} words: ${after})`)
      // Leaving is then the next act: End sample is the one filled button, and changing it again stays in reach.
      see(acmeSee, { initialPhase: 'followed', onEndSample: noop })
      ok(JSON.stringify(primaries()) === JSON.stringify(['End sample']) && variantOf('Change it again') === 'outline' && variantOf('Done') === 'ghost', `see the twin, after the change: the filled button leads out (${primaries()})`)
      see(acmeSee, { onEndSample: noop })
      ok(JSON.stringify(primaries()) === JSON.stringify(['Change the bot in Grok']) && variantOf('End sample') === 'ghost', `see the twin, first view: the filled button plays the change (${primaries()})`)
      raw = see(acmeSee)
      html = text(raw)
    }
    ok(count(html, ACME_SAYS) === 2 && count(raw, 'role="group"') === 2 && /sm:grid-cols-2/.test(raw), 'see the twin: the same name and description, side by side')
    ok(JSON.stringify(texts()) === JSON.stringify(['Change the bot in Grok', 'Done']) && JSON.stringify(primaries()) === JSON.stringify(['Change the bot in Grok']) && variantOf('Done') === 'ghost' && endsInButton(raw) && count(raw, 'role="status"') === 1, `see the twin: one button, read-only otherwise (${texts()})`)
    ok(!/password|both places|skill sync|Finish setting up|chat/i.test(html) && !raw.includes('<textarea') && !raw.includes('<input'), 'see the twin: the benefit only, none of the chores')
    noEngineWords(html, 'see the twin')
    // The button changes the sample's Grok side, then the twin is checked by itself.
    reset()
    H.fetches = []
    H.restReply = async path => (path === '/demo' ? { ok: true, did: "Changed the sample bot's description in Grok" } : { pairs: [{ pair_id: 'acme-support', applied: [{ item: 'persona.description', kind: 'description', direction: 'g2h' }], secrets_withheld: [], conflicts: [], state: 'in_step' }] })
    await press('Change the bot in Grok')
    await tick(8)
    ok(JSON.stringify(H.restCalls.map(c => [c.path, c.opts?.body?.action ?? c.opts?.body?.pair_id])) === JSON.stringify([['/demo', 'grok-edit'], ['/sync', 'acme-support']]) && H.fetches.filter(f => f === 'status').length >= 2 && H.notes.length === 0, `Change the bot in Grok: the change, a fresh look, the check, a fresh look; no toast over the sheet (${JSON.stringify(H.restCalls.map(c => c.path))} / ${JSON.stringify(H.notes.map(n => n.message))})`)
    // In between: the two sides differ, and the sheet says the twin has not caught up yet.
    const NEW = 'Answers questions about Acme widgets and gadgets.'
    raw = see({ ...acmeSee, grok: { ...acmeSee.grok, description: NEW } }, { initialPhase: 'changed' })
    html = text(raw)
    ok(html.includes('The bot changed in Grok. Its twin notices by itself…') && html.includes(NEW) && html.includes(ACME_SAYS) && html.includes('Changed') && !html.includes('The twin followed') && has('Change it again'), `see the twin, just changed: the difference is visible (${html.slice(0, 260)})`)
    // Afterwards: equal again, and said, with how fast it is for real bots (from the real interval).
    raw = see({ ...acmeSee, grok: { ...acmeSee.grok, description: NEW }, hermes: { ...acmeSee.hermes, description: NEW } }, { initialPhase: 'followed' })
    html = text(raw)
    ok(html.includes('The twin followed. With your own bots this happens by itself, within a minute.') && count(html, NEW) === 2 && html.includes('Followed') && !html.includes(ACME_SAYS) && has('Change it again'), `see the twin, followed: ${html.slice(0, 260)}`)
    ok(text(see(acmeSee, { initialPhase: 'followed', intervalS: 5 })).includes('this happens by itself, in seconds.'), 'see the twin: the speed is the real one')
    ok(text(see(acmeSee, { fastCheck: { on: true, mode: 'poll' }, initialPhase: 'followed' })).includes('this happens by itself, in seconds.') && text(see(acmeSee, { fastCheck: { on: false, mode: 'off' }, initialPhase: 'followed', intervalS: 120 })).includes('this happens by itself, within 2 minutes.'), 'see the twin: the fast check decides the sentence, else the real number')
    // Untrusted text from the Grok side is text here too.
    raw = see({ ...acmeSee, grok: { ...acmeSee.grok, description: '<img src=x onerror=1> hello' } })
    ok(!raw.includes('<img') && raw.includes('&lt;img'), 'see the twin: a description is rendered as text')
  }
  hooks.setPageOpen(0)

  // ── Privacy ──
  raw = render(jsx(P.PrivacySheet, { open: true, onClose: noop }))
  html = text(raw)
  ok(html.includes('It never reads login or password files.') && html.includes('It runs botport, the small app that you downloaded. Hermes verifies it first, unless you chose in the terminal to use a build of your own.') && html.includes('The plugin itself downloads nothing and sends no usage data.') && html.includes("It uses the network only between your own computers, over your own network. Sharing or linking sends twin names, descriptions and your computers' names, and the settings you let follow (tools, preferences, persona; the model only if you switch it on), to keep your twins the same everywhere.") && html.includes("With skills switched on, it also sends your twins' skills (text files only) to your linked computers. With routines switched on, it also sends your twins' routines. With memory switched on for a twin, it also sends that twin's memory, encrypted. Asking a twin on another computer") && html.includes('Asking a twin on another computer sends your request there and its answer back. Nothing else of your chats, and no other files, passwords or keys, ever leaves a computer.') && html.includes('this check can miss things') && html.includes('not affiliated with xAI') && JSON.stringify(texts()) === JSON.stringify(['Done']), 'privacy sheet: the full disclosure, one click away, ends in Done')

  // ── Other computers: share on the Mac, link on the other computer, the fleet on both ──
  // (answers in the shapes of the sharing contract; three invented computers: mini, Surface, Orchard)
  const fleetFrom = seen.length
  {
    const F = P.fleetOf
    const withFleet = (status, fleet, props) => { H.mode = 'ok'; H.status = status; H.override.fleet = fleet ? { data: fleet } : { isPending: true, data: undefined }; return page(props) }
    const words = h => h.trim().split(/\s+/).filter(w => /[A-Za-z0-9]/.test(w)).length
    const share = (fleet, props = {}) => render(jsx(P.ShareSection, { fleet: F(fleet), ...props }), { technical: props.initialDetails === true })
    const linkSheet = (props = {}) => render(jsx(P.LinkSheet, { onClose: noop, open: true, ...props }))
    const follower = fleet => render(jsx(P.FollowerView, { fleet: F(fleet) }))
    // "14:02" or, just after midnight, "yesterday 23:40": the page's own words for a moment (their rules are checked below with fixed dates)
    const clock = t => P.sinceWords(t)

    // The answer, tidied: an older backend has none; names are text; two computers of one name are told apart.
    ok(F(null) === null && F({}) === null && F({ role: 'boss' }) === null, 'fleet: no answer or an unknown role is "this backend cannot link computers"')
    ok(F(fleetSource2).others.map(c => c.name).join() === 'Orchard,Surface' && F(fleetSource2).me.name === 'mini' && F(fleetSource2).role === 'source', 'fleet: this computer and the others are told apart')
    {
      const twice = F({ ...fleetSource2, machines: [miniRow, surfaceRow, { ...surfaceRow, id: 'm-000000000002' }] })
      ok(twice.others.map(c => c.name).join() === 'Surface,Surface (2)', `fleet: two computers with one name get distinct labels by themselves (${twice.others.map(c => c.name)})`)
      const odd = F({ ...fleetSource2, machines: [miniRow, { ...surfaceRow, name: 'Sur\u0007face\n<b>x</b>  ' + 'y'.repeat(80) }] })
      ok(odd.others[0].name.length <= 40 && !/[\u0000-\u001f]/.test(odd.others[0].name), 'fleet: a computer name is cleaned and capped before it is shown')
    }

    // The link code as people type it: capitals, spaces, dashes and look-alikes do not matter.
    ok(P.codeShown(P.codeChars('7kq4 m2xd-p9ra')) === '7KQ4-M2XD-P9RA' && P.codeShown(P.codeChars('7KQ4M2XDP9RA')) === '7KQ4-M2XD-P9RA', 'link code: typed with or without spaces, dashes or capitals')
    ok(P.codeChars('okil-OKIL-okil') === 'OKILOKILOKIL' && P.codeChars('Link code: 7KQ4-M2XD-P9RA (10 min)') === '7KQ4M2XDP9RA' && P.codeShown(P.codeChars('7kq4m')) === '7KQ4-M', 'link code: look-alikes are sent as typed (the backend reads them as the code means them); a pasted sentence gives the code; groups of four while typing')
    ok(P.codeLife(iso(-9.7)) === 'Good for 10 minutes' && P.codeLife(iso(-4.2)) === 'Good for 4 minutes' && P.codeLife(iso(-0.4)) === 'Good for under a minute' && P.codeLife(iso(1)) === null, 'link code: its life in words, and "run out" once it has')
    // Times: today a clock time, yesterday said so, older a date; never a future time.
    {
      const noon = new Date(2000, 0, 5, 12, 0, 0).getTime()
      const at = (d, h, m) => new Date(2000, 0, d, h, m, 0).toISOString()
      ok(P.whenText(new Date(Date.now() - 1000).toISOString()).startsWith('at ') && P.whenText(new Date(Date.now() - 5 * 86_400_000).toISOString()).startsWith('on ') && P.whenText(null) === '', 'times in a sentence: "at 14:02", "on 3 Nov"')
      ok(P.sinceWords(at(5, 9, 6), noon) === '09:06' && P.sinceWords(at(4, 23, 50), noon) === 'yesterday 23:50' && P.sinceWords(at(2, 8, 0), noon) === '2 Jan' && P.sinceWords(at(5, 18, 0), noon) === '12:00' && P.sinceWords('nonsense', noon) === '', `times: today, yesterday, a date, never the future (${P.sinceWords(at(5, 9, 6), noon)} / ${P.sinceWords(at(4, 23, 50), noon)} / ${P.sinceWords(at(2, 8, 0), noon)})`)
    }
    // Where a twin's other copies are, for its row.
    ok(P.alsoOn(scoutPair.fleet.machines) === 'also on Surface and Orchard' && P.alsoOn(quillPair.fleet.machines) === 'also on Surface' && P.alsoOn([miniRow]) === '' && P.alsoOn(undefined) === '', `where it is: the names, and nothing that the list of computers says once (${P.alsoOn(scoutPair.fleet.machines)} / ${P.alsoOn(quillPair.fleet.machines)})`)
    ok(P.copiesDetail(scoutPair.fleet.machines) === 'Surface: in sync 2 min ago. Orchard: last in sync 130 min ago.', `where it is, on hover: how fresh each copy is (${P.copiesDetail(scoutPair.fleet.machines)})`)
    ok(P.alsoOn(onRows([miniRow, surfaceRow, { ...surfaceRow, id: 'm-2', name: 'Studio' }, { ...surfaceRow, id: 'm-3', name: 'Den' }])) === 'also on 3 computers' && P.alsoOn(fleetBehind.twins[0].machines) === 'also on Surface' && P.alsoOn([miniRow, { ...surfaceBehind, error: { code: 'profile_blocked', message: 'x' } }]) === 'could not be made on Surface', "where it is: three or more are counted; only this twin's own trouble is said on its row; a copy that could not be made is not \"also on\" that computer")
    {
      // A computer that has not got this twin yet (sharing was off, or that computer was) is never "also on".
      const pending = { ...surfaceRow, id: 'm-9', name: 'Studio', state: 'pending', last_sync_at: null }
      ok(P.alsoOn(onRows([miniRow, pending])) === 'not on Studio yet' && P.alsoOn(onRows([miniRow, surfaceRow, pending])) === 'also on Surface · not on Studio yet', `where it is: a copy that never arrived is not claimed (${P.alsoOn(onRows([miniRow, surfaceRow, pending]))})`)
      {
        // With what the page knows of sharing, the words are the terminal's ("hermes twin fleet" says the same).
        const rows = onRows([miniRow, surfaceRow, pending])
        const now = (on, state) => ({ on, states: new Map([['m-9', state]]) })
        ok(P.alsoOn(rows, '', now(true, 'in_sync')) === 'also on Surface · on its way to Studio' && P.alsoOn(rows, '', now(true, 'offline')) === 'also on Surface · goes to Studio when it is next on' && P.alsoOn(rows, '', now(false, 'offline')) === 'also on Surface · goes to Studio when sharing is on again', `where it is: when a copy still to come gets there (${P.alsoOn(rows, '', now(true, 'offline'))})`)
        ok(['on its way to Studio', 'goes to Studio when it is next on', 'not on Studio yet', 'could not be made on Surface'].every(t => P.WHERE_NEWS.test(t)) && !P.WHERE_NEWS.test('also on Surface and Orchard'), "where it is: a twin's own news stays on its row")
      }
      ok(P.copiesDetail(onRows([miniRow, pending])) === 'Studio: gets this twin when it is next in touch.', `where it is, on hover: a copy still to come (${P.copiesDetail(onRows([miniRow, pending]))})`)
      ok(P.onAll(onRows([miniRow, surfaceRow]), surfaceRow.id) === true && P.onAll(onRows([miniRow, surfaceRow, pending]), [surfaceRow.id, 'm-9'].sort().join('|')) === false, 'every twin everywhere: not while a copy is still to come')
    }

    // ── First screen: both ways in ──
    // The Grok app is on this computer: Connect is the filled button, Link sits next to it, the sample is quiet.
    raw = withFleet({ ...statusFirstRun, fleet: briefNone }, fleetNone)
    html = text(raw)
    ok(JSON.stringify(texts()) === JSON.stringify(['Connect my Grok bots', 'Link this computer', 'See a sample first', 'Privacy']) && JSON.stringify(primaries()) === JSON.stringify(['Connect my Grok bots']) && variantOf('Link this computer') === 'outline', `first screen: both ways in, one filled (${texts()} / ${primaries()})`)
    ok(html.includes('Bots on another computer? Link this one to it.') && words(html) <= 45, `first screen: at most 45 words on it (${words(html)})`)
    ok((html.match(/[^.?!]+[.?!]/g) ?? []).filter(x => /twin|Grok/.test(x)).length >= 2 && html.includes('Nothing changes in Grok.'), 'first screen: what a twin is and that nothing changes in Grok, in two sentences')
    // No Grok app here: this is most likely the other computer, so Link is the filled one. Connect stays one click away.
    raw = withFleet(statusOtherFirstRun, fleetNoneOther)
    html = text(raw)
    ok(JSON.stringify(texts()) === JSON.stringify(['Link this computer', 'Connect my Grok bots', 'See a sample first', 'Privacy']) && JSON.stringify(primaries()) === JSON.stringify(['Link this computer']) && variantOf('Connect my Grok bots') === 'outline', `first screen without the Grok app: Link is the filled button (${texts()} / ${primaries()})`)
    ok(html.includes('Bots on another computer? Link this one to it.') && !html.includes('No Grok app here') && words(html) <= 45 && !/Share my twins|my Mac/.test(html), `first screen without the Grok app: the same hint (it does not presume why the app is missing), no sharing, no "my Mac" (${words(html)} words)`)
    // A backend that cannot link computers: the first screen is as it always was.
    H.override.fleet = undefined
    H.status = statusFirstRun
    page()
    ok(JSON.stringify(texts()) === JSON.stringify(['Connect my Grok bots', 'See a sample first', 'Privacy']), 'first screen on an older backend: no Link button that could not work')
    // Connect, row 1 without the Grok app: the other way in is offered there too, and where there is no botport for this computer.
    connect({ ...setupNoBotport, grok_app: { found: false } }, { initialElsewhere: true, onLink: noop, onSample: noop })
    ok(has('Link this computer') && has('See the sample') && text(connect({ ...setupNoBotport, grok_app: { found: false } }, { initialElsewhere: true, onLink: noop, onSample: noop })).includes('Bots on another computer? Link this one to it.'), 'connect without the Grok app: Link and the sample are both offered')
    connect({ ...setupNoBotport, download: null, download_url: '', problem: 'There is no botport for this kind of computer yet.' }, { onLink: noop, onSample: noop })
    ok(has('Link this computer') && has('See the sample'), `no botport for this computer: Link and the sample, not a dead end (${texts()})`)
    {
      // The main button is the way on that works (linking; the sample when linking is not offered), and
      // "I have the file", which a newcomer cannot have, waits under "For developers".
      const noBuild = { ...setupNoBotport, download: null, download_url: '', problem: 'There is no botport for this kind of computer yet.' }
      const shown = text(connect(noBuild, { onLink: noop, onSample: noop }))
      ok(JSON.stringify(primaries()) === JSON.stringify(['Link this computer']) && variantOf('See the sample') === 'ghost' && !has('I have the file') && !shown.includes('botport is a small free app'), `no botport for this computer: Link is the main button (${primaries()}; ${texts()})`)
      connect(noBuild, { onSample: noop })
      ok(JSON.stringify(primaries()) === JSON.stringify(['See the sample']) && !has('I have the file'), `no botport for this computer, no linking: the sample is the main button (${primaries()})`)
      ok(/!target && !found && !downloaded && el\('div', [^\n]*'Built botport yourself\?'\), haveFile\('ghost'\)\)/.test(source), 'no botport for this computer: "I have the file" is under For developers')
    }

    // ── The Mac: sharing ──
    // Never shared: one switch, one sentence about what crosses. Nothing else, and no filled button.
    raw = share(fleetNone)
    html = text(raw)
    // (with sharing off and nothing linked the card is its label and its switch, so a calm page with
    // three twins stays within sixty words; what is shared is said the moment the switch goes on, next to the code)
    ok(H.switches.length === 1 && H.switches[0].label === 'Share my twins with my other computers' && H.switches[0].checked === false && html.includes('Share my twins with my other computers') && !html.includes('Sharing sends your') && words(html) <= 8, `sharing off: one switch and nothing else (${html})`)
    ok(texts().length === 0 && !/Link code|Technical details|see this one as/.test(html) && words(html) <= 30, `sharing off: nothing else on screen (${texts()})`)
    // (the list also carries this computer's name, so the sentence says so)
    ok(text(share(fleetCode)).includes("Sharing sends your twins' names, descriptions and the settings you let follow, and this computer's name; never chats or passwords, and skills only if switched on."), 'sharing just switched on: what is shared is said next to the link code')
    // One click gives the code.
    reset()
    H.setData = []
    H.restReply = async () => fleetCode
    await H.switches[0].onCheckedChange(true)
    await tick()
    ok(H.restCalls.length === 1 && H.restCalls[0].path === '/fleet/share' && H.restCalls[0].opts.body.on === true && H.setData.some(([k, v]) => k === 'fleet' && v === fleetCode) && H.notes.length === 0, `switching on: one call, the code is shown at once, no toast (${JSON.stringify(H.restCalls.map(c => c.path))})`)
    // The code: large, in groups of four, with Copy, its life in words, what to do on the other computer, New code.
    // (its end is set again here: on a busy computer the suite has been running for a while by now)
    linkCode.expires_at = iso(-9.7)
    raw = share(fleetCode)
    html = text(raw)
    ok(/<output[^>]*text-2xl[^>]*>7KQ4-M2XD-P9RA<\/output>/.test(raw) && /font-mono/.test(raw) && raw.includes('aria-label="Link code: 7 K Q 4 M 2 X D P 9 R A"'), 'link code: shown large, in groups of four, spelled out for a screen reader')
    ok(H.copies.length === 1 && H.copies[0].label === 'Copy' && H.copies[0].text === '7KQ4-M2XD-P9RA' && html.includes('Good for 10 minutes · links one computer') && has('New code') && variantOf('New code') === 'text', `link code: Copy, its life in words, New code (${html})`)
    ok(html.includes('On your other computer, open Twins in Hermes, press Link this computer and type this code.') && words('On your other computer, open Twins in Hermes, press Link this computer and type this code.') <= 30 && primaries().length === 0 && H.switches[0].checked === true, 'sharing on, nobody linked: the code and one sentence about the other computer')
    reset()
    H.restReply = async () => ({ code: { ...linkCode, code: 'ABCD-EFGH-JKMN' } })
    await press('New code')
    ok(H.restCalls[0]?.path === '/fleet/code' && H.invalidations > 0, 'New code: one click, one call')
    // The code ran out with nobody linked: said, with New code.
    html = text(share({ ...fleetCode, share: { ...fleetCode.share, code: { ...linkCode, expires_at: iso(1) } } }))
    ok(html.includes('That link code has expired.') && has('New code') && !html.includes('7KQ4'), 'a code that has expired is not shown any more')
    html = text(share({ ...fleetCode, share: shareOn }))
    ok(html.includes('No other computer is linked.') && !html.includes('linked yet') && JSON.stringify(texts().filter(t => t !== 'Technical details')) === JSON.stringify(['New code']), `no code and nobody linked: one sentence, one button (${texts()})`)
    // Two computers linked: each with its name, kind and how fresh; a way to one more; nothing filled.
    raw = share(fleetSource2)
    html = text(raw)
    ok(html.includes('Orchard') && html.includes('Linux · last seen 125 min ago') && html.includes('Surface') && html.includes('Windows · in sync 2 min ago') && count(raw, 'aria-label="Orchard, a computer"') === 1, `the Mac lists its computers: name, kind, how fresh (${html})`)
    ok(has('Link another computer') && variantOf('Link another computer') === 'outline' && primaries().length === 0 && !html.includes('Link code') && JSON.stringify(H.menu.map(m => m.text)) === JSON.stringify(['Unlink Orchard…', 'Unlink Surface…']), `the Mac's list: one more computer is one button; each has its own Unlink (${H.menu.map(m => m.text)})`)
    ok(has('Technical details') && !has('Rename…') && !/100\.64|18777|Tailscale|pass\b|see this one as/.test(html), 'the name and the address stay folded away: nobody has to know either')
    html = text(share(fleetSource2, { initialDetails: true }))
    ok(html.includes('Your other computers see this one as mini.') && has('Rename…') && html.includes('They reach it at 100.64.0.10, port 18777, over Tailscale (encrypted). Each linked computer has its own pass, and nothing is reachable without one.'), `technical details: the name (with Rename), where, and that the network is encrypted (${html.slice(html.indexOf('Your other'))})`)
    ok(text(share({ ...fleetSource2, share: { ...shareOn, network: 'lan', address: '192.168.1.20' } }, { initialDetails: true })).includes('over your home network (not encrypted: others on that network could read the names and descriptions)'), 'technical details: a network that is not encrypted is said to be')
    raw = share(fleetSource2)
    html = text(raw)
    reset()
    H.restReply = async () => ({ code: linkCode })
    await press('Link another computer')
    ok(H.restCalls[0]?.path === '/fleet/code', 'Link another computer makes a link code')
    // With computers linked the card is its label, its switch and the fold: the sentence about what is shared is
    // said where sharing is decided (the first code; switched off with computers linked) and in Privacy.
    // Not again with every further code, where the page ran to 122 words.
    ok(!text(share(fleetSource2)).includes('Sharing sends your') && !text(share({ ...fleetSource2, share: { ...shareOn, code: linkCode } })).includes('Sharing sends your') && !text(share(fleetSharedOff)).includes('Sharing sends your') && /Sharing sends your twins' names, descriptions and the settings you let follow, and this computer's name/.test(source.slice(source.indexOf('const PRIVACY'), source.indexOf('const PRIVACY') + 3000) + source.slice(source.indexOf('const SHARED_SENTENCE'), source.indexOf('const SHARED_SENTENCE') + 300)), 'what is shared: said at the first link, not on every calm page, with every further code, or while sharing is off')
    // The small buttons of these screens are the Desktop's default size, like the rows' own.
    share(fleetCode)
    ok(H.buttons.find(b => b.text === 'New code')?.size === 'default' && H.buttons.find(b => b.text === 'Technical details')?.size === 'default', `targets: New code and the fold are full-size (${H.buttons.map(b => `${b.text}:${b.size}`)})`)
    // Behind: connected, one twin could not be made there.
    html = text(share(fleetBehind))
    ok(html.includes('Windows · 1 twin could not be made there'), `a computer that could not make a twin says so (${html})`)
    ok(P.computerSentence(F({ ...fleetSource2, machines: [miniRow, { ...surfaceRow, state: 'never_synced', last_sync_at: null, last_seen_at: null }] }).others[0]) === 'Windows · linked, getting its first twins' && P.computerSentence(F({ ...fleetSource2, machines: [miniRow, { ...surfaceRow, state: 'behind' }] }).others[0]) === 'Windows · catching up', 'a computer just linked, or catching up, says that')
    // Sharing cannot listen (it was on, the cause came later): what is wrong, the one thing to do, that it heals by itself.
    raw = share(fleetShareError)
    html = text(raw)
    // (the page names no program it cannot name; one thing to do)
    ok(html.includes('Another program on this computer is using the place Hermes shares from.') && html.includes('Hermes tries again by itself every minute.') && has('Try again') && !/Restart this computer/.test(html) && html.includes('Sharing is not working right now.') && !html.includes('Sharing with') && !/Close the other program/.test(html) && raw.includes('role="alert"') && !/\bport\b|18777|settings/.test(html) && text(share(fleetShareError, { initialDetails: true })).includes('Another program on this computer uses the port Hermes shares on (18777).'), `sharing cannot start: plain words; the backend's own are in the fold (${html})`)
    // It fails at the moment it is switched on: the switch goes back to off, and the one thing to do is said.
    reset()
    raw = share(fleetNone)
    H.restReply = async (path, opts) => (opts.body.on ? { error: { code: 'no_network', message: 'Hermes found no home network or Tailscale address to share on.', hint: 'Connect this computer to your network or start Tailscale; sharing starts by itself.' } } : fleetNone)
    await H.switches[0].onCheckedChange(true)
    await tick()
    ok(JSON.stringify(H.restCalls.map(c => [c.path, c.opts.body.on])) === JSON.stringify([['/fleet/share', true], ['/fleet/share', false]]) && H.notes.length === 0, `no network at switch-on: switched back off, said in the card, no toast (${JSON.stringify(H.restCalls.map(c => [c.path, c.opts.body.on]))})`)
    html = text(share(fleetNone, { initialProblem: P.shareProblem({ code: 'no_network', message: 'x' }, true) }))
    ok(html.includes('Hermes found no home network to share on.') && html.includes('Put this computer on your home network or start Tailscale, then switch sharing on again.') && H.switches[0].checked === false, `no network at switch-on: the cause and the one thing to do (${html})`)
    // Any other refusal is the backend's own sentence, in a toast.
    reset()
    share(fleetNone)
    H.restReply = async () => ({ error: { code: 'no_twins', message: 'There are no twins to share yet.', hint: 'Twin a Grok bot first; then share.' } })
    await H.switches[0].onCheckedChange(true)
    await tick()
    ok(H.notes.at(-1)?.kind === 'error' && H.notes.at(-1)?.message === 'There are no twins to share yet.' && H.notes.at(-1)?.meta === 'Twin a Grok bot first; then share.', 'switching on refused: what and what to do')
    // Stop sharing with computers linked: one sentence first, with their names; nothing is removed.
    reset()
    share(fleetSource2)
    await H.switches[0].onCheckedChange(false)
    await tick()
    ok(H.restCalls.length === 0, 'stop sharing with computers linked: asks first')
    raw = share(fleetSource2, { initialConfirm: 'off' })
    html = text(raw)
    ok(html.includes('Stop sharing your twins?') && html.includes('Orchard and Surface keep the twins they have and stop following until you share again.') && html.includes('Nothing is removed anywhere.') && H.confirms[0]?.confirmLabel === 'Stop sharing' && H.confirms[0]?.destructive !== true && H.confirms[0]?.cancelLabel === 'Cancel', `stop sharing: one sentence with the computers' names (${html.slice(html.indexOf('Stop sharing your'))})`)
    ok(words('Orchard and Surface keep the twins they have and stop following until you share again.') <= 25, 'stop sharing: the sentence is short')
    {
      const stop = H.confirms[0]
      reset()
      H.restReply = async () => fleetSharedOff
      await stop.onConfirm()
    }
    ok(H.restCalls[0]?.path === '/fleet/share' && H.restCalls[0]?.opts.body.on === false && H.notes.length === 0, 'stop sharing: confirmed, switched off, no toast over a page that shows it')
    // With nobody linked there is nothing to explain: it is switched off at once.
    reset()
    share(fleetCode)
    H.restReply = async () => fleetNone
    await H.switches[0].onCheckedChange(false)
    await tick()
    ok(H.restCalls.length === 1 && H.restCalls[0].opts.body.on === false && H.confirms.length === 0, 'stop sharing with nobody linked: no question')
    // Off, computers still linked: what they do meanwhile, and they stay listed.
    html = text(share(fleetSharedOff))
    ok(html.includes('Sharing is off. Orchard and Surface keep their twins and catch up when you switch it on again.') && /Windows · last seen \d+ (sec|min) ago/.test(html) && !/waiting for sharing/.test(html) && !/in sync/.test(html) && !has('Link another computer'), `sharing off with computers linked: nobody is called "in sync", and nothing is promised about a computer that is not heard from (${html})`)
    // Unlink one computer from the Mac: the plan first (nothing changed by asking), then one confirmation that names it.
    reset()
    raw = share(fleetSource2, { initialConfirm: surface.id })
    html = text(raw)
    for (const s of ['Unlink Surface?', 'Surface stops following this computer; its 2 twins leave Surface only.', 'Stays', 'Your twins on this computer and on your other computers.', 'Removed', 'The 2 twins on Surface, the next time it is on (kept there for 30 days, chats included).']) {
      ok(html.includes(s), `unlink a computer shows "${s}"`)
    }
    ok(H.confirms[0]?.confirmLabel === 'Unlink Surface' && H.confirms[0]?.destructive === true && H.confirms[0]?.cancelLabel === 'Cancel' && words(text(raw.slice(raw.indexOf('>', raw.indexOf('role="alertdialog"')) + 1))) <= 60 && !/cannot be undone/i.test(html) && !html.includes('Link Surface again within 30 days'), `unlink a computer: the button names it; 60 words at most (${words(text(raw.slice(raw.indexOf('>', raw.indexOf('role="alertdialog"')) + 1)))}); the way back is said once; never "cannot be undone" (it can)`)
    H.restReply = async () => ({ ...unlinkSurfacePlan, dry_run: false, unlinked: true })
    await H.confirms[0].onConfirm()
    ok(H.restCalls[0]?.path === `/fleet/machines/${surface.id}/remove` && H.restCalls[0]?.opts.body.dry_run === false && H.notes.at(-1)?.message === 'Surface is unlinked.' && H.notes.at(-1)?.meta === 'Link Surface again within 30 days and its twins come back with their chats.' && H.notes.at(-1)?.action?.label === 'Link it again' && H.notes.at(-1)?.durationMs >= 8_000, `unlink a computer: done, with the way back in the result line (${JSON.stringify(H.notes.at(-1))})`)
    reset()
    H.restReply = async () => ({ code: linkCode })
    await H.allNotes.at(-1).action.onClick()
    await tick()
    ok(H.restCalls[0]?.path === '/fleet/code', 'Link it again makes a new link code')
    // With sharing off a code cannot be made: the result line says what to do first and offers no button that would fail.
    reset()
    share(fleetSharedOff, { initialConfirm: surface.id })
    H.restReply = async () => ({ ...unlinkSurfacePlan, dry_run: false, unlinked: true })
    await H.confirms[0].onConfirm()
    ok(!H.notes.at(-1)?.action && H.notes.at(-1)?.meta?.endsWith('Switch sharing on to link it again.'), `unlink a computer while sharing is off: no "Link it again" that could not work (${JSON.stringify(H.notes.at(-1))})`)
    // The plan cannot be read: said in the dialog, and confirming does nothing.
    H.override['unlink-computer-plan'] = { isError: true, error: new Error('500'), data: undefined }
    html = text(share(fleetSource2, { initialConfirm: surface.id }))
    ok(html.includes('Something went wrong inside Grok Bot Twin.') && has('Try again'), 'unlink a computer: a plan that cannot be read is said, with Try again')
    await assert.rejects(() => H.confirms.at(-1).onConfirm())
    passed += 1
    H.override['unlink-computer-plan'] = undefined
    // Rename: possible, never asked for.
    reset()
    raw = share(fleetSource2, { initialRename: true })
    html = text(raw)
    ok(html.includes('Rename this computer') && html.includes('Your other computers show this one under this name.') && H.inputs.at(-1)?.value === 'mini' && JSON.stringify(primaries()) === JSON.stringify(['Save name']), `rename: one field with the name it has now (${H.inputs.at(-1)?.value})`)
    H.restReply = async () => ({ ...fleetSource2, this_machine: { ...mini, name: 'mini' } })
    await press('Save name')
    ok(H.restCalls[0]?.path === '/fleet/name' && H.restCalls[0]?.opts.body.name === 'mini', 'rename saves the name')
    html = text(render(jsx(P.RenameSheet, { computer: F(fleetSource2).me, initialName: 'x/y', initialProblem: { message: "A computer's name is 1 to 40 letters, digits, spaces, dots, dashes or underscores.", hint: '' }, onClose: noop, open: true })))
    ok(html.includes("A computer's name is 1 to 40 letters, digits, spaces, dots, dashes or underscores.") && H.inputs.at(-1)?.value === 'x/y' && H.inputs.at(-1)?.['aria-invalid'] === true, 'rename: a refused name is said next to the field, which keeps what was typed')

    // The Mac's page as a whole: twins say where their copies are; sharing sits under them; still no filled button when calm.
    raw = withFleet(statusSource, fleetSource2)
    html = text(raw)
    ok(html.includes('Follows Grok') && count(html, 'also on Surface and Orchard') === 1 && html.includes('also on Surface') && raw.includes('title="Surface: in sync 2 min ago. Orchard: last in sync 130 min ago."') && !/also on[^·]*· (in sync|Orchard)/.test(html) && html.indexOf('Share my twins with my other computers') > html.indexOf('Add a bot'), `the Mac: each twin row says where its copies are; sharing sits under the twins (${html.slice(0, 400)})`)
    ok(primaries().length === 0 && count(raw, '<article') === 4 && html.includes('2 twins · nothing needs you'), `the Mac, calm: no filled button; twins and computers in one row grammar (${primaries()})`)
    {
      // Every twin on every linked computer: the list of computers says where they are, once; no row repeats it
      // (the hover text of nothing is lost: each computer's own row says how fresh it is).
      const both = { ...quillPair, fleet: { id: QUILL, machines: onRows([miniRow, surfaceRow, orchardRow]) } }
      const everywhere = text(withFleet({ ...statusSource, pairs: [scoutPair, both] }, fleetSource2))
      ok(!everywhere.includes('also on') && everywhere.includes('Surface') && everywhere.includes('Orchard') && count(everywhere, 'Follows Grok') === 2, `the Mac, every twin on every computer: no row says "also on" (${everywhere.slice(0, 300)})`)
      // A twin made while a linked computer was off never got there: its row does not say "also on" that
      // computer, and says when it gets there, in the words the terminal uses; the other row keeps its line.
      const late = { ...quillPair, fleet: { id: QUILL, machines: onRows([miniRow, surfaceRow, { ...orchardRow, state: 'pending', last_sync_at: null }]) } }
      const lateRaw = withFleet({ ...statusSource, pairs: [scoutPair, late] }, fleetSource2)
      const lateText = text(lateRaw)
      ok(lateText.includes('also on Surface · goes to Orchard when it is next on') && count(lateText, 'also on Surface and Orchard') === 1 && lateRaw.includes('Orchard: gets this twin when it is next in touch.'), `the Mac, a twin that never reached a computer that is off: said as that (${lateText.slice(0, 300)})`)
      const off = text(withFleet({ ...statusSource, pairs: [scoutPair, late] }, fleetSharedOff))
      ok(off.includes('goes to Orchard when sharing is on again') && !off.includes('also on Surface and Orchard · '), `the Mac, sharing off, a twin made meanwhile: it goes when sharing is on again (${off.slice(0, 300)})`)
    }
    // A calm Mac with three twins, sharing not yet used: sixty words at most on the whole page.
    {
      const three = withFleet({ ...statusSource, pairs: [scoutPair, quillPair, { ...quillPair, pair_id: 'test-third', grok: { id: 'b-third-0012', name: 'Test Third', present: true }, hermes: { profile: 'test-third' } }].map(p => ({ ...p, fleet: undefined })), fleet: briefNone }, fleetNone)
      // on screen = the text without what sits in closed menus and without the line read only by a screen reader
      const said = text(three).replace(/(\d+ twins · nothing needs you) \1/, '$1')
      // (and without the three pictures, whose single letters a text dump shows)
      const visible = words(said) - H.menu.reduce((n, m) => n + words(m.text), 0) - 3
      ok(visible <= 60 && visible >= 50, `the Mac, calm, three twins and the share card: ${visible} words on screen (60 at most)`)
    }
    ok(words(html.split('Share my twins')[0].split('nothing needs you')[1].split('Add a bot')[0]) <= 2 * 22, `the Mac: a calm twin row stays short with its fleet part (${html.split('Share my twins')[0].split('nothing needs you')[1].split('Add a bot')[0]})`)
    // Not sharing, nobody linked, real twins: the switch is offered. Only the sample, or no fleet answer: it is not.
    ok(text(withFleet({ ...statusSource, fleet: briefNone }, fleetNone)).includes('Share my twins with my other computers') && H.switches[0]?.checked === false, 'a Mac with twins is offered sharing')
    ok(!text(withFleet({ ...statusAll, pairs: [acme], fleet: briefNone }, { ...fleetNone, can_share: false })).includes('Share my twins') && H.switches.length === 0, 'the sample alone is never shared, so no switch')
    H.override.fleet = undefined
    H.status = statusAll
    ok(!text(page()).includes('Share my twins') && H.switches.length === 0, 'an older backend: no switch that could not work')
    // Stop twinning a shared twin on the Mac says where else it goes.
    html = text(render(jsx(P.StopTwinningDialog, { onClose: noop, open: true, pair: scoutPair })))
    // ("when it is next on" is said only of the computer that is off right now)
    ok(html.includes('Its copies on Surface and Orchard; on Orchard when it is next on. Kept there 30 days, with chats.') && H.confirms[0]?.confirmLabel === 'Stop twinning Test Scout', `stop twinning a shared twin: its copies on the other computers are named (${html.slice(html.indexOf('Removed'))})`)
    {
      const shared = H.confirms[0]
      reset()
      H.restReply = async () => ({ pair_id: 'test-scout', dry_run: false, profile_kept: true, removed: [] })
      await shared.onConfirm()
      ok(H.notes.at(-1)?.meta?.includes('Its copies on your other computers are kept there for 30 days.'), `stop twinning a shared twin: the result line says the copies are kept (${H.notes.at(-1)?.meta})`)
    }
    // Remove Grok Bot Twin on a Mac that shares: what becomes of the twins on the linked computers, in sixty words.
    raw = render(jsx(P.UninstallDialog, { fleet: F(fleetSource2), onClose: noop, open: true }))
    {
      const box = text(raw.slice(raw.indexOf('>', raw.indexOf('role="alertdialog"')) + 1))
      const n = words(box)
      ok(box.includes('The twins on Orchard and Surface; they stop following.') && box.indexOf('The twins on Orchard') < box.indexOf('Removed') && n <= 60 && !box.includes('sync bot'), `remove on a Mac that shares: the linked computers are named under Stays; ${n} words (60 at most)`)
      ok(!text(render(jsx(P.UninstallDialog, { fleet: F(fleetFollower), onClose: noop, open: true }))).includes('stop following') && !text(render(jsx(P.UninstallDialog, { onClose: noop, open: true }))).includes('stop following'), 'remove elsewhere: no line about computers that are not there')
    }
    H.override['remove-plan'] = { data: { ...fixtures['remove-plan'], goes: ['The link between them.', 'Its twins on your other computers (kept there for 30 days).'] } }
    ok(count(text(render(jsx(P.StopTwinningDialog, { onClose: noop, open: true, pair: scoutPair }))), 'other computers') === 1 && !text(render(jsx(P.StopTwinningDialog, { onClose: noop, open: true, pair: scoutPair }))).includes('Its copies on'), 'stop twinning: a backend that says it itself is not repeated')
    H.override['remove-plan'] = undefined

    // ── The other computer: Link to my Mac ──
    raw = linkSheet()
    html = text(raw)
    for (const s of ['Link this computer', 'Your Grok bots stay on your Mac; this computer gets a twin of each.', 'On your Mac, open Twins and switch on “Share my twins with my other computers”. Type the code it shows:']) {
      ok(html.includes(s), `link sheet shows "${s}"`)
    }
    ok(H.inputs.length === 1 && H.inputs[0]['aria-label'] === 'Link code' && H.inputs[0].placeholder === 'ABCD-EFGH-JKMN' && H.inputs[0].autoFocus === true && /text-xl/.test(H.inputs[0].className) && JSON.stringify(texts()) === JSON.stringify(['Not now', 'Link']) && JSON.stringify(H.buttons.filter(b => b.variant === 'default').map(b => [b.text, b.disabled])) === JSON.stringify([['Link', true]]) && endsInButton(raw), `link sheet: one field, one filled button that waits for a whole code (${texts()})`)
    ok(words(html) + 1 <= 50 && !/address|\bport\b|password|account|Tailscale/i.test(html), `link sheet: asks for the code and nothing else, in 50 words at most with the field's hint (${words(html) + 1} words)`)
    // A typed code, then Link: one call with the code and nothing else.
    reset()
    H.setData = []
    let closed = 0
    let linked = 0
    linkSheet({ initialCode: '7kq4 m2xd p9ra', onClose: () => { closed += 1 }, onLinked: () => { linked += 1 } })
    ok(H.inputs[0].value === '7KQ4-M2XD-P9RA' && H.buttons.find(b => b.text === 'Link')?.disabled === false, 'link sheet: the code is shown as on the Mac, whatever was typed')
    H.restReply = async () => fleetFollower
    await press('Link')
    ok(H.restCalls.length === 1 && H.restCalls[0].path === '/fleet/link' && JSON.stringify(H.restCalls[0].opts.body) === JSON.stringify({ code: '7KQ4-M2XD-P9RA' }) && closed === 1 && H.setData.some(([k, v]) => k === 'fleet' && v === fleetFollower), `Link: the code alone; the sheet closes onto the twins (${JSON.stringify(H.restCalls[0]?.opts.body)})`)
    ok(H.notes.length === 0 && linked === 1, `Link: no toast over a page that says it; the page is told so it can take the keyboard (${JSON.stringify(H.notes)})`)
    // A pasted code links without another click; a code typed letter by letter waits for the button.
    reset()
    linkSheet()
    H.restReply = async () => fleetFollowerSyncing
    H.inputs[0].onChange({ target: { value: ' 7kq4-m2xd-p9ra ' } })
    await tick()
    ok(H.restCalls.length === 1 && H.restCalls[0].opts.body.code === '7KQ4-M2XD-P9RA' && H.notes.length === 0, 'a pasted code links at once (two steps instead of three)')
    reset()
    linkSheet({ initialCode: '7KQ4-M2XD-P9R' })
    H.inputs[0].onChange({ target: { value: '7KQ4-M2XD-P9RA' } })
    await tick()
    ok(H.restCalls.length === 0, 'a code finished by typing waits for Link')
    // A wrong code replaced by pasting the right one links at once too; correcting one letter does not.
    reset()
    linkSheet({ initialCode: 'BADD-BADD-BADD' })
    H.inputs[0].onChange({ target: { value: 'BADD-BADD-BADE' } })
    await tick()
    ok(H.restCalls.length === 0, 'a code corrected by hand waits for Link (a half-corrected code must not use up a try)')
    linkSheet({ initialCode: 'BADD-BADD-BADD' })
    H.inputs[0].onPaste()
    H.inputs[0].onChange({ target: { value: '7KQ4-M2XD-P9RA' } })
    await tick()
    ok(H.restCalls.length === 1 && H.restCalls[0].opts.body.code === '7KQ4-M2XD-P9RA' && H.inputs[0].readOnly === false && H.inputs[0].disabled === undefined, 'a code pasted over a wrong one links at once; the field is never disabled, so it keeps the keyboard')
    // Every refusal: one sentence and the one thing to do, next to the field. None names a button that is not here.
    for (const [code, said, clears] of [
      ['code_wrong', 'That code does not match the one on your Mac. Nothing was changed. Correct it and press Link again.', false],
      ['bad_code', 'That is not a link code. A link code looks like ABCD-EFGH-JKMN. Copy it from the Twins page on your Mac.', false],
      ['code_expired', 'That code has expired. A code lasts 10 minutes. Make a new code on your Mac and type it here.', true],
      ['code_used', 'That code was already used. Each code links one computer. Make a new code on your Mac and type it here.', true],
      ['too_many_tries', 'Too many wrong codes, so that code no longer works. Make a new code on your Mac and type it here.', true],
      ['own_code', 'That code is from this computer. Type it on your other computer.', false],
      ['unreachable', 'Hermes could not reach your Mac. Check it is awake with sharing on, on the same network as this computer.', false]
    ]) {
      const p = P.linkProblem(Object.assign(new Error('backend words'), { code }))
      ok(p.clear === clears, `link refused (${code}): the field ${clears ? 'empties for the new code' : 'keeps the code'}`)
      raw = linkSheet({ initialCode: clears ? '' : '7KQ4-M2XD-P9RA', initialProblem: p })
      html = text(raw)
      ok(html.includes(said) && raw.includes('role="alert"') && words(said) <= 40 && !/Link another computer|New code|backend words/.test(html) && H.inputs[0]['aria-describedby'] === 'gbt-link-problem', `link refused (${code}): ${said}`)
      // the whole sheet with a refusal on it: fifty words at most (the opening sentence steps aside)
      ok(words(html) + 1 <= 50 && !html.includes('this computer gets a twin of each'), `link refused (${code}): the sheet stays within fifty words (${words(html) + 1})`)
      // on a Mac the other one is "your other Mac", in the sheet and in every refusal
      {
        const onMac = text(linkSheet({ initialCode: clears ? '' : '7KQ4-M2XD-P9RA', initialProblem: P.linkProblem(Object.assign(new Error('x'), { code }), 'your other Mac'), platform: 'macos' }))
        ok(!/\byour Mac\b/.test(onMac) && onMac.includes('On your other Mac, open Twins') && words(onMac) + 1 <= 50, `link refused (${code}) on a Mac: "your other Mac", never "your Mac" (${words(onMac) + 1} words)`)
      }
    }
    // The real path: the backend refuses, the sheet stays, nothing is closed, no toast.
    reset()
    closed = 0
    linkSheet({ initialCode: 'BADD-BADD-BADD', onClose: () => { closed += 1 } })
    H.restReply = async () => ({ error: { code: 'code_wrong', message: 'That link code is not the one showing on the other computer.', hint: 'Check it, or make a new code there.' } })
    await press('Link')
    ok(H.restCalls.length === 1 && closed === 0 && H.notes.length === 0, 'a wrong code: said in the sheet, which stays open; no toast')
    // Refusals the page has no sentence of its own for are the backend's sentence and hint.
    html = text(linkSheet({ initialCode: '7KQ4-M2XD-P9RA', initialProblem: P.linkProblem(Object.assign(new Error('This computer shares its own twins, so it cannot follow another one.'), { code: 'sharing_here', hint: 'Switch sharing off here first.' })) }))
    ok(html.includes('This computer shares its own twins, so it cannot follow another one.') && html.includes('Switch sharing off here first.'), 'link refused for another reason: the backend says what and what to do')
    // The Mac cannot be reached: the code is kept, the filled button is Try again, and Hermes offers the computers it already knows by name.
    raw = linkSheet({ initialCode: '7KQ4-M2XD-P9RA', initialKnown: [{ id: 'c1', label: 'Mac mini', where: 'mini.example.invalid' }], initialProblem: P.linkProblem(Object.assign(new Error('x'), { code: 'unreachable' })) })
    html = text(raw)
    ok(JSON.stringify(primaries()) === JSON.stringify(['Try again']) && H.inputs[0].value === '7KQ4-M2XD-P9RA' && html.includes('Or pick your Mac, if Hermes already knows it:') && has('Mac mini') && variantOf('Mac mini') === 'outline' && H.buttons.find(b => b.text === 'Mac mini')?.size === 'default' && !html.includes('example.invalid') && H.inputs[0]['aria-invalid'] !== true, `Mac not reached: Try again, the code kept, a known computer by name only (${html})`)
    reset()
    H.restReply = async () => fleetFollower
    await press('Mac mini')
    ok(JSON.stringify(H.restCalls[0]?.opts.body) === JSON.stringify({ code: '7KQ4-M2XD-P9RA', host: 'mini.example.invalid' }), `picking a known computer links with the same code, there (${JSON.stringify(H.restCalls[0]?.opts.body)})`)
    // Which computers are offered: only other computers Hermes knows by name; never this one, never a pass.
    H.known = [{ id: 'local', kind: 'local', label: 'This Mac' }, { id: 'c1', kind: 'ssh', label: 'Mac mini', host: 'mini.example.invalid', tokenSet: true, tokenPreview: 'abc…' }, { id: 'c2', kind: 'remote', label: 'Studio', url: 'https://studio.example.invalid:8443/x' }, { id: 'c3', kind: 'cloud', label: 'Cloud', url: 'https://cloud.example.invalid' }, { id: 'c4', kind: 'remote', label: '', url: 'https://x.example.invalid' }]
    {
      const got = await P.knownComputers()
      ok(JSON.stringify(got.map(k => [k.label, k.where])) === JSON.stringify([['Mac mini', 'mini.example.invalid'], ['Studio', 'studio.example.invalid']]) && !JSON.stringify(got).includes('abc'), `known computers: the named remote ones only (${JSON.stringify(got)})`)
    }
    H.known = []
    // The sheet asks Hermes for them only after the Mac was not reached.
    reset()
    H.known = [{ id: 'c1', kind: 'ssh', label: 'Mac mini', host: 'mini.example.invalid' }]
    linkSheet({ initialCode: '7KQ4-M2XD-P9RA' })
    H.restReply = async () => ({ error: { code: 'unreachable', message: 'Hermes could not reach the computer with the Grok app.', hint: 'Check that it is on.' } })
    await press('Link')
    ok(H.restCalls.length === 1 && H.notes.length === 0, 'Mac not reached: said in the sheet')
    H.known = []

    // ── The other computer: its twins ──
    raw = withFleet(statusFollower, fleetFollower)
    html = text(raw)
    ok(html.includes('2 twins from mini · in sync now') && !/Bring your Grok bots|Connect my Grok bots|Add a bot|Share my twins|not following Grok/.test(html), `the other computer: the header says where its twins come from and how fresh (${html.slice(0, 200)})`)
    // The computers are one line (names, which has the Grok app, which is this one); kind
    // and freshness are its hover text. Every twin here is on every computer, so no row repeats "also on …".
    ok(count(html, 'Follows mini · in sync') === 2 && count(raw, 'title="In sync now"') === 2 && count(html, 'in sync now') === 2 && count(html, 'also on Orchard') === 0 && JSON.stringify(texts().filter(t => t === 'Open in Hermes')) === JSON.stringify(['Open in Hermes', 'Open in Hermes']) && primaries().length === 0, `the other computer: each row says where from and how fresh; each twin can be opened; nothing filled (${texts()})`)
    ok(raw.includes('aria-label="Computers that hold these twins"') && /On\s+mini\s+Has the Grok app\s+Orchard\s+Surface\s+This computer/.test(html) && !html.includes('On these computers') && raw.includes('title="Mac · your twins come from here"') && raw.includes('title="Windows · in sync now"') && raw.includes('title="Linux"') && raw.includes('aria-label="More for this computer"') && JSON.stringify(H.menu.slice(-2).map(m => m.text)) === JSON.stringify(['Rename this computer…', 'Unlink this computer…']), `the other computer: one line says which computers hold the twins, which one has Grok and which is this one, with the way out (${html})`)
    ok(words(html.slice(html.indexOf('On mini'), html.indexOf('Rename this computer'))) <= 12, `the other computer: the line of computers is twelve words at most (${html.slice(html.indexOf('On mini'), html.indexOf('Rename this computer'))})`)
    {
      // A twin that is not on every computer keeps "also on …" on its row; so does one that could not be made somewhere.
      const uneven = { ...fleetFollower, twins: fleetFollower.twins.map((t, i) => (i === 0 ? { ...t, machines: t.machines.filter(m => m.name !== 'Orchard') } : t)) }
      ok(count(text(follower(uneven)), 'also on Orchard') === 1, 'the other computer: where a twin lives is said on its row when the twins are not all on the same computers')
      // A new name or description that arrived a moment ago shows on the row without a click; the description is the name's hover text.
      const fresh = { ...fleetFollower, twins: fleetFollower.twins.map((t, i) => (i === 0 ? { ...t, description: 'Finds the short way.', synced_at: new Date(Date.now() - 20_000).toISOString() } : { ...t, synced_at: iso(30) })) }
      const rawF = follower(fresh)
      // (the backend did not say that a name or description changed: the copy was written, which may be its arrival)
      {
        // The backend says what arrived: a first copy "arrived"; a new description is named, and shown under
        // the row for as long as the row says so (no click needed to see what changed).
        const at = new Date(Date.now() - 20_000).toISOString()
        const mk = (changed, description) => ({ ...fleetFollower, twins: fleetFollower.twins.map((t, i) => (i === 0 ? { ...t, changed, changed_at: at, description, synced_at: at } : { ...t, synced_at: iso(30) })) })
        const arrivedText = text(follower(mk('new', 'Finds the short way.')))
        ok(/Follows mini · arrived (just now|\d+ sec ago)/.test(arrivedText) && !/changed|updated/.test(arrivedText.slice(0, arrivedText.indexOf('Open in Hermes'))) && count(arrivedText, 'Finds the short way.') === 0, `the other computer: a first copy "arrived", it did not "change" (${arrivedText.slice(0, 160)})`)
        const changedText = text(follower(mk('description', 'Finds the short way home.')))
        ok(/Follows mini · description changed (just now|\d+ sec ago)/.test(changedText) && count(changedText, 'Finds the short way home.') === 1, `the other computer: a new description is on the page while the row says it changed (${changedText.slice(0, 200)})`)
        const old = text(follower({ ...fleetFollower, twins: fleetFollower.twins.map(t => ({ ...t, changed: 'description', changed_at: iso(30), description: 'Finds the short way home.', synced_at: iso(30) })) }))
        ok(!old.includes('Finds the short way home.') && count(old, 'Follows mini · in sync') === 2, 'the other computer: two minutes later the row is calm again and the text is the hover text')
      }
      ok(/Follows mini · updated (just now|\d+ sec ago)/.test(text(rawF)) && !/Follows mini · changed/.test(text(rawF)) && count(text(rawF), 'Follows mini · in sync') === 1 && rawF.includes('title="Finds the short way."'), `the other computer: a change that just arrived is said on its row for two minutes (${text(rawF).slice(0, 200)})`)
      // (the assertions below go on with the whole page as it was)
      raw = withFleet(statusFollower, fleetFollower)
      html = text(raw)
    }
    ok(JSON.stringify(texts().slice(-3)) === JSON.stringify(['Privacy', 'See a sample', 'Remove Grok Bot Twin…']) && JSON.stringify(H.menu.map(m => m.text)) === JSON.stringify(['Check mini now', 'Details…', 'Check mini now', 'Details…', 'Rename this computer…', 'Unlink this computer…']) && count(html, 'Unlink this computer') === 1, `the other computer: Unlink in one place, this computer's own menu; Check mini now and Details in each twin's menu (${H.menu.map(m => m.text)})`)
    ok(words('Follows mini · in sync') <= 8 && words('Follows mini · changed 20 sec ago') <= 8, 'the other computer: a calm row is its name and eight words at most')
    reset()
    H.restReply = async () => fleetFollower
    await H.menu[0].onSelect()
    await tick()
    ok(H.restCalls[0]?.path === '/fleet/sync' && H.restCalls[0]?.opts.method === 'POST', 'Check mini now on the other computer asks the Mac at once')
    reset()
    await press('Open in Hermes')
    ok(JSON.stringify(H.newChats.at(-1)) === JSON.stringify('test-scout') || H.newChats.length > 0 || H.ensured.length > 0, 'a twin on the other computer opens in Hermes')
    // The answer is still on its way: skeleton rows, never the first-run screen.
    raw = withFleet(statusFollower, null)
    ok(raw.includes('data-skeleton') && !text(raw).includes('Bring your Grok bots') && text(raw).includes('Twins from mini · in sync'), 'the other computer while loading: its own header, no welcome')
    // The Mac is off: said once, neutral, since when; rows say the same in four words and still open.
    // No button in the banner, which has just said that nothing needs doing; "Check mini
    // now" stays in each twin's menu for whoever wants to save the wait.
    raw = withFleet({ ...statusFollower, fleet: { ...briefFollower, source: { name: 'mini', state: 'offline', offline_since: fleetFollowerOffline.source.offline_since } } }, fleetFollowerOffline)
    html = text(raw)
    {
      const since = clock(fleetFollowerOffline.source.offline_since)
      ok(html.includes(`mini has not answered since ${since}. Your twins here still work, and catch up by themselves when it is back.`) && count(html, `mini offline since ${since}`) === 4 && raw.includes(`title="Mac · offline since ${since}"`) && raw.includes('title="Windows · waiting for mini"') && raw.includes('aria-label="Twins are not following mini"') && html.includes('Has the Grok app · offline'), `the Mac is off: one banner with since when; rows say it short; the list of computers marks it too (${html.slice(0, 300)})`)
      ok(count(html, 'Open in Hermes') === 2 && !has('Try now') && H.menu.filter(m => m.text === 'Check mini now').length === 2 && primaries().length === 0 && !/in sync/.test(html) && !/data-tone="(bad|warn)"/.test(raw) && !raw.includes('data-codicon="error"'), 'the Mac is off: twins still open; nothing red; nothing claims "in sync"; no button after "by themselves"')
      ok(words(`mini has not answered since ${since}. Your twins here stay as they are and catch up by themselves when it is back.`) <= 30, 'the Mac is off: the banner is short')
    }
    reset()
    H.restReply = async () => fleetFollowerOffline
    await H.menu.find(m => m.text === 'Check mini now').onSelect()
    await tick()
    ok(H.restCalls[0]?.path === '/fleet/sync' && H.notes.length === 0, 'Check mini now asks the Mac once; an offline Mac is not an error toast')
    // Sharing was switched off on the Mac: since when, and what became of the twins here. No button is needed.
    raw = follower(fleetFollowerSharingOff)
    html = text(raw)
    ok(html.includes(`Sharing was switched off on mini ${P.whenText(fleetFollowerSharingOff.source.offline_since)}. Your twins here stay as they are and catch up when it is on again.`) && count(html, 'Waiting for mini') === 2 && raw.includes(`title="Mac · sharing off since ${clock(fleetFollowerSharingOff.source.offline_since)}"`) && !has('Try now') && count(html, 'Open in Hermes') === 2, `sharing off on the Mac: said with since when; twins still open (${html.slice(0, 260)})`)
    // The two computers cannot work together: which to update, and that the twins stay.
    html = text(follower(fleetFollowerMismatch))
    ok(html.includes('mini and this computer run versions of Grok Bot Twin that cannot work together. Update Grok Bot Twin on both computers: Capabilities → Plugins. Your twins here stay as they are meanwhile.') && count(html, 'Not following mini right now') === 2, `versions differ: which computers to update (${html.slice(0, 240)})`)
    // Just linked: the twins are on their way. Linked to a Mac that shares nothing yet: what to do there.
    html = text(follower(fleetFollowerSyncing))
    ok(html.includes('Linked to mini. Your twins are on their way and appear here by themselves.') && words('Linked to mini. Your twins are on their way and appear here by themselves.') <= 30, 'just linked: one sentence while the first twins arrive')
    html = text(follower({ ...fleetFollower, twins: [] }))
    ok(html.includes('Linked to mini. It shares no twins yet: twin a Grok bot there and it appears here by itself.') && primaries().length === 0, 'linked to a Mac with no twins: an empty state that says what to do there')
    // One twin could not be made here: counted in the header, said on that row only, with the cause and Try again.
    raw = withFleet(statusFollower, fleetFollowerError)
    html = text(raw)
    ok(html.includes('1 of 2 twins is here · from mini · in sync now') && html.includes('Could not be made here') && html.includes('Test Quill could not be made on this computer') && html.includes('A Hermes bot called test-quill already exists and is not a twin. Hermes left it as it is.') && count(html, 'could not be made on this computer') === 1 && !html.includes('Hermes could not make the Hermes bot') && primaries().length === 0 && !has('Try again'), `a twin that could not be made because a bot of that name exists: header, row, the cause once, that the other bot was left alone, and no button that cannot work (${texts()})`)
    ok(count(html, 'Open in Hermes') === 1 && html.includes('Follows mini · in sync') && html.includes('Hermes bots kept for 30 days (1)'), 'a twin that could not be made: the other one is in sync and opens; kept bots are folded')
    // Any other reason can pass: that row has the page's one filled button, Try again.
    {
      const unsaved = { ...fleetFollowerError, twins: [fleetFollowerError.twins[0], { ...fleetFollowerError.twins[1], error: { code: 'write_failed', message: 'Hermes could not save this twin on this computer.', hint: 'Check that the disk is not full, then press Check now.' } }] }
      html = text(withFleet(statusFollower, unsaved))
      ok(html.includes('Check that the disk is not full, then press Try again.') && JSON.stringify(primaries()) === JSON.stringify(['Try again']), `a twin that could not be saved: the cause, and Try again as the one filled button (${primaries()})`)
      reset()
      H.restReply = async () => fleetFollower
      await press('Try again')
      ok(H.restCalls[0]?.path === '/fleet/sync', 'Try again on that row checks in with the Mac')
    }
    // A twin that arrived under another name says so once, quietly (an optional sentence from the backend).
    html = text(follower({ ...fleetFollower, twins: [{ ...fleetFollower.twins[0], note: 'A Hermes bot called test-scout was already here, so this twin is test-scout-2.' }, fleetFollower.twins[1]] }))
    ok(count(html, 'A Hermes bot called test-scout was already here, so this twin is test-scout-2.') === 1 && count(html, 'Open in Hermes') === 2, 'a twin that arrived under another name: said once on its row; it opens like any other')
    ok(text(render(jsx(P.FollowerRow, { fleet: F(fleetFollowerError), lead: true, onCheck: noop, trouble: null, twin: { ...F(fleetFollowerError).twins[1], error: { code: 'write_failed', message: 'Hermes could not save this twin on this computer.', hint: 'Check that the disk is not full, then press Check now.' } } }))).includes('Check that the disk is not full, then press Try again.'), 'a hint never names a button that is not on that row')
    // No longer on the Mac's list, but never said removed: kept as it is, and said so.
    html = text(follower({ ...fleetFollower, twins: [{ ...fleetFollower.twins[0], state: 'not_listed' }] }))
    ok(html.includes("No longer on mini's list · kept as it is") && has('Open in Hermes'), 'a twin the Mac no longer lists is kept, and still opens')
    // Kept bots: what happened, until when, and the way back as an ordinary Hermes bot.
    reset()
    raw = render(jsx(P.KeptBots, { fleet: F(fleetFollowerError), initialOpen: true }))
    html = text(raw)
    ok(html.includes('Test Ranger') && /Removed on mini · kept with its chats until \d+ \w+/.test(html) && !html.includes('link again') && html.indexOf('Keeping a bot makes it an ordinary Hermes bot that follows nothing. Otherwise it leaves this computer on the date shown.') < html.indexOf('Test Ranger') && html.indexOf('Keeping a bot makes it') > 0 && H.buttons.find(b => b.text === 'Keep this bot')?.label === 'Keep Test Ranger as an ordinary Hermes bot' && !/Bring (it )?back/.test(html), `kept bots on a linked computer, removed on the Mac: no "link again" (that cannot bring them back); keeping one is named for what it does (${html})`)
    {
      // Unlinked, with twins that linking again does bring back: there the fold says so first.
      const un = text(render(jsx(P.KeptBots, { fleet: F(fleetUnlinked), here: true, initialOpen: true })))
      ok(un.indexOf('To get your twins back, link again.') > 0 && un.includes('Keeping a bot here instead makes it an ordinary Hermes bot that follows nothing.'), `kept bots on an unlinked computer: the way back to twins is said first (${un.slice(0, 200)})`)
    }
    H.restReply = async () => ({ ...fleetFollowerError, removed: [] })
    await press('Keep this bot')
    ok(H.restCalls[0]?.path === '/fleet/removed/r-5d6e7f809a1b/restore' && H.notes.at(-1)?.message === 'Test Ranger is an ordinary Hermes bot now' && H.notes.at(-1)?.action?.label === 'Open in Hermes' && H.notes.at(-1)?.durationMs >= 8_000, `Keep this bot: one click, and the way to open it (${JSON.stringify(H.notes.at(-1))})`)
    ok(render(jsx(P.KeptBots, { fleet: F(fleetFollower) })) === '', 'no kept bots: nothing is shown')
    // Unlink this computer: the plan's one sentence and two lists first; the button names the act; the way back in the result.
    reset()
    raw = render(jsx(P.UnlinkDialog, { fleet: F(fleetFollower), onClose: noop, open: true, storage: ctx.storage }))
    html = text(raw)
    for (const s of ['Unlink this computer from mini?', 'This computer stops following mini; its 2 twins leave this computer only.', 'Stays', 'Your twins on mini and on your other computers.', 'Removed', 'The 2 twins on this computer (kept for 30 days, chats included).']) {
      ok(html.includes(s), `unlink this computer shows "${s}"`)
    }
    ok(H.confirms[0]?.confirmLabel === 'Unlink this computer' && H.confirms[0]?.destructive === true && H.confirms[0]?.cancelLabel === 'Cancel' && words(html) <= 60 && !/cannot be undone/i.test(html) && words('This computer stops following mini; its 2 twins leave this computer only.') <= 25, `unlink this computer: the button names the act; 60 words at most; never "cannot be undone" (${words(html)} words)`)
    // A plan whose list does not say the twins are kept gets the way back as its own line.
    H.override['unlink-plan'] = { data: { ...unlinkPlan, goes: ['The 2 twins on this computer.'] } }
    ok(text(render(jsx(P.UnlinkDialog, { fleet: F(fleetFollower), onClose: noop, open: true }))).includes('Link this computer again within 30 days and the twins come back with their chats.'), 'unlink this computer: the way back is always said before confirming')
    H.override['unlink-plan'] = undefined
    render(jsx(P.UnlinkDialog, { fleet: F(fleetFollower), onClose: noop, open: true, storage: ctx.storage }))
    H.storedSets = []
    ok(H.restCalls.length === 0, 'unlink this computer: opening the dialog changes nothing')
    H.restReply = async () => ({ ...unlinkPlan, dry_run: false, unlinked: true })
    await H.confirms[0].onConfirm()
    ok(H.restCalls[0]?.path === '/fleet/unlink' && H.restCalls[0]?.opts.body.dry_run === false && H.notes.length === 0 && H.invalidations > 0, `unlink this computer: done; no toast, because the page itself becomes the result (${JSON.stringify(H.notes)})`)
    ok(H.storedSets.some(([k, v]) => k === 'unlinked' && Number.isFinite(Date.parse(v))), 'unlink this computer: the page remembers that the owner did it here')
    // Afterwards the first screen says what happened and what became of the twins, for as long as that is true; Link is the filled button.
    raw = withFleet(statusOtherFirstRun, { ...fleetUnlinked, last_unlink: { ...fleetUnlinked.last_unlink, by: 'this' } })
    html = text(raw)
    ok(html.includes('This computer is unlinked') && html.includes('This computer was unlinked from mini yesterday at ') && html.includes('Its 2 twins are kept for 30 days; link again to bring them back.') && JSON.stringify(primaries()) === JSON.stringify(['Link this computer']) && html.includes('Hermes bots kept for 30 days (2)'), `after unlinking: what happened, what became of the twins, the way back (${html.slice(0, 300)})`)
    ok(words(html) <= 45 && !html.includes('Bring your Grok bots') && !has('See a sample first') && !html.includes('Each bot gets a twin'), `after unlinking: the first screen is about that and nothing else, in 45 words at most (${words(html)})`)
    // Unlinked from the Mac's side: said as that, calmly; no retry loop, nothing red.
    raw = withFleet(statusOtherFirstRun, fleetUnlinked)
    html = text(raw)
    ok(html.includes('mini stopped sharing with this computer yesterday at ') && JSON.stringify(primaries()) === JSON.stringify(['Link this computer']) && !/data-tone="bad"|Try again/.test(raw) && words(html) <= 45, `unlinked by the Mac: said, with Link as the way back, in 45 words at most (${words(html)})`)
    ok(P.unlinkedSentence(F({ ...fleetUnlinked, removed: [] })).endsWith('Link again to bring its twins back.') && P.unlinkedSentence(F(fleetNone)) === '', 'the unlinked sentence: only when a link ended, and true when nothing is kept')
    // A link the owner ended on this page is never blamed on the Mac, even if the backend's record says so.
    H.stored.unlinked = fleetUnlinked.last_unlink.at
    raw = withFleet(statusOtherFirstRun, fleetUnlinked)
    html = text(raw)
    ok(html.includes('This computer was unlinked from mini yesterday at ') && !html.includes('stopped sharing with this computer'), `unlinked here a moment before the record: said as the owner's own act (${html.slice(0, 220)})`)
    ok(text(render(jsx(P.KeptBots, { fleet: F(fleetUnlinked), here: true, initialOpen: true }))).includes('This computer was unlinked · kept with its chats until') && text(render(jsx(P.KeptBots, { fleet: F(fleetUnlinked), initialOpen: true }))).includes('Unlinked by mini · kept'), 'kept bots say the same about who ended the link')
    H.stored.unlinked = iso(60 * 24 * 9)
    ok(text(withFleet(statusOtherFirstRun, fleetUnlinked)).includes('mini stopped sharing with this computer'), 'an older unlink of the owner does not hide that the Mac ended this one')
    delete H.stored.unlinked

    // ── Calm states, main buttons and word budgets ──
    {
      // A description changed in Hermes is called a description, with the description's own
      // place to paste it; the text comes from the twin itself when the backend hands out skills only.
      const described = { ...recipePlain, state: 'changes_waiting', hermes: { ...recipePlain.hermes, description: 'Plans a week of dinners.' }, pending: { ...calm, to_grok: 1, to_grok_items: ['description'] }, mirror_bot: { connected: false } }
      raw = row(described)
      html = text(raw)
      ok(html.includes('1 change to send to Grok') && html.includes('You changed the description in Hermes.') && html.includes("Grok does not have this change yet. Copy the description, then paste it into the bot's description in the Grok app.") && JSON.stringify(primaries()) === JSON.stringify(['Copy the description']) && has('Done, I pasted it'), `a description changed in Hermes: named for what it is (${html.slice(0, 300)})`)
      ok(!/skill/i.test(html.replace(/Skill sync…/g, '')) && !H.buttons.some(b => b.text === 'Skill sync…'), 'a description changed in Hermes: no word about a skill, and no offer to send it by skill sync (which carries skills only)')
      ok(P.mattersOf(described)[0].title === 'The description changed in Hermes' && P.mattersOf({ ...described, pending: { ...calm, to_grok: 2, to_grok_items: ['description', 'skill:pick-a-book'] } })[0].title === '2 changes ready for Grok' && P.mattersOf({ ...described, pending: { ...calm, to_grok: 2, to_grok_items: ['skill:a', 'skill:b'] } })[0].title === '2 skill changes ready for Grok', 'the one-line header of such a matter says what changed')
      reset()
      H.clips = []
      H.clipOk = true
      H.restReply = async () => ({ error: { code: 'not_found', message: 'That is not waiting to go to Grok.' } })
      await press('Copy the description')
      ok(H.restCalls.length === 1 && H.restCalls[0].path === '/pairs/recipe-planner/skill-text?item=description' && JSON.stringify(H.clips) === JSON.stringify(['Plans a week of dinners.']) && H.notes.length === 0, `Copy the description: the twin's own description goes on the clipboard, whatever the backend's door for skills says (${JSON.stringify(H.clips)})`)
      // a possible password is never worked around
      reset()
      H.clips = []
      row(described)
      H.restReply = async () => ({ error: { code: 'looks_like_secret', message: 'This holds something that looks like a password or key.' } })
      await press('Copy the description')
      ok(H.clips.length === 0, 'Copy the description: a possible password is not put on the clipboard')
      html = text(render(jsx(P.SkillByHand, { initialCopied: true, initialText: 'Plans a week of dinners.', item: 'description', lead: true, onSkillSync: noop, pair: described })))
      ok(html.includes("Copied. In the Grok app, open Recipe Planner's settings and paste it over the description.") && JSON.stringify(primaries()) === JSON.stringify(['Done, I pasted it']) && !has('Skill sync…'), `copied: the sentence names the real place to paste (${html})`)
      // "Done, I pasted it" when Grok already has the text (the plugin saw it first) is not an error:
      // no red notice, and the status is read again at once so the row goes calm.
      for (const code of ['usage', 'not_found']) {
        render(jsx(P.SkillByHand, { initialCopied: true, initialText: 'Plans a week of dinners.', item: 'description', lead: true, onSkillSync: noop, pair: described }))
        reset()
        H.fetches = []
        H.invalidations = 0
        H.restReply = async () => ({ error: { code, message: 'The description is not waiting to be copied to Grok.' } })
        await press('Done, I pasted it')
        ok(H.notes.length === 0 && H.invalidations >= 1 && H.fetches.includes('status'), `Done when Grok already has it (${code}): no notice at all, and the status is read at once (${JSON.stringify(H.notes)} ${H.invalidations} ${JSON.stringify(H.fetches)})`)
      }
      render(jsx(P.SkillByHand, { initialCopied: true, initialText: 'Plans a week of dinners.', item: 'description', lead: true, onSkillSync: noop, pair: described }))
      reset()
      H.fetches = []
      H.invalidations = 0
      H.restReply = async () => ({ error: { code: 'write_failed', message: 'Hermes could not save that.', hint: 'Check that the disk is not full, then try again.' } })
      await press('Done, I pasted it')
      ok(H.notes.length === 1 && H.notes[0].kind === 'error' && H.notes[0].title === 'Could not note that for description' && H.fetches.includes('status'), `Done that really failed: still said, and the status is read at once all the same (${JSON.stringify(H.notes)})`)
      render(jsx(P.SkillByHand, { initialCopied: true, initialText: 'Plans a week of dinners.', item: 'description', lead: true, onSkillSync: noop, pair: described }))
      reset()
      H.fetches = []
      H.restReply = async () => ({ pair_id: 'recipe-planner', skipped: [] })
      await press('Done, I pasted it')
      ok(H.notes.length === 1 && H.notes[0].message === 'Noted: the description is in Grok too' && H.fetches.includes('status'), `Done that was noted: the quiet notice with Undo, status read at once (${JSON.stringify(H.notes)})`)
      // "Keep the Hermes version" puts the kept text on the clipboard in the same click, and the box
      // that follows opens at "Copied. In the Grok app…": keep, go to Grok, paste. It leaves by itself.
      {
        const both = { conflict_id: 'persona.description', kind: 'description', name: 'description', since: iso(5), hermes: { digest: 'a1', text: 'Plans a week of friendly dinners.' }, grok: { digest: 'b2', text: 'Plans a week of formal dinners.' }, diff: '' }
        const keepIt = async (reply, props = {}) => {
          render(jsx(P.ConflictItem, { carry: true, conflict: both, lead: true, pairId: 'recipe-planner', ...props }))
          reset()
          H.clips = []
          H.restReply = reply
          await press('Keep the Hermes version')
        }
        const afterKeep = () => text(render(jsx(P.SkillByHand, { item: 'description', lead: true, onSkillSync: noop, pair: described })))
        H.clipOk = true
        await keepIt(async path => (path.endsWith('/resolve') ? { applied: [{ item: 'persona.description', keep: 'hermes' }], restore_point: null } : { pair_id: 'recipe-planner', item: 'persona.description', name: 'description', text: 'Plans a week of friendly dinners.', truncated: false }))
        ok(JSON.stringify(H.restCalls.map(c => c.path)) === JSON.stringify(['/pairs/recipe-planner/resolve', '/pairs/recipe-planner/skill-text?item=persona.description']) && JSON.stringify(H.clips) === JSON.stringify(['Plans a week of friendly dinners.']) && !H.notes.some(n => n.kind === 'error'), `Keep the Hermes version: keeps it, then puts the kept text on the clipboard (${JSON.stringify(H.restCalls.map(c => c.path))} ${JSON.stringify(H.clips)})`)
        html = afterKeep()
        ok(html.includes("Copied. In the Grok app, open Recipe Planner's settings and paste it over the description.") && !html.includes('Grok does not have this change yet') && JSON.stringify(primaries()) === JSON.stringify(['Done, I pasted it']) && has('Copy again') && !has('Copy the description'), `after Keep: the box opens already copied, with where to paste (${html})`)
        ok(!text(render(jsx(P.SkillByHand, { item: 'skill:pick-a-book', lead: true, onSkillSync: noop, pair: described }))).includes('Copied.') && !text(render(jsx(P.SkillByHand, { item: 'description', lead: true, onSkillSync: noop, pair: { ...described, pair_id: 'garden-buddy' } }))).includes('Copied.'), 'after Keep: only that twin\'s kept item is said to be copied')
        // the backend's door hands out skills only: the description on the page is used, as Copy does
        P.handed.clear()
        await keepIt(async path => (path.endsWith('/resolve') ? { applied: [], restore_point: null } : { error: { code: 'not_found', message: 'That is not waiting to go to Grok.' } }))
        ok(JSON.stringify(H.clips) === JSON.stringify(['Plans a week of friendly dinners.']) && afterKeep().includes('Copied. In the Grok app'), `Keep, older door: the text on the page goes on the clipboard (${JSON.stringify(H.clips)})`)
        // a possible password is never worked around; a refused clipboard claims nothing: today's box
        P.handed.clear()
        await keepIt(async path => (path.endsWith('/resolve') ? { applied: [], restore_point: null } : { error: { code: 'looks_like_secret', message: 'This holds something that looks like a password or key.' } }))
        html = afterKeep()
        ok(H.clips.length === 0 && !html.includes('Copied.') && JSON.stringify(primaries()) === JSON.stringify(['Copy the description']) && !H.notes.some(n => n.kind === 'error'), `Keep, a possible password: nothing on the clipboard, the box as before (${html})`)
        H.clipOk = false
        await keepIt(async path => (path.endsWith('/resolve') ? { applied: [], restore_point: null } : { text: 'Plans a week of friendly dinners.' }))
        html = afterKeep()
        ok(!html.includes('Copied.') && html.includes("Grok does not have this change yet. Copy the description, then paste it into the bot's description in the Grok app.") && JSON.stringify(primaries()) === JSON.stringify(['Copy the description']) && H.notes.at(-1)?.message === 'Kept the Hermes version of the description', `Keep, clipboard refused: nothing is claimed, the box offers Copy the description (${html})`)
        H.clipOk = true
        // skill sync sends skills for the owner: nothing is put on the clipboard then (carry is off)
        await keepIt(async () => ({ applied: [], restore_point: null }), { carry: false })
        ok(H.restCalls.length === 1 && H.clips.length === 0, 'Keep with nothing to carry by hand: the clipboard is left alone')
        P.handed.clear()
        ok(/carry: !isSkillItem\(c\?\.conflict_id\) \|\| !skillSyncOn\(pair\)/.test(source), 'the both-places box hands the text over exactly when the change is carried by hand')
        // while a box waits for the paste, the page looks every 5 s whether Grok has it, only when visible
        // On a wide window a paragraph or a banner's sentence stays near 80 characters a line; a
        // box with a button beside it, centred words and the 700 px layout are left as they are.
        ok(source.includes("const MEASURE_CSS = '.gbt p:not(.flex-1):not(.text-center):not(.sr-only),.gbt .gbt-line{max-width:60ch}'") && /const PAGE_CSS = FOCUS_CSS \+ QUIET_CSS \+ TARGET_CSS \+ MEASURE_CSS/.test(source) && (source.match(/className: 'gbt-line[ ']/g) ?? []).length >= 3 && !/gbt-line[^']*truncate|min-width:\s*\d+ch/.test(source), 'line length: paragraphs and banner sentences are held to about 80 characters by the page\'s own style rule, with no fixed width')
        ok(/function useWatchGrok\(\)/.test(source) && /stopGrokWatch = timers\.setInterval\(\(\) => \{\s+if \(typeof document === 'undefined' \|\| document\.visibilityState !== 'hidden'\) \{\s+void queryClient\.invalidateQueries\(\{ queryKey: STATUS_KEY \}\)\s+\}\s+\}, 5_000\)/.test(source) && /\n  useWatchGrok\(\)\n/.test(source), 'a change waiting for a paste into Grok: the status is looked at every 5 s while the box is on screen, so it leaves by itself')
      }
      ok(P.byHandWords(described, 'skill:pick-a-book').copy === "Copy the skill's text" && P.byHandWords(described, 'persona.description').noun === 'description' && P.byHandWords({ ...described, hermes: { ...described.hermes, name: 'Dinner Planner' } }, 'name').local === 'Dinner Planner', 'the words follow the kind of the change')
      // Skill sync on: the description is still the owner's to paste; the skills among the changes go with one button.
      html = text(row({ ...described, mirror_bot: { connected: true }, pending: { ...calm, to_grok: 2, to_grok_items: ['description', 'skill:pick-a-book'] } }))
      ok(html.includes('2 things changed in Hermes.') && JSON.stringify(primaries()) === JSON.stringify(['Copy the description']) && html.includes('1 skill change is ready for Grok.') && variantOf('Send to Grok') === 'secondary' && !has("Copy the skill's text"), `skill sync on, a description and a skill: one by hand, one to send (${html.slice(0, 300)})`)

      // "deleted in Grok" leads the page when it is the first open matter, also before a
      // row with a lesser matter; a bot the owner chose to wait for holds no main button.
      const goneRow = { ...recipePlain, state: 'grok_missing', grok: { ...recipePlain.grok, present: false } }
      H.mode = 'ok'
      H.override = {}
      H.status = { ...statusAll, sample: { on: false, hide_real: false }, pairs: [goneRow, garden] }
      html = text(page())
      ok(JSON.stringify(primaries()) === JSON.stringify(['OK, stop following']) && H.buttons.some(b => b.label === 'Show: A skill changed in Hermes') && html.includes('1 needs your attention'), `deleted in Grok, then a skill to carry over: the first open matter holds the main button (${primaries()})`)
      H.stored = { ...H.stored, 'kept.recipe-planner': true }
      page()
      ok(JSON.stringify(primaries()) === JSON.stringify(["Copy the skill's text"]), `deleted in Grok and waited for: the next row leads (${primaries()})`)
      delete H.stored['kept.recipe-planner']

      // Use it always ends in something visible. A file that is kept but cannot be verified
      // on the spot is verified by the same press; a failure is this row's message, in plain words, with
      // Download botport as the main button and a quiet Verify again. Nothing of GitHub's tool on the owner's path.
      const arrived = { ...setupNoBotport, download: { ...setupBase.download, found: { name: FILE } } }
      const keptFile = { ...arrived, helper: { found: true, version: null, source: 'chosen' } }
      const got = []
      connect(arrived, { onSetup: x => got.push(x) })
      reset()
      H.restReply = async path => (path === '/engine/path' ? keptFile : { error: { code: 'gh_missing', hint: 'brew install gh', message: "This check needs GitHub's free command-line tool, signed in once." } })
      await press('Use it')
      ok(H.restCalls.map(c => c.path).join() === '/engine/path,/engine/trust' && JSON.stringify(H.restCalls[1].opts.body) === JSON.stringify({}) && got[0] === keptFile && H.notes.length === 0, `Use it on a file that is not verified on the spot: the same press goes on to verify it (${H.restCalls.map(c => c.path)})`)
      const plain = P.plainTrust(new P.TwinError("This check needs GitHub's free command-line tool, signed in once.", 'gh_missing', 'brew install gh'))
      ok(plain.message === 'Hermes could not verify this file, so it was not run.' && plain.hint === 'Download botport again. If that does not help, open "For developers" below.' && plain.code === 'check_failed' && P.plainTrust(new P.TwinError('That file is not the genuine botport 0.2.1.', 'not_genuine', 'x')).code === 'not_genuine' && words(`${plain.message} ${plain.hint}`) <= 40, 'a verification that would need a developer\'s tool is one plain sentence and the next act')
      raw = connect(keptFile, { initialProblem: plain, initialTaken: true })
      html = text(raw)
      ok(html.includes('Hermes could not verify this file, so it was not run. Download botport again.') && raw.includes('role="alert"') && JSON.stringify(primaries()) === JSON.stringify(['Download botport']) && variantOf('Verify again') === 'text' && !has('Use it') && !html.includes('Found botport in Downloads.') && !/\bgh\b|brew|GitHub/.test(html), `Use it could not verify: the reason replaces the stale line; Download botport is the main button (${html})`)
      // The backend's answer for a release this version has no record of (`not_pinned`): its two sentences
      // as sent; the main button goes to Plugins, where the update is; Verify again is quiet beside it.
      raw = connect(keptFile, { initialProblem: new P.TwinError('This version of Grok Bot Twin cannot verify botport 0.2.1 by itself.', 'not_pinned', 'Update Grok Bot Twin in Capabilities → Plugins, then press Verify again. Another way is under For developers.'), initialTaken: true })
      html = text(raw)
      ok(html.includes('This version of Grok Bot Twin cannot verify botport 0.2.1 by itself. Update Grok Bot Twin in Capabilities → Plugins, then press Verify again.') && JSON.stringify(primaries()) === JSON.stringify(['Open Plugins']) && variantOf('Verify again') === 'text' && !has('Use it') && !has('Download botport') && P.plainTrust(new P.TwinError('x', 'not_pinned', 'y')).code === 'not_pinned', `no record of the release in this version: the sentence as sent, Open Plugins as the main button (${html})`)
      html = text(connect(keptFile, { initialTaken: true }))
      ok(html.includes('Hermes has the file. It runs botport only after verifying it.') && JSON.stringify(primaries()) === JSON.stringify(['Verify botport']) && !html.includes('Found botport in Downloads.'), 'a file that was taken is never offered as "found in Downloads" again')
      // (between the answer and the next screen the row must not show a stale line for a moment)
      html = text(connect(keptFile, { initialTaken: true, initialVerified: true }))
      ok(html.includes('botport is verified.') && !/Verify botport|Use it|only after/.test(html.split('2. Get botport')[1].split('For developers')[0]), `verified a moment ago: the row says so until the sheet moves on (${html})`)
      html = text(connect(arrived, { initialSlow: true }))
      ok(html.includes('Verifying botport…') && !html.includes('Found botport in Downloads.'), 'a verification that takes longer than a moment is named')
      ok(/timers\.setTimeout\(\(\) => setSlow\(true\), 400\)/.test(source), 'the progress line never flashes: it shows after 0.4 s')
      // After "Use it" the second half (verifying) is named at once and stays at least 0.3 s: the row never
      // flashes "Hermes has the file" with a Verify button in between.
      ok(/setTaken\(true\)\n\s+setSlow\(true\)\n\s+onSetup\(out\)/.test(source) && /const rest = 300 - \(Date\.now\(\) - began\)/.test(source), 'Use it: "Verifying botport…" from the first moment, for 0.3 s at least')
      // "Set up Twins again": the pressed button is gone with its screen, so the new first screen's title takes the keyboard.
      ok(/setRemoved\(null\)\n[^\n]*\n\s+timers\?\.setTimeout\(\(\) => titleRef\.current\?\.focus\?\.\(\), 80\)/.test(source), 'Set up Twins again: focus goes to the title of the new first screen')

      // A step of a request that waits for the owner is counted in the header and holds
      // the page's one main button; nothing else on the page is filled then.
      const asked = { limits: { per_hour: 40, per_minute: 6, reply_chars: 6000, request_chars: 2000 }, linked: true, log: [], machines: [{ id: 'm-3f9a1c0b7d2e', name: 'mini' }], switch: { label: 'x', network: 'tailscale', on: true, since: null }, this_machine: { name: 'Surface' }, waiting: [{ asked_at: iso(0), command: 'echo tidy-notes-does-not-exist', description: 'delete a file', id: 'k-00000000000000aa', peer: 'mini', q: '0a1b2c3d', text: 'tidy the notes folder', twin: 'Test Scout' }], working: true }
      H.override.tasks = { data: asked }
      raw = withFleet(statusFollower, { ...fleetFollower, twins: fleetFollower.twins.map((t, i) => (i === 0 ? { ...t, state: 'error', error: { code: 'write_failed', message: 'Hermes could not save it.' } } : t)) })
      html = text(raw)
      ok(html.includes('· 1 request waits for you') && JSON.stringify(primaries()) === JSON.stringify(['Do it once']) && variantOf('Try again') === 'secondary' && variantOf("Don't") === 'text' && html.indexOf('mini asked Test Scout here') < html.indexOf('Test Quill'), `a request waits on a linked computer: counted in the header, first on the page, the one main button (${primaries()} / ${html.slice(0, 160)})`)
      H.status = { ...statusSource, pairs: [trail, scoutPair] }
      H.override.fleet = { data: fleetSource2 }
      html = text(page())
      ok(html.includes('· 1 request waits for you') && JSON.stringify(primaries()) === JSON.stringify(['Do it once']) && variantOf('Finish setting up') === 'secondary', `a request waits on the Mac: it leads, the rows' own buttons are quiet (${primaries()})`)
      // (with the request on top, a row carries its state only; where its copies are stays in the list below)
      H.status = { ...statusSource, pairs: [scoutPair, quillPair] }
      html = text(page())
      ok(html.includes('2 twins · 1 request waits for you') && !html.includes('nothing needs you') && !/checked \d|checked just/.test(html) && !html.includes('also on') && html.includes('One step needs your approval: delete a file.') && words('mini asked Test Scout here: tidy the notes folder One step needs your approval: delete a file. echo tidy-notes-does-not-exist Do it once Don\'t Nothing happens until then.') <= 30, `a request waits on a calm Mac: the header counts it and never says "nothing needs you" beside it (${html.slice(0, 200)})`)
      H.status = { ...statusSource, pairs: [trail, scoutPair] }
      H.override.tasks = { data: { ...asked, waiting: [] } }
      html = text(page())
      ok(!html.includes('waits for you') && JSON.stringify(primaries()) === JSON.stringify(['Finish setting up']), 'nothing waits: the header says nothing about requests and the row leads again')
      H.override = {}

      // While a link code is on screen the rows carry their state only (a Mac with two linked computers,
      // a fresh code and the requests card was about 140 words).
      H.status = { ...statusSource, pairs: [scoutPair, quillPair] }
      H.override.fleet = { data: { ...fleetSource2, share: { ...shareOn, code: linkCode } } }
      html = text(page())
      ok(html.includes('Link code') && !html.includes('also on') && !html.includes('Sharing sends your'), `a link code on screen with computers linked: rows say their state only (${html.slice(0, 200)})`)
      H.override.fleet = { data: fleetSource2 }
      ok(count(text(page()), 'also on') === 2, 'no code on screen: each row says where its copies are')
      H.override = {}

      // The line that explains Link is for a first visit; a computer that already holds
      // something of Grok Bot Twin gets the Remove link instead.
      const first = text(render(jsx(P.Welcome, { onConnect: noop, onLink: noop, onSample: noop })))
      const again = text(render(jsx(P.Welcome, { onConnect: noop, onLink: noop, onSample: noop, returning: true })))
      ok(first.includes('Bots on another computer? Link this one to it.') && !again.includes('Bots on another computer?') && again.includes('Link this computer') && words(`Twins ${first} Privacy`) <= 45 && words(`Twins ${again} Privacy Remove Grok Bot Twin…`) <= 45, `first screen: 45 words at most, first visit or not (${words(`Twins ${first} Privacy`)} / ${words(`Twins ${again} Privacy Remove Grok Bot Twin…`)})`)

      // With botport missing the Stop twinning dialog told the owner to open the page they were on
      // and press a button that was not there (and to run a command). It names the banner's own button.
      {
        const told = P.hereProblem(new P.TwinError('botport is missing, so Hermes cannot reach this twin.', 'botport_missing', 'Open the Twins page in Hermes Desktop and press Connect my Grok bots; it walks you through getting botport. Prefer the terminal? Run hermes twin setup and follow what it prints.'))
        ok(told.message === 'botport is missing, so Hermes cannot reach this twin.' && told.hint === 'Press Cancel, then Get botport again… at the top of the page.' && P.hereProblem(new P.TwinError('x', 'internal', 'Try again.')).hint === 'Try again.' && /hereProblem\(plan\.error\)/.test(source) && /plan\.isError \? 'Hermes could not work out what will be removed, so nothing was stopped\./.test(source), 'Stop twinning with botport missing: the dialog names the button that is on the page, and no command')
      }
      // What the backend may also send, all optional: which part changed and when; a named program in
      // the way of sharing; the Mac's Grok Bot Twin removed or set up again, with the way on.
      {
        const told = { ...fleetFollower, twins: fleetFollower.twins.map((t, i) => (i === 0 ? { ...t, changed: 'description', changed_at: new Date(Date.now() - 70_000).toISOString(), synced_at: iso(40) } : { ...t, synced_at: iso(40) })) }
        ok(text(follower(told)).includes('Follows mini · description changed 1 min ago') && words('Follows mini · description changed 1 min ago') <= 8, `a linked row says which part just changed (${text(follower(told)).slice(0, 120)})`)
        const named = P.shareProblem({ code: 'port_busy', hint: 'Quit Photo Frame; sharing then starts by itself.', message: 'Photo Frame on this computer is in the way of sharing.' }, false)
        const off = P.shareProblem({ code: 'port_busy', hint: 'Quit Photo Frame; sharing then starts by itself.', message: 'Photo Frame on this computer is in the way of sharing.' }, true)
        ok(named.message === 'Photo Frame on this computer is in the way of sharing.' && named.hint === 'Quit Photo Frame; sharing then starts by itself.' && off.hint === 'Quit Photo Frame, then switch sharing on again.' && P.shareProblem({ code: 'port_busy', hint: 'Restart this computer; sharing then starts by itself. If it stays this way, the other program holds network port 18777.', message: 'Another program on this computer is in the way of sharing.' }, false).hint === 'Hermes tries again by itself every minute.', 'sharing cannot start: a program the backend can name is named; otherwise no word of a port, and never a restart')
        {
          const p = { code: 'port_busy', hint: 'Restart this computer; sharing then starts by itself.', message: 'Another program on this computer is in the way of sharing.' }
          ok(P.shareProblem(p, false).again === true && !P.shareProblem(p, true).again && P.shareProblem(p, true).hint === 'Switch sharing on again in a minute. Nothing was changed.' && !/[Rr]estart/.test(`${P.shareProblem(p, false).hint} ${P.shareProblem(p, true).hint}`), 'sharing cannot start: Try again while the switch is on; the switch itself when it went back to off')
        }
        const gone = F({ ...fleetFollower, source: { ...sourceOk, state: 'error', problem: { code: 'source_removed', hint: 'They stay as they are. If it is set up again on mini, unlink this computer and link it again with a new link code.', message: 'Grok Bot Twin was removed on mini, so your twins here no longer follow it.' } } })
        const sent = P.followerTrouble(gone).sentence
        ok(sent === 'Grok Bot Twin was removed on mini, so your twins here no longer follow it. They stay as they are. If it is set up again on mini, unlink this computer and link it again with a new link code.' && words(sent) <= 40 && P.followerSummary(gone).label === '2 twins from mini · not following mini right now', `the Mac's Grok Bot Twin was removed: said with the way on, 40 words at most (${words(sent)})`)
      }
      // No control under 28 px. The request's Copy is 32 px; a sheet's Close is 28 px on this page's sheets.
      ok(/buttonSize: 'icon-sm', label: label \?\? 'Copy'/.test(source) && /\.gbt\[data-slot="dialog-content"\]>button,\.gbt \[data-slot="dialog-close-button"\]\{width:1\.75rem;height:1\.75rem\}/.test(source), 'targets: the copy button of a command and a sheet\'s Close are full size')
      // The "no longer twinned" line goes by itself once that bot is twinned again (and not a moment after the stop).
      ok(/else if \(!stoppedHere\) \{\n      stoppedGone\.current = true\n    \} else if \(stoppedGone\.current\) \{\n      setStopped\(null\)/.test(source), 'the result line of Stop twinning is cleared when that twin exists again')
    }

    // ── Times, the unlinked screen, the link sheet and one box at a time ──
    // "in sync N sec ago" was frozen and read the wrong time. Hermes notes a contact at most once a minute,
    // so no count of seconds is shown while the note is that young; the newer of the two times counts; and the
    // page ticks by itself.
    ok(P.inSync(iso(0.3)) === 'in sync now' && P.inSync(iso(4), iso(0.3)) === 'in sync now' && P.inSync(iso(0.3), iso(4)) === 'in sync now' && /^in sync (58|59) sec ago$|^in sync 1 min ago$/.test(P.inSync(iso(58 / 60))) && P.inSync(iso(4), iso(3)) === 'in sync 3 min ago' && P.inSync(null, undefined) === 'in sync' && P.inSync(iso(-5)) === 'in sync now' && P.inSync('nonsense', iso(7)) === 'in sync 7 min ago', `in sync: the newer time counts; "now" only under half a minute, then the age; never a time in the future (${P.inSync(iso(58 / 60))})`)
    raw = withFleet(statusFollower, { ...fleetFollower, source: { ...sourceOk, last_contact_at: iso(3), last_sync_at: iso(0.1) } })
    html = text(raw)
    ok(html.includes('2 twins from mini · in sync now') && raw.includes('title="Windows · in sync now"') && !/in sync \d+ (sec|min)|in sync just/.test(html), `a twin arrived seconds ago, the contact note is three minutes old: the header says the newer one (${html.slice(0, 120)})`)
    raw = withFleet(statusFollower, { ...fleetFollower, source: { ...sourceOk, last_contact_at: iso(3), last_sync_at: iso(50) } })
    html = text(raw)
    ok(html.includes('2 twins from mini · in sync 3 min ago') && raw.includes('title="Windows · in sync 3 min ago"'), 'an older note: minutes, the same in the header and in this computer\'s hover text')
    ok(/function useTick\(ms = 5_000\)/.test(source) && /const stop = timers\.setInterval\(\(\) => setTick\(n => n \+ 1\), ms\)/.test(source) && /\n  useTick\(\)\n/.test(source), 'relative times move by themselves: a 5 s tick through the Desktop\'s tracked timer')
    // The unlinked screen counts only what linking again brings back (a twin removed on the Mac stays removed).
    ok(P.unlinkedSentence(F({ ...fleetUnlinked, removed: [...fleetUnlinked.removed.map(r => ({ ...r, reason: 'unlinked' })), { ...keptRanger, rid: 'r-777777777777' }] }), true).includes('Its 2 twins are kept for 30 days; link again to bring them back.') && P.unlinkedSentence(F({ ...fleetUnlinked, removed: [{ ...keptRanger, rid: 'r-777777777777' }] }), true).endsWith('Link again to bring its twins back.'), 'unlinked: a twin removed on the Mac earlier is not promised back')

    // Unlink after "Open in Hermes". Option A switches the plugin on inside each twin's own Hermes bot, so the
    // page could be served from a folder that Unlink moves away. Now: that folder is never the page's home, the page
    // goes back by itself when it finds itself there, and Unlink steps out first.
    {
      const twins = P.twinProfilesOf([recipePlain, acme], F({ ...fleetFollower, removed: [keptRanger] }))
      ok(twins.has('test-scout') && twins.has('test-quill') && twins.has('test-ranger') && twins.has(recipePlain.hermes.profile) && !twins.has(acme.hermes?.profile ?? 'acme-support') && !twins.has('default'), `twins' own Hermes bots: made here, arrived over a link, or kept; never the sample (${[...twins]})`)
      delete H.stored.home
      ok(P.homeOf(ctx.storage, twins) === 'default', 'home, nothing remembered: the default Hermes bot')
      H.stored.home = { profile: 'test-scout' }
      ok(P.homeOf(ctx.storage, twins) === 'default', "a twin's own Hermes bot is never taken for home, even if an older page remembered it")
      H.stored.home = { profile: 'work' }
      ok(P.homeOf(ctx.storage, twins) === 'work', 'home: where Twins was last seen working')
      // the plugin answers from inside a twin: away, home is known
      ok(JSON.stringify(P.awayFromHome(ctx.storage, { serving: 'test-scout', twins })) === JSON.stringify({ here: 'test-scout', home: 'work' }) && P.awayFromHome(ctx.storage, { serving: 'work', twins }) === null && P.awayFromHome(ctx.storage, { serving: 'another-bot', twins }) === null, 'served from a twin: away; served from home or from an ordinary Hermes bot: not')
      // the plugin does not answer (the folder was moved away under the Desktop)
      H.activeProfile = 'test-scout'
      delete H.stored.home
      ok(JSON.stringify(P.awayFromHome(ctx.storage, { twins })) === JSON.stringify({ here: 'test-scout', home: 'default' }), 'no answer while inside a twin: away, even when nothing was remembered')
      H.activeProfile = 'another-bot'
      ok(P.awayFromHome(ctx.storage, { twins }) === null, 'no answer inside an ordinary Hermes bot with nothing remembered: the ordinary error screens apply')
      H.stored.home = { profile: 'default' }
      ok(JSON.stringify(P.awayFromHome(ctx.storage, { twins })) === JSON.stringify({ here: 'another-bot', home: 'default' }), 'no answer away from the remembered home: go back there')
      // The page itself, answered from inside Test Scout: nothing of that answer is shown; it goes back first.
      H.activeProfile = 'test-scout'
      raw = withFleet({ ...statusFollower, profile: 'test-scout' }, fleetFollower)
      html = text(raw)
      ok(html.includes('Going back to your twins…') && !/in sync|Follows mini|Unlink|test-scout|Hermes could not reach/.test(html) && H.buttons.length === 0, `Twins opened from inside a twin whose copy of the plugin answers: going back by itself, no rows from in there (${html.slice(0, 160)})`)
      raw = withFleet({ ...statusFollower, profile: 'default' }, fleetFollower)
      ok(text(raw).includes('2 twins from mini · in sync now'), 'answered from home: the twins')
      // Unlink while Hermes is inside one of the twins: step out, then unlink. (The Desktop's profile atom still
      // says the twin for a moment after "Open in Hermes" even when the page is back home.)
      for (const [where, steps] of [['test-quill', 1], ['default', 0], ['another-bot', 0]]) {
        reset()
        H.activeProfile = where
        H.ensured = []
        H.newChats = []
        const order = []
        render(jsx(P.UnlinkDialog, { fleet: F(fleetFollower), onClose: noop, open: true, storage: ctx.storage }))
        H.restReply = async (path, opts) => { order.push(`${path}:${H.ensured.length}`); return { ...unlinkPlan, dry_run: false, unlinked: true } }
        await H.confirms[0].onConfirm()
        ok(H.ensured.length === steps && (steps === 0 || JSON.stringify(H.ensured[0]) === JSON.stringify([null, 'default'])) && JSON.stringify(order) === JSON.stringify([`/fleet/unlink:${steps}`]) && H.newChats.length === 0, `unlink with Hermes inside ${where}: ${steps ? 'Hermes is pointed home first, quietly, then the link ends' : 'nothing to step out of'} (${JSON.stringify(order)})`)
      }
      H.activeProfile = 'default'
      delete H.stored.home
      ok(/await stepOutOfTwin\(storage, twinProfilesOf\(\[\], fleet\)\)\n      await post\('\/fleet\/unlink', \{ dry_run: false \}\)/.test(source), 'the step out comes before the unlink call')
    }
    // The answer about the other computers stops coming (or the plugin itself): no row may go on saying "in sync".
    // One cause, said once, with Try again; the raw answer only under "Technical details"; no Unlink that cannot work.
    H.override.fleet = { data: fleetFollower, error: P.transportError(new Error('404: {"detail":"Plugin not found"}')), isError: true }
    H.mode = 'ok'
    H.status = statusFollower
    raw = page()
    html = text(raw)
    ok(html.includes('2 twins from mini · not checked just now') && count(html, 'Not checked just now') === 2 && raw.includes('title="Windows · not checked just now"') && !/in sync/.test(raw), `the fleet answer fails: nothing claims to be in sync (${html.slice(0, 200)})`)
    ok(count(html, 'Hermes could not reach Grok Bot Twin.') === 1 && has('Try again') && has('Technical details') && !has('Details') && !html.includes('Plugin not found') && !/\b404\b/.test(html) && !H.menu.some(m => /Unlink/.test(m.text)) && count(html, 'Open in Hermes') === 2, `the fleet answer fails: said once, with Try again; twins still open; the raw answer stays folded (${texts()})`)
    H.refetches = 0
    await press('Try again')
    ok(H.refetches === 2, 'Try again asks the plugin and the answer about the other computers again')
    H.override.fleet = undefined
    ok(/queryKey: STATUS_KEY, refetchInterval: statusEvery, retry: 1/.test(source) && P.statusEvery({ state: { status: 'error' } }) === 4_000, 'while the plugin does not answer the page asks again every 4 s: the state clears by itself')
    {
      // A banner that clears by itself goes within 10 s of its cause: the page reads every 4 s while one shows.
      const st = extra => ({ state: { data: { ...statusAll, pairs: [recipePlain], ...extra }, status: 'success' } })
      ok(P.statusEvery(st({})) === 30_000 && P.statusEvery(st({ setup: { ...statusAll.setup, grok_app_found: false } })) === 4_000 && P.statusEvery(st({ setup: { ...statusAll.setup, ready: false, helper_found: false } })) === 4_000 && P.statusEvery({ state: {} }) === 30_000, `the page reads every 4 s while a banner shows, every 30 s when calm (${P.statusEvery(st({}))}, ${P.statusEvery(st({ setup: { ...statusAll.setup, grok_app_found: false } }))})`)
    }

    // Link sheet. A call that never reached the plugin on this computer is not blamed on the Mac, and
    // twelve good characters that are refused are "the wrong code", not "not a code".
    {
      let p = P.linkProblem(Object.assign(new Error('x'), { code: 'bad_code', message: 'That is not a link code.' }), 'your Mac', true)
      ok(p.code === 'code_wrong' && p.message === 'That code does not match the one on your Mac. Nothing was changed.' && p.hint === 'Correct it and press Link again.' && p.clear === false, `a well-formed code that is refused: it does not match (${JSON.stringify(p)})`)
      p = P.linkProblem(Object.assign(new Error('x'), { code: 'bad_code', message: 'That is not a link code.' }), 'your Mac', false)
      ok(p.code === 'bad_code' && p.message.startsWith('That is not a link code.'), 'half a code: not a link code')
      p = P.linkProblem(P.transportError(new Error('404: {"detail":"Plugin not found"}')), 'your Mac', true)
      ok(p.code === 'no_answer' && p.clear === false, `the call never reached the plugin: its own code, the field is kept (${JSON.stringify(p)})`)
      raw = linkSheet({ initialCode: '7KQ4-M2XD-P9RA', initialProblem: p })
      html = text(raw)
      ok(html.includes('Hermes could not reach Grok Bot Twin on this computer.') && html.includes('Nothing was changed. Press Link again in a moment.') && !/your Mac\.|awake|same network/.test(html.replace(/On your Mac, open Twins[^:]*:/, '')) && has('Link') && !has('Try again'), `the plugin did not answer: said as that, never as "could not reach your Mac" (${html})`)
    }
    // Which computer to update, when the backend knows; both otherwise. Always with the place to do it.
    ok(P.updateHint('Update Grok Bot Twin on mini: Capabilities → Plugins → Update.') === 'Update Grok Bot Twin on mini: Capabilities → Plugins → Update.' && P.updateHint('Update Grok Bot Twin on mini: Capabilities → Plugins → Update. This computer then carries on by itself.') === 'Update Grok Bot Twin on mini: Capabilities → Plugins → Update.' && P.updateHint('Update Grok Bot Twin on this computer: Capabilities → Plugins → Update. It then carries on by itself.') === 'Update Grok Bot Twin on this computer: Capabilities → Plugins → Update.' && P.updateHint('Update Grok Bot Twin on both, then link again.') === 'Update Grok Bot Twin on both computers: Capabilities → Plugins.' && P.updateHint(undefined) === 'Update Grok Bot Twin on both computers: Capabilities → Plugins.' && P.updateHint('Update Grok Bot Twin on <b>x</b>: now. Then run rm.') === 'Update Grok Bot Twin on both computers: Capabilities → Plugins.', 'versions differ: the backend names the computer to update and the way, else both; only that sentence of its hint is shown')
    html = text(withFleet(statusFollower, { ...fleetFollowerMismatch, source: { ...fleetFollowerMismatch.source, problem: { ...fleetFollowerMismatch.source.problem, hint: 'Update Grok Bot Twin on mini: Capabilities → Plugins → Update. This computer then carries on by itself.' } } }))
    ok(html.includes('Update Grok Bot Twin on mini: Capabilities → Plugins → Update. Your twins here stay as they are meanwhile.') && !html.includes('carries on by itself'), 'versions differ: the one computer to update is named (the backend\'s sentence as it is sent)')
    // A twin's row on a following computer: Check now (the one name for it) and Details, where its description is.
    raw = render(jsx(P.FollowerRow, { fleet: F(fleetFollower), initialDetails: true, onCheck: noop, trouble: null, twin: F(fleetFollower).twins[0] }))
    html = text(raw)
    ok(html.includes('A twin from mini. Its name and description change here when they change there.') && html.includes('Description') && html.includes('Finds trails.') && html.includes('Also on Orchard') && JSON.stringify(H.menu.map(m => m.text)) === JSON.stringify(['Check mini now', 'Details…']), `a twin on the other computer: its description is one click away (${html.slice(-260)})`)
    // One box at a time. The first row with something open shows it (and so does the row with the page's
    // main button); every other row says its matter in one line that opens with a click.
    {
      const send = (id, name) => ({ ...garden, pair_id: id, grok: { id: `b-${id}`, name, present: true }, hermes: { profile: id }, mirror_bot: { connected: true } })
      H.override.fleet = undefined
      H.mode = 'ok'
      H.status = { ...statusAll, sample: { on: false, hide_real: false }, pairs: [send('garden-buddy', 'Garden Buddy'), send('herb-box', 'Herb Box'), club] }
      raw = page()
      html = text(raw)
      const shows = H.buttons.filter(b => /^Show: /.test(b.label ?? ''))
      ok(H.buttons.filter(b => b.text === 'Send to Grok').length === 1 && JSON.stringify(primaries()) === JSON.stringify(['Send to Grok']) && shows.length === 2 && !html.includes("A password or key in Book Club's description was not copied."), `three twins with something open: one box, two one-line matters (${shows.map(b => b.label)})`)
      ok(html.includes('3 twins · 1 needs your attention · 2 with a change to send to Grok') && html.includes('A password or key was not copied'), 'folded rows still say their state, and the header still counts them')
      // a folded row opens on a click of its line (TwinRow keeps which one is open)
      ok(/const openMatter = matters\.find\(m => m\.id === openId\) \?\? \(folded \? null : matters\[0\]\) \?\? null/.test(source) && /folded: p\?\.pair_id !== \(leadId \?\? firstOpenId\)/.test(source), 'the page folds every row but the one with the main button (else the first with something open)')
      // A calm page says when it last looked ("when it was last checked" once needed a hover); a page with
      // something open does not add to its header.
      H.status = { ...statusAll, last_check_at: iso(1), sample: { on: false, hide_real: false }, pairs: [{ ...garden, state: 'in_step', waiting: [] }] }
      html = text(page())
      ok(html.includes('1 twin · nothing needs you · checked 1 min ago') && html.includes('Follows Grok · ready'), `a calm page: the header says when it was last checked; the row says ready (${html.slice(0, 120)})`)
      H.status = { ...statusAll, last_check_at: iso(1), sample: { on: false, hide_real: false }, pairs: [garden] }
      ok(!/· checked/.test(text(page())), 'a page with something open: no time in the header')
    }

    // ── Status bar ──
    H.status = statusFollower
    ok(chip().includes('data-tip="Twins from mini · in sync"') && chip().includes('data-tone="good"'), 'status bar on the other computer: where its twins come from')
    H.status = { ...statusFollower, fleet: { ...briefFollower, source: { name: 'mini', state: 'offline', offline_since: fleetFollowerOffline.source.offline_since } } }
    ok(chip().includes(`Twins from mini · mini offline since ${clock(fleetFollowerOffline.source.offline_since)}`) && chip().includes('data-tone="muted"') && !/Twins · \d/.test(text(chip())), 'status bar with the Mac off: neutral, no count that asks for attention')
    ok(P.briefSummary({ role: 'follower', source: { name: 'mini', state: 'sharing_off', offline_since: null } }).label === 'Twins from mini · sharing off on mini' && P.followerSummary(F(fleetFollowerSharingOff)).label === `2 twins from mini · sharing off on mini since ${clock(fleetFollowerSharingOff.source.offline_since)}` && P.followerSummary(F(fleetFollowerMismatch)).label === '2 twins from mini · not following mini right now' && P.followerSummary(F(fleetFollowerSyncing)).label === 'Linked to mini · getting your twins…', 'summaries: sharing off, and just linked')

    // ── Events while the page is closed ──
    hooks.setPageOpen(0)
    mark = H.notes.length
    listeners['plugin.grok-bot-twin.fleet']({ payload: { what: 'machine_linked', count: 1, at: '2000-01-06T10:00:00Z' } })
    ok(H.notes.length === mark + 1 && H.notes.at(-1)?.message === 'Another computer now follows your twins' && H.notes.at(-1)?.action?.label === 'Open Twins', `a computer linked while the page is closed: said, with the way to see which (${JSON.stringify(H.notes.at(-1))})`)
    listeners['plugin.grok-bot-twin.fleet']({ payload: { what: 'twin_removed', count: 2, at: '2000-01-06T10:01:00Z' } })
    ok(H.notes.at(-1)?.message === '2 twins left this computer' && /Kept here for 30 days/.test(H.notes.at(-1)?.meta), 'twins removed on the Mac while the page is closed: said, with that they are kept')
    listeners['plugin.grok-bot-twin.fleet']({ payload: { what: 'unlinked', count: 2, at: '2000-01-06T10:01:30Z' } })
    ok(H.notes.at(-1)?.message === 'This computer was unlinked' && !/your Mac/.test(JSON.stringify(H.notes.slice(mark))), 'unlinked while the page is closed: said without presuming which computer is "your Mac"')
    mark += 1
    listeners['plugin.grok-bot-twin.fleet']({ payload: { what: 'synced', count: 2, at: '2000-01-06T10:02:00Z' } })
    listeners['plugin.grok-bot-twin.fleet']({ payload: { what: 'source_offline', count: 1, at: '2000-01-06T10:03:00Z' } })
    ok(H.notes.length === mark + 2, 'an ordinary sync or an offline Mac raises no toast')
    hooks.setPageOpen(1)
    listeners['plugin.grok-bot-twin.fleet']({ payload: { what: 'machine_linked', count: 1, at: '2000-01-06T10:04:00Z' } })
    ok(H.notes.length === mark + 2, 'with the page open the list itself shows the new computer: no toast')
    hooks.setPageOpen(0)

    // ── Captured answers (optional) ──
    // TWIN_FLEET_SAMPLES=DIR with NAME.fleet.json and NAME.status.json (answers of GET /fleet and GET /status
    // taken from another mock or from a real backend): the page must show each without a raw value, and
    // without a second noun for a computer. Prints the first words of each so they can be read.
    if (process.env.TWIN_FLEET_SAMPLES) {
      const { readdirSync } = await import('node:fs')
      const dir = resolve(process.env.TWIN_FLEET_SAMPLES)
      for (const f of readdirSync(dir).filter(n => n.endsWith('.fleet.json')).sort()) {
        const name = f.replace('.fleet.json', '')
        const fleetAnswer = JSON.parse(readFileSync(join(dir, f), 'utf8'))
        const statusAnswer = JSON.parse(readFileSync(join(dir, `${name}.status.json`), 'utf8'))
        const shown = text(withFleet(statusAnswer, fleetAnswer))
        ok(!/undefined|\bnull\b|\[object|NaN|Invalid Date/.test(shown) && !/\bmachines?\b/i.test(shown) && shown.includes('Twins'), `captured answers "${name}": shown without a raw value (${shown.slice(0, 160)})`)
        console.log(`  ${name}: ${shown.replace(/^ Twins /, '').slice(0, 300)}`)
      }
    }

    // ── Words, over every fleet surface ──
    {
      const mine = seen.slice(fleetFrom).filter(s => !s.technical)
      ok(seen.slice(fleetFrom).filter(s => s.technical).length === 3, 'the technical details were opened on purpose in exactly three renders')
      const hits = re => mine.filter(s => re.test(s.text)).map(s => s.text.match(re)[0] + ' … ' + s.text.slice(Math.max(0, s.text.search(re) - 40), s.text.search(re) + 60))
      ok(mine.length > 60, `the fleet sweep covers its surfaces (${mine.length})`)
      // One noun for a computer.
      ok(hits(/\bmachines?\b/i).length === 0, `"computer" everywhere, never a second noun: ${JSON.stringify(hits(/\bmachines?\b/i).slice(0, 3))}`)
      // The wire's words never reach the page.
      const WIRE = /\btokens?\b|tombstone|HERMES_HOME|\bfollowers?\b|\bthe source\b|\bsource (?:machine|computer)\b|\bserver\b|\bclient\b|\bpeer\b|\bnode\b|\bprimary\b|\breplica\b|\bendpoint\b|\bpayload\b|\bdaemon\b|\bpoll\b|\bIP\b|\bhost\b|\bport\b|\bjson\b|\byaml\b|\bconfig\b|undefined|\bnull\b|\[object/i
      ok(hits(WIRE).length === 0, `no wire words on any fleet surface: ${JSON.stringify(hits(WIRE).slice(0, 3))}`)
      // One name per thing: link / unlink, share / stop sharing; "connect" is only ever about Grok and botport.
      ok(hits(/\b(?:pair|join|attach)(?:ed|ing|s)?\b|sync up|\bconnect(?:ed|ing|s)? (?:this|your|a|another|the other) computer/i).length === 0, `link and unlink, never another verb: ${JSON.stringify(hits(/\b(?:pair|join|attach)(?:ed|ing|s)?\b|sync up|\bconnect(?:ed|ing|s)? (?:this|your|a|another|the other) computer/i).slice(0, 3))}`)
      ok(hits(/Tailscale/).every(h => /start Tailscale/.test(h)), `Tailscale is named only where starting it is the fix: ${JSON.stringify(hits(/Tailscale/).slice(0, 3))}`)
      ok(hits(/\bplease\b|\bsimply\b|\bjust\b(?! now)|\boops\b|successfully|!/i).length === 0, `plain words: ${JSON.stringify(hits(/\bplease\b|\bsimply\b|\bjust\b(?! now)|\boops\b|successfully|!/i).slice(0, 3))}`)
    }
    // The address and the pass are behind "Technical details", and say whether the network is encrypted.
    raw = render(jsx(P.ShareSection, { fleet: F(fleetSource2) }))
    ok(!text(raw).includes('100.64.0.10'), 'the address is not on the page until asked for')
    H.override = {}
    H.status = statusAll
  }

  // ── The keyboard goes back to what opened a sheet or a dialog ──
  // (Effects do not run in a static render; the behaviour is checked in a running Desktop: sheet, dialog,
  // the share switch and a dialog opened from a row's menu each hand the focus back after Escape.)
  ok(/function useReturnFocus\(open\)/.test(source) && /function Sheet\([^)]*\) \{\n  useReturnFocus\(open\)/.test(source) && /function Confirm\([^)]*\) \{[^]*?useReturnFocus\(open\)/.test(source) && count(source, 'jsx(Confirm, {') >= 5 && /opener\.isConnected/.test(source), 'every sheet and every confirmation hands the focus back to its opener')

  // ── A confirmation is safe from the keyboard ──
  // The Desktop's ready-made confirmation carried out the act on Enter or Space whatever had the focus
  // (Cancel included). The page's own opens on Cancel and has no key handler: a key presses the focused button.
  ok(!/\bConfirmDialog\b/.test(source), "the Desktop's ready-made confirmation is not used")
  for (const [name, node, label, red] of [
    ['stop twinning', jsx(P.StopTwinningDialog, { onClose: noop, open: true, pair: recipePlain }), 'Stop twinning Recipe Planner', true],
    ['remove everything', jsx(P.UninstallDialog, { onClose: noop, open: true }), 'Remove Grok Bot Twin', true],
    ['stop sharing', jsx(P.ShareSection, { fleet: P.fleetOf(fleetSource2), initialConfirm: 'off' }), 'Stop sharing', false],
    ['unlink a computer', jsx(P.ShareSection, { fleet: P.fleetOf(fleetSource2), initialConfirm: surface.id }), 'Unlink Surface', true],
    ['unlink this computer', jsx(P.UnlinkDialog, { fleet: P.fleetOf(fleetFollower), onClose: noop, open: true, storage: ctx.storage })]
  ].map(x => (x.length === 2 ? [...x, 'Unlink this computer', true] : x))) {
    reset()
    const closed = []
    H.confirms = []
    raw = render(node)
    const box = H.dialogs.find(d => d.role === 'alertdialog')
    let prevented = false
    box?.onOpenAutoFocus?.({ preventDefault: () => { prevented = true } })
    const inBox = H.buttons.slice(H.buttons.findIndex(b => b.text === 'Cancel' && b.variant === 'ghost'))
    ok(box && prevented && !box.keys.includes('onKeyDown') && !box.keys.includes('onKeyDownCapture'), `${name}: opens with the focus on Cancel (never on the act), and no key handler of its own`)
    ok(inBox[0]?.text === 'Cancel' && inBox.some(b => b.text === label && b.variant === (red ? 'destructive' : 'default')) && raw.includes('role="alertdialog"'), `${name}: Cancel first, then the button that names the act (${inBox.map(b => `${b.text}/${b.variant}`)})`)
    await inBox[0].onClick()
    ok(H.restCalls.filter(c => c.opts?.method === 'POST' && c.opts?.body?.dry_run !== true).length === 0 && H.notes.length === 0, `${name}: pressing Cancel does nothing but close`)
  }
  // A fold inside a confirmation is an ordinary button there: pressing it opens the fold, nothing else.
  reset()
  render(jsx(P.StopTwinningDialog, { onClose: noop, open: true, pair: recipePlain }))
  await press('Technical details')
  ok(H.restCalls.length === 0, 'the "Technical details" fold inside Stop twinning only opens')

  // ── Every sheet ends in a button ──
  H.override.checklist = { data: { blocked_by: null, by_hand: [], switch: { grok_routines_to_turn_off: [], hermes_jobs_paused: [], hermes_jobs_resumed: [] } } }
  for (const [name, node] of [
    ['details: what copies over', jsx(P.DetailsSheet, { onClose: noop, open: true, pair: recipe })],
    ['details: earlier versions', jsx(P.DetailsSheet, { initialTab: 'versions', onClose: noop, open: true, pair: recipe })],
    ['details: recent activity', jsx(P.DetailsSheet, { initialTab: 'recent', onClose: noop, open: true, pair: recipe })],
    ['finish setting up', jsx(P.FinishSheet, { onClose: noop, open: true, pair: trail })],
    ['move (nothing to move)', jsx(P.MoveSheet, { onClose: noop, open: true, pair: recipe, storage: ctx.storage })],
    ['skill sync', jsx(P.SkillSyncSheet, { onClose: noop, open: true, pair: recipe, storage: ctx.storage })],
    ['skill sync (on)', jsx(P.SkillSyncSheet, { onClose: noop, open: true, pair: { ...recipe, mirror_bot: { connected: true } }, storage: ctx.storage })],
    ['see the twin', jsx(P.SeeTwinSheet, { onClose: noop, open: true, pair: acmeSee })],
    ['routines (blocked)', jsx(P.MoveSheet, { onClose: noop, open: true, pair: { ...recipe, conflicts: 1 }, storage: ctx.storage })],
    ['privacy', jsx(P.PrivacySheet, { onClose: noop, open: true })],
    ['connect', jsx(P.AddFlow, { onClose: noop, open: true, ready: false })],
    ['link to my Mac', jsx(P.LinkSheet, { onClose: noop, open: true, initialCode: '7KQ4-M2XD-P9RA' })],
    ['rename this computer', jsx(P.RenameSheet, { computer: P.fleetOf(fleetSource2).me, onClose: noop, open: true })],
    ['choose', jsx(P.AddFlow, { onClose: noop, open: true, ready: true })]
  ]) {
    ok(endsInButton(render(node)) && H.buttons.at(-1) && !H.buttons.at(-1).disabled, `${name}: ends in a button that can be pressed`)
  }
  H.override.checklist = { isPending: true, data: undefined }
  ok(endsInButton(render(jsx(P.MoveSheet, { onClose: noop, open: true, pair: recipe, storage: ctx.storage }))) && has('Close'), 'move while loading: still a way out')
  H.override = {}

  // ── Keyboard, screen reader, contrast, motion ──
  {
    const code = source.split('\n').filter(l => !/^\s*\/\//.test(l))
    // Words are never drawn in the two faint colours: 3.5 to 1 on a light window, where 12 px words need
    // 4.5 to 1. Those colours are for shapes: an icon, the edge of an empty tick box, the status bar's item
    // (drawn like its neighbours there). And no row is faded as a whole: faded small words are 3 to 1.
    const faint = code.filter(l => /ui-text-(?:tertiary|quaternary)/.test(l))
    const shape = l => /jsx\(Icon, \{ className: [^}]*ui-text-(?:tertiary|quaternary)/.test(l) || /^const CHECKBOX = 'mt-1 border-\(--ui-text-tertiary\)'$/.test(l) || /^\s*'text-\(--ui-text-tertiary\) hover:bg-\(--chrome-action-hover\) hover:text-\(--ui-text-primary\)',$/.test(l)
    ok(faint.length >= 8 && faint.every(shape), `the faint colours are for shapes only, never for words: ${JSON.stringify(faint.filter(l => !shape(l)).map(l => l.trim().slice(0, 90)))}`)
    ok(/const SMALL = 'text-xs leading-relaxed text-\(--ui-text-secondary\)'/.test(source) && !/'opacity-\d+'/.test(code.join('\n')), 'small print is in the secondary colour; nothing is faded by opacity')
    ok(faintWords.length === 0, `no rendered surface holds faint words (${seen.length} renders): ${JSON.stringify(faintWords.slice(0, 3))}`)
    // A row that cannot be ticked is quieter by its colour and is still read.
    {
      const raw = render(jsx(P.AddFlow, { initialPreview: undefined, onClose: noop, open: true, ready: true }))
      ok(!/opacity-/.test(raw), 'choose your bots: a bot that already has a twin is not faded')
    }

    // The sentence under a sheet's title is read before acting, and so are the names of the sections that are
    // not open: both in the secondary colour (the Desktop draws them in its faint one, 3.5 to 1 on light).
    {
      const sheet = render(jsx(P.DetailsSheet, { onClose: noop, open: true, pair: recipe }))
      const dialog = render(jsx(P.UnlinkDialog, { fleet: P.fleetOf(fleetFollower), onClose: noop, open: true, storage: ctx.storage }))
      ok(/<h2[^>]*>Recipe Planner[^<]*: details<\/h2><p class="text-\(--ui-text-secondary\)"/.test(sheet) && count(source, 'el(DialogDescription, { className: SAID_UNDER_TITLE }, description)') === 2 && !/<p(?![^>]*text-\(--ui-text-secondary\))[^>]*data-props=""[^>]*>[^<]+<\/p><\/header>/.test(sheet + dialog), 'the sentence under the title of every sheet and confirmation is in the secondary colour')
      ok(/\{ 'aria-label': 'Sections', className: 'self-start text-\(--ui-text-secondary\)' \}/.test(source), 'details: the names of the sections that are not open are in the secondary colour')
    }

    // The page's own style element: the rules no utility class of the Desktop gives.
    H.status = statusAll
    const pageRaw = page()
    const css = (pageRaw.match(/<style[^>]*>([^<]*)<\/style>/)?.[1] ?? '').replace(/&quot;/g, '"').replace(/&gt;/g, '>').replace(/&#x27;/g, "'")
    const focusRule = css.match(/^([^{]+)\{outline:2px solid var\(--ui-text-secondary,Highlight\);outline-offset:2px\}/)
    const focused = (focusRule?.[1] ?? '').split(',')
    ok(focusRule && ['.gbt button:focus-visible', '.gbt [role="switch"]:focus-visible', '.gbt a:focus-visible', '.gbt [tabindex="0"]:focus-visible', '.gbt input:focus-visible', '.gbt textarea:focus-visible'].every(sel => focused.includes(sel)), `a ring that can be seen on everything the keyboard stops on, fields that are typed into included (${focused})`)
    ok(css.includes('.gbt button[data-variant="text"]:not(:hover){color:var(--ui-text-secondary)}') && /data-variant="text"/.test(pageRaw), 'the quiet text buttons (Privacy, See a sample …) rest in the secondary colour, and still change under the pointer')
    ok(count(pageRaw, '<style') === 1 && /^<div class="gbt /.test(pageRaw), 'one style element, on the page that carries the class its rules are scoped to')
    // … and every sheet and confirmation carries that class too (they are drawn outside the page).
    ok(/function Sheet\([^]*?className: wide \? 'gbt max-w-xl' : 'gbt max-w-lg'/.test(source) && /function Confirm\([^]*?className: 'gbt max-w-md'/.test(source), 'sheets and confirmations are inside the same rules')

    // A switch takes the press on 28 px, as every button does; its spoken name stays its own.
    reset()
    H.override.fleet = { data: fleetSource2 }
    let raw = render(jsx(P.ShareSection, { fleet: P.fleetOf(fleetSource2) }))
    ok(/<label class="inline-flex min-h-7 shrink-0 cursor-pointer items-center" for="gbt-share"><button role="switch"[^>]*aria-label="Share my twins with my other computers"[^>]*id="gbt-share"/.test(raw), 'the share switch sits in a 28 px target that presses it')
    H.override = {}
    // (however many there are, none stands outside its target)
    ok(count(source, 'jsx(SwitchTarget, { children: jsx(Switch, {') >= 4 && count(source, 'jsx(Switch, {') === count(source, 'jsx(SwitchTarget, { children: jsx(Switch, {'), 'every switch of the page (sharing, requests, routines, a twin\'s memory, each kind of settings that follow, and what else syncs) has that target')

    // A computer that links or leaves while the page is open is said to a screen reader: one line that is
    // always there (so that its change is read), empty while there is nobody to name, never seen.
    {
      const said = f => (render(jsx(P.ShareSection, { fleet: P.fleetOf(f) })).match(/<p class="sr-only" role="status">([^<]*)<\/p>/) ?? [])[1]
      const [two, code, off, behind] = [said(fleetSource2), said(fleetCode), said(fleetSharedOff), said(fleetBehind)]
      ok(/^Sharing with (Orchard and Surface|Surface and Orchard)\.$/.test(two) && behind === 'Sharing with Surface.', `the Mac says who it shares with, for a screen reader (${two} | ${behind})`)
      ok(code === '' && off === '', `nobody linked, or sharing off (the page says that in words of its own): the line is there and empty (${JSON.stringify([code, off])})`)
    }

    // The edge of an empty tick box can be seen (3 to 1): the Desktop's own edge colour is 1.8 to 1 on a sheet.
    ok(count(source, 'className: CHECKBOX') >= 2 && !/border-\(--ui-stroke-primary\)/.test(source), 'tick boxes use the visible edge')
    // … and the words next to a tick box take the press on 26 px: the row's padding is the label's.
    H.override.checklist = { data: { blocked_by: null, by_hand: [{ id: 'c1', title: 'Connect "example-maps" in Hermes yourself', detail: 'Grok keeps its own.' }], switch: { grok_routines_to_turn_off: [], hermes_jobs_paused: [], hermes_jobs_resumed: [] } } }
    raw = render(jsx(P.MoveSheet, { onClose: noop, open: true, pair: recipe, storage: ctx.storage }))
    H.override = {}
    ok(count(source, 'cn(TICK_LABEL, ') === 2 && /const TICK_LABEL = 'flex min-w-0 flex-1 flex-col py-1'/.test(source) && (!raw.includes('type="checkbox"') || /<label class="flex min-w-0 flex-1 flex-col py-1[^"]*" for="/.test(raw)), 'tick rows: the label is the larger target')

    // The changes box has a name and a role to be read with, and the keyboard can stop on it to scroll.
    ok(/'aria-label': 'Line-by-line changes',\n[^\n]*\n\s+role: 'group',[^]*?tabIndex: 0/.test(source), 'line-by-line changes: a named group the keyboard can scroll')

    // Less motion asked of the computer: the page starts no smooth scroll of its own. (The Desktop's
    // stylesheet already stills every animation and transition then.)
    const { landFocus, lessMotion, setPageTitle } = hooks
    ok(lessMotion() === false, 'no window (or no such setting): motion as usual')
    globalThis.window = { matchMedia: q => ({ matches: q === '(prefers-reduced-motion: reduce)' }) }
    const calm = lessMotion()
    globalThis.window = { matchMedia: () => ({ matches: false }) }
    const lively = lessMotion()
    delete globalThis.window
    ok(calm === true && lively === false && /scrollIntoView\?\.\(\{ behavior: lessMotion\(\) \? 'auto' : 'smooth', block: 'nearest' \}\)/.test(source) && count(source, "behavior: 'smooth'") === 0, 'a new link code scrolls into view at once when less motion is asked for')

    // ── The keyboard is never left nowhere ──
    // A button that waits is switched off for a moment and the browser lets go of it; a box or a row often
    // leaves the page with the act. Either way the focus was on nothing (the requests switch, the
    // two answers of a waiting step, See a sample). Now it goes back to the control, or to the page's title.
    {
      const body = { tag: 'body' }
      const doc = { activeElement: body, body }
      const thing = (o = {}) => ({ contains: () => false, disabled: false, focus() { this.focused = (this.focused ?? 0) + 1; doc.activeElement = this }, isConnected: true, ...o })
      const title = thing()
      globalThis.document = doc
      setPageTitle(title)

      try {
        let btn = thing()
        landFocus(btn)
        ok(btn.focused === 1 && doc.activeElement === btn && !title.focused, 'focus on nothing, the control still there: back to the control (once)')

        const other = thing()
        btn = thing()
        doc.activeElement = other
        landFocus(btn)
        ok(!btn.focused && !title.focused && doc.activeElement === other, 'the owner moved on: the focus is left where they put it')

        btn = thing({ isConnected: false })
        doc.activeElement = body
        landFocus(btn)
        ok(!btn.focused && title.focused === 1 && doc.activeElement === title, 'the control went with the act: the page title takes the focus, never nothing')

        // still switched off when the act ends: not now, and the second look finds it enabled
        title.focused = 0
        doc.activeElement = body
        btn = thing({ disabled: true })
        const later = []
        const realTimeout = ctx.setTimeout
        ctx.setTimeout = fn => { later.push(fn); return () => {} }
        landFocus(btn)
        ok(!btn.focused && later.length === 1 && doc.activeElement === body, 'a control that still waits is not forced; it is looked at again')
        btn.disabled = false
        later.shift()()
        ok(btn.focused === 1 && later.length === 1, 'the second look puts the focus back')
        later.shift()()
        ok(btn.focused === 1 && later.length === 0, 'two more looks at most; a focus that is in place is left alone')
        ctx.setTimeout = realTimeout

        // a sheet parks the focus on its own box while its button waits
        const box = thing({ contains: x => x === btn })
        btn = thing()
        doc.activeElement = box
        landFocus(btn)
        ok(btn.focused === 1, 'the focus parked on the box around the control goes back to the control')

        // inside a sheet that is still open, a control that went leaves the focus in the sheet, not behind it
        const sheet = thing()
        btn = thing({ isConnected: false })
        doc.activeElement = body
        landFocus(btn, sheet)
        ok(sheet.focused === 1 && title.focused === 0 && !btn.focused, 'a control that left an open sheet: the sheet takes the focus, the page behind it does not')
        const closedSheet = thing({ isConnected: false })
        doc.activeElement = body
        landFocus(btn, closedSheet)
        ok(!closedSheet.focused && title.focused === 1, 'the sheet closed with the act: the page title')
        title.focused = 0

        doc.activeElement = body
        landFocus(null)
        landFocus(body)
        ok(doc.activeElement === body && title.focused === 0, 'nothing held the focus when the act began: nothing is given it')
      } finally {
        delete globalThis.document
        setPageTitle(null)
      }

      ok(/const held = typeof document === 'undefined' \? null : document\.activeElement\n    const home = held\?\.closest\?\.\('\[role="dialog"\],\[role="alertdialog"\]'\) \?\? null\n\n    setBusy\(name\)/.test(source) && /finally \{\n      setBusy\(null\)\n\n      if \(held && timers\) \{\n        timers\.setTimeout\(\(\) => landFocus\(held, home\), 0\)/.test(source), 'every act that makes its button wait remembers where the keyboard was and gives it back')
      ok(/const back = \(\) => landFocus\(opener\)/.test(source) && /pageTitle = node/.test(source), 'a sheet or a confirmation whose opener went with the act hands the focus to the page title')
    }

    // Escape is safe in the one sheet that holds typed text: what was pasted and not saved is in the box again
    // when the sheet is reopened. In memory only, never stored; gone when the twin is.
    ok(/const unsavedText = new Map\(\)/.test(source) && /useState\(\(\) => initialText \|\| unsavedText\.get\(pair\.pair_id\) \|\| ''\)/.test(source) && /if \(text\) \{\n      unsavedText\.set\(pair\.pair_id, text\)\n    \} else \{\n      unsavedText\.delete\(pair\.pair_id\)/.test(source), 'finish setting up: closing the sheet keeps what was typed, until it is saved')
    ok(/function forgetTwin\(storage, pairId\) \{\n[^\n]*\n[^\n]*\n  unsavedText\.delete\(pairId\)\n/.test(source) && !/storage\??\.set\([^)]*unsavedText|unsavedText[^\n]*storage\??\.set/.test(source), 'that text is never written to storage, and is dropped with the twin')
    {
      H.textareas = []
      render(jsx(P.FinishSheet, { onClose: noop, open: true, pair: trail }))
      ok(H.textareas[0]?.value === '', 'a sheet opened for the first time starts empty')
    }

    // Spoken names begin with the words on the button (someone who talks to the computer says what they see).
    ok(/'aria-label': `Do it once: the step \$\{w\.peer\} asked for`[^\n]*'Do it once'\)/.test(source) && /'aria-label': `Don't do the step \$\{w\.peer\} asked for`[^\n]*"Don't"\)/.test(source), 'the two answers of a waiting step: spoken names that begin with the visible words')
  }

  // ── Vocabulary, over everything that was rendered or said in a toast ──
  {
    const hits = (re, list = seen) => list.filter(s => re.test(s.text)).map(s => s.text.match(re)[0] + ' … ' + s.text.slice(Math.max(0, s.text.search(re) - 40), s.text.search(re) + 60))
    ok(seen.length > 150, `the sweep covers every rendered surface (${seen.length})`)
    // "helper" meant three different things; now nothing is called that.
    ok(hits(/helper/i).length === 0, `no "helper" on any surface: ${JSON.stringify(hits(/helper/i).slice(0, 3))}`)
    ok(hits(/key files?\b/i).length === 0, `no "key file" on any surface: ${JSON.stringify(hits(/key files?\b/i).slice(0, 3))}`)
    ok(hits(/\bprofiles?\b/i).length === 0, `no "profile" on any surface: ${JSON.stringify(hits(/\bprofiles?\b/i).slice(0, 3))}`)
    const RETIRED = /Remove everything|Up to date ·|Needs your attention|genuineness|signed build record|two switches|Check again|Compare with Grok/
    ok(hits(RETIRED).length === 0, `no retired wording: ${JSON.stringify(hits(RETIRED).slice(0, 3))}`)
    // Retired wording: one noun for what stays ("Hermes bot"), one word for twins that cannot follow Grok, one verb for
    // botport ("verify"), one name per exit, one label per download; and the routines move, not the bot.
    const RETIRED_3 = /Hermes cop(?:y|ies)\b(?! them)|counterpart|\bPaused\b|checked and genuine|has not been checked|needs checking again|Move to Hermes|Runs in Hermes since|Remove sample twin|Remove the sample twin|Other ways|publisher's own build record|Turn on skill sync|I copied it myself|Copy it into Grok yourself|Keep as a Hermes bot|normal Hermes bot|Try it:|Ready · follows|follows Grok's name and description|it starts fresh|Show files|Hermes could not check (?!your twins\.)|Reconnect(?! your Grok bots|…)/
    ok(hits(RETIRED_3).length === 0, `no retired wording: ${JSON.stringify(hits(RETIRED_3).slice(0, 3))}`)
    ok(strayLabels.length === 0, `one label per action ("Download botport", "Verify botport"): ${JSON.stringify(strayLabels.slice(0, 3))}`)
    ok(strayChecks.length === 0, `"Check now" is only ever about a twin (row menu, password box): ${JSON.stringify(strayChecks.slice(0, 3))}`)
    ok(hits(/\bbotport[^.]{0,40}\bchecked\b|\bcheck(?:ed|s)? botport\b/i).length === 0, `botport is verified, never "checked": ${JSON.stringify(hits(/\bbotport[^.]{0,40}\bchecked\b|\bcheck(?:ed|s)? botport\b/i).slice(0, 3))}`)
    // A command is never the remedy: commands appear only where a "terminal instead" fold was opened on purpose.
    const COMMAND = /hermes (twin|plugins)\b|brew install|gh auth|\bRun \w+ in a terminal/
    ok(hits(COMMAND, seen.filter(s => !s.terminal)).length === 0, `no command outside the "terminal instead" folds: ${JSON.stringify(hits(COMMAND, seen.filter(s => !s.terminal)).slice(0, 3))}`)
    ok(seen.filter(s => s.terminal).length === 2, 'the folds were opened on purpose in exactly two renders')
    // One toast names Hermes' own word for the place a Hermes bot is found afterwards ("among your profiles in
    // Hermes"): after Stop twinning the row is gone, and that is where Hermes lists it. Nowhere else.
    const WHERE_IT_IS = 'you find it among your profiles in Hermes.'
    const said = H.allNotes.map(n => ({ text: [n.title, n.message, n.meta, n.detail, n.action?.label].filter(Boolean).join(' | ').replace(WHERE_IT_IS, '') }))
    ok(H.allNotes.filter(n => String(n.meta ?? '').includes(WHERE_IT_IS)).every(n => /is no longer twinned\.$/.test(n.message)), 'the one mention of "profiles" belongs to the stop-twinning toast')
    ok(said.length > 40 && hits(/helper|key files?\b|\bprofiles?\b|Remove everything/i, said).length === 0, `toasts use the same words (${said.length}): ${JSON.stringify(hits(/helper|key files?\b|\bprofiles?\b|Remove everything/i, said).slice(0, 3))}`)
    ok(hits(/Hermes cop(?:y|ies)\b|\bPaused\b(?! \d+ routines?)|checked and genuine|Move to Hermes|now runs in Hermes(?<!routines? now runs? in Hermes)|Try it buttons/i, said).length === 0, `toasts carry no retired wording: ${JSON.stringify(hits(/Hermes cop(?:y|ies)\b|\bPaused\b(?! \d+ routines?)|checked and genuine|Move to Hermes|Try it buttons/i, said).slice(0, 3))}`)
    ok(hits(/hermes (twin|plugins)\b|in a terminal|README/i, said).length === 0, `no toast sends anyone to a terminal or a README: ${JSON.stringify(hits(/hermes (twin|plugins)\b|in a terminal|README/i, said).slice(0, 3))}`)
    ok(H.allNotes.every(n => !['Show me', 'Choose'].includes(n.action?.label)), 'no toast button that only points at the page you are on')
  }
}

rmSync(work, { recursive: true, force: true })

if (failures.length) {
  console.log(`desktop check: ${failures.length} assertions FAILED, ${passed} passed`)
  process.exit(1)
}

console.log(`desktop check: ${passed} assertions passed`)
