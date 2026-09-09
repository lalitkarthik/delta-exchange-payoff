# R1 measurement run — payload size on Redis Streams

The run behind every number in
[0001-stream-naming-and-payload-format.md](0001-stream-naming-and-payload-format.md). Kept whole
so the tables there can be checked rather than trusted.

## How to reproduce

```
docker run -d --name r1-redis -p 6399:6379 redis:7-alpine --save '' --appendonly no
cd engine
.venv/Scripts/python.exe ../tools/measure_payload_size.py
```

Without a Redis on `127.0.0.1:6399` the byte counts still print and the Redis columns say so.
`msgpack` and `protobuf` must be in the venv. The events are the committed fixtures
`engine/tests/fixtures/ws-ob-l2-04-09-2026.json` and `ws-ticker-04-09-2026.json`, decoded by the
real `adapters.DeltaAdapter`, so the run touches no network and no venue.

**Run-to-run variation is about ±0.5%** on the Redis columns, because `MEMORY USAGE` reports
allocator-rounded sizes. The run below is 2026-09-09, Redis 7.4.11 in Docker on the development
laptop, Python 3.13 in `engine/.venv`.

## What each encoding is

| | Redis fields | Payload |
|---|---|---|
| A `json:one-field` | `payload` | the whole event as JSON |
| B `json:envelope-flat` | the envelope, one field each, plus `payload` | the type's own keys as JSON |
| C `msgpack:envelope-flat` | the same flat envelope, plus `payload` | the type's own keys as MessagePack |
| D `msgpack:one-field` | `payload` | the whole event as MessagePack |
| E `protobuf:one-field` | `type`, `payload` | the whole event as protobuf |

The protobuf encoder is written to the wire format by hand — `protoc` is not installed — and is
checked against the `google.protobuf` runtime on a fully populated message at the top of every
run. The line `verified byte-for-byte against google.protobuf` in the output below is that check.

## The run

```
====================================================================================
R1  PAYLOAD SIZE ON REDIS STREAMS
====================================================================================
fixtures      : D:\Convex Hedge\delta-exchange-payoff\engine\tests\fixtures
  md.option_quote         136 real events via adapters.DeltaAdapter
  md.option_reference     136 real events via adapters.DeltaAdapter
  md.index_quote          136 real events via adapters.DeltaAdapter
protobuf      : verified byte-for-byte against google.protobuf
redis         : up at redis://127.0.0.1:6399

------------------------------------------------------------------------------------
1. ONE NAMED EVENT, BYTES  (payload field / whole XADD entry incl. field names)
------------------------------------------------------------------------------------

md.option_quote   P-BTC-75600-040926
    encoding                     payload     entry   fields  vs JSON-1
    A json:one-field                 415       422        1      1.00x
    B json:envelope-flat              95       329        9      0.78x
    C msgpack:envelope-flat           76       310        9      0.73x
    D msgpack:one-field              246       253        1      0.60x
    E protobuf:one-field             156       182        2      0.43x

md.option_reference   P-BTC-78500-040926
    encoding                     payload     entry   fields  vs JSON-1
    A json:one-field                 668       675        1      1.00x
    B json:envelope-flat             344       582        9      0.86x
    C msgpack:envelope-flat          281       519        9      0.77x
    D msgpack:one-field              455       462        1      0.68x
    E protobuf:one-field             258       288        2      0.43x

md.index_quote   BTC
    encoding                     payload     entry   fields  vs JSON-1
    A json:one-field                 237       244        1      1.00x
    B json:envelope-flat              35       202        7      0.83x
    C msgpack:envelope-flat           30       197        7      0.81x
    D msgpack:one-field              173       180        1      0.74x
    E protobuf:one-field              75       100        2      0.41x

------------------------------------------------------------------------------------
2. MEAN OVER EVERY FIXTURE EVENT, AND WHAT REDIS ACTUALLY HOLDS
------------------------------------------------------------------------------------

md.option_quote   n=136
    encoding                    mean entry   redis/entry   overhead
    A json:one-field                424.0B        487.9B      63.9B
    B json:envelope-flat            331.0B        315.6B     -15.4B
    C msgpack:envelope-flat         310.0B        294.6B     -15.4B
    D msgpack:one-field             253.0B        294.1B      41.1B
    E protobuf:one-field            182.0B        211.5B      29.5B

md.option_reference   n=136
    encoding                    mean entry   redis/entry   overhead
    A json:one-field                665.3B        735.9B      70.6B
    B json:envelope-flat            572.9B        627.9B      55.1B
    C msgpack:envelope-flat         517.1B        549.1B      32.0B
    D msgpack:one-field             460.1B        548.1B      88.0B
    E protobuf:one-field            285.9B        317.7B      31.8B

md.index_quote   n=136
    encoding                    mean entry   redis/entry   overhead
    A json:one-field                244.0B        275.5B      31.5B
    B json:envelope-flat            202.0B        187.0B     -15.0B
    C msgpack:envelope-flat         197.0B        186.4B     -10.6B
    D msgpack:one-field             180.0B        211.6B      31.6B
    E protobuf:one-field            100.0B        115.5B      15.5B

------------------------------------------------------------------------------------
3. MEMORY AT THIRTY MINUTES' RETENTION
------------------------------------------------------------------------------------
  frames/s            1693.6   measured, tools/measure_feed.py, 2026-09-08
  book frames/s       1537.4   derived, split by the 508 ms / 5,001 ms intervals
  ticker frames/s      156.2   derived, same split
  events/s            1849.8   derived, one ticker frame is two events
  retention             1800 s

    encoding                       bytes/s      30 min    (measured on Redis)
    A json:one-field               886.8K    1558.7M
    B json:envelope-flat           598.2K    1051.5M
    C msgpack:envelope-flat        554.5K     974.8M
    D msgpack:one-field            557.4K     979.7M
    E protobuf:one-field           383.7K     674.4M

------------------------------------------------------------------------------------
4. WHAT SPLITTING PER CONTRACT COSTS  (encoding B, 100 entries per stream)
------------------------------------------------------------------------------------
  13,600 entries in 1 stream    :    4,316,152 bytes
  13,600 entries in 136 streams :    4,968,508 bytes
  cost of the split           :      652,356 bytes (4,797 B per extra stream key, 1.15x)

------------------------------------------------------------------------------------
5. WHY ONE STREAM PER EVENT TYPE IS CHEAPER THAN ONE MIXED STREAM
------------------------------------------------------------------------------------
  12,000 entries, 3 streams one per type :    4,534,628 bytes
  12,000 entries, 1 stream interleaved   :    5,115,188 bytes
  the mixed stream costs        :      580,560 bytes more (1.13x), 48.4 B per entry
```

## Reading §4 and §5

**§4 is not a memory argument against per-contract streams.** 15% is affordable; the argument in
the findings file is criterion 1, discovery. What §4 does give is `measured` 4,797 bytes for a
stream key that exists at all, which is the number to reach for when someone proposes thousands of
them. It is measured at 100 entries a stream rather than one, because a stream holding a single
entry is dominated by the listpack pre-allocation a filling node later shrinks away.

**§5 is the master entry, priced.** Three streams of one event type each against one stream
carrying all three interleaved, the same 12,000 entries either way: 48.4 bytes an entry more when
the field set stops repeating. It is why the flat envelope is free in B and C, and it only holds
while a stream carries one event type.

## What is not measured here

- The other six event types. `computed.chain` is `derived` under 1% of the market-data volume at
  one recompute pass a minute per live expiry; the other five are rare by construction. Neither is
  measured, and #57's memory budget should say so.
- The CPU cost of each encoding, and the latency it adds. #57's I2 measures the batch interval,
  which is the term that dominates.
- Anything at ten times the rate. Those columns in the findings file are `derived` by
  multiplication and assume the per-entry size does not change with volume, which is true for the
  listpack but untested above 20,000 entries a stream.
