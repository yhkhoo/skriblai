import httpx
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import playwright.async_api
from pynput import keyboard
import asyncio
import logging
from logging.handlers import QueueHandler, QueueListener
import queue
import re
from os import getenv

OPENROUTER_API_KEY = getenv("OPENROUTER_API_KEY")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
HEADERS = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "Content-Type": "application/json",
}
MODEL = "google/gemma-4-31b-it:free"
PROMPT = """
You are playing skribbl.io. Given a drawing and hint, analyse them to guess the word.
Format:
Output your top 3 guesses, one on each line, with no additional text.
An example of a hint (not the actual hint): 'h__ ___'. This hint means that the answer is comprised of two words, each being 3 letters long, and the first letter of the first word is an 'h'.
Your answer MUST:
- Match the number of blanks exactly
- Use any revealed letters in their correct positions
- Be a common English word or phrase (skribbl.io uses common words)
Words are not just nouns, they can be verbs, adjectives, adverbs, but are always from everyday vocabulary.
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

async def route_handler(route: playwright.async_api.Route):
    if re.match(r"^(?:https?:\/\/)(skribbl\.io|fonts\.googleapis\.com|fonts\.gstatic\.com|cdn\.jsdelivr\.net).*", route.request.url):
        await route.continue_()
    else:
        await route.abort()

async def main():
    loop = asyncio.get_event_loop()
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge", headless=False)
        page = await browser.new_page()
        await page.route("**/*", route_handler)
        link = await loop.run_in_executor(None, input)
        if not link:
            link = "https://skribbl.io"
        await page.goto(link)

        async def capture_catch():
            try:
                await capture()
            except Exception as e:
                logging.exception(e)

        async def capture():
            logging.info("Captured!")
            data_url = await page.evaluate("document.querySelector('canvas').toDataURL('image/png')")
            hints = await page.locator(".hints").text_content()
            word_length = await page.locator(".word-length").text_content()
            hints = hints[:-len(word_length)]
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
                                        "text": PROMPT + "\n\n Hint: " + hints
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
                        "reasoning": {"enabled": False},
                    }
                )
                logging.info("Response received!")
                resp = response.json()
                content = resp["choices"][0]["message"]["content"]
                guesses = content.split('\n')
                logging.info("Guesses: " + str(guesses))
                box = page.locator("#game-chat").locator("input").first
                for guess in guesses:
                    logging.info(f"Guessing: {guess}")
                    await box.fill(guess)
                    await box.press("Enter")
                    guessed = page.locator(".guessed:has(.me)")
                    try:
                        await guessed.wait_for(state="attached", timeout=500)
                        logging.info("Guessed correctly!")
                        break
                    except PlaywrightTimeoutError:
                        logging.info("Incorrect guess.")

        hotkey = keyboard.GlobalHotKeys({'<ctrl>+<f1>': lambda: asyncio.run_coroutine_threadsafe(capture_catch(), loop)})
        hotkey.start()
        while hotkey.is_alive():
            await asyncio.sleep(0.1)

if __name__ == "__main__":
    que = queue.Queue(-1)
    queue_handler = QueueHandler(que)
    handler = logging.FileHandler("latest.log")
    listener = QueueListener(que, handler)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(queue_handler)
    formatter = logging.Formatter("[%(asctime)s] [%(threadName)s/%(levelname)s] [%(name)s] %(message)s")
    handler.setFormatter(formatter)
    listener.start()
    root.warning("Woah!")

    asyncio.run(main())
