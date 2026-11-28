# Working on LSPvoice

Guidance for anyone (person or AI agent) developing, testing or troubleshooting this repository.
What the system is and how it is laid out: [README.md](README.md). This file is the working
rules and the lessons behind the current design. Keep it current when the design changes.

## Rules

- **No personal details in the repository**: no people's names, hostnames, domains, phone
  numbers, account IDs or local paths. Use roles ("project lead", "product owner") and
  placeholders (`voice.example.com`, `<server>`). Test callers are fictional ("Sam Taylor").
- **No secrets anywhere**: not in files, commits, logs, test output or chat. Credentials live
  only in the private folder (`private-config/`, or `/data/private` in the container), owner-only.
  Never ask anyone to paste a key or token into chat; ask them to put it in the file themselves.
- **No AI attribution**: no `Co-Authored-By`, "Generated with" or "AI-assisted" lines in commits,
  PRs or files. Naming Claude Sonnet as the model the voice agent runs on is a product fact and fine.
- **Commits and pushes only when the project lead says so.** Otherwise prepare the change on a
  branch, with a commit message file, and report.
- **Twilio number routing, live tickets and spending** are the project lead's decisions: never
  run `configure.py --update-number` or switch `tickets` without a go-ahead, and don't run large
  harness sets unasked (each full run costs money; runs are capped by `--budget`).
- **One agent process, one worker.** Call state (sessions, nonces, ticket status) is in memory.
- **Text files are LF, UTF-8, no BOM** (`.gitattributes`). Knowledge files are fingerprinted: a
  CRLF or BOM change alters the sha256 and the agent refuses calls.

## Before you commit

1. Run every offline test (all must pass; 144 + 35 at the time of writing; on the server, use
   the container command under "Running and deploying on the server"):
   ```
   cd voice-agent
   python -m unittest test_pilot test_claude test_stages test_harness test_pilot_loop test_dashboard test_flows test_knowledge_edit test_flow_edit test_listening
   cd ../mock-backend && python -m unittest test_static_inventory test_vouchers
   ```
   `test_pilot` hangs (rather than fails) if a test waits for speech that is never sent; run
   modules one at a time with a timeout to find which.
2. For a change in behaviour, add a test that fails without the fix. Reproduce real-call bugs
   offline first (`FakeClient` / `FakeStream` / `tool_use` in `test_claude.py` script a model;
   `test_pilot.PilotTests` drives the real `/relay` WebSocket).
3. Scan the diff: CRLF, BOM, personal details, secrets, attribution.
4. Update the docs the change touches (`docs/flows.md` keeps the design history and test results).
5. Report: the commit hash, what changed and why, test count, and measurements where speed is
   affected (below).

## Running and deploying on the server

The service runs in Docker (`compose.yaml`, runbook `docs/home-server.md`). The image contains
the code, knowledge, flows and scenarios; the dashboard's editing is off in the container. So a
change is: edit in the checkout → tests → commit → rebuild:

```
export GIT_COMMIT=$(git rev-parse --short HEAD); docker compose up -d --build
```

- Rebuild only when no call is in progress (dashboard Live page, or
  `docker compose exec agent python transcripts.py --list`). A restart cuts live calls.
- Deploy committed code: every transcript is stamped with `GIT_COMMIT`, and uncommitted changes
  would carry the previous commit's stamp.
- Tests run against the checkout using the image's dependencies, without the private data. The
  server has no Python of its own, so this is the way (from the repository root, using the
  deployed image):
  ```
  IMAGE=$(docker compose ps agent --format '{{.Image}}')
  docker run --rm --network none --tmpfs /tmp -v "$PWD":/src:ro -w /src/voice-agent \
    -e LSPCARE_PRIVATE_DIR=/tmp/p -e LSPCARE_RUNS_DIR=/tmp/r -e PYTHONDONTWRITEBYTECODE=1 \
    "$IMAGE" python -m unittest test_pilot test_claude test_stages test_harness \
    test_pilot_loop test_dashboard test_flows test_knowledge_edit test_flow_edit test_listening
  ```
  The same with `-w /src/mock-backend` and `test_static_inventory test_vouchers`, and with
  `python latency_probe.py` for the speed probe. `--network none` guarantees no API calls. If
  `requirements.txt` changed, build first (`docker compose build`) and set `IMAGE` to the new
  `lspvoice:<commit>` image. Counts on
  28 Nov 2026: 144 voice-agent tests (1 skipped) and 35 mock-backend tests.
- Harness and reviews need the API key, so they run in the agent container:
  `docker compose exec agent python harness.py --only TIX-01 --repeat 2`. Results go to `/data/runs`.
- Windows-only pieces (`run.ps1`, `.venv\Scripts\...` in older docs) are for a PC set-up.

## How a call works (where to look)

`app.py` `/voice` returns TwiML with `<ConversationRelay>` → Twilio opens `/relay` (WebSocket) →
each caller `prompt` becomes a turn in `conversation.py` → `claude_reply.py` streams the model,
runs tool rounds through `app.make_runner` (flows, lookup, guides, tickets) → spoken text goes
back as `text` tokens → `/end` handles the fixed final words. `listening.py`: echo check, silence
re-ask, relay logging. `flows.py` + `flows/*.json`: code-enforced ticket flow. `tickets.py`:
guarded ticket creation. The harness (`harness.py`) runs the same `conversation.py` in text.

## Design rules (each learned from a real failure)

- **Tool results are data only.** Field names, values, the read-back text and short codes
  (`mentions_the_caller`, `readback_not_answered`, `not_au_phone`, …); never instructions,
  examples or how-to wording. The model treats instructions in tool results as possible prompt
  injection, comments on them, or says them aloud ("The problem fields need a spoken sentence and
  a summary, so I'll record those"). What each code means is in the system prompt
  (`app.TICKET_CODES`); how to ask for each field is in the flow status note. Fix values in code
  where possible (`flows.tidy_spoken`) instead of bouncing them back.
- **Flow status notes are system messages at the start of a turn**, right after the caller's
  words, and stay in history (append-only; `FLOW_ENDED_NOTE` when a flow ends). Removing or
  replacing old notes made the model argue with "the note above". Notes after every tool result
  made it comment on them.
- **System-message placement** (Claude API): a mid-conversation `role: system` message must
  follow a user message and be followed by an assistant message or end the array. Turn notes are
  stored only for completed turns; an interrupted turn must not leave one dangling (a 400 error).
- **Nothing the model says before a tool call is spoken.** All text of a model round is held
  until the round ends (`conversation._round_end`): spoken if it was the answer, dropped and
  logged as `suppressed` if the round ended in a tool call. Narration can't be recognised by its
  wording; an earlier first-sentence gate let it through. Cost: ~0.05 s (one sentence) to ~0.4 s
  (three) on replies without tools. Words said with `finish_call` (the goodbye) are spoken.
- **Holding lines are fixed text from code**, never generated: the ticket line when a ticket is
  being written, and an acknowledgement ("Got it." / "Thanks." / "Okay.") only after a silent
  first-round `flow_record` / `lookup_device`. A model reply that starts with a bare
  acknowledgement after one is trimmed (no "Thanks. Thanks.").
- **Rules that must never break are in code, not the prompt**: ticket only after a read-back the
  caller answered in a later turn, one ticket per call, card-number detection, approved guides
  only, the fixed clinical ending. The read-back is built by code and said word for word; a
  read-back that was dropped unheard is reset so a "yes" can't confirm it.
- **Interruptions**: history keeps only what the caller heard (`utteranceUntilInterrupt`).
- **Echo filtering is off** (`echoPromptFiltering="false"`): Twilio's filter silently dropped
  answers made of the agent's own words ("Royal Adelaide" after "Royal Adelaide or Royal North
  Shore?"). Echo is handled by the non-interruptible greeting, medium interrupt sensitivity,
  ignored backchannel and `listening.is_echo` (4+ words repeating the agent, during or just
  after its speech). Don't switch Twilio's filter back on.
- **Silence re-ask** counts 9 s from Twilio's `agentSpeaking` "off", restarts on "on", is
  cancelled by `clientSpeaking` "on"; the playback estimate is only a fallback.
- **Latency first**: thinking `between_tools` at effort low (Sonnet 5.5) is the default because
  it was ~1 s faster on slow replies; `SpokenText` strips formatting before speech. The tool list
  and cached rules never change within a call (prompt caching).
- **Fix causes, not symptoms.** A prompt rule like "don't narrate" did not stop narration that
  tool-result wording caused. Find what the model was shown.

## ConversationRelay messages seen on real calls

- `setup` {sessionId, direction, callType, callStatus, customParameters…}
- `prompt` {voicePrompt, lang, last}
- `info` with `name` `agentSpeaking` / `clientSpeaking`, `value` "on" / "off" (`speaker-events`);
  `agentSpeaking` "on" arrives ~1 s after text is sent, "off" with the last `tokensPlayed`;
  `clientSpeaking` "off" arrives with the final prompt.
- `info` `tokensPlayed`, `value` = the text chunk played (`tokens-played`).
- `interrupt` {utteranceUntilInterrupt} when the caller talks over the agent; no `dtmf` seen yet.
Every relay message is logged as a `relay` transcript event (private fields left out).

## Troubleshooting a call

1. Get the call reference (spoken at the end of each call) and open it: dashboard Calls page
   (`ssh -L 8102:127.0.0.1:8102 <server>`, then http://localhost:8102) or
   `docker compose exec agent python transcripts.py --find <ref>`. Raw JSONL is in the private
   `transcripts/` folder.
2. Read the events in order: `caller`, `agent`, `tool` (args and result exactly as the model saw
   them), `suppressed` (text not spoken, with reason), `interrupted` (what was heard), `relay`
   (every Twilio message), `echo_ignored`, `silence` (`timed_from`), `reprompt`, `turn`
   (`first_word_ms`, `total_ms`, tools), `finish`. The version stamp says which commit ran.
3. Odd wording from the model: look first at what it was shown that turn: tool results, status
   notes, system prompt. Lost or late speech: `relay` events and timings.
4. Reproduce as an offline test, then (for behaviour) a harness scenario:
   `harness.py --from-transcript <ref>` drafts one from the call; edit its expectations so it
   fails for the problem. Scenarios repeat with `--repeat` because replies vary.
5. Speed: `voice-agent/latency_probe.py` measures time to first word offline (stand-in model
   streaming at ~80 tokens a second, no network). Compare against an earlier commit with
   `git worktree add /tmp/before <commit>` and run the probe there with `PYTHONPATH=.`.
6. Things only a real call shows: speech recognition, voice, echo, barge-in and playback timing.

## Documents

`docs/flows.md` (flow engine, design history, test results), `docs/testing.md` (tests, harness,
staff-testing loop, issue triage), `docs/knowledge.md` (changing reviewed knowledge and its
fingerprints), `docs/business-rules.md`, `docs/home-server.md` (Docker runbook),
`docs/deployment-plan.md`.
