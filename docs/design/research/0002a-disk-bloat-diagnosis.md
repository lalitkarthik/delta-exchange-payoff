# The Docker Desktop disk growth: a diagnosis to run, not a conclusion to send

Part of #69. The hosting decision is
[../decisions/0002-redis-hosting.md](../decisions/0002-redis-hosting.md); this file is the
separate question the same ticket asks.

**What the senior said, 2026-09-07:** "I am also looking for alternative to redis streams
like kafka as there is a disk bloating issue with redis in docker desktop which we need to
clean manually daily or thrice a week."

**I cannot run any of this.** It is his machine, his Docker Desktop, his Redis. Everything
below is written so that he can run it in about twenty minutes and the answer falls out of
the output. Nothing here concludes anything about his system; the three candidate causes
are from the vault's `teach/message-bus/the-disk-bloat.md` and the point of the checklist is
to tell them apart.

## The message to send

Short, and it asks the question rather than answering it.

> Before we switch — can we find out whether the growth is inside Redis or is the Docker
> VHDX not shrinking? `docker system df` against the actual size of `docker_data.vhdx` will
> say it in one line: if Docker thinks it's using a few hundred MB and the .vhdx is tens of
> GB, the space was freed inside the Linux VM and Windows never took it back, and no broker
> changes that — Kafka would land in the same Docker Desktop and keep more on disk by
> design.
>
> If it *is* Redis, the two things worth checking are `XLEN` on each stream (a stream with
> no `MAXLEN`/`XTRIM` grows forever — and `XACK` frees nothing, only trimming does) and
> whether `appendonly` is on. I've written the exact commands and what each result means,
> five steps, about twenty minutes — happy to sit with you while it runs.
>
> Separately: independent deployability is a real argument for looking at Kafka, and it's
> worth having on its own terms. It's just not a disk-space fix.

## The checklist

Steps 1–5 are **read-only**: they list, measure and print. Nothing in them deletes,
restarts or reconfigures anything. Step 6 changes the machine and is his call, not ours.

Run PowerShell as the same user that runs Docker Desktop. Replace `<redis>` with the
container name from `docker ps`.

### 1. What Docker thinks it is using

```powershell
docker system df
docker system df -v          # per image, per container, per volume
```

*Reads:* the totals across images, containers, volumes and build cache.
**Write the total down.** `RECLAIMABLE` is what a prune would return; the `SIZE` column is
what is actually held.

### 2. What Windows says the virtual disk is

```powershell
Get-ChildItem "$env:LOCALAPPDATA\Docker\wsl" -Recurse -Filter *.vhdx |
  Select-Object FullName, @{n='GB';e={[math]::Round($_.Length/1GB,2)}}
```

*Reads:* the size of `docker_data.vhdx` (Docker's data) and `ext4.vhdx` (its Linux VM).

**This is the step that usually settles it.** A large gap between step 1 and step 2 means
the space was freed inside the guest and never returned to Windows — **cause 3, and no
broker change fixes it**. WSL 2 "automatically resizes these VHD files to meet storage
needs" and Microsoft's own guidance notes that "the process of reducing a virtual disk size
is much more complicated"
([manage WSL disk space](https://learn.microsoft.com/en-us/windows/wsl/disk-space)).
Docker Desktop does not even offer a disk-size control on the WSL 2 backend: **Disk usage
limit** and **Disk image location** are listed for "Mac, Linux, Windows Hyper-V"
([settings](https://docs.docker.com/desktop/settings-and-maintenance/settings/)).

*For scale, `measured` on this machine, 2026-09-09, Docker Desktop 29.7.2, WSL 2.7.8.0, with
two small containers running:* `docker system df` totals **571 MB**; `docker_data.vhdx` is
**4.1 GB**. **A 7.2x gap on a machine nobody would call bloated.** His gap is the number
that matters; this one only shows that the gap is normal and grows in one direction.

### 3. How long each stream actually is

```powershell
docker exec <redis> redis-cli --scan --type stream
docker exec <redis> redis-cli XLEN <each stream name>
docker exec <redis> redis-cli XINFO STREAM <each stream name>
```

*Reads:* the stream names, their lengths, and per stream `length`, `radix-tree-keys`,
`max-deleted-entry-id`, `entries-added`.

**Millions of entries, or `entries-added` far above `length` never moving, means cause 1** —
`XADD` with no `MAXLEN` and no `XTRIM` job. The fix is one argument on the write, not a new
broker:

```
XADD <stream> MAXLEN ~ 100000 * field value      # cap by count
XTRIM <stream> MINID ~ <id>                      # or cap by age, Redis 6.2+
```

**The trap to name out loud: `XACK` does not free memory.** Acknowledging removes an entry
from the group's pending list; the entry stays in the stream until it is trimmed.
`measured` here, 2026-09-09, `tools/measure_redis_hosting.py`: 5,000 entries acked, `XLEN`
still 5,000, `MEMORY USAGE` unchanged at 1.81 MB; only `XTRIM` moved it, to 730 KB. A system
that acks diligently and never trims grows exactly as fast as one that does neither. If he
uses Nautilus Trader's Redis backing, `autotrim_mins` is the setting to check.

### 4. How much Redis itself holds

```powershell
docker exec <redis> redis-cli INFO memory | Select-String "used_memory_human|used_memory_rss_human|maxmemory"
docker exec <redis> redis-cli CONFIG GET maxmemory-policy
```

*Reads:* what the process actually holds, and whether there is a ceiling at all.

**If `used_memory_human` is small while the .vhdx is large, Redis is not the growth** —
whatever grew is on disk, not in memory, which points at step 5 or at cause 3. If
`maxmemory` is `0` there is no ceiling, and under `noeviction` a full machine is an error
while under `allkeys-lru` Redis silently deletes whole keys — for streams, whole streams
([key eviction](https://redis.io/docs/latest/develop/reference/eviction/)).

### 5. Whether it is writing to disk at all

```powershell
docker exec <redis> redis-cli CONFIG GET appendonly
docker exec <redis> redis-cli CONFIG GET save
docker exec <redis> redis-cli CONFIG GET dir
docker exec <redis> sh -c "ls -la /data /data/appendonlydir 2>/dev/null; du -sh /data"
docker exec <redis> redis-cli INFO persistence | Select-String "aof_enabled|aof_base_size|aof_current_size|rdb_last_bgsave_status|rdb_changes_since_last_save"
```

*Reads:* both persistence mechanisms and the files they leave.

**A large `aof_current_size`, or a large `/data`, is cause 2.** With `appendonly yes` Redis
logs every write; the rewrite threshold is a setting and a high write rate against a small
threshold means constant rewriting and a large working set.

**Two things worth expecting even when nobody turned persistence on.** `redis:7-alpine`
ships **`save 3600 1 300 100 60 10000`** and `appendonly no` (`measured`, 2026-09-09) — so
snapshotting is on by default, and at a busy write rate the "10,000 changes in 60 seconds"
rule fires every minute. And if durability is really coming from **reconciliation against
IBKR** — which is what he said on 2026-09-07 — then the AOF is buying very little, and its
cost is worth re-examining on those terms.

## Reading the result

| What the steps show | Cause | What actually fixes it |
|---|---|---|
| Step 1 total small, step 2 .vhdx large | **3 — the VHDX never shrank** | Reclaim, below. **No broker change touches this** |
| Step 3 shows millions of entries, or `entries-added` climbing with `length` | **1 — streams never trimmed** | `MAXLEN ~` or `XTRIM MINID ~` on the write path. One line |
| Step 5 shows a large AOF or `/data` | **2 — AOF growth** | `appendonly no` if IBKR reconciliation is the real durability, or tune the rewrite threshold |
| Step 4 shows Redis holding gigabytes in memory | none of the three — it is a working-set problem | Cap it: `maxmemory` plus a chosen `maxmemory-policy` |

More than one can be true at once. Steps 1 and 2 come first because cause 3 is the only one
that survives every other fix.

## Step 6 — reclaiming, if it is cause 3

**This stops Docker and is his decision to make, not ours.** The order matters, and skipping
the first step is why most attempts fail.

```powershell
# 1. mark the freed blocks as discarded, from inside the guest
wsl.exe -d docker-desktop -e fstrim -av

# 2. then let Windows take the space back. Either:
wsl.exe --shutdown
wsl.exe --manage docker-desktop --set-sparse true      # WSL 2.5+, ongoing
# or, once, on the file itself:
Optimize-VHD -Path "$env:LOCALAPPDATA\Docker\wsl\disk\docker_data.vhdx" -Mode Full
# (Windows Home has no Optimize-VHD: diskpart -> select vdisk file="..." -> compact vdisk)
```

`--set-sparse` is the durable form: `wsl --help` describes it as "Set the VHD of distro to
be sparse, allowing disk space to be automatically reclaimed" (`measured`, WSL 2.7.8.0,
2026-09-09).

**Rule out one more thing before blaming Redis at all:** Docker Desktop has a documented
leak of stale ISO blobs under `isocache`, reported at 7.5–25 GB and invisible to
`docker system prune` — [docker/desktop#375](https://github.com/docker/desktop/issues/375).

## Why Kafka is the wrong instinct for this particular problem

**Kafka's whole design is to keep data on disk for a configured retention period.** Redis
Streams hold data in memory and touch disk only as a persistence side-effect. Swapping an
in-memory log for a disk-first log to solve a disk problem is backwards — and it would run
in the same Docker Desktop, so cause 3 would follow it across unchanged.

The good argument for Kafka is the one he already made himself: independent deployability,
and a retention model that suits several consumers replaying history at their own pace. That
is worth having on its own terms. It is not a disk-space fix.
