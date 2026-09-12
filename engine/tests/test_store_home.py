"""Parsing the store's root into a home: the host mount, or S3, R3's decision (0004).

Pure parsing, no `BarStore` involved — see `store_home.py`'s own docstring for why this
seam is separate. `test_store.py` covers what a `BarStore` does with what this module
hands back.
"""

from __future__ import annotations

import pytest

from deltapayoff.store_home import (
    S3Home,
    StoreHomeError,
    StoreHomeUnavailable,
    is_s3_root,
    parse_s3_root,
)


def test_is_s3_root_recognizes_the_s3_scheme() -> None:
    assert is_s3_root("s3://convex-hedge-bars/prod")


def test_is_s3_root_rejects_a_local_path() -> None:
    assert not is_s3_root("/mnt/data")
    assert not is_s3_root("D:\\data")
    assert not is_s3_root("data")


def test_is_s3_root_rejects_none_and_a_path_object() -> None:
    from pathlib import Path

    assert not is_s3_root(None)
    assert not is_s3_root(Path("s3://not-really"))  # a Path, not a str: not this scheme


def test_parse_s3_root_splits_bucket_and_prefix() -> None:
    home = parse_s3_root("s3://convex-hedge-bars/prod/bars")

    assert home == S3Home(bucket="convex-hedge-bars", prefix="prod/bars")


def test_parse_s3_root_accepts_a_bucket_with_no_prefix() -> None:
    home = parse_s3_root("s3://convex-hedge-bars")

    assert home == S3Home(bucket="convex-hedge-bars", prefix="")


def test_parse_s3_root_strips_a_trailing_slash_from_the_prefix() -> None:
    home = parse_s3_root("s3://convex-hedge-bars/prod/")

    assert home.prefix == "prod"


def test_parse_s3_root_rejects_a_root_with_no_bucket() -> None:
    with pytest.raises(StoreHomeError):
        parse_s3_root("s3:///prod")


def test_parse_s3_root_rejects_a_non_s3_string() -> None:
    with pytest.raises(StoreHomeError):
        parse_s3_root("/mnt/data")


def test_s3_home_url_round_trips_bucket_and_prefix() -> None:
    assert S3Home(bucket="b", prefix="p/q").url() == "s3://b/p/q"
    assert S3Home(bucket="b", prefix="").url() == "s3://b"


def test_store_home_unavailable_names_the_home_and_never_the_host_mount() -> None:
    home = S3Home(bucket="convex-hedge-bars", prefix="prod")

    error = StoreHomeUnavailable(home)

    assert error.home == home
    assert "s3://convex-hedge-bars/prod" in str(error)
    # No local path leaks into the reason -- only the pointer to more detail does.
    assert "data" not in str(error).split("docs/")[0]
