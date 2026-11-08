"""
A lecture made from a PowerPoint: each slide's speaker notes read in the lecturer's cloned voice
(Chatterbox, MIT licence), over the slides, with captions straight from the notes and the topics list
from the slide titles.

Two steps, so the slow part can run on many machines at once:
  speak:     python narrate.py speak <deck.pptx> <voice.wav> <pronunciations.txt|-> <first> <last> <outdir>
             -> outdir/slide-NNN.wav and slide-NNN.json (when each caption is said) for slides first..last
  assemble:  python narrate.py assemble <deck.pptx> <slides.pdf|-> <audiodir> <out.mp4> "<title>" [exam] [module] [up next]
             -> out.mp4, out.vtt (captions) and out.topics.json

Without a PDF, the slides are drawn with LibreOffice; the PDF exported from PowerPoint looks exactly right.
Needs ffmpeg, pdftoppm (poppler), python-pptx, Pillow; speak also needs chatterbox-tts and torch.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import captions  # noqa: E402
import narration  # noqa: E402

W, H, FPS = 1280, 720, 30
LEAD = 0.6          # slide on screen before the voice starts
TAIL = 0.9          # after the voice ends, before the next slide
FADE = 0.4          # crossfade between slides
INTRO, OUTRO = 2.5, 5.0
LOUDNESS = -16
SR = 24000


def run(cmd):
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {res.stderr[-1500:]}")
    return res


# ---------- speak ----------

def chatterbox(voice_wav):
    import torch
    from chatterbox.tts import ChatterboxTTS
    torch.set_num_threads(max(1, torch.get_num_threads()))
    model = ChatterboxTTS.from_pretrained(device="cuda" if torch.cuda.is_available() else "cpu")
    model.prepare_conditionals(str(voice_wav), exaggeration=0.5)

    def say(text, energy):
        wav = model.generate(text, exaggeration=energy, cfg_weight=0.5)  # voice prepared once above
        return wav.squeeze(0).cpu().numpy(), model.sr
    return say


def write_wav(path, samples, sr):
    import numpy as np
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)


def speak(pptx, voice_wav, lexicon_text, first, last, outdir, say=None):
    import numpy as np
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    notes = captions.notes_from_pptx(str(pptx))
    lex = narration.read_lexicon(lexicon_text)
    say = say or chatterbox(voice_wav)
    for n in range(first, min(last, len(notes)) + 1):
        parts, cues, t, sr = [], [], 0.0, SR
        for step in narration.plan(notes[n - 1], lex):
            if step[0] == "pause":
                parts.append(np.zeros(int(sr * step[1]), dtype=np.float32))
                t += step[1]
            else:
                audio, sr = say(step[1], step[3])
                parts.append(audio.astype(np.float32))
                cues.append([round(t, 3), round(t + len(audio) / sr, 3), step[2]])
                t += len(audio) / sr
        audio = np.concatenate(parts) if parts else np.zeros(int(sr * 1.5), dtype=np.float32)
        write_wav(outdir / f"slide-{n:03d}.wav", audio, sr)
        (outdir / f"slide-{n:03d}.json").write_text(json.dumps({"slide": n, "seconds": round(len(audio) / sr, 3), "cues": cues}))
        print(f"slide {n}: {len(audio) / sr:.1f}s", flush=True)


# ---------- assemble ----------

def slide_images(pptx, pdf, work):
    if not pdf:
        run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(work), str(pptx)])
        pdf = work / (Path(pptx).stem + ".pdf")
    run(["pdftoppm", "-png", "-scale-to-x", str(W), "-scale-to-y", str(H), str(pdf), str(work / "img")])
    return sorted(work.glob("img-*.png"), key=lambda p: int(re.findall(r"\d+", p.stem)[-1]))


def _split_cue(start, end, text):
    """A long sentence becomes captions of up to two lines, timed by their share of the characters."""
    words = text.split()
    chunks, cur = [], []
    for w in words:
        if cur and len(" ".join(cur + [w])) > 2 * captions.MAX_LINE:
            chunks.append(" ".join(cur))
            cur = []
        cur.append(w)
    if cur:
        chunks.append(" ".join(cur))
    total = sum(len(c) for c in chunks) or 1
    out, t = [], start
    for c in chunks:
        d = (end - start) * len(c) / total
        out.append((t, t + d, captions._two_lines(c)))
        t += d
    return out


def assemble(pptx, pdf, audiodir, out_mp4, title, exam="", module="", up_next=None):
    import polish
    audiodir, out_mp4 = Path(audiodir), Path(out_mp4)
    work = Path(tempfile.mkdtemp(prefix="narrate_"))
    try:
        images = slide_images(pptx, pdf, work)
        titles = captions.slide_titles(str(pptx))
        n = min(len(images), len(sorted(audiodir.glob("slide-*.wav"))))
        if n == 0:
            raise RuntimeError("no slides to assemble")

        # brand cards, from the same code as the polished lectures
        fake = work / f"{exam} Lectures" / (module or "Lecture") / "lecture.mp4"
        fake.parent.mkdir(parents=True)
        polish.nice_title = lambda _src: title
        polish.UP_NEXT = {"lecture": up_next} if up_next else {}
        cards = polish.make_cards(fake, work)

        info = [json.loads((audiodir / f"slide-{k:03d}.json").read_text()) for k in range(1, n + 1)]
        durs = [LEAD + i["seconds"] + TAIL for i in info]
        shots = ([(cards[0], INTRO)] if cards else []) + list(zip(images[:n], durs)) + ([(cards[1], OUTRO)] if cards else [])

        # video: stills crossfading into each other; shot k starts at sum(d_j - FADE) for j < k
        inputs, starts, t = [], [], 0.0
        for img, d in shots:
            inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{d:.3f}", "-i", str(img)]
            starts.append(t)
            t += d - FADE
        total = t + FADE
        chains = [f"[{k}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=white,"
                  f"setsar=1,fps={FPS},format=yuv420p,settb=1/{FPS}[s{k}]" for k in range(len(shots))]
        cur = "s0"
        for k in range(1, len(shots)):
            chains.append(f"[{cur}][s{k}]xfade=transition=fade:duration={FADE}:offset={starts[k]:.3f}[x{k}]")
            cur = f"x{k}"
        chains.append(f"[{cur}]fade=t=in:st=0:d=0.5,fade=t=out:st={total - 0.6:.3f}:d=0.6[v]")

        # audio: each slide's narration placed LEAD after its slide appears
        first = 1 if cards else 0
        a_in, a_chains = [], []
        for k in range(n):
            a_in += ["-i", str(audiodir / f"slide-{k + 1:03d}.wav")]
            delay = int((starts[first + k] + LEAD) * 1000)
            a_chains.append(f"[{len(shots) + k}:a]aresample=48000,adelay={delay}:all=1[a{k}]")
        a_chains.append("".join(f"[a{k}]" for k in range(n)) + f"amix=inputs={n}:normalize=0:duration=longest,"
                        f"apad,atrim=0:{total:.3f},highpass=f=70,loudnorm=I={LOUDNESS}:TP=-1.5:LRA=11,aresample=48000[a]")
        script = work / "graph.txt"
        script.write_text(";\n".join(chains + a_chains), encoding="utf-8")
        run(["ffmpeg", "-nostdin", "-y", *inputs, *a_in, "-filter_complex_script", str(script), "-map", "[v]", "-map", "[a]",
             "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-tune", "stillimage",
             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out_mp4)])

        # captions and topics on the same clock
        cue_list = []
        for k, i in enumerate(info):
            base = starts[first + k] + LEAD
            for a, b, text in i["cues"]:
                cue_list += _split_cue(base + a, base + b, text)
        out_mp4.with_suffix(".vtt").write_text(captions.to_vtt(cue_list), encoding="utf-8")
        topics = [{"slide": 1, "start": 0.0, "title": f"Title Slide: {title}"}]
        for k in range(1, n):
            topics.append({"slide": k + 1, "start": round(starts[first + k], 2),
                           "title": (titles[k] if k < len(titles) and titles[k] else f"Slide {k + 1}")})
        out_mp4.with_suffix(".topics.json").write_text(json.dumps(topics, indent=1), encoding="utf-8")
        print(f"Done: {out_mp4.name}, {total / 60:.1f} min, {n} slides, {len(cue_list)} captions")
        return total
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["speak"] and len(a) == 7:
        lex = "" if a[3] == "-" else Path(a[3]).read_text(encoding="utf-8")
        speak(a[1], a[2], lex, int(a[4]), int(a[5]), a[6])
    elif a[:1] == ["assemble"] and len(a) >= 6:
        assemble(a[1], None if a[2] == "-" else a[2], a[3], a[4], a[5], *a[6:9])
    else:
        print(__doc__)
