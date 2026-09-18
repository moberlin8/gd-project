#!/usr/bin/env python3
"""
gd_lyrics_scraper.py -- Grateful Dead song lyrics + interpretations harvester.

PERSONAL PRIVATE RESEARCH DATASET. Lyrics/notes are stored on local disk only
(data/gd_lyrics.json) and are never republished. Do not redistribute this file's
output.

Sources
-------
1. dead.net (PRIMARY, official, VERIFIED ACCESSIBLE with the polite UA) -- per-song
   pages /song/<slug> with "Lyrics By:", "Music By:", full lyrics and a body field
   carrying song notes/annotations ("(1) ..." footnotes referenced from the lyrics
   as "(note 1)"). Index: /songs?page=N (10 songs/page, 47 pages).
   NOTE: a desktop-Chrome User-Agent gets HTTP 403 from the Akamai edge; the polite
   UA "hal-dave/1.0 personal-research-bot" gets HTTP 200. Never impersonate a browser.
2. whitegum.com (FALLBACK lyrics + community notes, Alex Allan's "Grateful Dead
   Lyric And Song Finder", static HTML). Index: longlist.htm ->
   ~acsa/songfile/<CODE>.HTM. VERIFIED ACCESSIBLE.
3. reddit.com/r/gratefuldead "Song Discussion" flair (2nd-preference INTERPRETATION
   source per user direction 2026-09-14). PUBLIC JSON ENDPOINTS ARE BLOCKED:
   /r/gratefuldead/search.json -> HTTP 403 (bot wall); old.reddit.com -> HTTP 302.
   The scraper tries once, politely, at >= 4.5 s, records the exact code and leaves
   the per-song hook "pending-reddit-fetch (HTTP 403)". It does NOT bypass.
4. annotate.com -- DEMOTED to optional/third. The domain no longer hosts lyric
   annotations (corporate annotate.co product now). Source label 'annotate-optional'.

Interpretation entry schema
---------------------------
{"source": "dead.net-notes" | "whitegum-notes" | "reddit" | "annotate-optional",
 "text": str, "url": str, "date": optional str}
while an entry is waiting on a blocked source it also carries
"interpretation_hooks": ["pending-deadnet-browser-fetch", "pending-reddit-fetch (HTTP 403)"]
so a browser-based fetcher can fill them later.

House conventions (gd-project)
------------------------------
* >= 3.25 s between HTTP requests (--delay), globally enforced across hosts.
* Incremental save every 10 songs (SAVE_EVERY).
* Resumable state file: data/gd_lyrics_state.json (per song, per source).
* Polite UA: "hal-dave/1.0 personal-research-bot".
* robots.txt is fetched and RESPECTED for every host before any crawl
  (urllib.robotparser, checked with our own UA). Never bypass bot protections.
* Never hammer, never retry-hammer: bounded retries with exponential backoff,
  and 429/403 aborts that source rather than escalating.
* No post-1995 material (band rule); lyrics are undated repertoire, so this
  scraper only records songs, it never records post-1995 performances.

Subcommands
-----------
  --build-list           derive the song corpus from existing project data
                         (data/gd_comments_combined.json setlists) -> songlist
  --run --limit N        scrape up to N songs from BOTH sources; resumable
  --status               print state summary, no network

Nothing in this script mutates existing project files; it only writes the new
files listed in PATHS below.
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

# --------------------------------------------------------------------------
# HTTP layer: requests preferred, urllib fallback (stdlib-first, both fine)
# --------------------------------------------------------------------------
try:  # pragma: no cover - environment dependent
    import requests  # type: ignore

    _HAVE_REQUESTS = True
except Exception:  # pragma: no cover
    requests = None  # type: ignore
    _HAVE_REQUESTS = False

import urllib.error
import urllib.request

USER_AGENT = "hal-dave/1.0 personal-research-bot"
DELAY_SECONDS = 3.25
SAVE_EVERY = 10
MAX_RETRIES = 3
RETRY_BASE = 5.0

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"

PATHS = {
    "output": DATA_DIR / "gd_lyrics.json",
    "state": DATA_DIR / "gd_lyrics_state.json",
    "songlist": DATA_DIR / "gd_lyrics_songlist.json",
    "log": LOG_DIR / "gd_lyrics_scraper.log",
}

COMBINED_COMMENTS = DATA_DIR / "gd_comments_combined.json"

DEADNET_BASE = "https://www.dead.net"
DEADNET_INDEX = "https://www.dead.net/songs"
WHITEGUM_BASE = "https://www.whitegum.com"
WHITEGUM_INDEX = "https://www.whitegum.com/longlist.htm"

# Recorded, with the exact observations that produced them.
BLOCKERS = {
    "annotate.com": (
        "DEMOTED to optional/third (user direction 2026-09-14). The domain no longer "
        "hosts lyric annotations: https://www.annotate.com/ returns HTTP 200 but serves "
        "the corporate 'Annotate' document-annotation product (links to annotate.co/"
        "signin.php, /departments/...); /songs/... -> HTTP 404; /robots.txt -> HTTP 404. "
        "No Grateful Dead lyric annotations remain. source label if ever used: "
        "'annotate-optional'."
    ),
    "reddit.com/r/gratefuldead": (
        "BLOCKED for scripted clients. GET https://www.reddit.com/r/gratefuldead/"
        "search.json?q=flair%3ASong+Discussion&restrict_sr=1&sort=top&limit=25 -> "
        "HTTP 403 (bot-wall HTML body, not JSON). old.reddit.com variant -> HTTP 302. "
        "Not bypassed. Fill later with a browser session or the authenticated OAuth API; "
        "interpretation source label: 'reddit'."
    ),
    "dead.net (browser UA)": (
        "Sending a desktop Chrome User-Agent to /song/<slug> yields HTTP 403 "
        "'Access Denied' (Akamai edge). The polite UA 'hal-dave/1.0 "
        "personal-research-bot' is served normally (HTTP 200). Always use the "
        "polite UA -- never impersonate a browser or try to defeat the WAF."
    ),
}

_log = logging.getLogger("gd_lyrics")

# --------------------------------------------------------------------------
# Rate limiting + robots
# --------------------------------------------------------------------------
_last_request_at = 0.0
_robots: Dict[str, Optional[RobotFileParser]] = {}


def _sleep_for_politeness() -> None:
    global _last_request_at
    wait = DELAY_SECONDS - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def load_robots(url: str) -> Optional[RobotFileParser]:
    """Fetch + parse robots.txt for a URL's host (once per host). Respect it."""
    parsed = urlparse(url)
    host = f"{parsed.scheme}://{parsed.netloc}"
    if host in _robots:
        return _robots[host]
    rp: Optional[RobotFileParser] = None
    robots_url = host + "/robots.txt"
    try:
        _sleep_for_politeness()
        body, status = _raw_fetch(robots_url)
        if status == 200 and body:
            rp = RobotFileParser()
            rp.parse(body.splitlines())
            _log.info("robots.txt loaded for %s (%d bytes)", host, len(body))
        else:
            # No robots.txt (404) => no restrictions published. Stay polite anyway.
            _log.info("robots.txt for %s -> HTTP %s (treating as unrestricted)", host, status)
    except Exception as exc:  # network hiccup: be conservative
        _log.warning("robots.txt fetch failed for %s: %s", host, exc)
    _robots[host] = rp
    return rp


def robots_allows(url: str) -> bool:
    rp = load_robots(url)
    if rp is None:
        return True
    allowed = rp.can_fetch(USER_AGENT, url)
    if not allowed:
        _log.warning("robots.txt DISALLOWS %s -- skipping", url)
    return allowed


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------
def _raw_fetch(url: str) -> Tuple[str, int]:
    """One HTTP GET with the polite UA. Returns (body_text, status_code)."""
    if _HAVE_REQUESTS and requests is not None:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        return resp.text, resp.status_code
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", errors="replace"), r.status
    except urllib.error.HTTPError as e:
        return e.read().decode("utf-8", errors="replace"), e.code


def fetch(url: str, *, label: str = "") -> Optional[str]:
    """Polite, robots-respecting GET with bounded backoff. None on hard failure."""
    if not robots_allows(url):
        return None
    for attempt in range(1, MAX_RETRIES + 1):
        _sleep_for_politeness()
        try:
            body, status = _raw_fetch(url)
        except Exception as exc:
            _log.warning("fetch error %s (%s) attempt %d: %s", url, label, attempt, exc)
            body, status = "", 0
        if status == 200 and body:
            return body
        if status in (403, 451):
            _log.error(
                "HTTP %s for %s -- bot protection / access denied. "
                "NOT retrying and NOT bypassing.", status, url,
            )
            return None
        if status == 404:
            _log.info("HTTP 404 for %s (%s)", url, label)
            return None
        if status == 429 or status >= 500 or status == 0:
            wait = RETRY_BASE * (2 ** (attempt - 1))
            _log.warning("HTTP %s for %s -- backing off %.1fs (attempt %d/%d)",
                         status, url, wait, attempt, MAX_RETRIES)
            time.sleep(wait)
            continue
        _log.warning("unexpected HTTP %s for %s", status, url)
        return None
    _log.error("giving up on %s after %d attempts", url, MAX_RETRIES)
    return None


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------
def strip_tags(fragment: str) -> str:
    fragment = re.sub(r"(?i)<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"(?i)</p>\s*<p[^>]*>", "\n\n", fragment)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    return html_mod.unescape(fragment)


def normalize_name(name: str) -> str:
    """Aggressive key used to match song names across sources.

    Handles the setlist-source variants that otherwise silently miss:
    'E: U.S. Blues', 'Me & My Uncle' vs 'Me and My Uncle', "Playin' In The Band"
    vs 'Playing In The Band', 'Touch Of Gray' vs 'Touch of Grey'.
    """
    n = html_mod.unescape(name).lower()
    n = n.replace("&", " and ")
    # leading set/encore markers: "E:", "E1:", "Encore:", "Set II -", "1."
    n = re.sub(r"^\s*(?:e\d*|encore|set\s*(?:i{1,3}|\d)|1st set|2nd set|\d+)\s*[:\-\.]\s*", "", n)
    n = re.sub(r"^\s*\((?:[^)]*)\)\s*", "", n)        # leading "(Baby)" etc.
    n = re.sub(r"\bgrey\b", "gray", n)
    n = re.sub(r"\bplayin\b", "playing", n)
    n = re.sub(r"\bnfa\b", "not fade away", n)
    n = re.sub(r"\bwrs\b", "weather report suite", n)
    n = re.sub(r"\bthe\b", " ", n)
    n = re.sub(r"[^a-z0-9]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


class NameIndex:
    """norm-name -> value, with a conservative fuzzy fallback (difflib).

    ALIASES maps setlist-side titles onto the canonical title used by the lyric
    sites. Only add pairs you have actually verified are the same song.
    """

    ALIASES = {
        "minglewood blues": "new minglewood blues",
        "women are smarter": "man smart woman smarter",
        "woman are smarter": "man smart woman smarter",
        "man smart": "man smart woman smarter",
        "sunshine daydream": "sunshine daydream",
        "c c rider": "cc rider",
        "big boss man": "big boss man",
        "going down the road feeling bad": "go down road feeling bad",
        "i ko i ko": "iko iko",
    }

    def __init__(self, mapping: Dict[str, str]):
        self.map = mapping
        self.keys: Optional[List[str]] = None

    def get(self, name: str, cutoff: float = 0.9) -> Optional[str]:
        key = normalize_name(name)
        key = self.ALIASES.get(key, key)
        if key in self.map:
            return self.map[key]
        import difflib

        if self.keys is None:
            self.keys = list(self.map)
        hits = difflib.get_close_matches(key, self.keys, n=1, cutoff=cutoff)
        return self.map[hits[0]] if hits else None

    def __len__(self) -> int:
        return len(self.map)


def clean_song_name(name: str) -> str:
    n = html_mod.unescape(name)
    n = re.sub(r"\s+", " ", n).strip()
    n = re.sub(r"^[\*\#$%\-\u2013\u2014]+\s*", "", n)
    n = re.sub(r"^\(\d+\)\s*", "", n)
    n = re.sub(r"\s*\(note \d+\)\s*$", "", n, flags=re.I)
    n = re.sub(r"\s*(?:&gt;|>|->|,)+\s*$", "", n)
    return n.strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Song-list construction (from EXISTING project data, never a fresh crawl)
# --------------------------------------------------------------------------
# Crew/credit lines and setlist noise that must not enter the song corpus.
CREDIT_LINE = re.compile(
    r"^[A-Z][\w\.'\- ]{0,30}\s*-\s*(Guitar|Drums|Bass|Keyboards?|Vocals?|Piano|"
    r"Organ|Harmonica|Sax(ophone)?|Percussion|Trombone|Trumpet|Fiddle|Violin|"
    r"Mandolin|Banjo|Squeezebox)\b",
    re.I,
)
NOISE_LINE = re.compile(
    r"(other artist|weir on|acoustic guitar|\(\d+ mins|^\(set |^set [12]\b|^encore$|"
    r"^soundcheck|tuning|tease|splice|^missing |last played|first live|^\(unknown|"
    r"^\(late show|http|\.com|^\w\d+t\d+$|^[-=*#_%$'\"]{2,}|^\+|^\^\^?|^\.\.|^//|"
    r"^comment\b|^banter\b|^crowd\b|^applause\b|^chatter\b|^intro\b|^outro\b|"
    r"^jam\b|^drums\b|^space\b|^feedback\b|^tuning\b|^dead air\b|^talk\b)",
    re.I,
)


def build_songlist() -> Dict[str, Any]:
    """Union of setlist song names from gd_comments_combined.json.

    Source of truth: setlists[*].sets[*].songs[*].name (2,626 parsed entries),
    falling back to setlists[*].songs (flat list) where present.
    """
    if not COMBINED_COMMENTS.exists():
        raise SystemExit(f"missing {COMBINED_COMMENTS}")
    with open(COMBINED_COMMENTS, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    setlists = data.get("setlists") or {}

    counts: Dict[str, int] = {}
    for _key, entry in setlists.items():
        for st in entry.get("sets") or []:
            for song in st.get("songs") or []:
                name = song.get("name") if isinstance(song, dict) else song
                if isinstance(name, str) and name.strip():
                    counts.setdefault(name.strip(), 0)
                    counts[name.strip()] += 1
        for name in entry.get("songs") or []:
            if isinstance(name, str) and name.strip():
                counts.setdefault(name.strip(), 0)
                counts[name.strip()] += 1

    raw_unique = len(counts)
    cleaned: Dict[str, int] = {}
    for name, cnt in counts.items():
        if len(name) > 60 or CREDIT_LINE.match(name) or NOISE_LINE.search(name):
            continue
        name = clean_song_name(name)
        if not (2 <= len(name) <= 60):
            continue
        cleaned[name] = cleaned.get(name, 0) + cnt

    songs = sorted(cleaned.items(), key=lambda kv: (-kv[1], kv[0]))
    payload = {
        "generated_at": now_iso(),
        "source_file": str(COMBINED_COMMENTS),
        "setlist_entries": len(setlists),
        "raw_unique_names": raw_unique,
        "unique_songs": len(songs),
        "songs": [{"name": n, "setlist_mentions": c} for n, c in songs],
    }
    PATHS["songlist"].parent.mkdir(parents=True, exist_ok=True)
    with open(PATHS["songlist"], "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, ensure_ascii=False)
    _log.info(
        "songlist: %d unique songs from %d setlist entries (%d raw names, noise/crew filtered)",
        len(songs), len(setlists), raw_unique,
    )
    return payload


# --------------------------------------------------------------------------
# Source indexes
# --------------------------------------------------------------------------
def fetch_deadnet_index(max_pages: int = 200) -> Dict[str, str]:
    """dead.net /songs paginated index -> {normalized_name: slug}."""
    index: Dict[str, str] = {}
    page = 0
    while page < max_pages:
        url = DEADNET_INDEX if page == 0 else f"{DEADNET_INDEX}?page={page}"
        body = fetch(url, label=f"dead.net index p{page}")
        if not body:
            break
        found = re.findall(
            r'href="(/song/[^"#?]+)"[^>]*>(.*?)</a>', body, re.S
        )
        if not found:
            break
        for href, title in found:
            slug = href.rsplit("/", 1)[-1]
            name = strip_tags(title).strip()
            if name:
                index[normalize_name(name)] = slug
        if len(found) < 10:  # last page
            break
        page += 1
    _log.info("dead.net index: %d songs (%d pages)", len(index), page + 1)
    return index


def fetch_whitegum_index() -> Dict[str, str]:
    """whitegum.com longlist.htm -> {normalized_name: absolute song URL}."""
    index: Dict[str, str] = {}
    body = fetch(WHITEGUM_INDEX, label="whitegum index")
    if not body:
        return index
    for href, title in re.findall(
        r'HREF=["\']?(/~acsa/songfile/[^"\'>]+)["\']?[^>]*>(.*?)</A>', body, re.I | re.S
    ):
        name = strip_tags(title).strip()
        if name:
            index.setdefault(normalize_name(name), urljoin(WHITEGUM_BASE, href))
    _log.info("whitegum index: %d songs", len(index))
    return index


# --------------------------------------------------------------------------
# dead.net song page parsing
# --------------------------------------------------------------------------
def _field_block(doc: str, field: str, anchor: Optional[str] = None) -> str:
    """Raw HTML of a Drupal field, bounded by the next field div / </article>.

    NOTE: must anchor on the exact field name -- 'field--name-field-lyrics' is a
    prefix of 'field--name-field-lyrics-by', so a naive find() returns the wrong
    field (this cost a debug cycle). NOTE 2: 'field--name-body' also appears in
    dozens of inline CSS/JS blocks, so callers pass `anchor` (e.g. the
    'node__content' wrapper) to land on the real node body.
    """
    if anchor:
        a = doc.find(anchor)
        doc = doc[a:] if a != -1 else doc
    m = re.search(
        r'<div class="[^"]*(?:field--name-field-' + re.escape(field) + r'|field--name-' + re.escape(field) + r')(?=["\s])',
        doc,
    )
    if not m:
        return ""
    tail = doc[m.end():]
    ends = [x.start() for x in re.finditer(r'class="[^"]*field--name-field-', tail)]
    ends.append(tail.find("</article>"))
    ends = [e for e in ends if e != -1]
    cut = min(ends) if ends else len(tail)
    return tail[:cut]


def _field_items(block: str) -> List[str]:
    out: List[str] = []
    for raw in re.findall(r'<div class="field__item">([^<]*)</div>', block):
        val = html_mod.unescape(raw).strip()
        val = re.sub(r"\s*\(note \w+\)\s*$", "", val, flags=re.I).strip()
        if val:
            out.append(val)
    return out


# Site chrome that shares the 'field--name-body' class but is not a song note.
BOILERPLATE = re.compile(
    r"(get on the list|sign ?up|newsletter|subscribe|cookie|privacy policy|"
    r"tapers section|all the years live|jam of the week|comix by)",
    re.I,
)


def _find_body_block(doc: str, anchor: str = 'class="node__content"') -> str:
    """The node's body field: the first 'field--name-body' div holding real prose.

    'field--name-body' also occurs in dozens of inline CSS/JS blocks and site
    navigation blocks, so we search only AFTER the node content wrapper (falling
    back to the whole document), take candidates in order, and keep the first one
    whose text survives style/script stripping.
    """
    if anchor:
        a = doc.find(anchor)
        if a != -1:
            doc = doc[a:]
    for m in re.finditer(r'<div class="[^"]*field--name-body(?=["\s])', doc):
        tail = doc[m.end():]
        ends = [x.start() for x in re.finditer(r'class="[^"]*field--name-field-', tail)]
        ends.append(tail.find("</article>"))
        ends = [e for e in ends if e != -1]
        blk = tail[:min(ends)] if ends else tail
        cleaned = re.sub(r"(?is)<(style|script)\b.*?</\1>", " ", blk)
        txt = re.sub(r"\s+", " ", strip_tags(cleaned)).strip()
        if len(txt) <= 40 or txt.count("{") >= 3:
            continue
        if BOILERPLATE.search(txt):
            continue
        return blk
    return ""


def parse_deadnet_song(slug: str, doc: str) -> Dict[str, Any]:
    title_m = re.search(r"<title>([^<]*)</title>", doc)
    title = strip_tags(title_m.group(1)).split("|")[0].strip() if title_m else slug

    lyrics_block = _field_block(doc, "lyrics", anchor='class="node__content"')
    lyrics_text = ""
    if lyrics_block:
        body = lyrics_block.split('<div class="field__label">Lyrics</div>', 1)[-1]
        lyrics_text = strip_tags(body)
        lyrics_text = re.sub(r"[ \t]+\n", "\n", lyrics_text)
        lyrics_text = re.sub(r"\n{3,}", "\n\n", lyrics_text).strip()

    lyricists = _field_items(_field_block(doc, "lyrics-by", anchor='class="node__content"'))
    music_by = _field_items(_field_block(doc, "music-by", anchor='class="node__content"'))

    # Song notes / annotations live in the node body field.
    interpretations: List[Dict[str, str]] = []
    url = f"{DEADNET_BASE}/song/{slug}"
    body_block = _find_body_block(doc)
    if body_block:
        body_html = re.sub(r"(?is)<(style|script)\b.*?</\1>", " ", body_block)
        body_html = re.sub(r"^[^<]*>", "", body_html, count=1)  # drop partial tag remnant
        paras = re.findall(r"<p[^>]*>(.*?)</p>", body_html, re.S)
        if not paras:
            txt = strip_tags(body_html).strip()
            paras = [txt] if txt else []
        for p in paras:
            txt = re.sub(r"\s+", " ", strip_tags(p)).strip()
            txt = re.sub(r"<[^>]*$", "", txt).strip()   # drop truncated trailing tag
            if not txt:
                continue
            if re.match(r"^\(\d+\)", txt) or len(txt) > 15:
                interpretations.append({"source": "dead.net-notes", "text": txt, "url": url})

    first_played = None
    fp = re.search(r"first played[^.]*?(\d{1,2}\s+\w{3,9}\s+\d{4}|\d{4})", doc, re.I)
    if fp:
        first_played = fp.group(1)

    return {
        "title": title or slug,
        "lyricists": lyricists,
        "music_by": music_by,
        "lyrics_text": lyrics_text,
        "interpretations": interpretations,
        "first_played_hint": first_played,
    }


# --------------------------------------------------------------------------
# whitegum song page parsing
# --------------------------------------------------------------------------
def parse_whitegum_song(url: str, doc: str) -> Dict[str, Any]:
    title_m = re.search(r"<title>([^<]*)</title>", doc, re.I)
    title = strip_tags(title_m.group(1)).strip() if title_m else url

    text = re.sub(r"(?i)<br\s*/?>", "\n", doc)
    text = strip_tags(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)

    lyricists: List[str] = []
    music_by: List[str] = []
    m = re.search(r"^\s*Lyrics:\s*(.+)$", text, re.M)
    if m:
        lyricists = [x.strip() for x in re.split(r"[,/&]| and ", m.group(1)) if x.strip()]
    m2 = re.search(r"^\s*Music:\s*(.+)$", text, re.M)
    if m2:
        music_by = [x.strip() for x in re.split(r"[,/&]| and ", m2.group(1)) if x.strip()]
    # whitegum puts the lyric's "(note a)" markers on the credits line too
    lyricists = [re.sub(r"\s*\(note \w+\)\s*$", "", x).strip() for x in lyricists]
    music_by = [re.sub(r"\s*\(note \w+\)\s*$", "", x).strip() for x in music_by]
    lyricists = [x for x in lyricists if x]
    music_by = [x for x in music_by if x]

    # Lyrics run from just after the "Music:" line to the Notes / recordings block.
    lyrics_text = ""
    lm = re.search(
        r"^\s*Music:\s*.+$\n(.*?)(?=\nNotes\n|\nGrateful Dead Recordings\b|\n"
        r"For more information\b|\nFurther Information\b|\nDiscograph)",
        text, re.S | re.M,
    )
    if lm:
        lyrics_text = re.sub(r"\n{3,}", "\n\n", lm.group(1)).strip()
    if lyrics_text and lyrics_text.strip() in ("(instrumental)", "Instrumental", "instrumental"):
        lyrics_text = ""

    # Community notes == interpretations.
    interpretations: List[Dict[str, str]] = []
    nm = re.search(r"\nNotes\n(.*?)(?=\nGrateful Dead Recordings\b|\nFor more information\b|"
                   r"\nFurther Information\b|\nHome\n\||\Z)", text, re.S)
    if nm:
        for note in re.split(r"\n(?=\(\w+\))", nm.group(1).strip()):
            note = re.sub(r"\s+", " ", note).strip()
            if len(note) > 10:
                interpretations.append({"source": "whitegum-notes", "text": note, "url": url})

    return {
        "title": title,
        "lyricists": lyricists,
        "music_by": music_by,
        "lyrics_text": lyrics_text,
        "interpretations": interpretations,
        "first_played_hint": None,
    }


# --------------------------------------------------------------------------
# Reddit interpretations (2nd preference per user direction 2026-09-14)
# --------------------------------------------------------------------------
# r/gratefuldead "Song Discussion" flair threads, plus lyricsfuge crossposts,
# are the wanted material. Reddit hard-blocks scripted clients on the public
# JSON endpoints (HTTP 403 with a bot-wall HTML body). We try ONCE, politely,
# at >= 4.5 s pacing, and if it is blocked we record the exact status code and
# leave a schema hook -- we do NOT attempt to defeat the block.
REDDIT_DELAY = 4.5
REDDIT_SEARCH = (
    "https://www.reddit.com/r/{sub}/search.json"
    "?q=flair%3ASong%20Discussion&restrict_sr=1&sort=top&limit=100"
)
REDDIT_SUBREDDIT = "gratefuldead"


def _reddit_fetch(url: str) -> Tuple[Optional[str], int]:
    global _last_request_at
    wait = max(REDDIT_DELAY, DELAY_SECONDS) - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()
    try:
        return _raw_fetch(url)
    except Exception as exc:
        _log.warning("reddit fetch error for %s: %s", url, exc)
        return None, 0


def fetch_reddit_discussions(max_pages: int = 5) -> Tuple[List[Dict[str, str]], Optional[Dict[str, Any]]]:
    """Return (posts, blocker). blocker is set (never raised) if Reddit blocks us."""
    posts: List[Dict[str, str]] = []
    url = REDDIT_SEARCH.format(sub=REDDIT_SUBREDDIT)
    for page in range(max_pages):
        body, status = _reddit_fetch(url)
        if status == 200 and body:
            try:
                payload = json.loads(body)
            except Exception:
                _log.warning("reddit returned non-JSON (len=%d)", len(body))
                break
            children = (payload.get("data") or {}).get("children") or []
            for ch in children:
                d = ch.get("data") or {}
                if d.get("title"):
                    posts.append({
                        "title": d["title"],
                        "text": (d.get("selftext") or "")[:4000],
                        "url": "https://www.reddit.com" + (d.get("permalink") or ""),
                        "created": datetime.fromtimestamp(
                            d.get("created_utc") or 0, timezone.utc
                        ).strftime("%Y-%m-%d"),
                    })
            after = (payload.get("data") or {}).get("after")
            if not after or not children:
                break
            url = REDDIT_SEARCH.format(sub=REDDIT_SUBREDDIT) + f"&after={after}"
            continue
        blocker = {
            "source": "reddit",
            "status_code": status,
            "url": url,
            "note": ("Reddit public JSON endpoint refused scripted access "
                     f"(HTTP {status}). Not bypassed. Browser-based fetch or the "
                     "authenticated API (OAuth client credentials) can fill this later."),
            "checked_at": now_iso(),
        }
        _log.warning("reddit: HTTP %s for %s -- recording as blocked, not retrying",
                     status, url)
        return posts, blocker
    _log.info("reddit: collected %d discussion posts", len(posts))
    return posts, None


def match_reddit_posts(posts: List[Dict[str, str]]) -> Dict[str, List[Dict[str, str]]]:
    """Map normalized song name -> reddit posts whose title mentions it."""
    out: Dict[str, List[Dict[str, str]]] = {}
    for p in posts:
        nt = normalize_name(p["title"])
        for word in ("song discussion", "official", "discussion"):
            nt = nt.replace(word, " ")
        nt = re.sub(r"\s+", " ", nt).strip()
        out.setdefault(nt, []).append(p)
    return out


def reddit_for_song(song: str, by_title: Dict[str, List[Dict[str, str]]]) -> List[Dict[str, str]]:
    """Interpretation entries for one song, from Reddit discussion titles."""
    norm = normalize_name(song)
    if len(norm) < 4:
        return []
    hits: List[Dict[str, Any]] = []
    for key, posts in by_title.items():
        if re.search(r"\b" + re.escape(norm) + r"\b", key):
            for p in posts:
                text = p["title"]
                if p.get("text"):
                    text = f"{text} -- {p['text'][:600]}"
                hits.append({
                    "source": "reddit",
                    "text": re.sub(r"\s+", " ", text).strip(),
                    "url": p["url"],
                    "date": p.get("created"),
                })
    return hits[:5]


# --------------------------------------------------------------------------
# State + output
# --------------------------------------------------------------------------
def load_json(path: Path, default: Any) -> Any:
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:
            _log.error("could not read %s (%s); starting fresh", path, exc)
    return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, ensure_ascii=False)
    tmp.replace(path)


def new_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "delay_seconds": DELAY_SECONDS,
        "user_agent": USER_AGENT,
        "sources": {
            "dead.net": {"status": "untested", "url": DEADNET_BASE},
            "whitegum.com": {"status": "untested", "url": WHITEGUM_BASE},
            "reddit.com/r/gratefuldead": {"status": "untested", "url": REDDIT_SEARCH.format(sub=REDDIT_SUBREDDIT)},
            "annotate.com": {"status": "blocked-optional", "note": BLOCKERS["annotate.com"]},
        },
        "blockers": BLOCKERS,
        "indexes": {"dead_net": {}, "whitegum": {}},
        "reddit_posts": [],
        "reddit_blocker": None,
        "indexes_built_at": None,
        "songs_total": 0,
        "done": {},           # song_key -> {"sources": {"dead.net": ts, "whitegum": ts}}
        "coverage": {},
        "runs": [],
    }


def merge_entry(entry: Dict[str, Any], key: str, src: str, parsed: Dict[str, Any], url: str) -> None:
    entry.setdefault("title", parsed.get("title") or key)
    entry.setdefault("slug(s)", {})
    entry["slug(s)"][src] = url.rsplit("/", 1)[-1]
    urls = entry.setdefault("source_urls", [])
    if url not in urls:
        urls.append(url)
    for field in ("lyricists", "music_by"):
        have = entry.setdefault(field, [])
        for v in parsed.get(field) or []:
            if v and v not in have:
                have.append(v)
    # prefer the longer lyrics rendering
    if parsed.get("lyrics_text") and len(parsed["lyrics_text"]) > len(entry.get("lyrics_text") or ""):
        entry["lyrics_text"] = parsed["lyrics_text"]
    interps = entry.setdefault("interpretations", [])
    seen = {(i.get("source"), i.get("text")) for i in interps}
    for item in parsed.get("interpretations") or []:
        if (item.get("source"), item.get("text")) not in seen:
            interps.append(item)
            seen.add((item.get("source"), item.get("text")))
    if parsed.get("first_played_hint") and not entry.get("first_played_hint"):
        entry["first_played_hint"] = parsed["first_played_hint"]
    entry["scraped_at"] = now_iso()


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def cmd_build_list() -> int:
    payload = build_songlist()
    print(f"songlist -> {PATHS['songlist']}")
    print(f"  setlist entries : {payload['setlist_entries']}")
    print(f"  raw unique names: {payload['raw_unique_names']}")
    print(f"  unique songs    : {payload['unique_songs']}")
    top = payload["songs"][:10]
    print("  most-played     : " + ", ".join(f"{s['name']} ({s['setlist_mentions']})" for s in top))
    return 0


def cmd_status() -> int:
    state = load_json(PATHS["state"], None)
    if not state:
        print(f"no state file at {PATHS['state']} -- run --build-list / --run first")
        return 0
    out = load_json(PATHS["output"], {})
    print(f"state file     : {PATHS['state']}  (updated {state.get('updated_at')})")
    print(f"output file    : {PATHS['output']}")
    print(f"delay / UA     : {state.get('delay_seconds')}s / {state.get('user_agent')}")
    print(f"songs in output: {len(out)}")
    print(f"songs completed: {len(state.get('done') or {})} / {state.get('songs_total')}")
    print("sources        :")
    for name, meta in (state.get("sources") or {}).items():
        print(f"  - {name:16s} {meta.get('status')}")
    idx = state.get("indexes") or {}
    print(f"indexes        : dead.net {len(idx.get('dead_net') or {})} | whitegum {len(idx.get('whitegum') or {})}")
    cov = state.get("coverage") or {}
    if cov:
        print(f"coverage       : {cov}")
    runs = state.get("runs") or []
    if runs:
        last = runs[-1]
        print(f"last run       : {last.get('started_at')} limit={last.get('limit')} "
              f"fetched={last.get('fetched')} skipped={last.get('skipped')}")
    return 0


def cmd_run(limit: int, sources: List[str]) -> int:
    songlist = load_json(PATHS["songlist"], None)
    if not songlist:
        _log.info("no songlist yet -- building it from project data")
        songlist = build_songlist()

    state = load_json(PATHS["state"], None) or new_state()
    state.setdefault("indexes", {"dead_net": {}, "whitegum": {}})
    state.setdefault("done", {})
    out = load_json(PATHS["output"], {})

    run_info = {"started_at": now_iso(), "limit": limit, "sources": sources,
                "fetched": 0, "skipped": 0}

    def _checkpoint() -> None:
        state["updated_at"] = now_iso()
        state["songs_total"] = len(out)
        save_json(PATHS["state"], state)
        save_json(PATHS["output"], out)

    def _on_sigterm(_signum, _frame):
        _log.warning("SIGTERM received -- checkpointing state and exiting")
        _checkpoint()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_sigterm)

    def _src_status(name: str, status: str) -> None:
        state.setdefault("sources", {}).setdefault(name, {})["status"] = status

    # ---- indexes (cached in state; fetched once) ----
    if "dead.net" in sources and not state["indexes"].get("dead_net"):
        idx = fetch_deadnet_index()
        state["indexes"]["dead_net"] = idx
        _src_status("dead.net", "ok" if idx else "unreachable")
        _checkpoint()
    if "whitegum" in sources and not state["indexes"].get("whitegum"):
        idx = fetch_whitegum_index()
        state["indexes"]["whitegum"] = idx
        _src_status("whitegum.com", "ok" if idx else "unreachable")
        _checkpoint()
    reddit_by_title: Dict[str, List[Dict[str, str]]] = {}
    if "reddit" in sources:
        if state.get("reddit_posts"):
            reddit_by_title = match_reddit_posts(state["reddit_posts"])
        elif state.get("reddit_blocker"):
            _log.info("reddit already recorded as blocked (HTTP %s) -- skipping",
                      (state["reddit_blocker"] or {}).get("status_code"))
        else:
            posts, blocker = fetch_reddit_discussions()
            if blocker:
                state["reddit_blocker"] = blocker
                _src_status("reddit.com/r/gratefuldead", f"blocked (HTTP {blocker['status_code']})")
            else:
                state["reddit_posts"] = posts
                _src_status("reddit.com/r/gratefuldead", "ok" if posts else "ok (no posts)")
                reddit_by_title = match_reddit_posts(posts)
            _checkpoint()
    state["indexes_built_at"] = state.get("indexes_built_at") or now_iso()

    dn_index = NameIndex(state["indexes"].get("dead_net") or {})
    wg_index = NameIndex(state["indexes"].get("whitegum") or {})

    # ---- pick the next N unfinished songs (most-played first) ----
    selected: List[Dict[str, Any]] = []
    for song in songlist["songs"]:
        key = song["name"]
        done_srcs = set((state["done"].get(key) or {}).get("sources", {}))
        if done_srcs >= set(sources):
            continue
        selected.append(song)
        if len(selected) >= limit:
            break

    misses: Dict[str, List[str]] = {"dead.net": [], "whitegum": [], "reddit": []}
    fetched = 0
    reddit_blocked = bool(state.get("reddit_blocker"))
    for i, song in enumerate(selected, 1):
        key = song["name"]
        norm = normalize_name(key)
        entry = out.get(key) or {"title": key}
        done = state["done"].setdefault(key, {"sources": {}})
        for src in sources:
            if src in done["sources"]:
                continue
            if src == "reddit":
                if reddit_blocked:
                    # Reddit refuses scripted access: leave the schema hook and
                    # mark the source done so we never hammer it from a cron run.
                    done.setdefault("hooks", [])
                    if "pending-reddit-fetch (HTTP 403)" not in done["hooks"]:
                        done["hooks"].append("pending-reddit-fetch (HTTP 403)")
                    done["sources"]["reddit"] = "blocked"
                    continue
                hits = reddit_for_song(key, reddit_by_title)
                if not hits:
                    misses["reddit"].append(key)
                    continue
                merge_entry(entry, key, "reddit", {"interpretations": hits, "title": entry.get("title")},
                            hits[0]["url"])
                done["sources"]["reddit"] = now_iso()
                fetched += 1
                continue
            if src == "dead.net":
                slug = dn_index.get(norm)
                if not slug:
                    misses["dead.net"].append(key)
                    continue
                url = f"{DEADNET_BASE}/song/{slug}"
                doc = fetch(url, label=f"dead.net {slug}")
                if not doc:
                    continue
                parsed = parse_deadnet_song(slug, doc)
            else:
                url = wg_index.get(norm)
                if not url:
                    misses["whitegum"].append(key)
                    continue
                if "#" in url:
                    # Shared jam/instrumental page (JAMS.HTM#drums etc): the notes on
                    # it belong to whichever song, so keep the link but no text.
                    parsed = {"title": key, "lyricists": [], "music_by": [],
                              "lyrics_text": "", "interpretations": [],
                              "first_played_hint": None}
                else:
                    doc = fetch(url, label=f"whitegum {key}")
                    if not doc:
                        continue
                    parsed = parse_whitegum_song(url, doc)
            merge_entry(entry, key, src, parsed, url)
            done["sources"][src] = now_iso()
            fetched += 1

        # Schema hook: which interpretation sources were expected but not captured.
        hooks = []
        if "dead.net" in sources and not any(
                x.get("source") == "dead.net-notes" for x in entry.get("interpretations", [])):
            hooks.append("pending-deadnet-browser-fetch")
        if reddit_blocked:
            hooks.append("pending-reddit-fetch (HTTP 403)")
        if hooks:
            entry["interpretation_hooks"] = sorted(set(hooks))

        out[key] = entry
        run_info["fetched"] = fetched
        if i % SAVE_EVERY == 0:
            _log.info("checkpoint after %d/%d songs (%d fetches)", i, len(selected), fetched)
            _checkpoint()

    run_info["skipped"] = sum(1 for m in misses.values() for _ in m)
    run_info["finished_at"] = now_iso()
    state.setdefault("runs", []).append(run_info)

    total = len(songlist["songs"])
    in_dn = sum(1 for s in songlist["songs"] if dn_index.get(s["name"]) is not None)
    in_wg = sum(1 for s in songlist["songs"] if wg_index.get(s["name"]) is not None)
    state["coverage"] = {
        "corpus_songs": total,
        "matched_dead_net": in_dn,
        "matched_whitegum": in_wg,
        "unmatched_dead_net": total - in_dn,
        "unmatched_whitegum": total - in_wg,
    }
    _checkpoint()

    with_lyrics = [k for k, v in out.items() if v.get("lyrics_text")]
    with_interp = [k for k, v in out.items() if v.get("interpretations")]
    print(f"run complete: {fetched} page(s) fetched | {len(with_lyrics)} songs with lyrics | "
          f"{len(with_interp)} songs with interpretations")
    print(f"coverage: {state['coverage']}")
    if reddit_blocked:
        print(f"reddit : BLOCKED (HTTP {state['reddit_blocker']['status_code']}) -- "
              f"hooks left per song")
    for name in ("dead.net", "whitegum", "reddit"):
        if misses.get(name):
            print(f"  {name} misses: {len(misses[name])} (sample: {misses[name][:5]})")
    print(f"output -> {PATHS['output']}")
    print(f"state  -> {PATHS['state']}")
    return 0


# --------------------------------------------------------------------------
def main(argv: Optional[Iterable[str]] = None) -> int:
    global DELAY_SECONDS
    p = argparse.ArgumentParser(description="GD lyrics + interpretations scraper (private research dataset)")
    p.add_argument("--build-list", action="store_true", help="derive song list from gd_comments_combined.json")
    p.add_argument("--run", action="store_true", help="scrape songs (resumable)")
    p.add_argument("--status", action="store_true", help="print state summary")
    p.add_argument("--limit", type=int, default=0, help="max songs to process this run")
    p.add_argument("--delay", type=float, default=DELAY_SECONDS, help="seconds between requests (default 3.25)")
    p.add_argument("--sources", default="dead.net,whitegum,reddit", help="comma list: dead.net,whitegum,reddit")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(list(argv) if argv is not None else None)

    DELAY_SECONDS = args.delay

    PATHS["log"].parent.mkdir(parents=True, exist_ok=True)
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(PATHS["log"], encoding="utf-8"))
    except Exception:
        pass
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=handlers,
    )

    if args.build_list:
        return cmd_build_list()
    if args.status:
        return cmd_status()
    if args.run:
        if args.limit <= 0:
            p.error("--run requires --limit N (use --limit 0 only via explicit full-run flag)")
        sources = [s.strip() for s in args.sources.split(",") if s.strip()]
        return cmd_run(args.limit, sources)
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
