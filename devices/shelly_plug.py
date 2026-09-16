from typing import Any, Optional, override

from api.capabilities import Switch
from api.device import Device


class ShellyPlug(Device, Switch):
	is_writable: bool = True  # relay accepts on/off commands

	@override
	def __init__(self, name: str, connector_options: dict[str, Any], listener_options: dict[str, Any], controller_options: dict[str, Any]) -> None:
		super().__init__(name, connector_options, listener_options, controller_options)

	def update_data(self, data: dict[str, Any]) -> None:
		self.LOGGER.info(f"Updated data for {self.name}")
		self.data = data

	@override
	def receive(self, *args, **kwargs) -> Optional[bool]:
		match self.connector_options["protocol"]:
			case "mqtt":
				return self.receive_mqtt(*args, **kwargs)
			case _:
				self.LOGGER.error(f"Unknown protocol {self.connector_options['protocol']} for {self.name}")
				self.LOGGER.debug(f"{self.connector_options=}, {args=}, {kwargs=}")
				raise NotImplementedError(f"Protocol {self.connector_options['protocol']} not implemented for {self.name}")

	def receive_mqtt(self, topic: str, payload: str) -> bool:
		self.LOGGER.info(f"Received MQTT message for {self.name} on topic {topic}: {payload}")
		# shellies/<model>-<deviceid>/relay/0 to report status: on, off or overpower
		# shellies/<model>-<deviceid>/relay/0/power to report instantaneous power consumption rate in Watts
		# shellies/<model>-<deviceid>/relay/0/energy to report amount of energy consumed in Watt-minute
		# shellies/<model>-<deviceid>/relay/0/overpower_value reports the value in Watts, on which an overpower condition is detected
		# that is invalid code, we need to use if else chains instead of match case in python
		if topic.endswith("/relay/0/power"):
			self.data["power"] = payload
		elif topic.endswith("/relay/0/energy"):
			self.data["energy"] = payload
		elif topic.endswith("/relay/0/overpower_value"):
			self.data["overpower_value"] = payload
		elif topic.endswith("/relay/0"):
			match payload:
				case "on":
					self.data["status"] = True
				case "off":
					self.data["status"] = False
				case _:
					self.LOGGER.error(f"Unknown status {payload} for {self.name}")
					self.LOGGER.debug(f"{self.connector_options=}, {topic=}, {payload=}")
					raise NotImplementedError(f"Status {payload} not implemented for {self.name}")
		elif topic.endswith("/temperature"):
			self.data["temperature"] = payload
		elif topic.endswith("/temperature_f"):
			self.data["temperature_f"] = payload
		elif topic.endswith("/overtemperature"):
			match payload:
				case "0":
					self.data["overtemperature"] = False
				case "1":
					self.data["overtemperature"] = True
				case _:
					self.LOGGER.error(f"Unknown overtemperature status {payload} for {self.name}")
					self.LOGGER.debug(f"{self.connector_options=}, {topic=}, {payload=}")
					raise NotImplementedError(f"Overtemperature status {payload} not implemented for {self.name}")
		else:
			# Not a failure, just not ours: the plug publishes topics this device does
			# not model. Nothing changed, so nothing should be republished.
			self.LOGGER.warning(f"Ignoring unknown topic {topic} for {self.name}")
			return False
		return True

