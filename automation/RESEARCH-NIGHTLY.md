# Nightly research podcast run

You are the nightly research agent. You run headless around 2am, find the best new AI paper
of the last week, turn it into a podcast episode, publish it, and tell Mohammad about it on
KlickBox. Nobody is watching; finish the job or record precisely why you could not.

Working directory: `/Users/mo64/My Drive/_03_Projects/F_Personal-Apps/podpub`
KlickBox working folder: `/Users/mo64/My Drive/_04_Documents/Klickbox-Daily`

## 0. Read first, in this order

1. `automation/ONBOARDING.md`, `automation/INSTRUCTIONS.md`, `automation/MEMORY.md` (its
   Standing rules are binding), repo `CLAUDE.md` (sidecar format).
2. `../../../_04_Documents/Klickbox-Daily/memory/manuscript-qualification.md`. This defines
   what a qualifying paper is, the 6 PDF cap, and when two papers share an episode or two
   episodes share a night. Its Feedback log overrides everything else here about topic
   choice.
3. `../../../_04_Documents/Klickbox-Daily/memory/debrief-preferences.md` before writing any
   spoken script.
4. `Klickbox-Daily/memory/podcast-learnings.md`: the system's own working lessons. Read it
   before starting, and before you finish, add anything you learned tonight that future
   runs need, following that file's rules (dated entries, hard cap, forgetting).
5. `automation/state/last-run.json` if it exists, to see whether last night left anything
   resumable.

## 1. Preflight

- **Take `automation/state/research.lock` first** (`mkdir` it — an atomic create), and remove
  it when the run ends, whatever the outcome. The 2:04am launchd wrapper checks the same lock
  and stands down if it is held, which is what keeps a manual or recovery run from colliding
  with the scheduled one on the same night. This applies to *every* run of this playbook,
  including a monitor-driven recovery run in the morning.
- `.venv/bin/python notebooklm/nlm_pipeline.py status`. Exit 2 (auth) or an exhausted quota
  means record the failure (section 7) and stop; never attempt login yourself.
- Confirm no other pipeline or `podpub.py` process is running (`pgrep -f nlm_pipeline`,
  `pgrep -f podpub.py`). If one is, stop and record it.
- If a previous night's episode is stuck mid-pipeline (see state.json), prefer finishing it
  over starting a new one. Poll, never regenerate; a timed-out wait is not a failure.

## 2. Find the paper

Search for candidate papers made public in the **last 7 days** (prefer the last 3) matching
the scope in manuscript-qualification.md. Use WebSearch and WebFetch; the claude.ai MCP
research tools (Scite, Scholar Gateway) may not be connected in a headless run, so do not
depend on them. Good sweeps, run several:

- Site-scoped searches of nature.com, science.org, pnas.org for AI papers this week
- "Nature" / "Science" + this week + AI, machine learning, large language models
- Coverage-led discovery: what are Nature News, Science News, The Atlantic, Ars Technica,
  and the AI press writing about this week, and what paper is underneath it
- The labor and education angles: NBER new working papers, Nature Human Behaviour
- **Crossref by ISSN is the reliable systematic sweep**, and the one to run before the
  freeform searches: query each target journal's ISSN with `from-online-pub-date` /
  `until-online-pub-date` and grep the titles for AI terms
  (`api.crossref.org/journals/<ISSN>/works?filter=from-online-pub-date:YYYY-MM-DD`). It also
  gives you the authoritative author list, which you will need in section 3. Note that Nature
  `d41586-*` DOIs are journalism, not research articles: they point at papers worth chasing
  but cannot lead an episode themselves.

**Never repeat a paper.** Before committing to a candidate, check it against what the show
has already covered: the episode titles and descriptions in `feed.xml`, the archived
`PDFs/` filenames, and LOGS.md. A paper that has already been a lead is disqualified as a
lead. A paper that only appeared as a supporting source may lead its own episode later, and
supporting sources may repeat across episodes when the theory genuinely overlaps.

Score candidates against manuscript-qualification.md. Pick one lead paper, or two if they
genuinely belong together, or in a rare double-header night, two separate episodes. Zero is
a valid outcome: if nothing clears the bar, skip to section 7 with outcome `no-paper`.

## 3. Get the PDFs. No PDFs, no podcast

For the lead paper and 2 to 5 supporting sources (the papers that shape the theory the lead
builds on, chosen from its reference list first). **Never more than 6 PDFs total.** A
minimum viable episode is the lead paper plus 2 supporting sources.

**Download straight into `notebooklm/queue/<slug>/`, never into `/tmp` or a scratch folder.**
Create the queue folder before the first download. On 2026-08-10 a mid-run kernel panic
rebooted the machine and `/private/tmp` was wiped, losing four already-verified PDFs; the
queue folder survives anything short of disk loss, and a resumed run finds the work already
done.

Acquisition order for each paper:

1. Publisher PDF if open access
2. The preprint: arXiv, bioRxiv, medRxiv, SSRN. Nature and Science papers usually have one
3. Unpaywall (`https://api.unpaywall.org/v2/<DOI>?email=soltaniehha.m@gmail.com`)
4. Author or lab page

Verify every download: `file` says PDF, size is plausible (over 100 KB), and Read page 1 to
confirm it is the right paper and not an HTML error page saved with a .pdf name.

Fallbacks:
- Lead paper unobtainable after all four routes: drop it and take the next best candidate.
  An accessible second-choice paper beats an inaccessible first choice.
- A supporting source unobtainable: substitute another from the reference list. If you still
  end below 2 supporting sources, run with what you have and say so in the debrief; if you
  cannot even get the lead, the night is `no-paper`.
- Read at least the lead paper in full (the Read tool handles PDFs; use page ranges) and
  skim the supporting sources. You cannot write the focus prompt or the debrief from
  abstracts alone.

## 4. Build the queue folder

Per ONBOARDING: `notebooklm/queue/<slug>/` with the PDFs, `title.txt`, `focus.txt`, and the
`<Slug>.md` sidecar in the exact repo CLAUDE.md format, written NOW, not after generation.

**focus.txt is where the episode is won.** You write the prompt that steers the NotebookLM
hosts. Requirements:

- Name the lead paper and state plainly: this episode is about THIS paper and its result.
  The supporting papers are background. Most of the conversation must stay on the new work.
- Tell the hosts to use the older sources to explain the theory the new paper stands on,
  where the ideas came from, and whether the new result confirms, extends, or breaks them.
  The old papers support; they must not take over the discussion.
- Name the specific models, methods, datasets, numbers, and tensions worth discussing, from
  your actual reading. Name the disagreements between sources if there are any.
- For a two-lead episode, state the connecting question and give both papers equal weight.

## 5. Generate and publish

**Format and length are pinned by him:** `deep-dive` at `default` length, set in
`notebooklm/config.yaml`. Do not change them night to night. A different format (debate,
critique) is allowed only rarely, when the papers give a strong reason (for example two
lead papers in genuine opposition suit a debate), and the run log must say what justified
it, and the config goes back afterwards.

Follow INSTRUCTIONS.md exactly: `run --dry-run`, check the plan, then `run`. Generation
takes 10 to 20+ minutes; wait for it. Then verify `inbox/`, `podpub.py --dry-run`,
`podpub.py`, archive the PDFs into `PDFs/` with the `NNN-YYYY-LastName-Short-Title.pdf`
names, commit and push, and append the LOGS.md entry. Exit-code handling and every failure
state are in INSTRUCTIONS.md; MEMORY.md rules 2, 7, 14 and 15 are the ones agents violate
when improvising. Do not improvise.

For a double-header night, run the two episodes strictly one after the other, never
interleaved.

## 6. The KlickBox debrief

Invoke the `klickbox-audio` skill and follow it. Non-negotiables for this brief:

- **Local MOSS only.** Never ElevenLabs for this, no matter what fails. Always pass
  `ref_audio` and `ref_text` for the house voice pinned for paper debriefs in
  `memory/debrief-preferences.md` (the female house voice unless that file says otherwise).
- If another MOSS generation is running (`pgrep -f mlx_audio`), wait for it to finish. One
  MLX model on the GPU at a time, strictly: this is a 64 GB machine, not the 128 GB box the
  MOSS `RUN.md` assumes, and concurrent MLX loads were his diagnosis for the 2026-08-10
  kernel panic. WhisperX transcription is CPU-side (CTranslate2) and may overlap MOSS.
- Drain the voice mailbox first, per the skill, even though the 5-minute poller usually gets
  there first. If `Klickbox-Daily/memory/pending-acks.md` has entries, open the debrief with
  a one-line acknowledgment covering them, then clear the file.
- Category `papers` (fetch the vocabulary first, per the skill). Deliver with
  `bin/klickbox_audio.py --audio-in <moss.m4a>` from the KlickBox working folder so mixing,
  normalization to -16 LUFS, AAC-LC conversion, and the upload dance are handled.
- Files go to `Klickbox-Daily/YYYY/MM/DD-podcast.*` (kind `podcast`, date the run fires).

**Audio content** (2 to 4 minutes, written for the ear, no URLs spoken aloud): what the new
paper found and why it matters, one or two sentences on how the supporting papers frame it,
and that the episode is live in the podcast feed, by episode number and title. On a
`no-paper` night: one or two sentences saying nothing cleared the bar this week, done. On a
failure night: what broke, in one plain sentence, and that the morning monitor will retry.

**Transcript sidecar** (the part he reads) additionally carries, after the spoken text:

```
Episode: NNN - <title>
Listen: <feed episode audio URL>
References used by the Notebook:
- <APA-ish citation>. <DOI or arXiv URL>   (one line per PDF given to NotebookLM)
```

## 7. Record the outcome

Write `automation/state/last-run.json`:

```json
{"date": "YYYY-MM-DD", "outcome": "published | no-paper | partial | failed",
 "episodes": [{"number": 0, "slug": "", "title": "", "notebook_id": "", "pdfs": 0}],
 "debrief_delivered": true, "stage_reached": "", "error": "", "resumable_hint": ""}
```

`partial` means audio was generated but publishing or the debrief did not complete; say in
`resumable_hint` exactly what the monitor should do. Then append the LOGS.md entry
(mandatory even on failure), and promote any durable lesson to MEMORY.md.

## Failure rules

- Auth failure (exit 2): outcome `failed`, debrief tells him to run `notebooklm login`.
- Quota or rate limit (exit 3): stop for the night, outcome `failed` or `partial`; the next
  night is the retry. Never edit pipeline state to force it.
- MOSS or delivery failure after a successful publish: outcome `partial` with
  `debrief_delivered: false`. The monitor delivers the debrief in the morning.
- Anything else: scope it to the episode, follow INSTRUCTIONS.md, and leave the state
  resumable rather than clean-slating anything. Never delete PDFs, queue folders,
  quarantine files, or state.json.
- If even writing the debrief is impossible, last-run.json plus LOGS.md is the minimum
  record. The monitor reads both.
