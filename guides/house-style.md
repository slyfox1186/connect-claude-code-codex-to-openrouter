---
topic: house-style
triggers: writing anything a person other than the developer will read - UI copy, emails, docs, README, error messages, commit messages, marketing pages, generated reports
source: written from scratch
verified: 2026-09-11
---

# House style

Anything shipped to a customer must not read as machine-written. This is part of
"done", not a polish pass afterwards. When one instance of a tell is found, sweep
the whole codebase for that pattern rather than fixing the single occurrence.

## Sentence-level tells

**Dashes.** No em dashes or en dashes in customer-facing copy. A comma, a full
stop, or a rewrite. This is the single most recognisable signal.

**Self-praising honesty.** "Honest about where it is." "Quotas we state plainly."
"The difference is never blurred." Announcing your own integrity reads as
marketing written by something with none. State the fact and stop.

**Unrequested reassurance closers.** A paragraph that ends by promising the
reader everything is fine. If the reassurance were warranted the facts would
carry it.

**Anaphoric tricolons.** Three clauses opening with the same word, building to a
crescendo. Occasionally a real rhetorical choice, usually a reflex.

**Colon reveals.** "There's one problem: everything." Rationed to roughly never.

**"X, not Y" headlines.** "A bridge, not a wrapper." "Fast, not fragile."
Instantly recognisable, and it says less than the plain version.

**Verbless comma catalogs.** "Faster builds, cleaner output, fewer surprises."
Write a sentence with a verb in it.

**Bolded lead-in labels on every bullet.** One or two in a list is emphasis.
Every bullet is a template. (This guide uses them because it is a reference for
a machine, not customer copy.)

**Rule-of-three everywhere.** Real lists have two items, or five, or one.

**Uniformly polished rhythm.** Human writing has short sentences next to long
ones, and the occasional slightly awkward construction. Prose where every
sentence is the same length is the giveaway.

## Vocabulary to delete on sight

delve, tapestry, leverage (as a verb), utilize, robust, comprehensive, seamless,
streamline, empower, elevate, unlock, harness, cutting-edge, transformative,
game-changing, best-in-class, moreover, furthermore, "it's worth noting that",
"in today's fast-paced world", "not only X but also Y".

Most have a plain replacement: use, strong, full, smooth, simplify. "Utilize" is
always "use".

## Visual tells

Covered in full in the css guide. The short version:

- No decorative coloured left rail down the side of a card, callout, banner or
  status row. The only exception is a neutral hairline beside a genuine quotation
  of someone else's words.
- No one-radius, one-padding, one-card-shape repeated across every component.
- No purple-to-blue gradient, and Inter is not a typographic decision on its own.
- No generic gradient block standing in for a logo, mark or photo.

## Code

Comments explain why, never what. A comment restating the line below it is a
tell, and it rots as soon as the line changes.

```python
# Increment the counter          <- delete
count += 1

# Retry only on 408 and 429: a 5xx here can arrive after the provider
# already generated and billed the tokens.          <- keep
```

Match the surrounding code's naming, comment density and idiom. A file where one
function is documented in a different register than its neighbours is obvious.

Commit messages say what changed and why, in plain sentences. No trailing
attribution lines, no emoji, no generated-by footer.

## Email and messages

Write in the sender's own voice: plain, direct, slightly imperfect. Read the
whole thread first and do not repeat a greeting, a pleasantry or a fact already
sent. No canned openers, no heavy hedging, no semicolon habit.

## The test

Read it aloud. If it sounds like a press release, a support macro, or a
particularly agreeable assistant, rewrite it. If a specific person could have
written it on a Tuesday, it passes.
