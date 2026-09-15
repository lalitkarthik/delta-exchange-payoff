# Glossary

This page defines every term used in the rest of this documentation. It is meant to be kept open in
a second tab while you read the other pages rather than read straight through, but reading it
straight through is a reasonable way to spend fifteen minutes before starting on
[Architecture](architecture.md).

The terms are grouped: first the trading words, then the words for the data we collect, then the
words for how the software is built, then the words for where it runs. Within each group the terms
are ordered so that later ones build on earlier ones, not alphabetically.

## Trading terms

These are the words that come from the financial world rather than from this codebase.

| Term | What it means |
|---|---|
| **Venue** | The marketplace where contracts are bought and sold. For this project the venue is Delta Exchange India. We say "venue" rather than "exchange" so that the word stays the same when a second marketplace is added later. |
| **Underlying** | The thing an option contract is *about*. Here it is Bitcoin (`BTC`) or Ethereum (`ETH`). |
| **Spot price** | What the underlying is worth right now, today, in ordinary buying and selling. |
| **Option** | A contract that gives its owner the right, but not the obligation, to buy or sell the underlying at a fixed price on a fixed future date. |
| **Call** and **put** | The two kinds of option. A call is the right to *buy*; a put is the right to *sell*. Written `C` and `P` throughout this codebase. |
| **Strike** | The fixed price written into the option contract. A call with a strike of 60,000 is the right to buy Bitcoin for $60,000. |
| **Expiry** | The date the option contract ends. After it, the contract no longer exists. |
| **Contract** | One specific option: one underlying, one strike, one expiry, and either a call or a put. `C-BTC-77600-040926` is the venue's name for one. |
| **Option chain** | All the contracts for one underlying and one expiry, laid out as a table with one row per strike -- calls on the left, puts on the right. This is the main screen of the application. |
| **Order book** | The list of offers to buy and to sell a contract, at each price, at this instant. |
| **Bid** and **ask** | The best price anyone is currently offering to buy at (bid) and to sell at (ask). |
| **Top of book** | Just the best bid and the best ask, without the rest of the list. It is all this project needs. |
| **Mid** | The midpoint between the bid and the ask. It is the usual stand-in for "the price" when there is no recent trade. |
| **Mark price** | The venue's own official valuation of a contract, used for settling accounts. We record it but never calculate with it. |
| **Open interest** | How many of a contract are currently outstanding. Written `oi`. |
| **Forward** | What the market currently expects the underlying to be worth on the expiry date. It is not published by anyone; it has to be worked out from option prices, and this project works it out. |
| **Volatility** | How much a price moves around. Bigger moves mean higher volatility. |
| **Implied volatility** (IV) | The volatility figure that, put into a standard pricing formula, produces the price the market is actually charging. It is the market's own opinion about future movement, read backwards out of the prices. This is the single most important number the project computes. |
| **Realised volatility** | How much the price actually did move, measured from history. Comparing it against implied volatility is one of the screens. |
| **Greeks** | Five numbers describing how an option's price reacts to change: `delta` (to the underlying's price), `gamma` (to delta itself), `vega` (to volatility), `theta` (to the passing of time) and `rho` (to interest rates). |
| **Out of the money** | An option whose strike is on the unprofitable side of the current price -- a call with a strike above the forward, or a put below it. These contracts are where implied volatility is most reliably recovered, which is why the project uses them for it. |
| **Straddle** and **strangle** | Two common combinations of contracts, priced together. One screen lists every one of them for an expiry. |
| **Settlement** | How a contract is paid out when it expires. Delta's options are **linear** and **USD-settled**, which means the ordinary textbook option mathematics applies with no adjustments. |

## Data terms

These describe the information the system collects and stores.

| Term | What it means |
|---|---|
| **Channel** | A named stream of updates the venue offers. Delta has two we use: one for the order book, one for everything else. |
| **Frame** | One message as it arrives from the venue, still in the venue's own format. |
| **Tick** | One observation of a price, at one moment. |
| **Bar** | A summary of everything that happened to one contract during one minute: the first price, the highest, the lowest, the last, and how many observations there were. The project stores bars, not individual ticks. |
| **Sealing** | Deciding that a minute is finished and no further observations will be added to its bar. |
| **Grace** | A short waiting period after a minute ends, before its bar is sealed, to allow for messages that arrive slightly late. |
| **Flush** | Writing the bars that have accumulated in memory out to a file on disk. |
| **Forward-filling** | Filling a gap in a price series by repeating the previous value. **This project never does it**, because it invents data that was never observed. A minute with no observations produces no bar at all. |
| **Parquet** | A file format designed for storing tables of numbers compactly and reading columns out of them quickly. All our permanent data is in Parquet files. |
| **Partitioning** | Splitting stored data into folders by a couple of key fields -- here the underlying and the date -- so a program looking for one day's Bitcoin data can skip every other folder without opening it. |
| **Hive layout** | The naming convention for those folders: `underlying=BTC/date=2026-09-08/`. It is a widely used convention, so other tools understand our folders without being told about them. |
| **Compaction** | Merging many small files into one large one, overnight, after a day is finished. It makes reading much faster and cheaper. |

## Software terms

These describe how the program is put together.

| Term | What it means |
|---|---|
| **REST** | The ordinary way a program asks a web server a question and gets one answer back. Each question is separate. |
| **WebSocket** | A connection that stays open, over which the server can keep sending new information without being asked again. This is how the venue sends us prices and how our web page receives updates. |
| **Process** | One running program. This system can run as one process or as several, depending on configuration. |
| **Monolith** | The single-process arrangement: everything -- the venue connection, the calculations, the file writing, the web server -- inside one program. It is the simplest way to run the system and is the default when developing. |
| **Split mode** | The multi-process arrangement: the same work divided into four programs plus a message broker, each doing one job. This is how it runs in production. |
| **Event** | A single, self-describing message that one part of the system sends without knowing who will read it. There are ten kinds. See [Events](events.md). |
| **Message bus** | The delivery mechanism that carries events from whoever produced them to whoever wants them. See [Message bus](message-bus.md). |
| **Publish** and **subscribe** | To *publish* is to put an event onto the bus. To *subscribe* is to ask for a copy of every event of a given kind. The publisher never knows who the subscribers are. |
| **Stream** | One named queue on the bus, holding events of one kind. |
| **Consumer group** | A label that lets one part of the system keep its own place in a stream, independently of any other part reading the same stream. |
| **Lossless** | A subscription where no message may be dropped, however busy things get. The file writer uses one, because a dropped message would leave a permanent hole in the record. |
| **Drop-oldest** | A subscription where, if messages pile up, the oldest are thrown away. The live screen uses one, because a four-second-old price is worthless to it. |
| **Adapter** | The one piece of code that knows how a particular venue speaks. Everything else in the system is written against our own vocabulary and never sees the venue's. See [Adapters](adapters.md). |
| **Checkpoint** | A saved note of how far through a stream a program had got, so that after a restart it can carry on from there rather than starting over or skipping ahead. |
| **Replay** | Reading a stream again from a saved checkpoint, to recover messages that arrived while a program was restarting. |
| **Health check** | A web address a program answers on to say whether it is working. Something else watches it and restarts the program if the answer stops being good. |
| **Pure function** | A piece of code that takes values in and returns values out, touching no files, no network and no clock. Pure code is easy to test, which is why most of the mathematics here is written this way. |

## Deployment terms

These describe where the system runs when it is not on somebody's laptop.

| Term | What it means |
|---|---|
| **Container** | A packaged program with everything it needs to run, so that it behaves the same on a laptop and on a server. |
| **Docker Compose** | A tool that starts several containers together on one machine, using a single configuration file. We use it for local development. |
| **AWS** | Amazon Web Services, the company whose computers we rent. |
| **EC2** | The AWS service that rents you a virtual computer by the hour. All the back-end containers run on one of these. |
| **ECS** | The AWS service that starts your containers on an EC2 computer, restarts them if they stop, and records that it did. |
| **S3** | The AWS service that stores files cheaply and permanently. Our Parquet files live there in production. |
| **Amplify** | The AWS service that builds a website from a code repository and serves it worldwide. Our web page is served by it. |
| **Redis** | A program that holds data in memory very quickly. We use one feature of it -- streams -- as our message bus. |
| **Reverse proxy** | A program sitting in front of others that receives every web request and forwards it to whichever program should answer. It is why the browser only ever talks to one address. |
| **CORS** | A browser safety rule that stops a web page from calling a server at a different address unless that server explicitly allows it. Arranging that the browser only ever uses one address is how the project avoids the rule entirely. |
| **Region** | The part of the world an AWS computer physically sits in. Ours is `ap-south-1`, which is Mumbai. |
