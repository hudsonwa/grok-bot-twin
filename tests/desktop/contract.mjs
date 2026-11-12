// Contract check for the page harness's mock answers.
//
//   node tests/desktop/contract.mjs [--verbose]
//
// tests/desktop/check.mjs renders the Twins page against mock answers written in its section
// "3. fixtures". This script reads that section as text, evaluates it on its own, and checks
// every mock answer against tests/contract/api_shapes.json, the same file the backend's
// answers are checked against (tests/test_api_contract.py). So a mock can never drift from
// what the backend really sends without this failing.
//
// Rules for a mock answer (stricter than for a backend answer in one way, looser in another):
//   - every `required` key is there and has the right type;
//   - `backend` keys (sent by every backend answer, not needed by the page's tests) may be missing;
//   - NO key that the contract does not list: a mock must not invent what the backend does not send.
// No dependencies. Invented data only.

import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const verbose = process.argv.includes('--verbose')
const fail = msg => {
  console.error(`CONTRACT CHECK FAILED\n${msg}`)
  process.exit(1)
}

const spec = JSON.parse(readFileSync(resolve(here, '../contract/api_shapes.json'), 'utf8'))
const SHAPES = spec.shapes
// The answers about other computers (the sharing contract): the page's own copy of their shapes is used only
// for what the backend's file does not define yet, so the backend's file always wins once it has them.
const fleetSpec = JSON.parse(readFileSync(resolve(here, 'fleet_shapes.json'), 'utf8'))
const fromPage = []
for (const [name, shape] of Object.entries(fleetSpec.shapes)) {
  if (!SHAPES[name]) {
    SHAPES[name] = shape
    fromPage.push(name)
  }
}
for (const [name, more] of Object.entries(fleetSpec.extend ?? {})) {
  for (const [key, typ] of Object.entries(more.optional ?? {})) {
    const s = SHAPES[name]
    if (s && !(key in (s.required ?? {})) && !(key in (s.backend ?? {})) && !(key in (s.optional ?? {}))) s.optional = { ...(s.optional ?? {}), [key]: typ }
  }
}

// ── 1. the harness's fixtures section, evaluated on its own ─────────────────

const harness = readFileSync(resolve(here, 'check.mjs'), 'utf8').split('\n')
const START = '// ── 3. fixtures'
const END = '// ── 4. recording host'
const from = harness.findIndex(l => l.startsWith(START))
const to = harness.findIndex(l => l.startsWith(END))
if (from < 0 || to < 0 || to <= from) {
  fail(`tests/desktop/check.mjs no longer has the lines starting "${START}" and "${END}" (in that order).\n` +
    'This check reads the mock answers between them. Put the markers back, or update tests/desktop/contract.mjs.')
}
const block = harness.slice(from + 1, to).join('\n')
const names = [...block.matchAll(/^const\s+([A-Za-z_$][\w$]*)\s*=/gm)].map(m => m[1])
let mock
try {
  // Evaluated here only, and only this repo's own test text (the fixtures are plain data plus `iso()`).
  mock = new Function(`${block}\nreturn { ${names.join(', ')} }`)()
} catch (e) {
  fail(`The fixtures section of tests/desktop/check.mjs could not be evaluated on its own: ${e.message}\n` +
    'It must stay self-contained (plain data; no names from other sections).')
}
if (!mock.fixtures || typeof mock.fixtures !== 'object') fail('The fixtures section no longer defines `fixtures`.')

// ── 2. validator (the same few rules as tests/test_api_contract.py) ─────────

const kind = v => (v === null ? 'null' : Array.isArray(v) ? 'list' : typeof v === 'object' ? 'dict'
  : typeof v === 'boolean' ? 'bool' : typeof v === 'string' ? 'str' : typeof v === 'number' ? (Number.isInteger(v) ? 'int' : 'number') : typeof v)
const BASIC = {
  str: v => typeof v === 'string',
  int: v => Number.isInteger(v),
  number: v => typeof v === 'number',
  bool: v => typeof v === 'boolean',
  null: v => v === null,
  dict: v => v !== null && typeof v === 'object' && !Array.isArray(v),
  list: v => Array.isArray(v),
  any: () => true
}
const missingBackendKeys = []

function checkType(value, typ, path) {
  const options = typ.split('|').map(t => t.trim())
  const found = []
  for (const t of options) {
    let probs
    if (BASIC[t]) probs = BASIC[t](value) ? [] : [`${path}: expected ${t}, got ${kind(value)}`]
    else if (t.startsWith('list[') && t.endsWith(']')) {
      probs = Array.isArray(value) ? value.flatMap((v, i) => checkType(v, t.slice(5, -1), `${path}[${i}]`)) : [`${path}: expected ${t}, got ${kind(value)}`]
    } else if (SHAPES[t]) probs = checkShape(value, t, path)
    else fail(`api_shapes.json names an unknown type "${t}" at ${path}`)
    if (probs.length === 0) return []
    found.push(probs)
  }
  return found.reduce((a, b) => (b.length < a.length ? b : a))
}

function checkShape(value, name, path) {
  const s = SHAPES[name]
  if (!BASIC.dict(value)) return [`${path}: expected ${name} (an object), got ${kind(value)}`]
  const required = s.required ?? {}
  const backend = s.backend ?? {}
  const optional = s.optional ?? {}
  const out = []
  for (const [k, t] of Object.entries(required)) {
    if (!(k in value)) out.push(`${path}: missing key "${k}" (${name})`)
    else out.push(...checkType(value[k], t, `${path}.${k}`))
  }
  for (const [k, t] of Object.entries({ ...backend, ...optional })) {
    if (k in value) out.push(...checkType(value[k], t, `${path}.${k}`))
    else if (k in backend) missingBackendKeys.push(`${path}.${k}`)
  }
  const unknown = Object.keys(value).filter(k => !(k in required) && !(k in backend) && !(k in optional))
  if (unknown.length) out.push(`${path}: the mock has keys the backend does not send (${name}): ${unknown.join(', ')}`)
  return out
}

// ── 3. which shape each mock answer must fit ────────────────────────────────

// Keys of `fixtures` (what the harness's useQuery hands the page per query name).
const FIXTURE_SHAPES = {
  setup: 'Setup',
  conflicts: 'Conflicts',
  parity: 'Parity',
  recent: 'Recent',
  'restore-points': 'RestorePoints',
  'remove-plan': 'RemovePlan',
  'uninstall-plan': 'UninstallPlan',
  checklist: 'Checklist',
  'grok-bots': 'GrokBots',
  fleet: 'Fleet',
  'unlink-plan': 'Leaving',
  'unlink-computer-plan': 'Leaving',
  'setup-export': 'SetupExportInfo',
  batch: 'BatchInfo'
}
// Named answers in the section. A name not listed here is classified by what it holds (below).
const NAMED_SHAPES = {
  bothVersions: 'Conflict', longVersions: 'Conflict',
  mini: 'Machine', surface: 'Machine', orchard: 'Machine', shareOff: 'Share', shareOn: 'Share', linkCode: 'LinkCode',
  miniRow: 'FleetMachine', surfaceRow: 'FleetMachine', orchardRow: 'FleetMachine', surfaceBehind: 'FleetMachine',
  sourceOk: 'Source', keptRanger: 'RemovedTwin', unlinkPlan: 'Leaving', unlinkSurfacePlan: 'Leaving',
  briefNone: 'FleetBrief', briefSource: 'FleetBrief', briefFollower: 'FleetBrief',
  exportInfo: 'SetupExportInfo', exportInfoLast: 'SetupExportInfo', exportPlain: 'SetupExport', exportFull: 'SetupExport', exportOther: 'SetupExport', exportDone: 'SetupExport',
  batchInfo: 'BatchInfo', batchModels: 'BatchModels', batchDownloads: 'BatchDownloads', batchImport: 'BatchImport'
}
// Names that are parts or helpers, not answers.
const NOT_ANSWERS = new Set(['iso', 'coverage', 'calm', 'noRoutines', 'ready', 'setupBase', 'fixtures', 'ACME_SAYS', 'EXAMPLE', 'FILE', 'same', 'differs', 'onRows', 'SCOUT', 'QUILL', 'sharedTwin', 'seenFrom', 'hereRows', 'hereTwin', 'EXPORT_REQUEST', 'EXPORT_PRIVACY', 'EXPORT_TEXT'])

function shapeOf(name, value) {
  if (NAMED_SHAPES[name]) return NAMED_SHAPES[name]
  if (!BASIC.dict(value)) return null
  if ('pair_id' in value && 'grok' in value && 'hermes' in value) return 'Pair'
  if ('mode' in value && 'pairs' in value) return 'Status'
  if ('helper' in value && 'check' in value) return 'Setup'
  if ('this_machine' in value && 'share' in value) return 'Fleet'
  return null
}

let passed = 0
const problems = []
const check = (value, shape, label) => {
  if (!SHAPES[shape]) fail(`api_shapes.json has no shape "${shape}" (needed for ${label})`)
  const probs = checkShape(value, shape, label)
  if (probs.length) problems.push(...probs)
  else passed += 1
}

for (const name of names) {
  if (NOT_ANSWERS.has(name)) continue
  const shape = shapeOf(name, mock[name])
  if (!shape) {
    problems.push(`${name}: a new name in the fixtures section that this check cannot place. Add it to NAMED_SHAPES ` +
      '(with its shape in tests/contract/api_shapes.json) or to NOT_ANSWERS in tests/desktop/contract.mjs.')
    continue
  }
  check(mock[name], shape, name)
}
for (const [key, value] of Object.entries(mock.fixtures)) {
  const shape = FIXTURE_SHAPES[key]
  if (!shape) {
    problems.push(`fixtures['${key}']: a mock answer with no shape. If the backend has this route, add its shape to ` +
      'tests/contract/api_shapes.json and to FIXTURE_SHAPES here; if it does not, the page is being built against ' +
      'an answer that does not exist.')
    continue
  }
  check(value, shape, `fixtures['${key}']`)
}
// parts used inside answers
check({ ...mock.calm }, 'Pending', 'calm')
check({ ...mock.ready }, 'SetupBrief', 'ready')
if (mock.noRoutines) check({ ...mock.noRoutines }, 'PairRoutines', 'noRoutines')
for (const [i, row] of (mock.coverage ?? []).entries()) check(row, 'CoverageRow', `coverage[${i}]`)

// the answers the harness must have for this check to mean anything
for (const must of ['statusAll', 'statusFirstRun', 'setupNoBotport', 'setupUnchecked', 'setupReady', 'acme', 'recipe', 'fleetNone', 'fleetNoneOther', 'fleetCode', 'fleetSource2', 'fleetBehind', 'fleetShareError', 'fleetFollower', 'fleetFollowerOffline', 'fleetFollowerSharingOff', 'fleetFollowerError', 'fleetUnlinked', 'statusSource', 'statusFollower', 'exportInfo', 'exportPlain', 'exportFull', 'exportDone', 'batchInfo', 'batchDownloads']) {
  if (!(must in mock)) problems.push(`the fixtures section no longer defines \`${must}\``)
}
for (const must of Object.keys(FIXTURE_SHAPES)) {
  if (!(must in mock.fixtures)) problems.push(`fixtures no longer has '${must}'`)
}

if (verbose && missingBackendKeys.length) {
  console.log('Keys every backend answer carries that the mock leaves out (allowed):')
  for (const k of [...new Set(missingBackendKeys)]) console.log(`  ${k}`)
}
if (verbose && fromPage.length) console.log(`Shapes taken from tests/desktop/fleet_shapes.json (the backend's file does not define them yet): ${fromPage.join(', ')}`)
if (problems.length) fail(problems.join('\n'))
console.log(`${passed} contract checks passed`)
