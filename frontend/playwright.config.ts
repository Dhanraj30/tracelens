import { defineConfig } from '@playwright/test'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
const __dirname = path.dirname(fileURLToPath(import.meta.url))

const ROOT = path.resolve(__dirname, '..')
const PY = process.platform === 'win32' ? path.join(ROOT, '.venv', 'Scripts', 'python.exe') : path.join(ROOT, '.venv', 'bin', 'python')

// One command: `npm run e2e` (from frontend/). Starts the API and Vite if they are not already running.
// Every spec uses its own tenant graph (tracelens:e2e-*), auto-seeded, so the demo graph is untouched. No live LLM calls.
export default defineConfig({
  testDir: './e2e',
  timeout: 120_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  outputDir: path.join(ROOT, 'e2e', 'test-output'),
  use: {
    baseURL: 'http://127.0.0.1:5173',
    viewport: { width: 1920, height: 1080 },
    screenshot: 'off',
    trace: 'retain-on-failure',
  },
  webServer: [
    {
      command: `"${PY}" -m uvicorn backend.api.main:app --host 127.0.0.1 --port 8000`,
      url: 'http://127.0.0.1:8000/api/health',
      cwd: ROOT,
      reuseExistingServer: true,
      timeout: 60_000,
    },
    {
      command: 'npm run dev -- --host 127.0.0.1 --port 5173',
      url: 'http://127.0.0.1:5173/',
      cwd: __dirname,
      reuseExistingServer: true,
      timeout: 60_000,
    },
  ],
})
