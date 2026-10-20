import { expect, test, type Page } from '@playwright/test';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';

const evidenceDir = path.resolve(process.cwd(), process.env.E2E_EVIDENCE_DIR ?? '../runtime/verification/part5');
const audioPath = path.resolve(process.cwd(), '../runtime/samples/asr_example_zh.wav');
const realEnabled = (process.env.E2E_REAL ?? process.env.PART5_REAL) === '1';

interface Transcription {
  text: string;
  segments: { start: number; end: number; text: string }[];
  device: string;
  modelRepo: string;
  modelRevision: string;
  computeType: string;
  transcriptionSeconds: number;
  audioDurationSeconds: number;
}

interface Result {
  taskId: string;
  modelId: string;
  modelVersion: string;
  schemaVersion: string;
  output: Transcription;
}

type ObservedWindow = Window & {
  __asrSubmissionFields?: { modelId: string; modelVersion: string }[];
};

async function download(page: Page, buttonName: string, evidenceName: string) {
  const pending = page.waitForEvent('download');
  await page.getByRole('button', { name: buttonName, exact: true }).click();
  const item = await pending;
  expect(await item.failure()).toBeNull();
  const target = path.join(evidenceDir, evidenceName);
  await item.saveAs(target);
  return { path: target, suggestedFilename: item.suggestedFilename() };
}

test('真实 Whisper：上传、等待、全文与分段、复制下载、刷新及三种屏幕', async ({ page, context }) => {
  test.skip(!realEnabled, '设置 E2E_REAL=1 并先启动真实 API、投递器、Worker、Vite 才执行；不自动创建任务。');
  await mkdir(evidenceDir, { recursive: true });
  const pageErrors: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));
  const report: Record<string, unknown> = {
    evidenceKind: 'real-browser-real-backend-real-whisper',
    startedAt: new Date().toISOString(),
    sourceAudio: audioPath,
    noNetworkMocking: true,
  };
  // Playwright may omit multipart bodies containing files. Observe only these
  // two text fields immediately before native fetch, forwarding the original
  // request unchanged; no endpoint or response is mocked.
  await page.addInitScript(() => {
    const observed = window as ObservedWindow;
    observed.__asrSubmissionFields = [];
    const nativeFetch = window.fetch.bind(window);
    window.fetch = (input, init) => {
      if (typeof input === 'string' && new URL(input, window.location.href).pathname === '/api/tasks/asr'
        && init?.method === 'POST' && init.body instanceof FormData) {
        observed.__asrSubmissionFields!.push({
          modelId: String(init.body.get('modelId') ?? ''),
          modelVersion: String(init.body.get('modelVersion') ?? ''),
        });
      }
      return nativeFetch(input, init);
    };
  });
  const catalogResponse = page.waitForResponse((response) =>
    response.request().method() === 'GET' && new URL(response.url()).pathname === '/api/models');
  await page.goto('/');
  const catalog = (await (await catalogResponse).json()).data.items as { modelId: string; modelVersion: string }[];
  const selectedModel = catalog.find((model) => model.modelId === 'whisper-small');
  expect(selectedModel).toBeDefined();
  const selectedVersion = selectedModel!.modelVersion;
  await expect(page.getByRole('region', { name: '任务列表', exact: true })).toBeVisible();
  await page.getByLabel('选择音频', { exact: true }).setInputFiles(audioPath);
  await page.getByLabel('模型', { exact: true }).selectOption(JSON.stringify(['whisper-small', selectedVersion]));
  await expect(page.getByRole('region', { name: '新建转写任务', exact: true })).toContainText(selectedVersion);
  await page.getByLabel('识别语言', { exact: true }).selectOption('zh');
  const accepted = page.waitForResponse((response) =>
    response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/tasks/asr');
  await page.getByRole('button', { name: '提交转写', exact: true }).click();
  const acceptedResponse = await accepted;
  report.idempotencyKey = acceptedResponse.request().headers()['idempotency-key'];
  expect(acceptedResponse.status()).toBe(201);
  const submittedFields = await page.evaluate(() => (window as ObservedWindow).__asrSubmissionFields);
  expect(submittedFields).toEqual([{ modelId: 'whisper-small', modelVersion: selectedVersion }]);
  const receipt = (await acceptedResponse.json()).data;
  expect(receipt.task.modelVersion).toBe(selectedVersion);
  const taskId = receipt.task.taskId as string;
  expect(taskId).toMatch(/^[0-9a-f]{32}$/);
  report.taskId = taskId;
  report.receipt = receipt;
  report.submittedModel = {
    modelId: 'whisper-small', modelVersion: selectedVersion, exactVersionInMultipart: true,
    verificationMethod: 'observe-original-FormData-before-native-fetch',
  };
  await expect(page).toHaveURL(new RegExp(`task=${taskId}`));
  await expect(page.getByRole('button', { name: '已提交', exact: true })).toBeDisabled();
  await writeFile(path.join(evidenceDir, 'real-browser-report.json'), JSON.stringify(report, null, 2), 'utf8');

  await expect.poll(async () => {
    const response = await page.request.get(`/api/tasks/${taskId}`);
    expect(response.ok()).toBeTruthy();
    return (await response.json()).data.status;
  }, { timeout: 120_000, intervals: [1000, 2000] }).toBe('succeeded');
  const taskResponse = await page.request.get(`/api/tasks/${taskId}`);
  const task = (await taskResponse.json()).data;
  const resultResponse = await page.request.get(`/api/tasks/${taskId}/result`);
  expect(resultResponse.status()).toBe(200);
  const result = (await resultResponse.json()).data as Result;
  expect(result.taskId).toBe(taskId);
  expect(result.modelVersion).toBe(selectedVersion);
  expect(result.output.text.trim().length).toBeGreaterThan(0);
  expect(result.output.segments.length).toBeGreaterThan(0);
  expect(result.output.device).toBe('cpu');
  expect(task.attempt).toBe(1);
  const detail = page.getByRole('region', { name: '任务详情', exact: true });
  await expect(detail).toContainText(result.output.text);
  await page.getByRole('tab', { name: '时间分段', exact: true }).click();
  for (const segment of result.output.segments) {
    await expect(detail).toContainText(segment.text.trim());
  }
  await page.getByRole('tab', { name: '执行信息', exact: true }).click();
  await expect(detail).toContainText(result.modelId);
  await expect(detail).toContainText(result.modelVersion);
  await expect(detail).toContainText(result.output.computeType);
  await page.getByRole('tab', { name: '转写全文', exact: true }).click();
  await expect(detail).toContainText(result.output.text);

  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.getByRole('button', { name: '复制文字', exact: true }).click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(result.output.text);
  const txtDownload = await download(page, '下载 TXT', 'real-transcript.txt');
  const jsonDownload = await download(page, '下载 JSON', 'real-transcript.json');
  expect(await readFile(txtDownload.path, 'utf8')).toBe(result.output.text);
  expect(JSON.parse(await readFile(jsonDownload.path, 'utf8'))).toEqual(result);

  await page.reload();
  await expect(page).toHaveURL(new RegExp(`task=${taskId}`));
  await expect(detail).toContainText(result.output.text);
  const layouts: Record<string, unknown>[] = [];
  for (const viewport of [
    { width: 1920, height: 1080 },
    { width: 1366, height: 768 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    await expect(detail).toContainText(result.output.text);
    const dimensions = await page.evaluate(() => ({
      viewportWidth: window.innerWidth,
      documentWidth: document.documentElement.scrollWidth,
      bodyWidth: document.body.scrollWidth,
    }));
    expect(dimensions.documentWidth).toBeLessThanOrEqual(viewport.width);
    expect(dimensions.bodyWidth).toBeLessThanOrEqual(viewport.width);
    // Exercise the real mobile tab and result controls, not only a screenshot.
    await page.getByRole('tab', { name: '时间分段', exact: true }).click();
    await expect(detail).toContainText(result.output.segments[0].text.trim());
    await page.getByRole('tab', { name: '转写全文', exact: true }).click();
    // Interaction scrolls the selected controls into view. Capture from the
    // top without keyboard focus so a full-page image represents the layout.
    await page.evaluate(() => {
      if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
      window.scrollTo({ top: 0, left: 0, behavior: 'instant' });
    });
    await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
    const screenshotName = `real-${viewport.width}x${viewport.height}.png`;
    await page.screenshot({ path: path.join(evidenceDir, screenshotName), fullPage: true });
    layouts.push({ ...viewport, ...dimensions, screenshotName });
  }
  expect(pageErrors).toEqual([]);
  Object.assign(report, {
    completedAt: new Date().toISOString(),
    task,
    result,
    copiedTextMatches: true,
    txtDownload,
    jsonDownload,
    downloadsMatchPersistedResult: true,
    selectionAndResultSurvivedRefresh: true,
    layouts,
    pageErrors,
    passed: true,
  });
  await writeFile(path.join(evidenceDir, 'real-browser-report.json'), JSON.stringify(report, null, 2), 'utf8');
});

test('真实接口拒绝损坏音频，页面显示错误且不新增任务', async ({ page }) => {
  test.skip(!realEnabled, '与真实服务一同验收；损坏音频不得创建任务。');
  await mkdir(evidenceDir, { recursive: true });
  await page.goto('/');
  const beforeResponse = await page.request.get('/api/tasks');
  expect(beforeResponse.status()).toBe(200);
  const beforeTotal = (await beforeResponse.json()).data.total;
  await page.getByLabel('选择音频', { exact: true }).setInputFiles({
    name: 'damaged-audio.wav',
    mimeType: 'audio/wav',
    buffer: Buffer.from('This is deliberately not an audio file.'),
  });
  const rejected = page.waitForResponse((response) =>
    response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/tasks/asr');
  await page.getByRole('button', { name: '提交转写', exact: true }).click();
  const response = await rejected;
  expect(response.status()).toBeGreaterThanOrEqual(400);
  expect(response.status()).toBeLessThan(500);
  const error = await response.json();
  expect(error.error.code).toBe('AUDIO_DECODE_FAILED');
  await expect(page.getByRole('alert')).toContainText(/无法正常解码|损坏/);
  const afterTotal = (await (await page.request.get('/api/tasks')).json()).data.total;
  expect(afterTotal).toBe(beforeTotal);
  await page.screenshot({ path: path.join(evidenceDir, 'real-damaged-audio.png'), fullPage: true });
  await writeFile(path.join(evidenceDir, 'real-damaged-audio-report.json'), JSON.stringify({
    evidenceKind: 'real-browser-real-backend-invalid-audio',
    at: new Date().toISOString(),
    status: response.status(),
    error,
    beforeTotal,
    afterTotal,
    passed: true,
  }, null, 2), 'utf8');
});
