import asyncio
import logging
from logging.handlers import QueueHandler, QueueListener
import queue
import re

import playwright.async_api
from playwright.async_api import async_playwright
from PySide6.QtWidgets import QApplication
import qasync

from drawing import handle_draw
from guessing import close_guess_dialog, handle_guess


async def route_handler(route: playwright.async_api.Route):
    if re.match(
        r"^(?:https?:\/\/)(skribbl\.io|fonts\.googleapis\.com|fonts\.gstatic\.com|cdn\.jsdelivr\.net).*",
        route.request.url,
    ):
        await route.continue_()
    else:
        await route.abort()


async def main():
    loop = asyncio.get_event_loop()
    async with async_playwright() as p:
        link = await loop.run_in_executor(None, input)
        if not link:
            link = "https://skribbl.io"
        browser = await p.firefox.launch(headless=False)
        context = await browser.new_context(
            no_viewport=True,
        )
        page = await context.new_page()
        await page.route("**/*", route_handler)
        await page.goto(link)
        # Force inject floating buttons directly into rendered page
        await page.expose_function("pythonCapture", lambda: asyncio.create_task(handle_guess(page)))
        await page.expose_function("pythonCaptureDraw", lambda: asyncio.create_task(handle_draw(page)))
        await page.evaluate("""() => {
            const makeButton = (id, label, top, color, onClick) => {
                const btn = document.createElement('button');
                btn.id = id;
                btn.innerText = label;
                btn.style.cssText = `position:fixed;top:${top}px;right:20px;z-index:999999999;padding:15px 30px;background:${color};color:#fff;font-size:18px;font-weight:bold;border:none;border-radius:10px;cursor:pointer;box-shadow:0 0 15px rgba(0,0,0,0.5);`;
                btn.onclick = onClick;
                document.body.appendChild(btn);
            };
            makeButton('skriblai-solve', '⚡ SOLVE GAME', 20, '#2563eb', () => window.pythonCapture());
            makeButton('skriblai-draw', '🎨 AI DRAW', 85, '#16a34a', () => window.pythonCaptureDraw());
        }""")

        await page.wait_for_event("close", timeout=0)
        close_guess_dialog()


if __name__ == "__main__":
    log_queue = queue.Queue(-1)
    queue_handler = QueueHandler(log_queue)
    handler = logging.FileHandler("latest.log")
    listener = QueueListener(log_queue, handler)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(queue_handler)
    formatter = logging.Formatter("[%(asctime)s] [%(threadName)s/%(levelname)s] [%(name)s] %(message)s")
    handler.setFormatter(formatter)
    listener.start()
    root.warning("Woah!")

    app = QApplication([])
    # The guess dialog is the only Qt window; closing it must not quit the
    # event loop while main() (Playwright) is still running.
    app.setQuitOnLastWindowClosed(False)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    with loop:
        loop.run_until_complete(main())
