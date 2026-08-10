# Automation memory — NotebookLM pipeline

The curated, high-level knowledge for running this pipeline. Everything here is
binding.

**This file is not a diary.** Per-run history lives in
[LOGS.md](LOGS.md) — append every run's entry there, and promote anything
durable up into the Standing rules below.

## Protocol for agents

1. **Read this file before every run.** The Standing rules override your
   instincts about how the pipeline "should" behave.
2. **After every run of `notebooklm/nlm_pipeline.py`, append a dated entry to
   [LOGS.md](LOGS.md)** — successful or not, using the entry format
   documented there. That step is mandatory.
3. **Promote durable lessons here.** If something will apply to *every* future
   run, add it as a Standing rule rather than leaving it buried in a dated
   entry. Say in the run-log entry which rule you added.
4. **Keep the rules curated.** Consolidation (folding old run-log entries into
   rules) is described in LOGS.md. Never delete a rule because it seems
   obvious — delete one only when it is demonstrably wrong, and say so in that
   run's entry.

## Standing rules

Seeded from the design guide, verified during implementation, and extended by
every run since. Sources are noted where it matters.

1. **`generate.timeout` ≥ 1800.** Long Deep Dives run 10–20 minutes. Timeouts
   under 1200s report a false failure while the job completes server-side.
2. **Poll before you ever regenerate.** A timed-out wait is not a failure. The
   pipeline keeps the `task_id` and polls on the next run. Deleting a record to
   "start clean" spends another quota unit on an episode that already exists.
   The pipeline also adopts an orphaned artifact (crash between quota spend and
   `task_id` save) rather than regenerating.
3. **Track quota locally; there is no API for it.** 3 audio generations/day on
   free, 20 on AI Pro. `daily_audio_cap` defaults to 3; 15 is the safe AI Pro
   setting, leaving headroom for manual web-UI generations on the same account.
   The counter is debited *before* the call, on purpose.
4. **Pin the `notebooklm-py` version.** Upgrade only deliberately, after a
   diagnosed failure, and re-verify the flags in `nlm_cli.py`'s CLI CONTRACT
   section. A silent upgrade is the most likely way this breaks.
5. **Use a dedicated Google account.** notebooklm-py drives NotebookLM's
   internal batchexecute RPC; automated access is against Google's ToS as
   written, and the account carries the risk. **In effect since 2026-08-10:**
   the pipeline runs under a dedicated account via the notebooklm profile
   `dedicated` (`profile: dedicated` in `notebooklm/config.yaml`; to see
   which account, run `notebooklm -p dedicated auth check` locally —
   don't write the address into this public repo;
   free tier, 3/day — matches `daily_audio_cap`). Never point `profile` back
   at `default` — that is the user's personal account, kept only for the
   episode-013-era login. Auth checks/refreshes for the pipeline must use
   `notebooklm -p dedicated …`.
6. **Auth failure is a human's problem.** One `auth refresh` keepalive is
   attempted automatically. If it fails, stop and ask the user to run
   `notebooklm login`. Never loop, never attempt browser login from an agent.
7. **Rate limited → back off.** Stop the run. Do not retry in the same session.
   The next scheduled run is the retry mechanism.
8. **One at a time.** Never two pipeline runs, never a pipeline run concurrent
   with `podpub.py`. The lockfile covers the first; you cover the second.
9. **Quarantine, never delete.** Bad downloads, suspect files, source PDFs —
   nothing gets deleted. Source PDFs go to `PDFs/` after publishing (episodes
   008–011's papers were lost once to a delete-after-publish workflow).
10. **Write the `.md` sidecar before generating.** It ships to the live RSS
    feed; without it podpub publishes a generic "Episode N of …" description.
    The format lives in the repo `CLAUDE.md`.
11. **Install the CLI on Python 3.13.** `uv tool install` defaults to the
    system Python; on 3.9 the tool crashes on import with
    `TypeError: unsupported operand type(s) for |`. Use
    `uv tool install --python 3.13 "notebooklm-py[browser]"`.
12. **Verified download shape: AAC in an MP4 container.** Confirmed against
    real NotebookLM output (`codec=aac`, `format_name=mov,mp4,m4a,3gp,3g2,mj2`).
    Anything else is quarantined.
13. **Watch the first real download's format.** notebooklm-py's download default
    extension is `.mp3` and it writes the RPC bytes untranscoded. If NotebookLM
    ever serves MP3, `verify_audio` quarantines it — the failure is safe, not
    silent, but the fix is a deliberate decision (accept MP3, or transcode),
    not a quick edit to the expected-codec constant. Log what you see on the
    first real run.
14. **Only auth, rate limits, and the daily cap may stop a whole run.** Every
    other failure is scoped to one episode. A wedged episode that halts the
    queue is a bug — it once did exactly that, on every subsequent run.
15. **A rate limit at generate time means nothing was generated.** The quota
    unit is refunded and the episode rewinds so the next run retries it. Do not
    "help" by editing state after a rate limit.
16. **Distinguish "the file is bad" from "the destination is taken".** Bad audio
    is quarantined; an occupied inbox filename leaves the verified download in
    `notebooklm/tmp/` and the record resumable. Never hand-move a tmp file into
    `inbox/`.
17. **Never delete a queue folder.** Archive it to `notebooklm/queue/.archived/`
    after confirming its PDFs are committed *and pushed* to `PDFs/`.
18. **The version pin is enforced, not advisory.** `run` refuses to start unless
    the installed CLI matches `VERIFIED_CLI_VERSION`. A renamed JSON field would
    otherwise read as "no task_id" and invite a regenerate — one wasted quota
    unit per episode.
19. **Sources must be `ready` before generating; a successful upload is not
    enough.** NotebookLM refuses audio generation while any source is still
    being indexed, answering `Error: Audio generation is unavailable`. Observed
    on the first real run: 3 PDFs (34MB) needed ~90s before
    `source list -n <ID> --json` reported every source `ready`. The pipeline now
    polls for readiness before generating (`generate.source_ready_timeout`,
    default 300s) — deliberately *before* the quota debit, so a timeout costs
    nothing. If you ever generate by hand, wait for `ready` first. Treat that
    refusal as "nothing was generated": refund, rewind, retry next run.

20. **Generation format is pinned: `deep-dive` at `default` length** (user
    directive, 2026-08-10). A different format (debate, critique) needs a strong
    paper-driven justification, a note in the run log, and the config restored
    afterwards. Never drift to `long` by habit; episode 014 was the last `long`.

## Run history

See [LOGS.md](LOGS.md) — append-only, newest first.
