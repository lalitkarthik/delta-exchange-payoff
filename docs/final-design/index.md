# delta-exchange-payoff documentation

Option chain, volatility and payoff analysis for **Delta Exchange India** crypto options
(BTC, ETH). One websocket to the venue, one canonical event stream, a solved strike ladder on
screen once a second, and a Parquet store that folds the same stream into one-minute bars.

**These pages carry the decisions and nothing else** -- what the system does, how it is shaped, and
what it runs on. The alternatives weighed, the measurements taken and the records behind each choice
live under [`docs/design/`](../design/). Where a page here and a wire contract disagree about a
payload, the contract wins.

## Getting started

| Guide | What it covers |
|---|---|
| [Getting started](getting-started.md) | Install, run the engine and the web app, run the tests, bring up the Docker stack |
| [Overview](overview.md) | What the system does, the thesis behind it, and the feature set |

## Concepts

| Guide | What it covers |
|---|---|
| [Architecture](architecture.md) | Processes, threads, the pure core, the two topologies, and where it runs on AWS |
| [Adapters](adapters.md) | The Delta adapter, the adapter protocol, and how to add a venue |
| [Events](events.md) | The envelope and the ten canonical events |
| [Message bus](message-bus.md) | Queue policy, Redis Streams, acknowledgement, trimming, persistence |
| [Data store](data-store.md) | The five Parquet dataset roots, sealing, flushing and compaction |
| [Logging](logging.md) | The JSON record, the sinks, the event-name catalogue and levels |
| [Configuration](configuration.md) | Every environment variable, by process |
| [Deployment](deployment.md) | The production shape on AWS, sizing, cost and monthly operations |

## Reference

| Guide | What it covers |
|---|---|
| [API reference](api-reference.md) | Every REST route and the `/ws/chain` websocket |

## Conventions used throughout

- **Every number is tagged** `measured`, `derived` or `assumed`.
- **`null` is not `0`.** An absent quote is `null` even where the venue spells it `"0"`;
  a zero in open interest or a greek is a real zero.
- **Every decimal is a JSON number or `null`, never a string.** Converted once, at the
  adapter boundary.
- **Implied volatility is a decimal fraction on the wire and a percentage only on screen.**
  The engine never multiplies by 100.
- **Never forward-fill.** A minute with no arrivals produces no row -- not nulls, and never
  the previous close.
