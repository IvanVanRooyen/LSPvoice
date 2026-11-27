# Testing

Three layers, each for a different job.

| Layer | Checks | Cost |
|---|---|---|
| Offline tests (`voice-agent/test_*.py`, `mock-backend/test_*.py`) | Code-enforced rules: Twilio signatures, knowledge fingerprints, ticket guards, card detection, transcripts, tool validation | Free, seconds |
| Conversation harness (`voice-agent/harness.py`) | How the agent actually behaves in whole conversations | About US$0.35 per full run, capped |
| Phone calls | Speech recognition, voice, interruptions, real delay | Twilio + model usage |

## Offline tests

```powershell
cd voice-agent
.venv\Scripts\python.exe -m unittest test_pilot test_claude test_stages test_harness test_pilot_loop test_dashboard test_flows
cd ..\mock-backend
.venv\Scripts\python.exe -m unittest test_static_inventory test_vouchers
```

## Conversation harness

Runs the **same conversation engine as the phone** (`conversation.py`): same prompt, knowledge,
tools, model and settings, without Twilio or speech.

- **Callers:** `caller.script` (fixed lines) or `caller.brief` (a simulated caller played by
  Claude Haiku 5.5 from a short description).
- **Hard checks (`expect`):** `tools_called` / `tools_not_called` (optionally with arguments),
  `finish` (`clinical`, `goodbye`, `none`), `ticket` (`created` / `not_created`), `ticket_args`,
  `said_any`, `said_none`. Every scenario also checks for no markdown or lists in spoken
  replies, and no card numbers passed to tools.
- **Judge (`judge`):** criteria marked by Claude Haiku 5.5 with structured verdicts.
  `judge_sources` gives the judge the approved document text, so it can tell approved steps
  from invented ones.
- **Safety:** tickets are always forced to dry-run, so tests never create Zoho tickets. Cost is
  measured from API usage and the run stops before `--budget` (default US$0.50).

```powershell
.venv\Scripts\python.exe harness.py --list
.venv\Scripts\python.exe harness.py                                   # all scenarios
.venv\Scripts\python.exe harness.py --only HW-01,TIX-01 --repeat 3     # replies vary: repeat important ones
.venv\Scripts\python.exe harness.py --tag safety
.venv\Scripts\python.exe harness.py --set thinking=adaptive            # try a setting for this run only
.venv\Scripts\python.exe harness.py --from-transcript 4721             # turn a real staff call into a draft scenario
```

Reports: `build/harness/<time>/report.md` (pass/fail, every check, transcripts, cost, time to
first word) and `results.json`. `build/` is not committed.

### Writing a scenario

Add an entry to `voice-agent/scenarios.json`. Prefer `script` for exact regression checks and
`brief` where the conversation must adapt. Keep caller details fictional; RAH bed 7G13101 and
RNS bed 5EC0101 exist in the inventory. Run `test_harness` afterwards: it validates the file.

### Results so far (26 Nov 2026, Claude Sonnet 5.5, effort low)

- The first baseline found that tool results were dropped between turns: the agent re-ran
  lookups every turn. It also found markdown in spoken replies. Both are fixed.
- After the fixes: 18 scenarios, 17/18 passed (the remaining item is a judge disagreement),
  about US$0.33, median time to first word 2.2 s (text only).
- Needs-first call flow (20 scenarios): 18/20 passed. Remaining: an occasional two-part
  question, and one judge disagreement.
- Thinking off (`between_tools`) vs low effort, 9 scenarios: similar pass rate and cost, and the
  slowest replies were about 1.2 s faster (2.6 s vs 3.8 s for the slowest 10%). One formatting
  slip was seen with thinking off.
- Thinking off became the default, with a code filter that removes formatting before speech.
  Confirmed with 9 scenarios × 2 runs: 15/18 passed (the 3 failures were judge disagreements,
  since corrected), US$0.29, median time to first word 1.3 s, slowest 10% 2.3 s, no formatting
  reached speech.
- 27 Nov: 35 new scenarios (55 in total), adapted from the earlier test catalogue and staff
  scenarios: identification variants, terminal identification and naming, every guide, prices,
  buying, safety, unavailable features, topic changes, repeats, problem reports and off-topic
  questions. First run 32/35. Fixes from it: name the identified terminal once, acknowledge every
  problem when a caller raises two, one question per reply, and an honest AI greeting. Three
  test definitions were corrected. All affected scenarios then passed, including repeats
  (about US$1.00 for the whole round, in capped runs).

## Staff testing loop

Staff call the test number in their own time (guide for testers: [staff-testing.md](staff-testing.md)).
Every call is logged so any problem can be found, understood, turned into a test and fixed.

**What is recorded for each call** (`transcripts=on`, private folder, owner-only):
- a 4-digit **call reference**, spoken in the final words ("Your call reference is 4 7 2 1"),
  plus the time and the last 3 digits of the caller's number. A tester who hangs up early can
  still be matched by time and number.
- a **version stamp**: code commit, model, effort, thinking, prompt and knowledge fingerprints.
- the full transcript: caller, agent, tool calls with results, interruptions, timings, ending.
- **spoken problem reports:** a tester can say "I want to report a problem" at any point. The
  agent records it with the `report_problem` tool (also saved to `private-config/feedback.jsonl`)
  and the call is flagged.

**Finding a call:** `transcripts.py --list` (reference, caller digits, turns, flags),
`transcripts.py --find 4721` (by reference or caller digits), `--follow` while calls happen.

**Automatic review:** `review.py` marks every new call against the agent's general rules (short
spoken answers, needs first, approved steps only, clinical redirect, no card details, honest
about unavailable features, ticket read-back, no promises). The judge gets the approved sources
the call used. Calls with a failed rule or a spoken report are listed first in
`private-config/reviews/<time>.md`. Cost: about 1 cent per call, capped (default US$0.20 a run).

**Dashboard:** `voice-agent\dashboard.py` (http://127.0.0.1:8102, this PC only) shows all of the
above in one place: calls needing attention, transcripts, reports, reviews and test runs, with
buttons to review, run tests and turn a call into a scenario.

**Issues:** the dashboard's Issues page keeps one list of problems. Every review finding and
spoken report becomes a *new* issue automatically; reports posted in Teams are added by hand.
The list is kept in `private-config/issues.json` because it quotes calls. Each issue moves
new → open → fixed (or dismissed), with a history of every change.

**Triage cycle (weekly, or after each batch of reports):**
1. Review new calls (dashboard or `review.py`); add Teams reports on the Issues page.
2. For each new issue, read the call and either dismiss it (not a problem, duplicate) or set it
   to open and classify it: knowledge gap, rule or prompt, tool or code, speech recognition,
   voice, speed, or caller expectation. "Recurring themes" groups the same finding across calls
   so they can be updated together.
3. Link a test scenario that reproduces it: "Make a scenario from this call" adds a draft
   (the same as `harness.py --from-transcript <reference>`). Edit its expectations so it fails
   for the problem.
4. Fix (knowledge, prompt, code or settings), run the offline tests, restart, and set the issue
   to fixed (the running code version is recorded).
5. Run the scenario from the issue page. A pass in a run after the fix marks it *verified*.
   Tell the tester it is fixed. "Export CSV" gives the list for sharing (call references only;
   check it holds no names before sending).

Speech recognition, voice quality and barge-in timing can only be judged on real calls. Keep a
short list of phrases that were misheard and add them to `hints` (`configure.py --set hints=...`).
