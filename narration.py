"""
Speaker notes as a script for the AI voice.

Marks you can put in the PowerPoint notes:
  [pause]        a short beat (0.8 s)
  [pause 2]      a beat of that many seconds
  blank line     a paragraph break (0.7 s)
  *word*         stress: the word gets a small run-up and the sentence is read with more energy

A pronunciation list (one "word = how to say it" per line, # for comments) changes only what the voice
says. The captions keep the notes' own spelling, without the marks.

Standard library only.
"""
import re

SENTENCE_PAUSE = 0.35
PARAGRAPH_PAUSE = 0.70
DEFAULT_PAUSE = 0.80
STRESS_ENERGY = 0.65     # Chatterbox "exaggeration" for a sentence with a stressed word (normal 0.5)

_PAUSE = re.compile(r"\[pause(?:\s+(\d+(?:\.\d+)?))?\]", re.I)


LETTERS = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "ay bee see dee ee eff jee aitch eye jay kay el em en oh pee cue ar ess tee you vee W ex why zed".split()))
LETTERS["W"] = "double you"
ROMAN = {"II": "two", "III": "three"}
_ABBR = re.compile(r"(?<![\w])([A-Z]{2,})(s?)(?![\w])")


def read_lexicon(text):
    """'ceftriaxone = seff try axe own' lines -> {word: spoken form}. Entries with capitals match only that exact case
    (so "US = you ess" leaves "us" alone); lowercase entries match any case."""
    lex = {}
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            word, said = (x.strip() for x in line.split("=", 1))
            if word and said:
                lex[word if word != word.lower() else word.lower()] = said
    return lex


def spell(abbr, plural=""):
    """HIV -> 'aitch eye vee'; STIs -> 'ess tee eyes'."""
    if abbr in ROMAN:
        return ROMAN[abbr]
    names = [LETTERS[c] for c in abbr]
    if plural:
        names[-1] += "es" if names[-1].endswith(("s", "x", "ch")) else "s"
    return " ".join(names)


def _say(text, lex):
    """The text as the voice should read it: pronunciations swapped in, other capital abbreviations spelled out
    letter by letter, stress marks removed. Hyphens inside a swapped-in word are dropped (they make the voice pause)."""
    if lex:
        words = sorted(lex, key=len, reverse=True)
        exact = [w for w in words if w != w.lower()]
        loose = [w for w in words if w == w.lower()]
        def sub(m):
            return lex.get(m.group(1)) or lex[m.group(1).lower()]
        for group, flags in ((exact, 0), (loose, re.I)):
            if group:
                pattern = re.compile(r"(?<![\w-])(" + "|".join(re.escape(w) for w in group) + r")(?![\w-])", flags)
                text = pattern.sub(lambda m: sub(m).replace("-", ""), text)
    text = _ABBR.sub(lambda m: spell(m.group(1), m.group(2)), text)
    text = re.sub(r"\s*\*([^*]+)\*", r", \1", text)   # a comma before a stressed word gives it a small run-up
    text = re.sub(r",(\s*,)+", ",", text)
    return re.sub(r"\s+", " ", text).strip(" ,")


def caption_text(text):
    """The words as students read them: no pause or stress marks."""
    text = _PAUSE.sub(" ", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def recorded_pattern(words):
    """A regex finding the words the lecturer recorded (capitalised ones match exactly, lowercase ones in any case)."""
    exact = sorted((w for w in words if w != w.lower()), key=len, reverse=True)
    loose = sorted((w for w in words if w == w.lower()), key=len, reverse=True)
    alts = [re.escape(w) for w in exact] + ["(?i:" + re.escape(w) + ")" for w in loose]
    return re.compile(r"(?<![\w-])(" + "|".join(alts) + r")(?![\w-])") if alts else None


def segments(sentence, lex, rec=None, recorded=None):
    """
    A sentence as what the voice says, with the recorded words taken out:
      [("tts", spoken_text) | ("clip", word) | ("gap", seconds)]
    """
    if not rec:
        return [("tts", _say(sentence, lex))]
    plain = sentence.replace("*", "")
    segs, at = [], 0
    for m in rec.finditer(plain):
        segs += _text_seg(plain[at:m.start()], lex)
        word = m.group(1)
        key = word if word in recorded else word.lower()
        segs.append(("clip", key if key in recorded else next(k for k in recorded if k.lower() == word.lower())))
        at = m.end()
    segs += _text_seg(plain[at:], lex)
    return segs


def _text_seg(text, lex):
    """Words between recorded ones; a comma next to a recorded word becomes a short gap."""
    if not re.search(r"\w", text):
        return [("gap", 0.18)] if re.search(r"[,;:]", text) else []
    segs = [("tts", _say(text, lex))]
    if re.match(r"\s*[,;:]", text):
        segs.insert(0, ("gap", 0.18))
    if re.search(r"[,;:]\s*$", text):
        segs.append(("gap", 0.18))
    return segs


def plan(notes, lex=None, recorded=None):
    """
    One slide's notes -> what to do, in order:
      ("say", [segments], caption_text, energy)  or  ("pause", seconds)
    recorded: the words the lecturer recorded in their own voice (played instead of the AI voice).
    """
    rec = recorded_pattern(recorded or ())
    steps = []
    paragraphs = [p for p in re.split(r"\n\s*\n", notes or "") if p.strip()]
    for pi, para in enumerate(paragraphs):
        if pi:
            steps.append(("pause", PARAGRAPH_PAUSE))
        # pauses split the paragraph; each piece is read sentence by sentence
        pieces = _PAUSE.split(para.replace("\n", " "))
        for k, piece in enumerate(pieces):
            if k % 2 == 1:  # a [pause n] capture group
                steps.append(("pause", float(piece) if piece else DEFAULT_PAUSE))
                continue
            for sent in re.split(r"(?<=[.!?])\s+", piece.strip()):
                if not re.search(r"\w", sent):
                    continue
                energy = STRESS_ENERGY if "*" in sent else 0.5
                steps.append(("say", segments(sent, lex, rec, recorded), caption_text(sent), energy))
                steps.append(("pause", SENTENCE_PAUSE))
    # pauses merge; none at the very end (the slide timing adds its own)
    merged = []
    for s in steps:
        if s[0] == "pause" and merged and merged[-1][0] == "pause":
            merged[-1] = ("pause", max(merged[-1][1], s[1]))
        else:
            merged.append(s)
    while merged and merged[-1][0] == "pause":
        merged.pop()
    return merged
