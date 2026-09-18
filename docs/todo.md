# GD Project — TODO

## Lemieux Discord bot (text-only) — code done 2026-09-18
- [ ] Create "Lemieux" app in Discord Developer Portal → Bot → Reset Token; enable MESSAGE CONTENT INTENT
- [ ] Invite: scopes `bot`; perms View Channels, Send Messages, Read Message History
- [ ] `cp bot/.env.example bot/.env`; set `LEMIEUX_DISCORD_TOKEN` + `LEMIEUX_CHANNEL_IDS`
- [ ] Run `bot/start_lembot_discord.sh`; test in channel: a question, `search …`, `lyrics Bird Song`, `meaning Bird Song`
- [ ] If keeping it running: systemd unit or cron @reboot (decide after first live test)

## Index coverage gaps (found while testing the bot)
- Only 979 real fan comments vs 21,719 setlist rows — plain questions return thin results
- Europe '72: 11 entries total
- Lyrics: 59 songs only (no Ripple); 345 interpretations

## Later
- Lemieux voice (see bot/voice_todo.md)
