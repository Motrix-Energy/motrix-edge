from abc import ABC, abstractmethod
from datetime import datetime
from logging import Logger, getLogger
from typing import Any

from api.device import Device


class StorageBackend(ABC):
	"""Abstract base class for storage backends.

	Storage backends persist device data and algorithm decisions.
	Declare zero or more in config.json — the system works identically
	when no storage is configured.
	"""
	name: str
	LOGGER: Logger

	@abstractmethod
	def __init__(self, name: str) -> None:
		self.LOGGER = getLogger(f"{self.__class__.__name__}/{name}")
		self.name = name

	@abstractmethod
	def write_device_data(self, device: Device, data: dict[str, Any]) -> None:
		"""Called on every device receive()."""
		pass

	@abstractmethod
	def write_algorithm_decision(self, algorithm: str, device: str, command: str) -> None:
		"""Called when an algorithm sends a control command."""
		pass

	@abstractmethod
	def read(self, device: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
		"""Read stored device data within a time range."""
		pass

	def close(self) -> None:  # noqa: B027  (empty and non-abstract by design — see below)
		"""Flush buffers and release resources. Called once, at shutdown.

		Concrete (not abstract) so existing backends keep working: a backend that
		writes straight through has nothing to do. Override it if you batch writes
		or hold a connection — anything still buffered when this returns is lost.
		"""
		pass

	# --- event time -------------------------------------------------------------
	# Neither write method takes a timestamp: a backend derives its own, and under a
	# replay that must be the simulated moment, not the wall clock. The two writes
	# resolve to different clocks, which is why this lives here instead of being
	# hand-rolled in every backend.

	def _data_timestamp(self) -> datetime:
		"""Event time for a device reading: when that reading was produced.

		Under a replay this is the timestep currently being dispatched, so a reading is
		filed against its own entry rather than the previously committed step.
		"""
		from simulation.clock import SimulationClock
		return SimulationClock().get_event_time() or datetime.now()

	def _decision_timestamp(self) -> datetime:
		"""Event time for an algorithm decision: the timestep the algorithm was stepping.

		Deliberately the committed step rather than the dispatch clock — a decision
		computed while processing T belongs to T even if the replay has moved on.

		Delegated to `api.decisions` so the rule has one implementation: the in-memory
		history behind `/decisions` stamps the same decision, and two copies of this would
		eventually disagree about when it happened.
		"""
		from api.decisions import decision_timestamp
		return decision_timestamp()
