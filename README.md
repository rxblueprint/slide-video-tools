# slide-video-tools

Turns a slide deck (PowerPoint + its PDF) and its speaker notes into a narrated video, with captions and a
topics list. Narration uses [Chatterbox](https://github.com/resemble-ai/chatterbox) (MIT) with a reference voice
clip. Runs as a GitHub Actions workflow started by a website; all inputs and outputs travel through short-lived
links, and nothing is stored here.

Speaker-notes marks: `[pause]`, `[pause 2]`, a blank line (paragraph pause) and `*word*` (stress). A pronunciation
list (`word = how to say it`) changes only what the voice says; captions keep the notes' spelling.
