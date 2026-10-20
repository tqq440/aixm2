import { expect, test } from '@playwright/test';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';

test('模拟接口断开：明确说明不可用，不显示伪造任务或运行状态', async ({ page }) => {
  // Only this error-path test intercepts network traffic. It is explicitly
  // separate from real-pipeline.spec.ts and is not model-execution evidence.
  // Vite serves /src/api/client.ts too; match only the backend URL prefix.
  await page.route((url) => url.pathname.startsWith('/api/'), (route) => route.abort('connectionrefused'));
  await page.goto('/');
  await expect(page.getByRole('alert').first()).toBeVisible();
  await expect(page.locator('body')).toContainText(/无法连接|连接失败|接口断开|暂时无法|无法访问/);
  const runtime = page.getByRole('region', { name: '运行状态', exact: true });
  await expect(runtime).toContainText('连接异常');
  await expect(runtime.getByText('状态未知', { exact: true })).toHaveCount(2);
  await expect(page.getByRole('region', { name: '任务列表', exact: true })).toContainText('任务列表暂时不可用');
  await expect(page.getByRole('button', { name: /^查看任务 / })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '提交转写', exact: true })).toBeDisabled();
  const evidenceDir = path.resolve(process.cwd(), process.env.E2E_EVIDENCE_DIR ?? '../runtime/verification/part5');
  await mkdir(evidenceDir, { recursive: true });
  await page.screenshot({ path: path.join(evidenceDir, 'simulated-api-disconnected.png'), fullPage: true });
  await writeFile(path.join(evidenceDir, 'simulated-api-disconnected-report.json'), JSON.stringify({
    evidenceKind: 'browser-network-failure-simulation',
    at: new Date().toISOString(),
    routeAborted: 'URL.pathname starts with /api/',
    inferenceVerified: false,
    passed: true,
  }, null, 2), 'utf8');
});
