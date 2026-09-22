import logging
import sys
import textwrap
from unittest.mock import patch

import pytest

from api.algorithm import Algorithm
from api.connector import Connector
from api.device import Device
from api.service import Service
from devices_manager.devices_manager import DevicesManager
from main import Main
from simulation.clock import SimulationClock
from supervisor.supervisor import RestartPolicy, Supervisor


@pytest.fixture
def app(main_app):
    """Main wired to a stubbed Config — see `main_app` in conftest."""
    return main_app


class TestCreateClasses:
    def test_loads_pseudo_connector(self, app):
        config_list = [
            {
                "name": "pseudo_1",
                "protocol": "pseudo",
                "options": {
                    "replay_file": "nonexistent.csv",
                },
            }
        ]
        result = app.create_classes(
            config_list, "protocol", "connectors", Connector,
            expected_name_suffix="connector",
        )
        assert len(result) == 1
        obj = next(iter(result))
        assert obj.name == "pseudo_1"

    def test_plugins_are_returned_in_config_order(self, app):
        """create_classes used to return a set, so instantiation order was id-ordered and
        varied between runs of the same config — and with it the order devices reach
        DevicesManager, the order an algorithm iterates self.devices.values(), and the
        order two decisions land in storage within one timestep."""
        config_list = [
            {
                "name": f"dev_{i}",
                "kind": "pseudo",
                "options": {
                    "connector_options": {"name": "c1", "protocol": "pseudo"},
                    "listener_options": {},
                    "controller_options": {},
                },
            }
            for i in range(12)  # enough entries that a set would reorder them
        ]

        result = app.create_classes(config_list, "kind", "devices", Device)

        assert [obj.name for obj in result] == [entry["name"] for entry in config_list]

    def test_loads_lorawan_connector(self, app):
        """Regression for F-3: a connector that implements send() is loaded by create_classes
        instead of having a TypeError swallowed as 'not found'.

        Targets `lorawan` rather than `lora`: the serial connector needs pyserial, and a test
        that constructs it here would fail on a core-only checkout for a reason that has
        nothing to do with what it is checking. The LoRaWAN connector needs no extra."""
        config_list = [
            {
                "name": "lorawan_1",
                "protocol": "lorawan",
                "options": {"host": "lns.example"},
            }
        ]
        result = app.create_classes(
            config_list, "protocol", "connectors", Connector,
            expected_name_suffix="connector",
        )
        assert len(result) == 1
        obj = next(iter(result))
        assert obj.name == "lorawan_1"

    def test_instantiation_failure_reports_honestly(self, app, caplog):
        """F-3: a TypeError during construction is reported as 'could not be
        instantiated', not the misleading 'not found'."""
        # pseudo connector requires a 'replay_file'; omitting it raises TypeError
        config_list = [
            {
                "name": "bad",
                "protocol": "pseudo",
                "options": {},
            }
        ]
        with caplog.at_level(logging.ERROR):
            result = app.create_classes(
                config_list, "protocol", "connectors", Connector,
                expected_name_suffix="connector",
            )
        assert len(result) == 0
        assert any("could not be instantiated" in record.message for record in caplog.records)
        assert not any("not found" in record.message for record in caplog.records)

    def test_loads_pseudo_device(self, app):
        config_list = [
            {
                "name": "dev_1",
                "kind": "pseudo",
                "options": {
                    "connector_options": {"name": "c1", "protocol": "pseudo"},
                    "listener_options": {},
                    "controller_options": {},
                },
            }
        ]
        result = app.create_classes(config_list, "kind", "devices", Device)
        assert len(result) == 1
        obj = next(iter(result))
        assert obj.name == "dev_1"

    def test_missing_module_returns_empty(self, app):
        config_list = [
            {"name": "x", "kind": "nonexistent_module_xyz", "options": {}}
        ]
        result = app.create_classes(config_list, "kind", "devices", Device)
        assert len(result) == 0

    def test_extra_arguments_forwarded(self, app):
        """The 'arguments' kwarg is forwarded to the constructor."""
        dm = DevicesManager()
        config_list = [
            {
                "name": "checker_1",
                "class": "device_checker",
                "options": {},
            }
        ]
        result = app.create_classes(
            config_list, "class", "algorithms", Algorithm,
            arguments={"devices_manager": dm},
        )
        assert len(result) == 1
        obj = next(iter(result))
        assert obj.name == "checker_1"
        assert obj.devices_manager is dm


class TestServicesAxis:
    """The fifth axis: a config-declared supervised worker that owns no devices."""

    SERVICE_ENTRY = {"name": "api", "class": "rest_api",
                     "options": {"host": "127.0.0.1", "port": 0}}

    def test_loads_the_rest_api_service_with_both_injected_handles(self, app):
        pytest.importorskip("fastapi", reason="pip install -r requirements-api.txt")

        dm, supervisor = DevicesManager(), Supervisor(RestartPolicy())
        result = app.create_classes(
            [self.SERVICE_ENTRY], "class", "services", Service,
            expected_name_suffix="service",
            arguments={"devices_manager": dm, "supervisor": supervisor},
        )
        assert len(result) == 1
        service = next(iter(result))
        assert service.name == "api"
        assert service.devices_manager is dm
        assert service.supervisor is supervisor

    def test_a_missing_service_module_is_reported_and_startup_continues(self, app, caplog):
        """The optional-dependency contract seen from the other side: a service that
        cannot be loaded is one logged error, not a dead EMS."""

        with caplog.at_level(logging.ERROR):
            result = app.create_classes(
                [{"name": "x", "class": "nonexistent_service_xyz", "options": {}}],
                "class", "services", Service, expected_name_suffix="service",
            )
        assert len(result) == 0
        assert any("not found" in record.message for record in caplog.records)

    def test_services_are_supervised_after_connectors_and_algorithms(self):
        """An observer that comes up before the things it observes reports an empty
        runtime as a healthy one."""
        pytest.importorskip("fastapi", reason="pip install -r requirements-api.txt")
        supervised: list[str] = []

        with patch("main.Config") as MockConfig:
            cfg = MockConfig.return_value
            cfg.logging_level = 10
            cfg.devices = []
            cfg.storage = []
            cfg.connectors = [{"name": "replay", "protocol": "pseudo",
                               "options": {"replay_file": "nonexistent.csv"}}]
            cfg.algorithms = [{"name": "checker", "class": "device_checker", "options": {}}]
            cfg.services = [self.SERVICE_ENTRY]
            app = Main()

        def record(workers, method):
            supervised.append(method)
            return []

        with patch.object(app.SUPERVISOR, "supervise_all", side_effect=record):
            with patch.object(app, "SHUTDOWN_EVENT") as event:
                event.wait.return_value = True
                app.main()

        assert supervised == ["start", "loop", "start"]  # connectors, algorithms, services

    def test_services_without_a_connector_warn_that_the_run_will_not_stay_alive(self, caplog):
        """main waits on the connectors only, so a services-only config exits at once —
        deliberate (a server never finishes and would hang every replay), but surprising."""
        pytest.importorskip("fastapi", reason="pip install -r requirements-api.txt")

        with patch("main.Config") as MockConfig:
            cfg = MockConfig.return_value
            cfg.logging_level = 10
            cfg.devices = []
            cfg.storage = []
            cfg.connectors = []
            cfg.algorithms = []
            cfg.services = [self.SERVICE_ENTRY]
            cfg.shutdown_timeout = 2
            app = Main()

        with caplog.at_level(logging.WARNING):
            app.main()

        assert "nothing keeps this run alive" in caplog.text


class TestStartupOrdering:
    """Every worker is constructed before any of them is started.

    A replay connector begins publishing timesteps as soon as its thread runs, and an
    algorithm only becomes a participant of the lockstep barrier once it exists — built
    after the connectors were started, it silently missed the opening timesteps.
    """

    def test_algorithms_exist_before_connectors_are_started(self):

        participants_at_supervise: list[list[str]] = []

        with patch("main.Config") as MockConfig:
            cfg = MockConfig.return_value
            cfg.logging_level = 10
            cfg.devices = []
            cfg.storage = []
            cfg.services = []
            cfg.connectors = [{
                "name": "replay", "protocol": "pseudo",
                "options": {"replay_file": "nonexistent.csv"},
            }]
            cfg.algorithms = [{"name": "checker", "class": "device_checker", "options": {}}]
            app = Main()

        def record(workers, method):
            participants_at_supervise.append(SimulationClock().participants())
            return []

        with patch.object(app.SUPERVISOR, "supervise_all", side_effect=record):
            with patch.object(app, "SHUTDOWN_EVENT") as event:
                event.wait.return_value = True  # exit the wait loop at once
                app.main()

        # The first supervise_all call is the connectors; the algorithm must already
        # have registered by then.
        assert participants_at_supervise[0] == ["checker"]


BAD_PLUGINS = {
    # A *bare* ImportError, not a ModuleNotFoundError. ModuleNotFoundError subclasses it,
    # so the loader's `except (AttributeError, ModuleNotFoundError)` never saw this one and
    # it ended the whole run.
    "raises_import_error.py": """
        raise ImportError("an optional dependency said no in a way pip cannot fix")
    """,
    # A RuntimeError at module scope — the generic "a stranger's module did something at
    # import time" case.
    "raises_runtime_error.py": """
        raise RuntimeError("module-level work that failed")
    """,
    # Not importable at all. SyntaxError is not an ImportError and was never caught.
    "has_syntax_error.py": """
        def broken(:
    """,
    # sys.exit() at module top. SystemExit derives from BaseException, so neither the old
    # clauses nor a plain `except Exception` would have stopped it.
    "exits_at_import.py": """
        import sys
        sys.exit(3)
    """,
    # Imports cleanly; raises when constructed. ValueError is the canonical case — it is
    # what a plugin coercing its own options with a bare int() produces, which is why
    # api/options.py exists.
    "raises_value_error.py": """
        from api.device import Device

        class RaisesValueError(Device):
            def __init__(self, name, **kwargs):
                raise ValueError("bad option")

            def receive(self, *args, **kwargs):
                pass
    """,
    # KeyboardInterrupt is an operator action, not a plugin defect. It must still
    # terminate startup, which is why the loader catches SystemExit + Exception rather
    # than BaseException wholesale.
    "interrupts_at_import.py": """
        raise KeyboardInterrupt
    """,
    # The healthy neighbour. Devices are the first create_classes call and sit inside a
    # try whose only handler is a finally, so before this commit one bad device module
    # meant no connector, algorithm or storage backend was constructed either.
    "good.py": """
        from api.device import Device

        class Good(Device):
            def __init__(self, name, **kwargs):
                super().__init__(name, {}, {}, {})

            def receive(self, *args, **kwargs):
                pass
    """,
}


@pytest.fixture
def bad_plugins(tmp_path):
    """A real importable package of plugins that fail in every shape that used to be fatal.

    Real files rather than a patched import_module: the point of the commit is what the
    *import machinery* raises, and a SyntaxError in particular has no faithful stand-in.
    `create_classes` takes the package name as an argument, so no shipped axis is touched.
    """
    package = tmp_path / "badplugins"
    package.mkdir()
    (package / "__init__.py").write_text("")
    for filename, body in BAD_PLUGINS.items():
        (package / filename).write_text(textwrap.dedent(body).strip() + "\n")
    sys.path.insert(0, str(tmp_path))
    try:
        yield "badplugins"
    finally:
        sys.path.remove(str(tmp_path))
        for name in [m for m in sys.modules if m == "badplugins" or m.startswith("badplugins.")]:
            del sys.modules[name]


class TestOneBadPluginIsOneSkippedEntry:
    """Every shape of import-time and construction-time failure is contained to its entry.

    Before this, `create_classes` caught only AttributeError, ModuleNotFoundError and
    TypeError. Everything else — a bare ImportError, a RuntimeError, a SyntaxError, a
    module-top sys.exit() — escaped into main()'s try, whose only handler is a finally,
    and took the process down with exit 1.
    """

    @pytest.mark.parametrize("module,expected_log", [
        ("raises_import_error", "raised while loading"),
        ("raises_runtime_error", "raised while loading"),
        ("has_syntax_error", "raised while loading"),
        ("raises_value_error", "raised while loading"),
        ("exits_at_import", "tried to exit the process"),
    ])
    def test_a_bad_module_is_one_skipped_entry(self, app, bad_plugins, caplog, module, expected_log):
        config_list = [{"name": "bad", "kind": module, "options": {}}]
        with caplog.at_level(logging.ERROR):
            result = app.create_classes(config_list, "kind", bad_plugins, Device)
        assert result == []
        assert any(expected_log in record.message for record in caplog.records)
        # The two narrow clauses keep their own wording: a module that blew up is not a
        # module that was missing, and an operator reading the log must be able to tell.
        assert not any("not found" in record.message for record in caplog.records)
        assert not any("could not be instantiated" in record.message for record in caplog.records)

    @pytest.mark.parametrize("module", [
        "raises_import_error", "raises_runtime_error", "has_syntax_error",
        "raises_value_error", "exits_at_import",
    ])
    def test_a_healthy_entry_beside_a_bad_one_is_still_created(self, app, bad_plugins, caplog, module):
        """The property that actually matters. One bad device used to mean no connector.

        Devices are the first create_classes call, inside main()'s try whose only handler
        is a finally — so a device module that blew up took the connectors, the algorithms
        and the storage backends with it, none of which were built yet.
        """
        config_list = [
            {"name": "bad", "kind": module, "options": {}},
            {"name": "healthy", "kind": "good", "options": {}},
        ]
        with caplog.at_level(logging.ERROR):
            result = app.create_classes(config_list, "kind", bad_plugins, Device)
        assert [device.name for device in result] == ["healthy"]

    def test_keyboard_interrupt_during_startup_still_terminates(self, app, bad_plugins):
        """SystemExit + Exception, never BaseException: Ctrl-C is an operator action.

        Catching it here would leave the operator holding a key combination the process
        answers by carrying on and constructing the next plugin.
        """
        config_list = [{"name": "interrupted", "kind": "interrupts_at_import", "options": {}}]
        with pytest.raises(KeyboardInterrupt):
            app.create_classes(config_list, "kind", bad_plugins, Device)

    def test_the_created_line_names_where_the_module_came_from(self, app, bad_plugins, caplog):
        """A plugin silently shadowed by another on sys.path is one of the two startup
        failures an operator cannot otherwise debug from the logs."""
        config_list = [{"name": "healthy", "kind": "good", "options": {}}]
        with caplog.at_level(logging.INFO):
            result = app.create_classes(config_list, "kind", bad_plugins, Device)
        assert len(result) == 1
        created = [r.message for r in caplog.records if "created from" in r.message]
        assert len(created) == 1
        assert "good.py" in created[0]
