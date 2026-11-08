"""
Turns a slide deck into a narrated video, for .github/workflows/generate.yml.

  python job.py plan <id>                 -> prints the slide groups for the speak jobs (JSON)
  python job.py speak <id> <first> <last> -> narrates those slides, uploads their audio
  python job.py assemble <id>             -> builds the video, captions and topics, uploads them

The site (SITE_URL, Authorization: Bearer $POLISH_SECRET) hands out short-lived links to the deck,
the voice clip and where results go; nothing is kept here. Signed links are never printed.
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

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SITE = os.environ.get("SITE_URL", "").rstrip("/")
SECRET = os.environ.get("POLISH_SECRET", "")
MAX_GROUPS = 20


def site(action, **body):
    req = urllib.request.Request(
        f"{SITE}/api/lectures/polish", method="POST", data=json.dumps({"action": action, **body}).encode(),
        headers={"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json", "User-Agent": "slide-video-tools"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def curl(*args):
    res = subprocess.run(["curl", "-fsS", "--retry", "5", "--retry-all-errors", "--retry-delay", "5", *args])
    if res.returncode != 0:
        raise RuntimeError(f"{'upload' if '-T' in args else 'download'} failed (curl exit {res.returncode})")


def fetch(url, path):
    curl("-o", str(path), url)
    return path


def deck(job, work):
    pptx = fetch(job["pptx_url"], work / "deck.pptx")
    pdf = fetch(job["pdf_url"], work / "deck.pdf") if job.get("pdf_url") else None
    return pptx, pdf


def plan(lid):
    import captions
    job = site("gen-start", id=lid)
    work = Path(tempfile.mkdtemp())
    pptx, _ = deck(job, work)
    words = [max(1, len(n.split())) for n in captions.notes_from_pptx(str(pptx))]
    # contiguous groups of slides with about the same number of words each
    groups, target, cur, size = [], sum(words) / min(MAX_GROUPS, len(words)), [], 0
    for i, w in enumerate(words, 1):
        cur.append(i)
        size += w
        if size >= target and len(groups) < MAX_GROUPS - 1:
            groups.append([cur[0], cur[-1]])
            cur, size = [], 0
    if cur:
        groups.append([cur[0], cur[-1]])
    print(json.dumps({"groups": groups, "slides": len(words)}))


def speak(lid, first, last):
    import narrate
    job = site("gen-start", id=lid, peek=True)
    work = Path(tempfile.mkdtemp())
    pptx, _ = deck(job, work)
    voice = fetch(job["voice_url"], work / "voice-in")
    wav = work / "voice.wav"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(voice), "-ac", "1", "-ar", "24000", str(wav)], check=True)
    lexicon = fetch(job["lexicon_url"], work / "lex.txt").read_text(encoding="utf-8") if job.get("lexicon_url") else ""
    out = work / "audio"
    narrate.speak(pptx, wav, lexicon, first, last, out)
    urls = site("gen-audio", id=lid, first=first, last=last, mode="put")["urls"]
    for u in urls:
        f = out / u["name"]
        if f.exists():
            curl("-T", str(f), u["url"])


def assemble(lid):
    import narrate
    job = site("gen-start", id=lid, peek=True)
    work = Path(tempfile.mkdtemp())
    pptx, pdf = deck(job, work)
    audio = work / "audio"
    audio.mkdir()
    import captions
    n = len(captions.notes_from_pptx(str(pptx)))
    for u in site("gen-audio", id=lid, first=1, last=n, mode="get")["urls"]:
        fetch(u["url"], audio / u["name"])
    out = work / "lecture.mp4"
    total = narrate.assemble(pptx, pdf, audio, out, job["title"], job["exam"], job["module"], job.get("up_next"))
    curl("-T", str(out), "-H", "Content-Type: video/mp4", job["output_url"])
    curl("-T", str(out.with_suffix(".vtt")), "-H", "Content-Type: text/vtt", job["captions_url"])
    topics = json.loads(out.with_suffix(".topics.json").read_text())
    site("done", id=lid, key=job["output_key"], captions_key=job["captions_key"], duration_seconds=round(total), chapters=topics)
    print(f"Uploaded the lecture ({out.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    mode, lid = (sys.argv[1:3] + ["", ""])[:2]
    try:
        if not re.fullmatch(r"[0-9a-f-]{36}", lid):
            sys.exit("Not a lecture id")
        if mode == "plan":
            plan(lid)
        elif mode == "speak":
            speak(lid, int(sys.argv[3]), int(sys.argv[4]))
        elif mode == "assemble":
            assemble(lid)
        else:
            sys.exit(__doc__)
    except BaseException as e:  # noqa: BLE001
        if isinstance(e, SystemExit) and e.code in (0, None):
            raise
        traceback.print_exc()
        try:
            site("failed", id=lid, note=f"{mode}: {type(e).__name__}: {e}"[:300])
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)
