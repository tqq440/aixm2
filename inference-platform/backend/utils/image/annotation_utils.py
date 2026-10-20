"""在已归正的 RGB 图片上绘制原图坐标框，不执行文件持久化。"""

from io import BytesIO


def annotated_png(rgb, annotations: list[tuple[str, float, tuple[float, float, float, float]]]) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    with Image.fromarray(rgb) as image:
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default(size=max(12, min(28, round(min(image.size) / 45))))
        line_width = max(2, round(min(image.size) / 250))
        for name, confidence, box in annotations:
            # 连续坐标允许等于 width/height，绘图坐标裁剪到最后一个实际像素。
            left, top, right, bottom = box
            rectangle = (min(left, image.width - 1), min(top, image.height - 1),
                         min(right, image.width - 1), min(bottom, image.height - 1))
            draw.rectangle(rectangle, outline="#00e6a8", width=line_width)
            label = f"{name} {confidence:.2f}"
            bounds = draw.textbbox((0, 0), label, font=font)
            label_width, label_height = bounds[2] - bounds[0] + 8, bounds[3] - bounds[1] + 8
            label_x = max(0, min(left, image.width - label_width))
            label_y = max(0, top - label_height)
            draw.rectangle((label_x, label_y, label_x + label_width, label_y + label_height), fill="#00e6a8")
            draw.text((label_x + 4, label_y + 4 - bounds[1]), label, font=font, fill="#003529")
        with BytesIO() as output:
            image.save(output, format="PNG")
            return output.getvalue()
