# Agent onboarding — automated podcast pipeline

You are an agent on this machine tasked with turning PDFs into published
podcast episodes, and answering questions about them afterwards. This file is
your entry point: it tells you where everything lives, how to run the whole
chain, and what to read next. It assumes nothing.

## The handoff prompt

If you were given only this file, this is your job description:

> Work in `/Users/mo64/My Drive/_03_Projects/F_Personal-Apps/podpub`. Read
> `automation/ONBOARDING.md`, `automation/INSTRUCTIONS.md`,
> `automation/MEMORY.md`, and the repo `CLAUDE.md` first — MEMORY.md's
> Standing rules are binding. Given one or more PDFs: build a queue folder
> under `notebooklm/queue/`, write the episode metadata (title.txt, focus.txt,
> and the `.md` description sidecar per CLAUDE.md's format), run
> `.venv/bin/python notebooklm/nlm_pipeline.py run --dry-run` and check the
> plan, then run it for real to generate the NotebookLM Deep Dive audio. When
> the audio lands in `inbox/`, publish with `.venv/bin/python podpub.py`
> (dry-run first), archive the source PDFs into `PDFs/`, commit and push, and
> append a dated entry to `automation/LOGS.md`. Never run two pipeline
> processes at once, never run the pipeline and podpub.py simultaneously, and
> never touch the `default` notebooklm profile — the pipeline uses
> `-p dedicated`.

## Where everything is

| Path | What it is |
|---|---|
| `notebooklm/nlm_pipeline.py` | The generation pipeline CLI (`run`, `status`, `--dry-run`, `--limit N`). Run with `.venv/bin/python`. |
| `notebooklm/queue/<episode>/` | Input drop zone: one folder per episode containing PDFs + `title.txt` + `focus.txt` + `<Slug>.md`. |
| `notebooklm/queue/.archived/` | Where queue folders go after their episode is published (never deleted). |
| `notebooklm/state.json` | The pipeline's memory: per-episode status, **notebook_id**, task_id, quota ledger. Read it to find an episode's notebook. Never delete it. |
| `notebooklm/config.yaml` | Pipeline config (gitignored): paths, `profile: dedicated`, `daily_audio_cap`, generation format/length/timeouts. Template: `config.yaml.example`. |
| `notebooklm/logs/pipeline.log` | Machine log of pipeline runs (rotating). |
| `notebooklm/tmp/`, `notebooklm/quarantine/` | Downloads in flight; failed-verification files with `.reason.txt`. Never delete quarantine contents. |
| `notebooklm/README.md` | Pipeline design doc + setup + failure tables. |
| `inbox/` | podpub's drop zone. The pipeline delivers `<Slug>.m4a` + `<Slug>.md` here; `podpub.py` consumes them. |
| `podpub.py` | The publisher: transcribes (WhisperX), numbers the episode, rebuilds `feed.xml`, commits, pushes. |
| `audio/`, `transcripts/`, `feed.xml` | Published artifacts, served by GitHub Pages. **Never move or rename** — URLs are baked into the feed. |
| `PDFs/` | Tracked archive of every episode's source papers: `NNN-YYYY-LastName-Short-Title.pdf`. |
| `automation/INSTRUCTIONS.md` | The detailed step-by-step playbook (intake → publish → archive). Follow it for every run. |
| `automation/MEMORY.md` | Curated standing rules. **Binding.** Read before every run. |
| `automation/LOGS.md` | Append-only run history. You MUST add an entry after every pipeline run. |
| `CLAUDE.md` (repo root) | podpub's own workflow + the episode-description format (the `.md` sidecar spec lives here, nowhere else). |
| `~/.notebooklm/profiles/` | notebooklm-py's auth storage (outside the repo). `dedicated` = the pipeline's account; `default` = the user's personal account — do not use it. |

Published episode URLs: `https://soltaniehha.com/podpub/feed.xml` (feed),
`https://soltaniehha.com/podpub/audio/NNN%20-%20Title.m4a` (audio),
`.../transcripts/NNN%20-%20Title.vtt` (transcript). GitHub repo:
`https://github.com/soltaniehha/podpub` (Pages deploys `main` automatically,
usually within ~2 minutes; check `gh api repos/soltaniehha/podpub/pages/builds/latest`).

## Running the full pipeline (condensed)

The authoritative version with failure handling is `automation/INSTRUCTIONS.md`.
The shape:

```bash
cd "/Users/mo64/My Drive/_03_Projects/F_Personal-Apps/podpub"

# 0. Preflight
.venv/bin/python notebooklm/nlm_pipeline.py status     # cli version, auth, quota, lock

# 1. Intake — one folder per episode
mkdir -p notebooklm/queue/my-episode
cp paper1.pdf paper2.pdf notebooklm/queue/my-episode/
#    + title.txt (one line), focus.txt (see next section),
#    + <Slug>.md description sidecar per CLAUDE.md's format (write it NOW, not after)

# 2. Preview, then generate (10–20+ min; 1 quota unit per episode, 3/day)
.venv/bin/python notebooklm/nlm_pipeline.py run --dry-run
.venv/bin/python notebooklm/nlm_pipeline.py run

# 3. Verify delivery, then publish
ls inbox/                                              # expect <Slug>.m4a + <Slug>.md
.venv/bin/python podpub.py --dry-run
.venv/bin/python podpub.py                             # transcribe, feed, commit, PUSH (public!)

# 4. Archive PDFs (see INSTRUCTIONS.md Step 7), then append your LOGS.md entry
```

Exit codes and every failure state are tabulated in INSTRUCTIONS.md Step 4.
The short version: exit 0 check inbox, exit 1 read the log, exit 2 stop and
ask the human to re-login, exit 3 quota/rate-limit — stop for today, exit 4
another run is active, exit 5 read the error.

## Customizing the podcast generation prompt

The two hosts are steered per-episode by `focus.txt` in the queue folder — it
is passed to `notebooklm generate audio` as the prompt. This is the single
highest-leverage file for episode quality:

- Derive it from the papers' actual theses after reading the PDFs. Name the
  specific models, methods, results, and tensions you want discussed.
- For multi-paper episodes, state the connecting thread, not per-paper
  summaries.
- Without `focus.txt` the pipeline falls back to a generic template
  (`episodes.py:DEFAULT_FOCUS_TEMPLATE`) — noticeably worse output.

Episode-independent knobs live in `notebooklm/config.yaml` under `generate:`:
`format` (deep-dive | brief | critique | debate), `length` (short | default |
long), timeouts. The show default is `deep-dive`/`default`, pinned by the user
(2026-08-10). Other formats only rarely, with a strong stated justification,
for example two opposing lead papers suiting a debate. If a run changes the
config it changes it back after.

## Asking questions back to the NotebookLM notebook

Every episode's notebook stays alive in NotebookLM after publishing, with the
source PDFs attached — you can use it as a grounded Q&A engine over the
episode's papers (answers come with inline citations into the sources).

**Finding the notebook.** `notebooklm/state.json` → `episodes` → the entry
whose `slug`/`title` matches the episode → `notebook_id`. Alternatively
`notebooklm -p dedicated list` shows all notebooks in the pipeline's account
(episode title = notebook title).

**Profile rule.** All notebooks from 2026-08-10 onward live in the `dedicated`
profile — always pass `-p dedicated`. Exception: episode 013 ("When AI Makes
Discoveries", notebook `97d181b1-…`) predates the switch and lives in the
user's personal `default` profile — do not query it without the user asking.

```bash
# One-off question, answer cites sources as [1], [2]:
notebooklm -p dedicated ask -n <NOTEBOOK_ID> "What accuracy did AlphaFold reach at CASP14?"

# Structured output (source IDs per reference) for programmatic use:
notebooklm -p dedicated ask -n <NOTEBOOK_ID> "..." --json

# Long prompts from a file:
notebooklm -p dedicated ask -n <NOTEBOOK_ID> --prompt-file question.txt

# Restrict to specific sources (ids from: notebooklm -p dedicated source list -n <ID> --json):
notebooklm -p dedicated ask -n <NOTEBOOK_ID> -s <SOURCE_ID> "..."

# Ideas for good questions, and past conversation turns:
notebooklm -p dedicated suggest-prompts -n <NOTEBOOK_ID>
notebooklm -p dedicated history -n <NOTEBOOK_ID>
```

Conversation state: `ask` continues the notebook's last server-side
conversation by default; `-c <conversation_id>` targets a specific one.
**Caution: `--new` is destructive** — it deletes the notebook's current
server-side conversation before starting fresh (it will prompt; never pass
`--yes` on someone else's conversation without the user's say-so).

Beyond chat, the same notebook can generate further artifacts from the same
sources — `notebooklm -p dedicated generate report|quiz|flashcards|mind-map
-n <ID>` and matching `download` commands (`notebooklm generate --help` for
the full list). Only ONE audio overview exists per notebook — never
`generate audio` on a published episode's notebook; that is what the
one-notebook-per-episode rule exists for.

These `ask`/`suggest-prompts`/`history` calls are read-mostly and cost no
audio quota, but they are still automated access on the dedicated account —
be reasonable about volume, and back off on errors per MEMORY.md rule 7.

## Ground rules (the short list — MEMORY.md is the full one)

1. One pipeline run at a time; never concurrently with `podpub.py`.
2. Publishing is PUBLIC — `podpub.py` pushes to the live feed. Sidecar first.
3. Quota: 3 audio generations/day, tracked locally only. Check `status` first.
4. Never delete: PDFs, queue folders (archive them), quarantine files, state.json.
5. Auth problems → stop and ask the human. Never run `notebooklm login` yourself.
6. After every run: dated entry in `automation/LOGS.md`; durable lessons
   promoted to `automation/MEMORY.md`.
