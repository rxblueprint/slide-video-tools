"""
Captions that read like the speaker notes.

Whisper hears the lecture and gives each spoken word a time. The lecturer reads from the PowerPoint
notes, so wherever the speech follows the notes, the caption uses the notes' own words (drug names,
spelling, punctuation) on Whisper's timing. Where the lecturer goes off-script, Whisper's words stay,
with any misheard drug name swapped for the closest word from the notes.

Standard library only.
"""
import difflib
import re
import zipfile
from pathlib import PurePosixPath

MAX_LINE = 42           # characters per caption line (two lines at most)
MAX_CUE_SECONDS = 6.0
MIN_GAP_BREAK = 0.6     # a pause this long starts a new caption


# ---------- reading the notes ----------

def _xml_text(paragraph_xml):
    parts = re.findall(r"<a:t>([^<]*)</a:t>|<a:br/>", paragraph_xml)
    text = "".join(p if p else " " for p in parts)
    return (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&apos;", "'"))


def notes_from_pptx(path):
    """Speaker notes of each slide, in slide order."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        rels = z.read("ppt/_rels/presentation.xml.rels").decode("utf-8")
        target = dict(re.findall(r'Id="([^"]+)"[^>]*?Target="([^"]+)"', rels))
        target.update({k: v for v, k in re.findall(r'Target="([^"]+)"[^>]*?Id="([^"]+)"', rels)})
        pres = z.read("ppt/presentation.xml").decode("utf-8")
        slides = [target[r] for r in re.findall(r'<p:sldId [^>]*r:id="([^"]+)"', pres) if r in target]
        out = []
        for s in slides:
            slide = str(PurePosixPath("ppt") / s.lstrip("/").replace("ppt/", "", 1))
            rel = str(PurePosixPath(slide).parent / "_rels" / (PurePosixPath(slide).name + ".rels"))
            notes = ""
            if rel in names:
                m = re.search(r'Target="([^"]*notesSlide[^"]*)"', z.read(rel).decode("utf-8"))
                if m:
                    p = str(PurePosixPath(slide).parent / m.group(1))
                    p = re.sub(r"[^/]+/\.\./", "", p)
                    if p in names:
                        xml = z.read(p).decode("utf-8")
                        bodies = [sp for sp in re.findall(r"<p:sp>.*?</p:sp>", xml, re.S) if 'type="body"' in sp]
                        paras = [_xml_text(p) for b in bodies for p in re.findall(r"<a:p>.*?</a:p>|<a:p/>", b, re.S)]
                        notes = "\n".join(x.strip() for x in paras if x.strip())
            out.append(notes)
        return out


_TXT_SLIDE = re.compile(r"^\s*##\s*(.*)$", re.M)


def _txt_slides(path):
    """A .txt script split at its "## Slide title" lines -> [(title, notes)]; none if it has no such lines."""
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    heads = list(_TXT_SLIDE.finditer(text))
    if not heads:
        return [], text
    return [(h.group(1).strip(), text[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)].strip())
            for i, h in enumerate(heads)], text


_DIRECTION = re.compile(r"\s*\[(?!pause\b)[^\]]*\]", re.I)   # stage directions such as [Click]


def _clean(text):
    """Notes as spoken: no stage directions, pause marks or stress stars."""
    text = _DIRECTION.sub(" ", text)
    text = re.sub(r"\[pause[^\]]*\]", " ", text, flags=re.I)
    return re.sub(r"(?<![\w*])\*([^*\s][^*]*?)\*(?![\w*])", r"\1", text)


def slide_notes(path):
    """Each slide's notes, from a .pptx, or a .txt with a "## Slide title" line before each slide's notes."""
    path = str(path)
    if path.lower().endswith(".pptx"):
        return [_clean(n) for n in notes_from_pptx(path)]
    return [_clean(n) for _, n in _txt_slides(path)[0]]


def read_notes(path):
    """The whole script as one text, from a .pptx (its speaker notes) or a .txt file."""
    path = str(path)
    if path.lower().endswith(".pptx"):
        return "\n\n".join(_clean(n) for n in notes_from_pptx(path) if n)
    slides, text = _txt_slides(path)
    return _clean("\n\n".join(n for _, n in slides if n) if slides else text)


def slide_titles(path):
    """Each slide's header: its title placeholder, else its biggest text (skipping bare numbers like "01")."""
    if not str(path).lower().endswith(".pptx"):
        return [t for t, _ in _txt_slides(str(path))[0]]
    try:
        from pptx import Presentation
    except ImportError:
        return []
    titles = []
    for slide in Presentation(path).slides:
        title = slide.shapes.title.text_frame.text if slide.shapes.title is not None else ""
        if not title.strip():
            best = (0.0, 0, "")
            for sh in slide.shapes:
                if not sh.has_text_frame or sh.is_placeholder and "SLIDE_NUMBER" in str(sh.placeholder_format.type):
                    continue
                text = sh.text_frame.text.strip()
                if len(text) < 3 or not re.search(r"[A-Za-z]{3}", text):
                    continue
                sizes = [r.font.size.pt for para in sh.text_frame.paragraphs for r in para.runs if r.font.size]
                size = max(sizes) if sizes else 0.0
                top = sh.top or 0
                if (size, -top) > (best[0], -best[1]):
                    best = (size, top, text)
            title = best[2]
        titles.append(re.sub(r"\s+", " ", title).strip()[:200])
    return titles


# ---------- lining the notes up with the speech ----------

def _key(word):
    return re.sub(r"[^a-z0-9]", "", word.lower())


def _vocabulary(note_tokens):
    """Notes words worth fixing in Whisper's output: long or capitalised ones (mostly names)."""
    vocab = {}
    for w in note_tokens:
        k = _key(w)
        if len(k) >= 6 or (len(k) >= 4 and w[:1].isupper()):
            vocab.setdefault(k, re.sub(r"^[^\w]+|[^\w]+$", "", w))
    return vocab


def _fix_word(word, vocab, keys):
    k = _key(word)
    if len(k) < 5 or k in vocab:
        return word
    m = difflib.get_close_matches(k, keys, n=1, cutoff=0.8)
    if not m:
        return word
    core = re.sub(r"^[^\w]+|[^\w]+$", "", word)
    return word.replace(core, vocab[m[0]]) if core else word


def align(words, notes):
    """
    words: Whisper's [(text, start, end)], in order. notes: the script as text.
    Returns [(text, start, end)] for the captions.
    """
    heard = [(w.strip(), s, e) for w, s, e in words if w.strip()]
    if not notes or not notes.strip():
        return heard
    script = notes.split()
    hk = [_key(w) for w, _, _ in heard]
    nk = [_key(w) for w in script]
    vocab = _vocabulary(script)
    keys = list(vocab)
    out = []
    sm = difflib.SequenceMatcher(None, hk, nk, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            out += [(script[j1 + k], heard[i1 + k][1], heard[i1 + k][2]) for k in range(i2 - i1)]
        elif op == "replace" and (i2 - i1) <= 8 and (j2 - j1) <= 2 * (i2 - i1) + 2:
            # the same few words, heard differently ("50" for "fifty", a mangled drug name): use the notes
            s, e = heard[i1][1], heard[i2 - 1][2]
            n = j2 - j1
            out += [(script[j1 + k], s + (e - s) * k / n, s + (e - s) * (k + 1) / n) for k in range(n)]
        elif op in ("replace", "delete"):
            # said but not in the notes: keep what was said, with names spelled as in the notes
            out += [(_fix_word(w, vocab, keys), s, e) for w, s, e in heard[i1:i2]]
        # "insert": in the notes but not said, so not captioned
    return _fix_case(out, script)


def _fix_case(tokens, script):
    """A notes sentence that now follows spoken words mid-sentence ("And so Let's") loses its capital."""
    lower = {w.lower() for w in script if w[:1].islower()}
    fixed = []
    for k, (w, s, e) in enumerate(tokens):
        prev = fixed[-1][0] if fixed else "."
        if (w[:1].isupper() and not re.search(r"[.?!:]$", prev) and w.lower() in lower
                and not re.fullmatch(r"I('[a-z]+)?", w)):
            w = w[0].lower() + w[1:]
        fixed.append((w, s, e))
    return fixed


# ---------- captions ----------

def cues(tokens):
    """Groups words into captions of up to two lines, breaking at sentence ends and pauses."""
    result, cur = [], []

    def flush():
        if cur:
            result.append((cur[0][1], cur[-1][2], _two_lines(" ".join(w for w, _, _ in cur))))
            cur.clear()

    for tok in tokens:
        if cur:
            text = " ".join(w for w, _, _ in cur)
            if (len(text) + 1 + len(tok[0]) > 2 * MAX_LINE or tok[2] - cur[0][1] > MAX_CUE_SECONDS
                    or tok[1] - cur[-1][2] > MIN_GAP_BREAK
                    or (re.search(r"[.?!]$", cur[-1][0]) and len(text) > MAX_LINE * 0.6)):
                flush()
        cur.append(tok)
    flush()
    return result


def _two_lines(text):
    if len(text) <= MAX_LINE:
        return text
    words = text.split()
    best, best_cost = text, 1e9
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        cost = abs(len(a) - len(b)) + (0 if re.search(r"[,;:]$", a) else 6)
        if max(len(a), len(b)) <= MAX_LINE + 6 and cost < best_cost:
            best, best_cost = f"{a}\n{b}", cost
    return best


def stamp(x):
    h, r = divmod(max(0.0, x), 3600)
    m, s = divmod(r, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def to_vtt(cue_list):
    lines = ["WEBVTT", ""]
    for n, (a, b, text) in enumerate(cue_list, 1):
        lines += [str(n), f"{stamp(a)} --> {stamp(b)}", text, ""]
    return "\n".join(lines)


def slide_starts(words, slide_notes):
    """
    When each slide's notes start being spoken, from the same alignment as the captions.
    words: Whisper's [(text, start, end)]; slide_notes: one notes text per slide.
    Returns one time (seconds) or None per slide; None when too little of its notes was heard.
    """
    heard = [(w.strip(), s) for w, s, _ in words if w.strip()]
    script, owner = [], []
    for i, notes in enumerate(slide_notes):
        for w in (notes or "").split():
            script.append(_key(w))
            owner.append(i)
    starts = [None] * len(slide_notes)
    hits = [0] * len(slide_notes)
    sm = difflib.SequenceMatcher(None, [_key(w) for w, _ in heard], script, autojunk=False)
    for i, j, n in sm.get_matching_blocks():
        if n < 2:  # a lone matching word can be chance
            continue
        for k in range(n):
            slide = owner[j + k]
            hits[slide] += 1
            if starts[slide] is None:
                starts[slide] = heard[i + k][1]
    return [t if hits[n] >= 3 else None for n, t in enumerate(starts)]
