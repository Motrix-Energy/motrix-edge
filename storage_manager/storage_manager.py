from datetime import datetime
from logging import Logger, getLogger
from typing import Any

from __metaclasses.singleton import Singleton
from api.decisions import DecisionLog
from api.device import Device
from api.storage_backend import StorageBackend


class StorageManager(metaclass=Singleton):
	"""Singleton that fans out storage calls to all registered backends.

	Populated by main.py during startup. If no backends are registered, `write_device_data`
	and `read()`'s error are the whole story — but `write_algorithm_decision` still records
	to the in-memory `DecisionLog`, which is deliberately not a backend and therefore not
	subject to the operator having configured one. See that method.

	It holds no lock of its own, deliberately: `register()` is only ever called from the
	main thread during startup, before any connector or algorithm exists to write, and the
	backend list is never mutated afterwards. What *is* concurrent is the fan-out itself —
	every connector thread calls write_device_data() — and thread safety there is each
	backend's own job (CsvFileBackend holds a lock, InfluxDBBackend hands off to the
	client's batching writer).
	"""
	LOGGER: Logger

	def __init__(self) -> None:
		self.LOGGER = getLogger(self.__class__.__name__)
		self._backends: list[StorageBackend] = []
		self._closed = False

	def register(self, backend: StorageBackend) -> None:
		self._backends.append(backend)
		self.LOGGER.info(f"Storage backend registered: {backend.name} ({backend.__class__.__name__})")

	def _fan_out(self, method: str, *args: Any) -> None:
		"""Call `method` on every backend, isolating each one's failures from the rest.

		A storage backend is a side channel: a broken one must never take down the
		connector thread that was merely reporting a reading.
		"""
		for backend in self._backends:
			try:
				getattr(backend, method)(*args)
			except Exception as e:
				self.LOGGER.error(f"Storage backend {backend.name} failed on {method}: {e}")

	def write_device_data(self, device: Device, data: dict[str, Any]) -> None:
		self._fan_out("write_device_data", device, data)

	def write_algorithm_decision(self, algorithm: str, device: str, command: str) -> None:
		# Recorded before the fan-out and outside it. This is the one funnel every backend
		# already sees, so hooking it here is what makes /decisions and
		# algorithm_decisions.csv hold the same decisions in the same order by construction
		# — a second call site in Algorithm.control_device would be one careless edit from
		# drifting. Outside the fan-out because the history is not a backend: it must still
		# be recorded when the operator configured no storage at all.
		#
		# Isolated for the same reason _fan_out isolates backends: a side channel must never
		# take down the algorithm thread that was merely reporting a decision.
		try:
			DecisionLog().record(algorithm, device, command)
		except Exception as e:
			self.LOGGER.error(f"Decision history failed to record: {e}")
		self._fan_out("write_algorithm_decision", algorithm, device, command)

	def close_all(self) -> None:
		"""Close every backend at shutdown, isolating failures the same way writes are.

		Idempotent: main calls it from a `finally`, so a second call after an
		explicit one must be harmless.
		"""
		if self._closed:
			return
		self._closed = True
		for backend in self._backends:
			try:
				backend.close()
				self.LOGGER.info(f"Storage backend closed: {backend.name}")
			except Exception as e:
				self.LOGGER.error(f"Storage backend {backend.name} failed on close: {e}")

	def read(self, device: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
		if not self._backends:
			raise ValueError("No storage backends configured")
		for backend in self._backends:
			try:
				result = backend.read(device, start, end)
				if result:
					return result
			except Exception as e:
				self.LOGGER.error(f"Storage backend {backend.name} failed on read: {e}")
		return []
