import csv
from datetime import datetime
from threading import Event, Thread
from typing import Any, Callable, Optional, Sequence
from unittest.mock import MagicMock

import pytest

from __metaclasses.singleton import Singleton, AbstractSingleton
from api.algorithm import Algorithm
from api.connector import Connector
from api.device import Device
from api.devices_access import DevicesAccess
from api.storage_backend import StorageBackend
from devices.p1 import P1
from devices.pseudo import Pseudo
from devices.shelly_plug import ShellyPlug
from parsers.obis import crc16_arc

# A stopped worker must return in well under this; the suite stays fast.
STOP_TIMEOUT = 2.0


@pytest.fixture(autouse=True)
def _clear_singletons():
    """Clear singleton instances before each test to prevent state leakage."""
    Singleton._instances.clear()
    AbstractSingleton._instances.clear()


# --- threading harness -------------------------------------------------------------
# Every test that drives a blocking worker needs the same three moves: run it in a daemon
# thread, poll until something becomes true, and assert it returns after stop(). They
# lived in four files with four slightly different timeouts.


def run_in_thread(target: Callable[[], Any]) -> Thread:
    """Run `target` in a started daemon thread."""
    thread = Thread(target=target, daemon=True)
    thread.start()
    return thread


def wait_until(predicate: Callable[[], bool], timeout: float = STOP_TIMEOUT, interval: float = 0.01) -> bool:
    """Poll `predicate` until it holds or `timeout` elapses. True if it held.

    Event().wait() rather than time.sleep(): the whole suite is interruptible, so a
    wedged test fails on its timeout instead of hanging a CI worker.
    """
    clock = Event()
    deadline = timeout
    while deadline > 0:
        if predicate():
            return True
        clock.wait(interval)
        deadline -= interval
    return predicate()


def assert_stops(worker: Any, thread: Thread, timeout: float = STOP_TIMEOUT) -> None:
    """Call stop() and assert the worker's thread returns inside `timeout`."""
    worker.stop()
    thread.join(timeout)
    assert not thread.is_alive(), f"{worker} did not return within {timeout}s of stop()"


class StubDevice(Device):
    """Concrete Device subclass for testing."""

    def __init__(
        self,
        name: str = "test_device",
        connector_options: dict[str, Any] | None = None,
        listener_options: dict[str, Any] | None = None,
        controller_options: dict[str, Any] | None = None,
        is_readable: bool = True,
        is_writable: bool = False,
    ):
        # Set instance-level flags before super().__init__ which reads them
        self.__dict__["is_readable"] = is_readable
        self.__dict__["is_writable"] = is_writable
        super().__init__(
            name,
            connector_options or {},
            listener_options or {},
            controller_options or {},
        )

    def receive(self, *args, **kwargs) -> None:
        pass


class StubConnector(Connector):
    """Concrete Connector subclass for testing."""

    def __init__(self, name: str = "test_connector"):
        super().__init__(name)

    def start(self) -> None:
        pass

    def send(self, device: Device, payload: str) -> None:
        pass


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


class StubStorageBackend(StorageBackend):
    """Concrete StorageBackend subclass for testing. Records all calls."""

    def __init__(self, name: str = "test_storage"):
        super().__init__(name)
        self.device_data_calls: list[tuple] = []
        self.algorithm_decision_calls: list[tuple] = []
        self.read_calls: list[tuple] = []

    def write_device_data(self, device, data):
        self.device_data_calls.append((device, data))

    def write_algorithm_decision(self, algorithm, device, command):
        self.algorithm_decision_calls.append((algorithm, device, command))

    def read(self, device, start, end):
        self.read_calls.append((device, start, end))
        return []


@pytest.fixture
def storage_manager():
    """Fresh StorageManager instance (singleton is cleared by autouse fixture)."""
    from storage_manager.storage_manager import StorageManager
    return StorageManager()


class StubAlgorithm(Algorithm):
    """Counts main() calls so a running loop is observable."""

    def __init__(self, name: str = "stub_algo", devices_manager: Any = None, **kwargs):
        super().__init__(name, devices_manager if devices_manager is not None else make_devices_access(), **kwargs)
        self.main_calls = 0
        self.ran = Event()

    def main(self) -> None:
        super().main()
        self.main_calls += 1
        self.ran.set()


def make_devices_access(devices: Optional[dict] = None, simulation_time: Optional[datetime] = None) -> MagicMock:
    """A DevicesAccess double. spec'd, so a renamed method fails here instead of
    silently returning a new Mock that every assertion then passes against."""
    manager = MagicMock(spec=DevicesAccess)
    manager.get_devices.return_value = devices or {}
    manager.get_simulation_time.return_value = simulation_time
    return manager


@pytest.fixture
def mock_devices_access():
    """Factory for DevicesAccess doubles."""
    return make_devices_access


# --- real-device factories ---------------------------------------------------------
# The stubs above cover "any Device"; these cover the three concrete classes, which a
# dozen tests were each rebuilding inline with subtly different options.


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


REPLAY_HEADERS = ["timestamp", "device_name", "topic", "payload"]


def write_replay(path, rows: Sequence[Sequence[Any]], quoted: bool = False) -> str:
    """Write a CSV replay file from (timestamp, device_name, topic, payload) rows.

    `quoted=True` routes through csv.writer, so a payload may contain commas, quotes or
    an embedded CRLF — which the plain join cannot express. The unquoted form is kept
    because it is what a hand-written replay file actually looks like.
    """
    with open(path, "w", newline="") as f:
        if quoted:
            writer = csv.writer(f)
            writer.writerow(REPLAY_HEADERS)
            writer.writerows(rows)
        else:
            f.write(",".join(REPLAY_HEADERS) + "\n")
            for row in rows:
                f.write(",".join(str(value) for value in row) + "\n")
    return str(path)
