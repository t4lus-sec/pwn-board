---
name: search-board
description: How to run a search board — for bugs and goals where you know the start and can test the end but not the path (flaky failures, regressions, a metric that has to cross a threshold).
---

# Running a search board

Use a board when the problem has a known **origin**, a **goal you can test**, and **no plannable
route** between them. Not for to-do lists, not for questions with an answer you can look up.

## Before opening one

Try the problem directly, once. Most problems are not actually hard. If the direct attempt works,
there is nothing to search.

## Opening

`search_board_open` with:

- **goal_test_kind `command`** whenever a shell command can tell success from failure
  (`pytest tests/test_flush.py --count 50`, `./bench.sh | awk '$2 < 120 {exit 0} {exit 1}'`).
  The exit status is a verdict you don't author. The test is fixed when the board opens.
- `judgment` only when nothing mechanical exists. Such a board can reach `proposed_solved` at most.
- A step budget that fits the problem (default 60). Every finding and every test spends one.

## The loop

1. **Read** (`search_board_read`). The operator's notes come first: weigh them, don't just obey them.
2. **Declare leads** (`search_board_leads`): concrete directions with a rationale that names the
   findings behind them. Seed leads at the start have no `from_findings`; later ones should.
3. **Claim one** (`search_board_claim`), then explore it with any tools.
4. **Record exactly one finding** (`search_board_finding`) against the claimed lead. Cite the
   `e<N>` evidence ids that the tool results you just received carry. A negative result is a
   finding with polarity `rules_out`, and most of a search is elimination.
5. **Rule out** leads that a `rules_out` finding eliminated (`search_board_rule_out`).
6. **Test** (`search_board_test`) when you think the goal is met. The plugin runs the command and
   writes the verdict. A failure is a finding: let it suggest the next leads.

## Closing

- A passing test closes the board `solved`. You can't close it solved yourself.
- `proposed_solved` (judgment/observation boards) and `proposed_exhausted` (frontier truly empty,
  with the argument why) wait for the operator.
- `abandoned` when the search isn't worth continuing.

## After it's finished: write-up, review, verification

A board that passed its test, or that you closed proposed_solved / proposed_exhausted, owes a
**write-up** (`search_board_report`): what was done, how it was checked, what is left or uncertain,
and the paths of the files that make up the work. The files are read now and kept as delivered.

1. **Review.** The operator reads it and may leave notes (R1, R2…). If they send it back, the board
   is open again in a new round: answer every note with `search_board_answer`, do the work, close
   it again (or pass the test), and write it up again.
2. **Verification.** If they send it on, check it yourself, independently: read what was delivered
   (`search_board_review`), run the test, try each claim. File it with `search_board_verify`. An
   independent verifier checks it too; you see its report only after filing your own.
3. **Reconcile** the two in `search_board_verification`: where they agree, where they differ and
   which is right, and the verdict. The operator accepts it as complete or sends it back.

Never write the verdict you hope for: a failed check that is honest is worth more than a pass.

## Subagents

Delegate a lead by giving the subagent the `search_board` toolset and the board's name and lead
id. It claims one lead at a time, must name that lead in its finding, can't open or close boards,
and its reads leave the operator's notes marked new for you. The write-up, review and verification are yours alone.
