# Run log — NotebookLM pipeline

Append-only history of what actually happened, newest first. This is the
evidence; the curated conclusions live in [MEMORY.md](MEMORY.md).

Not to be confused with `notebooklm/logs/pipeline.log`, which is the machine's
own timestamped output for one run. This file is the human/agent account of
what a run meant: what was attempted, what broke, and what changed as a result.

## How to append

**Add an entry after EVERY run of `notebooklm/nlm_pipeline.py`, successful or
not.** A boring run is data too — it tells the next agent which parts are
stable. New entries go directly below this section, above the previous newest.

```markdown
### YYYY-MM-DD — <one-line summary>

- **What happened:** episodes attempted, exit code, what reached `inbox/`.
- **Root cause:** for anything that went wrong. "Unknown" is an acceptable and
  useful answer; a guess presented as fact is not.
- **Rule adopted:** the behavior change, or "none".
```

Two rules that keep this file useful:

- **Promote, don't bury.** Anything that will apply to *every* future run
  belongs in MEMORY.md's Standing rules. Leaving it only here means the next
  agent has to read the whole history to find it.
- **Consolidate past ~10 entries.** Fold the durable lessons from the oldest
  entries into MEMORY.md's Standing rules, then shrink those entries to a single
  summary line each. Keep the most recent ~10 detailed.

Record the `notebooklm-py` version whenever it changes, and whenever a run fails
in a new way.

---

### 2026-08-10 (monitor, 06:36) — Nothing to repair; episode 014 verified live end to end

- **What happened:** First run of the morning monitor. No repairs were needed and no audio
  was produced. Verified against evidence rather than the state file: `last-run.json` dated
  today with outcome `published`; `feed.xml` lists **014 - The Internet Paradox Comes For AI
  Companions**; the local `.m4a` (86 MB) and WhisperX `.vtt` (65 KB) exist; `git status` is
  clean for `feed.xml`, `audio/`, `transcripts/`, `PDFs/` and `git log origin/main..main` is
  empty; the public feed, episode audio, transcript, and an archived PDF all return HTTP 200
  and the live feed contains episode 014; five `PDFs/014-*` papers are archived and the
  `ai-companions-wellbeing` queue folder is in `queue/.archived/`; the KlickBox debrief pair
  `2026/08/10-podcast.{m4a,md}` exists (AAC/M4A, 118s) and `--check-stale` reports no
  `pending_upload` rows. Pipeline health for tonight is good: CLI 0.8.0 pinned, lock free,
  quota 1/3, `tmp/` and `quarantine/` both empty, `research.lock` released, and the
  `podcast-research` / `reply-poll` launchd jobs are loaded with last exit 0. Two things
  worth recording that nobody logged last night: the first Pages build for episode 014
  **errored** (06:56Z) and the retry two minutes later built successfully, and the reply
  mailbox intake files (`pending-acks.md`, `pending-requests.md`) are empty. Did not drain
  the voice mailbox: the dedicated 5-minute `reply-poll` job owns that, and a monitor drain
  would race it.
- **Root cause:** n/a, nothing failed. The near-empty `automation/logs/research-20260810.log`
  ("another research run holds the lock, exiting") is the 2:04am wrapper correctly standing
  down while last night's interactive run held the lock, not a missed night.
- **Rule adopted:** none in MEMORY.md; the pipeline itself needed no new rule. As janitor of
  `Klickbox-Daily/memory/podcast-learnings.md`, promoted four entries out of the hot cache
  into the playbooks and deleted them there: PDFs download straight into the queue folder
  (RESEARCH-NIGHTLY §3), the Crossref-by-ISSN weekly sweep and the `d41586-*`-is-journalism
  caveat (§2), taking and releasing `automation/state/research.lock` on every run including
  a monitor recovery run (§1, plus MONITOR §2, which previously told the monitor to rerun the
  playbook without mentioning the lock), and one MLX model on the GPU at a time with the
  64 GB / kernel-panic rationale (§6). Added two monitor lessons in their place: a near-empty
  research log is not a failed night, and a publish is verified by curl rather than by the
  Pages build list, which can error once before succeeding and can lag the newest commit.

### 2026-08-10 — Episode 014 published: first run of the automated research pipeline (interactive test)

- **What happened:** Interactive test of the new nightly research flow (RESEARCH-NIGHTLY.md),
  run by hand ahead of the 2:04am launchd job. Paper sweep via Crossref found the Aug 4
  Nature Human Behaviour AI-companions study (Zhang, Zhao, Hancock, Kraut, Yang); NHB PDF is
  paywalled but the full preprint exists as arXiv:2506.12605 under a different title, found
  by searching arXiv on the Crossref author list. Queued it with 4 supporting sources
  (Kraut 1998, De Freitas JCR 2025, Phang arXiv 2025, Guingrich arXiv 2023). Pipeline run
  exit 0, one quota unit, 44.5 min deep-dive/long delivered. podpub exit 0, episode 014
  pushed; PDFs archived and pushed; queue folder archived. KlickBox debrief delivered via
  MOSS (female voice). Mid-run the machine kernel-panicked (unrelated: WindowServer
  watchdog under system-wide memory exhaustion; see memory/podcast-learnings.md) and the
  reboot wiped /tmp including four already-downloaded PDFs; re-downloaded into the queue
  folder. The 2:04am launchd wrapper fired during the test and correctly stood down on
  automation/state/research.lock.
- **Root cause:** n/a for the pipeline; the panic predated any heavy tool in this system.
- **Rule adopted:** MEMORY.md rule 20 (format pinned to deep-dive/default from episode 015
  on, per user). Working lessons seeded into Klickbox-Daily/memory/podcast-learnings.md,
  which every agent in this system now reads and maintains.

### 2026-08-09 — Episode 013 delivered and published end-to-end (second run of the day)

- **What happened:** Retry of the first real run after the source-ingestion
  refusal (see entry below). Cleared the wedged record per INSTRUCTIONS
  ("Episode marked failed" remediation: status → `sources_added`), re-ran; the
  pipeline reused notebook `97d181b1`, skipped the already-uploaded sources,
  generated (task `4acb3a03`, attempt 2, quota 2/3), waited ~10 min, and
  delivered a verified 43.2-min AAC/M4A + sidecar to `inbox/`. Exit 0. podpub
  then published it as **013 - When AI Makes Discoveries** (commit `b1e1ec5`,
  WhisperX transcript included), PDFs archived to `PDFs/013-*` (commit
  `0968263`), queue folder moved to `queue/.archived/`. Local quota ledger says
  2/3 used; the server-side count is likely 1 (the first attempt never started
  generation), so the ledger is conservative by one — correct by design.
- **Root cause:** n/a for this run; the failure it retried is the entry below.
- **Rule adopted:** none new. Confirmed working in production: resume-without-
  regenerate from a cleared record, notebook/source reuse, ffprobe gate, atomic
  inbox delivery with mtime stamping, sidecar handoff, and the full podpub
  publish chain. NotebookLM's real download was AAC/M4A (not the .mp3 fallback
  rule 18 warns about) at ~83 MB for 43 min. Generation wall-clock for a
  3-paper long Deep Dive: ~10 minutes.

### 2026-08-09 — First real run: sources must finish ingesting before generating

- **What happened:** The first genuine generation run. `generate audio` fired
  immediately after uploading 3 PDFs (34MB) and NotebookLM refused it
  synchronously: `[NOTEBOOKLM_ERROR] Error: Audio generation is unavailable`.
  About 90 seconds later `notebooklm source list -n <id> --json` reported every
  source as `"status": "ready"`, and the retry generated normally. The existing
  ambiguous-error handling behaved correctly (kept the quota debit, left the
  episode for adoption), so nothing was lost — but this race will hit most
  future episodes, since every episode uploads PDFs and then generates.
- **Root cause:** NotebookLM will not start an audio overview while any source
  is still being indexed. The pipeline had no readiness gate: it treated a
  successful `source add` as "ready to generate", when `source add` only means
  "upload accepted". Source status lives behind a separate call the pipeline
  was not making.
- **Rule adopted:** Standing rule 19. The pipeline now polls
  `source list -n <ID> --json` until every source reports `ready` before
  generating, bounded by `generate.source_ready_timeout` (default 300s) and
  placed *before* the quota debit, so giving up costs nothing. The specific
  "Audio generation is unavailable" refusal is additionally treated as
  generation-did-not-start: quota refunded, episode rewound to `sources_added`,
  and the run continues to the next episode (unlike a rate limit, this says
  nothing about episodes in other notebooks).

### 2026-08-09 — Review + fix cycle: 7 blocking, 8 major defects closed

- **What happened:** A tester and a security reviewer went over the initial
  build; no NotebookLM calls were made in this cycle (no quota spent). Fifteen
  real defects were confirmed and fixed. The ones worth remembering: a rate
  limit at generate time wedged the episode *and* then halted the entire queue
  on every subsequent run; the inbox-collision error told the user their
  download was waiting in `tmp/` while the code quarantined it; a missing
  `notebooklm` binary was reported as "MANUAL RE-LOGIN NEEDED"; the lockfile was
  non-atomic (two processes shared one temp path, and the loser crashed —
  80/80 race trials); a server-side generation failure exited 0 as "pending";
  two queue folders with identical PDFs published one generation twice; and a
  scratch config that omitted `state_file` silently used production's state and
  lock. Test count went 114 → 170, all green, no expectedFailures.
- **Root cause:** Two recurring themes. (1) Ordering assumptions that only hold
  on the happy path — quota debited before the call that might refuse it, state
  written before the atomicity that protects it. (2) Docs and code drifting into
  disagreement about the *same* failure, which is worse than either being wrong
  alone: an agent following the message took an action the code had made
  impossible.
- **Rule adopted:** Standing rules 13–18. Also a working principle for future
  fixes: when an error message tells the user where something is or what to do
  next, that message is part of the contract — a test should hold the code to
  it. Several of the regression tests do exactly that by parsing the remediation
  text out of `last_error`.

### 2026-08-09 — Pipeline built; no episodes generated

- **What happened:** Implemented `notebooklm/` and `automation/`. No
  authenticated or network call was made — no notebook created, no audio
  generated, no quota consumed. Verified the CLI surface against a real install
  of **notebooklm-py 0.8.0 (8fb61cb1)** via `--help` and its source, and
  confirmed `verify.py` accepts a real published episode
  (`audio/001 - Why AI Has A Body Problem.m4a`: aac / mp4 / 2203s).
- **Root cause:** n/a (initial build).
- **Rule adopted:** Standing rules 11 and 12. Also: the shipped wrapper uses
  `generate audio --no-wait` followed by `artifact wait`, rather than
  `generate audio --wait` as the original design guide suggested — the
  `task_id` has to be durable in `state.json` before any long block, or a killed
  run cannot resume without re-billing.
