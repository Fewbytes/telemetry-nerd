# /// script
# requires-python = ">=3.12"
# dependencies = ["resvg-py>=0.2", "pillow>=11"]
# ///
"""Render brand PNGs and favicon.ico from the SVG sources in assets/brand."""

import argparse
import io
from pathlib import Path

import resvg_py
from PIL import Image

BRAND = Path(__file__).resolve().parent.parent / "assets" / "brand"

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

    for path in sorted(args.out.iterdir()):
        print(path.relative_to(BRAND.parent.parent))


if __name__ == "__main__":
    main()
