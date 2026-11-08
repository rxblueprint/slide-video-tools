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


def read_lexicon(text):
    """'ceftriaxone = seff-try-AXE-own' lines -> {lowercase word: spoken form}."""
    lex = {}
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            word, said = (x.strip() for x in line.split("=", 1))
            if word and said:
                lex[word.lower()] = said
    return lex


def _say(text, lex):
    """The text as the voice should read it: pronunciations swapped in, stress marks removed."""
    if lex:
        words = sorted(lex, key=len, reverse=True)
        pattern = re.compile(r"(?<![\w-])(" + "|".join(re.escape(w) for w in words) + r")(?![\w-])", re.I)
        text = pattern.sub(lambda m: lex[m.group(1).lower()], text)
    text = re.sub(r"\s*\*([^*]+)\*", r", \1", text)   # a comma before a stressed word gives it a small run-up
    return re.sub(r"\s+", " ", text).strip(" ,")


def caption_text(text):
    """The words as students read them: no pause or stress marks."""
    text = _PAUSE.sub(" ", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def plan(notes, lex=None):
    """
    One slide's notes -> what to do, in order:
      ("say", spoken_text, caption_text, energy)  or  ("pause", seconds)
    """
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
                steps.append(("say", _say(sent, lex), caption_text(sent), energy))
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
