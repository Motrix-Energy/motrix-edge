import glob
import importlib
import inspect
import json
import os

import pytest
from jsonschema import validators

from __metaclasses.singleton import Singleton
from config.config import Config
from config.enums.environment import Environment


def _write_config(tmp_path, config_dict):
    """Write a config dict to a temp file and return the path."""
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config_dict))
    return str(config_file)


def _import_plugin_module(package: str, stem: str):
    """Import `<package>/<stem>.py`, or None when only an optional dependency is missing.

    A plugin may import a library core requirements do not ship — services/rest_api.py
    imports fastapi (see requirements-api.txt), and a future Home Assistant connector
    would import websockets. On a machine without it the lockstep check simply cannot run,
    and failing would turn every contributor's suite red over a dependency that is
    optional by design.

    A schema naming a module that does not exist is a different thing entirely — it is the
    drift this whole check exists to catch — so only the first case is tolerated.
    `ModuleNotFoundError.name` is what tells them apart, and getting that comparison
    backwards would leave the test green while checking nothing: it is shared across the
    four axis classes rather than copy-pasted for exactly that reason.
    """
    try:
        return importlib.import_module(f"{package}.{stem}")
    except ModuleNotFoundError as missing:
        if missing.name == f"{package}.{stem}":
            raise
        return None


@pytest.fixture
def valid_config():
    return {
        "version": "0.0.0",
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

        assert cfg.version == "0.0.0"
        assert cfg.env == Environment.DEV
        assert len(cfg.connectors) == 1
        assert cfg.connectors[0]["name"] == "mqtt_1"
        assert len(cfg.devices) == 1
        assert cfg.devices[0]["name"] == "meter"

    def test_missing_config_file(self, tmp_path):
        cfg = Config(file_path=str(tmp_path / "nonexistent.json"))

        assert cfg.version == "0.0.0"
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

    Both rules are axis-independent, so they are stated once here rather than four times:
    a name with no schema gets no options validation (the same trust model algorithms
    have, and a typo still fails loudly later in `create_classes`), and a name that is
    not a plain module name must never reach the filesystem at all.

    The entry survives Config either way — skipping validation is not rejection.
    """

    # (axis key, discriminator, extra options an entry on this axis needs)
    AXES = [
        pytest.param("connectors", "protocol", {}, id="connectors"),
        pytest.param("devices", "kind", {"connector_options": {"name": "c"}}, id="devices"),
        pytest.param("storage", "class", {}, id="storage"),
        pytest.param("services", "class", {}, id="services"),
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
    def test_traversal_in_a_plugin_name_never_reaches_the_filesystem(self, tmp_path, caplog, axis, key, extra):
        config = self._config(axis, key, "../evil", dict(extra))
        cfg = Config(file_path=_write_config(tmp_path, config))
        assert "invalid options" not in caplog.text
        assert len(getattr(cfg, axis)) == 1


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

    One parametrized class for all four axes: the four copies differed only in the
    directory to glob and the suffix in the expected class name.
    """

    AXES = [
        pytest.param("connectors", "connector", id="connectors"),
        pytest.param("devices", "", id="devices"),
        pytest.param("storage", "backend", id="storage"),
        pytest.param("services", "service", id="services"),
    ]

    @staticmethod
    def _schema_files(package: str):
        directory = os.path.join(os.path.dirname(os.path.dirname(__file__)), package)
        paths = sorted(glob.glob(os.path.join(directory, "*.schema.json")))
        assert paths, f"no {package} schema files found"
        return paths

    @pytest.mark.parametrize("package,suffix", AXES)
    def test_every_schema_is_valid_json_schema(self, package, suffix):
        for schema_path in self._schema_files(package):
            with open(schema_path) as f:
                schema = json.load(f)
            validators.validator_for(schema).check_schema(schema)

    @pytest.mark.parametrize("package,suffix", AXES)
    def test_schema_matches_constructor_signature(self, package, suffix):
        """The kwargs contract: config options are spread into the constructor, so every
        key a schema allows (and everything it requires) must be a constructor parameter.
        This automates the "keep schema and constructor in lockstep" discipline.

        Devices are the interesting case: every device shares Device.__init__, so their
        schemas' top-level keys must be a subset of the three option sub-dicts. Services
        are the other one — `devices_manager`/`supervisor` are real constructor parameters
        injected by main, so the subset direction still holds.

        A plugin whose module needs an uninstalled optional dependency is skipped rather
        than failed; if that leaves nothing checked at all, the test reports itself as
        skipped instead of going green having verified nothing.
        """
        checked = 0
        for schema_path in self._schema_files(package):
            with open(schema_path) as f:
                schema = json.load(f)
            stem = os.path.basename(schema_path).removesuffix(".schema.json")
            module = _import_plugin_module(package, stem)
            if module is None:
                continue
            checked += 1
            expected_class_name = f"{stem}{suffix}".replace("_", "")
            cls = next(
                obj for name, obj in vars(module).items()
                if isinstance(obj, type) and name.lower() == expected_class_name
            )
            params = set(inspect.signature(cls.__init__).parameters) - {"self", "name"}
            schema_keys = set(schema.get("properties", {}))
            assert schema_keys <= params, f"{stem}: schema allows options the constructor rejects: {schema_keys - params}"
            required = set(schema.get("required", []))
            assert required <= params, f"{stem}: schema requires options the constructor lacks: {required - params}"
        if not checked:
            pytest.skip(f"every {package} module needs an optional dependency that is not installed")

    def test_no_service_schema_declares_an_injected_handle(self):
        # Services only: devices_manager/supervisor are spread by
        # create_classes(arguments=...). A schema declaring one would let a config entry
        # collide with the injected value and fail instantiation with "got multiple
        # values" — which the subset check above cannot catch, since both are genuine
        # constructor parameters.
        for schema_path in self._schema_files("services"):
            with open(schema_path) as f:
                schema = json.load(f)
            declared = set(schema.get("properties", {}))
            assert not declared & {"devices_manager", "supervisor"},                 f"{schema_path}: injected handles must not be config options"




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
        cfg = Config(file_path=_write_config(tmp_path, {"version": "0.0.0"}))
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
