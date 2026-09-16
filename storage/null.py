from datetime import datetime
from typing import Any, override

from api.device import Device
from api.storage_backend import StorageBackend


class NullBackend(StorageBackend):
	"""Explicit no-op storage backend. Useful for testing and as a placeholder."""

	@override
	def __init__(self, name: str) -> None:
		super().__init__(name)

	@override
	def write_device_data(self, device: Device, data: dict[str, Any]) -> None:
		pass

	@override
	def write_algorithm_decision(self, algorithm: str, device: str, command: str) -> None:
		pass

	@override
	def read(self, device: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
		return []
