/**
 * P6: keeping up with the market — and, above all, *not* keeping up with it when the
 * link says a stored minute.
 *
 * **The claim that matters is a request count, not a duration.** "A historical link
 * never re-asks" is a statement about how many times `POST /analyse` is issued, and a
 * test that waited a second and then looked would be testing the machine's timer rather
 * than this module's rule — and would read the wall clock, which nothing in this project
 * is allowed to do. So the scheduler is injected: the test holds the tick and fires it
 * by hand, and the only thing asserted is how many requests came out the other side.
 *
 * The seam is the pure one this repo already uses everywhere else: no DOM, no network,
 * no clock. `bun tests/analyse-live.test.ts`.
 */
import assert from "node:assert/strict";

import { POLL_MS, subscribeAnalysis } from "@/lib/analyse-live";
import type { AnalyseResponse, LegRequest } from "@/lib/payoff";

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

const LEGS: LegRequest[] = [
  { instrument: "DELTA-BTC-20260904-77000-C-USD", direction: 1, quantity: 1 },
];

/** Enough of a response to be handed back; the shape is pinned in `analyse.test.ts`. */
function answer(spot: number): AnalyseResponse {
  return {
    underlying: "BTC",
    expiry: "04-09-2026",
    as_of: "2026-09-04T09:21:00Z",
    spot,
    forward: 77609.4,
    discount: 0.99961,
    contract_value: 0.001,
    legs: [],
    total_greeks: null,
    curve: {
      corners: [{ price: 74000, pnl: -1240 }],
      slope_left: 0,
      slope_right: 1,
      window: { low: 74000, high: 81000 },
    },
    metrics: {
      max_profit: null,
      max_loss: -1240,
      breakevens: [],
      net_premium: 1240,
      reward_risk: null,
    },
    table: [{ price: 74000, pnl: -1240 }],
  };
}

/**
 * Let every pending promise settle, without a timer and without a clock: a fixed number
 * of microtask turns, which is deterministic — `await`ing a real millisecond would be
 * the wall clock creeping in through the back door.
 */
async function flush(): Promise<void> {
  for (let i = 0; i < 20; i++) await Promise.resolve();
}

/**
 * A scheduler the test drives by hand. `tick()` is the one-second timer firing, and
 * nothing here consults a real clock — the point of injecting it.
 */
function heldTimer() {
  const registered: { ms: number; fire: () => void }[] = [];
  let cancelled = 0;
  return {
    registered,
    get cancelled() {
      return cancelled;
    },
    schedule(fire: () => void, ms: number): () => void {
      registered.push({ ms, fire });
      return () => {
        cancelled++;
      };
    },
    /** One turn of the timer, plus the microtasks the request it starts resolves in. */
    async tick(): Promise<void> {
      for (const entry of registered) entry.fire();
      await flush();
    },
  };
}

async function main(): Promise<void> {
  console.log("analyse-live / subscribeAnalysis — live when live, static when historical");

  await check("A STORED MINUTE NEVER RE-ASKS: one request, and no timer at all", async () => {
    const asks: (string | null)[] = [];
    const timer = heldTimer();
    subscribeAnalysis(
      LEGS,
      "2026-09-04T09:21:00Z",
      { onAnalysis: () => {}, onError: () => {} },
      {
        ask: async (_legs, asOf) => {
          asks.push(asOf);
          return answer(77543);
        },
        schedule: timer.schedule,
        now: () => "2026-09-04T09:21:03Z",
      },
    );
    await timer.tick();
    await timer.tick();
    assert.equal(asks.length, 1, "a stored minute is a fixed set of numbers");
    assert.equal(asks[0], "2026-09-04T09:21:00Z", "and it is asked for by name");
    assert.equal(timer.registered.length, 0, "no timer is registered at all");
  });

  await check("A LIVE LINK RE-ASKS: one request per tick, and the timer is one second", async () => {
    const asks: (string | null)[] = [];
    const timer = heldTimer();
    subscribeAnalysis(LEGS, null, { onAnalysis: () => {}, onError: () => {} }, {
      ask: async (_legs, asOf) => {
        asks.push(asOf);
        return answer(77543);
      },
      schedule: timer.schedule,
      now: () => "2026-09-04T09:21:03Z",
    });
    await flush();
    assert.equal(asks.length, 1, "the first request is immediate, not a second away");
    assert.equal(timer.registered.length, 1, "exactly one timer");
    assert.equal(timer.registered[0]!.ms, POLL_MS);
    assert.equal(POLL_MS, 1000, "one second — docs/superpowers/specs, P6");

    await timer.tick();
    await timer.tick();
    assert.equal(asks.length, 3, "two ticks, two more requests");
    assert.deepEqual(asks, [null, null, null], "and none of them names a minute");
  });

  await check("each answer carries when it arrived — the as-of chip's 'updated'", async () => {
    const stamps: string[] = [];
    const timer = heldTimer();
    const clock = ["2026-09-04T09:21:03Z", "2026-09-04T09:21:04Z"];
    let at = 0;
    subscribeAnalysis(
      LEGS,
      null,
      { onAnalysis: (_analysis, receivedAt) => stamps.push(receivedAt), onError: () => {} },
      {
        ask: async () => answer(77543),
        schedule: timer.schedule,
        now: () => clock[at++]!,
      },
    );
    await flush();
    await timer.tick();
    assert.deepEqual(stamps, clock, "the instant belongs to the answer, not to the render");
  });

  await check("a tick while one is still in flight issues nothing — no stacking", async () => {
    // /analyse is a fat response and a slow one can outlast the second it was asked in.
    // Two in flight at once would let the older answer land last and put a stale spot on
    // screen, so the tick is skipped rather than queued.
    let release: ((analysis: AnalyseResponse) => void) | null = null;
    let asks = 0;
    const timer = heldTimer();
    const spots: number[] = [];
    subscribeAnalysis(
      LEGS,
      null,
      { onAnalysis: (analysis) => spots.push(analysis.spot!), onError: () => {} },
      {
        ask: async () => {
          asks++;
          return new Promise<AnalyseResponse>((resolve) => {
            release = resolve;
          });
        },
        schedule: timer.schedule,
        now: () => "2026-09-04T09:21:03Z",
      },
    );
    await flush();
    assert.equal(asks, 1);

    await timer.tick();
    await timer.tick();
    assert.equal(asks, 1, "the first request has not answered yet");

    release!(answer(77543));
    await flush();
    assert.deepEqual(spots, [77543]);

    await timer.tick();
    assert.equal(asks, 2, "once it has answered, the next tick asks again");
  });

  await check("stopping cancels the timer and drops the answer already in flight", async () => {
    let release: ((analysis: AnalyseResponse) => void) | null = null;
    const timer = heldTimer();
    let delivered = 0;
    const stop = subscribeAnalysis(
      LEGS,
      null,
      { onAnalysis: () => delivered++, onError: () => {} },
      {
        ask: async () =>
          new Promise<AnalyseResponse>((resolve) => {
            release = resolve;
          }),
        schedule: timer.schedule,
        now: () => "2026-09-04T09:21:03Z",
      },
    );
    await flush();
    stop();
    assert.equal(timer.cancelled, 1, "the timer is cancelled, not left running");
    release!(answer(77543));
    await flush();
    assert.equal(delivered, 0, "an answer to a subscription nobody holds is dropped");
  });

  await check("no legs is no request — an empty strategy is never sent to be refused", async () => {
    let asks = 0;
    const timer = heldTimer();
    subscribeAnalysis([], null, { onAnalysis: () => {}, onError: () => {} }, {
      ask: async () => {
        asks++;
        return answer(77543);
      },
      schedule: timer.schedule,
      now: () => "2026-09-04T09:21:03Z",
    });
    await timer.tick();
    assert.equal(asks, 0);
    assert.equal(timer.registered.length, 0, "and nothing is polling for one either");
  });

  if (failures > 0) {
    console.error(`\n${failures} failed`);
    process.exit(1);
  }
  console.log("\nall passed");
}

await main();
