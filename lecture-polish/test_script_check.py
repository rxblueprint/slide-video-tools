"""
Checks for the "sorry" restarts in script_check.py, on made-up word timings: python3 test_script_check.py
(no Whisper or ffmpeg needed). Each case is a real miss or a case that must not change.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import script_check as sc  # noqa: E402


def words(text, pauses):
    """Each word 0.3 s long with 0.05 s between; pauses: {word index: pause after it}."""
    out, sil, t = [], [], 0.0
    for n, w in enumerate(text.split()):
        out.append((" " + w, t, t + 0.3))
        t += 0.3
        g = pauses.get(n, 0.05)
        if g >= sc.MIN_GAP:
            sil.append((t, t + g))
        t += g
    return out, sil


def after(text, word, n=1):
    """Index of the n-th word equal to word."""
    hits = [i for i, w in enumerate(text.split()) if w == word]
    return hits[n - 1]


CASES = [
    # Hep A: Whisper put a full stop before "sorry"; the clause before it is said again, so it goes too
    ("Hep A is an RNA virus spread by fecal oral route. It's self-limited, becomes chronic. Sorry. "
     "It never becomes chronic and mortality is under one percent.",
     {"route.": 0.9, "self-limited,": 0.9, "chronic.": 0.5, "Sorry.": 0.2},
     "becomes chronic. Sorry."),
    # the same with commas: only the clause said again is cut, never "self-limited", which is said once
    ("Hep A is an RNA virus spread by fecal oral route. It is self-limited, becomes chronic, sorry, "
     "it never becomes chronic and mortality is under one percent.",
     {"route.": 0.9, "self-limited,": 0.9, "chronic,": 0.5, "sorry,": 0.2},
     "becomes chronic, sorry,"),
    # Electrophysiology: Whisper heard "Amiodarone" the second time as "Amute everyone"
    ("Torsade risk is highest with bradycardia and low potassium. Amiodarone has a little reverse use "
     "dependence. So torsade is rare and even though it is its QT is long. Sorry. Amute everyone has little "
     "reverse use dependence. So torsade is rare even though its QT is long. In short,",
     {"potassium.": 1.2, "dependence.": 0.3, "long.": 0.6, "Sorry.": 1.0},
     "Amiodarone has a little reverse use dependence. So torsade is rare and even though it is its QT is long. Sorry."),
    # unchanged: a word put right
    ("The dose is every four, sorry, every six weeks for maintenance.", {}, "every four, sorry,"),
]


def main():
    bad = 0
    for text, pauses, want in CASES:
        ws, sil = words(text, {after(text, k): v for k, v in pauses.items()})
        got = [c[2] for c in sc.sorry_cuts(ws, sil)]
        ok = want in got
        bad += not ok
        print("ok  " if ok else "FAIL", got, "" if ok else f"(wanted {want!r})")
    # a different drug, or a different number, is never "said again"
    for take, redo in ((["lecanemab", "is", "given", "monthly"], ["donanemab", "is", "given", "monthly"]),
                       (["vancomycin", "every", "12", "hours"], ["vancomycin", "every", "8", "hours"])):
        ok = sc._restated(take, redo) == 0.0
        bad += not ok
        print("ok  " if ok else "FAIL", "not restated:", " ".join(take), "/", " ".join(redo))
    ok = sc._restated(["amiodarone", "has", "little", "reverse"], ["amute", "everyone", "has", "little", "reverse"]) > 0
    bad += not ok
    print("ok  " if ok else "FAIL", "restated though Whisper misheard the drug")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
