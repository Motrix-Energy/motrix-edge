from enum import EnumType
from importlib.resources import files
from json import JSONDecodeError, load, loads
from logging import Logger, getLogger
from os import environ, path
from re import compile
from sys import stdout
from typing import Any, Optional

from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for

from __metaclasses.singleton import Singleton
from api.options import float_option
from config.enums.environment import Environment
from config.enums.logger_level import LoggerLevel
from config.log_format import make_handler
from config.version import CONFIG_FORMAT_VERSION, Compatibility, compare

# ${VAR} or ${VAR:-default} — group 1 is the var name, group 2 the default
# (None when no ":-" is present, "" for a bare "${VAR:-}")
_ENV_PATTERN = compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

# A protocol/kind/class value is turned into a path — connectors/<protocol>.schema.json,
# devices/<kind>.schema.json, algorithms/<class>.schema.json, storage/<class>.schema.json
# or services/<class>.schema.json — so it must be a dotted chain of plain module names and
# nothing else. A third-party plugin lives at <axis>/<vendor>/<name>.py and names itself
# "<vendor>.<name>" in config, which is why the dot is allowed; each segment still has to
# be a Python identifier.
#
# This is the **only** traversal guard there is. `files(package).joinpath()` performs no
# containment check of its own, so '../etc/passwd' escapes the package directory and an
# absolute name escapes entirely. Keep `.fullmatch`: with `.match` the trailing `*` would
# accept 'evil/../x' by matching only its leading 'evil'. The rejected shapes are worth
# naming, because each is a real config typo or a real attempt: '../etc/passwd', 'a/b',
# '/abs', '.hidden', 'a..b' and 'a.' all fail.
#
# The *import* path needs no such guard — `import_module` raises ModuleNotFoundError for
# every hostile name, which the loader already treats as a clean per-entry skip.
_PLUGIN_NAME = compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")

# Config loads before main() configures logging; this bootstrap handler uses the same
# format so early warnings render identically — see `config/log_format.py`. warn()/error()
# attach it only while the root logger has no handlers, then defer to the root ones.
_BOOTSTRAP_HANDLER = make_handler(stdout)


class Config(metaclass=Singleton):
	"""
	This class hold the config of the application

	See `config.schema.json` for the schema; per-protocol connector options are
	validated against `connectors/<protocol>.schema.json` shipped with each connector
	"""
	DEFAULT_ENV: Environment = Environment.PROD
	DEFAULT_LOGGER_LEVEL: dict[Environment, LoggerLevel] = {
		Environment.DEV: LoggerLevel.DEBUG,
		Environment.TEST: LoggerLevel.DEBUG,
		Environment.PROD: LoggerLevel.INFO
	}
	DEFAULT_LISTENERS: list[dict[str, Any]] = []
	DEFAULT_ALGORITHMS: list[dict[str, Any]] = []
	DEFAULT_DEVICES: list[dict[str, Any]] = []
	DEFAULT_STORAGE: list[dict[str, Any]] = []
	DEFAULT_SERVICES: list[dict[str, Any]] = []
	# Shutdown grace period + thread-restart policy; see supervisor/supervisor.py
	DEFAULT_RUNTIME: dict[str, Any] = {
		"shutdown_timeout_seconds": 10,
		"restart": True,
		"max_restarts": 5,
		"backoff_seconds": 1,
		"max_backoff_seconds": 60
	}

	VERSION: Optional[str]
	ENV: Environment
	LOGGER_LEVEL: LoggerLevel
	CONNECTORS: list[dict[str, Any]]
	ALGORITHMS: list[dict[str, Any]]
	DEVICES: list[dict[str, Any]]
	STORAGE: list[dict[str, Any]]
	SERVICES: list[dict[str, Any]]
	RUNTIME: dict[str, Any]

	def __init__(self, file_path: path = "config.json") -> None:
		try:
			with open(file_path) as file:
				config = load(file)
		except FileNotFoundError:
			config = {}
		try:
			with open("config.schema.json") as file:
				schema = load(file)
		except FileNotFoundError:
			schema = {}
		validator = validator_for(schema)(schema)
		errors = list(validator.iter_errors(config))
		if errors:
			Config.warn(f"Found {len(errors)} errors in config file")
			for error in errors:
				Config.warn(error)
		# Read from the RAW config, before `_interpolate()` below. A document's format version
		# is a property of the document, not of the machine reading it, so `${VAR}` in it is a
		# mistake rather than a feature — and reading it raw turns that mistake into a warning
		# naming the template instead of a mysterious null. It also makes `Config.version` hold
		# exactly the bytes in the file, which is what `motrix-edge-view`'s topology.ts reads
		# from the same file and never interpolates.
		#
		# `and declared` folds the empty string in with absence, matching topology.ts's
		# `stringOrNull` (`value !== ''`): the two repositories then report the identical value
		# for the identical file in every case, which is the property that lets one of them
		# cite the other. The raw value — not this normalised one — is what is judged, so a
		# `version` of another JSON type is reported as unreadable rather than as unsaid.
		declared_version = config.get("version")
		self.VERSION = declared_version if isinstance(declared_version, str) and declared_version else None
		self._report_config_format(declared_version)
		self._plugin_schemas: dict[tuple[str, str], Optional[dict]] = {}
		self._validate_plugin_options(config.get("connectors", []), "connectors", "protocol", "Connector")
		self._validate_plugin_options(config.get("devices", []), "devices", "kind", "Device")
		self._validate_plugin_options(config.get("storage", []), "storage", "class", "Storage")
		self._validate_plugin_options(config.get("services", []), "services", "class", "Service")
		# Algorithms are the fifth axis, and were the one with no options validation at all.
		# They are also the axis a contributor is most likely to write, and the one whose
		# options (`required_devices`, `delay_seconds`, `wait_for_devices_timeout`) most need
		# a declared contract: the base class reads them, so a typo in one was silently
		# ignored rather than warned about.
		self._validate_plugin_options(config.get("algorithms", []), "algorithms", "class", "Algorithm")
		# Resolve ${VAR} env references after validating the raw template: the templates
		# are valid strings under the schema, whereas an interpolated optional credential
		# may resolve to null — validating first avoids spurious "not of type string" warnings
		config = self._interpolate(config)
		self.ENV = self._get_enum(Environment, config.get("env", self.DEFAULT_ENV), self.DEFAULT_ENV)
		self.LOGGER_LEVEL = self._get_enum(LoggerLevel, config.get("logger_level", self.DEFAULT_LOGGER_LEVEL[self.ENV]), self.DEFAULT_LOGGER_LEVEL[self.ENV])
		# Per-key merge: an operator overriding one knob keeps the defaults for the rest
		self.RUNTIME = {**self.DEFAULT_RUNTIME, **config.get("runtime", {})}

		# Every axis is deduplicated by name, and for two of them that is load-bearing
		# rather than tidiness:
		# - SimulationClock keys its barrier participants by algorithm name, so two
		#   algorithms sharing one would be tracked as a single participant and the first
		#   to ack() would release the replay while the second is still inside main() —
		#   the exact lockstep failure simulation/clock.py exists to prevent.
		# - DevicesManager.update_device() and main's connector-matching both key by
		#   device name, so a duplicate silently drops one device on iteration order.
		#   Deduplicating here means the dropped entry never reaches _resolved_devices.
		self.CONNECTORS = self._unique_by_name(config.get("connectors", self.DEFAULT_LISTENERS), "Connector")
		self.ALGORITHMS = self._unique_by_name(config.get("algorithms", self.DEFAULT_ALGORITHMS), "Algorithm")
		self.DEVICES = self._unique_by_name(config.get("devices", self.DEFAULT_DEVICES), "Device")
		self.STORAGE = self._unique_by_name(config.get("storage", self.DEFAULT_STORAGE), "Storage")
		self.SERVICES = self._unique_by_name(config.get("services", self.DEFAULT_SERVICES), "Service")

		self._connector_by_name: dict[str, dict] = {
			c["name"]: c for c in self.CONNECTORS
		}

		self._resolved_devices: list[dict] = []
		for device in self.DEVICES:
			connector_name = device.get("options", {}).get("connector_options", {}).get("name")
			connector = self._connector_by_name.get(connector_name)
			if connector is None:
				Config.error(f"Connector '{connector_name}' not found for device '{device.get('name')}'")
				continue
			# A connector may stand in for another transport — a replay connector with
			# `emulates: "mqtt"` should look to devices exactly like the broker it
			# replaces, so they need no knowledge of being replayed.
			protocol = connector.get("options", {}).get("emulates") or connector["protocol"]
			# Build a clean copy with protocol injected, don't mutate original
			resolved = {
				**device,
				"options": {
					**device.get("options", {}),
					"connector_options": {
						**device.get("options", {}).get("connector_options", {}),
						"protocol": protocol
					}
				}
			}
			self._resolved_devices.append(resolved)

	@property
	def version(self) -> Optional[str]:
		return self.VERSION

	@property
	def env(self) -> Environment:
		return self.ENV

	@property
	def logging_level(self) -> int:
		return self.LOGGER_LEVEL.logging_level()

	@property
	def connectors(self) -> list[dict[str, Any]]:
		return self.CONNECTORS

	@property
	def algorithms(self) -> list[dict[str, Any]]:
		return self.ALGORITHMS

	@property
	def devices(self) -> list[dict[str, Any]]:
		return self._resolved_devices

	@property
	def storage(self) -> list[dict[str, Any]]:
		return self.STORAGE

	@property
	def services(self) -> list[dict[str, Any]]:
		return self.SERVICES

	@property
	def runtime(self) -> dict[str, Any]:
		return self.RUNTIME

	@property
	def shutdown_timeout(self) -> float:
		"""Coerced, never bare `float()`: main reads this from inside shutdown()'s
		`finally`, where a raise would mask the exception actually being handled."""
		return float_option(
			Config._logger(), "shutdown_timeout_seconds",
			self.RUNTIME.get("shutdown_timeout_seconds"),
			self.DEFAULT_RUNTIME["shutdown_timeout_seconds"], minimum=0.0,
		)

	@staticmethod
	def _report_config_format(declared: Any) -> None:
		"""Say what this build makes of the document's declared format version.

		The verdict itself is `config/version.py`'s, which is pure and logs nothing; this is
		the only place it becomes English. Split that way so a future migrator branches on
		`Compatibility` rather than on a log line — see that module's docstring for why there
		is no migrator yet.

		Every message that is not silence ends in an action an operator can take — set the
		key, rewrite the file, upgrade the build. This loader emits genuinely broken wiring
		into the same channel at the same levels, before logging is even configured, so a
		line that only reports a difference spends an operator's attention without buying
		them anything. A verdict for which no action can be named has not earned a message,
		which is most of why `UNDECLARED` and `COMPATIBLE` say nothing at all.

		Nothing here raises, and nothing here is fatal — not even an incompatible major.
		`Config` is built in `Main.__init__`, before `main()`'s `try/finally`, so an ERROR is
		the loudest thing this loader can honestly do. It renders through the bootstrap
		handler on stdout rather than through the operator's configured handler, as every
		other `Config` diagnostic already does.
		"""
		match compare(declared):
			case Compatibility.UNDECLARED:
				# A file that claims nothing is told nothing. The key is optional in
				# config.schema.json, and a check that contradicts the schema is a check
				# nobody can satisfy — which is the failure this replaced.
				pass
			case Compatibility.UNREADABLE:
				# Worth its own line although the schema already refused the value: the
				# schema says the string is malformed, and only this says what it cost —
				# that no compatibility check happened at all. When no config.schema.json
				# is present the schema says nothing whatsoever, and this is the only line.
				interpolated = " — '${' is not resolved in this key, because a document's format is a property of the document and not of the machine reading it" if isinstance(declared, str) and "${" in declared else ""
				Config.warn(
					f"Configuration version {declared!r} could not be read{interpolated}, so it was not "
					f"checked against this build's {CONFIG_FORMAT_VERSION}. Set \"version\" to three "
					f"numbers, or remove the key to say nothing about the format"
				)
			case Compatibility.COMPATIBLE:
				pass
			case Compatibility.FORWARD_MINOR:
				Config.warn(
					f"Configuration version '{declared}' is a newer minor than this build's "
					f"{CONFIG_FORMAT_VERSION}. A minor version only ever adds, so this file is read in "
					f"full — but anything it declares that this build's config.schema.json does not "
					f"know is ignored without comment. Upgrade Motrix Edge, or check the file against "
					f"the schema this build ships"
				)
			case Compatibility.INCOMPATIBLE_OLDER:
				Config.error(
					f"Configuration version '{declared}' is an older major than this build's "
					f"{CONFIG_FORMAT_VERSION}. Across a major version a key can have been renamed or "
					f"have changed meaning, so this file may be read wrongly rather than incompletely — "
					f"it is being read anyway. Rewrite it against config.schema.json and set \"version\" "
					f"to \"{CONFIG_FORMAT_VERSION}\"; there is no automatic migration"
				)
			case Compatibility.INCOMPATIBLE_NEWER:
				Config.error(
					f"Configuration version '{declared}' is a newer major than this build's "
					f"{CONFIG_FORMAT_VERSION}. Keys this file relies on may not exist here, and keys "
					f"that do exist may mean something else — it is being read anyway. Upgrade Motrix "
					f"Edge to a build that declares '{declared}', or rewrite the file against the "
					f"config.schema.json this build ships"
				)

	@staticmethod
	def _unique_by_name(entries: list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
		"""Drop entries whose `name` was already seen, keeping the first and logging the rest."""
		seen: set[Any] = set()
		unique: list[dict[str, Any]] = []
		for entry in entries:
			name = entry.get("name")
			if name in seen:
				Config.error(f"{label} name '{name}' is not unique")
			else:
				seen.add(name)
				unique.append(entry)
		return unique

	@staticmethod
	def _get_enum(enum: EnumType, value: Any, default: Any) -> Any:
		try:
			return enum(value)
		except ValueError:
			Config.warn(f"Invalid value '{value}' for enum '{enum.__name__}', using default '{default}' instead")
			return default

	def _load_plugin_schema(self, package: str, name: str) -> Optional[dict]:
		"""Load `<package>/<name>.schema.json`, or None when the plugin ships no schema."""
		key = (package, name)
		if key not in self._plugin_schemas:
			# The dot in a namespaced plugin name is a directory separator on disk:
			# "acme.solar" is connectors/acme/solar.schema.json, never a file literally
			# named acme.solar.schema.json, which no layout produces. This line and the
			# `_PLUGIN_NAME` regex are one change in two places: widening the regex alone
			# turns a silent skip into a silent *miss*, which is strictly worse — the entry
			# would look validated and never be.
			relative = f"{name.replace(chr(46), chr(47))}.schema.json"
			schema_file = files(package).joinpath(relative)
			schema: Optional[dict] = None
			if schema_file.is_file():
				try:
					schema = loads(schema_file.read_text())
				except JSONDecodeError as e:
					Config.warn(f"Invalid JSON in plugin schema '{package}/{relative}': {e}")
			else:
				Config._logger().debug(f"No options schema for '{package}/{relative}', skipping validation")
			self._plugin_schemas[key] = schema
		return self._plugin_schemas[key]

	def _validate_plugin_options(self, entries: list, package: str, key: str, label: str) -> None:
		"""Validate each entry's options against its plugin schema — warnings only, never fatal.

		One body for all five axes: they differ only in which config key names the plugin
		(`protocol` for connectors, `kind` for devices, `class` for algorithms, storage and
		services) and in the noun used in the warnings.
		"""
		for entry in entries:
			if not isinstance(entry, dict):
				continue
			plugin = entry.get(key)
			if not isinstance(plugin, str) or not _PLUGIN_NAME.fullmatch(plugin):
				continue
			schema = self._load_plugin_schema(package, plugin)
			if schema is None:
				continue
			validator_class = validator_for(schema)
			try:
				validator_class.check_schema(schema)
			except SchemaError as e:
				# Same translation as `_load_plugin_schema`: name the file that exists on disk,
				# not the one a literal reading of the config value would suggest.
				Config.warn(f"Invalid {label.lower()} schema '{plugin.replace(chr(46), chr(47))}.schema.json': {e.message}")
				continue
			validator = validator_class(schema)
			for error in validator.iter_errors(entry.get("options", {})):
				Config.warn(f"{label} '{entry.get('name')}' ({key} '{plugin}'): invalid options: {error.message}")

	@staticmethod
	def _interpolate(value: Any) -> Any:
		"""Recursively resolve ${VAR} / ${VAR:-default} env references in config values."""
		if isinstance(value, dict):
			return {key: Config._interpolate(item) for key, item in value.items()}
		if isinstance(value, list):
			return [Config._interpolate(item) for item in value]
		if isinstance(value, str):
			return Config._interpolate_str(value)
		return value

	@staticmethod
	def _interpolate_str(value: str) -> Optional[str]:
		if "${" not in value:
			return value
		match = _ENV_PATTERN.fullmatch(value)
		if match:
			# Whole value is a single token: allow a null result so optional fields
			# (e.g. MQTT username/password) fall back to "absent" instead of "".
			return Config._resolve_env(match.group(1), match.group(2)) or None
		# Embedded token(s) inside a larger string: always produce a string.
		return _ENV_PATTERN.sub(lambda m: Config._resolve_env(m.group(1), m.group(2)), value)

	@staticmethod
	def _resolve_env(name: str, default: Optional[str]) -> str:
		raw = environ.get(name)
		if raw:
			return raw
		if default is not None:  # ${VAR:-default}: unset or empty falls back to the default
			return default
		if raw is None:
			Config.warn(f"Environment variable '{name}' is not set")
		return ""

	def __repr__(self):
		return f"Config({self.VERSION}, {self.ENV}, {self.LOGGER_LEVEL}, {self.CONNECTORS}, {self.ALGORITHMS}, {self._resolved_devices}, {self.STORAGE}, {self.SERVICES})"

	@staticmethod
	def _logger() -> Logger:
		logger = getLogger(__class__.__name__)
		if getLogger().handlers:
			# Logging is configured (main's handler, or pytest's capture):
			# records propagate to the root handlers only. removeHandler
			# is a no-op when the bootstrap handler isn't attached.
			logger.removeHandler(_BOOTSTRAP_HANDLER)
		elif _BOOTSTRAP_HANDLER not in logger.handlers:
			logger.addHandler(_BOOTSTRAP_HANDLER)
		return logger

	@staticmethod
	def warn(message: Any) -> None:
		Config._logger().warning(message)

	@staticmethod
	def error(message: Any) -> None:
		Config._logger().error(message)
