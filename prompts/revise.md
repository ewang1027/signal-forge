A quality gate reviewed this project idea and concluded the underlying problem is
real but the framing is wrong. Your job is to produce the corrected version.

## The idea as written

```json
{candidate}
```

## What the gate said

{critique}

## What to do

Rewrite the idea so the objection no longer applies. This is a revision, not a
new idea — the problem being attacked stays the same, and the same evidence
still motivates it.

Concretely:

- **If the hard part was inflated**, replace it with the difficulty that is
  actually there. Do not restate the same claim in stronger words. If the gate
  named a library or API call that makes the stated core easy, the new core must
  be somewhere that call does not reach. If after honest accounting there is no
  hard core left, say so — set `"withdrawn": true` and stop.
- **If the first weekend could not fail**, replace it with one that can. The test
  is whether a bad outcome is possible and would tell you something.
- **If the scope was several projects**, cut it to the one worth building. Drop
  the rest rather than compressing them.
- **If prior art already covers it**, narrow to the specific gap the gate named.

Keep `evidence_refs` exactly as they are. Do not add new ones.

## Output

One JSON object in a ```json fence, nothing outside it. Same schema as the input,
plus two fields:

{
  "title": "...",
  "one_liner": "...",
  "problem": "...",
  "evidence_refs": ["unchanged"],
  "why_hard": "...",
  "what_you_learn": "...",
  "first_weekend": "...",
  "milestones": ["..."],
  "prior_art": "...",
  "kill_criteria": "...",
  "revision_note": "one sentence: what changed and why",
  "withdrawn": false
}
