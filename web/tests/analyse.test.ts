/**
 * P5: the one request this screen makes, and the two shapes a refusal can arrive in.
 *
 * Two claims, and they are separate:
 *
 *   1. **`assertPayoff` actually runs on the response.** It landed with P1 and nothing
 *      called it — a guard nothing calls guarantees nothing. `postAnalyse` is the first
 *      caller, so this file proves the guard bites *through the loader*, not merely
 *      when handed a payload directly (`payoff.test.ts` already covers that).
 *   2. **Both refusal envelopes are readable.** `docs/payoff-contract.md` §Refusals:
 *      the seven semantic refusals travel as FastAPI's `{"detail": "..."}` — a string —
 *      and the one schema refusal (`legs` empty, caught by `AnalyseRequest` before the
 *      route is entered) travels as the request-validation envelope
 *      `{"detail": [{"type": ..., "loc": [...], "msg": ...}]}`, a **list**. A client
 *      reading `detail` must expect either, and a screen that renders `[object Object]`
 *      at a trader has told them nothing.
 *
 * **No network.** `globalThis.fetch` is replaced for the duration; the real one is put
 * back afterwards. **No clock**: every stamp here is a literal.
 */
import assert from "node:assert/strict";

import {
  ContractViolationError,
  EngineResponseError,
  EngineUnreachableError,
  postAnalyse,
} from "@/lib/engine";
import { analyseHref, decodeLegs } from "@/lib/legs-url";
import { refusalDetail, type AnalyseResponse } from "@/lib/payoff";

let failures = 0;

function check(name: string, body: () => void | Promise<void>): Promise<void> {
  return Promise.resolve()
    .then(body)
    .then(() => console.log(`  ok    ${name}`))
    .catch((err) => {
      failures++;
      console.error(`  FAIL  ${name}`);
      console.error(`        ${err instanceof Error ? err.message : String(err)}`);
    });
}

/** `docs/payoff-contract.md`'s worked response, copied out of the document by hand. */
function worked(): AnalyseResponse {
  return {
    underlying: "BTC",
    expiry: "04-09-2026",
    as_of: "2026-09-04T09:21:00Z",
    spot: 77543.0,
    forward: 77609.4,
    discount: 0.99961,
    contract_value: 0.001,
    legs: [
      {
        instrument: "DELTA-BTC-20260904-77000-C-USD",
        direction: 1,
        quantity: 1,
        entry_price: 1240.0,
        iv: 0.3712,
        greeks: { delta: 0.5231, gamma: 0.0000312, vega: 0.4118, theta: -66.58, rho: 0.129 },
      },
    ],
    total_greeks: { delta: 0.5231, gamma: 0.0000312, vega: 0.4118, theta: -66.58, rho: 0.129 },
    curve: {
      corners: [
        { price: 74000.0, pnl: -1240.0 },
        { price: 77000.0, pnl: -1240.0 },
        { price: 81000.0, pnl: 2760.0 },
      ],
      slope_left: 0.0,
      slope_right: 1.0,
      window: { low: 74000.0, high: 81000.0 },
    },
    metrics: {
      max_profit: null,
      max_loss: -1240.0,
      breakevens: [78240.0],
      net_premium: 1240.0,
      reward_risk: null,
    },
    table: [{ price: 74000.0, pnl: -1240.0 }],
  };
}

interface Sent {
  url: string;
  init: RequestInit | undefined;
}

const realFetch = globalThis.fetch;
const sent: Sent[] = [];

/** Answer the next `fetch` with this status and body, and record what was asked. */
function answerWith(status: number, body: unknown): void {
  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    sent.push({ url: String(url), init });
    return {
      ok: status >= 200 && status < 300,
      status,
      statusText: status === 200 ? "OK" : "Error",
      json: async () => body,
    } as Response;
  }) as unknown as typeof fetch;
}

/** Refuse to connect at all — the engine is not running. */
function refuseToConnect(): void {
  globalThis.fetch = (async () => {
    throw new TypeError("fetch failed");
  }) as unknown as typeof fetch;
}

const LEGS = [{ instrument: "DELTA-BTC-20260904-77000-C-USD", direction: 1 as const, quantity: 1 }];

async function main(): Promise<void> {
  console.log("payoff / refusalDetail — the two envelopes of docs/payoff-contract.md");

  await check("a semantic refusal's string detail is read straight through", () => {
    assert.equal(
      refusalDetail({ detail: "the legs span two expiries: 04-09-2026 and 11-09-2026" }),
      "the legs span two expiries: 04-09-2026 and 11-09-2026",
    );
  });

  await check("the schema refusal's validation list is read as a sentence, not [object Object]", () => {
    // FastAPI's own envelope for `legs` failing `min_length=1` on `AnalyseRequest`.
    const detail = refusalDetail({
      detail: [
        {
          type: "too_short",
          loc: ["body", "legs"],
          msg: "List should have at least 1 item after validation, not 0",
        },
      ],
    });
    assert.ok(detail !== null, "a validation list must be readable");
    assert.match(detail, /legs/, "the field that was wrong must be named");
    assert.match(detail, /at least 1 item/, "the reason must survive");
    assert.doesNotMatch(detail, /\[object Object\]/);
  });

  await check("two validation entries are both reported", () => {
    const detail = refusalDetail({
      detail: [
        { type: "too_short", loc: ["body", "legs"], msg: "List should have at least 1 item" },
        { type: "int_parsing", loc: ["body", "legs", 0, "quantity"], msg: "not an integer" },
      ],
    });
    assert.ok(detail !== null);
    assert.match(detail, /at least 1 item/);
    assert.match(detail, /not an integer/);
  });

  await check("a body with no detail at all is not invented into one", () => {
    assert.equal(refusalDetail({ nothing: true }), null);
    assert.equal(refusalDetail("plain text"), null);
    assert.equal(refusalDetail(null), null);
  });

  console.log("\nengine / postAnalyse — the only request this screen makes");

  await check("the worked response comes back whole, and the POST names /analyse", async () => {
    sent.length = 0;
    answerWith(200, worked());
    const response = await postAnalyse(LEGS, null);
    assert.equal(response.metrics.net_premium, 1240.0);
    assert.equal(sent.length, 1);
    assert.match(sent[0]!.url, /\/analyse$/);
    assert.equal(sent[0]!.init?.method, "POST");
    assert.deepEqual(JSON.parse(String(sent[0]!.init?.body)), { legs: LEGS });
  });

  await check("a named minute travels as as_of; absent means live and is not sent", async () => {
    sent.length = 0;
    answerWith(200, worked());
    await postAnalyse(LEGS, "2026-09-04T09:21:00Z");
    assert.deepEqual(JSON.parse(String(sent[0]!.init?.body)), {
      legs: LEGS,
      as_of: "2026-09-04T09:21:00Z",
    });
  });

  await check("THE WIRING: a decimal sent as a string is refused by the loader itself", async () => {
    // P1 shipped `assertPayoff` and nothing called it. This is the test that the loader
    // is the caller — the same relationship `loadChain` has with `assertNumeric`.
    const breaching = worked() as unknown as Record<string, unknown>;
    breaching.forward = "77609.4";
    answerWith(200, breaching);
    await assert.rejects(() => postAnalyse(LEGS, null), ContractViolationError);
  });

  await check("a 422 schema refusal reaches the screen as a readable message", async () => {
    answerWith(422, {
      detail: [
        {
          type: "too_short",
          loc: ["body", "legs"],
          msg: "List should have at least 1 item after validation, not 0",
        },
      ],
    });
    await assert.rejects(
      () => postAnalyse([], null),
      (err: unknown) => {
        assert.ok(err instanceof EngineResponseError, "wrong error type");
        assert.equal(err.status, 422);
        assert.match(err.message, /at least 1 item/);
        return true;
      },
    );
  });

  await check("a 503 semantic refusal keeps its own sentence", async () => {
    answerWith(503, {
      detail: "no live ladder for BTC expiring 04-09-2026 yet; the chain cache has not warmed",
    });
    await assert.rejects(
      () => postAnalyse(LEGS, null),
      (err: unknown) => {
        assert.ok(err instanceof EngineResponseError);
        assert.equal(err.status, 503);
        assert.match(err.message, /has not warmed/);
        return true;
      },
    );
  });

  await check("an engine that is not running is unreachable, not a refusal", async () => {
    refuseToConnect();
    await assert.rejects(() => postAnalyse(LEGS, null), EngineUnreachableError);
  });

  globalThis.fetch = realFetch;

  console.log("\nlegs-url / analyseHref — the link the chain's Analyse button opens");

  await check("one leg, readable, with the colon left alone", () => {
    assert.equal(
      analyseHref(LEGS, null),
      "/analyse?legs=DELTA-BTC-20260904-77000-C-USD:B1",
    );
  });

  await check("a stored minute travels with it, so a historical tab stays historical", () => {
    assert.equal(
      analyseHref(LEGS, "2026-09-04T09:21:00Z"),
      "/analyse?legs=DELTA-BTC-20260904-77000-C-USD:B1&minute=2026-09-04T09:21:00Z",
    );
  });

  await check("the strategy round trips through the link it is put in", () => {
    const many = [
      ...LEGS,
      { instrument: "DELTA-BTC-20260904-80000-P-USD", direction: -1 as const, quantity: 2 },
    ];
    const query = new URL(analyseHref(many, null), "http://localhost:3000").searchParams;
    assert.deepEqual(decodeLegs(query.get("legs")), many);
  });

  if (failures > 0) {
    console.error(`\n${failures} failed`);
    process.exit(1);
  }
  console.log("\nall passed");
}

await main();
