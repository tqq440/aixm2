// Real backend acceptance only: start API, dispatcher and Vite externally.
// PART6_MODE=queued requires Worker stopped; running requires Worker ready.
// This script creates one task and requests cancellation through the real page.
import { chromium, expect } from '@playwright/test';
import { mkdir, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const platformDir = fileURLToPath(new URL('../../', import.meta.url));
const mode = process.env.PART6_MODE ?? 'running';
if (!['queued', 'running'].includes(mode)) throw new Error('PART6_MODE must be queued or running.');
const audioPath = path.resolve(process.env.PART6_SAMPLE ?? path.join(platformDir, 'runtime/samples/asr_example_zh.wav'));
const evidenceDir = path.resolve(process.env.PART6_EVIDENCE_DIR ?? path.join(platformDir, 'runtime/verification/part6'));
const baseURL = process.env.PART6_BASE_URL ?? 'http://127.0.0.1:5173';
const report = { evidenceKind: 'real-browser-real-backend-cancellation', mode, startedAt: new Date().toISOString(), sourceAudio: audioPath, noNetworkMocking: true, pageErrors: [], layouts: [], passed: false };
await mkdir(evidenceDir, { recursive: true });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const page = await browser.newPage({ baseURL, viewport: { width: 1366, height: 768 } });
  page.setDefaultTimeout(120_000);
  page.on('pageerror', error => report.pageErrors.push(error.message));
  await page.goto('/');
  await page.getByLabel('选择音频', { exact: true }).setInputFiles(audioPath);
  await page.getByLabel('模型', { exact: true }).selectOption('whisper-small');
  await page.getByLabel('识别语言', { exact: true }).selectOption('zh');
  const accepted = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/tasks/asr');
  await page.getByRole('button', { name: '提交转写', exact: true }).click();
  const acceptedResponse = await accepted;
  expect(acceptedResponse.status()).toBe(201);
  report.receipt = (await acceptedResponse.json()).data;
  const taskId = report.receipt.task.taskId;
  report.taskId = taskId;
  expect(taskId).toMatch(/^[0-9a-f]{32}$/);
  const readTask = async () => {
    const response = await page.request.get(`/api/tasks/${taskId}`);
    expect(response.status()).toBe(200);
    return (await response.json()).data;
  };
  await expect.poll(async () => (await readTask()).status, { timeout: 120_000, intervals: [100, 250] }).toBe(mode);
  report.beforeCancel = await readTask();
  const detail = page.getByRole('region', { name: '任务详情', exact: true });
  await expect(detail.getByText(mode === 'queued' ? '排队中' : '转写中', { exact: true })).toBeVisible({ timeout: 120_000 });
  const cancelled = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === `/api/tasks/${taskId}/cancel`);
  await detail.getByRole('button', { name: '取消任务', exact: true }).click();
  const cancelledResponse = await cancelled;
  expect(cancelledResponse.status()).toBe(200);
  report.cancelReceipt = (await cancelledResponse.json()).data;
  report.cancelDecision = report.cancelReceipt.decision;
  expect(report.cancelDecision).toBe(mode === 'queued' ? 'cancelled' : 'cancel_requested');
  await expect.poll(async () => (await readTask()).status, { timeout: 120_000, intervals: [100, 250, 1000] }).toBe('cancelled');
  report.task = await readTask();
  expect(report.task.attempt).toBe(mode === 'queued' ? 0 : 1);
  expect(report.task.resultAvailable).toBe(false);
  await expect(detail.getByText('已取消', { exact: true })).toBeVisible({ timeout: 120_000 });
  await expect(detail.getByRole('button', { name: /取消/ })).toHaveCount(0);
  await page.reload();
  await expect(page).toHaveURL(new RegExp(`task=${taskId}`));
  await expect(detail.getByText('已取消', { exact: true })).toBeVisible({ timeout: 120_000 });
  report.selectionAndCancellationSurvivedRefresh = true;
  await detail.locator('summary').click();
  for (const viewport of [{ width: 1366, height: 768 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    await expect(detail.getByText('排队截止时间', { exact: true })).toBeVisible();
    await page.evaluate(() => {
      if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
      window.scrollTo({ top: 0, left: 0, behavior: 'instant' });
    });
    const dimensions = await page.evaluate(() => ({ documentWidth: document.documentElement.scrollWidth, bodyWidth: document.body.scrollWidth }));
    expect(dimensions.documentWidth).toBeLessThanOrEqual(viewport.width);
    expect(dimensions.bodyWidth).toBeLessThanOrEqual(viewport.width);
    const screenshotName = `cancel-${mode}-${viewport.width}x${viewport.height}.png`;
    await page.screenshot({ path: path.join(evidenceDir, screenshotName), fullPage: true });
    report.layouts.push({ ...viewport, ...dimensions, screenshotName });
  }
  expect(report.pageErrors).toEqual([]);
  report.passed = true;
} catch (error) {
  report.error = error instanceof Error ? error.stack : String(error);
  process.exitCode = 1;
} finally {
  await browser.close();
  report.completedAt = new Date().toISOString();
  await writeFile(path.join(evidenceDir, `cancel-${mode}-report.json`), JSON.stringify(report, null, 2), 'utf8');
  console.log(JSON.stringify(report));
}
