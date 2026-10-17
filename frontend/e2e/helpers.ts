import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
const __dirname = path.dirname(fileURLToPath(import.meta.url))
import type { Page } from '@playwright/test'

export const ROOT = path.resolve(__dirname, '..', '..')
export const SHOTS = path.join(ROOT, 'e2e', 'screenshots')
const PY = process.platform === 'win32' ? path.join(ROOT, '.venv', 'Scripts', 'python.exe') : path.join(ROOT, '.venv', 'bin', 'python')
const API = 'http://127.0.0.1:8000'

export function tenantName(prefix: string) {
  return `e2e-${prefix}-${Date.now().toString(36).slice(-5)}`
}

export async function resetTenant(tenant: string) {
  // auto-seed on first touch, then wipe agent memory so every spec starts clean
  await fetch(`${API}/api/health?tenant=${tenant}`)
  await fetch(`${API}/api/demo/reset?memory_only=true&tenant=${tenant}`, { method: 'POST' })
}

/** Replayed guest investigation over MCP (no LLM). Returns the server's verification result. */
export function runReplayGuest(tenant: string, incident = 'inc-1042', naive = false) {
  const args = ['-m', 'evals.e2e_guest', '--tenant', tenant, '--incident', incident, ...(naive ? ['--naive'] : [])]
  const out = execFileSync(PY, args, { cwd: ROOT, env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONWARNINGS: 'ignore' }, encoding: 'utf-8' })
  const line = out.trim().split('\n').pop() ?? '{}'
  return JSON.parse(line) as { recorded_id: string; agent: string; verified_outcome: string; hypotheses: { suspect: string; status: string; failed_checks: string[] }[] }
}

export async function openInvestigation(page: Page, tenant: string, incident: string, extra = '') {
  await page.goto(`/?tenant=${tenant}&incident=${incident}&instant=1${extra}`)
  // wait until the replay has finished (step counter reaches n/n) or the unknown-entity panel shows
  await page.waitForFunction(() => {
    const t = document.body.innerText
    return /\b(\d+)\/\1\b/.test(t) || t.includes('Unknown entity')
  }, null, { timeout: 60_000 })
}

export async function shot(page: Page, name: string) {
  fs.mkdirSync(SHOTS, { recursive: true })
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: false })
}
