"""Render a scrubbed hardware transcript as an animated terminal SVG.

    python tools/render_demo.py tests/fixtures/hw-switch-2950-recover-no-restore.ytx.pub \
        docs/demo/2950-recovery.svg --title "..."

The README's demo is *replayed*, not staged: every character comes from a real
device session committed as a ``hardware:`` fixture, played back with the long
silences (a boot, a flash_init) compressed so it watches in under a minute.
GitHub renders an animated SVG inline where it would not play a cast file, and
an SVG diff is reviewable where a GIF is not.

Only a ``.ytx.pub`` is accepted: a demo is published by definition, and the
schema refuses to load a raw recording as public.
"""

from __future__ import annotations

import argparse
import re
import sys
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ciscoyoke.transcript.schema import Direction, read

COLUMNS = 92
ROWS = 24
LINE = 17.0
CHAR = 8.4
PAD = 16.0
BAR = 30.0

# Long silences -- a boot, a flash_init -- are compressed to this, and anything
# shorter is sped up by SPEED, so a four-minute session watches in well under one.
MAX_GAP = 0.9
SPEED = 2.5

_LINE_BREAK = re.compile(r"\r\n|\n\r|\n|\r")


def _lines(path: Path) -> list[tuple[float, str]]:
    """(time the line was complete, text), from the device's side of the session."""
    transcript = read(path, require_public=True)
    lines: list[tuple[float, str]] = []
    current = ""
    clock = 0.0
    previous_t = 0.0
    for record in transcript:
        gap = max(record.t - previous_t, 0.0)
        previous_t = record.t
        clock += min(gap, MAX_GAP * SPEED) / SPEED
        if record.direction is not Direction.RX:
            continue
        parts = _LINE_BREAK.split(record.data.decode("latin-1"))
        current += parts[0]
        for part in parts[1:]:
            lines.append((clock, current))
            current = part
    if current:
        lines.append((clock, current))

    tidy: list[tuple[float, str]] = []
    for at, text in lines:
        text = "".join(ch for ch in text if ch.isprintable()).rstrip()
        if text.count("#") > 40:  # the image-load progress bar
            text = text[: text.index("#")] + "#" * 40 + " ..."
        if not text and tidy and not tidy[-1][1]:
            continue  # collapse runs of blank lines
        tidy.append((at, text[:COLUMNS]))
    return tidy


def render(path: Path, title: str) -> str:
    lines = _lines(path)
    duration = (lines[-1][0] if lines else 0.0) + 3.0
    width = COLUMNS * CHAR + 2 * PAD
    height = ROWS * LINE + 2 * PAD + BAR

    texts: list[str] = []
    scroll: list[tuple[float, float]] = [(0.0, 0.0)]
    for index, (at, text) in enumerate(lines):
        y = BAR + PAD + (index + 1) * LINE
        shade = "p" if text.rstrip().endswith(("#", ">", "switch:", "]:", "?")) else "t"
        texts.append(
            f'<text class="{shade}" x="{PAD}" y="{y:.1f}" '
            f'style="animation-delay:{at:.2f}s">{escape(text) or " "}</text>'
        )
        if index >= ROWS:
            scroll.append((at, -(index - ROWS + 1) * LINE))

    frames = []
    for at, offset in scroll:
        percent = 100.0 * at / duration
        frames.append(f"{percent:.3f}%{{transform:translateY({offset:.1f}px)}}")
    frames.append(f"100%{{transform:translateY({scroll[-1][1]:.1f}px)}}")

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" viewBox="0 0 {width:.0f} {height:.0f}" role="img" aria-label="{escape(title)}">
<title>{escape(title)}</title>
<style>
text{{font:13px ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;white-space:pre;opacity:0;animation:on .01s linear both}}
.t{{fill:#d4d4d4}} .p{{fill:#7ee787}}
.bar{{font:12px -apple-system,"Segoe UI",Helvetica,Arial,sans-serif;fill:#8b949e;opacity:1;animation:none}}
#s{{animation:scroll {duration:.2f}s steps(1,end) both}}
@keyframes on{{to{{opacity:1}}}}
@keyframes scroll{{{" ".join(frames)}}}
</style>
<rect width="100%" height="100%" rx="8" fill="#0d1117"/>
<rect width="100%" height="{BAR}" rx="8" fill="#161b22"/>
<circle cx="18" cy="15" r="5" fill="#ff5f56"/><circle cx="34" cy="15" r="5" fill="#ffbd2e"/><circle cx="50" cy="15" r="5" fill="#27c93f"/>
<text class="bar" x="66" y="19">{escape(title)}</text>
<clipPath id="c"><rect x="0" y="{BAR}" width="{width:.0f}" height="{height - BAR:.0f}"/></clipPath>
<g clip-path="url(#c)"><g id="s">
{chr(10).join(texts)}
</g></g>
</svg>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("transcript", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--title", required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(args.transcript, args.title), encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
