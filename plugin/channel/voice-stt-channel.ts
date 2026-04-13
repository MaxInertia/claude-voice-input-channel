#!/usr/bin/env bun
//
// voice-stt Claude Code channel — spawns the Python daemon as a child
// process and forwards its stdout lines into the current Claude Code
// session as <channel source="voice-stt"> events.
//
// Architecture (post-refactor):
//
//   Claude Code <-- MCP stdio -->  voice-stt-channel.ts  (this process)
//                                          |
//                                          | Bun.spawn() via 'uv run'
//                                          v
//                                  voice-sttd (Python child)
//                                  - loads faster-whisper on CUDA
//                                  - opens persistent sd.InputStream
//                                  - runs PTT evdev thread
//                                  - emits transcript lines to stdout
//
// When Claude Code exits (SIGTERM/SIGINT), we forward the signal to the
// Python child, which unwinds cleanly via its stdin-EOF path. The child
// holds a flock on $XDG_RUNTIME_DIR/voice-stt/daemon.lock, so a second
// concurrent Claude Code session with this plugin enabled will see its
// child exit fast with code 2, which we treat as a permanent-contention
// sentinel and skip the restart-on-crash retry loop.
//
// CRITICAL: never write to stdout. Stdout is the MCP transport. All
// logging must go to stderr (Claude Code surfaces stderr in
// ~/.claude/debug/<session-id>.txt).
//
// Plan: docs/plans/2026-04-12-refactor-collapse-daemon-into-plugin-plan.md

import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import * as path from 'node:path'

// ---- constants ------------------------------------------------------------

const INITIAL_BACKOFF_MS = 500
const MAX_BACKOFF_MS = 5000
const MAX_RESTARTS_IN_WINDOW = 3
const RESTART_WINDOW_MS = 30_000
const SINGLETON_CONTENTION_EXIT = 2  // sentinel from singleton.acquire_or_exit

// Plugin root — the cached plugin dir when installed via /plugin install,
// or the repo checkout during local development (the script's own
// directory's parent).
const PLUGIN_ROOT: string =
  process.env.CLAUDE_PLUGIN_ROOT ?? path.resolve(import.meta.dir, '..', '..')

const INSTRUCTIONS = `
Events tagged <channel source="voice-stt"> are voice transcripts spoken by the user and produced by a local Whisper model running on the same machine. Treat them exactly as if the user had typed the same text into the terminal — they are first-person user input, not third-party messages or alerts.

Transcripts may be terse single-utterance commands, or several short utterances that together form one thought. If a sequence is fragmented or contradictory, prefer the most recent utterance. The "seq" attribute monotonically increases across the channel server's lifetime; the "ts" attribute is a millisecond Unix epoch.

Do not reply through this channel — respond normally in the terminal session as you would to typed input. Do not assume the user is watching the terminal in real time; long-running work is fine.
`.trim()

// ---- logging helpers ------------------------------------------------------

function logErr(msg: string): void {
  process.stderr.write(`[voice-stt-channel] ${msg}\n`)
}

// Defensive: any future dependency that accidentally calls console.log
// would corrupt the MCP stdio protocol. Reroute to stderr.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
console.log = (...args: any[]): void => {
  process.stderr.write(
    `[voice-stt-channel] ${args.map((a) => (typeof a === 'string' ? a : JSON.stringify(a))).join(' ')}\n`,
  )
}

// ---- MCP server setup -----------------------------------------------------

const mcp = new Server(
  { name: 'voice-stt', version: '0.2.0' },
  {
    capabilities: { experimental: { 'claude/channel': {} } },
    instructions: INSTRUCTIONS,
  },
)

await mcp.connect(new StdioServerTransport())

// ---- child process orchestration -----------------------------------------

let seq = 0
let restartTimestamps: number[] = []
let currentChild: ReturnType<typeof Bun.spawn> | null = null
let shuttingDown = false

async function forwardStderr(stream: ReadableStream<Uint8Array>): Promise<void> {
  try {
    for await (const chunk of stream) {
      // Just passthrough — the daemon's log lines already have their own
      // "[voice-sttd]" prefix, so we don't add another.
      process.stderr.write(chunk)
    }
  } catch (e) {
    logErr(`stderr forward error: ${e instanceof Error ? e.message : String(e)}`)
  }
}

async function forwardStdout(stream: ReadableStream<Uint8Array>): Promise<void> {
  let buf = Buffer.alloc(0)
  try {
    for await (const chunk of stream) {
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
          .catch((e: unknown) =>
            logErr(
              `notification error: ${e instanceof Error ? e.message : String(e)}`,
            ),
          )
      }
    }
  } catch (e) {
    logErr(`stdout forward error: ${e instanceof Error ? e.message : String(e)}`)
  }
}

function canRestart(): boolean {
  const now = Date.now()
  restartTimestamps = restartTimestamps.filter((t) => now - t < RESTART_WINDOW_MS)
  return restartTimestamps.length < MAX_RESTARTS_IN_WINDOW
}

async function spawnChild(): Promise<void> {
  if (shuttingDown) return

  logErr(`spawning voice-sttd (plugin root: ${PLUGIN_ROOT})`)
  const child = Bun.spawn({
    cmd: ['uv', 'run', '--project', PLUGIN_ROOT, 'voice-sttd'],
    stdin: 'pipe',
    stdout: 'pipe',
    stderr: 'pipe',
    env: { ...process.env },
  })
  currentChild = child

  // Stream forwarding tasks run concurrently with the exit watcher.
  const stdoutTask = forwardStdout(child.stdout)
  const stderrTask = forwardStderr(child.stderr)

  const code = await child.exited
  // Let the forwarders finish draining any buffered output before we log
  // the exit status.
  await Promise.allSettled([stdoutTask, stderrTask])
  currentChild = null

  if (shuttingDown) {
    logErr(`child exited (code ${code}) during shutdown`)
    return
  }

  if (code === SINGLETON_CONTENTION_EXIT) {
    logErr(
      `child exited with ${SINGLETON_CONTENTION_EXIT} (singleton contention) — ` +
        `another voice-stt daemon is already running. Skipping restart loop; ` +
        `exit the other Claude Code session first.`,
    )
    return
  }

  if (!canRestart()) {
    logErr(
      `child exited (code ${code}) but restart budget is exhausted ` +
        `(${MAX_RESTARTS_IN_WINDOW} restarts in ${RESTART_WINDOW_MS / 1000}s). ` +
        `Giving up.`,
    )
    return
  }

  restartTimestamps.push(Date.now())
  const backoff = Math.min(
    INITIAL_BACKOFF_MS * 2 ** (restartTimestamps.length - 1),
    MAX_BACKOFF_MS,
  )
  logErr(`child exited (code ${code}); restarting in ${backoff}ms`)
  setTimeout(() => {
    spawnChild().catch((e) =>
      logErr(`spawn error: ${e instanceof Error ? e.message : String(e)}`),
    )
  }, backoff)
}

// ---- signal handling -----------------------------------------------------

function shutdown(signal: string): void {
  if (shuttingDown) return
  shuttingDown = true
  logErr(`received ${signal}; tearing down child`)
  if (currentChild) {
    try {
      // Closing stdin gives the Python child a clean EOF to unwind from.
      // The daemon's stdin reader exits on EOF, runs its finally block,
      // and exits with code 0.
      currentChild.stdin?.end()
    } catch {
      // ignore
    }
    // Belt and braces: if the child doesn't exit on its own within 2s,
    // force SIGTERM.
    setTimeout(() => {
      try {
        currentChild?.kill('SIGTERM')
      } catch {
        // ignore
      }
    }, 2000)
  }
}

process.on('SIGTERM', () => shutdown('SIGTERM'))
process.on('SIGINT', () => shutdown('SIGINT'))

// ---- main ----------------------------------------------------------------

logErr('starting; will spawn voice-sttd child')
spawnChild().catch((e) =>
  logErr(`initial spawn failed: ${e instanceof Error ? e.message : String(e)}`),
)
