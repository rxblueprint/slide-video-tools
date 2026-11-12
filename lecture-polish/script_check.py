"""
What the lecturer said, compared with the script (the lecture's notes), for polish.py:

  retakes(words, silences)  restarted sentences: "the first-line drug is... the first-line drug is vancomycin",
                            and corrected ones: "the dose is 500 mg. Sorry. The dose is 1 gram" (a sentence
                            said again with a word or two changed, after "sorry" or a long pause).
                            The abandoned take is cut, but only where there's a real pause on both sides of it,
                            so a cut never lands inside a word.
  checks(words, notes)      lines where a dose, number or drug name heard differs from the script, for the
                            lecturer to listen to before students do. Speech-to-text can mishear, so these are
                            things to check, not errors.

words: Whisper's [(text, start, end)] on the recording's timeline. Pure Python, no ffmpeg or Whisper needed.
"""
import difflib
import re

import captions

# said between an abandoned take and the restart; cut along with the abandoned take
FILLER = {"uh", "um", "er", "erm", "ah", "sorry", "so", "ok", "okay", "actually", "let", "me", "say", "that",
          "again", "i", "mean", "rather", "no", "oops", "wait", "start", "over"}
MIN_TAKE = 2          # words an abandoned take must repeat before it counts (one repeated word can be on purpose)
MAX_TAKE = 30         # words; a longer "repeat" is more likely a recap than a retake
GAP_SEARCH = 0.35     # seconds around a word edge to look for the pause a cut must sit in
MIN_GAP = 0.12        # the pause a cut needs on each side (seconds)
# a corrected sentence: said again mostly the same, starting with the same words, after "sorry" (or another
# filler) or a pause at least this long. Without one of those it's more likely two similar sentences on purpose
# ("Linezolid covers MRSA. Linezolid covers VRE.") and nothing is cut.
REDO_PAUSE = 1.0
REDO_SAME_LONG, REDO_SAME_LIKE = 6, 0.85   # words, and how alike, for a repeat that needs no "sorry"
REDO_WORDS = {"sorry", "oops", "again", "mean", "rather", "no", "wait", "over", "scratch"}   # "so" or "um" aren't enough
REDO_MIN = 3          # words each take needs
REDO_SAME_START = 2   # first words that must match
REDO_LIKE = 0.7       # how alike the two takes must be (0 to 1) after a long pause
REDO_LIKE_SAID = 0.5  # after "sorry" and the like, which also catches a take abandoned halfway

_DRUG = re.compile(
    r"(mycin|micin|cillin|floxacin|oxacin|azole|cycline|penem|cef\w+|ceph\w+|vir|mab|pril|sartan|olol|statin|"
    r"parin|xaban|gatran|azepam|zolam|oxetine|triptan|dipine|tinib|fungin|tegravir|buvir|asvir|gliptin|"
    r"gliflozin|glutide|semide|thiazide|platin|rubicin|mustine|tide|lukast|terol|sonide|olone|asone|"
    r"prazole|tidine|setron|barbital|caine|done|phine|nidazole|fosfomycin|colistin|linezolid|tedizolid|"
    r"daptomycin|vancomycin|aztreonam|tigecycline|nitrofurantoin|trimethoprim|sulfamethoxazole|warfarin|"
    r"digoxin|amiodarone|lithium|insulin|heparin|metformin)$")
_NUM_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
              "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
              "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
              "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
              "once": 1, "twice": 2, "thrice": 3}   # "twice daily" said as "two times a day"
_SCALE = {"hundred": 100, "thousand": 1000, "million": 1000000}
# a number counts as a dose (or other number worth checking) only with one of these just after it, so
# "Two: suppress herpes" or a list number Whisper heard as "1." is never flagged
_UNITS = {"mg", "mcg", "g", "gram", "grams", "milligram", "milligrams", "microgram", "micrograms", "kg", "kgs",
          "kilogram", "kilograms", "ml", "mls", "milliliter", "milliliters", "millilitre", "millilitres", "l", "liter",
          "liters", "litre", "litres", "unit", "units", "iu", "meq", "mmol", "mmhg", "hour", "hours", "h", "hr",
          "hrs", "day", "days", "week", "weeks", "month", "months", "year", "years", "minute", "minutes", "min",
          "percent", "times", "dose", "doses", "tablet", "tablets", "mgkg", "mgdl", "mgday", "mgkgday", "mlmin"}


def _k(w):
    return captions._key(w)


def drug_like(word):
    k = _k(word)
    return len(k) >= 6 and bool(_DRUG.search(k))


def _unit(token):
    k = _k(token)
    tail = [p for p in re.split(r"-", token.lower().strip(".,;:!?()\"'"))[1:] if p]
    if any(p in _UNITS for p in tail):
        return True                               # "eight-week", "31 -year -old"
    return k in _UNITS or token.strip().endswith("%") or bool(re.fullmatch(r"\d+(\.\d+)?(mg|mcg|g|kg|ml|h|hr|units?|meq)\w*", k))


def numbers(tokens, doses_only=False, within=None):
    """
    The numbers in some words: digits ("1,000", "2.5", "q8h" gives 8) and spoken ones ("a hundred and fifty").
    doses_only: just those with a unit within the next three words ("15 to 20 mg/kg" gives 15 and 20).
    within: only numbers that start among the first `within` words (the rest is context for the units).
    """
    out, cur, first, skip = [], None, 0, set()
    tot = grp = 0        # a spoken number: thousands and up so far, and the part under a thousand
    dec = ""             # its digits after "point"

    def keep(value, start, end, extra=0):
        if within is not None and start >= within:
            return
        if not doses_only or any(_unit(t) for t in tokens[end:end + 4 + extra]):
            out.append(value)

    for i, t in enumerate(tokens):
        w = t.lower().strip(".,;:!?()\"'")
        if i in skip:
            continue                              # "000" of "100 ,000", or "million" of "24 million": used
        parts = [p for p in re.split(r"[-\s]", w) if p]
        tail = False
        if len(parts) > 1 and parts[0] in _NUM_WORDS and not all(p in _NUM_WORDS or p in _SCALE for p in parts):
            # "eighty-five-year-old": the number words at the front
            parts = parts[:next(i for i, p in enumerate(parts) if p not in _NUM_WORDS and p not in _SCALE)]
            tail = True
        if cur is not None and not dec and parts == ["and"] and [_k(x) for x in tokens[i + 1:i + 3]] == ["a", "half"]:
            grp += 0.5                            # "seven and a half"
            cur = tot + grp
            skip.update({i + 1, i + 2})
            if re.search(r"[.,;:!?)]$", tokens[i + 2].strip()):
                keep(cur, first, i + 3)
                cur = None
            continue
        numeric = parts and all(p in _NUM_WORDS or p in _SCALE for p in parts)
        # "and" joins only after hundreds or thousands: "a hundred and fifty", not "twenty-seven and thirty-six"
        joining = cur is not None and not dec and grp % 100 == 0 and tot + grp >= 100 and parts in (["and"],)
        scale_next = parts in (["a"],) and i + 1 < len(tokens) and _k(tokens[i + 1]) in _SCALE
        # "one point eight"
        point = (cur is not None and not dec and parts == ["point"] and i + 1 < len(tokens)
                 and _k(tokens[i + 1]) in _NUM_WORDS and _NUM_WORDS[_k(tokens[i + 1])] < 10)
        if numeric or joining or scale_next or point:
            if cur is None:
                first, tot, grp, dec = i, 0, 0, ""
            if point:
                dec = "."
                cur = tot + grp
                continue
            for p in parts:
                if p in _NUM_WORDS:
                    v = _NUM_WORDS[p]
                    if dec:
                        if v >= 10:
                            keep(cur, first, i)
                            first, tot, grp, dec = i, 0, v, ""
                        else:
                            dec += str(v)
                    elif cur is not None and i > first and not (grp % 100 == 0 and (grp or tot) if v >= 10
                                                                  else grp >= 20 and grp % 10 == 0 or grp == 0 and tot):
                        keep(float(tot + grp), first, i)   # "twenty-one five" is two numbers
                        first, tot, grp = i, 0, v
                    else:
                        grp += v
                elif p in _SCALE and dec:
                    tot, grp, dec = float(str(int(tot + grp)) + dec) * _SCALE[p], 0, ""   # "1.2 million"
                elif p in _SCALE:
                    if _SCALE[p] == 100:
                        grp = (grp or 1) * 100
                    else:
                        tot, grp = tot + (grp or 1) * _SCALE[p], 0
                cur = tot + grp
            cur = float(str(int(tot + grp)) + dec) if dec and dec != "." else float(tot + grp)
            if tail or re.search(r"[.,;:!?)]$", t.strip()):
                keep(cur, first, i if tail else i + 1)   # "-year-old" is its unit; a comma ends it
                cur = None
            continue
        if cur is not None:
            keep(cur, first, i)
            cur = None
        found = re.findall(r"\d[\d,]*(?:\.\d+)?", w)
        for n, m in enumerate(found):
            value = float(m.replace(",", ""))
            last = n == len(found) - 1
            nxt = tokens[i + 1].strip() if i + 1 < len(tokens) else ""
            if last and re.fullmatch(r"\.\d+[.,;:]?", nxt) and "." not in m:
                value = float(m.replace(",", "") + "." + re.sub(r"\D", "", nxt))   # Whisper's "0 .15"
                skip.add(i + 1)
                after = _k(tokens[i + 2]) if i + 2 < len(tokens) else ""
                if after in _SCALE:
                    value *= _SCALE[after]                # "1 .2 million"
                    skip.add(i + 2)
            elif last and re.fullmatch(r",\d{3}[.,;:]?", nxt):
                value = value * 1000 + float(nxt[1:4])  # Whisper's "100 ,000"
                skip.add(i + 1)
            elif last and re.fullmatch(r"\.\d+", nxt.rstrip(".,;:")) is None and _k(nxt) in _SCALE:
                value *= _SCALE[_k(nxt)]           # "24 million"
                skip.add(i + 1)
            keep(value, i, i, sum(1 for k in (i + 1, i + 2) if k in skip))   # "95 million" reads past "million"
    if cur is not None:
        keep(cur, first, len(tokens))
    return out


def _same_take(a, b):
    """An abandoned take a (keys) is the start of what follows, b: equal, or the last word cut off ("vanco")."""
    if len(a) > len(b):
        return False
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y and not (i == len(a) - 1 and len(x) >= 3 and y.startswith(x)):
            return False
    return True


def _between(heard, k):
    """The middle of the gap before word k, when there is one (Whisper's word times), else None."""
    if k == 0:
        return max(0.0, heard[0][1] - 0.05)
    a, b = heard[k - 1][2], heard[k][1]
    return (a + b) / 2 if b > a else b             # Whisper's words often touch: cut where the next one starts


def _pause_near(t, sil):
    """The middle of the pause (at least MIN_GAP long) nearest to time t, or None."""
    best = None
    for s, e in sil:
        if e is None or e - s < MIN_GAP:
            continue
        if s - GAP_SEARCH <= t <= e + GAP_SEARCH:
            mid = (s + e) / 2
            if best is None or abs(mid - t) < abs(best - t):
                best = mid
    return best


def _gap_after(heard, k, sil):
    """How long the pause after word k is: the gap between the words, or a silence there (Whisper's word
    times often run into the pause)."""
    if k + 1 >= len(heard):
        return 0.0
    a, b = heard[k][2], heard[k + 1][1]
    found = [e - s for s, e in sil if e is not None and s <= b + GAP_SEARCH and e >= a - GAP_SEARCH
             and heard[k][1] < (s + e) / 2 < heard[k + 1][2]]
    return max([b - a] + found)


def _only_small_lost(a, b):
    """Every word of a that the redo b doesn't keep is a short one (4 letters or fewer)."""
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return all(len(w) <= 4 for op, i1, i2, _, _ in sm.get_opcodes() if op in ("replace", "delete") for w in a[i1:i2])


def _corrected(heard, keys, i, sil):
    """(n, j, signalled): the first n words from i are a take said again, changed, from j; or None.
    signalled is False for a close repeat with no "sorry" or long pause before it: that one isn't cut."""
    clean_start = i == 0 or _gap_after(heard, i - 1, sil) >= MIN_GAP
    best = None
    for n in range(min(MAX_TAKE, len(heard) - i - REDO_MIN), REDO_MIN - 1, -1):
        a = keys[i:i + n]
        if not all(a):
            continue
        j = i + n
        while j < len(heard) and keys[j] in FILLER and j - (i + n) < 6:
            j += 1
        said = any(k in REDO_WORDS for k in keys[i + n:j])
        # a take starts and ends at a pause, unless "sorry" marks it
        if not said and (not clean_start or _gap_after(heard, i + n - 1, sil) < MIN_GAP):
            continue
        signalled = said or _gap_after(heard, i + n - 1, sil) >= REDO_PAUSE
        if j >= len(heard) or keys[j:j + REDO_SAME_START] != a[:REDO_SAME_START]:
            continue
        for m in range(REDO_MIN, min(MAX_TAKE, n + 4, len(heard) - j) + 1):   # the redo can be shorter
            like = difflib.SequenceMatcher(None, a, keys[j:j + m], autojunk=False).ratio()
            if like >= (REDO_LIKE_SAID if said else REDO_LIKE) and (best is None or like > best[0]):
                # a long sentence said again almost word for word is a retake, "sorry" or not, as long as
                # nothing it says is lost: only small words ("it's" / "that's") differ, not "bacteremia" / "pneumonia"
                same = n >= REDO_SAME_LONG and like >= REDO_SAME_LIKE and _only_small_lost(a, keys[j:j + m])
                best = (like, n, j, signalled or same)
        if best and best[3]:
            break                                 # the longest take that has a match
    return best[1:] if best else None


def retakes(words, sil, left=None):
    """
    [(cut_start, cut_end, said)]: the abandoned takes, on the recording's timeline. sil: the recording's
    pauses [(start, end)] (ffmpeg silencedetect), so each cut starts and ends inside one.
    left: if a list, gets (time, said, why) for each repeat that is NOT cut, for the lecturer to re-record.
    """
    heard = [(w.strip(), s, e) for w, s, e in words if w.strip()]
    keys = [_k(w) for w, _, _ in heard]
    sorry = _sorry_takes(heard, keys, sil)        # "sorry" always means a restart
    cuts, i, left_until = [c for _, _, c in sorry], 0, 0
    jump = {a: b for a, b, _ in sorry}
    while i < len(heard):
        if i in jump:
            i = jump[i]
            continue
        if any(a < i < b for a, b in jump.items()):
            i += 1
            continue
        found = None
        # the longest take starting here that is repeated straight after (fillers allowed in between)
        for n in range(min(MAX_TAKE, (len(heard) - i) // 2), MIN_TAKE - 1, -1):
            a = keys[i:i + n]
            if not all(a):
                continue
            j = i + n
            while j < len(heard) and keys[j] in FILLER and j - (i + n) < 6:
                j += 1
            if j + n <= len(heard) and _same_take(a, keys[j:j + n]):
                # "...in older adults. Older adults are..." is how people talk, not a restart: a finished
                # sentence followed by one that starts the same way needs "sorry" to count
                if re.search(r"[.?!]$", heard[i + n - 1][0]) and not any(k in REDO_WORDS for k in keys[i + n:j]):
                    continue
                found = (n, j)
                break
        if not found:
            found = _corrected(heard, keys, i, sil)
            if found and not found[2]:
                if left is not None and i >= left_until:
                    left.append((round(heard[i][1], 2), " ".join(w for w, _, _ in heard[i:found[1]]), "no-signal"))
                    left_until = found[1]
                i += 1
                continue
        if not found:
            i += 1
            continue
        n, j = found[:2]
        start = _pause_near(heard[i][1], sil)
        end = _pause_near(heard[j][1], sil)
        if any(k in REDO_WORDS for k in keys[i + n:j]):
            # "sorry" said straight through: cut between the words, however short the gap
            if start is None or start > heard[i][1] + 0.05:
                start = _between(heard, i)
            if end is None or end > heard[j][1] + 0.05:
                end = _between(heard, j)
        # a pause before each take, and a cut no longer than the words in it could take to say
        ok = (start is not None and end is not None and start <= heard[i][1] + 0.05 and end <= heard[j][1] + 0.05
              and 0.3 < end - start <= max(6.0, 0.9 * (j - i)))
        if ok and not any(i < a < j for a in jump):
            cuts.append((round(start, 3), round(end, 3), " ".join(w for w, _, _ in heard[i:j])))
            i = j
        else:
            if left is not None and i >= left_until:
                left.append((round(heard[i][1], 2), " ".join(w for w, _, _ in heard[i:j]), "no-pause"))
                left_until = j
            i += 1
    return sorted(cuts)


SORRY = {"sorry", "oops"}
SORRY_LOOKBACK = 80   # words back to look for where the abandoned take began
RESTATED = 0.6        # share of a take's first sentence the restart must say again, when the take is long
SENTENCE_GAP = 0.35   # a full stop counts as the end of a finished sentence only with a pause this long after it
_LITTLE = {"which", "that", "this", "these", "those", "they", "them", "then", "than", "there", "their", "with",
           "from", "have", "were", "what", "when", "will", "would", "also", "into", "just", "like", "your"}


def _content(keys):
    """The words that carry meaning: not fillers or little words."""
    return [k for k in keys if k and len(k) >= 4 and k not in FILLER and k not in SORRY and k not in _LITTLE]


def _restated(take, restart):
    """Share of the take's meaningful words that the restart says again, allowing for Whisper hearing a word a
    little differently each time ("metazapine" / "metasapine"). 1.0 when the take has none."""
    num = lambda k: str(_NUM_WORDS[k]) if k in _NUM_WORDS else k   # noqa: E731  ("three" and "3" alike)
    take, restart = [num(k) for k in take if k], [num(k) for k in restart if k]
    said = restart
    # a drug name or a number has to be said again as it was: "Lecanemab is given every two weeks" isn't
    # restarted by "Donanemab is given every four weeks", however alike the rest is
    if any((drug_like(k) or (k.isdigit() and k != "1")) and k not in said for k in take):
        return 0.0
    words = _content(take)
    if not words:
        return 1.0
    hit = sum(1 for k in words if k in said or any(difflib.SequenceMatcher(None, k, x).ratio() >= 0.75
                                                   for x in said if len(x) >= 4))
    return hit / len(words)


def _sentence_ends(heard, i, s, sil):
    """Positions in heard[i:s - 1] of words that end a finished sentence: a full stop and a pause after it."""
    return [p for p in range(i, s - 1)
            if re.search(r"(?<!\.\.)[.?!]$", heard[p][0]) and _gap_after(heard, p, sil) >= SENTENCE_GAP]


def _put_right(keys, s, r, lo):
    """Where the take begins when the restart just puts a word right ("from 2020, sorry, 2016 says", "every
    four, sorry, every six weeks", "twice daily, sorry, once daily"): the last one to three words before "sorry"
    said again with one word changed. Only that much is cut, not the sentence. None when it isn't that."""
    for n in (1, 2, 3):
        if s - n < lo or r + n > len(keys):
            break
        take, redo = keys[s - n:s], keys[r:r + n]
        if not all(take) or not all(redo):
            break
        diff = [k for k in range(n) if take[k] != redo[k]]
        if len(diff) == 1 and (n > 1 or _swapped(take[0], redo[0])):
            return s - n
    return None


def _swapped(before, after):
    """True when the word after "sorry" stands in for the one just before it: a number for a number, or a drug
    name for a drug name."""
    num = lambda k: (k in _NUM_WORDS and k != "one") or k.isdigit()   # noqa: E731  ("one" is often a pronoun)
    return bool(before and after and before != after
                and ((num(before) and num(after)) or (drug_like(before) and drug_like(after))))


def _sorry_takes(heard, keys, sil):
    """
    [(first word, restart word, cut)] for each "sorry": the restart is the first word after it (fillers
    skipped); the abandoned take began at an earlier place that starts like the restart (the same first two
    words, or the same two after the first, or one long word), else at the start of the sentence "sorry" ends.
    All of it, "sorry" included, is cut.

    A take that reaches back past a finished sentence, or that was found by one long word, is only cut when
    the restart says its first sentence again. "8th one is ... with MRI. For BPSD, ... sorry, 9th one is for
    BPSD" restarts with "one is", like point 8 did, but point 8 is never said again: only the abandoned
    point 9 is cut, and point 8 stays. A finished sentence at the front of a take is kept the same way.
    """
    out, done = [], 0
    for s, k in enumerate(keys):
        if k not in SORRY or s < done:
            continue
        r = s + 1
        while r < len(heard) and keys[r] in FILLER and r - s < 5:
            r += 1
        if r >= len(heard):
            continue
        lo = max(done, s - SORRY_LOOKBACK)
        found = []                                              # (start, rule), nearest first within a rule
        found += [(k2, 1) for k2 in range(s - 1, lo - 1, -1)   # the same first two words
                  if k2 + 1 < s and keys[k2:k2 + 2] == keys[r:r + 2]]
        if r + 2 < len(heard) and all(keys[r + 1:r + 3]):      # or the same two words after the first,
            found += [(k2, 2) for k2 in range(s - 2, lo - 1, -1)   # which Whisper may mishear ("she" / "he")
                      if keys[k2 + 1:k2 + 3] == keys[r + 1:r + 3]]
        if len(keys[r]) >= 5:                                   # or one long one ("vancomycin")
            found += [(k2, 3) for k2 in range(s - 1, lo - 1, -1) if keys[k2] == keys[r]]
        # or the start of one of the last two finished sentences, when the restart says that sentence again
        # (Whisper can hear the two takes a little differently: "It's got out of 30" / "It's scored out of 30")
        starts = [lo] + [p + 1 for p in _sentence_ends(heard, lo, s, sil)]
        for j in starts[::-1][:2]:
            if j < s and difflib.SequenceMatcher(None, keys[j:s], keys[r:r + (s - j)], autojunk=False).ratio() >= 0.6:
                found.append((j, 4))
        i = None
        for k2, rule in found:
            ends = _sentence_ends(heard, k2, s, sil)
            if rule < 3 and not ends:
                i = k2                                          # a restart within the same sentence
                break
            first = keys[k2:ends[0] + 1] if ends else keys[k2:s]
            if len(_content(first)) < 3 and rule == 1:
                i = k2                                          # too short to judge: trust the same first words
                break
            if _restated(first, keys[r:r + (s - k2) + 8]) >= (RESTATED - 0.1 if rule == 1 else RESTATED):
                i = k2
                break
        if i is None:                                           # else a word or two put right
            i = _put_right(keys, s, r, lo)
        if i is None:                                           # else the sentence "sorry" ends
            i = s                                               # (the word before "sorry" is part of it,
            if s - 1 > lo and not _sentence_ends(heard, s - 1, s + 1, sil):   # unless it ends a finished one)
                i = s - 1
            while i > lo and not re.search(r"[.?!]$", heard[i - 1][0]) and _gap_after(heard, i - 1, sil) < 0.5:
                i -= 1
            # and back past a pause, over each earlier part of the sentence that is said again soon after
            # ("all three work equally well, we choose by the form, sorry, ... all three work equally well")
            j = i
            while j > lo and not re.search(r"[.?!]$", heard[j - 1][0]):
                j -= 1
                while j > lo and not re.search(r"[.?!]$", heard[j - 1][0]) and _gap_after(heard, j - 1, sil) < 0.5:
                    j -= 1
                part = keys[j:i]
                if any(k in SORRY for k in part):
                    break
                if len(_content(part)) < 2:
                    if j > lo and not re.search(r"[.?!]$", heard[j - 1][0]):
                        continue                                # too little to judge yet: take in the part before
                    if all(k in keys[r:r + 40] for k in part if k):
                        i = j                                   # the first word or two of the sentence, said again
                    break
                if _restated(part, keys[r:r + 40]) < 1.0:
                    break
                i = j
        # keep a finished sentence at the front of the take that the restart doesn't say again
        # ("...an extended-release capsule. Third row... sorry, in third row" keeps "capsule")
        for p in _sentence_ends(heard, i, s, sil):
            lead = keys[i:p + 1]
            if len(_content(lead)) >= 3 and _restated(lead, keys[r:r + (s - i) + 8]) < RESTATED:
                i = p + 1
            else:
                break
        start = _pause_near(heard[i][1], sil)
        if start is None or start > heard[i][1] + 0.05:
            start = _between(heard, i)
        end = _pause_near(heard[r][1], sil)
        if end is None or end > heard[r][1] + 0.05 or end < heard[r - 1][2] - 0.05:
            end = _between(heard, r)
        if start is not None and end is not None and end > start:
            out.append((i, r, (round(start, 3), round(end, 3), " ".join(w for w, _, _ in heard[i:r]))))
            done = r
    return out


def sorry_cuts(words, sil):
    """[(cut_start, cut_end, said)] for the "sorry" restarts only (the second listen, on the edited audio)."""
    heard = [(w.strip(), s, e) for w, s, e in words if w.strip()]
    return [c for _, _, c in _sorry_takes(heard, [_k(w) for w, _, _ in heard], sil)]


def imagined_sorry(cut, plain, primed, after=40):
    """True when a "sorry" cut found only by the listen primed for "sorry" would remove teaching: the plain
    listen hears no "sorry" there, but three or more meaningful words that neither listen hears said again
    just after. ("I'm, I'm sorry" imagined over "The textbook says they may be used, citing one study".)
    The primed listen counts for the "said again": the plain one can run two takes into one ("guideline from
    2020, sorry, the American Psychiatric Association guideline from 2016" heard as "guideline from 2016")."""
    a, b = cut[0], cut[1]
    inside = [_k(w) for w, s, e in plain if a <= (s + e) / 2 <= b]
    if any(k in SORRY for k in inside) or len(_content(inside)) < 3:
        return False
    later = [[_k(w) for w, s, e in heard if (s + e) / 2 > b][:after] for heard in (plain, primed)]
    return _restated(inside, later[0] + later[1]) < RESTATED


def start_on_plain(cut, plain):
    """A "sorry" cut found only by the primed listen, started where the plain listen hears its first meaningful
    word. The primed listen can drop the words before a take ("Then we set a clear time to review it.") and
    start the cut at the pause before them; the plain one heard them, so the cut starts after them."""
    a, b, said = cut
    keys = [_k(w) for w in said.split()]
    f = next((n for n, k in enumerate(keys) if k and _content([k])), None)
    if f is None:
        return cut
    lead = [k for k in keys[:f] if k]                           # the little words before it ("The American")
    heard = [(w.strip(), s, e) for w, s, e in plain if w.strip()]
    for n, (w, s, e) in enumerate(heard):
        if a - 0.5 <= s < b and _k(w) == keys[f]:
            m = n - len(lead) if lead and [_k(x) for x, _, _ in heard[max(0, n - len(lead)):n]] == lead else n
            if m > 0 and heard[m - 1][2] > a:                   # words before the take the primed listen missed
                a = round((heard[m - 1][2] + heard[m][1]) / 2, 3)
            return (a, b, said)
    return cut


def is_sorry(said):
    """True when a cut's words include "sorry" (a restart the lecturer flagged)."""
    return any(_k(w) in SORRY for w in said.split())


def lost_in_cuts(words, cuts, notes, near=90.0, min_words=3):
    """[(cut_start, said, words)] for each cut that takes out words from the notes that aren't said anywhere
    else nearby: a cut that may have removed teaching, not just a retake, for the lecturer to listen to."""
    script = {_k(w) for w in (notes or "").split()}
    kept = drop_cut(words, cuts)
    out = []
    for a, b, said in cuts:
        nearby = {_k(w) for w, s, _ in kept if a - near <= s <= b + near}
        lost = list(dict.fromkeys(w.strip(" ,.;:?!") for w in said.split()
                                  if _k(w) in script and _k(w) not in nearby and _content([_k(w)])))
        if len(lost) >= min_words:
            out.append((a, said, lost))
    return out


def drop_cut(words, cuts):
    """Whisper's words without those inside a cut (so they aren't captioned or checked)."""
    return [w for w in words if not any(a <= (w[1] + w[2]) / 2 <= b for a, b, _ in cuts)]


def checks(words, notes, context=3, near=10):
    """[(time, said, script)] where a number or drug name heard differs from the script."""
    heard = [(w.strip(), s, e) for w, s, e in words if w.strip()]
    script = (notes or "").split()
    if not heard or not script:
        return []
    vocab = captions._vocabulary(script)
    keys = list(vocab)
    sm = difflib.SequenceMatcher(None, [_k(w) for w, _, _ in heard], [_k(w) for w in script], autojunk=False)
    out = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "replace" or i2 - i1 > 8 or j2 - j1 > 8:
            continue
        said = [w for w, _, _ in heard[i1:i2]]
        meant = script[j1:j2]
        if "".join(_k(w) for w in said) == "".join(_k(w) for w in meant):
            continue                              # the same words, split differently ("forty -year -old")
        # a dose in the script that wasn't said, or a dose said that isn't in the script
        nm_all, ns_all = numbers(meant), numbers(said)
        nm = numbers(script[j1:j2 + 4], doses_only=True, within=j2 - j1)
        ns = numbers(said + [w for w, _, _ in heard[i2:i2 + 4]], doses_only=True, within=i2 - i1)
        problem = any(n not in ns_all and n not in ns for n in nm) or any(n not in nm_all and n not in nm for n in ns)
        if not problem:
            # another drug from the notes said where the script has a drug, and not one mentioned nearby
            fixed = {_k(captions._fix_word(w, vocab, keys)) for w in said}
            wanted = {_k(w) for w in meant if drug_like(w)}
            nearby = {_k(w) for w in script[max(0, j1 - near):j2 + near] if drug_like(w)}
            other = {k for k in fixed if k in vocab and drug_like(k) and k not in nearby}
            problem = bool(wanted and other)
        if problem:
            a = " ".join(w for w, _, _ in heard[max(0, i1 - context):min(len(heard), i2 + context)])
            b = " ".join(script[max(0, j1 - context):min(len(script), j2 + context)])
            out.append((round(heard[i1][1], 2), a[:300], b[:300]))
    return out


# ---- coaching, for slide_watch.py ----
PACE = 130            # Abin's usual words per finished minute
FAST, SLOW = 165, 95  # words per finished minute worth a mention
KEEP_PAUSE, MIN_PAUSE_TO_TRIM = 0.45, 0.70   # as polish.py shortens pauses


def finished_seconds(words, sil, cuts=()):
    """How long the recording will be once polished: first word to last, less cut takes and the part of
    each long pause that polishing trims."""
    kept = [w for w in words if w[0].strip()]
    if not kept:
        return 0.0
    a, b = kept[0][1], kept[-1][2]
    cut = sum(max(0.0, min(e, b) - max(s, a)) for s, e, _ in cuts)
    trimmed = sum(max(0.0, min(e, b) - max(s, a)) - KEEP_PAUSE for s, e in sil
                  if e is not None and e - s >= MIN_PAUSE_TO_TRIM and min(e, b) - max(s, a) > KEEP_PAUSE
                  and not any(cs <= s and e <= ce for cs, ce, _ in cuts))
    return max(0.0, b - a - cut - trimmed)


def _doses(tokens):
    """[(value, "600 mg")]: the numbers in the notes that have a unit straight after them."""
    out = []
    for i, t in enumerate(tokens):
        if not re.search(r"\d", t):
            continue
        vals = numbers([t])
        if not vals:
            continue
        unit = next((tokens[k] for k in (i, i + 1) if k < len(tokens) and _unit(tokens[k]) and
                     (k > i or re.search(r"\d[a-zA-Z%]", t))), None)
        if unit:
            label = t if unit is t else f"{t} {unit}"
            out.append((vals[0], label.strip(".,;:()")))
    return out


def missed(words, notes, limit=6):
    """Drug names and doses in the notes that weren't heard (the recording may have misheard, so: 'not heard')."""
    script = (notes or "").split()
    heard = [w.strip() for w, _, _ in words if w.strip()]
    if not script:
        return []
    vocab = captions._vocabulary(script)
    keys = list(vocab)
    said = {_k(captions._fix_word(w, vocab, keys)) for w in heard} | {_k(w) for w in heard}
    out, seen = [], set()
    for w in script:
        k = _k(w)
        if drug_like(w) and k not in said and k not in seen:
            seen.add(k)
            out.append(re.sub(r"^[^\w]+|[^\w]+$", "", w))
    said_nums = set(numbers(heard))
    for v, label in _doses(script):
        if v not in said_nums and label not in seen:
            seen.add(label)
            out.append(label)
    return out[:limit]


def sentence_with(notes, snippet):
    """The whole sentence of the notes that contains snippet (a run of the notes' words), or snippet."""
    tokens, want = (notes or "").split(), snippet.split()
    for i in range(len(tokens) - len(want) + 1):
        if tokens[i:i + len(want)] == want:
            a = i
            while a > 0 and not re.search(r"[.!?:]$", tokens[a - 1]):
                a -= 1
            b = i + len(want) - 1
            while b < len(tokens) - 1 and not re.search(r"[.!?]$", tokens[b]):
                b += 1
            return " ".join(tokens[a:b + 1]) if b - a < 60 else snippet
    return snippet
