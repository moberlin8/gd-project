#!/usr/bin/env python3
"""
Lemieux GD RAT — Discord bot (text only)
========================================
Answers Grateful Dead questions from the FAISS archive index.

Responds when:
  • a message is posted in a channel listed in LEMIEUX_CHANNEL_IDS, or
  • the bot is @mentioned anywhere it can read.

Message forms (the leading "!gd" is optional):
  <question>                → retrieve + Grok-synthesized answer
  search <text>             → raw matching comments
  lyrics <song name>        → lyrics + source URLs
  meaning <song name>       → lyrics + interpretations

Env (bot/.env, loaded by start_lembot_discord.sh):
  LEMIEUX_DISCORD_TOKEN   bot token from the Lemieux Discord application
  LEMIEUX_CHANNEL_IDS     comma-separated channel IDs to answer freely in
  XAI_API_KEY             Grok key for synthesized answers (optional)

Smoke test without Discord:  python3 lembot_discord.py --test "best Dark Star"
"""
import argparse
import asyncio
import os
import re
import sys

import discord

import lembot_core as core

TOKEN_ENV = "LEMIEUX_DISCORD_TOKEN"
CHANNELS_ENV = "LEMIEUX_CHANNEL_IDS"
DISCORD_LIMIT = 2000
ACTIONS = ("ask", "search", "lyrics", "meaning")


def parse_request(text: str) -> tuple[str, str]:
    """'!gd lyrics Ripple' -> ('lyrics', 'Ripple'); bare text -> ('ask', text)."""
    text = re.sub(r"^!gd\b", "", text.strip(), flags=re.I).strip()
    m = re.match(r'^(\w+)\s+"?(.+?)"?$', text, flags=re.S)
    if m and m.group(1).lower() in ACTIONS:
        return m.group(1).lower(), m.group(2).strip()
    return "ask", text


def format_search(query: str, results: list, max_items: int = 5) -> str:
    if not results:
        return "No comments found matching that query."
    lines = [f"**Results for:** *{query}*"]
    for i, r in enumerate(results[:max_items], 1):
        m = r["meta"]
        text = m.get("comment_text", "")[:350].replace("\n", " ")
        label = core._source_label(m)
        link = f" <{core.show_url(m['show_identifier'])}>" if m.get("show_identifier") else ""
        lines.append(f"**{i}. {label}**{link}\n{text}")
    return "\n\n".join(lines)


def chunk(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        out.append(text)
    return out


class Lemieux(discord.Client):
    def __init__(self, channel_ids: set[int]):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.channel_ids = channel_ids
        print("⏳ Loading FAISS index + embedding model...")
        self.index, self.metadata, self.lyrics, self.model = core.load_rat()
        print(f"✅ {len(self.metadata)} entries | {len(self.lyrics)} lyrics/interps")

    def answer(self, action: str, question: str) -> str:
        """Blocking RAG work; run via asyncio.to_thread."""
        if action in ("lyrics", "meaning"):
            hits = core.search_lyrics(self.index, self.metadata, self.model, question, k=15)
            fmt = core.format_lyrics if action == "lyrics" else core.format_meaning
            return fmt(question, hits)
        hits = core.search(self.index, self.metadata, self.model, question, k=12)
        if action == "search":
            return format_search(question, hits)
        return core.summarize_with_llm(question, hits) or (
            format_search(question, hits) + "\n\n*(LLM unavailable — showing raw matches.)*"
        )

    async def on_ready(self):
        print(f"🚀 {self.user} online | free channels: {sorted(self.channel_ids) or 'none'} "
              f"| LLM: {'✅ ' + core.LLM_MODEL if os.environ.get(core.LLM_KEY_ENV) else '❌'}")

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        mentioned = self.user in message.mentions
        if not (mentioned or message.channel.id in self.channel_ids):
            return
        text = re.sub(rf"<@!?{self.user.id}>", "", message.content).strip()
        if not text:
            await message.reply("Ask me about a show, song, lyric, or meaning. "
                                "Try: `lyrics Ripple`, `search 1972 Dark Star`, or just a question.")
            return
        action, question = parse_request(text)
        async with message.channel.typing():
            try:
                reply = await asyncio.to_thread(self.answer, action, question)
            except Exception as e:
                print(f"[ERROR] {action} {question!r}: {e}", file=sys.stderr)
                reply = "Something broke on my end while searching the archive."
        first = True
        for part in chunk(reply):
            if first:
                await message.reply(part, suppress_embeds=True)
                first = False
            else:
                await message.channel.send(part, suppress_embeds=True)


def main():
    ap = argparse.ArgumentParser(description="Lemieux GD Discord bot")
    ap.add_argument("--test", metavar="QUESTION", help="run the pipeline once, no Discord")
    args = ap.parse_args()

    if args.test:
        action, question = parse_request(args.test)
        bot = Lemieux(set())
        print(f"\naction={action} question={question!r}\n")
        for part in chunk(bot.answer(action, question)):
            print(part)
            print(f"--- ({len(part)} chars)")
        return

    token = os.environ.get(TOKEN_ENV)
    if not token:
        sys.exit(f"❌ {TOKEN_ENV} not set — create the Lemieux app in the Discord "
                 f"Developer Portal and put its token in bot/.env")
    raw = os.environ.get(CHANNELS_ENV, "")
    channel_ids = {int(c) for c in re.split(r"[,\s]+", raw) if c.strip().isdigit()}
    Lemieux(channel_ids).run(token, log_handler=None)


if __name__ == "__main__":
    main()
