"""Delta Exchange option-chain engine.

Fetches `GET /v2/tickers` from Delta, pivots calls and puts sharing a strike onto one
row, and serves the shape fixed in `docs/chain-contract.md`. Computes nothing else.
"""

__version__ = "0.1.0"
