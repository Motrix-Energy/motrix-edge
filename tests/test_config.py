import glob
import json
import logging
import os
import sys

import pytest

from __metaclasses.singleton import Singleton
from api import conformance
from api.algorithm import Algorithm
from api.connector import Connector
from api.device import Device
from api.service import Service
from api.storage_backend import StorageBackend
from config.config import Config
from config.enums.environment import Environment
from config.version import CONFIG_FORMAT_VERSION, Compatibility, compare, parse


# One minimal, dependency-free config entry per axis for the load check below. Each is
# the shipped plugin that needs no optional extra and no network: `pseudo` on three axes,
# and the two algorithms, which need only an injected DevicesManager.
SHIPPED_ENTRY = {
    "connectors": (
        {"name": "pseudo_1", "protocol": "pseudo", "options": {"replay_file": "nonexistent.csv"}},
        "protocol", Connector, None,
    ),
    "devices": (
        {"name": "dev_1", "kind": "pseudo", "options": {
            "connector_options": {"name": "c1", "protocol": "pseudo"},
            "listener_options": {}, "controller_options": {},
        }},
        "kind", Device, None,
    ),
    "storage": ({"name": "null_1", "class": "null", "options": {}}, "class", StorageBackend, None),
    "services": (
        {"name": "api_1", "class": "rest_api", "options": {}}, "class", Service,
        {"devices_manager": None, "supervisor": None},
    ),
    "algorithms": (
        {"name": "checker_1", "class": "device_checker", "options": {}}, "class", Algorithm,
        {"devices_manager": None},
    ),
}


def _write_config(tmp_path, config_dict):
    """Write a config dict to a temp file and return the path."""
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config_dict))
    return str(config_file)


# `_import_plugin_module` moved to api/conformance.py, where a plugin in its own
# repository can reach it. It also gained the prefix discrimination the local copy
# lacked: with a dotted package (connectors.acme.solar), a missing connectors/acme/
# raised with name == 'connectors.acme', which is not equal to the target and was
# waved through as 'optional dependency absent'.
_import_plugin_module = conformance.import_plugin_module


@pytest.fixture
def valid_config():
    return {
        "version": "1.0.0",
        "env": "dev",
        "logger_level": "debug",
        "connectors": [
            {
                "name": "mqtt_1",
                "protocol": "mqtt",
                "options": {
                    "host": "localhost",
                    "port": 1883,
                    "version": "3.1.1",
                },
            }
        ],
        "algorithms": [
            {"name": "checker", "class": "device_checker"}
        ],
        "devices": [
            {
                "name": "meter",
                "kind": "p1",
                "options": {
                    "connector_options": {"name": "mqtt_1"},
                    "listener_options": {"pattern": "p1/.*"},
                    "controller_options": {"topic": "cmd/meter"},
                },
            }
        ],
    }


class TestConfigLoading:
    def test_valid_config(self, tmp_path, valid_config):
        path = _write_config(tmp_path, valid_config)
        cfg = Config(file_path=path)

        assert cfg.version == "1.0.0"
        assert cfg.env == Environment.DEV
        assert len(cfg.connectors) == 1
        assert cfg.connectors[0]["name"] == "mqtt_1"
        assert len(cfg.devices) == 1
        assert cfg.devices[0]["name"] == "meter"

    def test_missing_config_file(self, tmp_path):
        cfg = Config(file_path=str(tmp_path / "nonexistent.json"))

        # A file that does not exist declares nothing, and absence is not a claim —
        # see config/version.py. This used to read "0.0.0", the value the deleted
        # DEFAULT_VERSION put there, which made a silent file indistinguishable from
        # one that had spelled out a version.
        assert cfg.version is None
        assert cfg.env == Environment.PROD
        assert cfg.connectors == []
        assert cfg.algorithms == []
        assert cfg.devices == []

    def test_duplicate_connector_names(self, tmp_path):
        config = {
            "connectors": [
                {"name": "dup", "protocol": "mqtt", "options": {"host": "a", "port": 1883, "version": "3.1.1"}},
                {"name": "dup", "protocol": "mqtt", "options": {"host": "b", "port": 1884, "version": "3.1.1"}},
            ]
        }
        path = _write_config(tmp_path, config)
        cfg = Config(file_path=path)

        assert len(cfg.connectors) == 1
        assert cfg.connectors[0]["options"]["host"] == "a"

    def test_duplicate_algorithm_names(self, tmp_path, caplog):
        """Two algorithms sharing a name collapse into one SimulationClock barrier
        participant, so the first ack() releases a replay step the second has not
        finished. The duplicate must be dropped at load time, not at runtime."""
        config = {
            "algorithms": [
                {"name": "dup", "class": "device_checker", "options": {"delay_seconds": 1}},
                {"name": "dup", "class": "auto_toggle", "options": {"delay_seconds": 2}},
            ]
        }
        cfg = Config(file_path=_write_config(tmp_path, config))

        assert "Algorithm name 'dup' is not unique" in caplog.text
        assert len(cfg.algorithms) == 1
        assert cfg.algorithms[0]["class"] == "device_checker"

    def test_duplicate_device_names(self, tmp_path, caplog):
        """DevicesManager and main's connector matching both key by device name, so a
        duplicate silently drops one on iteration order."""
        config = {
            "connectors": [
                {"name": "mqtt_1", "protocol": "mqtt", "options": {"host": "h", "port": 1883, "version": "3.1.1"}}
            ],
            "devices": [
                {
                    "name": "dup",
                    "kind": "p1",
                    "options": {"connector_options": {"name": "mqtt_1"}, "listener_options": {"pattern": "a/.*"}, "controller_options": {"topic": "cmd/a"}},
                },
                {
                    "name": "dup",
                    "kind": "p1",
                    "options": {"connector_options": {"name": "mqtt_1"}, "listener_options": {"pattern": "b/.*"}, "controller_options": {"topic": "cmd/b"}},
                },
            ],
        }
        cfg = Config(file_path=_write_config(tmp_path, config))

        assert "Device name 'dup' is not unique" in caplog.text
        assert len(cfg.devices) == 1
        assert cfg.devices[0]["options"]["listener_options"]["pattern"] == "a/.*"

    def test_device_missing_connector(self, tmp_path):
        config = {
            "connectors": [
                {"name": "real_conn", "protocol": "mqtt", "options": {"host": "h", "port": 1883, "version": "3.1.1"}}
            ],
            "devices": [
                {
                    "name": "orphan",
                    "kind": "p1",
                    "options": {"connector_options": {"name": "nonexistent"}, "listener_options": {}, "controller_options": {}},
                },
                {
                    "name": "valid",
                    "kind": "p1",
                    "options": {"connector_options": {"name": "real_conn"}, "listener_options": {"pattern": ".*"}, "controller_options": {"topic": "cmd/meter"}},
                },
            ],
        }
        path = _write_config(tmp_path, config)
        cfg = Config(file_path=path)

        assert len(cfg.devices) == 1
        assert cfg.devices[0]["name"] == "valid"

    def test_schema_validation_errors(self, tmp_path):
        config = {"version": "not-semver", "env": "invalid_env"}
        path = _write_config(tmp_path, config)
        # Should not raise — warns and continues
        cfg = Config(file_path=path)
        # Falls back to defaults for invalid enum
        assert cfg.env == Environment.PROD

    def test_device_protocol_resolution(self, tmp_path, valid_config):
        path = _write_config(tmp_path, valid_config)
        cfg = Config(file_path=path)

        device = cfg.devices[0]
        assert device["options"]["connector_options"]["protocol"] == "mqtt"


class TestRuntimeBlock:
    """The optional `runtime` block: shutdown grace period + restart policy."""

    def test_defaults_when_absent(self, tmp_path, valid_config):
        cfg = Config(file_path=_write_config(tmp_path, valid_config))

        assert cfg.runtime == Config.DEFAULT_RUNTIME
        assert cfg.shutdown_timeout == 10.0

    def test_missing_config_file_still_has_runtime_defaults(self, tmp_path):
        cfg = Config(file_path=str(tmp_path / "nonexistent.json"))

        assert cfg.shutdown_timeout == 10.0
        assert cfg.runtime["restart"] is True

    def test_keys_are_merged_over_defaults(self, tmp_path, valid_config):
        """Overriding one knob must not wipe out the others."""
        config = {**valid_config, "runtime": {"shutdown_timeout_seconds": 30}}
        cfg = Config(file_path=_write_config(tmp_path, config))

        assert cfg.shutdown_timeout == 30.0
        assert cfg.runtime["max_restarts"] == Config.DEFAULT_RUNTIME["max_restarts"]
        assert cfg.runtime["restart"] is True

    def test_full_override(self, tmp_path, valid_config):
        config = {**valid_config, "runtime": {
            "shutdown_timeout_seconds": 5,
            "restart": False,
            "max_restarts": 1,
            "backoff_seconds": 2,
            "max_backoff_seconds": 4,
        }}
        cfg = Config(file_path=_write_config(tmp_path, config))

        assert cfg.shutdown_timeout == 5.0
        assert cfg.runtime["restart"] is False
        assert cfg.runtime["max_restarts"] == 1

    def test_feeds_the_restart_policy(self, tmp_path, valid_config):
        from supervisor.supervisor import RestartPolicy

        config = {**valid_config, "runtime": {"restart": False, "max_restarts": 3}}
        cfg = Config(file_path=_write_config(tmp_path, config))
        policy = RestartPolicy.from_runtime(cfg.runtime)

        assert policy.enabled is False
        assert policy.max_restarts == 3
        assert policy.backoff_seconds == Config.DEFAULT_RUNTIME["backoff_seconds"]

    def test_unknown_key_warns(self, tmp_path, valid_config, caplog):
        import logging

        config = {**valid_config, "runtime": {"typo_seconds": 1}}
        with caplog.at_level(logging.WARNING):
            Config(file_path=_write_config(tmp_path, config))

        assert any("errors in config file" in str(r.message) for r in caplog.records)


class TestEnvInterpolation:
    """F-5: Config resolves ${VAR} / ${VAR:-default} references from the environment
    so secrets stay out of config.json."""

    @staticmethod
    def _config_with_host(host_value):
        return {
            "connectors": [
                {
                    "name": "m",
                    "protocol": "mqtt",
                    "options": {"host": host_value, "port": 1883, "version": "3.1.1"},
                }
            ]
        }

    def test_set_var_is_interpolated(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MQTT_HOST", "broker.example")
        path = _write_config(tmp_path, self._config_with_host("${MQTT_HOST}"))
        cfg = Config(file_path=path)
        assert cfg.connectors[0]["options"]["host"] == "broker.example"

    def test_default_form_uses_default_when_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MQTT_HOST", raising=False)
        path = _write_config(tmp_path, self._config_with_host("${MQTT_HOST:-localhost}"))
        cfg = Config(file_path=path)
        assert cfg.connectors[0]["options"]["host"] == "localhost"

    def test_set_var_overrides_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MQTT_HOST", "real.broker")
        path = _write_config(tmp_path, self._config_with_host("${MQTT_HOST:-localhost}"))
        cfg = Config(file_path=path)
        assert cfg.connectors[0]["options"]["host"] == "real.broker"

    def test_whole_value_unset_var_becomes_none(self, tmp_path, monkeypatch):
        # F-5: an unset optional credential resolves to None (not ""), so MQTT's
        # `if self.username is not None` treats it as absent.
        monkeypatch.delenv("MQTT_USERNAME", raising=False)
        config = {
            "connectors": [
                {
                    "name": "m",
                    "protocol": "mqtt",
                    "options": {"host": "h", "port": 1883, "version": "3.1.1", "username": "${MQTT_USERNAME}"},
                }
            ]
        }
        path = _write_config(tmp_path, config)
        cfg = Config(file_path=path)
        assert cfg.connectors[0]["options"]["username"] is None

    def test_whole_value_empty_env_becomes_none(self, tmp_path, monkeypatch):
        # docker-compose's `${VAR:-}` sets creds to an empty string inside the container.
        monkeypatch.setenv("MQTT_USERNAME", "")
        config = {
            "connectors": [
                {
                    "name": "m",
                    "protocol": "mqtt",
                    "options": {"host": "h", "port": 1883, "version": "3.1.1", "username": "${MQTT_USERNAME}"},
                }
            ]
        }
        path = _write_config(tmp_path, config)
        cfg = Config(file_path=path)
        assert cfg.connectors[0]["options"]["username"] is None

    def test_embedded_token_returns_string(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MQTT_HOST", "broker")
        path = _write_config(tmp_path, self._config_with_host("tcp://${MQTT_HOST}:1883"))
        cfg = Config(file_path=path)
        assert cfg.connectors[0]["options"]["host"] == "tcp://broker:1883"

    def test_embedded_missing_var_becomes_empty_and_warns(self, tmp_path, monkeypatch, caplog):
        monkeypatch.delenv("NOPE", raising=False)
        path = _write_config(tmp_path, self._config_with_host("x-${NOPE}-y"))
        cfg = Config(file_path=path)
        assert cfg.connectors[0]["options"]["host"] == "x--y"
        assert "NOPE" in caplog.text

    def test_plain_string_is_untouched(self, tmp_path, monkeypatch):
        path = _write_config(tmp_path, self._config_with_host("localhost"))
        cfg = Config(file_path=path)
        assert cfg.connectors[0]["options"]["host"] == "localhost"

    def test_interpolation_recurses_into_nested_and_lists(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEVICE_ID", "meter1")
        config = {
            "connectors": [
                {"name": "m", "protocol": "mqtt", "options": {"host": "h", "port": 1883, "version": "3.1.1"}}
            ],
            "devices": [
                {
                    "name": "meter",
                    "kind": "p1",
                    "options": {
                        "connector_options": {"name": "m"},
                        "listener_options": {"pattern": "p1/.*"},
                        "controller_options": {"topic": "cmd/${DEVICE_ID}"},
                    },
                }
            ],
        }
        path = _write_config(tmp_path, config)
        cfg = Config(file_path=path)
        assert cfg.devices[0]["options"]["controller_options"]["topic"] == "cmd/meter1"

    def test_unset_no_default_warns(self, tmp_path, monkeypatch, caplog):
        monkeypatch.delenv("MISSING", raising=False)
        path = _write_config(tmp_path, self._config_with_host("${MISSING}"))
        Config(file_path=path)
        assert "MISSING" in caplog.text


class TestControllerOptionsSchema:
    """Regression: the p1 schema documents the control key as 'topic' (what
    MQTTConnector.send reads), not the legacy 'controller', and it is optional."""

    @staticmethod
    def _schema_errors(controller_options):
        import os
        from jsonschema import validators

        schema_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.schema.json")
        with open(schema_path) as f:
            schema = json.load(f)
        validator = validators.validator_for(schema)(schema)
        config = {
            "connectors": [
                {"name": "m", "protocol": "mqtt", "options": {"host": "h", "port": 1883, "version": "3.1.1"}}
            ],
            "devices": [
                {
                    "name": "d",
                    "kind": "p1",
                    "options": {
                        "connector_options": {"name": "m"},
                        "listener_options": {"pattern": "p1/.*"},
                        "controller_options": controller_options,
                    },
                }
            ],
        }
        return list(validator.iter_errors(config))

    def test_topic_key_is_accepted(self):
        assert self._schema_errors({"topic": "cmd/d"}) == []

    def test_controller_options_is_optional(self):
        assert self._schema_errors({}) == []


class TestUnresolvablePluginNames:
    """What every axis does with a plugin name it cannot turn into a schema file.

    Both rules are axis-independent, so they are stated once here rather than five times:
    a name with no schema file gets no options validation (a typo still fails loudly
    later in `create_classes`), and a name that is not a dotted chain of plain module
    names must never reach the filesystem at all.

    All five axes now, including algorithms — which used to be the example of the first
    rule, being the one axis that shipped no schemas at all.

    The entry survives Config either way — skipping validation is not rejection.
    """

    # (axis key, discriminator, extra options an entry on this axis needs)
    AXES = [
        pytest.param("connectors", "protocol", {}, id="connectors"),
        pytest.param("devices", "kind", {"connector_options": {"name": "c"}}, id="devices"),
        pytest.param("storage", "class", {}, id="storage"),
        pytest.param("services", "class", {}, id="services"),
        pytest.param("algorithms", "class", {}, id="algorithms"),
    ]

    @staticmethod
    def _config(axis: str, key: str, plugin: str, options: dict):
        entry = {"name": "x", key: plugin, "options": options}
        config = {axis: [entry]}
        if axis == "devices":
            # a matching connector keeps Config's cross-check quiet, so the device
            # survives to be counted; the options pass runs independently of it
            config["connectors"] = [{"name": "c", "protocol": "pseudo", "options": {"replay_file": "r.csv"}}]
        return config

    @pytest.mark.parametrize("axis,key,extra", AXES)
    def test_unknown_plugin_name_skips_validation_and_survives(self, tmp_path, caplog, axis, key, extra):
        config = self._config(axis, key, "no_such_plugin", {**extra, "anything": True})
        cfg = Config(file_path=_write_config(tmp_path, config))
        assert "invalid options" not in caplog.text
        assert len(getattr(cfg, axis)) == 1

    @pytest.mark.parametrize("axis,key,extra", AXES)
    @pytest.mark.parametrize("hostile", ["../evil", "a/b", "/abs", ".hidden", "a..b", "a.", ".a"])
    def test_traversal_in_a_plugin_name_never_reaches_the_filesystem(self, tmp_path, caplog, axis, key, extra, hostile):
        """`files(package).joinpath()` performs no containment check, so the regex is the
        only guard there is. Widening it to allow the dot in `vendor.name` widened exactly
        one thing: every shape below must still be refused before it becomes a path."""
        config = self._config(axis, key, hostile, dict(extra))
        cfg = Config(file_path=_write_config(tmp_path, config))
        assert "invalid options" not in caplog.text
        assert len(getattr(cfg, axis)) == 1

    @pytest.mark.parametrize("axis,key,extra", AXES)
    def test_a_namespaced_name_with_no_schema_is_silent(self, tmp_path, caplog, axis, key, extra):
        """A dotted name is a legal plugin name now, not a traversal attempt. With no
        schema file behind it the entry is simply unvalidated, exactly as an unknown plain
        name is — not warned about, and certainly not rejected."""
        config = self._config(axis, key, "acme.solar", {**extra, "anything": True})
        cfg = Config(file_path=_write_config(tmp_path, config))
        assert "invalid options" not in caplog.text
        assert len(getattr(cfg, axis)) == 1


class TestANamespacedPluginIsValidated:
    """The two-line fix, end to end: a dotted name resolves to a nested schema file.

    Both halves or neither. Widening `_PLUGIN_NAME` alone lets `acme.solar` through the
    guard and then looks for a file literally named `acme.solar.schema.json`, which no
    layout produces — turning a silent skip into a silent *miss*, where the entry looks
    validated and never is. This is the test that would have caught that.

    Driven through a temporary importable package rather than a vendor directory inside
    `connectors/`, so the repository is never written to: `_validate_plugin_options` takes
    the package name as an argument, and `importlib.resources.files` resolves it the same
    way whatever it is called.
    """

    SCHEMA = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"host": {"type": "string"}},
        "additionalProperties": False,
    }

    @pytest.fixture
    def vendor_package(self, tmp_path):
        package = tmp_path / "vendorhost"
        (package / "acme").mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "acme" / "__init__.py").write_text("")
        (package / "acme" / "solar.schema.json").write_text(json.dumps(self.SCHEMA))
        sys.path.insert(0, str(tmp_path))
        try:
            yield "vendorhost"
        finally:
            sys.path.remove(str(tmp_path))
            for name in [m for m in sys.modules if m == "vendorhost" or m.startswith("vendorhost.")]:
                del sys.modules[name]

    @staticmethod
    def _config(tmp_path):
        return Config(file_path=_write_config(tmp_path, {"connectors": []}))

    def test_a_dotted_name_finds_its_nested_schema_and_warns_about_a_bad_option(self, tmp_path, vendor_package, caplog):
        cfg = self._config(tmp_path)
        entry = {"name": "roof", "protocol": "acme.solar", "options": {"nonsense": 1}}
        with caplog.at_level(logging.WARNING):
            cfg._validate_plugin_options([entry], vendor_package, "protocol", "Connector")
        assert "Connector 'roof' (protocol 'acme.solar'): invalid options" in caplog.text
        assert "nonsense" in caplog.text

    def test_a_dotted_name_with_valid_options_is_silent(self, tmp_path, vendor_package, caplog):
        cfg = self._config(tmp_path)
        entry = {"name": "roof", "protocol": "acme.solar", "options": {"host": "10.0.0.7"}}
        with caplog.at_level(logging.WARNING):
            cfg._validate_plugin_options([entry], vendor_package, "protocol", "Connector")
        assert "invalid options" not in caplog.text

    def test_the_literal_dotted_filename_is_never_looked_for(self, tmp_path, vendor_package):
        """`acme.solar` means acme/solar.schema.json. A file actually named
        acme.solar.schema.json is not the contract and must not resolve, or the two layouts
        would both half-work and an author could not tell which one they were relying on."""
        cfg = self._config(tmp_path)
        assert cfg._load_plugin_schema(vendor_package, "acme.solar") == self.SCHEMA
        assert cfg._load_plugin_schema(vendor_package, "acme.missing") is None


class TestConnectorPluginSchemas:
    """Connector options are validated against connectors/<protocol>.schema.json
    (shipped next to each connector module) instead of a central oneOf — adding
    a connector never touches config.schema.json."""

    @staticmethod
    def _config_with_connector(connector):
        return {"connectors": [connector]}

    def test_valid_options_no_warning(self, tmp_path, caplog):
        config = self._config_with_connector(
            {"name": "m", "protocol": "mqtt", "options": {"host": "h", "port": 1883, "version": "3.1.1"}}
        )
        Config(file_path=_write_config(tmp_path, config))
        assert "invalid options" not in caplog.text

    def test_missing_required_option_warns_with_name_and_protocol(self, tmp_path, caplog):
        config = self._config_with_connector(
            {"name": "m", "protocol": "mqtt", "options": {"port": 1883, "version": "3.1.1"}}
        )
        Config(file_path=_write_config(tmp_path, config))
        assert "Connector 'm' (protocol 'mqtt'): invalid options" in caplog.text
        assert "host" in caplog.text

    def test_missing_options_block_warns(self, tmp_path, caplog):
        # An absent options block validates as {} and fails the schema's required
        # list — preserving the old central schema's `required: ["options"]`.
        config = self._config_with_connector({"name": "m", "protocol": "mqtt"})
        Config(file_path=_write_config(tmp_path, config))
        assert "Connector 'm' (protocol 'mqtt'): invalid options" in caplog.text

    def test_unknown_option_key_warns(self, tmp_path, caplog):
        config = self._config_with_connector(
            {"name": "p", "protocol": "pseudo", "options": {"replay_file": "r.csv", "warp_speed": 9}}
        )
        Config(file_path=_write_config(tmp_path, config))
        assert "Connector 'p' (protocol 'pseudo'): invalid options" in caplog.text
        assert "warp_speed" in caplog.text


    def test_env_template_passes_validation(self, tmp_path, monkeypatch, caplog):
        # Plugin validation runs on the raw config, before ${VAR} interpolation —
        # the template is a valid string, so no spurious type warning.
        monkeypatch.delenv("MQTT_HOST", raising=False)
        config = self._config_with_connector(
            {"name": "m", "protocol": "mqtt", "options": {"host": "${MQTT_HOST:-localhost}", "port": 1883, "version": "3.1.1"}}
        )
        Config(file_path=_write_config(tmp_path, config))
        assert "invalid options" not in caplog.text

    def test_schema_loaded_once_per_protocol(self, tmp_path):
        config = {
            "connectors": [
                {"name": "a", "protocol": "mqtt", "options": {"host": "h", "port": 1883, "version": "3.1.1"}},
                {"name": "b", "protocol": "mqtt", "options": {"host": "h", "port": 1884, "version": "3.1.1"}},
            ]
        }
        cfg = Config(file_path=_write_config(tmp_path, config))
        assert list(cfg._plugin_schemas.keys()) == [("connectors", "mqtt")]


class TestDevicePluginSchemas:
    """Device options are validated against devices/<kind>.schema.json (shipped
    next to each device module) instead of a central oneOf — adding a device
    never touches config.schema.json."""

    @staticmethod
    def _config_with_device(device):
        # a matching connector keeps Config's connector cross-check quiet; the
        # device-options validation pass runs independently of it
        return {
            "connectors": [{"name": "c", "protocol": "pseudo", "options": {"replay_file": "r.csv"}}],
            "devices": [device],
        }

    def test_valid_options_no_warning(self, tmp_path, caplog):
        config = self._config_with_device(
            {"name": "d", "kind": "p1", "options": {"connector_options": {"name": "c"}, "listener_options": {"pattern": "p1/+"}}}
        )
        Config(file_path=_write_config(tmp_path, config))
        assert "invalid options" not in caplog.text

    def test_missing_required_option_warns_with_name_and_kind(self, tmp_path, caplog):
        # p1 requires listener_options.pattern
        config = self._config_with_device(
            {"name": "d", "kind": "p1", "options": {"connector_options": {"name": "c"}, "listener_options": {}}}
        )
        Config(file_path=_write_config(tmp_path, config))
        assert "Device 'd' (kind 'p1'): invalid options" in caplog.text
        assert "pattern" in caplog.text

    def test_unknown_option_key_warns(self, tmp_path, caplog):
        config = self._config_with_device(
            {"name": "d", "kind": "p1", "options": {"connector_options": {"name": "c"}, "listener_options": {"pattern": "p1/+"}, "bogus": 1}}
        )
        Config(file_path=_write_config(tmp_path, config))
        assert "Device 'd' (kind 'p1'): invalid options" in caplog.text
        assert "bogus" in caplog.text



class TestShippedSchemas:
    """Contract tests over the shipped */*.schema.json files themselves.

    A thin caller over `api/conformance.py`, deliberately. The checks live there so a
    plugin in its own repository can run them, and this class is what keeps the two from
    drifting: if the kit breaks, this repository's pytest goes red.

    The axis list is the parametrisation; everything else is one call and one assertion.
    """

    ROOT = os.path.dirname(os.path.dirname(__file__))

    AXES = [
        pytest.param("connectors", "connector", id="connectors"),
        pytest.param("devices", "", id="devices"),
        pytest.param("storage", "backend", id="storage"),
        pytest.param("services", "service", id="services"),
        pytest.param("algorithms", "", id="algorithms"),
    ]

    @classmethod
    def _directory(cls, package: str) -> str:
        return conformance.axis_directory(cls.ROOT, package)

    @pytest.mark.parametrize("package,suffix", AXES)
    def test_every_schema_is_valid_json_schema(self, package, suffix):
        report = conformance.check_schemas_are_valid(self._directory(package))
        assert report.checked, f"no {package} schema files found"
        assert report.ok, report.describe()

    @pytest.mark.parametrize("package,suffix", AXES)
    def test_schema_matches_constructor_signature(self, package, suffix):
        """The kwargs contract, in both directions — see `check_kwargs_lockstep`.

        A plugin whose module needs an uninstalled optional dependency is skipped rather
        than failed. If that leaves nothing checked, the test says so rather than going
        green having verified nothing — and it distinguishes that from an empty or
        misnamed directory, which is a broken test rather than an absent extra.
        """
        report = conformance.check_kwargs_lockstep(self._directory(package), package, suffix)
        assert report.ok, report.describe()
        assert report.checked or report.skipped, f"no {package} schema files found"
        if not report.checked:
            pytest.skip(f"every {package} module needs an optional dependency that is not installed")

    @pytest.mark.parametrize("package,suffix", AXES)
    def test_no_schema_declares_an_injected_handle(self, package, suffix):
        """`devices_manager` and `supervisor` are spread by create_classes(arguments=...).

        A schema declaring one lets a config entry collide with the injected value and fail
        instantiation with "got multiple values" — which the subset check cannot catch,
        since both are genuine constructor parameters. Parametrised over every axis now,
        not services only: algorithms receive `devices_manager` too, and the map in
        `api/conformance.py` is per-axis so a connector parameter that happened to be
        named `supervisor` is not quietly excused.
        """
        report = conformance.check_injected_handles_absent(self._directory(package), package)
        assert report.checked, f"no {package} schema files found"
        assert report.ok, report.describe()

    @pytest.mark.parametrize("package,suffix", AXES)
    def test_one_entry_of_every_axis_actually_loads(self, package, suffix):
        """The naming rule and the issubclass gate had no test at all.

        A plugin can satisfy every schema check here and still be rejected at startup,
        because `create_classes` matches the class by name and then gates on the axis ABC.
        This drives the real loader, which is also what proves `conformance.expected_class`
        still agrees with `main`.
        """
        entry, class_key, base, arguments = SHIPPED_ENTRY[package]
        report = conformance.check_loads(
            entry, class_key, package, base,
            expected_name_suffix=suffix or None, arguments=arguments,
        )
        assert report.ok, report.describe()
        assert report.created == (entry["name"],)


class TestStoragePluginSchemas:
    """Storage options are validated against storage/<class>.schema.json (shipped
    next to each backend module) instead of a central enum + oneOf — adding a
    storage backend never touches config.schema.json."""

    @staticmethod
    def _config_with_storage(storage):
        return {"storage": [storage]}

    def test_valid_options_no_warning(self, tmp_path, caplog):
        config = self._config_with_storage(
            {"name": "s", "class": "csv_file", "options": {"output_dir": "out"}}
        )
        Config(file_path=_write_config(tmp_path, config))
        assert "invalid options" not in caplog.text

    def test_null_backend_no_options_no_warning(self, tmp_path, caplog):
        # null takes no options; its schema is the no-options stub.
        config = self._config_with_storage({"name": "s", "class": "null"})
        cfg = Config(file_path=_write_config(tmp_path, config))
        assert "invalid options" not in caplog.text
        assert len(cfg.storage) == 1

    def test_unknown_option_key_warns(self, tmp_path, caplog):
        config = self._config_with_storage(
            {"name": "s", "class": "csv_file", "options": {"output_dir": "out", "bogus": 1}}
        )
        Config(file_path=_write_config(tmp_path, config))
        assert "Storage 's' (class 'csv_file'): invalid options" in caplog.text
        assert "bogus" in caplog.text


    def test_schema_loaded_once_per_class(self, tmp_path):
        config = {
            "storage": [
                {"name": "a", "class": "csv_file", "options": {"output_dir": "a"}},
                {"name": "b", "class": "csv_file", "options": {"output_dir": "b"}},
            ]
        }
        cfg = Config(file_path=_write_config(tmp_path, config))
        assert list(cfg._plugin_schemas.keys()) == [("storage", "csv_file")]




class TestServicePluginSchemas:
    """Service options are validated against services/<class>.schema.json (shipped next
    to each service module) instead of a central enum + oneOf — adding a service never
    touches config.schema.json."""

    @staticmethod
    def _config_with_service(service):
        return {"services": [service]}

    def test_valid_options_no_warning(self, tmp_path, caplog):
        config = self._config_with_service(
            {"name": "api", "class": "rest_api", "options": {"host": "127.0.0.1", "port": 8000}}
        )
        Config(file_path=_write_config(tmp_path, config))
        assert "invalid options" not in caplog.text

    def test_env_templates_validate_before_interpolation(self, tmp_path, caplog):
        # Every scalar also accepts "string" and "null" precisely so a raw ${VAR}
        # template passes validation, which runs before interpolation resolves it.
        config = self._config_with_service(
            {"name": "api", "class": "rest_api",
             "options": {"host": "${MOTRIX_API_HOST:-0.0.0.0}", "port": "${MOTRIX_API_PORT:-8000}"}}
        )
        Config(file_path=_write_config(tmp_path, config))
        assert "invalid options" not in caplog.text

    def test_unknown_option_key_warns(self, tmp_path, caplog):
        config = self._config_with_service(
            {"name": "api", "class": "rest_api", "options": {"port": 8000, "bogus": 1}}
        )
        Config(file_path=_write_config(tmp_path, config))
        assert "Service 'api' (class 'rest_api'): invalid options" in caplog.text
        assert "bogus" in caplog.text

    def test_duplicate_service_name_is_dropped(self, tmp_path, caplog):
        config = {"services": [
            {"name": "api", "class": "rest_api", "options": {"port": 8000}},
            {"name": "api", "class": "rest_api", "options": {"port": 8001}},
        ]}
        cfg = Config(file_path=_write_config(tmp_path, config))
        assert "Service name 'api' is not unique" in caplog.text
        assert len(cfg.services) == 1

    def test_schema_loaded_once_per_class(self, tmp_path):
        config = {"services": [
            {"name": "a", "class": "rest_api", "options": {"port": 8000}},
            {"name": "b", "class": "rest_api", "options": {"port": 8001}},
        ]}
        cfg = Config(file_path=_write_config(tmp_path, config))
        assert list(cfg._plugin_schemas.keys()) == [("services", "rest_api")]

    def test_services_default_to_empty(self, tmp_path):
        cfg = Config(file_path=_write_config(tmp_path, {"version": "1.0.0"}))
        assert cfg.services == []




class TestEmulatedProtocol:
    """A replay connector can stand in for the transport it replaces, so real device
    classes — which dispatch on `protocol` — can be driven from a replay file."""

    @staticmethod
    def _config(connector_options: dict) -> dict:
        return {
            "connectors": [{"name": "replay", "protocol": "pseudo", "options": connector_options}],
            "devices": [{
                "name": "meter", "kind": "p1",
                "options": {"connector_options": {"name": "replay"},
                            "listener_options": {"pattern": "p1/+"}, "controller_options": {}},
            }],
        }

    def _protocol(self, tmp_path, connector_options: dict) -> str:
        cfg = Config(file_path=_write_config(tmp_path, self._config(connector_options)))
        return cfg.devices[0]["options"]["connector_options"]["protocol"]

    def test_emulates_overrides_the_injected_protocol(self, tmp_path):
        assert self._protocol(tmp_path, {"replay_file": "r.csv", "emulates": "mqtt"}) == "mqtt"

    def test_without_emulates_the_connectors_own_protocol_is_used(self, tmp_path):
        assert self._protocol(tmp_path, {"replay_file": "r.csv"}) == "pseudo"

    def test_empty_emulates_falls_back(self, tmp_path):
        # A "${VAR}" that resolved to nothing must not blank out the protocol.
        assert self._protocol(tmp_path, {"replay_file": "r.csv", "emulates": None}) == "pseudo"

    def test_emulates_is_accepted_by_the_pseudo_schema(self, tmp_path, caplog):
        Config(file_path=_write_config(tmp_path, self._config({"replay_file": "r.csv", "emulates": "mqtt"})))
        assert "invalid options" not in caplog.text

    def test_emulates_is_rejected_for_other_protocols(self, tmp_path, caplog):
        # It only means something for a connector that substitutes for a transport.
        config = {"connectors": [{"name": "m", "protocol": "mqtt", "options": {
            "host": "h", "port": 1883, "version": "3.1.1", "emulates": "lora"}}]}
        Config(file_path=_write_config(tmp_path, config))
        assert "Connector 'm' (protocol 'mqtt'): invalid options" in caplog.text


class TestShippedExampleConfigs:
    """Every config committed under examples/ must validate against the shipped schemas.

    These are the files a stranger copies, so a stale option name in one is a worse first
    impression than a bug — and the plugin schemas are warnings-only at load time, which
    means nothing else would ever catch it. `examples/connectors/*` in particular cannot be
    run in CI (each needs hardware on the other end), so this is the only thing standing
    between them and silent rot.
    """

    EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples")

    @staticmethod
    def _config_files():
        paths = sorted(glob.glob(os.path.join(TestShippedExampleConfigs.EXAMPLES, "**", "*.json"), recursive=True))
        # MANIFEST.json is checksum bookkeeping, not a config.
        paths = [p for p in paths if os.path.basename(p) != "MANIFEST.json"]
        assert paths, "no example configs found"
        return paths

    def test_every_example_config_loads_without_option_warnings(self, caplog):
        for path in self._config_files():
            Singleton._instances.clear()
            caplog.clear()
            Config(file_path=path)
            offending = [r.message for r in caplog.records if "invalid options" in str(r.message)]
            assert not offending, f"{os.path.relpath(path, self.EXAMPLES)}: {offending}"

    def test_every_example_device_names_a_declared_connector(self, caplog):
        for path in self._config_files():
            Singleton._instances.clear()
            caplog.clear()
            config = Config(file_path=path)
            missing = [r.message for r in caplog.records if "not found for device" in str(r.message)]
            assert not missing, f"{os.path.relpath(path, self.EXAMPLES)}: {missing}"
            assert len(config.devices) == len(config.DEVICES), "a device was dropped during wiring"


class TestConfigFormatVersion:
    """`config.json`'s `version`, which declares the format the document was written against.

    Two tiers, the way tests/test_storage_contract.py and tests/test_storage_csv.py split:
    the pure verdict first, then what an operator actually sees through `Config`. The pure
    tier is what keeps the migration seam honest — `config/version.py` computes a verdict
    and logs nothing, so a future migrator can branch on the same value.
    """

    # --- the pure function -------------------------------------------------------------

    @pytest.mark.parametrize("text,expected", [
        ("1.0.0", (1, 0, 0)),
        ("10.20.30", (10, 20, 30)),
        ("01.0.0", (1, 0, 0)),  # the EMS pattern accepts leading zeros, so this does too
    ])
    def test_parse_reads_three_integer_components(self, text, expected):
        assert parse(text) == expected

    @pytest.mark.parametrize("value", [
        "1.0", "1.0.0.0", "v1.0.0", "1.0.0-rc1", "1.0.0+build", " 1.0.0", "1.0.0 ",
        "", "not-semver", "${EMS_CFG_VER}", None, 1.0, {}, [],
        # Non-ASCII decimal digits. Python's \d matches these and ECMA-262's does not, so
        # accepting them would put this parser outside both config.schema.json (a JSON
        # Schema pattern, therefore ECMA-262) and the viewer reading the same bytes.
        "٢.٠.٠", "２.0.0",
    ])
    def test_parse_refuses_anything_else_without_raising(self, value):
        assert parse(value) is None

    def test_parse_refuses_a_component_too_long_to_be_an_int(self):
        # config.schema.json bounds nothing but the shape, so a 4301-digit component is
        # schema-valid — and int() raises ValueError above sys.get_int_max_str_digits().
        # A raise out of Config.__init__ has no shutdown path (Main.__init__ runs before
        # main()'s try/finally), so the parser's own pattern is what prevents it.
        assert parse("1" * 4400 + ".0.0") is None

    def test_the_build_constant_is_readable_by_its_own_grammar(self):
        assert parse(CONFIG_FORMAT_VERSION) is not None

    @pytest.mark.parametrize("declared,expected", [
        (None, Compatibility.UNDECLARED),
        ("", Compatibility.UNDECLARED),
        ("1.0.0", Compatibility.COMPATIBLE),
        ("1.0.9", Compatibility.COMPATIBLE),  # patch, either direction
        ("1.0", Compatibility.UNREADABLE),
        (1.0, Compatibility.UNREADABLE),  # present, just not a string
        ("2.0.0", Compatibility.INCOMPATIBLE_NEWER),
        ("0.9.0", Compatibility.INCOMPATIBLE_OLDER),
        ("1.9.0", Compatibility.FORWARD_MINOR),
    ])
    def test_compare_returns_a_verdict_and_logs_nothing(self, declared, expected, caplog):
        with caplog.at_level("DEBUG"):
            assert compare(declared) is expected
        assert caplog.records == [], "the verdict is pure; the message belongs to Config"

    # --- what an operator sees ---------------------------------------------------------

    @staticmethod
    def _version_records(caplog):
        """Only the records this feature emits, not the schema's complaint about the same key."""
        return [str(r.message) for r in caplog.records if str(r.message).startswith("Configuration version")]

    @pytest.mark.parametrize("config", [
        {},                    # no key at all
        {"version": None},     # explicit null
        {"version": ""},       # empty string, which topology.ts also folds in with absence
        {"version": "1.0.0"},  # exactly this build's format
        {"version": "1.0.7"},  # a patch difference
        {"version": "1.0.0", "env": "dev"},
    ])
    def test_a_file_this_build_understands_is_never_mentioned(self, tmp_path, caplog, config):
        with caplog.at_level("DEBUG"):
            Config(file_path=_write_config(tmp_path, config))
        assert self._version_records(caplog) == []

    def test_an_absent_version_reads_as_none_rather_than_a_default(self, tmp_path):
        # The bug this replaced: DEFAULT_VERSION was both "assumed when absent" and
        # "compared against", so a file that said nothing claimed 0.0.0 and passed while a
        # file that declared its format honestly was warned about.
        assert Config(file_path=_write_config(tmp_path, {"env": "dev"})).version is None

    def test_an_empty_version_reads_as_none_so_both_repositories_agree(self, tmp_path):
        # motrix-edge-view's stringOrNull is `typeof value === 'string' && value !== ''`,
        # so the viewer reports null for this file. Config must report the same.
        assert Config(file_path=_write_config(tmp_path, {"version": ""})).version is None

    def test_a_newer_minor_warns_and_says_what_gets_ignored(self, tmp_path, caplog):
        with caplog.at_level("DEBUG"):
            Config(file_path=_write_config(tmp_path, {"version": "1.9.0"}))
        records = self._version_records(caplog)
        assert len(records) == 1
        assert "ignored without comment" in records[0]
        assert "Upgrade Motrix Edge" in records[0], "a message that names no action buys nothing"

    @pytest.mark.parametrize("declared", ["0.9.0", "2.0.0"])
    def test_a_major_mismatch_is_an_error_naming_both_versions_and_an_action(self, tmp_path, caplog, declared):
        with caplog.at_level("DEBUG"):
            Config(file_path=_write_config(tmp_path, {"version": declared}))
        errors = [r for r in caplog.records if r.levelname == "ERROR" and str(r.message).startswith("Configuration version")]
        assert len(errors) == 1
        message = str(errors[0].message)
        assert declared in message and CONFIG_FORMAT_VERSION in message
        assert "Upgrade Motrix Edge" in message or "Rewrite it" in message

    def test_a_major_mismatch_never_raises_and_the_rest_of_the_config_still_loads(self, tmp_path, valid_config):
        # Config is built in Main.__init__, before main()'s try/finally, so an incompatible
        # document cannot be fatal — there is no shutdown path to raise into.
        cfg = Config(file_path=_write_config(tmp_path, {**valid_config, "version": "9.0.0"}))
        assert len(cfg.connectors) == 1
        assert len(cfg.devices) == 1

    def test_an_unreadable_version_warns_once_and_makes_no_comparison(self, tmp_path, caplog):
        with caplog.at_level("DEBUG"):
            Config(file_path=_write_config(tmp_path, {"version": "not-semver"}))
        records = self._version_records(caplog)
        assert len(records) == 1
        assert "was not checked" in records[0]

    def test_a_template_is_named_as_a_template_rather_than_silently_unread(self, tmp_path, caplog, monkeypatch):
        # `version` is read from the raw config, before _interpolate(), so this stays the
        # literal template even with the variable set — a document's format is a property
        # of the document, not of the machine reading it.
        monkeypatch.setenv("EMS_CFG_VER", "2.0.0")
        with caplog.at_level("DEBUG"):
            cfg = Config(file_path=_write_config(tmp_path, {"version": "${EMS_CFG_VER}"}))
        assert cfg.version == "${EMS_CFG_VER}"
        records = self._version_records(caplog)
        assert len(records) == 1
        assert "${" in records[0]
        assert not [r for r in caplog.records if r.levelname == "ERROR" and str(r.message).startswith("Configuration version")], (
            "reading the raw value must not let an environment variable decide the verdict"
        )

    # --- lockstep ----------------------------------------------------------------------

    def test_the_schema_comment_names_the_version_this_build_understands(self):
        # A bump that moves the constant and forgets the schema prose fails here rather
        # than at an operator's desk. Same role as
        # tests/test_storage_contract.py::TestGoldenFixture::test_version_lockstep.
        schema_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.schema.json")
        with open(schema_path, encoding="utf-8") as handle:
            schema = json.load(handle)
        comment = schema["properties"]["version"]["$comment"]
        assert f'"{CONFIG_FORMAT_VERSION}"' in comment, (
            f"config.schema.json's version.$comment does not name {CONFIG_FORMAT_VERSION}"
        )

    def test_no_shipped_example_declares_a_version_this_build_complains_about(self, caplog):
        # The regression test for the defect itself. examples/auto_toggle/config.json has
        # been tripping the old check on every quickstart, and the sibling tests in
        # TestShippedExampleConfigs filter for "invalid options" and "not found for device",
        # so neither of them ever noticed.
        for path in TestShippedExampleConfigs._config_files():
            Singleton._instances.clear()
            caplog.clear()
            with caplog.at_level("DEBUG"):
                Config(file_path=path)
            offending = self._version_records(caplog)
            assert not offending, f"{os.path.basename(path)}: {offending}"


class TestConfigReadsUtf8WhateverTheLocale:
    """config.json, config.schema.json and every plugin schema are decoded as UTF-8.

    They used to be opened with the platform default, which is the locale's codec: cp1252
    on Windows. There every non-ASCII byte of a device name, topic or entity id decoded as
    mojibake, silently — and that name is what storage then records the device under — while
    a byte cp1252 leaves undefined (the second byte of "č" is 0x8D) crashed the plugin-schema
    load at startup.

    Linux CI runs a UTF-8 locale, where the old code passed by luck. So each test here puts
    the Windows default back in place for the duration — an `encoding` the caller leaves out
    becomes cp1252 — which makes a missing `encoding="utf-8"` fail on every platform.
    """

    NAME = "Pompe à chaleur — étage 1"
    CONNECTOR = "Chaudière"

    @pytest.fixture
    def cp1252_default(self, monkeypatch):
        """Open files the way a Windows locale does when no encoding is given."""
        import builtins
        import pathlib

        import config.config as config_module

        real_open = builtins.open
        real_read_text = pathlib.Path.read_text

        def locale_open(file, mode="r", buffering=-1, encoding=None, *args, **kwargs):
            if "b" not in mode and encoding is None:
                encoding = "cp1252"
            return real_open(file, mode, buffering, encoding, *args, **kwargs)

        def locale_read_text(self, encoding=None, *args, **kwargs):
            return real_read_text(self, "cp1252" if encoding is None else encoding, *args, **kwargs)

        # A module global shadows the builtin for config/config.py alone, so pytest's own
        # file handling is untouched.
        monkeypatch.setattr(config_module, "open", locale_open, raising=False)
        monkeypatch.setattr(pathlib.Path, "read_text", locale_read_text)

    def test_a_non_ascii_device_and_connector_name_survive(self, tmp_path, cp1252_default):
        config = {
            "connectors": [{"name": self.CONNECTOR, "protocol": "pseudo", "options": {"replay_file": "nonexistent.csv"}}],
            "devices": [{"name": self.NAME, "kind": "pseudo", "options": {"connector_options": {"name": self.CONNECTOR}}}],
        }
        path = tmp_path / "config.json"
        path.write_bytes(json.dumps(config, ensure_ascii=False).encode("utf-8"))
        cfg = Config(file_path=str(path))
        assert [device["name"] for device in cfg.devices] == [self.NAME]
        assert [connector["name"] for connector in cfg.connectors] == [self.CONNECTOR]

    def test_a_plugin_schema_with_a_byte_cp1252_cannot_decode_loads(self, tmp_path, cp1252_default):
        comment = "Options du capteur de la chaudière — čerpadlo"
        package = tmp_path / "utf8plugins"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "sensor.schema.json").write_bytes(
            json.dumps({"$comment": comment, "type": "object"}, ensure_ascii=False).encode("utf-8")
        )
        sys.path.insert(0, str(tmp_path))
        try:
            cfg = Config(file_path=_write_config(tmp_path, {"connectors": []}))
            assert cfg._load_plugin_schema("utf8plugins", "sensor")["$comment"] == comment
        finally:
            sys.path.remove(str(tmp_path))
            sys.modules.pop("utf8plugins", None)
