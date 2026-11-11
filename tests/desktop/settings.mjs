// Settings that follow between the owner's computers (tools, preferences, persona, model): the Twins page's part
// of it, outside Hermes Desktop.
//
//   node tests/desktop/settings.mjs [--modules DIR]
//
// DIR is a node_modules folder holding react + react-dom (19.x). A temp copy of desktop/plugin.js gets its three
// bare imports rewritten to local shims (a small stub of @hermes/plugin-sdk and the real React), as check.mjs
// and requests.mjs do; this file only knows the settings section. Invented data only.

import assert from 'node:assert/strict'
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const source = readFileSync(resolve(here, '../../desktop/plugin.js'), 'utf8')
const SHAPES = JSON.parse(readFileSync(resolve(here, 'settings_shapes.json'), 'utf8')).shapes
const argModules = process.argv.indexOf('--modules')
const modulesDir = argModules > 0 ? resolve(process.argv[argModules + 1]) : resolve(here, '../../node_modules')

let passed = 0
const ok = (cond, msg) => {
  assert.ok(cond, msg)
  passed += 1
}

if (!existsSync(join(modulesDir, 'react/package.json')) || !existsSync(join(modulesDir, 'react-dom/package.json'))) {
  console.log('settings check: skipped (no react / react-dom in the modules folder)')
  process.exit(0)
}

// ── 1. shims ────────────────────────────────────────────────────────────────

const work = mkdtempSync(join(tmpdir(), 'twin-settings-'))
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
  Button: `({ children, disabled, loading, onClick, ...p }) => { H.buttons.push({ text: H.text(children), label: p['aria-label'], expanded: p['aria-expanded'], onClick, disabled: Boolean(disabled || loading), variant: p.variant ?? 'default' }); return jsx('button', { disabled: Boolean(disabled || loading), 'aria-label': p['aria-label'], children }) }`,
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
    .concat('\nexport const __settings = { SettingsSection, settingsOf, settingsLine, settingsTwinWords, handleEvent }\n')
)

const H = (globalThis.__h = {
  buttons: [],
  switches: [],
  notes: [],
  restCalls: [],
  restReply: async () => ({}),
  setData: [],
  settings: undefined,
  queries: [],
  invalidated: 0,
  text: c => (Array.isArray(c) ? c.map(H.text).join('') : typeof c === 'string' || typeof c === 'number' ? String(c) : c?.props?.children ? H.text(c.props.children) : ''),
  host: { notify: n => H.notes.push(n), navigate: () => {}, state: { profile: { get: () => 'default' }, gateway: { get: () => 'open' } } },
  queryClient: { getQueryData: () => undefined, setQueryData: (key, value) => H.setData.push([key[1], value]), invalidateQueries: async o => { H.invalidated += 1; H.invalidatedKey = o?.queryKey } },
  useQuery: opts => {
    H.queries.push(opts)

    return { data: opts.queryKey[1] === 'fleetSettings' ? H.settings : undefined, error: null, isError: false, isPending: false, refetch: async () => {} }
  }
})

const mod = await import(pathToFileURL(join(work, 'plugin.mjs')).href)
const P = mod.__settings
const { renderToStaticMarkup } = req('react-dom/server')
const { jsx } = req('react/jsx-runtime')
const timeouts = []

mod.default.register({
  onDispose: () => {},
  onEvent: () => {},
  registerMany: () => {},
  rest: async (path, opts) => {
    H.restCalls.push({ path, opts })

    return H.restReply(path, opts)
  },
  setInterval: () => () => {},
  setTimeout: fn => { timeouts.push(fn); return () => {} },
  storage: { get: (k, f) => f, remove: () => {}, set: () => {} }
})

const tick = async (n = 3) => {
  for (let i = 0; i < n; i++) {
    await new Promise(r => setImmediate(r))
  }
}
const text = html => html.replace(/<[^>]+>/g, ' ').replace(/&#x27;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, '&').replace(/&gt;/g, '>').replace(/&lt;/g, '<').replace(/\s+/g, ' ').trim()
let raw = ''
const render = (data, props = {}) => {
  H.buttons = []
  H.switches = []
  H.queries = []
  H.settings = data
  raw = renderToStaticMarkup(jsx(P.SettingsSection, props))

  return text(raw)
}
const open = data => render(data, { initialOpen: true })
const words = s => s.trim().split(/\s+/).filter(w => /[A-Za-z0-9]/.test(w)).length
const sentences = s => s.match(/[^.?!]+[.?!]/g) ?? []
const button = t => H.buttons.find(b => b.text === t)

// ── 2. shapes (tests/desktop/settings_shapes.json) ──────────────────────────

function fits(value, type, path) {
  const out = []

  for (const alt of type.split('|')) {
    const list = alt.match(/^list\[(.+)\]$/)

    if (alt === 'null' ? value === null : alt === 'str' ? typeof value === 'string' : alt === 'bool' ? typeof value === 'boolean' : alt === 'int' ? Number.isInteger(value) : false) {
      return []
    }

    if (list && Array.isArray(value)) {
      return value.flatMap((v, i) => fits(v, list[1], `${path}[${i}]`))
    }

    if (alt === 'ErrorBody' && value && typeof value === 'object' && typeof value.code === 'string' && typeof value.message === 'string') {
      return []
    }

    if (SHAPES[alt] && value && typeof value === 'object' && !Array.isArray(value)) {
      const sh = SHAPES[alt]

      for (const [k, t] of Object.entries(sh.required)) {
        out.push(...(k in value ? fits(value[k], t, `${path}.${k}`) : [`${path}.${k} is missing`]))
      }

      for (const [k, t] of Object.entries(sh.optional ?? {})) {
        if (k in value) {
          out.push(...fits(value[k], t, `${path}.${k}`))
        }
      }

      for (const k of Object.keys(value)) {
        if (sh.closed && !(k in sh.required) && !(k in (sh.optional ?? {}))) {
          out.push(`${path}.${k} is not in the shape`)
        }
      }

      return out
    }
  }

  return [`${path} is not ${type}`]
}

// ── 3. fixtures (invented) ──────────────────────────────────────────────────

const iso = minsAgo => new Date(Date.now() - minsAgo * 60_000).toISOString()
const NEVER = 'Never copied: passwords, keys, sign-ins, the .env file, how a connector is set up, approval and safety settings.'
const ITEMS = [
  { id: 'tools', label: 'Tools', on: true, what: 'Which sets of tools each twin may use.' },
  { id: 'preferences', label: 'Preferences', on: true, what: 'A few plain choices, such as its personality and its time zone.' },
  { id: 'persona', label: 'Persona', on: true, what: 'The instructions in its prompt file.' },
  { id: 'model', label: 'Model', note: 'Each computer keeps its own model.', on: false, what: 'Which model it thinks with.' }
]
const T1 = 't-0123456789abcdef'
const T2 = 't-00aa11bb22cc33dd'
const twin = (over = {}) => ({ changed: [], held: false, id: T1, name: 'Test Scout', needs: [], profile: 'test-scout', restore: [], state: 'in_sync', synced_at: iso(4), ...over })
const point = (over = {}) => ({ at: iso(4), items: ['tools', 'persona'], label: 'before the settings from mini arrived, 14:05', point: 'p-0a1b2c3d4e5f', ...over })
const sharing = { items: ITEMS, never: NEVER, other: null, role: 'source', sentence: 'Your twins take these settings from this computer to your other computers.', state: 'on', twins: [] }
const linked = { items: ITEMS, never: NEVER, other: 'mini', role: 'follower', sentence: 'Your twins here take these settings from mini.', state: 'on', twins: [twin(), twin({ id: T2, name: 'Test Quill', profile: 'test-quill' })] }
const needy = { ...linked, twins: [twin({ needs: [{ kind: 'connector', message: 'Set up the connector notes on this computer.', name: 'notes' }, { kind: 'tools', message: 'The tools called browser are not on this computer.', name: 'browser' }], state: 'set_up' }), twin({ id: T2, name: 'Test Quill', profile: 'test-quill' })] }
const restorable = { ...linked, twins: [twin({ changed: ['tools', 'persona'], restore: [point(), point({ label: 'before, 13:00', point: 'p-000000000001' })] })] }
const held = { ...linked, twins: [twin({ held: true, restore: [point()], state: 'held' })] }

for (const [name, view] of Object.entries({ held, linked, needy, restorable, sharing })) {
  const wrong = fits(view, 'Settings', name)

  ok(wrong.length === 0, `the invented answer "${name}" has the shape of the contract (${wrong.join('; ')})`)
}

// ── 4. hidden where there is nothing to say ─────────────────────────────────

ok(render({ ...sharing, role: 'none', state: 'none' }) === '' && render(undefined) === '' && render({ error: { code: 'internal' } }) === '' && render({ ...sharing, items: [] }) === '' && render('junk') === '', 'not linked and not sharing, an older backend, or an answer that is not one: no section at all')
ok(H.queries.length === 1 && H.queries[0].queryKey[1] === 'fleetSettings' && H.queries[0].refetchInterval === 30000, 'the section reads its own answer, and again every half minute')
H.restCalls = []
await H.queries[0].queryFn()
ok(H.restCalls.length === 1 && H.restCalls[0].path === '/fleet/settings' && H.restCalls[0].opts === undefined, `it reads GET /fleet/settings (${JSON.stringify(H.restCalls)})`)

// where it sits: under the list of computers on a linked computer (not while the page could not ask the backend),
// and under the sharing card on the computer that shares, only once it does share
ok(source.includes('fleet && !stale && jsx(SettingsSection, {}),') && source.includes("sharing?.role === 'source' && jsx(SettingsSection, {}),") && source.split('jsx(SettingsSection, {})').length - 1 === 2, 'the section is on both kinds of computer, once each')

// ── 5. the calm page: one line and one button ───────────────────────────────

let html = render(sharing)
const LINE = 'Settings follow too: tools, preferences, persona. Each computer keeps its own model.'
ok(html === `${LINE} Change` && H.switches.length === 0 && words(html) <= 14, `folded: one line and "Change" (${html})`)
ok(H.buttons.length === 1 && H.buttons[0].text === 'Change' && H.buttons[0].label === 'Change which settings follow' && H.buttons[0].expanded === false && H.buttons[0].variant === 'ghost', `folded: the one button says what it opens, and that it is closed (${JSON.stringify(H.buttons)})`)
ok(raw.includes('aria-label="Settings that follow between your computers"'), 'the section has a name')
ok(render(linked) === `${LINE} Change`, 'a linked computer: the same line')
ok(render({ ...sharing, items: ITEMS.map(i => ({ ...i, on: true })) }) === 'Settings follow too: tools, preferences, persona, model. Change', 'the model follows too once switched on, and the line stops saying it does not')
ok(render({ ...sharing, items: ITEMS.map(i => ({ ...i, on: false })) }) === 'No settings follow. Change', 'everything off: said in three words')
ok(render({ ...linked, state: 'older' }) === 'mini needs a newer Grok Bot Twin before settings can follow. Change', 'the other computer has an older Grok Bot Twin: one line says so')
ok(render({ ...linked, state: 'waiting' }) === `${LINE} Waiting for mini. Change`, 'nothing arrived yet: "Waiting for mini."')
html = render(needy)
ok(html === `${LINE} Change Set up on this computer: Test Scout.` && raw.includes('role="note"'), `a twin needs something here: said on the calm page in one line, as a note (${html})`)
ok(render({ ...needy, twins: ['Test Scout', 'Test Quill', 'Test Wren', 'Test Finch'].map((name, i) => twin({ id: `t-000000000000000${i}`, name, needs: needy.twins[0].needs, state: 'set_up' })) }).endsWith('Set up on this computer: Test Scout, Test Quill and Test Wren and more.'), 'many twins need something: three names at most')

// ── 6. opened: what follows, the four switches, what never does ─────────────

html = open(sharing)
ok(html.includes(sharing.sentence) && html.indexOf(sharing.sentence) < html.indexOf('Tools') && html.includes(NEVER) && html.indexOf(NEVER) > html.indexOf('Which model it thinks with.'), `opened: the sentence about what follows first, what never does last (${html})`)
ok(JSON.stringify(H.switches.map(s => [s.label, s.id, s.checked])) === JSON.stringify([['Tools', 'gbt-settings-tools', true], ['Preferences', 'gbt-settings-preferences', true], ['Persona', 'gbt-settings-persona', true], ['Model', 'gbt-settings-model', false]]), `opened: four switches, each named by its label, the model off (${JSON.stringify(H.switches.map(s => [s.label, s.checked]))})`)
ok(ITEMS.every(i => html.includes(i.what)) && html.split('Each computer keeps its own model.').length - 1 === 2 && html.indexOf('Which model it thinks with.') < html.lastIndexOf('Each computer keeps its own model.'), 'opened: each switch says what it is; the model says that each computer keeps its own')
ok(ITEMS.every(i => raw.includes(`for="gbt-settings-${i.id}"`)) && H.buttons[0].expanded === true, 'opened: every label belongs to its switch; the button says it is open')
ok(!/Put back|Follow .* again|Settings in sync/.test(html), 'the computer that shares: no list of twins, nothing to put back')

for (const [i, item] of ITEMS.entries()) {
  const after = { ...sharing, items: ITEMS.map(x => (x.id === item.id ? { ...x, on: !x.on } : x)) }

  H.restCalls = []
  H.setData = []
  H.notes = []
  H.restReply = async () => after
  open(sharing)
  await H.switches[i].onCheckedChange(!item.on)
  await tick()
  ok(H.restCalls.length === 1 && H.restCalls[0].path === '/fleet/settings' && H.restCalls[0].opts.method === 'POST' && JSON.stringify(H.restCalls[0].opts.body) === JSON.stringify({ item: item.id, on: !item.on }), `${item.label}: one call that names the switch and its new place (${JSON.stringify(H.restCalls)})`)
  ok(H.setData.some(([k, v]) => k === 'fleetSettings' && v === after) && H.notes.length === 0, `${item.label}: the answer is shown at once, without a toast`)
}

// a refusal by the backend is said in its own plain words, as a toast
H.notes = []
H.restReply = async () => ({ error: { code: 'bad_item', hint: 'Try again.', message: 'That is not one of the settings that can follow.' } })
open(sharing)
await H.switches[3].onCheckedChange(true)
await tick()
ok(H.notes.length === 1 && H.notes[0].kind === 'error' && H.notes[0].title === 'Could not switch this on' && H.notes[0].message === 'That is not one of the settings that can follow.' && H.notes[0].meta === 'Try again.', `a refusal: what was tried, what happened, what to do (${JSON.stringify(H.notes)})`)
H.notes = []
open(sharing)
await H.switches[0].onCheckedChange(false)
await tick()
ok(H.notes[0]?.title === 'Could not switch this off', 'a refusal when switching off says so')

// ── 7. a linked computer: each twin's settings ──────────────────────────────

html = open(linked)
ok(html.includes('Your twins here take these settings from mini.') && html.includes('Test Scout Settings in sync, 4 min ago') && html.includes('Test Quill Settings in sync, 4 min ago') && H.switches.length === 4, `linked: the switches, then each twin and how its settings stand (${html})`)
ok(H.buttons.map(b => b.text).join('|') === 'Change', 'in step and nothing to put back: no button beside a twin')
ok(open({ ...linked, twins: [twin({ state: 'waiting', synced_at: null })] }).includes('Test Scout Waiting for settings from mini'), 'nothing arrived for a twin yet: waiting, with the name of the other computer')
ok(open({ ...linked, twins: [twin({ state: 'off', synced_at: null })] }).includes('Test Scout No settings follow here'), 'every switch off here: said per twin')
ok(open({ ...linked, twins: [twin({ state: 'error' })] }).includes('Test Scout Its settings could not be saved here') && open({ ...linked, twins: [twin({ error: { code: 'write_failed', message: 'Hermes could not save its settings on this computer.' }, state: 'error' })] }).includes('Test Scout Hermes could not save its settings on this computer.'), 'a twin whose settings could not be written: said, in the backend\'s words when it sends some')
ok(open({ ...linked, twins: [twin({ state: 'something_new' })] }).includes('Test Scout Waiting for settings from mini'), 'a state the page does not know reads as waiting, never as a raw word')

// a tool set or a connector this computer lacks
html = open(needy)
ok(html.includes('Test Scout Set up on this computer Set up the connector notes on this computer. The tools called browser are not on this computer.') && !html.includes('Set up on this computer:'), `a twin needs something this computer lacks: "Set up on this computer", then what, in the backend's words (${html})`)
ok(raw.includes('aria-label="Set up on this computer, for Test Scout"') && !/alert/.test(raw), 'what to set up is a named list, and nothing shouts')
ok(open({ ...linked, twins: [twin({ needs: [{ kind: 'connector', name: 'notes' }, { kind: 'model', name: '' }, { kind: 'tools', name: 'browser' }] })] }).includes('Settings in sync, 4 min ago Set up on this computer: Set up the connector notes on this computer. Choose a model for it on this computer. The tools called browser are not on this computer.'), 'needs without a sentence from the backend still read as sentences')

// Put back: the newest restore point
html = open(restorable)
ok(html.includes('Settings in sync, 4 min ago · last changed: tools, persona') && html.includes('Before the settings from mini arrived, 14:05') && !html.includes('before, 13:00'), `what arrived last, and what "Put back" goes back to (${html})`)
ok(H.buttons.map(b => [b.text, b.variant, b.label].join('/')).join('|') === 'Change/ghost/Change which settings follow|Put back/secondary/Put back the settings Test Scout had here', `one quiet button, named for its twin (${JSON.stringify(H.buttons)})`)
H.restCalls = []
H.setData = []
H.notes = []
H.restReply = async () => held
await button('Put back').onClick()
await tick()
ok(H.restCalls.length === 1 && H.restCalls[0].path === '/fleet/settings/restore' && JSON.stringify(H.restCalls[0].opts.body) === JSON.stringify({ point: 'p-0a1b2c3d4e5f', twin: T1 }), `Put back: one call with the twin and the newest point (${JSON.stringify(H.restCalls)})`)
ok(H.setData.some(([k, v]) => k === 'fleetSettings' && v === held) && H.notes.length === 1 && H.notes[0].kind === 'success' && H.notes[0].message === 'Test Scout has its earlier settings again' && H.notes[0].meta === 'They stay as they are here until they next change on mini.' && H.notes[0].action === undefined, `Put back: the new state at once, and a toast that says what holds now and offers nothing (${JSON.stringify(H.notes)})`)

// held: kept as they are here, and the way back to following
html = open(held)
ok(html.includes('Test Scout Kept as they are here') && H.buttons.map(b => b.text).join('|') === 'Change|Follow mini again' && button('Follow mini again').label === 'Follow mini again with the settings of Test Scout' && button('Follow mini again').variant === 'secondary', `held: said, with one button that names the other computer (${html})`)
H.restCalls = []
H.setData = []
H.notes = []
H.restReply = async () => linked
await button('Follow mini again').onClick()
await tick()
ok(H.restCalls.length === 1 && H.restCalls[0].path === '/fleet/settings/follow' && JSON.stringify(H.restCalls[0].opts.body) === JSON.stringify({ twin: T1 }) && H.setData.some(([k, v]) => k === 'fleetSettings' && v === linked) && H.notes.length === 0, `Follow again: one call with the twin; the answer is shown (${JSON.stringify(H.restCalls)})`)
ok(open({ ...linked, twins: [twin({ held: true })] }).includes('Kept as they are here'), 'held is believed from either key')

// errors of the two buttons: the usual envelope, as a toast
for (const [view, label, title] of [[restorable, 'Put back', 'Could not put the settings back'], [held, 'Follow mini again', 'Could not follow mini again']]) {
  H.notes = []
  H.restReply = async () => ({ error: { code: 'unknown_point', hint: 'Open Twins again and look.', message: 'That earlier version is no longer kept.' } })
  open(view)
  await button(label).onClick()
  await tick()
  ok(H.notes.length === 1 && H.notes[0].kind === 'error' && H.notes[0].title === title && H.notes[0].message === 'That earlier version is no longer kept.' && H.notes[0].meta === 'Open Twins again and look.', `${label} refused: message and hint (${JSON.stringify(H.notes)})`)
}

// what is shown is text, and what is not an answer is dropped
html = open({ ...linked, items: [...ITEMS, { id: 'secrets', label: 'Secrets', on: true, what: 'x' }, null, 'junk'], other: '<b>mini</b>', twins: [twin({ name: '<script>alert(1)</script>', needs: [{ kind: 'connector', message: '<img src=x onerror=1>', name: 'x' }, null], restore: [{ label: 'x', point: '../../x' }, point()] }), { ...twin(), id: '../../x' }, 'junk', null] })
ok(html.includes('<script>alert(1)</script>') && html.includes('<img src=x onerror=1>') && !raw.includes('<script>') && !raw.includes('<img') && H.switches.length === 4 && !html.includes('Secrets'), 'untrusted text is shown as text; a switch the page does not know is not shown')
ok(H.buttons.filter(b => b.text === 'Put back').length === 1 && html.split('Settings in sync').length - 1 === 1, 'a twin or a restore point with a malformed id is dropped')
H.restCalls = []
H.restReply = async () => linked
await button('Put back').onClick()
await tick()
ok(H.restCalls[0].opts.body.point === 'p-0a1b2c3d4e5f', 'Put back never sends a point of another shape')

// ── 8. events: any news from the backend reads the section again ────────────

for (const [i, what] of ['settings', 'synced', 'linked', 'unlinked'].entries()) {
  H.invalidated = 0
  timeouts.length = 0
  // (each with its own time: the page handles an event it has seen before only once)
  P.handleEvent({ at: iso(i + 1), payload: { count: 1, what }, type: 'fleet' }, { setTimeout: fn => { timeouts.push(fn); return () => {} } })
  timeouts.forEach(fn => fn())
  ok(H.invalidated >= 1 && JSON.stringify(H.invalidatedKey) === JSON.stringify(['grok-bot-twin']), `a fleet event "${what}": everything of the page is read again, this section with it (${H.invalidated})`)
  await tick()
}

// ── 9. words ────────────────────────────────────────────────────────────────

const all = [render(sharing), render(linked), render(needy), render({ ...linked, state: 'older' }), render({ ...linked, state: 'waiting' }), open(sharing), open(linked), open(needy), open(restorable), open(held)].join(' ')
const visible = all.split(NEVER).join('').split(sharing.sentence).join('').split(linked.sentence).join('')

// ("in sync" is the page's own word for a computer that has what the other one has)
for (const w of ['synced', 'syncing', 'in step', 'token', 'follower', 'source', 'machine', 'server', 'port ', 'payload', 'config', 'json', 'yaml', 'null', 'undefined', ' id ', 't-0', 'p-0', 'set_up', 'in_sync', 'allowlist', 'restore point']) {
  ok(!visible.toLowerCase().includes(w), `no "${w.trim()}" on screen`)
}
ok(!/hermes twin|terminal|README/i.test(visible), 'no command and no terminal as a remedy')
ok(!/[!]|please|simply|just |successfully|oops/i.test(visible), 'short and plain: no exclamation mark, no filler')
// (the rows of the opened list are not sentences: the lines the page itself writes are checked one by one)
const own = [render(sharing), render(linked), render(needy), render({ ...linked, state: 'older' }), render({ ...linked, state: 'waiting' })].map(l => l.replace(/ Change/, '.'))
ok(own.every(l => sentences(l).every(s => words(s) <= 22)) && words('They stay as they are here until they next change on mini.') <= 22, `no sentence of the page's own over 22 words (${own.join(' | ')})`)
ok(words(render(linked)) <= 14 && words(render(needy)) <= 22, 'the calm page pays at most 14 words for this section, 22 with something to set up')

rmSync(work, { force: true, recursive: true })
console.log(`settings check: ${passed} assertions passed`)
