"""Option coercion for plugin constructors.

`Config` validates a plugin's options schema **before** resolving `${VAR}`, and an
interpolated value is always a `str` — or `None` when a whole-value token resolves empty.
So every numeric and boolean option a plugin declares can arrive as a string, and every
one can arrive as `None`.

These helpers never raise. `main.create_classes` catches only `AttributeError`,
`ModuleNotFoundError` and `TypeError`, so a `ValueError` escaping a plugin's `__init__`
escapes `create_classes` too and takes the whole process down over one mistyped tuning
knob. Warn and fall back to the default instead.

The same reasoning covers config values read outside a plugin constructor:
`RestartPolicy.from_runtime` runs in `Main.__init__`, *before* the `try/finally` in
`main()` is entered, and `Config.shutdown_timeout` is read from inside that `finally` —
a raise in either place has no shutdown path, or masks the exception being handled.
"""
from logging import Logger
from math import isfinite
from typing import Any, Optional

TRUTHY = frozenset({"1", "true", "yes", "on"})
FALSY = frozenset({"0", "false", "no", "off", ""})


def int_option(logger: Logger, key: str, value: Any, default: int, minimum: int = 0, maximum: Optional[int] = None) -> int:
	"""Coerce a config option to an int, warning and defaulting on anything unusable."""
	if value is None or value == "":
		return default
	try:
		number = int(str(value).strip())
	except (TypeError, ValueError):
		logger.warning(f"Option '{key}'={value!r} is not an integer, using {default}")
		return default
	if number < minimum:
		logger.warning(f"Option '{key}'={number} is below {minimum}, using {default}")
		return default
	if maximum is not None and number > maximum:
		logger.warning(f"Option '{key}'={number} is above {maximum}, using {default}")
		return default
	return number


def float_option(logger: Logger, key: str, value: Any, default: float, minimum: float = 0.0, maximum: Optional[float] = None) -> float:
	"""Coerce a config option to a float, warning and defaulting on anything unusable.

	Non-finite values are rejected rather than passed through: `float("nan")` compares
	False against every bound, so an unguarded NaN would satisfy any `minimum` and land
	in a `wait()` or a backoff multiplier as an unbounded sleep.
	"""
	if value is None or value == "":
		return default
	try:
		number = float(str(value).strip())
	except (TypeError, ValueError):
		logger.warning(f"Option '{key}'={value!r} is not a number, using {default}")
		return default
	if not isfinite(number):
		logger.warning(f"Option '{key}'={value!r} is not a finite number, using {default}")
		return default
	if number < minimum:
		logger.warning(f"Option '{key}'={number} is below {minimum}, using {default}")
		return default
	if maximum is not None and number > maximum:
		logger.warning(f"Option '{key}'={number} is above {maximum}, using {default}")
		return default
	return number


def bool_option(logger: Logger, key: str, value: Any, default: bool) -> bool:
	"""Coerce a config option to a bool, warning and defaulting on anything unusable."""
	if value is None:
		return default
	if isinstance(value, bool):
		return value
	text = str(value).strip().lower()
	if text in TRUTHY:
		return True
	if text in FALSY:
		return False
	logger.warning(f"Option '{key}'={value!r} is not a boolean, using {default}")
	return default
