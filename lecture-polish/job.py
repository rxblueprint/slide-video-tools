"""
Polishes one uploaded lecture, for the GitHub Actions job in .github/workflows/polish-lecture.yml.

This repository is public, so its Actions logs are too: the job never prints lecture titles or what was said.

  python3 job.py <lecture id>

Asks the website for a link to the raw upload and a link to put the result (POST /api/lectures/polish,
Authorization: Bearer $POLISH_SECRET), runs polish.py on it, uploads the polished video and reports
back. Any failure is reported too, so the lecture goes live as uploaded instead of waiting forever.
Needs ffmpeg, curl, Pillow and faster-whisper (for captions). Environment: SITE_URL, POLISH_SECRET.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import polish  # noqa: E402

SITE = os.environ.get("SITE_URL", "https://rxblueprint.ca").rstrip("/")
SECRET = os.environ.get("POLISH_SECRET", "")


def site(action, **body):
    req = urllib.request.Request(
        f"{SITE}/api/lectures/polish", method="POST",
        data=json.dumps({"action": action, **body}).encode(),
        headers={"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json",
                 "User-Agent": "rxblueprint-polish-job"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def curl(*args):
    # signed links never go to the log
    res = subprocess.run(["curl", "-fsS", "--retry", "5", "--retry-all-errors", "--retry-delay", "5", *args])
    if res.returncode != 0:
        raise RuntimeError(f"{'upload' if '-T' in args else 'download'} failed (curl exit {res.returncode})")


def safe(name):
    return re.sub(r'[\\/:*?"<>|]+', " ", name).strip() or "Lecture"


def main(lecture_id):
    if not re.fullmatch(r"[0-9a-f-]{36}", lecture_id):
        sys.exit("Not a lecture id")
    job = site("start", id=lecture_id)
    print("Polishing the lecture", flush=True)
    work = Path(tempfile.mkdtemp(prefix="lecture_"))
    # polish.py reads the card text from the folders and file name, as on the desktop
    src = work / f"{job['exam']} Lectures" / safe(job["module"] or "Lecture") / "lecture.mp4"
    src.parent.mkdir(parents=True)
    curl("-o", str(src), job["source_url"])
    print(f"Downloaded {src.stat().st_size / 1e6:.0f} MB", flush=True)
    if job.get("notes_url") and job.get("notes_ext") in ("pptx", "txt"):
        # next to the video with the same name, where polish.py looks for it
        curl("-o", str(src.with_suffix("." + job["notes_ext"])), job["notes_url"])
        print("Downloaded the notes for the captions", flush=True)

    polish.nice_title = lambda _src: job["title"]
    polish.UP_NEXT = {src.stem: job["up_next"]} if job.get("up_next") else {}
    polish.CAPTIONS = True
    polish.WHISPER_MODEL = os.environ.get("WHISPER_MODEL", polish.WHISPER_MODEL)
    polish.process(src)

    out = src.with_name(f"{src.stem}_POLISHED.mp4")
    if not out.exists():
        raise RuntimeError("polish.py didn't write a video")
    chapters_file = src.with_name(f"{src.stem}_POLISHED.chapters.json")
    chapters = json.loads(chapters_file.read_text()) if chapters_file.exists() else []
    curl("-T", str(out), "-H", "Content-Type: video/mp4", job["output_url"])
    vtt = src.with_name(f"{src.stem}_POLISHED.vtt")
    captions_key = None
    if vtt.exists() and job.get("captions_url"):
        curl("-T", str(vtt), "-H", "Content-Type: text/vtt", job["captions_url"])
        captions_key = job["captions_key"]
    review_file = src.with_name(f"{src.stem}_POLISHED.review.json")
    review = json.loads(review_file.read_text()) if review_file.exists() else None
    if review:
        # the site keeps up to 300 checks and 500 cuts
        review = {"checks": review.get("checks", [])[:300], "cuts": review.get("cuts", [])[:500],
                  "lost": review.get("lost", [])[:100]}
        print(f"{len(review['cuts'])} cuts and {len(review['checks'])} lines to check (listed on the site)", flush=True)
    # each slide's notes, for the search on the site (it places them at the slide's topic)
    slide_notes = []
    notes_file = polish.find_notes(src)
    if notes_file:
        import captions
        slide_notes = [{"slide": n, "text": t.strip()[:4000]} for n, t in enumerate(captions.slide_notes(str(notes_file)), 1) if t and t.strip()]
        while len(json.dumps(slide_notes)) > 700_000:   # the site takes up to 1 MB in all
            slide_notes = [{**x, "text": x["text"][: len(x["text"]) * 3 // 4]} for x in slide_notes]
    site("done", id=lecture_id, key=job["output_key"], captions_key=captions_key,
         duration_seconds=round(polish.duration(out)), chapters=chapters, review=review, slide_notes=slide_notes)
    print(f"Uploaded the polished video ({out.stat().st_size / 1e6:.0f} MB)", flush=True)


if __name__ == "__main__":
    lid = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        main(lid)
    except BaseException as e:  # noqa: BLE001 (polish.py exits on an ffmpeg error)
        traceback.print_exc()
        note = "ffmpeg couldn't process this video" if isinstance(e, SystemExit) else f"{type(e).__name__}: {e}"[:300]
        try:
            site("failed", id=lid, note=note)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        sys.exit(1)
