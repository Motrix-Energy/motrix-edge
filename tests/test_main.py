import logging
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
