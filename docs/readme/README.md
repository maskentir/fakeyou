# README artwork

The images on the project README are SVG files in [`assets/readme/`](../../assets/readme).
They are generated, so edit the sources here and regenerate instead of editing the SVGs.

| File | Role |
| --- | --- |
| `art.json` | Palette and every word that appears in the artwork |
| `generate.py` | Deterministic generator: reads `art.json`, writes `assets/readme/*.svg` |
| `check.mjs` | Validates, renders and previews the artwork and the README |
| `previews/` | Written by `check.mjs`: PNG renders, README screenshots and the check report. Ignored by git; regenerate it locally |

## Regenerate

```sh
uv run --no-sync python docs/readme/generate.py
```

The generator uses only the Python standard library. The same input always produces
byte-identical SVGs, so a diff shows exactly what changed. It deletes and rewrites
every SVG in `assets/readme/`.

## Check

```sh
npm ci --ignore-scripts
npx playwright install chromium
node docs/readme/check.mjs            # assets only
node docs/readme/check.mjs --readme   # also renders the README through GitHub's Markdown API
```

To use a Chromium that is already installed, set `PLAYWRIGHT_CHROMIUM_EXECUTABLE` to its
path. `--readme` calls `gh api markdown`, so it needs the GitHub CLI and network
access.

The check fails, and lists every problem, when an SVG:

- has no `viewBox`, `role="img"` or `<title>`
- references an external URL, font or stylesheet, or contains a script or event
  handler
- animates without a `prefers-reduced-motion` fallback
- contains credential-like text
- has text clipped by its canvas, text that leaves the box it belongs to
  (`data-in`), or two text elements that overlap. Consecutive lines of one text block
  (`data-lines`) count as overlapping only when their baselines are closer than 0.9 em.

It runs the layout checks twice: once with the intended fonts, and once with every
non-monospace font replaced by Verdana, to simulate a viewer whose system lacks Arial
Black. It writes a PNG of each asset with motion reduced, which is the final frame of
every animation, and with `--readme` adds desktop (1280 px) and mobile (390 px)
screenshots in light and dark colour schemes.

## Design rules

- Palette: red `#EE2D27`, ink `#111111`, cream `#F6EEDF`, lime `#D7F043`.
- The FAKEYOU wordmark is drawn as paths, so it never depends on an installed font.
  Other text uses system font stacks; GitHub renders SVGs as images, which cannot
  load web fonts.
- Animations run once on load (entrances, pops, slides). The only loops are a slow
  sticker wobble, the hero ticker and the voice bars. With reduced motion every
  animation is off and the artwork shows its final state.
- Each asset carries its own background, so it reads the same on light and dark
  GitHub themes.
- Text inside the artwork stays at headline length. Everything a reader needs to act
  on is in the Markdown, so it can be searched, copied and read on a phone.
- The artwork makes the same claims as the README text, and both must match the code.
  When a capability changes, update `art.json`, the README and the image `alt` text
  together.
