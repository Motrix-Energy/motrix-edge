from typing import Any, Optional, override

from api.capabilities import Switch
from api.device import Device


class ShellyPlug(Device, Switch):
	is_writable: bool = True  # relay accepts on/off commands

	SUPPORTED_PROTOCOLS = ("mqtt",)

	PROTOCOL_REFUSAL = (
		"a Shelly Plug S reports one value per topic over MQTT, so this device reads 'mqtt' "
		"and nothing else"
	)

	UNSERVABLE_PROTOCOLS = {
		# A Gen1 Shelly really does have an HTTP API, so this is a reasonable thing to try and
		# a specific thing to be told about. /status is one JSON document holding every field
		# at once; this device's whole parse is a topic-suffix chain, and there is no reading
		# in a document with no topic attached to it.
		"http_api": "a Gen1 Shelly does serve /status over HTTP, but that is one JSON document holding every field at once, not the one-value-per-topic messages this device parses; there is no HTTP path here",
	}

	@override
	def __init__(self, name: str, connector_options: dict[str, Any], listener_options: dict[str, Any], controller_options: dict[str, Any]) -> None:
		super().__init__(name, connector_options, listener_options, controller_options)

	def update_data(self, data: dict[str, Any]) -> None:
		self.LOGGER.info(f"Updated data for {self.name}")
		self.data = data

	@override
	def receive(self, *args, **kwargs) -> Optional[bool]:
		"""Refuse a protocol this device cannot serve, then route on the topic.

		Unlike `devices/p1.py`, `devices/lora.py` and `devices/modbus_meter.py`, this device
		cannot absorb the one-argument arity with a sentinel or an `args[-1]`: the topic is
		not decoration here, it is the entire parse — `receive_mqtt` routes on its suffix and
		there is no other field in a Shelly message saying what the value means. So a replay
		row with an empty `topic` column is a gap, and is stated as one.

		What it must not be is what it was: this forwarded `*args, **kwargs` straight into a
		two-argument-only `receive_mqtt`, so such a row raised `TypeError` on the caller's
		thread — caught by `connectors/pseudo.py`'s replay loop and logged as "Error replaying
		entry", which sends an operator to inspect a replay file that is perfectly fine.
		"""
		if self.refuse_unserved_protocol(*args, **kwargs):
			return False
		if len(args) != 2:
			self.LOGGER.warning(f"A Shelly reading needs its topic to be readable; got {args=} on {self.name}, no reading taken")
			return False
		return self.receive_mqtt(args[0], args[1])

	def receive_mqtt(self, topic: str, payload: str) -> bool:
		if not isinstance(payload, str):
			# A replay line truncated after its `topic` column reaches here as (topic, None),
			# because csv.DictReader pads a short row. Storing that would file a reading of
			# nothing under a real topic name: losing a sample is acceptable, inventing one is
			# not.
			self.LOGGER.warning(f"No value in the message handed to {self.name} on {topic}, nothing to record")
			return False
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
					self.data.pop("overpower", None)
				case "off":
					self.data["status"] = False
					self.data.pop("overpower", None)
				case "overpower":
					# A real status this hardware really publishes — the comment above has
					# listed it since this file was written — and it used to raise
					# NotImplementedError from connectors/mqtt.py's per-message daemon thread,
					# where threading.excepthook prints to stderr past the configured logger.
					# A plug tripping its overpower protection is the moment an operator most
					# needs a log line, not the moment to lose one.
					#
					# `status` is deliberately left alone. The plug is saying it tripped, not
					# what its relay now reads; writing False here would be an inference
					# published as a measurement. Losing a sample is acceptable, inventing one
					# is not — and the previous `status` is the honest record, being the last
					# thing the plug actually said about the relay.
					#
					# Popped on the next on/off rather than initialised to False, and that is
					# not fastidiousness: storage/csv_file.py writes self.data verbatim into
					# data_json, so an always-present key would rewrite all eighteen shelly
					# rows of examples/auto_toggle/expected/device_data.csv — checksummed in
					# examples/MANIFEST.json and vendored into the viewer's repository. A key
					# that exists only while the fault does costs the fixture nothing. Clearing
					# it on on/off is reading rather than inferring: the three tokens are one
					# mutually exclusive status field, so "on" *is* the plug saying it is no
					# longer tripped.
					self.data["overpower"] = True
					self.LOGGER.warning(
						f"{self.name} reports an overpower trip; its overpower_value topic carries "
						f"the threshold in Watts. The relay status is left at its last reported value"
					)
				case _:
					# Not a failure, just not modelled — same treatment as an unmodelled topic
					# below, and for the same reason: nothing changed, so nothing should be
					# republished under a new timestamp.
					self.LOGGER.warning(f"Unmodelled relay status {payload!r} for {self.name}, no reading taken")
					return False
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
					# Same treatment as an unmodelled relay status: nothing usable arrived, so
					# nothing is republished — and it is no longer a crash on the per-message
					# daemon thread.
					self.LOGGER.warning(f"Unmodelled overtemperature status {payload!r} for {self.name}, no reading taken")
					return False
		else:
			# Not a failure, just not ours: the plug publishes topics this device does
			# not model. Nothing changed, so nothing should be republished.
			self.LOGGER.warning(f"Ignoring unknown topic {topic} for {self.name}")
			return False
		return True

