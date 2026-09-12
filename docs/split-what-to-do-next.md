# The split, what to do next

Open tickets only. Each item names the file or the command. Numbers carry their tag.

## Do now

1. **Re-collect one clean day.** The stack has been recording since 2026-09-12T15:52:27Z.
   Run `python tools/measure_containers.py` for a full day, then
   `python tools/measure_computed_gaps.py --root .stack-data --date <day>`. **One run closes
   two tickets** — #63's criterion 4 and #79's criterion 1 — and adds the two things the I13
   collection could not: `discord-alerts` inside the main run, and a `store` that consumes
   throughout. About a day of waiting, ten minutes of work.
2. **Read #111 before anything else in the code.** A lossless bus reader loses every batch
   whose pass fails after `XREADGROUP`. On the running stack the api's `bar-buffer` group held
   `measured` **35,312 pending of 121,056 read — 29.2%** and rising. It reports `lag 0`.
3. **Rebuild the stack after the next merge.** `discord-alerts`' `/health` changed shape and can
   now return **503**. The running container is on an older image. On rebuild it reports
   `unhealthy` whenever the last post failed. That is intended and will look like a regression.
4. **Get AWS credentials.** #70's criteria 2–4, #71 and #80 cannot start without them. #70's
   closing comment lists exactly what a person with credentials runs, in order.
5. **Decide the epic.** #57 cannot close while #70, #71 and #80 are open. Nothing else blocks it.

## Open, ranked by what it costs to leave alone

| # | What it is | Why it matters |
|---|---|---|
| **#111** | A lossless reader loses a batch after `XREADGROUP` and says `lag 0` | **Live data loss**, 29.2% of `md.option_bar` and rising |
| **#110** | A restart lost the open minute from `computed-bars` only, and left no record | A store restart is silent, so the mechanism is unknown |
| **#109** | A pause in split mode writes no generation and **no checkpoint** | The watermark stays behind bars on disk |
| **#108** | `feed` reports `status: ok` while stopped with its budget spent | A 348 s DNS failure kills it permanently |
| **#107** | The monolith builds its writer with no `publish` | `store.flush_failed` is unreachable on `:8000` |
| **#118** | `1.0888` and `1,056.4` each carry two tags | 1.0888 is what record 0008's 2.8-core threshold is read against |
| **#116** | `hld.md` names `/iv-vs-rv`, which 404s | Five routes the engine serves are not listed |
| **#117** | The R5 check passes with the counter behind a never-true condition | It catches structure, not evaluation |

## Waiting on a person, not on work

1. **#70, #71, #80** — AWS credentials. Nothing else is missing.
2. **#57** — the epic closes when those three do.

## The one thing to carry forward

**The defect shape here is not a crash. It is a value that quietly stopped meaning what it says.**

Eight instances were found on 2026-09-12. A counter that still increments. A status field that
is a literal. An exception a defensive handler swallows. A rule that silently became a special
case. **Every one of them left the system reporting success.**

Five questions found all eight. Use them:

1. **Ask the dependency, not the process.** `docker exec dxp-redis redis-cli XINFO GROUPS <stream>`
   tells you what a `/health` route cannot.
2. **Ask what has never executed.** `git log -S <symbol>`, read against the tests that claim to
   cover it. Record 0010 R4 had never once run.
3. **Ask where a denominator comes from** — the data, or the request. A stall shortens a span
   rather than holing it.
4. **Ask what a counter reads if the thing it counts never happens.** Usually zero, which reads
   the same as healthy.
5. **When an A/B result comes out clean, ask what else differs between the arms.**

**And one rule for tests: mutate, do not read.** Delete the guard, no-op the commit, force the
cadence, revert the fix. A test that stays green under the mutation it claims to catch is the
finding. Ten were found here this week and none by inspection.

## Next action

Start the day's collection: `python tools/loadgen.py check`, then leave the stack alone.
