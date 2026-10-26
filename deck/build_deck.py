"""Build the story-driven deck for the agentic SDLC demo.

    uv run python deck/build_deck.py            # writes deck/out/agentic-sdlc.pptx
    uv run python deck/build_deck.py --render   # also renders every slide to deck/out/png/ for review

The deck starts from the buildkit's Temporal master deck so it keeps the theme, backgrounds and
layouts, then draws every slide's text and shapes explicitly (fonts, sizes, colours), which
survives conversion to Google Slides. Code is one text box per line, per guide 14. Every number
on a slide has a source on the slide and on the final Sources slide (see docs/story.md).

The output is not committed; upload it to the demo's Drive folder as a WORKING copy (guide 13).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Pt

HERE = Path(__file__).resolve().parent
BUILDKIT = Path(os.environ.get("BUILDKIT", Path.home() / "Source/temporal-toolkit-demo-buildkit"))
MASTER = BUILDKIT / "assets/decks/Temporal Slidey Slides Optimized v2026.pptx"
OUT = HERE / "out"
ASSETS = HERE / "assets"

W, H = 9144000, 5143500
IN = 914400

WHITE = RGBColor(0xF8, 0xFA, 0xFC)
MUTED = RGBColor(0x92, 0xA4, 0xC3)
INDIGO = RGBColor(0xCA, 0xCB, 0xF9)
UV = RGBColor(0x44, 0x4C, 0xE7)
PURPLE = RGBColor(0xB6, 0x64, 0xFF)
GREEN = RGBColor(0x1F, 0xF1, 0xA5)
YELLOW = RGBColor(0xFE, 0xCB, 0x2F)
PINK = RGBColor(0xFF, 0x6B, 0xFF)
CYAN = RGBColor(0x4F, 0xD8, 0xFF)
CARD = RGBColor(0x1C, 0x1D, 0x3A)
CARD_EDGE = RGBColor(0x34, 0x37, 0x6B)
SLATE = RGBColor(0x46, 0x5A, 0x78)
CODE_BG = RGBColor(0x12, 0x12, 0x1C)
# Guide 14's fixed code palette.
K_KEYWORD = RGBColor(0x7F, 0x86, 0xF1)
K_FUNC = RGBColor(0x4F, 0xD8, 0xFF)
K_COMMENT = RGBColor(0x8A, 0x8F, 0xA8)

SANS = "Inter"
MONO = "Noto Sans Mono"

SOURCES = {
    "forrester": ("Diego Lo Giudice, Forrester, “Agentic Software Development Takes The Lead”, June 8 2026",
                  "forrester.com/blogs/agentic-software-development-takes-the-lead-from-code-assistants-to-orchestrated-sdlc-agents"),
    "keyhole": ("Keyhole Software, “Agentic AI in the SDLC: 2026 adoption and impact data”, Sept 2 2026",
                "keyholesoftware.com/agentic-ai-in-the-software-development-lifecycle-2026-adoption-and-impact-data"),
    "ciklum": ("Julian Beckers, Ciklum, “The Agentic SDLC”, July 12 2026", "ciklum.com/blog/ai-revolutionize-software-development-lifecycle"),
    "duolingo": ("Jesse Welch, Duolingo Engineering, “Building AI agents”, Dec 4 2025", "blog.duolingo.com/buildingaiagents"),
    "port": ("Zohar Einy, Port, “The agentic SDLC”, 2026", "port.io/blog/agentic-sdlc-software-lifecycle-rebuilt-around-agents"),
    "kanbots": ("KanBots, “Parallelize AI coding agents”", "kanbots.dev/learn/parallelize-ai-coding-agents"),
    "veza": ("Matt Biggin, Veza Digital, “Best AI agents”, April 17 2026", "vezadigital.com/post/best-ai-agents"),
}


class Deck:
    def __init__(self) -> None:
        self.prs = Presentation(str(MASTER))
        self.original = len(self.prs.slides)
        layouts = {layout.name: layout for layout in self.prs.slide_layouts}
        self.title_layout = layouts["TITLE_2_2_1_2_2_1_1"]
        self.blank = layouts["BLANK_1"]
        self.plain = layouts["BLANK_2"]

    # -- primitives ------------------------------------------------------------------------

    def slide(self, layout=None):
        slide = self.prs.slides.add_slide(layout or self.blank)
        for shape in list(slide.placeholders):
            if shape.placeholder_format.type != 13:  # keep slide numbers only
                shape._element.getparent().remove(shape._element)
        return slide

    def text(self, slide, x, y, w, h, text, *, size=14, color=WHITE, bold=False, font=SANS, align=PP_ALIGN.LEFT,
             anchor=MSO_ANCHOR.TOP, line_spacing=None, light=False):
        box = slide.shapes.add_textbox(Emu(x), Emu(y), Emu(w), Emu(h))
        frame = box.text_frame
        frame.word_wrap = True
        frame.vertical_anchor = anchor
        frame.margin_left = frame.margin_right = frame.margin_top = frame.margin_bottom = 0
        lines = text if isinstance(text, list) else [text]
        for i, line in enumerate(lines):
            para = frame.paragraphs[0] if i == 0 else frame.add_paragraph()
            para.alignment = align
            if line_spacing:
                para.line_spacing = line_spacing
            runs = line if isinstance(line, list) else [(line, {})]
            for content, style in runs:
                run = para.add_run()
                run.text = content
                f = run.font
                f.name = style.get("font", font)
                f.size = Pt(style.get("size", size))
                f.bold = style.get("bold", bold)
                f.color.rgb = style.get("color", color)
                if light and not style.get("bold", bold):
                    f.name = f"{style.get('font', font)} Light" if style.get("font", font) == SANS else f.name
        return box

    def headline(self, slide, text, *, size=24, y=0.42 * IN, w=None):
        return self.text(slide, int(0.45 * IN), int(y), w or int(8.9 * IN), int(0.9 * IN), text, size=size, light=True)

    def box(self, slide, x, y, w, h, *, fill=CARD, edge=CARD_EDGE, shape=MSO_SHAPE.RECTANGLE, width=0.75, dash=False):
        s = slide.shapes.add_shape(shape, Emu(x), Emu(y), Emu(w), Emu(h))
        s.fill.solid()
        s.fill.fore_color.rgb = fill
        if edge is None:
            s.line.fill.background()
        else:
            s.line.color.rgb = edge
            s.line.width = Pt(width)
            if dash:
                from pptx.enum.dml import MSO_LINE
                s.line.dash_style = MSO_LINE.DASH
        s.shadow.inherit = False
        if s.has_text_frame:
            s.text_frame.margin_left = s.text_frame.margin_right = Emu(int(0.06 * IN))
        return s

    def label_box(self, slide, x, y, w, h, label, *, edge=UV, size=9, color=WHITE, fill=CARD, shape=MSO_SHAPE.RECTANGLE, font=MONO):
        s = self.box(slide, x, y, w, h, fill=fill, edge=edge, shape=shape, width=1.25)
        frame = s.text_frame
        frame.word_wrap = True
        frame.vertical_anchor = MSO_ANCHOR.MIDDLE
        frame.margin_left = frame.margin_right = Emu(int(0.04 * IN))
        for i, line in enumerate(label.split("\n")):
            para = frame.paragraphs[0] if i == 0 else frame.add_paragraph()
            para.alignment = PP_ALIGN.CENTER
            run = para.add_run()
            run.text = line
            run.font.name, run.font.size, run.font.color.rgb = font, Pt(size), color
        return s

    def arrow(self, slide, x1, y1, x2, y2, color=GREEN, width=1.25):
        from pptx.enum.shapes import MSO_CONNECTOR
        line = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Emu(x1), Emu(y1), Emu(x2), Emu(y2))
        line.line.color.rgb = color
        line.line.width = Pt(width)
        tail = line.line._get_or_add_ln()
        from lxml import etree
        end = etree.SubElement(tail, "{http://schemas.openxmlformats.org/drawingml/2006/main}tailEnd")
        end.set("type", "triangle")
        return line

    def image(self, slide, name, x, y, w=None, h=None):
        return slide.shapes.add_picture(str(ASSETS / name), Emu(x), Emu(y), Emu(w) if w else None, Emu(h) if h else None)

    def fit_image(self, slide, name, x, y, w, h, *, border=True):
        """Place an image inside a box, keeping its aspect ratio, with a thin frame."""
        from PIL import Image

        iw, ih = Image.open(ASSETS / name).size
        scale = min(w / iw, h / ih)
        pw, ph = int(iw * scale), int(ih * scale)
        px, py = x + (w - pw) // 2, y
        pic = slide.shapes.add_picture(str(ASSETS / name), Emu(px), Emu(py), Emu(pw), Emu(ph))
        if border:
            pic.line.color.rgb = CARD_EDGE
            pic.line.width = Pt(0.75)
        return pic

    def source(self, slide, *keys, y=None):
        text = "Source: " + "; ".join(SOURCES[k][0] for k in keys)
        self.text(slide, int(0.45 * IN), y or int(5.12 * IN), int(8.6 * IN), int(0.3 * IN), text, size=7, color=MUTED)

    def notes(self, slide, text):
        slide.notes_slide.notes_text_frame.text = text.strip()

    def finish(self, path: Path) -> None:
        ids = self.prs.slides._sldIdLst
        for sld_id in list(ids)[: self.original]:
            self.prs.part.drop_rel(sld_id.rId)
            ids.remove(sld_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.prs.save(str(path))


# -- the slides ----------------------------------------------------------------------------


def build(d: Deck) -> None:
    # 1. Title
    s = d.slide(d.title_layout)
    d.text(s, int(0.6 * IN), int(1.35 * IN), int(5.6 * IN), int(1.9 * IN),
           ["Agents that ship", "Agentic SDLC on Temporal"], size=32, light=True)
    d.text(s, int(0.6 * IN), int(3.45 * IN), int(5.0 * IN), int(0.6 * IN),
           "A GitHub issue taken to production by AI agents, and kept alive by Temporal", size=13, color=INDIGO)
    d.text(s, int(6.4 * IN), int(5.05 * IN), int(3.3 * IN), int(0.3 * IN), "Temporal Solutions Architecture · 2026", size=8, color=MUTED,
           align=PP_ALIGN.RIGHT)
    d.notes(s, """
Today I want to show you what happens when you stop using AI agents just to write code, and start letting them ship it.
I'll start with the result, then tell you why it's harder than it looks, and then we'll do it live.
""")

    # 2. Result first
    s = d.slide()
    d.headline(s, "This change was planned, coded, reviewed and shipped by AI agents.", size=22)
    d.fit_image(s, "result.jpg", int(0.45 * IN), int(1.3 * IN), int(5.9 * IN), int(3.4 * IN))
    stats = [("4m 34s", "issue to production"), ("35", "model calls"), ("$0.65", "estimated model cost")]
    for i, (value, label) in enumerate(stats):
        y = int((1.25 + i * 1.12) * IN)
        d.text(s, int(6.65 * IN), y, int(2.9 * IN), int(0.5 * IN), value, size=28, color=GREEN, light=True)
        d.text(s, int(6.65 * IN), y + int(0.52 * IN), int(2.9 * IN), int(0.3 * IN), label, size=10, color=INDIGO)
    d.text(s, int(6.65 * IN), int(4.6 * IN), int(2.9 * IN), int(0.4 * IN),
           "One run of this demo, October 2026: the search box was added by agents, reviewed by a different model, and canaried before it shipped.",
           size=8, color=MUTED)
    d.notes(s, """
This is a storefront. Twenty minutes before this talk, I gave a team of AI agents a GitHub issue: add a search box.
A planner agent wrote a plan, two coding agents built it in parallel, a reviewer agent from a different model family checked it, and it went through staging and a canary into production.
I approved two things: the plan, and the release. Everything else, the agents did. Temporal ran the process around them.
So why isn't everyone doing this already?
""")

    # 3. Big number: Forrester
    s = d.slide()
    d.text(s, 0, int(1.0 * IN), W, int(1.5 * IN), "30–40% → under 10%", size=60, color=GREEN, align=PP_ALIGN.CENTER, light=True)
    d.text(s, int(1.2 * IN), int(2.7 * IN), int(7.6 * IN), int(1.0 * IN),
           "“Coding may improve by 30% to 40%, but if planning, testing, and release remain manual, overall team productivity often increases by less than 10%.”",
           size=15, color=WHITE, align=PP_ALIGN.CENTER, light=True)
    d.source(s, "forrester", y=int(4.6 * IN))
    d.notes(s, """
Here's the problem in one line, from Forrester this June. AI made coding thirty to forty percent faster.
But when planning, testing and release stay manual, the team as a whole gets less than ten percent faster.
Coding was never the whole job.
""")

    # 4. Big number: adoption gap
    s = d.slide()
    d.text(s, 0, int(1.0 * IN), W, int(1.5 * IN), "84–90% vs 13%", size=60, color=GREEN, align=PP_ALIGN.CENTER, light=True)
    d.text(s, int(1.2 * IN), int(2.7 * IN), int(7.6 * IN), int(1.0 * IN),
           "Roughly 84–90% of developers use AI tools for coding, but only about 13% of teams have AI operating across the full development lifecycle.",
           size=15, align=PP_ALIGN.CENTER, light=True)
    d.source(s, "keyhole", y=int(4.6 * IN))
    d.notes(s, """
And almost nobody has crossed that gap. Most developers now use AI to write code. About one team in eight has it working across the whole lifecycle.
Keyhole pulled these numbers from Gartner, DORA and others; treat them as a range, but the gap is the point.
""")

    # 5. Statement
    s = d.slide()
    d.text(s, int(1.0 * IN), int(1.6 * IN), int(8.0 * IN), int(1.4 * IN),
           "The bottleneck moved from writing code to everything after it.", size=30, align=PP_ALIGN.CENTER, light=True)
    d.text(s, int(1.5 * IN), int(3.2 * IN), int(7.0 * IN), int(0.6 * IN),
           "“The new bottleneck is shifting from code generation to review, quality, governance, and architecture.”",
           size=12, color=INDIGO, align=PP_ALIGN.CENTER)
    d.source(s, "ciklum", y=int(4.6 * IN))
    d.notes(s, "Review, testing, release, operations. That's where the time goes now, and that's what agentic SDLC is about.")

    # 6. The lifecycle
    s = d.slide()
    d.headline(s, "Agentic SDLC puts agents in every stage, and people where they're accountable.")
    stages = [("Plan", "Agent drafts the plan", True), ("Code", "Agents build in parallel", False), ("Test", "Agents fix their own failures", False),
              ("Review", "A second model reviews", False), ("Release", "Canary, then promote", True), ("Operate", "Roll back a bad change", False)]
    for i, (name, what, gate) in enumerate(stages):
        x = int((0.45 + i * 1.5) * IN)
        d.box(s, x, int(1.55 * IN), int(1.38 * IN), int(0.06 * IN), fill=GREEN if not gate else YELLOW, edge=None)
        d.text(s, x, int(1.75 * IN), int(1.38 * IN), int(0.4 * IN), name, size=18, light=True)
        d.text(s, x, int(2.2 * IN), int(1.38 * IN), int(0.6 * IN), what, size=10, color=INDIGO)
        if gate:
            d.text(s, x, int(2.75 * IN), int(1.38 * IN), int(0.3 * IN), "◆ a person approves", size=9, color=YELLOW, font=MONO)
    d.text(s, int(0.45 * IN), int(3.6 * IN), int(8.6 * IN), int(0.8 * IN),
           "“A deploy agent might run free in staging but need approval in production, or run on its own at noon but check with a human at 3 a.m.”",
           size=12, color=WHITE, light=True)
    d.source(s, "port", y=int(4.55 * IN))
    d.notes(s, """
So what do people mean by agentic SDLC? Agents across the whole lifecycle: planning, coding, testing, review, release and operations.
And people stay accountable. Port describes autonomy as a dial: a deploy agent can run free in staging but needs approval in production.
In our demo, people approve the plan and the production release. The yellow diamonds. Agents do the rest.
""")

    # 7. Meet Maya
    s = d.slide()
    d.headline(s, "Maya's team gave agents the backlog the week before code freeze.")
    d.text(s, int(0.45 * IN), int(1.45 * IN), int(5.2 * IN), int(3.0 * IN), [
        "Maya leads the storefront team at Orbit Supply Co. Forty issues, five days.",
        "",
        "Her team already uses coding agents. This week they wire them into the whole pipeline: an agent plans each issue, four agents code in parallel overnight, a review agent checks the PRs, and a release script ships what passes.",
        "",
        "Thursday night, she goes home.",
    ], size=14, light=True)
    d.box(s, int(6.2 * IN), int(1.45 * IN), int(3.3 * IN), int(1.85 * IN), fill=CARD, edge=CARD_EDGE)
    d.text(s, int(6.4 * IN), int(1.6 * IN), int(3.0 * IN), int(2.4 * IN), [
        [("The plan", {"bold": True, "color": INDIGO, "size": 11})],
        "Issue → plan → 4 coding agents → review agent → release script",
        "",
        [("Held together by", {"bold": True, "color": INDIGO, "size": 11})],
        "a job queue, a state table, a cron job to retry stuck runs, a Slack bot for approvals",
    ], size=11, color=WHITE)
    d.notes(s, """
Let me tell you about Maya. She leads the storefront team at a company called Orbit Supply. It's the week before the holiday code freeze and there are forty issues left.
Her team already uses coding agents. So this week they wire them into the whole pipeline.
And like most teams, they hold it together with what they have: a job queue, a table to track state, a cron job that retries stuck runs, and a Slack bot for approvals.
Thursday night she goes home. What do you think she finds on Friday morning?
""")

    # 8. What went wrong
    s = d.slide()
    d.headline(s, "Friday morning, every kind of failure had happened once.")
    rows = [
        ("01", "The agent on the biggest issue crashed at 2 a.m.", "The retry started from scratch and paid for every model call again."),
        ("02", "The plan approval sat in Slack for six hours.", "The script waiting for it timed out and the run was lost."),
        ("03", "Four agents hit the provider's rate limit together.", "Four times the burn, and all four stalled."),
        ("04", "A change that passed review broke checkout in production.", "Nobody noticed until customers did."),
        ("05", "Someone changed the agent prompts at midnight.", "Runs already in flight picked them up halfway."),
        ("06", "“Which agent did this?”", "No record of what each agent did, or why."),
        ("07", "Maya's review comment on the PR went unanswered.", "The only choices were merge it or start over."),
    ]
    for i, (n, what, cost) in enumerate(rows):
        y = int((1.25 + i * 0.53) * IN)
        d.box(s, int(0.45 * IN), y, int(8.6 * IN), int(0.45 * IN), fill=CARD, edge=None)
        d.box(s, int(0.45 * IN), y, int(0.05 * IN), int(0.45 * IN), fill=YELLOW, edge=None)
        d.text(s, int(0.65 * IN), y, int(0.5 * IN), int(0.45 * IN), n, size=14, color=YELLOW, bold=True, anchor=MSO_ANCHOR.MIDDLE)
        d.text(s, int(1.2 * IN), y + int(0.03 * IN), int(7.7 * IN), int(0.42 * IN),
               [[(what + " ", {"bold": True, "size": 11}), (cost, {"size": 11, "color": INDIGO})]], anchor=MSO_ANCHOR.MIDDLE)
    d.notes(s, """
Every kind of failure happened once.
The agent on the biggest issue crashed at two in the morning. The cron job retried it, from scratch, paying for every model call again. Duolingo, who run coding agents in production, put it plainly: an agent may go off the rails, crash, hang, and need to be retried.
The plan approval sat in Slack for six hours; the script waiting for it timed out.
Four agents hit a rate limit together. Four times the burn, and all of them stalled.
A change that passed review broke checkout in production.
And when Maya left a comment on a PR, nothing read it. Her only choices were to merge it anyway or throw the work away and start again.
Someone changed the prompts at midnight, and runs in flight picked it up halfway.
And on Friday morning, nobody could say which agent did what.
These aren't AI problems. They're distributed systems problems.
""")

    # 9. Before
    s = d.slide()
    d.headline(s, "The agents were fine. The glue between them wasn't.")
    nodes = [("ISSUE\nTRACKER", 0.5, 1.9), ("JOB\nQUEUE", 2.1, 1.9), ("AGENT\nSANDBOXES ×4", 3.7, 1.9), ("RELEASE\nSCRIPT", 5.3, 1.9),
             ("STATE\nTABLE", 2.1, 3.3), ("CRON:\nRETRY STUCK", 3.7, 3.3), ("SLACK BOT\nAPPROVALS", 5.3, 3.3), ("PRODUCTION", 7.2, 1.9)]
    for label, x, y in nodes:
        d.label_box(s, int(x * IN), int(y * IN), int(1.3 * IN), int(0.75 * IN), label, edge=SLATE, color=MUTED)
    for (x1, y1), (x2, y2) in [((1.8, 2.27), (2.1, 2.27)), ((3.4, 2.27), (3.7, 2.27)), ((5.0, 2.27), (5.3, 2.27)), ((6.6, 2.27), (7.2, 2.27)),
                               ((2.75, 2.65), (2.75, 3.3)), ((4.35, 3.3), (4.35, 2.65)), ((5.95, 3.3), (5.95, 2.65))]:
        d.arrow(s, int(x1 * IN), int(y1 * IN), int(x2 * IN), int(y2 * IN), color=SLATE)
    d.text(s, int(0.5 * IN), int(4.35 * IN), int(8.5 * IN), int(0.5 * IN),
           "Every box is code Maya's team wrote and has to keep alive: retries, timeouts, resumes, approvals, rollbacks, audit.", size=11, color=INDIGO)
    d.notes(s, "This is what Maya built. Every grey box is code her team has to keep alive. Most of it exists to handle the failures we just saw, and none of it handles them well.")

    # 10. Statement
    s = d.slide()
    d.text(s, int(0.8 * IN), int(1.7 * IN), int(8.4 * IN), int(1.6 * IN),
           ["Agents decide inside a step.", "Temporal runs the process."], size=34, align=PP_ALIGN.CENTER, light=True)
    d.notes(s, """
Here's the idea. Agents are great at deciding what to do inside a step: what to write, what to test, what to say in a review.
They're bad at being a reliable process: waiting days, surviving crashes, retrying correctly, rolling back.
So let the agents decide inside a step, and let Temporal run the process.
""")

    # 11. After
    s = d.slide()
    d.headline(s, "With Temporal, the pipeline is one Workflow and every agent is a Workflow.")
    d.label_box(s, int(0.5 * IN), int(2.1 * IN), int(1.4 * IN), int(0.7 * IN), "GITHUB\nISSUE", edge=GREEN)
    wf = d.box(s, int(2.3 * IN), int(1.35 * IN), int(5.0 * IN), int(2.95 * IN), fill=RGBColor(0x1A, 0x18, 0x30), edge=UV, width=1.5)
    d.text(s, int(2.45 * IN), int(1.42 * IN), int(4.8 * IN), int(0.3 * IN), "IssueToProduction Workflow", size=10, color=INDIGO, font=MONO)
    steps = [("PLANNER\nAGENT", 2.5, 1.95, PURPLE, MSO_SHAPE.OVAL), ("◆ /approve", 3.75, 1.95, YELLOW, MSO_SHAPE.DIAMOND),
             ("CODER", 5.0, 1.75, PURPLE, MSO_SHAPE.OVAL), ("CODER", 5.0, 2.45, PURPLE, MSO_SHAPE.OVAL),
             ("REVIEW\nAGENT", 6.15, 1.95, PURPLE, MSO_SHAPE.OVAL),
             ("STAGING", 2.5, 3.2, CYAN, MSO_SHAPE.RECTANGLE), ("◆ /ship", 3.75, 3.2, YELLOW, MSO_SHAPE.DIAMOND),
             ("CANARY", 5.0, 3.2, CYAN, MSO_SHAPE.RECTANGLE), ("ROLLBACK\nSAGA", 6.15, 3.2, CYAN, MSO_SHAPE.RECTANGLE)]
    for label, x, y, color, shape in steps:
        d.label_box(s, int(x * IN), int(y * IN), int(1.0 * IN), int(0.62 * IN), label, edge=color, size=7, shape=shape)
    d.arrow(s, int(1.9 * IN), int(2.45 * IN), int(2.3 * IN), int(2.45 * IN))
    d.label_box(s, int(7.7 * IN), int(1.5 * IN), int(1.4 * IN), int(0.6 * IN), "ANTHROPIC\nOPENAI · GEMINI", edge=GREEN, size=7)
    d.label_box(s, int(7.7 * IN), int(2.3 * IN), int(1.4 * IN), int(0.6 * IN), "GITHUB\nBRANCHES · PRs", edge=GREEN, size=7)
    d.label_box(s, int(7.7 * IN), int(3.1 * IN), int(1.4 * IN), int(0.6 * IN), "PRODUCTION", edge=GREEN, size=7)
    d.arrow(s, int(7.3 * IN), int(2.0 * IN), int(7.7 * IN), int(1.8 * IN))
    d.arrow(s, int(7.3 * IN), int(2.6 * IN), int(7.7 * IN), int(2.6 * IN))
    d.arrow(s, int(7.3 * IN), int(3.4 * IN), int(7.7 * IN), int(3.4 * IN))
    d.text(s, int(0.5 * IN), int(4.45 * IN), int(8.6 * IN), int(0.5 * IN),
           [[("● agent Workflow   ", {"color": PURPLE, "font": MONO, "size": 9}), ("◆ a person, replying on GitHub   ", {"color": YELLOW, "font": MONO, "size": 9}),
             ("■ Activity with a Retry Policy", {"color": CYAN, "font": MONO, "size": 9})]])
    d.notes(s, """
Here's the same pipeline on Temporal. The whole thing is one Workflow, and every agent is a Workflow of its own, a child.
Every model call and every tool call is an Activity, so it's retried for you and recorded when it finishes.
The diamonds are people, replying on the issue or the PR. GitHub's webhook delivers each reply to the Workflow as a Signal, and they can take as long as they like.
The job queue, the state table, the cron job and the Slack bot are gone. That's Temporal.
""")

    # 12. Decision criteria
    s = d.slide()
    d.headline(s, "Seven things to prove before agents ship your code.")
    criteria = [
        ("D1", "An agent crash loses no work and re-spends no tokens", "Kill the worker mid-fix"),
        ("D2", "Humans stay accountable without babysitting", "/approve and /ship on GitHub"),
        ("D3", "Parallel agents, flaky APIs and spend stay under control", "Rate-limit one agent; hit the budget"),
        ("D4", "A bad change in production is caught and undone", "Break the canary"),
        ("D5", "The pipeline can change while runs are in flight", "Roll out v2 mid-run"),
        ("D6", "Every agent action is auditable", "Replay a dependency repair"),
        ("D7", "People can steer agents without starting over", "Comment on the issue or PR"),
    ]
    d.text(s, int(1.15 * IN), int(1.25 * IN), int(5 * IN), int(0.3 * IN), "What you need proven", size=9, color=MUTED, font=MONO)
    d.text(s, int(6.3 * IN), int(1.25 * IN), int(3 * IN), int(0.3 * IN), "What you'll see live", size=9, color=MUTED, font=MONO)
    for i, (cid, need, see) in enumerate(criteria):
        y = int((1.5 + i * 0.5) * IN)
        d.box(s, int(0.45 * IN), y, int(8.6 * IN), int(0.43 * IN), fill=CARD, edge=None)
        d.text(s, int(0.6 * IN), y, int(0.5 * IN), int(0.43 * IN), cid, size=11, color=K_KEYWORD, font=MONO, anchor=MSO_ANCHOR.MIDDLE)
        d.text(s, int(1.15 * IN), y, int(5.0 * IN), int(0.43 * IN), need, size=12, anchor=MSO_ANCHOR.MIDDLE, light=True)
        d.text(s, int(6.3 * IN), y, int(2.7 * IN), int(0.43 * IN), see, size=11, color=INDIGO, anchor=MSO_ANCHOR.MIDDLE)
    d.notes(s, """
So let's be specific. If you were going to let agents ship your software, these are the seven things you'd want proven. Each one is one of Maya's failures.
I'll show every one of them live, and the console keeps score: a criterion only ticks when the system recorded the evidence.
""")

    # 13. Demo marker
    s = d.slide(d.plain)
    d.box(s, int(3.0 * IN), int(2.05 * IN), int(4.0 * IN), int(1.05 * IN), fill=RGBColor(0x14, 0x14, 0x14), edge=GREEN, shape=MSO_SHAPE.ROUNDED_RECTANGLE, width=1.5)
    d.text(s, int(3.0 * IN), int(2.05 * IN), int(4.0 * IN), int(1.05 * IN), "Demo time", size=32, bold=True, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    d.notes(s, """
Do: switch to the console, press 4, click Start a scenario, choose "Dependency bump that breaks the build". Show the bump PR on GitHub with the agent's Required check pending.
Watch the run appear from the webhook; the tests fail, the coding agent diagnoses why from the changelog.
Do: while it fixes the code, kill the worker. Point at the yellow banner. Start it again; the agent carries on. Optionally Inject 429 on its card.
Do: show the PR: the diagnosis comment, the agent's commit on the bump branch, the rewritten description, the review, the green check, the locked merge button.
Do: Start a scenario, "A PR for the agent to review". Show the inline review. Reply "Please fix the rounding" on the PR; watch it in the Conversation tab, fixed and re-reviewed.
Do: Start a scenario, an open-ended issue ("Let customers save items for later"). Answer the planner on the issue, reply /approve.
Do: while the coders work, comment on the issue to steer them; watch it become their next turn.
Do: at the review window, comment on the PR. At staging, open it, then reply /ship on the issue. While the canary bakes, click Break the canary.
Watch the rollback branch appear, the revert PR open, and the issue reopen.
Do: press 5, Roll out v2 during a second issue run. Then press 6 for the scorecard.
""")

    # The headline scenario: a broken dependency bump, repaired and explained on the PR
    s = d.slide()
    d.headline(s, "A broken bump, fixed and explained by an agent.")
    flow = [("BUMP PR\nopened by an Action", GREEN, MSO_SHAPE.RECTANGLE), ("TESTS\nfail", CYAN, MSO_SHAPE.RECTANGLE),
            ("DIAGNOSE\nfailures + changelog", PURPLE, MSO_SHAPE.OVAL), ("FIX\non the bump branch", PURPLE, MSO_SHAPE.OVAL),
            ("EXPLAIN\nrewrite the PR", CYAN, MSO_SHAPE.RECTANGLE), ("REVIEW\nsecond model", PURPLE, MSO_SHAPE.OVAL),
            ("✓ READY\nTO MERGE", GREEN, MSO_SHAPE.RECTANGLE)]
    for i, (label, color, shape) in enumerate(flow):
        x = 0.45 + i * 1.3
        d.label_box(s, int(x * IN), int(1.7 * IN), int(1.12 * IN), int(0.8 * IN), label, edge=color, size=7, shape=shape)
        if i:
            d.arrow(s, int((x - 0.18) * IN), int(2.1 * IN), int(x * IN), int(2.1 * IN))
    d.text(s, int(0.45 * IN), int(2.85 * IN), int(8.6 * IN), int(1.2 * IN), [
        [("The agent's required check stays pending until it's done. ", {"size": 12}),
         ("The PR ends up explaining what changed in the dependency, what broke, what we changed and why, and how to verify.", {"size": 12, "color": INDIGO})],
    ])
    d.text(s, int(0.45 * IN), int(4.1 * IN), int(8.6 * IN), int(0.5 * IN),
           "One Workflow per PR, started by GitHub's webhook · each agent a child Workflow · each model and tool call an Activity",
           size=9, color=MUTED, font=MONO)
    d.notes(s, """
This is the everyday version of agentic SDLC. Dependabot tells you a bump is available; when it breaks the build, the PR sits red until someone has time.
Here GitHub's webhook starts a Workflow for the PR. It runs the tests. They fail, so a coding agent works out why from the failures and the package's changelog before touching anything, fixes our code on the bump branch until the tests pass, and rewrites the PR so the human who merges it knows exactly what happened. A second model reviews it.
The agent's check is required, so nobody merges the upgrade while the agents are still on it. And if the worker dies mid-fix, nothing is lost.
""")

    # 14–19. One slide per beat: the behaviour first, then the feature
    beats = [
        ("D1", "A crashed worker cost seconds, not the agent's work.", "d1-agents.jpg",
         "We killed the worker while a coding agent was fixing a broken upgrade. A new worker picked it up from its last completed step. Every edit was already pushed to the PR branch.",
         "Activities with heartbeats · Event History · a retried Activity re-attaches to the same agent turn",
         "Each model call and tool call is an Activity, so the ones that finished are in the agent's Event History and never run twice. That's the difference between a crash costing seconds and costing the whole task."),
        ("D2", "People approved the plan and the release, on their own schedule.", "d2-gate.jpg",
         "The run waited for /approve and /ship on the issue, and the agent's required check held each PR. No process held the waits open: they cost nothing and would have survived a restart or a deploy, for minutes or for days.",
         "Durable waits · Signals from GitHub webhooks · a required commit status",
         "A reply on GitHub reaches the Workflow as a Signal; nothing polls. Waiting is free, so you put the gate where accountability belongs, not where it's cheap."),
        ("D3", "Agents worked in parallel and retried through rate limits.", "d3-lanes.jpg",
         "Two coding agents built on separate branches at once. One was rate limited and retried while the other kept working. A per-run budget pauses the run for a person when spend reaches it.",
         "Child Workflows · Retry Policies (incl. Retry-After) · a budget the Workflow enforces",
         "There's no retry loop in this code. Provider SDK retries are off, so every retry is a visible Activity attempt with backoff, and GitHub's Retry-After becomes the next retry delay."),
        ("D4", "A bad canary was rolled back without anyone paging in.", "d4-rollback.jpg",
         "The canary failed its checks. The pipeline stopped its traffic, reverted the merge through a PR, and reopened the issue with the findings.",
         "Durable timers · a saga of compensations",
         "The bake is a durable timer, so a restart doesn't reset it. The rollback is a saga: each compensation is an Activity with its own retries, run in order until it's done."),
        ("D5", "The pipeline changed while work was in flight.", "d5-versions.jpg",
         "We rolled out v2, which adds a security-review agent, in the middle of a run. That run finished on v1; the next run got the new stage.",
         "Worker Versioning · Pinned Workflows · Upgrade-on-Continue-as-New for the long-lived entity",
         "Each run is pinned to the build it started on, so changing agents, prompts or stages never lands on a run halfway. Nothing is patched in place."),
        ("D6", "Every step is on the record.", "d6-history.jpg",
         "Every model call, tool call, approval, retry and compensation is in Event History, in order, with inputs and outputs.",
         "Event History",
         "That's the audit trail Maya didn't have on Friday morning, and nobody had to build it."),
        ("D7", "Developers steered the agents, and nobody started over.", "d7-conversation.jpg",
         "The planner asked questions on the issue. A comment on the issue reached the coders mid-task. Comments on PRs were fixed and re-reviewed; a new requirement sent the run back to planning.",
         "Signals from GitHub webhooks · the Workflow routes each comment to the right agent",
         "People aren't just approvers here. They answer the planner, steer the coders, and comment on PRs, all on GitHub where they already work. Feedback gets fixed on the spot; a new requirement voids the plan approval and goes back through planning, and nothing that already happened is lost."),
    ]
    for cid, claim, img, what, feature, note in beats:
        s = d.slide()
        d.text(s, int(0.45 * IN), int(0.32 * IN), int(1 * IN), int(0.3 * IN), cid, size=10, color=K_KEYWORD, font=MONO)
        d.headline(s, claim, y=0.55 * IN)
        from PIL import Image

        iw, ih = Image.open(ASSETS / img).size
        if iw / ih > 2.5:
            # A wide strip (the graph, the version table): full width, then the text in two columns.
            d.fit_image(s, img, int(0.45 * IN), int(1.45 * IN), int(8.6 * IN), int(1.6 * IN))
            text_y, col1, col2, cw = 3.3, 0.45, 5.0, 4.1
            d.text(s, int(col1 * IN), int(text_y * IN), int(cw * IN), int(0.3 * IN), "What you saw", size=9, color=MUTED, font=MONO)
            d.text(s, int(col1 * IN), int((text_y + 0.27) * IN), int(cw * IN), int(1.3 * IN), what, size=11, light=True)
            d.text(s, int(col2 * IN), int(text_y * IN), int(cw * IN), int(0.3 * IN), "How", size=9, color=MUTED, font=MONO)
            d.text(s, int(col2 * IN), int((text_y + 0.27) * IN), int(cw * IN), int(1.0 * IN), feature, size=11, color=GREEN)
        else:
            d.fit_image(s, img, int(0.45 * IN), int(1.45 * IN), int(5.4 * IN), int(3.45 * IN))
            d.text(s, int(6.1 * IN), int(1.45 * IN), int(3.4 * IN), int(0.3 * IN), "What you saw", size=9, color=MUTED, font=MONO)
            d.text(s, int(6.1 * IN), int(1.72 * IN), int(3.4 * IN), int(1.5 * IN), what, size=11, light=True)
            d.text(s, int(6.1 * IN), int(3.35 * IN), int(3.4 * IN), int(0.3 * IN), "How", size=9, color=MUTED, font=MONO)
            d.text(s, int(6.1 * IN), int(3.62 * IN), int(3.4 * IN), int(1.0 * IN), feature, size=10, color=GREEN)
        d.notes(s, f"{claim}\n{what}\n{note}")

    # 20. Code
    s = d.slide()
    d.headline(s, "The whole lifecycle is one Workflow method you can read top to bottom.")
    code = [
        ("async def run(self, request: RunRequest) -> RunState:", "def"),
        ("    repo = await self._open_issue()", ""),
        ("    plan = await self._plan(repo)              # planner agent, then a person approves", ""),
        ("    branches = await self._build(repo, plan)   # one coding agent per subtask, in parallel", ""),
        ("    pr = await self._open_pull_request(repo, plan, branches)", ""),
        ("    await self._review(repo, plan, pr)         # a second model reviews", ""),
        ("    merge_sha = await self._merge(repo, pr)", ""),
        ("    await self._deploy_to_staging(merge_sha)", ""),
        ('    await self._gate("production", "Ship this change to production?", ...)', ""),
        ("    shipped = await self._canary(repo, merge_sha)  # promote, or the rollback saga", ""),
        ("    await self._finish(repo, shipped)", ""),
    ]
    d.box(s, int(0.45 * IN), int(1.3 * IN), int(8.6 * IN), int(3.3 * IN), fill=CODE_BG, edge=CARD_EDGE)
    for i, (line, _kind) in enumerate(code):
        y = int((1.45 + i * 0.27) * IN)
        indent = len(line) - len(line.lstrip(" "))
        body = line.strip()
        code_part, _, comment = body.partition("  #")
        runs = []
        for token in _tokens(code_part):
            runs.append(token)
        if comment:
            runs.append(("  #" + comment, {"color": K_COMMENT}))
        d.text(s, int((0.65 + indent * 0.075) * IN), y, int(8.2 * IN), int(0.27 * IN), [runs], size=10, font=MONO)
    d.text(s, int(0.45 * IN), int(4.7 * IN), int(8.6 * IN), int(0.3 * IN),
           "agentic_sdlc/pipeline/workflow.py · abridged · github.com/temporal-sa/temporal-demo-agentic-sdlc", size=8, color=MUTED)
    d.notes(s, """
This is the actual pipeline, abridged. It's ordinary Python. Each line can take seconds or days: the plan line waits for a person, the build line runs agents in parallel, the canary line bakes on a durable timer.
What isn't here is just as important: no job queue, no state table, no retry loops, no resume logic. That's what Temporal gives you.
""")

    # 21. Customer story
    s = d.slide()
    d.headline(s, "Duolingo runs its coding agents on Temporal.")
    d.box(s, int(0.45 * IN), int(1.4 * IN), int(5.4 * IN), int(2.6 * IN), fill=CARD, edge=None)
    d.box(s, int(0.45 * IN), int(1.4 * IN), int(0.05 * IN), int(2.6 * IN), fill=PINK, edge=None)
    d.text(s, int(0.7 * IN), int(1.55 * IN), int(5.0 * IN), int(2.4 * IN), [
        "“We built this agent on the workflow orchestration service Temporal using the Codex CLI, a pattern we intend to extend to future agents.”",
        "",
        "“Even a well prompted agent may go off the rails and crash, fail to produce any changes, hang/freeze, or otherwise fail to complete, and need to be retried.”",
    ], size=12, light=True)
    d.text(s, int(6.2 * IN), int(1.4 * IN), int(3.3 * IN), int(2.6 * IN), [
        [("The agent", {"bold": True, "color": INDIGO, "size": 11})],
        "Removes stale feature flags: clone the repo, run Codex CLI, open a PR.",
        "",
        [("Why Temporal", {"bold": True, "color": INDIGO, "size": 11})],
        "Agents that crash, hang or produce nothing get retried by the Workflow, not by a person.",
    ], size=11)
    d.source(s, "duolingo", y=int(4.6 * IN))
    d.notes(s, "This isn't hypothetical. Duolingo's feature-flag-removal agent runs Codex CLI inside a Temporal Workflow, for exactly the reason in their second quote.")

    # 22. Recap
    s = d.slide()
    d.headline(s, "What Maya's Friday morning looks like now.")
    recap = [
        "A crashed worker cost seconds, not the agent's work.",
        "People approved the plan and the release, on their own schedule.",
        "Agents worked in parallel and retried through rate limits, inside a budget.",
        "A bad canary was rolled back and reported without anyone paging in.",
        "The pipeline changed while work was in flight.",
        "Developers steered the agents from GitHub, and nobody started over.",
        "Every step is on the record.",
    ]
    for i, line in enumerate(recap):
        y = int((1.3 + i * 0.46) * IN)
        d.text(s, int(0.5 * IN), y, int(0.5 * IN), int(0.45 * IN), str(i + 1), size=18, color=GREEN, light=True)
        d.text(s, int(1.0 * IN), y + int(0.05 * IN), int(8.0 * IN), int(0.45 * IN), line, size=15, light=True)
    d.text(s, int(0.5 * IN), int(4.55 * IN), int(8.6 * IN), int(0.4 * IN),
           "Replace “storefront issue” with your migration backlog, your dependency upgrades, your incident follow-ups.", size=11, color=INDIGO)
    d.notes(s, """
So here's Maya's Friday morning with Temporal underneath.
Read the seven lines.
And replace storefront issue with whatever you'd give agents first: a migration backlog, dependency upgrades, incident follow-ups. The shape is the same.
""")

    # 23. Resources
    s = d.slide()
    d.headline(s, "Take it with you.")
    cards = [
        ("Demo repository", "github.com/temporal-sa/temporal-demo-agentic-sdlc", "Pipeline, agents, console, RUNBOOK"),
        ("Agent harness", "github.com/temporal-sa/temporal-agent-harness", "Durable agents on Temporal with Pydantic AI"),
        ("Pydantic AI + Temporal", "ai.pydantic.dev/durable_execution/temporal", "Model and tool calls as Activities"),
        ("Worker Versioning", "docs.temporal.io/worker-versioning", "Change code while work is in flight"),
    ]
    for i, (title, url, what) in enumerate(cards):
        x = int((0.45 + (i % 2) * 4.35) * IN)
        y = int((1.35 + (i // 2) * 1.6) * IN)
        d.box(s, x, y, int(4.2 * IN), int(1.4 * IN), fill=CARD, edge=CARD_EDGE)
        d.box(s, x + int(0.2 * IN), y + int(0.25 * IN), int(0.04 * IN), int(0.9 * IN), fill=GREEN if i % 2 else UV, edge=None)
        d.text(s, x + int(0.4 * IN), y + int(0.22 * IN), int(3.7 * IN), int(0.35 * IN), title, size=15, bold=True)
        d.text(s, x + int(0.4 * IN), y + int(0.6 * IN), int(3.7 * IN), int(0.3 * IN), what, size=10, color=INDIGO)
        d.text(s, x + int(0.4 * IN), y + int(0.9 * IN), int(3.7 * IN), int(0.3 * IN), url, size=9, color=MUTED, font=MONO)
    d.notes(s, "Everything you saw is open: the demo, the harness, and the docs for the two features that carried most of the weight.")

    # 24. Sources
    s = d.slide()
    d.headline(s, "Sources")
    lines = [[(label + "  ", {"size": 9}), (url, {"size": 8, "color": MUTED, "font": MONO})] for label, url in SOURCES.values()]
    lines.append([("Demo figures (slide 2 and the live run) come from the running system; model cost is estimated from token counts with the genai-prices database.", {"size": 9, "color": INDIGO})])
    d.text(s, int(0.45 * IN), int(1.25 * IN), int(8.6 * IN), int(3.6 * IN), lines, size=9, line_spacing=1.35)
    d.notes(s, "All quotes are verbatim from the sources listed. docs/story.md in the repo has the full research.")


def _tokens(code: str):
    """Very small Python highlighter for the code slide, using guide 14's palette."""
    import re

    out = []
    pattern = re.compile(r'("[^"]*"|\b(?:async|await|def|return)\b|\b[a-z_][a-z0-9_]*(?=\()|[^"\w]+|\w+)')
    for match in pattern.finditer(code):
        tok = match.group(0)
        if tok.startswith('"'):
            out.append((tok, {"color": GREEN}))
        elif tok in ("async", "await", "def", "return"):
            out.append((tok, {"color": K_KEYWORD}))
        elif re.fullmatch(r"[a-z_][a-z0-9_]*", tok) and code[match.end():match.end() + 1] == "(":
            out.append((tok, {"color": K_FUNC}))
        else:
            out.append((tok, {"color": WHITE}))
    return out


def render(pptx: Path) -> None:
    """Render to PDF with LibreOffice, then to PNGs, with fontconfig pointed at the real font
    folders (guide 14: the macOS LibreOffice build otherwise sees only its bundled fonts)."""
    png_dir = OUT / "png"
    shutil.rmtree(png_dir, ignore_errors=True)
    png_dir.mkdir(parents=True)
    conf = Path(tempfile.mkdtemp()) / "fonts.conf"
    conf.write_text(f"""<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig>
<dir>{Path.home()}/Library/Fonts</dir><dir>/Library/Fonts</dir><dir>/System/Library/Fonts</dir>
<dir>/opt/homebrew/Caskroom</dir>
<alias><family>Noto Sans Mono Medium</family><prefer><family>Noto Sans Mono</family></prefer></alias>
<alias><family>Noto Sans Mono SemiBold</family><prefer><family>Noto Sans Mono</family></prefer></alias>
<alias><family>Inter Light</family><prefer><family>Inter</family></prefer></alias>
</fontconfig>""")
    env = {**os.environ, "FONTCONFIG_FILE": str(conf)}
    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(OUT), str(pptx)], check=True, env=env,
                   capture_output=True)
    pdf = OUT / (pptx.stem + ".pdf")
    subprocess.run(["pdftoppm", "-png", "-r", "110", str(pdf), str(png_dir / "slide")], check=True)
    print(subprocess.run(["pdffonts", str(pdf)], capture_output=True, text=True).stdout)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--render", action="store_true")
    args = parser.parse_args()
    deck = Deck()
    build(deck)
    out = OUT / "agentic-sdlc.pptx"
    deck.finish(out)
    print(f"wrote {out} ({len(deck.prs.slides)} slides)")
    if args.render:
        render(out)


if __name__ == "__main__":
    main()
