import { expect, test, type Locator, type Page } from '@playwright/test';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import type { DetectionResult, TaskResultView, TaskView, VisionModelSpec, VisionTaskView } from '../src/types/api';

const realEnabled = process.env.E2E_REAL === '1';
const evidenceDir = path.resolve(process.cwd(), process.env.E2E_EVIDENCE_DIR ?? '../runtime/verification/part11');
const busPath = path.resolve(process.cwd(), '../runtime/samples/yolo-bus.jpg');
const blankPath = path.resolve(process.cwd(), process.env.E2E_BLANK_IMAGE ?? '../runtime/verification/part9/blank.png');
const parameters = { confidenceThreshold: 0.25, iouThreshold: 0.45, maxDetections: 100 };
type DetectionTaskResult = TaskResultView<DetectionResult>;
type SubmissionFields = {
  modelId: string; modelVersion: string; confidenceThreshold: string; iouThreshold: string;
  maxDetections: string; fileName: string; fileSize: number;
};
type ObservedWindow = Window & { __visionSubmissionFields?: SubmissionFields[] };

async function saveReport(name: string, report: Record<string, unknown>) {
  await writeFile(path.join(evidenceDir, name), JSON.stringify(report, null, 2), 'utf8');
}

async function openVision(page: Page) {
  // This observes the original FormData immediately before native fetch; it
  // forwards the unmodified request and never supplies a mock response.
  await page.addInitScript(() => {
    const observed = window as ObservedWindow;
    observed.__visionSubmissionFields = [];
    const nativeFetch = window.fetch.bind(window);
    window.fetch = (input, init) => {
      if (typeof input === 'string' && new URL(input, window.location.href).pathname === '/api/tasks/vision'
        && init?.method === 'POST' && init.body instanceof FormData) {
        const file = init.body.get('file');
        observed.__visionSubmissionFields!.push({
          modelId: String(init.body.get('modelId') ?? ''),
          modelVersion: String(init.body.get('modelVersion') ?? ''),
          confidenceThreshold: String(init.body.get('confidenceThreshold') ?? ''),
          iouThreshold: String(init.body.get('iouThreshold') ?? ''),
          maxDetections: String(init.body.get('maxDetections') ?? ''),
          fileName: file instanceof File ? file.name : '',
          fileSize: file instanceof File ? file.size : -1,
        });
      }
      return nativeFetch(input, init);
    };
  });
  const catalogPending = page.waitForResponse(response => {
    const url = new URL(response.url());
    return response.request().method() === 'GET' && url.pathname === '/api/models'
      && url.searchParams.get('taskType') === 'vision';
  });
  await page.goto('/?type=vision');
  const response = await catalogPending;
  expect(response.status()).toBe(200);
  const models = (await response.json()).data.items as VisionModelSpec[];
  const model = models.find(item => item.modelId === 'yolo-general');
  expect(model).toBeDefined();
  expect(model!.modelVersion).toBe('yolov5n-v7.0-onnx');
  expect(model!.taskType).toBe('vision');
  await expect(page.getByRole('navigation', { name: '主导航' })
    .getByRole('button', { name: '图片检测', exact: true })).toHaveAttribute('aria-current', 'page');
  return model!;
}

async function submitImage(page: Page, model: VisionModelSpec, inputPath: string) {
  const upload = page.getByRole('region', { name: '新建图片检测任务', exact: true });
  await upload.getByLabel('选择图片', { exact: true }).setInputFiles(inputPath);
  await upload.getByLabel('检测模型', { exact: true })
    .selectOption(JSON.stringify([model.modelId, model.modelVersion]));
  await expect(upload).toContainText(model.modelVersion);
  await upload.getByLabel('置信度阈值', { exact: true }).fill(String(parameters.confidenceThreshold));
  await upload.getByLabel('重叠阈值（IoU）', { exact: true }).fill(String(parameters.iouThreshold));
  await upload.getByLabel('最多目标数', { exact: true }).fill(String(parameters.maxDetections));
  await expect(upload.getByRole('img', { name: '待提交图片预览', exact: true })).toBeVisible();
  const accepted = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/tasks/vision');
  await upload.getByRole('button', { name: '提交检测', exact: true }).click();
  const response = await accepted;
  expect(response.status()).toBe(201);
  const receipt = (await response.json()).data as { task: VisionTaskView; replayed: boolean };
  expect(receipt.replayed).toBe(false);
  expect(receipt.task.taskId).toMatch(/^[0-9a-f]{32}$/);
  expect(receipt.task.taskType).toBe('vision');
  expect(receipt.task.modelVersion).toBe(model.modelVersion);
  expect(receipt.task.parameters).toEqual(parameters);
  const fields = await page.evaluate(() => (window as ObservedWindow).__visionSubmissionFields);
  expect(fields).toEqual([{
    modelId: model.modelId, modelVersion: model.modelVersion,
    confidenceThreshold: String(parameters.confidenceThreshold), iouThreshold: String(parameters.iouThreshold),
    maxDetections: String(parameters.maxDetections), fileName: path.basename(inputPath),
    fileSize: (await readFile(inputPath)).length,
  }]);
  await expect(page).toHaveURL(url => url.searchParams.get('type') === 'vision'
    && url.searchParams.get('task') === receipt.task.taskId);
  await expect(upload.getByRole('button', { name: '已提交', exact: true })).toBeDisabled();
  return { receipt, fields, idempotencyKey: response.request().headers()['idempotency-key'] };
}

async function completedResult(page: Page, taskId: string) {
  await expect.poll(async () => {
    const response = await page.request.get(`/api/tasks/${taskId}`);
    expect(response.status()).toBe(200);
    return (await response.json()).data.status;
  }, { timeout: 120_000, intervals: [500, 1000, 2000] }).toBe('succeeded');
  const taskResponse = await page.request.get(`/api/tasks/${taskId}`);
  const resultResponse = await page.request.get(`/api/tasks/${taskId}/result`);
  expect(taskResponse.status()).toBe(200);
  expect(resultResponse.status()).toBe(200);
  const task = (await taskResponse.json()).data as VisionTaskView;
  const result = (await resultResponse.json()).data as DetectionTaskResult;
  expect(task.attempt).toBe(1);
  expect(result.taskId).toBe(taskId);
  expect(result.modelVersion).toBe(task.modelVersion);
  expect(result.output.device).toBe('cpu');
  expect(result.output.coordinateSystem).toBe('original-pixels-exif-normalized');
  return { task, result };
}

async function assertImage(locator: Locator, width: number, height: number) {
  await expect(locator).toBeVisible();
  await expect.poll(() => locator.evaluate(image => ({
    complete: (image as HTMLImageElement).complete,
    width: (image as HTMLImageElement).naturalWidth,
    height: (image as HTMLImageElement).naturalHeight,
  }))).toEqual({ complete: true, width, height });
}

async function saveDownload(page: Page, detail: Locator, buttonName: string, evidenceName: string) {
  const pending = page.waitForEvent('download');
  await detail.getByRole('button', { name: buttonName, exact: true }).click();
  const download = await pending;
  expect(await download.failure()).toBeNull();
  const target = path.join(evidenceDir, evidenceName);
  await download.saveAs(target);
  return { path: target, suggestedFilename: download.suggestedFilename() };
}

async function assertDownloads(page: Page, detail: Locator, result: DetectionTaskResult, prefix: string) {
  const jsonDownload = await saveDownload(page, detail, '下载 JSON', `${prefix}.json`);
  expect(JSON.parse(await readFile(jsonDownload.path, 'utf8'))).toEqual(result);
  const pngDownload = await saveDownload(page, detail, '下载框图 PNG', `${prefix}-annotated.png`);
  const authoritative = await page.request.get(`/api/tasks/${result.taskId}/image?variant=annotated`);
  expect(authoritative.status()).toBe(200);
  expect(authoritative.headers()['content-type']).toContain('image/png');
  const pngBytes = await authoritative.body();
  expect(await readFile(pngDownload.path)).toEqual(pngBytes);
  expect(pngBytes.subarray(0, 8)).toEqual(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
  return { jsonDownload, pngDownload, jsonMatchesPersistedResult: true, pngMatchesImageEndpoint: true };
}

test('真实 YOLO 页面：图片、参数、检测表与下载、刷新和双工作台及三视口', async ({ page }) => {
  test.skip(!realEnabled, '设置E2E_REAL=1并先启动真实平台；本测试不启动服务、不下载模型或自动创建替身。');
  await mkdir(evidenceDir, { recursive: true });
  const pageErrors: string[] = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  const report: Record<string, unknown> = {
    evidenceKind: 'real-browser-real-backend-real-yolo', startedAt: new Date().toISOString(),
    sourceImage: busPath, noNetworkMocking: true, passed: false,
  };
  try {
    const model = await openVision(page);
    const submission = await submitImage(page, model, busPath);
    const taskId = submission.receipt.task.taskId;
    Object.assign(report, { taskId, model, submission });
    await saveReport('vision-browser-report.json', report);
    const { task, result } = await completedResult(page, taskId);
    expect(result.output.imageWidth).toBe(810);
    expect(result.output.imageHeight).toBe(1080);
    expect(result.output.detections).toHaveLength(4);
    expect(result.output.detections.map(item => item.className).sort()).toEqual(['bus', 'person', 'person', 'person']);
    const detail = page.getByRole('region', { name: '图片任务详情', exact: true });
    await expect(detail).toContainText(taskId);
    await expect(detail).toContainText('检测到 4 个目标');
    await expect(detail).toContainText('810 × 1080');
    const table = detail.getByRole('table', { name: '检测目标的类别、置信度和原图像素坐标', exact: true });
    await expect(table.getByRole('row')).toHaveCount(5);
    for (const [index, detection] of result.output.detections.entries()) {
      expect(detection.box.xMin).toBeGreaterThanOrEqual(0);
      expect(detection.box.yMin).toBeGreaterThanOrEqual(0);
      expect(detection.box.xMax).toBeLessThanOrEqual(810);
      expect(detection.box.yMax).toBeLessThanOrEqual(1080);
      expect(detection.box.xMin).toBeLessThan(detection.box.xMax);
      expect(detection.box.yMin).toBeLessThan(detection.box.yMax);
      await expect(table.getByRole('row').nth(index + 1).getByRole('cell')).toHaveText([
        String(index + 1), `${detection.className} / ${detection.classId}`,
        `${(detection.confidence * 100).toFixed(2)}%`, detection.box.xMin.toFixed(2),
        detection.box.yMin.toFixed(2), detection.box.xMax.toFixed(2), detection.box.yMax.toFixed(2),
      ]);
    }
    await assertImage(detail.getByRole('img', { name: '任务原图', exact: true }), 810, 1080);
    const original = await page.request.get(`/api/tasks/${taskId}/image?variant=original`);
    expect(original.status()).toBe(200);
    await writeFile(path.join(evidenceDir, 'vision-original.png'), await original.body());
    await detail.getByRole('button', { name: '检测框图', exact: true }).click();
    await expect(detail.getByRole('button', { name: '检测框图', exact: true })).toHaveAttribute('aria-pressed', 'true');
    await assertImage(detail.getByRole('img', { name: '目标检测框图', exact: true }), 810, 1080);
    await detail.getByRole('tab', { name: '执行信息', exact: true }).click();
    await expect(detail).toContainText(model.modelVersion);
    await expect(detail).toContainText('CPU / float16');
    await expect(detail).toContainText('0.25 / 0.45');
    await detail.getByRole('tab', { name: '检测目标', exact: true }).click();
    const downloads = await assertDownloads(page, detail, result, 'vision-result');

    await page.reload();
    await expect(page).toHaveURL(url => url.searchParams.get('type') === 'vision' && url.searchParams.get('task') === taskId);
    await expect(detail).toContainText('检测到 4 个目标');
    await expect(detail).toContainText(taskId);
    await assertImage(detail.getByRole('img', { name: '任务原图', exact: true }), 810, 1080);

    // Read an existing ASR task without creating an extra ASR test task. The
    // separate real-pipeline spec exercises a new real transcription/upload.
    const asrPage = await page.request.get('/api/tasks?taskType=asr&page=1&pageSize=20');
    expect(asrPage.status()).toBe(200);
    const asrTasks = (await asrPage.json()).data.items as TaskView[];
    const asrTask = asrTasks.find(item => item.status === 'succeeded');
    expect(asrTask).toBeDefined();
    const asrResultResponse = await page.request.get(`/api/tasks/${asrTask!.taskId}/result`);
    expect(asrResultResponse.status()).toBe(200);
    const asrText = (await asrResultResponse.json()).data.output.text as string;
    const navigation = page.getByRole('navigation', { name: '主导航', exact: true });
    await navigation.getByRole('button', { name: '音频转写', exact: true }).click();
    await page.getByRole('button', { name: `查看任务 ${asrTask!.input.fileName} ${asrTask!.taskId.slice(0, 8)}`, exact: true }).click();
    const asrDetail = page.getByRole('region', { name: '任务详情', exact: true });
    await expect(asrDetail).toContainText(asrTask!.taskId);
    await expect(asrDetail).toContainText(asrText);
    await expect(page).toHaveURL(url => !url.searchParams.has('type') && url.searchParams.get('task') === asrTask!.taskId);
    await expect(detail).not.toBeVisible();
    await navigation.getByRole('button', { name: '图片检测', exact: true }).click();
    await expect(detail).toContainText(taskId);
    await expect(detail).toContainText('检测到 4 个目标');
    await expect(asrDetail).not.toBeVisible();
    await expect(page).toHaveURL(url => url.searchParams.get('type') === 'vision' && url.searchParams.get('task') === taskId);
    await navigation.getByRole('button', { name: '音频转写', exact: true }).click();
    await expect(asrDetail).toContainText(asrTask!.taskId);
    await expect(asrDetail).toContainText(asrText);
    await navigation.getByRole('button', { name: '图片检测', exact: true }).click();
    await expect(detail).toContainText(taskId);

    const layouts: Record<string, unknown>[] = [];
    for (const viewport of [{ width: 1920, height: 1080 }, { width: 1366, height: 768 }, { width: 390, height: 844 }]) {
      await page.setViewportSize(viewport);
      await detail.getByRole('button', { name: '原图', exact: true }).click();
      await assertImage(detail.getByRole('img', { name: '任务原图', exact: true }), 810, 1080);
      await detail.getByRole('button', { name: '检测框图', exact: true }).click();
      await assertImage(detail.getByRole('img', { name: '目标检测框图', exact: true }), 810, 1080);
      await detail.getByRole('tab', { name: '执行信息', exact: true }).click();
      await expect(detail).toContainText(model.modelVersion);
      await detail.getByRole('tab', { name: '检测目标', exact: true }).click();
      const dimensions = await page.evaluate(() => ({
        viewportWidth: window.innerWidth, documentWidth: document.documentElement.scrollWidth, bodyWidth: document.body.scrollWidth,
      }));
      expect(dimensions.documentWidth).toBeLessThanOrEqual(viewport.width);
      expect(dimensions.bodyWidth).toBeLessThanOrEqual(viewport.width);
      const tableScroll = detail.getByRole('region', { name: '检测目标表，可横向滚动', exact: true });
      const tableDimensions = await tableScroll.evaluate(element => ({ width: element.clientWidth, contentWidth: element.scrollWidth }));
      await tableScroll.scrollIntoViewIfNeeded();
      await tableScroll.evaluate(element => { element.scrollLeft = element.scrollWidth; });
      await expect(table.getByRole('columnheader', { name: 'yMax', exact: true })).toBeInViewport();
      await tableScroll.evaluate(element => { element.scrollLeft = 0; });
      await page.evaluate(() => {
        if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
        window.scrollTo({ top: 0, left: 0, behavior: 'instant' });
      });
      await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
      const screenshotName = `vision-${viewport.width}x${viewport.height}.png`;
      await page.screenshot({ path: path.join(evidenceDir, screenshotName), fullPage: true });
      layouts.push({ ...viewport, ...dimensions, tableDimensions, screenshotName });
    }
    const finalResult = (await (await page.request.get(`/api/tasks/${taskId}/result`)).json()).data;
    expect(finalResult).toEqual(result);
    expect(pageErrors).toEqual([]);
    Object.assign(report, { task, result, downloads, layouts, pageErrors,
      exactOriginalFormData: true, selectionAndResultSurvivedRefresh: true,
      workspaceSelectionsIsolated: true, existingAsrTaskId: asrTask!.taskId, persistedResultUnchanged: true, passed: true });
  } catch (error) {
    report.failure = error instanceof Error ? error.message : String(error);
    throw error;
  } finally {
    Object.assign(report, { completedAt: new Date().toISOString(), pageErrors });
    await saveReport('vision-browser-report.json', report);
  }
});

test('真实 YOLO 纯白图片：空检测仍成功并可下载', async ({ page }) => {
  test.skip(!realEnabled, '需要现有真实模型和640×480纯白PNG；不自动生成或下载样例。');
  await mkdir(evidenceDir, { recursive: true });
  const pageErrors: string[] = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  const report: Record<string, unknown> = {
    evidenceKind: 'real-browser-real-backend-real-yolo-empty', sourceImage: blankPath,
    startedAt: new Date().toISOString(), noNetworkMocking: true, passed: false,
  };
  try {
    const model = await openVision(page);
    const submission = await submitImage(page, model, blankPath);
    Object.assign(report, { taskId: submission.receipt.task.taskId, submission });
    const { task, result } = await completedResult(page, submission.receipt.task.taskId);
    expect(result.output.imageWidth).toBe(640);
    expect(result.output.imageHeight).toBe(480);
    expect(result.output.detections).toEqual([]);
    const detail = page.getByRole('region', { name: '图片任务详情', exact: true });
    await expect(detail.getByRole('heading', { name: '检测成功，本次没有发现目标', exact: true })).toBeVisible();
    await expect(detail).toContainText('检测到 0 个目标');
    await expect(detail.getByRole('table')).toHaveCount(0);
    await detail.getByRole('button', { name: '检测框图', exact: true }).click();
    await assertImage(detail.getByRole('img', { name: '目标检测框图', exact: true }), 640, 480);
    const downloads = await assertDownloads(page, detail, result, 'vision-empty-result');
    await page.reload();
    await expect(detail).toContainText('检测成功，本次没有发现目标');
    await expect(page).toHaveURL(url => url.searchParams.get('type') === 'vision' && url.searchParams.get('task') === task.taskId);
    await page.screenshot({ path: path.join(evidenceDir, 'vision-empty.png'), fullPage: true });
    expect(pageErrors).toEqual([]);
    Object.assign(report, { task, result, downloads, refreshed: true, passed: true });
  } catch (error) {
    report.failure = error instanceof Error ? error.message : String(error);
    throw error;
  } finally {
    Object.assign(report, { completedAt: new Date().toISOString(), pageErrors });
    await saveReport('vision-empty-report.json', report);
  }
});

test('损坏图片：真实页面预检拒绝且后台任务数不增加', async ({ page }) => {
  test.skip(!realEnabled, '需要真实API核对前后任务数；仅验证前端格式预检，不冒称后台解码验收。');
  await mkdir(evidenceDir, { recursive: true });
  const pageErrors: string[] = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  await openVision(page);
  const beforeResponse = await page.request.get('/api/tasks?taskType=all');
  expect(beforeResponse.status()).toBe(200);
  const beforeTotal = (await beforeResponse.json()).data.total;
  let visionPostCount = 0;
  page.on('request', request => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/tasks/vision') visionPostCount += 1;
  });
  const upload = page.getByRole('region', { name: '新建图片检测任务', exact: true });
  await upload.getByLabel('选择图片', { exact: true }).setInputFiles({
    name: 'damaged-image.png', mimeType: 'image/png', buffer: Buffer.from('This file is deliberately not an image.'),
  });
  await expect(upload.getByRole('alert')).toContainText('文件内容与支持的格式不符');
  await expect(upload.getByRole('button', { name: '提交检测', exact: true })).toBeDisabled();
  const afterResponse = await page.request.get('/api/tasks?taskType=all');
  expect(afterResponse.status()).toBe(200);
  const afterTotal = (await afterResponse.json()).data.total;
  expect(afterTotal).toBe(beforeTotal);
  expect(visionPostCount).toBe(0);
  expect(pageErrors).toEqual([]);
  await page.screenshot({ path: path.join(evidenceDir, 'vision-invalid-image.png'), fullPage: true });
  await saveReport('vision-invalid-image-report.json', {
    evidenceKind: 'real-browser-client-rejection-real-backend-count', at: new Date().toISOString(),
    noNetworkMocking: true, beforeTotal, afterTotal, visionPostCount, pageErrors, passed: true,
  });
});
