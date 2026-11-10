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
const { buildTree, visibleRows, plainText, staleBuild, BUILD } = await import('./.plugin.under-test.js')
const sdk = await import('@hermes/plugin-sdk')
// Written by the real backend routes: SEARCH_BOARD_PANE_FIXTURE=tests/desktop/fixture.json pytest -k fixture
const fixture = JSON.parse(readFileSync(new URL('./fixture.json', import.meta.url), 'utf8'))

// --- the tree, without React
const tree = buildTree(fixture.board)
assert.deepEqual(tree.children.board, ['origin'])
assert.deepEqual(tree.children.origin, ['L1'])
assert.deepEqual(tree.children.L1, ['F1'])
assert.deepEqual(tree.children.F1, ['L2'])
assert.deepEqual(tree.children.L2, ['N1'])
assert.deepEqual(visibleRows(tree, {}).map(r => [r.id, r.depth]), [['origin', 0], ['L1', 1], ['F1', 2], ['L2', 3], ['N1', 4]])
assert.deepEqual(visibleRows(tree, { F1: true }).map(r => r.id), ['origin', 'L1', 'F1'])
assert.deepEqual(visibleRows(tree, { origin: true }).map(r => r.id), ['origin'])
const bare = buildTree({ ...fixture.board, leads: [], findings: [], notes: [] })
assert.deepEqual(visibleRows(bare, {}).map(r => r.id), ['origin'], 'the origin is there before anything else is')

// --- stale builds: what Hermes has loaded against this pane
assert.equal(staleBuild(null), null)
assert.equal(staleBuild({ api: BUILD, agent: BUILD }), null)
assert.equal(staleBuild({ api: BUILD, agent: null }), null, 'no word from the agent side yet is not an alarm')
assert.match(staleBuild({ api: null, agent: null }), /older search-board backend \(before 0\.2\.0.*hermes gateway restart/)
assert.match(staleBuild({ api: '0.1.0', agent: '0.1.0' }), /older search-board backend \(0\.1\.0/)
assert.match(staleBuild({ api: '9.0.0', agent: '9.0.0' }), /This pane is an older search-board build.*desktop half off and on/)
assert.match(staleBuild({ api: BUILD, agent: '0.1.0' }), /agent side is still running search-board 0\.1\.0.*hermes gateway restart/)
assert.equal(staleBuild({ api: '0.2.0', agent: '0.2.0' }) === null, BUILD === '0.2.0')

// --- one-line Markdown for tree rows
assert.equal(plainText('Goal test passed: `pytest --count 50` exited 0.'), 'Goal test passed: pytest --count 50 exited 0.')
assert.equal(plainText('**Bold**, _it_, [a link](http://x) and snake_case_name'), 'Bold, it, a link and snake_case_name')
assert.equal(plainText('## Heading\n- one\n- two'), 'Heading one two')
assert.equal(plainText('a * b * c'), 'a * b * c')

// The workflow carries Markdown, as agents write it; and a delivered Markdown file.
fixture.workflow.reports[0].text = "**Raised** the container's tmpfs to 1 GB; fifty runs pass on the goal test.\n\n- `docker-compose.yml`: `size=1g`"
fixture.workflow.files.push({ path: '/w/NOTES.md', chars: 24, truncated: false, error: null })

let buildAnswer = () => ({ api: BUILD, agent: BUILD })

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
    if (path === '/build') return buildAnswer()
    if (path.startsWith('/boards?')) return fixture.boards
    if (path === '/boards/flaky-flush/workflow') return fixture.workflow
    if (path.startsWith('/boards/flaky-flush/file?') && path.includes('NOTES.md')) return { path: '/w/NOTES.md', content: '# Notes\n\n**1 GB** is enough.' }
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
assert.ok(calls.some(([p]) => p === '/build'), 'the pane asks which builds Hermes runs')
assert.equal(document.querySelector('[role=alert]'), null, 'same build everywhere: no banner')
const rowTexts = () => [...document.querySelectorAll('[role=treeitem]')].map(r => r.textContent)
assert.match(text(), /Find why flush fails one run in fifty/)
assert.match(text(), /proposed solved/)                       // the status pill
assert.match(text(), /✓Search✓Write-up✓Review4Verify5Complete/) // the stepper: at verification
assert.match(text(), /In verification: decide it in that tab\./)
assert.ok(!/Confirm solved/.test(text()))
assert.match(rowTexts()[0], /^▾OriginCI fails about 2%→ goal: fifty green runs in a row$/)
assert.match(rowTexts()[1], /^▾Intent 1p5explored.*Reproduce with fifty runs/)
assert.match(rowTexts()[2], /^▾Finding 1supportse1Fails 3 of 50 with ENOSPC/)
assert.match(rowTexts()[3], /^▾Intent 2p7open.*Measure \/tmp inside the container/)
assert.match(rowTexts()[4], /^Note 1newtry a 1 GB tmpfs/)
assert.equal(document.querySelector('[role=treeitem]').getAttribute('aria-selected'), 'true', 'the origin is selected first')
assert.match(text(), /Origin.*Copy.*origin.*CI fails about 2%.*goal.*fifty green runs/)
assert.equal(storage.get('board'), 'flaky-flush')
assert.ok(typeof listeners['plugin.search-board.board.changed'] === 'function')

// --- the four tabs are always there
const tabs = () => [...document.querySelectorAll('[role=tab]')]
assert.deepEqual(tabs().map(t => t.textContent), ['Board', 'Review', 'Verification', 'Complete'])
assert.equal(document.querySelector('[class]'), null, 'no utility classes: styling is inline')

// --- the pane's text is selectable: it opts back in from the app's unselectable chrome
const paneRoot = document.querySelector('[data-selectable-text=true]')
assert.ok(paneRoot, 'opts in to selection')
assert.equal(paneRoot.style.userSelect, 'text')

// --- select Finding 1: the strip shows its evidence
const rows = [...document.querySelectorAll('[role=treeitem]')]
// a click that ends a drag-to-select leaves the selection alone
const realSelection = window.getSelection
window.getSelection = () => ({ isCollapsed: false, toString: () => 'fifty runs' })
await act(async () => rows[2].dispatchEvent(new window.MouseEvent('click', { bubbles: true })))
await settle()
assert.equal(rows[0].getAttribute('aria-selected'), 'true', 'selecting text is not picking a row')
window.getSelection = realSelection
await act(async () => rows[2].dispatchEvent(new window.MouseEvent('click', { bubbles: true })))
await settle()
assert.match(text(), /Finding 1 — supports, observed, by model/)
assert.match(text(), /e1 · terminal/)
assert.ok(sdk.rendered.some(([how, p]) => how === 'chat' && p.text === 'Fails 3 of 50 with ENOSPC'), 'a finding renders as Markdown')

// --- Copy: the entry, and a cited tool output, as written
const copyBtn = what => document.querySelector(`[data-copy="${what}"]`)
await act(async () => copyBtn('entry').click())
await settle()
assert.match(sdk.copied.at(-1), /^Finding 1 — supports.*\nFails 3 of 50 with ENOSPC\nexplored: Intent 1/)
assert.equal(copyBtn('entry').textContent, 'Copied')
await act(async () => copyBtn('output of e1').click())
await settle()
assert.equal(sdk.copied.at(-1), fixture.evidence.evidence[0].excerpt)
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
const caret = [...document.querySelectorAll('[role=treeitem]')][2].querySelector('span')
await act(async () => caret.dispatchEvent(new window.MouseEvent('click', { bubbles: true })))
assert.equal(document.querySelectorAll('[role=treeitem]').length, 3)

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
assert.match(text(), /\*\*Raised\*\* the container's tmpfs to 1 GB/)  // the stub echoes the source it was handed
const writeUp = sdk.rendered.find(([how, p]) => how === 'chat' && p.text.startsWith('**Raised**'))
assert.ok(writeUp, 'the write-up goes through the chat renderer')
assert.equal(writeUp[1].previewOnly, true, 'preview-only: no images, no live directives')
assert.equal(writeUp[1].media, false)
assert.ok(sdk.rendered.some(([how, p]) => how === 'chat' && p.text === 'Did you try arm64?'), 'review notes render as Markdown')
await act(async () => copyBtn('write-up').click())
await settle()
assert.equal(sdk.copied.at(-1), fixture.workflow.reports[0].text, 'the write-up is copied as written, Markdown and all')
assert.match(text(), /Review note 1 · you · round 1.*awaiting answer.*Did you try arm64\?/)
const fileRow = [...document.querySelectorAll('[role=button]')].find(e => e.textContent.startsWith('/w/docker-compose.yml'))
await act(async () => fileRow.click())
await settle()
assert.match(text(), /tmpfs: \/tmp:size=1g/)
await act(async () => copyBtn('file').click())
await settle()
assert.equal(sdk.copied.at(-1), 'tmpfs: /tmp:size=1g')
assert.ok(!button(/^Show source/), 'no Markdown toggle for a yml file')
const mdRow = [...document.querySelectorAll('[role=button]')].find(e => e.textContent.startsWith('/w/NOTES.md'))
await act(async () => mdRow.click())
await settle()
assert.ok(sdk.rendered.some(([how, p]) => how === 'chat' && p.text.startsWith('# Notes')), 'a .md file renders')
assert.equal(document.querySelector('pre')?.textContent?.startsWith('# Notes') ?? false, false)
await act(async () => button(/^Show source/).click())
assert.ok([...document.querySelectorAll('pre')].some(e => e.textContent.startsWith('# Notes')), 'and shows its source on request')

// --- Verification tab: accept wants two clicks
await act(async () => tabs()[2].click())
await settle()
assert.match(text(), /Independent verifier \(subagent\)done✓ PASSED/)
assert.match(text(), /The agent's own check✓ PASSED/)
assert.match(text(), /Reconciled✓ PASSED.*Both checks agree/)
await act(async () => copyBtn("verifier's report").click())
await settle()
assert.equal(sdk.copied.at(-1), 'VERDICT: passed\n\nThe compose file shows 1 GB.')
for (const t of ['VERDICT: passed\n\nThe compose file shows 1 GB.', 'Both checks agree: the fix is the tmpfs size, and the test passes.']) {
  assert.ok(sdk.rendered.some(([how, p]) => how === 'chat' && p.text === t), `rendered as Markdown: ${t.slice(0, 20)}`)
}
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
// --- Hermes still has the old build loaded: say so, and what to restart
buildAnswer = () => ({ api: BUILD, agent: '0.1.0' })
const rootOld = createRoot(document.getElementById('root'))
await act(async () => rootOld.render(contributions[0].render()))
await settle()
assert.match(document.querySelector('[role=alert]').textContent, /^Restart needed\. The agent side is still running search-board 0\.1\.0/)
await act(async () => rootOld.unmount())
buildAnswer = () => { const e = new Error('404: {"detail":"Not Found"}'); e.statusCode = 404; throw e }
const rootOlder = createRoot(document.getElementById('root'))
await act(async () => rootOlder.render(contributions[0].render()))
await settle()
assert.match(document.querySelector('[role=alert]').textContent, /older search-board backend \(before 0\.2\.0/)
await act(async () => rootOlder.unmount())
buildAnswer = () => { throw new Error('connection refused') }
const rootOffline = createRoot(document.getElementById('root'))
await act(async () => rootOffline.render(contributions[0].render()))
await settle()
assert.equal(document.querySelector('[role=alert]'), null, 'an unreachable /build says nothing about builds')
await act(async () => rootOffline.unmount())
buildAnswer = () => ({ api: BUILD, agent: BUILD })

// --- an older shell: Streamdown, with images and links shown as text, never fetched or followed
sdk.__setRenderers({ chat: null })
sdk.rendered.length = 0
const root3 = createRoot(document.getElementById('root'))
await act(async () => root3.render(contributions[0].render()))
await settle()
await act(async () => tabs()[1].click())
await settle()
const streamed = sdk.rendered.find(([how]) => how === 'streamdown')
assert.ok(streamed, 'falls back to Streamdown')
const { img, a } = streamed[1].components
const shownImg = img({ alt: 'key', src: '/home/user/.ssh/id_rsa' })
assert.notEqual(shownImg.type, 'img')
assert.equal(shownImg.props.children, '[image: key]')
const shownLink = a({ href: 'http://x.test/', children: 'site' })
assert.notEqual(shownLink.type, 'a')
await act(async () => root3.unmount())

// --- neither renderer: plain text, still readable
sdk.__setRenderers({ chat: null, streamdown: null })
const root4 = createRoot(document.getElementById('root'))
await act(async () => root4.render(contributions[0].render()))
await settle()
await act(async () => tabs()[1].click())
await settle()
assert.ok(document.querySelector('[data-md=plain]'))
assert.match(text(), /\*\*Raised\*\* the container's tmpfs/)
await act(async () => root4.unmount())

// --- a renderer that throws doesn't take the pane down
sdk.__setRenderers({ chat: () => { throw new Error('boom') } })
const origError = console.error
console.error = () => {}
const root5 = createRoot(document.getElementById('root'))
await act(async () => root5.render(contributions[0].render()))
await settle()
await act(async () => tabs()[1].click())
await settle()
console.error = origError
assert.match(text(), /Write-up · round 1/)
assert.match(text(), /\*\*Raised\*\* the container's tmpfs/)
await act(async () => root5.unmount())
sdk.__setRenderers()
console.log('pane: all checks passed')
