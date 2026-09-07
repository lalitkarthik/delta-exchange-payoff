"""The seven fields every event carries, and the registry that names the other nine.

**An envelope is the part of a message that is the same for every message.** Split out
here so that a consumer can identify, deduplicate, attribute and time an event without
knowing which of the nine it is holding — and so that adding a tenth adds a payload and
nothing else.

**Registered, so the catalogue is enumerable from the code.** `parse_event` looks the
type up and refuses what it has never heard of. That refusal is the whole point: it is
the difference between a typo becoming an error and a typo becoming a silently ignored
message, which is this project's standing objection to plausible-and-wrong.

**A hand-written discriminated union.** Pydantic can build one over `type` with
`Field(discriminator=...)`, and that was the alternative considered. A dictionary lookup
was chosen because the registry has to be open — a decorator, filled at import time —
and because the lookup miss is where `UnknownEventType` needs to be raised with the
offending name in it. A union would raise `ValidationError` listing nine failures
instead.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from math import isfinite
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .instrument import Instrument


class UnknownEventType(ValueError):
    """A payload naming a type nobody registered, or naming none at all.

    Carries the offending name so the log record says what was actually on the wire.
    """

    def __init__(self, type_name: str | None) -> None:
        self.type_name = type_name
        if type_name is None:
            super().__init__("payload carries no 'type' field")
        else:
            super().__init__(f"no event type is registered as {type_name!r}")


class UnknownSchemaVersion(ValueError):
    """A payload of a known type at a version this build does not understand.

    **The consumer decides what it knows, and what it knows is the version its own class
    declares.** `docs/design/events.md` puts it plainly: a version is bumped when a field
    *changes meaning*, so a payload at another version is one whose fields this build
    would read with the wrong meaning — plausible, wrong, and silent. #35 left the check
    out deliberately because no consumer existed to define "known"; #37 is that consumer.

    Carries the type and both versions so a log record says what was actually on the wire.
    """

    def __init__(self, type_name: str, version: object, known: int) -> None:
        self.type_name = type_name
        self.version = version
        self.known = known
        super().__init__(
            f"{type_name!r} arrived at schema_version {version!r}; "
            f"this build understands {known!r}"
        )


class Event(BaseModel):
    """The base every catalogued event extends.

    Frozen, because a consumer holding an event must not be surprised by another; extra
    fields forbidden, because a field nobody declared is either a producer bug or a
    version this consumer does not understand, and both should fail loudly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The event's name, one of the nine. Each subclass fixes its own.
    type: str
    #: Unique per event. What lets a consumer deduplicate and what a log record joins on.
    #: Random rather than sequential: there is no single counter to draw from once a
    #: second adapter exists.
    event_id: str = Field(default_factory=lambda: uuid4().hex)
    #: Per type, starting at 1. Bumped when a field changes meaning, never when one is
    #: added with a default. See `docs/design/lld/events.md`.
    schema_version: int = 1
    #: Who built it — the adapter's venue name, or the engine component that emitted it.
    source: str
    #: The venue's own stamp, **as the venue gave it**, and `null` where the venue gives
    #: none. Not corrected when the venue's clock is wrong: the disagreement is the data.
    ts_venue: datetime | None = None
    #: Our wall clock at arrival. **Required, and never defaulted from a clock in here**
    #: — the producer knows when the frame arrived and this package has no clock at all,
    #: which is what keeps every test of it deterministic and offline.
    #:
    #: Neither stamp is a latency clock: this one comes from `time.time()`, which steps
    #: backwards under an NTP correction. Elapsed time is measured on the monotonic
    #: clock, as `timing.time_it` does. The arrival lag is `ts_received - ts_venue`.
    ts_received: datetime
    #: The contract this is about, or `null` for events that are not about one.
    instrument: Instrument | None = None

    @model_validator(mode="after")
    def _refuse_non_finite_numbers(self) -> Event:
        """**A `NaN` price must not become an absent quote.**

        Pydantic serialises `NaN` and `Infinity` to JSON `null` by default, so a garbage
        number arriving from a venue would come out the far end indistinguishable from a
        quote that was never there — the exact null-is-not-zero confusion this catalogue
        exists to prevent, and silent. `ser_json_inf_nan="strict"` would raise at dump
        time but is not offered by this pydantic (`measured` 2.13.5: only `null`,
        `constants` and `strings`), so it is refused here, at construction, where the
        traceback still names the producer.

        Values inside `md.option_bar`'s `columns` are checked one level down, which is
        as deep as any payload here nests.
        """
        for value in self.__dict__.values():
            if isinstance(value, float) and not isfinite(value):
                raise ValueError(f"{value} is not a finite number")
            if isinstance(value, dict):
                for inner in value.values():
                    if isinstance(inner, float) and not isfinite(inner):
                        raise ValueError(f"{inner} is not a finite number")
        return self


EventT = TypeVar("EventT", bound=type[Event])

_REGISTRY: dict[str, type[Event]] = {}


def register(cls: EventT) -> EventT:
    """Class decorator. Puts `cls` in the registry under its own `type` default.

    The name is read off the field default rather than passed as an argument, so there
    is exactly one place a type name is written and no way for the two to disagree.
    """
    field = cls.model_fields.get("type")
    type_name = None if field is None else field.default
    if not isinstance(type_name, str) or not type_name:
        raise TypeError(f"{cls.__name__} declares no default for 'type'")
    if type_name in _REGISTRY:
        raise ValueError(
            f"{type_name!r} is already registered to {_REGISTRY[type_name].__name__}; "
            "two classes under one name would make parsing depend on import order"
        )
    _REGISTRY[type_name] = cls
    return cls


def registry() -> Mapping[str, type[Event]]:
    """The catalogue, enumerable. A copy, so a caller cannot register by mutation."""
    return dict(_REGISTRY)


def known_schema_version(cls: type[Event]) -> int:
    """The one `schema_version` this build understands for `cls`.

    Read off the field default rather than kept in a second table, for the same reason
    `register` reads the type name off its default: two places to write a version is two
    places for it to disagree.
    """
    field = cls.model_fields.get("schema_version")
    default = None if field is None else field.default
    return default if isinstance(default, int) else 1


def parse_event(payload: str | bytes | bytearray | Mapping[str, Any]) -> Event:
    """Bytes or a mapping to the right typed event, in one step.

    Raises `UnknownEventType` for a type nobody registered — including a payload with no
    `type` at all — `UnknownSchemaVersion` for a registered type at a version this build
    does not understand, and pydantic's `ValidationError` for a payload that does not fit.

    **Version is checked here and not in the model**, so that a producer inside this
    process can still build its own events: construction is a component declaring what it
    emits, while parsing is a consumer asking whether it understands what it was handed.
    A payload that omits `schema_version` takes the class default and is therefore known.
    """
    if isinstance(payload, (str, bytes, bytearray)):
        data = json.loads(payload)
    else:
        data = payload
    if not isinstance(data, Mapping):
        raise UnknownEventType(None)
    type_name = data.get("type")
    if not isinstance(type_name, str):
        raise UnknownEventType(None)
    cls = _REGISTRY.get(type_name)
    if cls is None:
        raise UnknownEventType(type_name)
    known = known_schema_version(cls)
    version = data.get("schema_version", known)
    # **An integer, and `bool` is not one.** `bool` subclasses `int` in Python and
    # `True == 1`, so a payload spelling its version `true` would otherwise arrive as
    # version 1; `1.0 == 1` likewise. A version is an integer or it is not a version.
    if not isinstance(version, int) or isinstance(version, bool) or version != known:
        raise UnknownSchemaVersion(type_name, version, known)
    return cls.model_validate(dict(data))
