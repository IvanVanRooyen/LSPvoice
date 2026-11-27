# Knowledge workflow

## How the agent uses knowledge

Each document in `knowledge/registry.json` that the agent may use has a delivery mode in
`voice-agent/knowledge.py` (`DELIVERY`):

| Mode | Behaviour | Current documents |
|---|---|---|
| `core` | Included in every request (limit 40,000 characters) | KB-PURCHASE, KB-PRICES, KB-UNKNOWN-HARDWARE, KB-ARM-FAULTS |
| `guide` | Fetched whole by exact ID with `get_guide` once the caller's screen is identified; the model only sees the registry `description` until then | KB-TERMINAL-LSPC15MF, KB-TERMINAL-TOUCH-KEYS |
| `search` | Split at `##` headings into an in-memory full-text index; queried with `search_knowledge` (top 3 sections) | none yet (for manuals and FAQs) |

Documents not listed in `DELIVERY` are never loaded. KB-STAFF-CONTACTS stays excluded until
staff and patient routing are separated.

## Changing an existing document in the dashboard

Knowledge → open the source → **Edit**. Change the text (and, for a guide, the routing text the
agent sees before fetching it), say who approved it by role and what changed, then **Preview and
check**. The dashboard applies the change to a scratch copy of the library and loads it exactly
as the agent does (fingerprints, readiness, size limits), and shows the differences. **Save**
then:

- keeps the current text as `knowledge/content/archive/<file>-v<old version>.md`
- writes the new text (LF line endings) and updates the registry entry: version bumped,
  `content_sha256`, `reviewed_date`, `approval` and a `change_history` line
- refuses to save over a newer edit made since the editor was opened

The running agent re-reads the library when files change, so the edit is used from its next
reply. Then run the related scenarios (listed on the source page) and commit `knowledge/`.
New documents, delivery modes and readiness changes still follow the steps below.

## Adding or changing a document

1. **Prepare the content.** Turn the original material (manual, guidance, photos) into a clear
   Markdown document for a phone agent. Claude can help: give it the original, ask for a draft
   that only states what the source supports, and list open questions for a subject expert.
2. **Review.** A subject expert answers the open questions and approves the wording.
3. **Save** the file in `knowledge/content/`.
4. **Register it** in `knowledge/registry.json`:
   - bump `version` (new entry: `1.0`)
   - set `content_sha256` to the file's sha256, e.g.
     `py -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" knowledge\content\<file>.md`
   - set `readiness` to `development_ready` and `export` to `true`
   - write a clear `description`: for guides this is the routing text the agent sees ("Use only when the caller confirms…")
   - record who approved it, and when, in `approval`
5. **Choose delivery:** add `'<ID>': 'core' | 'guide' | 'search'` to `DELIVERY` (new documents only).
6. **Add a test:** a harness scenario (or a `retrieval-checks.csv` row) that asks a question
   only the new document answers.
7. **Check:** run the offline tests and the affected harness scenarios, then restart the agent.

Keep business policy (prices, eligibility, terms) in reviewed documents or code, never in staff
feedback notes. Feedback can propose a change; only review approves it.
