from abc import ABC, abstractmethod
from datetime import datetime
from typing import NamedTuple, Optional

from api.device import Device


class DeviceCounts(NamedTuple):
	"""How many devices exist, are connected, and have produced a reading."""
	total: int
	connected: int
	data_ready: int


class DevicesAccess(ABC):
	@abstractmethod
	def __init__(self) -> None:
		pass

	@abstractmethod
	def get_devices(self, filters: Optional[dict[str, object]] = None) -> dict[str, Device]:
		pass

	def get_device(self, name: str) -> Optional[Device]:
		"""One device by name, or None when it is unknown.

		Concrete, not abstract: an implementation written before this existed keeps
		working, and name-based lookup is already part of this contract's vocabulary —
		`control()` takes a name too. `DevicesManager` overrides it to copy the one device
		instead of copying the whole registry to throw all but one away.
		"""
		return self.get_devices().get(name)

	def count_devices(self) -> DeviceCounts:
		"""Readiness totals across the registry, without materialising it.

		Concrete for the same reason `get_device` is: an implementation written before
		this existed keeps working. `DevicesManager` overrides it to read under its lock
		without copying — the health endpoint asks for these three integers on every poll,
		and paying a deepcopy of every device payload to compute them is pure waste.
		"""
		devices = self.get_devices().values()
		return DeviceCounts(
			total=len(devices),
			connected=sum(1 for device in devices if device.is_connected()),
			data_ready=sum(1 for device in devices if device.is_data_ready()),
		)

	@abstractmethod
	def get_simulation_time(self) -> Optional[datetime]:
		pass

	@abstractmethod
	def control(self, device_name: str, command: str) -> None:
		pass
