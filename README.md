# LSPcare Voice Agent

AI phone support for LSPcare bedside entertainment. Callers reach a Twilio number. The agent
works out what they need, helps with buying, prices and screen problems using reviewed
knowledge, and logs support tickets in Zoho Desk when follow-up is needed. It ends clinical
calls immediately with a nurse-call message.

**Status:** staff pilot. Fictional caller details only. Not yet approved for patient calls.

## How it works

```
Caller ⇄ Twilio Voice + ConversationRelay ⇄ voice-agent (this repo) ⇄ Claude Sonnet 5.5
         speech-to-text (Deepgram, en-AU)        rules · tools · knowledge
         text-to-speech (ElevenLabs)             ├─ device lookup (hospital inventory)
                                                 ├─ reviewed knowledge (core / guides / search)
                                                 └─ Zoho Desk tickets (guarded)
```

- **Conversation:** one agent loop. Each turn the model sees the rules, the core knowledge,
  the call so far and its tools, then speaks, uses a tool or ends the call. Rules that must
  never be broken are enforced in code: ticket read-back and one per call, no card numbers,
  approved guides only, fixed clinical ending.
- **Knowledge:** reviewed documents in `knowledge/`, each fingerprinted in `registry.json`.
  An unreviewed change stops calls instead of being served.
- **Testing:** offline unit tests, plus a conversation harness that runs the real agent in
  text against scripted or simulated callers (about US$0.35 per full run, capped).

Full technical reference: [`docs/technical-reference.pdf`](docs/technical-reference.pdf).

## Repository layout

| Path | Contents |
|---|---|
| `voice-agent/` | The service: Twilio endpoints, conversation engine, tools, tickets, transcripts, test harness ([README](voice-agent/README.md)) |
| `mock-backend/` | Mock LSPcare API (synthetic vouchers, device lookup) and shared modules (`static_inventory.py`, `private_files.py`) ([README](mock-backend/README.md)) |
| `knowledge/` | Reviewed knowledge library and registry ([README](knowledge/README.md)) |
| `scripts/` | `setup_zoho_desk.py`: Zoho Desk OAuth set-up and read-only check |
| `CLAUDE.md` | Working rules and design lessons for developers and AI agents |
| `docs/` | [Testing](docs/testing.md), [knowledge workflow](docs/knowledge.md), [business rules](docs/business-rules.md), [mock backend contract](docs/mock-backend.md), technical reference |

## Private data stays outside the repo

Credentials, the hospital inventory, call transcripts and dry-run ticket logs live in a folder
named `private-config` **next to** the repository folder (or set `LSPCARE_PRIVATE_DIR`). The
folder and its files are restricted to the current user. Never copy them into the repo.

| File in `private-config/` | Created by |
|---|---|
| `twilio-pilot.json` | `voice-agent/configure.py` (Twilio + AI keys, settings) |
| `zoho-desk.json` | `scripts/setup_zoho_desk.py` |
| `lspcare-inventory.json` | Approved, sanitised inventory export (supplied separately) |
| `transcripts/`, `pilot-dry-run-tickets.jsonl` | The running agent |

## Quick start (Windows)

```powershell
cd voice-agent
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe configure.py                 # Twilio + OpenAI keys (hidden prompts)
.venv\Scripts\python.exe configure.py --use-claude    # Anthropic key; Claude Sonnet 5.5
powershell -ExecutionPolicy Bypass -File run.ps1     # tunnel + server + Twilio number; wait for READY
```

Tests:

```powershell
.venv\Scripts\python.exe -m unittest test_pilot test_claude test_stages test_harness test_pilot_loop test_dashboard test_flows test_knowledge_edit test_flow_edit test_listening   # free
.venv\Scripts\python.exe harness.py                                                    # conversations, capped at US$0.50
```

## Current limitations

- Runs on a single staff-testing server (Docker and a Cloudflare named tunnel: [docs/home-server.md](docs/home-server.md)); not production hosting.
- No SMS, vouchers, payments or transfer to a person yet; the agent says so truthfully.
- At most 2 simultaneous calls; call state is in memory.
- Transcripts are on for the staff pilot; turn them off before any real patient data.
