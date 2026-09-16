from typing import override

from api.algorithm import Algorithm
from api.device import Device
from api.devices_access import DevicesAccess


class DeviceChecker(Algorithm):
	def __init__(self, name: str, devices_manager: DevicesAccess, **kwargs) -> None:
		super().__init__(name, devices_manager, **kwargs)

	@override
	def main(self) -> None:
		super().main()
		if not self.devices:
			self.LOGGER.info("No devices")
		else:
			devices_without_data: dict[str, Device]
			devices_with_data: dict[str, Device]
			devices_without_data, devices_with_data = {}, {}
			for device in self.devices.values():
				(devices_without_data if not device.data else devices_with_data)[device.name] = device
			self.LOGGER.info(f"Devices without data: {devices_without_data}\nDevices with data: {devices_with_data}")
