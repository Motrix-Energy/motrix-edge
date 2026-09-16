from abc import ABC, abstractmethod
from logging import Logger, getLogger
from typing import Optional

from api.device import Device
from api.stoppable import Stoppable


class Connector(Stoppable, ABC):
	name: str
	devices: dict[str, Device]
	LOGGER: Logger

	@abstractmethod
	def __init__(self, name: str) -> None:
		super().__init__()  # Stoppable: arms the cooperative-stop event
		self.LOGGER = getLogger(f"{self.__class__.__name__}/{name}")
		self.name = name

	@abstractmethod
	def start(self) -> None:
		"""Blocking run loop, driven by the supervisor in its own thread.

		Poll `self.is_stopping()` and sleep via `self.wait_stop(seconds)` so a
		shutdown request is honoured promptly; override `stop()` if the loop
		blocks on something an event alone cannot interrupt (see `Stoppable`).
		"""
		pass

	def inject_devices(self, devices: dict[str, Device]) -> None:
		self.devices = devices
		for device in devices.values():
			device.connector = self  # back-reference for control commands
		self.LOGGER.info(f"{len(devices)} device(s) injected: {list(devices.keys())}")

	@abstractmethod
	def send(self, device: Device, payload: str) -> None:
		pass

	def on_connected(self) -> None:
		"""Call this in concrete connectors once transport is established"""
		for device in self.devices.values():
			if not device.is_readable:
				device.mark_connected()
		self.LOGGER.info("Connector established, write-only devices marked connected")

	def on_device_data_received(self, device: Device, accepted: Optional[bool] = True) -> None:
		"""Call this in concrete connectors after device.receive(), passing its result.

		A payload arriving always proves the transport is alive, but only an accepted one
		is a reading. Publishing a rejected payload would re-report the device's previous
		value under the new timestamp, turning a stalled or corrupt meter into a flat line
		in storage instead of the gap it actually is.

		`accepted` defaults to True so a connector written before this contract existed
		keeps publishing everything, exactly as it did.
		"""
		device.mark_connected()  # something arrived, so the connection is live
		if accepted is False:
			return
		device.mark_data_ready()
		from devices_manager.devices_manager import DevicesManager
		DevicesManager().update_device(device)  # publish live snapshot so algorithms see it
		from storage_manager.storage_manager import StorageManager
		StorageManager().write_device_data(device, device.data)
