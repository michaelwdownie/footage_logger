#!/usr/bin/env python3
"""
footage_logger.py - turn long gameplay captures into searchable, timestamped logs.

For each video it:
  1. grabs frames at scene changes (and at least every few seconds),
  2. transcribes the audio with Whisper (dialogue, subtitles you can hear),
  3. asks a local vision model (via Ollama) to describe each frame,
  4. writes a timestamped Markdown log with thumbnails, a JSON log for searching,
     and a Final Cut Pro XML file that imports the clip with every moment as a marker.

Everything runs locally. Jobs are resumable: stop it any time and run the same
command again to carry on where it left off.

Usage:
  python3 footage_logger.py log "/Volumes/Footage/Pragmata captures" --context "Game: Pragmata"
  python3 footage_logger.py search "/Volumes/Footage/Pragmata captures" "hugh" "lab"

Run `python3 footage_logger.py log -h` for all options.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from xml.sax.saxutils import quoteattr

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm", ".flv", ".ts", ".mts"}
DEFAULT_OUT_NAME = "logs"
DEFAULT_MODEL = "gemma4:12b"
DEFAULT_OLLAMA = "http://localhost:11434"

SCENE_TYPES = ["gameplay", "cutscene", "dialogue", "menu", "map", "loading", "title", "other"]

CAPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "scene_type": {"type": "string", "enum": SCENE_TYPES},
        "summary": {"type": "string"},
        "on_screen_text": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["scene_type", "summary", "on_screen_text", "tags"],
}

# Phrases Whisper tends to invent over music or silence.
HALLUCINATIONS = {
    "thanks for watching", "thank you for watching", "thank you.", "thank you",
    "please subscribe", "subtitles by the amara.org community", "you",
}


# --------------------------------------------------------------------------- utils

def log(msg: str = "") -> None:
    print(msg, flush=True)


def die(msg: str) -> None:
    print(f"\nError: {msg}", file=sys.stderr)
    sys.exit(1)


def ts(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def slugify(rel: str) -> str:
    rel = rel.replace(os.sep, "__")
    return re.sub(r"[^\w\-. ]+", "_", rel).strip() or "video"


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def check_tools() -> None:
    missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if missing:
        die(f"{', '.join(missing)} not found. Install with:  brew install ffmpeg")


# --------------------------------------------------------------------------- probing

def probe(video: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(video)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe could not read the file: {out.stderr.strip()[:300]}")
    info = json.loads(out.stdout)
    streams = info.get("streams", [])
    vstreams = [s for s in streams if s.get("codec_type") == "video"
                and not s.get("disposition", {}).get("attached_pic")]
    astreams = [s for s in streams if s.get("codec_type") == "audio"]
    if not vstreams:
        raise RuntimeError("no video stream found")
    v = vstreams[0]
    fps = Fraction(v.get("avg_frame_rate") or "0/1") if v.get("avg_frame_rate") not in (None, "0/0") else Fraction(0)
    if fps <= 0 or fps > 240:
        fps = Fraction(v.get("r_frame_rate") or "30/1")
    fps = fps.limit_denominator(1001)
    duration = float(info.get("format", {}).get("duration") or v.get("duration") or 0)
    return {
        "duration": duration,
        "fps_num": fps.numerator,
        "fps_den": fps.denominator,
        "width": int(v.get("width") or 1920),
        "height": int(v.get("height") or 1080),
        "audio_streams": len(astreams),
    }


# --------------------------------------------------------------------------- frames

def extract_frames(video: Path, work: Path, meta: dict, args) -> list[dict]:
    """Pick frames at scene changes, with a guaranteed frame at least every max_gap seconds."""
    index_path = work / "frames.json"
    frames_dir = work / "frames"
    if index_path.exists():
        return json.loads(index_path.read_text(encoding="utf-8"))

    if frames_dir.exists():
        shutil.rmtree(frames_dir)  # half-finished earlier run
    frames_dir.mkdir(parents=True)

    expr = (
        f"gt(isnan(prev_selected_t)"
        f"+gte(t-prev_selected_t,{args.max_gap})"
        f"+gt(scene,{args.scene})*gte(t-prev_selected_t,{args.min_gap}),0)"
    )
    vf = f"scale={args.frame_width}:-2,select='{expr}',showinfo"
    base = ["ffmpeg", "-hide_banner", "-nostdin", "-y"]
    tail = ["-i", str(video), "-an", "-sn", "-vf", vf, "-fps_mode", "vfr", "-q:v", "3",
            str(frames_dir / "f_%06d.jpg")]

    for attempt, hw in enumerate((["-hwaccel", "auto"], [])):
        times: list[float] = []
        proc = subprocess.Popen(base + hw + tail, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                text=True, errors="replace")
        last_print = 0.0
        tail_lines: list[str] = []
        for line in proc.stderr:
            m = re.search(r"pts_time:\s*([\d.]+)", line)
            if m and "showinfo" in line:
                t = float(m.group(1))
                times.append(t)
                now = time.time()
                if meta["duration"] and now - last_print > 2:
                    pct = min(100, 100 * t / meta["duration"])
                    print(f"\r  scanning video… {pct:5.1f}%  ({len(times)} frames)", end="", flush=True)
                    last_print = now
            else:
                tail_lines = (tail_lines + [line.rstrip()])[-8:]
        proc.wait()
        print("\r" + " " * 60 + "\r", end="")
        if proc.returncode == 0:
            break
        if attempt == 0:
            log("  hardware decoding failed, retrying in software…")
            shutil.rmtree(frames_dir)
            frames_dir.mkdir(parents=True)
            continue
        raise RuntimeError("ffmpeg frame extraction failed:\n    " + "\n    ".join(tail_lines))

    files = sorted(frames_dir.glob("f_*.jpg"))
    if len(files) != len(times):
        log(f"  note: {len(files)} frame files vs {len(times)} timestamps; using the shorter list")
    frames = [{"file": f.name, "t": round(t, 3)} for f, t in zip(files, times)]
    for f in files[len(frames):]:
        f.unlink()
    write_json(index_path, frames)
    return frames


# --------------------------------------------------------------------------- audio

def transcribe(video: Path, work: Path, meta: dict, args) -> list[dict]:
    cache = work / "transcript.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    if args.no_audio or meta["audio_streams"] == 0:
        return []
    if args.audio_track >= meta["audio_streams"]:
        die(f"--audio-track {args.audio_track} requested but {video.name} has "
            f"{meta['audio_streams']} audio track(s) (numbered from 0)")

    backend = None
    try:
        import mlx_whisper  # noqa: F401
        backend = "mlx"
    except ImportError:
        try:
            import faster_whisper  # noqa: F401
            backend = "faster"
        except ImportError:
            die("no Whisper library found. On the Mac mini run:\n"
                "    pip3 install mlx-whisper\n"
                "or run with --no-audio to skip transcription.")

    wav = work / "audio.wav"
    log("  extracting audio…")
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", str(video),
         "-map", f"0:a:{args.audio_track}", "-vn", "-ac", "1", "-ar", "16000", str(wav)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"audio extraction failed: {r.stderr.strip()[:300]}")

    if backend == "mlx" and args.device != "auto":
        log(f"  note: --device {args.device} is ignored by mlx-whisper, which always uses the Mac's GPU")
    log(f"  transcribing audio ({backend}-whisper) — long captures take a while…")
    segs: list[dict] = []
    if backend == "mlx":
        import mlx_whisper
        result = mlx_whisper.transcribe(
            str(wav),
            path_or_hf_repo=args.whisper_model or "mlx-community/whisper-large-v3-turbo",
            language=args.language,
            condition_on_previous_text=False,
            verbose=False,
        )
        raw = result.get("segments", [])
        segs = [{"start": s["start"], "end": s["end"], "text": s["text"],
                 "nsp": s.get("no_speech_prob", 0), "lp": s.get("avg_logprob", 0)} for s in raw]
    else:
        from faster_whisper import WhisperModel
        model = WhisperModel(args.whisper_model or "large-v3-turbo", device=args.device, compute_type="int8")
        raw, _ = model.transcribe(str(wav), language=args.language, vad_filter=True,
                                  condition_on_previous_text=False)
        segs = [{"start": s.start, "end": s.end, "text": s.text,
                 "nsp": s.no_speech_prob, "lp": s.avg_logprob} for s in raw]

    cleaned: list[dict] = []
    for s in segs:
        text = s["text"].strip()
        if not text or text.lower().strip(" .!") in HALLUCINATIONS:
            continue
        if s["nsp"] > 0.6 and s["lp"] < -1.0:  # Whisper's own "probably not speech" rule
            continue
        if cleaned and cleaned[-1]["text"] == text:  # stuck-on-repeat
            continue
        cleaned.append({"start": round(s["start"], 2), "end": round(s["end"], 2), "text": text})

    wav.unlink(missing_ok=True)
    write_json(cache, cleaned)
    return cleaned


def speech_between(transcript: list[dict], start: float, end: float) -> list[dict]:
    return [s for s in transcript if start <= s["start"] < end]


# --------------------------------------------------------------------------- vision

def build_prompt(t: float, prev: dict | None, speech: str, context: str) -> str:
    parts = ["You are logging video-game footage for a video essayist who needs to find moments later."]
    if context:
        parts.append(f"Context about this footage: {context}")
    if prev:
        parts.append(f"The previous logged frame ({ts(prev['t'])}) was: {prev['summary']}")
    if speech:
        parts.append(f"Speech heard around this moment: \"{speech}\"")
    parts.append(
        f"Describe THIS frame at {ts(t)} as JSON:\n"
        f"- scene_type: one of {', '.join(SCENE_TYPES)}\n"
        "- summary: 1-2 factual sentences. Where are we, who or what is on screen, what is happening? "
        "Only name characters or places if on-screen text, the speech, or the context identifies them; "
        "otherwise describe them. If little has changed since the previous frame, say so briefly.\n"
        "- on_screen_text: legible subtitles, objectives, menu titles or item names, verbatim. "
        "Empty string if none.\n"
        "- tags: 3-6 short lowercase search keywords."
    )
    return "\n".join(parts)


def ollama_caption(image: Path, prompt: str, args) -> dict:
    b64 = base64.b64encode(image.read_bytes()).decode()
    payload = {
        "model": args.model,
        "messages": [{"role": "user", "content": prompt, "images": [b64]}],
        "stream": False,
        "format": CAPTION_SCHEMA,
        "think": False,
        "keep_alive": "30m",
        "options": {"temperature": 0.2, "num_ctx": 8192},
    }
    url = args.ollama_url.rstrip("/") + "/api/chat"
    last_err = None
    for attempt in range(4):
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=args.timeout) as resp:
                body = json.loads(resp.read())
            content = body.get("message", {}).get("content", "").strip()
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                data = {"scene_type": "other", "summary": content, "on_screen_text": "", "tags": []}
            return {
                "scene_type": str(data.get("scene_type", "other")).lower(),
                "summary": str(data.get("summary", "")).strip(),
                "on_screen_text": str(data.get("on_screen_text", "")).strip(),
                "tags": [str(x).lower().strip() for x in data.get("tags", []) if str(x).strip()][:8],
            }
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            if e.code == 404 and "not found" in detail.lower():
                die(f"Ollama doesn't have the model '{args.model}'. Run:  ollama pull {args.model}")
            if "think" in detail.lower() and "think" in payload:
                payload.pop("think")  # older Ollama without the think switch
                continue
            last_err = f"HTTP {e.code}: {detail[:200]}"
        except urllib.error.URLError as e:
            if attempt == 0 and isinstance(e.reason, ConnectionRefusedError):
                die(f"can't reach Ollama at {args.ollama_url}. Open the Ollama app (or run `ollama serve`).")
            last_err = str(e.reason)
        except (TimeoutError, OSError) as e:
            last_err = str(e)
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"vision model failed repeatedly: {last_err}")


def caption_frames(frames: list[dict], work: Path, transcript: list[dict], args) -> dict[str, dict]:
    cache = work / "captions.jsonl"
    done: dict[str, dict] = {}
    if cache.exists():
        for line in cache.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                done[row["file"]] = row
            except (json.JSONDecodeError, KeyError):
                pass  # a line cut off by an interruption
    todo = [f for f in frames if f["file"] not in done]
    if not todo:
        return done
    if done:
        log(f"  resuming: {len(done)} frames already described, {len(todo)} to go")
    else:
        log(f"  describing {len(todo)} frames with {args.model}…")

    order = {f["file"]: i for i, f in enumerate(frames)}
    started = time.time()
    with cache.open("a", encoding="utf-8") as fh:
        for n, fr in enumerate(todo, 1):
            i = order[fr["file"]]
            prev = done.get(frames[i - 1]["file"]) if i > 0 else None
            speech = " ".join(s["text"] for s in speech_between(
                transcript, fr["t"] - args.max_gap / 2, fr["t"] + args.max_gap / 2))[:400]
            prompt = build_prompt(fr["t"], prev, speech, args.context)
            cap = ollama_caption(work / "frames" / fr["file"], prompt, args)
            row = {"file": fr["file"], "t": fr["t"], **cap}
            done[fr["file"]] = row
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            elapsed = time.time() - started
            eta = elapsed / n * (len(todo) - n)
            print(f"\r  frame {n}/{len(todo)}  ·  {elapsed / n:4.1f}s each  ·  ~{ts(eta)} left   ",
                  end="", flush=True)
    print()
    return done


# --------------------------------------------------------------------------- outputs

def build_entries(frames: list[dict], captions: dict[str, dict], transcript: list[dict],
                  duration: float) -> list[dict]:
    entries = []
    for i, fr in enumerate(frames):
        cap = captions.get(fr["file"])
        if not cap:
            continue
        start = 0.0 if i == 0 else fr["t"]
        end = frames[i + 1]["t"] if i + 1 < len(frames) else duration + 1
        entries.append({
            "t": fr["t"],
            "frame": fr["file"],
            "scene_type": cap["scene_type"],
            "summary": cap["summary"],
            "on_screen_text": cap["on_screen_text"],
            "tags": cap["tags"],
            "speech": speech_between(transcript, start, end),
        })
    return entries


def write_markdown(path: Path, video: Path, slug: str, meta: dict, entries: list[dict], args) -> None:
    lines = [
        f"# {video.name}",
        "",
        f"- **Source:** `{video}`",
        f"- **Length:** {ts(meta['duration'])}  ·  **Moments logged:** {len(entries)}",
        f"- **Logged:** {datetime.now():%Y-%m-%d %H:%M} with `{args.model}`",
        "",
        "---",
        "",
    ]
    for e in entries:
        lines.append(f"### `{ts(e['t'])}` · {e['scene_type'].title()}")
        lines.append("")
        if args.thumbnails:
            lines.append(f"![{ts(e['t'])}](<{slug}/frames/{e['frame']}>)")
            lines.append("")
        lines.append(e["summary"] or "_(no description)_")
        lines.append("")
        if e["on_screen_text"]:
            lines.append(f"**On screen:** “{e['on_screen_text']}”")
            lines.append("")
        for s in e["speech"]:
            lines.append(f"> `{ts(s['start'])}` {s['text']}")
        if e["speech"]:
            lines.append("")
        if e["tags"]:
            lines.append("*" + ", ".join(e["tags"]) + "*")
            lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_fcpxml(path: Path, video: Path, meta: dict, entries: list[dict]) -> None:
    """A Final Cut Pro XML: imports the source clip into a new event with a marker per moment."""
    num, den = meta["fps_num"], meta["fps_den"]  # fps = num/den; one frame = den/num s

    def at(seconds: float) -> str:
        frames = int(round(seconds * num / den))
        return f"{frames * den}/{num}s"

    total = at(int(meta["duration"] * num / den) * den / num)
    name = quoteattr(video.stem)
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<!DOCTYPE fcpxml>",
        '<fcpxml version="1.10">',
        "  <resources>",
        f'    <format id="r1" frameDuration="{den}/{num}s" width="{meta["width"]}" height="{meta["height"]}"/>',
        f'    <asset id="r2" name={name} start="0s" duration="{total}" hasVideo="1" '
        f'hasAudio="{1 if meta["audio_streams"] else 0}" format="r1">',
        f"      <media-rep kind=\"original-media\" src={quoteattr(video.resolve().as_uri())}/>",
        "    </asset>",
        "  </resources>",
        "  <library>",
        f'    <event name={quoteattr("Footage log – " + video.stem)}>',
        f'      <asset-clip ref="r2" name={name} duration="{total}" format="r1">',
    ]
    for e in entries:
        label = f"{e['scene_type'].title()}: {e['summary']}"
        if len(label) > 90:
            label = label[:87].rstrip() + "…"
        note = e["summary"]
        if e["on_screen_text"]:
            note += f"  |  On screen: {e['on_screen_text']}"
        if e["speech"]:
            note += "  |  Speech: " + " ".join(s["text"] for s in e["speech"])
        out.append(f'        <marker start="{at(e["t"])}" duration="{den}/{num}s" '
                   f"value={quoteattr(label)} note={quoteattr(note[:1000])}/>")
    out += ["      </asset-clip>", "    </event>", "  </library>", "</fcpxml>", ""]
    path.write_text("\n".join(out), encoding="utf-8")


# --------------------------------------------------------------------------- commands

def find_videos(paths: list[Path], out_dir: Path) -> list[tuple[Path, Path]]:
    """Returns (video, root) pairs; root is used to build a unique name for the log."""
    found = []
    for p in paths:
        if p.is_file():
            found.append((p, p.parent))
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if (f.is_file() and f.suffix.lower() in VIDEO_EXTS and not f.name.startswith("._")
                        and out_dir not in f.parents):
                    found.append((f, p))
        else:
            die(f"not found: {p}")
    return found


def cmd_log(args) -> None:
    check_tools()
    paths = [Path(p).expanduser().resolve() for p in args.paths]
    first = paths[0] if paths[0].is_dir() else paths[0].parent
    out_dir = Path(args.out).expanduser() if args.out else first / DEFAULT_OUT_NAME
    out_dir.mkdir(parents=True, exist_ok=True)

    videos = find_videos(paths, out_dir)
    if not videos:
        die("no video files found")
    log(f"Found {len(videos)} video(s). Logs go to: {out_dir}\n")

    failures = []
    for n, (video, root) in enumerate(videos, 1):
        slug = slugify(str(video.relative_to(root).with_suffix("")))
        work = out_dir / slug
        md_path = out_dir / f"{slug}.md"
        log(f"[{n}/{len(videos)}] {video.name}")
        if (work / "log.json").exists() and not args.force:
            log("  already logged — skipping (use --force to redo)\n")
            continue
        if args.force and work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True, exist_ok=True)
        try:
            meta = probe(video)
            log(f"  {ts(meta['duration'])} long, {meta['width']}x{meta['height']}, "
                f"{meta['audio_streams']} audio track(s)")
            transcript = transcribe(video, work, meta, args)
            if transcript:
                log(f"  {len(transcript)} lines of speech")
            frames = extract_frames(video, work, meta, args)
            log(f"  {len(frames)} frames selected")
            captions = caption_frames(frames, work, transcript, args)
            entries = build_entries(frames, captions, transcript, meta["duration"])
            write_json(work / "log.json", {
                "video": str(video), "name": video.name, "duration": meta["duration"],
                "model": args.model, "created": datetime.now().isoformat(timespec="seconds"),
                "entries": entries,
            })
            write_markdown(md_path, video, slug, meta, entries, args)
            outputs = [md_path.name]
            if not args.no_fcpxml:
                fx = out_dir / f"{slug}.fcpxml"
                write_fcpxml(fx, video, meta, entries)
                outputs.append(fx.name)
            log(f"  done → {', '.join(outputs)}\n")
        except KeyboardInterrupt:
            log("\n\nStopped. Run the same command again to pick up where it left off.")
            sys.exit(130)
        except Exception as e:  # keep going with the other videos
            log(f"\n  FAILED: {e}\n")
            failures.append(video.name)

    if failures:
        log(f"{len(failures)} video(s) failed: {', '.join(failures)}")
    log(f"Search your logs with:\n  python3 {Path(sys.argv[0]).name} search {quote_path(out_dir)} \"your words\"")


def quote_path(p: Path) -> str:
    s = str(p)
    return f'"{s}"' if " " in s else s


def cmd_search(args) -> None:
    base = Path(args.folder).expanduser()
    roots = [base / DEFAULT_OUT_NAME] if (base / DEFAULT_OUT_NAME).is_dir() else [base]
    logs = [p for r in roots for p in sorted(r.rglob("log.json"))]
    if not logs:
        die(f"no logs found under {base}")
    terms = [t.lower() for t in args.terms]
    hits = 0
    for lp in logs:
        data = json.loads(lp.read_text(encoding="utf-8"))
        for e in data["entries"]:
            if args.type and e["scene_type"] != args.type:
                continue
            speech = " ".join(s["text"] for s in e["speech"])
            hay = " ".join([e["summary"], e["on_screen_text"], " ".join(e["tags"]), speech]).lower()
            if all(t in hay for t in terms):
                hits += 1
                log(f"{data['name']}  {ts(e['t'])}  [{e['scene_type']}]  {e['summary']}")
                if e["on_screen_text"] and any(t in e["on_screen_text"].lower() for t in terms):
                    log(f"    on screen: {e['on_screen_text']}")
                for s in e["speech"]:
                    if any(t in s["text"].lower() for t in terms):
                        log(f"    {ts(s['start'])} said: {s['text']}")
                if hits >= args.limit:
                    log(f"\n(showing first {args.limit}; use --limit to see more)")
                    return
    log(f"\n{hits} match(es)." if hits else "No matches.")


def main() -> None:
    p = argparse.ArgumentParser(description="Searchable timestamped logs for long video captures.")
    sub = p.add_subparsers(dest="cmd", required=True)

    lg = sub.add_parser("log", help="log one or more videos or folders of videos")
    lg.add_argument("paths", nargs="+", help="video files and/or folders (searched recursively)")
    lg.add_argument("--out", help=f"where logs go (default: <folder>/{DEFAULT_OUT_NAME})")
    lg.add_argument("--context", default="", help='what the footage is, e.g. "Game: Pragmata, main story"')
    lg.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollama vision model (default {DEFAULT_MODEL})")
    lg.add_argument("--ollama-url", default=DEFAULT_OLLAMA)
    lg.add_argument("--max-gap", type=float, default=8, help="always grab a frame at least this often, seconds (default 8)")
    lg.add_argument("--min-gap", type=float, default=2, help="never grab scene-change frames closer than this, seconds (default 2)")
    lg.add_argument("--scene", type=float, default=0.3, help="scene-change sensitivity 0-1, lower = more frames (default 0.3)")
    lg.add_argument("--frame-width", type=int, default=1280, help="frame size sent to the model (default 1280)")
    lg.add_argument("--audio-track", type=int, default=0, help="which audio track to transcribe, from 0 (default 0)")
    lg.add_argument("--no-audio", action="store_true", help="skip transcription")
    lg.add_argument("--whisper-model", help="override the Whisper model")
    lg.add_argument("--language", help='force the spoken language, e.g. "en" (default: auto-detect)')
    lg.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto",
                    help="where faster-whisper runs; auto uses an NVIDIA GPU if found (default auto). "
                         "mlx-whisper always uses the Mac's GPU")
    lg.add_argument("--no-thumbnails", dest="thumbnails", action="store_false", help="leave frame images out of the Markdown log")
    lg.add_argument("--no-fcpxml", action="store_true", help="don't write the Final Cut Pro marker file")
    lg.add_argument("--timeout", type=int, default=300, help="seconds to wait per frame (default 300)")
    lg.add_argument("--force", action="store_true", help="redo videos that already have a log")
    lg.set_defaults(func=cmd_log)

    sr = sub.add_parser("search", help="search existing logs")
    sr.add_argument("folder", help=f"the footage folder (or its {DEFAULT_OUT_NAME} folder)")
    sr.add_argument("terms", nargs="+", help="words that must all appear")
    sr.add_argument("--type", choices=SCENE_TYPES, help="only this kind of moment")
    sr.add_argument("--limit", type=int, default=50)
    sr.set_defaults(func=cmd_search)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
