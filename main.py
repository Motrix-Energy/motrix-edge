import argparse
import logging
import signal
from importlib import import_module
from logging import Logger, getLogger
from sys import stdout
from threading import Event
from time import time
from typing import Any, Optional

from api.algorithm import Algorithm
from api.connector import Connector
from api.device import Device
from api.service import Service
from api.storage_backend import StorageBackend
from config.config import Config
from config.log_format import make_handler
from devices_manager.devices_manager import DevicesManager
from storage_manager.storage_manager import StorageManager
from supervisor.supervisor import RestartPolicy, Supervisor


class Main:
	LOGGER: Logger
	CONFIG: Config
	DEVICES_MANAGER: DevicesManager
	STORAGE_MANAGER: StorageManager
	SUPERVISOR: Supervisor
	SHUTDOWN_EVENT: Event
	SIGNAL_RECEIVED: Optional[int]

	def __init__(self, config_path: str = "config.json") -> None:
		self.CONFIG = Config(config_path)
		logger: Logger = getLogger()
		logger.addHandler(make_handler(stdout))
		logger.setLevel(self.CONFIG.logging_level)
		self.LOGGER = getLogger(__class__.__name__)
		self.DEVICES_MANAGER = DevicesManager()
		self.STORAGE_MANAGER = StorageManager()
		self.SUPERVISOR = Supervisor(RestartPolicy.from_runtime(self.CONFIG.runtime))
		self.SHUTDOWN_EVENT = Event()
		self.SIGNAL_RECEIVED = None

	def main(self) -> None:
		self.LOGGER.debug(self.CONFIG)
		signal.signal(signal.SIGTERM, self._request_shutdown)
		signal.signal(signal.SIGINT, self._request_shutdown)

		# Everything past this point is covered by shutdown(): a failure during startup
		# must still stop whatever already runs and close whatever storage is registered
		try:
			# create devices from the config earlier and store them in the devices manager so that then the connectors can use them
			devices = self.create_classes(self.CONFIG.devices, "kind", "devices", Device)
			for device in devices:
				self.DEVICES_MANAGER.update_device(device)

			# Storage backends (optional — system works with zero backends)
			if self.CONFIG.storage:
				backends = self.create_classes(self.CONFIG.storage, "class", "storage", StorageBackend, expected_name_suffix="backend")
				for backend in backends:
					self.STORAGE_MANAGER.register(backend)

			connectors: list[Connector] = self.create_classes(self.CONFIG.connectors, "protocol", "connectors", Connector, expected_name_suffix="connector")
			# device serialization
			connectors_by_name: dict[str, Connector] = {c.name: c for c in connectors}
			for connector_name, connector in connectors_by_name.items():
				connector_devices: dict[str, Device] = {
					d.name: d for d in devices
					if d.connector_options.get("name") == connector_name
				}
				connector.inject_devices(connector_devices)
				self.LOGGER.info(f"Connector {connector_name}: {len(connector_devices)} device(s) injected")

			# Construct every worker before starting any of them. A replay connector
			# begins publishing timesteps the moment its thread runs, and an algorithm
			# only becomes a participant of the lockstep barrier once it exists — built
			# after the connectors were started, it would miss the opening timesteps.
			algorithms = self.create_classes(self.CONFIG.algorithms, "class", "algorithms", Algorithm, arguments={"devices_manager": self.DEVICES_MANAGER})

			# Services own no devices; they observe the runtime or expose it. Both handles
			# are injected here rather than fetched from a singleton, so a service declares
			# its dependencies and stays unit-testable. The supervisor is safe to hand over
			# while it still holds no workers — a service keeps the reference, not a copy.
			services = self.create_classes(self.CONFIG.services, "class", "services", Service, expected_name_suffix="service", arguments={"devices_manager": self.DEVICES_MANAGER, "supervisor": self.SUPERVISOR})

			connector_workers = self.SUPERVISOR.supervise_all(connectors, "start")
			self.SUPERVISOR.supervise_all(algorithms, "loop")
			# Started last, deliberately: an observer that comes up before the things it
			# observes reports an empty runtime as a healthy one.
			self.SUPERVISOR.supervise_all(services, "start")
			self.LOGGER.debug(f"{self.SUPERVISOR.workers=}")

			if services and not connector_workers:
				# A log line, not control flow. Widening the liveness set below to include
				# services is exactly what must not happen — a server never finishes, so it
				# would keep every completed replay alive forever.
				self.LOGGER.warning("Service(s) configured but no connector: nothing keeps this run alive, it will shut down immediately")

			while not self.SHUTDOWN_EVENT.wait(0.5):
				# Worker-based, not thread-based: a connector between restarts is not finished.
				# An empty set is "all finished" — as before, a config with no connector exits at once.
				if all(worker.is_finished() for worker in connector_workers):
					self.LOGGER.info("All connectors finished, shutting down")
					break
		finally:
			self.shutdown()

	def _request_shutdown(self, signum: int, frame: Any) -> None:
		"""Signal handler: only sets the event.

		Logging here could deadlock — a signal interrupts the main thread wherever
		it is, including mid-emit inside the logging module's own lock.
		"""
		self.SIGNAL_RECEIVED = signum
		self.SHUTDOWN_EVENT.set()

	def shutdown(self) -> None:
		"""Wind the runtime down: stop the workers, then close storage."""
		reason = f"signal {self.SIGNAL_RECEIVED}" if self.SIGNAL_RECEIVED is not None else "run complete"
		self.LOGGER.info(f"Shutting down ({reason})...")
		stragglers = self.SUPERVISOR.stop_all(timeout=self.CONFIG.shutdown_timeout)
		for worker in stragglers:
			self.LOGGER.warning(
				f"Thread {worker.name} did not stop within {self.CONFIG.shutdown_timeout}s; "
				f"it is a daemon and will be killed at exit"
			)
		self.STORAGE_MANAGER.close_all()
		self.LOGGER.info("Shutdown complete")

	def create_classes(self, config_list: list, class_key: str, package: str, base_class: Any, *, expected_name_suffix: Optional[str] = None, arguments: Optional[dict] = None) -> list[Any]:
		"""Instantiate one plugin per config entry, in config order.

		A list, not a set: set iteration over instances is id-ordered, so devices reached
		DevicesManager in an order that varied between runs of the same config — and with
		it the order an algorithm sees in `self.devices.values()`, and the order two
		decisions land in storage within one timestep. Nothing is deduplicated by the
		change either, since two distinct plugin instances were never equal.
		"""
		if not expected_name_suffix:
			expected_name_suffix = ""
		if not arguments:
			arguments = {}
		classes: list[Any] = []
		for config_entry in config_list:
			try:
				clazz: str = config_entry[class_key]
				module = import_module(f"{package}.{clazz}")
				class_base = clazz.rsplit(".", 1)[-1] if "." in clazz else clazz
				expected_class_name = (class_base + expected_name_suffix).replace("_", "")
				actual_class: Optional[type] = None
				for name, cls in vars(module).items():
					if isinstance(cls, type) and name.lower() == expected_class_name:
						actual_class = cls
						break
				if actual_class is None:
					self.LOGGER.error(f"{base_class.__name__} {config_entry} not found : no class matching '{expected_class_name}' in module '{package}.{clazz}'")
					continue
				if issubclass(actual_class, base_class):
					classes.append(actual_class(config_entry["name"], **arguments, **config_entry.get("options", {})))
					self.LOGGER.info(f"{base_class.__name__} {config_entry["name"]} created")
					self.LOGGER.debug(f"{actual_class=}")
				else:
					self.LOGGER.error(f"{actual_class} is not a subclass of {base_class.__name__}")
			except (AttributeError, ModuleNotFoundError) as e:
				self.LOGGER.error(f"{base_class.__name__} {config_entry} not found : {e}")
			except TypeError as e:
				self.LOGGER.error(f"{base_class.__name__} {config_entry} could not be instantiated : {e}")
		self.LOGGER.debug(f"{classes=}")
		return classes


if __name__ == '__main__':
	parser = argparse.ArgumentParser(description="Motrix Edge")
	parser.add_argument("--config", default="config.json", help="Path to the configuration file (default: config.json)")
	args = parser.parse_args()

	start_time: float = time()
	try:
		Main(args.config).main()
	finally:
		getLogger().setLevel(logging.DEBUG)
		end_time: float = time()
		getLogger(__name__).debug(f"Execution time: {end_time - start_time:.2f} seconds")
