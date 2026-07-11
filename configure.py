#!/usr/bin/env python
"""Fill in missing configuration.

Regenerates `.env` from the canonical `.env.example` template:
  • keeps every value you already set,
  • adds any NEW settings that appeared in `.env.example`,
  • and PROMPTS you (showing the help comment) only for settings that are
    still empty.

Run directly or via `./manage.sh reconfigure`. Safe to re-run.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"

# Empty settings we DON'T prompt for (fine to leave blank / rarely used).
SKIP_PROMPT = {
    "ARTICLES_SHEET_ID", "EVENTS_SHEET_ID", "TWITTER_BEARER_TOKEN",
    "GOOGLE_SERVICE_ACCOUNT_FILE",
}


def parse_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def parse_template(path: Path):
    """Yield (key, default, help_lines) in file order."""
    help_lines: list[str] = []
    for raw in path.read_text().splitlines():
        s = raw.strip()
        if s.startswith("#"):
            help_lines.append(s.lstrip("# ").rstrip())
            continue
        if not s:
            help_lines = []
            continue
        if "=" in s:
            k, default = s.split("=", 1)
            yield k.strip(), default.strip(), help_lines
            help_lines = []


def main() -> int:
    if not EXAMPLE.exists():
        print("No .env.example found — cannot configure.")
        return 1
    existing = parse_env(ENV)
    interactive = sys.stdin.isatty()
    result: dict[str, str] = {}
    prompted = 0

    print("── Filling missing configuration (existing values are kept) ──\n")
    for key, default, help_lines in parse_template(EXAMPLE):
        cur = existing.get(key)
        if cur is not None and cur != "":
            result[key] = cur                      # keep what you have
            continue
        # A NEW setting that ships with a sensible default → use it silently
        # (don't nag about toggles like ENABLE_WEB_SEARCH=1).
        if key not in existing and default != "":
            result[key] = default
            continue
        if cur == "" and default != "":
            result[key] = cur                      # you cleared it on purpose
            continue
        if not interactive or key in SKIP_PROMPT:
            result[key] = default
            continue
        if help_lines:
            print("• " + " ".join(help_lines))
        try:
            ans = input(f"  {key} [{default}]: ").strip()
        except EOFError:
            ans = ""
        result[key] = ans or default
        prompted += 1

    # Preserve any keys that exist in .env but not in the template.
    for k, v in existing.items():
        result.setdefault(k, v)

    lines = [f"{k}={v}" for k, v in result.items()]
    ENV.write_text("\n".join(lines) + "\n")
    os.chmod(ENV, 0o600)
    print(f"\n✓ Wrote {ENV} (permissions 600). Prompted for {prompted} setting(s).")
    print("Restart to apply:  ./manage.sh restart")
    return 0


if __name__ == "__main__":
    sys.exit(main())
