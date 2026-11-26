# Knowledge library

Reviewed documents the voice agent may use. Each entry in `registry.json` records its version,
readiness, scope, approval notes and the **sha256 fingerprint** of its content file. The agent
re-checks the fingerprints on every call. If a file changes without its registry entry being
updated, calls are refused rather than served with unreviewed content.

| Path | Contents |
|---|---|
| `content/` | The documents (Markdown) |
| `registry.json` | Source IDs, versions, readiness, descriptions, fingerprints |
| `sources.json` | Where the original material came from (originals are held outside the repo) |
| `retrieval-checks.csv` | Authored knowledge checks (question → expected source and behaviour) |
| `templates/`, `hardware/` | Templates for document changes and hardware profiles |

Readiness values: `development_ready` (usable in development/pilot), `needs_confirmation`,
`reference_only`, `retired`. Only `development_ready` entries with `export: true` can be
delivered.

How the agent receives each document (always included, fetched as a guide, or searched) is set
in `voice-agent/knowledge.py` (`DELIVERY`). The workflow for adding or changing a document is
in [docs/knowledge.md](../docs/knowledge.md).

Line endings: `.gitattributes` keeps these files LF on every checkout. A CRLF conversion would
change the bytes and break the fingerprints.
