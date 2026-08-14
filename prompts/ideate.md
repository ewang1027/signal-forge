You are generating a project idea for an engineer who wants to build technically
complex systems that solve real problems. He is not looking for a startup or a
portfolio piece — he wants to learn deeply and build something hard.

## Who is reading this

An early-career developer — think a strong intern or a first-year engineer. He
has written real code and knows the fundamentals, but he has **not** worked on
kernels, schedulers, compilers, or distributed databases, and he does not know
the vocabulary those fields take for granted.

The project should be **above his current level** — that is the entire point,
and you must not water it down to reach him. But he has to be able to tell what
the project *is* and why it is hard before deciding to spend two months on it.
An idea he cannot understand is an idea he cannot choose, however good it is.

So: keep the depth, and build a ramp into it. Explain the thing before you use
the words that describe it. The first time a term like *stall-time integral*,
*monomorphization*, *write amplification* or *quorum* appears, it must already
have been explained or it must be in the glossary.

Assume he will look things up — but only if he already knows why they matter.

Below is raw evidence: real complaints harvested from Hacker News, in the domain
**{domain}**. These are unedited comments from people who personally hit a wall.

<evidence>
{evidence}
</evidence>

## What has landed well before

{taste}

## What to do

Read the evidence and find the *underlying* problem — the thing that keeps
recurring across several of these complaints, not a restatement of any single
one. Then propose THREE projects that attack it.

The three must differ in *kind*, not in detail — three framings of the same
build are useless. Aim at different layers or different parts of the problem, so
that rejecting one does not reject all three.

## Hard constraints

- **3–8 weekends of work.** Not a weekend toy, not a year-long rewrite.
- **One genuinely hard technical core.** There must be a part where the difficulty
  is real algorithmic or systems difficulty — a scheduling problem, a consistency
  problem, a parsing problem, a performance problem. If the whole project is
  wiring APIs together, it is the wrong project.
- **Grounded in the evidence above.** You must cite specific complaints. If the
  evidence does not support a good project, say so rather than inventing one.
- **Not a wrapper.** "A nicer UI for X" and "an LLM that does X" are rejected
  unless the hard part is somewhere other than the LLM call.

## What not to do

Do not optimize for novelty. Something that already exists but is done badly, in
a way the evidence demonstrates, is a *better* project than something nobody has
built because nobody wanted it. If a mature tool already solves this well, say
that explicitly and pivot to the specific gap the evidence shows.

Do not pad. No "this could be extended to..." sections.

## Output

Emit a JSON array of exactly three objects inside a ```json fence, no prose
outside it. Order them best-first. Each object:

{
  "title": "short, concrete, names the thing being built",
  "one_liner": "one sentence an early-career developer would understand. no jargon at all in this line.",
  "in_plain_terms": "3-5 sentences, no jargon, for someone who has never touched this area. What goes wrong today, who it hurts, and what the thing you would build actually does. Use a concrete scenario or an everyday analogy. Someone who has only written web apps should finish this and be able to explain the project to a friend.",
  "why_it_is_hard_plainly": "2-3 sentences on why this is genuinely difficult, in plain language, BEFORE the precise version below. Name the shape of the difficulty — 'you have to measure something that destroys the evidence as it happens', 'two things have to agree but neither can wait for the other'. No jargon.",
  "problem": "2-3 sentences on the real underlying problem, referencing the evidence",
  "evidence_refs": ["url", "url"],
  "why_hard": "the specific technical difficulty at the core, named precisely. This is the deep version and it is allowed to be dense — the plain version above is what carries the reader into it.",
  "what_you_learn": "concrete skills/knowledge, not 'you will learn about distributed systems'",
  "first_weekend": "the smallest thing that proves the core works. must be runnable and testable. name the actual tools/commands where you can.",
  "milestones": ["weekend 2-3: ...", "weekend 4-5: ...", "weekend 6-8: ..."],
  "prior_art": "what already exists and specifically where it falls short, or 'none found' with what you checked",
  "kill_criteria": "what would tell you within 2 weekends that this is not worth finishing",
  "glossary": [
    {"term": "the exact jargon word as used above", "means": "one sentence, plain, concrete. what it is and why it matters here — not a dictionary definition."}
  ],
  "starting_points": ["a specific doc, paper, source file or man page to read first — with why it helps, one line each"]
}

`glossary` must cover **every** term in this idea that an early-career developer
would not already know — aim for 4 to 8. Under-filling it is the most common way
to make an otherwise good idea unusable. `starting_points` should be 2-4 real,
findable things; do not invent URLs, and prefer naming a document precisely over
guessing at a link.
