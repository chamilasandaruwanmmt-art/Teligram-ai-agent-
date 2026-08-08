"""
Telegram AI Agent Bot
----------------------
Gemini API (AI brain) + Playwright (browser automation) + python-telegram-bot

Setup:
1. pip install -r requirements.txt
2. playwright install chromium
3. Set environment variables: TELEGRAM_BOT_TOKEN, GEMINI_API_KEY
4. python bot.py
"""

import os
import logging
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from google import genai
from playwright.async_api import async_playwright

# ---------- Config ----------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"  # free tier friendly

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- Gemini client ----------
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# ---------- Playwright browser (shared, lazy-started) ----------
_playwright = None
_browser = None
_browser_lock = asyncio.Lock()


async def get_browser():
    """Start (once) and reuse a single headless Chromium browser instance."""
    global _playwright, _browser
    async with _browser_lock:
        if _browser is None:
            _playwright = await async_playwright().start()
            _browser = await _playwright.chromium.launch(headless=True)
        return _browser


async def fetch_page_text(url: str, wait_selector: str | None = None, timeout_ms: int = 20000) -> str:
    """Open a URL with Playwright and return visible text content."""
    browser = await get_browser()
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        if wait_selector:
            await page.wait_for_selector(wait_selector, timeout=timeout_ms)
        text = await page.inner_text("body")
        return text[:8000]  # keep it bounded for the AI prompt
    finally:
        await context.close()


async def screenshot_page(url: str, out_path: str, timeout_ms: int = 20000) -> str:
    browser = await get_browser()
    context = await browser.new_context(viewport={"width": 1280, "height": 800})
    page = await context.new_page()
    try:
        await page.goto(url, timeout=timeout_ms, wait_until="networkidle")
        await page.screenshot(path=out_path, full_page=False)
        return out_path
    finally:
        await context.close()


# ---------- Gemini call ----------
def ask_gemini(prompt: str) -> str:
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )
    return response.text


# ---------- Telegram handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "AI agent bot එක ready.\n\n"
        "Commands:\n"
        "/ask <question> - Gemini AI ගෙන් අහන්න\n"
        "/browse <url> - page එකේ content කියවලා summarize කරන්න\n"
        "/shot <url> - page එකේ screenshot එකක් ගන්න\n"
        "සාමාන්‍ය message එකක් type කළත් AI reply එකක් දෙනවා."
    )


async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Usage: /ask <question>")
        return
    await update.message.chat.send_action("typing")
    try:
        answer = await asyncio.to_thread(ask_gemini, query)
        await update.message.reply_text(answer)
    except Exception as e:
        logger.exception("Gemini error")
        await update.message.reply_text(f"Error: {e}")


async def browse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /browse <url>")
        return
    url = context.args[0]
    await update.message.reply_text(f"{url} browse කරනවා...")
    try:
        page_text = await fetch_page_text(url)
        summary_prompt = (
            f"Summarize the key content of this webpage in simple Sinhala, "
            f"in 5-6 bullet points:\n\n{page_text}"
        )
        summary = await asyncio.to_thread(ask_gemini, summary_prompt)
        await update.message.reply_text(summary)
    except Exception as e:
        logger.exception("Browse error")
        await update.message.reply_text(f"Error: {e}")


async def shot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /shot <url>")
        return
    url = context.args[0]
    await update.message.reply_text("Screenshot ගන්නවා...")
    out_path = "/tmp/shot.png"
    try:
        await screenshot_page(url, out_path)
        with open(out_path, "rb") as f:
            await update.message.reply_photo(f)
    except Exception as e:
        logger.exception("Screenshot error")
        await update.message.reply_text(f"Error: {e}")


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback: any plain text message goes to Gemini directly."""
    text = update.message.text
    await update.message.chat.send_action("typing")
    try:
        answer = await asyncio.to_thread(ask_gemini, text)
        await update.message.reply_text(answer)
    except Exception as e:
        logger.exception("Chat error")
        await update.message.reply_text(f"Error: {e}")


async def on_shutdown(app: Application):
    global _browser, _playwright
    if _browser:
        await _browser.close()
    if _playwright:
        await _playwright.stop()


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is running")

    def log_message(self, format, *args):
        pass  # silence default HTTP logging


def start_health_server():
    """Render Web Services need an open port to detect the app as 'live'."""
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info(f"Health check server listening on port {port}")


def main():
    start_health_server()

    if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN සහ GEMINI_API_KEY environment variables දෙකම set කරන්න."
        )

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_shutdown(on_shutdown).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ask", ask))
    app.add_handler(CommandHandler("browse", browse))
    app.add_handler(CommandHandler("shot", shot))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
