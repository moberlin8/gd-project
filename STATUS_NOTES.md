# GD RAT Project — Status Notes

## Current State
- **Scraper:** Incremental, resumeable, paginated (handles 100+ shows/year)
- **State file:** `data/scraper_state.json` — 234 shows processed (1965-1994)
- **Combined data:** `data/gd_comments_combined.json` — 889 comments, 228 setlists (747 KB)
- **Cron job:** `0 2 * * *` (daily 2am UTC) — job ID `8c8231459d08`
- **Delivery:** Discord channel after each overnight run

## Key Facts
- **Total GD shows on archive.org:** 18,327 (confirmed via API)
- **Comments per show (sample):** ~3.8
- **Disk space (full scrape):** ~1.4 GB (57MB JSON + 107MB FAISS + 2MB setlists)
- **Runtime at 2s delay:** 10 hours 24/7 / ~15 hours at 3s
- **Bandwidth:** ~1.4 GB for full collection

## IA Rules
- User-Agent must be descriptive (e.g., `GD-RAT-Scraper/2.0-Incremental`)
- No published rate limit — be polite, expect 429s
- Honor Retry-After headers
- Source: `research/internet_archive_rules.md`

## Acceleration Levers
1. Decrease delay (down to 1s if no 429s)
2. Parallel workers (2-3 concurrent)
3. Bulk endpoints (/items/?identifier= for multi-fetch)

## Next Steps
- [ ] Let overnight cron run naturally
- [ ] Rebuild FAISS index after ~50 new shows added
- [ ] Consider 24/7 run if IA doesn't rate-limit at 1-2s delay
