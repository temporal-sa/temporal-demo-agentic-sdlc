"""Screenshot the console, for visual review.

    uv run --with playwright python scripts/screenshot.py out.png [--act N] [--run RUN_ID]

Uses the installed Google Chrome, so no browser download is needed.
"""

import argparse
import asyncio

from playwright.async_api import async_playwright

parser = argparse.ArgumentParser()
parser.add_argument("out")
parser.add_argument("--base", default="http://localhost:8000")
parser.add_argument("--act", type=int, default=4)
parser.add_argument("--run", default=None)
parser.add_argument("--width", type=int, default=1920)
parser.add_argument("--height", type=int, default=1080)
parser.add_argument("--click", default=None, help="text of a button to click before the screenshot")
args = parser.parse_args()


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome")
        context = await browser.new_context(viewport={"width": args.width, "height": args.height})
        page = await context.new_page()
        await page.goto(args.base + (f"/?run={args.run}" if args.run else "/"))
        await page.wait_for_timeout(2500)
        await page.keyboard.press(str(args.act))
        if args.click:
            await page.get_by_text(args.click, exact=True).first.click()
        await page.wait_for_timeout(2500)
        await page.screenshot(path=args.out)
        await browser.close()


asyncio.run(main())
