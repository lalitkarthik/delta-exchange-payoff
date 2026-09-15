# Data store

**What this page contains.** How the permanent record of past prices is organised, how it gets
written, what happens after a restart, how it is read back, and why it is tidied up every night.

**How to read it.** The first two sections describe what is on disk and are useful on their own.
The rest follows the life of a minute of data from arrival to long-term storage; read it in order.
[Data flow](data-flow.md) covers the earlier part of that journey, before the store sees anything.

## What the store is for

Everything the system sees is fleeting. The venue sends a price, it is displayed, and a moment later
it is replaced by a newer one. The store exists so that any past minute can be examined again: to
check what the market was doing at a particular time, to compare what we computed against what
happened next, or to test an idea against months of real data.

It holds **one-minute summaries, not individual prices.** Every observation of a contract within one
minute is folded into a single row recording the first, highest, lowest and last values seen, and
how many observations there were.

## Five separate collections

The record is deliberately split into five collections rather than one big table, so that what we
observed, what the venue claimed, and what we concluded are never confused with each other. The
table below names each one.

| Collection | What it holds |
|---|---|
| `quote-bars` | What the order book did -- bids, asks, and the sizes offered |
| `reference-bars` | What the venue said -- its mark price, open interest, turnover, and its own volatility figures |
| `spot-bars` | What the underlying itself was worth |
| `computed-bars` | What this project concluded -- our expected future price, our volatility, our Greeks |
| `index-bars` | Longer-range index history, filled in separately by a tool rather than by the live system |

Keeping them apart has a practical benefit beyond tidiness. Each collection has its own set of
columns, so a program reading one of them opens files containing only the columns it cares about. A
single combined table would force every read to carry a filter, and would make every file a mixture
of five different shapes.

All five share the same two organising keys, so joining them together -- our volatility beside the
venue's, beside the underlying's price, for the same minute -- needs no translation.

## How the files are organised

Each collection is a folder tree, and the folder names contain the information needed to skip
irrelevant files without opening them:

```
quote-bars/underlying=BTC/date=2026-09-08/20260908T131500Z-000287.parquet
```

This naming style is a widely used convention, so other tools recognise it without being configured.
A program looking for Bitcoin data on 8 September can see from the folder names alone that every
other folder is irrelevant.

**Only the underlying and the date are folders. Everything else is a column.** It is tempting to
make the expiry a folder too, but there are thousands of expiry-and-strike combinations and doing so
would produce thousands of folders each holding a handful of rows -- which is slow on a disk and
expensive on cloud storage, where every file costs money to list and to open.

One implementation detail is worth knowing because it has bitten before. The library we use to write
Parquet files offers to lay out these folders for you, and if allowed to, it names every file it
writes identically. The ten o'clock write would silently replace the nine o'clock one. So the
folders are created by hand, every file gets a unique name, and there is a test that fails if that
ever stops being true.

## From an observation to a file

The journey has four stages.

**1. Bucketing.** Each observation is placed in the bucket for the minute it happened in, according
to **the venue's timestamp, not ours**. If we used our own arrival time, network delay would
occasionally push an observation into the wrong minute.

**2. Sealing.** When a minute is over, and a short grace period has passed to allow for messages
still in transit, the bucket is *sealed* and nothing further can be added. The two venue streams get
different grace periods, because their delays differ by roughly a factor of ten; a single grace
period would either seal the slow stream too early and lose data, or make everything wait for it.

**3. Accumulating.** Sealed minutes pile up in memory. They are announced on the bus as they are
sealed, so other parts can use them immediately without waiting for a file.

**4. Writing.** Every five minutes, everything accumulated is written out -- one file per collection
per underlying per write. The write happens on a separate thread so that it cannot pause the part of
the program reading from the venue.

That five-minute interval is worth understanding, because **it is exactly the amount of work that
would be lost if the program were killed at the worst possible moment.** That is why the store also
keeps track of how far it had read, so it can go back and recover after a restart.

**A minute in which nothing arrived produces no row and no file.** Not a row of blanks, and never a
copy of the previous minute. This is the most important rule in the whole store.

## Restarting without losing or duplicating data

After each successful write, the store records how far through the message stream it had got. On
restart, it carries on from that recorded position -- never from the beginning, which would
re-record everything and produce duplicates, and never from wherever the stream happens to be now,
which would silently skip whatever arrived while it was down.

Sometimes the messages it needs have already been discarded by the bus, which only keeps thirty
minutes. When that happens the store does not refuse to start. It reads whatever is still available,
reports exactly how many entries it lost and between which two points, raises an alert, and carries
on. A visible, counted gap is recoverable; a silent one is not.

There is one subtlety while it is catching up. During a catch-up, "now" is taken from the timestamps
inside the messages being processed rather than from the clock on the wall. Without that, the first
pass after an outage would consider every recovered minute to be hopelessly late and discard the
very data it had just gone to the trouble of recovering.

## Reading it back

All four ways of reading are the same operation against the same folder tree, with filters that the
folder names answer before any file is opened. The table below lists them.

| What asks | What it reads |
|---|---|
| The historical chain screen | One minute, across four collections |
| The contract chart | One contract's whole day, across two collections |
| The volatility screen | One expiry's stored volatility |
| A backtest, or a person exploring | Whole days, across all collections |

In the multi-process arrangement the API also keeps a short in-memory copy of the most recently
sealed minutes, received over the bus. That is what lets it answer questions about the last few
minutes accurately even though it is not the program writing the files.

**A program reading this data does not have to be written in our language.** The folder layout and
file format are both standard, so DuckDB, Athena, pandas or anything else reads the same files
directly, with no export step and no service that has to be running.

## Compaction: tidying up overnight

Writing every five minutes produces a great many small files -- around two thousand for a busy day.
Small files are slow to read and, on cloud storage, expensive: every file costs a request to find
and another to open, and a single historical query might touch a thousand of them.

So once a night, a tool merges each finished day's files into one file per collection. A real day
folds from around two thousand files into eight, and gets about a sixth smaller into the bargain.
The same query afterwards touches four files instead of over a thousand.

Because this deletes data, the order of operations is careful and deliberately paranoid:

1. Read every file in the day's folder.
2. Write the merged result to a temporary file.
3. **Read that temporary file back in full** and check it contains what it should.
4. Write a small manifest recording what was merged into what.
5. Only then delete the original files, and publish the merged one.

The principle behind that ordering is that **a missing file is a visible problem that can be fixed,
while a duplicated one is invented data that may never be noticed.** If the process is interrupted,
the manifest is what makes the state recoverable.

It runs overnight on finished days rather than continuously on today's data. Parquet files cannot be
appended to, so merging today's folder would mean rewriting it repeatedly while the store is still
writing into it -- a lot of machinery, running beside the live writer, to speed up a query that is
already fast enough.

## Storage in production

In production the files live in Amazon S3 rather than on a local disk, in one bucket per
environment, and the code path is unchanged: the same call, a different root location. A few
settings on that bucket are chosen deliberately, and the table below explains them.

| Setting | Choice | Why |
|---|---|---|
| Keeping old versions of files | Off | Compaction's safety argument depends on deleted files actually being gone. Kept versions would let a careless listing read the day twice |
| Public access | Blocked | Nothing here should be reachable from the internet |
| Encryption | On, managed by Amazon | No reason not to |
| Automatic deletion | None | Bars are kept indefinitely. The only rule cleans up half-finished uploads |
| Storage class | Standard | The cheaper classes charge a minimum size per file, and before compaction most of our files are far below it -- one collection's files would be billed at fifty times their actual size |

The storage bill for both underlyings comes to roughly two dollars a month, which is set out
alongside the rest of the costs in [Deployment](deployment.md).

## Where to go next

[Data flow](data-flow.md) covers what happens before the store sees a message.
[API reference](api-reference.md) lists the web addresses that read this data.
