# Project Memory

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

The local dev repo is at `/home/user/openclaw/` (this repo).

## Service

- Systemd unit: `polymarket`
- Restart: `sudo systemctl restart polymarket`
- Logs: `journalctl -u polymarket -f`
