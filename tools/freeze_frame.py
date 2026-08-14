"""Write the frame an animated GIF is showing at a given moment out as a PNG.

The home page plays ``sc_loading_animation.gif`` for a moment and then holds
still. A GIF cannot be stopped from script, and capturing the live frame with
``canvas.drawImage`` does not work either -- browsers hand back the GIF's *first*
frame, not the one on screen -- so the held frame has to be a separate still
image that the page swaps in on a timer.

This script produces that still. Re-run it whenever the GIF is replaced or the
hold time in ``static/js/home.js`` changes, and keep the two in step: the swap is
only seamless while the PNG shows what the animation had reached at ``--at``.

Pillow is needed to read the frames and is not a GREMLIN runtime dependency:
install it just for this (``pip install pillow``).

Usage::

    python tools/freeze_frame.py                       # 1600 ms of the home page GIF
    python tools/freeze_frame.py --at 2000
    python tools/freeze_frame.py --gif in.gif --out still.png --at 500
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GIF = REPO_ROOT / "static" / "img" / "sc_loading_animation.gif"
DEFAULT_OUT = REPO_ROOT / "static" / "img" / "sc_loading_animation_still.png"
DEFAULT_AT_MS = 1600


def frame_at(gif_path: Path, at_ms: int) -> Image.Image:
    """Return the frame on screen ``at_ms`` into the first play of the GIF.

    Frames are found by walking the per-frame delays the same way a browser
    does. Past the end of a single loop the last frame is returned rather than
    wrapping, so an over-long hold time fails visibly (a held end frame) instead
    of quietly picking a frame from the second time round.
    """

    with Image.open(gif_path) as gif:
        elapsed = 0
        last = None
        for index in range(getattr(gif, "n_frames", 1)):
            gif.seek(index)
            last = gif.convert("RGBA")
            elapsed += int(gif.info.get("duration", 0))
            if elapsed > at_ms:
                return last
        if last is None:
            raise SystemExit(f"{gif_path} has no frames.")
        return last


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gif", type=Path, default=DEFAULT_GIF, help="Animated GIF to read.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="PNG to write.")
    parser.add_argument(
        "--at",
        type=int,
        default=DEFAULT_AT_MS,
        dest="at_ms",
        help="Milliseconds into the animation to capture (default: %(default)s).",
    )
    args = parser.parse_args()

    frame = frame_at(args.gif, args.at_ms)
    frame.save(args.out, format="PNG", optimize=True)
    print(f"Wrote {args.out} ({args.at_ms} ms into {args.gif.name}, {frame.width}x{frame.height})")


if __name__ == "__main__":
    main()
