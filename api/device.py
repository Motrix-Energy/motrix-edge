import copy
from abc import ABC, abstractmethod
from logging import Logger, getLogger
from threading import Event
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from connector import Connector
class Device(ABC):
	name: str
	data: dict[str, Any]
	connector_options: dict[str, Any]
	listener_options: dict[str, Any]
	controller_options: dict[str, Any]
	connector: "Connector"
	LOGGER: Logger
	_data_ready_event: Event
	_connected_event: Event
	is_readable: bool = True  # receives data from connector
	is_writable: bool = False  # can receive control commands
	@abstractmethod
	def __init__(self, name: str, connector_options: dict[str, Any], listener_options: dict[str, Any], controller_options: dict[str, Any]) -> None:
		self.LOGGER = getLogger(f"{self.__class__.__name__}/{name}")
		self.name = name
		self.connector_options = connector_options
		self.listener_options = listener_options
		self.controller_options = controller_options
		self.data = {}
		self._data_ready_event = Event()
		self._connected_event = Event()
		if not self.is_readable:
			self._data_ready_event.set()  # no data to wait for

	def __deepcopy__(self, memo):
		"""Snapshot the *data*; share the live handles.

		A snapshot exists so an algorithm can read a device without racing the connector
		that writes it. Two attributes are not data and are deliberately shared rather
		than copied — both hold unpicklable locks, so copying them was never an option
		anyway, and both must reach the live object to mean anything:

		- `connector`: control has to arrive at the real transport, not at a copy of a
		  paho client.
		- the readiness `Event`s: they are how the connector *signals* the device, so a
		  copied Event is a handle nothing will ever set. Copying them made
		  `Algorithm._wait_for_required_devices` — which waits on a snapshot — burn its
		  full `wait_for_devices_timeout` on every required device, twice, however ready
		  the device actually was, and under a replay that silently cost the opening
		  timesteps of the backtest. Sharing also makes `is_data_ready()` / `is_connected()`
		  answer for *now* rather than for whenever the snapshot was taken, which is what
		  every caller of them wants.

		Nothing writes through a snapshot: `mark_data_ready()` and `mark_connected()` are
		called by connectors, on the live device.
		"""
		cls = self.__class__
		new = cls.__new__(cls)
		memo[id(self)] = new
		for k, v in self.__dict__.items():
			if isinstance(v, Event) or k == "connector":
				setattr(new, k, v)
			else:
				setattr(new, k, copy.deepcopy(v, memo))
		return new

	def is_data_ready(self) -> bool:
		return self._data_ready_event.is_set()

	def is_connected(self) -> bool:
		"""Non-blocking read of the connected event, the counterpart to is_data_ready().

		`wait_until_connected(timeout=0)` answers the same question, but putting a *wait*
		call in a read path is one careless edit away from blocking the caller forever.
		"""
		return self._connected_event.is_set()

	def mark_data_ready(self) -> None:
		"""Called by connector after first successful receive()"""
		self._data_ready_event.set()

	def mark_connected(self) -> None:
		"""Called by connector once transport is established"""
		self._connected_event.set()

	def wait_until_connected(self, timeout: float | None = None) -> bool:
		return self._connected_event.wait(timeout=timeout)

	def wait_until_ready(self, timeout: float | None = None) -> bool:
		"""Returns True if ready, False if timed out"""
		return self._data_ready_event.wait(timeout=timeout)
	@abstractmethod
	def receive(self, *args, **kwargs) -> Optional[bool]:
		"""Parse raw transport data into `self.data`.

		Return **False** when the payload produced no usable update — a corrupt frame, a
		topic this device does not handle — so the framework records a gap rather than
		republishing the previous reading under a new timestamp. Anything else, `None`
		included, counts as accepted, so a device written before this contract existed
		keeps working unchanged.
		"""
		pass

	def control(self, command: str) -> None:
		if not self.is_writable:
			self.LOGGER.warning(f"Device '{self.name}' is not writable, ignoring command '{command}'")
			return
		if hasattr(self, "connector"):
			self.connector.send(self, command)
		else:
			self.LOGGER.warning("Cannot send control command: no connector assigned")
