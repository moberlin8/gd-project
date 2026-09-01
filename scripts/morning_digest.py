#!/usr/bin/env python3
"""
GD RAT Morning Digest — Sends a daily email with GD knowledge bot results.

Queries the vector index for interesting discussions, synthesizes with LLM,
and emails the result to a configured recipient.

Intended for daily cron (e.g., 8am UTC).

Usage:
    python3 morning_digest.py

Requires:
    - GD RAT index files (vector_index.faiss + index_metadata.json)
    - OPENAI_API_KEY (for LLM synthesis) — optional
    - SMTP credentials in Hermes config (email.smtp.*)
"""

import json
import os
import smtplib
import ssl
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import faiss
from sentence_transformers import SentenceTransformer

INDEX_DIR = os.path.join(os.path.dirname(__file__), "..", "index")
INDEX_PATH = os.path.join(INDEX_DIR, "vector_index.faiss")
META_PATH = os.path.join(INDEX_DIR, "index_metadata.json")


def load_index():
    """Load FAISS index, metadata, and embedding model."""
    index = faiss.read_index(INDEX_PATH)
    with open(META_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, list):
        metadata = raw
        setlists = {}
    else:
        metadata = raw.get("comments", [])
        setlists = raw.get("setlists", {})
    model = SentenceTransformer("all-MiniLM-L6-v2")
    return index, metadata, setlists, model


def search_recent_comments(index, metadata, model, days: int = 7, k: int = 20):
    """Search for comments from the last N days by embedding recent date strings."""
    # Search for comments mentioning "best" or "great" shows
    queries = [
        "best performance ever played",
        "best version I ever heard",
        "this show is amazing",
        "unforgettable",
    ]

    all_results = []
    seen_idx = set()

    for q in queries:
        q_vec = model.encode([q], convert_to_numpy=True)
        distances, indices = index.search(q_vec, k)
        for dist, idx in zip(distances[0], indices[0]):
            if idx < 0 or idx >= len(metadata) or idx in seen_idx:
                continue
            seen_idx.add(idx)
            all_results.append({"score": float(dist), "meta": metadata[idx]})

    # Sort by score
    all_results.sort(key=lambda x: x["score"])
    return all_results[:k]


def generate_digest_content(results: list[dict], api_key: str = None) -> str:
    """Generate email content, with LLM summary if API key available."""
    date_str = datetime.now().strftime("%Y-%m-%d")

    if api_key:
        try:
            return generate_llm_digest(date_str, results, api_key)
        except Exception as e:
            print(f"LLM error: {e}, falling back to extractive summary")

    return generate_extractive_digest(date_str, results)


def generate_extractive_digest(date_str: str, results: list[dict]) -> str:
    """Generate digest from top comments without LLM."""
    lines = [
        f"# GD RAT Morning Digest — {date_str}",
        "",
        f"**{len(results)} interesting fan comments** from archive.org:",
        "",
    ]

    for i, r in enumerate(results, 1):
        m = r["meta"]
        text = m["comment_text"]
        if len(text) > 400:
            text = text[:400] + "..."
        lines.append(
            f"**#{i}** · [{m['show_identifier']}](https://archive.org/details/{m['show_identifier']})\n"
            f"Rating: {m['rating']}/5 · {m['created']}\n"
            f"{text}\n"
        )

    lines.append("---")
    lines.append("*Extractive summary (no LLM available). Set OPENAI_API_KEY for synthesized insights.*")
    return "\n".join(lines)


def generate_llm_digest(date_str: str, results: list[dict], api_key: str) -> str:
    """Generate digest using LLM summarization."""
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

    prompt = f"""You are "Douglas," a Grateful Dead knowledge bot. Generate a daily morning digest email titled "GD RAT Morning Digest — {date_str}" summarizing the most interesting fan comments from archive.org.

Include:
- A brief intro line about today's highlights
- 5-8 key insights/citations organized by theme (best performances, rare versions, attendee experiences)
- Each with archive.org show link and comment excerpt
- A closing line

Keep it concise and engaging. Ground everything in the provided comments.

Comments:
{context}
"""

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=2000,
    )
    return resp.choices[0].message.content.strip()


def send_email(content: str, subject: str, to_email: str, smtp_config: dict, smtp_password: str):
    """Send email via SMTP."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_config["username"]
    msg["To"] = to_email

    text_part = content.replace("#", "").replace("**", "").replace("[", "").replace("](https://archive.org/details/", " https://archive.org/details/")
    html_part = content.replace("# ", "<h3>").replace("#", "</h3>")
    html_part = html_part.replace("**", "<strong>").replace("</strong>", "</strong>")
    # Basic markdown to HTML conversion
    html = ""
    for line in content.split("\n"):
        if line.startswith("# "):
            html += f"<h2>{line[2:]}</h2>\n"
        elif line.startswith("**"):
            html += f"<strong>{line}</strong>\n"
        elif line.startswith("- "):
            html += f"<li>{line[2:]}</li>\n"
        elif line.strip() == "":
            html += "<br>\n"
        else:
            html += f"<p>{line}</p>\n"

    msg.attach(MIMEText(text_part, "plain"))
    msg.attach(MIMEText(html, "html"))

    server = smtplib.SMTP(smtp_config["host"], smtp_config["port"])
    server.starttls(context=ssl.create_default_context())
    server.login(smtp_config["username"], smtp_password)
    server.sendmail(smtp_config["username"], to_email, msg.as_string())
    server.quit()


def main():
    # Load RAT index
    index, metadata, setlists, model = load_index()

    # Search for interesting comments
    results = search_recent_comments(index, metadata, model, k=20)

    # Generate content
    api_key = os.environ.get("OPENAI_API_KEY")
    content = generate_digest_content(results, api_key)

    # Try to send email
    to_email = os.environ.get("GD_DIGEST_EMAIL", "moberlin8@gmail.com")
    smtp_config = {
        "host": os.environ.get("EMAIL_SMTP_HOST", "smtp.gmail.com"),
        "port": int(os.environ.get("EMAIL_SMTP_PORT", "587")),
        "username": os.environ.get("EMAIL_SMTP_USERNAME", "moberlin8@gmail.com"),
    }
    smtp_password = os.environ.get("EMAIL_SMTP_PASSWORD")

    if smtp_password:
        subject = f"GD RAT Morning Digest — {datetime.now().strftime('%Y-%m-%d')}"
        send_email(content, subject, to_email, smtp_config, smtp_password)
        print(f"Email sent to {to_email}")
    else:
        # Print to stdout if no SMTP password
        print(content)
        print(f"\n--- To enable email, set EMAIL_SMTP_PASSWORD environment variable ---")


if __name__ == "__main__":
    main()
