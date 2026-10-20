"""有界解码静态图片；文件和像素上限由调用者提供。"""

from dataclasses import dataclass
from io import BytesIO
import os
from pathlib import Path
import stat
from typing import TYPE_CHECKING
import warnings

if TYPE_CHECKING:
    import numpy as np


class ImageDecodeError(ValueError):
    """图片为空、损坏、截断，或无法正常读取和解码。"""


class ImageFileTooLargeError(ImageDecodeError):
    """输入文件超过调用者提供的字节上限。"""


class ImagePixelLimitError(ImageDecodeError):
    """图片像素数超过调用者或解码器的安全上限。"""


class ImageNotFoundError(ImageDecodeError):
    """指定路径不存在或不是普通文件。"""


class ImageFormatError(ImageDecodeError):
    """文件不是受支持的静态 JPEG 或 PNG。"""


@dataclass(frozen=True)
class PreparedImage:
    """EXIF 归正后的原图；rgb 为连续排列的 uint8 H×W×3 数组。"""

    rgb: "np.ndarray"
    width: int
    height: int


def _check_file_metadata(metadata: os.stat_result, max_file_bytes: int) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise ImageNotFoundError("图片路径不是普通文件")
    if metadata.st_size > max_file_bytes:
        raise ImageFileTooLargeError("图片文件大小超过上限")
    if metadata.st_size == 0:
        raise ImageDecodeError("图片文件为空")


def _read_bounded_image(path: Path, max_file_bytes: int) -> bytes:
    try:
        _check_file_metadata(path.stat(), max_file_bytes)
        with path.open("rb") as source:
            _check_file_metadata(os.fstat(source.fileno()), max_file_bytes)
            # 不使用 read_bytes：即使文件在 stat 后增长，也只读取上限加一个字节。
            content = source.read(max_file_bytes + 1)
            if len(content) > max_file_bytes:
                raise ImageFileTooLargeError("图片文件大小超过上限")
            _check_file_metadata(os.fstat(source.fileno()), max_file_bytes)
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
        raise ImageNotFoundError("图片路径不存在或不是普通文件") from exc
    except OSError as exc:
        raise ImageDecodeError("无法读取图片文件") from exc
    if not content:
        raise ImageDecodeError("图片文件为空")
    return content


def decode_image_file(
    path: Path,
    *,
    max_file_bytes: int,
    max_pixels: int,
) -> PreparedImage:
    """将本地静态 JPEG/PNG 转为 RGB 原图，按真实内容识别格式。

    先校验文件大小与头部尺寸，再验证文件完整性和解码像素。拒绝动画及
    多帧图片，保留 Pillow 自带的解压炸弹保护，不修改共享解码器设置。
    EXIF 方向会归正，透明像素合成到白底；返回坐标系以归正后的尺寸为准。
    """
    for limit in (max_file_bytes, max_pixels):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("图片字节和像素上限必须为正整数")

    content = _read_bounded_image(Path(path), max_file_bytes)

    # 仅在真实解码时加载图片和数组依赖，导入工具不触发模型或文件初始化。
    import numpy as np
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content), formats=["JPEG", "PNG"]) as probe:
                width, height = probe.size
                if width <= 0 or height <= 0:
                    raise ImageDecodeError("图片尺寸无效")
                if width * height > max_pixels:
                    raise ImagePixelLimitError("图片像素数超过上限")
                if getattr(probe, "n_frames", 1) != 1 or getattr(probe, "is_animated", False):
                    raise ImageFormatError("不支持动画或多帧图片")
                probe.verify()

            # verify 后必须重新打开；两次读取的是同一份有界字节快照。
            with Image.open(BytesIO(content), formats=["JPEG", "PNG"]) as source:
                source.load()
                with ImageOps.exif_transpose(source) as oriented:
                    if "A" in oriented.getbands() or "transparency" in oriented.info:
                        with oriented.convert("RGBA") as foreground:
                            with Image.new("RGBA", oriented.size, (255, 255, 255, 255)) as background:
                                with Image.alpha_composite(background, foreground) as combined:
                                    with combined.convert("RGB") as converted:
                                        rgb = np.array(converted, dtype=np.uint8, copy=True, order="C")
                    else:
                        with oriented.convert("RGB") as converted:
                            rgb = np.array(converted, dtype=np.uint8, copy=True, order="C")
                    width, height = oriented.size
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImagePixelLimitError("图片超过解码器安全像素上限") from exc
    except UnidentifiedImageError as exc:
        if content.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8")):
            raise ImageDecodeError("JPEG 或 PNG 图片头损坏或截断") from exc
        raise ImageFormatError("仅支持静态 JPEG 和 PNG 图片") from exc
    except ImageDecodeError:
        raise
    except (OSError, ValueError, SyntaxError, EOFError) as exc:
        raise ImageDecodeError("图片损坏、截断或无法解码") from exc

    return PreparedImage(rgb=rgb, width=width, height=height)
