# Reading a run back

```bash
python -m deskhand.replay <run_id>            # the trajectory
python -m deskhand.replay <run_id> --at 7     # what the model saw at step 7
python -m deskhand.replay <run_id> --diverge  # replay against the current config
```

Two capabilities that answer different questions. The first is an audit tool.
The second is a prompt-regression tool, and it is the one worth building.

## What happened

`transcript.rebuild()` is a pure function of the step rows and the opening
prompt — no clock, no randomness, no ambient state. That has a useful
consequence: the conversation as it stood before *any* step can be
reconstructed exactly, today or in a year, on a machine the run never touched.

```
$ python -m deskhand.replay ffc01386… --at 7

what the model saw before step 7 of run ffc01386…
reconstructed from the step log — no model was called

── user ──────────────────────────────────────────────────────────────
Work support ticket NW-1 (subject: Beans arrived stale).
...
── assistant ─────────────────────────────────────────────────────────
get_ticket(reference=NW-1)

── user ──────────────────────────────────────────────────────────────
tool_result
<<<untrusted:46902a27cf0c>>>
Ticket NW-1: Beans arrived stale
...
```

"Why did it decide to refund?" is answerable by looking at exactly what it had
in front of it when it decided — including whether the fence was where it
should have been. The same view is available per step in the run viewer, behind
*what the model saw here*.

The trajectory listing interleaves approvals, which live in their own table. A
*granted* approval writes no step — only denials do — so without that a run
which stopped, waited for a person, and was allowed to continue would replay
with no sign that the most consequential thing in it had happened:

```
    7  model  tool_use
       issue_refund(amount_cents=1900, order_reference=NW-1042, …)
       ⏸ approved by owner@northwind.test  Refund 19.00 USD against order NW-1042
    8  issue_refund  ok
```

## What a change would have done

Divergence replays a recorded run against a changed configuration — a new
system prompt, a different model — and reports the first decision that differs.

```
$ python -m deskhand.replay ffc01386… --diverge --system-prompt more-cautious.txt

  diverged at step 7
  3 of 7 decisions matched first

  originally:
    issue_refund({"amount_cents":1900,"order_reference":"NW-1042",…)
  now:
    set_ticket_status({"reference":"NW-1","status":"escalated"})
```

The new model is asked to make each decision again, given exactly the
observations the original run got. Where it agrees, the replay continues. Where
it does not, that is the fork.

### It never executes a tool

This is what makes it safe to point at runs that moved real money. When the
replayed model asks for a call, the **recorded result** of that call is handed
back — the tool itself is never invoked, and nothing is written. There is a
test that runs a divergence twice, including against an agent that tries to
issue a refund the original never made, and asserts the refunds table, the
ticket messages, the step log and the run row are all byte-identical afterwards.

### Decisions are compared, not prose

The comparison is on tool name plus canonical arguments. Two runs that both
call `issue_refund` for 1900 have made the same decision however differently
they narrate it, and a report that fired on rewording would be noise. Two runs
that call `issue_refund` for 1900 and 4800 have not, which is the case a diff
of tool *names* would miss and the one that costs money.

### The limitation, stated plainly

Once the replayed model asks for something the original run never asked for,
there is no recorded observation to hand back, and the replay stops.

**Divergence tells you where behaviour changed, not what would have happened
next.** For that you have to let a real run go, with real tools and a real
approval gate. Anything that claimed to simulate the rest of the trajectory
would be inventing observations, and an invented observation is worse than no
answer.

## What it is for

Change the system prompt, replay every recorded run, and count. How many
diverge? At which step? Toward more caution or less? That is a regression
suite for prompt changes built out of production traffic, and it costs one
model call per decision with no side effects at all.

The natural next step — not built — is to run it across a corpus rather than
one run at a time, and report the distribution: *"of 200 recorded runs, 12
diverge under the new prompt; 11 of those escalate where they previously
refunded."* The single-run version is the piece worth getting right first,
because the corpus version is a loop around it.
