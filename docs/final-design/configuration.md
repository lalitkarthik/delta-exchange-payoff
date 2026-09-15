# Configuration

**What this page contains.** Every setting the system reads, grouped by what it affects, with an
explanation of what each one does and what goes wrong if it is set incorrectly.

**How to read it.** Skim the first section, which explains where settings come from, and then jump
to the group you need. The groups are independent of each other. If a setting's purpose is unclear,
the page it belongs to -- linked at the end of each group -- explains the mechanism behind it.

## Where settings come from

Everything on this page is an **environment variable**: a named value the operating system hands to
a program when it starts. The program reads them once, at startup; changing one means restarting the
program.

**There is no configuration file to create, and no secret in the repository.** Delta's market data
is public, so there is no key to obtain and nothing to rotate. The only secret the system can hold
is a Discord address for forwarding alerts, and that lives in a file which is deliberately excluded
from version control.

For the Docker setup, `stack.env` holds the defaults and is checked into the repository precisely
because nothing in it is sensitive.

## Choosing how the system runs

These four settings decide the overall shape of what runs. The table below explains each.

| Setting | Default | What it does |
|---|---|---|
| `DELTA_BUS` | not set | Leaving it unset runs everything in one program. Setting it to `redis` runs the four-program arrangement |
| `DELTA_REDIS_URL` | none | Where to find Redis, such as `redis://redis:6379`. **Required** whenever `DELTA_BUS=redis` |
| `DELTA_LIVE_FEED` | `1` | Setting it to `0` runs everything normally but never connects to the venue |
| `DELTA_LIVE_UNDERLYINGS` | `BTC,ETH` | Which underlyings to subscribe to. The Docker setup narrows this to `BTC` alone |
| `DELTA_FEED_ADAPTER` | the real venue adapter | Which adapter to use. This is how the self-contained test substitutes a fake venue |

Two consequences are worth stating plainly.

**If Redis cannot be reached in the multi-program arrangement, the feed refuses to start.** It does
not start and quietly publish into nothing, because a feed that looks healthy while discarding
everything is the most damaging failure available.

**The feed program does not honour `DELTA_LIVE_FEED`.** Starting the Docker setup without the
fake-venue override therefore creates a genuine second connection to Delta alongside anything else
you have running.

## The message bus

These three control how messages are batched, how long they are kept, and how a reader identifies
itself. See [Message bus](message-bus.md) for what each mechanism does.

| Setting | Default | What it does |
|---|---|---|
| `DELTA_BUS_BATCH_MS` | `50` | How long the publisher gathers messages before sending them as a batch. **The system is sized for 50** |
| `DELTA_BUS_RETENTION_SECONDS` | `1800` | How long messages stay available on the bus. Thirty minutes is the promise made to a restarting recorder |
| `DELTA_BUS_INSTANCE` | `1` | The number identifying this copy of a program among others of the same kind |

Redis itself is configured on its own command line rather than through these variables:

```sh
redis-server --save "" --appendonly no --maxmemory 2gb --maxmemory-policy noeviction
```

All four of those matter, and the last one is a decision about losing data rather than about
performance. [Message bus](message-bus.md) explains why.

## The store

These control where data is written, how often, and when the recorder complains. See
[Data store](data-store.md).

| Setting | Default | What it does |
|---|---|---|
| `DELTA_STORE_ROOT` | `data/` | Where the files are written. The Docker setup uses `/data`, which maps to `.stack-data/` on the host |
| `FLUSH_SECONDS` | `300` | How often accumulated data is written out. **This is also how much work a crash would lose** |
| `STORE_STATE_INTERVAL_SECONDS` | `10` | How often the recorder reports on itself |
| `STORE_STATE_STALE_SECONDS` | -- | How old such a report may be before the API stops believing it |
| `STORE_BUS_MONITOR_INTERVAL_SECONDS` | `10` | How often the recorder checks whether it has fallen behind |
| `STORE_BUS_MONITOR_STALE_INTERVALS` | -- | How many silent intervals before a reader is presumed dead |
| `STORE_LAG_ALERT_ENTRIES` | -- | How far behind the recorder may fall before it raises an alert |
| `STORE_QUEUE_SIZE` | -- | The level at which a lossless backlog starts being reported |
| `STORE_CONTROL_QUEUE_SIZE` | -- | The same, for operator instructions |
| `STORE_COMMAND_ACK_TIMEOUT_SECONDS` | -- | How long to wait for the recorder to confirm an instruction |

One rule about locations is worth repeating: **the `data/` folder holds the real recorded history and
must never be written to by anything other than the live system.** In the Docker setup this is
enforced rather than trusted -- the recorder gets read-and-write access to the data folder and every
other container gets read-only access.

## Alerts

| Setting | Default | What it does |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | empty | Where alerts are posted. Leaving it empty simply disables posting |

The real value belongs in `stack.local.env`, a file excluded from version control which Docker loads
after the committed one. Nothing sensitive is ever committed.

## The web page

| Setting | Default | What it does |
|---|---|---|
| `NEXT_PUBLIC_ENGINE_URL` | `http://localhost:8000` | Where the browser should send its requests |

This one behaves differently from all the others and it catches people out. **Its value is baked
into the page when the front end is built, not read when it runs.** Changing it therefore requires
rebuilding the front end; restarting it will have no effect.

Its correct value depends on how the page is being served, as the table below shows.

| How the page is served | Correct value | Why |
|---|---|---|
| `bun run dev` on your machine | `http://localhost:8000` | The browser calls the back end directly, which is permitted for port 3000 |
| The Docker setup | `http://localhost:8080/api` | Everything goes through one proxy, so the browser only sees one address |
| Production, on Amplify | `/api` | The same idea: Amplify forwards anything starting with `/api` to the back end |

## Ports

The table below lists every port involved and what listens on it.

| Port | What |
|---|---|
| 8000 | The back end, when running it directly |
| 3000 | The front end, when running it directly |
| 8080 | The Docker setup's single entry point |

**The back end only permits browser requests from port 3000**, on either `localhost` or `127.0.0.1`.
Serving the front end from another port produces a page that loads and then stays empty, which looks
identical to the back end being down. The Docker setup and production both avoid the issue entirely
by putting everything behind one address.

## Docker setup overrides

These two exist so that a second copy of the Docker setup can run alongside one that is already
running, which is what the self-contained test needs.

| Setting | Default | What it does |
|---|---|---|
| `DXP_CONTAINER_PREFIX` | `dxp` | The prefix given to every container's name |
| `DXP_PROXY_PORT` | `8080` | The single port published to the host |

Both are needed together. Changing only the port is not enough, because container names must also be
unique on a machine -- two copies would collide on a name and the second would simply fail to start.

## Where to go next

[Getting started](getting-started.md) shows these settings in use.
[Deployment](deployment.md) describes how they are set in production.
