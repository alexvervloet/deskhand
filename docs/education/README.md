# Deskhand — the reading path

The repository is an argument, and this is the order it makes sense in.

## Read

1. **[01-thesis.md](01-thesis.md)** — why the agent loop is 3% of the code and
   the least interesting part. Read this before the source or the source will
   look over-engineered.
2. **[02-concept-index.md](02-concept-index.md)** — where every idea lives. Use
   it as a lookup, not a read-through.
3. **[03-exactly-once.md](03-exactly-once.md)** — how "never re-executes a
   completed side effect" is true, and the single assumption it rests on.
4. **[04-evals.md](04-evals.md)** — asserting properties of a *path* rather than
   an answer, and what happened when each safety layer was deliberately deleted.
5. **[05-replay.md](05-replay.md)** — reading a run back exactly as it happened,
   and asking what a changed prompt would have done to it.

## Then break it

The exercises are the point. Each is a one-line change with a verified result,
and each takes five to ten minutes.

| # | Exercise | What it shows |
|---|---|---|
| [01](exercises/01-remove-the-approval-gate.md) | Remove the approval gate | What a load-bearing mechanism looks like: 11 of 19 evals fail, across four invariants |
| [02](exercises/02-remove-the-invisible-layer.md) | Remove the fence | **The one worth doing.** Delete the most visible anti-injection defence and 18 of 19 evals keep passing |
| [03](exercises/03-make-the-key-random.md) | Make the idempotency key a uuid | A change that looks like an improvement and silently disables exactly-once |
| [04](exercises/04-let-it-loop.md) | Remove loop detection | Why "it terminates eventually" is not the same as bounded |

If you only do one, do **02**. It is the exercise that changed how this project
tests itself.

## Or take the five levels

The same system explained five times over, each to a different audience, from a
high-school intro-CS student to a senior AI engineer reviewing the design.

[five-levels/](five-levels/). Pick your level, or read two adjacent ones and
watch what gets added.

## And read what went wrong

[LESSONS.md](../../LESSONS.md) — ten entries, written while the detail was fresh.
The three most useful:

- **#2** — a full-text search that failed *open* on a policy lookup. An agent
  reading "no such policy" reasonably concludes it is unconstrained. A retrieval
  bug became a permissions bug.
- **#6** — the mutation-testing result the exercises above are built on.
- **#10** — the fence sanitiser rebuilding the marker it removed, and the test
  that asserted the right property against the only payload its author had
  imagined.

## Try it

**https://deskhand.fly.dev** — sign in as `owner@northwind.test`, password
`demo-password-123`, open `NW-1`, press *Run the agent*, and watch it stop.
