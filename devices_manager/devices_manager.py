from copy import deepcopy
from datetime import datetime
from logging import getLogger
from threading import Lock
from typing import Optional, override

from __metaclasses.singleton import AbstractSingleton
from api.device import Device
from api.devices_access import DeviceCounts, DevicesAccess
from simulation.clock import SimulationClock


class DevicesManager(DevicesAccess, metaclass=AbstractSingleton):
	devices: dict[str, Device]
	devices_lock: Lock

	@override
	def __init__(self) -> None:
		self.LOGGER = getLogger(__class__.__name__)
		super().__init__()
		self.devices_lock = Lock()
		self.devices = {}

	@override
	def get_devices(self, filters: Optional[dict[str, object]] = None) -> dict[str, Device]:
		def match(value, filter_value):
			if isinstance(filter_value, dict) and isinstance(value, dict):
				return all(k in value and match(value[k], v) for k, v in filter_value.items())
			return value == filter_value
		if filters is None:
			filters = {}
		with self.devices_lock:
			return {
				name: deepcopy(device)
				for name, device in self.devices.items()
				if all(
					match(getattr(device, key), value)
					for key, value in filters.items()
				)
			}

	@override
	def get_device(self, name: str) -> Optional[Device]:
		with self.devices_lock:
			return deepcopy(self.devices.get(name))

	@override
	def count_devices(self) -> DeviceCounts:
		"""Readiness totals, read live under the lock — no copy.

		The base implementation would deepcopy every device, payloads included, to then
		call three predicates on the copies. Nothing here needs a snapshot: both predicates
		read a `threading.Event`, and `Device.__deepcopy__` deliberately *shares* those
		events, so the copies answer for the live objects anyway.
		"""
		with self.devices_lock:
			devices = list(self.devices.values())
		return DeviceCounts(
			total=len(devices),
			connected=sum(1 for device in devices if device.is_connected()),
			data_ready=sum(1 for device in devices if device.is_data_ready()),
		)

	def update_device(self, device: Device) -> None:
		with self.devices_lock:
			self.devices[device.name] = device

	def set_simulation_time(self, dt: Optional[datetime]) -> None:
		"""Publish a replay timestep, or None to hand algorithms back to the wall clock.

		Kept as a delegate so a connector written against the old single-clock API still
		works. The clock itself lives in `simulation/clock.py`: its barrier must not be
		held behind `devices_lock`, or a replay waiting for algorithms would block the
		`update_device()` calls those algorithms are waiting on.
		"""
		if dt is None:
			SimulationClock().reset()
		else:
			SimulationClock().publish_step(dt)

	@override
	def get_simulation_time(self) -> Optional[datetime]:
		"""The last committed timestep. Unchanged semantics — algorithms and algorithm
		decisions read this; only device readings moved to the event clock."""
		return SimulationClock().get_step_time()

	@override
	def control(self, device_name: str, command: str) -> bool:
		with self.devices_lock:
			device = self.devices.get(device_name)
		if device is None:
			self.LOGGER.warning(f"Cannot control unknown device '{device_name}'")
			return False
		return device.control(command)

	def remove_device(self, name: str) -> None:
		with self.devices_lock:
			del self.devices[name]
