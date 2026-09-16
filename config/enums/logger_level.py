import logging
from enum import StrEnum, auto


class LoggerLevel(StrEnum):
	DEBUG = auto()
	INFO = auto()
	WARNING = auto()
	ERROR = auto()
	CRITICAL = auto()

	def logging_level(self) -> int:
		return {
			self.DEBUG: logging.DEBUG,
			self.INFO: logging.INFO,
			self.WARNING: logging.WARNING,
			self.ERROR: logging.ERROR,
			self.CRITICAL: logging.CRITICAL,
		}[self]
