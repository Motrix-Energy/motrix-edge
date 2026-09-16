from typing import Any, override

from api.capabilities import Switch
from devices.lora import LoRa


class LoRaSwitch(LoRa, Switch):
	"""A LoRa node an algorithm may actuate — a relay, a valve, a setpoint.

	A separate kind rather than a flag on `LoRa`, because `Switch` is a *type* claim that
	algorithms act on directly and cannot be made conditional per instance:
	`algorithms/auto_toggle.py` selects actuators with `isinstance(device, Switch) and
	device.data`, with no `is_writable` check — and `Algorithm.control_device` writes the
	decision to storage *before* `Device.control` gets to refuse a non-writable device. A
	read-only temperature node subclassing `Switch` would therefore put a row in
	`algorithm_decisions.csv` claiming an algorithm turned a thermometer on, every tick: a
	wrong entry in the versioned storage contract, not merely a noisy log.

	**A downlink here is queued, not sent.** A Class A node opens its receive windows only
	just after its own uplink, so the decision row this device's actuation produces is
	timestamped when the EMS decided, and the relay may move minutes later or not at all. That
	is true of `connectors/lorawan.py` and of `connectors/lora.py` alike; see the former's
	class docstring for what can and cannot be done about it.
	"""
	is_writable: bool = True  # the connector base64-encodes controller_options.on_payload/off_payload

	@override
	def __init__(self, name: str, connector_options: dict[str, Any], listener_options: dict[str, Any], controller_options: dict[str, Any]) -> None:
		super().__init__(name, connector_options, listener_options, controller_options)
		if not controller_options.get("f_port"):
			# Writable by class but unroutable by config: say so once at startup rather than
			# once per control command for the life of the run. There is deliberately no
			# default port — see LoRaWANConnector._resolve_f_port.
			self.LOGGER.warning(
				f"{self.name} is a lora_switch but declares no controller_options.f_port; "
				f"control commands will be dropped by the connector"
			)
		if connector_options.get("protocol") == "mqtt":
			# A plain MQTTConnector publishes the raw command token to controller_options.topic
			# with no encoding and no envelope, so the network server would reject it — or
			# worse, accept a downlink the node cannot read. Reading over plain mqtt is
			# supported on purpose; writing is not.
			self.LOGGER.warning(
				f"{self.name} is a lora_switch behind a plain 'mqtt' connector; it can read, but a "
				f"downlink needs the 'lorawan' or 'lora' protocol to be encoded and wrapped"
			)
