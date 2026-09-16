from abc import ABCMeta
from threading import Lock
from typing import Any


class Singleton(type):
	"""
	This allows to turn a class into a Thread-Safe Singleton

	Usage:
	class ExampleClass(metaclass=Singleton):
	"""
	_instances: dict = {}
	_lock: Lock = Lock()

	def __call__(cls, *args: Any, **kwargs: Any) -> Any:
		# Fast path: no lock once the instance exists (hot path for DevicesManager())
		if cls not in cls._instances:
			with cls._lock:
				if cls not in cls._instances:
					cls._instances[cls] = super().__call__(*args, **kwargs)
		return cls._instances[cls]


class AbstractSingleton(ABCMeta):
	"""
	This allows to turn an abstract class into a Thread-Safe Singleton

	Usage:
	class ExampleClass(metaclass=AbstractSingleton):
	"""
	_instances: dict = {}
	_lock: Lock = Lock()

	def __call__(cls, *args: Any, **kwargs: Any) -> Any:
		if cls not in cls._instances:
			with cls._lock:
				if cls not in cls._instances:
					cls._instances[cls] = super().__call__(*args, **kwargs)
		return cls._instances[cls]
