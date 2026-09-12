"""Where the store's bytes live: the host mount, or the chosen home from 0004.

`BarStore` decides between the two by looking at its own root string. A bare path is the
host mount, the only home that exists today. `s3://bucket/prefix` is S3 Standard — R3's
decision, [0004-durable-store.md](../../../docs/design/decisions/0004-durable-store.md) —
and no second configuration surface exists for it: `DELTA_STORE_ROOT` already names the
root, and the same value now doubles as the switch, exactly as 0004 says the read side
already does by handing `pl.scan_parquet` an `s3://` path with `storage_options` instead
of a local one.

**This module is pure parsing.** It opens no socket, imports no cloud SDK, and holds no
state. It exists apart from `store.py` so the one rule — what a root string means — has a
seam a test can drive without constructing a `BarStore` at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_S3_URI = re.compile(r"^s3://(?P<bucket>[^/]*)(?:/(?P<prefix>.*))?$")


class StoreHomeError(ValueError):
    """A configured root looks like the S3 home but does not parse as one."""


@dataclass(frozen=True, slots=True)
class S3Home:
    """The chosen home: S3 Standard, the same `underlying=.../date=.../` layout, under
    one prefix in one bucket. `prefix` never carries a leading or trailing slash, so a
    caller can join it the same way regardless of whether the root named one."""

    bucket: str
    prefix: str

    def url(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}" if self.prefix else f"s3://{self.bucket}"


class StoreHomeUnavailable(RuntimeError):
    """The configured home is `S3Home`, and this build cannot reach it.

    **Never a silent fallback to the host mount.** A store that believed it was durable
    on S3 while quietly writing local bytes instead would be a worse failure than one
    that refused outright — the exact shape of error this store refuses everywhere else,
    just moved from a bar's minute to a process's configuration.
    """

    def __init__(self, home: S3Home) -> None:
        self.home = home
        super().__init__(
            f"store configured for {home.url()} (0004's chosen home), but this build "
            "carries no S3 backend to reach it — the seam exists, the dependency does "
            "not. See docs/storage-start-here.md for what closing this needs."
        )


def is_s3_root(root: object) -> bool:
    """True if this root names the S3 home rather than the host mount.

    Anything else — `None`, a `Path`, a bare string — names the host mount, today's only
    home, and is untouched by this module.
    """
    return isinstance(root, str) and root.startswith("s3://")


def parse_s3_root(root: str) -> S3Home:
    """Parse `s3://bucket[/prefix]` into its bucket and prefix.

    Raises `StoreHomeError` for anything `is_s3_root` would call true that still fails to
    name a bucket — an empty bucket segment (`s3:///x`) most of all, since that is the one
    shape a regex alone would otherwise let through silently.
    """
    match = _S3_URI.fullmatch(root)
    if not match or not match.group("bucket"):
        raise StoreHomeError(f"{root!r} does not name an S3 bucket")
    prefix = (match.group("prefix") or "").strip("/")
    return S3Home(bucket=match.group("bucket"), prefix=prefix)
