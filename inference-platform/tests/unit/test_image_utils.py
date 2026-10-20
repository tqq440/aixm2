"""真实生成的小图片验证解码边界，不作为真实 YOLO 检测验收。"""

import os
from pathlib import Path
import subprocess
import sys

import numpy as np
from PIL import Image
import pytest

from backend.utils.image.image_utils import (
    ImageDecodeError,
    ImageFileTooLargeError,
    ImageFormatError,
    ImageNotFoundError,
    ImagePixelLimitError,
    decode_image_file,
)


@pytest.fixture
def image_factory(tmp_path):
    def create(*, mode="RGB", size=(8, 6), color=None, image_format="PNG", **save_options):
        path = tmp_path / f"fixture-{mode}.{image_format.lower()}"
        with Image.new(mode, size, color=color) as source:
            source.save(path, format=image_format, **save_options)
        return path

    return create


def decode(path, **limits):
    return decode_image_file(path, max_file_bytes=limits.get("max_file_bytes", 65536),
                             max_pixels=limits.get("max_pixels", 10000))


@pytest.mark.parametrize("image_format", ["JPEG", "PNG"])
def test_rgb_image_has_original_dimensions_and_contiguous_uint8_rgb(image_factory, image_format):
    path = image_factory(color=(255, 10, 30), image_format=image_format)
    result = decode(path)
    assert (result.width, result.height) == (8, 6)
    assert result.rgb.shape == (6, 8, 3)
    assert result.rgb.dtype == np.uint8
    assert result.rgb.flags.c_contiguous
    assert np.allclose(result.rgb[0, 0], [255, 10, 30], atol=2)


def test_format_is_checked_from_content_instead_of_extension(image_factory):
    path = image_factory(color=(12, 34, 56))
    renamed = path.with_suffix(".jpeg")
    path.rename(renamed)
    assert decode(renamed).rgb[0, 0].tolist() == [12, 34, 56]


def test_gif_with_jpeg_extension_is_rejected(image_factory):
    path = image_factory(image_format="GIF")
    renamed = path.with_suffix(".jpeg")
    path.rename(renamed)
    with pytest.raises(ImageFormatError, match="JPEG 和 PNG"):
        decode(renamed)


@pytest.mark.parametrize("content,exception", [
    (b"", ImageDecodeError),
    (b"this is not an image", ImageFormatError),
    (b"\x89PNG\r\n\x1a\n", ImageDecodeError),
    (b"\xff\xd8", ImageDecodeError),
])
def test_empty_unknown_and_damaged_headers_have_explicit_errors(tmp_path, content, exception):
    path = tmp_path / "broken.png"
    path.write_bytes(content)
    with pytest.raises(exception):
        decode(path)


@pytest.mark.parametrize("kind", ["missing", "directory", "parent_is_file"])
def test_input_must_be_an_existing_regular_file(tmp_path, kind):
    path = tmp_path / "input.png"
    if kind == "directory":
        path.mkdir()
    elif kind == "parent_is_file":
        path.write_bytes(b"x")
        path = path / "child.png"
    with pytest.raises(ImageNotFoundError):
        decode(path)


def test_file_at_byte_limit_is_allowed_but_one_byte_below_rejected(image_factory):
    path = image_factory()
    size = path.stat().st_size
    assert decode(path, max_file_bytes=size).width == 8
    with pytest.raises(ImageFileTooLargeError):
        decode(path, max_file_bytes=size - 1)


def test_growth_after_stat_cannot_trigger_an_unbounded_read(image_factory, monkeypatch):
    path = image_factory()
    max_file_bytes = path.stat().st_size + 5
    original_open = Path.open
    requested_sizes = []

    class GrowingFile:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            requested_sizes.append(size)
            with original_open(path, "ab") as writer:
                writer.write(b"x" * 10)
            return self.stream.read(size)

    def open_growing(candidate, mode="r", *args, **kwargs):
        stream = original_open(candidate, mode, *args, **kwargs)
        return GrowingFile(stream) if candidate == path and mode == "rb" else stream

    monkeypatch.setattr(Path, "open", open_growing)
    with pytest.raises(ImageFileTooLargeError):
        decode(path, max_file_bytes=max_file_bytes)
    assert requested_sizes == [max_file_bytes + 1]


def test_pixel_limit_is_checked_before_load(image_factory, monkeypatch):
    path = image_factory(size=(64, 48))
    assert decode(path, max_pixels=64 * 48).width == 64

    def forbidden_load(*args, **kwargs):
        pytest.fail("像素超限时不应载入像素")

    from PIL import PngImagePlugin
    monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", forbidden_load)
    with pytest.raises(ImagePixelLimitError):
        decode(path, max_pixels=64 * 48 - 1)


def test_decode_does_not_change_shared_pillow_pixel_limit(image_factory):
    original_limit = Image.MAX_IMAGE_PIXELS
    decode(image_factory())
    assert Image.MAX_IMAGE_PIXELS == original_limit


@pytest.mark.parametrize("image_format", ["JPEG", "PNG"])
def test_truncated_payload_is_rejected(image_factory, image_format):
    path = image_factory(image_format=image_format)
    content = path.read_bytes()
    path.write_bytes(content[:-12] if image_format == "PNG" else content[:-100])
    with pytest.raises(ImageDecodeError):
        decode(path)


def test_png_corrupted_payload_crc_is_rejected(image_factory):
    path = image_factory()
    content = bytearray(path.read_bytes())
    payload_start = content.index(b"IDAT") + 4
    content[payload_start] ^= 1
    path.write_bytes(content)
    with pytest.raises(ImageDecodeError):
        decode(path)


def test_animated_png_is_rejected(tmp_path):
    path = tmp_path / "animated.png"
    with Image.new("RGB", (8, 6), "red") as first:
        with Image.new("RGB", (8, 6), "blue") as second:
            first.save(path, format="PNG", save_all=True, append_images=[second], duration=50, loop=0)
    with pytest.raises(ImageFormatError, match="动画或多帧"):
        decode(path)


def test_exif_orientation_changes_pixels_and_reported_coordinate_dimensions(tmp_path):
    original = np.array([
        [[255, 0, 0], [0, 255, 0]],
        [[0, 0, 255], [255, 255, 0]],
        [[0, 255, 255], [255, 0, 255]],
    ], dtype=np.uint8)
    path = tmp_path / "oriented.png"
    exif = Image.Exif()
    exif[274] = 6
    with Image.fromarray(original) as source:
        source.save(path, exif=exif)
    result = decode(path)
    assert (result.width, result.height) == (3, 2)
    np.testing.assert_array_equal(result.rgb, np.rot90(original, k=3))


def test_jpeg_exif_orientation_is_also_applied(image_factory):
    exif = Image.Exif()
    exif[274] = 8
    result = decode(image_factory(size=(8, 6), image_format="JPEG", exif=exif))
    assert (result.width, result.height) == (6, 8)


@pytest.mark.parametrize("mode,color,expected", [
    ("RGBA", (255, 0, 0, 0), [255, 255, 255]),
    ("RGBA", (255, 0, 0, 128), [255, 127, 127]),
    ("LA", (0, 128), [127, 127, 127]),
    ("L", 42, [42, 42, 42]),
])
def test_alpha_is_composited_on_white_and_grayscale_becomes_rgb(image_factory, mode, color, expected):
    assert decode(image_factory(mode=mode, color=color)).rgb[0, 0].tolist() == expected


def test_palette_png_transparency_is_composited_on_white(tmp_path):
    path = tmp_path / "palette.png"
    with Image.new("P", (2, 1)) as source:
        source.putpalette([255, 0, 0, 0, 0, 255] + [0] * (768 - 6))
        source.putdata([0, 1])
        source.save(path, transparency=1)
    assert decode(path).rgb.tolist() == [[[255, 0, 0], [255, 255, 255]]]


def test_rgb_png_transparent_color_is_composited_on_white(image_factory):
    path = image_factory(color=(0, 128, 255), transparency=(0, 128, 255))
    assert decode(path).rgb[0, 0].tolist() == [255, 255, 255]


def test_cmyk_jpeg_is_converted_to_rgb(image_factory):
    result = decode(image_factory(mode="CMYK", color=(0, 255, 255, 0), image_format="JPEG"))
    assert result.rgb[0, 0].tolist() == [255, 0, 0]


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
@pytest.mark.parametrize("parameter", ["max_file_bytes", "max_pixels"])
def test_limits_must_be_positive_integers(tmp_path, limit, parameter):
    with pytest.raises(ValueError, match="正整数"):
        decode(tmp_path / "unused", **{parameter: limit})


def test_importing_utility_does_not_import_pillow_numpy_or_create_files(tmp_path):
    project_root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ, PYTHONPATH=str(project_root), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import backend.utils.image.image_utils; "
         "assert 'PIL' not in sys.modules; assert 'numpy' not in sys.modules"],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []
