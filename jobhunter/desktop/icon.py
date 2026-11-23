"""The tray / app icon ("Crosshair": a white scope crosshair on an orange tile), drawn in code so the build needs no image files.
`python -m jobhunter.desktop.icon out_dir` writes jobhunter.ico and jobhunter.png (and the .icns source set) for the
installers. States: ok (orange), attention (orange + red dot: a sign-in needs you), paused (grey)."""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

# tile gradient (top, bottom) per state
TILES = {"ok": ((255, 138, 31), (240, 82, 29)), "attention": ((255, 138, 31), (240, 82, 29)),
         "paused": ((154, 154, 146), (124, 124, 116))}
WHITE, RED = (255, 255, 255, 255), (229, 50, 42, 255)
SS = 4  # draw at 4x and downscale, so small sizes stay smooth


def _tile(px: int, top: tuple, bottom: tuple) -> Image.Image:
    grad = Image.new("RGBA", (1, px))
    for y in range(px):
        t = y / max(1, px - 1)
        grad.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
    return grad.resize((px, px))


def image(size: int = 64, state: str = "ok", pad: float = 0.0) -> Image.Image:
    """The icon at `size` px. `pad` is the empty margin as a fraction of size (macOS app icons use ~0.1)."""
    top, bottom = TILES.get(state, TILES["ok"])
    px = size * SS
    inner = round(px * (1 - 2 * pad))
    u = inner / 64  # design grid is 64 x 64

    def b(x0, y0, x1, y1):
        return [x0 * u, y0 * u, x1 * u, y1 * u]

    mask = Image.new("L", (inner, inner), 0)
    ImageDraw.Draw(mask).rounded_rectangle(b(1, 1, 63, 63), radius=14 * u, fill=255)
    art = Image.new("RGBA", (inner, inner), (0, 0, 0, 0))
    art.paste(_tile(inner, top, bottom), (0, 0), mask)

    d = ImageDraw.Draw(art)
    small = size <= 24  # thicker strokes so the crosshair survives tray size
    w = round((6 if small else 4.5) * u)
    r = 19 * u
    d.ellipse([32 * u - r, 32 * u - r, 32 * u + r, 32 * u + r], outline=WHITE, width=w)
    for x0, y0, x1, y1 in ((32, 7, 32, 23), (32, 41, 32, 57), (7, 32, 23, 32), (41, 32, 57, 32)):
        d.line(b(x0, y0, x1, y1), fill=WHITE, width=w)
        for x, y in ((x0, y0), (x1, y1)):  # round caps
            d.ellipse([x * u - w / 2, y * u - w / 2, x * u + w / 2, y * u + w / 2], fill=WHITE)
    c = (5 if small else 4) * u
    d.ellipse([32 * u - c, 32 * u - c, 32 * u + c, 32 * u + c], fill=WHITE)
    if state == "attention":
        r = 10 * u
        d.ellipse([52 * u - r, 12 * u - r, 52 * u + r, 12 * u + r], fill=RED, outline=WHITE, width=round(3 * u))

    canvas = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    off = (px - inner) // 2
    canvas.alpha_composite(art, (off, off))
    return canvas.resize((size, size), Image.LANCZOS)


def write_assets(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    image(1024).save(out / "jobhunter.png")
    # build each .ico frame at its own size so 16/24/32 use the simplified tray drawing
    sizes = [16, 24, 32, 48, 64, 128, 256]
    frames = [image(s) for s in sizes]
    frames[-1].save(out / "jobhunter.ico", sizes=[(s, s) for s in sizes], append_images=frames[:-1])
    iconset = out / "jobhunter.iconset"
    iconset.mkdir(exist_ok=True)
    for s in (16, 32, 128, 256, 512):  # macOS app icons sit inside a margin, like the system ones
        image(s, pad=0.1 if s >= 128 else 0).save(iconset / f"icon_{s}x{s}.png")
        image(s * 2, pad=0.1 if s >= 128 else 0).save(iconset / f"icon_{s}x{s}@2x.png")


if __name__ == "__main__":
    write_assets(Path(sys.argv[1] if len(sys.argv) > 1 else "build/assets"))
