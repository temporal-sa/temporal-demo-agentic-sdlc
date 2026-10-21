# Deck

`build_deck.py` writes the story-driven deck that goes with the demo, 24 slides with speaker notes:

```bash
uv run python deck/build_deck.py --render   # deck/out/agentic-sdlc.pptx, plus a PNG per slide for review
```

It starts from the buildkit's Temporal master deck (set `BUILDKIT` if the kit isn't at
`~/Source/temporal-toolkit-demo-buildkit`), so the theme and backgrounds carry over, and it draws all
text explicitly so it survives conversion to Google Slides. Fonts: Inter and Noto Sans Mono.

**The story:** Maya's team wires coding agents into their whole pipeline the week before code freeze, and every
kind of failure happens once overnight. The deck sets up the same six decision criteria the console scores
(D1–D6), hands over to the live demo, then shows one slide per criterion with what the audience saw and the
Temporal feature behind it. Every figure and quote is verbatim from a source on the slide and on the Sources
slide; the research is in [docs/story.md](../docs/story.md).

**Where it goes:** `deck/out/` is gitignored. Upload the `.pptx` to the demo's Drive folder as
`[WORKING - NOT FOR USE]` and open it with *File → Save as Google Slides* (buildkit guide 13). The images in
`assets/` are crops of the console and the Temporal UI from real runs; re-capture them with
`scripts/screenshot.py` if the UI changes.
