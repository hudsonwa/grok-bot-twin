// Requests between the owner's computers ("on Surface: …"): the Twins page's part of it, outside Hermes Desktop.
//
//   node tests/desktop/requests.mjs [--modules DIR]
//
// DIR is a node_modules folder holding react + react-dom (19.x). A temp copy of desktop/plugin.js gets its three
// bare imports rewritten to local shims (a small stub of @hermes/plugin-sdk and the real React), as check.mjs
// does; this file is separate so that it only knows the requests section. Invented data only.

import assert from 'node:assert/strict'
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const source = readFileSync(resolve(here, '../../desktop/plugin.js'), 'utf8')
const argModules = process.argv.indexOf('--modules')
const modulesDir = argModules > 0 ? resolve(process.argv[argModules + 1]) : resolve(here, '../../node_modules')

let passed = 0
const ok = (cond, msg) => {
  assert.ok(cond, msg)
  passed += 1
}

if (!existsSync(join(modulesDir, 'react/package.json')) || !existsSync(join(modulesDir, 'react-dom/package.json'))) {
  console.log('requests check: skipped (no react / react-dom in the modules folder)')
  process.exit(0)
}

// ── 1. shims ────────────────────────────────────────────────────────────────

const work = mkdtempSync(join(tmpdir(), 'twin-requests-'))
const req = createRequire(join(modulesDir, 'x.js'))

function shim(file, mod) {
  const keys = Object.keys(req(mod)).filter(k => /^[A-Za-z_$][\w$]*$/.test(k) && k !== 'default')

  writeFileSync(join(work, file), `import { createRequire } from 'node:module'\nconst m = createRequire(${JSON.stringify(join(modulesDir, 'x.js'))})(${JSON.stringify(mod)})\nexport default m\nexport const { ${keys.join(', ')} } = m\n`)
}

shim('react.mjs', 'react')
shim('jsx.mjs', 'react/jsx-runtime')

const sdkNames = source
  .match(/import\s*\{([^}]+)\}\s*from\s*'@hermes\/plugin-sdk'/)[1]
  .split(',')
  .map(s => s.trim())
  .filter(Boolean)
const special = {
  Button: `({ children, disabled, loading, onClick, ...p }) => { H.buttons.push({ text: H.text(children), label: p['aria-label'], onClick, disabled: Boolean(disabled || loading), variant: p.variant ?? 'default' }); return jsx('button', { disabled: Boolean(disabled || loading), 'aria-label': p['aria-label'], children }) }`,
  cn: `(...a) => a.flat(Infinity).filter(Boolean).join(' ')`,
  Dialog: `({ open, children }) => (open ? jsx('div', { role: 'dialog', children }) : null)`,
  DialogContent: `({ children, className, role }) => jsx('div', { className, role, children })`,
  host: `H.host`,
  queryClient: `H.queryClient`,
  relativeTime: `ms => { const m = Math.round((Date.now() - ms) / 60000); return m < 1 ? 'just now' : m + ' min ago' }`,
  Switch: `p => { H.switches.push({ label: p['aria-label'], checked: Boolean(p.checked), disabled: Boolean(p.disabled), id: p.id, onCheckedChange: p.onCheckedChange }); return jsx('button', { role: 'switch', 'aria-checked': Boolean(p.checked), 'aria-label': p['aria-label'], id: p.id }) }`,
  useQuery: `opts => H.useQuery(opts)`,
  useQueryClient: `() => H.queryClient`,
  PALETTE_AREA: `'palette'`,
  ROUTES_AREA: `'routes'`,
  SIDEBAR_NAV_AREA: `'sidebar.nav'`,
  STATUSBAR_AREAS: `{ left: 'statusBar.left', right: 'statusBar.right' }`
}
const pass = `({ children, className }) => jsx('div', { className, children })`

writeFileSync(
  join(work, 'sdk.mjs'),
  `import { jsx } from './jsx.mjs'\nconst H = globalThis.__h\n` + sdkNames.map(n => `export const ${n} = ${special[n] ?? pass}`).join('\n') + '\n'
)
writeFileSync(
  join(work, 'plugin.mjs'),
  source
    .replace(/from\s*'@hermes\/plugin-sdk'/, `from './sdk.mjs'`)
    .replace(/from\s*'react\/jsx-runtime'/, `from './jsx.mjs'`)
    .replace(/from\s*'react'/, `from './react.mjs'`)
    // Harness-only (as in check.mjs): every open confirmation, and every choice of the approval setting, is
    // recorded with its props (its words, and what choosing or confirming does).
    .replace('function Confirm({ open,', 'function Confirm(__p) { if (__p.open) globalThis.__h.confirms.push(__p); return __Confirm(__p) }\nfunction __Confirm({ open,')
    .replace('function ModeChoice({ choice,', 'function ModeChoice(__p) { globalThis.__h.modes.push(__p); return __ModeChoice(__p) }\nfunction __ModeChoice({ choice,')
    .concat('\nexport const __requests = { RequestsSection, RequestsWaiting, tasksOf, requestTitle, toastTasks, handleEvent, setPageOpen: n => { pageOpen = n } }\n')
)

const H = (globalThis.__h = {
  buttons: [],
  switches: [],
  confirms: [],
  modes: [],
  raw: '',
  notes: [],
  restCalls: [],
  restReply: async () => ({}),
  setData: [],
  tasks: undefined,
  queries: [],
  text: c => (Array.isArray(c) ? c.map(H.text).join('') : typeof c === 'string' || typeof c === 'number' ? String(c) : c?.props?.children ? H.text(c.props.children) : ''),
  host: { notify: n => H.notes.push(n), navigate: () => {}, state: { profile: { get: () => 'default' }, gateway: { get: () => 'open' } } },
  queryClient: { getQueryData: () => undefined, setQueryData: (key, value) => H.setData.push([key[1], value]), invalidateQueries: async () => { H.invalidated = (H.invalidated ?? 0) + 1 } },
  useQuery: opts => {
    H.queries.push(opts)

    return { data: opts.queryKey[1] === 'tasks' ? H.tasks : undefined, error: null, isError: false, isPending: false, refetch: async () => {} }
  }
})

const mod = await import(pathToFileURL(join(work, 'plugin.mjs')).href)
const P = mod.__requests
const { renderToStaticMarkup } = req('react-dom/server')
const { jsx } = req('react/jsx-runtime')

mod.default.register({
  onDispose: () => {},
  onEvent: () => {},
  registerMany: () => {},
  rest: async (path, opts) => {
    H.restCalls.push({ path, opts })

    return H.restReply(path, opts)
  },
  setInterval: () => () => {},
  setTimeout: () => () => {},
  storage: { get: (k, f) => f, remove: () => {}, set: () => {} }
})

const tick = async (n = 3) => {
  for (let i = 0; i < n; i++) {
    await new Promise(r => setImmediate(r))
  }
}
const text = html => html.replace(/<[^>]+>/g, ' ').replace(/&#x27;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, '&').replace(/&gt;/g, '>').replace(/&lt;/g, '<').replace(/\s+/g, ' ').trim()
const renderPart = (part, data, props = {}) => {
  H.buttons = []
  H.switches = []
  H.confirms = []
  H.modes = []
  H.queries = []
  H.tasks = data
  H.raw = renderToStaticMarkup(jsx(part, props))

  return text(H.raw)
}
const render = (data, props) => renderPart(P.RequestsSection, data, props) // the switch and the list (lower on the page)
const renderWaiting = (data, props) => renderPart(P.RequestsWaiting, data, props) // what waits for the owner (first on the page)
const words = s => s.trim().split(/\s+/).filter(w => /[A-Za-z0-9]/.test(w)).length
const sentences = s => s.match(/[^.?!]+[.?!]/g) ?? []

// ── 2. fixtures (invented) ──────────────────────────────────────────────────

const iso = minsAgo => new Date(Date.now() - minsAgo * 60_000).toISOString()
const LABEL = 'Let my other computers ask this one to do things'
const base = { limits: { per_hour: 40, per_minute: 6, reply_chars: 6000, request_chars: 2000 }, linked: true, log: [], machines: [{ id: 'm-3f9a1c0b7d2e', name: 'mini' }], switch: { label: LABEL, network: 'tailscale', on: false, since: null }, this_machine: { name: 'Surface' }, waiting: [], working: false }
const row = (over = {}) => ({ approval: null, asked_at: iso(3), code: null, dir: 'in', ended_at: iso(3), id: 'k-0123456789abcdef', label: 'done here', peer: 'mini', peer_to: null, reply: 'Windows 11', seconds: 6, state: 'done', text: 'what operating system do you run?', twin: 'Test Scout', ...over })
const waiting = { asked_at: iso(0), command: 'rm -f ./does-not-exist-7c1e/old-note.txt', description: 'delete a file', id: 'k-00000000000000aa', peer: 'mini', q: '0a1b2c3d', text: 'tidy up the scratch folder', twin: 'Test Scout' }

// ── 3. the switch ───────────────────────────────────────────────────────────

let html = render(base)
ok(H.switches.length === 1 && H.switches[0].label === LABEL && H.switches[0].checked === false && H.switches[0].id === 'gbt-tasks', 'off by default: one switch, its label, not checked')
// Beside the switch it says "Off."; the label is the sentence that says what the switch allows,
// and the fold's first line says who asks whom and that approvals stay with the owner. (A Mac with computers
// linked and a fresh link code ran to about 140 words with the longer line.)
ok(html.includes(LABEL) && html.includes('Off.') && !html.includes('When on,') && words(LABEL) <= 12, `off: the label says what the switch allows; beside it, one word (${html})`)
// (one fold says how to ask from this computer; nothing else is on the card)
ok(JSON.stringify(H.buttons.map(b => b.text)) === JSON.stringify(['How to ask from here']) && words(html) <= 16 && sentences(html.replace(LABEL, '')).every(s => words(s) <= 22), `off: the switch, its sentence and one fold (${words(html)} words)`)
{
  const how = render(base, { initialHow: true })
  ok(how.includes('In any Hermes chat on this computer, type this. mini needs its own switch on.') && how.includes('/twin ask mini: what system are you on?') && how.includes('The answer comes back into that chat, with the name of the computer that did it.') && sentences(how.replace(LABEL, '')).every(s => words(s) <= 22), `how to ask: the line to type, with the other computer's own name, and where the answer appears (${how})`)
  ok(!/\b(token|relay|mailbox|stdio|id)\b/i.test(how), 'how to ask: none of the words an owner need not know')
  // The example names a computer that takes requests when one is known (the backend
  // says so, or it answered a request from here before); only when none is known does it speak of the switch.
  const two = { ...base, machines: [{ id: 'm-000000000001', name: 'Orchard' }, { id: 'm-000000000002', name: 'Studio', takes_requests: true }] }
  const told = render(two, { initialHow: true })
  ok(told.includes('/twin ask Studio: what system are you on?') && told.includes('In any Hermes chat on this computer, type this.') && !told.includes('needs its own switch on'), `how to ask: the example names the computer that takes requests (${told})`)
  const seen = render({ ...base, log: [row({ dir: 'out', label: 'done on Studio - 2 s', peer: 'Studio', state: 'done' })], machines: [{ id: 'm-000000000001', name: 'Orchard' }, { id: 'm-000000000002', name: 'Studio' }] }, { initialHow: true })
  ok(seen.includes('/twin ask Studio: what system are you on?') && !seen.includes('needs its own switch on'), 'how to ask: a computer that answered before is the example')
  ok(render({ ...base, machines: [{ id: 'm-000000000001', name: 'Orchard' }, { id: 'm-000000000002', name: 'Studio' }] }, { initialHow: true }).includes('Orchard needs its own switch on.'), 'how to ask: nothing known about the others, so the first one and the hint')
}
ok(render({ ...base, linked: false }) === '' && render({ ...base, machines: [] }) === '' && render(undefined) === '' && render({ error: { code: 'internal' } }) === '', 'a computer that is not linked, or an older backend: no switch that could not work')

// one click switches it on: one call, the answer is shown at once, no toast
H.restCalls = []
H.setData = []
H.notes = []
const onView = { ...base, switch: { label: LABEL, network: 'tailscale', on: true, since: iso(0) }, working: true }
H.restReply = async () => onView
render(base)
await H.switches[0].onCheckedChange(true)
await tick()
ok(H.restCalls.length === 1 && H.restCalls[0].path === '/tasks/switch' && H.restCalls[0].opts.method === 'POST' && H.restCalls[0].opts.body.on === true, `switching on: one call (${JSON.stringify(H.restCalls)})`)
ok(H.setData.some(([k, v]) => k === 'tasks' && v === onView) && H.notes.length === 0, 'switching on: the new state is shown at once, without a toast')

// Linked pages ran to 135 to 167 words. Now, beside the switch it says "On."; what it allows and how
// approvals work is the first line of the fold.
html = render(onView)
ok(H.switches[0].checked === true && html.includes('On.') && !html.includes('Hermes asks for your approval') && words(html) <= 16, `on: one word beside the switch (${html})`)
html = render(onView, { initialHow: true })
ok(html.includes('Your twins on your other computers can ask their copies here to do things. Hermes asks for your approval here, by the same rules as in your own chats.') && html.indexOf('Hermes asks for your approval') < html.indexOf('In any Hermes chat'), `on: the fold says what it allows and what stays yours, first (${html})`)
ok(render(base, { initialHow: true }).includes('When on, your twins on your other computers can ask their copies here to do things. Hermes asks for your approval here, by the same rules as in your own chats.'), 'off: the fold says what switching it on allows, and what stays yours')
html = render(onView)
ok(sentences(html.replace(LABEL, '')).every(s => words(s) <= 22), 'on: no sentence over 22 words')

// linked over a plain home network: one more sentence, only while it is on
ok(!render(onView).includes('home network') && !render({ ...base, switch: { ...base.switch, network: 'lan' } }).includes('home network'), 'no word about the network where there is nothing to say')
html = render({ ...onView, switch: { ...onView.switch, network: 'lan' } })
ok(html.includes('Your computers are linked over your home network: what is asked and what comes back can be read by others on it.') && sentences(html.replace(LABEL, '')).every(s => words(s) <= 22), `on a home network: said in one sentence (${html})`)
render(onView)

// one click switches it off again
H.restCalls = []
H.restReply = async () => base
await H.switches[0].onCheckedChange(false)
await tick()
ok(H.restCalls.length === 1 && H.restCalls[0].path === '/tasks/switch' && H.restCalls[0].opts.body.on === false, 'switching off: one call, nothing to confirm')

// a refusal by the backend is said in its own plain words, as a toast, and the switch stays as it was
H.notes = []
H.restReply = async () => ({ error: { code: 'internal', hint: 'Try again.', message: 'Something went wrong inside Grok Bot Twin.' } })
render(base)
await H.switches[0].onCheckedChange(true)
await tick()
ok(H.notes.length === 1 && H.notes[0].kind === 'error' && H.notes[0].title === 'Could not switch this on' && H.notes[0].message === 'Something went wrong inside Grok Bot Twin.', `a failure: what was tried, what happened (${JSON.stringify(H.notes)})`)

// ── 4. a step that waits for the owner's approval on this computer ─────────

const waitView = { ...onView, log: [row({ approval: waiting, id: waiting.id, label: 'waiting for your approval here', state: 'waiting_approval', text: waiting.text })], waiting: [waiting] }

ok(renderWaiting(onView) === '' && renderWaiting(base) === '' && renderWaiting(undefined) === '' && renderWaiting({ ...waitView, linked: false }) === '', 'nothing waits: nothing is shown at the top of the page')
ok(!render(waitView).includes('does-not-exist-7c1e') && H.buttons.every(b => b.text !== 'Do it once'), 'the waiting step is shown once, at the top, not again next to the switch')
html = renderWaiting(waitView)
ok(html.includes('mini asked Test Scout here: tidy up the scratch folder') && html.includes('One step needs your approval: delete a file.') && html.includes('rm -f ./does-not-exist-7c1e/old-note.txt'), `waiting: who asked, what, and the step as Hermes describes it (${html})`)
ok(JSON.stringify(H.buttons.map(b => [b.text, b.variant])) === JSON.stringify([['Do it once', 'secondary'], ["Don't", 'text']]), `waiting: two answers, neither a filled button (${JSON.stringify(H.buttons.map(b => [b.text, b.variant]))})`)
ok(H.buttons.every(b => b.variant !== 'default') && html.includes('Nothing happens until then.'), 'waiting, shown by itself: nothing filled, and it says that nothing happens meanwhile')
// On the page a waiting step is the first thing and the one thing to press, so the page
// gives its one main button to "Do it once" of the first step. "Don't" stays quiet beside it; nothing is pressed
// for the owner, and no key answers by itself.
renderWaiting({ ...waitView, waiting: [waiting, { ...waiting, id: 'k-00000000000000bb' }] }, { lead: true })
ok(JSON.stringify(H.buttons.map(b => [b.text, b.variant])) === JSON.stringify([['Do it once', 'default'], ["Don't", 'text'], ['Do it once', 'secondary'], ["Don't", 'text']]), `waiting, leading the page: one main button, on the first step (${JSON.stringify(H.buttons.map(b => [b.text, b.variant]))})`)
html = renderWaiting(waitView)
ok(!/always|for this session|every time|remember/i.test(html), 'waiting: no standing permission is offered')
ok(H.queries[0].refetchInterval({ state: { data: waitView } }) === 2000 && H.queries[0].refetchInterval({ state: { data: onView } }) === 10000, 'the section looks again every 2 s while a request is open, every 10 s otherwise')

for (const [label, choice, said] of [['Do it once', 'once', 'Approved. That one step goes ahead.'], ["Don't", 'deny', 'Refused. That step is not done.']]) {
  H.restCalls = []
  H.notes = []
  H.restReply = async () => onView
  renderWaiting(waitView)
  await H.buttons.find(b => b.text === label).onClick()
  await tick()
  ok(H.restCalls.length === 1 && H.restCalls[0].path === `/tasks/${waiting.id}/approve` && JSON.stringify(H.restCalls[0].opts.body) === JSON.stringify({ choice, q: '0a1b2c3d' }), `${label}: one call with "${choice}" and the mark of the step that was shown (${JSON.stringify(H.restCalls)})`)
  ok(H.notes.length === 1 && H.notes[0].message === said, `${label}: says what happens now (${JSON.stringify(H.notes)})`)
}

// the step changed under the owner's finger: the answer is refused, the page reads the open step again and says so
H.restCalls = []
H.notes = []
H.invalidated = 0
H.restReply = async () => ({ error: { code: 'step_changed', hint: 'Look at it again, then answer.', message: 'The step that waits for you is not the one you answered.' } })
renderWaiting(waitView)
await H.buttons.find(b => b.text === 'Do it once').onClick()
await tick()
ok(H.invalidated === 1 && H.notes.length === 1 && H.notes[0].kind === 'error' && H.notes[0].message === 'The step that waits for you is not the one you answered.' && H.notes[0].meta === 'Look at it again, then answer.', `another step waits by now: read again, and say why nothing was done (${JSON.stringify(H.notes)})`)
// an older backend without marks: the answer goes without one
H.restCalls = []
H.restReply = async () => onView
renderWaiting({ ...waitView, waiting: [{ ...waiting, q: undefined }] })
await H.buttons.find(b => b.text === 'Do it once').onClick()
await tick()
ok(JSON.stringify(H.restCalls[0].opts.body) === JSON.stringify({ choice: 'once' }), 'no mark from the backend: none is made up')

// what is shown is text: markup in a request, a name or a command stays text, and an id of another shape is dropped
html = renderWaiting({ ...onView, waiting: [{ ...waiting, command: '<img src=x onerror=1> && rm', peer: '<b>mini</b>', text: '<script>alert(1)</script>' }, { ...waiting, id: '../../x' }, 'junk', null] })
ok(html.includes('<img src=x onerror=1> && rm') && html.includes('<script>alert(1)</script>') && H.buttons.filter(b => b.text === 'Do it once').length === 1, 'waiting: untrusted text is shown as text, a malformed entry is not shown at all')

// ── 4b. what happens here when a step needs approval: three choices ────────

const WARNING = 'Anyone who can use one of your linked computers can then approve a step that runs on this one.'
const MODE = {
  choices: [
    { label: 'Ask on this computer', note: 'A step waits until you answer it here.', value: 'here' },
    { label: 'Let my own linked computers approve', note: 'The computer that asked shows the step and can answer it too.', value: 'linked' },
    { label: 'Only safe requests', note: 'A step that would need approval is refused at once. Requests that only read still work.', value: 'safe' }
  ],
  label: 'When a step needs approval',
  value: 'here',
  warning: WARNING
}
const modeView = { ...onView, asking: [], mode: MODE }
const modeIs = value => ({ ...modeView, mode: { ...MODE, value } })
const radios = () => H.raw.match(/<input[^>]*type="radio"[^>]*>/g) ?? []

// not shown while the switch is off, and not on a backend that does not know the setting
html = render({ ...base, asking: [], mode: MODE })
ok(H.modes.length === 0 && radios().length === 0 && !H.raw.includes('radiogroup') && !html.includes(MODE.label) && !html.includes('Only safe requests'), `switch off: no word about approvals beside it (${html})`)
html = render(onView)
ok(H.modes.length === 0 && radios().length === 0 && !H.raw.includes('radiogroup') && !html.includes('When a step needs approval'), 'an older backend (no mode): the choice is not shown at all')
ok(P.tasksOf(onView).mode.value === 'here' && P.tasksOf(onView).mode.choices.length === 0 && P.tasksOf(onView).asking.length === 0, 'an older backend: asks here, as before, and nothing waits elsewhere')

// on: one group with its name, three choices, each with its one line; the one in force is checked
html = render(modeView)
ok(H.raw.includes('role="radiogroup"') && H.raw.includes('aria-label="When a step needs approval"') && html.includes('When a step needs approval'), 'on: one group of choices, named for the eye and for a screen reader')
ok(JSON.stringify(H.modes.map(m => [m.choice.value, m.choice.label, m.checked, m.disabled])) === JSON.stringify([['here', 'Ask on this computer', true, false], ['linked', 'Let my own linked computers approve', false, false], ['safe', 'Only safe requests', false, false]]), `on: three choices, the one in force is checked (${JSON.stringify(H.modes.map(m => [m.choice.value, m.checked]))})`)
ok(MODE.choices.every(c => html.includes(c.label) && html.includes(c.note)) && html.indexOf('On.') < html.indexOf(MODE.label) && html.indexOf(MODE.label) < html.indexOf('How to ask from here'), 'on: each choice with its one line, directly under the switch')
ok(radios().length === 3 && radios().every(r => r.includes('name="gbt-tasks-mode"') && /id="gbt-tasks-mode-(here|linked|safe)"/.test(r) && /aria-describedby="gbt-tasks-mode-(here|linked|safe)-note"/.test(r) && /aria-labelledby="gbt-tasks-mode-(here|linked|safe)-label"/.test(r)) && H.raw.includes('<span class="break-words text-[0.8125rem] leading-snug text-(--ui-text-primary)" id="gbt-tasks-mode-safe-label">Only safe requests</span>') && radios().filter(r => /\schecked=""/.test(r)).length === 1 && (H.raw.match(/<label[^>]*for="gbt-tasks-mode-/g) ?? []).length === 3, `on: three radio buttons under one name, each with its label (${radios().join(' ')})`)
ok(H.confirms.length === 0 && !html.includes(WARNING) && !H.raw.includes('role="dialog"'), 'on: nothing is asked before the owner chooses')
render(modeIs('safe'))
ok(JSON.stringify(H.modes.map(m => m.checked)) === JSON.stringify([false, false, true]), 'the backend says which choice is in force')

// "Only safe requests": at once, one call, the answer is shown
H.restCalls = []
H.setData = []
H.notes = []
const safeView = modeIs('safe')
H.restReply = async () => safeView
render(modeView)
H.modes.find(m => m.choice.value === 'safe').onChoose('safe')
await tick()
ok(H.restCalls.length === 1 && H.restCalls[0].path === '/tasks/mode' && H.restCalls[0].opts.method === 'POST' && JSON.stringify(H.restCalls[0].opts.body) === JSON.stringify({ mode: 'safe' }), `only safe requests: one call, at once (${JSON.stringify(H.restCalls)})`)
ok(H.setData.some(([k, v]) => k === 'tasks' && v === safeView) && H.notes.length === 0, 'only safe requests: the new state is shown at once, without a toast')
H.restCalls = []
render(safeView)
H.modes.find(m => m.choice.value === 'here').onChoose('here')
await tick()
ok(H.restCalls.length === 1 && JSON.stringify(H.restCalls[0].opts.body) === JSON.stringify({ mode: 'here' }), 'back to asking here: one call, nothing to confirm')
H.restCalls = []
render(safeView)
H.modes.find(m => m.choice.value === 'safe').onChoose('safe')
await tick()
ok(H.restCalls.length === 0, 'the choice already in force: no call')

// "Let my own linked computers approve": nothing is sent until the owner has read the backend's sentence and said yes
H.restCalls = []
render(modeView)
H.modes.find(m => m.choice.value === 'linked').onChoose('linked')
await tick()
ok(H.restCalls.length === 0, 'letting other computers approve: choosing it sends nothing')
html = render(modeView, { initialConfirm: true })
ok(H.confirms.length === 1 && H.confirms[0].title === 'Let your linked computers approve steps here?' && H.confirms[0].description === WARNING && H.confirms[0].confirmLabel === 'Let them approve' && H.confirms[0].cancelLabel === 'Cancel' && H.confirms[0].destructive !== true, `the question: the backend's own sentence, two plain answers (${JSON.stringify(H.confirms.map(c => [c.title, c.description]))})`)
ok(html.includes(WARNING) && H.raw.includes('role="alertdialog"') && JSON.stringify(H.buttons.map(b => b.text).filter(t => t !== 'How to ask from here')) === JSON.stringify(['Cancel', 'Let them approve']) && H.restCalls.length === 0, 'the question is on screen with its sentence; still nothing sent')
// Cancel (the button, Escape): nothing is sent
H.buttons.find(b => b.text === 'Cancel').onClick()
H.confirms[0].onClose()
await tick()
ok(H.restCalls.length === 0, 'Cancel: nothing is sent')
const linkedView = modeIs('linked')
H.restReply = async () => linkedView
H.setData = []
await H.confirms[0].onConfirm()
await tick()
ok(H.restCalls.length === 1 && H.restCalls[0].path === '/tasks/mode' && JSON.stringify(H.restCalls[0].opts.body) === JSON.stringify({ mode: 'linked' }) && H.setData.some(([k, v]) => k === 'tasks' && v === linkedView), `Let them approve: one call (${JSON.stringify(H.restCalls)})`)
render(linkedView)
ok(JSON.stringify(H.modes.map(m => m.checked)) === JSON.stringify([false, true, false]) && H.confirms.length === 0, 'afterwards: that choice is checked, no question left open')
// (without the sentence that says what it means, that choice is not offered; the other two are)
render({ ...modeView, mode: { ...MODE, warning: '' } }, { initialConfirm: true })
ok(JSON.stringify(H.modes.map(m => m.choice.value)) === JSON.stringify(['here', 'safe']) && H.confirms.length === 0, 'no warning sentence from the backend: other computers cannot be let in from here')

// a refusal by the backend: said as a toast, in its own words
H.notes = []
H.restReply = async () => ({ error: { code: 'internal', hint: 'Try again.', message: 'Something went wrong inside Grok Bot Twin.' } })
render(modeView)
H.modes.find(m => m.choice.value === 'safe').onChoose('safe')
await tick()
ok(H.notes.length === 1 && H.notes[0].kind === 'error' && H.notes[0].title === 'Could not change this' && H.notes[0].message === 'Something went wrong inside Grok Bot Twin.', `a failure: what was tried, what happened (${JSON.stringify(H.notes)})`)

// the fold's first line says what is true under each choice
ok(render(modeView, { initialHow: true }).includes('Your twins on your other computers can ask their copies here to do things. Hermes asks for your approval here, by the same rules as in your own chats.'), 'asking here: the line as before')
html = render(linkedView, { initialHow: true })
ok(html.includes('Your twins on your other computers can ask their copies here to do things. Hermes asks for your approval here or on the computer that asked, by the same rules as in your own chats.') && !html.includes('Hermes asks for your approval here, by'), `other computers may approve: the line says so (${html})`)
html = render(safeView, { initialHow: true })
ok(html.includes('Your twins on your other computers can ask their copies here to do things. A step that would need your approval is refused at once.') && !html.includes('Hermes asks for your approval'), 'only safe requests: the line says so')
ok(render({ ...safeView, switch: base.switch }, { initialHow: true }).includes('When on, your twins on your other computers can ask their copies here to do things. A step that would need your approval is refused at once.'), 'off: the line says what would hold when on')
ok([modeView, linkedView, safeView].every(v => sentences(render(v, { initialHow: true }).replace(LABEL, '')).every(s => words(s) <= 22)), 'no sentence over 22 words under any choice')

// what the backend sends is tidied: an unknown value means asking here; junk is dropped; markup stays text
{
  const t = P.tasksOf({ ...onView, mode: { choices: [{ label: '<b>Ask</b> here', note: 'x'.repeat(500), value: 'here' }, { label: 'Twice', note: '', value: 'here' }, { label: 'Other', note: '', value: 'always' }, { label: '', note: '', value: 'safe' }, null, 'junk'], label: 7, value: 'always', warning: 'w'.repeat(900) } })
  ok(t.mode.value === 'here' && t.mode.label === 'When a step needs approval' && t.mode.warning.length === 300 && JSON.stringify(t.mode.choices.map(c => [c.value, c.label])) === JSON.stringify([['here', '<b>Ask</b> here']]) && t.mode.choices[0].note.length === 200, `mode, tidied (${JSON.stringify(t.mode.choices.map(c => c.value))})`)
  html = render({ ...onView, mode: { ...MODE, choices: [{ label: '<b>Ask</b> here', note: '<i>n</i>', value: 'here' }, MODE.choices[2]] } })
  ok(html.includes('<b>Ask</b> here') && html.includes('<i>n</i>') && !H.raw.includes('<b>Ask</b>'), 'a choice is shown as text')
}

// ── 4c. a step that waits on another computer, which this one may answer ───

const asking = { asked_at: iso(0), command: 'rm -f /tmp/gbt-sandbox/does-not-exist-41 <b>x</b>', description: 'delete a file', id: 'k-00000000000000cc', peer: 'mini', q: '4d5e6f70', text: 'clear the old note', twin: 'Test Scout' }
const askView = { ...modeView, asking: [asking], log: [row({ dir: 'out', id: asking.id, label: 'waiting for approval on mini', state: 'waiting_approval', text: asking.text })] }

ok(renderWaiting(modeView) === '', 'nothing waits here or elsewhere: nothing at the top of the page')
html = renderWaiting(askView)
ok(askView.waiting.length === 0 && html.includes('mini asks before it does one step for your request to Test Scout: clear the old note') && html.includes('One step needs your approval: delete a file.'), `a step on another computer is shown though nothing waits here (${html})`)
ok(html.includes('rm -f /tmp/gbt-sandbox/does-not-exist-41 <b>x</b>') && H.raw.includes('does-not-exist-41 &lt;b&gt;x&lt;/b&gt;</code>') && !H.raw.includes('<b>x</b>'), 'the step: the exact command, as text')
ok(html.includes('Nothing happens on mini until then. You can also answer on mini.') && H.raw.includes('role="alert"') && H.raw.includes('aria-label="Your request to mini waits for your approval"'), 'it says where the step waits, and that it can be answered there too')
ok(JSON.stringify(H.buttons.map(b => [b.text, b.label, b.variant])) === JSON.stringify([['Do it once', 'Do it once: the step on mini', 'secondary'], ["Don't", "Don't do the step on mini", 'text']]), `two answers, each named with the computer (${JSON.stringify(H.buttons.map(b => [b.text, b.label, b.variant]))})`)
ok(!/always|for this session|every time|remember/i.test(html), 'no standing permission is offered')
ok(!render(askView).includes('does-not-exist-41'), 'shown once, at the top, not again next to the switch')
// the page's one main button: the first step of all, and a step that waits here comes first
renderWaiting(askView, { lead: true })
ok(JSON.stringify(H.buttons.map(b => b.variant)) === JSON.stringify(['default', 'text']), 'leading the page by itself: its "Do it once" is the main button')
html = renderWaiting({ ...askView, waiting: [waiting] }, { lead: true })
ok(html.indexOf('mini asked Test Scout here') < html.indexOf('mini asks before it does one step') && JSON.stringify(H.buttons.map(b => b.variant)) === JSON.stringify(['default', 'text', 'secondary', 'text']), 'with a step that waits here: that one comes first and holds the main button')
ok(H.queries[0].refetchInterval({ state: { data: askView } }) === 2000, 'while it waits the page looks again every 2 s')

for (const [label, choice, said] of [['Do it once', 'once', 'Sent to mini: do that one step once.'], ["Don't", 'deny', "Sent to mini: don't do that step."]]) {
  H.restCalls = []
  H.notes = []
  H.restReply = async () => modeView
  renderWaiting(askView)
  await H.buttons.find(b => b.text === label).onClick()
  await tick()
  ok(H.restCalls.length === 1 && H.restCalls[0].path === `/tasks/${asking.id}/answer` && H.restCalls[0].opts.method === 'POST' && JSON.stringify(H.restCalls[0].opts.body) === JSON.stringify({ choice, q: '4d5e6f70' }), `${label}, for a step elsewhere: one call with "${choice}" and the mark of the step shown (${JSON.stringify(H.restCalls)})`)
  ok(H.notes.length === 1 && H.notes[0].message === said && H.notes[0].kind === (choice === 'once' ? 'success' : 'info'), `${label}: says what happens now, and where (${JSON.stringify(H.notes)})`)
}

// an entry without the mark of its step is not shown (the answer always carries it); nor one with another id
html = renderWaiting({ ...askView, asking: [{ ...asking, q: undefined }, { ...asking, q: 'XYZ' }, { ...asking, id: '../../x' }, 'junk', null] })
ok(html === '' && H.buttons.length === 0, 'no mark, or a malformed entry: not shown at all')
ok(P.tasksOf({ ...askView, asking: Array.from({ length: 9 }, (_, i) => ({ ...asking, command: 'echo hello '.repeat(60), id: `k-${String(i).padStart(16, '0')}` })) }).asking.length === 5 && P.tasksOf({ ...askView, asking: [{ ...asking, command: 'echo hello '.repeat(60) }] }).asking[0].command.length === 400, 'at most five, and a command of at most 400 characters')

// the step changed meanwhile: read again, and say why nothing was done
H.restCalls = []
H.notes = []
H.invalidated = 0
H.restReply = async () => ({ error: { code: 'step_changed', hint: 'Look at it again, then answer.', message: 'The step that waits for you is not the one you answered.' } })
renderWaiting(askView)
await H.buttons.find(b => b.text === 'Do it once').onClick()
await tick()
ok(H.invalidated === 1 && H.notes.length === 1 && H.notes[0].kind === 'error' && H.notes[0].title === 'Could not pass your answer on' && H.notes[0].message === 'The step that waits for you is not the one you answered.', `a step elsewhere changed: read again (${JSON.stringify(H.notes)})`)
// that computer takes its answers itself by now: said in the backend's words, nothing read again for it
H.notes = []
H.invalidated = 0
H.restReply = async () => ({ error: { code: 'approve_there', hint: '', message: 'That step can only be answered on mini.' } })
renderWaiting(askView)
await H.buttons.find(b => b.text === "Don't").onClick()
await tick()
ok(H.invalidated === 0 && H.notes.length === 1 && H.notes[0].kind === 'error' && H.notes[0].message === 'That step can only be answered on mini.', 'not allowed from here any more: said plainly')

// ── 5. the list of requests ─────────────────────────────────────────────────

const logView = {
  ...onView,
  log: [
    row({ dir: 'out', id: 'k-00000000000000b1', label: 'done on mini - 12 s', peer: 'mini', text: 'how much disk space is free?' }),
    row({ id: 'k-00000000000000b2' }),
    row({ dir: 'out', id: 'k-00000000000000b3', label: 'not sent: requests are switched off on mini', state: 'refused' }),
    row({ dir: 'relay', id: 'k-00000000000000b4', label: 'done on MacBook Pro - 6 s', peer: 'Orchard', peer_to: 'MacBook Pro', text: '', twin: '' })
  ]
}

html = render(logView)
ok(H.buttons.map(b => b.text).join('|') === 'How to ask from here|Requests (4)' && !html.includes('how much disk space') && !/k-0{8}|\d{4}-\d\d-\d\dT/.test(html), 'the list is folded: the page stays short; no id and no raw time stamp on the card')
html = render(logView, { initialOpen: true })
ok(html.includes('You asked Test Scout on mini') && html.includes('Done on mini - 12 s') && html.includes('how much disk space is free?'), `asked from here: who, where, how it ended, what (${html})`)
ok(html.includes('mini asked Test Scout here') && html.includes('Done here · 3 min ago') && html.includes('what operating system do you run?'), 'asked of this computer: who asked, how it ended, when')
ok(html.includes('Not sent: requests are switched off on mini'), 'a refusal says why')
ok(html.includes('Orchard asked MacBook Pro') && html.includes('Done on MacBook Pro - 6 s'), 'passed along between two other computers: who asked whom')
ok(P.requestTitle({ dir: 'relay', peer: 'Orchard', peerTo: '' }) === 'Orchard asked another computer' && P.requestTitle({ dir: 'in', peer: 'mini', twin: '' }) === 'mini asked a twin here', 'a missing name never leaves a gap')
ok(render({ ...onView, log: Array.from({ length: 60 }, (_, i) => row({ id: `k-${String(i).padStart(16, '0')}` })) }, { initialOpen: true }).split('mini asked Test Scout here').length - 1 === 20, 'at most 20 requests are listed')

// ── 6. words ────────────────────────────────────────────────────────────────

const all = [render(base), render(onView), renderWaiting(waitView), render(waitView, { initialOpen: true }), render(logView, { initialOpen: true }), render(modeView, { initialConfirm: true, initialHow: true }), render(linkedView, { initialHow: true }), render(safeView, { initialHow: true }), renderWaiting(askView)].join(' ')
const visible = all.replace(/rm -f \.\/does-not-exist-7c1e\/old-note\.txt/g, '').replace(/rm -f \/tmp\/gbt-sandbox\/does-not-exist-41 <b>x<\/b>/g, '')

for (const w of ['token', 'relay', 'mailbox', 'stdio', 'machine', 'pass ', 'server', 'port', 'payload', 'config', 'json', 'follower', 'source', 'null', 'undefined', ' id ', 'k-0', 'queued', 'waiting_approval']) {
  ok(!visible.toLowerCase().includes(w), `no "${w.trim()}" on screen`)
}
ok(!/hermes twin|terminal|README/i.test(visible), 'no command and no terminal as a remedy')
ok(!/[!]|please|simply|just |successfully|oops/i.test(visible), 'short and plain: no exclamation mark, no filler')

// ── 7. the toast when the page is not on screen ─────────────────────────────

H.notes = []
P.setPageOpen(0)
P.toastTasks({ count: 1, what: 'needs_approval' })
ok(H.notes.length === 1 && H.notes[0].message === 'A request waits for your approval' && H.notes[0].action?.label === 'Open Twins' && H.notes[0].kind === 'warning', `a step waits and the page is closed: one toast that opens the page (${JSON.stringify(H.notes.map(n => n.message))})`)
ok(words(H.notes[0].meta) <= 22 && !/mini|Surface|rm -f/.test(JSON.stringify(H.notes[0])), 'the toast names no computer and no command (events carry none)')
for (const what of ['asked', 'answered', 'arrived', 'finished', 'switch_on', 'approved', '', undefined]) {
  P.toastTasks({ count: 1, what })
}
ok(H.notes.length === 1, 'nothing else about requests makes a toast')
P.setPageOpen(1)
P.toastTasks({ count: 1, what: 'needs_approval' })
ok(H.notes.length === 1, 'no toast while the Twins page is on screen: the page itself shows it')
P.setPageOpen(0)

// a step on another computer waits for an answer from here: its own toast, only while the page is closed
H.notes = []
P.toastTasks({ count: 1, what: 'needs_answer' })
ok(H.notes.length === 1 && H.notes[0].id.endsWith(':tasks:asking') && H.notes[0].kind === 'warning' && H.notes[0].message === 'A step on another computer waits for your approval' && H.notes[0].meta === 'A request you sent needs one step approved. Open Twins to answer.' && H.notes[0].action?.label === 'Open Twins', `a step elsewhere waits and the page is closed: one toast that opens the page (${JSON.stringify(H.notes)})`)
ok(words(H.notes[0].meta) <= 22 && !/mini|Surface|rm -f/.test(JSON.stringify(H.notes[0])), 'that toast names no computer and no command either')
P.toastTasks({ count: 1, what: 'needs_approval' })
ok(H.notes.length === 2 && H.notes[1].id.endsWith(':tasks:waiting') && H.notes[1].id !== H.notes[0].id, 'the two kinds of waiting have their own toasts: one does not replace the other')
P.setPageOpen(1)
P.toastTasks({ count: 1, what: 'needs_answer' })
ok(H.notes.length === 2, 'no such toast while the Twins page is on screen')
P.setPageOpen(0)

rmSync(work, { force: true, recursive: true })
console.log(`requests check: ${passed} assertions passed`)
