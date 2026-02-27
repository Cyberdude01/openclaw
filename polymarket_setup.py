#!/usr/bin/env python3
"""
Polymarket 15M — Remote setup script.
Downloads all files from GitHub and installs dependencies.

Usage on any server with internet access:
    python3 polymarket_setup.py
    pip install aiohttp websockets rich eth-account
    python3 -m polymarket --data-only
"""
import os, sys, urllib.request, subprocess

BRANCH = "claude/polymarket-data-collection-A7tQQ"
REPO   = "Cyberdude01/openclaw"
BASE   = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/polymarket"

FILES = [
    "__init__.py",
    "__main__.py",
    "config.py",
    "models.py",
    "analytics.py",
    "collector.py",
    "decision.py",
    "trader.py",
    "main.py",
    "requirements.txt",
]

dest = os.path.join(os.getcwd(), "polymarket")
os.makedirs(dest, exist_ok=True)
print(f"Writing to: {dest}\n")

ok = True
for fname in FILES:
    url  = f"{BASE}/{fname}"
    path = os.path.join(dest, fname)
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            content = r.read()
        with open(path, "wb") as f:
            f.write(content)
        print(f"  OK  {fname}")
    except Exception as e:
        print(f"  FAIL  {fname}: {e}")
        ok = False

if not ok:
    print("\nSome files failed. Check your network or GitHub access.")
    sys.exit(1)

print("\nInstalling dependencies…")
subprocess.check_call([sys.executable, "-m", "pip", "install",
    "aiohttp>=3.9.0", "websockets>=12.0", "rich>=13.0.0",
    "eth-account>=0.10.0", "requests>=2.31.0"])

print("\n✓ Setup complete!")
print("\nRun:")
print("  python3 -m polymarket --data-only    # live dashboard, no trading")
print("  python3 -m polymarket --paper         # paper-trade mode")
print("  python3 -m polymarket                 # live trading (needs env vars)")
