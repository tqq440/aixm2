"""YOLOv5 原始输出的有界数值转换；不读取配置、不加载模型。"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np


class DetectionOutputError(ValueError):
    """推理输出不符合约定，不能作为成功检测交付。"""


@dataclass(frozen=True)
class LetterboxInput:
    tensor: "np.ndarray"
    scale: float
    pad_x: float
    pad_y: float
    original_width: int
    original_height: int


@dataclass(frozen=True)
class RawDetection:
    class_id: int
    confidence: float
    x_min: float
    y_min: float
    x_max: float
    y_max: float


def letterbox_rgb(rgb: "np.ndarray", *, input_size: int = 640) -> LetterboxInput:
    """Pillow 双线性等比缩放，居中填充114，输出连续 RGB/FP16/NCHW。

    本项目固定为 pillow-bilinear-letterbox-v1，不宣称与 OpenCV 插值逐位相同。
    逆变换使用本次实际采用的整数左/上填充与统一缩放比例。
    """
    import numpy as np
    from PIL import Image

    if not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("输入必须为 uint8 RGB 图片数组")
    if isinstance(input_size, bool) or not isinstance(input_size, int) or input_size <= 0:
        raise ValueError("模型输入尺寸必须为正整数")
    height, width = rgb.shape[:2]
    if min(height, width) <= 0:
        raise ValueError("图片尺寸不能为空")
    scale = min(input_size / height, input_size / width)
    resized_width = max(1, round(width * scale))
    resized_height = max(1, round(height * scale))
    left = (input_size - resized_width) // 2
    top = (input_size - resized_height) // 2
    with Image.fromarray(rgb) as original:
        with original.resize((resized_width, resized_height), Image.Resampling.BILINEAR) as resized:
            pixels = np.asarray(resized, dtype=np.uint8)
            canvas = np.full((input_size, input_size, 3), 114, dtype=np.uint8)
            canvas[top:top + resized_height, left:left + resized_width] = pixels
    normalized = canvas.transpose(2, 0, 1).astype(np.float32) / np.float32(255.0)
    tensor = np.ascontiguousarray(normalized[None], dtype=np.float16)
    return LetterboxInput(tensor, scale, float(left), float(top), width, height)


def postprocess_yolov5(
    prediction: "np.ndarray",
    transform: LetterboxInput,
    *,
    confidence_threshold: float,
    iou_threshold: float,
    max_detections: int,
    class_count: int = 80,
    max_candidates: int = 30000,
) -> list[RawDetection]:
    """解析固定640模型的原始候选，执行一次按类别NMS，再还原原图像素。

    该输出尚未NMS，列依次为中心xywh、目标概率和各类别条件概率。
    概率使用float32、几何使用float64避免FP16溢出；最多25200候选、保留300框。
    """
    import numpy as np

    if not (0 <= confidence_threshold <= 1 and 0 <= iou_threshold <= 1):
        raise ValueError("阈值必须在0到1之间")
    if isinstance(max_detections, bool) or not isinstance(max_detections, int) or not 1 <= max_detections <= 300:
        raise ValueError("检测框数量必须在1到300之间")
    if class_count != 80 or isinstance(class_count, bool):
        raise ValueError("固定模型必须使用80个类别")
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or max_candidates < 1:
        raise ValueError("候选数量上限无效")
    if not isinstance(prediction, np.ndarray) or prediction.shape != (1, 25200, 5 + class_count):
        raise DetectionOutputError("模型输出形状应为1×25200×85")
    if prediction.dtype not in (np.float16, np.float32):
        raise DetectionOutputError("模型输出必须是浮点数组")
    if prediction.shape[1] > max_candidates:
        raise DetectionOutputError("模型候选数量超过上限")
    if not np.isfinite(prediction).all():
        raise DetectionOutputError("模型输出含非有限数值")
    rows = prediction[0].astype(np.float32, copy=True)
    if np.any(rows[:, 2:4] < 0) or np.any(rows[:, 4:] < 0) or np.any(rows[:, 4:] > 1):
        raise DetectionOutputError("模型输出含无效尺寸或概率")
    if (
        not np.isfinite([transform.scale, transform.pad_x, transform.pad_y]).all()
        or transform.scale <= 0 or min(transform.original_width, transform.original_height) <= 0
        or min(transform.pad_x, transform.pad_y) < 0
    ):
        raise DetectionOutputError("图片坐标变换无效")

    rows = rows[rows[:, 4] > confidence_threshold]
    if not len(rows):
        return []
    class_ids = rows[:, 5:].argmax(axis=1)
    scores = rows[:, 4] * rows[np.arange(len(rows)), class_ids + 5]
    selected = scores > confidence_threshold
    rows, class_ids, scores = rows[selected], class_ids[selected], scores[selected]
    if not len(rows):
        return []
    if np.any(rows[:, 2:4] <= 0):
        raise DetectionOutputError("有效候选的宽高必须大于零")
    # 先在模型平面做NMS；不得提前clip，以免改变交并比。
    # 使用float64计算几何量，避免不可信的大float32值在面积/交并比时溢出。
    geometry = rows[:, :4].astype(np.float64)
    boxes = np.empty((len(rows), 4), dtype=np.float64)
    boxes[:, :2] = geometry[:, :2] - geometry[:, 2:4] / 2
    boxes[:, 2:] = geometry[:, :2] + geometry[:, 2:4] / 2
    if not np.isfinite(boxes).all():
        raise DetectionOutputError("模型坐标转换溢出")
    if np.any(boxes[:, 2:] <= boxes[:, :2]):
        raise DetectionOutputError("模型检测框坐标没有正面积")
    areas = geometry[:, 2] * geometry[:, 3]
    if not np.isfinite(areas).all():
        raise DetectionOutputError("模型检测框面积溢出")
    remaining = np.argsort(-scores, kind="stable")
    output: list[RawDetection] = []
    retained = 0
    while len(remaining) and retained < max_detections:
        current = remaining[0]
        retained += 1
        # 还原后完全落在填充区的候选没有可交付的原图区域。
        restored = boxes[current].astype(np.float64)
        restored[[0, 2]] = (restored[[0, 2]] - transform.pad_x) / transform.scale
        restored[[1, 3]] = (restored[[1, 3]] - transform.pad_y) / transform.scale
        restored[[0, 2]] = restored[[0, 2]].clip(0, transform.original_width)
        restored[[1, 3]] = restored[[1, 3]].clip(0, transform.original_height)
        if not np.isfinite(restored).all():
            raise DetectionOutputError("原图坐标转换溢出")
        if restored[2] > restored[0] and restored[3] > restored[1]:
            output.append(RawDetection(int(class_ids[current]), float(scores[current]), *map(float, restored)))
        rest = remaining[1:]
        if not len(rest):
            break
        intersection_wh = np.maximum(0, np.minimum(boxes[current, 2:], boxes[rest, 2:]) - np.maximum(boxes[current, :2], boxes[rest, :2]))
        intersection = intersection_wh[:, 0] * intersection_wh[:, 1]
        union = areas[current] + areas[rest] - intersection
        iou = np.divide(intersection, union, out=np.zeros_like(intersection), where=union > 0)
        suppress = (class_ids[rest] == class_ids[current]) & (iou > iou_threshold)
        remaining = rest[~suppress]
    return output
