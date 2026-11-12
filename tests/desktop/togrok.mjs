// Hermes → Grok: the Twins page's sheet for sending a twin's own changes to the bot in Grok, outside Hermes Desktop.
//
//   node tests/desktop/togrok.mjs [--modules DIR]
//
// DIR is a node_modules folder holding react + react-dom (19.x). A temp copy of desktop/plugin.js gets its three
// bare imports rewritten to local shims, as syncs.mjs does. Invented bots and data only, and every invented answer
// is checked against tests/desktop/togrok_shapes.json first (written from the plugin's to-grok routes).
//
// The render is a static one: each press is made through the sheet's own acts (toGrokActs), what they hand back is
// looked at, and the sheet is then drawn with exactly that.

import assert from 'node:assert/strict'
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const source = readFileSync(resolve(here, '../../desktop/plugin.js'), 'utf8')
const SHAPES = JSON.parse(readFileSync(resolve(here, 'togrok_shapes.json'), 'utf8')).shapes
const argModules = process.argv.indexOf('--modules')
const modulesDir = argModules > 0 ? resolve(process.argv[argModules + 1]) : resolve(here, '../../node_modules')

let passed = 0
const ok = (cond, msg) => {
  assert.ok(cond, msg)
  passed += 1
}

if (!existsSync(join(modulesDir, 'react/package.json')) || !existsSync(join(modulesDir, 'react-dom/package.json'))) {
  console.log('togrok check: skipped (no react / react-dom in the modules folder)')
  process.exit(0)
}

// ── 1. shims ────────────────────────────────────────────────────────────────

const work = mkdtempSync(join(tmpdir(), 'twin-togrok-'))
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
  STATUSBAR_AREAS: `{ left: 'statusBar.left', right: 'statusBar.right' }`,
  CopyButton: `({ label, text }) => { H.copies.push({ label, text }); return jsx('button', { 'data-copy': 'true', children: label }) }`
}
const pass = `({ children, className }) => jsx('div', { className, children })`

writeFileSync(join(work, 'sdk.mjs'), `import { jsx } from './jsx.mjs'\nconst H = globalThis.__h\n` + sdkNames.map(n => `export const ${n} = ${special[n] ?? pass}`).join('\n') + '\n')
writeFileSync(
  join(work, 'plugin.mjs'),
  source
    .replace(/from\s*'@hermes\/plugin-sdk'/, `from './sdk.mjs'`)
    .replace(/from\s*'react\/jsx-runtime'/, `from './jsx.mjs'`)
    .replace(/from\s*'react'/, `from './react.mjs'`)
    .concat('\nexport const __togrok = { ToGrokSheet, toGrokActs, toGrokOf, CopiesPanel }\n')
)

// the page's clock stands at 09:03 on 1 Jan 2000
const NOW = Date.parse('2000-01-01T09:03:00Z')
Date.now = () => NOW

const H = (globalThis.__h = {
  buttons: [],
  copies: [],
  notes: [],
  restCalls: [],
  restReply: async () => ({}),
  text: c => (Array.isArray(c) ? c.map(H.text).join('') : typeof c === 'string' || typeof c === 'number' ? String(c) : c?.props?.children ? H.text(c.props.children) : ''),
  host: { notify: n => H.notes.push(n), navigate: () => {}, state: { profile: { get: () => 'default' }, gateway: { get: () => 'open' } } },
  queryClient: { getQueryData: () => undefined, setQueryData: () => {}, invalidateQueries: async () => {} },
  useQuery: () => ({ data: undefined, error: null, isError: false, isPending: false, refetch: async () => {} })
})

const mod = await import(pathToFileURL(join(work, 'plugin.mjs')).href)
const P = mod.__togrok
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

const text = html => html.replace(/<[^>]+>/g, ' ').replace(/&#x27;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, '&').replace(/&gt;/g, '>').replace(/&lt;/g, '<').replace(/\s+/g, ' ').trim()
let markup = ''
const ACME = 'acme-helper'
const pair = { pair_id: ACME, grok: { name: 'Acme Helper' }, hermes: { profile: ACME } }
const render = (info, props = {}) => {
  H.buttons = []
  H.copies = []
  markup = renderToStaticMarkup(jsx(P.ToGrokSheet, { initialInfo: info, onClose: () => {}, open: true, pair, ...props }))

  return text(markup)
}
const button = name => H.buttons.find(b => b.text === name)
const quiet = reply => {
  H.restCalls = []
  H.notes = []
  H.restReply = reply
}
// an HTTP refusal as the Desktop's rest door throws it
const refusal = (code, message = 'Refused.') => {
  throw new Error(`HTTP 409: ${JSON.stringify({ detail: { code, message } })}`)
}
// the patches the acts hand the sheet, collected into one state
const actsFor = () => {
  const state = { patches: [] }
  const acts = P.toGrokActs(ACME, patch => {
    state.patches.push(patch)
    Object.assign(state, patch)
  })

  return { acts, state }
}

// ── 2. shapes ───────────────────────────────────────────────────────────────

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

const AT = '2000-01-01T09:00:00Z'
const RULE = 'Allow the Mirror to edit the bot "Acme Helper": name, description, instructions, routines'
const WHERE = 'Grok: Settings > Permissions > Auto-Review > Allow without Asking > Add rule'
const STEPS = 'Step 1. Open the Mirror\'s chat.\nStep 2. Paste this whole text and send it.'
const XSS = '<img src=x onerror=alert(1)>'
const LONG = Array.from({ length: 100 }, (_, i) => `Line ${String(i).padStart(3, '0')}: be kind to every Acme customer, and answer in full.`).join('\n')
const LONG_AFTER = `${LONG}\nLine 100: the new last line.`
const PROMPT = `Write the daily recipe plan for the household. ${'Use what is in the pantry first. '.repeat(40)}End with the shopping list.`
const CID = 'c-0123456789abcdef'
const CID2 = 'c-fedcba9876543210'
const DONE_ID = 'c-1111222233334444'

const routine = (over = {}) => ({ name: 'Morning recipes', schedule: '0 9 * * *', schedule_words: 'every day at 09:00 UTC', enabled: true, prompt: PROMPT, ...over })
const preview = (over = {}) => ({
  change_id: CID,
  nothing: false,
  sentence: 'Three changes are ready to make in Grok.',
  permission: 'ready',
  changes: [
    { item: 'name', before: 'Acme Helper', after: `Acme Helper ${XSS}` },
    { item: 'instructions', before: LONG, after: LONG_AFTER, diff: [...LONG.split('\n').map(l => ` ${l}`), '-Line 099: old ending.', '+Line 100: the new last line.'] },
    { item: 'routine:morning', before: routine({ enabled: false, prompt: 'Old prompt.' }), after: routine(), note: 'It stays paused in Grok until you switch it on there.' }
  ],
  held: [{ item: 'description', code: 'secret', why: 'It looks like it holds a password, so it is not sent.' }],
  off: [{ item: 'routines', why: 'You have not allowed routines for this twin.' }],
  ...over
})
const change = (over = {}) => ({ id: DONE_ID, at: AT, state: 'done', items: ['name', 'instructions'], undo_of: null, undone_by: null, message: 'Grok took both changes.', can_undo: true, kind: 'change', ...over })
const info = (over = {}) => ({
  pair_id: ACME,
  allowed: ['name', 'instructions', 'routines'],
  bot_known: true,
  writer: 'mirror',
  permission: 'ready',
  grant: { items: ['name', 'instructions', 'routines'], at: AT, rule: RULE, where: WHERE },
  in_grok: { allow_rule: RULE, where: WHERE, mirror_steps: STEPS },
  runner: { morning: 'grok' },
  changes: [],
  preview: preview(),
  ...over
})
const offInfo = info({ allowed: [], grant: null, preview: undefined })
delete offInfo.preview
const REVOKE = { rule: RULE, where: 'Grok: Settings > Permissions > Auto-Review > Allow without Asking', sentence: 'Nothing more goes to Grok for this twin. Delete the rule in Grok too.', forgot: 1 }
const result = (over = {}) => ({ state: 'done', change_id: CID, items: ['name', 'instructions', 'routine:morning'], message: 'Grok took all three changes.', sentence: 'Made in Grok: name, instructions and one routine.', ...over })

for (const [name, value, type] of [
  ['info', info(), 'Info'],
  ['off', offInfo, 'Info'],
  ['history', info({ changes: [change(), change({ id: CID2, can_undo: false, undo_why: 'Grok changed the name again since, so it is not put back.' })] }), 'Info'],
  ['revoke', { ...offInfo, revoke: REVOKE }, 'Info'],
  ['previewError', { error: { code: 'no_permission', message: 'Not allowed in Grok.' } }, 'PreviewError'],
  ['result', result(), 'Result'],
  ['giveUp', { state: 'given_up', sentence: 'Stopped waiting. Nothing more is sent for that change.' }, 'GiveUp']
]) {
  const probs = fits(value, type, name)

  ok(probs.length === 0, `the invented answer "${name}" fits togrok_shapes.json: ${probs.join('; ')}`)
}
ok(fits({ ...info(), extra: 1 }, 'Info', 'x').length === 1, 'the shape checker itself catches a key too many')

// ── 4. off by default ───────────────────────────────────────────────────────

let html = render(offInfo)
ok(html.includes('Nothing goes from this twin to Grok until you allow it.'), `off: says nothing goes until allowed (${html.slice(0, 200)})`)
ok(['name', 'description', 'instructions', 'routines'].every(k => new RegExp(`<label[^>]*for="gbt-tg-${k}"[^>]*><input id="gbt-tg-${k}" type="checkbox"/>`).test(markup)), 'four checkboxes, each in its own label, none ticked')
ok(!markup.includes('checked=""'), 'off: nothing ticked')
ok(!button('Make these changes in Grok') && !html.includes('What would change in Grok') && !html.includes('Stop changes in Grok'), 'off: no preview, no Apply, no Stop')
ok(button('Allow')?.disabled === true && button('Allow').variant === 'default', 'off: Allow waits for a tick, and is the main button')
// even an answer that wrongly carried a preview shows none while nothing is allowed
ok(!render({ ...offInfo, preview: preview() }).includes('What would change in Grok') && !button('Make these changes in Grok'), 'off: a preview in the answer is not shown')
html = render(offInfo, { initialTicked: ['name', 'routines'] })
ok(button('Allow')?.disabled === false && (markup.match(/checked=""/g) ?? []).length === 2, 'ticked: Allow can be pressed')

// the sheet reads INFO when it opens (and only that)
{
  const { acts, state } = actsFor()

  quiet(async () => offInfo)
  await acts.read()
  ok(H.restCalls.length === 1 && H.restCalls[0].path === `/pairs/${ACME}/to-grok` && H.restCalls[0].opts === undefined && state.info === offInfo, `opening reads GET /pairs/{pid}/to-grok (${JSON.stringify(H.restCalls)})`)
}

// ── 5. allow, stop ──────────────────────────────────────────────────────────

{
  const { acts, state } = actsFor()

  quiet(async () => info({ allowed: ['name', 'routines'] }))
  await acts.allow(['routines', 'name'], [])
  ok(H.restCalls.length === 1 && H.restCalls[0].path === `/pairs/${ACME}/to-grok/allow` && H.restCalls[0].opts.method === 'POST' && JSON.stringify(H.restCalls[0].opts.body) === '{"items":["name","routines"]}', `Allow posts exactly the ticked items (${JSON.stringify(H.restCalls)})`)
  ok(state.said === 'Allowed for this twin: name and routines.' && state.info.allowed.length === 2, `Allow: said in words (${state.said})`)

  quiet(async path => (path.endsWith('disallow') ? info({ allowed: ['name'] }) : info({ allowed: ['name', 'instructions'] })))
  await acts.allow(['name'], ['name', 'instructions'])
  ok(H.restCalls.length === 2 && H.restCalls[1].path.endsWith('/to-grok/disallow') && JSON.stringify(H.restCalls[1].opts.body) === '{"items":["instructions"]}', `an unticked item is disallowed (${JSON.stringify(H.restCalls.map(c => [c.path, c.opts.body]))})`)

  quiet(async () => ({ ...offInfo, revoke: REVOKE }))
  await acts.stop()
  ok(H.restCalls.length === 1 && H.restCalls[0].path.endsWith('/to-grok/disallow') && JSON.stringify(H.restCalls[0].opts.body) === '{"items":"all"}' && state.revoke === REVOKE, 'Stop changes in Grok disallows all')
  html = render(state.info, { initialRevoke: state.revoke, initialSaid: state.said })
  ok(html.includes('Delete this rule in Grok') && html.includes(RULE) && html.includes(REVOKE.where) && html.includes(REVOKE.sentence) && H.copies.some(c => c.text === RULE && c.label === 'Copy the rule'), `after Stop: the rule to delete, where, and Copy (${html.slice(0, 300)})`)
  ok(markup.includes('role="status"') && html.includes('Nothing goes from this twin to Grok any more.'), 'after Stop: said in the status line')
}

// ── 6. the one-time set-up in Grok ──────────────────────────────────────────

html = render(info({ permission: 'not_set_up', preview: preview({ permission: 'not_set_up', blocked: 'Grok needs the rule above first.' }) }), { initialSteps: true })
ok(html.includes('Grok needs this once') && html.includes(RULE) && html.includes(`Where: ${WHERE}`) && H.copies.some(c => c.text === RULE), 'not set up: the exact rule, where, and Copy')
ok(html.includes("Step 1. Open the Mirror's chat.") && H.copies.some(c => c.text === STEPS && c.label === 'Copy the steps') && markup.includes('Step 1. Open the Mirror&#x27;s chat.\nStep 2.'), 'the Mirror steps, whole, with their own Copy')
ok(button('Make these changes in Grok')?.disabled === true && html.includes('Grok needs the rule above first.') && markup.includes('aria-describedby="gbt-tg-blocked"'), 'blocked: Apply is there but waits, with the reason beside it')
render(info({ permission: 'not_set_up' }))
ok(button("Steps for the Mirror's chat")?.expanded === false && !markup.includes('Step 1.'), 'the steps are folded at first')
html = render(info({ permission: 'no_permission' }))
ok(html.includes('Grok did not allow the last change.'), 'no permission: said so')
ok(!render(info()).includes('Grok needs this once'), 'ready: no set-up')

// ── 7. the preview ──────────────────────────────────────────────────────────

html = render(info())
ok(html.includes('What would change in Grok') && html.includes('Three changes are ready to make in Grok.'), 'the preview and its sentence')
ok(markup.includes(LONG.replace(/'/g, '&#x27;')) && markup.includes(LONG_AFTER.replace(/'/g, '&#x27;')) && LONG.length > 5000, `long instructions in full, before and after (${LONG.length} chars), not cut`)
ok((markup.match(/data-line="added"/g) ?? []).length === 1 && (markup.match(/data-line="removed"/g) ?? []).length === 1 && (markup.match(/data-line="same"/g) ?? []).length === 100, 'the diff: every line, added and removed marked')
ok(markup.includes('<span class="sr-only">added: </span>+Line 100: the new last line.') && markup.includes('<span class="sr-only">removed: </span>-Line 099: old ending.'), 'the diff: "added" and "removed" for the ear')
ok(markup.includes(PROMPT) && html.includes('Morning recipes') && html.includes('Runs every day at 09:00 UTC') && html.includes('On in Grok') && html.includes('Paused in Grok') && html.includes('Old prompt.'), 'a routine: name, schedule in words, on or paused, the whole prompt')
ok(html.includes('It stays paused in Grok until you switch it on there.'), 'a change\'s note')
ok(html.includes('Not sent Description: It looks like it holds a password, so it is not sent.') && html.includes('Not allowed for this twin Routines: You have not allowed routines for this twin.'), 'held and off, each with why')
ok(button('Make these changes in Grok')?.disabled === false && button('Make these changes in Grok').variant === 'default', 'Apply: one main button, ready')
ok(/max-h-48[^"]*"[^>]*role="group" tabindex="0"/.test(markup), 'long text sits in a box that scrolls and the keyboard can reach')
render(info({ preview: preview({ nothing: true, changes: [], sentence: 'Grok already has everything.' }) }))
ok(!button('Make these changes in Grok') && text(markup).includes('Grok already has everything.'), 'nothing to change: no Apply')
html = render(info({ preview: { error: { code: 'no_permission', message: 'raw message no_permission' } } }))
ok(html.includes('Grok has not given permission for this yet.') && !html.includes('no_permission') && button('Look again'), `a preview that failed: owner words and a way to look again (${html.slice(0, 200)})`)

// ── 8. apply, stale, pending ────────────────────────────────────────────────

{
  const { acts, state } = actsFor()
  const fresh = info({ changes: [change({ id: CID })], preview: preview({ nothing: true, changes: [], held: [], off: [] }) })

  quiet(async path => (path.endsWith('/apply') ? result() : fresh))
  await acts.apply(CID)
  ok(H.restCalls.length === 2 && H.restCalls[0].path === `/pairs/${ACME}/to-grok/apply` && JSON.stringify(H.restCalls[0].opts.body) === `{"change_id":"${CID}"}` && H.restCalls[1].path === `/pairs/${ACME}/to-grok`, `Apply posts exactly the preview's change_id, then reads again (${JSON.stringify(H.restCalls.map(c => [c.path, c.opts?.body]))})`)
  ok(state.said === 'Made in Grok: name, instructions and one routine.' && state.info === fresh && !state.problem, 'Apply: said in the backend\'s sentence')
  ok(H.notes.length === 0, 'no toast: said inside the sheet')

  // the preview moved on while the owner looked
  const newer = info({ preview: preview({ change_id: CID2, sentence: 'One change is ready to make in Grok.' }) })

  quiet(async path => (path.endsWith('/apply') ? refusal('stale', 'change_id c-0123456789abcdef is stale') : newer))
  await acts.apply(CID)
  ok(H.restCalls.length === 2 && H.restCalls[1].path === `/pairs/${ACME}/to-grok` && state.info === newer && state.said === 'Something changed since you looked; here is the new preview.' && !state.problem, 'stale: the new preview is read and said so')
  html = render(state.info, { initialSaid: state.said })
  ok(html.includes('Something changed since you looked; here is the new preview.') && html.includes('One change is ready to make in Grok.') && !html.includes('c-0123'), 'stale: the new preview is shown, no id')

  // a change already waiting
  quiet(async path => (path.endsWith('/apply') ? refusal('pending') : info({ preview: preview({ pending: CID }) })))
  await acts.apply(CID2)
  ok(state.said.startsWith("The last change is still waiting for the Mirror's next run.") && !state.problem, `pending: said in words (${state.said})`)
  html = render(state.info)
  ok(html.includes("Waiting for the Mirror's next run.") && button('Stop waiting')?.disabled === false && button('Make these changes in Grok')?.disabled === true, 'pending: waiting, Stop waiting, Apply waits')

  // while it waits, a quiet look again: the sentence said stays
  state.patches = []
  quiet(async () => info({ preview: preview({ pending: CID }) }))
  await acts.refresh()
  ok(H.restCalls.length === 1 && H.restCalls[0].path === `/pairs/${ACME}/to-grok` && state.patches.length === 1 && !('said' in state.patches[0]) && !('problem' in state.patches[0]), 'waiting: read again quietly, what was said stays')
  quiet(async () => refusal('internal'))
  state.patches = []
  await acts.refresh()
  ok(state.patches.length === 0, 'waiting: a failed quiet read changes nothing')

  quiet(async path => (path.endsWith('/give-up') ? { state: 'given_up', sentence: 'Stopped waiting. Nothing more is sent for that change.' } : info()))
  await acts.giveUp()
  ok(H.restCalls[0].path === `/pairs/${ACME}/to-grok/give-up` && JSON.stringify(H.restCalls[0].opts.body) === '{}' && state.said === 'Stopped waiting. Nothing more is sent for that change.', 'Stop waiting posts give-up and says the answer')

  // a sent change: at the Mirror's next run
  quiet(async path => (path.endsWith('/apply') ? result({ state: 'sent', sentence: '' , message: '' }) : info()))
  await acts.apply(CID)
  ok(state.said === "Sent. It is made at the Mirror's next run.", `sent: said plainly (${state.said})`)

  // failed: a problem in words
  quiet(async path => (path.endsWith('/apply') ? result({ state: 'failed', sentence: 'Grok did not take the change.' }) : info()))
  await acts.apply(CID)
  ok(state.problem?.message === 'Grok did not take the change.', 'failed: said as a problem')

  // every refusal in the owner's words
  for (const code of ['refused', 'not_allowed', 'no_permission', 'changed_in_grok', 'empty', 'undone', 'is_move', 'no_change', 'bad_input']) {
    quiet(async () => refusal(code, `raw ${code} text`))
    await acts.apply(CID)
    html = render(info(), { initialProblem: state.problem })
    ok(state.problem && !html.includes(code) && !html.includes('raw ') && html.includes(state.problem.message), `${code}: owner words (${state.problem?.message})`)
  }
  quiet(async () => refusal('weird', 'The beta of this is off.'))
  await acts.apply(CID)
  ok(state.problem?.message === 'Something went wrong inside Grok Bot Twin.', 'an unknown answer that says beta is not shown')
}

// a thing changed on both sides: the owner chooses which version Grok keeps
{
  const both = info({ preview: preview({ held: [{ item: 'description', code: 'conflict', why: 'It was changed in Hermes and in Grok.' }] }) })

  html = render(both)
  ok(button('Send the Hermes version')?.label === 'Description: send the Hermes version' && button('Keep the Grok version'), 'a held conflict: the two choices, named for the thing')
  ok(!render(info()).includes('Send the Hermes version'), 'a held secret: no choice, only why')

  const { acts, state } = actsFor()
  const fresh = preview({ change_id: CID2, held: [] })

  quiet(async () => fresh)
  await acts.resolve('description', 'hermes', both)
  ok(H.restCalls.length === 1 && H.restCalls[0].path === `/pairs/${ACME}/to-grok/resolve` && JSON.stringify(H.restCalls[0].opts.body) === '{"item":"description","keep":"hermes"}' && state.info.preview === fresh && state.info.allowed === both.allowed, 'resolve posts the item and the side kept; the answer is the new preview')
}

// ── 9. history and undo ─────────────────────────────────────────────────────

{
  const WHY = 'Grok changed the name again since, so it is not put back.'
  const hist = info({ changes: [change({ id: CID2, can_undo: false, undo_why: WHY, items: ['name'] }), change()] })

  html = render(hist)
  const undos = H.buttons.filter(b => b.text === 'Undo')
  ok(html.includes('Changes made in Grok') && undos.length === 1 && undos[0].label === 'Undo: Name and Instructions' && html.includes(WHY) && html.includes('Grok took both changes.') && html.includes('3 min ago') && html.includes('Done'), `history: when, state, message; Undo only where it can be (${html.slice(-400)})`)
  ok(!html.includes(DONE_ID) && !html.includes(CID2), 'history: no ids')

  html = render(hist, { initialUndo: P.toGrokOf(hist).changes[1] })
  ok(html.includes('Undo this change in Grok?') && html.includes('Name and Instructions in Grok go back to what they were before this change (3 min ago).') && button('Put back') && button('Keep it'), `undo asks first, naming what goes back (${html.slice(-300)})`)

  const { acts, state } = actsFor()

  quiet(async path => (path.endsWith('/undo') ? result({ change_id: 'c-9999888877776666', sentence: 'Put back in Grok: name and instructions.' }) : info()))
  await acts.undo(DONE_ID)
  ok(H.restCalls[0].path === `/pairs/${ACME}/to-grok/undo` && JSON.stringify(H.restCalls[0].opts.body) === `{"change_id":"${DONE_ID}"}` && state.said === 'Put back in Grok: name and instructions.', 'Undo posts that change\'s id, after the confirm')
  ok(P.toGrokOf(info({ changes: [change({ id: 'not-an-id' })] })).changes[0].canUndo === false, 'an id that is not a change id cannot be undone')
}

// ── 10. the entry in Details ────────────────────────────────────────────────

{
  let opened = 0

  H.buttons = []
  markup = renderToStaticMarkup(jsx(P.CopiesPanel, { fastCheck: false, intervalS: 60, onSkillSync: () => {}, onToGrok: () => { opened += 1 }, open: true, pair }))
  button('Changes to Grok…')?.onClick()
  ok(opened === 1 && button('Changes to Grok…').variant === 'secondary', 'Details: "Changes to Grok…" opens the sheet')
  H.buttons = []
  renderToStaticMarkup(jsx(P.CopiesPanel, { fastCheck: false, intervalS: 60, onSkillSync: () => {}, onToGrok: () => {}, open: true, pair: { ...pair, sample: true } }))
  ok(!button('Changes to Grok…'), 'not on the sample')
}

// ── 11. untrusted text, words ───────────────────────────────────────────────

{
  const evil = info({
    changes: [change({ message: XSS, undo_why: XSS, can_undo: false })],
    preview: preview({ sentence: XSS, blocked: XSS, held: [{ item: 'description', code: 'secret', why: XSS }], changes: [{ item: 'routine:x', before: null, after: routine({ name: XSS, prompt: XSS, schedule_words: XSS }) }, { item: 'description', before: XSS, after: XSS }] }),
    grant: { items: ['name'], at: AT, rule: XSS, where: XSS },
    permission: 'not_set_up'
  })

  html = render(evil, { initialRevoke: { rule: XSS, where: XSS, sentence: XSS }, initialSaid: XSS })
  ok(!/<img/i.test(markup) && markup.includes('&lt;img src=x onerror=alert(1)&gt;') && html.includes(XSS), 'bot text renders inert, as text')
  ok(html.includes('Routine "<img'), 'a routine named with markup is still its name, as text')

  const all = [render(offInfo), render(info()), render(info({ permission: 'not_set_up' }), { initialSteps: true }), render(info({ changes: [change(), change({ id: CID2, can_undo: false, undo_why: 'No.' })], preview: preview({ pending: CID }) })), html].join(' ')

  for (const w of ['undefined', 'null', '[object', 'NaN', '2000-01-01T', 'change_id', 'c-0123', 'routine:', 'not_set_up', 'no_permission', 'can_undo', 'mirror_steps']) {
    ok(!all.includes(w), `no "${w}" on screen`)
  }
  ok(!/\bbeta\b/i.test(all) && !/\bbeta\b/i.test(source.slice(source.indexOf('// ── Hermes → Grok'), source.indexOf('function CopiesPanel('))), 'never the word beta')
  ok(!/[!]|please|simply|successfully|oops/i.test(all.replace(/onerror=alert\(1\)/g, '')), 'short and plain: no exclamation mark, no filler')
}

console.log(`togrok check: ${passed} assertions passed`)
