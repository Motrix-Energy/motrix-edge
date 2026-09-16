from typing import override

from api.algorithm import Algorithm
from api.capabilities import EnergyMeter, Switch
from api.devices_access import DevicesAccess


class AutoToggle(Algorithm):
	def __init__(self, name: str, devices_manager: DevicesAccess, **kwargs) -> None:
		super().__init__(name, devices_manager, **kwargs)

	@override
	def main(self) -> None:
		super().main()
		total: float = 0
		for device in self.devices.values():
			if isinstance(device, EnergyMeter):
				energy = device.get_total_energy_kwh()
				if energy is None:
					self.LOGGER.warning(f"Skipping {device.name}: no usable energy data")
					continue
				total += energy
		self.LOGGER.info(f"Total: {total}")
		for device in self.devices.values():
			if isinstance(device, Switch) and device.data:
				self.control_device(device, device.COMMAND_ON if total > 500 else device.COMMAND_OFF)
