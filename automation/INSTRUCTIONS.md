# Automated episode pipeline — agent playbook

You are a Claude agent taking PDFs from idea to a published podcast episode.
This file is the end-to-end procedure. Follow it in order.

**Read first, every run:**
0. New to this repo? `automation/ONBOARDING.md` — the map of where everything
   is, plus prompt customization and post-publish notebook Q&A.
1. This file.
2. `automation/MEMORY.md` — the curated Standing rules; they override your
   instincts. (Per-run history lives in `automation/LOGS.md`; skim the newest
   entries if you are debugging something odd.)
3. The repo's `CLAUDE.md` — the description format and the publish workflow.

**Append this run's entry to `automation/LOGS.md` at the end of every run,
and promote anything durable into `automation/MEMORY.md`'s Standing rules. That
step is mandatory and is Step 8 below.**

---

## Hard rules

- **Never run two `nlm_pipeline.py` instances at once.** The lockfile enforces
  it; do not delete `notebooklm/.lock` to get around it.
- **Never run `nlm_pipeline.py` and `podpub.py` at the same time.** Both write
  into `inbox/`; podpub also rewrites `feed.xml` and pushes git. Sequential only.
- **Never regenerate audio to "retry" a slow generation.** A Deep Dive takes
  10–20 minutes and each generation costs a quota unit that cannot be refunded
  or even queried. If a wait times out, the pipeline polls on the next run —
  that is correct behavior, not a failure.
- **Never delete a PDF, a queue folder, or anything in `quarantine/`.** An
  earlier workflow deleted source PDFs after publishing and lost the episode
  008–011 papers. Finished queue folders get *archived*, not removed — Step 7.
- **Never run `notebooklm login` yourself.** It opens a browser for a human.
  When auth is dead, stop and tell the user.
- **Never commit `notebooklm/config.yaml`, `state.json`, or anything under
  `queue/`, `tmp/`, `quarantine/`, `logs/`.** They are gitignored; keep it that way.

---

## Step 0 — Preflight

```bash
cd "<repo root>"
.venv/bin/python notebooklm/nlm_pipeline.py status
```

Check the output, which looks like this when healthy:

```
INFO    cli:     NotebookLM CLI, version 0.8.0 (8fb61cb1)
INFO    quota:   0/3 used today (2026-08-09), 3 remaining
INFO    lock:    free
```

Decision points:

- **`cli:` line reports `is not on PATH`** → stop. Tell the user to run
  `uv tool install --python 3.13 "notebooklm-py[browser]==0.8.0"`.
- **`cli: … - MISMATCH`** → stop. `run` will refuse to start. The pipeline is
  verified against 0.8.0 only; reinstall that exact version, or follow
  "Upgrading the CLI" in `notebooklm/README.md` if the upgrade is intentional.
- **`lock:` anything other than `free`** → stop. `RUNNING` means a run is in
  progress; `held by pid N on another host` means another machine on this Drive
  folder is running one; `unreadable` is treated as held. Wait in all three
  cases, and never delete the lockfile to get moving.
- **`quota: 3/3 used today`** → no generation is possible today. You can still
  do intake (Steps 1–2) and publish anything already sitting in `inbox/`.
- **`config error`** → `cp notebooklm/config.yaml.example notebooklm/config.yaml`
  and set `notebooklm_bin` / `ffprobe_bin` to absolute paths.
- **`SIDECAR MISSING`** next to a queued episode → you still owe it a `.md`
  (Step 2).

If `.venv/bin/python` fails with "permission denied" or "no such file", the
Drive-synced venv is broken — see "Fresh-machine gotchas" in `CLAUDE.md` and
rebuild it before continuing.

## Step 1 — Intake: build the queue folder

For each new episode, create one folder under `notebooklm/queue/`:

```bash
mkdir -p notebooklm/queue/<short-kebab-name>
cp /path/to/paper.pdf notebooklm/queue/<short-kebab-name>/
```

Multiple PDFs in one folder = one thematic multi-paper episode. Separate
folders = separate episodes — but note that episode identity is the *hash of the
PDF contents*, so two folders holding the same paper are one episode. The
pipeline processes the first and skips the rest with a warning; to make two
genuinely different episodes from one paper, they need different sources.

Then write the two metadata files. Both are optional to the tool and important
in practice — the defaults (title from folder name, focus from title) produce a
noticeably worse episode.

**`title.txt`** — one line, the episode title as listeners will see it. This
becomes the delivered filename and therefore the podcast episode title:

```bash
cat > notebooklm/queue/<name>/title.txt <<'EOF'
Could AI Pass Introductory Physics
EOF
```

**`focus.txt`** — the prompt that steers the two hosts. Derive it from the
paper's actual thesis after reading the PDF; name the specific things you want
discussed. Generic prompts produce generic episodes:

```bash
cat > notebooklm/queue/<name>/focus.txt <<'EOF'
Deep dive into whether GPT-4 can pass a calculus-based introductory physics
course. Walk through the exam methodology, where the model's reasoning breaks
down on multi-step mechanics problems, and what this means for physics
assessment design.
EOF
```

For a **multi-paper** episode, the focus prompt must state the thread that
connects the papers, not just summarize each.

## Step 2 — Author the `.md` sidecar BEFORE generating

The sidecar is the episode description that ships in the public RSS feed and
shows up in Apple Podcasts. Write it now, not after — an episode delivered
without one needs a second pass and risks being published with a placeholder
description.

Read the PDF(s) with the `Read` tool, then write the sidecar to
`notebooklm/queue/<name>/<Slug>.md`, where `<Slug>` is the title with spaces
replaced by underscores (e.g. `Could_AI_Pass_Introductory_Physics.md`). A single
`.md` file with any name also works, but matching the slug is unambiguous.

**Follow the exact format specified in the repo's `CLAUDE.md`** — see
"Standardized episode description format" and its non-negotiable rules
(two prose paragraphs, first-person plural, present tense, named technical
contributions, APA-ish Reference line, Google Scholar citations only if known).
Do not invent a variant of that format here; `CLAUDE.md` is the single source of
truth for it, including the multi-paper variant.

## Step 3 — Dry run

```bash
.venv/bin/python notebooklm/nlm_pipeline.py run --dry-run
```

This makes no network calls and writes nothing. Verify for each episode:

- `sidecar:` names your `.md` file and does **not** say `MISSING`.
- `pdfs:` lists every paper you intended.
- `delivers:` is the filename you want as the episode title.
- `action:` is what you expect (`create notebook…` for new work,
  `poll existing task` for in-flight work, `skip` for finished work).
- The quota arithmetic leaves room for every episode you queued.

Fix anything wrong in the queue folder and re-run the dry run. It is free.

## Step 4 — Run

```bash
.venv/bin/python notebooklm/nlm_pipeline.py run
```

Expect 10–20 minutes **per episode** — this blocks. Do not start a second
instance and do not kill it because it looks stuck; `notebooklm/logs/pipeline.log`
shows progress.

Handle the exit code:

| Exit | Meaning | Your action |
|---|---|---|
| **0** | All delivered / skipped / still generating | Check whether anything actually reached `inbox/` (Step 5). If an episode is `pending`, re-run later — it will poll, not regenerate. |
| **1** | An episode failed or was quarantined | Read `notebooklm/logs/pipeline.log` and the `.reason.txt` in `notebooklm/quarantine/`. See "Failure states" below. Other episodes may still have succeeded — exit 1 does not mean the run accomplished nothing. |
| **2** | Auth is dead | **Stop.** Tell the user: "NotebookLM auth expired — please run `notebooklm login` in a terminal, then I'll retry." Do not attempt login yourself, do not loop. |
| **3** | Daily cap reached, or rate limited | Stop generating today. Remaining episodes stay queued; the next run picks them up. Do not retry immediately — rate limits get worse when hammered. You may still publish anything already delivered. |
| **4** | Lock held | Usually another run is active: wait, and do not delete the lock. An **unreadable or half-written `.lock`** also blocks, deliberately — on a Drive-synced volume a partially synced lockfile is more likely a live run than a dead one, and the pipeline will not steal it. Only a human clears that: confirm no run is active on *any* machine sharing this folder, then delete `notebooklm/.lock` by hand. |
| **5** | Config / state / tooling problem, including a CLI version mismatch | Read the error — it names the fix. Never "fix" a corrupt `state.json` by deleting it (see below). |

### Failure states in detail

**Generation still pending after the timeout.** Normal for long Deep Dives.
State keeps the `task_id`. Re-run the pipeline later; it polls that task.
Never delete the record to "start clean" — that regenerates and re-bills.

**Episode marked `failed` (server-side).** Recorded with the error and skipped
on later runs; the run reports exit 1. Look at the notebook in the NotebookLM
web UI. If it genuinely produced nothing, clear that episode's `status`,
`task_id`, and `attempts` in `notebooklm/state.json`, then re-run. Tell the user
you are spending another quota unit.

**Episode marked `failed` with "Refusing to regenerate blindly".** State says a
generation was started but no `task_id` was saved, and the notebook has no audio
artifact to adopt. Only that episode is affected — the rest of the queue ran
normally. Open the notebook in the web UI: if audio is there, the next run
adopts it automatically; if the notebook is genuinely empty, clear that
episode's `status`, `task_id`, and `attempts` in `notebooklm/state.json` to
allow one retry, and tell the user it costs a quota unit.

**Inbox filename already taken (exit 1, `outcome failed`).** Nothing is wrong
with the audio: it is verified and waiting in `notebooklm/tmp/`, and the record
stays `generated`. Publish or rename the file already sitting in `inbox/`, then
re-run — delivery completes with no network calls and no quota cost. Do not
hand-move the tmp file into `inbox/`.

**Quarantined download.** `notebooklm/quarantine/` has the file and a
`.reason.txt`. Common cause: the download was not AAC/MP4 (an error page, a
truncated file). Do not hand-move it into `inbox/`. Investigate, and if the
artifact is fine in the web UI, download it manually and verify with
`ffprobe -show_streams <file>` before placing it.

**`state.json` corrupt (exit 5).** The pipeline refuses to run rather than
regenerate paid-for episodes. Do **not** delete it. Open it, and if it is
unrecoverable, run `notebooklm list` to see which notebooks already exist and
reconstruct the records before moving the file aside.

## Step 5 — Verify delivery

```bash
ls -la inbox/
```

For each episode you expected, confirm both `<Slug>.m4a` and `<Slug>.md` are
present. If the `.md` is missing, write it now (Step 2's format) directly into
`inbox/` — podpub falls back to a generic "Episode N of …" description
otherwise, and that ships to the live feed.

If a `.md` you wrote directly into `inbox/` already existed, the pipeline keeps
yours and logs a warning rather than overwriting it — check the log if you
expected the queued version to win.

Sanity-check the audio before publishing:

```bash
ffprobe -v error -show_entries format=duration,format_name -of default=nw=1 "inbox/<Slug>.m4a"
```

A Deep Dive should be roughly 15–45 minutes. The pipeline already quarantines
anything under `min_duration_sec` (default 300s), so a delivered file that still
looks too short is worth investigating before it reaches the feed.

### Batch ordering

podpub assigns episode numbers by audio-file **mtime**, oldest → lowest number.
The pipeline stamps each file at delivery time, so a batch is numbered in the
order the pipeline processed it (queue-folder mtime order). To force a different
order, `touch -t` the files in `inbox/` in the sequence you want **before**
running podpub.

## Step 6 — Publish with podpub

Follow the publishing workflow in the repo's `CLAUDE.md`. In short:

```bash
.venv/bin/python podpub.py --dry-run     # check the rename plan, feed XML, commit message
.venv/bin/python podpub.py               # transcribe, move, rebuild feed, commit, push
```

Confirm the pipeline is not running first. Transcription takes 1–2 minutes per
episode.

## Step 7 — Archive the PDFs, then archive the queue folder

Per `CLAUDE.md` step 4: rename each source PDF to
`NNN-YYYY-LastName-Short-Title.pdf` (NNN = the new episode number) and move it
from the queue folder into `PDFs/` at the repo root, then commit
(`Add episode NNN source papers`) and push. `PDFs/` is tracked; the user's
standing preference is a clean inbox, so do this without asking.

**Verify the PDFs are committed *and pushed* before touching the queue folder.**
`PDFs/` is the only permanent home for these papers, and a commit sitting
unpushed on a Drive-synced laptop is not a backup:

```bash
git status --short PDFs/          # must be empty - nothing staged or untracked
git log --oneline -1 -- PDFs/     # the commit you just made
git status -sb | head -1          # must NOT say "ahead" - the push landed
```

Only when all three look right, **move** the queue folder aside — never delete
it:

```bash
mkdir -p notebooklm/queue/.archived
mv notebooklm/queue/<name> notebooklm/queue/.archived/
```

`.archived/` starts with a dot, so the scanner ignores it and the episode will
not be picked up again. If any check above fails, or you are unsure publishing
succeeded, leave everything exactly where it is and say so.

## Step 8 — Write the run log, promote the lessons (mandatory)

Two files, two jobs. Do both.

**1. Append a dated entry to `automation/LOGS.md`** (newest at the top,
directly under its "How to append" section) describing this run: what you did,
what went wrong, the root cause, and any rule worth adopting. The exact entry
format is documented in that file. Do this even when the run was uneventful —
"three episodes, no surprises" is useful signal about what is stable.

**2. If you learned something that applies to every future run, promote it into
`automation/MEMORY.md`'s Standing rules** and say which rule you added in the
run-log entry. MEMORY.md is the curated, high-level file an agent reads before
every run; LOGS.md is the evidence trail. A lesson left only in the run log
will not be read in time to matter.
