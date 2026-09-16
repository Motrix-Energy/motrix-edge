from typing import Any, override

from api.capabilities import Switch
from devices.ha_entity import HaEntity


class HaSwitch(HaEntity, Switch):
	"""A Home Assistant entity an algorithm may actuate — a switch, a light, a climate mode.

	A separate kind rather than a flag on `HaEntity`, because `Switch` is a *type* claim
	that algorithms act on directly and cannot be made conditional per instance:
	`algorithms/auto_toggle.py` selects actuators with `isinstance(device, Switch) and
	device.data`, with no `is_writable` check — and `Algorithm.control_device` writes the
	decision to storage *before* `Device.control` gets to refuse a non-writable device. A
	read-only `sensor.living_room_temperature` subclassing `Switch` would therefore put a
	row in `algorithm_decisions.csv` claiming an algorithm turned a thermometer on, every
	tick: a wrong entry in the versioned storage contract, not merely a noisy log.

	It inherits all of `HaEntity`'s parsing, because a Home Assistant switch reports its own
	state on the same subscription that carries every other entity's.
	"""
	is_writable: bool = True  # actuated via controller_options.commands -> call_service

	@override
	def __init__(self, name: str, connector_options: dict[str, Any], listener_options: dict[str, Any], controller_options: dict[str, Any]) -> None:
		super().__init__(name, connector_options, listener_options, controller_options)
		if not controller_options.get("entity_id") and not controller_options.get("commands"):
			# Writable by class but unroutable by config: say so once at startup rather
			# than once per control command for the life of the run.
			self.LOGGER.warning(
				f"{self.name} is an ha_switch but declares neither controller_options.entity_id "
				f"nor controller_options.commands; control commands will be dropped"
			)
