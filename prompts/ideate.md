You are generating a project idea for an engineer who wants to build technically
complex systems that solve real problems. He is not looking for a startup or a
portfolio piece — he wants to learn deeply and build something hard.

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

{{
  "title": "short, concrete, names the thing being built",
  "one_liner": "one sentence a competent engineer would understand",
  "problem": "2-3 sentences on the real underlying problem, referencing the evidence",
  "evidence_refs": ["url", "url"],
  "why_hard": "the specific technical difficulty at the core, named precisely",
  "what_you_learn": "concrete skills/knowledge, not 'you will learn about distributed systems'",
  "first_weekend": "the smallest thing that proves the core works. must be runnable and testable.",
  "milestones": ["weekend 2-3: ...", "weekend 4-5: ...", "weekend 6-8: ..."],
  "prior_art": "what already exists and specifically where it falls short, or 'none found' with what you checked",
  "kill_criteria": "what would tell you within 2 weekends that this is not worth finishing"
}}
