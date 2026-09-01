#!/usr/bin/env python3
"""
GD RAT Discord Bot

Listens for messages in Discord channels and answers Grateful Dead questions
using the vector index.

Setup:
    1. Create a bot at https://discord.com/developers/applications
    2. Enable MESSAGE CONTENT INTENT
    3. Invite bot to your server with scopes: bot
    4. Copy token to .env: DISCORD_BOT_TOKEN=...

Usage:
    python3 bot.py
    DISCORD_BOT_TOKEN=... OPENAI_API_KEY=... python3 bot.py

Commands:
    /gd ask "question"   → Retrieve + synthesize answer
    /gd search "text"    → Show raw matching comments
"""

import json
import os
import re
import asyncio

import discord
from discord import app_commands

import faiss
import numpy as np
from sentence_transformers import SentenceTransformer

INDEX_DIR = os.path.join(os.path.dirname(__file__), "..", "index")
INDEX_PATH = os.path.join(INDEX_DIR, "vector_index.faiss")


def load_rat():
    """Load the FAISS index, metadata, and embedding model."""
    index = faiss.read_index(INDEX_PATH)
    with open(os.path.join(INDEX_DIR, "index_metadata.json"), "r", encoding="utf-8") as f:
        metadata = json.load(f)
    model = SentenceTransformer("all-MiniLM-L6-v2")
    return index, metadata, model


def search(index, metadata, model, query: str, k: int = 10):
    q_vec = model.encode([query], convert_to_numpy=True)
    distances, indices = index.search(q_vec, k)
    results = []
    for dist, idx in zip(distances[0], indices[0]):
        if idx < 0 or idx >= len(metadata):
            continue
        results.append({"score": float(dist), "meta": metadata[idx]})
    return results


def format_show_url(show_id: str) -> str:
    return f"https://archive.org/details/{show_id}"


def format_results(query: str, results: list[dict], max_items: int = 5) -> str:
    """Format results as Discord-friendly text with citations."""
    if not results:
        return "No comments found matching that query."

    lines = [f"**Results for:** *{query}*\n"]
    for i, r in enumerate(results[:max_items], 1):
        m = r["meta"]
        # Truncate long comments
        text = m["comment_text"]
        if len(text) > 400:
            text = text[:400] + "..."
        lines.append(
            f"**#{i}** · [{m['show_identifier']}]({format_show_url(m['show_identifier'])})\n"
            f"Rating: {m['rating']}/5 · {m['created']}\n"
            f"{text}\n"
        )
    lines.append(f"\n_Top {len(results[:max_items])} of {len(results)} results. "
                 f"Use /gd search for more._")
    return "\n".join(lines)


class GDRatBot:
    def __init__(self):
        self.index, self.metadata, self.model = load_rat()

        intents = discord.Intents.default()
        intents.message_content = True  # requires privileged intent in dev portal
        self.bot = discord.Bot(intents=intents)

        @self.bot.event
        async def on_ready():
            print(f"GD RAT Bot logged in as {self.bot.user} (ID: {self.bot.user.id})")
            print("Ready to answer Grateful Dead questions!")

        @self.bot.tree.command(name="gd", description="Query the GD RAT knowledge base")
        @app_commands.describe(
            action="Action: 'ask' (synthesized) or 'search' (raw results)",
            question="Your question about the Grateful Dead"
        )
        async def gd(
            ctx: discord.Interaction,
            action: str,
            question: str
        ):
            await ctx.response.defer()
            await self._handle_query(ctx, action, question)

    async def _handle_query(self, ctx, action, question):
        results = search(self.index, self.metadata, self.model, question, k=10)

        if action == "ask":
            # Use LLM summarization if API key available
            api_key = os.environ.get("OPENAI_API_KEY")
            if api_key:
                summary = self._summarize_with_llm(question, results, api_key)
                await ctx.followup.send(summary)
            else:
                summary = self._extractive_summary(question, results)
                await ctx.followup.send(summary + "\n\n*No OpenAI key set. Install one for synthesized answers.*")
        else:
            await ctx.followup.send(format_results(question, results, max_items=5))

    def _summarize_with_llm(self, query, results, api_key):
        from openai import OpenAI
        client = OpenAI(api_key=api_key)

        context_parts = []
        for i, r in enumerate(results, 1):
            m = r["meta"]
            context_parts.append(
                f"[{i}] Show: {m['show_identifier']} "
                f"(rating: {m['rating']}/5, date: {m['created']})\n"
                f"Comment: {m['comment_text']}"
            )
        context = "\n\n".join(context_parts)

        prompt = f"""You are "Douglas," a Grateful Dead knowledge bot. The user asked: "{query}"

Based on {len(results)} relevant fan comments from archive.org, provide a helpful, concise answer. Cite specific shows with archive.org links. Stay grounded in the comments — do not invent facts.

Fan comments:
{context}
"""

        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1000,
        )
        return resp.choices[0].message.content.strip()

    def _extractive_summary(self, query, results):
        return format_results(query, results, max_items=5)

    def run(self, token: str):
        self.bot.run(token)


if __name__ == "__main__":
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("ERROR: Set DISCORD_BOT_TOKEN environment variable")
        exit(1)
    bot = GDRatBot()
    bot.run(token)
