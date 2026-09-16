from abc import ABC, abstractmethod
from typing import Any


class EnergyMeter(ABC):
	"""Device that measures cumulative imported energy."""

	@abstractmethod
	def get_total_energy_kwh(self) -> float | None:
		"""Total imported energy in kWh.

		Returns 0.0 when the data is well-formed but holds no kWh register,
		and None when the data is missing or malformed (log a warning, never raise).
		"""


class Switch(ABC):  # noqa: B024  (no abstract method by design — see below)
	"""On/off-controllable device.

	Subclasses override the command tokens if their hardware doesn't speak literal on/off.

	Deliberately a marker interface with no abstract method: a switch is actuated through
	`DevicesAccess.control(name, command)`, not through a method on the device, so what
	this class declares is *that the device accepts these tokens* — which is exactly what
	an algorithm's `isinstance(device, Switch)` check asks.
	"""

	COMMAND_ON: str = "on"
	COMMAND_OFF: str = "off"


class MetricSource(ABC):
	"""Device that can name its own readings.

	The other capabilities are the device↔algorithm contract; this one is the
	device↔storage contract. It exists for stores that turn structure into schema — a
	time-series database keys on field names, so a payload whose *position* carries
	meaning (P1's OBIS list) becomes unreadable once flattened generically: index 7 is
	a different register the moment the meter emits a different number of lines.
	A device implementing this decides its own names; a device whose payload is already
	flat and stably keyed does not need it, and the generic path stays correct for it.
	"""

	@abstractmethod
	def get_metrics(self) -> dict[str, Any]:
		"""Flat {name: scalar} view of the current data.

		Names must be stable across messages — never positional. Returns {} when there
		is nothing to report, and {} when the data is missing or malformed (log a
		warning, never raise).
		"""
