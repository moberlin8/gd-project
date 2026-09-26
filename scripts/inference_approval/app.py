#!/usr/bin/env python3
"""Web approval interface for multi-singer-tag singer-tagging inferences.

Serves a page at http://127.0.0.1:8090 (Tailscale-reachable as
http://hermes.cinnamon-pinecone.ts.net:8090 via Tailscale Serve proxy) listing inferred vocal attributions from
data/vocal_inferences_staging.json, built from data/gd_singer_assignments.json
against songs in data/gd_lyrics.json that lack explicit inline [Singer] tags.

Multi-singer selection:
  * Each pending song shows the *recommended* tag plus buttons for every OTHER
    plausible singer (era-filtered against band-member tenure dates in
    gd_singer_assignments.json -> band_members).
  * Clicking ANY singer tag (recommended OR alternate) APPROVES that tag —
    it writes the bracketed [Tag] into canonical gd_lyrics.json, records the
    decision in SQLite as 'approved', and the song leaves the pending list.
  * Reject marks the song 'rejected' in SQLite and in the staging file (kept in
    staging) and moves it to the bottom of the pending view.

Other features: confidence level + source citations per recommendation, and
10 songs per page with pagination.

Approval state is tracked in a small SQLite DB (approval.db) so decisions survive
regeneration of the staging file. The MCP bot keeps serving explicit tags from
gd_lyrics.json as before.

Run:
    cd scripts/inference_approval
    python3 app.py                 # Flask must be installed
    # then open http://100.86.202.0:8090  (or http://localhost:8090)
"""

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone

from flask import Flask, request, redirect, url_for

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
LYRICS_PATH = os.path.join(DATA_DIR, "gd_lyrics.json")
STAGING_PATH = os.path.join(DATA_DIR, "vocal_inferences_staging.json")
ASSIGN_PATH = os.path.join(DATA_DIR, "gd_singer_assignments.json")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "approval.db")

HOST = os.environ.get("APPROVAL_HOST", "127.0.0.1")  # localhost only; Tailscale Serve (port 8090) proxies externally
PORT = int(os.environ.get("APPROVAL_PORT", "8090"))
PAGE_SIZE = 10

app = Flask(__name__)
_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# SQLite approval-state store
# --------------------------------------------------------------------------- #
def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    with _get_conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS decisions (
                song_name TEXT PRIMARY KEY,
                status TEXT NOT NULL,           -- pending | approved | rejected
                tag TEXT,
                primary_vocalist TEXT,
                decided_at TEXT
            )"""
        )


def db_get(name):
    with _get_conn() as c:
        row = c.execute("SELECT * FROM decisions WHERE song_name=?", (name,)).fetchone()
    return dict(row) if row else None


def db_upsert(name, status, tag=None, vocalist=None):
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as c:
        c.execute(
            """INSERT INTO decisions (song_name, status, tag, primary_vocalist, decided_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(song_name) DO UPDATE SET
                status=excluded.status, tag=excluded.tag,
                primary_vocalist=excluded.primary_vocalist,
                decided_at=excluded.decided_at""",
            (name, status, tag, vocalist, now),
        )


def db_all_statuses():
    with _get_conn() as c:
        rows = c.execute("SELECT song_name, status FROM decisions").fetchall()
    return {r["song_name"]: r["status"] for r in rows}


# --------------------------------------------------------------------------- #
# File helpers
# --------------------------------------------------------------------------- #
def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def load_staging():
    """Return list of inference dicts merged with DB state. Pending songs first
    (in original staging order), then approved, then rejected (bottom)."""
    statuses = db_all_statuses()
    if not os.path.exists(STAGING_PATH):
        return []
    staging = _read_json(STAGING_PATH)
    inferences = []
    for inf in staging.get("inferences", []):
        name = inf["song_name"]
        rec = inf.copy()
        db_status = statuses.get(name)
        # DB is source of truth for status; staging's own 'status' is a fallback.
        rec["status"] = db_status or inf.get("status", "pending")
        inferences.append(rec)
    # pending in original order, then approved, then rejected at the bottom
    order = {"pending": 0, "approved": 1, "rejected": 2}
    inferences.sort(key=lambda i: (order.get(i["status"], 0),))
    return inferences


def update_staging_status(name, status, tag=None, vocalist=None):
    """Persist a decision into the staging file so the file reflects reality."""
    if not os.path.exists(STAGING_PATH):
        return
    staging = _read_json(STAGING_PATH)
    for inf in staging.get("inferences", []):
        if inf["song_name"] == name:
            inf["status"] = status
            if tag:
                inf["tag"] = tag
                inf["proposed_tags"] = f"[{tag}]"
            if vocalist:
                inf["primary_vocalist"] = vocalist
            break
    _write_json(STAGING_PATH, staging)


# --------------------------------------------------------------------------- #
# Accept / Reject actions
# --------------------------------------------------------------------------- #
def _promote_to_lyrics(inf, tag):
    """Write the bracketed [tag] into canonical gd_lyrics.json and return True.
    `tag` is the specific singer tag the reviewer chose (recommended or alternate)."""
    canonical_title = inf.get("canonical_title") or inf["song_name"]
    if not tag:
        raise ValueError(f"No tag to promote for {inf['song_name']}")
    lyrics = _read_json(LYRICS_PATH)
    if canonical_title not in lyrics:
        # Fall back to exact title.
        if inf["song_name"] in lyrics:
            canonical_title = inf["song_name"]
        else:
            raise KeyError(f"Song '{canonical_title}' not found in gd_lyrics.json")
    entry = lyrics[canonical_title]
    text = entry.get("lyrics_text", "") or ""
    if re.search(rf"^\s*\[{re.escape(tag)}\]", text):
        raise ValueError(f"{canonical_title} already carries a leading [{tag}] tag")
    entry["lyrics_text"] = f"[{tag}]\n\n{text}"
    lyrics[canonical_title] = entry
    _write_json(LYRICS_PATH, lyrics)
    return True


def _find_singer_for_tag(inf, tag):
    """Resolve a chosen tag to (inline_tag, canonical_vocalist_name).
    Accepts the recommended tag, backup names, or any plausible-singer tag."""
    tag = (tag or "").strip()
    # Recommended tag (e.g. name "Jerry Garcia" + tag "Jerry")
    if inf.get("tag") and tag.lower() == inf["tag"].lower():
        return inf["tag"], inf.get("primary_vocalist", "")
    # Match against plausible singers (alternates) by name or tag
    for alt in inf.get("plausible_singers", []):
        if tag.lower() in (alt.get("name", "").lower(), alt.get("tag", "").lower()):
            return alt["tag"], alt.get("name", "")
    # Match primary by name
    if tag.lower() == inf.get("primary_vocalist", "").lower():
        return inf.get("tag", tag), inf.get("primary_vocalist", "")
    return None, None


@app.route("/", methods=["GET"])
def index():
    inferences = load_staging()
    counts = {"pending": 0, "approved": 0, "rejected": 0}
    for i in inferences:
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    page = max(1, request.args.get("page", 1, type=int))
    pending = [i for i in inferences if i["status"] == "pending"]
    n_pages = max(1, -(-len(pending) // PAGE_SIZE))
    page = min(page, n_pages)
    start = (page - 1) * PAGE_SIZE
    page_items = pending[start:start + PAGE_SIZE]
    return render_page(inferences, counts, page_items, page, n_pages)


@app.route("/action", methods=["POST"])
def action():
    name = request.form.get("song_name")
    act = request.form.get("action")
    tag = request.form.get("tag")  # which singer tag the reviewer clicked
    if not name or act not in ("accept", "reject"):
        return render_page(load_staging(), {}, [], 1, 1, error="Missing or invalid action.")
    with _lock:
        inf = next((i for i in load_staging() if i["song_name"] == name), None)
        if inf is None:
            return render_page(load_staging(), {}, [], 1, 1, error=f"No pending inference for '{name}'.")
        try:
            if act == "accept":
                chosen_tag, vocalist = _find_singer_for_tag(inf, tag or inf.get("tag"))
                if not chosen_tag:
                    raise ValueError(f"Unknown singer tag '{tag}' for {inf['song_name']}")
                _promote_to_lyrics(inf, chosen_tag)
                db_upsert(name, "approved", chosen_tag, vocalist or inf.get("primary_vocalist"))
                update_staging_status(name, "approved", chosen_tag, vocalist or inf.get("primary_vocalist"))
            else:
                db_upsert(name, "rejected", inf.get("tag"), inf.get("primary_vocalist"))
                update_staging_status(name, "rejected")
        except Exception as e:  # noqa: BLE001 - surface human-readable errors
            return render_page(load_staging(), {}, [], 1, 1, error=f"Failed to {act} '{name}': {e}")
    return redirect(url_for("index"))


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render_page(inferences, counts, page_items, page, n_pages, error=None):
    rows = "".join(_row_html(i) for i in page_items)
    n_items = len(page_items)
    error_html = f'<p class="error">{error}</p>' if error else ""
    summary = {
        "pending": counts.get("pending", 0),
        "approved": counts.get("approved", 0),
        "rejected": counts.get("rejected", 0),
    }
    # Pagination links
    def page_link(p, label):
        return f'<a class="pagelink" href="?page={p}">{label}</a>'
    prev = f'<span class="pagelink disabled">prev</span>' if page <= 1 else page_link(page - 1, "prev")
    nxt = f'<span class="pagelink disabled">next</span>' if page >= n_pages else page_link(page + 1, "next")
    pagination = f"""
    <div class="pagination">
      {prev} &nbsp; Page <b>{page}/{n_pages}</b> ({n_items} of {summary['pending']} pending) &nbsp; {nxt}
    </div>"""
    bm_note = _band_members_note()
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Singer-Tag Inference Approval</title>
<style>
 body{{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;margin:2rem auto;max-width:900px;color:#222}}
 h1{{font-size:1.5rem}} .badge{{display:inline-block;padding:.15rem .5rem;border-radius:10px;font-size:.75rem;color:#fff}}
 .pending{{background:#d97706}} .approved{{background:#059669}} .rejected{{background:#b91c1c}}
 .card{{border:1px solid #ddd;border-radius:8px;padding:1rem 1.2rem;margin:.9rem 0}}
 pre{{background:#f6f8fa;padding:.6rem;border-radius:6px;white-space:pre-wrap;overflow:auto}}
 .meta{{color:#555;font-size:.9rem}} .actions{{margin-top:.7rem}}
 .tagbtn{{display:inline-block;margin:.15rem .2rem;padding:.35rem .7rem;border-radius:6px;border:1px solid #999;
   cursor:pointer;font-size:.85rem;background:#fff;color:#1d4ed8;text-align:left}}
 .tagbtn:hover{{background:#eff6ff}}
 .tagbtn .t{{font-weight:700}} .tagbtn .n{{color:#555;font-size:.78rem}}
 .reject{{background:#fef2f2;color:#b91c1c;border-color:#c99;padding:.4rem .9rem;border-radius:6px;border:1px solid #ccc;cursor:pointer}}
 .done{{color:#059669;font-weight:600}} .error{{color:#b91c1c;background:#fef2f2;padding:.5rem .8rem;border-radius:6px}}
 .pagination{{margin:1rem 0;padding:.5rem;background:#f0f4ff;border-radius:6px}}
 .pagelink{{color:#1d4ed8;font-weight:600}} .pagelink.disabled{{color:#aaa}}
 .conf-legend span{{margin-right:1rem}} ul{{margin:.3rem 0 .3rem 1.2rem;padding:0}}
 .src{{color:#777;font-size:.8rem}} .era{{color:#777;font-size:.85rem;font-style:italic}}
</style></head><body>
 <h1>🎤 Singer-Tag Inference Approval</h1>
 <p class="meta">Pending: <b>{summary['pending']}</b> &nbsp;|&nbsp; Approved: <b>{summary['approved']}</b>
  &nbsp;|&nbsp; Rejected: <b>{summary['rejected']}</b>
  <span class="era">(click <b>any</b> singer button to approve that tag; Reject moves it to the bottom)</span></p>
 <p class="meta conf-legend">Confidence: <span style="color:#059669;font-weight:600">high (≥90)</span>
  <span style="color:#d97706;font-weight:600">medium (60–89)</span>
  <span style="color:#b91c1c;font-weight:600">low (&lt;60)</span></p>
 {error_html}
 {pagination}
 {rows}
 {pagination}
 <p class="meta">{bm_note}<br>
  Staging: <code>{os.path.basename(STAGING_PATH)}</code> · Canonical: <code>{os.path.basename(LYRICS_PATH)}</code> ·
  State: <code>{os.path.basename(DB_PATH)}</code></p>
</body></html>"""


def _band_members_note():
    try:
        doc = _read_json(ASSIGN_PATH)
        members = doc.get("band_members", [])
        if not members:
            return "Band-member tenure: <i>not loaded</i>."
        spans = ", ".join(f"{m['name']} ({m['start_date']}–{m['end_date']})" for m in members)
        cnt = len(members)
        return (f"Alternate-singer buttons are era-filtered using {cnt} band-member "
                f"tenure ranges in <code>gd_singer_assignments.json</code>: {spans}.")
    except Exception:
        return "Alternate-singer buttons: era-filtered by band-member tenure."


def _row_html(i):
    status = i.get("status", "pending")
    badge = f'<span class="badge {status}">{status}</span>'
    backup = i.get("backup_vocalists", [])
    backup_html = ", ".join(backup) if backup else "none"
    conf = i.get("confidence", "")
    conf_colors = {"high": "#059669", "medium": "#d97706", "low": "#b91c1c"}
    conf_color = conf_colors.get(conf, "#555")
    sources = ", ".join(i.get("sources", [])) or "n/a"
    year = i.get("year")
    year_html = f" &middot; era: {year}" if year else ""

    # Singer tag buttons: recommended first, then era-filtered alternates.
    buttons = []
    rec = (i.get("tag") or "").strip()
    buttons.append(
        f'<button class="tagbtn" name="tag" value="{rec}" form="f_{_safe(i["song_name"])}">'
        f'<span class="t">[{rec}]</span> <span class="n">{i.get("primary_vocalist","")} ✓ recommended</span></button>'
    )
    for alt in i.get("plausible_singers", []):
        atag = (alt.get("tag") or "").strip()
        buttons.append(
            f'<button class="tagbtn" name="tag" value="{atag}" form="f_{_safe(i["song_name"])}">'
            f'<span class="t">[{atag}]</span> <span class="n">{alt.get("name","")}</span></button>'
        )
    buttons_html = "\n".join(buttons)

    actions = f"""
    <form id="f_{_safe(i['song_name'])}" method="post" action="/action" class="actions">
      <input type="hidden" name="song_name" value="{_safe(i['song_name'])}">
      <input type="hidden" name="action" value="accept">
      {buttons_html}
    </form>
    <div class="actions">
      <form method="post" action="/action" style="display:inline">
        <input type="hidden" name="song_name" value="{_safe(i['song_name'])}">
        <button class="reject" name="action" value="reject">Pending (move to bottom)</button>
      </form>
    </div>"""

    notes = i.get("notes", "")
    notes_html = f"<p><b>Notes:</b> {notes}</p>" if notes else ""
    songwriter = i.get("songwriter", "unknown")
    writer_html = f"<b>Songwriter:</b> {songwriter}<br>" if songwriter and songwriter != "unknown" else ""
    return f"""<div class="card">
 <h3>{i['song_name']} {badge}{year_html}</h3>
 <p class="meta"><b>Recommended:</b> {i.get('primary_vocalist','')} ·
   <b>Confidence:</b> <span style="color:{conf_color};font-weight:600">{conf} ({i.get('confidence_score','')}/100)</span><br>
   {writer_html}<b>Backup vocalists:</b> {backup_html}<br>
   <b>Other plausible singers (era-filtered):</b></p>
 {actions}
 <p class="src"><b>Sources:</b> {sources} &middot; <b>Proposed tag:</b> <code>{i.get('proposed_tags','')}</code></p>
 {notes_html}
 <b>Lyrics preview:</b>
 <pre>{i.get('lyrics_preview','')}</pre>
</div>"""


def _safe(s):
    """Escape a value for safe embedding in HTML attribute/body text."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def main():
    db_init()
    print(f"Approval UI running at http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()