#!/usr/bin/env python3
"""
Lemieux GD RAT Telegram Bot
============================
Grateful Dead RAG bot on Telegram, powered by the same FAISS vector index
used by the Hermes MCP server (lemieux).

Architecture:
    [Telegram User] → [@BotFather token] → python-telegram-bot →
    FAISS + SentenceTransformer (lembot_core) → (optional Grok summary) →
    back to Telegram chat

Voice note support:
    Placeholder for future integration with cloned "Lemieux" voice.
    When `ENABLE_LEMIEU_VOICE=true` and piper model is present,
    /gd ask "query" --voice will synthesize the LLM summary via Piper
    and return an audio file. Currently disabled — text-only.

Setup:
    1. Talk to @BotFather on Telegram, run /newbot
    2. Set LEMIEUX_TELEGRAM_TOKEN=<token>
    3. Optional: set XAI_API_KEY for Grok-synthesized answers
    4. python3 lembot_telegram.py

Commands:
    /gd ask "question"         → Retrieve + summarize GD archive answer
    /gd search "text"           → Show raw matching comments
    /start                      → Welcome message
    /help                       → Show usage

Author: Hal (Douglas Raines persona system)
        Grateful Dead division of Nous Research
"""

import argparse
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

# python-telegram-bot v20+ async API
from telegram import Update, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ── Shared RAG core ───────────────────────────────────────────────────────────
from lembot_core import (  # noqa: E402
    PROJECT_DIR, load_rat, search, search_lyrics, format_lyrics, format_meaning,
    summarize_with_llm, show_url as format_show_url,
)

TOKEN_ENV_KEY = "LEMIEUX_TELEGRAM_TOKEN"
PIPER_BINARY = shutil.which("piper")  # may be None
PIPER_MODEL_DIR = Path("/home/hermes/.hermes/hal_voices/piper_hal_model")
WAV_OUTPUT_DIR = PROJECT_DIR / "bot" / "output"
WAV_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Placeholder: only activates once Lemur voice is trained
ENABLE_LEMIEU_VOICE = os.environ.get("ENABLE_LEMIEU_VOICE", "false").lower() == "true"


def format_results(query: str, results: list[dict], max_items: int = 5) -> str:
    """Format search results as Telegram-friendly markdown."""
    if not results:
        return "No comments found matching that query."
    lines = [f"*Results for:* _{query}_\n"]
    for i, r in enumerate(results[:max_items], 1):
        m = r["meta"]
        text = m.get("comment_text", "")[:400]
        text = text[:-3] + "..." if len(text) > 400 else text
        lines.append(
            f"*{i}* [{m.get('show_identifier', '?')}]({format_show_url(m.get('show_identifier', ''))})\n"
            f"Rating: {m.get('rating', 'N/A')}/5 | Date: {m.get('created', 'N/A')}\n"
            f"{text}\n"
        )
    lines.append(
        f"\n_Top {len(results[:max_items])} of {len(results)} results. "
        f"Use `/gd search \"text\"` for more._"
    )
    return "\n".join(lines)


# ── Telegram Bot ──────────────────────────────────────────────────────────────
class LemurTGBot:

    def __init__(self, token: str):
        self.token = token
        self.app: Optional[Application] = None
        self.index = None
        self.metadata = None
        self.lyrics = None
        self.model = None
        self.openai_key: Optional[str] = os.environ.get("XAI_API_KEY")
        self._load_rat()

    def _load_rat(self):
        """Load RAG resources once at startup."""
        print("⏳ Loading FAISS index + embedding model...")
        try:
            self.index, self.metadata, self.lyrics, self.model = load_rat()
            print(f"✅ Loaded: {len(self.metadata)} metadata entries | "
                  f"{len(self.lyrics)} lyrics/interpretations | index size ~{self.index.ntotal}")
        except Exception as e:
            print(f"❌ Failed to load RAG resources: {e}", file=sys.stderr)
            sys.exit(1)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start."""
        await update.message.reply_text(
            "🎵 *Lemieux GD Bot* — Your Grateful Dead archive assistant.\n\n"
            "Use:\n"
            "/gd ask \"question\"\n"
            "/gd search \"text\"\n"
            "/gd lyrics \"song name\"\n"
            "/gd meaning \"song name\"\n"
            "/help for details."
        )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help."""
        await update.message.reply_text(
            "*Commands:*\n"
            "/start — Show welcome message\n"
            "/help — Show this help\n"
            "/gd ask \"question\" — Synthesize answer from fan reviews\n"
            "/gd search \"text\" — Show raw matching comments\n"
            "/gd lyrics \"song name\" — Show lyrics + source URLs\n"
            "/gd meaning \"song name\" — Show lyrics + all interpretations\n\n"
            "_Powered by the Grateful Dead RAG knowledge base._"
        )

    def _parse_gd_args(self, text: str) -> tuple[str, str]:
        """
        Parse arguments for /gd command or bare text.
        Returns (action: str, question: str). Bare text defaults to 'ask'.
        """
        text = text.strip()
        match = re.match(r'(\w+)\s*"(.+?)"', text, flags=re.S)
        if match and match.group(1).lower() in ("ask", "search", "lyrics", "meaning"):
            return match.group(1).lower(), match.group(2)
        parts = text.split(None, 1)
        if parts and parts[0].lower() in ("ask", "search", "lyrics", "meaning"):
            return parts[0].lower(), parts[1] if len(parts) > 1 else ""
        return "ask", text

    async def _send(self, update: Update, text: str, markdown: bool = False):
        """Reply in <=4000-char chunks; if Telegram rejects Markdown, resend as plain text."""
        chunks, limit = [], 4000
        while len(text) > limit:
            cut = text.rfind("\n", 0, limit)
            cut = cut if cut > limit // 2 else limit
            chunks.append(text[:cut]); text = text[cut:].lstrip("\n")
        if text:
            chunks.append(text)
        for part in chunks:
            try:
                await update.message.reply_text(
                    part, parse_mode="Markdown" if markdown else None,
                    disable_web_page_preview=True)
            except Exception as e:
                if not markdown:
                    raise
                print(f"[WARN] Markdown send failed ({e}); resending plain", file=sys.stderr)
                await update.message.reply_text(part, disable_web_page_preview=True)

    async def text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Bare text (no /gd) — treat as a question, or 'lyrics X' / 'meaning X' / 'search X'."""
        if not update.message or not update.message.text:
            return
        text = update.message.text
        # Strip bot @mention prefix that appears when addressed in groups
        bot_username = context.bot.username
        if bot_username and text.startswith(f"@{bot_username}"):
            text = text[len(f"@{bot_username}"):]
        elif bot_username and f" @{bot_username}" in text:
            text = text.replace(f" @{bot_username}", " ")
        text = text.strip()
        action, question = self._parse_gd_args(text)
        if question:
            await self._answer(update, context, action, question)

    async def gd_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /gd ask/search/lyrics/meaning \"...\"."""
        if not context.args:
            await update.message.reply_text(
                "Usage:\n"
                "/gd ask \"your question\"\n"
                "/gd search \"your search term\"\n"
                "/gd lyrics \"song name\"\n"
                "/gd meaning \"song name\"\n"
                "…or just type a question."
            )
            return
        action, question = self._parse_gd_args(" ".join(context.args))
        await self._answer(update, context, action, question)

    async def _answer(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                      action: str, question: str):
        await context.bot.send_chat_action(update.effective_chat.id, "typing")

        # Lyrics / meaning lookups (no LLM, plain-text output)
        if action in ("lyrics", "meaning"):
            results = search_lyrics(self.index, self.metadata, self.model, question, k=15)
            fmt = format_lyrics if action == "lyrics" else format_meaning
            await self._send(update, fmt(question, results))
            return

        results = search(self.index, self.metadata, self.model, question, k=15)

        # Augment general vector search with lyric/interpretation hits.
        # The index is dominated by setlist_song entries (~31K of ~44K),
        # so plain vector search for "Jack Straw lyrics" returns setlists
        # and the LLM never sees the actual lyrics.  Folding in
        # search_lyrics() results guarantees song-specific questions
        # always reach the lyric vectors.
        for r in search_lyrics(self.index, self.metadata, self.model, question, k=5):
            if r["meta"] not in (h["meta"] for h in results):
                results.append(r)

        if action == "ask":
            summary = summarize_with_llm(question, results, self.openai_key) if self.openai_key else None
            if summary:
                await self._send(update, summary)  # plain: LLM markdown breaks Telegram's parser
            else:
                note = ("\n\n_No XAI_API_KEY set. "
                        "For synthesized answers, set `XAI_API_KEY`._") if not self.openai_key else ""
                await self._send(update, format_results(question, results, max_items=5) + note, markdown=True)

        elif action == "search":
            await self._send(update, format_results(question, results, max_items=5), markdown=True)

    async def voice_ready(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Placeholder handler for future voice flag parsing."""
        await update.message.reply_text(
            "🔊 Voice synthesis for Lemieux is currently offline.\n"
            "Will activate once voice training is complete."
        )

    async def on_error(self, update, context):
        print(f"[ERROR] {context.error!r}", file=sys.stderr)

    def run(self):
        """Start the Telegram bot."""
        self.app = Application.builder().token(self.token).build()

        # Register handlers
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("help", self.help))
        self.app.add_handler(CommandHandler("gd", self.gd_command))
        # Private chats: respond to all text messages.
        # Groups/supergroups: respond only when the bot is @mentioned.
        self.app.add_handler(MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            self.text_message))
        self.app.add_handler(MessageHandler(
            (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP)
            & filters.TEXT & filters.Mention,
            self.text_message))
        self.app.add_error_handler(self.on_error)

        print("🚀 Lemieux GD Telegram Bot starting...")
        print(f"   Voice enabled: {ENABLE_LEMIEU_VOICE}")
        print(f"   LLM key: {'✅' if self.openai_key else '❌'}")

        self.app.run_polling()
        print("👋 Bot stopped.")


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Lemieux GD RAT Telegram Bot")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run a quick smoke test of the RAG pipeline without starting the bot",
    )
    args = parser.parse_args()

    if args.test:
        print("🧪 Smoke testing RAG pipeline...")
        idx, meta, lyr, mdl = load_rat()
        print(f"✅ Index loaded | Entries: {len(meta)} | Lyrics: {len(lyr)} | Model ready")
        # Quick comment search
        results = search(idx, meta, mdl, "Dark Star", k=1)
        print(f"🔍 Sample result: {results[0]['meta'].get('comment_text', 'N/A')[:80]}...")
        # Lyrics smoke test
        lyric_results = search_lyrics(idx, meta, mdl, "China Cat Sunflower", k=5)
        lyric_types = [r["meta"].get("type") for r in lyric_results]
        print(f"🎵 Lyric search types: {lyric_types}")
        if lyric_results:
            m = lyric_results[0]["meta"]
            print(f"🎵 Top lyric: {m.get('song_name', '?')} | {len(m.get('comment_text',''))} chars")
        return

    token = os.environ.get(TOKEN_ENV_KEY)
    if not token:
        print(f"❌ Environment variable {TOKEN_ENV_KEY} not set.", file=sys.stderr)
        print("   Register a bot via @BotFather and set the token.", file=sys.stderr)
        sys.exit(1)

    bot = LemurTGBot(token)
    bot.run()


if __name__ == "__main__":
    main()