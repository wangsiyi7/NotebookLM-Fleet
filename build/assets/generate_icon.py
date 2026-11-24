"""Generate high-DPI Windows .ico and .png brand icons for NotebookLM-Fleet."""

from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw


def build_icon_image(size: int = 256) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    margin = max(2, round(size * 0.05))
    radius = max(4, round(size * 0.22))
    # Deep teal gradient-look base card
    draw.rounded_rectangle(
        [margin, margin, size - margin - 1, size - margin - 1],
        radius=radius,
        fill=(19, 111, 99, 255),
        outline=(11, 79, 70, 255),
        width=max(1, round(size * 0.02)),
    )

    # Warm cream inner notebook sheet
    sheet_l = round(size * 0.23)
    sheet_t = round(size * 0.19)
    sheet_r = round(size * 0.77)
    sheet_b = round(size * 0.81)
    draw.rounded_rectangle(
        [sheet_l, sheet_t, sheet_r, sheet_b],
        radius=max(2, round(size * 0.07)),
        fill=(246, 244, 238, 255),
    )

    # Three gold/teal notebook rings & analytics bars
    bar_x0 = round(size * 0.32)
    bar_h = max(2, round(size * 0.075))
    bars = [
        (0.32, 0.68, (19, 111, 99, 255)),
        (0.47, 0.61, (196, 125, 43, 255)),
        (0.62, 0.70, (38, 84, 124, 255)),
    ]
    for y_frac, x1_frac, color in bars:
        y0 = round(size * y_frac)
        x1 = round(size * x1_frac)
        draw.rounded_rectangle(
            [bar_x0, y0, x1, y0 + bar_h],
            radius=max(1, bar_h // 2),
            fill=color,
        )

    return img


def main() -> None:
    out_dir = Path(__file__).resolve().parent
    out_dir.mkdir(parents=True, exist_ok=True)
    master = build_icon_image(256)
    master.save(out_dir / "app.png", format="PNG")
    master.save(
        out_dir / "app.ico",
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print("Generated:", out_dir / "app.ico", out_dir / "app.png")


if __name__ == "__main__":
    main()
