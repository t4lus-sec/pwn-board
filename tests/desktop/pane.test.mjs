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
assert.match(text(), /Find why flush fails one run in fifty/)
assert.match(text(), /\[\+\] L1 \(p5\) Reproduce with fifty runs/)
assert.match(text(), /\[\+\] F1 Fails 3 of 50 with ENOSPC/)
assert.match(text(), /\[\?\] L2 \(p7\) Measure \/tmp inside the container/)
assert.match(text(), /\[!\] N1 note: try a 1 GB tmpfs/)
assert.equal(storage.get('board'), 'flaky-flush')
assert.ok(typeof listeners['plugin.search-board.board.changed'] === 'function')

// --- select F1: the strip shows its evidence
const rows = [...document.querySelectorAll('[role=treeitem]')]
await act(async () => rows[1].dispatchEvent(new window.MouseEvent('click', { bubbles: true })))
await settle()
assert.match(text(), /e1 · terminal/)
assert.match(text(), /ENOSPC/)
assert.ok(calls.some(([p]) => p === '/evidence?ids=1'))

// --- leave a note about F1
const input = document.querySelector('input')
assert.equal(input.placeholder, 'Note about F1…')
const setValue = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set
await act(async () => { setValue.call(input, 'check the tmpfs size'); input.dispatchEvent(new window.Event('input', { bubbles: true })) })
await act(async () => document.querySelector('form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true })))
await settle()
const posted = calls.find(([p, o]) => o?.method === 'POST')
assert.deepEqual(posted, ['/boards/flaky-flush/note', { method: 'POST', body: { text: 'F1: check the tmpfs size' } }])

// --- fold F1 with its caret
const caret = [...document.querySelectorAll('[role=treeitem]')][1].querySelector('span')
await act(async () => caret.dispatchEvent(new window.MouseEvent("click", { bubbles: true }))); await settle()
assert.equal(document.querySelectorAll('[role=treeitem]').length, 2)

// --- abandon wants two clicks
const abandon = () => [...document.querySelectorAll('button')].find(b => /Abandon/.test(b.textContent))
await act(async () => abandon().click())
assert.equal(abandon().textContent, 'Abandon?')
assert.equal(calls.filter(([p]) => p.endsWith('/decide')).length, 0)
await act(async () => abandon().click())
await settle()
assert.equal(calls.filter(([p]) => p.endsWith('/decide')).length, 1)

// --- the workflow: Review tab
const tabs = () => [...document.querySelectorAll('[role=tab]')]
assert.deepEqual(tabs().map(t => t.textContent), ['Board', 'Review', 'Verification'])
await act(async () => tabs()[1].click())
await settle()
assert.match(text(), /write-up · round 1/)
assert.match(text(), /Raised the container's tmpfs to 1 GB/)
assert.match(text(), /\[\?\] R1 \(round 1\): Did you try arm64\?/)
const fileRow = [...document.querySelectorAll('[role=button]')].find(e => e.textContent.startsWith('/w/docker-compose.yml'))
await act(async () => fileRow.click())
await settle()
assert.match(text(), /tmpfs: \/tmp:size=1g/)

// --- the workflow: Verification tab, accept wants two clicks
await act(async () => tabs()[2].click())
await settle()
assert.match(text(), /independent verifier · done · \[\+\] passed/)
assert.match(text(), /the agent's own check · \[\+\] passed/)
assert.match(text(), /Both checks agree/)
const accept = () => [...document.querySelectorAll('button')].find(b => /Accept as complete/.test(b.textContent))
const before = calls.length
await act(async () => accept().click())
assert.equal(accept().textContent, 'Accept as complete?')
assert.equal(calls.length, before)
await act(async () => accept().click())
await settle()
assert.ok(calls.some(([p, o]) => p === '/boards/flaky-flush/accept' && o?.method === 'POST'))

await act(async () => root.unmount())
console.log('pane: all checks passed')
