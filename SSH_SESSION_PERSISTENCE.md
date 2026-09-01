## SSH Session Persistence Fix

**Problem:** SSH sessions time out, losing context during long-running GD project tasks (scraper runs, index rebuilds, etc.)

**Solutions Implemented:**

### 1. SSH Keepalive Configuration
Added to `/etc/ssh/sshd_config` (or `~/.ssh/config`):
```
ClientAliveInterval 600
ClientAliveCountMax 3
TCPKeepAlive yes
```
This sends a keepalive packet every 10 minutes and keeps the connection alive for up to 30 minutes of inactivity.

### 2. Tmux Session Wrapper
All long-running tasks should be wrapped in tmux so they survive SSH disconnects:
```bash
tmux new-session -d -s gd-project 'cd /home/mao/DaveMatt/gd-project && python3 scrapers/gd_comment_scraper.py --target 100 --delay 3.25 2>&1'
```

To reattach after reconnect: `tmux attach -t gd-project`

### 3. Hermes Session Auto-Save
Hermes automatically saves session dumps to `/home/hermes/.hermes/sessions/` every few minutes. To restore:
```bash
hermes --resume <session_id>
# Or list available sessions:
ls /home/hermes/.hermes/sessions/
```

### Best Practices for Long-Running Tasks
1. Always use tmux for tasks that might outlive the SSH session
2. Use `--target` flags to limit scraper runs (100 shows per session is safe)
3. Incremental saves happen every 10 shows (state file)
4. The overnight cron handles unattended runs automatically

## Example Workflow
```bash
# Start a long scraper run in tmux
tmux new-session -d -s gd-scraper 'cd /home/mao/DaveMatt/gd-project && python3 scrapers/gd_comment_scraper.py --target 500 --delay 3.25'

# Check progress
tail -f data/gd_comments_combined.json | tail -5
cat data/scraper_state.json | jq '.shows_processed | length'

# Detach (keeps running)
# Ctrl+B, then D

# Reattach later
tmux attach -t gd-scraper
```

## Remarks
- <hal> 2026-Aug-31: SSH keepalive + tmux persistence configured
- <hal> 2026-Aug-31: Hermes session dump auto-save already functional
- User noted: SSH app timeouts lose session info — tmux is the solution
