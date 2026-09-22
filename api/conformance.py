"""Checks a plugin can run against its own code, in or out of this repository.

**Not a supported public API**, for the same reason as `api/testing.py`: it is extracted so
an author working in their own repository can run the checks this repository runs on the
plugins it ships, and so `TestShippedSchemas` and the kit cannot drift — the suite here is
a thin caller, so if the kit breaks, this repository's `pytest` goes red.

Nothing here imports `pytest`. Each check returns a `Report`; turning a report into an
assertion or a skip is the caller's job, because only the caller knows whether it is
running under pytest, under unittest, or in a script. Nothing here cites a line number
either: this is the one module meant to be read outside the checkout, and the lines it
would name move.

The checks answer questions the loader asks at startup and a schema cannot ask itself:

- `check_schemas_are_valid` — is each shipped schema a valid JSON Schema at all?
- `check_kwargs_lockstep` — config options are spread into the constructor, so every key a
  schema allows must be a parameter, and every parameter that is not injected must be a
  key. Drift here is a plugin that passes its own tests and is rejected at startup.
- `check_injected_handles_absent` — `devices_manager` and `supervisor` are handed in by
  `main`, not by config. A schema declaring one lets a config entry collide with the
  injected value and fail instantiation with "got multiple values".
- `check_switch_honesty` — `Switch` is a class-level type claim algorithms act on with no
  `is_writable` check, so a read-only class inheriting it is selected as an actuator.
- `check_loads` — the one nothing had: drive the real loader and see an instance come back.
"""

import glob
import importlib
import inspect
import json
import logging
import os
from typing import NamedTuple, Optional

from jsonschema import validators

from api.capabilities import Switch

# Which constructor parameters `main` supplies rather than config, per axis. Per-axis and
# not one flat set: used as a subtraction, a shared set would excuse a connector whose own
# constructor happened to take a parameter named `supervisor`. None does today, and this
# check should not be the reason that stays true.
INJECTED_BY_AXIS: dict[str, frozenset[str]] = {
	"connectors": frozenset(),
	"devices": frozenset(),
	"storage": frozenset(),
	"algorithms": frozenset({"devices_manager"}),
	"services": frozenset({"devices_manager", "supervisor"}),
}


class Finding(NamedTuple):
	"""One thing that is wrong, named by the plugin it is wrong in."""
	plugin: str
	problem: str


class Report(NamedTuple):
	"""What a check looked at, what it could not look at, and what was wrong.

	`checked` and `skipped` are both reported because "nothing was wrong" and "nothing was
	examined" are different answers and a caller must be able to tell them apart. A check
	that globbed an empty directory returns both empty, which is a failure of setup rather
	than a pass — and a green run having verified nothing is the failure mode these checks
	exist to prevent.
	"""
	checked: tuple[str, ...] = ()
	failures: tuple[Finding, ...] = ()
	skipped: tuple[str, ...] = ()
	advisories: tuple[Finding, ...] = ()

	@property
	def ok(self) -> bool:
		return not self.failures

	def describe(self) -> str:
		"""One line per finding, each naming its plugin — the shape the assertions had."""
		return "; ".join(f"{finding.plugin}: {finding.problem}" for finding in self.failures)

	def describe_advisories(self) -> str:
		"""Same shape, for the things that are worth a look but may be deliberate."""
		return "; ".join(f"{finding.plugin}: {finding.problem}" for finding in self.advisories)


class LoadReport(NamedTuple):
	"""`check_loads` reports separately: examined is not the same as constructed."""
	checked: tuple[str, ...] = ()
	created: tuple[str, ...] = ()
	failures: tuple[Finding, ...] = ()

	@property
	def ok(self) -> bool:
		return not self.failures

	def describe(self) -> str:
		return "; ".join(f"{finding.plugin}: {finding.problem}" for finding in self.failures)


def schema_files(directory: str) -> list[str]:
	"""Every `*.schema.json` in `directory`, sorted.

	A directory rather than a package name, so the same call works from a test file, from a
	script, or from another repository's layout. The in-repo caller derives it from
	`__file__`; nothing here does.
	"""
	return sorted(glob.glob(os.path.join(directory, "*.schema.json")))


def import_plugin_module(package: str, stem: str):
	"""Import `<package>.<stem>`, or None when only an optional dependency is missing.

	A plugin needing a library outside `requirements.txt` imports it at module top on
	purpose, so the loader logs one clean per-entry skip. A checkout without that extra
	installed cannot check the plugin and should not fail over it.

	A schema naming a module that does not exist is a different thing entirely — it is the
	drift these checks exist to catch — so only the first case is tolerated.

	The discrimination is on **prefix**, not equality. `package` may be dotted, because the
	published layout for a third-party plugin is `<axis>/<vendor>/<name>.py`: with
	`package="connectors.acme"` and a missing `connectors/acme/`, CPython raises with
	`name == "connectors.acme"`, which is not equal to the target and would be waved
	through as "optional dependency absent" — leaving the check green having verified
	nothing at all.
	"""
	target = f"{package}.{stem}"
	try:
		return importlib.import_module(target)
	except ModuleNotFoundError as missing:
		if missing.name and (missing.name == target or target.startswith(missing.name + ".")):
			raise
		return None


def _walk_init(cls: type, want_defaultless: bool) -> set[str]:
	"""Constructor parameter names, following `**kwargs` up the MRO.

	`inspect.signature` stops at the class it is given, so a plugin whose `__init__` is a
	pure `**kwargs` forwarder appears to accept nothing but `kwargs` — while at runtime the
	base class reads real, documented options out of it. Both shipped algorithms are exactly
	that shape, so without this walk an algorithm schema could not declare anything at all.

	The walk stops at the first `__init__` in the MRO that does **not** take `**kwargs`:
	that is where the forwarding ends and the real signature begins. `self` and `name` are
	dropped — `name` comes from the config entry, never from `options`.
	"""
	names: set[str] = set()
	for klass in cls.__mro__:
		init = klass.__dict__.get("__init__")
		if init is None:
			continue
		try:
			parameters = inspect.signature(init).parameters
		except (TypeError, ValueError):  # a C-level or otherwise unintrospectable __init__
			break
		takes_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())
		names |= {
			name for name, parameter in parameters.items()
			if parameter.kind not in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
			and (parameter.default is inspect.Parameter.empty or not want_defaultless)
		}
		if not takes_kwargs:
			break
	return names - {"self", "name"}


def effective_parameters(cls: type) -> set[str]:
	"""Every option name the constructor chain accepts."""
	return _walk_init(cls, want_defaultless=False)


def required_parameters(cls: type) -> set[str]:
	"""The subset of `effective_parameters` that has no default."""
	return _walk_init(cls, want_defaultless=True)


def expected_class(module, stem: str, suffix: str = "") -> Optional[type]:
	"""The class the loader will look for: module name plus the axis suffix, underscores
	stripped, matched case-insensitively over the module namespace.

	Re-derived here rather than imported from `main`, so a plugin author can run the schema
	checks without constructing a `Main`. `check_loads` drives the real loader instead, and
	is what proves this derivation still agrees with it.
	"""
	wanted = f"{stem}{suffix}".replace("_", "")
	for name, obj in vars(module).items():
		if isinstance(obj, type) and name.lower() == wanted:
			return obj
	return None


def _load(schema_path: str) -> dict:
	with open(schema_path, encoding="utf-8") as handle:
		return json.load(handle)


def check_schemas_are_valid(directory: str) -> Report:
	"""Every `*.schema.json` in `directory` is itself a valid JSON Schema."""
	checked: list[str] = []
	failures: list[Finding] = []
	for path in schema_files(directory):
		stem = os.path.basename(path).removesuffix(".schema.json")
		checked.append(stem)
		try:
			schema = _load(path)
		except json.JSONDecodeError as bad:
			failures.append(Finding(stem, f"not valid JSON: {bad}"))
			continue
		try:
			validators.validator_for(schema).check_schema(schema)
		except Exception as bad:  # SchemaError normally, but a bad $schema can raise others
			failures.append(Finding(stem, f"not a valid JSON Schema: {bad}"))
	return Report(tuple(checked), tuple(failures))


def check_kwargs_lockstep(directory: str, package: str, suffix: str = "") -> Report:
	"""Schema keys and constructor parameters agree, in both directions.

	Config options are spread into the constructor, so:

	- every key the schema allows must be a parameter, or the entry fails to instantiate;
	- everything the schema *requires* must be a parameter, for the same reason;
	- every parameter config could supply must be a key, or an operator cannot configure it
	  and `additionalProperties: false` rejects them for trying.

	The third subtracts the axis's injected handles, which come from `main` and never from
	config.

	The fourth — everything without a default appears in `required` — runs only for a schema
	that declares `required` at all. Most do not, deliberately: interpolation runs after
	validation, so a `${VAR}` template satisfies `required` and still arrives as `null`,
	which makes the keyword worse than useless for anything an environment variable can
	fill. No device declares it either, because a device's real options live inside the
	three option sub-dicts rather than beside them. Those are reported as skipped for this
	half rather than quietly passing it.
	"""
	injected = INJECTED_BY_AXIS.get(package.split(".")[0], frozenset())
	checked: list[str] = []
	skipped: list[str] = []
	failures: list[Finding] = []
	for path in schema_files(directory):
		stem = os.path.basename(path).removesuffix(".schema.json")
		module = import_plugin_module(package, stem)
		if module is None:
			skipped.append(stem)
			continue
		cls = expected_class(module, stem, suffix)
		if cls is None:
			failures.append(Finding(stem, f"no class named like '{stem}{suffix}' in {package}.{stem}"))
			continue
		checked.append(stem)
		schema = _load(path)
		params = effective_parameters(cls)
		properties = set(schema.get("properties", {}))
		required = set(schema.get("required", []))
		if properties - params:
			failures.append(Finding(stem, f"schema allows options the constructor rejects: {sorted(properties - params)}"))
		if required - params:
			failures.append(Finding(stem, f"schema requires options the constructor lacks: {sorted(required - params)}"))
		configurable = params - injected
		if configurable - properties:
			failures.append(Finding(stem, f"constructor takes options the schema does not declare: {sorted(configurable - properties)}"))
		if "required" not in schema:
			continue
		mandatory = required_parameters(cls) - injected
		if mandatory - required:
			failures.append(Finding(stem, f"constructor demands options the schema does not require: {sorted(mandatory - required)}"))
	return Report(tuple(checked), tuple(failures), tuple(skipped))


def check_injected_handles_absent(directory: str, package: str) -> Report:
	"""No schema declares a handle `main` injects.

	The subset check cannot catch this: both are genuine constructor parameters, so
	declaring one is legal by that rule and fatal at startup.
	"""
	injected = INJECTED_BY_AXIS.get(package.split(".")[0], frozenset())
	checked: list[str] = []
	failures: list[Finding] = []
	for path in schema_files(directory):
		stem = os.path.basename(path).removesuffix(".schema.json")
		checked.append(stem)
		declared = set(_load(path).get("properties", {})) & injected
		if declared:
			failures.append(Finding(stem, f"declares injected handle(s) as config options: {sorted(declared)}"))
	return Report(tuple(checked), tuple(failures))


def check_switch_honesty(*device_classes: type) -> Report:
	"""A class inheriting `Switch` is genuinely writable. The reverse is only advised.

	Algorithms select actuators with `isinstance(device, Switch)` and no `is_writable`
	check, and `isinstance` is class-level, so this cannot be decided per instance. A
	read-only class inheriting `Switch` is therefore picked as an actuator and commanded
	every tick, which is the direction that does damage and the one reported as a failure.

	The reverse — writable but not a `Switch` — is an **advisory**, because the two claims
	are not the same one. `Switch` promises a device accepts the `COMMAND_ON`/`COMMAND_OFF`
	tokens; `is_writable` promises only that a command can be sent at all. `devices/pseudo.py`
	is the shipped example of the gap: it accepts any command string and logs it, which is
	writable without being a binary switch. Reporting that as a failure would push every
	such device into a capability it does not implement.
	"""
	checked: list[str] = []
	failures: list[Finding] = []
	advisories: list[Finding] = []
	for cls in device_classes:
		checked.append(cls.__name__)
		claims = issubclass(cls, Switch)
		writable = bool(getattr(cls, "is_writable", False))
		if claims and not writable:
			failures.append(Finding(cls.__name__, "inherits Switch but is not writable, so every decision it is sent is refused"))
		elif writable and not claims:
			advisories.append(Finding(cls.__name__, "is writable but does not inherit Switch, so no algorithm can select it as an actuator"))
	return Report(tuple(checked), tuple(failures), (), tuple(advisories))


def check_loads(entry: dict, class_key: str, package: str, base_class: type, *,
				expected_name_suffix: Optional[str] = None,
				arguments: Optional[dict] = None) -> LoadReport:
	"""Drive the real loader with one config entry and see whether an instance comes back.

	This is the check nothing had. Every other check here reasons about a schema and a
	signature, and a plugin can satisfy both and still be rejected at startup — because the
	class name does not match the naming rule, or the class does not subclass the axis ABC.
	Those two gates had no test at all, in a repository whose own contributing guide tells
	authors to rely on them.

	One entry per call, deliberately: the loader's "is not a subclass" error names neither
	the entry nor its name, so with several entries in flight a failure cannot be attributed
	to the one that caused it.
	"""
	from unittest.mock import patch

	from main import Main

	name = str(entry.get("name", "<unnamed>"))
	records: list[logging.LogRecord] = []

	class _Capture(logging.Handler):
		def emit(self, record: logging.LogRecord) -> None:
			if record.levelno >= logging.ERROR:
				records.append(record)

	handler = _Capture()
	root = logging.getLogger()
	# `Main.__init__` unconditionally adds a stdout handler to the root logger, so calling
	# this in a loop would otherwise multiply every log line by the number of calls.
	# Snapshot and restore rather than change startup logging, which is not this module's
	# business.
	before = list(root.handlers)
	level_before = root.level
	root.addHandler(handler)
	try:
		with patch("main.Config") as MockConfig:
			config = MockConfig.return_value
			config.logging_level = logging.DEBUG
			config.connectors, config.algorithms, config.devices, config.storage, config.services = [], [], [], [], []
			config.runtime = {}
			config.shutdown_timeout = 0.1
			created = Main().create_classes(
				[entry], class_key, package, base_class,
				expected_name_suffix=expected_name_suffix, arguments=arguments or {},
			)
	finally:
		root.removeHandler(handler)
		for added in [h for h in root.handlers if h not in before]:
			root.removeHandler(added)
		root.setLevel(level_before)

	failures: list[Finding] = []
	if not created:
		detail = " / ".join(record.getMessage() for record in records) or "no instance, and nothing logged to say why"
		failures.append(Finding(name, detail))
	return LoadReport((name,), tuple(instance.name for instance in created), tuple(failures))


def axis_directory(repository_root: str, package: str) -> str:
	"""`<repository_root>/<axis>/<vendor>` for a dotted package, `<root>/<axis>` otherwise.

	The dotted form is the published third-party layout, where `connectors.acme.solar`
	means `connectors/acme/solar.py` beside `connectors/acme/solar.schema.json`.
	"""
	return os.path.join(repository_root, *package.split("."))
