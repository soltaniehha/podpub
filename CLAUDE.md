# podpub — instructions for Claude

This is a lightweight CLI (`podpub.py`) that publishes NotebookLM audio episodes to a private podcast RSS feed hosted on GitHub Pages. The feed URL is `https://soltaniehha.com/podpub/feed.xml` and is served via the `itunes:block=yes` directive (hidden from Apple's public directory, but subscribable by URL).

## Publishing workflow

When the user asks to publish, inspect `inbox/` and follow this procedure:

1. **Identify pairs.** For each audio file (`.m4a` / `.mp3` / `.wav`) not already prefixed with `NNN - `, check whether there is a companion file with the same basename:
   - `.md` sidecar present → use it as-is. Skip to step 3.
   - `.pdf` present but no `.md` → generate the `.md` from the PDF (step 2).
   - Neither → the script will auto-generate a generic `"Episode N of …"` description. Ask the user if that's acceptable before running, or if they want to provide a description.
   - **PDFs in a subfolder** named after the audio basename (`inbox/<basename>/*.pdf`) → a multi-paper episode; read them from there and write one `.md` next to the audio.

2. **Generate `.md` from PDF.** Read the PDF with the `Read` tool, then write a file at `inbox/<same basename as audio>.md` following the format in the next section. Do not proceed to publish until the `.md` exists.

3. **Preview, then publish.**
   - First run: `.venv/bin/python podpub.py --dry-run` — sanity-check the rename plan, feed XML, and commit message.
   - If everything looks right, run: `.venv/bin/python podpub.py` — this transcribes each new episode (takes 1–2 min per episode), moves files into `audio/` and `transcripts/`, rebuilds `feed.xml`, commits, and pushes to `origin/main`. GitHub Pages auto-deploys within ~30 seconds.
   - To publish without transcribing (e.g., transcription tooling is broken): add `--skip-transcripts`.

4. **Archive the PDF(s) after publish.** Once `podpub.py` succeeds, rename each source PDF to `NNN-YYYY-LastName-Short-Title.pdf` (NNN = the new episode number, 4–6 title words, e.g. `012-2023-Kortemeyer-Could-AI-Pass-Introductory-Physics.pdf`) and move it from `inbox/` into `PDFs/` at the repo root, removing any per-episode subfolder. `PDFs/` is tracked in git — commit the new PDFs (`Add episode NNN source papers`) and push. The user's standing preference is a clean inbox — do this without asking. If you're not sure publishing succeeded, leave the PDFs in `inbox/` and flag it. **Never delete the source PDFs** — an earlier delete-after-publish workflow lost the episode 008–011 papers and they had to be recovered from another project.

> **Episode order.** Numbers are assigned by audio-file **mtime** (oldest → lowest `NNN`), continuing from the feed's current max. To control the order of a multi-file batch, `touch -t` the audio files in the desired sequence before running. Run only one `podpub.py` at a time — it mutates `feed.xml` and git.

## Standardized episode description format

The `.md` sidecar becomes the `<description>` in the RSS feed. It is visible in Apple Podcasts and every other podcast app. Use this exact structure:

```
<Paper / book / article title> (<Month Year of publication>)

In this episode we unpack <1–2 sentences: authors, institution if notable, page count if notable, and the central argument or thesis>.

We walk through <specific technical contributions: models, frameworks, methods, benchmarks — cite them by their actual names>, <concrete case studies, datasets, or findings>, and close on <broader implications, open questions, or ethical tensions>.

Reference: <Full APA-style citation>. <DOI or arXiv URL>

Google Scholar citations: <number>
```

### Rules (non-negotiable)

- **Tone**: first-person plural ("we"), present tense. This matches NotebookLM's two-host deep-dive style.
- **Length**: exactly two paragraphs of prose + a Reference line + (optionally) a Google Scholar citations line. No headings, no bullets, no code blocks inside the description.
- **Opening paragraph**: name the authors (or lead author + "et al." for long lists), state where it was published if notable (arXiv, journal, conference), and give the core claim in plain language. Avoid jargon in the thesis sentence.
- **Middle paragraph**: name technical contributions by their real names — model names, benchmark names, framework acronyms. Do not substitute generic phrasing like "various models" where the paper actually names V-JEPA 2-AC, IntPhys 2, etc.
- **Reference line**: APA-ish — "LastName, F., Second, L., Third, L., et al. (Year). Title. Venue or arXiv:ID. URL". For arXiv papers, include both the ID and the full URL.
- **Google Scholar citations**: include this line only if the user explicitly provides the number or if it's easily retrievable. Never guess. If unknown, omit the line entirely.

### Multi-paper episodes

When the inbox contains more than one PDF paired with a single audio file (the audio's basename may not match either PDF — that's fine; use the audio's basename for the `.md` file), the episode is a *thematic pairing*. Keep the structure at two prose paragraphs, but:

- **Title line**: for two or three papers, combine the titles with `+` and each paper's month/year, e.g. `Paper One Title (2005) + Paper Two Title (2024)`. For four or more papers, do **not** concatenate every title — it becomes unreadable in Apple Podcasts. Instead, synthesize one short descriptive title that captures the thread, taking direct inspiration from the audio filename (it doesn't have to match the filename word-for-word, just align with it), and put the collective year range in parentheses, e.g. `Audio filename → episode title`: `Why_physical_robots_need_social_intelligence.m4a → Why Physical Robots Need Social Intelligence (2021–2025)`. The individual paper titles still get named in the opening paragraph and in the Reference lines, so nothing is lost.
- **Opening paragraph**: introduce both papers, their authors and affiliations, and the thread that connects them. State the pairing's argument, not each paper's in isolation.
- **Middle paragraph**: walk through each paper's named contributions in turn — do not blend them into abstractions. Close on the synthesis.
- **Reference block**: one `Reference:` line per paper, back-to-back. Podcast clients render as plain text with line breaks, so two references read cleanly in Apple Podcasts.

### Extracting from a PDF

The `Read` tool handles PDFs directly. For long PDFs (>10 pages), pass a `pages` parameter (e.g., `pages: "1-8"`) — the abstract, introduction, and contributions section are usually all that's needed. Pull:

- **Title** → opens the description (with Month/Year in parentheses).
- **Author list + affiliations** → compresses into the opening sentence.
- **Abstract** → primary source for the opening paragraph's thesis.
- **Introduction / Contributions section** → for the middle paragraph's named artifacts.
- **DOI / arXiv ID** → from the front matter for the Reference line. Common filename-to-DOI patterns you can verify against the paper's footer or URL: arXiv uses `YYMM.NNNNN` (e.g., `2410.00037.pdf` → `arXiv:2410.00037`, Oct 2024); Nature News & Views uses the `d41586-*` prefix (e.g., `d41586-021-01170-0.pdf` → `10.1038/d41586-021-01170-0`); Nature Machine Intelligence uses `s42256-*` (e.g., `s42256-025-01005-x.pdf` → `10.1038/s42256-025-01005-x`); MIT Press journals often use the article ID as the DOI suffix (e.g., `1064546053278973.pdf` → `10.1162/1064546053278973`).
- **Publication month / year** → from the front matter or the arXiv ID (e.g., `arXiv:2506.22355` → June 2025). If only the year is verifiable (older journal articles without a clear month), use just the year — never invent a month.

Reference example — a real one from the first episode:
```
Embodied AI Agents: Modeling the World (June 2025)

In this episode we unpack a 40 page position paper from Meta AI Research, led by Pascale Fung with Jitendra Malik and 19 co-authors. The argument: the next generation of AI agents will not live in chat windows. They will be embodied as virtual avatars, as wearables like Meta's AI Glasses, and as robots, and none of them will be useful without a proper world model.

We walk through the three agent types, the case for joint embedding predictive architectures like V-JEPA 2-AC and Vision-Language World Models over pure generative approaches, and the distinction between physical world models (perception, motion, planning) and mental world models (the Theory of Mind layer needed for real collaboration). We also cover the four new benchmarks the paper introduces (MVP, IntPhys 2, CausalVQA, WorldPrediction), and close on lifelong embodied learning, multi agent collaboration, and the ethical tensions around privacy and anthropomorphism.

Reference: Fung, P., Bachrach, Y., Celikyilmaz, A., et al. (2025). Embodied AI Agents: Modeling the World. arXiv:2506.22355. https://arxiv.org/abs/2506.22355

Google Scholar citations: 66
```

## Commands reference

- `.venv/bin/python podpub.py` — publish new inbox items (moves files, rebuilds feed, commits, pushes).
- `.venv/bin/python podpub.py --dry-run` — preview without writing or pushing.
- `.venv/bin/python podpub.py --no-push` — commit locally but skip `git push`.
- `.venv/bin/python podpub.py --rebuild-feed` — re-emit `feed.xml` from existing items without processing the inbox. Use after editing `config.yaml` (show title, description, cover URL) so the feed picks up channel-level changes.
- `.venv/bin/python podpub.py --backfill-transcripts` — generate VTTs for existing episodes that don't have one, inject transcript URLs into `feed.xml`, commit, and push.
- `.venv/bin/python podpub.py --skip-transcripts` — publish without generating transcripts for new episodes.
- `.venv/bin/python transcribe.py <audio_file> [--output <vtt>] [--force]` — standalone transcriber; useful for one-off re-transcription.

## Layout notes

- **Root (served by GitHub Pages)**: `feed.xml`, `audio/`, `NotebookLM-PodPub-Cover.png`. Don't move these — their URLs are baked into `feed.xml`.
- **`PDFs/`**: tracked archive of every episode's source papers, named `NNN-YYYY-LastName-Short-Title.pdf`. Populated as part of each publish (workflow step 4).
- **`setup/`**: `requirements.txt`, `config.yaml.example`. Setup-only, not touched day-to-day.
- **`inbox/`**: user's drop zone. Contents gitignored (including PDFs).
- **`config.yaml`** (gitignored, at root): paths + podcast metadata. Read by `podpub.py` on every run.

## Transcription setup (one-time, per machine)

Episode transcripts are generated locally by `transcribe.py` using WhisperX
(faster-whisper `large-v3`) and pyannote.audio for speaker diarization. Both
run fully offline once the model weights are downloaded. Speakers are labeled
**Daniel** (male voice) and **Maya** (female voice).

To enable transcription on a fresh machine:

1. **Create a HuggingFace account** (free): https://huggingface.co/join
2. **Accept the license** on each of these three models (click through once):
   - https://huggingface.co/pyannote/speaker-diarization-3.1
   - https://huggingface.co/pyannote/segmentation-3.0
   - https://huggingface.co/pyannote/speaker-diarization-community-1 (required by pyannote 4.x as a transitive dependency of `speaker-diarization-3.1`, even though we pin to 3.1 in `config.yaml`)
3. **Generate a read-scope access token** at
   https://huggingface.co/settings/tokens. Copy it.
4. **Paste the token** into `config.yaml` under `transcription.hf_token`
   (replace `hf_REPLACE_ME`). This file is gitignored — the token never leaves
   your machine.
5. **First transcription run** downloads ~3 GB of model weights to
   `~/.cache/huggingface/`. Subsequent runs are offline.

If the token is missing or the licenses are not accepted, `transcribe.py`
fails fast with a pointer to the URLs above.

**Transcripts in Apple Podcasts:** the feed's `<podcast:transcript>` VTTs (with `<v Daniel>` / `<v Maya>` speaker labels) render automatically in Podcasting 2.0 apps (Overcast, Pocket Casts, Podverse, etc.). **Apple Podcasts only shows transcripts for shows processed via Apple Podcasts Connect** — this private, `itunes:block=yes`, URL-added feed will not display them in Apple's own app (neither auto-generated nor provided).

## Fresh-machine gotchas (learned)

- **Broken venv after Drive sync.** `.venv` lives in a Google Drive–synced folder; on a new machine Drive turns `.venv/bin/python` symlinks into plain-text files and drops the exec bit (`permission denied` running `.venv/bin/python`). Rebuild: `rm -rf .venv && python3 -m venv .venv && .venv/bin/python -m pip install -r setup/requirements.txt`. Match whatever `python3` is — the pinned wheels work on 3.13.
- **SSL `CERTIFICATE_VERIFY_FAILED` on first transcription.** The alignment-model download goes through `torch.hub` → stdlib `urllib`, which has no CA bundle on a fresh python.org install (HuggingFace downloads still work via `certifi`). Fix once: run `/Applications/Python <ver>/Install Certificates.command`.
- **ffmpeg required** for `.m4a` decode: `brew install ffmpeg`. The `libtorchcodec_core*.dylib … no LC_RPATH` warnings are harmless (whisperx decodes via the ffmpeg subprocess).
- `large-v3` is already the best Whisper model (config default); weights cache under `~/.cache/huggingface/` on first run (~3 GB).
- **CPU thread scaling (benchmarked Jul 2026, 18-core Mac: 6P+12E).** The whisper stage's thread count is set via `threads=` in the `whisperx.load_model()` call in `transcribe.py` — pinned to **10**, the best single measurement (CTranslate2's default of 4 left most of the machine idle). On a 2-min clip of large-v3/int8: 4 threads → 87 s, 6 → 54 s, 8 → 51–62 s, 10 → 48 s, 12 → 56 s, 14 → 72 s, 18 → 88 s. The 6–12 range is a plateau (~1.6–1.8× faster than the old default; differences within run-to-run noise); above ~12, work spills onto efficiency cores and it gets *slower* — 18 threads is as slow as 4.

## One-time setup (only needed on a fresh clone or new machine)

If `config.yaml` does not exist, the project has not been initialized yet. Walk the user through, in order:

1. **GitHub repo + Pages**. Repo must exist on GitHub; in its Settings → Pages, Source = "Deploy from a branch", branch = `main`, folder = `/ (root)`. Feed URL will be `https://<user>.github.io/<repo>/feed.xml`. `git push` must work non-interactively (SSH key or credential helper).
2. **Cover art**. Square PNG/JPG at repo root (1400×1400 minimum). `NotebookLM-PodPub-Cover.png` ships in the repo; replacing it is fine but keep the filename (or update `cover_image_url` in `config.yaml`).
3. **Python deps**. `python3 -m venv .venv && source .venv/bin/activate && pip install -r setup/requirements.txt`.
4. **First run**. `.venv/bin/python podpub.py` — prompts for paths, base URL, and podcast metadata; writes `config.yaml`. See `setup/config.yaml.example` for the schema.
