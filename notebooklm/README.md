# notebooklm — automated Deep Dive generation for podpub

Generates NotebookLM audio overviews from PDFs without touching the web UI, and
delivers verified `.m4a` files into podpub's `inbox/` where `podpub.py` takes
over (transcription → feed → push).

This package **shells out** to the external `notebooklm` CLI
([notebooklm-py](https://github.com/teng-lin/notebooklm-py), MIT). It never
imports that library and never stores credentials.

> **Pinned to notebooklm-py 0.8.0 (8fb61cb1), and enforced at runtime.** The
> wrapper was written against that version's `--help` output and source, not
> from documentation, and `run` refuses to start against any other version: a
> renamed JSON field reads as "no task_id", which looks like "generation never
> started" and invites a regenerate — one wasted quota unit per episode. See
> [Upgrading the CLI](#upgrading-the-cli).

> **Unofficial and fragile.** notebooklm-py drives NotebookLM's internal
> batchexecute RPC. Google can break it without notice, and automated access is
> against Google's ToS as written. Use a dedicated Google account, keep volume
> low, and expect to fall back to the web UI occasionally.

---

## Setup (one time per machine)

```bash
# 1. Install the CLI. Both parts matter: --python 3.13 (on 3.9 the tool crashes
#    on import) and the exact version (the pipeline refuses anything else).
uv tool install --python 3.13 "notebooklm-py[browser]==0.8.0"

# 2. Sign in once, in a browser (a human step - the pipeline never does this).
notebooklm login
#    For unattended/launchd runs there is also:
#    notebooklm login --master-token --account you@example.com
#    Understand what that stores before you use it: a Google master token is a
#    plaintext, non-expiring credential with access to the WHOLE account, not
#    just NotebookLM, and it sits on disk until you revoke it. Use it only with
#    a dedicated throwaway account that owns nothing else.

# 3. Confirm auth works.
notebooklm auth check --test --json

# 4. Configure the pipeline.
cp notebooklm/config.yaml.example notebooklm/config.yaml
$EDITOR notebooklm/config.yaml     # set notebooklm_bin / ffprobe_bin absolute paths

# 5. ffmpeg supplies ffprobe, used to verify every download.
brew install ffmpeg
```

Pin the CLI version. Upgrade only deliberately, after a failure you have
diagnosed — a silent upgrade is the most likely way this pipeline breaks.

## Commands

```bash
# Preview: prints the plan. No network calls, no state, no files, no lock.
.venv/bin/python notebooklm/nlm_pipeline.py run --dry-run

# Process the queue for real.
.venv/bin/python notebooklm/nlm_pipeline.py run

# One episode only (useful when the daily cap is tight).
.venv/bin/python notebooklm/nlm_pipeline.py run --limit 1

# Where things stand: CLI version, quota, lock, queue, in-flight episodes.
.venv/bin/python notebooklm/nlm_pipeline.py status
```

Exit codes:

| Code | Meaning | What to do |
|---|---|---|
| 0 | Everything queued was delivered, skipped, or is still generating | Publish with podpub, or re-run later for pending work |
| 1 | At least one episode failed or was quarantined | Read the log; check `quarantine/`. Other episodes may still have succeeded |
| 2 | Auth is dead and could not refresh | Run `notebooklm login` interactively |
| 3 | Stopped early: daily cap reached or rate limited | Wait for the next run; do not retry immediately |
| 4 | Another pipeline run holds the lock | Wait |
| 5 | Config, state, or tooling problem (including a CLI version mismatch) | Read the message; it names the fix |

Only auth failure, a rate limit, and the daily cap stop a whole run. Everything
else is scoped to one episode, so a single wedged item cannot block the queue.

## Queue layout

One folder per episode under `notebooklm/queue/`:

```
notebooklm/queue/why-robots-need-social-intelligence/
├── 2021-paper.pdf                                   # 1+ PDFs (required)
├── 2025-paper.pdf
├── title.txt                                        # optional: episode title
├── focus.txt                                        # optional: focus prompt
└── Why_Robots_Need_Social_Intelligence.md           # optional: podpub sidecar
```

Defaults when the optional files are missing: the title comes from the folder
name, and the focus prompt from the title. Both defaults produce a worse
episode than a hand-written one — see `automation/INSTRUCTIONS.md`.

The `.md` sidecar is the episode description podpub publishes to the RSS feed.
Write it **before** generating (its format is specified in the repo's
`CLAUDE.md`). Without it the pipeline still delivers the audio, logs a loud
warning, and records `sidecar: missing`.

Delivered filename = the slugified title, so
`title.txt: "Could AI Pass Introductory Physics"` becomes
`inbox/Could_AI_Pass_Introductory_Physics.m4a`, which podpub renders as episode
title *"Could AI Pass Introductory Physics"*.

---

## DESIGN

### Architecture

```
queue/<episode>/*.pdf
        │
        ▼
   episodes.py ──► QueuedEpisode(key = hash of PDF contents, title, focus, sidecar)
        │
        ▼
 nlm_pipeline.py  ── orchestration, quota, resume decisions
        │  │
        │  └──► state.py     state.json: per-episode record + daily quota ledger
        │  └──► locking.py   .lock: one run at a time
        │  └──► nlm_cli.py   subprocess ──► `notebooklm` ──► NotebookLM RPC
        │
        ▼
   delivery.py ──► verify.py (ffprobe) ──► inbox/<Slug>.m4a  +  inbox/<Slug>.md
                        └── on failure ──► quarantine/<ts>-<name> + .reason.txt
                                                │
                                                ▼
                                      podpub.py publishes
```

`nlm_cli.py` is the only module that spawns the external tool, so the whole
pipeline is testable with a fake (`tests/fakes.py`). Every CLI string lives in
that file's `CLI CONTRACT` section.

One naming caveat: this package is called `notebooklm`, the same as the
notebooklm-py library's import name. Running Python from the repo root makes
this directory win. That is harmless here — we shell out and never import the
library — but if you ever `pip install notebooklm-py` into `.venv`, import it
from somewhere other than the repo root.

### Data flow per episode

1. **Identify** — `episode_key` = SHA-256 over the sorted SHA-256 digests of the
   folder's PDFs. Content-addressed, so renaming the folder or a PDF is not a
   new episode. This is what makes a re-run free instead of expensive.
2. **Create** notebook (`notebooklm create`), record the id.
3. **Add sources** (`notebooklm source add`), one call per PDF, each recorded so
   a resumed run does not re-upload.
4. **Wait for ingestion** — poll `notebooklm source list --json` until every
   source reports `ready`. NotebookLM refuses to generate audio before that
   ("Audio generation is unavailable"), and a successful upload is not the same
   as a ready source. Bounded by `generate.source_ready_timeout` (default 300s)
   and run *before* the quota debit, so giving up here costs nothing.
5. **Generate** (`notebooklm generate audio --format deep-dive --length long
   --no-wait`) → returns a `task_id` immediately.
6. **Wait** (`notebooklm artifact wait --timeout 1800`), preceded by a one-shot
   `artifact poll`.
7. **Download** to `tmp/`, **verify** with ffprobe, **`os.replace`** into
   `inbox/`. Same volume, so the move is atomic — podpub never sees a partial file.

### Why `--no-wait` instead of `--wait`

The guide for this project suggested `generate audio --wait`. We split it into
`generate --no-wait` + `artifact wait` on purpose: the `task_id` must be durable
in `state.json` *before* any long block. If the process dies during a 20-minute
wait (laptop sleeps, launchd kills it, Drive hiccups), the next run polls that
task instead of paying a second quota unit for an episode that already exists.

### Failure handling

| Failure | Behavior |
|---|---|
| Wait times out | **Not** an error. Record stays `generating` with its `task_id`; next run polls. Never regenerates. |
| Crash between "quota spent" and "task_id saved" | Next run sees `generating` with no `task_id`, lists the notebook's audio artifacts and **adopts** one. If there is genuinely none, that one episode is marked `failed` with instructions — the rest of the queue continues. |
| Rate limited *before* generation started | The server refused, so nothing was generated: the quota unit is refunded, the episode rewinds to `sources_added`, and the run stops. The next run retries it normally. |
| Sources still ingesting | Generation waits for every source to report `ready` first. If they never do, the episode fails with no quota spent and the queue continues. |
| "Audio generation is unavailable" | NotebookLM's synchronous refusal while sources ingest. Treated as did-not-start: quota refunded, episode rewound, **run continues** — unlike a rate limit, this says nothing about episodes in other notebooks. |
| A source fails to ingest (`status: error`) | That episode fails with the offending source named. Re-uploading needs a fresh notebook, so its record must be cleared once the PDF is fixed. |
| `generate audio` fails ambiguously (local timeout, unparseable response, network drop) | The request may have reached NotebookLM, so the debit and the `generating` state are kept and the next run adopts the artifact rather than paying twice. |
| Generation fails server-side | Recorded as `failed` with the error and reported as a failure (exit 1); skipped on later runs until a human clears it. No auto-retry. |
| Auth expired | One `auth refresh` keepalive is attempted; if that fails, exit 2 with "MANUAL RE-LOGIN NEEDED" instead of looping. A *missing binary* is never mistaken for this — it exits 5 with the install command. |
| Download not AAC/MP4, empty, zero-length, or shorter than `min_duration_sec` | Quarantined with a `.reason.txt`. Never delivered, never deleted. |
| Download written somewhere other than the path we asked for | Rejected. The file is never touched, because the next step would move it into podpub's inbox. |
| Inbox already has that filename | Refuses to overwrite. The verified download stays in `tmp/`, the record stays `generated`, and re-running after clearing the inbox delivers it with no further network calls. |
| Inbox already has that `.md` sidecar | Kept. A hand-written description outranks the queued one, and nothing is ever overwritten. |
| Corrupt `state.json` | Hard error. A blank state would regenerate paid-for episodes, so the pipeline refuses to start. |
| Two runs at once | `O_CREAT\|O_EXCL` lockfile. A live holder is always respected; a dead PID *on this host* is reclaimed with a warning; an unreadable lockfile or one from another machine is treated as held. |
| Two queue folders with identical PDFs | They are one episode by content hash. The first is processed, the rest are skipped with a warning — otherwise one generation would be published twice. |

### Quota

NotebookLM allows 3 audio generations/day (free) or 20 (AI Pro), and exposes no
API to query the remaining count — so the ledger in `state.json` is local,
date-stamped, and rolls over at local midnight. `daily_audio_cap` defaults to 3;
use 15 on AI Pro to leave headroom for manual generations on the same account.
A unit is debited *before* the generate call, because an untracked generation is
worse than an over-counted one.

### Things this deliberately does not do

- **Never deletes anything.** Bad downloads are quarantined; queue folders are
  left in place after delivery (state marks them delivered and skips them).
- **Never logs credentials.** All subprocess output passes through `scrub.py`.
- **Never runs `notebooklm login`.** Sign-in is a human step, always.
- **Never touches `podpub.py`, `feed.xml`, or git.** The handoff is one file
  landing in `inbox/`.

### Configuration paths

Relative paths in `config.yaml` — and the defaults for any key you leave out —
resolve against **the directory containing the config file**, not the package.
That is what keeps a scratch config elsewhere from sharing production's
`state.json` and lockfile. Keep the pipeline's working paths inside
`notebooklm/` regardless: the repo's `.gitignore` rules are path-literal.

### Google Drive notes

This repo lives in a Drive-synced folder. The pipeline uses no symlinks and
assumes no exec bits; `tmp/`, `quarantine/`, and `inbox/` are all on the same
volume, so `os.replace` is atomic. Note that `.venv/bin/python` symlinks are
routinely destroyed by Drive sync on a new machine — see "Fresh-machine
gotchas" in the repo `CLAUDE.md` for the rebuild.

## Tests

```bash
.venv/bin/python -m unittest discover notebooklm/tests
```

170 tests, no network and no `notebooklm` binary required — the CLI wrapper is
faked and `subprocess.run` is patched. They cover episode-key stability, resume
(timeout → poll, never regenerate), orphan adoption, rate-limit refund, quota
rollover and cap, ffprobe pass/fail routing, sidecar handling, lockfile mutual
exclusion under real concurrent processes, redaction of every credential shape
notebooklm-py stores, and that `--dry-run` spawns no process and writes no file.

`notebooklm/tests/test_regressions.py` is a separate class of test: each case
came from a defect found in review, and its docstring records the original
diagnosis. Two suites need optional tooling and skip cleanly without it —
`test_config.py` needs PyYAML, `test_verify_ffprobe.py` needs ffmpeg/ffprobe.

## Upgrading the CLI

`run` refuses to start unless the installed version matches
`VERIFIED_CLI_VERSION` in `nlm_cli.py`, so upgrading is a deliberate, four-file
act rather than something that happens to you:

1. Read the [repo's recent issues](https://github.com/teng-lin/notebooklm-py/issues)
   — a Google-side RPC change usually shows up there first.
2. `uv tool install --python 3.13 "notebooklm-py[browser]==<new>"`
3. Diff the flags you depend on: `notebooklm create --help`,
   `source add`, `generate audio`, `artifact poll`, `artifact wait`,
   `download audio`, `auth check`, `auth refresh`. Check the JSON field names
   too (`task_id`, `notebook.id`, `output_path`) — those are what the wrapper
   parses, and a rename is silent.
4. Update the `CLI CONTRACT` section and `VERIFIED_CLI_VERSION` in
   `nlm_cli.py`, the pin in `config.yaml.example`, and the note at the top of
   this README.
5. Run the tests, then a `--dry-run`, then one real episode with `--limit 1`.
6. Log what changed in `automation/LOGS.md`, and update the pin rule in
   `automation/MEMORY.md`.

If the tool breaks entirely: generate in the NotebookLM web UI and drop the
`.m4a` straight into `inbox/` — podpub's original manual workflow still works.
A secondary community tool, `jacob-bd/gemini-notebook-mcp-cli`, is the backup
option.
