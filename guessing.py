import asyncio
import logging
import httpx
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QVBoxLayout

from config import HEADERS, OPENROUTER_URL
from drawing import is_drawing

MODEL = "google/gemini-3.6-flash"
PROMPT = """
You are playing skribbl.io. Guess the word.
Format:
Output your top 5 guesses, one on each line, with no additional text.

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

_current_dialog: "GuessDialog | None" = None


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


def close_guess_dialog():
    """Close the current guess dialog if one is active."""
    global _current_dialog
    if _current_dialog is not None:
        _current_dialog.close()
        _current_dialog = None


async def submit_guess(page, box, guess):
    """Fill and enter a guess into the chat box, returning True if correct."""
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


async def request_guesses(data_url: str, hints: str, word_length: str) -> list[str]:
    """Send canvas image and hints to the LLM to get candidate guesses."""
    words = f"The answer has {len(word_length.split())} words. " + "".join(
        f"Word {i} has {length} letters. " for i, length in enumerate(word_length.split(), start=1)
    )
    logging.info("Hint: " + hints)
    logging.info("Words: " + words)
    async with httpx.AsyncClient() as client:
        logging.info("Sending response to API...")
        response = await client.post(
            timeout=None,
            url=OPENROUTER_URL,
            headers=HEADERS,
            json={
                "model": MODEL,
                "service_tier": "flex",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": PROMPT + words + "\nHint: " + hints,
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": data_url,
                                },
                            },
                        ],
                    },
                ],
                "reasoning": {"effort": "minimal"},
                "provider": {
                    "only": ["google-ai-studio"],
                },
            },
        )
        logging.info("Response received!")
        resp = response.json()
        content = resp["choices"][0]["message"]["content"]
        guesses = content.split("\n")
        logging.info("Guesses: " + str(guesses))
        return guesses


async def guess_turn(page):
    """Capture canvas and hints, ask the LLM for guesses, and show selection dialog."""
    global _current_dialog
    if is_drawing():
        logging.info("Skipping the guess solver, we are drawing right now.")
        return
    logging.info("Captured!")
    data_url = await page.evaluate("document.querySelector('canvas').toDataURL('image/png')")
    hints_text = await page.locator(".hints").text_content() or ""
    word_length_text = await page.locator(".word-length").text_content() or ""
    hints = hints_text[:-len(word_length_text)] if len(word_length_text) > 0 else hints_text

    guesses = await request_guesses(data_url, hints, word_length_text)
    box = page.locator("#game-chat").locator("input").first
    _current_dialog = GuessDialog(
        guesses,
        f"Hint: {hints}\nWord lengths: {word_length_text}",
    )
    _current_dialog.show()
    try:
        while True:
            choice = await _current_dialog.next_choice()
            if choice is None:
                break
            if await submit_guess(page, box, choice):
                break
    finally:
        if _current_dialog is not None:
            _current_dialog.close()
            _current_dialog = None


capture = guess_turn


async def handle_guess(page):
    """Wrapper that catches and logs any exceptions during guess handling."""
    try:
        await guess_turn(page)
    except Exception as e:
        logging.exception(e)


capture_catch = handle_guess
