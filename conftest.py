"""pytest configuration for async test support."""
import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "asyncio: mark a test as an async test"
    )


pytest_plugins = ("pytest_asyncio",)
