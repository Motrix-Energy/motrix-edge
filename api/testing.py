"""Axis-generic test doubles and a threading harness, importable outside this checkout.

**Not a supported public API.** It is extracted so a plugin in its own repository can be
tested the way the shipped plugins are, and so the five recipes in `CONTRIBUTING.md` stop
telling authors to copy files that open with `from tests.conftest import …` — an import
that resolves in this checkout and nowhere else. The shape of what is here may change with
the code it doubles; treat it as internal until a version handshake says otherwise
(`api/version.py` does not exist, deliberately — see SHAREABLE_COMPONENTS.md).

Two rules decide what lives here rather than in `tests/conftest.py`, and both are
load-bearing:

- **Nothing here imports `pytest`.** This is a shipped module on the normal import path;
  a `pytest` import would put the test framework into every production environment, and
  into the Docker image. Every pytest *fixture* therefore stays in `tests/conftest.py`,
  which re-exports the plain callables below so no test file's imports change.
- **Nothing here imports `devices/`, `parsers/` or `connectors/`.** `api/` is the layer
  every plugin axis is written against; importing an axis from it inverts the dependency
  the whole architecture rests on. That is why `make_p1`, `make_shelly`, `make_pseudo` and
  `build_p1_telegram` stay in `tests/conftest.py` — they build concrete shipped devices and
  belong to this repository's own suite, not to the kit.
"""

import csv
from datetime import datetime
from threading import Event, Thread
from typing import Any, Callable, Optional, Sequence
from unittest.mock import MagicMock

from api.algorithm import Algorithm
from api.connector import Connector
from api.device import Device
from api.devices_access import DevicesAccess
from api.storage_backend import StorageBackend

# A stopped worker must return in well under this; the suite stays fast.
STOP_TIMEOUT = 2.0

REPLAY_HEADERS = ["timestamp", "device_name", "topic", "payload"]


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


# --- axis doubles ------------------------------------------------------------------


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
