from enum import StrEnum, auto


class Environment(StrEnum):
	PROD = auto()
	TEST = auto()
	DEV = auto()
