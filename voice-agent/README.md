# voice-agent

The phone service. Twilio Voice + ConversationRelay handles the call, speech recognition and
the voice; this service applies LSPcare's rules, knowledge and tools; Claude Sonnet 5.5
decides each reply.

## Files

| File | Role |
|---|---|
| `app.py` | Twilio endpoints (`/voice`, `/relay`, `/end`, `/health`), signature checks, sessions, system prompt, voice settings, tool dispatch |
| `conversation.py` | Shared turn engine used by the phone and the test harness |
| `claude_reply.py` | Claude request options, streaming, tool rounds |
| `tools.py` | Tool definitions and exact-schema validation |
| `knowledge.py` | Staged knowledge from `../knowledge/registry.json` (core / guide / search), hash-checked |
| `flows.py`, `flows/*.json` | Flow engine and flow definitions: code-enforced step order (tickets) — [docs/flows.md](../docs/flows.md) |
| `tickets.py`, `zoho_desk.py` | Guarded `create_ticket` and the Zoho Desk client |
| `transcripts.py` | Live call transcripts (pilot window + private file), lookup by call reference |
| `pilot_log.py` | Staff pilot: call references, version stamps, spoken problem reports |
| `dashboard.py` | Local admin dashboard: calls, reports, reviews, tests, settings, knowledge |
| `review.py` | Automatic review of staff calls against the agent's rules ([docs/testing.md](../docs/testing.md)) |
| `harness.py`, `scenarios.json` | Conversation test harness ([docs/testing.md](../docs/testing.md)) |
| `latency_probe.py` | Offline time-to-first-word check for changes to the speaking path |
| `listening.py` | Echo check, silence re-ask timing, relay message logging |
| `configure.py` | Private credentials and settings |
| `run.ps1` | Windows launcher: tunnel, server, Twilio number update, READY banner |
| `test_*.py` | Offline tests (fake Twilio, Zoho and AI) |

Uses `../mock-backend/static_inventory.py` (device lookup) and `../mock-backend/private_files.py`
(private folder location and owner-only permissions).

## Set up

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe configure.py               # Twilio SID/token, OpenAI key (hidden prompts)
.venv\Scripts\python.exe configure.py --use-claude  # Anthropic key; default Claude Sonnet 5.5
winget install --id Cloudflare.cloudflared          # tunnel for the pilot
```

Also needed in the private folder: `lspcare-inventory.json` and, for tickets, `zoho-desk.json`
(`..\scripts\setup_zoho_desk.py --configure`, then `--check`).

## Run

```powershell
powershell -ExecutionPolicy Bypass -File run.ps1    # wait for the green READY banner, then call
.venv\Scripts\python.exe transcripts.py --follow    # optional: live transcript in a second window
```

If the banner says NOT READY: `.venv\Scripts\python.exe configure.py --update-number`.

## Dashboard

```powershell
.venv\Scripts\python.exe dashboard.py    # opens http://127.0.0.1:8102
```

One place to see calls and transcripts, spoken problem reports, automatic reviews and test runs,
to start a review or a test run (with a cost cap), turn a call into a test scenario, change
non-secret settings and check the knowledge library. It runs on this PC only: it listens on
127.0.0.1, is not reachable through the tunnel, rejects other host names, needs a per-session
token for every action, and never shows credentials. Run it in its own window alongside `run.ps1`.

## Settings

Change with `configure.py --set KEY=VALUE`. These apply on the next call with no restart.
Code changes need a restart.

| Key | Values |
|---|---|
| `anthropic_model` | e.g. `claude-sonnet-5-5` |
| `effort` | `low` · `medium` · `high` · `xhigh` · `max` |
| `thinking` | `between_tools` (off, the default on Sonnet 5.5 at effort high or below) · `adaptive` |
| `tickets` | `dry_run` · `live` · `off` |
| `transcripts` | `on` · `off` |
| `call_reference` | `on` (default: reference spoken at the end of each call) · `off` |
| `flows` | `on` (default: tickets only through the ticket flow) · `off` (direct ticket tool) |
| `staff_feedback` | `on` (default: callers can report a problem by voice) · `off` |
| `say_voice` | voice for the fixed final words, default `Polly.Olivia-Neural` |
| `voice`, `ttsProvider`, `language`, `hints` | ConversationRelay speech settings |
| `ticket_subject_prefix` | default `[TEST] ` |

## Test

```powershell
.venv\Scripts\python.exe -m unittest test_pilot test_claude test_stages test_harness test_pilot_loop test_dashboard test_flows test_knowledge_edit test_flow_edit test_listening
.venv\Scripts\python.exe harness.py --list
.venv\Scripts\python.exe harness.py --only HW-01 --set thinking=adaptive
.venv\Scripts\python.exe review.py                 # review new staff calls (capped at US$0.20)
```
