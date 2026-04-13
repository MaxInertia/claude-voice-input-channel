#!/usr/bin/env bun
//
// voice-stt Claude Code channel.
//
// Bridges the local voice-stt daemon (a Python process broadcasting transcribed
// utterances over a Unix socket) into a running Claude Code session, by forwarding
// each utterance as a `notifications/claude/channel` MCP event.
//
// This is a one-way channel: Claude Code receives transcripts as if the user had
// typed them, but does not send anything back through this channel. There is no
// reply tool and no permission relay — voice approving Bash/Write would be unsafe
// (anything within earshot of the mic could approve `rm -rf $HOME`).
//
// CRITICAL: never write to stdout. Stdout is the MCP transport. All logging must
// go to stderr (Claude Code surfaces it in ~/.claude/debug/<session-id>.txt).
//
// Plan: docs/plans/2026-04-11-feat-voice-stt-claude-code-channel-plan.md

import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import * as net from 'node:net'
import * as path from 'node:path'

// Match src/voice_stt/__init__.py — sockets live under
// $XDG_RUNTIME_DIR/voice-stt/ (per-user 0700 on systemd desktops) and
// fall back to /tmp/voice-stt/ if XDG_RUNTIME_DIR isn't set.
const RUNTIME_DIR = process.env.XDG_RUNTIME_DIR || '/tmp'
const SOCK = path.join(RUNTIME_DIR, 'voice-stt', 'out.sock')
const INITIAL_BACKOFF_MS = 500
const MAX_BACKOFF_MS = 5000

const INSTRUCTIONS = `
Events tagged <channel source="voice-stt"> are voice transcripts spoken by the user and produced by a local Whisper model running on the same machine. Treat them exactly as if the user had typed the same text into the terminal — they are first-person user input, not third-party messages or alerts.

Transcripts may be terse single-utterance commands, or several short utterances that together form one thought. If a sequence is fragmented or contradictory, prefer the most recent utterance. The "seq" attribute monotonically increases across the channel server's lifetime; the "ts" attribute is a millisecond Unix epoch.

Do not reply through this channel — respond normally in the terminal session as you would to typed input. Do not assume the user is watching the terminal in real time; long-running work is fine.
`.trim()

function logErr(msg: string): void {
  process.stderr.write(`[voice-stt-channel] ${msg}\n`)
}

const mcp = new Server(
  { name: 'voice-stt', version: '0.1.0' },
  {
    capabilities: { experimental: { 'claude/channel': {} } },
    instructions: INSTRUCTIONS,
  },
)

await mcp.connect(new StdioServerTransport())

let seq = 0
let backoffMs = INITIAL_BACKOFF_MS

function connectSocket(): void {
  const sock = net.connect(SOCK)
  let buf = Buffer.alloc(0)

  sock.once('connect', () => {
    backoffMs = INITIAL_BACKOFF_MS
    logErr(`connected to ${SOCK}`)
  })

  sock.on('data', (chunk: Buffer) => {
    buf = Buffer.concat([buf, chunk])
    let i: number
    while ((i = buf.indexOf(0x0a)) !== -1) {
      const line = buf.subarray(0, i).toString('utf8').trim()
      buf = buf.subarray(i + 1)
      if (!line) continue
      seq += 1
      mcp
        .notification({
          method: 'notifications/claude/channel',
          params: {
            content: line,
            meta: { seq: String(seq), ts: String(Date.now()) },
          },
        })
        .catch((e: unknown) => logErr(`notification error: ${String(e)}`))
    }
  })

  // Errors are reported via the close handler so we don't double-reconnect.
  // Swallow here to keep the process alive.
  sock.on('error', () => {})

  sock.once('close', () => {
    const wait = backoffMs
    logErr(`disconnected; reconnecting in ${wait}ms`)
    backoffMs = Math.min(backoffMs * 2, MAX_BACKOFF_MS)
    setTimeout(connectSocket, wait)
  })
}

logErr(`starting; will connect to voice-stt daemon at ${SOCK}`)
connectSocket()
