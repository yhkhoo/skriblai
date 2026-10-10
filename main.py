import httpx
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import playwright.async_api
import asyncio
import logging
from logging.handlers import QueueHandler, QueueListener
import queue
import re
from os import getenv
import qasync
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton, QVBoxLayout

OPENROUTER_API_KEY = getenv("OPENROUTER_FREE_KEY")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
HEADERS = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "Content-Type": "application/json",
}
MODEL = "google/gemini-3.5-flash-lite"
PROMPT = """
You are playing skribbl.io. Guess the word.
Format:
Output your top 3 guesses, one on each line, with no additional text.

"""
SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "skribbl_output",
        "strict": True,
        "schema": {
            "type": "object",
        },
        "properties": {
            "guess1": {
                "type": "string"
            },
            "guess2": {
                "type": "string"
            },
            "guess3": {
                "type": "string"
            },
        }
    },
}

class GuessDialog(QDialog):
    """Non-modal dialog with one button per guess.

    Stays open across submissions; each click resolves the future returned
    by next_choice() with the chosen guess. Cancel/close resolves it with None.
    """

    def __init__(self, guesses, hint, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose a guess")
        self._future = None
        self._pending = []
        layout = QVBoxLayout(self)
        hint_label = QLabel(hint)
        hint_label.setWordWrap(True)
        layout.addWidget(hint_label)
        layout.addWidget(QLabel("Click a guess to submit it:"))
        for guess in guesses:
            button = QPushButton(guess)
            button.clicked.connect(lambda checked=False, g=guess: self._resolve(g))
            layout.addWidget(button)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self._cancel)
        layout.addWidget(cancel)

    def next_choice(self):
        """Await this to get the next guess clicked (or None if cancelled)."""
        loop = asyncio.get_running_loop()
        if self._pending:
            future = loop.create_future()
            future.set_result(self._pending.pop(0))
            return future
        self._future = loop.create_future()
        return self._future

    def _resolve(self, value):
        if self._future is None or self._future.done():
            # A click arrived while we're submitting or between choices.
            self._pending.append(value)
            return
        future, self._future = self._future, None
        future.get_loop().call_soon_threadsafe(future.set_result, value)

    def _cancel(self):
        self._resolve(None)
        self.close()

    def closeEvent(self, event):
        self._resolve(None)
        super().closeEvent(event)

async def route_handler(route: playwright.async_api.Route):
    if re.match(r"^(?:https?:\/\/)(skribbl\.io|fonts\.googleapis\.com|fonts\.gstatic\.com|cdn\.jsdelivr\.net).*", route.request.url):
        await route.continue_()
    else:
        await route.abort()

async def submit_guess(page, box, guess):
    logging.info(f"Guessing: {guess}")
    await box.fill(guess)
    await box.press("Enter")
    guessed = page.locator(".guessed:has(.me)")
    try:
        await guessed.wait_for(state="attached", timeout=1000)
        logging.info("Guessed correctly!")
        return True
    except PlaywrightTimeoutError:
        logging.info("Incorrect guess.")
        return False

async def main():
    loop = asyncio.get_event_loop()
    guess_dialog = None
    async with async_playwright() as p:
        browser = await p.firefox.launch(headless=False)
        context = await browser.new_context(
            no_viewport=True
        )
        page = await context.new_page()
        await page.route("**/*", route_handler)
        link = await loop.run_in_executor(None, input)
        if not link:
            link = "https://skribbl.io"
        await page.goto(link)
        # Force inject floating button directly into rendered page
        await page.expose_function("pythonCapture", lambda: asyncio.create_task(capture_catch()))
        await page.evaluate("""() => {
            const btn = document.createElement('button');
            btn.innerText = '⚡ SOLVE GAME';
            btn.style.cssText = 'position:fixed;top:20px;left:50%;transform:translateX(-50%);z-index:999999999;padding:15px 30px;background:#2563eb;color:#fff;font-size:18px;font-weight:bold;border:none;border-radius:10px;cursor:pointer;box-shadow:0 0 15px rgba(0,0,0,0.5);';
            btn.onclick = () => window.pythonCapture();
            document.body.appendChild(btn);
        }""")

        async def capture_catch():
            try:
                await capture()
            except Exception as e:
                logging.exception(e)

        async def capture():
            nonlocal guess_dialog
            logging.info("Captured!")
            data_url = await page.evaluate("document.querySelector('canvas').toDataURL('image/png')")
            hints = await page.locator(".hints").text_content()
            word_length = await page.locator(".word-length").text_content()
            hints = hints[:-len(word_length)]
            logging.info("Hint: " + hints)
            words = f"The answer has {len(word_length.split())} words. " + "".join(f"Word {i} has {length} letters. " for i, length in enumerate(word_length.split(), start=1))
            logging.info("Words: " + words)
            async with httpx.AsyncClient() as client:
                logging.info("Sending response to API...")
                response = await client.post(
                    timeout=None,
                    url=OPENROUTER_URL,
                    headers=HEADERS,
                    json={
                        "model": MODEL,
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": PROMPT + words + "\nHint: " + hints
                                    },
                                    {
                                        "type": "image_url",
                                        "image_url": {
                                            "url": data_url
                                        }
                                    },
                                ]
                            },
                        ],
                        "reasoning": {"effort": "minimal"},
                    }
                )
                logging.info("Response received!")
                resp = response.json()
                content = resp["choices"][0]["message"]["content"]
                guesses = content.split('\n')
                logging.info("Guesses: " + str(guesses))
                box = page.locator("#game-chat").locator("input").first
                guess_dialog = GuessDialog(
                    guesses,
                    f"Hint: {hints}\nWord lengths: {word_length}",
                )
                guess_dialog.show()
                try:
                    while True:
                        choice = await guess_dialog.next_choice()
                        if choice is None:
                            break
                        if await submit_guess(page, box, choice):
                            break
                finally:
                    guess_dialog.close()
                    guess_dialog = None

        await page.wait_for_event("close", timeout=0)
        if guess_dialog is not None:
            guess_dialog.close()

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
