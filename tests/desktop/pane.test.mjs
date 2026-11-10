import assert from 'node:assert/strict'
import { copyFileSync, readFileSync } from 'node:fs'
import { JSDOM } from 'jsdom'

const dom = new JSDOM('<!doctype html><div id="root"></div>', { pretendToBeVisual: true })
globalThis.window = dom.window
globalThis.document = dom.window.document
globalThis.HTMLElement = dom.window.HTMLElement
globalThis.IS_REACT_ACT_ENVIRONMENT = true

// The pane's bare imports resolve from beside it, so it is copied in next to node_modules.
copyFileSync(new URL('../../desktop/plugin.js', import.meta.url), new URL('./.plugin.under-test.js', import.meta.url))

const React = await import('react')
const { act } = React
const { createRoot } = await import('react-dom/client')
const plugin = (await import('./.plugin.under-test.js')).default
const { buildTree, visibleRows } = await import('./.plugin.under-test.js')
// Written by the real backend routes: SEARCH_BOARD_PANE_FIXTURE=tests/desktop/fixture.json pytest -k fixture
const fixture = JSON.parse(readFileSync(new URL('./fixture.json', import.meta.url), 'utf8'))

// --- the tree, without React
const tree = buildTree(fixture.board)
assert.deepEqual(tree.children.board, ['L1'])
assert.deepEqual(tree.children.L1, ['F1'])
assert.deepEqual(tree.children.F1, ['L2'])
assert.deepEqual(tree.children.L2, ['N1'])
assert.deepEqual(visibleRows(tree, {}).map(r => [r.id, r.depth]), [['L1', 0], ['F1', 1], ['L2', 2], ['N1', 3]])
assert.deepEqual(visibleRows(tree, { F1: true }).map(r => r.id), ['L1', 'F1'])

// --- registration
const contributions = []
const calls = []
const listeners = {}
const storage = new Map()
const ctx = {
  register: c => { contributions.push(c); return () => {} },
  onEvent: (type, fn) => { listeners[type] = fn; return () => delete listeners[type] },
  setInterval: () => () => {},
  storage: { get: (k, d) => storage.has(k) ? storage.get(k) : d, set: (k, v) => storage.set(k, v) },
  rest: async (path, opts) => {
    calls.push([path, opts])
    if (path.startsWith('/boards?')) return fixture.boards
    if (path === '/boards/flaky-flush/workflow') return fixture.workflow
    if (path.startsWith('/boards/flaky-flush/file?')) return fixture.file
    if (path.startsWith('/boards/flaky-flush') && !opts) return fixture.board
    if (path.startsWith('/evidence')) return fixture.evidence
    if (opts?.method === 'POST') return { ok: true }
    throw new Error(`unexpected ${path}`)
  }
}
plugin.register(ctx)
assert.equal(plugin.id, 'search-board')
assert.equal(contributions.length, 1)
assert.equal(contributions[0].area, 'panes')

// --- render the pane
const root = createRoot(document.getElementById('root'))
const settle = async () => { for (let i = 0; i < 5; i++) await act(async () => { await new Promise(r => setTimeout(r, 0)) }) }
await act(async () => root.render(contributions[0].render()))
await settle()
const text = () => document.getElementById('root').textContent
const rowTexts = () => [...document.querySelectorAll('[role=treeitem]')].map(r => r.textContent)
assert.match(text(), /Find why flush fails one run in fifty/)
assert.match(text(), /proposed solved/)                       // the status pill
assert.match(text(), /✓Search✓Write-up✓Review4Verify5Complete/) // the stepper: at verification
assert.match(text(), /In verification: decide it in that tab\./)
assert.ok(!/Confirm solved/.test(text()))
assert.match(rowTexts()[0], /^▾Intent 1p5explored.*Reproduce with fifty runs/)
assert.match(rowTexts()[1], /^▾Finding 1supportse1Fails 3 of 50 with ENOSPC/)
assert.match(rowTexts()[2], /^▾Intent 2p7open.*Measure \/tmp inside the container/)
assert.match(rowTexts()[3], /^Note 1newtry a 1 GB tmpfs/)
assert.equal(storage.get('board'), 'flaky-flush')
assert.ok(typeof listeners['plugin.search-board.board.changed'] === 'function')

// --- the four tabs are always there
const tabs = () => [...document.querySelectorAll('[role=tab]')]
assert.deepEqual(tabs().map(t => t.textContent), ['Board', 'Review', 'Verification', 'Complete'])
assert.equal(document.querySelector('[class]'), null, 'no utility classes: styling is inline')

// --- select Finding 1: the strip shows its evidence
const rows = [...document.querySelectorAll('[role=treeitem]')]
await act(async () => rows[1].dispatchEvent(new window.MouseEvent('click', { bubbles: true })))
await settle()
assert.match(text(), /Finding 1 — supports, observed, by model/)
assert.match(text(), /e1 · terminal/)
assert.ok(calls.some(([p]) => p === '/evidence?ids=1'))

// --- leave a note about Finding 1
const input = document.querySelector('input')
assert.equal(input.placeholder, 'Note about Finding 1…')
const setValue = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set
await act(async () => { setValue.call(input, 'check the tmpfs size'); input.dispatchEvent(new window.Event('input', { bubbles: true })) })
await act(async () => document.querySelector('form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true })))
await settle()
assert.deepEqual(calls.find(([p, o]) => o?.method === 'POST'), ['/boards/flaky-flush/note', { method: 'POST', body: { text: 'F1: check the tmpfs size' } }])

// --- fold Finding 1 with its caret
const caret = [...document.querySelectorAll('[role=treeitem]')][1].querySelector('span')
await act(async () => caret.dispatchEvent(new window.MouseEvent('click', { bubbles: true })))
assert.equal(document.querySelectorAll('[role=treeitem]').length, 2)

// --- abandon wants two clicks
const button = re => [...document.querySelectorAll('button')].find(b => re.test(b.textContent))
await act(async () => button(/^Abandon/).click())
assert.equal(button(/^Abandon/).textContent, 'Abandon?')
assert.equal(calls.filter(([p]) => p.endsWith('/decide')).length, 0)
await act(async () => button(/^Abandon/).click())
await settle()
assert.equal(calls.filter(([p]) => p.endsWith('/decide')).length, 1)

// --- Review tab
await act(async () => tabs()[1].click())
await settle()
assert.match(text(), /Reviewed: the board is in verification\./)
assert.match(text(), /Write-up · round 1/)
assert.match(text(), /Raised the container's tmpfs to 1 GB/)
assert.match(text(), /Review note 1 · you · round 1.*awaiting answer.*Did you try arm64\?/)
const fileRow = [...document.querySelectorAll('[role=button]')].find(e => e.textContent.startsWith('/w/docker-compose.yml'))
await act(async () => fileRow.click())
await settle()
assert.match(text(), /tmpfs: \/tmp:size=1g/)

// --- Verification tab: accept wants two clicks
await act(async () => tabs()[2].click())
await settle()
assert.match(text(), /Independent verifier \(subagent\)done✓ PASSED/)
assert.match(text(), /The agent's own check✓ PASSED/)
assert.match(text(), /Reconciled✓ PASSED.*Both checks agree/)
const before = calls.length
await act(async () => button(/^Accept as complete/).click())
assert.equal(button(/^Accept as complete/).textContent, 'Accept as complete?')
assert.equal(calls.length, before)
await act(async () => button(/^Accept as complete/).click())
await settle()
assert.ok(calls.some(([p, o]) => p === '/boards/flaky-flush/accept' && o?.method === 'POST'))

// --- Complete tab: there before it is reached, saying so
await act(async () => tabs()[3].click())
await settle()
assert.match(text(), /Not complete yet/)

await act(async () => root.unmount())

// --- a proposal not yet in the workflow is decided on the Board tab
fixture.workflow.flow.stage = 'working'
const root2 = createRoot(document.getElementById('root'))
await act(async () => root2.render(contributions[0].render()))
await settle()
assert.ok(button(/^Confirm solved/), 'confirm is offered')
await act(async () => button(/^Send back$/).click())
await settle()
assert.ok(calls.some(([p, o]) => p === '/boards/flaky-flush/decide' && o?.body?.decision === 'reject'))
await act(async () => root2.unmount())
console.log('pane: all checks passed')
