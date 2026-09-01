"""Test-only SQL fixture for asserting legacy table rows.

The production Store intentionally exposes no generic database handle.  These
tests still need a small Core view to seed and inspect rows that are not yet
covered by a named repository method.
"""

from ustc_crawler.db.core import CoreConnection


def store_core(store: object) -> CoreConnection:
    return getattr(store, "_core")
