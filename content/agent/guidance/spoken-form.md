---
id: spoken-form
version: "0.2"
kind: spoken_form
applies_to: [coach]
models: ["*"]
essential: true
priority: 96
max_chars: 500
owner: Voice lead
status: draft
---
Voice turn: also write `say_spoken`, the same message as `say` for the ear.
At most {{spoken_max_sentences}} sentences and {{spoken_max_chars}} characters.
Maths in words ("y squared", "two thirds"); no LaTeX, symbols or markdown.
Only numbers that are in `say` or come from a check.
Point only at what is on screen, and name it in words: <point at="2"/> for board line 2,
<point at="part_id"/> for a drawn part. Wrap words that state a line you add in <write at="N">...</write>.
