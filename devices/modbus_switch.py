from typing import Any, override

from api.capabilities import Switch
from devices.modbus_meter import ModbusMeter


class ModbusSwitch(ModbusMeter, Switch):
	"""A Modbus device an algorithm may actuate — a relay, a breaker, a setpoint register.

	A separate kind rather than a flag on `ModbusMeter`, because `Switch` is a *type* claim
	that algorithms act on directly and cannot be made conditional per instance:
	`algorithms/auto_toggle.py` selects actuators with `isinstance(device, Switch) and
	device.data`, with no `is_writable` check. A read-only revenue meter subclassing `Switch` is
	therefore selected as an actuator and commanded on every tick. `Device.control` refuses each
	command and `Algorithm.control_device` records nothing when it does, so `algorithm_decisions.csv`
	stays honest — but the algorithm still believes it holds an actuator, and goes on asking a device
	that can never move to close a coil. The gate in `control_device` protects the versioned storage
	contract; splitting the class is what stops the algorithm making that mistake in the first place.

	It inherits `ModbusMeter`'s parsing because a device that both meters and switches is
	the common case — a smart breaker, an inverter with a power setpoint — and a coil
	read-back is itself a reading.
	"""
	is_writable: bool = True  # accepts on/off (or a numeric setpoint) via controller_options

	@override
	def __init__(self, name: str, connector_options: dict[str, Any], listener_options: dict[str, Any], controller_options: dict[str, Any]) -> None:
		super().__init__(name, connector_options, listener_options, controller_options)
		if not controller_options.get("address") and controller_options.get("address") != 0:
			# Writable by class but unroutable by config: say so at startup rather than
			# once per control command for the life of the run.
			self.LOGGER.warning(
				f"{self.name} is a modbus_switch but declares no controller_options.address; "
				f"control commands will be dropped by the connector"
			)
