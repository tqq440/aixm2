"""复用有界解码工具，将图片输入问题转换为平台错误。"""

from pathlib import Path

from backend.common.exceptions.app_error import AppError
from backend.utils.image.image_utils import (
    ImageDecodeError, ImageFileTooLargeError, ImageFormatError, ImageNotFoundError,
    ImagePixelLimitError, PreparedImage, decode_image_file,
)


def prepare_image(path: Path, *, max_file_bytes: int, max_pixels: int) -> PreparedImage:
    try:
        return decode_image_file(path, max_file_bytes=max_file_bytes, max_pixels=max_pixels)
    except ImageFileTooLargeError as exc:
        raise AppError("IMAGE_TOO_LARGE", "图片超过文件大小上限。", 413) from exc
    except ImagePixelLimitError as exc:
        raise AppError("IMAGE_TOO_MANY_PIXELS", "图片解码后的像素数量超过上限。", 413) from exc
    except ImageNotFoundError as exc:
        raise AppError("IMAGE_NOT_FOUND", "图片文件不存在或不是普通文件。", 404) from exc
    except ImageFormatError as exc:
        raise AppError("IMAGE_FORMAT_UNSUPPORTED", "仅支持静态 JPEG 或 PNG 图片。", 422) from exc
    except ImageDecodeError as exc:
        raise AppError("IMAGE_DECODE_FAILED", "图片损坏、截断或无法正常解码。", 422) from exc
    except ImportError as exc:
        raise AppError("VISION_DEPENDENCY_MISSING", "图片运行库不可用，请核对 requirements/vision.txt。", 503) from exc
