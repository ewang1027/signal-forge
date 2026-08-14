You are adding a plain-language pass to a project idea that was written before
one was required. The idea itself is good and **must not change** — you are
building a ramp into it, not rewriting it or simplifying it.

## Who is reading it

An early-career developer — a strong intern or first-year engineer. He has
written real code and knows the fundamentals, but has never worked on kernels,
schedulers, compilers or distributed databases, and does not know the vocabulary
those fields take for granted.

The project is deliberately above his current level. That is the point, and you
must not water it down to reach him. But he has to be able to tell what it *is*
and why it is hard before committing two months to it.

## The idea

```json
{idea}
```

## What to write

Emit a single JSON object inside a ```json fence, no prose outside it:

{
  "in_plain_terms": "3-5 sentences, no jargon, for someone who has never touched this area. What goes wrong today, who it hurts, and what the thing being built actually does. Use a concrete scenario or an everyday analogy. Someone who has only written web apps should finish this able to explain the project to a friend.",
  "why_it_is_hard_plainly": "2-3 sentences on why this is genuinely difficult, in plain language. Name the shape of the difficulty — 'you have to measure something that destroys the evidence as it happens', 'two things have to agree but neither can wait for the other'. No jargon.",
  "glossary": [
    {"term": "the exact jargon word as used in the idea above", "means": "one sentence, plain and concrete — what it is and why it matters here, not a dictionary definition"}
  ],
  "starting_points": ["a specific doc, paper, source file or man page to read first, and why it helps — one line each"]
}

`glossary` must cover **every** term in the idea above that an early-career
developer would not already know — aim for 4 to 8, and read `why_hard`
carefully, since that is where the jargon concentrates. `starting_points` should
be 2-4 real, findable things; do not invent URLs, and prefer naming a document
precisely over guessing at a link.
