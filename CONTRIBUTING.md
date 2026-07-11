# Contributing to Briefer 🤝

Thanks for taking a look! Briefer is a small, self-hostable Telegram intake
bot, and contributions of all sizes are welcome — bug reports, docs, new
parsers, or platform support.

## Ground rules

- **Never commit secrets.** `.env`, `token.json`, `service_account.json`,
  `cookies.txt`, `storage_state.json`, `browser_profile/` and `data/` are all
  git-ignored — keep it that way. Double-check `git status` before committing.
- **Keep untrusted content contained.** Briefer ingests arbitrary forwarded
  content. Never run it through a shell, and route every fetched URL through
  the SSRF guard in `src/briefer/security.py`. See [`docs/SECURITY.md`](docs/SECURITY.md).
- **Match the surrounding style.** No large reformatting-only diffs.

## Dev setup

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cp .env.example .env      # fill in a bot token + Anthropic key at minimum
PYTHONPATH=src ./.venv/bin/python -m briefer
```

## Tests

```bash
PYTHONPATH=src ./.venv/bin/python -m pytest -q
```

Please add a test for any behaviour change — the suite is fast and offline
(the LLM/Sheets/network are all faked). Bug fixes should come with a
regression test that fails before the fix.

## Diagrams

The README diagrams are generated, not hand-drawn:

```bash
python3 docs/diagrams/gen_diagrams.py     # writes docs/diagrams/*.svg
```

## Pull requests

Keep PRs focused and describe **what changed and why**. If it changes
user-facing behaviour, update the `README.md` and add a `CHANGELOG.md` entry
under **Unreleased**.
