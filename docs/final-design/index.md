# Documentation for delta-exchange-payoff

Welcome. This folder is the reader's guide to the project. It is written for someone who has never
seen this codebase before and does not necessarily know anything about options trading, so terms are
explained the first time they appear, and anything unexplained has an entry in the
[Glossary](glossary.md).

## What is this project, in one paragraph?

Delta Exchange India is an online marketplace where people buy and sell **options** on Bitcoin and
Ethereum. An option is a contract whose price depends on where the underlying coin's price is
expected to go. This project connects to that marketplace, continuously collects the prices being
quoted there, does its own mathematics on those prices to work out what the market is implying about
future volatility, shows the result on a web page that updates roughly once a second, and writes
every minute of what it saw into permanent files so the same analysis can be repeated on any past
day.

It is a study and analysis tool. **It does not place orders and it holds no money.** Everything it
reads is public information that anyone can fetch without an account.

## How to read these documents

The pages are meant to be read in an order. Pick the row that matches why you are here.

| If you are... | Read, in this order |
|---|---|
| **Completely new to the project** | [Overview](overview.md), then [Glossary](glossary.md), then [Architecture](architecture.md) |
| **Trying to run it on your machine** | [Getting started](getting-started.md), then [Configuration](configuration.md) |
| **Going to change the code** | [Architecture](architecture.md), then [Data flow](data-flow.md), then [Events](events.md), then the page for the part you are changing |
| **Connecting a new exchange to it** | [Events](events.md), then [Adapters](adapters.md), then [Adding an adapter](adding-an-adapter.md) |
| **Responsible for it running in production** | [Deployment](deployment.md), then [Logging](logging.md), then [Configuration](configuration.md) |
| **Writing something that reads our data** | [API reference](api-reference.md) and [Data store](data-store.md) |

If you only have twenty minutes, read the [Overview](overview.md) and then the first two sections of
[Architecture](architecture.md). That is enough to hold a conversation about the system. If you have
an hour, add [Data flow](data-flow.md) and [Events](events.md), and you will understand how the
pieces actually fit together.

## What is on each page

The pages fall into three groups. The first group tells you what the system is and how to start it.

| Page | What it contains |
|---|---|
| [Overview](overview.md) | What the system does and why it exists, with the options vocabulary explained as it goes |
| [Getting started](getting-started.md) | Step-by-step instructions to install it, run it, and check that it works |
| [Glossary](glossary.md) | Plain-English definitions of every term used anywhere in this folder |

The second group explains how the system is built. These are the pages to read before changing code.

| Page | What it contains |
|---|---|
| [Architecture](architecture.md) | The parts of the system, drawn as diagrams, with a description of what each part is responsible for |
| [Data flow](data-flow.md) | One price followed from the venue to the screen and the files, and what happens when the connection misbehaves |
| [Events](events.md) | The ten kinds of message the parts send each other, and what is inside each one |
| [Message bus](message-bus.md) | How those messages actually travel between the parts, and the rules that keep them from being lost |
| [Adapters](adapters.md) | What an adapter is, and how the connection to Delta Exchange is built |
| [Adding an adapter](adding-an-adapter.md) | A step-by-step guide to connecting a different exchange, and the naming conventions to follow |
| [Data store](data-store.md) | How the permanent record of past prices is written, organised and read back |
| [Logging](logging.md) | What the system writes down about its own behaviour, and how to read it when something goes wrong |

The third group is for running the system and for writing programs that talk to it.

| Page | What it contains |
|---|---|
| [Deployment](deployment.md) | Where the system runs in production on Amazon Web Services, and what that costs |
| [Configuration](configuration.md) | Every setting you can change, what it does, and what happens if you get it wrong |
| [API reference](api-reference.md) | Every web address the system answers on, and what it sends back |

## Conventions used in these documents

A few habits repeat across every page, and knowing them in advance saves confusion later.

**Numbers carry a tag.** A number marked `measured` was observed by running something. One marked
`derived` was calculated from measured numbers. One marked `assumed` is an educated guess nobody has
checked yet. The tag tells you how much to trust the figure.

**"Absent" and "zero" are different things.** If nobody is offering to buy a contract, its price is
*absent* -- written as `null`, which means "there is no value here". That is not the same as a price
of zero, which would mean somebody offered to buy it for nothing. The system keeps these apart
everywhere, and a lot of the design exists to protect that distinction.

**Where to find the reasoning.** These pages state what was decided. The alternatives that were
considered, the measurements taken, and the arguments behind each choice are recorded separately
under [`docs/design/`](../design/). If you want to know *why not something else*, that is where to
look.
