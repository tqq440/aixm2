import { defineConfig, devices } from '@playwright/test';
import path from 'node:path';

const evidenceDir = process.env.E2E_EVIDENCE_DIR ?? '../runtime/verification/part5';

// Services are started separately. The real-model test is opt-in because it
// creates a persisted task in the currently configured local backend.
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 150_000,
  expect: { timeout: 15_000 },
  outputDir: path.join(evidenceDir, 'test-results'),
  reporter: [
    ['list'],
    ['json', { outputFile: path.join(evidenceDir, 'playwright-results.json') }],
  ],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? process.env.PART5_BASE_URL ?? 'http://127.0.0.1:5173',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    acceptDownloads: true,
  },
  projects: [{
    name: 'installed-edge',
    use: { ...devices['Desktop Edge'], channel: 'msedge', viewport: { width: 1366, height: 768 } },
  }],
});
