# Units reconciliation for research 0001 (#95)

> Evidence for [0001-stream-naming-and-payload-format.md](0001-stream-naming-and-payload-format.md).
> Moved out of that record 2026-09-12 so it stays a research record, and stays under the
> 200-line bound `AGENTS.md` sets.

**Units corrected 2026-09-12 (#95). The values did not change; the labels were wrong.**
This table read `KB/s`, `MB` and `GB`. Every row reconciles only as binary: 598.2 x 1024 x
1800 = 1,102,602,240 bytes, which is **1,051.5 MiB exactly** and 1,102.6 MB. Checked on all
five rows — A 1558.8, B 1051.5, C 974.7, D 979.8, E 674.5 against the stated 1558.7, 1051.5,
974.8, 979.7, 674.4 — and the decimal reading matches none of them. This mattered: record
0002 rejects `cache.t4g.small` on a margin of 692,060 bytes, **0.0628%**, computed from this
figure. Read as decimal MB the node would have fitted by 4.9% and the rejection would have
needed a different reason.
