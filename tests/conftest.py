"""Shared pytest fixtures for the ANFS test suite.

Existing tests build their own engine inline; new tests should prefer
these fixtures instead of repeating the tempdir + AnfsEngine setup.
"""

import pytest

import anfs_core


@pytest.fixture
def anfs_paths(tmp_path):
    """(db_path, objs_dir) strings inside a per-test temporary directory."""
    return str(tmp_path / "anfs.db"), str(tmp_path / "objs")


@pytest.fixture
def anfs_engine(anfs_paths):
    """A fresh AnfsEngine backed by a per-test SQLite database."""
    db_path, objs_dir = anfs_paths
    return anfs_core.AnfsEngine(db_path, objs_dir)
