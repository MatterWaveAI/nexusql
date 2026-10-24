"""
Test helpers for code built on NexusQL.

Factories describe a table's row shape once so each test states only the
columns it cares about; snapshots capture tables and put them back or report
what changed; ``truncate`` empties tables with the dialect-correct statement
and resets their generated keys.

The pytest fixtures live in :mod:`nexusql.testing.pytest_plugin`, which
pytest loads through the ``pytest11`` entry point. Nothing here imports
pytest, so the factories and snapshots work from any test runner or from a
plain script seeding a development database.
"""

from .cleanup import truncate
from .factories import Factory, Sequence, SubFactory
from .snapshots import Snapshot, TableDiff, snapshot

__all__ = [
    'Factory',
    'Sequence',
    'SubFactory',
    'Snapshot',
    'TableDiff',
    'snapshot',
    'truncate',
]
