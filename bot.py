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
import uuid
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
import psycopg2
from psycopg2 import pool as pg_pool

# ---------- Config ----------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-flash-latest"  # always points to the current stable Flash model
DATABASE_URL = os.environ.get("DATABASE_URL", "")  # Supabase Postgres connection string
HISTORY_LIMIT = 12  # how many past messages to feed back as context per chat
GENERATED_APPS_DIR = os.environ.get("GENERATED_APPS_DIR", "/tmp/generated_apps")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- Gemini client ----------
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# ---------- Postgres (Supabase) memory ----------
_pg_pool: pg_pool.SimpleConnectionPool | None = None


def get_pool() -> pg_pool.SimpleConnectionPool:
    global _pg_pool
    if _pg_pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL environment variable එක set කරලා නෑ.")
        _pg_pool = pg_pool.SimpleConnectionPool(1, 5, dsn=DATABASE_URL)
    return _pg_pool


def init_db():
    con = get_pool().getconn()
    try:
        with con, con.cursor() as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )"""
            )
            cur.execute(
                """CREATE TABLE IF NOT EXISTS snippets (
                    chat_id BIGINT NOT NULL,
                    name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (chat_id, name)
                )"""
            )
    finally:
        get_pool().putconn(con)


def save_message(chat_id: int, role: str, content: str):
    con = get_pool().getconn()
    try:
        with con, con.cursor() as cur:
            cur.execute(
                "INSERT INTO messages (chat_id, role, content) VALUES (%s, %s, %s)",
                (chat_id, role, content),
            )
    finally:
        get_pool().putconn(con)


def get_recent_history(chat_id: int, limit: int = HISTORY_LIMIT):
    con = get_pool().getconn()
    try:
        with con, con.cursor() as cur:
            cur.execute(
                "SELECT role, content FROM messages WHERE chat_id = %s "
                "ORDER BY id DESC LIMIT %s",
                (chat_id, limit),
            )
            rows = cur.fetchall()
    finally:
        get_pool().putconn(con)
    return list(reversed(rows))  # oldest -> newest


def save_snippet(chat_id: int, name: str, content: str):
    con = get_pool().getconn()
    try:
        with con, con.cursor() as cur:
            cur.execute(
                """INSERT INTO snippets (chat_id, name, content) VALUES (%s, %s, %s)
                   ON CONFLICT (chat_id, name)
                   DO UPDATE SET content = EXCLUDED.content, created_at = NOW()""",
                (chat_id, name, content),
            )
    finally:
        get_pool().putconn(con)


def get_snippet(chat_id: int, name: str):
    con = get_pool().getconn()
    try:
        with con, con.cursor() as cur:
            cur.execute(
                "SELECT content FROM snippets WHERE chat_id = %s AND name = %s",
                (chat_id, name),
            )
            row = cur.fetchone()
    finally:
        get_pool().putconn(con)
    return row[0] if row else None


def list_snippets(chat_id: int):
    con = get_pool().getconn()
    try:
        with con, con.cursor() as cur:
            cur.execute(
                "SELECT name, created_at FROM snippets WHERE chat_id = %s ORDER BY created_at DESC",
                (chat_id,),
            )
            rows = cur.fetchall()
    finally:
        get_pool().putconn(con)
    return rows


def delete_snippet(chat_id: int, name: str) -> bool:
    con = get_pool().getconn()
    try:
        with con, con.cursor() as cur:
            cur.execute(
                "DELETE FROM snippets WHERE chat_id = %s AND name = %s", (chat_id, name)
            )
            deleted = cur.rowcount > 0
    finally:
        get_pool().putconn(con)
    return deleted


def clear_history(chat_id: int):
    con = get_pool().getconn()
    try:
        with con, con.cursor() as cur:
            cur.execute("DELETE FROM messages WHERE chat_id = %s", (chat_id,))
    finally:
        get_pool().putconn(con)

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


async def check_app_for_errors(html: str, timeout_ms: int = 10000) -> list[str]:
    """Load generated HTML in a headless browser and collect any JS console
    errors or uncaught exceptions, so we can warn the user before delivery."""
    browser = await get_browser()
    context = await browser.new_context()
    page = await context.new_page()
    errors: list[str] = []

    def on_console(msg):
        if msg.type == "error":
            errors.append(f"Console error: {msg.text}")

    def on_pageerror(exc):
        errors.append(f"Uncaught error: {exc}")

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)

    try:
        await page.set_content(html, timeout=timeout_ms, wait_until="load")
        await page.wait_for_timeout(1500)  # let any async errors surface
    except Exception as e:
        errors.append(f"Load failed: {e}")
    finally:
        await context.close()

    return errors


# ---------- Gemini call ----------
def ask_gemini(prompt: str) -> str:
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )
    return response.text


APP_BUILDER_SYSTEM_PROMPT = """You are an expert web app developer. Generate a COMPLETE, SELF-CONTAINED
single HTML file that implements the app the user describes. Requirements:
- Everything (HTML, CSS, JS) must be in ONE .html file, no external dependencies except CDN links if truly needed.
- The app must work fully offline in a browser after download (localStorage is fine for saving data).
- Make the UI clean and mobile-friendly (the user will likely open this on a phone browser).
- Output ONLY the raw HTML code. No markdown code fences, no explanation text before or after.
"""


def build_app_html(description: str) -> str:
    """Ask Gemini to generate a complete single-file HTML app for the given description."""
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            {"role": "user", "parts": [{"text": APP_BUILDER_SYSTEM_PROMPT + "\n\nApp description: " + description}]}
        ],
    )
    html = response.text.strip()
    # Strip markdown code fences if the model added them despite instructions
    if html.startswith("```"):
        lines = html.split("\n")
        lines = lines[1:] if lines[0].startswith("```") else lines
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        html = "\n".join(lines)
    return html


def ask_gemini_with_history(chat_id: int, prompt: str) -> str:
    """Build the recent conversation into Gemini's multi-turn `contents` format,
    call the model, then persist both the user turn and the model reply."""
    history = get_recent_history(chat_id)
    contents = []
    for role, content in history:
        gemini_role = "user" if role == "user" else "model"
        contents.append({"role": gemini_role, "parts": [{"text": content}]})
    contents.append({"role": "user", "parts": [{"text": prompt}]})

    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
    )
    answer = response.text

    save_message(chat_id, "user", prompt)
    save_message(chat_id, "model", answer)
    return answer


# ---------- Telegram handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "AI agent bot එක ready.\n\n"
        "Commands:\n"
        "/ask <question> - Gemini AI ගෙන් අහන්න (chat history මතකයි)\n"
        "/browse <url> - page එකේ content කියවලා summarize කරන්න\n"
        "/shot <url> - page එකේ screenshot එකක් ගන්න\n"
        "/save <name> <content> - Pine Script/notes save කරන්න\n"
        "/recall <name> - save කරපු එකක් ආපහු බලන්න\n"
        "/list - save කරපු ඔක්කොම names බලන්න\n"
        "/forget <name> - save කරපු එකක් delete කරන්න\n"
        "/reset - chat history clear කරන්න (fresh start)\n"
        "/buildapp <description> - AI ම app එකක් හදලා download link (file) එකක් දෙනවා\n\n"
        "සාමාන්‍ය message එකක් type කළත් AI reply එකක් දෙනවා, කලින් chat එකත් මතක තියාගෙන."
    )


async def send_long_text(message, text: str):
    """Telegram caps messages at ~4096 chars; split longer replies into chunks."""
    if not text:
        text = "(empty reply)"
    for i in range(0, len(text), 4000):
        await message.reply_text(text[i:i + 4000])


async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Usage: /ask <question>")
        return
    chat_id = update.message.chat_id
    await update.message.chat.send_action("typing")
    try:
        answer = await asyncio.to_thread(ask_gemini_with_history, chat_id, query)
        await send_long_text(update.message, answer)
    except Exception as e:
        logger.exception("Gemini error")
        await update.message.reply_text(f"Error: {e}")


async def save_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /save <name> <content>\n"
            "උදා: /save jobless_v2 [Pine Script code එක මෙතනින්]"
        )
        return
    name = context.args[0]
    content = " ".join(context.args[1:])
    chat_id = update.message.chat_id
    try:
        await asyncio.to_thread(save_snippet, chat_id, name, content)
        await update.message.reply_text(f"'{name}' save කළා. ආපහු ගන්න: /recall {name}")
    except Exception as e:
        logger.exception("Save error")
        await update.message.reply_text(f"Error: {e}")


async def recall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /recall <name>")
        return
    name = context.args[0]
    chat_id = update.message.chat_id
    content = await asyncio.to_thread(get_snippet, chat_id, name)
    if content is None:
        await update.message.reply_text(f"'{name}' කියලා දෙයක් save කරලා නෑ. /list කරලා බලන්න.")
    else:
        # Telegram message limit is ~4096 chars; split if needed
        for i in range(0, len(content), 4000):
            await update.message.reply_text(content[i:i + 4000])


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    rows = await asyncio.to_thread(list_snippets, chat_id)
    if not rows:
        await update.message.reply_text("තවම කිසිම එකක් save කරලා නෑ.")
        return
    lines = "\n".join(f"- {name} ({created})" for name, created in rows)
    await update.message.reply_text(f"Save කරපු items:\n{lines}")


async def forget_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /forget <name>")
        return
    name = context.args[0]
    chat_id = update.message.chat_id
    deleted = await asyncio.to_thread(delete_snippet, chat_id, name)
    if deleted:
        await update.message.reply_text(f"'{name}' delete කළා.")
    else:
        await update.message.reply_text(f"'{name}' කියලා දෙයක් හම්බුනේ නෑ.")


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    await asyncio.to_thread(clear_history, chat_id)
    await update.message.reply_text("Chat history clear කළා. අලුතින් පටන් ගමු.")


async def buildapp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    description = " ".join(context.args)
    if not description:
        await update.message.reply_text(
            "Usage: /buildapp <app description>\n"
            "උදා: /buildapp calculator app එකක්, results notebook එකකට local save කරන්න පුළුවන්"
        )
        return
    await update.message.reply_text("App එක හදනවා... ටිකක් වෙලාව යනවා ⏳")
    try:
        html = await asyncio.to_thread(build_app_html, description)

        # Test-load the app in a headless browser and check for JS errors
        await update.message.reply_text("App එක test කරනවා (errors තියෙනවද කියලා)... 🔍")
        errors = await check_app_for_errors(html)

        # If errors were found, ask Gemini to fix them once and re-check
        if errors:
            fix_prompt = (
                f"{APP_BUILDER_SYSTEM_PROMPT}\n\nApp description: {description}\n\n"
                f"Here is a previous version of the app that had these errors when loaded:\n"
                f"{chr(10).join(errors)}\n\n"
                f"Previous code:\n{html}\n\n"
                f"Fix the errors and output the corrected COMPLETE single HTML file."
            )
            html = await asyncio.to_thread(
                lambda: gemini_client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=[{"role": "user", "parts": [{"text": fix_prompt}]}],
                ).text.strip()
            )
            if html.startswith("```"):
                lines = html.split("\n")
                lines = lines[1:] if lines[0].startswith("```") else lines
                if lines and lines[-1].strip().startswith("```"):
                    lines = lines[:-1]
                html = "\n".join(lines)
            errors = await check_app_for_errors(html)

        status_msg = (
            "✅ App එක clean - errors හම්බුනේ නෑ."
            if not errors
            else "⚠️ App එක fix කරන්න try කළා, ඒත් තවම මේ errors තියෙනවා:\n" + "\n".join(errors[:5])
        )
        await update.message.reply_text(status_msg)

        # Save privately for direct download as a Telegram document
        file_path = f"/tmp/app_{update.message.chat_id}.html"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(html)

        # Also save into the public /apps/ folder so it gets a shareable link
        os.makedirs(GENERATED_APPS_DIR, exist_ok=True)
        public_filename = f"app_{update.message.chat_id}_{uuid.uuid4().hex[:8]}.html"
        public_path = os.path.join(GENERATED_APPS_DIR, public_filename)
        with open(public_path, "w", encoding="utf-8") as f:
            f.write(html)

        base_url = get_public_base_url()
        public_link = f"{base_url}/apps/{public_filename}" if base_url else None

        with open(file_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename="my_app.html",
                caption="App එක ready! Download කරලා open කරන්න පුළුවන් (offline වැඩ කරයි).",
            )

        if public_link:
            await update.message.reply_text(
                "📱 Phone එකේ app icon එකක් විදිහට install කරගන්න:\n"
                f"{public_link}\n\n"
                "1. මේ link එක Chrome/Brave එකෙන් open කරන්න\n"
                "2. Menu (⋮) → 'Add to Home screen' select කරන්න\n"
                "3. Home screen එකේ app icon එකක් විදිහට පේනවා\n\n"
                "🤖 Real APK file එකක්ම ඕන නම්:\n"
                "pwabuilder.com යන්න → link එක paste කරන්න → 'Android' package එක download කරන්න."
            )
    except Exception as e:
        logger.exception("Build app error")
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
    """Fallback: any plain text message goes to Gemini, with memory of recent turns."""
    text = update.message.text
    chat_id = update.message.chat_id
    await update.message.chat.send_action("typing")
    try:
        answer = await asyncio.to_thread(ask_gemini_with_history, chat_id, text)
        await send_long_text(update.message, answer)
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
        # Serve generated apps at /apps/<filename>.html so they get a public,
        # shareable link (used for "Add to Home Screen" and for feeding to
        # third-party APK builders like pwabuilder.com).
        if self.path.startswith("/apps/"):
            filename = os.path.basename(self.path[len("/apps/"):])
            filepath = os.path.join(GENERATED_APPS_DIR, filename)
            if os.path.isfile(filepath):
                with open(filepath, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(content)
                return
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return

        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is running")

    def log_message(self, format, *args):
        pass  # silence default HTTP logging


def get_public_base_url() -> str:
    """Render sets RENDER_EXTERNAL_URL automatically for web services."""
    url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if url:
        return url
    hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")
    if hostname:
        return f"https://{hostname}"
    return ""


def start_health_server():
    """Render Web Services need an open port to detect the app as 'live'."""
    os.makedirs(GENERATED_APPS_DIR, exist_ok=True)
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info(f"Health check server listening on port {port}")


def main():
    start_health_server()
    init_db()

    if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN සහ GEMINI_API_KEY environment variables දෙකම set කරන්න."
        )

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_shutdown(on_shutdown).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ask", ask))
    app.add_handler(CommandHandler("browse", browse))
    app.add_handler(CommandHandler("shot", shot))
    app.add_handler(CommandHandler("save", save_cmd))
    app.add_handler(CommandHandler("recall", recall_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("forget", forget_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("buildapp", buildapp_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
