# GD Project — TODO

## Lemieux Discord bot (text-only) — code done 2026-09-18
- [ ] Create "Lemieux" app in Discord Developer Portal → Bot → Reset Token; enable MESSAGE CONTENT INTENT
- [ ] Invite: scopes `bot`; perms View Channels, Send Messages, Read Message History
- [ ] `cp bot/.env.example bot/.env`; set `LEMIEUX_DISCORD_TOKEN` + `LEMIEUX_CHANNEL_IDS`
- [ ] Run `bot/start_lembot_discord.sh`; test in channel: a question, `search …`, `lyrics Bird Song`, `meaning Bird Song`
- [ ] If keeping it running: systemd unit or cron @reboot (decide after first live test)

## Scraper review fixes — all 8 done 2026-09-18 (docs/scraper_review.md)
- [ ] Run comment recovery: `python3 scrapers/gd_comment_scraper.py --refetch-missing --target 5000` (~3.3 h; 4,702 shows lost to the Sep 2 truncated write; checkpoints every 10)
- [ ] Un-pause cron 8c8231459d08 "GD RAT Overnight Scraper" once recovery is done (numFound paging will now enumerate shows beyond the 7,330)
- [ ] Lyrics: 2 AM cron now works a 459-song canonical corpus; 242 dead.net songs still to fetch (Ripple included) — check in a few nights
- [ ] Rebuild FAISS index after recovery + lyrics catch-up, restart Lemieux

## Later
- Lemieux voice (see bot/voice_todo.md)
