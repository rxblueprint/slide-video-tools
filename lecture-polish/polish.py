"""
RxBlueprint lecture processor, v2.

Drag an MP4 lecture onto PROCESS_LECTURE.bat. Writes, next to the input:
  <name>_POLISHED.mp4            web-ready video (H.264 + AAC, fast start, -16 LUFS voice)
  <name>_POLISHED.vtt            captions on the final timeline (if faster-whisper is installed)
  <name>_POLISHED.chapters.json  where each slide starts in the final video
  <name>_POLISHED.review.json    restarted sentences cut, and lines where a dose, number or drug name
                                 heard differs from the notes (to listen to)

What it does:
  * Shortens long pauses using the audio itself (ffmpeg silencedetect). Nothing spoken is cut.
    (v1 cut by Whisper's word list on a drifting audio clock, so its cuts landed mid-sentence.)
  * Keeps about a second where the slide changes, and crossfades slides over 0.4 s.
  * Smooths the background noise so the audio doesn't drop to dead silence between phrases,
    then levels the voice to -16 LUFS.
  * Adds a short brand card at the start and an end card, timed so no fade ever touches your
    voice: the first word starts after the title slide is fully on screen, and the ending
    waits until after your last word.
  * No speed-up baked in: the website sets the pace (1.1x as "Normal").
  * Two ffmpeg passes with filter script files, so it fits Windows' command-line limit and
    uses little memory.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # captions.py sits next to this file

# ------------------------------------------------------------------ settings
SILENCE_DB = -45          # quieter than this counts as silence
MIN_PAUSE_TO_TRIM = 0.70  # only pauses longer than this get shortened (seconds)
KEEP_PAUSE = 0.45         # what a shortened pause keeps (seconds)
SLIDE_PAUSE = 1.00        # what a pause keeps when the slide changes inside it (seconds)
SCENE_THRESHOLD = 0.08    # how different a frame must be to count as a slide change
LEAD_IN = 0.80            # silence before the first word (longer than any fade)
END_HOLD = 1.50           # last slide stays up this long after the last word, before the end card
SLIDE_FADE = 0.40         # crossfade between slides (0 turns it off)
NOISE_SMOOTHING = True    # gentle background noise reduction (afftdn)
CLARITY = True            # clearer consonants: small 3 kHz lift + de-esser
LOUDNESS = -16            # LUFS, the usual target for spoken web video
CRF = 22                  # x264 quality; slides compress very well, so this stays small
CAPTIONS = True            # .vtt captions (needs faster-whisper); wording from the notes when the
                          # .pptx or .txt with the same name sits next to the video
WHISPER_MODEL = "base.en"
RETAKES = True            # a restarted sentence loses its first take (needs faster-whisper; only where there's
                          # a pause on both sides, so a cut never lands inside a word)
LISTEN_AGAIN = 6          # after cutting, listen to the edited audio up to this many more times for a missed "sorry"
NOTES = None              # notes file to use instead of looking next to the video

BRAND_CARDS = True        # intro and end cards (needs Pillow: py -m pip install pillow)
INTRO_LEN = 2.5           # seconds the brand card is on screen
INTRO_XFADE = 0.6         # brand card fades into the title slide over this long
OUTRO_LEN = 5.0           # seconds the end card is on screen
OUTRO_XFADE = 1.0
LOGO = HERE / "logo-white.png"
NAVY, TEAL, MINT = (11, 65, 95), (0, 151, 136), (95, 248, 227)
# Optional "Up next" line on the end card, by lecture file name (without .mp4):
UP_NEXT = {
    # "RxBlueprint-Sexually-Transmitted-Infections-Deck": "Surgical Prophylaxis",
}
W, H, FPS = 1280, 720, 30


def run(cmd, **kw):
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw)
    if res.returncode != 0:
        print("ffmpeg failed:\n" + res.stderr[-3000:])
        sys.exit(1)
    return res


def duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", str(path)]).stdout
    return float(out.strip())


def frame_rate(path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
               "-of", "default=nw=1:nk=1", str(path)]).stdout.strip()
    a, _, b = out.partition("/")
    return float(a) / float(b or 1) if float(a or 0) else 30.0


def snap(spans, fps):
    """Span edges moved to the middle between two frames, so the picture keeps exactly as long as the sound."""
    out = []
    for s, e in spans:
        a, b = (max(0.0, (round(s * fps) - 0.5) / fps), (round(e * fps) - 0.5) / fps)
        if b > a:
            out.append([a, b])
    return out


def cut_audio(src, spans, out, sr=48000):
    """The kept parts of the audio, cut to the sample (ffmpeg's aselect works in whole audio frames, and over
    hundreds of cuts the sound ends up a second or more ahead of the picture)."""
    import wave
    full = out.with_name("full.wav")
    # aresample=async keeps the audio on the video's clock; screen recorders drift ~1 s per 20 min
    run(["ffmpeg", "-nostdin", "-y", "-i", str(src), "-vn", "-af", "aresample=async=1:first_pts=0",
         "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", str(full)])
    with wave.open(str(full), "rb") as r, wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        n = r.getnframes()
        for s, e in spans:
            a, b = min(n, round(s * sr)), min(n, round(e * sr))
            r.setpos(a)
            data = r.readframes(b - a)
            w.writeframes(data + b"\0\0" * (round(e * sr) - round(s * sr) - (b - a)))   # past the end: silence
    full.unlink()


NUM = r"(-?[\d.]+(?:e[-+]?\d+)?)"   # ffmpeg can print 2.3e-05


def silences(path, min_len):
    """(start, end) of every silence at least min_len long; one that runs to the end gets end=None."""
    err = run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path), "-vn",
               "-af", f"silencedetect=noise={SILENCE_DB}dB:d={min_len}", "-f", "null", "-"]).stderr
    starts = [max(0.0, float(x)) for x in re.findall(r"silence_start: " + NUM, err)]
    ends = [float(x) for x in re.findall(r"silence_end: " + NUM, err)]
    return [(s, ends[i] if i < len(ends) else None) for i, s in enumerate(starts)]


def speech_bounds(path, total):
    """When the first word starts and the last word ends."""
    sil = silences(path, 0.15)
    first = sil[0][1] if sil and sil[0][0] <= 0.05 and sil[0][1] else 0.0
    last = total
    if sil:
        s, e = sil[-1]
        if e is None or e >= total - 0.05:
            last = s
    return first, last


def find_slide_changes(path, exact=False):
    """Times the slide changes. exact=True checks every frame (used on the edited video for the crossfades)."""
    vf = "scale=320:180," + ("" if exact else "fps=5,") + f"select='gt(scene,{SCENE_THRESHOLD})',showinfo"
    err = run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path), "-an",
               "-vf", vf,
               "-f", "null", "-"]).stderr
    out = []
    for t in (float(x) for x in re.findall(r"pts_time:([\d.]+)", err)):
        if not out or t - out[-1] > 2.0:   # one slide change, not each frame of an animation
            out.append(t)
    return out


def plan_keep_spans(total, pauses, slides, first, last):
    """Everything from just before the first word to just after the last word, minus the middle of long pauses."""
    start, stop = max(0.0, first - LEAD_IN), last + END_HOLD
    keep, cursor = [], start
    for s, e in pauses:
        if e is None or s <= first or e >= last:
            continue                                 # lead-in and ending are handled above
        has_slide = any(s <= t <= e for t in slides)
        want = SLIDE_PAUSE if has_slide else KEEP_PAUSE
        if e - s <= want:
            continue
        cut = (e - s) - want                         # how much of this pause to remove
        if has_slide:
            # end the cut right where the slide changes, so the new slide gets the kept pause
            t = next(t for t in slides if s <= t <= e)
            b = min(max(t, s + 0.15 + cut), e - 0.15)
            a = b - cut
        else:
            a = s + want / 2
            b = a + cut
        keep.append([cursor, a])
        cursor = b
    keep.append([cursor, min(total, stop)])
    pad_end = max(0.0, stop - total)                 # recording ends too soon after the last word
    return [k for k in keep if k[1] - k[0] > 0.02], pad_end


def to_edited_time(t, spans):
    acc = 0.0
    for s, e in spans:
        if t < s:
            return acc
        if t <= e:
            return acc + (t - s)
        acc += e - s
    return acc


def to_source_time(t, spans):
    """A time on the edited timeline, back on the recording's timeline."""
    acc = 0.0
    for s, e in spans:
        if t <= acc + (e - s):
            return s + (t - acc)
        acc += e - s
    return spans[-1][1] if spans else t


def keep_to_slide(cuts, slides):
    """
    Cuts never cross a slide change: that would cut the slide, and it's more likely a new point. A "sorry"
    restart is the exception: the lecturer flagged it, so it is cut from the slide change on.
    """
    import script_check
    out = []
    for a, b, said in cuts:
        inside = [t for t in slides if a < t < b]
        if not inside:
            out.append((a, b, said))
        elif script_check.is_sorry(said) and b - inside[-1] > 0.3:
            out.append((round(inside[-1] + 0.05, 3), b, said))
    return out


def nice_title(src):
    t = re.sub(r"^RxBlueprint-|-Deck$", "", src.stem)
    t = t.replace("-", " ")
    return re.sub(r"\bCDiff\b", "C. diff", t)


def make_cards(src, work):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("      (Pillow not installed, skipping brand cards: py -m pip install pillow)")
        return None

    def font(bold, size):
        names = (["segoeuib.ttf", "Carlito-Bold.ttf", "DejaVuSans-Bold.ttf"] if bold
                 else ["segoeui.ttf", "Carlito-Regular.ttf", "DejaVuSans.ttf"])
        for n in names:
            for d in ["C:/Windows/Fonts", "/usr/share/fonts/truetype/crosextra", "/usr/share/fonts/truetype/dejavu"]:
                p = Path(d) / n
                if p.exists():
                    return ImageFont.truetype(str(p), size)
        return ImageFont.load_default()

    def base():
        im = Image.new("RGB", (W, H), NAVY)
        d = ImageDraw.Draw(im)
        for x in range(0, W, 40):
            d.line([(x, 0), (x, H)], fill=(18, 76, 108))
        for y in range(0, H, 40):
            d.line([(0, y), (W, y)], fill=(18, 76, 108))
        return im, d

    def paste_logo(im, width, xy):
        if LOGO.exists():
            lg = Image.open(LOGO).convert("RGBA")
            lg = lg.resize((width, int(width * lg.size[1] / lg.size[0])), Image.LANCZOS)
            im.paste(lg, (xy[0] if xy[0] >= 0 else (W - width) // 2, xy[1]), lg)

    exam = src.parent.parent.name.split()[0].upper() if src.parent.parent.name else ""
    module = src.parent.name.upper()
    title = nice_title(src)

    im, d = base()
    paste_logo(im, 560, (-1, 250))
    tag = "  ·  ".join(x for x in [exam, module] if x)
    f = font(True, 26)
    d.text(((W - d.textlength(tag, font=f)) // 2, 440), tag, font=f, fill=MINT)
    d.rectangle([(W // 2 - 60, 488), (W // 2 + 60, 492)], fill=TEAL)
    intro = work / "intro.png"
    im.save(intro)

    im, d = base()
    paste_logo(im, 300, (80, 70))
    d.text((80, 210), "YOU FINISHED", font=font(True, 24), fill=MINT)
    size = 58
    while size > 30 and d.textlength(title, font=font(True, size)) > W - 160:  # long titles shrink to fit
        size -= 2
    d.text((80, 248), title, font=font(True, size), fill=(255, 255, 255))
    d.rectangle([(80, 345), (200, 349)], fill=TEAL)
    y = 390
    nxt = UP_NEXT.get(src.stem)
    if nxt:
        d.text((80, y), "UP NEXT", font=font(True, 24), fill=MINT)
        size = 40
        while size > 24 and d.textlength(nxt, font=font(True, size)) > W - 160:
            size -= 2
        d.text((80, y + 36), nxt, font=font(True, size), fill=(255, 255, 255))
        y += 130
    d.text((80, y), "Questions about this lecture? Ask them right under the video.",
           font=font(False, 28), fill=(200, 222, 232))
    outro = work / "outro.png"
    im.save(outro)
    return intro, outro


SORRY_PROMPT = "Um, so, sorry, uh, I mean, sorry, let me restart."   # makes Whisper write the "sorry"s it tidies away
HUNT_PIECE, HUNT_OVERLAP = 30.0, 3.0               # seconds: the pieces it listens to for them, overlapping


def transcribe(audio_path, notes, vad=True, hunt=False):
    """Whisper's words [(text, start, end)] on the recording's timeline, or None without faster-whisper.
    hunt=True listens for "sorry": Whisper tends to leave a stammered "sorry" out of a tidy transcript, so it's
    told to expect them, and a "sorry" it then only imagines (in silence, unsure, or "I'm sorry, I'm sorry"
    repeated on and on) is dropped."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("      (faster-whisper not installed, skipping captions: py -m pip install faster-whisper)")
        return None
    import captions
    # the notes' drug names help Whisper hear them right
    names = sorted(captions._vocabulary(notes.split()).values(), key=len, reverse=True)[:60]
    model = WhisperModel(WHISPER_MODEL, device="auto", compute_type="auto")
    # read the 16 kHz WAV ourselves: some PyAV versions break faster-whisper's own decoder
    import wave
    import numpy as np
    with wave.open(str(audio_path), "rb") as wf:
        audio = np.frombuffer(wf.readframes(wf.getnframes()), np.int16).astype(np.float32) / 32768.0
    prompt = ("Drug names: " + ", ".join(names) + ".") if names else ""
    if not hunt:
        # condition_on_previous_text off: on long recordings it can repeat itself, which would look like a retake
        segments, _ = model.transcribe(audio, language="en", vad_filter=vad, word_timestamps=True,
                                       condition_on_previous_text=False, initial_prompt=prompt or None)
        return [(w.word, w.start, w.end) for seg in segments for w in (seg.words or [])]
    # the hint only reaches the first 30 s of a long transcription, so listen in short pieces, each with it
    words, t, total = [], 0.0, len(audio) / 16000
    while t < total:
        stop = min(total, t + HUNT_PIECE)
        segments, _ = model.transcribe(audio[int(t * 16000):int(stop * 16000)], language="en", vad_filter=vad,
                                       word_timestamps=True, condition_on_previous_text=False,
                                       initial_prompt=SORRY_PROMPT)   # the drug names crowd out the hint
        lo, hi = (t + HUNT_OVERLAP / 2 if t else 0.0), (stop - HUNT_OVERLAP / 2 if stop < total else total)
        words += [(w.word, t + w.start, t + w.end, w.probability) for seg in segments for w in (seg.words or [])
                  if lo <= t + (w.start + w.end) / 2 < hi]   # each moment from one piece only
        if stop >= total:
            break
        t = stop - HUNT_OVERLAP
    if hunt:
        import script_check
        keys = [script_check._k(w) for w, _, _, _ in words]

        def loud(s, e):
            x = audio[int(s * 16000):max(int(e * 16000), int(s * 16000) + 1600)]
            return len(x) and 20 * np.log10(float(np.sqrt((x ** 2).mean())) + 1e-9) > -40

        def imagined(n):
            run = sum(1 for m in range(max(0, n - 6), min(len(words), n + 7)) if keys[m] in script_check.SORRY)
            return words[n][3] < 0.1 or run >= 3 or not loud(words[n][1], words[n][2])
        words = [w for n, w in enumerate(words) if not (keys[n] in script_check.SORRY and imagined(n))]
    return [(w, s, e) for w, s, e, _ in words]


def write_vtt(words, spans, offset, out_path, notes):
    """Captions on the final timeline: what was said, with drug names spelled as in the notes (.pptx or .txt)."""
    import captions
    cue_list = []
    # word for word what was said (the lecturer often leaves the script); the notes only spell the names
    for a, b, text in captions.cues(captions.align(words, notes, verbatim=True)):
        a, b = offset + to_edited_time(a, spans), offset + to_edited_time(b, spans)
        if b - a >= 0.2:
            cue_list.append((a, b, text))
    Path(out_path).write_text(captions.to_vtt(cue_list), encoding="utf-8")


def remove_ranges(spans, cuts):
    """The kept spans without the cut ranges [(start, end, ...)]."""
    out = []
    for s, e in spans:
        parts = [[s, e]]
        for a, b, *_ in cuts:
            nxt = []
            for x, y in parts:
                if b <= x or a >= y:
                    nxt.append([x, y])
                    continue
                if a > x:
                    nxt.append([x, a])
                if b < y:
                    nxt.append([b, y])
            parts = nxt
        out += [p for p in parts if p[1] - p[0] > 0.02]
    return out


def make_topics(words, notes_file, spans, offset, changes, title):
    """
    The topics list: each slide's header and when it starts, from the PowerPoint. A slide starts when its notes
    start being spoken (snapped to the slide change just before, when there is one); slide 1 is the title slide.
    Without the speech, the slide changes are used, if there is one per slide.
    """
    if not notes_file:
        return None
    import captions
    titles = captions.slide_titles(str(notes_file))
    if not titles:
        return None
    if words:
        heard = captions.slide_starts(words, captions.slide_notes(str(notes_file)))
        starts = []
        for t in heard:
            if t is None:
                starts.append(None)
                continue
            e = to_edited_time(t, spans)
            near = [c for c in changes if e - 4.0 <= c <= e + 0.5]
            starts.append(offset + (max(near) if near else e))
    elif len(changes) + 1 == len(titles):
        starts = [offset] + [offset + c for c in changes]
    else:
        return None
    topics, last = [], -1.0
    for i, (name, t) in enumerate(zip(titles, starts)):
        if i == 0:
            topics.append({"slide": 1, "start": 0.0, "title": f"Title Slide: {title}"})
            last = 0.0
        elif t is not None and t > last + 1.0:
            topics.append({"slide": i + 1, "start": round(t, 2), "title": name or f"Slide {i + 1}"})
            last = t
    return topics


def find_notes(src):
    """The lecture's notes next to the video: same name, .pptx or .txt."""
    for ext in (".pptx", ".txt"):
        p = src.with_suffix(ext)
        if p.exists():
            return p
    return None


def integrated_loudness(path):
    """Integrated loudness (LUFS) of a file's audio."""
    r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path), "-map", "0:a",
                        "-af", "ebur128", "-f", "null", "-"], capture_output=True, text=True)
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)
    return float(m[-1]) if m else LOUDNESS


def process(src):
    src = Path(src).resolve()
    if not src.exists():
        print(f"File not found: {src}")
        return
    out_mp4 = src.with_name(f"{src.stem}_POLISHED.mp4")
    out_vtt = src.with_name(f"{src.stem}_POLISHED.vtt")
    out_chapters = src.with_name(f"{src.stem}_POLISHED.chapters.json")
    out_review = src.with_name(f"{src.stem}_POLISHED.review.json")
    work = Path(tempfile.mkdtemp(prefix="rx_lecture_"))
    try:
        total = duration(src)
        print(f"\n Processing {src.name} ({total / 60:.1f} min)\n")

        print("[1/5] Finding speech, pauses and slide changes...")
        first, last = speech_bounds(src, total)
        pauses = silences(src, MIN_PAUSE_TO_TRIM)
        slides = find_slide_changes(src)
        spans, pad_end = plan_keep_spans(total, pauses, slides, first, last)
        edited = sum(e - s for s, e in spans) + pad_end
        print(f"      {len(spans) - 1} pauses to shorten, {len(slides)} slide changes; "
              f"{total / 60:.1f} -> {edited / 60:.1f} min")

        # the speech, before cutting: for the captions and topics, restarted sentences and the script check
        notes_file = NOTES or find_notes(src)
        notes = ""
        if notes_file:
            import captions
            notes = captions.read_notes(notes_file)
            print(f"      using the notes in {Path(notes_file).name} for the wording")
        words, cuts, checks, lost = None, [], [], []
        if CAPTIONS:
            print("      listening to the speech (Whisper)...")
            wav = work / "voice.wav"
            # aresample=async keeps the audio on the video's clock; screen recorders drift ~1 s per 20 min
            run(["ffmpeg", "-nostdin", "-y", "-i", str(src), "-vn", "-af", "aresample=async=1:first_pts=0",
                 "-ac", "1", "-ar", "16000", str(wav)])
            words = transcribe(wav, notes)
        if words:
            import script_check
            # a long silence after the last word, then a click as the recording stops, would keep the last
            # slide up in silence: end the video just after the last word instead
            said_last = max(e for _, _, e in words)
            if any(said_last - 1.0 <= s <= said_last + 1.0 and e and e - s >= 3.0 and e > spans[-1][1] - 3.0
                   for s, e in pauses):
                stop = said_last + END_HOLD
                print(f"      silent ending trimmed: {spans[-1][1] - stop:.0f} s")
                spans = [[s, min(e, stop)] for s, e in spans if s < stop]
                pad_end = 0.0
            words_heard = list(words)
            if RETAKES:
                cuts = script_check.retakes(words, silences(src, script_check.MIN_GAP))
                cuts = keep_to_slide(cuts, slides)
                spans = remove_ranges(spans, cuts)
                words = script_check.drop_cut(words, cuts)
                # Whisper misses some "sorry"s in a long recording (its speech filter drops a short word after
                # a pause). Listen again to the edited audio without that filter, until nothing new turns up.
                for n in range(LISTEN_AGAIN):
                    print(f"      listening again ({n + 1}) for any \"sorry\" it missed...")
                    again = work / "again.wav"
                    cut_audio(src, spans, again, sr=16000)
                    gaps = silences(again, script_check.MIN_GAP)
                    # two listens: a plain one and one primed for "sorry". Each catches some the other misses
                    plain = transcribe(again, notes, vad=False) or []
                    more = script_check.sorry_cuts(plain, gaps)
                    for c in script_check.sorry_cuts(transcribe(again, notes, vad=False, hunt=True) or [], gaps):
                        if any(c[0] < b and a < c[1] for a, b, _ in more):
                            continue
                        # primed for "sorry", Whisper can also imagine one over real teaching: keep the cut only
                        # when the plain listen agrees it isn't lost
                        if script_check.imagined_sorry(c, plain):
                            print(f"      not cut at {c[0]:.0f} s: a \"sorry\" only the primed listen heard, over words said once")
                            continue
                        more.append(c)
                    more.sort()
                    more = [(to_source_time(a, spans), to_source_time(b, spans), said) for a, b, said in more]
                    more = keep_to_slide([c for c in more if c[1] > c[0]], slides)
                    print(f"      {len(more)} more found")
                    if not more:
                        break
                    cuts = sorted(cuts + more)
                    spans = remove_ranges(spans, more)
                    words = script_check.drop_cut(words, more)
                if cuts:
                    print(f"      {len(cuts)} restarted sentences: first takes cut ({sum(b - a for a, b, _ in cuts):.0f} s)")
            if notes:
                lost = script_check.lost_in_cuts(words_heard, cuts, notes)
                for a, said, gone in lost:
                    print(f"      check the cut at {a:.0f} s: it removes words said nowhere else nearby")
                checks = script_check.checks(words, notes)
                print(f"      {len(checks)} lines to listen to (a number or drug name differs from the notes)")

        print("[2/5] Cutting pauses and cleaning the audio...")
        src_fps = frame_rate(src)
        spans = snap(spans, src_fps)
        between = "+".join(f"between(t,{s:.5f},{e:.5f})" for s, e in spans)
        # fps= first: screen recordings are often variable frame rate (30.3 frames a second, labelled 30), and
        # renumbering their frames at 30 would let the picture fall behind the voice (20 s by minute 35)
        v = (f"[0:v]fps={src_fps:.6f},select='{between}',setpts=N/({src_fps:.6f}*TB),fps={FPS},"
             f"scale={W}:{H},format=yuv420p")
        cut_audio(src, spans, work / "cut.wav")
        a = "[1:a]highpass=f=80"
        if NOISE_SMOOTHING:
            a += ",afftdn=nr=12:nf=-60:tn=1"
        if CLARITY:
            # lift the 3 kHz "presence" band where consonants live, then tame the harsh S sounds it brings out
            a += ",equalizer=f=3000:t=q:w=1.2:g=3,equalizer=f=200:t=q:w=1:g=-1.5,deesser=i=0.4:m=0.5:f=0.5"
        a += (",acompressor=threshold=-20dB:ratio=2.5:attack=5:release=80,"
              f"loudnorm=I={LOUDNESS}:TP=-1.5:LRA=11,aresample=48000")
        if pad_end > 0:
            v += f",tpad=stop_mode=clone:stop_duration={pad_end:.3f}"
            a += f",apad=pad_dur={pad_end:.3f}"
        (work / "edit.txt").write_text(v + "[v];\n" + a + "[a]", encoding="utf-8")
        edit = work / "edit.mov"
        run(["ffmpeg", "-nostdin", "-y", "-i", str(src), "-i", str(work / "cut.wav"), "-filter_complex_script", str(work / "edit.txt"),
             "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
             "-c:a", "pcm_s16le", "-ac", "1", str(edit)])
        edited = duration(edit)

        print("[3/5] Adding slide crossfades and brand cards...")
        cards = make_cards(src, work) if BRAND_CARDS else None
        pre = (INTRO_LEN - INTRO_XFADE) if cards else 0.0      # lecture starts this far into the video
        post = (OUTRO_LEN - OUTRO_XFADE) if cards else 0.0
        changes = [t for t in find_slide_changes(edit, exact=True) if 1.0 < t < edited - 1.0]

        inputs = ["-i", str(edit)]
        chains, n = [], 1
        vbase = f"[0:v]tpad=start_duration={pre:.3f}:start_mode=add:color=black"
        vbase += f",tpad=stop_mode=clone:stop_duration={post + (OUTRO_XFADE if cards else 0):.3f}" if cards else ""
        chains.append(vbase + "[b0]")
        cur = "b0"
        if SLIDE_FADE > 0:
            for k, t in enumerate(changes):
                still = work / f"still{k}.png"
                run(["ffmpeg", "-nostdin", "-y", "-ss", f"{max(0, t - 1.5 / FPS):.3f}", "-i", str(edit),
                     "-frames:v", "1", str(still)])
                inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{SLIDE_FADE:.3f}", "-i", str(still)]
                chains.append(f"[{n}:v]format=yuva420p,fade=t=out:st=0:d={SLIDE_FADE}:alpha=1,"
                              f"setpts=PTS+{pre + t:.3f}/TB[s{k}]")
                chains.append(f"[{cur}][s{k}]overlay=eof_action=pass:format=auto[b{k + 1}]")
                cur, n = f"b{k + 1}", n + 1
        if cards:
            intro, outro = cards
            inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{INTRO_LEN:.3f}", "-i", str(intro)]
            chains.append(f"[{n}:v]format=yuva420p,fade=t=in:st=0:d=0.5,"
                          f"fade=t=out:st={pre:.3f}:d={INTRO_XFADE}:alpha=1[in]")
            chains.append(f"[{cur}][in]overlay=eof_action=pass:format=auto[ci]")
            n += 1
            outro_at = pre + edited
            inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{OUTRO_LEN:.3f}", "-i", str(outro)]
            chains.append(f"[{n}:v]format=yuva420p,fade=t=in:st=0:d={OUTRO_XFADE}:alpha=1,"
                          f"fade=t=out:st={OUTRO_LEN - 0.6:.3f}:d=0.6,setpts=PTS+{outro_at:.3f}/TB[out]")
            chains.append(f"[ci][out]overlay=eof_action=pass:format=auto,format=yuv420p[v]")
            n += 1
        else:
            chains.append(f"[{cur}]fade=t=in:st=0:d=0.5,"
                          f"fade=t=out:st={edited - 0.8:.3f}:d=0.8,format=yuv420p[v]")
        final_len = pre + edited + (OUTRO_LEN if cards else 0)
        # audio is never faded: it already starts and ends in silence
        # one-pass loudnorm lands a few dB quiet, so measure and top it up (limiter guards peaks)
        gain = LOUDNESS - integrated_loudness(edit)
        chains.append(f"[0:a]volume={gain:.2f}dB,alimiter=limit=0.84:level=false,"
                      f"adelay={int(pre * 1000)}:all=1,apad,atrim=0:{final_len:.3f}[a]")
        (work / "polish.txt").write_text(";\n".join(chains), encoding="utf-8")
        tmp_out = work / "out.mp4"
        run(["ffmpeg", "-nostdin", "-y", *inputs, "-filter_complex_script", str(work / "polish.txt"),
             "-map", "[v]", "-map", "[a]", "-t", f"{final_len:.3f}",
             "-c:v", "libx264", "-preset", "medium", "-crf", str(CRF), "-tune", "stillimage",
             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(tmp_out)])
        shutil.move(str(tmp_out), out_mp4)

        print("[4/5] Writing chapters...")
        starts = [pre] + [pre + t for t in changes]
        out_chapters.write_text(json.dumps([{"slide": i + 1, "start": round(s, 2)} for i, s in enumerate(starts)],
                                           indent=1), encoding="utf-8")

        if words is not None:
            print("[5/5] Writing captions...")
            write_vtt(words, spans, pre, out_vtt, notes)
        at = lambda t: round(pre + to_edited_time(t, spans), 2)  # noqa: E731
        out_review.write_text(json.dumps({
            "checks": [{"t": at(t), "said": said, "script": meant} for t, said, meant in checks],
            "cuts": [{"t": at(a), "seconds": round(b - a, 2), "said": said[:300]} for a, b, said in cuts],
            "lost": [{"t": at(a), "said": said[:300], "words": ", ".join(gone)[:300]} for a, said, gone in lost],
        }, indent=1), encoding="utf-8")

        topics = make_topics(words or [], notes_file, spans, pre, changes, nice_title(src))
        if topics:
            print(f"      topics: {len(topics)} slides, titled from {Path(notes_file).name}")
            out_chapters.write_text(json.dumps(topics, indent=1), encoding="utf-8")

        got = duration(out_mp4)
        print(f"\n Done: {out_mp4.name}  {total / 60:.1f} -> {got / 60:.1f} min "
              f"({len(spans) - 1} pauses shortened, {len(changes)} slide crossfades, nothing spoken removed)\n")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        process(sys.argv[1])
    else:
        print("Drag and drop a lecture MP4 onto PROCESS_LECTURE.bat.")
