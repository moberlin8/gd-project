# GD Project — TODO

## ✅ Sep 23 — Fix Lemieux "Jack Straw" lyrics missing
### Root Cause
- Lyrics scrape was working (463 songs in gd_lyrics.json) but FAISS index
  was stale (only 59 lyric entries) — no cron job rebuilt the index after
  scraping. Bot loaded index on startup and never saw new data.
### Fixes Applied
- [x] Rebuilt FAISS index via build_faiss_incremental.py
  - Vectors: 27,428 → 43,725
  - Lyric entries: 59 → 463
  - Interpretations: 345 → 1,619
- [x] Created scripts/build_index_overnight.sh (wrapper with logging)
- [x] Added cron job "GD RAT Index Rebuild Overnight" (30 2 * * * UTC)
- [x] Restarted Lemieux bot (PID 21394, supervised by PID 21354)
- [x] Verified Jack Straw lyrics searchable via search_lyrics()
- [x] Committed to git (d9e9cac)

## ✅ Sep 23 — Fix Lemieux Telegram bot (crash loop + Grok 403)
- [x] Fixed start_lembot.sh kill-restart loop (sleep 5 → wait $!)
- [x] Switched LLM from XAI/Grok to Nous inference API
- [x] Killed HAL Discord bot crash loop

## ✅ Sep 23 — Fix Lemieux not doing analysis
### Root Cause
- Nous API token in .env expired after ~1 hour, but Hermes runtime auto-refreshes
  the token in ~/.hermes/shared/nous_auth.json. Bot got 401 errors and silently
  fell back to extractive search only.
### Fixes Applied (via Ada)
- [x] Added _get_nous_token() to lembot_core.py — reads live token from
  ~/.hermes/shared/nous_auth.json
- [x] Modified summarize_with_llm() to try .env key first, then fall back to
  auth file token on 401 (retry logic)
- [x] Bot restarted with patched code (PID 47336)
- [x] Verified: LLM synthesis returns analysis even with stale .env token
- [x] Committed to git (4675660)
## Lemieux Discord bot (text-only) — code done 2026-09-18
- [ ] Create "Lemieux" app in Discord Developer Portal → Bot → Reset Token; enable MESSAGE CONTENT INTENT
- [ ] Invite: scopes `bot`; perms View Channels, Send Messages, Read Message History
- [ ] `cp bot/.env.example bot/.env`; set `LEMIEUX_DISCORD_TOKEN` + `LEMIEUX_CHANNEL_IDS`
- [ ] Run `bot/start_lembot_discord.sh`; test in channel: a question, `search …`, `lyrics Bird Song`, `meaning Bird Song`
- [ ] If keeping it running: systemd unit or cron @reboot (decide after first live test)

## Scraper review fixes — all 8 done 2026-09-18 (docs/scraper_review.md)
- [x] Rebuild FAISS index after recovery + lyrics catch-up ← DONE (Sep 23, d9e9cac)
- [ ] Run comment recovery: `python3 scrapers/gd_comment_scraper.py --refetch-missing --target 5000` (~3.3 h; 4,702 shows lost to the Sep 2 truncated write; checkpoints every 10)
- [ ] Un-pause cron 8c8231459d08 "GD RAT Overnight Scraper" once recovery is done (numFound paging will now enumerate shows beyond the 7,330)
- [ ] Lyrics: 2 AM cron now works a 459-song canonical corpus; 242 dead.net songs still to fetch (Ripple included) — check in a few nights
- [ ] Wire b2_backup_guard.sh into daily backup cron (see memory note)

## Later
- Lemieux voice (see bot/voice_todo.md)
