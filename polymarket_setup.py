#!/usr/bin/env python3
"""
Polymarket 15M — Remote setup script.
Downloads all files from GitHub, creates a venv, installs dependencies,
and optionally installs a systemd service (run as root with --install-service).

Usage on any server with internet access:
    # Basic setup only:
    python3 polymarket_setup.py

    # Full setup + systemd service (run as root):
    sudo python3 polymarket_setup.py --install-service
"""
import os, sys, urllib.request, subprocess, venv as _venv, pathlib, textwrap

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
    "exporter.py",
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

# ── Optional: Install systemd service ────────────────────────────────────────
if "--install-service" in sys.argv:
    if os.geteuid() != 0:
        print("\nERROR: --install-service must be run as root (use sudo).")
        sys.exit(1)

    env_file   = pathlib.Path("/etc/polymarket.env")
    svc_file   = pathlib.Path("/etc/systemd/system/polymarket.service")
    work_dir   = str(pathlib.Path.home())
    python_bin = venv_python

    # ── Write credentials env file (only if it doesn't exist) ────────────────
    if not env_file.exists():
        env_file.write_text(textwrap.dedent("""\
            # Polymarket credentials — fill in before starting the service.
            # Permissions are restricted to root only (chmod 600).
            POLY_PRIVATE_KEY=
            POLY_ADDRESS=
            POLY_API_KEY=
            POLY_API_SECRET=
            POLY_API_PASSPHRASE=

            # GitHub export (optional — remove lines to disable)
            GITHUB_TOKEN=
            EXPORT_REPO=Cyberdude01/Bob
            EXPORT_INTERVAL=300
        """))
        env_file.chmod(0o600)
        print(f"\nCreated credentials file: {env_file}")
        print("  → Edit it and fill in your credentials before starting the service.")
    else:
        print(f"\nCredentials file already exists: {env_file} (not overwritten)")

    # ── Write systemd service file ────────────────────────────────────────────
    svc_content = textwrap.dedent(f"""\
        [Unit]
        Description=Polymarket 15M Crypto Market System
        After=network-online.target
        Wants=network-online.target

        [Service]
        Type=simple
        User=root
        WorkingDirectory={work_dir}
        EnvironmentFile=/etc/polymarket.env
        ExecStart={python_bin} -m polymarket
        Restart=always
        RestartSec=10
        StandardOutput=journal
        StandardError=journal

        [Install]
        WantedBy=multi-user.target
    """)
    svc_file.write_text(svc_content)
    print(f"Created service file:      {svc_file}")

    # ── Reload systemd and enable service ─────────────────────────────────────
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "polymarket"], check=True)
    print("\n✓ Service installed and enabled.")
    print("\nNext steps:")
    print(f"  1. Edit credentials:  nano {env_file}")
    print("  2. Start the service: systemctl start polymarket")
    print("  3. Check status:      systemctl status polymarket")
    print("  4. Watch logs:        journalctl -u polymarket -f")

else:
    print("\n✓ Setup complete!")
    print("\nActivate the venv, then run:")
    print(f"  source {venv_dir}/bin/activate")
    print("  python3 -m polymarket          # full pipeline (paper trade by default)")
    print("  python3 -m polymarket --data-only  # data collection only, no trading")
    print("\nTo install as a persistent systemd service (run as root):")
    print("  sudo python3 polymarket_setup.py --install-service")
    print(f"\nOr without activating venv:")
    print(f"  {venv_python} -m polymarket")
