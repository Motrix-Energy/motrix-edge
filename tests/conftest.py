"""Fixtures for this repository's suite, plus a re-export of the importable kit.

The axis-generic doubles and the threading harness live in `api/testing.py` so a plugin in
its own repository can import them; they are re-exported here so no test file's imports
change. `ruff.toml` already exempts this file from F401 for exactly that reason.

What stays here, and why it cannot move:

- Every **pytest fixture** — `api/testing.py` must not import `pytest`, or the test
  framework joins the production import path and the Docker image.
- The four **concrete-device factories** (`make_p1`, `make_shelly`, `make_pseudo`,
  `build_p1_telegram`) — they import `devices/` and `parsers/`, and `api/` importing an
  axis inverts the dependency the architecture rests on. They are this repository's own
  helpers, not part of the kit.
"""

from typing import Any, Optional

import pytest

from __metaclasses.singleton import Singleton, AbstractSingleton
from api.testing import (  # noqa: F401  (re-exported for the suite; see the module docstring)
	REPLAY_HEADERS,
	STOP_TIMEOUT,
	StubAlgorithm,
	StubConnector,
	StubDevice,
	StubStorageBackend,
	assert_stops,
	make_devices_access,
	run_in_thread,
	wait_until,
	write_replay,
)
from devices.p1 import P1
from devices.pseudo import Pseudo
from devices.shelly_plug import ShellyPlug
from parsers.obis import crc16_arc


@pytest.fixture(autouse=True)
def _clear_singletons():
    """Clear singleton instances before each test to prevent state leakage."""
    Singleton._instances.clear()
    AbstractSingleton._instances.clear()


@pytest.fixture
def make_device():
    """Factory fixture to create StubDevice instances."""
    def _make(**kwargs) -> StubDevice:
        return StubDevice(**kwargs)
    return _make


@pytest.fixture
def make_connector():
    """Factory fixture to create StubConnector instances."""
    def _make(**kwargs) -> StubConnector:
        return StubConnector(**kwargs)
    return _make


@pytest.fixture
def devices_manager():
    """Fresh DevicesManager instance (singleton is cleared by autouse fixture)."""
    from devices_manager.devices_manager import DevicesManager
    return DevicesManager()


@pytest.fixture
def storage_manager():
    """Fresh StorageManager instance (singleton is cleared by autouse fixture)."""
    from storage_manager.storage_manager import StorageManager
    return StorageManager()


@pytest.fixture
def mock_devices_access():
    """Factory for DevicesAccess doubles."""
    return make_devices_access


# --- real-device factories ---------------------------------------------------------
# The stubs in api/testing.py cover "any Device"; these cover the three concrete classes,
# which a dozen tests were each rebuilding inline with subtly different options. They stay
# here rather than moving with the stubs: they import devices/ and parsers/, which api/
# must not.


def make_p1(name: str = "p1_meter", protocol: str = "mqtt", **kwargs) -> P1:
    return P1(
        name=name,
        connector_options={"name": f"{protocol}_1", "protocol": protocol},
        listener_options=kwargs.get("listener_options", {"pattern": "p1/+"}),
        controller_options=kwargs.get("controller_options", {}),
    )


def make_shelly(name: str = "shelly_1", protocol: str = "mqtt", **kwargs) -> ShellyPlug:
    return ShellyPlug(
        name=name,
        connector_options={"name": f"{protocol}_1", "protocol": protocol},
        listener_options=kwargs.get("listener_options", {}),
        controller_options=kwargs.get("controller_options", {}),
    )


def make_pseudo(name: str = "pseudo_1", **kwargs) -> Pseudo:
    return Pseudo(
        name=name,
        connector_options=kwargs.get("connector_options", {"name": "pseudo_conn", "protocol": "pseudo"}),
        listener_options=kwargs.get("listener_options", {}),
        controller_options=kwargs.get("controller_options", {}),
    )


def build_p1_telegram(obis_lines: str) -> str:
    """A valid P1 telegram with a correct CRC: /XXX5<ident>\\r\\n\\r\\n<data>!<crc>\\r\\n.

    The `\\\\2` in the identification line is a literal backslash, as DSMR specifies and
    as a real ISKRA meter emits. One copy of this used a raw \\x02 STX byte instead, so
    two different telegrams travelled under one name.
    """
    body = f"/ISK5\\2M550E-1012\r\n\r\n{obis_lines}!"
    return f"{body}{crc16_arc(body.encode()):04X}\r\n"


@pytest.fixture
def main_app():
    """A `Main` wired to a stubbed Config, with every axis empty.

    One fixture rather than one per file: the two copies had already drifted, each
    stubbing a different subset of the config attributes, so a test that touched the
    missing one got a MagicMock instead of a value.
    """
    import logging
    from unittest.mock import patch

    from main import Main
    with patch("main.Config") as MockConfig:
        cfg = MockConfig.return_value
        cfg.logging_level = logging.DEBUG
        cfg.connectors, cfg.algorithms, cfg.devices, cfg.storage, cfg.services = [], [], [], [], []
        cfg.runtime = {}
        # Short on purpose: the tests that exercise the grace period assert on the
        # *message*, not on how long the wait took.
        cfg.shutdown_timeout = 0.1
        yield Main()
