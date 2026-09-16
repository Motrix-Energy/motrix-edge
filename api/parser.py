from abc import ABC, abstractmethod


class Parser(ABC):
	@abstractmethod
	def __init__(self) -> None:
		pass

	@abstractmethod
	def parse(self, transmission: str) -> dict:
		pass
