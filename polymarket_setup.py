#!/usr/bin/env python3
"""
Polymarket 15M — Remote setup script.
Downloads all files from GitHub, creates a venv, and installs dependencies.

Usage on any server with internet access:
    python3 polymarket_setup.py
    source ~/venv/bin/activate
    python3 -m polymarket --data-only
"""
import os, sys, urllib.request, subprocess, venv as _venv, pathlib

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

# ── Create / reuse venv ───────────────────────────────────────────────────────
venv_dir = pathlib.Path.home() / "venv"
if not (venv_dir / "bin" / "python").exists():
    print(f"\nCreating virtual environment at {venv_dir} …")
    _venv.create(str(venv_dir), with_pip=True)
else:
    print(f"\nReusing existing venv at {venv_dir}")

venv_python = str(venv_dir / "bin" / "python")

print("Installing dependencies into venv…")
subprocess.check_call([venv_python, "-m", "pip", "install", "--quiet",
    "aiohttp>=3.9.0", "websockets>=12.0", "rich>=13.0.0",
    "eth-account>=0.10.0", "requests>=2.31.0"])

print("\n✓ Setup complete!")
print("\nActivate the venv, then run:")
print(f"  source {venv_dir}/bin/activate")
print("  python3 -m polymarket --data-only    # live dashboard, no trading")
print("  python3 -m polymarket --paper         # paper-trade mode")
print("  python3 -m polymarket                 # live trading (needs env vars)")
print("\nOr without activating:")
print(f"  {venv_python} -m polymarket --data-only")
