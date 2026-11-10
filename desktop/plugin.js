/**
 * search-board — the Desktop pane.
 *
 * Four tabs, always there: Board, Review, Verification, Complete.
 *
 * - Board: the search drawn as the tree it was searched as — the origin → intents → what
 *   each found → the intents that opened. Click a row for its full text below (a finding with
 *   the tool output it cites); fold a row with its caret. Leave notes for the agent, confirm
 *   or send back a proposal, extend a suspended board, abandon one.
 * - Review: the write-up and the files as delivered, your review notes with the agent's
 *   answers; send it back for another round, or on to verification.
 * - Verification: the independent verifier (a subagent), the agent's own check and the
 *   reconciled report; accept it, or send it back.
 * - Complete: the delivered work, once you've accepted it.
 *
 * Every move that can't be undone in one click wants a second click.
 *
 * All text in the pane can be selected and copied (the app makes its chrome unselectable; the pane
 * opts back in). Cards and the detail strip also have a Copy button for their text as written.
 *
 * Styling is inline, with the app's theme variables only: a disk plugin is loaded
 * uncompiled, so no utility class it uses can be counted on to exist. It reskins with
 * every theme, light or dark.
 *
 * Write-ups, reports, notes and answers render as Markdown with the chat's own renderer
 * (the SDK's MessageTextContent, preview-only: no images, no live directives). On a shell
 * without it, Streamdown with images and links shown as text; with neither, plain text.
 *
 * Data comes from this package's own backend (dashboard/plugin_api.py) through ctx.rest.
 * It refreshes on the plugin's `board.changed` event and polls every 5 s as a fallback.
 */

import * as sdk from '@hermes/plugin-sdk'
import { Component, useCallback, useEffect, useMemo, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const { PANES_AREA } = sdk
const EVENT = 'plugin.search-board.board.changed'
/** This pane's build; plugin.yaml's version (a test keeps them equal). */
export const BUILD = '0.2.0'

// ------------------------------------------------------------------ look

const C = {
  text: 'var(--ui-text-primary)',
  text2: 'var(--ui-text-secondary)',
  text3: 'var(--ui-text-tertiary)',
  line: 'var(--ui-stroke-secondary)',
  line2: 'var(--ui-stroke-tertiary)',
  accent: 'var(--ui-accent)',
  intent: 'var(--ui-blue)',
  supports: 'var(--ui-green)',
  rulesOut: 'var(--ui-red)',
  test: 'var(--ui-purple)',
  note: 'var(--ui-yellow)',
  review: 'var(--ui-orange)',
  origin: 'var(--ui-cyan, var(--ui-text-secondary))',
  card: 'var(--ui-bg-card)'
}
const tint = (color, pct) => `color-mix(in srgb, ${color} ${pct}%, transparent)`
const MONO = 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'

const STATUS = {
  open: ['open', C.intent],
  suspended: ['suspended', C.note],
  proposed_solved: ['proposed solved', C.test],
  proposed_exhausted: ['proposed exhausted', C.test],
  solved: ['solved', C.supports],
  exhausted: ['exhausted', C.rulesOut],
  abandoned: ['abandoned', C.text3]
}

const KINDS = { L: 'Intent', F: 'Finding', N: 'Note', R: 'Review note' }
export const label = id => (id && KINDS[id[0]] && /^\d+$/.test(id.slice(1)) ? `${KINDS[id[0]]} ${id.slice(1)}` : id)
const labels = ids => ids.map(label).join(', ')

const S = {
  pane: { display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0, fontSize: 13, color: C.text, userSelect: 'text', WebkitUserSelect: 'text', cursor: 'auto' },
  scroll: { flex: 1, minHeight: 0, overflow: 'auto' },
  muted: { color: C.text3 },
  pad: { padding: '10px 12px' },
  card: { background: C.card, border: `1px solid ${C.line}`, borderRadius: 10, padding: '10px 12px', margin: '8px 10px' },
  h: { fontSize: 11, fontWeight: 600, letterSpacing: '0.04em', textTransform: 'uppercase', color: C.text3, marginBottom: 6, userSelect: 'none' },
  pre: { whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontFamily: MONO, fontSize: 11.5, lineHeight: 1.5, color: C.text2, margin: 0 },
  prose: { whiteSpace: 'pre-wrap', wordBreak: 'break-word', lineHeight: 1.55, color: C.text },
  input: { flex: 1, minWidth: 0, background: 'transparent', color: C.text, border: `1px solid ${C.line}`, borderRadius: 7, padding: '5px 9px', fontSize: 12.5, outline: 'none' },
  row: { display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }
}

// ------------------------------------------------------------------ markdown

// What the agent writes is untrusted: never fetch an image or follow a link from it on
// the older-shell path. The alt text and the link text (with its target) are shown instead.
const SAFE_MD = {
  img: ({ alt, src }) => jsx('span', { style: { color: C.text3 }, children: `[image: ${alt || src || ''}]` }),
  a: ({ children, href }) => jsxs('span', { style: { color: C.accent }, title: href, children: [children, href ? ` (${href})` : null] })
}

class MdBoundary extends Component {
  constructor(props) { super(props); this.state = { failed: false } }
  static getDerivedStateFromError() { return { failed: true } }
  componentDidUpdate(prev) { if (prev.text !== this.props.text && this.state.failed) this.setState({ failed: false }) }
  render() { return this.state.failed ? jsx('div', { style: S.prose, children: this.props.text }) : this.props.children }
}

/** Markdown as the chat renders it, sized for the pane. */
export function Md({ text, muted = false, size = 13 }) {
  const body = String(text ?? '')
  if (!body.trim()) return null
  const Rich = sdk.MessageTextContent
  const Stream = sdk.Streamdown
  let inner
  if (Rich) inner = jsx(Rich, { text: body, media: false, previewOnly: true })
  else if (Stream) inner = jsx(Stream, { components: SAFE_MD, children: body })
  else return jsx('div', { 'data-md': 'plain', style: { ...S.prose, color: muted ? C.text2 : C.text }, children: body })
  return jsx('div', {
    'data-md': Rich ? 'chat' : 'streamdown',
    style: { '--conversation-text-font-size': `${size}px`, fontSize: size, minWidth: 0, color: muted ? C.text2 : C.text, overflowWrap: 'anywhere' },
    children: jsx(MdBoundary, { text: body, children: inner })
  })
}

const MD_FILE = /\.(md|markdown|mdx)$/i

/** One line of Markdown as plain text, for the tree's rows: no **, `, #, or link syntax. */
export function plainText(text) {
  return String(text ?? '')
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/(\*\*|__)(.+?)\1/g, '$2')
    .replace(/(^|[^*\w])[*_]([^*_\s][^*_]*?)[*_](?![*\w])/g, '$1$2')
    .replace(/`+([^`]+)`+/g, '$1')
    .replace(/^\s{0,3}(#{1,6}\s+|>\s?|[-*+]\s+)/gm, '')
    .replace(/\s*\n\s*/g, ' ')
    .trim()
}

// ------------------------------------------------------------------ selecting and copying

/** A click that ends a drag-to-select is the reader selecting text, not picking a row. */
function selectingText() {
  const sel = typeof window !== 'undefined' && window.getSelection ? window.getSelection() : null
  return !!sel && !sel.isCollapsed && sel.toString().trim() !== ''
}

async function copyText(text) {
  if (typeof sdk.writeClipboardText === 'function') return sdk.writeClipboardText(text)
  return navigator.clipboard.writeText(text)
}

/** "Copy" → "Copied": the raw text (Markdown source as written), not the rendering. */
export function CopyBtn({ text, what = 'text' }) {
  const [state, setState] = useState('idle')
  useEffect(() => {
    if (state === 'idle') return undefined
    const timer = setTimeout(() => setState('idle'), 1500)
    return () => clearTimeout(timer)
  }, [state])
  if (!text) return null
  return jsx('button', {
    type: 'button',
    title: `Copy the ${what}`,
    'data-copy': what,
    onClick: event => {
      event.stopPropagation()
      copyText(String(text)).then(() => setState('copied'), () => setState('failed'))
    },
    style: {
      fontSize: 11, fontWeight: 600, padding: '1px 7px', borderRadius: 6, cursor: 'pointer', userSelect: 'none',
      color: state === 'failed' ? C.rulesOut : state === 'copied' ? C.supports : C.text3,
      background: 'transparent', border: `1px solid ${C.line}`
    },
    children: state === 'copied' ? 'Copied' : state === 'failed' ? 'Copy failed' : 'Copy'
  })
}

function pill(text, color, { strong = false, key } = {}) {
  return jsx('span', {
    style: {
      display: 'inline-flex', alignItems: 'center', gap: 4, whiteSpace: 'nowrap',
      padding: '1px 8px', borderRadius: 999, fontSize: 11, fontWeight: 600, lineHeight: '18px', userSelect: 'none',
      color, background: tint(color, strong ? 22 : 13), border: `1px solid ${tint(color, 35)}`
    },
    children: text
  }, key)
}

function Btn({ onClick, children, disabled, title, type = 'button', tone = 'plain' }) {
  const color = tone === 'primary' ? C.accent : tone === 'danger' ? C.rulesOut : C.text2
  return jsx('button', {
    type, onClick, disabled, title,
    style: {
      padding: '5px 11px', borderRadius: 7, fontSize: 12, fontWeight: tone === 'plain' ? 500 : 600,
      cursor: disabled ? 'default' : 'pointer', opacity: disabled ? 0.45 : 1, color,
      background: tone === 'plain' ? 'transparent' : tint(color, 16), border: `1px solid ${tone === 'plain' ? C.line : tint(color, 45)}`
    },
    children
  })
}

function Empty({ title, children }) {
  return jsxs('div', {
    style: { ...S.card, borderStyle: 'dashed', textAlign: 'center', padding: '22px 16px' },
    children: [
      jsx('div', { style: { fontWeight: 600, marginBottom: 4 }, children: title }),
      jsx('div', { style: { ...S.muted, lineHeight: 1.5 }, children })
    ]
  })
}

function ago(stamp) {
  if (!stamp) return ''
  const seconds = Math.max(0, Math.round((Date.now() - Date.parse(stamp)) / 1000))
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`
  return `${Math.floor(seconds / 86400)} d ago`
}

// ------------------------------------------------------------------ the tree

function leadRank(lead) {
  const rank = { claimed: 0, open: 1, explored: 2, ruled_out: 3 }[lead.state] ?? 4
  return [rank, -lead.priority, Number(lead.id.slice(1)) || 0]
}

function byRank(a, b) {
  const x = leadRank(a)
  const y = leadRank(b)
  for (let i = 0; i < x.length; i++) if (x[i] !== y[i]) return x[i] - y[i]
  return 0
}

export function buildTree(snap) {
  const leads = Object.fromEntries(snap.leads.map(l => [l.id, l]))
  const findings = Object.fromEntries(snap.findings.map(f => [f.id, f]))
  const notes = Object.fromEntries(snap.notes.map(n => [n.id, n]))
  // The origin is the root entry: where the search starts. Seed intents, goal tests and board notes hang off it.
  const children = { board: ['origin'], origin: [] }
  const also = {}
  for (const id of [...Object.keys(leads), ...Object.keys(findings), ...Object.keys(notes)]) children[id] = []

  for (const lead of [...snap.leads].sort(byRank)) {
    const parents = lead.from_findings.filter(f => findings[f])
    children[parents[0] ?? 'origin'].push(lead.id)
    if (parents.length > 1) also[lead.id] = parents.slice(1)
  }
  for (const f of snap.findings) children[leads[f.from_lead] ? f.from_lead : 'origin'].push(f.id)
  for (const n of snap.notes) children[n.about && children[n.about] && n.about !== 'origin' ? n.about : 'origin'].push(n.id)

  return { board: snap.board, leads, findings, notes, children, also }
}

export function visibleRows(tree, folded) {
  const rows = []
  const walk = (id, depth) => {
    for (const child of tree.children[id] ?? []) {
      rows.push({ id: child, depth })
      if (!isFolded(tree, folded, child)) walk(child, depth + 1)
    }
  }
  walk('board', 0)
  return rows
}

function isFolded(tree, folded, id) {
  if (id in folded) return folded[id]
  // Ruled-out intents start folded, so they don't pile up; everything else starts open.
  return tree.leads[id]?.state === 'ruled_out'
}

/** What a row shows: its colour, its badge, its state chips, its text. */
function look(tree, id) {
  if (id === 'origin') {
    const b = tree.board ?? {}
    return { color: C.origin, badge: 'Origin', chips: [], text: b.origin ?? '', sub: b.goal ? `→ goal: ${b.goal}` : null }
  }
  const lead = tree.leads[id]
  if (lead) {
    const chips = [pill(`p${lead.priority}`, C.text3, { key: 'p' })]
    if (lead.state === 'claimed') chips.push(pill(`● exploring · ${lead.claimed_by ?? 'someone'}`, C.intent, { strong: true, key: 's' }))
    if (lead.state === 'explored') chips.push(pill('explored', C.supports, { key: 's' }))
    if (lead.state === 'ruled_out') chips.push(pill(`ruled out by ${label(lead.closed_by)}`, C.rulesOut, { key: 's' }))
    if (lead.state === 'open') chips.push(pill('open', C.intent, { key: 's' }))
    return { color: C.intent, badge: label(lead.id), chips, text: lead.direction, dim: lead.state === 'ruled_out' }
  }
  const f = tree.findings[id]
  if (f) {
    const test = f.author === 'plugin'
    const color = test ? C.test : f.polarity === 'supports' ? C.supports : C.rulesOut
    const chips = test
      ? [pill(f.polarity === 'supports' ? 'goal test passed' : 'goal test failed', f.polarity === 'supports' ? C.supports : C.rulesOut, { strong: true, key: 'v' })]
      : [pill(f.polarity === 'supports' ? 'supports' : 'rules out', color, { key: 'v' })]
    if (f.author === 'operator') chips.push(pill('by you', C.note, { key: 'a' }))
    if (f.evidence.length) chips.push(pill(f.evidence.map(e => `e${e}`).join(' '), C.text3, { key: 'e' }))
    return { color, badge: label(f.id), chips, text: f.statement }
  }
  const n = tree.notes[id]
  return {
    color: C.note, badge: label(n.id), text: n.text,
    chips: n.read ? [] : [pill('new', C.note, { strong: true, key: 'n' })]
  }
}

function Row({ tree, row, folded, selected, onSelect, onToggle }) {
  const kids = tree.children[row.id] ?? []
  const open = !isFolded(tree, folded, row.id)
  const l = look(tree, row.id)
  const also = tree.also[row.id]
  return jsxs('div', {
    role: 'treeitem',
    'aria-selected': selected,
    'aria-expanded': kids.length ? open : undefined,
    onClick: () => { if (!selectingText()) onSelect(row.id) },
    style: {
      display: 'flex', alignItems: 'flex-start', gap: 6,
      margin: '2px 8px', marginLeft: 8 + row.depth * 16, padding: '6px 8px 6px 6px', borderRadius: 8,
      borderLeft: `3px solid ${l.color}`,
      background: selected ? tint(C.accent, 14) : tint(l.color, 5),
      boxShadow: selected ? `inset 0 0 0 1px ${tint(C.accent, 45)}` : 'none',
      opacity: l.dim ? 0.6 : 1
    },
    children: [
      jsx('span', {
        onClick: event => {
          event.stopPropagation()
          if (kids.length) onToggle(row.id, open) // open now → fold it
        },
        style: { width: 12, flexShrink: 0, color: C.text3, userSelect: 'none', lineHeight: '20px', cursor: kids.length ? 'pointer' : 'default' },
        children: kids.length ? (open ? '▾' : '▸') : ''
      }),
      jsxs('div', {
        style: { minWidth: 0, flex: 1 },
        children: [
          jsxs('div', {
            style: { display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', marginBottom: 2 },
            children: [
              jsx('span', { style: { fontWeight: 700, color: l.color, fontSize: 12 }, children: l.badge }),
              ...l.chips
            ]
          }),
          jsx('div', {
            style: { color: C.text, lineHeight: 1.45, textDecoration: l.dim ? 'line-through' : 'none' },
            children: plainText(l.text) + (also ? ` (also from ${labels(also)})` : '')
          }),
          l.sub && jsx('div', { style: { color: C.text3, fontSize: 12, lineHeight: 1.4, marginTop: 2 }, children: plainText(l.sub) })
        ]
      })
    ]
  })
}

function Detail({ ctx, snap, tree, id }) {
  const [evidence, setEvidence] = useState([])
  const finding = tree.findings[id]

  useEffect(() => {
    let live = true
    setEvidence([])
    if (finding && finding.evidence.length) {
      ctx.rest(`/evidence?ids=${finding.evidence.join(',')}`)
        .then(body => live && setEvidence(body.evidence ?? []))
        .catch(() => {})
    }
    return () => { live = false }
  }, [ctx, id, finding?.evidence.join(',')])

  const b = snap.board
  let title
  let lines // [field name or null, text, markdown?]
  if (!id || id === 'board' || id === 'origin') {
    title = 'Origin'
    lines = [['origin', b.origin, true], ['goal', b.goal, true], [`test (${b.goal_test_kind})`, b.goal_test, false]]
    if (b.outcome) lines.push(['outcome', b.outcome, true])
  } else if (tree.leads[id]) {
    const l = tree.leads[id]
    title = `${label(l.id)} — ${l.state.replace('_', ' ')}, priority ${l.priority}`
    lines = [['direction', l.direction, true], ['rationale', l.rationale, true]]
    if (l.from_findings.length) lines.push(['came from', labels(l.from_findings), false])
    if (l.state === 'claimed') lines.push([null, `claimed by ${l.claimed_by ?? 'someone'}, ${ago(l.claimed_at)}`, false])
    if (l.closed_by) lines.push([l.state === 'explored' ? 'explored in' : 'ruled out by', label(l.closed_by), false])
  } else if (finding) {
    title = `${label(finding.id)} — ${finding.polarity.replace('_', ' ')}, ${finding.basis}, by ${finding.author === 'plugin' ? 'the goal test' : finding.author}, ${ago(finding.created_at)}`
    lines = [[null, finding.statement, true]]
    if (finding.from_lead) lines.push(['explored', label(finding.from_lead), false])
    if (finding.from_findings.length) lines.push(['inferred from', labels(finding.from_findings), false])
  } else if (tree.notes[id]) {
    const n = tree.notes[id]
    title = `${label(n.id)} — your note, ${ago(n.created_at)}${n.about ? `, about ${label(n.about)}` : ''}`
    lines = [[null, n.text, true], [null, n.read ? 'read by the agent' : 'not read yet', false]]
  } else {
    title = ''
    lines = []
  }

  return jsxs('div', {
    style: { ...S.card, maxHeight: '38%', overflow: 'auto', flexShrink: 0 },
    children: [
      jsxs('div', { style: { display: 'flex', alignItems: 'flex-start', gap: 8, marginBottom: 4 }, children: [
        jsx('div', { style: { fontWeight: 600, flex: 1, minWidth: 0 }, children: title }, 't'),
        jsx(CopyBtn, { text: [title, ...lines.map(([field, value]) => (field ? `${field}: ${value}` : value))].join('\n'), what: 'entry' }, 'c')
      ] }),
      ...lines.map(([field, value, md], i) => jsxs('div', {
        style: { color: C.text2, lineHeight: 1.5, marginTop: i ? 4 : 0 },
        children: md || field?.startsWith('test (')
          ? [
              field && jsx('div', { style: { ...S.muted, fontSize: 11 }, children: field }, 'f'),
              md ? jsx(Md, { text: value, muted: !!field }, 'v') : jsx('div', { style: { ...S.pre, fontSize: 12 }, children: value }, 'v')
            ]
          : [field && jsx('span', { style: S.muted, children: `${field}: ` }, 'f'), value]
      }, `l${i}`)),
      ...evidence.map(ev => jsxs('div', {
        style: { marginTop: 8, borderTop: `1px solid ${C.line2}`, paddingTop: 6 },
        children: [
          jsxs('div', { style: { display: 'flex', alignItems: 'center', gap: 8, marginBottom: 3 }, children: [
            jsx('div', { style: { ...S.muted, fontSize: 11, flex: 1 }, children: `e${ev.id} · ${ev.tool_name} · ${ev.chars} chars · ${ago(ev.created_at)}` }, 't'),
            jsx(CopyBtn, { text: ev.excerpt, what: `output of e${ev.id}` }, 'c')
          ] }),
          jsx('pre', { style: S.pre, children: ev.excerpt.slice(0, 2000) })
        ]
      }, `e${ev.id}`))
    ]
  })
}

// ------------------------------------------------------------------ moves

function useMove(ctx, onDone) {
  const [armed, setArmed] = useState(null)
  const [error, setError] = useState('')
  const run = useCallback(async (path, body) => {
    setError('')
    try {
      await ctx.rest(path, { method: 'POST', body: body ?? {} })
      setArmed(null)
      onDone()
    } catch (e) {
      setError(String(e?.message ?? e))
    }
  }, [ctx, onDone])
  // A move that can't be undone in one click wants a second.
  const twice = (key, text, path, body, tone = 'plain') => jsx(Btn, {
    tone,
    onClick: () => (armed === key ? run(path, body) : setArmed(key)),
    children: armed === key ? `${text}?` : text
  }, key)
  const errorLine = error && jsx('div', { style: { color: C.rulesOut, fontSize: 12 }, children: error })
  return { run, twice, errorLine }
}

function Bar({ children }) {
  return jsxs('div', { style: { display: 'flex', flexDirection: 'column', gap: 8, padding: '8px 10px 10px', borderTop: `1px solid ${C.line}`, flexShrink: 0 }, children })
}

function BoardActions({ ctx, snap, wf, selected, tree, onDone }) {
  const [text, setText] = useState('')
  const { run, twice, errorLine } = useMove(ctx, () => { setText(''); onDone() })
  const b = snap.board
  const about = tree.leads[selected] || tree.findings[selected] ? selected : null
  const name = encodeURIComponent(b.name)
  const closed = ['solved', 'exhausted', 'abandoned'].includes(b.status)
  const buttons = []
  // Once it is in the workflow, the proposal is decided in the Review and Verification tabs.
  const inFlow = wf && ['review', 'verification', 'deliverable'].includes(wf.flow.stage)
  if (!inFlow && b.status === 'proposed_solved') buttons.push(twice('confirm', 'Confirm solved', `/boards/${name}/decide`, { decision: 'confirm' }, 'primary'))
  if (!inFlow && b.status === 'proposed_exhausted') buttons.push(twice('exhaust', 'Close exhausted', `/boards/${name}/decide`, { decision: 'exhaust' }, 'primary'))
  if (!inFlow && b.status.startsWith('proposed_')) buttons.push(jsx(Btn, { title: 'Your note text becomes the reason', onClick: () => run(`/boards/${name}/decide`, { decision: 'reject', why: text }), children: 'Send back' }, 'r'))
  if (b.status === 'suspended') buttons.push(jsx(Btn, { tone: 'primary', onClick: () => run(`/boards/${name}/extend`, { steps: 20 }), children: 'Extend +20 steps' }, 'e'))
  if (tree.notes[selected]) buttons.push(jsx(Btn, { onClick: () => run(`/boards/${name}/unnote`, { id: selected }), children: `Take ${label(selected)} off` }, 'u'))
  if (!closed) buttons.push(twice('abandon', 'Abandon', `/boards/${name}/decide`, { decision: 'abandon', why: text }, 'danger'))

  return jsxs(Bar, {
    children: [
      !closed && jsxs('form', {
        style: { display: 'flex', gap: 6 },
        onSubmit: event => {
          event.preventDefault()
          if (text.trim()) run(`/boards/${name}/note`, { text: about ? `${about}: ${text}` : text })
        },
        children: [
          jsx('input', { style: S.input, placeholder: about ? `Note about ${label(about)}…` : 'Note for the agent…', value: text, onChange: event => setText(event.target.value) }),
          jsx(Btn, { type: 'submit', tone: 'primary', disabled: !text.trim(), children: 'Leave note' })
        ]
      }),
      inFlow && jsx('div', { style: { ...S.muted, fontSize: 12 }, children: `In ${wf.flow.stage === 'deliverable' ? 'Complete' : wf.flow.stage === 'review' ? 'your review' : 'verification'}: decide it in that tab.` }),
      buttons.length > 0 && jsx('div', { style: S.row, children: buttons }),
      errorLine
    ]
  })
}

// ------------------------------------------------------------------ the workflow tabs

function Files({ ctx, name, files }) {
  const [open, setOpen] = useState(null)
  const [content, setContent] = useState('')
  const [source, setSource] = useState(false)
  useEffect(() => {
    let live = true
    if (open) {
      ctx.rest(`/boards/${encodeURIComponent(name)}/file?path=${encodeURIComponent(open)}`)
        .then(body => live && setContent(body.content ?? ''))
        .catch(e => live && setContent(`(could not load: ${e?.message ?? e})`))
    }
    return () => { live = false }
  }, [ctx, name, open])
  if (!files.length) return jsx('div', { style: S.muted, children: 'No files delivered.' })
  return jsxs('div', {
    children: [
      ...files.map(f => jsxs('div', {
        role: 'button',
        onClick: () => { setSource(false); setOpen(open === f.path ? null : f.path) },
        style: {
          display: 'flex', alignItems: 'center', gap: 8, padding: '5px 8px', margin: '3px 0', borderRadius: 7, cursor: 'pointer',
          background: open === f.path ? tint(C.accent, 14) : tint(C.text3, 6), fontFamily: MONO, fontSize: 12
        },
        children: [
          jsx('span', { style: { flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, children: f.path }),
          jsx('span', { style: { ...S.muted, fontSize: 11 }, children: `${f.chars} chars${f.truncated ? ' · cut' : ''}` }),
          f.error && pill('not kept', C.rulesOut)
        ]
      }, f.path)),
      open && jsxs('div', { style: { display: 'flex', justifyContent: 'flex-end', alignItems: 'center', gap: 10, marginTop: 6 }, children: [
        MD_FILE.test(open) && jsx('button', {
          type: 'button', onClick: () => setSource(!source),
          style: { fontSize: 11, color: C.accent, background: 'none', border: 'none', padding: 0, cursor: 'pointer' },
          children: source ? 'Show rendered' : 'Show source'
        }, 's'),
        jsx(CopyBtn, { text: content, what: 'file' }, 'c')
      ] }),
      open && (MD_FILE.test(open) && !source
        ? jsx('div', { style: { maxHeight: 320, overflow: 'auto', marginTop: 4, padding: '6px 10px', borderRadius: 7, background: tint(C.text3, 6) }, children: jsx(Md, { text: content }) })
        : jsx('pre', { style: { ...S.pre, maxHeight: 260, overflow: 'auto', marginTop: 6, padding: 8, borderRadius: 7, background: tint(C.text3, 6) }, children: content }))
    ]
  })
}

function Notes({ notes, canTakeBack, onTakeBack }) {
  if (!notes.length) return jsx('div', { style: S.muted, children: 'No review notes.' })
  return jsx('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 8 },
    children: notes.map(n => jsxs('div', {
      children: [
        jsxs('div', {
          style: { marginLeft: 28, padding: '7px 10px', borderRadius: '10px 10px 2px 10px', background: tint(C.review, 13), border: `1px solid ${tint(C.review, 35)}` },
          children: [
            jsxs('div', { style: { display: 'flex', gap: 6, alignItems: 'center', marginBottom: 2 }, children: [
              jsx('span', { style: { fontWeight: 700, color: C.review, fontSize: 12 }, children: `${label(n.id)} · you · round ${n.round}` }),
              !n.answer && pill('awaiting answer', C.review)
            ] }),
            jsx(Md, { text: n.text }),
            !n.answer && canTakeBack && jsx('button', { type: 'button', onClick: () => onTakeBack(n.id), style: { marginTop: 4, fontSize: 11, color: C.text3, background: 'none', border: 'none', padding: 0, cursor: 'pointer' }, children: 'take back' })
          ]
        }),
        n.answer && jsxs('div', {
          style: { marginRight: 28, marginTop: 4, padding: '7px 10px', borderRadius: '10px 10px 10px 2px', background: tint(C.intent, 10), border: `1px solid ${tint(C.intent, 30)}` },
          children: [
            jsx('div', { style: { fontWeight: 700, color: C.intent, fontSize: 12, marginBottom: 2 }, children: 'agent' }),
            jsx(Md, { text: n.answer })
          ]
        })
      ]
    }, n.id))
  })
}

function Card({ title, right, children, color, copy, copyWhat }) {
  return jsxs('div', {
    style: { ...S.card, borderTop: color ? `3px solid ${color}` : S.card.border },
    children: [
      jsxs('div', { style: { display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }, children: [
        jsx('div', { style: { ...S.h, marginBottom: 0, flex: 1 }, children: title }, 't'), right,
        copy ? jsx(CopyBtn, { text: copy, what: copyWhat ?? String(title).toLowerCase() }, 'c') : null
      ] }),
      children
    ]
  })
}

function verdictPill(verdict, pending = 'pending') {
  if (verdict === 'passed') return pill('✓ PASSED', C.supports, { strong: true })
  if (verdict === 'failed') return pill('✗ FAILED', C.rulesOut, { strong: true })
  return pill(pending, C.text3)
}

function ReviewTab({ ctx, board, wf, onDone }) {
  const [text, setText] = useState('')
  const { run, twice, errorLine } = useMove(ctx, () => { setText(''); onDone() })
  const name = encodeURIComponent(board.name)
  const stage = wf.flow.stage
  const latest = wf.reports[wf.reports.length - 1]
  if (!latest) {
    return jsx('div', { style: S.scroll, children: jsx(Empty, {
      title: wf.write_up_due ? 'A write-up is due' : 'No write-up yet',
      children: wf.write_up_due
        ? 'The board is finished. The agent writes it up next, with the files that make up the work, and it lands here for your review.'
        : 'When the board is finished (solved by its goal test, or proposed solved or exhausted), the agent writes it up and it comes here for your review.'
    }) })
  }
  const exhausted = board.status === 'proposed_exhausted'
  const buttons = []
  if (stage === 'review') {
    buttons.push(exhausted
      ? twice('exhaust', 'Close exhausted', `/boards/${name}/decide`, { decision: 'exhaust' }, 'primary')
      : twice('verify', 'Send to verification', `/boards/${name}/verify`, undefined, 'primary'))
    buttons.push(twice('back', 'Send back to work', `/boards/${name}/back`, { why: text }))
    buttons.push(twice('discard', 'Discard', `/boards/${name}/discard`, { why: text }, 'danger'))
  }
  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 },
    children: [
      jsxs('div', {
        style: S.scroll,
        children: [
          stage !== 'review' && jsx('div', { style: { ...S.pad, ...S.muted }, children: `Reviewed: the board is ${stage === 'working' ? `back with the agent (round ${wf.flow.round})` : stage === 'deliverable' ? 'complete' : stage === 'verification' ? 'in verification' : stage}.` }),
          jsx(Card, {
            title: `Write-up · round ${latest.round}`, color: C.accent, copy: latest.text, copyWhat: 'write-up',
            right: jsx('span', { style: { ...S.muted, fontSize: 11 }, children: `${ago(latest.at)}${wf.reports.length > 1 ? ` · ${wf.reports.length - 1} earlier round(s)` : ''}` }),
            children: jsx(Md, { text: latest.text })
          }),
          jsx(Card, { title: 'Files as delivered', children: jsx(Files, { ctx, name: board.name, files: wf.files }) }),
          jsx(Card, { title: 'Review notes', color: C.review, children: jsx(Notes, { notes: wf.notes, canTakeBack: stage === 'review', onTakeBack: id => run(`/boards/${name}/uncomment`, { id }) }) })
        ]
      }),
      stage === 'review' && jsxs(Bar, {
        children: [
          jsxs('form', {
            style: { display: 'flex', gap: 6 },
            onSubmit: event => { event.preventDefault(); if (text.trim()) run(`/boards/${name}/comment`, { text }) },
            children: [
              jsx('input', { style: S.input, placeholder: 'A question or idea for the agent…', value: text, onChange: event => setText(event.target.value) }),
              jsx(Btn, { type: 'submit', tone: 'primary', disabled: !text.trim(), children: 'Add note' })
            ]
          }),
          jsx('div', { style: S.row, children: buttons }),
          errorLine
        ]
      })
    ]
  })
}

function VerificationTab({ ctx, board, wf, onDone }) {
  const [why, setWhy] = useState('')
  const { run, twice, errorLine } = useMove(ctx, () => { setWhy(''); onDone() })
  const name = encodeURIComponent(board.name)
  const f = wf.flow
  if (f.stage !== 'verification' && !f.verification) {
    return jsx('div', { style: S.scroll, children: jsx(Empty, {
      title: 'Not in verification',
      children: 'From your review, send the board to verification. A fresh subagent checks the work on the machine, the agent checks it on its own, and the agent reconciles the two for you to accept.'
    }) })
  }
  const verifierState = { wanted: 'starting at the agent’s next turn', running: '● running', done: 'done', failed: 'failed' }[f.verifier_state] ?? 'stopped'
  const buttons = []
  if (f.stage === 'verification') {
    if (f.verification) buttons.push(twice('accept', 'Accept as complete', `/boards/${name}/accept`, undefined, 'primary'))
    if (f.verifier_state === 'failed' && !f.verification) buttons.push(jsx(Btn, { onClick: () => run(`/boards/${name}/reverify`), children: 'Run the verifier again' }, 'rv'))
    buttons.push(twice('back', 'Send back to work', `/boards/${name}/back`, { why }))
    buttons.push(jsx(Btn, { onClick: () => run(`/boards/${name}/toreview`, { why }), children: 'Back to review' }, 'tr'))
    buttons.push(twice('discard', 'Discard', `/boards/${name}/discard`, { why }, 'danger'))
  }
  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 },
    children: [
      jsxs('div', {
        style: S.scroll,
        children: [
          jsx(Card, {
            title: 'Independent verifier (subagent)', color: C.test, copy: f.verifier_report, copyWhat: "verifier's report",
            right: jsxs('span', { style: S.row, children: [pill(verifierState, C.test), verdictPill(f.verifier_verdict, '—')] }),
            children: [
              f.verifier_note && jsx('div', { style: { ...S.muted, marginBottom: f.verifier_report ? 6 : 0 }, children: f.verifier_note }, 'n'),
              f.verifier_report ? jsx(Md, { text: f.verifier_report }, 'r') : !f.verifier_note && jsx('div', { style: S.muted, children: 'No report yet.' }, 'e')
            ]
          }),
          jsx(Card, {
            title: "The agent's own check", color: C.intent, copy: f.own_report, copyWhat: "agent's check",
            right: verdictPill(f.own_verdict, 'not filed yet'),
            children: f.own_report ? jsx(Md, { text: f.own_report }) : jsx('div', { style: S.muted, children: '—' })
          }),
          jsx(Card, {
            title: 'Reconciled', color: C.accent, copy: f.verification, copyWhat: 'reconciled report',
            right: verdictPill(f.verdict, 'not yet'),
            children: f.verification ? jsx(Md, { text: f.verification }) : jsx('div', { style: S.muted, children: 'The agent reconciles the two checks once both are in.' })
          })
        ]
      }),
      buttons.length > 0 && jsxs(Bar, {
        children: [
          jsx('input', { style: S.input, placeholder: 'Why (for send back, back to review, discard)…', value: why, onChange: event => setWhy(event.target.value) }),
          jsx('div', { style: S.row, children: buttons }),
          errorLine
        ]
      })
    ]
  })
}

function CompleteTab({ ctx, board, wf, onDone }) {
  const [why, setWhy] = useState('')
  const { run, twice, errorLine } = useMove(ctx, () => { setWhy(''); onDone() })
  const name = encodeURIComponent(board.name)
  const f = wf.flow
  if (f.stage !== 'deliverable') {
    return jsx('div', { style: S.scroll, children: jsx(Empty, {
      title: 'Not complete yet',
      children: 'A board is complete once you accept its verification (or close it exhausted, or confirm it). Then the delivered work lives here: the final write-up, the verification and the files.'
    }) })
  }
  const latest = wf.reports[wf.reports.length - 1]
  const ok = board.status === 'solved'
  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 },
    children: [
      jsxs('div', {
        style: S.scroll,
        children: [
          jsxs('div', {
            style: { ...S.card, textAlign: 'center', padding: '16px 12px', background: tint(ok ? C.supports : C.rulesOut, 12), border: `1px solid ${tint(ok ? C.supports : C.rulesOut, 40)}` },
            children: [
              jsx('div', { style: { fontSize: 22, color: ok ? C.supports : C.rulesOut }, children: ok ? '✓' : '∅' }),
              jsx('div', { style: { fontWeight: 700, fontSize: 15, color: ok ? C.supports : C.rulesOut }, children: ok ? 'Complete' : 'Closed exhausted' }),
              jsx('div', { style: { ...S.muted, marginTop: 2 }, children: `${f.verdict ? `verified (${f.verdict}) · ` : ''}accepted ${ago(f.accepted_at)}` })
            ]
          }),
          latest && jsx(Card, { title: `Final write-up · round ${latest.round}`, color: C.accent, copy: latest.text, copyWhat: 'write-up', children: jsx(Md, { text: latest.text }) }),
          f.verification && jsx(Card, { title: 'Verification', color: C.test, right: verdictPill(f.verdict), copy: f.verification, copyWhat: 'verification', children: jsx(Md, { text: f.verification }) }),
          jsx(Card, { title: 'Files as delivered', children: jsx(Files, { ctx, name: board.name, files: wf.files }) }),
          wf.notes.length > 0 && jsx(Card, { title: 'Review', color: C.review, children: jsx(Notes, { notes: wf.notes, canTakeBack: false }) })
        ]
      }),
      jsxs(Bar, {
        children: [
          jsx('input', { style: S.input, placeholder: 'Why (for reopen, back to review)…', value: why, onChange: event => setWhy(event.target.value) }),
          jsx('div', { style: S.row, children: [
            twice('back', 'Reopen', `/boards/${name}/back`, { why }),
            board.status !== 'exhausted' && twice('verify', 'Verify again', `/boards/${name}/verify`),
            jsx(Btn, { onClick: () => run(`/boards/${name}/toreview`, { why }), children: 'Back to review' }, 'tr')
          ] }),
          errorLine
        ]
      })
    ]
  })
}

// ------------------------------------------------------------------ the head

const STEPS = [['search', 'Search'], ['write-up', 'Write-up'], ['review', 'Review'], ['verification', 'Verify'], ['deliverable', 'Complete']]

function stepOf(board, wf) {
  if (!wf) return 'search'
  const stage = wf.flow.stage
  if (stage === 'working') return wf.write_up_due ? 'write-up' : 'search'
  return stage
}

function Stepper({ board, wf }) {
  const current = stepOf(board, wf)
  const at = STEPS.findIndex(([key]) => key === current)
  return jsx('div', {
    style: { display: 'flex', alignItems: 'center', gap: 4, margin: '8px 0 2px', userSelect: 'none' },
    children: STEPS.map(([key, text], i) => {
      const done = i < at
      const now = i === at
      const color = done ? C.supports : now ? C.accent : C.text3
      return jsxs('div', {
        style: { display: 'flex', alignItems: 'center', gap: 4, flex: i < STEPS.length - 1 ? 1 : 'none' },
        children: [
          jsx('span', { style: { width: 16, height: 16, borderRadius: 999, flexShrink: 0, display: 'inline-flex', alignItems: 'center', justifyContent: 'center', fontSize: 10, fontWeight: 700, color: done || now ? 'var(--ui-bg-editor)' : C.text3, background: done || now ? color : 'transparent', border: `1.5px solid ${color}` }, children: done ? '✓' : i + 1 }),
          jsx('span', { style: { fontSize: 11, fontWeight: now ? 700 : 500, color, whiteSpace: 'nowrap' }, children: text }),
          i < STEPS.length - 1 && jsx('span', { style: { flex: 1, height: 1.5, minWidth: 6, background: done ? C.supports : C.line } })
        ]
      }, key)
    })
  })
}

function Head({ board: b, wf, boards, name, onPick }) {
  const [statusText, statusColor] = STATUS[b.status] ?? [b.status, C.text3]
  const stepsPct = Math.min(100, Math.round((b.steps_used / Math.max(b.step_budget, 1)) * 100))
  return jsxs('div', {
    style: { padding: '10px 12px 8px', borderBottom: `1px solid ${C.line}`, flexShrink: 0, background: tint(statusColor, 5) },
    children: [
      jsx('select', {
        value: name,
        onChange: event => onPick(event.target.value),
        style: { width: '100%', background: 'transparent', color: C.text2, border: `1px solid ${C.line}`, borderRadius: 7, padding: '3px 6px', fontSize: 12, marginBottom: 8 },
        children: boards.map(x => jsx('option', {
          value: x.name,
          children: `${x.workflow === 'in review' || (x.workflow ?? '').startsWith('verified') ? '● ' : ''}${x.name} — ${x.workflow ?? (STATUS[x.status]?.[0] ?? x.status)}`
        }, x.name))
      }),
      jsxs('div', { style: { display: 'flex', alignItems: 'flex-start', gap: 8 }, children: [
        jsx('div', { style: { flex: 1, fontWeight: 700, fontSize: 15, lineHeight: 1.3 }, children: b.title }),
        pill(statusText, statusColor, { strong: true })
      ] }),
      jsx('div', { style: { ...S.muted, fontSize: 12, marginTop: 3 }, children: `${b.name}${b.workflow ? ` · ${b.workflow}` : ''}` }),
      jsx(Stepper, { board: b, wf }),
      jsxs('div', { style: { ...S.row, marginTop: 8 }, children: [
        pill(`${b.frontier} open intent${b.frontier === 1 ? '' : 's'}`, C.intent),
        pill(`${b.findings} finding${b.findings === 1 ? '' : 's'}`, C.supports),
        b.unread_notes ? pill(`${b.unread_notes} note${b.unread_notes === 1 ? '' : 's'} unread`, C.note, { strong: true }) : null,
        jsxs('div', { style: { display: 'flex', alignItems: 'center', gap: 6, flex: 1, minWidth: 110 }, children: [
          jsx('div', { style: { flex: 1, height: 6, borderRadius: 999, background: tint(C.text3, 18), overflow: 'hidden' }, children:
            jsx('div', { style: { width: `${stepsPct}%`, height: '100%', borderRadius: 999, background: stepsPct >= 85 ? C.note : C.accent } }) }),
          jsx('span', { style: { ...S.muted, fontSize: 11, whiteSpace: 'nowrap' }, children: `steps ${b.steps_used}/${b.step_budget}` })
        ] })
      ] })
    ]
  })
}

const TABS = [['board', 'Board'], ['review', 'Review'], ['verification', 'Verification'], ['complete', 'Complete']]

function waitingTab(wf) {
  if (!wf) return null
  if (wf.flow.stage === 'review') return 'review'
  if (wf.flow.stage === 'verification' && wf.flow.verification) return 'verification'
  return null
}

function Tabs({ shown, onPick, wf }) {
  const waiting = waitingTab(wf)
  return jsx('div', {
    role: 'tablist',
    style: { display: 'flex', borderBottom: `1px solid ${C.line}`, padding: '0 8px', flexShrink: 0 },
    children: TABS.map(([key, text]) => jsxs('button', {
      type: 'button', role: 'tab', 'aria-selected': shown === key,
      onClick: () => onPick(key),
      style: {
        position: 'relative', padding: '8px 10px', fontSize: 12.5, fontWeight: shown === key ? 700 : 500, cursor: 'pointer',
        color: shown === key ? C.accent : C.text2, background: 'none', borderStyle: 'solid', borderWidth: '0 0 2px 0',
        borderColor: shown === key ? C.accent : 'transparent', marginBottom: -1
      },
      children: [text, waiting === key && jsx('span', { title: 'waiting for you', style: { position: 'absolute', top: 6, right: 2, width: 7, height: 7, borderRadius: 999, background: C.review } })]
    }, key))
  })
}

// ------------------------------------------------------------------ the pane

// ------------------------------------------------------------------ stale builds

function newer(a, b) {
  const pa = String(a).split('.').map(n => parseInt(n, 10) || 0)
  const pb = String(b).split('.').map(n => parseInt(n, 10) || 0)
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    if ((pa[i] ?? 0) !== (pb[i] ?? 0)) return (pa[i] ?? 0) > (pb[i] ?? 0)
  }
  return false
}

/** What to tell the operator when Hermes has a different build loaded than this pane, or null.
 *  `info` is /build's answer, or { api: null } when this backend has no /build (older than 0.2.0). */
export function staleBuild(info) {
  if (!info) return null
  const restart = 'Run `hermes gateway restart` (or quit and reopen Hermes)'
  if (!info.api || newer(BUILD, info.api)) {
    return `Hermes is still running an older search-board backend (${info.api || 'before 0.2.0'}; this pane is ${BUILD}). ${restart}, then reopen this pane.`
  }
  if (newer(info.api, BUILD)) {
    return `This pane is an older search-board build (${BUILD}; installed: ${info.api}). In Capabilities → Plugins, switch search-board's desktop half off and on.`
  }
  if (info.agent && info.agent !== BUILD) {
    return `The agent side is still running search-board ${info.agent} (this pane is ${BUILD}), so tools, hooks and the verifier are the old ones. ${restart}.`
  }
  return null
}

function StaleBanner({ text }) {
  return jsxs('div', {
    role: 'alert',
    style: { margin: '8px 10px 0', padding: '8px 10px', borderRadius: 8, fontSize: 12, lineHeight: 1.45,
      color: C.text, background: tint(C.review, 12), border: `1px solid ${tint(C.review, 40)}` },
    children: [jsx('span', { style: { fontWeight: 700, color: C.review }, children: 'Restart needed. ' }, 'h'), text]
  })
}

function BoardPane({ ctx }) {
  const [boards, setBoards] = useState([])
  const [name, setName] = useState(() => ctx.storage.get('board', ''))
  const [snap, setSnap] = useState(null)
  const [wf, setWf] = useState(null)
  const [tab, setTab] = useState('board')
  const [selected, setSelected] = useState('origin')
  const [folded, setFolded] = useState({})
  const [tick, setTick] = useState(0)
  const [failure, setFailure] = useState('')
  const [buildInfo, setBuildInfo] = useState(null)
  const refresh = useCallback(() => setTick(t => t + 1), [])

  useEffect(() => {
    let live = true
    ctx.rest('/build')
      .then(body => live && setBuildInfo({ api: body?.api ?? null, agent: body?.agent ?? null }))
      .catch(e => {
        // A backend from before 0.2.0 has no /build; anything else (offline, ...) says nothing about builds.
        if (live && (e?.statusCode === 404 || /^404\b|not found/i.test(String(e?.message ?? e)))) setBuildInfo({ api: null, agent: null })
      })
    return () => { live = false }
  }, [ctx, tick])

  useEffect(() => {
    const stopEvent = ctx.onEvent(EVENT, refresh)
    const stopPoll = ctx.setInterval(refresh, 5000)
    return () => { stopEvent(); stopPoll() }
  }, [ctx, refresh])

  useEffect(() => {
    let live = true
    ctx.rest('/boards?all=true')
      .then(body => {
        if (!live) return
        const list = body.boards ?? []
        setBoards(list)
        setFailure('')
        if (!list.some(b => b.name === name) && list.length) {
          const first = list.find(b => !['solved', 'exhausted', 'abandoned'].includes(b.status) || b.workflow) ?? list[0]
          setName(first.name)
        }
      })
      .catch(e => live && setFailure(`Can't reach the search-board backend: ${e?.message ?? e}. Is the plugin enabled for this profile?`))
    return () => { live = false }
  }, [ctx, tick])

  useEffect(() => {
    if (!name) { setSnap(null); return }
    ctx.storage.set('board', name)
    let live = true
    ctx.rest(`/boards/${encodeURIComponent(name)}`)
      .then(body => live && setSnap(body))
      .catch(() => live && setSnap(null))
    ctx.rest(`/boards/${encodeURIComponent(name)}/workflow`)
      .then(body => live && setWf(body))
      .catch(() => live && setWf(null))
    return () => { live = false }
  }, [ctx, name, tick])

  const tree = useMemo(() => (snap ? buildTree(snap) : null), [snap])
  const rows = useMemo(() => (tree ? visibleRows(tree, folded) : []), [tree, folded])

  if (failure) return jsx('div', { style: { ...S.pad, ...S.muted }, children: failure })
  if (!boards.length) {
    return jsx(Empty, {
      title: 'No search boards yet',
      children: 'The agent opens one with search_board_open when a problem has a testable goal and no known path.'
    })
  }
  const b = snap?.board
  const pick = value => { setName(value); setSelected('origin'); setFolded({}); setTab('board') }

  return jsxs('div', {
    // The app makes its chrome unselectable; the pane's text is content, so it opts back in.
    'data-selectable-text': 'true',
    style: S.pane,
    children: [
      staleBuild(buildInfo) && jsx(StaleBanner, { text: staleBuild(buildInfo) }),
      b && jsx(Head, { board: b, wf, boards, name, onPick: pick }),
      jsx(Tabs, { shown: tab, onPick: setTab, wf }),
      tab === 'board' && jsx('div', {
        role: 'tree',
        style: { ...S.scroll, padding: '6px 0' },
        children: tree
          ? [
              ...rows.map(row => jsx(Row, {
                tree, row, folded,
                selected: selected === row.id,
                onSelect: setSelected,
                onToggle: (id, fold) => setFolded(f => ({ ...f, [id]: fold }))
              }, row.id)),
              !tree.children.origin.length && jsx('div', { style: { ...S.muted, margin: '4px 8px 0 32px', fontSize: 12 }, children: 'No intents declared yet, nothing found.' }, 'empty')
            ]
          : jsx(Empty, { title: 'Nothing yet', children: 'No intents declared, nothing found.' })
      }),
      tab === 'board' && snap && jsx(Detail, { ctx, snap, tree, id: selected }),
      tab === 'board' && snap && jsx(BoardActions, { ctx, snap, wf, selected, tree, onDone: refresh }),
      tab === 'review' && b && wf && jsx(ReviewTab, { ctx, board: b, wf, onDone: refresh }),
      tab === 'verification' && b && wf && jsx(VerificationTab, { ctx, board: b, wf, onDone: refresh }),
      tab === 'complete' && b && wf && jsx(CompleteTab, { ctx, board: b, wf, onDone: refresh })
    ]
  })
}

export default {
  id: 'search-board',
  name: 'Search boards',
  register(ctx) {
    ctx.register({
      id: 'pane',
      area: PANES_AREA,
      title: 'search boards',
      data: { placement: 'right', width: '420px' },
      render: () => jsx(BoardPane, { ctx })
    })
  }
}
