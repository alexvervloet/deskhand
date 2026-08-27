# Five levels of difficulty

The same system, explained five times, each time to someone who knows more than
the last. The format is borrowed from the Wired video series.

The rule each level follows: go as deep as the audience can actually follow, then
stop and say what was skipped and why. No level is a summary of the one above it.
Each one is a complete explanation of the whole system at its own depth, and each
covers things the levels below it cannot reach.

| Level | Audience | The version of the argument they get |
|---|---|---|
| [1](01-high-school.md) | A teenager in Intro to CS | An agent is a loop. Some actions cannot be undone. So the program, not the AI, decides which ones need a human. |
| [2](02-undergraduate.md) | A second-year CS undergraduate | It is a database problem. Append-only logs, leases, transactions, and why the idempotency key must not be random. |
| [3](03-cs-grad-learning-ai.md) | A CS graduate learning AI | What actually goes over the wire, why cost grows with the square of the turns, and why prompt injection has no parameterised-query equivalent. |
| [4](04-engineering-manager.md) | An engineering manager interviewing for an AI role | The 4%/96% ratio, the five questions to ask any agent design, and what a good answer sounds like. |
| [5](05-senior-ai-engineer.md) | A senior AI engineer | A design review. Precise guarantees, where each stops, and a confirmed fence bypass found while writing it. |

## If you only read one

**Level 2** if you write software. It is the level where the interesting ideas
are, and none of them require knowing anything about AI.

**Level 4** if you evaluate software or the people who write it. The section on
what happens when you delete a safety layer on purpose is the one that changes
how you read a test suite.

## How this relates to the rest of docs/education

[The reading path](../README.md) explains the system once, in the order the
argument makes sense in, for someone who is going to read the code. This folder
explains it five times, at five depths, for people who may not be.

The [exercises](../exercises/) work at any level from 2 upward. Each is a one-line
change with a verified result, and they are the only part of these docs that will
argue back.
