"""
FNOL Intelligence Platform — Launcher
=====================================
One-command bootstrap for the entire stack.

Usage
-----
    python fnol_launcher.py                  # default: 127.0.0.1:8000, opens browser
    python fnol_launcher.py --no-browser
    python fnol_launcher.py --host 0.0.0.0 --port 8080
    python fnol_launcher.py --check          # diagnostics only, do not start server

Environment variables (optional — sane defaults applied if missing):
    FNOL_API_KEY            (default: 6FVo_miZ8rXQcFiMuwh6x1nQk4TnmbdXd4i0MFez0VU)
    FNOL_HOST / FNOL_PORT
    FNOL_LLM_PROVIDER       (auto|anthropic|openai|azure|bedrock|mock)
    ANTHROPIC_API_KEY       (when provider=anthropic or auto with Claude)
    OPENAI_API_KEY          (when provider=openai)
    AZURE_OPENAI_*          (when provider=azure)
    AWS_*                   (when provider=bedrock)
    SOR_TYPE                (mock|duckcreek|guidewire — default: mock)

What it does:
  1. Verifies Python ≥ 3.9.
  2. Verifies / installs FastAPI, uvicorn, pydantic (and optional LLM SDKs).
  3. Prints an environment summary (LLM provider, SOR adapter, API key, URL).
  4. Optionally opens http://<host>:<port>/app in the default browser.
  5. Launches uvicorn with sensible defaults.

This launcher is intentionally dependency-light. It does NOT install LLM
SDKs by default — those are optional. The FNOL pipeline runs end-to-end on
the deterministic mock LLM with no external network calls.
"""

from __future__ import annotations

import argparse
import importlib
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path


# ───────────────────────────────────────────────────────────────────────────
# Cosmetics
# ───────────────────────────────────────────────────────────────────────────

def _supports_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

def c(code: str, text: str) -> str:
    if not _supports_color():
        return text
    return f"\033[{code}m{text}\033[0m"

BOLD  = lambda s: c("1", s)
DIM   = lambda s: c("2", s)
RED   = lambda s: c("31", s)
GREEN = lambda s: c("32", s)
YEL   = lambda s: c("33", s)
BLUE  = lambda s: c("34", s)
MAG   = lambda s: c("35", s)
CYAN  = lambda s: c("36", s)


def banner():
    print()
    print(BOLD(MAG("  ┌──────────────────────────────────────────────────────────────┐")))
    print(BOLD(MAG("  │                                                              │")))
    print(BOLD(MAG("  │   FNOL Intelligence Platform · Accenture                     │")))
    print(BOLD(MAG("  │   Auto Claims · Duck Creek-Native · 8-Agent Pipeline + A10   │")))
    print(BOLD(MAG("  │                                                              │")))
    print(BOLD(MAG("  └──────────────────────────────────────────────────────────────┘")))
    print()


# ───────────────────────────────────────────────────────────────────────────
# Pre-flight checks
# ───────────────────────────────────────────────────────────────────────────

REQUIRED_PACKAGES = [
    ("fastapi",  "fastapi"),
    ("uvicorn",  "uvicorn"),
    ("pydantic", "pydantic"),
]

OPTIONAL_PACKAGES = [
    ("anthropic", "anthropic"),
    ("openai",    "openai"),
    ("boto3",     "boto3"),
]


def check_python() -> None:
    if sys.version_info < (3, 9):
        print(RED("✗ Python 3.9+ required.")); sys.exit(1)
    print(GREEN(f"  ✓ Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"))


def check_modules(install_missing: bool = False) -> None:
    missing = []
    for mod, pkg in REQUIRED_PACKAGES:
        try:
            importlib.import_module(mod)
            print(GREEN(f"  ✓ {pkg}"))
        except ImportError:
            missing.append(pkg)
            print(YEL(f"  ⚠ {pkg} not installed"))

    if missing:
        if install_missing:
            print()
            print(BOLD(f"  → Installing missing packages: {' '.join(missing)}"))
            subprocess.check_call([sys.executable, "-m", "pip", "install",
                                   "--break-system-packages", "--quiet", *missing])
            print(GREEN("  ✓ Installed."))
        else:
            print()
            print(RED("  ✗ Missing required packages. Re-run with --install or run:"))
            print(BOLD(f"    pip install {' '.join(missing)}"))
            sys.exit(1)

    for mod, pkg in OPTIONAL_PACKAGES:
        try:
            importlib.import_module(mod)
            print(DIM(f"  · {pkg} (optional · available)"))
        except ImportError:
            print(DIM(f"  · {pkg} (optional · not installed — fine)"))


def check_files() -> None:
    here = Path(__file__).parent.resolve()
    required = [
        "fnol_api_server.py",
        "fnol_workflow_engine.py",
        "fnol_llm_adapter.py",
        "fnol_sor_adapter.py",
        "fnol_copilot_agent.py",
        "fnol_conversational_agent.py",
        "fnol_app.html",
    ]
    missing = []
    for f in required:
        if (here / f).exists():
            print(GREEN(f"  ✓ {f}"))
        else:
            missing.append(f)
            print(RED(f"  ✗ {f} (missing)"))
    if missing:
        print()
        print(RED("  ✗ Required platform files missing. Make sure all files are in the same directory."))
        sys.exit(1)


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        try:
            return s.connect_ex((host, port)) == 0
        except OSError:
            return False


# ───────────────────────────────────────────────────────────────────────────
# Environment summary
# ───────────────────────────────────────────────────────────────────────────

def env_summary(host: str, port: int) -> None:
    from fnol_settings import settings
    api_key_status = "set" if settings.fnol_api_key and not settings.api_key_is_default \
                              else "MISSING — set FNOL_API_KEY before starting"
    llm_pref = settings.fnol_llm_provider
    sor_type = settings.sor_type

    here = Path(__file__).parent.resolve()
    sys.path.insert(0, str(here))
    try:
        from fnol_llm_adapter import resolve_provider, health as llm_health
        from fnol_sor_adapter import get_sor_adapter
        llm = resolve_provider()
        hp  = llm_health()
        sor = get_sor_adapter().name
    except Exception as e:
        llm, hp, sor = "?", {"healthy": False, "error": str(e)}, "?"

    print()
    print(BOLD("  ENVIRONMENT"))
    print(f"    {DIM('Host:Port'):.<22} {BOLD(f'{host}:{port}')}")
    print(f"    {DIM('UI URL'):.<22} {CYAN(f'http://{host}:{port}/app')}")
    print(f"    {DIM('API base'):.<22} {CYAN(f'http://{host}:{port}/api/v1')}")
    print(f"    {DIM('API docs'):.<22} {CYAN(f'http://{host}:{port}/docs')}")
    print(f"    {DIM('API key'):.<22} {BOLD(api_key_status)}")
    print(f"    {DIM('LLM provider (pref)'):.<22} {BOLD(llm_pref)}")
    print(f"    {DIM('LLM provider (eff)'):.<22} {BOLD(llm)}  "
          + (GREEN("healthy") if hp.get("healthy") else YEL("not connected — using mock")))
    print(f"    {DIM('SOR adapter'):.<22} {BOLD(sor.upper())}")


# ───────────────────────────────────────────────────────────────────────────
# Browser open (delayed)
# ───────────────────────────────────────────────────────────────────────────

def open_browser_delayed(url: str, delay: float = 1.6) -> None:
    def _open():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=_open, daemon=True).start()


# ───────────────────────────────────────────────────────────────────────────
# Launch
# ───────────────────────────────────────────────────────────────────────────

def launch(host: str, port: int, reload: bool = False) -> None:
    import uvicorn

    here = Path(__file__).parent.resolve()
    sys.path.insert(0, str(here))

    print()
    print(BOLD(GREEN(f"  → Starting FNOL platform on http://{host}:{port}/app")))
    print(DIM("  (Ctrl+C to stop)"))
    print()
    uvicorn.run("fnol_api_server:app", host=host, port=port, reload=reload, log_level="info")


# ───────────────────────────────────────────────────────────────────────────
# main
# ───────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="FNOL Intelligence Platform launcher")
    parser.add_argument("--host", default=os.environ.get("FNOL_HOST", "127.0.0.1"))
    parser.add_argument("--port", default=int(os.environ.get("FNOL_PORT", "8000")), type=int)
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser.")
    parser.add_argument("--install", action="store_true", help="Auto-install missing required packages.")
    parser.add_argument("--check", action="store_true", help="Run pre-flight diagnostics and exit.")
    parser.add_argument("--reload", action="store_true", help="Enable uvicorn auto-reload (dev).")
    args = parser.parse_args()

    banner()
    print(BOLD("  PRE-FLIGHT CHECKS"))
    check_python()
    check_modules(install_missing=args.install)
    print()
    print(BOLD("  PLATFORM FILES"))
    check_files()

    env_summary(args.host, args.port)

    if args.check:
        print()
        print(GREEN("  ✓ All checks passed. (use without --check to launch)"))
        return

    if port_in_use(args.host, args.port):
        print()
        print(RED(f"  ✗ Port {args.port} on {args.host} is already in use."))
        print(YEL(f"    Either close the other process or use --port <other>."))
        sys.exit(1)

    if not args.no_browser:
        open_browser_delayed(f"http://{args.host}:{args.port}/app")
        print()
        print(DIM(f"  (browser opening in a moment to http://{args.host}:{args.port}/app)"))

    launch(args.host, args.port, reload=args.reload)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        print(DIM("  ↓ Stopped."))
