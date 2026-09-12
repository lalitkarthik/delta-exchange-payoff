# 0010b — the owner's questions on store replay, and the answers

Moved here from [../decisions/0010-store-replay.md](../decisions/0010-store-replay.md) when
that record reached its 200-line bound (#109 and #110, 2026-09-12). **Settled, and kept
verbatim.** The rules in the record carry their outcome; nothing here is open.

## What the repository owner should decide, not this record

1. **Acceptance criterion 1 for table C.** Tables A, B and D can be compared column for column
   against the in-process recording. Table C cannot: two processes recompute on their own
   timing, so the values differ below the second even when the minute keys match. Say what the
   proof is — key sets equal, or a single-process comparison against the event stream.
2. **The read paths' right edge in split mode.** With no writer in the api, `/smile`,
   `/history` and contract bars lose `BarStore.pending()` and read only what is on disk: up to
   one flush interval plus the open minute behind. Accept it, or open a ticket to feed the api
   the `md.option_bar` events the catalogue already defines.
3. **R8, once more.** A separate event type removes the hazard structurally; the settled
   wording says `control.command`. If the wording is reopened, everything else in this record
   stands unchanged.
4. **The store's graceful stop no longer writes partial open-minute bars** — it checkpoints
   them instead, so a restart within the retention window completes the minute rather than
   sealing a truncated one. A stop longer than thirty minutes loses those minutes, and the gap
   signal reports it. This changes a rule stated in `lld/store.md` §4 for the store process.
5. **The cutover from I3 to I4.** The first start has no checkpoint and begins at the head, so
   the seconds between stopping the old writer and starting `store` are not recorded. The
   alternative — starting at `0` per 0002 — would re-record up to thirty minutes the engine had
   already written, as duplicates.

## The owner's answers, 2026-09-12

1. **Table C is compared numerically, within a stated tolerance** — not by key sets alone.
   The tolerance is justified by what actually differs, two processes recomputing against a
   moving book, and is measured rather than chosen to pass. #63 carries it as a named
   constant.
2. **The read paths' right edge is not accepted as lost.** [#81](https://github.com/lalitkarthik/delta-exchange-payoff/issues/81)
   gives `/smile`, `/history` and the contract-bar routes their live edge back by folding
   `md.option_bar` in the api. #63 does not fix it, and the store LLD states the lag as
   current behaviour until #81 lands.
3. **R8 stands**: `control.command` with `target`, and the feed supervisor drops what is not
   addressed to it.
4. **R4's graceful stop stands**: the store checkpoints partial open-minute bars rather than
   writing a truncated minute.
5. **The I3 → I4 cutover's few unrecorded seconds are accepted**; re-recording thirty
   minutes as duplicates is refused.
