# Flows: code-enforced steps for multi-step processes

Most of a call is free conversation: the agent follows its rules and the reviewed knowledge.
Some processes must happen in a fixed order, though. A ticket needs details collected, read back
and confirmed before it is logged. Later, vouchers and payment links will need terms read and
accepted before anything is issued. For those, a **flow** makes the order a guarantee instead of
an instruction.

## How it works

A flow is a JSON file in `voice-agent/flows/`. It lists **fields** (with types) and **steps**:

| Step kind | What happens | Enforced in code |
|---|---|---|
| `collect` | The agent asks for details and records each one with `flow_record` | Each value is checked (Australian phone numbers, no card numbers, yes/no fields). The step is done only when its required fields are recorded |
| `confirm` | The system writes the read-back; the agent says it word for word and waits | `flow_confirm(yes)` is refused until the read-back exists **and** the caller has spoken since. Any change to a detail needs a new read-back |
| `action` | The system runs the action (for example `create_ticket`) | The agent cannot run it directly: it only happens after the confirmation |

The agent moves through a flow with four tools: `start_flow`, `flow_record`, `flow_confirm` and
`flow_cancel`. The current step is shown to it at the start of each caller turn as a short note,
and every flow tool's result says what is still needed and what to say next. The step pointer is
worked out from what has been recorded, so the agent can't skip ahead. Questions, safety issues
and interruptions are handled normally while a flow waits. A clinical emergency still ends the
call immediately.

The tool list and the cached rules never change while a flow runs, so speed and cost stay the same.
Flows are on by default; `configure.py --set flows=off` (or the dashboard) switches back to the
direct `create_ticket` tool, which is useful for comparisons in the test harness.

## The ticket flow (`flows/log_ticket.json`)

1. **Understand the problem** — problem (required), what was tried, urgent (optional)
2. **Where is the screen** — hospital, plus bed number or Device ID
3. **Who to call back** — name, Australian callback number
4. **Read back and confirm** — system-written read-back; caller must say yes in a later turn
5. **Log the ticket in Zoho Desk** — runs only after the yes; at most one ticket per call

The dashboard's **Flows** page draws this from the file. `python flows.py --diagram log_ticket`
prints a Mermaid diagram for documents.

## Changing a flow in the dashboard

Flows → **Edit**. The form changes the wording (what the agent is told at each step, how each
detail is asked for, labels used in the read-back), whether a detail is optional and its length
limit. "Advanced" edits the whole flow as JSON (add fields or steps, change the order).
**Preview and check** validates the result exactly as the agent does at start-up, together with
the other flows, and shows what changes and the new drawing. The checks include what each action
needs: the ticket flow must ask for hospital, bed or Device ID, name, an Australian callback
number and the problem, as required details, before the confirm step.

**Save** keeps the previous file in `voice-agent/flows/archive/`, and writes the flow with a
`version` and a `change_history` line (approved by role, with a note). The voice agent reads
flows when it starts, so **restart `run.ps1`** for calls to use the new version, run the `flow`
and `tickets` scenarios, and commit `voice-agent/flows/`.

## Adding a new flow

1. Copy an existing file in `voice-agent/flows/` and edit the fields, steps and wording. Keep
   instructions short and spoken-style; one question at a time.
2. Actions must exist in code (`ACTION_NAMES` in `flows.py`, implemented in `app.make_runner`).
   An action step must come after a confirm step; `python flows.py --check` enforces this.
3. Write harness scenarios for the new flow, including a correction ("no, the number's wrong")
   and a safety interruption, then run the offline tests and the harness.
4. Review the drawing on the dashboard's Flows page with whoever owns the process.

## Test results (27 Nov 2026, `flows` branch)

- 13 new offline tests (85 in total), including a four-turn ticket call through the real engine
  in which the AI tries to confirm before the caller has answered, and is refused.
- Ticket scenarios with flows on: first run 6/9. Fixes: read-back said word for word with the
  name first, record only what the caller said, call tools before speaking (no repeated
  questions), flow note only at the start of each turn (notes after every tool result made the
  model comment on them). After the fixes: 8/8 including repeats, and the 12 core non-ticket
  scenarios 12/12.
- Open polish item: occasional narration such as "I'll look up your bed first".

### Live phone test (27 Nov) and fixes

- Call 1 (ticket) worked end to end (ticket logged in Zoho) but repeated a question ("…which
  hospital are you at? Which hospital are you at, Sam?"). Cause: the model asked, then used a flow
  tool, and the tool's next-step instruction made it ask again. Fix in `conversation.py`: once a
  reply has asked a question before a tool runs, the tool result says so and anything further the
  model says in that reply is dropped (logged as "muted"). If a read-back was dropped that way, it
  is reset, so a "yes" can never confirm details the caller didn't hear.
- Calls 2 and 3 were cut off during the greeting, and the agent used names the caller never said.
  Cause: echo. The agent's voice came back through the caller's microphone, triggered an
  interruption and was transcribed ("this is LSPcare's" → "This is Alice"). Fix: the call setup
  now uses Twilio's documented `echoPromptFiltering="true"` (with ElevenLabs word timings), a
  greeting that always plays in full (`welcomeGreetingInterruptible="none"`),
  `interruptSensitivity="medium"` and `ignoreBackchannel="true"`. Each can be changed with
  `configure.py --set`. Echo can only be confirmed on a real call, not in the harness.
- After the fixes: 89 offline tests; harness 11/12 over two runs of six scenarios (the one fail was
  a judge contradiction), with no repeated questions.
