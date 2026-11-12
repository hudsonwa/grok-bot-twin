// What syncs: the Twins page's panel of a twin's eight things and which way each goes, outside Hermes Desktop.
//
//   node tests/desktop/syncs.mjs [--modules DIR]
//
// DIR is a node_modules folder holding react + react-dom (19.x). A temp copy of desktop/plugin.js gets its three
// bare imports rewritten to local shims (a small stub of @hermes/plugin-sdk and the real React), as memory.mjs
// does; this file only knows the "What syncs" fold. Invented data only, and every invented answer is checked
// against tests/desktop/syncs_shapes.json first, so the page is never tested against an answer that cannot come.
//
// The render is a static one, so what a press changes cannot be read back from the screen. Each act is checked
// in two halves that share one object: the press is made and what it hands back (the sentence to say, or the
// problem) is looked at, and then the panel is drawn with exactly that.

import assert from 'node:assert/strict'
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const source = readFileSync(resolve(here, '../../desktop/plugin.js'), 'utf8')
const SHAPES = JSON.parse(readFileSync(resolve(here, 'syncs_shapes.json'), 'utf8')).shapes
const argModules = process.argv.indexOf('--modules')
const modulesDir = argModules > 0 ? resolve(process.argv[argModules + 1]) : resolve(here, '../../node_modules')

let passed = 0
const ok = (cond, msg) => {
  assert.ok(cond, msg)
  passed += 1
}

if (!existsSync(join(modulesDir, 'react/package.json')) || !existsSync(join(modulesDir, 'react-dom/package.json'))) {
  console.log('syncs check: skipped (no react / react-dom in the modules folder)')
  process.exit(0)
}

// ── 1. shims ────────────────────────────────────────────────────────────────

const work = mkdtempSync(join(tmpdir(), 'twin-syncs-'))
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
  Button: `({ children, disabled, loading, onClick, ...p }) => { H.buttons.push({ text: H.text(children), label: p['aria-label'], expanded: p['aria-expanded'], describedBy: p['aria-describedby'], onClick, disabled: Boolean(disabled || loading), variant: p.variant ?? 'default' }); return jsx('button', { disabled: Boolean(disabled || loading), 'aria-label': p['aria-label'], 'aria-expanded': p['aria-expanded'], 'aria-describedby': p['aria-describedby'], children }) }`,
  cn: `(...a) => a.flat(Infinity).filter(Boolean).join(' ')`,
  host: `H.host`,
  queryClient: `H.queryClient`,
  relativeTime: `ms => { const m = Math.round((Date.now() - ms) / 60000); return m < 1 ? 'just now' : m + ' min ago' }`,
  useQuery: `opts => H.useQuery(opts)`,
  useQueryClient: `() => H.queryClient`,
  PALETTE_AREA: `'palette'`,
  ROUTES_AREA: `'routes'`,
  SIDEBAR_NAV_AREA: `'sidebar.nav'`,
  STATUSBAR_AREAS: `{ left: 'statusBar.left', right: 'statusBar.right' }`
}
const pass = `({ children, className }) => jsx('div', { className, children })`
const OPTION_HEAD = 'function SyncOption({ id, name, choice, checked, disabled, why, onChoose }) {'

assert.ok(source.includes(OPTION_HEAD), 'desktop/plugin.js still has SyncOption as this check knows it')
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
    // every way drawn is recorded with what it was handed (its press among them)
    .replace(OPTION_HEAD, `function SyncOption(__p) { globalThis.__h.options.push(__p); return __SyncOption(__p) }\nfunction __SyncOption({ id, name, choice, checked, disabled, why, onChoose }) {`)
    .concat('\nexport const __syncs = { SyncsFold, SyncsPanel, syncPanelOf, CopiesPanel }\n')
)

// the page's clock stands at 09:03 on 1 Jan 2000, three minutes after the invented last sync
const NOW = Date.parse('2000-01-01T09:03:00Z')
Date.now = () => NOW

const H = (globalThis.__h = {
  buttons: [],
  options: [],
  notes: [],
  restCalls: [],
  restReply: async () => ({}),
  setData: [],
  invalidated: 0,
  panel: undefined,
  queries: [],
  text: c => (Array.isArray(c) ? c.map(H.text).join('') : typeof c === 'string' || typeof c === 'number' ? String(c) : c?.props?.children ? H.text(c.props.children) : ''),
  host: { notify: n => H.notes.push(n), navigate: () => {}, state: { profile: { get: () => 'default' }, gateway: { get: () => 'open' } } },
  queryClient: { getQueryData: () => undefined, setQueryData: (key, value) => H.setData.push([key[1], value]), invalidateQueries: async () => { H.invalidated += 1 } },
  useQuery: opts => {
    H.queries.push(opts)

    return { data: opts.queryKey[1] === 'sync-panel' && opts.enabled !== false ? H.panel : undefined, error: null, isError: false, isPending: false, refetch: async () => {} }
  }
})

const mod = await import(pathToFileURL(join(work, 'plugin.mjs')).href)
const P = mod.__syncs
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
const ACME = 'acme-support'
const RECIPE = 'recipe-planner'
const pairOf = (id, over = {}) => ({ pair_id: id, grok: { name: 'Acme Support' }, hermes: { profile: id }, ...over })
const render = (data, { pair = pairOf(ACME), initialOpen = true, initialSaid = null, onMirror = () => {} } = {}) => {
  H.buttons = []
  H.options = []
  H.queries = []
  H.panel = data
  markup = renderToStaticMarkup(jsx(P.SyncsFold, { initialOpen, initialSaid, onMirror, pair }))

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
const option = (key, direction) => H.options.find(o => o.name === `gbt-sync-${key}` && o.choice.direction === direction)
const optionsOf = key => H.options.filter(o => o.name === `gbt-sync-${key}`)
const between = (from, to) => markup.slice(markup.indexOf(from), to ? markup.indexOf(to, markup.indexOf(from) + 1) : undefined)

// ── 2. shapes (tests/desktop/syncs_shapes.json) ─────────────────────────────

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

const SCHEMA = 'grok-bot-twin.syncpanel.v1'
const LABELS = ['Name', 'Description', 'Instructions', 'Skills', 'Routines', 'Memory facts', 'Session briefs', 'Conversations (full)']
const KEYS = ['name', 'description', 'instructions', 'skills', 'routines', 'memory_facts', 'session_briefs', 'transcripts']
const WAYS = ['grok_to_hermes', 'hermes_to_grok', 'both', 'off']
const WAY_WORDS = ['Grok → Hermes', 'Hermes → Grok', 'Both ways', 'Off']
const MIRROR_WHY = 'Needs the Mirror, with your permission to change this bot.'
const NOT_YET_WHY = 'Not in this version yet.'
const SKILLS_WHY = 'To change this, open Skill sync… and turn it off first.'
const MIRROR_SENTENCE = 'The Mirror is not set up yet: it is the small bot in your Grok account that carries changes.'
const G2H = 'Changes made in Grok reach this twin; a change made in Hermes stays in Hermes.'
const OFF = 'Nothing moves either way.'
const BOTH = 'Skills changed on either side reach the other.'
const PRIVATE = 'Memory, session briefs and conversations start off for every twin, and only you switch them on.'
const AT = '2000-01-01T09:00:00Z'

const choice = (direction, state, sentence, why) => ({ direction, sentence, state, ...(why ? { why } : {}) })
const choices = states => WAYS.map((d, i) => {
  const [state, why] = states[i]

  return choice(d, state, `Choosing ${WAY_WORDS[i]} would do what it says.`, state === 'ok' ? undefined : why)
})
const item = (key, over = {}) => ({
  choices: choices([['ok'], ['not_yet', NOT_YET_WHY], ['not_yet', NOT_YET_WHY], ['not_yet', NOT_YET_WHY]]),
  direction: 'grok_to_hermes',
  key,
  label: LABELS[KEYS.indexOf(key)],
  last_direction: null,
  last_synced_at: null,
  private: false,
  sentence: G2H,
  state: 'ok',
  ...over
})
const privateItem = key => item(key, { choices: choices([['not_yet', NOT_YET_WHY], ['not_yet', NOT_YET_WHY], ['not_yet', NOT_YET_WHY], ['ok']]), direction: 'off', private: true, sentence: OFF, state: 'not_yet' })
const acmeItems = (over = {}) => [
  // name: Off can be chosen too, the two ways back need the Mirror; it last moved three minutes ago
  item('name', { choices: choices([['ok'], ['needs_mirror', MIRROR_WHY], ['needs_mirror', MIRROR_WHY], ['ok']]), last_direction: 'grok_to_hermes', last_synced_at: AT, state: 'needs_mirror', ...over.name }),
  item('description', { choices: choices([['ok'], ['needs_mirror', MIRROR_WHY], ['needs_mirror', MIRROR_WHY], ['not_yet', NOT_YET_WHY]]), note: 'A change made on both sides is held until you choose.', state: 'needs_mirror', ...over.description }),
  item('instructions', { choices: choices([['ok'], ['needs_mirror', MIRROR_WHY], ['needs_mirror', MIRROR_WHY], ['not_yet', NOT_YET_WHY]]), last_synced_at: AT, state: 'needs_mirror', ...over.instructions }),
  item('skills', { choices: choices([['not_yet', SKILLS_WHY], ['not_yet', SKILLS_WHY], ['ok'], ['not_yet', SKILLS_WHY]]), direction: 'both', last_direction: 'hermes_to_grok', last_synced_at: AT, sentence: BOTH, ...over.skills }),
  item('routines', { choices: choices([['not_yet', NOT_YET_WHY], ['not_yet', NOT_YET_WHY], ['not_yet', NOT_YET_WHY], ['ok']]), direction: 'off', sentence: OFF, state: 'not_yet' }),
  privateItem('memory_facts'),
  privateItem('session_briefs'),
  privateItem('transcripts')
]
const acme = (over = {}) => ({ id: ACME, items: acmeItems(over), mirror: 'not_set_up', name: 'Acme Support' })
const READ_FAILED = 'Could not read when the instructions of Recipe Planner last arrived.'
const recipe = {
  id: RECIPE,
  items: KEYS.map(k => (['memory_facts', 'session_briefs', 'transcripts'].includes(k) ? privateItem(k) : k === 'instructions' ? item(k, { error: { code: 'read_failed', message: READ_FAILED }, state: 'error' }) : item(k))),
  mirror: 'ready',
  name: 'Recipe Planner'
}
const panel = (twins = [acme(), recipe], over = {}) => ({ mirror: { sentence: MIRROR_SENTENCE, state: 'not_set_up' }, schema: SCHEMA, twins, ...over })
const base = panel()
const SAID = 'Name now stays where it is: nothing moves either way.'
const afterOff = { ...panel([acme({ name: { direction: 'off', sentence: OFF } }), recipe]), said: { sentence: SAID } }
const REFUSED = { code: 'not_available', message: MIRROR_WHY }

for (const [name, value] of Object.entries({ base, afterOff, empty: panel([]) })) {
  const probs = fits(value, 'SyncPanel', name)

  ok(probs.length === 0, `the invented answer "${name}" fits syncs_shapes.json: ${probs.join('; ')}`)
}
ok(fits({ ...base, extra: 1 }, 'SyncPanel', 'x').length === 1 && fits({ ...base, twins: [{ ...recipe, items: [{ ...recipe.items[0], label: 5 }] }] }, 'SyncPanel', 'x').length === 1, 'the shape checker itself catches a key too many and a wrong type')

// ── 4. the fold and its one read ────────────────────────────────────────────

let html = render(base, { initialOpen: false })
ok(html === 'What syncs' && JSON.stringify(H.buttons.map(b => [b.text, b.expanded])) === JSON.stringify([['What syncs', false]]) && H.options.length === 0, `folded by default: one quiet line, nothing under it (${html})`)
ok(H.queries.length === 1 && H.queries[0].queryKey.join('/') === 'grok-bot-twin/sync-panel' && H.queries[0].refetchInterval === 10_000 && H.queries[0].retry === 1, `one read of its own, again every 10 s, one retry (${JSON.stringify(H.queries.map(q => q.queryKey))})`)
quiet(async () => base)
await H.queries[0].queryFn()
ok(H.restCalls.length === 1 && H.restCalls[0].path === '/sync/panel' && H.restCalls[0].opts === undefined, `the read is GET /sync/panel (${JSON.stringify(H.restCalls)})`)
ok(render(undefined) === '' && render({ ...base, schema: 'grok-bot-twin.syncpanel.v0' }) === '' && render({ twins: base.twins }) === '' && render('junk') === '' && render({ error: { code: 'internal', message: 'x' } }) === '', 'before the first answer, on a problem, on another schema or an older backend: nothing at all')
ok(P.syncPanelOf({ ...base, schema: 'other' }) === null && P.syncPanelOf(null) === null && P.syncPanelOf({ schema: SCHEMA, twins: 'junk' }) === null, 'syncPanelOf: null for anything that is not this answer')
ok(render(base, { pair: pairOf('someone-else') }) === '' && render(panel([])) === '', 'a twin the answer does not know: no fold')
ok(render(base, { pair: pairOf(ACME, { sample: true }) }) === '' && H.queries[0].enabled === false, 'the sample: no fold, and no read')

// ── 5. the eight rows ───────────────────────────────────────────────────────

html = render(base)
const headings = [...markup.matchAll(/<h3[^>]*>([^<]*)<\/h3>/g)].map(m => m[1])
ok(JSON.stringify(headings) === JSON.stringify(LABELS), `all eight things, in order, each a heading (${JSON.stringify(headings)})`)
ok(H.options.length === 32 && KEYS.every(k => JSON.stringify(optionsOf(k).map(o => o.choice.direction)) === JSON.stringify(WAYS)), 'four ways for each, always in the same order')
ok(WAY_WORDS.every((w, i) => KEYS.every(k => markup.includes(`id="gbt-sync-${k}-${WAYS[i]}"`) && new RegExp(`for="gbt-sync-${k}-${WAYS[i]}"[^>]*><input[^>]*>${w}</label>`).test(markup))), 'each way is a radio button inside its own label, with its words')
// a radio group per row, named for the eye and the ear
const groups = [...markup.matchAll(/role="radiogroup"/g)].length
ok(groups === 8 && markup.includes('aria-label="Instructions for Acme Support: which way it syncs"') && markup.includes('aria-label="Conversations (full) for Acme Support: which way it syncs"'), `one radio group per row, each with a spoken name (${groups})`)
ok((markup.match(/<input[^>]*type="radio"[^>]*>/g) ?? []).length === 32 && KEYS.every(k => (markup.match(new RegExp(`name="gbt-sync-${k}"`, 'g')) ?? []).length === 4), 'native radio buttons under one name per row: Tab reaches the group, the arrow keys move in it')
// the never-ahead rule: checked is what the backend last said
ok(KEYS.every(k => optionsOf(k).filter(o => o.checked).length === 1) && option('name', 'grok_to_hermes').checked && option('skills', 'both').checked && option('routines', 'off').checked && option('transcripts', 'off').checked, 'checked: the way the backend says is in effect, one per row')
ok((markup.match(/<input[^>]*checked=""[^>]*>/g) ?? []).length === 8, 'the markup has eight checked radios')
// a way that cannot be chosen is switched off, and why is said in words under the group
ok(option('name', 'hermes_to_grok').disabled && option('name', 'both').disabled && !option('name', 'off').disabled && !option('name', 'grok_to_hermes').disabled, 'Name: the two ways back wait for the Mirror; Off can be chosen')
ok(option('routines', 'grok_to_hermes').disabled && option('memory_facts', 'both').disabled && !option('memory_facts', 'off').disabled, 'not in this version: switched off')
ok(between('gbt-sync-name-label', 'gbt-sync-description-label').includes(`Hermes → Grok, Both ways: ${MIRROR_WHY}`), `why, in words, for the eye and the ear (${text(between('gbt-sync-name-label', 'gbt-sync-description-label'))})`)
ok(text(between('gbt-sync-description-label', 'gbt-sync-instructions-label')).includes(`Hermes → Grok, Both ways: ${MIRROR_WHY}`) && text(between('gbt-sync-description-label', 'gbt-sync-instructions-label')).includes(`Off: ${NOT_YET_WHY}`), 'two reasons: a line each')
ok(text(between('gbt-sync-skills-label', 'gbt-sync-routines-label')).includes(`Grok → Hermes, Hermes → Grok, Off: ${SKILLS_WHY}`), "the backend's own reason, as it came")
ok(/id="gbt-sync-name-hermes_to_grok"/.test(markup) && /<input aria-describedby="gbt-sync-name-why-0"[^>]*id="gbt-sync-name-hermes_to_grok"/.test(markup) && markup.includes(`id="gbt-sync-name-why-0">Hermes → Grok, Both ways: ${MIRROR_WHY}</p>`), 'a way that cannot be chosen is described by its reason')
ok(!/title="[^"]*Needs the Mirror/.test(markup), 'the reason is never only a hover text')
// the row states
ok((markup.match(/<span class="shrink-0 rounded-\[3px\][^"]*">Needs the Mirror<\/span>/g) ?? []).length === 0, 'a row that syncs one way carries no "Needs the Mirror" tag: its why line says what waits')
{
  const offHtml = render(afterOff)
  const offMarkup = markup
  ok((offMarkup.match(/<span class="shrink-0 rounded-\[3px\][^"]*">Needs the Mirror<\/span>/g) ?? []).length === 1 && /Needs the Mirror/.test(offHtml), 'Needs the Mirror: a small tag on a row where nothing moves until the Mirror is there')
  html = render(base)
}
ok((markup.match(/<span class="shrink-0 rounded-\[3px\][^"]*">Not in this version yet<\/span>/g) ?? []).length === 4, 'Not in this version yet: a tag on routines and the three private rows')
// the Mirror, once for the panel
ok(button('Skill sync…').length === 1 && (html.match(new RegExp(MIRROR_SENTENCE.replace(/[.:]/g, '.'), 'g')) ?? []).length === 1 && button('Skill sync…')[0].describedBy === 'gbt-sync-mirror' && button('Skill sync…')[0].variant === 'secondary', `the Mirror: one sentence and one button for the whole panel (${JSON.stringify(H.buttons.map(b => b.text))})`)
{
  let pressed = 0

  render(base, { onMirror: () => { pressed += 1 } })
  button('Skill sync…')[0].onClick()
  ok(pressed === 1, "Skill sync… opens the twin's skill sync set-up")
  html = render(base, { onMirror: null })
  ok(button('Skill sync…').length === 0 && html.includes("Set it up with Skill sync… in this twin's menu."), 'no way to open it from here: a sentence that says where it is')
}
ok(!render(base, { pair: pairOf(RECIPE) }).includes(MIRROR_SENTENCE) && button('Skill sync…').length === 0, 'a twin whose Mirror is ready: no word about setting it up')
// when, and what the way in effect means
html = render(base)
ok(text(between('gbt-sync-name-label', 'gbt-sync-description-label')).includes('Last synced 3 min ago, Grok → Hermes') && text(between('gbt-sync-instructions-label', 'gbt-sync-skills-label')).includes('Last synced 3 min ago') && !text(between('gbt-sync-instructions-label', 'gbt-sync-skills-label')).includes('3 min ago,'), 'last synced: when, and which way when the backend says')
ok(text(between('gbt-sync-skills-label', 'gbt-sync-routines-label')).includes('Last synced 3 min ago, Hermes → Grok') && text(between('gbt-sync-description-label', 'gbt-sync-instructions-label')).includes('Not synced yet') && (html.match(/Not synced yet/g) ?? []).length === 5, 'never synced: "Not synced yet"')
ok(text(between('gbt-sync-name-label', 'gbt-sync-description-label')).includes(G2H) && text(between('gbt-sync-skills-label', 'gbt-sync-routines-label')).includes(BOTH) && text(between('gbt-sync-routines-label', 'gbt-sync-memory_facts-label')).includes(OFF), "the way in effect, in the backend's one sentence")
ok(text(between('gbt-sync-description-label', 'gbt-sync-instructions-label')).includes('A change made on both sides is held until you choose.'), 'a note, when the backend has one')
ok(markup.includes(`title="Choosing Both ways would do what it says."`), "each way's own sentence is its hover text (extra, never the only place a reason is)")
// private rows: one line, once, before them
ok((html.match(new RegExp(PRIVATE.replace(/\./g, '\\.'), 'g')) ?? []).length === 1 && html.indexOf(PRIVATE) > html.indexOf('Routines') && html.indexOf(PRIVATE) < html.indexOf('Memory facts'), `the private rows: one plain line, once, just before them (${html.slice(html.indexOf('Routines'), html.indexOf('Memory facts') + 20)})`)
// an error row
html = render(base, { pair: pairOf(RECIPE) })
ok(html.includes(READ_FAILED) && (markup.match(/role="alert"/g) ?? []).length === 1 && text(between('gbt-sync-instructions-label', 'gbt-sync-skills-label')).includes(READ_FAILED), 'a row with a problem: what happened, as an alert, in that row')
ok(render(panel([{ ...recipe, items: [item('name', { state: 'error' })] }]), { pair: pairOf(RECIPE) }).includes('Something went wrong inside Grok Bot Twin.'), 'a problem without words: the page says so plainly')
ok(!html.includes(PRIVATE.slice(0, 10) + 'x') && !render(panel([{ ...recipe, items: [item('name')] }]), { pair: pairOf(RECIPE) }).includes(PRIVATE), 'no private rows: no line about them')

// ── 6. choosing a way ───────────────────────────────────────────────────────

render(base)
quiet(async () => afterOff)
let said = await option('name', 'off').onChoose('off')
await tick()
ok(H.restCalls.length === 1 && H.restCalls[0].path === '/sync/panel' && H.restCalls[0].opts.method === 'POST' && bodyOf(H.restCalls[0]) === JSON.stringify({ twin: ACME, item: 'name', direction: 'off' }), `a press: one POST with exactly the twin, the thing and the way (${JSON.stringify(H.restCalls)})`)
ok(H.setData.length === 1 && H.setData[0][0] === 'sync-panel' && H.setData[0][1] === afterOff && H.notes.length === 0 && H.invalidated === 0, 'the answer that came back is shown at once, without a toast')
ok(said.item === 'name' && said.said === SAID && !said.problem, "the row is handed the backend's sentence")
html = render(afterOff, { initialSaid: said })
ok(option('name', 'off').checked && !option('name', 'grok_to_hermes').checked && markup.includes(`role="status">${SAID}</p>`) && (markup.match(/role="status"/g) ?? []).length === 1 && text(between('gbt-sync-name-label', 'gbt-sync-description-label')).includes(SAID), `chosen: the way the answer has, and the sentence as a status line in that row (${html})`)
// a refusal: said in that row only, in the backend's words, and nothing moves
render(base)
quiet(async () => ({ error: REFUSED }))
said = await option('name', 'off').onChoose('off')
await tick()
ok(H.restCalls.length === 1 && H.setData.length === 0 && H.invalidated === 0 && H.notes.length === 0, 'a refusal: nothing shown as changed, no toast')
ok(said.item === 'name' && said.problem?.message === MIRROR_WHY && said.problem?.code === 'not_available', 'a refusal: the row is handed what the backend said')
html = render(base, { initialSaid: said })
ok((markup.match(/role="alert"/g) ?? []).length === 1 && between('gbt-sync-name-label', 'gbt-sync-description-label').includes('role="alert"') && option('name', 'grok_to_hermes').checked && !option('name', 'off').checked, 'a refusal: one alert, in that row only, and the way stays where it was')
// the call never reached the plugin
render(base)
quiet(async () => { throw new Error('socket hang up') })
said = await option('name', 'off').onChoose('off')
await tick()
ok(H.notes.length === 0 && said.problem && render(base, { initialSaid: said }).includes('Hermes could not reach Grok Bot Twin.'), 'the plugin cannot be reached: said in the row')
// an answer that is not this one is never shown as one
render(base)
quiet(async () => ({}))
said = await option('name', 'off').onChoose('off')
await tick()
ok(H.setData.length === 0 && H.invalidated === 1 && said.said === '', 'an empty answer: nothing is made up, the panel reads again')
// a way that cannot be chosen: switched off in the browser, and the page itself sends nothing for it either
render(base)
ok(option('name', 'both').disabled === true && option('routines', 'grok_to_hermes').disabled === true, 'disabled ways are disabled')
ok(!/disabled=""[^>]*id="gbt-sync-name-off"/.test(markup) && /<input[^>]*disabled=""[^>]*id="gbt-sync-name-both"/.test(markup), 'the markup agrees')
quiet(async () => afterOff)
said = await option('name', 'both').onChoose('both')
await option('routines', 'grok_to_hermes').onChoose('grok_to_hermes')
await tick()
ok(H.restCalls.length === 0 && H.setData.length === 0 && !said, 'a way that cannot be chosen, pressed all the same: nothing is posted')

// ── 7. everything shown is text ─────────────────────────────────────────────

{
  const rude = '<script>alert(1)</script>'
  const bidi = 'Acme\u202eSupport\u2066x'
  const long = 'y'.repeat(5000)
  const hostile = panel([
    {
      ...acme(),
      items: [
        { ...acmeItems()[0], label: `<b>Name</b>`, note: long, sentence: `${rude} ${bidi}` },
        { ...acmeItems()[1], direction: 'sideways', choices: [choice('sideways', 'ok', 'x'), choice('both', 'maybe', 'y'), 'junk', null] },
        { ...acmeItems()[2], state: 'weird', last_synced_at: 'not a date', last_direction: 'up' },
        { key: 'secrets', label: 'Secrets', direction: 'both', state: 'ok', choices: [] },
        acmeItems()[2],
        ...acmeItems().slice(3)
      ],
      name: `<i>Acme</i> ${bidi} ${long}`,
      unknown: 'x'
    },
    { ...recipe, id: 'r\n1' },
    { ...recipe, id: 'x'.repeat(121) },
    { ...recipe, id: 7 },
    'junk',
    null
  ], { mirror: { sentence: rude, state: 'odd' } })

  html = render(hostile)
  const tidy = P.syncPanelOf(hostile)
  ok(!/<script|<b>|<i>/.test(markup), 'no text from the backend becomes markup')
  ok(!/[\u202a-\u202e\u2066-\u2069]/.test(markup), 'bidi controls are taken out')
  ok(tidy.twins.length === 1 && tidy.twins[0].name.length <= 60 && tidy.twins[0].items.every(i => i.note.length <= 300 && i.sentence.length <= 300) && tidy.mirror.state === 'unknown', 'a long name or note is capped; a twin id that cannot go back as it came is left out; an odd Mirror state is unknown')
  ok(JSON.stringify(tidy.twins[0].items.map(i => i.key)) === JSON.stringify(KEYS) && !html.includes('Secrets'), 'an unknown thing is left out; the first of each known one is kept, in the fixed order')
  ok(tidy.twins[0].items[1].direction === null && optionsOf('description').every(o => !o.checked) && optionsOf('description').every(o => o.disabled) && tidy.twins[0].items[1].choices.every(c => c.state === 'not_yet' && c.why === 'Not in this version yet.'), 'an unknown way: nothing checked; a way not offered as it should be cannot be chosen, and says so')
  ok(tidy.twins[0].items[2].state === 'error' && tidy.twins[0].items[2].lastAt === null && tidy.twins[0].items[2].lastWay === null, 'an unknown row state is a problem; a time that is no time is no time')
  ok(html.includes('<b>Name</b>') && html.includes(`${rude}`), `markup in a label or a sentence is shown as the text it is`)
  ok(markup.includes('aria-label="&lt;b&gt;Name&lt;/b&gt; for &lt;i&gt;Acme&lt;/i&gt; Acme Support x yyy'), 'the spoken names use the clean text too')
  ok(render(panel([{ ...acme(), items: 'junk' }])) === '' && render(panel([{ ...acme(), items: [] }])) === '', 'a twin with no rows: no fold')
}

// ── 8. inside the twin's Details ────────────────────────────────────────────

{
  const draw = (pair, data) => {
    H.buttons = []
    H.options = []
    H.queries = []
    H.panel = data
    markup = renderToStaticMarkup(jsx(P.CopiesPanel, { fastCheck: false, intervalS: 60, onSkillSync: () => {}, open: true, pair }))

    return text(markup)
  }

  html = draw(pairOf(ACME), base)
  const names = H.buttons.map(b => b.text)
  ok(names.includes('What syncs') && names.indexOf('What syncs') === names.indexOf('The whole table') + 1 && H.options.length === 0, `Details: a folded "What syncs" just under the whole table (${JSON.stringify(names)})`)
  ok(!draw(pairOf(ACME, { sample: true }), base).includes('What syncs') && !draw(pairOf(ACME), undefined).includes('What syncs') && !draw(pairOf(ACME), { ...base, schema: 'x' }).includes('What syncs'), 'Details: not on the sample, nor on a backend without this answer')
}

// ── 9. words ────────────────────────────────────────────────────────────────

{
  const all = [render(base), render(base, { pair: pairOf(RECIPE) }), render(afterOff, { initialSaid: { item: 'name', said: SAID } })].join(' ')

  for (const w of ['grok_to_hermes', 'hermes_to_grok', 'needs_mirror', 'not_yet', 'memory_facts', 'acme-support', 'undefined', 'null', '[object', '2000-01-01T', 'source', 'follower', 'token', ' rev ']) {
    ok(!all.includes(w), `no "${w}" on screen`)
  }
  ok(!/[!]|please|simply|just |successfully|oops/i.test(all), 'short and plain: no exclamation mark, no filler')
  render(base)
  ok(H.buttons.every(b => b.variant !== 'default'), 'nothing in the panel is a main button')
}

console.log(`syncs check: ${passed} assertions passed`)
