// Routines on the owner's computers (which routine runs where): the Twins page's part of it, outside Hermes Desktop.
//
//   node tests/desktop/routines.mjs [--modules DIR]
//
// DIR is a node_modules folder holding react + react-dom (19.x). A temp copy of desktop/plugin.js gets its three
// bare imports rewritten to local shims (a small stub of @hermes/plugin-sdk and the real React), as requests.mjs
// does; this file only knows the routines section. Its canned answers are checked against
// tests/contract/routines_shapes.json first, so they cannot drift from what the backend sends. Invented data only.

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
  console.log('routines check: skipped (no react / react-dom in the modules folder)')
  process.exit(0)
}

// ── 1. shims ────────────────────────────────────────────────────────────────

const work = mkdtempSync(join(tmpdir(), 'twin-routines-'))
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
  Button: `({ children, disabled, loading, onClick, ...p }) => { H.buttons.push({ text: H.text(children), label: p['aria-label'], onClick, disabled: Boolean(disabled || loading), variant: p.variant ?? 'default', expanded: p['aria-expanded'] }); return jsx('button', { disabled: Boolean(disabled || loading), 'aria-label': p['aria-label'], children }) }`,
  cn: `(...a) => a.flat(Infinity).filter(Boolean).join(' ')`,
  host: `H.host`,
  queryClient: `H.queryClient`,
  relativeTime: `ms => { const m = Math.round((Date.now() - ms) / 60000); return m < 1 ? 'just now' : m + ' min ago' }`,
  Switch: `p => { H.switches.push({ label: p['aria-label'], checked: Boolean(p.checked), disabled: Boolean(p.disabled), id: p.id, describedBy: p['aria-describedby'], onCheckedChange: p.onCheckedChange }); return jsx('button', { role: 'switch', 'aria-checked': Boolean(p.checked), 'aria-label': p['aria-label'], id: p.id }) }`,
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
    .concat('\nexport const __routines = { RoutinesSection, routinesOf, staysHereWords, forceWords, handleEvent }\n')
)

const H = (globalThis.__h = {
  buttons: [],
  switches: [],
  notes: [],
  restCalls: [],
  restReply: async () => ({}),
  setData: [],
  routines: undefined,
  queries: [],
  invalidated: 0,
  text: c => (Array.isArray(c) ? c.map(H.text).join('') : typeof c === 'string' || typeof c === 'number' ? String(c) : c?.props?.children ? H.text(c.props.children) : ''),
  host: { notify: n => H.notes.push(n), navigate: () => {}, state: { profile: { get: () => 'default' }, gateway: { get: () => 'open' } } },
  queryClient: { getQueryData: () => undefined, setQueryData: (key, value) => H.setData.push([key[1], value]), invalidateQueries: async () => { H.invalidated += 1 } },
  useQuery: opts => {
    H.queries.push(opts)

    return { data: opts.queryKey[1] === 'fleet-routines' ? H.routines : undefined, error: null, isError: false, isPending: false, refetch: async () => {} }
  }
})

const mod = await import(pathToFileURL(join(work, 'plugin.mjs')).href)
const P = mod.__routines
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
  setTimeout: () => () => {},
  storage: { get: (k, f) => f, remove: () => {}, set: () => {} }
})

const tick = async (n = 3) => {
  for (let i = 0; i < n; i++) {
    await new Promise(r => setImmediate(r))
  }
}
const text = html => html.replace(/<[^>]+>/g, ' ').replace(/&#x27;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, '&').replace(/&gt;/g, '>').replace(/&lt;/g, '<').replace(/\s+/g, ' ').trim()
const render = (data, props = {}) => {
  H.buttons = []
  H.switches = []
  H.queries = []
  H.routines = data

  return text(renderToStaticMarkup(jsx(P.RoutinesSection, props)))
}
const reset = reply => {
  H.restCalls = []
  H.setData = []
  H.notes = []
  H.restReply = reply
}
const button = (words, where = () => true) => H.buttons.find(b => b.text === words && where(b))
const same = (a, b) => {
  try {
    assert.deepStrictEqual(a, b)

    return true
  } catch {
    return false
  }
}

// ── 2. fixtures (invented) ──────────────────────────────────────────────────

const iso = minsAgo => new Date(Date.now() - minsAgo * 60_000).toISOString()
const MINI = 'm-0a0a0a0a0a01'
const SURFACE = 'm-0b0b0b0b0b02'
const LINUX = 'm-0c0c0c0c0c03'
const MBP = 'm-0d0d0d0d0d04'
const at = (id, name, self = false) => ({ id, name, this: self })
const WHAT = ['the name', 'what it asks the twin', 'when it runs', 'the names of its skills']
const NEVER = ['scripts', 'folders it works in', 'model choices', 'where answers are sent', 'what earlier runs said']
const SENTENCE = 'Routines of your twins are copied to your other computers. Each one runs on one computer at a time.'
const SENTENCE_HERE = 'Routines of your twins on mini are copied here. Each one runs on one computer at a time.'
const routine = (over = {}) => ({ can_force: false, can_move_to: [at(SURFACE, 'Surface'), at(LINUX, 'Linux desktop')], id: 'j-00000000000000a1', label: 'runs here', name: 'Morning summary', on: true, runs_on: at(MINI, 'mini', true), schedule: 'every day at 08:00', state: 'running', twin: 't-00000000000000c1', twin_name: 'Test Scout', waiting_for: null, ...over })
const moving = routine({ can_move_to: [at(SURFACE, 'Surface'), at(LINUX, 'Linux desktop'), at(MINI, 'mini', true)], id: 'j-00000000000000a2', label: 'moving to Surface: waiting for mini to stop it', name: 'Friday review', runs_on: at(SURFACE, 'Surface'), schedule: 'Fridays at 16:30', state: 'moving', twin: 't-00000000000000c2', twin_name: 'Test Quill', waiting_for: 'mini' })
// on the computer with the Grok app (mini): two routines, one of them moving
const onMini = {
  can_run: true,
  local_only: 0,
  machines: [{ ...at(MINI, 'mini', true), can_run: true }, { ...at(SURFACE, 'Surface'), can_run: true }, { ...at(LINUX, 'Linux desktop'), can_run: true }],
  never: NEVER,
  on: true,
  problem: null,
  restore: [],
  role: 'source',
  routines: [routine(), moving],
  sentence: SENTENCE,
  state: 'on',
  supported: true,
  what_syncs: WHAT
}
// on a linked computer (Surface) where routines may not run yet
const onSurface = {
  ...onMini,
  can_run: false,
  machines: [{ ...at(MINI, 'mini'), can_run: true }, { ...at(SURFACE, 'Surface', true), can_run: false }],
  role: 'follower',
  routines: [routine({ can_move_to: [], label: 'runs on mini', runs_on: at(MINI, 'mini') })],
  sentence: SENTENCE_HERE
}
// linked to a computer whose Grok Bot Twin is an older one
const older = { ...onSurface, problem: { code: 'older_other_side', hint: 'Update Grok Bot Twin on mini: Capabilities → Plugins.', message: 'mini has an older Grok Bot Twin: routines do not copy until it is updated.' }, routines: [], sentence: '', state: 'older', supported: false }
const point = { at: iso(3), id: 'u-00000000000000e1', label: 'Morning summary, as it was before the copy from mini arrived', what: 'replaced' }
const withPoints = { ...onSurface, restore: [point, { ...point, at: '2000-01-05T12:00:00Z', id: 'u-00000000000000e2', label: 'Friday review, as it was before it was removed', what: 'removed' }] }
// the computer that has to stop a routine (MacBook Pro) left the owner's computers before it confirmed
const stuck = routine({ can_force: true, can_move_to: [at(SURFACE, 'Surface')], id: 'j-00000000000000a3', label: 'moving here: waiting for MacBook Pro to stop it', runs_on: at(MINI, 'mini', true), state: 'moving', waiting_for: 'MacBook Pro' })
const forced = { ...onMini, routines: [stuck] }

// ── 3. the canned answers fit the contract ──────────────────────────────────

{
  const shapes = { ...JSON.parse(readFileSync(resolve(here, '../contract/api_shapes.json'), 'utf8')).shapes, ...JSON.parse(readFileSync(resolve(here, '../contract/routines_shapes.json'), 'utf8')).shapes }
  const basic = { bool: v => typeof v === 'boolean', int: v => Number.isInteger(v), null: v => v === null, str: v => typeof v === 'string' }
  const fits = (v, type, path) => {
    const tried = type.split('|').map(t => {
      if (basic[t]) {
        return basic[t](v) ? [] : [`${path}: not ${t}`]
      }

      if (t.startsWith('list[')) {
        return Array.isArray(v) ? v.flatMap((x, i) => fits(x, t.slice(5, -1), `${path}[${i}]`)) : [`${path}: not a list`]
      }

      const s = shapes[t]

      if (!s || !v || typeof v !== 'object' || Array.isArray(v)) {
        return [`${path}: not ${t}`]
      }

      const known = { ...s.required, ...s.optional }

      return [...Object.entries(s.required).flatMap(([k, kt]) => (k in v ? fits(v[k], kt, `${path}.${k}`) : [`${path}: no "${k}"`])), ...Object.keys(v).filter(k => !(k in known)).map(k => `${path}: "${k}" is not in ${t}`)]
    })

    return tried.reduce((a, b) => (b.length < a.length ? b : a))
  }

  for (const [name, answer] of Object.entries({ forced, older, onMini, onSurface, withPoints })) {
    const wrong = fits(answer, 'Routines', name)

    ok(wrong.length === 0, `the canned answer "${name}" fits the Routines shape (${wrong.join('; ')})`)
  }
  ok(fits({ ...onMini, extra: 1 }, 'Routines', 'x').length === 1 && fits({ ...onMini, on: 'yes' }, 'Routines', 'x').length === 1, 'the check itself notices a key too many and a wrong type')
  ok(same(Object.keys(shapes.Routines.required), ['role', 'on', 'can_run', 'supported', 'state', 'sentence', 'what_syncs', 'never', 'machines', 'routines', 'local_only', 'restore', 'problem']) && same(Object.keys(shapes.Routine.required), ['id', 'twin', 'twin_name', 'name', 'schedule', 'on', 'runs_on', 'state', 'label', 'waiting_for', 'can_move_to', 'can_force']), 'the shapes list the keys of the sharing contract, in its order')
}

// ── 4. on the computer with the Grok app: the switch and the list ───────────

let html = render(onMini)
ok(H.switches.length === 1 && H.switches[0].label === 'Routines' && H.switches[0].checked === true && H.switches[0].id === 'gbt-routines' && H.switches[0].describedBy === 'gbt-routines-says', `one switch, named Routines, on, described by the sentence under it (${JSON.stringify(H.switches)})`)
ok(html.startsWith(`Routines ${SENTENCE}`), `the label, and under it the backend's own sentence, word for word (${html})`)
ok(html.includes('Morning summary Test Scout · every day at 08:00 Runs here') && html.includes('Friday review Test Quill · Fridays at 16:30 Moving to Surface: waiting for mini to stop it'), `each routine: its name, whose it is, when, and where it runs (${html})`)
ok(same(H.buttons.filter(b => /^Run /.test(b.text)).map(b => [b.text, b.label, b.disabled]), [
  ['Run on Surface', 'Run on Surface: Morning summary', false],
  ['Run on Linux desktop', 'Run on Linux desktop: Morning summary', false],
  ['Run on Surface', 'Run on Surface: Friday review', true],
  ['Run on Linux desktop', 'Run on Linux desktop: Friday review', false],
  ['Run here', 'Run here: Friday review', false]
]), `one button per computer a routine can move to; while moving, only another computer can be chosen (${JSON.stringify(H.buttons.map(b => [b.text, b.disabled]))})`)
ok(H.buttons.every(b => b.variant !== 'default' && b.variant !== 'destructive') && !H.buttons.some(b => b.text === 'Start it here anyway'), 'nothing filled, and no forced start where none is offered')
ok(!html.includes('stay on this computer') && !html.includes('as they were before') && !html.includes('Never copied'), 'nothing local, nothing to put back, and the small print is folded')
ok(same(H.buttons.filter(b => b.expanded !== undefined).map(b => [b.text, b.expanded]), [['What is copied', false]]), 'one fold: what is copied')
html = render(onMini, { initialOpen: true })
ok(html.includes('Copied: the name, what it asks the twin, when it runs, the names of its skills.') && html.includes('Never copied: scripts, folders it works in, model choices, where answers are sent, what earlier runs said.'), `the fold: what is copied and what never is, from the backend's lists (${html})`)
ok(H.queries.length === 1 && H.queries[0].queryKey[1] === 'fleet-routines' && H.queries[0].refetchInterval({ state: { data: onMini } }) === 5000 && H.queries[0].refetchInterval({ state: { data: onSurface } }) === 10000 && H.queries[0].refetchInterval({ state: { data: undefined } }) === 10000, 'read every 5 s while a routine is moving, every 10 s otherwise: never faster than the list of computers')

// switching off: one call, the answer is shown at once, no toast
const offMini = { ...onMini, on: false, routines: [], sentence: 'Routines stay on this computer.', state: 'off' }
reset(async () => offMini)
render(onMini)
await H.switches[0].onCheckedChange(false)
await tick()
ok(H.restCalls.length === 1 && H.restCalls[0].path === '/fleet/routines' && H.restCalls[0].opts.method === 'POST' && same(H.restCalls[0].opts.body, { on: false }), `switching off: one call (${JSON.stringify(H.restCalls)})`)
ok(H.setData.some(([k, v]) => k === 'fleet-routines' && v === offMini) && H.notes.length === 0, 'switching off: the new state is shown at once, without a toast')
html = render(offMini)
ok(H.switches[0].checked === false && html === 'Routines Routines stay on this computer. What is copied', `off: the switch, the sentence, the fold (${html})`)
reset(async () => onMini)
await H.switches[0].onCheckedChange(true)
await tick()
ok(H.restCalls.length === 1 && same(H.restCalls[0].opts.body, { on: true }), 'switching on again: one call')

// a refusal by the backend is said in its own words, as a toast
reset(async () => ({ error: { code: 'not_linked', hint: 'Link a computer first.', message: 'No other computer is linked.' } }))
render(offMini)
await H.switches[0].onCheckedChange(true)
await tick()
ok(H.notes.length === 1 && H.notes[0].kind === 'error' && H.notes[0].title === 'Could not switch this on' && H.notes[0].message === 'No other computer is linked.' && H.notes[0].meta === 'Link a computer first.' && H.setData.length === 0, `a failure: what was tried, what happened, what to do (${JSON.stringify(H.notes)})`)

// ── 5. moving a routine: one click ──────────────────────────────────────────

const afterMove = { ...onMini, routines: [routine({ label: 'moving to Surface: waiting for mini to stop it', runs_on: at(SURFACE, 'Surface'), state: 'moving', waiting_for: 'mini' }), moving] }
reset(async () => afterMove)
render(onMini)
await button('Run on Surface', b => b.label.endsWith('Morning summary')).onClick()
await tick()
ok(H.restCalls.length === 1 && H.restCalls[0].path === '/fleet/routines/j-00000000000000a1/move' && H.restCalls[0].opts.method === 'POST' && same(H.restCalls[0].opts.body, { to: SURFACE }), `Run on Surface: one call that names the computer (${JSON.stringify(H.restCalls)})`)
ok(H.setData.some(([k, v]) => k === 'fleet-routines' && v === afterMove) && H.notes.length === 0, 'the row itself says what happens next: the answer is shown at once, no toast')
reset(async () => onMini)
render(onMini)
await button('Run here').onClick()
await tick()
ok(H.restCalls.length === 1 && H.restCalls[0].path === '/fleet/routines/j-00000000000000a2/move' && same(H.restCalls[0].opts.body, { to: MINI }), 'Run here, while it is moving elsewhere: one call that names this computer')
reset(async () => ({ error: { code: 'cannot_run_there', hint: 'Switch "Routines may run on this computer" on there first.', message: 'Routines may not run on Linux desktop.' } }))
render(onMini)
await button('Run on Linux desktop').onClick()
await tick()
ok(H.notes.length === 1 && H.notes[0].kind === 'error' && H.notes[0].title === 'Could not move Morning summary' && H.notes[0].message === 'Routines may not run on Linux desktop.' && H.notes[0].meta === 'Switch "Routines may run on this computer" on there first.', `a move that is refused: said in the backend's words (${JSON.stringify(H.notes)})`)

// ── 6. on a linked computer: a second switch, off until the owner turns it on ─

html = render(onSurface)
ok(same(H.switches.map(s => [s.label, s.checked, s.id]), [['Routines', true, 'gbt-routines'], ['Routines may run on this computer', false, 'gbt-routines-run']]), `two switches; the second is off (${JSON.stringify(H.switches.map(s => [s.label, s.checked]))})`)
ok(html.includes(SENTENCE_HERE) && html.includes('Routines may run on this computer') && html.includes('Morning summary Test Scout · every day at 08:00 Runs on mini') && !H.buttons.some(b => /^Run /.test(b.text)), `it says where the routine runs, and offers no move the backend does not offer (${html})`)
const mayRun = { ...onSurface, can_run: true, routines: [routine({ can_move_to: [at(SURFACE, 'Surface', true)], label: 'runs on mini', runs_on: at(MINI, 'mini') })] }
reset(async () => mayRun)
await H.switches[1].onCheckedChange(true)
await tick()
ok(H.restCalls.length === 1 && H.restCalls[0].path === '/fleet/routines' && same(H.restCalls[0].opts.body, { can_run: true }) && H.setData.some(([k, v]) => k === 'fleet-routines' && v === mayRun) && H.notes.length === 0, `allowing routines to run here: one call (${JSON.stringify(H.restCalls)})`)
render(mayRun)
ok(H.switches[1].checked === true && same(H.buttons.filter(b => /^Run /.test(b.text)).map(b => [b.text, b.label]), [['Run here', 'Run here: Morning summary']]), 'allowed: the routine can be run here with one click')
reset(async () => onSurface)
await H.switches[1].onCheckedChange(false)
await tick()
ok(same(H.restCalls.map(c => c.opts.body), [{ can_run: false }]), 'and switched off again with one call')
ok(render(onMini) && H.switches.length === 1, 'the computer with the Grok app has no second switch')

// ── 7. an older Grok Bot Twin on the other side, or a problem: said, and no list ─

html = render(older)
ok(html.includes('mini has an older Grok Bot Twin: routines do not copy until it is updated.') && html.includes('Update Grok Bot Twin on mini: Capabilities → Plugins.'), `an older other side: the backend's sentence and what to do (${html})`)
html = render({ ...older, local_only: 2, routines: [routine()] })
ok(!html.includes('Morning summary') && !html.includes('stay on this computer') && !H.buttons.some(b => /^Run /.test(b.text)) && H.switches.length === 2, 'no list then, whatever was sent; the switches stay')
html = render({ ...onMini, problem: { code: 'internal', message: 'Hermes could not list the routines of Test Scout.' } })
ok(html.includes('Hermes could not list the routines of Test Scout.') && !html.includes('Morning summary'), 'a problem on a computer that is up to date: said the same way, and no list')
ok(render({ ...older, problem: null }).includes('What is copied') && !H.buttons.some(b => /^Run /.test(b.text)), 'not supported and nothing said about it: still no list')

// ── 8. routines that stay on this computer ──────────────────────────────────

ok(render({ ...onMini, local_only: 1 }).includes('1 routine stays on this computer because it runs a script.'), 'one routine stays: singular')
ok(render({ ...onMini, local_only: 3 }).includes('3 routines stay on this computer because they run a script.'), 'three stay: plural')
ok(P.staysHereWords(1) === '1 routine stays on this computer because it runs a script.' && P.staysHereWords(2) === '2 routines stay on this computer because they run a script.', 'the sentence itself')

// ── 9. restore points ───────────────────────────────────────────────────────

html = render(withPoints)
ok(H.buttons.some(b => b.text === 'Routines as they were before (2)' && b.expanded === false) && !html.includes('as it was before') && !H.buttons.some(b => b.text === 'Put back'), 'what can be put back is folded, with its number')
ok(!/restore point/i.test(html) && !/restore point/i.test(render(withPoints, { initialOpen: true })), 'the page keeps its own words: nothing is called a restore point on screen')
html = render(withPoints, { initialOpen: true })
ok(html.includes('Morning summary, as it was before the copy from mini arrived 3 min ago') && html.includes('Friday review, as it was before it was removed') && !/2000-01-05T|u-0{8}/.test(html), `each point: the backend's label and how long ago; no raw time stamp (${html})`)
ok(same(H.buttons.filter(b => b.text === 'Put back').map(b => [b.label, b.variant]), [['Put back: Morning summary, as it was before the copy from mini arrived', 'secondary'], ['Put back: Friday review, as it was before it was removed', 'secondary']]), 'one Put back per point, each with its own spoken name')
reset(async () => onSurface)
await button('Put back').onClick()
await tick()
ok(H.restCalls.length === 1 && H.restCalls[0].path === '/fleet/routines/restore/u-00000000000000e1' && H.restCalls[0].opts.method === 'POST' && same(H.restCalls[0].opts.body, {}), `Put back: one call (${JSON.stringify(H.restCalls)})`)
ok(H.notes.length === 1 && H.notes[0].kind === 'success' && H.notes[0].message === 'Put back in Hermes, paused' && H.notes[0].meta === point.label && H.setData.some(([k, v]) => k === 'fleet-routines' && v === onSurface), `Put back: a toast says what happened (${JSON.stringify(H.notes)})`)
reset(async () => ({ error: { code: 'unknown_restore', message: 'That restore point is gone.' } }))
render(withPoints, { initialOpen: true })
await button('Put back').onClick()
await tick()
ok(H.notes.length === 1 && H.notes[0].kind === 'error' && H.notes[0].title === 'Could not put that back' && H.notes[0].message === 'That restore point is gone.', 'a restore point that is gone: said, nothing claimed')

// ── 10. a computer that cannot confirm: a second, separate step ──────────────

const FORCE = 'MacBook Pro has not confirmed that it stopped this routine. If it is still on, the routine could run on both until it next reaches this computer.'

html = render(forced)
ok(html.includes('Moving here: waiting for MacBook Pro to stop it') && !html.includes('has not confirmed'), 'waiting for a computer that is gone: the label says so; the warning is not shown before it is asked for')
ok(same(H.buttons.filter(b => b.text === 'Start it here anyway').map(b => [b.label, b.variant, b.disabled]), [['Start it here anyway: Morning summary', 'outline', false]]) && button('Run on Surface').disabled === false, 'a separate, unfilled button; another computer can still be chosen with one click')
reset(async () => onMini)
await button('Start it here anyway').onClick()
await tick()
ok(H.restCalls.length === 0 && H.notes.length === 0, 'pressing it only asks: nothing is sent yet')
html = render(forced, { initialForce: stuck.id })
ok(html.includes('Start Morning summary here anyway?') && html.includes(FORCE) && P.forceWords('MacBook Pro') === FORCE && P.forceWords('').startsWith('The other computer has not confirmed'), `the question names the computer and says what could happen (${html})`)
ok(same(H.buttons.slice(-2).map(b => [b.text, b.variant]), [['Cancel', 'ghost'], ['Start it here anyway', 'default']]), 'the question: Cancel and the act')
await H.buttons.at(-2).onClick()
await tick()
ok(H.restCalls.length === 0, 'Cancel sends nothing')
await H.buttons.at(-1).onClick()
await tick()
ok(H.restCalls.length === 1 && H.restCalls[0].path === '/fleet/routines/j-00000000000000a3/move' && same(H.restCalls[0].opts.body, { force: true, to: MINI }) && H.setData.some(([k, v]) => k === 'fleet-routines' && v === onMini), `confirmed: one call that names this computer and says it is forced (${JSON.stringify(H.restCalls)})`)
render(onMini, { initialForce: 'j-00000000000000a1' })
ok(!H.buttons.some(b => b.text === 'Cancel'), 'no question for a routine that cannot be forced')
render({ ...forced, machines: forced.machines.filter(m => !m.this) })
ok(!H.buttons.some(b => b.text === 'Start it here anyway'), 'this computer is not named in the answer: no forced start is offered')

// ── 11. nothing to show, and untrusted text ─────────────────────────────────

ok(render(undefined) === '' && render({ error: { code: 'internal' } }) === '' && render({ ...onMini, role: 'none' }) === '' && render('junk') === '' && H.switches.length === 0, 'a computer that shares with no other one, or an older backend: no switch that could not work')
html = render({ ...onMini, routines: [routine({ label: '<b>runs</b> here', name: '<img src=x onerror=1>', schedule: '0 8 * * 1-5', twin_name: '<script>alert(1)</script>' }), routine({ id: '../../x' }), routine({ id: 'j 1' }), 'junk', null], sentence: '<i>Routines</i> are copied.' })
ok(html.includes('<img src=x onerror=1>') && html.includes('<script>alert(1)</script>') && html.includes('<b>runs</b> here') && html.includes('<i>Routines</i> are copied.') && H.buttons.filter(b => b.text === 'Run on Surface').length === 1, 'untrusted text is shown as text; an entry whose id could not go into an address is not shown at all')
ok(!html.includes('0 8 * * 1-5') && /08:00/.test(html), `a schedule that arrives as bare fields is said in words (${html})`)
html = render({ ...onMini, routines: Array.from({ length: 15 }, (_, i) => routine({ id: `j-${String(i).padStart(16, '0')}`, name: `Routine ${i + 1}` })) })
ok(html.includes('Routine 12') && !html.includes('Routine 13') && H.buttons.some(b => b.text === '3 more routines' && b.expanded === false), 'a long list: twelve rows, the rest folded')
ok(render({ ...onMini, routines: Array.from({ length: 15 }, (_, i) => routine({ id: `j-${String(i).padStart(16, '0')}`, name: `Routine ${i + 1}` })) }, { initialOpen: true }).includes('Routine 15'), 'the fold holds the rest')

// ── 12. words ───────────────────────────────────────────────────────────────

const all = [render(onMini, { initialOpen: true }), render(offMini), render(onSurface, { initialOpen: true }), render(mayRun), render(older), render(withPoints, { initialOpen: true }), render(forced, { initialForce: stuck.id }), render({ ...onMini, local_only: 2 })].join(' ')

for (const w of ['source', 'follower', 'token', 'tombstone', 'rev', 'cron', 'job', 'jobs', 'machine', 'machines', 'epoch', 'holder', 'server', 'payload', 'json', 'null', 'undefined', 'id']) {
  ok(!new RegExp(`\\b${w}\\b`, 'i').test(all), `no "${w}" on screen`)
}
ok(!/j-0{4}|m-0[a-d]|u-0{4}|t-0{4}|\d{4}-\d\d-\d\dT/.test(all), 'no id and no raw time stamp on screen')
ok(!/hermes twin|terminal|README/i.test(all), 'no command and no terminal as a remedy')
ok(!/[!]|please|simply|just |successfully|oops/i.test(all), 'short and plain: no exclamation mark, no filler')

// ── 13. the page reads again when the backend says something changed between computers ──

H.invalidated = 0
P.handleEvent({ payload: { count: 1, what: 'routines' }, type: 'fleet' }, { setTimeout: fn => { timeouts.push(fn); return () => {} } })
timeouts.forEach(fn => fn())
ok(H.invalidated === 1, 'a `fleet` event: everything the page shows is read again, the routines with it')

// (the folder this run made for its own shims, nothing else)
rmSync(work, { force: true, recursive: true })
console.log(`routines check: ${passed} assertions passed`)
