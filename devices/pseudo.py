from json import loads, JSONDecodeError
from typing import Any, override

from api.device import Device


class Pseudo(Device):
	"""Generic pseudo device for local development and backtesting.

	Accepts any payload, optionally parses JSON, and logs control commands
	instead of sending them to hardware. Algorithms are completely unaware
	they are running in simulation.
	"""
	is_readable: bool = True
	is_writable: bool = True

	@override
	def __init__(self, name: str, connector_options: dict[str, Any], listener_options: dict[str, Any], controller_options: dict[str, Any]) -> None:
		super().__init__(name, connector_options, listener_options, controller_options)

	@override
	def receive(self, *args, **kwargs) -> bool:
		if len(args) == 2:
			topic, payload = args[0], args[1]
			self.data["topic"] = topic
		elif len(args) == 1:
			payload = args[0]
			self.data["topic"] = None
		else:
			self.LOGGER.error(f"Unexpected receive args: {args}")
			return False

		self.data["payload"] = payload
		try:
			self.data["parsed"] = loads(payload)
		except (JSONDecodeError, TypeError):
			# A non-JSON payload is still a payload: this device stores it raw.
			self.data["parsed"] = None
		return True

	@override
	def control(self, command: str) -> None:
		self.LOGGER.info(f"[PSEUDO CONTROL] {command}")
		super().control(command)
