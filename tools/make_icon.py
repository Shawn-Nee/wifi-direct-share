"""生成应用图标 assets/icon.ico（渐变底 + 双向箭头）。"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "assets", "icon.ico")


def diagonal_gradient(size: int, c1: tuple, c2: tuple) -> Image.Image:
    small = Image.new("RGB", (2, 2))
    small.putpixel((0, 0), c1)
    small.putpixel((1, 1), c2)
    small.putpixel((1, 0), tuple((a + b) // 2 for a, b in zip(c1, c2)))
    small.putpixel((0, 1), tuple((a + b) // 2 for a, b in zip(c1, c2)))
    return small.resize((size, size), Image.BICUBIC)


def build(size: int = 1024) -> Image.Image:
    c1 = (56, 225, 196)     # 青
    c2 = (108, 140, 255)    # 紫蓝
    grad = diagonal_gradient(size, c1, c2).convert("RGBA")

    # 圆角遮罩
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    radius = int(size * 0.235)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)

    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    icon.paste(grad, (0, 0), mask)

    # 内层高光（顶部柔光，越靠下越透）
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for i in range(28):
        alpha = int(34 * (1 - i / 28) ** 2)
        y0 = -size * 0.42 + i * size * 0.018
        gd.ellipse([-size * 0.30, y0, size * 1.05, y0 + size * 0.62],
                   fill=(255, 255, 255, alpha))
    icon = Image.alpha_composite(icon, Image.composite(
        glow, Image.new("RGBA", (size, size), (0, 0, 0, 0)), mask))

    # 双向箭头
    s = size / 1024.0
    lay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lay)
    white = (255, 255, 255, 255)

    # 上箭头：向右
    ld.rectangle([250 * s, 330 * s, 640 * s, 418 * s], fill=white)
    ld.polygon([(610 * s, 268 * s), (806 * s, 374 * s), (610 * s, 480 * s)], fill=white)
    # 下箭头：向左
    ld.rectangle([384 * s, 606 * s, 774 * s, 694 * s], fill=white)
    ld.polygon([(414 * s, 544 * s), (218 * s, 650 * s), (414 * s, 756 * s)], fill=white)

    icon = Image.alpha_composite(icon, lay)

    # 轻微降噪：整体做一次锐化边缘处理不需要，直接输出
    return icon


def main() -> None:
    big = build(1024)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    big.save(OUT, format="ICO", sizes=sizes)
    big.resize((256, 256), Image.LANCZOS).save(
        os.path.join(os.path.dirname(OUT), "icon.png"), format="PNG")
    print("written:", OUT)


if __name__ == "__main__":
    main()
