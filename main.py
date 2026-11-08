import httpx
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import playwright.async_api
import asyncio
import logging
from logging.handlers import QueueHandler, QueueListener
import json
import queue
import re
from os import getenv
import qasync
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton, QVBoxLayout

OPENROUTER_API_KEY = getenv("OPENROUTER_API_KEY")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
HEADERS = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "Content-Type": "application/json",
}
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

# Drawing side: the canvas has a fixed internal resolution of 800x600,
# everything the model outputs is expressed in that coordinate space.
CANVAS_WIDTH, CANVAS_HEIGHT = 800, 600
DRAW_MODEL = "openai/gpt-6-luna:floor"
MAX_DRAW_POINTS = 1600  # ~24s of drawing at the game's input throttle
# skribbl.io's 26 swatches in the order they appear in the DOM (the page fills
# the toolbar with a top row of even palette indices and a bottom row of odd
# ones - see js/game.js), so index i here is colors.nth(i) in the browser.
DEFAULT_PALETTE = [
    "#ffffff", "#c1c1c1", "#ef130b", "#ff7100", "#ffe400", "#00cc00",
    "#00ff91", "#00b2ff", "#231fd3", "#a300ba", "#df69a7", "#ffac8e",
    "#a0522d", "#000000", "#505050", "#740b07", "#c23800", "#e8a200",
    "#004619", "#00785d", "#00569e", "#0e0865", "#550069", "#873554",
    "#cc774d", "#63300d",
]
# Names for the default swatches, so the model can reason in "apple red"
# instead of raw hex values. Unknown colors (custom skins) just show the hex.
COLOR_NAMES = {
    "#ffffff": "White", "#c1c1c1": "Light Gray", "#ef130b": "Red",
    "#ff7100": "Orange", "#ffe400": "Yellow", "#00cc00": "Green",
    "#00ff91": "Mint", "#00b2ff": "Skyblue", "#231fd3": "Seablue",
    "#a300ba": "Purple", "#df69a7": "Pink",
    "#ffac8e": "Beige", "#a0522d": "Brown", "#000000": "Black",
    "#505050": "Gray", "#740b07": "Dark Red", "#c23800": "Dark Orange",
    "#e8a200": "Dark Yellow", "#004619": "Dark Green", "#00785d": "Dark Mint",
    "#00569e": "Dark Skyblue", "#0e0865": "Dark Seablue", "#550069": "Dark Purple",
    "#873554": "Dark Pink", "#cc774d": "Dark Beige", "#63300d": "Dark Brown",
}
DRAW_PROMPT = """
You are playing skribbl.io and it is your turn to draw the word: __WORD__.

Describe ONE simple pictionary-style line drawing of that word as stroke data.

Rules:
- The coordinate space is 800 x 600 pixels, origin top-left, x grows right, y grows down.
- Use 15 to 50 strokes. Each stroke is one continuous pen movement of 3 to 30 points.
- Every stroke is an object: {"points": [[x,y],...], "color": "#rrggbb"}.
- "color" MUST be one of these exact hex values: __PALETTE__.
- Avoid White (#ffffff): the canvas is white, so white strokes are invisible.
- Outlines only: no shading, no hatching, no filled areas, no guides.
- NEVER write letters, numbers or any other text (spelling the word is forbidden).
- Every coordinate must stay inside 0-800 / 0-600.
- Have fun with the drawing! You may add additional things for context or to improve the drawing.

Output ONLY a JSON object in exactly this shape, no markdown, no commentary:
{"strokes": [{"points": [[x,y],[x,y],...], "color": "#000000"}, ...]}

Example for the word "house":
{"strokes": [{"points": [[140,470],[140,270],[400,130],[660,270],[660,470],[140,470]], "color": "#000000"}, {"points": [[320,470],[320,330],[500,330],[500,470]], "color": "#a0522d"}, {"points": [[180,300],[260,300],[260,370],[180,370],[180,300]], "color": "#00b2ff"}]}
""".strip()
STROKES_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "skribbl_strokes",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "strokes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "points": {
                                "type": "array",
                                "items": {"type": "array", "items": {"type": "number"}},
                            },
                            "color": {"type": "string"},
                        },
                        "required": ["points", "color"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["strokes"],
            "additionalProperties": False,
        },
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

def hex_to_rgb(value):
    """Parse '#rgb' / '#rrggbb' into (r, g, b), or None if it isn't a color."""
    if not isinstance(value, str):
        return None
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) != 6 or not re.fullmatch(r"[0-9a-fA-F]{6}", text):
        return None
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)

def nearest_color(color, palette):
    """Index of the palette swatch closest to `color`, or None if it can't tell."""
    rgb = hex_to_rgb(color)
    if rgb is None or not palette:
        return None
    best, best_distance = None, None
    for i, entry in enumerate(palette):
        other = hex_to_rgb(entry)
        if other is None:
            continue
        distance = sum((a - b) ** 2 for a, b in zip(rgb, other))
        if best_distance is None or distance < best_distance:
            best, best_distance = i, distance
    return best

def describe_palette(palette):
    """Format the swatches for the prompt, e.g. 'Red (#ef130b), Black (#000000)'."""
    return ", ".join(
        f"{COLOR_NAMES[entry]} ({entry})" if entry in COLOR_NAMES else entry
        for entry in palette
    )

def parse_strokes_json(content):
    """Pull the strokes object out of the model's reply (tolerates code fences)."""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in the model output")
    return json.loads(text[start:end + 1])

def validate_strokes(data, palette, max_points=MAX_DRAW_POINTS, max_points_per_stroke=40):
    """Clamp, dedupe and budget the model's strokes.

    Each returned stroke is (points, swatch_index); colors are snapped to the
    closest entry of `palette` so an off-palette hex still lands on a real
    swatch. Raises ValueError when the output is too broken to draw - the
    caller retries with the failure fed back to the model.
    """
    strokes = data.get("strokes") if isinstance(data, dict) else None
    if not isinstance(strokes, list):
        raise ValueError("missing 'strokes' array")
    default_color = nearest_color("#000000", palette)
    if default_color is None:
        default_color = 0
    clean, total = [], 0
    for raw in strokes:
        color = None
        if isinstance(raw, dict):
            color = raw.get("color")
            raw = raw.get("points")
        if not isinstance(raw, list):
            continue
        points = []
        for p in raw:
            if not isinstance(p, (list, tuple)) or len(p) != 2:
                continue
            try:
                x, y = float(p[0]), float(p[1])
            except (TypeError, ValueError):
                continue
            x = round(min(CANVAS_WIDTH - 1, max(0, x)))
            y = round(min(CANVAS_HEIGHT - 1, max(0, y)))
            if points and points[-1] == (x, y):
                continue
            points.append((x, y))
        if len(points) < 2:
            continue
        if len(points) > max_points_per_stroke:
            step = -(-len(points) // max_points_per_stroke)  # ceil division
            last = points[-1]
            points = points[::step]
            if points[-1] != last:
                points.append(last)
        room = max_points - total
        if room < 2:
            break
        points = points[:room]
        color_index = nearest_color(color, palette)
        clean.append((points, default_color if color_index is None else color_index))
        total += len(points)
    if not clean:
        raise ValueError("no usable strokes in the model output")
    if len(clean) < 3:
        raise ValueError(f"only {len(clean)} usable strokes, drawing looks empty")
    return clean

async def generate_strokes(word, palette, attempts=2):
    """Ask the LLM to draw `word`.

    Returns (points, swatch_index) strokes in 800x600 canvas space, where the
    swatch index points into `palette`.
    """
    prompt = (
        DRAW_PROMPT
        .replace("__WORD__", word)
        .replace("__PALETTE__", describe_palette(palette))
    )
    messages = [{"role": "user", "content": prompt}]
    last_error = "unknown error"
    for attempt in range(attempts):
        payload = {
            "model": DRAW_MODEL,
            "messages": messages,
            "reasoning": {"effort": "medium"},
        }
        # Structured output first; the retry drops it in case the
        # provider rejects response_format outright.
        if attempt == 0:
            payload["response_format"] = {"type": "json_schema", "json_schema": STROKES_SCHEMA}
        content = None
        try:
            async with httpx.AsyncClient() as client:
                logging.info(f"Requesting drawing strokes (attempt {attempt + 1})...")
                response = await client.post(
                    timeout=60,
                    url=OPENROUTER_URL,
                    headers=HEADERS,
                    json=payload,
                )
            if response.status_code != 200:
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            else:
                content = response.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
            last_error = f"{type(e).__name__}: {e}"
        if content:
            try:
                strokes = validate_strokes(parse_strokes_json(content), palette)
                logging.info("Stroke data is valid.")
                return strokes
            except ValueError as e:
                last_error = str(e)
                logging.warning(f"Bad stroke data: {last_error}")
                messages.append({"role": "assistant", "content": content})
        messages.append({
            "role": "user",
            "content": (
                f"That failed ({last_error}). Return ONLY a JSON object of the form "
                '{"strokes": [{"points": [[x,y],...], "color": "#rrggbb"}, ...]} '
                f"for the word {word}: 15-40 outline strokes, colors from "
                f"[{describe_palette(palette)}], 800x600 coordinates, no text."
            ),
        })
    raise RuntimeError(f"could not generate a drawing: {last_error}")

async def read_palette(page):
    """Read the toolbar's swatches as '#rrggbb' strings, in DOM order.

    The position in the returned list is the swatch index used everywhere
    else, so it must match the page's own ordering. Falls back to the known
    default palette if the toolbar can't be read.
    """
    palette = await page.evaluate("""() => {
        const toHex = (value) => {
            const m = (value || '').match(/\\d+(\\.\\d+)?/g);
            if (!m || m.length < 3) return null;
            return '#' + m.slice(0, 3)
                .map(n => Math.round(+n).toString(16).padStart(2, '0')).join('');
        };
        return Array.from(document.querySelectorAll('#game-toolbar .colors .color'))
            .map(el => toHex(getComputedStyle(el).backgroundColor) || '#000000');
    }""")
    if not palette:
        logging.warning("No color swatches found, falling back to the default palette.")
        palette = list(DEFAULT_PALETTE)
    return palette

async def setup_brush(page, palette, size_index=1):
    """Pick black and a medium brush - only takes effect during our own turn."""
    colors = page.locator("#game-toolbar .colors .color")
    swatch_count = await colors.count()
    if swatch_count:
        black = nearest_color("#000000", palette)
        if black is None or black >= swatch_count:
            black = 0
        await colors.nth(black).click()
        logging.info(f"Brush color: black (swatch {black})")
    sizes = page.locator("#game-toolbar .sizes .size")
    if not await sizes.count():
        return
    if not await sizes.first.is_visible():
        await page.locator("#game-toolbar .sizes .size-preview").click()
        try:
            await sizes.first.wait_for(state="visible", timeout=2000)
        except PlaywrightTimeoutError:
            # popup toggled closed instead of open - flip it and try once more
            await page.locator("#game-toolbar .sizes .size-preview").click()
            await sizes.first.wait_for(state="visible", timeout=2000)
    picked = min(size_index, await sizes.count() - 1)
    await sizes.nth(picked).click()
    logging.info(f"Brush size: #{picked}")

async def turn_active(page):
    """True while our word is showing and no word-choice overlay is up."""
    try:
        word = page.locator("#game-word .word")
        return await word.is_visible()
    except Exception:
        return False

async def draw_strokes(page, strokes, palette, delay=0.015):
    """Replay the colored polylines as mouse input on the game canvas.

    The client keeps at most ~90 points/second and batches 8 points per
    50ms to the server, so 15ms per point stays lossless. The toolbar swatch
    is only clicked when the color actually changes, and always between
    strokes - never while the pen is down.
    """
    canvas = page.locator("#game-canvas canvas")
    box = await canvas.bounding_box()
    if not box:
        raise RuntimeError("drawing canvas is not visible")
    scale_x = box["width"] / CANVAS_WIDTH
    scale_y = box["height"] / CANVAS_HEIGHT
    colors = page.locator("#game-toolbar .colors .color")
    swatch_count = await colors.count() if palette else 0
    # setup_brush() left us on black; mirror that so the first stroke doesn't
    # click a swatch it is already using.
    current = nearest_color("#000000", palette)
    broken = set()
    drawn = 0
    for points, color_index in strokes:
        if not await turn_active(page):
            logging.info("Turn ended, stopping the drawing.")
            break
        if color_index != current and color_index < swatch_count and color_index not in broken:
            try:
                await colors.nth(color_index).click(timeout=1000)
                current = color_index
            except PlaywrightTimeoutError:
                # Not worth stalling the turn - keep drawing in the current color.
                logging.warning(f"Could not select color swatch {color_index}, skipping it.")
                broken.add(color_index)
        screen_points = [(box["x"] + x * scale_x, box["y"] + y * scale_y) for x, y in points]
        await page.mouse.move(*screen_points[0])
        await page.mouse.down()
        for px, py in screen_points[1:]:
            await page.mouse.move(px, py)
            await asyncio.sleep(delay)
        await page.mouse.up()
        await asyncio.sleep(0.03)
        drawn += 1
    logging.info(f"Drew {drawn}/{len(strokes)} strokes.")

async def main():
    loop = asyncio.get_event_loop()
    guess_dialog = None
    drawing = False
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
        # Force inject floating buttons directly into rendered page
        await page.expose_function("pythonCapture", lambda: asyncio.create_task(capture_catch()))
        await page.expose_function("pythonCaptureDraw", lambda: asyncio.create_task(draw_catch()))
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

        async def capture_catch():
            try:
                await capture()
            except Exception as e:
                logging.exception(e)

        async def capture():
            nonlocal guess_dialog, drawing
            if drawing:
                logging.info("Skipping the guess solver, we are drawing right now.")
                return
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
                        "service_tier": "flex",
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
                        "provider": {
                            "only" : ["google-ai-studio"]
                        }
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

        async def set_draw_button(busy):
            try:
                await page.evaluate("""(busy) => {
                    const btn = document.getElementById('skriblai-draw');
                    if (!btn) return;
                    btn.disabled = busy;
                    btn.innerText = busy ? '🎨 DRAWING...' : '🎨 AI DRAW';
                }""", busy)
            except Exception:
                pass  # page may be closing

        async def current_word():
            """Return the word currently selected for drawing, or None if there
            isn't one (word selection stays manual)."""
            word_el = page.locator("#game-word .word")
            hints_el = page.locator("#game-word .hints .container")
            for _ in range(10):
                if await word_el.is_visible() and not await hints_el.is_visible():
                    text = (await word_el.text_content() or "").strip()
                    if text:
                        return text
                await asyncio.sleep(0.2)
            return None

        async def draw_turn():
            word = await current_word()
            if not word:
                logging.info("No word selected - pick a word first, then hit AI DRAW.")
                return
            logging.info(f"Drawing the word: {word}")
            palette = await read_palette(page)
            strokes = await generate_strokes(word, palette)
            logging.info(
                f"Got {len(strokes)} strokes, {sum(len(p) for p, _ in strokes)} points."
            )
            await setup_brush(page, palette)
            await draw_strokes(page, strokes, palette)
            logging.info("AI drawing finished.")

        async def draw_catch():
            nonlocal drawing
            if drawing:
                logging.info("A drawing is already in progress.")
                return
            drawing = True
            await set_draw_button(True)
            try:
                await draw_turn()
            except Exception as e:
                logging.exception(e)
            finally:
                drawing = False
                await set_draw_button(False)

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
