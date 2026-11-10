/**
 * search-board — the Desktop pane.
 *
 * The board drawn as the tree it was searched as: goal → leads → what each
 * found → the leads that opened. Click a row for its full text in the strip
 * below (a finding with the tool output it cites); fold a row with its caret.
 * The operator's actions sit under the strip: leave a note (about the selected
 * lead or finding), take one off, confirm or send back a proposal, extend a
 * suspended board, abandon one.
 *
 * A finished board goes through the workflow, in the Review and Verification
 * tabs: read the write-up and the files as delivered, leave review notes (the
 * agent answers each before writing up again), send it back for another round
 * or on to verification; read the independent verifier's report, the agent's
 * own check and the reconciled report; accept it as complete, or send it back.
 * Every move that can't be undone in one click wants a second click.
 *
 * Data comes from this package's own backend (dashboard/plugin_api.py) through
 * ctx.rest. It refreshes on the plugin's `board.changed` event and polls every
 * 5 s as a fallback.
 *
 * Plain ESM with jsx() calls: a disk plugin is loaded uncompiled.
 */

import { PANES_AREA, cn } from '@hermes/plugin-sdk'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const EVENT = 'plugin.search-board.board.changed'
const CLAIM_TTL_MS = 30 * 60 * 1000

const STATUS_MARK = {
  open: '[*]', suspended: '[!]', proposed_solved: '[!]', proposed_exhausted: '[!]',
  solved: '[+]', exhausted: '[-]', abandoned: '[-]'
}

// ------------------------------------------------------------------ the tree

function leadMark(lead) {
  if (lead.state === 'claimed') return '[*]'
  if (lead.state === 'explored') return '[+]'
  if (lead.state === 'ruled_out') return '[-]'
  return '[?]'
}

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
  // Ruled-out leads start folded, so they don't pile up; everything else starts open.
  return tree.leads[id]?.state === 'ruled_out'
}

function rowText(tree, id) {
  const lead = tree.leads[id]
  if (lead) {
    let text = `${leadMark(lead)} ${lead.id} (p${lead.priority}) ${lead.direction}`
    if (lead.state === 'claimed') text += ` — ${lead.claimed_by ?? 'someone'} exploring`
    if (lead.state === 'ruled_out') text += ` — ruled out by ${lead.closed_by}`
    return text
  }
  const f = tree.findings[id]
  if (f) {
    const who = f.author === 'plugin' ? ' (goal test)' : f.author === 'operator' ? ' (operator)' : ''
    return `${f.polarity === 'supports' ? '[+]' : '[-]'} ${f.id}${who} ${f.statement}`
  }
  const n = tree.notes[id]
  return `${n.read ? '[ ]' : '[!]'} ${n.id} note: ${n.text}`
}

function ago(stamp) {
  if (!stamp) return ''
  const seconds = Math.max(0, Math.round((Date.now() - Date.parse(stamp)) / 1000))
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`
  return `${Math.floor(seconds / 86400)} d ago`
}

// ------------------------------------------------------------------ pieces

const MUTED = 'text-(--ui-text-tertiary)'
const BUTTON = cn(
  'rounded border border-(--ui-stroke-secondary) px-2 py-0.5 text-[0.75rem]',
  'text-(--ui-text-secondary) hover:text-(--ui-accent) disabled:opacity-50'
)

function Btn({ onClick, children, disabled, title, type = 'button' }) {
  return jsx('button', { type, className: BUTTON, onClick, disabled, title, children })
}

function Row({ tree, row, folded, selected, onSelect, onToggle }) {
  const kids = tree.children[row.id] ?? []
  const open = !isFolded(tree, folded, row.id)
  const also = tree.also[row.id]
  return jsxs('div', {
    role: 'treeitem',
    'aria-selected': selected,
    'aria-expanded': kids.length ? open : undefined,
    onClick: () => onSelect(row.id),
    className: cn(
      'flex cursor-default items-start gap-1 py-0.5 pr-2 font-mono text-[0.75rem] leading-snug',
      selected ? 'text-(--ui-accent)' : 'text-(--ui-text-secondary)'
    ),
    style: { paddingLeft: `${8 + row.depth * 14}px` },
    children: [
      jsx('span', {
        className: cn('w-3 shrink-0 select-none', MUTED),
        onClick: event => {
          event.stopPropagation()
          if (kids.length) onToggle(row.id, open) // open now → fold it
        },
        children: kids.length ? (open ? '▾' : '▸') : ''
      }),
      jsx('span', {
        className: 'min-w-0 truncate',
        title: rowText(tree, row.id),
        children: rowText(tree, row.id) + (also ? ` (also ${also.join(', ')})` : '')
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
  let lines
  if (!id || id === 'board') {
    lines = [`${b.name} — ${b.title}`, `origin: ${b.origin}`, `goal: ${b.goal}`, `test (${b.goal_test_kind}): ${b.goal_test}`]
    if (b.outcome) lines.push(`outcome: ${b.outcome}`)
  } else if (tree.leads[id]) {
    const l = tree.leads[id]
    lines = [`${leadMark(l)} ${l.id} — ${l.state.replace('_', ' ')}, priority ${l.priority}`, `direction: ${l.direction}`, `rationale: ${l.rationale}`]
    if (l.from_findings.length) lines.push(`came from: ${l.from_findings.join(', ')}`)
    if (l.state === 'claimed') lines.push(`claimed by ${l.claimed_by ?? 'someone'}, ${ago(l.claimed_at)}`)
    if (l.closed_by) lines.push(`${l.state === 'explored' ? 'explored in' : 'ruled out by'} ${l.closed_by}`)
  } else if (finding) {
    lines = [`${finding.id} — ${finding.polarity}, ${finding.basis}, by ${finding.author}, ${ago(finding.created_at)}`, finding.statement]
    if (finding.from_lead) lines.push(`explored: ${finding.from_lead}`)
    if (finding.from_findings.length) lines.push(`inferred from: ${finding.from_findings.join(', ')}`)
  } else if (tree.notes[id]) {
    const n = tree.notes[id]
    lines = [`${n.id} — your note, ${ago(n.created_at)}${n.about ? `, about ${n.about}` : ''}`, n.text, n.read ? 'read by the agent' : 'not read yet']
  } else {
    lines = []
  }

  return jsxs('div', {
    className: 'max-h-[40%] shrink-0 overflow-auto border-t border-(--ui-stroke-secondary) px-2 py-1.5 text-[0.75rem]',
    children: [
      ...lines.map((line, i) => jsx('div', { className: i ? 'text-(--ui-text-secondary)' : 'font-medium', children: line }, `l${i}`)),
      ...evidence.map(ev => jsxs('div', {
        className: 'mt-1.5',
        children: [
          jsx('div', { className: MUTED, children: `e${ev.id} · ${ev.tool_name} · ${ev.chars} chars · ${ago(ev.created_at)}` }),
          jsx('pre', { className: 'whitespace-pre-wrap break-words font-mono text-[0.6875rem] text-(--ui-text-tertiary)', children: ev.excerpt.slice(0, 2000) })
        ]
      }, `e${ev.id}`))
    ]
  })
}

function Actions({ ctx, snap, selected, tree, onDone }) {
  const [text, setText] = useState('')
  const [armed, setArmed] = useState(null)
  const [error, setError] = useState('')
  const b = snap.board
  const about = tree.leads[selected] || tree.findings[selected] ? selected : null
  const name = encodeURIComponent(b.name)

  const act = useCallback(async (path, body) => {
    setError('')
    try {
      await ctx.rest(path, { method: 'POST', body })
      setText('')
      setArmed(null)
      onDone()
    } catch (e) {
      setError(String(e?.message ?? e))
    }
  }, [ctx, onDone])

  // Destructive or final actions want a second click.
  const twice = (key, run) => () => (armed === key ? run() : setArmed(key))
  const closed = ['solved', 'exhausted', 'abandoned'].includes(b.status)
  const buttons = []

  if (b.status === 'proposed_solved') {
    buttons.push(jsx(Btn, { onClick: twice('confirm', () => act(`/boards/${name}/decide`, { decision: 'confirm' })), children: armed === 'confirm' ? 'Confirm solved?' : 'Confirm solved' }, 'c'))
  }
  if (b.status === 'proposed_exhausted') {
    buttons.push(jsx(Btn, { onClick: twice('exhaust', () => act(`/boards/${name}/decide`, { decision: 'exhaust' })), children: armed === 'exhaust' ? 'Close exhausted?' : 'Close exhausted' }, 'x'))
  }
  if (b.status.startsWith('proposed_')) {
    buttons.push(jsx(Btn, { title: 'Your note text becomes the reason', onClick: () => act(`/boards/${name}/decide`, { decision: 'reject', why: text }), children: 'Send back' }, 'r'))
  }
  if (b.status === 'suspended') {
    buttons.push(jsx(Btn, { onClick: () => act(`/boards/${name}/extend`, { steps: 20 }), children: 'Extend +20 steps' }, 'e'))
  }
  if (tree.notes[selected]) {
    buttons.push(jsx(Btn, { onClick: () => act(`/boards/${name}/unnote`, { id: selected }), children: `Take ${selected} off` }, 'u'))
  }
  if (!closed) {
    buttons.push(jsx(Btn, { onClick: twice('abandon', () => act(`/boards/${name}/decide`, { decision: 'abandon', why: text })), children: armed === 'abandon' ? 'Abandon?' : 'Abandon' }, 'a'))
  }

  return jsxs('div', {
    className: 'flex shrink-0 flex-col gap-1.5 border-t border-(--ui-stroke-secondary) p-2',
    children: [
      !closed && jsxs('form', {
        className: 'flex gap-1.5',
        onSubmit: event => {
          event.preventDefault()
          if (text.trim()) act(`/boards/${name}/note`, { text: about ? `${about}: ${text}` : text })
        },
        children: [
          jsx('input', {
            className: 'min-w-0 flex-1 rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-0.5 text-[0.75rem]',
            placeholder: about ? `Note about ${about}…` : 'Note for the agent…',
            value: text,
            onChange: event => setText(event.target.value)
          }),
          jsx(Btn, { type: 'submit', disabled: !text.trim(), children: 'Leave note' })
        ]
      }),
      buttons.length > 0 && jsx('div', { className: 'flex flex-wrap gap-1.5', children: buttons }),
      error && jsx('div', { className: 'text-[0.75rem] text-(--ui-accent)', children: error })
    ]
  })
}

// ------------------------------------------------------------------ the workflow

const PRE = 'whitespace-pre-wrap break-words font-mono text-[0.6875rem] text-(--ui-text-secondary)'

function Section({ title, children }) {
  return jsxs('div', {
    className: 'border-b border-(--ui-stroke-secondary) px-2 py-1.5 text-[0.75rem]',
    children: [jsx('div', { className: cn('mb-1 font-medium', MUTED), children: title }), children]
  })
}

function useMove(ctx, onDone) {
  const [armed, setArmed] = useState(null)
  const [error, setError] = useState('')
  const run = useCallback(async (path, body) => {
    setError('')
    try {
      await ctx.rest(path, body === undefined ? { method: 'POST', body: {} } : { method: 'POST', body })
      setArmed(null)
      onDone()
    } catch (e) {
      setError(String(e?.message ?? e))
    }
  }, [ctx, onDone])
  // A move that can't be undone in one click wants a second.
  const twice = (key, label, path, body) => jsx(Btn, {
    onClick: () => (armed === key ? run(path, body) : setArmed(key)),
    children: armed === key ? `${label}?` : label
  }, key)
  return { run, twice, error }
}

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
  if (!files.length) return jsx('div', { className: MUTED, children: 'No files delivered.' })
  return jsxs('div', {
    children: [
      ...files.map(f => jsx('div', {
        role: 'button',
        className: cn('cursor-default truncate font-mono', open === f.path ? 'text-(--ui-accent)' : ''),
        onClick: () => setOpen(open === f.path ? null : f.path),
        children: `${f.path} · ${f.chars} chars${f.truncated ? ' (cut)' : ''}${f.error ? ` — not kept: ${f.error}` : ''}`
      }, f.path)),
      open && jsx('pre', { className: cn(PRE, 'mt-1 max-h-64 overflow-auto'), children: content })
    ]
  })
}

function ReviewTab({ ctx, board, wf, onDone }) {
  const [text, setText] = useState('')
  const { run, twice, error } = useMove(ctx, () => { setText(''); onDone() })
  const name = encodeURIComponent(board.name)
  const stage = wf.flow.stage
  const latest = wf.reports[wf.reports.length - 1]
  const exhausted = board.status === 'proposed_exhausted'
  const buttons = []
  if (stage === 'review') {
    buttons.push(twice('back', 'Send back', `/boards/${name}/back`, { why: text }))
    buttons.push(exhausted
      ? twice('exhaust', 'Close exhausted', `/boards/${name}/decide`, { decision: 'exhaust' })
      : twice('verify', 'To verification', `/boards/${name}/verify`))
  }
  if (stage !== 'discarded' && stage !== 'working') buttons.push(twice('discard', 'Discard', `/boards/${name}/discard`, { why: text }))

  return jsxs('div', {
    className: 'min-h-0 flex-1 overflow-auto',
    children: [
      wf.write_up_due && jsx(Section, { title: 'write-up', children: jsx('div', { children: 'The agent owes a write-up: it comes here when the agent files it.' }) }),
      latest && jsx(Section, {
        title: `write-up · round ${latest.round} · ${ago(latest.at)}${wf.reports.length > 1 ? ` · ${wf.reports.length - 1} earlier round(s)` : ''}`,
        children: jsx('div', { className: PRE, children: latest.text })
      }),
      latest && jsx(Section, { title: 'files as delivered', children: jsx(Files, { ctx, name: board.name, files: wf.files }) }),
      jsx(Section, {
        title: 'review notes',
        children: wf.notes.length
          ? wf.notes.map(n => jsxs('div', {
            className: 'mb-1',
            children: [
              jsx('div', { children: `${n.answer ? '[+]' : '[?]'} ${n.id} (round ${n.round}): ${n.text}` }),
              n.answer
                ? jsx('div', { className: cn('pl-4', MUTED), children: `agent: ${n.answer}` })
                : stage === 'review' && jsx('button', { type: 'button', className: cn('pl-4 text-[0.6875rem]', MUTED), onClick: () => run(`/boards/${name}/uncomment`, { id: n.id }), children: 'take back' })
            ]
          }, n.id))
          : jsx('div', { className: MUTED, children: 'None.' })
      }),
      (stage === 'review' || buttons.length > 0) && jsxs('div', {
        className: 'flex flex-col gap-1.5 p-2',
        children: [
          stage === 'review' && jsxs('form', {
            className: 'flex gap-1.5',
            onSubmit: event => { event.preventDefault(); if (text.trim()) run(`/boards/${name}/comment`, { text }) },
            children: [
              jsx('input', {
                className: 'min-w-0 flex-1 rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-0.5 text-[0.75rem]',
                placeholder: 'A question or idea for the agent…',
                value: text,
                onChange: event => setText(event.target.value)
              }),
              jsx(Btn, { type: 'submit', disabled: !text.trim(), children: 'Add note' })
            ]
          }),
          jsx('div', { className: 'flex flex-wrap gap-1.5', children: buttons }),
          error && jsx('div', { className: 'text-[0.75rem] text-(--ui-accent)', children: error })
        ]
      })
    ]
  })
}

function VerificationTab({ ctx, board, wf, onDone }) {
  const [why, setWhy] = useState('')
  const { run, twice, error } = useMove(ctx, () => { setWhy(''); onDone() })
  const name = encodeURIComponent(board.name)
  const f = wf.flow
  const mark = v => (v === 'passed' ? '[+]' : v === 'failed' ? '[-]' : '[*]')
  const buttons = []
  if (f.stage === 'verification') {
    if (f.verification) buttons.push(twice('accept', 'Accept as complete', `/boards/${name}/accept`))
    if (f.verifier_state === 'failed' && !f.verification) buttons.push(jsx(Btn, { onClick: () => run(`/boards/${name}/reverify`), children: 'Run the verifier again' }, 'rv'))
    buttons.push(twice('back', 'Send back to work', `/boards/${name}/back`, { why }))
    buttons.push(jsx(Btn, { onClick: () => run(`/boards/${name}/toreview`, { why }), children: 'Back to review' }, 'tr'))
  }
  if (f.stage === 'deliverable') {
    buttons.push(twice('back', 'Reopen', `/boards/${name}/back`, { why }))
    if (board.status !== 'exhausted') buttons.push(twice('verify', 'Verify again', `/boards/${name}/verify`))
    buttons.push(jsx(Btn, { onClick: () => run(`/boards/${name}/toreview`, { why }), children: 'Back to review' }, 'tr'))
  }
  if (f.stage !== 'discarded') buttons.push(twice('discard', 'Discard', `/boards/${name}/discard`, { why }))

  return jsxs('div', {
    className: 'min-h-0 flex-1 overflow-auto',
    children: [
      f.stage === 'deliverable' && jsx(Section, { title: 'complete', children: jsx('div', { children: `Accepted ${ago(f.accepted_at)}${f.verdict ? ` · verified (${f.verdict})` : ''}. ${board.outcome ?? ''}` }) }),
      jsx(Section, {
        title: `independent verifier · ${f.verifier_state ?? 'stopped'}${f.verifier_verdict ? ` · ${mark(f.verifier_verdict)} ${f.verifier_verdict}` : ''}`,
        children: jsx('div', { className: PRE, children: f.verifier_report || f.verifier_note || 'No report yet.' })
      }),
      jsx(Section, {
        title: `the agent's own check · ${f.own_verdict ? `${mark(f.own_verdict)} ${f.own_verdict}` : 'not filed yet'}`,
        children: jsx('div', { className: PRE, children: f.own_report || '—' })
      }),
      jsx(Section, {
        title: `reconciled · ${f.verdict ? `${mark(f.verdict)} ${f.verdict}` : 'not yet'}`,
        children: jsx('div', { className: PRE, children: f.verification || 'The agent reconciles the two checks once both are in.' })
      }),
      buttons.length > 0 && jsxs('div', {
        className: 'flex flex-col gap-1.5 p-2',
        children: [
          jsx('input', {
            className: 'min-w-0 rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-0.5 text-[0.75rem]',
            placeholder: 'Why (for send back / back to review / discard)…',
            value: why,
            onChange: event => setWhy(event.target.value)
          }),
          jsx('div', { className: 'flex flex-wrap gap-1.5', children: buttons }),
          error && jsx('div', { className: 'text-[0.75rem] text-(--ui-accent)', children: error })
        ]
      })
    ]
  })
}

function tabsFor(wf) {
  if (!wf) return ['board']
  const tabs = ['board']
  if (wf.reports.length || wf.write_up_due || wf.notes.length) tabs.push('review')
  if (['verification', 'deliverable'].includes(wf.flow.stage) || wf.flow.verification) tabs.push('verification')
  return tabs
}

// ------------------------------------------------------------------ the pane

function BoardPane({ ctx }) {
  const [boards, setBoards] = useState([])
  const [name, setName] = useState(() => ctx.storage.get('board', ''))
  const [snap, setSnap] = useState(null)
  const [selected, setSelected] = useState('board')
  const [folded, setFolded] = useState({})
  const [tick, setTick] = useState(0)
  const [failure, setFailure] = useState('')
  const [wf, setWf] = useState(null)
  const [tab, setTab] = useState('board')
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
          const first = list.find(b => !['solved', 'exhausted', 'abandoned'].includes(b.status)) ?? list[0]
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
  const tabs = tabsFor(wf)
  const shown = tabs.includes(tab) ? tab : 'board'
  const rows = useMemo(() => (tree ? visibleRows(tree, folded) : []), [tree, folded])

  if (failure) return jsx('div', { className: cn('p-3 text-[0.8125rem]', MUTED), children: failure })
  if (!boards.length) {
    return jsx('div', {
      className: cn('p-3 text-[0.8125rem]', MUTED),
      children: 'No search boards yet. The agent opens one with search_board_open when a problem has a testable goal and no known path.'
    })
  }

  const b = snap?.board
  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col',
    children: [
      jsxs('div', {
        className: 'flex shrink-0 flex-col gap-1 border-b border-(--ui-stroke-secondary) p-2',
        children: [
          jsx('select', {
            className: 'rounded border border-(--ui-stroke-secondary) bg-transparent px-1 py-0.5 text-[0.8125rem]',
            value: name,
            onChange: event => { setName(event.target.value); setSelected('board'); setFolded({}); setTab('board') },
            children: boards.map(x => jsx('option', {
              value: x.name,
              children: `${x.workflow === 'in review' || (x.workflow ?? '').startsWith('verified') ? '[!]' : STATUS_MARK[x.status] ?? '[?]'} ${x.name} — ${x.workflow ?? x.status}`
            }, x.name))
          }),
          b && jsxs('div', {
            className: 'cursor-default text-[0.75rem]',
            onClick: () => setSelected('board'),
            children: [
              jsx('div', { className: selected === 'board' ? 'font-medium text-(--ui-accent)' : 'font-medium', children: b.title }),
              jsx('div', { className: MUTED, children: `${b.status}${b.workflow ? ` · ${b.workflow}` : ''} · steps ${b.steps_used}/${b.step_budget} · frontier ${b.frontier} · ${b.findings} findings${b.unread_notes ? ` · ${b.unread_notes} note(s) not read yet` : ''}` })
            ]
          }),
          tabs.length > 1 && jsx('div', {
            role: 'tablist',
            className: 'flex gap-3 text-[0.75rem]',
            children: tabs.map(t => jsx('button', {
              type: 'button',
              role: 'tab',
              'aria-selected': shown === t,
              className: shown === t ? 'font-medium text-(--ui-accent)' : MUTED,
              onClick: () => setTab(t),
              children: t === 'board' ? 'Board' : t === 'review' ? 'Review' : 'Verification'
            }, t))
          })
        ]
      }),
      shown === 'review' && b && wf && jsx(ReviewTab, { ctx, board: b, wf, onDone: refresh }),
      shown === 'verification' && b && wf && jsx(VerificationTab, { ctx, board: b, wf, onDone: refresh }),
      shown === 'board' && jsx('div', {
        role: 'tree',
        className: 'min-h-0 flex-1 overflow-auto py-1',
        children: tree && rows.length
          ? rows.map(row => jsx(Row, {
            tree, row, folded,
            selected: selected === row.id,
            onSelect: setSelected,
            onToggle: (id, fold) => setFolded(f => ({ ...f, [id]: fold }))
          }, row.id))
          : jsx('div', { className: cn('px-3 py-2 text-[0.75rem]', MUTED), children: 'Nothing yet: no leads declared, nothing found.' })
      }),
      shown === 'board' && snap && jsx(Detail, { ctx, snap, tree, id: selected }),
      shown === 'board' && snap && jsx(Actions, { ctx, snap, selected, tree, onDone: refresh })
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
      data: { placement: 'right', width: '380px' },
      render: () => jsx(BoardPane, { ctx })
    })
  }
}
