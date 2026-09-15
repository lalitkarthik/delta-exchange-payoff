# Getting started

**What this page contains.** Everything needed to get the project running on your own machine, from
installing the tools to seeing a live option chain in a browser, followed by how to run the tests
and how to start the full multi-program setup.

**How to read it.** Work through it in order the first time. The three ways of running it are
independent -- pick the first one to begin with, and come back for the others when you need them.
If a term is unfamiliar, the [Glossary](glossary.md) has it.

## What you will need

The table below lists everything that must be installed, and why.

| Tool | Version | What it is for |
|---|---|---|
| Python | 3.13 | The back end. Version 3.12 works, with one known test failure |
| Bun | Current | The tool that installs and runs the front end. **Not npm and not pnpm** |
| Docker | 29 or later | Only needed for the full multi-container setup and a few tests |

**You do not need an account, an API key, or any credentials.** Everything this project reads from
Delta Exchange is public. There is no `.env` file to create and no secret to obtain.

One quirk to know about in advance: Delta's servers reject any request that does not identify itself
with a `User-Agent` header, answering with an error page instead of data. The code always sets one.
If you try a request by hand with `curl` and get an unexpected error, that is usually why.

## Installing

Two installations, one for each half of the project.

For the back end, from inside the `engine/` folder, create an isolated Python environment and
install into it:

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt          # macOS and Linux
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt  # Windows
```

There are two requirement files. `requirements.txt` lists what is needed to run the software;
`requirements-dev.txt` includes those and adds the testing tools. Install the second one unless you
have a reason not to.

Note that there is no step that installs the project itself as a package. Instead, the command that
starts it points at the source folder directly, which is what the `--app-dir src` argument below is
doing.

For the front end, from inside the `web/` folder:

```sh
bun install
```

## The simplest way to run it

This runs everything in one program, which is the easiest arrangement to work with. Start the back
end first, from `engine/`:

```sh
.venv/bin/python -m uvicorn --app-dir src deltapayoff.main:app --port 8000 --reload
```

Then start the front end, from `web/`:

```sh
bun run dev
```

Open http://localhost:3000. You should see the option chain filling in within a few seconds.

**The front end must be on port 3000.** For safety, browsers refuse to let a page call a server at a
different address unless that server explicitly permits it, and the back end permits port 3000 and
nothing else. If you serve the front end from any other port, the page will load but stay empty, and
it will look exactly as though the back end is not running.

### Running without connecting to the venue

Sometimes you want the back end running -- to work on the historical screens, or to run something
against it -- without opening a live connection to Delta. Set one variable:

```sh
DELTA_LIVE_FEED=0 .venv/bin/python -m uvicorn --app-dir src deltapayoff.main:app --port 8000
```

Everything answers normally; nothing connects to the venue. This is also what the test suite does.

## Running the full multi-program setup

In production the work is divided between four programs and a Redis server. The easiest way to run
that arrangement locally is with Docker, which starts all of it together. From the top of the
repository:

```sh
docker compose --project-name dxp --env-file stack.env up -d --wait --wait-timeout 120
```

Then open http://localhost:8080. To stop it, use the same project name -- never a general cleanup
command, which would also remove containers belonging to other work:

```sh
docker compose --project-name dxp --env-file stack.env down --remove-orphans
```

Three things about this setup are worth knowing.

**It uses port 8080 and nothing else.** A development back end already owns 8000 and a development
front end owns 3000, so the containers deliberately avoid both. Everything reaches the browser
through a single small proxy, which means the browser only ever talks to one address -- and the
browser safety rule described above never comes into play at all.

**It writes its data somewhere separate.** The containers write to `.stack-data/`, never to the
`data/` folder, which holds the real recorded history and is read-only.

**It connects to the real venue.** Starting it creates a genuine second subscriber alongside
anything else you have running. That is within what the venue allows, but do not leave it running by
accident.

The whole stack takes about fifteen seconds to become ready.

### Proving it works without touching the venue

There is a self-contained check that starts the stack with a *fake* venue that replays a recorded
script, drives the web page and the API, and then verifies that a real data file appeared on disk:

```sh
engine/.venv/bin/python tools/smoke_stack.py --project-name smoke65 --proxy-port 8099
```

Always pass those two arguments if you already have a stack running, since the script shuts down
whichever project it was given when it finishes.

## Running the tests

From `engine/`:

```sh
.venv/bin/python -m pytest -q      # the whole suite
.venv/bin/python -m ruff check .   # the style and correctness checker
```

The suite is about **1,610 tests** and takes seconds. If Docker is not available, the tests that
need Redis are skipped rather than failed, so the number reported as passing will be lower while the
number collected stays the same. **If far fewer than 1,610 tests are collected, something failed to
load** -- that is a real problem regardless of what the summary says.

From `web/`:

```sh
bun run typecheck
bun run test
bun run build
```

**No test is allowed to touch the network.** The test setup disables the live connection and
replaces the thing that makes web requests with one that raises an error, so a test that tries to
reach the internet fails loudly rather than passing slowly and unpredictably.

## Useful tools

The `tools/` folder holds small standalone scripts that are not part of the running system. They
answer questions about the venue and about the stored data without either half of the project
needing to be running. The table below lists the ones most people want first.

| Script | What it tells you |
|---|---|
| `measure_feed.py` | How often the venue actually sends each kind of update |
| `measure_arrival_lag.py` | How long messages are taking to reach us |
| `measure_store.py` | What is in the stored files and how much space it uses |
| `probe_api.py` | What the venue's ordinary web API returns |

## Where to go next

[Overview](overview.md) explains what you are looking at on screen.
[Architecture](architecture.md) explains how the parts you just started fit together.
[Configuration](configuration.md) lists every setting you can change.
