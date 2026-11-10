// Test stub of the Desktop SDK: only what pwn-board uses.
import { createElement } from 'react'

export const PANES_AREA = 'panes'
export const cn = (...parts) => parts.filter(Boolean).join(' ')

// The chat's Markdown renderer and Streamdown, recorded so the test sees what the pane asks of them.
// Live bindings: the test swaps them out to try the older-shell paths.
export const rendered = []
const chat = props => { rendered.push(['chat', props]); return createElement('div', { 'data-rendered': 'chat' }, props.text) }
const stream = props => { rendered.push(['streamdown', props]); return createElement('div', { 'data-rendered': 'streamdown' }, props.children) }
export let MessageTextContent = chat
export let Streamdown = stream
export function __setRenderers({ chat: c = chat, streamdown: s = stream } = {}) {
  MessageTextContent = c
  Streamdown = s
}

// The app's clipboard path, recorded.
export const copied = []
export async function writeClipboardText(text) { copied.push(text) }
