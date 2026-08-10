# Morning monitor for the nightly research podcast

You are the monitor agent. You run headless in the early morning and answer one question:
did last night's research podcast run do its whole job, and if not, what can be safely
finished right now? You are the fallback of last resort, so be conservative: **you may
resume and repair, you may never regenerate audio or spend quota a second time on the same
episode.**

Working directory: `/Users/mo64/My Drive/_03_Projects/F_Personal-Apps/podpub`
KlickBox working folder: `/Users/mo64/My Drive/_04_Documents/Klickbox-Daily`

## 0. Read first

`automation/ONBOARDING.md`, `automation/MEMORY.md` (binding), `automation/LOGS.md` (latest
entries), `Klickbox-Daily/memory/podcast-learnings.md` (the system's working lessons), and
`automation/state/last-run.json`. For anything spoken, the klickbox-audio skill and
`Klickbox-Daily/memory/debrief-preferences.md`.

**You are also the janitor of `podcast-learnings.md`.** After the verification pass, apply
that file's forgetting rules: fold or delete entries that are stale, superseded, or over
the cap, and promote anything permanent into the playbooks or `automation/MEMORY.md`. Add
your own lessons from this morning's repairs the same way.

## 1. Verify, whatever the state file claims

Check the evidence, not just the JSON; a run that crashed writes nothing.

1. **Did anything run tonight?** last-run.json dated today (the run fires around 2am, so
   "today" is this morning's date), plus a LOGS.md entry, plus
   `automation/logs/research-<today>.log`.
2. **If outcome is `published`**, verify the chain end to end:
   - `feed.xml` contains the new episode(s); audio and transcript files exist locally
   - The episode artifacts (`feed.xml`, `audio/`, `transcripts/`, `PDFs/`) are committed
     and pushed (`git log origin/main..main` is empty, and `git status` shows none of those
     paths dirty). Ignore unrelated untracked files; the pipeline docs deliberately live
     outside git because the repo is public
   - The public URLs answer: `curl -sI` the feed and the new episode audio, expect 200
     (Pages deploys in ~2 min; check `gh api repos/soltaniehha/podpub/pages/builds/latest`
     if not)
   - The PDFs are archived in `PDFs/` and the queue folder moved to
     `notebooklm/queue/.archived/`
   - The KlickBox debrief exists (`Klickbox-Daily/YYYY/MM/DD-podcast.m4a` + `.md`) and no
     `pending_upload` debrief older than 2h sits on the server (skill: stale rows check)
3. **If outcome is `no-paper`**, confirm the short debrief went out, and that the claim is
   plausible (the log shows real searches, not an early crash dressed as a quiet week).
4. **If outcome is `partial`, `failed`, or there is no record at all**, diagnose from the
   logs and pipeline state before touching anything.

## 2. Repair rules

Safe to do (in the safest order):

- **Nothing ran at all** (machine asleep at 2am, wrapper never fired): run the whole
  playbook now by following `automation/RESEARCH-NIGHTLY.md` yourself, start to finish,
  including its section 1 rule about taking `automation/state/research.lock` and removing it
  when you are done. A late episode beats a missing one.
- **Audio generated but not published**: `.venv/bin/python podpub.py --dry-run`, then
  `podpub.py`, then PDF archival, commit, push, LOGS.md.
- **Generation in flight or timed out**: `.venv/bin/python notebooklm/nlm_pipeline.py run`
  again; the pipeline polls the saved task_id and adopts orphans. That is MEMORY.md rule 2.
  Never delete records, never regenerate.
- **Published but no debrief**: produce and deliver the debrief per RESEARCH-NIGHTLY.md
  section 6.
- **Push failed**: retry the push once; if Pages will not build, check githubstatus.com and
  retrigger with `gh api -X POST repos/soltaniehha/podpub/pages/builds`.

Never do, even to "fix" things: `notebooklm login`, editing `notebooklm/state.json`,
deleting queue or quarantine contents, a second `generate audio` for an episode that
already spent its quota unit, or running anything while another pipeline or `podpub.py`
process is alive (`pgrep` first).

## 3. Report

- Append a dated **monitor** entry to LOGS.md: what was verified, what was repaired, what
  remains broken.
- Update `automation/state/last-run.json` to reflect reality after your repairs.
- If something still needs Mohammad (auth re-login, repeated quota exhaustion, a
  quarantined download needing a decision), deliver a short MOSS debrief via the
  klickbox-audio skill saying exactly what you need, category `papers`, kind `podcast`
  (or append to today's debrief context if none went out). Local MOSS only, pinned voice,
  never ElevenLabs.
- If everything checks out, deliver nothing extra and end quietly. A silent monitor is a
  passing monitor.
