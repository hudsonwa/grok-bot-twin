// The twins' skills on the owner's linked computers: the Twins page's part of it, outside Hermes Desktop.
//
//   node tests/desktop/skills.mjs [--modules DIR]
//
// DIR is a node_modules folder holding react + react-dom (19.x). A temp copy of desktop/plugin.js gets its three
// bare imports rewritten to local shims (a small stub of @hermes/plugin-sdk and the real React), as check.mjs
// does; this file is separate so that it only knows the skills section. Invented data only.

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
  console.log('skills check: skipped (no react / react-dom in the modules folder)')
  process.exit(0)
}

// ── 1. shims ────────────────────────────────────────────────────────────────

const work = mkdtempSync(join(tmpdir(), 'twin-skills-'))
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
    .concat('\nexport const __skills = { FleetSkills, skillsOf, toastSkill, SHARED_SENTENCE, PRIVACY }\n')
)

const H = (globalThis.__h = {
  buttons: [],
  switches: [],
  notes: [],
  restCalls: [],
  restReply: async () => ({}),
  setData: [],
  skills: undefined,
  queries: [],
  text: c => (Array.isArray(c) ? c.map(H.text).join('') : typeof c === 'string' || typeof c === 'number' ? String(c) : c?.props?.children ? H.text(c.props.children) : ''),
  host: { notify: n => H.notes.push(n), navigate: () => {}, state: { profile: { get: () => 'default' }, gateway: { get: () => 'open' } } },
  queryClient: { getQueryData: () => undefined, setQueryData: (key, value) => H.setData.push([key[1], value]), invalidateQueries: async () => { H.invalidated = (H.invalidated ?? 0) + 1 } },
  useQuery: opts => {
    H.queries.push(opts)

    return { data: opts.queryKey[1] === 'fleet-skills' ? H.skills : undefined, error: null, isError: false, isPending: false, refetch: async () => {} }
  }
})

const mod = await import(pathToFileURL(join(work, 'plugin.mjs')).href)
const P = mod.__skills
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
const render = (data, props = {}) => {
  H.buttons = []
  H.switches = []
  H.queries = []
  H.skills = data

  return text(renderToStaticMarkup(jsx(P.FleetSkills, props)))
}
const words = s => s.trim().split(/\s+/).filter(w => /[A-Za-z0-9]/.test(w)).length
const sentences = s => s.match(/[^.?!]+[.?!]/g) ?? []

// ── 2. fixtures (invented) ──────────────────────────────────────────────────

const iso = minsAgo => new Date(Date.now() - minsAgo * 60_000).toISOString()
const SHARE = "Share my twins' skills with my other computers"
const WHAT_SHARE = "Sends your twins' skills (text files only) to your linked computers; never chats or passwords."
const WHAT_TAKE = "Brings your twins' skills from mini to this computer. A skill you change here stays yours until you choose."
const SCOUT = 't-0123456789abcdef'
const QUILL = 't-00aa11bb22cc33dd'
const counts = (over = {}) => ({ changed_here: 0, conflicts: 0, off: 0, only_here: 0, skills: 0, ...over })
const skill = (over = {}) => ({ can_switch: true, choices: [], group: 'outdoors', key: 'outdoors/trail-notes', label: 'same as on mini', name: 'trail-notes', note: null, on: true, state: 'in_sync', switch_label: 'trail-notes: follow it from mini', updated_at: iso(30), version: 3, versions: [], ...over })
const sourceOff = { counts: counts(), message: '', on: false, other: null, role: 'source', state: 'off', twins: [{ id: SCOUT, name: 'Test Scout', skills: [] }], what: WHAT_SHARE }
const followerOff = { counts: counts(), message: '', on: false, other: { id: 'm-3f9a1c0b7d2e', name: 'mini' }, role: 'follower', state: 'off', twins: [], what: WHAT_TAKE }
const sourceOn = {
  ...sourceOff,
  counts: counts({ off: 1, skills: 2 }),
  on: true,
  state: 'on',
  twins: [
    {
      id: SCOUT,
      name: 'Test Scout',
      skills: [
        skill({ label: 'shared with Surface', switch_label: 'trail-notes: share it', versions: [{ at: iso(90), label: 'version 2, sent out until today', vid: 'v2' }] }),
        skill({ group: null, key: 'knots', label: 'not shared', name: 'knots', on: false, state: 'off', switch_label: 'knots: share it', version: 1 })
      ]
    },
    { id: QUILL, name: 'Test Quill', skills: [] }
  ]
}
const conflict = skill({ choices: [{ keep: 'mine', label: "Keep this computer's" }, { keep: 'theirs', label: "Keep mini's" }], key: 'outdoors/camp-list', label: 'changed here and on mini', name: 'camp-list', state: 'conflict', switch_label: 'camp-list: follow it from mini' })
const followerOn = {
  ...followerOff,
  counts: counts({ changed_here: 1, conflicts: 1, only_here: 1, skills: 5 }),
  message: '1 skill was changed on both computers.',
  on: true,
  state: 'on',
  twins: [
    {
      id: SCOUT,
      name: 'Test Scout',
      skills: [
        skill({ versions: [{ at: iso(5), label: 'replaced when version 3 arrived from mini', vid: 'k-0a1b2c3d4e5f' }, { at: iso(600), label: 'replaced when version 2 arrived from mini', vid: 'k-112233445566' }] }),
        skill({ key: 'outdoors/weather', label: 'changed on this computer', name: 'weather', state: 'changed_here', switch_label: 'weather: follow it from mini', choices: [{ keep: 'theirs', label: "Put back mini's" }] }),
        conflict,
        skill({ group: null, key: 'knots', label: 'only on this computer', name: 'knots', on: false, state: 'only_here', switch_label: 'knots: follow it from mini' }),
        skill({ can_switch: false, group: null, key: 'atlas', label: 'too large to bring over', name: 'atlas', note: '2 files were left out: not text.', on: true, state: 'too_big', switch_label: 'atlas: follow it from mini' })
      ]
    }
  ]
}

// every fixture has the shape the backend's answers are held to (tests/contract/skills_shapes.json)
{
  const shapes = JSON.parse(readFileSync(resolve(here, '../contract/skills_shapes.json'), 'utf8')).shapes
  const fits = (value, type, where) => {
    const alts = type.split('|')

    return alts.some(t => {
      if (t === 'null') return value === null
      if (t === 'str') return typeof value === 'string'
      if (t === 'bool') return typeof value === 'boolean'
      if (t === 'int') return Number.isInteger(value)
      if (t.startsWith('list[')) return Array.isArray(value) && value.every((v, i) => fits(v, t.slice(5, -1), `${where}[${i}]`))
      const shape = shapes[t]

      if (!shape || !value || typeof value !== 'object' || Array.isArray(value)) return false

      const known = { ...shape.required, ...(shape.optional ?? {}) }

      return Object.keys(shape.required).every(k => k in value) && Object.keys(value).every(k => (k in known ? fits(value[k], known[k], `${where}.${k}`) : shape.closed !== true))
    })
  }

  for (const [name, fx] of Object.entries({ followerOff, followerOn, sourceOff, sourceOn })) {
    ok(fits(fx, 'SkillsSync', name), `${name} has the shape SkillsSync`)
  }
  ok(fits({ ...followerOn, undo: { skill: 'outdoors/camp-list', twin: SCOUT, vid: 'k-0a1b2c3d4e5f' } }, 'SkillsSync', 'undo') && fits({ ...followerOn, undo: null }, 'SkillsSync', 'undo') && !fits({ ...followerOn, extra: 1 }, 'SkillsSync', 'x') && !fits({ ...followerOn, on: 'yes' }, 'SkillsSync', 'x'), 'an answer after a choice carries undo; an unknown key or a wrong type is caught')
}

// ── 3. off: one switch and one sentence ─────────────────────────────────────

let html = render(sourceOff)
ok(H.switches.length === 1 && H.switches[0].label === SHARE && H.switches[0].checked === false && H.switches[0].id === 'gbt-skills' && H.switches[0].disabled === false, 'the sharing computer, off by default: one switch with its name, not checked')
ok(html.includes(SHARE) && html.includes(WHAT_SHARE) && H.buttons.length === 0 && !html.includes('Test Scout'), `off: the switch's words and what it would send, nothing else (${html})`)
ok(words(html) < 40 && sentences(html).every(s => words(s) <= 24), `off: under 40 words (${words(html)})`)
ok(H.queries.length === 1 && H.queries[0].queryKey[1] === 'fleet-skills' && H.queries[0].refetchInterval === 5000, 'one read of its own, every 5 s')

html = render(followerOff)
ok(H.switches.length === 1 && H.switches[0].label === 'Take skills from mini' && H.switches[0].checked === false && html.includes('Take skills from mini') && html.includes(WHAT_TAKE) && H.buttons.length === 0 && words(html) < 40, `a linked computer, off: its own switch, named after the computer the skills come from (${html})`)
ok(render({ ...followerOff, other: null }).includes('Take skills from your Mac'), 'the other computer has no name yet: still a whole sentence')
ok(render({ ...followerOff, message: 'mini does not share skills yet. Switch it on there.', state: 'off_there' }).includes('mini does not share skills yet. Switch it on there.'), 'the state, in the sentence the backend sends')

// nothing where there is nothing to say
ok(render(undefined) === '' && render({ ...sourceOff, role: 'none' }) === '' && render({ error: { code: 'internal' } }) === '' && render({ role: 'source' }) === '' && render(null) === '' && H.switches.length === 0, 'role none, an older backend, an error: nothing is drawn')

// ── 4. the switch ───────────────────────────────────────────────────────────

H.restCalls = []
H.setData = []
H.notes = []
H.restReply = async () => sourceOn
render(sourceOff)
await H.switches[0].onCheckedChange(true)
await tick()
ok(H.restCalls.length === 1 && H.restCalls[0].path === '/fleet/skills/switch' && H.restCalls[0].opts.method === 'POST' && JSON.stringify(H.restCalls[0].opts.body) === '{"on":true}', `switching on: one call (${JSON.stringify(H.restCalls)})`)
ok(H.setData.some(([k, v]) => k === 'fleet-skills' && v === sourceOn) && H.notes.length === 0, 'switching on: the new state is shown at once, without a toast')

H.restCalls = []
H.restReply = async () => followerOff
render(followerOn)
await H.switches[0].onCheckedChange(false)
await tick()
ok(H.restCalls.length === 1 && H.restCalls[0].path === '/fleet/skills/switch' && H.restCalls[0].opts.body.on === false && H.notes.length === 0, 'switching off: one call, no toast')

// a refusal is said, in the backend's words
H.restCalls = []
H.notes = []
H.restReply = async () => ({ error: { code: 'skills_off', hint: 'Switch it on there first.', message: 'mini does not share skills.' } })
render(followerOff)
await H.switches[0].onCheckedChange(true)
await tick()
ok(H.notes.length === 1 && H.notes[0].kind === 'error' && H.notes[0].message === 'mini does not share skills.' && H.notes[0].meta === 'Switch it on there first.' && H.notes[0].title === 'Could not switch this on', `a refusal: what was tried, what happened, what to do (${JSON.stringify(H.notes)})`)

// ── 5. on, the sharing computer: each skill with its own switch ─────────────

html = render(sourceOn)
ok(H.switches.length === 3 && H.switches[0].checked === true && H.switches[1].label === 'trail-notes: share it' && H.switches[1].checked === true && H.switches[2].label === 'knots: share it' && H.switches[2].checked === false, `on: the main switch and one per skill (${JSON.stringify(H.switches.map(s => s.label))})`)
ok(html.includes('Test Scout') && !html.includes('Test Quill') && html.includes('trail-notes') && html.includes('outdoors') && html.includes('Shared with Surface') && html.includes('Not shared'), `on: twins that have skills, each skill with its state (${html})`)
ok(new Set(H.switches.map(s => s.id)).size === 3 && H.switches.every(s => s.id && s.label), 'every switch has its own id and a spoken name')
ok(H.buttons.every(b => b.variant !== 'default'), 'no filled button: the page keeps its one main button')
ok(JSON.stringify(H.buttons.map(b => b.text)) === JSON.stringify(['Earlier versions (1)']) && !html.includes('version 2, sent out'), 'earlier versions are folded away')
ok(!/\bv2\b|t-0123|\d{4}-\d\d-\d\dT/.test(html), 'no id and no raw time stamp on the card')

H.restCalls = []
H.restReply = async () => sourceOn
await H.switches[2].onCheckedChange(true)
await tick()
ok(H.restCalls.length === 1 && H.restCalls[0].path === '/fleet/skills/item' && JSON.stringify(H.restCalls[0].opts.body) === JSON.stringify({ on: true, skill: 'knots', twin: SCOUT }), `a skill's switch: one call that names the twin and the skill (${JSON.stringify(H.restCalls)})`)

html = render({ ...sourceOn, twins: [{ id: SCOUT, name: 'Test Scout', skills: [] }] })
ok(html.includes('Your twins have no skills yet.') && H.switches.length === 1, 'on with no skill anywhere: one sentence')

// ── 6. on, a linked computer: states, the choice, earlier versions ──────────

html = render(followerOn)
ok(H.switches.length === 6 && H.switches[0].label === 'Take skills from mini' && H.switches[0].checked === true, 'a linked computer, on: the main switch and one per skill')
ok(html.includes('1 skill was changed on both computers.') && html.includes('Same as on mini') && html.includes('Changed on this computer') && html.includes('Changed here and on mini') && html.includes('Only on this computer') && html.includes('Too large to bring over') && html.includes('2 files were left out: not text.'), `each state in the backend's words (${html})`)
ok(H.switches[4].label === 'knots: follow it from mini' && H.switches[4].checked === false && H.switches[5].disabled === true && H.switches[1].disabled === false, 'a skill kept only here is switched off; one that cannot be switched says so to the switch')
ok(H.switches.slice(1).every(s => s.label.startsWith(s.label.split(':')[0]) && html.includes(s.label.split(':')[0])), 'each spoken name begins with the skill name on screen')
{
  const choices = H.buttons.filter(b => /^Keep |^Put back mini/.test(b.text))
  ok(JSON.stringify(choices.map(b => b.text)) === JSON.stringify(["Put back mini's", "Keep this computer's", "Keep mini's"]) && choices.every(b => b.variant === 'outline'), `the choice: two answers of the same weight, neither one filled (${JSON.stringify(H.buttons.map(b => [b.text, b.variant]))})`)
  ok(choices.every(b => b.label.startsWith(b.text)) && choices[1].label === "Keep this computer's version of camp-list", 'spoken names begin with the words on the button and say which skill')

  // keep this computer's: one call; nothing was replaced, so no Undo
  H.restCalls = []
  H.notes = []
  H.setData = []
  H.restReply = async () => ({ ...followerOn, undo: null })
  await choices[1].onClick()
  await tick()
  ok(H.restCalls.length === 1 && H.restCalls[0].path === '/fleet/skills/keep' && JSON.stringify(H.restCalls[0].opts.body) === JSON.stringify({ keep: 'mine', skill: 'outdoors/camp-list', twin: SCOUT }), `keep mine: one call (${JSON.stringify(H.restCalls)})`)
  ok(H.notes.length === 1 && H.notes[0].kind === 'success' && H.notes[0].message === "Kept this computer's version of camp-list" && H.notes[0].action === undefined && H.setData.length === 1, `keep mine: said once, nothing to undo (${JSON.stringify(H.notes)})`)

  // keep the other computer's: this computer's version is replaced and kept, and the notice offers Undo
  render(followerOn)
  const theirs = H.buttons.find(b => b.text === "Keep mini's")
  const undo = { skill: 'outdoors/camp-list', twin: SCOUT, vid: 'k-aabbccddeeff' }
  H.restCalls = []
  H.notes = []
  H.restReply = async () => ({ ...followerOn, undo })
  await theirs.onClick()
  await tick()
  ok(H.restCalls.length === 1 && H.restCalls[0].opts.body.keep === 'theirs' && H.notes.length === 1 && H.notes[0].message === "Kept mini's version of camp-list" && H.notes[0].action?.label === 'Undo' && H.notes[0].durationMs >= 10_000 && H.notes[0].meta === 'What it replaced is kept under Earlier versions.', `keep theirs: Undo, and where the replaced version is (${JSON.stringify(H.notes)})`)
  H.restCalls = []
  H.restReply = async () => followerOn
  await H.notes[0].action.onClick()
  await tick()
  ok(H.restCalls.length === 1 && H.restCalls[0].path === '/fleet/skills/restore' && JSON.stringify(H.restCalls[0].opts.body) === JSON.stringify(undo) && H.notes.length === 2 && H.notes[1].message === 'camp-list is as it was before' && H.notes[1].action === undefined, `Undo: one call with what the backend named, then said (${JSON.stringify(H.restCalls)})`)
  // an Undo that fails says so
  H.notes = []
  H.restReply = async () => ({ ...followerOn, undo })
  render(followerOn)
  await H.buttons.find(b => b.text === "Keep mini's").onClick()
  await tick()
  H.restReply = async () => ({ error: { code: 'unknown_version', message: 'That version is no longer kept.' } })
  await H.notes[0].action.onClick()
  await tick()
  ok(H.notes.length === 2 && H.notes[1].kind === 'error' && H.notes[1].message === 'That version is no longer kept.' && H.notes[1].title === 'Could not undo that', 'an Undo that fails says why')
  // an answer with a malformed undo offers none
  H.notes = []
  H.restReply = async () => ({ ...followerOn, undo: { skill: 'x', twin: SCOUT, vid: '../../etc' } })
  render(followerOn)
  await H.buttons.find(b => b.text === "Keep mini's").onClick()
  await tick()
  ok(H.notes.length === 1 && H.notes[0].action === undefined, 'an undo that names no version is not offered')
}

// earlier versions: folded, then each with Put back
html = render(followerOn)
ok(H.buttons.some(b => b.text === 'Earlier versions (2)') && !html.includes('replaced when version 3 arrived') && !H.buttons.some(b => b.text === 'Put back'), 'earlier versions: folded, with their number')
html = render(followerOn, { initialVersions: 'outdoors/trail-notes' })
{
  const backs = H.buttons.filter(b => b.text === 'Put back')
  ok(html.includes('Replaced when version 3 arrived from mini · 5 min ago') && html.includes('Replaced when version 2 arrived from mini') && backs.length === 2 && backs.every(b => b.variant === 'text' && b.label.startsWith('Put back trail-notes')) && !html.includes('k-0a1b'), `earlier versions, open: what each is, when, and Put back (${html.slice(0, 400)})`)
  H.restCalls = []
  H.notes = []
  const undo = { skill: 'outdoors/trail-notes', twin: SCOUT, vid: 'k-ffeeddccbbaa' }
  H.restReply = async () => ({ ...followerOn, undo })
  await backs[1].onClick()
  await tick()
  ok(H.restCalls.length === 1 && H.restCalls[0].path === '/fleet/skills/restore' && JSON.stringify(H.restCalls[0].opts.body) === JSON.stringify({ skill: 'outdoors/trail-notes', twin: SCOUT, vid: 'k-112233445566' }), `Put back: one call that names the version (${JSON.stringify(H.restCalls)})`)
  ok(H.notes.length === 1 && H.notes[0].message === 'Put back an earlier version of trail-notes' && H.notes[0].action?.label === 'Undo', 'Put back: said, with Undo')
  H.notes = []
  H.restReply = async () => ({ error: { code: 'write_failed', hint: 'Check that the disk is not full, then try again.', message: 'Hermes could not save that skill on this computer.' } })
  render(followerOn, { initialVersions: 'outdoors/trail-notes' })
  await H.buttons.filter(b => b.text === 'Put back')[0].onClick()
  await tick()
  ok(H.notes.length === 1 && H.notes[0].kind === 'error' && H.notes[0].title === 'Could not put that version back' && H.notes[0].message === 'Hermes could not save that skill on this computer.', 'Put back that fails: said in the backend words')
}

// a linked computer's skill switch: off means "only on this computer"
H.restCalls = []
H.restReply = async () => followerOn
render(followerOn)
await H.switches[1].onCheckedChange(false)
await tick()
ok(H.restCalls.length === 1 && H.restCalls[0].path === '/fleet/skills/item' && JSON.stringify(H.restCalls[0].opts.body) === JSON.stringify({ on: false, skill: 'outdoors/trail-notes', twin: SCOUT }), 'a skill switched off on a linked computer: one call')

html = render({ ...followerOn, message: '', twins: [] })
ok(html.includes('No skills have arrived yet.'), 'on, nothing arrived yet: one sentence')

// ── 7. what arrives is data ─────────────────────────────────────────────────

{
  const hostile = {
    ...followerOn,
    message: '<img src=x onerror=1>',
    twins: [
      { id: SCOUT, name: '<script>alert(1)</script>', skills: [skill({ label: '<b>bold</b>', name: 'a\u0000b‮c' }), skill({ key: '../../outside' }), skill({ key: 'ok-name', state: 'made-up', versions: [{ at: 'x', label: 'old', vid: '../x' }, { at: iso(1), label: 'fine', vid: 'v7' }], choices: [{ keep: 'both', label: 'Keep both' }, { keep: 'mine', label: '' }] }), null, 'x'] },
      { id: 'not-an-id', name: 'Nobody', skills: [skill()] },
      null
    ]
  }
  const raw = (() => {
    H.buttons = []
    H.switches = []
    H.skills = hostile

    return renderToStaticMarkup(jsx(P.FleetSkills, { initialVersions: 'ok-name' }))
  })()
  ok(!raw.includes('<script>') && !raw.includes('<img') && !raw.includes('<b>') && text(raw).includes('<script>alert(1)</script>') && !raw.includes('‮') && !raw.includes('Nobody') && !raw.includes('outside'), 'untrusted text is shown as text; a twin or skill with a malformed id is not shown')
  ok(H.switches.length === 3 && H.buttons.filter(b => b.text === 'Put back').length === 1 && !H.buttons.some(b => /Keep/.test(b.text)), 'a malformed version or choice is not offered')
  const v = P.skillsOf(hostile)
  ok(v.twins.length === 1 && v.twins[0].skills.length === 2 && v.twins[0].skills[1].state === 'error' && v.twins[0].skills[1].versions.length === 1, 'the tidied view keeps only what has the right form')
  ok(P.skillsOf({ ...followerOn, twins: 'x' }) === null && P.skillsOf('x') === null && P.skillsOf({ ...followerOn, role: 'other' }) === null, 'not an answer: no view')
}

// ── 8. words ────────────────────────────────────────────────────────────────

const all = [render(sourceOff), render(followerOff), render(sourceOn), render(followerOn, { initialVersions: 'outdoors/trail-notes' })].join(' ')

for (const w of ['token', 'profile', 'helper', 'machine', 'server', 'payload', 'config', 'json', 'follower', 'source', 'null', 'undefined', 'local-only', 'local only', 'digest', 'manifest', 'in step', 'restore point', 'mirror', 'by hand', 'k-0', 't-0']) {
  ok(!all.toLowerCase().includes(w), `no "${w}" on screen`)
}
ok(!/hermes twin|terminal|README/i.test(all), 'no command and no terminal as a remedy')
ok(!/[!]|please|simply|just |successfully|oops/i.test(all), 'short and plain: no exclamation mark, no filler')
ok([WHAT_SHARE, WHAT_TAKE, followerOn.message].every(t => sentences(t).every(s => words(s) <= 24)), 'no sentence over 24 words')

// What the page says elsewhere about what leaves a computer stays true with skills (off until switched on).
ok(P.SHARED_SENTENCE === "Sharing sends your twins' names, descriptions and the settings you let follow, and this computer's name; never chats or passwords, and skills only if switched on." && words(P.SHARED_SENTENCE) <= 26, 'the sentence beside the sharing switch names skills as a choice')
ok(P.PRIVACY.join(' ').includes("With skills switched on, it also sends your twins' skills (text files only) to your linked computers.") && P.PRIVACY.join(' ').includes('no other files, passwords or keys, ever leaves a computer.') && !/no instructions, files or passwords/.test(source), 'Privacy says the same')
// one line each in the two views, and nowhere at the level of the page (other sections live there)
ok((source.match(/jsx\(FleetSkills, \{\}\)/g) ?? []).length === 2 && /jsx\(KeptBots, \{ fleet \}\),\n\s*jsx\(FleetSkills, \{\}\)/.test(source), 'the section sits in the linked view after the kept bots, and in the sharing view')
ok(/jsx\(SwitchTarget, \{ children: jsx\(Switch, \{ 'aria-label': label, checked: view\.on[^\n]*id: 'gbt-skills'[^\n]*htmlFor: 'gbt-skills' \}\)/.test(source), 'the switch sits in a 28 px target that presses it')

rmSync(work, { force: true, recursive: true })
console.log(`skills check: ${passed} assertions passed`)
