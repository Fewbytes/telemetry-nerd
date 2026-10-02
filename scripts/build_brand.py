# /// script
# requires-python = ">=3.12"
# dependencies = ["resvg-py>=0.2", "pillow>=11"]
# ///
"""Render brand PNGs and favicon.ico from the SVG sources in assets/brand, and sync web icons into ui/public."""

import argparse
import io
import shutil
from pathlib import Path

import resvg_py
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
BRAND = ROOT / "assets" / "brand"
UI_PUBLIC = ROOT / "ui" / "public"

# (source svg, output png, pixel size). favicon.svg is drawn for 16/32 px;
# icon.svg carries the full detail for larger sizes.
PNGS = [
    ("favicon.svg", "favicon-16.png", 16),
    ("favicon.svg", "favicon-32.png", 32),
    ("icon.svg", "apple-touch-icon.png", 180),
    ("icon.svg", "icon-512.png", 512),
    ("logo.svg", "logo-512.png", 512),
]
ICO_SIZES = [16, 32, 48]


def render(src: Path, width: int, height: int | None = None) -> bytes:
    return bytes(
        resvg_py.svg_to_bytes(
            svg_path=str(src),
            width=width,
            height=height or width,
            resources_dir=str(src.parent),
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=BRAND / "dist")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    for src, name, size in PNGS:
        (args.out / name).write_bytes(render(BRAND / src, size))

    frames = [Image.open(io.BytesIO(render(BRAND / "favicon.svg", s))) for s in ICO_SIZES]
    frames[-1].save(
        args.out / "favicon.ico", sizes=[(s, s) for s in ICO_SIZES], append_images=frames[:-1]
    )

    (args.out / "og.png").write_bytes(render(BRAND / "og.svg", 1200, 630))

    # Vite copies ui/public to the dist root, which the daemon serves at /.
    UI_PUBLIC.mkdir(exist_ok=True)
    for src in [
        BRAND / "favicon.svg",
        BRAND / "icon.svg",
        args.out / "favicon.ico",
        args.out / "apple-touch-icon.png",
    ]:
        shutil.copyfile(src, UI_PUBLIC / src.name)

    for path in sorted([*args.out.iterdir(), *UI_PUBLIC.iterdir()]):
        print(path.relative_to(ROOT))


if __name__ == "__main__":
    main()
