import json
import math
import random
import sys
import zlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[2]
CONFIG = json.loads(Path(__file__).with_name("art.json").read_text(encoding="utf-8"))
OUT = ROOT / "assets" / "readme"
P = CONFIG["palette"]
RED, INK, CREAM, LIME, PAPER, SHADE = (
    P["red"],
    P["ink"],
    P["cream"],
    P["lime"],
    P["paper"],
    P["shade"],
)
FILLS = {"red": RED, "ink": INK, "cream": CREAM, "lime": LIME, "paper": PAPER}
DISPLAY = "'Arial Black','Arial Bold',Gadget,Impact,'Helvetica Neue',Arial,sans-serif"
BODY = "'Helvetica Neue',Helvetica,Arial,'Liberation Sans',sans-serif"
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono','DejaVu Sans Mono',monospace"

CSS = """
.in,.pop,.slide,.wob{transform-box:fill-box;transform-origin:center}
.in{animation:in .75s cubic-bezier(.2,1.45,.35,1) both}
@keyframes in{from{transform:translateY(-36px) rotate(-7deg)}}
.pop{animation:pop .6s cubic-bezier(.2,1.6,.4,1) both}
@keyframes pop{from{transform:scale(.82) rotate(-5deg)}}
.slide{animation:slide .7s cubic-bezier(.2,1.2,.4,1) both}
@keyframes slide{from{transform:translateX(-36px)}}
.wob{animation:wob 3.4s ease-in-out infinite alternate}
@keyframes wob{from{transform:rotate(-3deg)}to{transform:rotate(3deg)}}
.pulse{transform-box:fill-box;transform-origin:50% 100%;animation:pulse 1.3s ease-in-out infinite alternate}
@keyframes pulse{from{transform:scaleY(.45)}}
.marquee{animation:marquee 46s linear infinite}
@keyframes marquee{to{transform:translateX(var(--shift))}}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}
"""

LETTERS = {
    "F": "M0 0H100V32H36V56H86V88H36V140H0Z",
    "A": "M30 0H70L100 140H64L59 114H41L36 140H0ZM44 86H56L50 50Z",
    "K": "M0 0H36V54L64 0H101L65 66L101 140H61L40 94L36 100V140H0Z",
    "E": "M0 0H100V30H36V55H88V85H36V110H100V140H0Z",
    "Y": "M0 0H39L50 46L61 0H100L68 82V140H32V82Z",
    "O": (
        "M50 0C86 0 100 26 100 70C100 114 86 140 50 140C14 140 0 114 0 70C0 26 14 0 50 0Z"
        "M50 34C40 34 36 46 36 70C36 94 40 106 50 106C60 106 64 94 64 70C64 46 60 34 50 34Z"
    ),
    "U": "M0 0H36V90C36 101 41 107 50 107C59 107 64 101 64 90V0H100V94C100 124 82 140 50 140C18 140 0 124 0 94Z",
}

Box = tuple[float, float, float, float]
Point = tuple[float, float]


def esc(value: str) -> str:
    return escape(value, {'"': "&quot;"})


def attrs(**values: Any) -> str:
    return "".join(
        f' {name.rstrip("_").replace("_", "-")}="{esc(str(value))}"'
        for name, value in values.items()
        if value is not None
    )


def svg(name: str, size: tuple[int, int], label: str, body: str, defs: str = "") -> None:
    width, height = size
    title = label.split(". ", maxsplit=1)[0]
    document = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" aria-labelledby="t d">'
        f'<title id="t">{esc(title)}</title><desc id="d">{esc(label)}</desc>'
        f"<style>{CSS}</style><defs>{defs}"
        f'<clipPath id="frame"><rect width="{width}" height="{height}" rx="{min(26, height // 4)}"/></clipPath>'
        f'</defs><g clip-path="url(#frame)">{body}</g></svg>\n'
    )
    (OUT / f"{name}.svg").write_text(document, encoding="utf-8")


def text(x: float, y: float, value: str, **style: Any) -> str:
    size = style.pop("size", 24)
    font = style.pop("font", DISPLAY)
    weight = style.pop("weight", 900)
    return (
        f"<text{attrs(x=f'{x:g}', y=f'{y:g}', font_family=font, font_size=size, font_weight=weight, **style)}>"
        f"{esc(value)}</text>"
    )


def lines(x: float, y: float, rows: Sequence[str], gap: float, **style: Any) -> str:
    block = f"b{zlib.crc32(repr((x, y, tuple(rows))).encode()):x}"
    return "".join(
        text(x, y + index * gap, row, data_lines=block, **dict(style))
        for index, row in enumerate(rows)
    )


def group(body: str, **values: Any) -> str:
    return f"<g{attrs(**values)}>{body}</g>"


def animated(body: str, kind: str, delay: float, transform: str | None = None) -> str:
    inner = group(body, class_=kind, style=f"animation-delay:{delay:.2f}s")
    return group(inner, transform=transform) if transform else inner


def rect(x: float, y: float, w: float, h: float, **style: Any) -> str:
    return f"<rect{attrs(x=f'{x:g}', y=f'{y:g}', width=f'{w:g}', height=f'{h:g}', **style)}/>"


def panel(box: Box, fill: str, **style: Any) -> str:
    x, y, w, h = box
    offset = style.pop("offset", 12)
    shadow = style.pop("shadow", INK)
    radius = style.pop("rx", 6)
    width = style.pop("stroke_width", 5)
    return rect(x + offset, y + offset, w, h, fill=shadow, rx=radius) + rect(
        x, y, w, h, fill=fill, stroke=INK, stroke_width=width, rx=radius, **style
    )


def halftone(box: Box, step: float, color: str, **shape: Any) -> str:
    x, y, w, h = box
    smallest, largest = shape.get("radii", (0.6, step * 0.42))
    axis = shape.get("axis", "x")
    dots = []
    for row in range(int(h // step) + 1):
        for column in range(int(w // step) + 1):
            cx = x + column * step + (step / 2 if row % 2 else 0)
            cy = y + row * step
            share = {
                "x": (cx - x) / w,
                "-x": 1 - (cx - x) / w,
                "y": (cy - y) / h,
                "-y": 1 - (cy - y) / h,
            }[axis]
            radius = smallest + (largest - smallest) * max(0.0, min(1.0, share))
            if cx <= x + w and radius > 0.4:
                dots.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{radius:.2f}"/>')
    return group("".join(dots), fill=color, opacity=shape.get("opacity", 1))


def starburst(center: Point, radii: tuple[float, float], spikes: int, **style: Any) -> str:
    cx, cy = center
    outer, inner = radii
    points = []
    for index in range(spikes * 2):
        radius = outer if index % 2 == 0 else inner
        angle = math.radians(-90 + index * 180 / spikes)
        points.append(f"{cx + radius * math.cos(angle):.1f},{cy + radius * math.sin(angle):.1f}")
    return f'<polygon points="{" ".join(points)}"{attrs(**style)}/>'


def sticker(center: Point, rows: Sequence[str], **style: Any) -> str:
    cx, cy = center
    radius = style.pop("radius", 66)
    fill = style.pop("fill", LIME)
    ink = style.pop("ink", INK)
    gap = style.pop("gap", 26)
    size = style.pop("size", 21)
    top = cy - (len(rows) - 1) * gap / 2 + size * 0.36
    burst = starburst((cx, cy), (radius, radius * 0.76), 14, fill=fill, stroke=INK, stroke_width=5)
    return animated(
        group(
            burst + lines(cx, top, rows, gap, size=size, fill=ink, text_anchor="middle"),
            class_="wob",
        ),
        "pop",
        style.pop("delay", 1.2),
    )


def letter(
    glyph: str, place: tuple[float, float, float, float], colors: tuple[str, str], delay: float
) -> str:
    x, y, scale, tilt = place
    fill, shadow = colors
    shape = LETTERS[glyph]
    body = (
        f'<path d="{shape}" transform="translate(9 9)" fill="{shadow}" fill-rule="evenodd"/>'
        f'<path d="{shape}" fill="{fill}" fill-rule="evenodd" stroke="{INK}" stroke-width="6" stroke-linejoin="round"/>'
        f'<path d="{shape}" fill="url(#ink-dots)" fill-rule="evenodd" opacity=".55"/>'
    )
    return animated(
        body, "in", delay, f"translate({x:g} {y:g}) rotate({tilt:.2f}) scale({scale:g})"
    )


def wordmark(word: str, origin: Point, scale: float, colors: tuple[str, str], delay: float) -> str:
    rng = random.Random(word)
    x, y = origin
    out = []
    for index, glyph in enumerate(word):
        out.append(
            letter(
                glyph,
                (x, y + rng.uniform(-6, 6), scale, rng.uniform(-4, 4)),
                colors,
                delay + index * 0.08,
            )
        )
        x += 112 * scale
    return "".join(out)


def stamp(number: str, title: str, origin: Point, colors: tuple[str, str, str]) -> str:
    x, y = origin
    fill, tag, title_fill = colors
    width = max(86, len(number) * 30 + 26)
    body = rect(x, y, width, 62, fill=tag, rx=4) + text(
        x + width / 2, y + 45, number, size=38, fill=fill, text_anchor="middle"
    )
    body += text(x + width + 22, y + 48, title, size=44, fill=title_fill, letter_spacing="-1")
    return animated(body, "slide", 0)


def dot_defs() -> str:
    return (
        '<pattern id="ink-dots" width="9" height="9" patternUnits="userSpaceOnUse" patternTransform="rotate(28)">'
        f'<circle cx="4.5" cy="4.5" r="1.6" fill="{PAPER}"/></pattern>'
        '<pattern id="red-dots" width="14" height="14" patternUnits="userSpaceOnUse" patternTransform="rotate(18)">'
        f'<circle cx="7" cy="7" r="2.4" fill="{INK}" fill-opacity=".16"/></pattern>'
        '<pattern id="cream-dots" width="16" height="16" patternUnits="userSpaceOnUse">'
        f'<circle cx="4" cy="4" r="1.8" fill="{INK}" fill-opacity=".08"/></pattern>'
        + "".join(
            f'<marker id="head-{name}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">'
            f'<path d="M0 0L10 5L0 10z" fill="{color}"/></marker>'
            for name, color in (("ink", INK), ("paper", PAPER), ("lime", LIME))
        )
    )


def face(center: Point, radius: float, fill: str) -> str:
    cx, cy = center
    return (
        f'<circle cx="{cx}" cy="{cy}" r="{radius}" fill="{fill}" stroke="{INK}" stroke-width="4"/>'
        f'<path d="M{cx - radius * 1.5} {cy + radius * 2.3}C{cx - radius * 1.4} {cy + radius * 1.1} '
        f'{cx + radius * 1.4} {cy + radius * 1.1} {cx + radius * 1.5} {cy + radius * 2.3}Z" fill="{fill}" stroke="{INK}" stroke-width="4"/>'
    )


def mask(center: Point, size: float) -> str:
    cx, cy = center
    s = size
    return (
        f'<path d="M{cx - s} {cy - s * 0.35}C{cx - s * 0.55} {cy - s * 0.75} {cx - s * 0.15} {cy - s * 0.45} {cx} {cy - s * 0.3}'
        f"C{cx + s * 0.15} {cy - s * 0.45} {cx + s * 0.55} {cy - s * 0.75} {cx + s} {cy - s * 0.35}"
        f"C{cx + s * 1.05} {cy + s * 0.35} {cx + s * 0.45} {cy + s * 0.6} {cx} {cy + s * 0.2}"
        f'C{cx - s * 0.45} {cy + s * 0.6} {cx - s * 1.05} {cy + s * 0.35} {cx - s} {cy - s * 0.35}Z" '
        f'fill="{INK}" stroke="{INK}" stroke-width="4" stroke-linejoin="round"/>'
        f'<ellipse cx="{cx - s * 0.42}" cy="{cy - s * 0.05}" rx="{s * 0.22}" ry="{s * 0.14}" fill="{LIME}"/>'
        f'<ellipse cx="{cx + s * 0.42}" cy="{cy - s * 0.05}" rx="{s * 0.22}" ry="{s * 0.14}" fill="{LIME}"/>'
    )


def bars(origin: Point, heights: Sequence[float], color: str) -> str:
    x, y = origin
    return "".join(
        group(
            rect(x + index * 14, y - height, 8, height, rx=4, fill=color),
            class_="pulse",
            style=f"animation-delay:{-index * 0.17:.2f}s",
        )
        for index, height in enumerate(heights)
    )


def meeting_window(origin: Point) -> str:
    copy = CONFIG["hero"]
    body = panel((0, 0, 470, 330), PAPER, offset=16)
    body += rect(0, 0, 470, 52, fill=INK, rx=6)
    body += "".join(
        f'<circle cx="{26 + i * 24}" cy="26" r="7" fill="{c}"/>'
        for i, c in enumerate((RED, LIME, PAPER))
    )
    body += text(104, 33, "MEET · LIVE", size=17, font=MONO, weight=700, fill=LIME)
    for (tx, ty), name in zip(((18, 70), (242, 70), (18, 196)), copy["participants"], strict=True):
        body += rect(tx, ty, 210, 112, fill=SHADE, stroke=INK, stroke_width=3, rx=6)
        body += face((tx + 52, ty + 46), 18, RED if name == "BUDI" else CREAM)
        body += text(tx + 104, ty + 66, name, size=22, fill=INK, font=MONO, weight=800)
    body += rect(242, 196, 210, 112, fill=LIME, stroke=INK, stroke_width=4, rx=6)
    body += mask((298, 246), 34)
    body += text(342, 236, copy["twin"], size=19, fill=INK, font=MONO, weight=800)
    body += bars((344, 290), (14, 30, 20, 38, 24, 12, 28), INK)
    return animated(body, "pop", 0.55, f"translate({origin[0]} {origin[1]}) rotate(-2)")


def bubble(origin: Point, rows: Sequence[str]) -> str:
    path = "M0 18C0 8 8 0 18 0H150L198 -40L192 0H262C272 0 280 8 280 18V82C280 92 272 100 262 100H18C8 100 0 92 0 82Z"
    body = (
        f'<path d="{path}" transform="translate(8 8)" fill="{INK}"/>'
        f'<path id="bubble" d="{path}" fill="{PAPER}" stroke="{INK}" stroke-width="5" stroke-linejoin="round"/>'
        + lines(22, 42, rows, 32, size=22, font=BODY, weight=800, fill=INK, data_in="bubble")
    )
    return animated(body, "pop", 1.25, f"translate({origin[0]} {origin[1]})")


def memory_stack(origin: Point) -> str:
    body = ""
    for index, fill in enumerate((CREAM, LIME, PAPER)):
        body += group(
            panel((0, 0, 190, 120), fill, offset=8, stroke_width=4),
            transform=f"translate({index * 12} {index * -14}) rotate({-6 + index * 4})",
        )
    nodes = [(48, -2), (110, -30), (150, 18), (86, 40)]
    graph = "".join(
        f'<line x1="{a[0] + 24}" y1="{a[1] + 50}" x2="{b[0] + 24}" y2="{b[1] + 50}" stroke="{INK}" stroke-width="4"/>'
        for a, b in zip(nodes, nodes[1:] + nodes[:1], strict=True)
    ) + "".join(
        f'<circle cx="{nx + 24}" cy="{ny + 50}" r="11" fill="{RED}" stroke="{INK}" stroke-width="4"/>'
        for nx, ny in nodes
    )
    body += animated(graph, "pop", 1.05)
    label = rect(18, 104, 150, 40, fill=INK, rx=4) + text(
        93, 132, CONFIG["hero"]["memory"], size=22, fill=LIME, text_anchor="middle"
    )
    body += animated(label, "slide", 1.1)
    return group(body, transform=f"translate({origin[0]} {origin[1]})")


def ticker(words: Sequence[str], y: float, width: float) -> str:
    phrase = "   ✶   ".join(words) + "   ✶   "
    run = text(0, 20, phrase * 3, size=17, font=MONO, weight=800, fill=CREAM, letter_spacing="2")
    shift = -len(phrase) * 12.25
    return rect(0, y, width, 28, fill=INK) + group(
        group(run, class_="marquee", style=f"--shift:{shift:.0f}px"),
        transform=f"translate(0 {y})",
        data_check_skip="1",
    )


def hero() -> None:
    copy = CONFIG["hero"]
    width, height = 1200, 680
    slab = "700,0 1200,0 1200,680 620,680"
    body = rect(0, 0, width, height, fill=CREAM) + rect(
        0, 0, width, height, fill="url(#cream-dots)"
    )
    body += (
        f'<polygon points="{slab}" fill="{RED}"/><polygon points="{slab}" fill="url(#red-dots)"/>'
    )
    body += halftone((720, 20, 470, 300), 18, INK, axis="x", opacity=0.18)
    body += f'<polygon points="680,0 716,0 636,680 600,680" fill="{INK}"/>'
    body += animated(
        rect(56, 34, 500, 46, fill=INK, rx=4)
        + text(76, 65, copy["eyebrow"], size=19, font=MONO, weight=800, fill=LIME),
        "slide",
        0,
    )
    body += wordmark("FAKE", (60, 108), 1.12, (INK, RED), 0.1)
    body += wordmark("YOU", (60, 290), 1.12, (RED, INK), 0.45)
    copyblock = lines(58, 504, copy["headline"], 42, size=36, fill=INK, letter_spacing="-1")
    copyblock += lines(60, 590, copy["lines"], 30, size=22, font=BODY, weight=700, fill=INK)
    body += animated(copyblock, "slide", 0.9)
    body += meeting_window((700, 96))
    body += memory_stack((650, 488))
    body += bubble((880, 476), copy["bubble"])
    body += sticker((1104, 84), copy["sticker"], radius=92, size=20, gap=27, delay=1.4)
    star = rect(0, 0, 316, 50, fill=INK, rx=6) + rect(
        5, 5, 306, 40, fill="none", stroke=LIME, stroke_width=2, stroke_dasharray="6 6", rx=4
    )
    star += text(20, 33, copy["star"][0], size=20, fill=LIME) + text(
        110, 32, copy["star"][1], size=17, font=MONO, weight=700, fill=PAPER
    )
    body += animated(star, "pop", 1.6, "translate(866 594)")
    body += ticker(copy["ticker"], 652, width)
    svg(
        "hero",
        (width, height),
        "Fakeyou. An open-source, self-hosted AI twin for Google Meet. It listens, speaks when it is addressed, "
        "and remembers what your team decided. Illustration: a Google Meet window where the Fakeyou twin, drawn as a mask, "
        "speaks next to three participants, beside a stack of memory cards linked as a graph.",
        body,
        dot_defs(),
    )


def why_art(kind: str, center: Point) -> str:
    cx, cy = center
    if kind == "decided":
        return (
            f'<circle cx="{cx}" cy="{cy}" r="72" fill="{LIME}" stroke="{INK}" stroke-width="6"/>'
            f'<path d="M{cx - 38} {cy}L{cx - 8} {cy + 30}L{cx + 42} {cy - 32}" fill="none" stroke="{INK}" '
            'stroke-width="16" stroke-linecap="round" stroke-linejoin="round"/>'
        )
    if kind == "question":
        marks = [(-70, 12, -14, 1.0, INK), (0, -6, 10, 1.3, RED), (70, 18, -6, 0.9, INK)]
        return group(
            "".join(
                group(
                    text(
                        0,
                        0,
                        "?",
                        size=round(100 * scale),
                        fill=fill,
                        stroke=INK,
                        stroke_width=3,
                        text_anchor="middle",
                    ),
                    transform=f"translate({cx + dx} {cy + 40 + dy}) rotate({tilt})",
                )
                for dx, dy, tilt, scale, fill in marks
            ),
            data_check_skip="1",
        )
    left = panel((cx - 120, cy - 52, 104, 112), PAPER, offset=8, stroke_width=4) + "".join(
        rect(cx - 104, cy - 30 + i * 20, 70 - (i % 2) * 16, 10, fill=INK) for i in range(3)
    )
    right = panel((cx + 16, cy - 52, 104, 112), RED, offset=8, stroke_width=4) + "".join(
        rect(cx + 32, cy - 30 + i * 20, 70 - (i % 2) * 16, 10, fill=PAPER) for i in range(3)
    )
    bolt = f'<path d="M{cx - 6} {cy - 86}L{cx + 22} {cy - 18}L{cx + 2} {cy - 14}L{cx + 18} {cy + 70}L{cx - 22} {cy - 4}L{cx} {cy - 8}Z" fill="{LIME}" stroke="{INK}" stroke-width="5" stroke-linejoin="round"/>'
    return (
        group(left, transform=f"rotate(-8 {cx} {cy})")
        + group(right, transform=f"rotate(8 {cx} {cy})")
        + animated(bolt, "pop", 1.0)
    )


def why() -> None:
    copy = CONFIG["why"]
    width, height = 1200, 620
    body = rect(0, 0, width, height, fill=RED) + rect(0, 0, width, height, fill="url(#red-dots)")
    body += halftone((0, 420, width, 200), 18, INK, axis="y", opacity=0.4)
    body += stamp(copy["stamp"], copy["title"], (56, 44), (RED, INK, PAPER))
    styles = ((PAPER, INK, INK, LIME), (LIME, INK, INK, LIME), (INK, RED, PAPER, PAPER))
    for index, item in enumerate(copy["panels"]):
        fill, header, ink, caption = styles[index]
        frame = f"panel{index}"
        inner = panel((0, 0, 330, 400), fill, offset=14, id=frame)
        inner += rect(0, 0, 330, 58, fill=header, rx=6) + text(
            22, 40, item["caption"], size=24, font=MONO, weight=800, fill=caption, data_in=frame
        )
        inner += why_art(item["art"], (165, 196))
        inner += text(
            165,
            364,
            item["line"],
            size=25 if len(item["line"]) < 18 else 21,
            fill=ink,
            text_anchor="middle",
            data_in=frame,
        )
        body += animated(
            inner,
            "pop",
            0.2 + index * 0.25,
            f"translate({56 + index * 370} 132) rotate({(-1.5, 1, -1)[index]})",
        )
    footer = rect(0, 0, 640, 46, fill=INK, rx=4) + text(
        320, 32, copy["footer"], size=22, fill=LIME, text_anchor="middle"
    )
    body += animated(footer, "slide", 1.1, "translate(280 560)")
    svg(
        "why",
        (width, height),
        "Why Fakeyou exists. A three-panel comic: on Tuesday the team decides; on Thursday nobody remembers who said it; "
        "next sprint the team decides it again. Decisions should outlive the meeting.",
        body,
        dot_defs(),
    )


def card_art(kind: str, center: Point, fill: str) -> str:
    cx, cy = center
    ink = PAPER if fill == INK else INK
    accent = LIME if fill in (INK, RED) else RED
    if kind == "door":
        return (
            panel((cx - 64, cy - 60, 128, 120), PAPER, offset=8, stroke_width=4)
            + rect(cx - 64, cy - 60, 128, 28, fill=INK, rx=6)
            + f'<path d="M{cx - 30} {cy + 40}L{cx + 4} {cy + 6}L{cx - 30} {cy - 28}" fill="none" stroke="{RED}" stroke-width="13" stroke-linecap="round" stroke-linejoin="round"/>'
            + rect(cx + 16, cy - 24, 28, 64, fill=LIME, stroke=INK, stroke_width=4, rx=3)
        )
    if kind == "wave":
        heights = (18, 46, 30, 70, 52, 88, 40, 64, 26, 50, 20)
        colors = (INK, INK, RED, RED, RED, INK, INK, INK, RED, RED, INK)
        return "".join(
            group(
                rect(cx - 82 + i * 15, cy - h / 2, 9, h, rx=4.5, fill=c),
                class_="pulse",
                style=f"animation-delay:{-i * 0.11:.2f}s",
            )
            for i, (h, c) in enumerate(zip(heights, colors, strict=True))
        )
    if kind == "judge":
        return (
            f'<circle cx="{cx - 46}" cy="{cy}" r="42" fill="{LIME}" stroke="{PAPER}" stroke-width="5"/>'
            f'<path d="M{cx - 66} {cy}L{cx - 50} {cy + 18}L{cx - 24} {cy - 18}" fill="none" stroke="{INK}" stroke-width="10" stroke-linecap="round" stroke-linejoin="round"/>'
            f'<circle cx="{cx + 46}" cy="{cy}" r="42" fill="{INK}" stroke="{PAPER}" stroke-width="5"/>'
            f'<path d="M{cx + 28} {cy - 18}L{cx + 64} {cy + 18}M{cx + 64} {cy - 18}L{cx + 28} {cy + 18}" stroke="{RED}" stroke-width="10" stroke-linecap="round"/>'
        )
    if kind == "speaker":
        return (
            f'<path d="M{cx - 70} {cy - 24}H{cx - 40}L{cx} {cy - 56}V{cy + 56}L{cx - 40} {cy + 24}H{cx - 70}Z" fill="{PAPER}" stroke="{INK}" stroke-width="5" stroke-linejoin="round"/>'
            + "".join(
                f'<path class="pulse" style="animation-delay:{-i * 0.2:.1f}s" d="M{cx + 20 + i * 22} {cy - 26 - i * 14}C{cx + 36 + i * 26} {cy - 8} '
                f'{cx + 36 + i * 26} {cy + 8} {cx + 20 + i * 22} {cy + 26 + i * 14}" fill="none" stroke="{LIME}" stroke-width="9" stroke-linecap="round"/>'
                for i in range(3)
            )
        )
    if kind == "graph":
        nodes = [(-70, -24), (-10, -56), (60, -20), (30, 44), (-46, 44)]
        links = [(0, 1), (1, 2), (2, 3), (3, 4), (4, 0), (1, 3)]
        edges = "".join(
            f'<line x1="{cx + nodes[a][0]}" y1="{cy + nodes[a][1]}" x2="{cx + nodes[b][0]}" y2="{cy + nodes[b][1]}" stroke="{INK}" stroke-width="6"/>'
            for a, b in links
        )
        dots = "".join(
            animated(
                f'<circle cx="{cx + nx}" cy="{cy + ny}" r="{24 if i == 1 else 17}" fill="{LIME if i == 1 else PAPER}" stroke="{INK}" stroke-width="5"/>',
                "pop",
                0.3 + i * 0.1,
            )
            for i, (nx, ny) in enumerate(nodes)
        )
        return edges + dots
    if kind == "report":
        return (
            group(
                panel((cx - 56, cy - 66, 112, 132), PAPER, offset=8, stroke_width=4),
                transform=f"rotate(-4 {cx} {cy})",
            )
            + "".join(
                rect(cx - 38, cy - 42 + i * 22, 76 - (i % 2) * 22, 9, fill=INK if i != 2 else RED)
                for i in range(5)
            )
            + f'<circle cx="{cx + 52}" cy="{cy + 48}" r="24" fill="{LIME}" stroke="{INK}" stroke-width="5"/>'
            + group(
                text(cx + 52, cy + 66, "“", size=40, fill=INK, text_anchor="middle"),
                data_check_skip="1",
            )
        )
    if kind == "stamp":
        return group(
            rect(cx - 104, cy - 50, 208, 100, fill="none", stroke=RED, stroke_width=8, rx=10)
            + rect(cx - 92, cy - 38, 184, 76, fill="none", stroke=RED, stroke_width=3, rx=6)
            + text(cx, cy - 13, "CLAIM", size=30, fill=RED, text_anchor="middle")
            + text(cx, cy + 34, "≠ APPROVAL", size=17, fill=RED, text_anchor="middle"),
            transform=f"rotate(-9 {cx} {cy})",
        )
    return (
        group(
            rect(cx - 80, cy - 24, 160, 48, fill=PAPER, stroke=ink, stroke_width=5, rx=10)
            + rect(cx + 28, cy - 24, 52, 48, fill=RED, stroke=ink, stroke_width=5, rx=10),
            transform=f"rotate(-20 {cx} {cy})",
        )
        + f'<path d="M{cx - 90} {cy + 58}H{cx + 40}" stroke="{accent}" stroke-width="8" stroke-linecap="round" stroke-dasharray="2 18"/>'
    )


def capabilities() -> None:
    copy = CONFIG["capabilities"]
    width, cell_w, cell_h = 1200, 520, 300
    rows = math.ceil(len(copy["cards"]) / 2)
    height = 160 + rows * (cell_h + 48)
    body = rect(0, 0, width, height, fill=CREAM) + rect(
        0, 0, width, height, fill="url(#cream-dots)"
    )
    body += stamp(copy["stamp"], copy["title"], (56, 44), (CREAM, INK, INK))
    rng = random.Random("capabilities")
    for index, card in enumerate(copy["cards"]):
        x = 60 + (index % 2) * (cell_w + 72)
        y = 150 + (index // 2) * (cell_h + 48)
        fill = FILLS[card["fill"]]
        ink = PAPER if fill in (INK, RED) else INK
        frame = f"card{index}"
        inner = panel((0, 0, cell_w, cell_h), fill, offset=14, id=frame)
        inner += rect(0, 0, cell_w, cell_h, fill="url(#red-dots)", rx=6) if fill == RED else ""
        inner += card_art(card["art"], (cell_w - 118, 106), fill)
        inner += rect(24, 24, 60, 44, fill=ink, rx=4) + text(
            54, 56, f"{index + 1:02d}", size=26, fill=fill, text_anchor="middle"
        )
        inner += text(
            26, 222, card["title"], size=52, fill=ink, letter_spacing="-1.5", data_in=frame
        )
        inner += text(
            28, 270, card["line"], size=21, font=BODY, weight=700, fill=ink, data_in=frame
        )
        body += animated(
            inner,
            "pop",
            0.15 + index * 0.09,
            f"translate({x} {y}) rotate({rng.uniform(-1.6, 1.6):.2f})",
        )
    body += sticker((1110, 84), ["LIVE"], radius=62, fill=RED, ink=PAPER, size=22, delay=1.0)
    svg(
        "capabilities",
        (width, height),
        "What Fakeyou does. Eight comic cards: attends Google Meet as a named guest; listens with a live transcript and speaker turns; "
        "judges whether a line is meant for it and answers only when addressed; speaks with streamed voice that can be interrupted; "
        "remembers through a memory graph per owner and twin; debriefs with reports that cite their sources; records decision claims "
        "that are never treated as approvals; and forgets a meeting on request.",
        body,
        dot_defs(),
    )


def node(box: Box, title: str, rows: Sequence[str], **style: Any) -> str:
    x, y, w, h = box
    fill = style.pop("fill", PAPER)
    ink = PAPER if fill in (INK, RED) else INK
    ident = style.pop("ident")
    size = style.pop("size", 22)
    font = style.pop("font", DISPLAY)
    out = panel((x, y, w, h), fill, offset=8, stroke_width=4, id=ident)
    out += text(
        x + w / 2,
        y + 36,
        title,
        size=size,
        fill=ink,
        text_anchor="middle",
        data_in=ident,
        font=font,
    )
    out += lines(
        x + w / 2,
        y + 64,
        rows,
        22,
        size=15,
        font=BODY,
        weight=700,
        fill=ink,
        text_anchor="middle",
        data_in=ident,
    )
    return out


def wire(points: str, **style: Any) -> str:
    color = style.pop("color", "ink")
    stroke = {"ink": INK, "paper": PAPER, "lime": LIME}[color]
    start = f"url(#head-{color})" if style.pop("both", None) else None
    return (
        f'<polyline points="{points}" fill="none" stroke="{stroke}" stroke-width="4" stroke-linejoin="round" '
        f'marker-end="url(#head-{color})"{attrs(marker_start=start, **style)}/>'
    )


def tag(center: Point, label: str, **style: Any) -> str:
    cx, cy = center
    fill = style.pop("fill", INK)
    ink = style.pop("ink", LIME)
    width = len(label) * 10.4 + 24
    return rect(cx - width / 2, cy - 17, width, 30, fill=fill, rx=4) + text(
        cx, cy + 4, label, size=15, font=MONO, weight=800, fill=ink, text_anchor="middle"
    )


def architecture() -> None:
    width, height = 1200, 1000
    body = rect(0, 0, width, height, fill=CREAM) + rect(
        0, 0, width, height, fill="url(#cream-dots)"
    )
    body += stamp("04", "ARCHITECTURE", (56, 44), (CREAM, INK, INK))
    body += node(
        (56, 150, 250, 106), "OPERATOR", ["fakeyou CLI", "or any HTTP client"], ident="operator"
    )
    body += node(
        (924, 150, 236, 106),
        "GOOGLE MEET",
        ["the twin joins as", "a named guest"],
        ident="meet",
        fill=RED,
        size=21,
    )
    body += node(
        (924, 330, 236, 128),
        "PROVIDERS",
        ["speech-to-text, voice,", "reply and memory", "models you configure"],
        ident="providers",
        fill=INK,
        size=22,
    )
    body += f'<rect x="40" y="306" width="840" height="664" rx="18" fill="none" stroke="{INK}" stroke-width="4" stroke-dasharray="14 10"/>'
    body += rect(60, 290, 320, 32, fill=INK, rx=4) + text(
        76, 313, "YOUR INFRASTRUCTURE", size=19, font=MONO, weight=800, fill=LIME
    )
    body += node(
        (316, 350, 312, 108),
        "api",
        ["REST and event stream,", "owner and admin tokens"],
        ident="api",
        fill=LIME,
        font=MONO,
        size=30,
    )
    body += node(
        (76, 504, 768, 58),
        "REDIS STREAMS · jobs and live session commands",
        [],
        ident="redis",
        size=19,
    )
    workers = (
        ("worker-ingestion", ["record → memory", "graph episodes"], PAPER),
        ("worker-debrief", ["record → debrief,", "decision claims"], PAPER),
        ("worker-reembed", ["memory graph", "embeddings"], PAPER),
        ("worker-sessions", ["browser, audio,", "judge and voice"], RED),
    )
    for index, (title, rows, fill) in enumerate(workers):
        body += node(
            (76 + index * 196, 612, 180, 122),
            title,
            rows,
            ident=f"w{index}",
            fill=fill,
            font=MONO,
            size=16,
        )
    body += f'<line x1="76" y1="772" x2="844" y2="772" stroke="{INK}" stroke-width="3" stroke-dasharray="6 8"/>'
    body += tag((460, 772), "SHARED STORES · USED BY api AND WORKERS", fill=CREAM, ink=INK)
    stores = (
        ("PostgreSQL", ["sessions, jobs,", "owner model"], SHADE),
        ("Neo4j", ["Graphiti", "memory graph"], SHADE),
        ("Records", ["transcript,", "debrief, lineage"], SHADE),
        ("RustFS", ["S3 audio", "segments"], SHADE),
        ("Embeddings", ["local", "inference"], LIME),
    )
    for index, (title, rows, fill) in enumerate(stores):
        body += node(
            (76 + index * 156, 812, 140, 116), title, rows, ident=f"s{index}", fill=fill, size=17
        )
    body += wire("306,203 472,203 472,342", both=True)
    body += tag((389, 203), "HTTPS")
    body += wire("472,458 472,498")
    body += "".join(wire(f"{166 + i * 196},562 {166 + i * 196},604") for i in range(4))
    body += f'<polyline points="844,640 896,640 896,203 918,203" fill="none" stroke="{RED}" stroke-width="5" stroke-linejoin="round" marker-end="url(#head-ink)"/>'
    body += wire("844,706 912,706 912,394 918,394")
    body += tag((1042, 292), "BROWSER + AUDIO", fill=RED, ink=PAPER)
    body += tag((1042, 494), "MODEL + SPEECH CALLS")
    svg(
        "architecture",
        (width, height),
        "Fakeyou architecture. An operator calls the token-authenticated api over HTTPS and follows its event stream. Inside your "
        "infrastructure the api hands jobs and live session commands to four worker roles over Redis streams. worker-sessions runs the "
        "browser, audio, turn judge and voice and joins Google Meet as a named guest; worker-ingestion turns the meeting record into memory "
        "graph episodes; worker-debrief writes debriefs and decision claims; worker-reembed refreshes memory graph embeddings. The api and "
        "workers share PostgreSQL, Neo4j with Graphiti, the meeting record files, RustFS object storage and a local embeddings service. "
        "The workers call the speech-to-text, voice, reply and memory model providers you configure.",
        body,
        dot_defs(),
    )


def lifecycle() -> None:
    copy = CONFIG["lifecycle"]
    width, height = 1200, 600
    body = rect(0, 0, width, height, fill=INK) + halftone(
        (0, 400, width, 200), 18, RED, axis="y", opacity=0.5
    )
    body += stamp(copy["stamp"], copy["title"], (56, 44), (INK, CREAM, CREAM))
    body += text(60, 162, copy["live_label"], size=18, font=MONO, weight=800, fill=LIME)
    body += text(
        1140, 384, copy["after_label"], size=18, font=MONO, weight=800, fill=LIME, text_anchor="end"
    )
    for row, (words, accent, y) in enumerate(
        ((copy["live"], RED, 184), (copy["after"], LIME, 404))
    ):
        for index, word in enumerate(words):
            x = 60 + index * 272
            ident = f"step{row}{index}"
            fill = accent if index % 2 == 0 else PAPER
            tile = panel((0, 0, 230, 92), fill, offset=10, id=ident)
            tile += text(
                115,
                58,
                word,
                size=27 if len(word) < 9 else 23,
                fill=PAPER if fill == RED else INK,
                text_anchor="middle",
                data_in=ident,
            )
            body += animated(tile, "pop", 0.2 + row * 0.5 + index * 0.12, f"translate({x} {y})")
            if index < len(words) - 1:
                body += f'<path d="M{x + 240} {y + 46}H{x + 262}" stroke="{PAPER}" stroke-width="6" marker-end="url(#head-paper)"/>'
    body += f'<path d="M1100 230C1172 230 1172 318 1100 318H175V394" fill="none" stroke="{PAPER}" stroke-width="5" stroke-dasharray="10 10" marker-end="url(#head-paper)"/>'
    body += f'<path d="M1100 450C1172 450 1172 546 1100 546H606" fill="none" stroke="{LIME}" stroke-width="6" marker-end="url(#head-lime)"/>'
    loop = rect(0, 0, 420, 42, fill=LIME, rx=4) + text(
        210, 29, copy["loop"], size=19, fill=INK, text_anchor="middle"
    )
    body += animated(loop, "slide", 1.6, "translate(176 525)")
    svg(
        "lifecycle",
        (width, height),
        "One meeting, start to memory. During the call, worker-sessions joins, listens, judges each line and speaks when addressed. "
        "After the call, background workers keep the record, ingest it into the memory graph, write a debrief and extract decision claims. "
        "The memory is recalled in the next meeting.",
        body,
        dot_defs(),
    )


def quickstart() -> None:
    copy = CONFIG["quickstart"]
    width, height = 1200, 280
    body = rect(0, 0, width, height, fill=LIME) + halftone(
        (600, 0, 600, height), 16, INK, axis="x", opacity=0.22
    )
    body += stamp(copy["stamp"], copy["title"], (56, 44), (LIME, INK, INK))
    for index, need in enumerate(copy["needs"]):
        ident = f"need{index}"
        fill = PAPER if index % 2 == 0 else INK
        chip = panel((0, 0, 240, 74), fill, offset=9, id=ident)
        chip += text(
            120,
            48,
            need,
            size=26,
            fill=INK if fill == PAPER else LIME,
            text_anchor="middle",
            data_in=ident,
        )
        body += animated(
            chip,
            "pop",
            0.2 + index * 0.12,
            f"translate({60 + index * 280} 150) rotate({(-2, 1.5, -1, 2)[index]})",
        )
    svg(
        "quickstart",
        (width, height),
        "Run it yourself. You need Docker, uv, Node.js 22 or newer and Python 3.12.",
        body,
        dot_defs(),
    )


def depth() -> None:
    copy = CONFIG["depth"]
    width = 1200
    rows = math.ceil(len(copy["receipts"]) / 3)
    height = 160 + rows * 256
    body = rect(0, 0, width, height, fill=CREAM) + rect(
        0, 0, width, height, fill="url(#cream-dots)"
    )
    body += stamp(copy["stamp"], copy["title"], (56, 44), (CREAM, INK, INK))
    tear = "".join(f"L{340 - (i + 1) * 17:g} {204 + (9 if i % 2 == 0 else 0)}" for i in range(20))
    shape = f"M0 0H340V204{tear}Z"
    for index, receipt in enumerate(copy["receipts"]):
        column, row = index % 3, index // 3
        ident = f"receipt{index}"
        fill = (PAPER, INK, RED)[(index + row) % 3]
        ink = INK if fill == PAPER else PAPER
        accent = RED if fill == PAPER else LIME
        tile = f'<path d="{shape}" transform="translate(10 10)" fill="{INK}"/><path id="{ident}" d="{shape}" fill="{fill}" stroke="{INK}" stroke-width="5" stroke-linejoin="round"/>'
        tile += text(22, 52, receipt["head"], size=23, fill=accent, data_in=ident)
        tile += f'<line x1="22" y1="72" x2="318" y2="72" stroke="{ink}" stroke-width="3" stroke-dasharray="4 7"/>'
        tile += lines(
            22, 112, receipt["body"], 30, size=18, font=BODY, weight=700, fill=ink, data_in=ident
        )
        tilt = (-1.2, 0.8, -0.6, 1, -0.9, 0.7)[index % 6]
        body += animated(
            tile,
            "pop",
            0.15 + index * 0.1,
            f"translate({56 + column * 372} {160 + row * 256}) rotate({tilt})",
        )
    svg(
        "depth",
        (width, height),
        "Under the hood. "
        + " ".join(f"{r['head']}: {' '.join(r['body'])}." for r in copy["receipts"]),
        body,
        dot_defs(),
    )


def direction() -> None:
    copy = CONFIG["direction"]
    width, height = 1200, 640
    body = rect(0, 0, width, height, fill=RED) + rect(0, 0, width, height, fill="url(#red-dots)")
    body += halftone((0, 440, width, 200), 18, INK, axis="y", opacity=0.35)
    body += stamp(copy["stamp"], copy["title"], (56, 44), (RED, INK, PAPER))
    fills = (LIME, PAPER, INK)
    for index, lane in enumerate(copy["lanes"]):
        ident = f"lane{index}"
        fill = fills[index]
        board = panel((0, 0, 330, 440), fill, offset=14, id=ident)
        board += rect(0, 0, 330, 64, fill=LIME if fill == INK else INK, rx=6)
        board += text(
            165,
            44,
            lane["head"],
            size=30,
            fill=INK if fill == INK else LIME,
            text_anchor="middle",
            data_in=ident,
        )
        for row, note in enumerate(lane["notes"]):
            note_id = f"{ident}n{row}"
            board += rect(
                20,
                90 + row * 84,
                290,
                66,
                fill=SHADE if fill == INK else CREAM,
                stroke=INK,
                stroke_width=3,
                rx=4,
                id=note_id,
            )
            board += text(
                34, 130 + row * 84, note, size=18, font=BODY, weight=800, fill=INK, data_in=note_id
            )
        body += animated(
            board,
            "pop",
            0.2 + index * 0.25,
            f"translate({60 + index * 370} 150) rotate({(-1, 1, -1.4)[index]})",
        )
    body += sticker((1110, 84), ["NEXT"], radius=62, size=22, delay=1.0)
    svg(
        "direction",
        (width, height),
        "Where Fakeyou is going. "
        + " ".join(f"{lane['head']}: {', '.join(lane['notes'])}." for lane in copy["lanes"]),
        body,
        dot_defs(),
    )


def outro() -> None:
    copy = CONFIG["outro"]
    width, height = 1200, 420
    body = rect(0, 0, width, height, fill=INK) + halftone(
        (0, 0, width, height), 20, RED, axis="x", opacity=0.45
    )
    body += f'<polygon points="0,300 1200,180 1200,420 0,420" fill="{RED}"/>'
    body += stamp(copy["stamp"], "CONTRIBUTE", (56, 44), (INK, CREAM, CREAM))
    body += animated(
        lines(60, 206, copy["title"], 100, size=90, fill=CREAM, letter_spacing="-3"), "slide", 0.2
    )
    body += text(62, 372, copy["line"], size=24, font=BODY, weight=800, fill=CREAM)
    burst = starburst((960, 170), (150, 118), 16, fill=LIME, stroke=INK, stroke_width=6)
    body += animated(
        group(
            burst + group(mask((0, 0), 70), transform="translate(960 150) rotate(-6)"), class_="wob"
        ),
        "pop",
        0.6,
    )
    svg("outro", (width, height), f"Build it with us. {copy['line']}", body, dot_defs())


def buttons() -> None:
    for button in CONFIG["buttons"]:
        width = max(150, round(36 + len(button["label"]) * 15.5))
        fill = FILLS[button["fill"]]
        ink = LIME if fill == INK else PAPER if fill == RED else INK
        edge, shadow = (LIME, RED) if fill == INK else (INK, INK)
        body = rect(6, 6, width - 12, 46, fill=shadow, rx=6) + rect(
            2, 2, width - 14, 44, fill=fill, stroke=edge, stroke_width=4, rx=6, id="button"
        )
        body += text(
            (width - 12) / 2 + 2,
            32,
            button["label"],
            size=19,
            fill=ink,
            text_anchor="middle",
            data_in="button",
        )
        svg(button["file"], (width, 56), button["label"].replace("★ ", "") + " button", body)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for stale in OUT.glob("*.svg"):
        stale.unlink()
    for build in (
        hero,
        why,
        capabilities,
        architecture,
        lifecycle,
        quickstart,
        depth,
        direction,
        outro,
        buttons,
    ):
        build()
    sys.stdout.write(f"wrote {len(list(OUT.glob('*.svg')))} assets to {OUT.relative_to(ROOT)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
