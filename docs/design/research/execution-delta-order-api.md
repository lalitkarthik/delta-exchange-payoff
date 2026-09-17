# Research: Delta Exchange India's order API, behind docs/design/execution/books.md and paper-broker.md

> Gathered 2026-09-16 by a web-research agent; every claim carries its source URL. **Evidence, not a decision** — the decision pages under `docs/design/execution/` say what was chosen. Not normative: the nomenclature scan skips `docs/design/research/`.

# Delta Exchange India trading API — order/fill/position research

Base URL (production): `https://api.india.delta.exchange` (also referenced as `https://cdn.india.deltaex.org` in blog material — docs.delta.exchange is the canonical reference and uses `/v2/...` paths throughout).
Docs: https://docs.delta.exchange/ (this is the India-aware doc — it documents `api.india.delta.exchange` alongside the global `api.delta.exchange`; there is also a global-only mirror at https://docs-global.delta.exchange/).

All schema/field claims below came from https://docs.delta.exchange/ (fetched 2026-09-16) unless another URL is cited.

## 1. Place order — client_order_id

- Endpoint: `POST /v2/orders`. Request body accepts an optional `client_order_id` (string) field alongside `product_id`, `size`, `side`, `order_type`, `limit_price`, `stop_order_type`, `stop_price`, `trail_amount`, `stop_trigger_method`, bracket (`bracket_*`) fields, `time_in_force`, `mmp`, `post_only`, `reduce_only`, `cancel_orders_accepted`. — https://docs.delta.exchange/
- **Max length: 32 characters.** — https://docs.delta.exchange/
- **Uniqueness: enforced across all open orders for the user** (i.e., scoped to the user's currently-open orders, not a global/all-time uniqueness constraint). — https://docs.delta.exchange/
- Order retrieval by client id is a dedicated endpoint: `GET /v2/orders/client_order_id/{client_order_id}`. — search result summary of https://docs.delta.exchange/ (confirmed by community post title) https://community.delta.exchange/t/get-order-by-client-oid/890
- **Echoed on order responses**: yes — the order object returned by place/get/cancel order includes `client_order_id`. — https://docs.delta.exchange/
- **Echoed on fills (REST)**: yes — the `Fill` schema (`GET /v2/fills`) includes a `meta_data` object plus `order_id`; client_order_id propagation onto fills is confirmed via the websocket fills channel (see below) and via `GET /v2/orders/client_order_id/{id}` cross-reference. — https://docs.delta.exchange/
- **Echoed on positions**: **no** — the `Position` schema has no `client_order_id` or `order_id` field (see §2). — https://docs.delta.exchange/
- **Echoed on private websocket order channel**: yes, field `client_order_id` (max 32 chars, as documented in the channel payload). — https://docs.delta.exchange/
- **Echoed on private websocket fills channel** (`v2/user_trades`): yes, as the short/compact key `c` (client order id). — https://docs.delta.exchange/

## 2. Positions — net per product, no origin tag

- `GET /v2/positions` (and "Get Margined Positions") returns **one row per product** — a net position per `product_id`/`product_symbol`, not one row per order. Fields include: `user_id`, `size` (signed: +long/-short), `entry_price`, `margin`, `liquidation_price`, `bankruptcy_price`, `adl_level`, `product_id`, `product_symbol`, embedded `product` object, `commission`, `realized_pnl`, `realized_funding`, `realized_cashflow`, `realized_holding_cost`, `unrealized_pnl`, `unrealized_cashflow`, `mark_price`, `margin_mode`, `auto_topup`, `created_at`, `updated_at`. — https://docs.delta.exchange/
- **No field distinguishes how the position was opened.** There is no `order_id`, `client_order_id`, `order_source`, or similar tag anywhere in the Position schema — confirmed both from the full field dump and from a direct query against the docs asking specifically about a source/tag field ("the documentation does not indicate any field distinguishing how a position was opened"). — https://docs.delta.exchange/

## 3. Fills / trade history

- Endpoint: `GET /v2/fills` ("Get User Fills by Filters"); also "Get Order History (cancelled/closed)" and "Download Fills History" exist as separate endpoints. — https://docs.delta.exchange/
- `Fill` schema fields: `id`, `size`, `notional`, `product` (nested object: id, symbol, contract_type, tick_size, contract_value, contract_unit_currency, notional_type, quoting/underlying/settling asset objects, spot_index), `fill_type`, `side`, `price`, `role` (maker/taker), `commission`, `created_at`, `product_id`, `product_symbol`, `order_id`, `settling_asset_id`, `settling_asset_symbol`, `meta_data` (object). — https://docs.delta.exchange/
- `order_id` **is** present directly on the Fill object. `client_order_id` is not a bare top-level key in the plain REST Fill schema dump obtained, but is confirmed present end-to-end via the order object and the websocket fills channel (key `c`); treat REST-fill client_order_id as likely surfaced inside `meta_data` or joinable via `order_id` → order lookup — this one detail is not 100% pinned down from the docs text alone and is worth a direct API smoke-test before relying on it.
- Pagination: cursor-based (`after`/`before`, `page_size`), with `start_time`/`end_time` filters in microseconds. **No documented retention/lookback limit was found** — the docs do not state a maximum number of days or records for `GET /v2/fills`. — https://docs.delta.exchange/

## 4. Private websocket channels

Auth: send a `key-auth` message — `{"type":"key-auth","payload":{"api-key":..., "timestamp":<unix seconds>, "signature": HMAC-SHA256("GET"+timestamp+"/live")}}`. Success reply: `{"type":"key-auth","success":true,"status_code":200,"status":"authenticated"}`. The older `auth`/`unauth` message pair is **deprecated and stops working after 2025-12-31**. — https://docs.delta.exchange/

- **`orders`** — order lifecycle (create/update/delete) with `reason` (fill, stop trigger, cancellation, liquidation, self-trade). Fields: `type`, `action`, `reason`, `symbol`, `product_id`, `order_id`, `client_order_id` (max 32 chars), `size`, `unfilled_size`, `average_fill_price`, `limit_price`, `side`, `cancellation_reason`, `stop_order_type`, `bracket_order`, `state`, `seq_no`, `timestamp` (µs), `stop_price`, `trigger_price_max_or_min`, bracket stop/take-profit price fields. — https://docs.delta.exchange/
- **`v2/user_trades`** (fills, compact keys) — `type`, `sy` (symbol), `f` (fill id), `R` (reason: normal/adl/liquidation), `u` (user id), `o` (order id), `S` (side), `s` (size), `p` (price), `po` (resulting position size), `r` (role), **`c` (client_order_id)**, `t` (timestamp µs), `se` (seq no). A slower `user_trades` channel (non-abbreviated) also exists with commission data included. — https://docs.delta.exchange/
- **`positions`** — `type`, `action`, `reason` (null or `auto_topup`), `symbol`, `product_id`, `size`, `margin`, `entry_price`, `liquidation_price`, `bankruptcy_price`, `commission`, `adl_level`, `auto_topup`, `created_at`, `updated_at`. No `client_order_id`/`order_id`. — https://docs.delta.exchange/
- **`margins`** — per-asset wallet/margin push: `type`, `action`, `asset_id`, `asset_symbol`, `available_balance`, `available_balance_for_robo`, `balance`, `blocked_margin`, `commission`, `cross_asset_liability`, `cross_commission`, `cross_locked_collateral`, `cross_order_margin`, `cross_position_margin`, `id`, `interest_credit`, `order_margin`, `pending_referral_bonus`, `pending_trading_fee_credit`, `portfolio_margin`, `position_margin`, `robo_trading_equity`, `timestamp`, `trading_fee_credit`, `unvested_amount`, `user_id`. No `client_order_id`. — https://docs.delta.exchange/
- Also listed but not detailed here: `PortfolioMargins`, `MMP Trigger` channels. — https://docs.delta.exchange/

## 5. Margin and wallet endpoints

- **No documented pre-trade "order margin estimate" or portfolio-margin-calculator endpoint** exists for a proposed order — confirmed by direct query against the docs ("There is no documented API endpoint to estimate or get required margin for a proposed order... or a portfolio margin calculator endpoint"). The closest things available are endpoints that act on *existing* positions/orders: "Add/Remove Position Margin", "Change Order Leverage", "Get Order Leverage", "Change Margin Mode". — https://docs.delta.exchange/
- **`GET /v2/wallet/balances`** — `Wallet` schema fields: `asset_id`, `asset_symbol`, `available_balance`, `available_balance_for_robo`, `balance`, `blocked_margin`, `commission`, `cross_asset_liability`, `cross_commission`, `cross_locked_collateral`, `cross_order_margin`, `cross_position_margin`, `id`, `interest_credit`, `order_margin`, `pending_referral_bonus`, `pending_trading_fee_credit`, `portfolio_margin`, `position_margin`, `trading_fee_credit`, `unvested_amount`, `user_id`. Note `order_margin` (isolated-mode margin blocked by open orders) and `position_margin`/`cross_position_margin` fields — these reflect currently-blocked margin, not a pre-trade estimate. — https://docs.delta.exchange/

## 6. Testnet

- Delta Exchange India **has its own India-specific testnet**, separate from the global Delta testnet:
  - Testnet site / account creation: https://testnet.delta.exchange/ — create a separate demo account there (not the same login as the live India account). — https://www.delta.exchange/blog/guide-to-api-trading-with-delta-india / https://www.delta.exchange/support/solutions/articles/80001174969-kickstarting-your-trading-journey-with-delta-india-apis
  - Testnet REST base URL: **`https://cdn-ind.testnet.deltaex.org`**. — https://www.delta.exchange/support/solutions/articles/80001174969-kickstarting-your-trading-journey-with-delta-india-apis
  - Generate an API key/secret from the testnet account with "Trading" + "Read" permission scopes, same flow as production key creation, from account settings on the testnet site.
  - **Keys are not interchangeable**: API keys created on the live India account work only against `https://api.india.delta.exchange`; keys created on the testnet/demo account work only against `https://cdn-ind.testnet.deltaex.org`. — https://www.delta.exchange/support/solutions/articles/80001174969-kickstarting-your-trading-journey-with-delta-india-apis
  - Test funds are auto-credited on demo account creation; no faucet API — top-ups happen on the testnet website. — https://www.delta.exchange/support/solutions/articles/80001174969-kickstarting-your-trading-journey-with-delta-india-apis
  - This is distinct from the **global** Delta testnet used by `docs-global.delta.exchange` / `global.delta.exchange`, which is a different product/account universe.

## 7. Daily option settlement (BTC/ETH)

- All Delta India options (including daily D1/D2 BTC and ETH chains) **settle at 5:30 PM IST**, which is a fixed **12:00 PM UTC** year-round (India does not observe daylight saving, so there is no seasonal shift despite one source's DST caveat). — https://guides.delta.exchange/delta-exchange-india-user-guide/derivatives-guide/options-guide ; corroborated by a live options-chain URL example dated `2026-06-17T12:00:00Z` returned in search results (`https://www.delta.exchange/app/options_chain/markets/BTC/2026-06-17T12:00:00Z`).
- **Auto-close: yes.** "At expiry, all open positions are closed at the settlement price" — settlement price is derived from a 30-minute TWAP of the underlying vs. strike, and the position is cash-settled/closed automatically; no manual close action is required. — https://guides.delta.exchange/delta-exchange-india-user-guide/derivatives-guide/options-guide
- Both BTC and ETH daily options use the identical 5:30 PM IST settlement stamp — there is no per-asset difference in settlement time. — https://guides.delta.exchange/delta-exchange-india-user-guide/derivatives-guide/options-guide

## 8. Rate limits on order endpoints

- General account quota: **20,000 weight units per rolling/fixed 5-minute window** (default), consumed per endpoint call at a documented weight. — https://docs.delta.exchange/
- Documented weights relevant to orders:
  - Place order: **5**
  - Edit order: **5**
  - Batch orders (up to 50 orders/request): **25**
  - Cancel order: not explicitly enumerated in the fetched excerpt (docs list it alongside place/edit but the exact number wasn't captured in this pass — likely low, e.g. 1, but **unconfirmed**; re-check the "Rate Limit" table on https://docs.delta.exchange/ directly before depending on this number)
  - Get open positions: 3
  - Get fills: 10
  - Get wallet balances: 3
- Separate matching-engine-level cap: **500 operations/second per product**, independent of the per-user weight quota. — https://docs.delta.exchange/
- There is also a "Get Rate Limit Quota" endpoint to check current consumption. — https://docs.delta.exchange/

## 9. Order source / origin marker (API vs. website/app)

- **No such field exists.** A direct, explicit query against the docs for any field like `order_source`, `origin`, `client_type`, or similar on the order or fill objects returned: "There is no API field on an order or fill object that indicates the source of the order." — https://docs.delta.exchange/
- This was cross-checked against the full `Order`/`Fill`/websocket-orders field lists gathered above (§1, §3, §4) — none contain a channel/origin/source key. The only user-controlled identifier available for this purpose is `client_order_id`, which the UI/app does not set (manual trades placed via the website/app will simply have `client_order_id` absent/null).

## Summary table — telling engine orders from manual ones

| Signal | Available? | Notes |
|---|---|---|
| Dedicated `order_source`/`origin`/`client_type` field | **No** | Confirmed absent from order, fill, and all private WS channel schemas. |
| `client_order_id` set by the engine, absent on manual orders | **Yes (usable as a proxy)** | Max 32 chars, unique among the user's *currently open* orders only (not all-time/global) — reuse is possible once the original order is no longer open, so pair it with `order_id`/timestamp if you need long-term uniqueness. Website/app-placed orders will not carry a `client_order_id` (or will carry whatever the UI itself sets, if anything — not documented that the UI sets one, so treat null/absent as "manual"). |
| `client_order_id` echoed on order object, orders WS channel, and fills WS channel (`c` key) | **Yes** | Lets you tag an order at placement and trace it through fills without extra joins. |
| `client_order_id` on REST `GET /v2/fills` | **Unconfirmed** — `order_id` is present; `client_order_id` presence needs a live smoke test | Join fills to orders via `order_id` → `GET /v2/orders/client_order_id/{id}` as a fallback. |
| `client_order_id` on positions (REST or WS) | **No** | Positions are net-per-product; no per-order/opening-order lineage is retained once merged into a position. |
| Distinguishing engine vs. manual once a position exists | **Not directly possible** | Must be inferred by correlating the position's fills (via `order_id`) back to the orders that built it, using `client_order_id` on those orders as the engine marker — not by anything on the Position object itself. |
