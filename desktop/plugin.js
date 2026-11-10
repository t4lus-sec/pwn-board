/**
 * search-board — the Desktop pane.
 *
 * Four tabs, always there: Board, Review, Verification, Complete.
 *
 * - Board: the search drawn as the tree it was searched as — goal → intents → what each
 *   found → the intents that opened. Click a row for its full text below (a finding with
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
 * Styling is inline, with the app's theme variables only: a disk plugin is loaded
 * uncompiled, so no utility class it uses can be counted on to exist. It reskins with
 * every theme, light or dark.
 *
 * Data comes from this package's own backend (dashboard/plugin_api.py) through ctx.rest.
 * It refreshes on the plugin's `board.changed` event and polls every 5 s as a fallback.
 */

import { PANES_AREA } from '@hermes/plugin-sdk'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const EVENT = 'plugin.search-board.board.changed'

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
  pane: { display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0, fontSize: 13, color: C.text },
  scroll: { flex: 1, minHeight: 0, overflow: 'auto' },
  muted: { color: C.text3 },
  pad: { padding: '10px 12px' },
  card: { background: C.card, border: `1px solid ${C.line}`, borderRadius: 10, padding: '10px 12px', margin: '8px 10px' },
  h: { fontSize: 11, fontWeight: 600, letterSpacing: '0.04em', textTransform: 'uppercase', color: C.text3, marginBottom: 6 },
  pre: { whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontFamily: MONO, fontSize: 11.5, lineHeight: 1.5, color: C.text2, margin: 0 },
  prose: { whiteSpace: 'pre-wrap', wordBreak: 'break-word', lineHeight: 1.55, color: C.text },
  input: { flex: 1, minWidth: 0, background: 'transparent', color: C.text, border: `1px solid ${C.line}`, borderRadius: 7, padding: '5px 9px', fontSize: 12.5, outline: 'none' },
  row: { display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }
}

function pill(text, color, { strong = false, key } = {}) {
  return jsx('span', {
    style: {
      display: 'inline-flex', alignItems: 'center', gap: 4, whiteSpace: 'nowrap',
      padding: '1px 8px', borderRadius: 999, fontSize: 11, fontWeight: 600, lineHeight: '18px',
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
  const children = { board: [] }
  const also = {}
  for (const id of [...Object.keys(leads), ...Object.keys(findings), ...Object.keys(notes)]) children[id] = []

  for (const lead of [...snap.leads].sort(byRank)) {
    const parents = lead.from_findings.filter(f => findings[f])
    children[parents[0] ?? 'board'].push(lead.id)
    if (parents.length > 1) also[lead.id] = parents.slice(1)
  }
  for (const f of snap.findings) children[leads[f.from_lead] ? f.from_lead : 'board'].push(f.id)
  for (const n of snap.notes) children[n.about && children[n.about] ? n.about : 'board'].push(n.id)

  return { leads, findings, notes, children, also }
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
    onClick: () => onSelect(row.id),
    style: {
      display: 'flex', alignItems: 'flex-start', gap: 6, cursor: 'default',
      margin: '2px 8px', marginLeft: 8 + row.depth * 18, padding: '6px 8px 6px 6px', borderRadius: 8,
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
            children: l.text + (also ? ` (also from ${labels(also)})` : '')
          })
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
  let lines
  if (!id || id === 'board') {
    title = 'The board'
    lines = [`origin: ${b.origin}`, `goal: ${b.goal}`, `test (${b.goal_test_kind}): ${b.goal_test}`]
    if (b.outcome) lines.push(`outcome: ${b.outcome}`)
  } else if (tree.leads[id]) {
    const l = tree.leads[id]
    title = `${label(l.id)} — ${l.state.replace('_', ' ')}, priority ${l.priority}`
    lines = [`direction: ${l.direction}`, `rationale: ${l.rationale}`]
    if (l.from_findings.length) lines.push(`came from: ${labels(l.from_findings)}`)
    if (l.state === 'claimed') lines.push(`claimed by ${l.claimed_by ?? 'someone'}, ${ago(l.claimed_at)}`)
    if (l.closed_by) lines.push(`${l.state === 'explored' ? 'explored in' : 'ruled out by'} ${label(l.closed_by)}`)
  } else if (finding) {
    title = `${label(finding.id)} — ${finding.polarity.replace('_', ' ')}, ${finding.basis}, by ${finding.author === 'plugin' ? 'the goal test' : finding.author}, ${ago(finding.created_at)}`
    lines = [finding.statement]
    if (finding.from_lead) lines.push(`explored: ${label(finding.from_lead)}`)
    if (finding.from_findings.length) lines.push(`inferred from: ${labels(finding.from_findings)}`)
  } else if (tree.notes[id]) {
    const n = tree.notes[id]
    title = `${label(n.id)} — your note, ${ago(n.created_at)}${n.about ? `, about ${label(n.about)}` : ''}`
    lines = [n.text, n.read ? 'read by the agent' : 'not read yet']
  } else {
    title = ''
    lines = []
  }

  return jsxs('div', {
    style: { ...S.card, maxHeight: '38%', overflow: 'auto', flexShrink: 0 },
    children: [
      jsx('div', { style: { fontWeight: 600, marginBottom: 4 }, children: title }),
      ...lines.map((line, i) => jsx('div', { style: { color: C.text2, lineHeight: 1.5 }, children: line }, `l${i}`)),
      ...evidence.map(ev => jsxs('div', {
        style: { marginTop: 8, borderTop: `1px solid ${C.line2}`, paddingTop: 6 },
        children: [
          jsx('div', { style: { ...S.muted, fontSize: 11, marginBottom: 3 }, children: `e${ev.id} · ${ev.tool_name} · ${ev.chars} chars · ${ago(ev.created_at)}` }),
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
        onClick: () => setOpen(open === f.path ? null : f.path),
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
      open && jsx('pre', { style: { ...S.pre, maxHeight: 260, overflow: 'auto', marginTop: 6, padding: 8, borderRadius: 7, background: tint(C.text3, 6) }, children: content })
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
            jsx('div', { style: S.prose, children: n.text }),
            !n.answer && canTakeBack && jsx('button', { type: 'button', onClick: () => onTakeBack(n.id), style: { marginTop: 4, fontSize: 11, color: C.text3, background: 'none', border: 'none', padding: 0, cursor: 'pointer' }, children: 'take back' })
          ]
        }),
        n.answer && jsxs('div', {
          style: { marginRight: 28, marginTop: 4, padding: '7px 10px', borderRadius: '10px 10px 10px 2px', background: tint(C.intent, 10), border: `1px solid ${tint(C.intent, 30)}` },
          children: [
            jsx('div', { style: { fontWeight: 700, color: C.intent, fontSize: 12, marginBottom: 2 }, children: 'agent' }),
            jsx('div', { style: S.prose, children: n.answer })
          ]
        })
      ]
    }, n.id))
  })
}

function Card({ title, right, children, color }) {
  return jsxs('div', {
    style: { ...S.card, borderTop: color ? `3px solid ${color}` : S.card.border },
    children: [
      jsxs('div', { style: { display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }, children: [
        jsx('div', { style: { ...S.h, marginBottom: 0, flex: 1 }, children: title }), right
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
            title: `Write-up · round ${latest.round}`, color: C.accent,
            right: jsx('span', { style: { ...S.muted, fontSize: 11 }, children: `${ago(latest.at)}${wf.reports.length > 1 ? ` · ${wf.reports.length - 1} earlier round(s)` : ''}` }),
            children: jsx('div', { style: S.prose, children: latest.text })
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
            title: 'Independent verifier (subagent)', color: C.test,
            right: jsxs('span', { style: S.row, children: [pill(verifierState, C.test), verdictPill(f.verifier_verdict, '—')] }),
            children: jsx('pre', { style: S.pre, children: f.verifier_report || f.verifier_note || 'No report yet.' })
          }),
          jsx(Card, {
            title: "The agent's own check", color: C.intent,
            right: verdictPill(f.own_verdict, 'not filed yet'),
            children: jsx('div', { style: S.prose, children: f.own_report || '—' })
          }),
          jsx(Card, {
            title: 'Reconciled', color: C.accent,
            right: verdictPill(f.verdict, 'not yet'),
            children: jsx('div', { style: S.prose, children: f.verification || 'The agent reconciles the two checks once both are in.' })
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
          latest && jsx(Card, { title: `Final write-up · round ${latest.round}`, color: C.accent, children: jsx('div', { style: S.prose, children: latest.text }) }),
          f.verification && jsx(Card, { title: 'Verification', color: C.test, right: verdictPill(f.verdict), children: jsx('div', { style: S.prose, children: f.verification }) }),
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
    style: { display: 'flex', alignItems: 'center', gap: 4, margin: '8px 0 2px' },
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

function BoardPane({ ctx }) {
  const [boards, setBoards] = useState([])
  const [name, setName] = useState(() => ctx.storage.get('board', ''))
  const [snap, setSnap] = useState(null)
  const [wf, setWf] = useState(null)
  const [tab, setTab] = useState('board')
  const [selected, setSelected] = useState('board')
  const [folded, setFolded] = useState({})
  const [tick, setTick] = useState(0)
  const [failure, setFailure] = useState('')
  const refresh = useCallback(() => setTick(t => t + 1), [])

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
  const pick = value => { setName(value); setSelected('board'); setFolded({}); setTab('board') }

  return jsxs('div', {
    style: S.pane,
    children: [
      b && jsx(Head, { board: b, wf, boards, name, onPick: pick }),
      jsx(Tabs, { shown: tab, onPick: setTab, wf }),
      tab === 'board' && jsx('div', {
        role: 'tree',
        style: { ...S.scroll, padding: '6px 0' },
        children: tree && rows.length
          ? rows.map(row => jsx(Row, {
            tree, row, folded,
            selected: selected === row.id,
            onSelect: setSelected,
            onToggle: (id, fold) => setFolded(f => ({ ...f, [id]: fold }))
          }, row.id))
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
