"""A plugin published outside this repository loads by exactly the rules a shipped one does.

`CONTRIBUTING.md` tells strangers they may keep a plugin in their own repository, copy
`<axis>/<vendor>/` into a checkout, and name it `"<vendor>.<name>"` in config. That promise
is the one thing in this repository with no first-party user: every shipped plugin sits
directly under its axis, so nothing else here exercises the nested layout end to end. This
file is what keeps the promise honest.

It builds a real vendor package in `tmp_path` and drives the **real loader** through
`api.conformance.check_loads`, on the three axes a third party is most likely to publish.
The two gates it covers are the ones no schema check can see, because they are about names
and types rather than options: the class-name convention (module name + axis suffix,
underscores stripped, matched case-insensitively) and the `issubclass` check against the
axis ABC. A plugin can satisfy every schema check and still be rejected at startup by
either.

**Why `__path__` and not `sys.path`.** The axis packages are *regular* packages, so
`import connectors` resolves to this checkout's copy and stops; a vendor directory anywhere
else is unreachable no matter what `sys.path` says. An operator does not hit this, because
they physically copy the directory in. A *test* cannot, so it extends the real package's
`__path__` instead — which is the same thing the publishing recipe recommends to a plugin
author testing from their own repository.

**And the limit of that trick, pinned below**: `importlib.resources.files()` returns the
first portion only, so a schema reached this way is invisible to `Config`. Imports merge;
resources do not. That is the same trap `pkgutil.extend_path` sets, and it is why the
checks in `api/conformance.py` take a *directory* argument rather than deriving one from a
package name. Schema resolution for a dotted name is covered separately, against a real
package, by `TestANamespacedPluginIsValidated` in `tests/test_config.py`.
"""

import importlib
import json
import os
import sys

import pytest

from api import conformance
from api.algorithm import Algorithm
from api.connector import Connector
from api.device import Device
from api.testing import make_devices_access

CONNECTOR_SOURCE = """
from api.connector import Connector


class SolarConnector(Connector):
\tdef __init__(self, name, host=None):
\t\tsuper().__init__(name)
\t\tself.host = host

\tdef start(self):
\t\tpass

\tdef send(self, device, payload):
\t\tpass
"""

DEVICE_SOURCE = """
from api.device import Device


class Inverter(Device):
\tSUPPORTED_PROTOCOLS = ("mqtt",)

\tdef __init__(self, name, connector_options, listener_options, controller_options):
\t\tsuper().__init__(name, connector_options, listener_options, controller_options)

\tdef receive(self, *args, **kwargs):
\t\treturn True
"""

ALGORITHM_SOURCE = """
from api.algorithm import Algorithm


class PeakShave(Algorithm):
\tdef __init__(self, name, devices_manager, **kwargs):
\t\tsuper().__init__(name, devices_manager, **kwargs)

\tdef main(self):
\t\tsuper().main()
"""

# One schema per axis, and they are deliberately not interchangeable — which is the
# thing a single worked example cannot teach. A connector's options are flat. A device's
# top-level keys are the three option sub-dicts, because every device shares
# `Device.__init__` and its own options live *inside* them. An algorithm declares the
# three the base class reads, even though its own `__init__` names none of them, because
# the lockstep check follows `**kwargs` up the MRO.
BASE = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}

CONNECTOR_SCHEMA = {**BASE, "properties": {"host": {"type": ["string", "null"]}}, "additionalProperties": False}

DEVICE_SCHEMA = {**BASE, "properties": {
	"connector_options": {"type": "object"},
	"listener_options": {"type": "object"},
	"controller_options": {"type": "object"},
}, "additionalProperties": False}

ALGORITHM_SCHEMA = {**BASE, "properties": {
	"delay_seconds": {"type": ["number", "string", "null"]},
	"required_devices": {"type": ["array", "null"], "items": {"type": "string"}},
	"wait_for_devices_timeout": {"type": ["number", "string", "null"]},
}, "additionalProperties": False}

# axis, module, source, schema, the config key an operator uses, the axis ABC, class suffix
AXES = [
	pytest.param("connectors", "solar", CONNECTOR_SOURCE, CONNECTOR_SCHEMA, "protocol", Connector, "connector", id="connectors"),
	pytest.param("devices", "inverter", DEVICE_SOURCE, DEVICE_SCHEMA, "kind", Device, "", id="devices"),
	pytest.param("algorithms", "peak_shave", ALGORITHM_SOURCE, ALGORITHM_SCHEMA, "class", Algorithm, "", id="algorithms"),
]

VENDOR = "acme"


@pytest.fixture
def publish(tmp_path):
	"""Write `<tmp>/<axis>/<vendor>/<module>.py` and make the axis package see it.

	Undone on the way out, both halves: the appended `__path__` entry and every
	`<axis>.<vendor>` module the import left in `sys.modules`. Without the second, a later
	test importing the same dotted name would get this one's module back from cache, long
	after the directory behind it was deleted.
	"""
	appended = []

	def _publish(axis: str, module: str, source: str, schema: dict = None) -> str:
		directory = tmp_path / axis / VENDOR
		directory.mkdir(parents=True, exist_ok=True)
		(directory / "__init__.py").write_text("")
		(directory / f"{module}.py").write_text(source.replace("\\t", "\t"))
		(directory / f"{module}.schema.json").write_text(json.dumps(schema if schema is not None else BASE))
		package = importlib.import_module(axis)
		portion = str(tmp_path / axis)
		package.__path__.append(portion)
		appended.append((package, portion))
		return str(directory)

	yield _publish

	for package, portion in appended:
		if portion in package.__path__:
			package.__path__.remove(portion)
	for name in [m for m in list(sys.modules) if f".{VENDOR}." in m or m.endswith(f".{VENDOR}")]:
		del sys.modules[name]


class TestAPublishedPluginLoads:
	@pytest.mark.parametrize("axis,module,source,schema,key,base,suffix", AXES)
	def test_the_real_loader_constructs_it_from_a_vendor_directory(self, publish, axis, module, source, schema, key, base, suffix):
		"""The whole published convention, end to end, through `Main.create_classes`."""
		publish(axis, module, source, schema)
		entry = {"name": "published", key: f"{VENDOR}.{module}", "options": self._options(axis)}
		report = conformance.check_loads(
			entry, key, axis, base,
			expected_name_suffix=suffix or None,
			arguments={"devices_manager": make_devices_access()} if axis == "algorithms" else None,
		)
		assert report.ok, report.describe()
		assert report.created == ("published",)

	@pytest.mark.parametrize("axis,module,source,schema,key,base,suffix", AXES)
	def test_the_conformance_checks_reach_a_vendor_directory(self, publish, axis, module, source, schema, key, base, suffix):
		"""They take a directory, so the nesting costs them nothing — which is the point.

		And each axis's schema is a different shape, which is why this is parametrised over
		three rather than demonstrated once: a device's top-level keys are the three option
		sub-dicts, and an algorithm declares options its own `__init__` never names.
		"""
		directory = publish(axis, module, source, schema)
		assert conformance.check_schemas_are_valid(directory).ok
		lockstep = conformance.check_kwargs_lockstep(directory, f"{axis}.{VENDOR}", suffix)
		assert lockstep.checked == (module,)
		assert lockstep.ok, lockstep.describe()

	def test_a_wrongly_named_class_is_rejected_by_the_loader_alone(self, publish):
		"""The gate no schema check can see. `solar.py` must hold `SolarConnector`; a class
		named anything else is a startup rejection with a perfectly valid schema."""
		publish("connectors", "solar", CONNECTOR_SOURCE.replace("SolarConnector", "SolarPlugin"), CONNECTOR_SCHEMA)
		report = conformance.check_loads(
			{"name": "published", "protocol": f"{VENDOR}.solar", "options": {}},
			"protocol", "connectors", Connector, expected_name_suffix="connector",
		)
		assert not report.ok
		assert "no class matching" in report.describe()

	def test_a_class_on_the_wrong_axis_is_rejected_by_the_loader_alone(self, publish):
		"""The other one: `issubclass` against the axis ABC. A device module placed under
		`connectors/` imports cleanly and matches by name, and is still refused."""
		publish("connectors", "inverter", DEVICE_SOURCE, DEVICE_SCHEMA)
		report = conformance.check_loads(
			{"name": "published", "protocol": f"{VENDOR}.inverter", "options": {}},
			"protocol", "connectors", Device, expected_name_suffix="",
		)
		assert not report.ok

	@staticmethod
	def _options(axis: str) -> dict:
		if axis == "devices":
			return {
				"connector_options": {"name": "c", "protocol": "mqtt"},
				"listener_options": {},
				"controller_options": {},
			}
		return {}


class TestTheLimitOfTheTestTimeTrick:
	"""Imports merge across `__path__`; resources do not. Pinned so nobody builds on it.

	A plugin author testing from their own repository extends the axis package's `__path__`
	rather than copying the directory in. That makes the *loader* work, which is what
	`check_loads` needs — and it does **not** make `Config` find the schema, because
	`importlib.resources.files()` resolves against the package's original location only.

	An operator never meets this: they copy the directory into the checkout, so the schema
	is physically inside the package and `files()` finds it. It matters here because a test
	that relied on the trick for schema validation would pass while validating nothing, and
	because it is precisely why `api/conformance.py`'s checks take a directory argument.
	"""

	def test_the_loader_sees_the_module_but_files_does_not_see_the_schema(self, publish):
		from importlib.resources import files

		publish("connectors", "solar", CONNECTOR_SOURCE, CONNECTOR_SCHEMA)

		assert importlib.import_module("connectors.acme.solar") is not None

		probe = files("connectors").joinpath(os.path.join(VENDOR, "solar.schema.json"))
		assert not probe.is_file(), (
			"files() now follows an appended __path__ — the caveat in this module's docstring, "
			"in api/conformance.py and in the publishing recipe can be dropped"
		)
