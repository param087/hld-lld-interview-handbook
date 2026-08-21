# HLD + LLD Interview Handbook

**Live site: https://hld-lld-interview-handbook.vercel.app**

A complete, self-contained preparation guide for SDE2-level system design (HLD) and low-level / object-oriented design (LLD) interviews at FAANG-style companies:

- **27 HLD fundamentals** — from back-of-envelope estimation and caching to consensus, Kafka internals and geospatial indexing, each with diagrams and a tested Python implementation of the core idea.
- **31 HLD case studies** — URL shortener, news feed, chat, Uber, Ticketmaster, payments, Kafka-like queue, S3, Google Docs, stock exchange and more. Same spine every time: requirements → estimation → API → data model → architecture → deep dives → scaling → trade-offs → 45-minute pacing.
- **8 LLD fundamentals + 31 design patterns** (23 GoF + 8 modern) in idiomatic Python (ABCs, Protocols, dataclasses, enums), each pattern with the canonical interview example *and* the Pythonic shortcut.
- **36 LLD problems** — parking lot, elevator, vending machine, Splitwise, BookMyShow, chess, text editor, payment wallet… each a full runnable package with pytest tests, class/sequence/state diagrams and an interview walkthrough.
- **11 cheatsheets**, **6 mock interviews** (full 45-minute transcripts with evolving diagrams) and an **8-week study roadmap**.

All diagrams are Mermaid (they render on GitHub too). Every code snippet on the site is embedded from `code/` — the exact files that `pytest` runs.

## Run it locally

```bash
bash scripts/bootstrap.sh          # creates .venv (Python 3.12 via uv) and installs deps
uv run mkdocs serve                # http://127.0.0.1:8000
uv run pytest                      # runs every LLD package and HLD module test
uv run python -m lld.parking_lot.demo
```

## Repository layout

```
docs/        the site (MkDocs Material): hld/, lld/, cheatsheets/, mocks/
code/        Python 3.12, stdlib only: common/, hld/, fundamentals/, patterns/, lld/<problem>/
scripts/     linters, validators and generators that keep 150 pages consistent
CATALOGUE.md the page list that drives indexes, navigation and completeness checks
AUTHORING.md the style guide every page follows
```

## Quality gates

`scripts/gate.sh` runs ruff, pytest, the page linter (template compliance, snippet paths, links, placeholder text), a headless-Chrome Mermaid validator, `mkdocs build --strict`, and a catalogue-vs-disk completeness check. CI runs the same on every push; Vercel builds the site from `main`.

## License

MIT — see [LICENSE](LICENSE).
