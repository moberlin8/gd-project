# Scraper Code Review — Grateful Dead gd-project
Reviewer: Ada · READ-ONLY review · 2026-09-18

## FACTS (measured, not guessed)
- LOC: `gd_comment_scraper.py` 736 · `gd_lyrics_scraper.py` 1069 · `gdao_site_crawler.py` 238 · `reddit_scraper.py` 196 (2277 total).
- `data/scraper_state.json`: 7,330 shows processed, 4,572 with comments; `current_year=1965`, **all 31 years (1965–95) `fully_scanned`**; metadata claims `comments_total=29,901` and `setlists_total=7,330` — but `gd_comments_combined.json` actually holds **979 comments / 2,626 setlists**.
- `data/gd_lyrics_state.json`: corpus `3,072` song names, **322 done** (284 with lyrics, 259 with interpretations); dead.net index **459**, whitegum index 3,547; `coverage` reports 1,928 corpus names with no dead.net match, 1,845 no whitegum match.

Findings are ordered by impact. Each fix is minimal; none require rearchitecting.

---

## 1. [HIGH · comment coverage] The keyword filter drops ~97% of comments.

> **Hal's correction (verified 2026-09-18):** the keyword gate is real but is NOT the main loss.
> The list is broad (`show`, `set`, `dead`, `play`…) and the 29,901 counter is *post-filter*
> (line 685 counts `all_comments`). The comments were collected, then **lost**: `save_combined_data`
> (lines 139–140) writes the 20 MB file in place with `open(..., "w")`; a crash mid-dump on Sep 2
> truncated it (`gd_comments_combined.json.bak.corrupt` ends inside the setlists block, 0 comments).
> On restart the scraper began from an empty `comments` list while `scraper_state.json` still held
> all 7,330 `processed_ids`, so no show was ever revisited. Result: 979 comments (post-crash only),
> 2,626 of 7,330 setlists. `gd_comments_enriched.json` (Sep 1) still holds 4,029 — the last good copy.
> **Real fix, in order:** (a) #7 atomic write first; (b) a one-off "re-fetch comments+setlists for
> all processed_ids" pass (~7,330 metadata calls, ~2 h at 1/s) to rebuild the corpus; (c) *then*
> optionally drop the keyword gate as below.

`gd_comment_scraper.py` — `filter_relevant_comment` (lines 535–540) and the caller `main` (line
681: `if comment_data["is_relevant"]: all_comments.append(...)`).
Only comments containing one of ~40 keywords (`song`, `jam`, `play`, `set`, …) are persisted.
State/counters accumulated 29,901 relevant comments across runs, yet the persisted file holds
979 — the rest were filtered out at store time (a rebuild/lossy write also left the counter
divergent from the file, see #7). Many genuine fan reviews ("best show of the tour, they were on
fire") never match, so the index is starved.
**Fix:** persist every raw comment and treat relevance as a filter flag, decided at *query/index*
time (you already store `is_relevant`); do not gate storage on it. Drop the early `if … is_relevant`
guard (line 681) and append every comment; let `build_index.py` filter later.
**Impact:** the 979 comment corpus grows toward the ~29,901 raw comments actually available — the
single largest coverage win.

## 2. [HIGH · show coverage] Years are marked "fully scanned" prematurely → only 7,330 shows scanned.
`gd_comment_scraper.py` — `search_shows_paginated` (lines 157–213) and `main` (line 646).
The function breaks after **5 consecutive pages with 0 new ids** (`stale_threshold`, lines 191–199)
or when a page returns `< rows_per_page` (line 203), and the empty result marks the whole year
`fully_scanned` (main line 646). IA's `advancedsearch.php` `start` pagination on the default sort
**repeats results**, so once most of a year's shows are already in `processed_ids`, the next pages are
all known ids → 0 new → early break → `fully_scanned`. That is why every year is "done" yet only
7,330 shows were ever enumerated.
**Fix:** page against IA's `response.numFound` — stop a year only when `start >= numFound`, and only
then flag it fully scanned. Remove/replace the 5-empty-page heuristic:
```python
num_found = data.get("response"].get("numFound", 0)
# loop: start += rows_per_page; break when start >= num_found; drop stale_threshold fully_scanned marking
```
**Impact:** year enumeration actually exhausts the collection instead of stopping at the first
repeated page.

## 3. [HIGH · robustness] Non-atomic writes → corruption (the `.bak.corrupt` file).
`gd_comment_scraper.py` — `save_state` (lines 102–106) and `save_combined_data` (lines 124–140) write
directly with `open(…,"w")`. A crash/kill mid-write truncates the file — the 17 MB
`gd_comments_combined.json.bak.corrupt` (vs 19 MB current) is direct evidence this already happened,
losing comments between the state counter and the file (#1/#7).
**Fix:** write to a `*.tmp` then `os.replace()` (the lyrics scraper already does exactly this in
`gd_lyrics_scraper.save_json`, lines 747–752 — copy that pattern to both functions here).
**Impact:** no more corrupted output/state on SIGTERM/interrupt; removes the crash-window that
created the `.bak.corrupt`.

## 4. [HIGH · lyrics coverage] Song corpus is setlist noise, not the dead.net catalog.
`gd_lyrics_scraper.py` — `build_songlist` (lines 346–397) derives the corpus from **setlist names**,
producing 3,072 entries, heavily polluted: `"1. Ripple [7:52]"`, `"d2t01 - Space"`,
`"08 Dark Star (reprise)"` are kept as separate "songs". `clean_song_name` strips leading numbers
but not `d2tNN - ` prefixes, `[mm:ss]` durations, or these variants. Result: ~1,928 names never
match dead.net, they dominate `limit`-selected runs, and rarely-played real songs (e.g. **Ripple**,
present in the songlist and in the dead.net index) sit below the run cut-off and never get fetched
→ only 322/459 dead.net songs were ever completed.
**Fix:** prefer the canonical source. Intersect/override the setlist-derived names with the already
fetched **dead.net index (459 canonical songs)** as the working corpus, coalescing variants by
`normalize_name` and picking the highest-frequency clean variant:
```python
canon = state["indexes"]["dead_net"]          # {norm_name: slug}
for s in songlist["songs"]:
    key = normalize_name(s["name"])
    if key in canon:        # drop the "d2t01 - "/"[7:52]" variants, keep real title
        corpus.setdefault(canon[key], 0); corpus[canon] += s["setlist_mentions"]
```
**Impact:** corpus collapses from ~3,072 → ~459 real songs, all matchable, so dead.net coverage
completes in a handful of runs and Ripple-class songs get lyrics.

## 5. [MEDIUM · lyrics efficiency] Unmatched songs re-queue forever and block completion.
`gd_lyrics_scraper.py` — `cmd_run` selection (lines 911–919) and `misses` (lines 921, 996).
A song with no dead.net/whitegum index hit is never marked done, so every run re-selects it, burns a
`limit` slot, and can starve real work (only 322 of 459 completed). The final `coverage` block (lines
1000–1009) already *counts* matched/unmatched but never records per-song "no match".
**Fix:** after the search, mark a source `done["sources"][src] = "no-match"` when `dn_index.get` /
`wg_index.get` returns None (the `misses` branch), so selection (which skips done sources at line
914) advances past it.
**Impact:** run-by-run throughput stops wasting slots on noise; the corpus completes instead of
capping out.

## 6. [MEDIUM · reddit] Obsolete scraper hits the same 403 gate, with no backoff and non-atomic save.
`reddit_scraper.py` — public JSON endpoints (lines 124, 148) return HTTP 403 bot-wall (the lyrics
scraper already documents this at `gd_lyrics_scraper.py` lines 21–24, 125–131). `get_reddit_json`
(lines 34–45) makes a single attempt with no retry/backoff and `raise_for_status()` aborts the whole
scope; the save (lines 184–186) is a non-atomic direct write that loses all prior data on a crash.
**Fix:** prefer the lyrics scraper's reddit path (records a clean `blocked (HTTP 403)` hook and never
hammers it: `gd_lyrics_scraper.py` lines 932–940). If kept, add exponential backoff and a tmp+rename
save. Otherwise deprecate the script in its docstring.
**Impact:** removes duplicate fragility/duplication for the blocked reddit source.

## 7. [MEDIUM · correctness] Metadata metrics lie (29,901 vs 979).
`gd_comment_scraper.py` — line 684 increments `state["total_comments"]` by re-scanning `all_comments`,
then writes that cumulative counter into `metadata.comments_total` (lines 130–137) while the actual
`comments` list has 979 entries. Counters and content diverge (root cause is #1 + #3), so any downstream
metric/report or index health-check reads a false picture.
**Fix:** in `save_combined_data` compute the real value: `metadata.comments_total = len(all_comments)`
(and `shows_with_comments` from setlists, not the counter).
**Impact:** metrics match reality; index health checks stop being misreading coverage.

## 8. [LOW · gdao crawler] No retry/backoff; outputs not atomic (minor, non-core).
`gdao_site_crawler.py` — `get_links_from_page` (lines 61–84) single-attempt (`raise_for_status` → returns
`[]` on any network error, no retry); `normalize_url` (lines 56–59) strips query strings (drops
paginated links); outputs written directly (lines 123–150). gdao.org is a static link-map harvest, so this
**does not affect the FAISS index** — the two scraper-adjacent items below are the real cost. Fix if
this crawler stays in use: wrap the GET in the same 2–3-attempt backoff the other scrapers use and
write via tmp+replace.
**Impact:** resilient to transient outages; keeps the graph harvest, not the RAT corpus.

---

## Top-3 to do first
1. (S1/#1) Stop gating comment storage on `RELEVANT_KEYWORDS — keep raw comments, filter at build time. Huge corpus gain.
2. (S2/#2) Page to `numFound` and stop flagging years "fully scanned" early — enumerate the real collection.
3. (S4/#4) Derive the lyrics corpus from the dead.net catalog, not setlist noise — fixes "no Ripple" / 59-song gap.
Plus make writes atomic (S3/#3) before any re-run.

— Ada