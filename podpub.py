#!/usr/bin/env python3
"""podpub - publish local audio files to a GitHub Pages-hosted podcast RSS feed.

Usage:
  python podpub.py                # scan inbox, publish new episodes, push
  python podpub.py --dry-run      # preview without moving files, writing feed, or pushing
  python podpub.py --no-push      # commit locally but skip git push
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import shutil
import subprocess
import sys
from email.utils import formatdate
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree as ET

import yaml
from feedgen.feed import FeedGenerator


SUPPORTED_EXTS = {
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
}
PROCESSED_RE = re.compile(r"^\d{3} - ")
EP_TITLE_PREFIX_RE = re.compile(r"^\d{3}\s*-\s*")
CONFIG_FILE = "config.yaml"
LOG_FILE = "podpub.log"
SCRIPT_DIR = Path(__file__).resolve().parent
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
PODCAST_NS = "https://podcastindex.org/namespace/1.0"


# ---------- config ----------

def prompt_config() -> dict:
    print("No config.yaml found. Let's set it up.\n")

    def ask(label: str, default: str | None = None) -> str:
        suffix = f" [{default}]" if default else ""
        while True:
            val = input(f"{label}{suffix}: ").strip()
            if val:
                return val
            if default is not None:
                return default
            print("  (required)")

    cfg: dict = {
        "inbox_dir": ask("Absolute path to inbox folder", str(SCRIPT_DIR / "inbox")),
        "repo_dir": ask("Absolute path to cloned GitHub repo", str(SCRIPT_DIR)),
        "audio_subdir": ask("Audio subfolder inside repo", "audio"),
        "feed_path": ask("Path to feed.xml inside repo", "feed.xml"),
        "base_url": ask("Public URL of repo (e.g., https://user.github.io/podpub)"),
    }
    cfg["podcast"] = {
        "title": ask("Podcast title"),
        "description": ask("Podcast description"),
        "author_name": ask("Author name"),
        "author_email": ask("Author email"),
        "language": ask("Language", "en-us"),
        "category": ask("iTunes category", "Education"),
    }
    default_cover = f"{cfg['base_url'].rstrip('/')}/NotebookLM-PodPub-Cover.png"
    cfg["podcast"]["cover_image_url"] = ask("Cover image URL", default_cover)
    return cfg


def load_config() -> dict:
    cfg_path = SCRIPT_DIR / CONFIG_FILE
    if not cfg_path.exists():
        cfg = prompt_config()
        cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
        print(f"\nWrote {cfg_path}\n")
        return cfg
    return yaml.safe_load(cfg_path.read_text())


# ---------- logging ----------

def setup_logging() -> logging.Logger:
    log_path = SCRIPT_DIR / LOG_FILE
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("podpub")


# ---------- title & id helpers ----------

def clean_title(stem: str) -> str:
    """underscore/hyphen -> space, smart title-case, preserve short ALL-CAPS tokens (AI, LLM, RSS)."""
    s = re.sub(r"[_\-]+", " ", stem)
    s = re.sub(r"\s+", " ", s).strip()

    def cap(w: str) -> str:
        if w.isupper() and 2 <= len(w) <= 5 and w.isalpha():
            return w
        return w[:1].upper() + w[1:] if w else w

    return " ".join(cap(w) for w in s.split())


def make_guid(filename: str) -> str:
    return hashlib.sha1(filename.encode("utf-8")).hexdigest()


def _strip_title_prefix(title: str) -> str:
    return EP_TITLE_PREFIX_RE.sub("", title).strip()


def _format_title(ep_num: int, clean: str) -> str:
    return f"{ep_num:03d} - {clean}"


def read_sidecar(audio_path: Path) -> str | None:
    for ext in (".md", ".txt"):
        sc = audio_path.with_suffix(ext)
        if sc.exists():
            text = sc.read_text(encoding="utf-8").strip()
            if text:
                return text
    return None


# ---------- inbox scan ----------

def scan_inbox(inbox_dir: Path) -> list[Path]:
    files: list[Path] = []
    for p in sorted(inbox_dir.iterdir(), key=lambda x: x.stat().st_mtime):
        if not p.is_file():
            continue
        if p.suffix.lower() not in SUPPORTED_EXTS:
            continue
        if PROCESSED_RE.match(p.name):
            continue
        files.append(p)
    return files


# ---------- feed parsing ----------

def parse_existing_feed(feed_path: Path) -> tuple[int, list[dict]]:
    if not feed_path.exists():
        return 0, []
    try:
        tree = ET.parse(feed_path)
    except ET.ParseError:
        return 0, []
    channel = tree.getroot().find("channel")
    if channel is None:
        return 0, []
    ns = {"itunes": ITUNES_NS, "podcast": PODCAST_NS}
    items: list[dict] = []
    max_ep = 0
    for el in channel.findall("item"):
        enc = el.find("enclosure")
        ep_text = el.findtext("itunes:episode", default="", namespaces=ns)
        ep_num = int(ep_text) if ep_text.isdigit() else 0
        max_ep = max(max_ep, ep_num)
        transcript_el = el.find("podcast:transcript", namespaces=ns)
        transcript_url = transcript_el.get("url") if transcript_el is not None else ""
        items.append({
            "title": _strip_title_prefix(el.findtext("title", "")),
            "guid": el.findtext("guid", ""),
            "pub_date": el.findtext("pubDate", ""),
            "description": el.findtext("description", ""),
            "enclosure_url": enc.get("url", "") if enc is not None else "",
            "enclosure_length": enc.get("length", "0") if enc is not None else "0",
            "enclosure_type": enc.get("type", "audio/mp4") if enc is not None else "audio/mp4",
            "episode": ep_num,
            "transcript_url": transcript_url,
        })
    return max_ep, items


# ---------- feed building ----------

def build_feed(config: dict, items: list[dict]) -> bytes:
    fg = FeedGenerator()
    fg.load_extension("podcast")

    pod = config["podcast"]
    base_url = config["base_url"].rstrip("/")

    fg.title(pod["title"])
    fg.link(href=base_url, rel="alternate")
    fg.description(pod["description"])
    fg.language(pod.get("language", "en-us"))
    fg.author({"name": pod["author_name"], "email": pod["author_email"]})

    fg.podcast.itunes_author(pod["author_name"])
    fg.podcast.itunes_owner(name=pod["author_name"], email=pod["author_email"])
    fg.podcast.itunes_category(pod.get("category", "Education"))
    fg.podcast.itunes_explicit("no")
    fg.podcast.itunes_image(pod["cover_image_url"])
    fg.podcast.itunes_block("Yes")

    sorted_items = sorted(items, key=lambda i: i["episode"], reverse=True)
    for it in sorted_items:
        fe = fg.add_entry()
        fe.title(_format_title(it["episode"], it["title"]))
        fe.guid(it["guid"], permalink=False)
        fe.pubDate(it["pub_date"])
        fe.description(it["description"])
        fe.enclosure(it["enclosure_url"], str(it["enclosure_length"]), it["enclosure_type"])
        fe.podcast.itunes_episode(it["episode"])

    feed_bytes = fg.rss_str(pretty=True)
    return _inject_podcast_transcripts(feed_bytes, items)


def _inject_podcast_transcripts(feed_bytes: bytes, items: list[dict]) -> bytes:
    """Given feedgen-generated RSS bytes and the list of items, inject the
    Podcasting 2.0 namespace and a <podcast:transcript> child on each item
    that has a non-empty transcript_url. Items are matched by guid.
    """
    # Re-register namespaces feedgen emits so ET preserves their prefixes
    # (otherwise itunes: gets rewritten as ns0: which Apple Podcasts ignores).
    ET.register_namespace("itunes", ITUNES_NS)
    ET.register_namespace("atom", "http://www.w3.org/2005/Atom")
    ET.register_namespace("content", "http://purl.org/rss/1.0/modules/content/")
    ET.register_namespace("podcast", PODCAST_NS)
    root = ET.fromstring(feed_bytes)

    guid_to_url = {it["guid"]: it["transcript_url"] for it in items if it.get("transcript_url")}
    if not guid_to_url:
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    channel = root.find("channel")
    for item in channel.findall("item"):
        guid = item.findtext("guid", "")
        url = guid_to_url.get(guid)
        if not url:
            continue
        trans = ET.SubElement(item, f"{{{PODCAST_NS}}}transcript")
        trans.set("url", url)
        trans.set("type", "text/vtt")
        trans.set("language", "en")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


# ---------- git ----------

def git_run(repo_dir: Path, *args: str, check: bool = True, log: logging.Logger | None = None) -> subprocess.CompletedProcess:
    cmd = ["git", "-C", str(repo_dir), *args]
    if log:
        log.info("git %s", " ".join(args))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if log and result.stdout.strip():
        log.info(result.stdout.strip())
    if log and result.stderr.strip():
        log.info(result.stderr.strip())
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


# ---------- orchestration ----------

def main() -> int:
    ap = argparse.ArgumentParser(description="Publish audio files to a GitHub Pages-hosted podcast feed.")
    ap.add_argument("--dry-run", action="store_true", help="Preview without moving files, writing feed, or pushing")
    ap.add_argument("--no-push", action="store_true", help="Commit locally but skip git push")
    ap.add_argument("--rebuild-feed", action="store_true", help="Re-emit feed.xml from existing items (no inbox processing); useful after editing config.yaml")
    ap.add_argument("--backfill-transcripts", action="store_true",
                    help="Generate VTTs for existing episodes missing them, add to feed, commit, push.")
    ap.add_argument("--skip-transcripts", action="store_true",
                    help="Publish without generating transcripts for new episodes.")
    args = ap.parse_args()

    log = setup_logging()
    log.info("podpub starting (dry_run=%s, no_push=%s)", args.dry_run, args.no_push)

    cfg = load_config()
    inbox_dir = Path(cfg["inbox_dir"]).expanduser()
    repo_dir = Path(cfg["repo_dir"]).expanduser()
    audio_dir = repo_dir / cfg["audio_subdir"]
    feed_path = repo_dir / cfg["feed_path"]
    base_url = cfg["base_url"].rstrip("/")
    audio_subdir = cfg["audio_subdir"]

    if not repo_dir.is_dir():
        log.error("repo_dir does not exist: %s", repo_dir)
        return 1
    audio_dir.mkdir(parents=True, exist_ok=True)

    if args.rebuild_feed:
        return _rebuild_feed(cfg, repo_dir, feed_path, args, log)

    if args.backfill_transcripts:
        return _backfill_transcripts(cfg, repo_dir, audio_dir, feed_path, base_url,
                                     audio_subdir, args, log)

    if not inbox_dir.is_dir():
        log.error("inbox_dir does not exist: %s", inbox_dir)
        return 1

    new_files = scan_inbox(inbox_dir)
    if not new_files:
        log.info("No new audio files in inbox. Nothing to do.")
        return 0

    max_ep, existing_items = parse_existing_feed(feed_path)
    log.info("Existing feed episodes: %d (max=%d)", len(existing_items), max_ep)
    log.info("New files to publish: %d", len(new_files))

    pod_title = cfg["podcast"]["title"]
    plans: list[dict] = []
    for i, src in enumerate(new_files, start=1):
        ep_num = max_ep + i
        title = clean_title(src.stem)
        new_name = f"{ep_num:03d} - {title}{src.suffix}"
        dest = audio_dir / new_name
        if dest.exists():
            log.error("Destination already exists, aborting: %s", dest)
            return 1
        sidecar_text = read_sidecar(src)
        description = sidecar_text if sidecar_text else f"Episode {ep_num} of {pod_title}"
        stat = src.stat()
        plans.append({
            "src": src,
            "dest": dest,
            "new_name": new_name,
            "episode": ep_num,
            "title": title,
            "description": description,
            "pub_date": formatdate(stat.st_mtime, localtime=False, usegmt=True),
            "size": stat.st_size,
            "mime": SUPPORTED_EXTS[src.suffix.lower()],
            "url": f"{base_url}/{quote(audio_subdir)}/{quote(new_name)}",
            "guid": make_guid(new_name),
            "has_sidecar": sidecar_text is not None,
            "base_url": base_url,
            "audio_subdir": audio_subdir,
        })

    log.info("")
    log.info("=== Plan ===")
    for p in plans:
        sc = " + sidecar" if p["has_sidecar"] else ""
        log.info("  ep %03d: %s -> %s%s", p["episode"], p["src"].name, p["new_name"], sc)
        log.info("         url: %s", p["url"])
    log.info("")

    if args.dry_run:
        log.info("--dry-run: no files moved, feed not written, no commit.")
        _preview_feed(cfg, existing_items, plans, log)
        log.info("=== Commit preview ===")
        log.info("  message: %s", _commit_message(plans))
        return 0

    # Transcribe each planned audio file. VTT lands in inbox alongside audio,
    # then we move it with the rest in the move loop below.
    if not args.skip_transcripts:
        import transcribe
        single_item = len(plans) == 1
        transcription_failures: list[tuple[str, str]] = []
        for p in plans:
            vtt_src = p["src"].with_suffix(".vtt")
            p["vtt_src"] = vtt_src
            p["vtt_dest"] = (repo_dir / "transcripts" / f"{p['dest'].stem}.vtt")
            try:
                log.info("transcribing: %s", p["src"].name)
                transcribe.transcribe_audio(p["src"], vtt_src, cfg, force=True)
            except Exception as e:
                log.error("transcription FAILED for %s: %s", p["src"].name, e)
                transcription_failures.append((p["src"].name, str(e)))
                if single_item:
                    log.error("single-item publish; aborting. Inbox untouched.")
                    return 1
                p["vtt_src"] = None  # mark as no-transcript; continue with others

        if transcription_failures and not single_item:
            log.warning("continuing publish despite %d transcription failure(s); "
                        "failed items ship without transcripts",
                        len(transcription_failures))
    else:
        log.info("--skip-transcripts: not generating transcripts")
        for p in plans:
            p["vtt_src"] = None
            p["vtt_dest"] = None

    moved_files: list[Path] = []
    for p in plans:
        shutil.move(str(p["src"]), str(p["dest"]))
        moved_files.append(p["dest"])
        log.info("moved: %s", p["dest"])
        if p["has_sidecar"]:
            for ext in (".md", ".txt"):
                sc = p["src"].with_suffix(ext)
                if sc.exists():
                    sc_dest = p["dest"].with_suffix(ext)
                    shutil.move(str(sc), str(sc_dest))
                    moved_files.append(sc_dest)
                    log.info("moved sidecar: %s", sc_dest)

        vtt_src = p.get("vtt_src")
        vtt_dest = p.get("vtt_dest")
        if vtt_src and vtt_src.exists() and vtt_dest:
            vtt_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(vtt_src), str(vtt_dest))
            moved_files.append(vtt_dest)
            log.info("moved transcript: %s", vtt_dest)

    new_items = [_plan_to_item(p) for p in plans]
    feed_bytes = build_feed(cfg, existing_items + new_items)
    feed_path.write_bytes(feed_bytes)
    log.info("wrote feed: %s (%d items)", feed_path, len(existing_items) + len(new_items))

    commit_msg = _commit_message(plans)
    git_run(repo_dir, "add", str(feed_path.relative_to(repo_dir)), log=log)
    for f in moved_files:
        git_run(repo_dir, "add", str(f.relative_to(repo_dir)), log=log)
    git_run(repo_dir, "commit", "-m", commit_msg, log=log)
    log.info("committed: %s", commit_msg)

    if args.no_push:
        log.info("--no-push: skipping git push")
    else:
        git_run(repo_dir, "push", "-u", "origin", "main", log=log)
        log.info("pushed to origin main")

    log.info("")
    log.info("=== Summary ===")
    log.info("Added %d episode(s):", len(plans))
    for p in plans:
        log.info("  ep %03d - %s", p["episode"], p["url"])
    log.info("Apple Podcasts may take up to an hour to refresh. Pull down in Library to force refresh.")
    return 0


def _rebuild_feed(cfg: dict, repo_dir: Path, feed_path: Path, args: argparse.Namespace, log: logging.Logger) -> int:
    _, items = parse_existing_feed(feed_path)
    if not items:
        log.info("No existing items found; nothing to rebuild.")
        return 0
    log.info("Rebuilding feed with %d existing item(s).", len(items))
    feed_bytes = build_feed(cfg, items)
    if args.dry_run:
        log.info("--dry-run: feed preview below, not written.")
        for line in feed_bytes.decode("utf-8").splitlines():
            log.info("  %s", line)
        return 0
    feed_path.write_bytes(feed_bytes)
    log.info("wrote feed: %s", feed_path)
    git_run(repo_dir, "add", str(feed_path.relative_to(repo_dir)), log=log)
    status = git_run(repo_dir, "diff", "--cached", "--quiet", check=False, log=log)
    if status.returncode == 0:
        log.info("feed.xml unchanged; nothing to commit.")
        return 0
    git_run(repo_dir, "commit", "-m", "Rebuild feed.xml", log=log)
    if args.no_push:
        log.info("--no-push: skipping git push")
    else:
        git_run(repo_dir, "push", log=log)
    return 0


def _backfill_transcripts(cfg: dict, repo_dir: Path, audio_dir: Path, feed_path: Path,
                          base_url: str, audio_subdir: str, args: argparse.Namespace,
                          log: logging.Logger) -> int:
    """Generate VTTs for existing audio/ entries that lack a sibling transcripts/ VTT.
    Rebuild feed with transcript URLs. Commit + push.
    """
    import transcribe  # lazy import so --help is instant and transcribe-only CLIs work

    transcripts_dir = repo_dir / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)

    _, existing_items = parse_existing_feed(feed_path)
    if not existing_items:
        log.info("No existing items in feed; nothing to backfill.")
        return 0

    # Find audio files that have no VTT yet
    missing: list[tuple[Path, Path]] = []  # (audio_path, vtt_path)
    for audio_path in sorted(audio_dir.iterdir()):
        if not audio_path.is_file():
            continue
        if audio_path.suffix.lower() not in SUPPORTED_EXTS:
            continue
        vtt_path = transcripts_dir / f"{audio_path.stem}.vtt"
        if not vtt_path.exists():
            missing.append((audio_path, vtt_path))

    if not missing:
        log.info("All existing episodes already have transcripts. Nothing to backfill.")
        return 0

    log.info("=== Backfill plan: %d episode(s) ===", len(missing))
    for a, v in missing:
        log.info("  %s -> %s", a.name, v.name)

    if args.dry_run:
        log.info("--dry-run: not transcribing.")
        return 0

    # Transcribe each; continue on per-episode failure
    failures: list[tuple[str, str]] = []
    new_vtts: list[Path] = []
    for audio_path, vtt_path in missing:
        try:
            log.info("transcribing: %s", audio_path.name)
            transcribe.transcribe_audio(audio_path, vtt_path, cfg)
            new_vtts.append(vtt_path)
        except Exception as e:  # intentional broad catch — per-episode isolation
            log.error("transcription FAILED for %s: %s", audio_path.name, e)
            failures.append((audio_path.name, str(e)))

    if not new_vtts:
        log.error("no episodes successfully transcribed; bailing without feed update.")
        return 1

    # Build transcript URL map keyed by guid, merge into existing_items
    guid_to_transcript_url = {}
    for it in existing_items:
        # Recover the audio filename from the enclosure URL to pair with the VTT
        name = f"{it['episode']:03d} - {it['title']}"
        vtt_name = f"{name}.vtt"
        vtt_path = transcripts_dir / vtt_name
        if vtt_path.exists():
            it["transcript_url"] = f"{base_url}/transcripts/{quote(vtt_name)}"
            guid_to_transcript_url[it["guid"]] = it["transcript_url"]

    log.info("transcript URLs in feed: %d", len(guid_to_transcript_url))

    # Rebuild feed
    feed_bytes = build_feed(cfg, existing_items)
    feed_path.write_bytes(feed_bytes)
    log.info("wrote feed: %s", feed_path)

    # Commit + push
    for v in new_vtts:
        git_run(repo_dir, "add", str(v.relative_to(repo_dir)), log=log)
    git_run(repo_dir, "add", str(feed_path.relative_to(repo_dir)), log=log)

    if len(new_vtts) == len(missing):
        commit_msg = f"Add transcripts for {len(new_vtts)} existing episode(s)"
    else:
        commit_msg = f"Add transcripts for {len(new_vtts)}/{len(missing)} existing episode(s)"
    git_run(repo_dir, "commit", "-m", commit_msg, log=log)
    log.info("committed: %s", commit_msg)

    if args.no_push:
        log.info("--no-push: skipping git push")
    else:
        git_run(repo_dir, "push", "-u", "origin", "main", log=log)
        log.info("pushed to origin main")

    if failures:
        log.error("=== Backfill completed with %d failure(s) ===", len(failures))
        for name, err in failures:
            log.error("  %s: %s", name, err)
        return 1
    return 0


def _plan_to_item(p: dict) -> dict:
    base_url = p.get("base_url", "")
    audio_subdir = p.get("audio_subdir", "audio")
    transcript_url = ""
    vtt_dest = p.get("vtt_dest")
    if vtt_dest is not None and vtt_dest.exists():
        transcript_url = f"{base_url}/transcripts/{quote(vtt_dest.name)}"
    return {
        "title": p["title"],
        "guid": p["guid"],
        "pub_date": p["pub_date"],
        "description": p["description"],
        "enclosure_url": p["url"],
        "enclosure_length": p["size"],
        "enclosure_type": p["mime"],
        "episode": p["episode"],
        "transcript_url": transcript_url,
    }


def _commit_message(plans: list[dict]) -> str:
    if len(plans) == 1:
        p = plans[0]
        return f"Add episode {p['episode']:03d}: {p['title']}"
    return f"Add episodes {plans[0]['episode']:03d}-{plans[-1]['episode']:03d}"


def _preview_feed(cfg: dict, existing: list[dict], plans: list[dict], log: logging.Logger) -> None:
    new_items = [_plan_to_item(p) for p in plans]
    feed = build_feed(cfg, existing + new_items).decode("utf-8")
    log.info("=== Feed preview (would be written) ===")
    for line in feed.splitlines():
        log.info("  %s", line)


if __name__ == "__main__":
    sys.exit(main())
