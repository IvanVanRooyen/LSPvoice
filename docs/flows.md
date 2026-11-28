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
`flow_cancel`. When the step changes, a status note (current step and its instruction, what is
still needed, what is recorded, the read-back) is sent at the start of the next caller turn as a
system message, right after the caller's words, and kept in the conversation: notes are never
removed, so the model's view of the call stays consistent. Flow tool results are data only (what
was recorded, what is still needed, the read-back text), never instructions: instructions inside
or right after a tool result can look like a prompt injection to the model, and it says so. The
step pointer is worked out from what has been recorded, so the agent can't skip ahead. Questions, safety issues
and interruptions are handled normally while a flow waits. A clinical emergency still ends the
call immediately.

The tool list and the cached rules never change while a flow runs, so speed and cost stay the same.
Flows are on by default; `configure.py --set flows=off` (or the dashboard) switches back to the
direct `create_ticket` tool, which is useful for comparisons in the test harness.

## The ticket flow (`flows/log_ticket.json`)

1. **Understand the problem** — the problem twice: for the ticket (`issue_summary`, detailed) and
   as one short sentence to the caller for the read-back (`problem_spoken`, checked: no brackets,
   semicolons, colons or "the caller"); what was tried and urgent are optional
2. **Where is the screen** — hospital, plus bed number or Device ID
3. **Who to call back** — name, Australian callback number
4. **Read back and confirm** — system-written read-back; caller must say yes in a later turn
5. **Log the ticket in Zoho Desk** — runs only after the yes; at most one ticket per call

**The read-back** is built by code from the recorded values with the flow's `readback` template,
so the caller confirms exactly what is read. Each part of the template is said only when every
field it names has a value (a yes/no field only decides whether its part is said), for example:
"Just to confirm: you're Sam at Royal Adelaide, bed 7 G 1 3 1 0 1, and the best number to call you
back on is 0 4 0 0, 1 2 3, 4 5 6. The arm holding your screen keeps dropping. Is that right?"
Phone numbers and fields marked `"say": "characters"` (bed, Device ID) are read one character at a
time, grouped with short pauses, like the ticket number. The template must include every required
detail (`"in_readback": false` marks ticket-only text such as `issue_summary`); a flow without a
template is read as "Label: value". The ticket sent to Zoho Desk is unchanged.

**Holding lines** (fixed wording, spoken by code, never generated) shorten the silences while a
reply waits on a tool and a second model response:
- "One moment while I log that for you." as soon as a ticket is being written (Zoho Desk takes
  several seconds); not when nothing is logged, for example a confirmation that came too early.
- A short acknowledgement ("Got it." / "Thanks." / "Okay.", in turn) when the model's first
  response is a silent call to `flow_record` or `lookup_device`: the caller has just given a detail.
  Other tool turns (opening a guide, starting a flow) and replies without tools get none.

A holding line is part of the turn's spoken text (kept in history; an interruption keeps only what
was heard), is never held back as narration and never counts as the turn's question.

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

### Lost answers (server test call, 28 Nov) and fixes

The agent asked "Which hospital are you at, Royal Adelaide or Royal North Shore?" and the caller's
answer never arrived, twice. Twilio's `echoPromptFiltering` drops caller words that match what the
agent just said, so an answer made only of the agent's words can vanish with no message. Changes:

- **Echo filtering off.** Echo is handled by the non-interruptible greeting, medium interrupt
  sensitivity, ignored backchannel, and an app-side check (`listening.py`): a prompt is ignored as
  echo only if it arrives during or just after the agent's speech, has at least four words and
  repeats the agent's words in order. Short answers (a hospital, a name, a bed number, "yes,
  that's right") always get through. Ignored prompts are logged as "Ignored as echo".
- **Silence fallback.** After a reply ending in a question, if nothing arrives 9 s after its
  estimated playback end (Twilio sends no playback-finished message; the estimate is about 2.6
  words a second), the agent asks once more: "Sorry, I didn't catch that." and the question (the
  whole read-back if that was the question). The re-ask is in history and the transcript. The
  greeting is covered too. After that it waits as before.
- **Every relay message is logged** in the transcript file (`relay` events; phone numbers, account
  and the call's nonce left out), and `speaker-events tokens-played` are switched on, so input
  that never becomes a turn is visible. Only caller messages count towards the per-call limit.

### Server test call (28 Nov) and fixes

A caller asked to resubmit a ticket. Mid-flow the agent said the flow note "sits in the user turn,
so it isn't a genuine system or tool result", took it back, said it again, and said there was no
call reference. It also narrated, talking about the caller in the third person ("<name> says
he's dealt with this before, so I'll record the problem now"). The code was the same as in the good PC calls of 27 Nov; this was the 27 Nov
"comment on the notes" behaviour (above) with its causes only partly removed:

- **Notes were removed after each turn.** The model kept its own remark about "the note above",
  then that note was gone and a new one appeared, so it flip-flopped. Notes now stay in history
  (append-only), with a closing "the flow has ended" note when a flow finishes.
- **Instructions inside tool results.** Each flow tool result carried the step's `instruction`
  (and sometimes `say_next`), and `flow_confirm` the success/failure wording. Results are now data
  only; the wording is in the status note.
- **The prompt didn't say where the notes come from.** It now says they come from LSPcare's own
  system, are genuine, and are never to be mentioned to the caller.
- **Narration with thinking off.** With `thinking: between_tools`, short remarks between tool calls
  arrive as text and were spoken. Now at most one sentence is spoken before the model's tool calls
  are known; text after it is held until the model's round ends, spoken if the round was the answer
  and dropped if it ended in a tool call (logged as "Not spoken (narration before a tool call)").
  A turn that opens like narration ("Let me…", "I'll…", "I'm going to…", "One moment…") is held
  from its first word. The first word is as quick as before (the opening ~25 characters are looked
  at first, about 50 ms). Words said with `finish_call` (the goodbye) are kept.
- **Call reference.** The model now gets it in a system message at the start of the call.

Regression scenarios: `RESUBMIT-01` (bed unknown, asks for the reference) and `RESUBMIT-02`
(completes the ticket).

### Live phone test (27 Nov) and fixes

- Call 1 (ticket) worked end to end (ticket logged in Zoho) but repeated a question ("…which
  hospital are you at? Which hospital are you at, Sam?"). Cause: the model asked, then used a flow
  tool, and the tool's next-step instruction made it ask again. Fix in `conversation.py`: once a
  reply has asked a question before a tool runs, the tool result says so (as data,
  `already_asked_caller`) and anything further the
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
