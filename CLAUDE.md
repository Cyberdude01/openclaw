# Project Memory

## Time Zone Note

The user's **local machine clock is not ET**. All service logs, report timestamps, and
the `Updated:` field in the GitHub repo use **US Eastern Time (ET)**. When comparing
a screenshot clock to a report timestamp, account for this offset — do not assume the
service has stalled just because the times look far apart.

## Server Paths

The production server runs the polymarket service from:

```
/root/polymarket/
```

Key files on the server:
- `/root/polymarket/collector.py`
- `/root/polymarket/models.py`
- `/root/polymarket/exporter.py`
- `/root/polymarket/config.py`
- `/etc/polymarket.env` — environment variables (GITHUB_TOKEN, EXPORT_REPO, etc.)
- `~/bob` — local git clone of the export repo (Cyberdude01/Bob)

**`/root/polymarket` is NOT a git repository** — files are deployed directly.
To apply fixes, patch the files in-place (e.g. with a Python script) then restart the service.

The local dev repo is at `/home/user/openclaw/` (this repo).

## Service

- Systemd unit: `polymarket`
- Restart: `sudo systemctl restart polymarket`
- Logs: `journalctl -u polymarket -f`

## Feed Continuity / Auto-Restart

The service has a built-in auto-restart mechanism (`AUTO_RESTART_HOURS` env var in `main.py`).
It is **disabled by default** (value = 0). To enable hourly restart:

1. Add `AUTO_RESTART_HOURS=1` to `/etc/polymarket.env`
2. Restart the service: `sudo systemctl restart polymarket`

The systemd unit must have `Restart=always` (or `Restart=on-failure`) for the auto-restart to
actually bring the process back up after `os.execv()` exits it.

To verify the service is running and the env var is applied:
```
sudo systemctl show polymarket | grep Restart
grep AUTO_RESTART_HOURS /etc/polymarket.env
journalctl -u polymarket --since "1 hour ago" | grep -i restart
```

To manually patch the env var on the server without editing the file interactively:
```
grep -q AUTO_RESTART_HOURS /etc/polymarket.env \
  && sed -i 's/AUTO_RESTART_HOURS=.*/AUTO_RESTART_HOURS=1/' /etc/polymarket.env \
  || echo 'AUTO_RESTART_HOURS=1' >> /etc/polymarket.env
sudo systemctl restart polymarket
```
