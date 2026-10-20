// Recheck the layout of the task from the completed real-model report.
// This helper issues only GET requests; it never creates another task.
import { chromium, expect } from '@playwright/test';
import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';

const evidenceDir = path.resolve(process.cwd(), '../runtime/verification/part5');
const existing = JSON.parse(await readFile(path.join(evidenceDir, 'real-browser-report.json'), 'utf8'));
if (!existing.passed || !/^[0-9a-f]{32}$/.test(existing.taskId)) {
  throw new Error('A completed real-browser report is required before a read-only layout recheck.');
}
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const report = { evidenceKind: 'read-only-real-result-layout-recheck', taskId: existing.taskId, layouts: [], mutationRequests: [] };
try {
  const page = await browser.newPage();
  page.on('request', (request) => {
    if (new URL(request.url()).pathname.startsWith('/api/') && request.method() !== 'GET') {
      report.mutationRequests.push({ method: request.method(), url: request.url() });
    }
  });
  await page.goto(`${process.env.PART5_BASE_URL ?? 'http://127.0.0.1:5173'}/?task=${existing.taskId}`);
  const detail = page.getByRole('region', { name: '任务详情', exact: true });
  await expect(detail).toContainText(existing.result.output.text);
  for (const viewport of [{ width: 1920, height: 1080 }, { width: 1366, height: 768 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    await page.getByRole('tab', { name: '时间分段', exact: true }).click();
    await expect(detail).toContainText(existing.result.output.segments[0].text.trim());
    await page.getByRole('tab', { name: '转写全文', exact: true }).click();
    await expect(detail).toContainText(existing.result.output.text);
    await page.evaluate(() => {
      if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
      window.scrollTo({ top: 0, left: 0, behavior: 'instant' });
    });
    await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
    const dimensions = await page.evaluate(() => ({
      viewportWidth: innerWidth,
      documentWidth: document.documentElement.scrollWidth,
      bodyWidth: document.body.scrollWidth,
      scrollY,
    }));
    expect(dimensions.documentWidth).toBeLessThanOrEqual(viewport.width);
    expect(dimensions.bodyWidth).toBeLessThanOrEqual(viewport.width);
    const screenshotName = `real-${viewport.width}x${viewport.height}.png`;
    await page.screenshot({ path: path.join(evidenceDir, screenshotName), fullPage: true });
    report.layouts.push({ ...viewport, ...dimensions, screenshotName });
  }
  expect(report.mutationRequests).toEqual([]);
  report.completedAt = new Date().toISOString();
  report.passed = true;
  await writeFile(path.join(evidenceDir, 'layout-recheck-report.json'), JSON.stringify(report, null, 2), 'utf8');
  console.log(JSON.stringify(report, null, 2));
} finally {
  await browser.close();
}
