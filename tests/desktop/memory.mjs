// Twin memory between the owner's computers: the Twins page's part of it, outside Hermes Desktop.
//
//   node tests/desktop/memory.mjs [--modules DIR]
//
// DIR is a node_modules folder holding react + react-dom (19.x). A temp copy of desktop/plugin.js gets its three
// bare imports rewritten to local shims (a small stub of @hermes/plugin-sdk and the real React), as check.mjs and
// requests.mjs do; this file is separate so that it only knows the memory section. Invented data only.
//
// The render is a static one, so what a press changes inside the section cannot be read back from the screen.
// Each act is therefore checked in two halves that share one object: the press is made and what it hands back
// (the sentence to say, or the problem) is looked at, and then the section is drawn with exactly that.

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
  console.log('memory check: skipped (no react / react-dom in the modules folder)')
  process.exit(0)
}

// ── 1. shims ────────────────────────────────────────────────────────────────

const work = mkdtempSync(join(tmpdir(), 'twin-memory-'))
// (the temp copy goes away however the run ends)
process.on('exit', () => rmSync(work, { force: true, recursive: true }))
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
  Button: `({ children, disabled, loading, onClick, ...p }) => { H.buttons.push({ text: H.text(children), label: p['aria-label'], expanded: p['aria-expanded'], describedBy: p['aria-describedby'], onClick, disabled: Boolean(disabled || loading), variant: p.variant ?? 'default' }); return jsx('button', { disabled: Boolean(disabled || loading), 'aria-label': p['aria-label'], 'aria-expanded': p['aria-expanded'], children }) }`,
  cn: `(...a) => a.flat(Infinity).filter(Boolean).join(' ')`,
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
    .concat('\nexport const __memory = { MemorySection, memoryOf }\n')
)

const H = (globalThis.__h = {
  buttons: [],
  switches: [],
  notes: [],
  restCalls: [],
  restReply: async () => ({}),
  setData: [],
  invalidated: 0,
  memory: undefined,
  queries: [],
  text: c => (Array.isArray(c) ? c.map(H.text).join('') : typeof c === 'string' || typeof c === 'number' ? String(c) : c?.props?.children ? H.text(c.props.children) : ''),
  host: { notify: n => H.notes.push(n), navigate: () => {}, state: { profile: { get: () => 'default' }, gateway: { get: () => 'open' } } },
  queryClient: { getQueryData: () => undefined, setQueryData: (key, value) => H.setData.push([key[1], value]), invalidateQueries: async () => { H.invalidated += 1 } },
  useQuery: opts => {
    H.queries.push(opts)

    return { data: opts.queryKey[1] === 'memory' ? H.memory : undefined, error: null, isError: false, isPending: false, refetch: async () => {} }
  }
})

const mod = await import(pathToFileURL(join(work, 'plugin.mjs')).href)
const P = mod.__memory
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
let markup = '' // the last render as it would reach the screen: tags and all
const render = (data, props = {}) => {
  H.buttons = []
  H.switches = []
  H.queries = []
  H.memory = data
  markup = renderToStaticMarkup(jsx(P.MemorySection, props))

  return text(markup)
}
// a fresh count of everything a press can cause
const quiet = reply => {
  H.restCalls = []
  H.setData = []
  H.notes = []
  H.invalidated = 0
  H.restReply = reply
}
const button = name => H.buttons.filter(b => b.text === name)
const bodyOf = call => JSON.stringify(call?.opts?.body)
const words = s => s.trim().split(/\s+/).filter(w => /[A-Za-z0-9]/.test(w)).length

// ── 2. fixtures (invented) ──────────────────────────────────────────────────

const SENTENCE = "A twin's memory stays on the computer it was written on. Switch a twin on here and its memory is kept the same on your linked computers that switch it on too."
const WHAT = [
  "What follows: the notes in that twin's two memory files, MEMORY.md (what it has learned) and USER.md (what it knows about you). Nothing else.",
  'How: encrypted from one of your computers to the other with a key made when you linked them. It is not readable on your network, and it goes to no cloud.',
  'Never: a note that looks like it holds a password, key or card number. It stays on the computer it is on. This check can miss things.',
  'A chat that is already open sees new notes when you start the next chat.'
]
const OFF = "Off. This twin's memory stays on this computer."
const HELD = '1 note here stays on this computer only: it looks like it holds a password or key.'
const LABEL = name => `Keep ${name}'s memory the same on my computers`
const SCOUT = 't-0123456789abcdef'
const QUILL = 't-00000000000000aa'
const NORTH = '0123456789abcdef01234567'
const SOUTH = '89abcdef0123456789abcdef'
const twin = (over = {}) => ({ can_forget: false, conflicts: [], count: { memory: 3, user: 1 }, from_others: 0, id: SCOUT, last: null, message: OFF, name: 'Test Scout', notes: [], on: false, profile: 'test-scout', restore: [], state: 'off', synced_at: null, ...over })
const view = (twins = [twin()], over = {}) => ({ other: 'mini', role: 'follower', sentence: SENTENCE, twins, what: WHAT, ...over })
const conflict = { at: '2000-01-01T09:05:00Z', file: 'MEMORY.md', id: 'c-0123456789ab', kind: 'both_changed', message: 'The same note was changed on mini and on Surface. Both versions are kept.', versions: [{ key: NORTH, text: 'Prefers the north trail.' }, { key: SOUTH, text: 'Prefers the south trail.' }] }
const point = { at: '2000-01-01T09:00:00Z', label: 'The memory before notes arrived from your other computers', point: 'p-0123456789ab' }
const quill = twin({ can_forget: true, conflicts: [conflict], from_others: 2, id: QUILL, last: { added: 2, at: '2000-01-01T09:05:00Z', removed: 0 }, message: 'In sync with mini. 4 notes here, 2 from your other computers.', name: 'Test Quill', notes: [HELD], on: true, profile: 'test-quill', restore: [point], state: 'in_sync', synced_at: '2000-01-01T09:05:00Z' })
const base = view()
const both = view([twin(), quill])

// ── 3. off until it is switched on ──────────────────────────────────────────

let html = render(base)
ok(H.switches.length === 1 && H.switches[0].label === LABEL('Test Scout') && H.switches[0].checked === false && H.switches[0].disabled === false, `off by default: one switch for the twin, its spoken name, not checked (${JSON.stringify(H.switches)})`)
ok(html.startsWith(`Twin memory ${SENTENCE}`) && html.includes('Test Scout') && html.includes(OFF), `the heading, the sentence under it, the twin's name and what the backend says about it (${html})`)
ok(/<h2[^>]*>Twin memory<\/h2>/.test(markup) && /<label[^>]*for="gbt-memory-0"[^>]*>Test Scout<\/label>/.test(markup) && H.switches[0].id === 'gbt-memory-0', "the heading is a heading; the twin's name is the switch's label")
ok(/<label class="inline-flex min-h-7 shrink-0 cursor-pointer items-center" for="gbt-memory-0"><button role="switch"[^>]*id="gbt-memory-0"/.test(markup), 'the switch sits in a 28 px target that presses it, as the other switches of the page do')
ok(JSON.stringify(H.buttons.map(b => [b.text, b.expanded])) === JSON.stringify([['What follows', false]]), `a twin that is off with nothing kept: the switch and one closed fold, nothing else to press (${JSON.stringify(H.buttons.map(b => b.text))})`)
ok(H.queries.length === 1 && H.queries[0].queryKey[1] === 'memory' && H.queries[0].refetchInterval === 5000, 'one read of its own, again every 5 s and no faster')
await H.queries[0].queryFn()
ok(H.restCalls.length === 1 && H.restCalls[0].path === '/fleet/memory' && H.restCalls[0].opts === undefined, `the read is GET /fleet/memory (${JSON.stringify(H.restCalls)})`)

// a computer that is linked to no other: the heading and the backend's sentence, no rows
{
  const alone = "A twin's memory stays on this computer. Once this computer is linked to another one, you can switch a twin's memory on to keep it the same on both."
  html = render(view([], { other: null, role: 'none', sentence: alone }))
  ok(html === `Twin memory ${alone}` && H.switches.length === 0 && H.buttons.length === 0 && !markup.includes('<li'), `not linked: the heading and the sentence only (${html})`)
  html = render(view([twin()], { other: null, role: 'none' }))
  ok(html === `Twin memory ${SENTENCE}` && H.switches.length === 0, 'not linked: no rows, whatever the answer lists')
}
ok(render(undefined) === '' && render({ error: { code: 'internal', message: 'Something went wrong inside Grok Bot Twin.' } }) === '' && render({ twins: [twin()] }) === '' && render('junk') === '', 'before the first answer, on a problem, or on an older backend: nothing at all')

// ── 4. the switch ───────────────────────────────────────────────────────────

const ON = 'In sync with mini. 4 notes here, 0 from your other computers.'
const onView = view([twin({ can_forget: true, message: ON, on: true, state: 'in_sync' })])

quiet(async () => onView)
render(base)
let said = await H.switches[0].onCheckedChange(true)
await tick()
ok(H.restCalls.length === 1 && H.restCalls[0].path === '/fleet/memory' && H.restCalls[0].opts.method === 'POST' && bodyOf(H.restCalls[0]) === JSON.stringify({ twin: SCOUT, on: true }), `switching on: one call with the twin and "on" (${JSON.stringify(H.restCalls)})`)
ok(H.setData.length === 1 && H.setData[0][0] === 'memory' && H.setData[0][1] === onView && H.notes.length === 0 && !said.problem && said.said === '', 'switching on: the answer that came back is what is shown, without a toast')
html = render(onView, { initialSaid: said })
ok(H.switches[0].checked === true && html.includes(ON) && !html.includes(OFF), `switched on: the returned state (${html})`)

quiet(async () => base)
await H.switches[0].onCheckedChange(false)
await tick()
ok(H.restCalls.length === 1 && bodyOf(H.restCalls[0]) === JSON.stringify({ twin: SCOUT, on: false }), 'switching off: one call, nothing to confirm')

// a refusal: said in the twin's own row in the backend's words, and the switch stays where it was
const refusal = { code: 'no_bot', hint: 'Wait until it is in sync, then switch memory on.', message: 'That twin has no Hermes bot on this computer yet.' }

quiet(async () => ({ error: refusal }))
render(base)
said = await H.switches[0].onCheckedChange(true)
await tick()
ok(H.restCalls.length === 1 && H.setData.length === 0 && H.invalidated === 0 && H.notes.length === 0, `a refusal: nothing is shown as changed, and no toast (${JSON.stringify(H.setData)})`)
ok(said.twin === SCOUT && said.problem?.message === refusal.message && said.problem?.hint === refusal.hint, 'a refusal: the row is handed what happened and what to do')
html = render(base, { initialSaid: said })
ok(html.includes(refusal.message) && html.includes(refusal.hint) && /role="alert"/.test(markup) && H.switches[0].checked === false, `a refusal: message and hint in the row, as an alert; the switch is still off (${html})`)
ok(render(both, { initialSaid: said }).includes(refusal.message) && markup.split('role="alert"').length - 1 === 1 && markup.indexOf('role="alert"') < markup.indexOf('Test Quill'), "a refusal is said once, in that twin's row")
// the call never reached the plugin: the same place, in plain words
quiet(async () => { throw new Error('socket hang up') })
render(base)
said = await H.switches[0].onCheckedChange(true)
await tick()
ok(H.notes.length === 0 && render(base, { initialSaid: said }).includes('Hermes could not reach Grok Bot Twin.') && H.switches[0].checked === false, 'the plugin cannot be reached: said in the row, the switch is still off')
// an answer that is not about memory is never shown as one: the section reads again instead
quiet(async () => ({}))
render(base)
await H.switches[0].onCheckedChange(true)
await tick()
ok(H.setData.length === 0 && H.invalidated === 1, 'an empty answer: nothing is made up, the section reads again')

// ── 5. what the backend notes about a twin ──────────────────────────────────

html = render(both)
ok(html.includes(HELD) && html.indexOf(HELD) > html.indexOf('Test Quill') && html.includes('In sync with mini. 4 notes here, 2 from your other computers.'), `a twin's notes are under its name (${html})`)
ok(H.switches.length === 2 && H.switches[1].label === LABEL('Test Quill') && H.switches[1].checked === true && H.switches[0].checked === false && H.switches[1].id === 'gbt-memory-1', 'two twins: a switch each, each as the backend says')
html = render(view([twin({ notes: [HELD, 'A second line.', 7, null, ''] })]))
ok(html.endsWith(`${OFF} ${HELD} A second line.`), `only lines of text are notes (${html})`)

// ── 6. a note changed on two computers ──────────────────────────────────────

html = render(both)
ok(html.includes(conflict.message) && html.includes('Prefers the north trail.') && html.includes('Prefers the south trail.'), `a conflict: what happened, and both versions (${html})`)
ok((markup.match(/<blockquote[^>]*>Prefers the (north|south) trail\.<\/blockquote>/g) ?? []).length === 2, 'each version is a quoted block of plain text')
ok(button('Keep this one').length === 2 && button('Keep both').length === 1, `a conflict: "Keep this one" for each version and one "Keep both" (${JSON.stringify(H.buttons.map(b => b.text))})`)
ok(button('Keep this one')[0].label === 'Keep this one: Prefers the north trail.' && button('Keep this one')[1].label === 'Keep this one: Prefers the south trail.' && button('Keep both')[0].label.startsWith('Keep both: '), 'each spoken name begins with the words on the button and says which note')

quiet(async () => view([twin(), { ...quill, conflicts: [] }]))
said = await button('Keep this one')[1].onClick()
await tick()
ok(H.restCalls.length === 1 && H.restCalls[0].path === '/fleet/memory/resolve' && bodyOf(H.restCalls[0]) === JSON.stringify({ twin: QUILL, conflict: 'c-0123456789ab', keep: SOUTH }), `Keep this one: the key of the version beside it (${JSON.stringify(H.restCalls)})`)
ok(H.setData.length === 1 && !said.problem && !render(H.setData[0][1]).includes('north trail') && button('Keep both').length === 0, 'kept: the returned state has nothing left to settle')
render(both)
quiet(async () => both)
await button('Keep both')[0].onClick()
await tick()
ok(H.restCalls.length === 1 && bodyOf(H.restCalls[0]) === JSON.stringify({ twin: QUILL, conflict: 'c-0123456789ab', keep: 'both' }), 'Keep both: "both"')
// a note removed on one computer and changed on the other has one version: there is no "both" to keep
render(view([{ ...quill, conflicts: [{ ...conflict, kind: 'removed_and_changed', message: 'A note was removed on mini and changed on Surface. The changed note is kept.', versions: [conflict.versions[1]] }] }]))
ok(button('Keep this one').length === 1 && button('Keep both').length === 0, 'one version: one button')
// settled meanwhile on another computer: said in the row
quiet(async () => ({ error: { code: 'unknown_conflict', message: 'That is already settled.' } }))
render(both)
said = await button('Keep both')[0].onClick()
await tick()
ok(said.twin === QUILL && render(both, { initialSaid: said }).includes('That is already settled.') && markup.indexOf('role="alert"') > markup.indexOf('Test Quill'), "already settled: said in that twin's row")

// ── 7. restore points ───────────────────────────────────────────────────────

html = render(both)
ok(JSON.stringify(button('Earlier versions (1)').map(b => b.expanded)) === '[false]' && !html.includes(point.label) && button('Put back').length === 0, `restore points: folded, with their number (${JSON.stringify(H.buttons.map(b => b.text))})`)
html = render(both, { initialPoints: true })
ok(button('Earlier versions (1)')[0].expanded === true && new RegExp(`${point.label} · on (1 Jan|31 Dec)`).test(html) && !html.includes('2000-01-01T') && button('Put back').length === 1, `restore points, opened: what each is and when, in words (${html})`)

const RESTORED = 'Put back. Memory for this twin is now switched off on this computer, so your other computers keep theirs. Switch it on again to bring their notes back.'
const afterRestore = view([twin(), { ...quill, conflicts: [], message: OFF, on: false, state: 'off' }])

quiet(async () => ({ ...afterRestore, restored: { point: 'p-0123456789ab', sentence: RESTORED, twin: QUILL } }))
said = await button('Put back')[0].onClick()
await tick()
ok(H.restCalls.length === 1 && H.restCalls[0].path === '/fleet/memory/restore' && bodyOf(H.restCalls[0]) === JSON.stringify({ twin: QUILL, point: 'p-0123456789ab' }), `Put back: one call with the twin and the point (${JSON.stringify(H.restCalls)})`)
ok(H.setData.length === 1 && said.twin === QUILL && said.said === RESTORED && H.notes.length === 0, 'Put back: the row is handed the sentence that came back')
html = render(afterRestore, { initialSaid: said })
ok(html.includes(RESTORED) && markup.includes(`role="status">${RESTORED}</p>`) && H.switches[1].checked === false, `put back: the sentence as a status line, and the switch as the answer has it (${html})`)

// ── 8. forget on this computer ──────────────────────────────────────────────

const FORGOT = 'Forgotten on this computer: 2 notes from your other computers and 1 earlier version kept here. Your own notes here stay, and nothing changed on your other computers.'
const FORGET_WHAT = 'Takes out the notes that came from your other computers. Your own notes here stay.'
const afterForget = view([twin(), { ...quill, can_forget: false, conflicts: [], from_others: 0, message: OFF, notes: [], on: false, restore: [], state: 'off' }])

html = render(both)
ok(button('Forget on this computer').length === 1 && html.includes(FORGET_WHAT) && html.indexOf(FORGET_WHAT) > html.indexOf('Test Quill'), 'Forget on this computer: only where there is something to forget, with what it does beside it')
ok(button('Forget on this computer')[0].describedBy === 'gbt-memory-1-forget' && markup.includes(`id="gbt-memory-1-forget">${FORGET_WHAT}</span>`), 'the sentence beside the button is its description')
quiet(async () => ({ ...afterForget, forgot: { notes: 2, points: 1, sentence: FORGOT, twin: QUILL } }))
said = await button('Forget on this computer')[0].onClick()
await tick()
ok(H.restCalls.length === 1 && H.restCalls[0].path === '/fleet/memory/forget' && H.restCalls[0].opts.method === 'POST' && bodyOf(H.restCalls[0]) === JSON.stringify({ twin: QUILL }), `Forget: one press, one call, nothing asked first (${JSON.stringify(H.restCalls)})`)
ok(H.setData.length === 1 && said.twin === QUILL && said.said === FORGOT && H.notes.length === 0, 'Forget: the row is handed the sentence that came back')
html = render(afterForget, { initialSaid: said })
ok(html.includes(FORGOT) && markup.includes(`role="status">${FORGOT}</p>`) && button('Forget on this computer').length === 0 && !html.includes(FORGET_WHAT), `forgotten: the sentence as a status line; nothing left to forget (${html})`)

// ── 8a. which computers take part (on the computer that shares) ─────────────

const TAKE_PART = 'Computers that take part'
const SURFACE = 'm-0123456789ab'
const MINI = 'm-0000000000aa'
const shared = (computers, over = {}) => view([twin(), { ...quill, computers, ...over }], { other: null, role: 'source' })
const sharedView = shared([{ id: SURFACE, name: 'Surface', on: true }, { id: MINI, name: 'mini', on: false }])

ok(!render(both).includes(TAKE_PART) && H.switches.length === 2 && !render(shared([])).includes(TAKE_PART) && H.switches.length === 2 && !render(shared(undefined)).includes(TAKE_PART) && !render(shared('junk')).includes(TAKE_PART), 'no list of computers given, or an empty one: no line about them and no switch')
html = render(sharedView)
ok(html.includes(`${HELD} ${TAKE_PART} Surface mini ${conflict.message}`) && markup.includes(`<p class="text-xs leading-relaxed text-(--ui-text-secondary)">${TAKE_PART}</p>`), `computers that take part: one small line under the twin's message and notes, then a row each (${html})`)
ok(JSON.stringify(H.switches.map(s => [s.label, s.checked, s.id])) === JSON.stringify([[LABEL('Test Scout'), false, 'gbt-memory-0'], [LABEL('Test Quill'), true, 'gbt-memory-1'], ["Let Surface take part in Test Quill's memory", true, 'gbt-memory-1-c0'], ["Let mini take part in Test Quill's memory", false, 'gbt-memory-1-c1']]), `a switch for each computer, with its spoken name, as the backend says (${JSON.stringify(H.switches.map(s => [s.label, s.checked]))})`)
ok(/<label[^>]*for="gbt-memory-1-c0"[^>]*>Surface<\/label><label class="inline-flex min-h-7 shrink-0 cursor-pointer items-center" for="gbt-memory-1-c0"><button role="switch"[^>]*id="gbt-memory-1-c0"/.test(markup), "a computer's name is its switch's label, and the switch sits in the same 28 px target")
ok(!render(view([twin(), quill], { role: 'source' })).includes(TAKE_PART) && !render(shared([{ id: SURFACE, name: 'Surface', on: true }], { computers: undefined })).includes(TAKE_PART), 'a twin without the list has no such rows')

const surfaceOut = shared([{ id: SURFACE, name: 'Surface', on: false }, { id: MINI, name: 'mini', on: false }])

render(sharedView)
quiet(async () => surfaceOut)
said = await H.switches[2].onCheckedChange(false)
await tick()
ok(H.restCalls.length === 1 && H.restCalls[0].path === '/fleet/memory/computer' && H.restCalls[0].opts.method === 'POST' && bodyOf(H.restCalls[0]) === JSON.stringify({ twin: QUILL, machine: SURFACE, on: false }), `a computer's switch: one call with the twin, the computer and the new value (${JSON.stringify(H.restCalls)})`)
ok(H.setData.length === 1 && H.setData[0][1] === surfaceOut && H.notes.length === 0 && !said.problem, 'the answer that came back is what is shown')
render(surfaceOut, { initialSaid: said })
ok(H.switches[2].checked === false && H.switches[3].checked === false && H.switches[1].checked === true, "switched off for that computer: the returned state; the twin's own switch is as it was")
quiet(async () => sharedView)
await H.switches[3].onCheckedChange(true)
await tick()
ok(bodyOf(H.restCalls[0]) === JSON.stringify({ twin: QUILL, machine: MINI, on: true }), 'switching a computer in: its own name back to the backend, and "on"')
quiet(async () => ({ error: { code: 'not_linked', hint: "Link it first; then a twin's memory can follow.", message: 'This computer is not linked to another one.' } }))
render(sharedView)
said = await H.switches[2].onCheckedChange(false)
await tick()
html = render(sharedView, { initialSaid: said })
ok(H.setData.length === 0 && said.twin === QUILL && html.includes('This computer is not linked to another one.') && markup.split('role="alert"').length - 1 === 1 && markup.indexOf('role="alert"') > markup.indexOf('Test Quill') && H.switches[2].checked === true, "a refusal: said in that twin's row, and the computer's switch stays where it was")
html = render(shared([{ id: 'm-XYZ', name: 'Surface', on: true }, { id: 'm-0123456789abc', name: 'Surface', on: true }, { id: 5, name: 'Surface', on: true }, { id: MINI, name: 7, on: true }, { name: 'Surface', on: true }, 'junk', null]))
ok(!html.includes(TAKE_PART) && H.switches.length === 2, 'computers that could not be named back to the backend, or have no name: left out, and no line about none')
html = render(shared([{ id: 'm-XYZ', name: 'Surface', on: true }, { id: MINI, name: '<b>mini</b>', on: 'yes' }]))
ok(html.includes(`${TAKE_PART} <b>mini</b>`) && !/<b>/.test(markup) && H.switches.length === 3 && H.switches[2].checked === false && !html.includes('Surface take part'), 'a malformed computer is left out, the others stay; a name is shown as text; only a plain "on" is on')

// ── 8b. kept for a twin that is no longer on this computer ──────────────────

const GONE = 't-00000000000000bb'
const LEFT = n => `Earlier versions of a twin that is no longer on this computer are still kept here (${n}).`
const FORGOT_LEFT = 'Forgotten on this computer: 0 notes from your other computers and 2 earlier versions kept here. Your own notes here stay, and nothing changed on your other computers.'
const alone = "A twin's memory stays on this computer. Once this computer is linked to another one, you can switch a twin's memory on to keep it the same on both."
const leftView = { ...both, left: [{ id: GONE, points: 2 }] }

ok(!render(both).includes('no longer on this computer') && button('Forget them').length === 0 && !render({ ...both, left: [] }).includes('no longer on this computer') && button('Forget them').length === 0 && !render({ ...both, left: 'junk' }).includes('no longer on this computer'), 'nothing kept for a twin that left: no line and no button')
html = render(leftView)
ok(html.endsWith(`${FORGET_WHAT} ${LEFT(2)} Forget them`) && button('Forget them').length === 1 && button('Forget them')[0].variant === 'outline' && button('Forget them')[0].describedBy === 'gbt-memory-left-0' && markup.includes(`id="gbt-memory-left-0">${LEFT(2)}</span>`), `kept for a twin that left: one line with their number, below the twins, and "Forget them" (${html})`)
html = render(view([], { left: [{ id: GONE, points: 1 }], other: null, role: 'none', sentence: alone }))
ok(html === `Twin memory ${alone} ${LEFT(1)} Forget them` && H.switches.length === 0, `also on a computer that is linked to no other (${html})`)

render(leftView)
quiet(async () => ({ ...both, forgot: { notes: 0, points: 2, sentence: FORGOT_LEFT, twin: GONE }, left: [] }))
said = await button('Forget them')[0].onClick()
await tick()
ok(H.restCalls.length === 1 && H.restCalls[0].path === '/fleet/memory/forget' && H.restCalls[0].opts.method === 'POST' && bodyOf(H.restCalls[0]) === JSON.stringify({ twin: GONE }), `Forget them: one press, one call with that twin, nothing asked first (${JSON.stringify(H.restCalls)})`)
ok(H.setData.length === 1 && said.twin === GONE && said.said === FORGOT_LEFT && H.notes.length === 0, 'Forget them: the sentence that came back is handed on')
html = render(H.setData[0][1], { initialSaid: said })
ok(html.endsWith(`${FORGET_WHAT} ${FORGOT_LEFT}`) && markup.includes(`role="status">${FORGOT_LEFT}</p>`) && button('Forget them').length === 0 && !html.includes('no longer on this computer'), `forgotten: the sentence as a status line, though the line it belonged to is gone (${html})`)
quiet(async () => ({ error: { code: 'unknown_twin', message: 'That twin is not on this computer.' } }))
render(leftView)
said = await button('Forget them')[0].onClick()
await tick()
html = render(leftView, { initialSaid: said })
ok(H.setData.length === 0 && html.includes('That twin is not on this computer.') && markup.indexOf('role="alert"') > markup.indexOf('gbt-memory-left-0') && button('Forget them').length === 1, 'a refusal: said below the line, which stays')
html = render({ ...both, left: [{ id: 'x', points: 2 }, { id: 't-0123', points: 2 }, { id: GONE, points: 0 }, { id: GONE, points: -1 }, { id: GONE, points: 1.5 }, { id: GONE, points: '2' }, { id: GONE }, { points: 2 }, 'junk', null] })
ok(!html.includes('no longer on this computer') && button('Forget them').length === 0, 'an entry without a twin to name back, or without a whole number above zero: left out')
html = render({ ...both, left: [{ id: 'x', points: 9 }, { id: GONE, points: 3 }, { id: SCOUT, points: 1 }] })
ok(!html.includes('(9)') && html.endsWith(`${LEFT(3)} Forget them ${LEFT(1)} Forget them`) && JSON.stringify(button('Forget them').map(b => b.describedBy)) === JSON.stringify(['gbt-memory-left-0', 'gbt-memory-left-1']), 'a malformed entry is left out, the others stay: a line and a button each')

// ── 9. what follows ─────────────────────────────────────────────────────────

html = render(both)
ok(WHAT.every(line => !html.includes(line)) && button('What follows')[0].expanded === false, 'What follows: closed until it is opened')
html = render(both, { initialWhat: true })
ok(WHAT.every(line => html.includes(line)) && button('What follows')[0].expanded === true && (markup.match(/<li class="gbt-line">/g) ?? []).length === 4, `What follows, opened: the four lines as a list (${html})`)
ok(html.indexOf(WHAT[0]) < html.indexOf('Test Scout'), 'the lines are above the twins')

// ── 10. everything shown is text ────────────────────────────────────────────

{
  const rude = '<script>alert(1)</script>'
  const img = '<img src=x onerror=1>'
  const hostile = view(
    [
      twin({ can_forget: true, conflicts: [{ ...conflict, message: `${img} changed`, versions: [{ key: NORTH, text: `${rude}\nsecond line <b>bold</b>` }, { key: SOUTH, text: 'plain' }] }], message: `<i>Off</i> ${rude}`, name: '<b>Test Scout</b>', notes: [`${rude} stays here`], restore: [{ ...point, label: `${img} before` }] }),
      { ...quill, id: 't-1\n2' },
      { ...quill, id: 'x'.repeat(121) },
      { ...quill, id: '' },
      { ...quill, id: 7 },
      'junk',
      null
    ],
    { sentence: `${SENTENCE} ${rude}`, what: [`${img} line`, 5, null] }
  )

  html = render(hostile, { initialPoints: true, initialSaid: { said: `${rude} said`, twin: SCOUT }, initialWhat: true })
  ok(!/<script|<img|<b>|<i>/.test(markup), 'no text from the backend becomes markup')
  ok(html.includes(`${SENTENCE} ${rude}`) && html.includes(`${img} line`) && html.includes('<b>Test Scout</b>') && html.includes(`<i>Off</i> ${rude}`) && html.includes(`${rude} stays here`) && html.includes(`${img} changed`) && html.includes(`${rude} second line <b>bold</b>`) && html.includes(`${img} before`) && html.includes(`${rude} said`), `a note, a name or a sentence with markup in it is shown as the text it is (${html})`)
  ok(markup.includes('&lt;script&gt;alert(1)&lt;/script&gt;\nsecond line'), 'a note keeps its line breaks')
  ok(H.switches.length === 1 && H.switches[0].label === LABEL('<b>Test Scout</b>') && !html.includes('Test Quill'), 'an entry without a plain name of its own from the backend is not shown at all')
  const odd = P.memoryOf(view([twin({ conflicts: [{ ...conflict, id: 'c-1\t2' }, { ...conflict, versions: [{ key: 'both', text: 'x' }, { key: 5, text: 'y' }] }, 'junk'], restore: [{ ...point, point: '' }, { ...point, point: 'p-0123456789abcdef0123456789' }, null] })])).twins[0]
  ok(odd.conflicts.length === 0 && odd.points.length === 0, 'a conflict or a restore point that could not be named back to the backend is left out')
}

// ── 11. words and controls ──────────────────────────────────────────────────

{
  const all = [render(base), render(both, { initialPoints: true, initialWhat: true }), render(afterForget, { initialSaid: { said: FORGOT, twin: QUILL } }), render({ ...sharedView, left: [{ id: GONE, points: 2 }] })].join(' ')

  for (const w of ['t-0', 'c-0', 'p-0', 'm-0', NORTH, SOUTH, 'test-quill', 'undefined', 'null', '[object', 'in_sync', 'both_changed', 'follower', '2000-01-01T']) {
    ok(!all.includes(w), `no "${w}" on screen`)
  }
  ok(!/[!]|please|simply|just |successfully|oops|Working/i.test(all), 'short and plain: no exclamation mark, no filler')
  render(both, { initialPoints: true, initialWhat: true })
  ok(H.buttons.length === 7 && H.buttons.every(b => b.variant !== 'default' && b.disabled === false) && H.switches.every(s => s.disabled === false), `nothing in the section is a main button, and nothing waits while nothing runs (${JSON.stringify(H.buttons.map(b => [b.text, b.variant]))})`)
  ok(words(render(base)) <= 50, `one twin, switched off: the heading, the sentence, the row (${words(render(base))} words)`)
  ok(!/dangerouslySetInnerHTML|innerHTML/.test(source), 'no raw HTML anywhere in the page')
}

console.log(`memory check: ${passed} assertions passed`)
