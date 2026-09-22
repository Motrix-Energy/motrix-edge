from typing import Any, override

from api.capabilities import Switch
from devices.openems import Openems


class OpenemsSwitch(Openems, Switch):
	"""An OpenEMS component with a writable channel — a relay output, an ESS setpoint.

	A separate kind rather than a flag on `Openems`, because `Switch` is a *type* claim
	that algorithms act on directly and cannot be made conditional per instance:
	`algorithms/auto_toggle.py` selects actuators with `isinstance(device, Switch) and
	device.data`, with no `is_writable` check. A read-only `_sum` view subclassing `Switch` is
	therefore selected as an actuator and commanded on every tick. `Device.control` refuses each
	command and `Algorithm.control_device` records nothing when it does, so `algorithm_decisions.csv`
	stays honest — but the algorithm still believes it holds an actuator, and goes on asking a device
	that can never move to accept a setpoint. The gate in `control_device` protects the versioned
	storage contract; splitting the class is what stops the algorithm making that mistake in the
	first place.

	It inherits all of `Openems`'s parsing, because readable-and-writable is the common
	case here — `ess0` is both, and splitting it into two config entries pointing at one
	component would be worse than one extra module.
	"""
	is_writable: bool = True  # POSTs {"value": ...} to controller_options.component/channel

	@override
	def __init__(self, name: str, connector_options: dict[str, Any], listener_options: dict[str, Any], controller_options: dict[str, Any]) -> None:
		super().__init__(name, connector_options, listener_options, controller_options)
		if not controller_options.get("component") or not controller_options.get("channel"):
			# Writable by class but unroutable by config: say so once at startup rather
			# than once per control command for the life of the run.
			self.LOGGER.warning(
				f"{self.name} is an openems_switch but declares no controller_options.component/channel; "
				f"control commands will be dropped by the connector"
			)
