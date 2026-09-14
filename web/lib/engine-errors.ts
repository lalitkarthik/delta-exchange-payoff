/**
 * The three ways a request to the engine can fail, as types.
 *
 * **Their own module because two directions of traffic met in `engine.ts` and one of
 * them only needed these.** `lib/payoff.ts` mirrors `docs/payoff-contract.md` and has to
 * raise `ContractViolationError` from `assertPayoff`; `engine.ts` has to call
 * `assertPayoff` and `refusalDetail` from `postAnalyse`. That is a genuine cycle, and
 * while it was safe — neither module touches a binding of the other at module-evaluation
 * time, so there is no temporal-dead-zone hazard in either entry order — **the invariant
 * protecting it was written down nowhere**. A future module-scope `const` in either file
 * would have broken it silently, at import time, in a way that reads as a bundler fault
 * rather than as a design one.
 *
 * Three classes with no imports of their own cannot participate in a cycle at all, which
 * is a cheaper guarantee than a comment asking people not to create one.
 *
 * They stay distinct rather than becoming one class with a `kind`, because callers
 * genuinely branch on them: an unreachable engine is a local condition a fixture can
 * sometimes stand in for, a refusal is the engine answering and its `detail` is the
 * useful part, and a contract violation is our own bug and is never softened.
 */

/** The engine answered, but with an error status. Carries FastAPI's `detail`. */
export class EngineResponseError extends Error {
  constructor(
    readonly status: number,
    detail: string,
  ) {
    super(detail);
    this.name = "EngineResponseError";
  }
}

/** The engine could not be reached at all — not running, wrong port, CORS, DNS. */
export class EngineUnreachableError extends Error {
  constructor(cause: unknown) {
    super(cause instanceof Error ? cause.message : String(cause));
    this.name = "EngineUnreachableError";
  }
}

/** The engine answered 200 with a body the contract forbids. */
export class ContractViolationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ContractViolationError";
  }
}
