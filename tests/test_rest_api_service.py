"""Read-only REST API service: response shapes, secret redaction, and the stop() bound."""

import logging
import socket
import urllib.error
import urllib.request
from datetime import datetime

import pytest

# fastapi/uvicorn are not in requirements.txt on purpose (see requirements-api.txt). A
# hard import here would abort collection for the WHOLE suite, not just this file — which
# is exactly what tests/test_storage_influxdb.py did before it was guarded.
pytest.importorskip("fastapi", reason="pip install -r requirements-api.txt")
pytest.importorskip("uvicorn", reason="pip install -r requirements-api.txt")
pytest.importorskip("httpx", reason="pip install -r requirements-dev.txt (TestClient needs httpx)")

from fastapi.testclient import TestClient

from api.capabilities import EnergyMeter, MetricSource, Switch
from api.decisions import DEFAULT_CAPACITY, DecisionLog
from connectors.mqtt import MQTTConnector
from devices_manager.devices_manager import DevicesManager
from services.rest_api import RestApiService
from supervisor.supervisor import RestartPolicy, SupervisedWorker, Supervisor
from tests.conftest import STOP_TIMEOUT, StubDevice, assert_stops, run_in_thread, wait_until

SECRET = "hunter2-do-not-leak"

# The exact serialised surface. Asserted as an equality so a new field cannot appear by
# accident — addition is the direction that leaks.
DEVICE_KEYS = {
    "name", "class", "connector", "protocol", "readable", "writable", "connected",
    "data_ready", "capabilities", "metrics", "total_energy_kwh", "data",
}
WORKER_KEYS = {
    "name", "axis", "class", "state", "restarts", "crashes", "max_restarts",
    "restart_enabled", "stopping",
}
DECISION_KEYS = {"seq", "timestamp", "algorithm", "device", "command"}
DECISIONS_ENVELOPE_KEYS = {
    "count", "total", "retained", "capacity", "oldest_seq", "missed",
    "next_cursor", "has_more", "epoch", "simulation_time", "decisions",
}


def make_service(**kwargs) -> RestApiService:
    defaults = dict(
        name="api",
        devices_manager=DevicesManager(),
        supervisor=Supervisor(RestartPolicy()),
        host="127.0.0.1",
        port=0,  # never bind 8000 in a test
    )
    defaults.update(kwargs)
    return RestApiService(**defaults)


def client(service: RestApiService) -> TestClient:
    """The real ASGI app, through the real router and serialiser, with no socket."""
    return TestClient(service.app)


def wait_until_serving(service: RestApiService, timeout: float = STOP_TIMEOUT) -> bool:
    return wait_until(service.is_serving, timeout, interval=0.02)


class MeterStubDevice(StubDevice, EnergyMeter, MetricSource):
    """A device that satisfies two capabilities, to prove they are discovered."""

    def get_total_energy_kwh(self) -> float:
        return 12.5

    def get_metrics(self) -> dict:
        return {"energy_import_t1_kwh": 12.5}


class BrokenStubDevice(StubDevice, MetricSource):
    """A device that breaks the never-raise capability contract."""

    def get_metrics(self) -> dict:
        raise RuntimeError("payload is nonsense")


class SwitchStubDevice(StubDevice, Switch):
    """A controllable device."""


class StubWorkerTarget:
    """The plugin-object surface a worker payload reads, with no thread behind it."""

    def __init__(self, name="w", stopping=False):
        self.name = name
        self._stopping = stopping

    def run(self) -> None:
        pass

    def is_stopping(self) -> bool:
        return self._stopping


def make_supervised(name="w", alive=True, finished=False, completed_cleanly=False,
                    restarts=0, crashes=0, policy=None, target=None) -> SupervisedWorker:
    """A SupervisedWorker with its lifecycle state set directly — no thread is started.

    The states /health has to tell apart (a replay that finished vs one that exhausted its
    restarts) are otherwise reachable only by crashing a real worker five times.
    """
    target = target or StubWorkerTarget(name)
    # SupervisedWorker resolves the target eagerly, and main supervises each axis with its
    # own verb: "start" for connectors and services, "loop" for algorithms.
    method = next(m for m in ("run", "start", "loop") if hasattr(target, m))
    worker = SupervisedWorker(target, method, policy or RestartPolicy())
    worker.restarts = restarts
    worker.crashes = crashes
    worker.completed_cleanly = completed_cleanly
    if finished:
        worker._finished.set()
    worker.is_alive = lambda: alive
    return worker


def supervisor_with(*workers) -> Supervisor:
    supervisor = Supervisor(RestartPolicy())
    supervisor.workers.extend(workers)
    return supervisor


class TestConstruction:
    """Options arrive as strings or None via ${VAR}; none of them may raise."""

    def test_defaults(self):
        service = make_service(host=None, port=None)
        assert service.host == "127.0.0.1"
        assert service.port == 8000
        assert service.docs is True
        assert service.access_log is False
        assert service.shutdown_timeout_seconds == 5

    def test_host_defaults_to_loopback(self):
        """The payload is device data and site topology, unauthenticated. A container
        overrides this to 0.0.0.0 so nginx can reach it; the safe value is the default."""
        assert make_service(host=None).host == "127.0.0.1"

    def test_binding_beyond_loopback_warns(self, caplog):
        with caplog.at_level(logging.WARNING):
            make_service(host="0.0.0.0")
        assert "unauthenticated" in caplog.text

    def test_loopback_binding_is_quiet(self, caplog):
        with caplog.at_level(logging.WARNING):
            make_service(host="127.0.0.1")
        assert "unauthenticated" not in caplog.text

    def test_a_string_port_from_an_env_var_is_coerced(self):
        assert make_service(port="8080").port == 8080

    @pytest.mark.parametrize("value,expected", [
        ("true", True), ("FALSE", False), ("1", True), ("0", False),
        ("on", True), ("off", False), (None, True), (True, True),
    ])
    def test_bool_options_coerce(self, value, expected):
        assert make_service(docs=value).docs is expected

    def test_an_unparsable_port_warns_and_falls_back(self, caplog):
        """create_classes only catches TypeError/AttributeError/ModuleNotFoundError, so a
        ValueError out of __init__ would take the whole EMS down for an optional service.
        Same rule as InfluxDBBackend's option helpers: warn, then default."""
        with caplog.at_level(logging.WARNING):
            service = make_service(port="not-a-port")
        assert service.port == 8000
        assert any("port" in record.message for record in caplog.records)
        assert not any(record.levelno >= logging.ERROR for record in caplog.records)

    def test_an_out_of_range_port_warns_and_falls_back(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert make_service(port=70000).port == 8000
        assert "above 65535" in caplog.text

    def test_construction_binds_no_socket(self):
        service = make_service(port=0)
        assert service.bound_port is None
        assert service.is_serving() is False


class TestDevicesEndpoint:
    """GET /devices reports live registry state, including for devices that never spoke."""

    def test_lists_every_registered_device(self, devices_manager):
        devices_manager.update_device(StubDevice(name="b"))
        devices_manager.update_device(StubDevice(name="a"))
        with client(make_service()) as c:
            body = c.get("/devices").json()
        assert body["count"] == 2
        assert [d["name"] for d in body["devices"]] == ["a", "b"]  # sorted, so it is diffable

    def test_a_configured_but_silent_device_is_reported_not_hidden(self, devices_manager):
        """The payload that justifies the endpoint: connected, zero readings, zero rows in
        any time-series store — invisible to every dashboard, visible here."""
        devices_manager.update_device(StubDevice(name="plug"))
        with client(make_service()) as c:
            device = c.get("/devices").json()["devices"][0]
        assert device["data"] == {}
        assert device["data_ready"] is False
        assert device["connected"] is False

    def test_readiness_flags_track_the_events(self, devices_manager):
        device = StubDevice(name="meter")
        device.mark_data_ready()
        device.mark_connected()
        devices_manager.update_device(device)
        with client(make_service()) as c:
            payload = c.get("/devices").json()["devices"][0]
        assert payload["data_ready"] is True
        assert payload["connected"] is True

    def test_capabilities_are_discovered_from_the_capability_module(self, devices_manager):
        devices_manager.update_device(MeterStubDevice(name="meter"))
        with client(make_service()) as c:
            payload = c.get("/devices").json()["devices"][0]
        assert sorted(payload["capabilities"]) == ["EnergyMeter", "MetricSource"]
        assert payload["total_energy_kwh"] == 12.5
        assert payload["metrics"] == {"energy_import_t1_kwh": 12.5}

    def test_a_device_without_a_capability_reports_null_not_an_error(self, devices_manager):
        devices_manager.update_device(SwitchStubDevice(name="plug"))
        with client(make_service()) as c:
            payload = c.get("/devices").json()["devices"][0]
        assert payload["capabilities"] == ["Switch"]
        assert payload["metrics"] is None
        assert payload["total_energy_kwh"] is None

    def test_a_device_that_breaks_the_capability_contract_does_not_500_the_list(self, devices_manager, caplog):
        """api/capabilities.py says get_metrics() never raises, but a read-only endpoint is
        the wrong place to find out a third-party device disagreed."""
        devices_manager.update_device(BrokenStubDevice(name="broken"))
        devices_manager.update_device(StubDevice(name="fine"))
        with caplog.at_level(logging.WARNING):
            with client(make_service()) as c:
                response = c.get("/devices")
        assert response.status_code == 200
        assert response.json()["count"] == 2
        assert "raised in get_metrics()" in caplog.text

    def test_class_is_reported_not_the_config_kind(self, devices_manager):
        devices_manager.update_device(StubDevice(name="meter"))
        with client(make_service()) as c:
            assert c.get("/devices").json()["devices"][0]["class"] == "StubDevice"

    def test_connector_and_protocol_come_from_connector_options(self, devices_manager):
        devices_manager.update_device(StubDevice(
            name="meter", connector_options={"name": "replay", "protocol": "mqtt"}))
        with client(make_service()) as c:
            payload = c.get("/devices").json()["devices"][0]
        assert payload["connector"] == "replay"
        assert payload["protocol"] == "mqtt"

    def test_a_single_device_is_reachable_by_name(self, devices_manager):
        devices_manager.update_device(StubDevice(name="meter"))
        with client(make_service()) as c:
            response = c.get("/devices/meter")
        assert response.status_code == 200
        assert response.json()["device"]["name"] == "meter"

    def test_a_device_name_with_a_space_is_reachable(self, devices_manager):
        """Config lets a device be called "P1 meter" (bak.config.json does exactly that),
        so the path parameter has to survive rather than 404."""
        devices_manager.update_device(StubDevice(name="P1 meter"))
        with client(make_service()) as c:
            response = c.get("/devices/P1%20meter")
        assert response.status_code == 200
        assert response.json()["device"]["name"] == "P1 meter"

    def test_a_device_name_with_a_slash_is_reachable(self):
        """`{name:path}` rather than `{name}`: nothing in config.schema.json forbids a
        slash in a device name, and a plain path segment would 404 on a legal name."""
        DevicesManager().update_device(StubDevice(name="site/meter"))
        with client(make_service()) as c:
            response = c.get("/devices/site/meter")
        assert response.status_code == 200
        assert response.json()["device"]["name"] == "site/meter"

    def test_an_unknown_device_is_404_with_a_json_detail(self):
        with client(make_service()) as c:
            response = c.get("/devices/nope")
        assert response.status_code == 404
        assert response.json() == {"detail": "Unknown device"}

    def test_an_unknown_device_does_not_echo_the_requested_name(self):
        with client(make_service()) as c:
            assert "<script>" not in c.get("/devices/<script>alert(1)</script>").text

    def test_devices_is_empty_before_any_device_is_registered(self):
        """A request can arrive during startup. The API is a snapshot of live state, never
        a participant in the readiness contract — it must not block on an Event."""
        with client(make_service()) as c:
            response = c.get("/devices")
        assert response.status_code == 200
        assert response.json() == {"count": 0, "simulation_time": None, "devices": []}

    def test_the_device_payload_is_exactly_the_allowlist(self, devices_manager):
        devices_manager.update_device(StubDevice(name="meter"))
        with client(make_service()) as c:
            assert set(c.get("/devices").json()["devices"][0]) == DEVICE_KEYS


class TestWorkersEndpoint:
    """GET /workers is keyed by the thing that actually owns the workers: the supervisor."""

    def test_reports_one_entry_per_supervised_worker(self):
        supervisor = supervisor_with(make_supervised("a"), make_supervised("b"))
        with client(make_service(supervisor=supervisor)) as c:
            body = c.get("/workers").json()
        assert body["count"] == 2
        assert {w["name"] for w in body["workers"]} == {"a", "b"}

    def test_the_axis_discriminator_covers_all_three_axes(self, make_connector):
        from algorithms.device_checker import DeviceChecker
        connector = make_connector(name="mqtt")
        algorithm = DeviceChecker("checker", DevicesManager())
        service = make_service()
        supervisor = supervisor_with(
            make_supervised(target=connector), make_supervised(target=algorithm),
            make_supervised(target=service),
        )
        service.supervisor = supervisor
        with client(service) as c:
            axes = {w["name"]: w["axis"] for w in c.get("/workers").json()["workers"]}
        assert axes == {"mqtt": "connector", "checker": "algorithm", "api": "service"}

    def test_a_connector_reports_device_names_only(self, make_connector, make_device):
        connector = make_connector(name="mqtt")
        connector.inject_devices({"meter": make_device(name="meter")})
        supervisor = supervisor_with(make_supervised(target=connector))
        with client(make_service(supervisor=supervisor)) as c:
            payload = c.get("/workers").json()["workers"][0]
        assert payload["devices"] == ["meter"]

    def test_a_connector_before_inject_devices_does_not_explode(self, make_connector):
        """Connector.devices is declared but only assigned by inject_devices()."""
        supervisor = supervisor_with(make_supervised(target=make_connector(name="mqtt")))
        with client(make_service(supervisor=supervisor)) as c:
            assert c.get("/workers").json()["workers"][0]["devices"] == []

    def test_an_algorithm_reports_its_cadence_and_run_accounting(self):
        from algorithms.device_checker import DeviceChecker
        algorithm = DeviceChecker("checker", DevicesManager(), delay_seconds=900,
                                  required_devices=["meter"])
        algorithm.runs = 3
        algorithm.last_run_seconds = 1.23456
        algorithm.last_run_at = datetime(2024, 3, 1, 8, 15)
        supervisor = supervisor_with(make_supervised(target=algorithm))
        with client(make_service(supervisor=supervisor)) as c:
            payload = c.get("/workers").json()["workers"][0]
        assert payload["delay_seconds"] == 900
        assert payload["required_devices"] == ["meter"]
        assert payload["runs"] == 3
        assert payload["last_run_seconds"] == 1.235
        assert payload["last_run"] == "2024-03-01T08:15:00"

    def test_an_algorithm_that_never_ran_reports_null_not_a_missing_key(self):
        from algorithms.device_checker import DeviceChecker
        supervisor = supervisor_with(make_supervised(target=DeviceChecker("checker", DevicesManager())))
        with client(make_service(supervisor=supervisor)) as c:
            payload = c.get("/workers").json()["workers"][0]
        assert payload["runs"] == 0
        assert payload["last_run"] is None
        assert payload["last_run_seconds"] is None

    def test_an_algorithm_reports_whether_it_is_a_barrier_participant(self):
        from algorithms.device_checker import DeviceChecker
        algorithm = DeviceChecker("checker", DevicesManager())  # __init__ joins the clock
        supervisor = supervisor_with(make_supervised(target=algorithm))
        with client(make_service(supervisor=supervisor)) as c:
            assert c.get("/workers").json()["workers"][0]["step_participant"] is True

    def test_lifecycle_counters_are_reported(self):
        supervisor = supervisor_with(make_supervised("a", restarts=2, crashes=3))
        with client(make_service(supervisor=supervisor)) as c:
            payload = c.get("/workers").json()["workers"][0]
        assert (payload["restarts"], payload["crashes"]) == (2, 3)
        assert payload["max_restarts"] == 5
        assert payload["restart_enabled"] is True

    def test_a_worker_without_stoppable_reports_null_rather_than_guessing(self):
        supervisor = supervisor_with(make_supervised("a"))
        with client(make_service(supervisor=supervisor)) as c:
            assert c.get("/workers").json()["workers"][0]["stopping"] is None

    def test_the_worker_payload_is_exactly_the_allowlist(self):
        supervisor = supervisor_with(make_supervised("a"))
        with client(make_service(supervisor=supervisor)) as c:
            assert set(c.get("/workers").json()["workers"][0]) == WORKER_KEYS


class TestWorkerState:
    """is_finished() is set on all five of SupervisedWorker._run's exit paths, so
    completed_cleanly is what separates a finished replay from one that gave up."""

    def test_a_live_worker_is_running(self):
        assert RestApiService._worker_state(make_supervised(alive=True, finished=False)) == "running"

    def test_a_clean_return_is_finished(self):
        assert RestApiService._worker_state(
            make_supervised(alive=False, finished=True, completed_cleanly=True)) == "finished"

    def test_an_exhausted_restart_budget_is_down(self):
        assert RestApiService._worker_state(
            make_supervised(alive=False, finished=True, completed_cleanly=False,
                            restarts=5, crashes=6)) == "down"

    def test_a_disabled_restart_policy_is_down_even_with_zero_restarts(self):
        """The case no restarts/crashes arithmetic gets right: _run breaks immediately when
        the policy is disabled, so restarts is still 0 while the worker is permanently down."""
        assert RestApiService._worker_state(make_supervised(
            alive=False, finished=True, completed_cleanly=False, restarts=0, crashes=1,
            policy=RestartPolicy(enabled=False))) == "down"

    def test_a_clean_return_after_crashes_is_finished_not_down(self):
        assert RestApiService._worker_state(make_supervised(
            alive=False, finished=True, completed_cleanly=True, restarts=2, crashes=2)) == "finished"

    def test_a_dead_thread_that_never_finished_is_lost(self):
        """A SystemExit escaping a library kills the thread without reaching _finished.set()
        and without the supervisor's `except Exception` seeing it. Nothing else reports it."""
        assert RestApiService._worker_state(make_supervised(alive=False, finished=False)) == "lost"


class TestHealthStatus:
    """/health's status becomes the container's HEALTHCHECK exit code, so the derivation is
    a deployment contract, not a cosmetic field."""

    def test_ok_when_every_worker_is_alive(self):
        supervisor = supervisor_with(make_supervised("a"), make_supervised("b"))
        with client(make_service(supervisor=supervisor)) as c:
            response = c.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_degraded_while_a_worker_is_restarting(self):
        supervisor = supervisor_with(make_supervised("mqtt", alive=True, restarts=2, crashes=2))
        with client(make_service(supervisor=supervisor)) as c:
            response = c.get("/health")
        assert response.status_code == 200, "a restarting worker must not make Docker kill the container"
        assert response.json()["status"] == "degraded"

    def test_down_when_a_worker_exhausted_its_restarts(self):
        supervisor = supervisor_with(make_supervised(
            "mqtt", alive=False, finished=True, completed_cleanly=False, restarts=5, crashes=6))
        with client(make_service(supervisor=supervisor)) as c:
            response = c.get("/health")
        assert response.status_code == 503
        assert response.json()["status"] == "down"

    def test_a_replay_that_ran_to_completion_is_not_down(self):
        """Otherwise every green backtest would answer 503 and compose would restart it."""
        supervisor = supervisor_with(make_supervised(
            "replay", alive=False, finished=True, completed_cleanly=True))
        with client(make_service(supervisor=supervisor)) as c:
            response = c.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_a_lost_worker_is_down(self):
        supervisor = supervisor_with(make_supervised("api", alive=False, finished=False))
        with client(make_service(supervisor=supervisor)) as c:
            assert c.get("/health").json()["status"] == "down"

    def test_answers_before_any_worker_exists(self):
        """The --start-period window: /health must answer during startup or the container
        flaps before it has had a chance to come up."""
        with client(make_service()) as c:
            response = c.get("/health")
        assert response.status_code == 200
        assert response.json()["workers"]["total"] == 0

    def test_reports_worker_counts_by_state(self):
        supervisor = supervisor_with(
            make_supervised("a"),
            make_supervised("b", alive=False, finished=True, completed_cleanly=True),
            make_supervised("c", alive=False, finished=True, restarts=5, crashes=6),
        )
        with client(make_service(supervisor=supervisor)) as c:
            counts = c.get("/health").json()["workers"]
        assert counts == {"total": 3, "running": 1, "finished": 1, "down": 1, "lost": 0,
                          "crashed": 1, "restarts": 5}

    def test_reports_device_counts(self, devices_manager):
        ready = StubDevice(name="ready")
        ready.mark_data_ready()
        ready.mark_connected()
        devices_manager.update_device(ready)
        devices_manager.update_device(StubDevice(name="silent"))
        with client(make_service()) as c:
            assert c.get("/health").json()["devices"] == {"total": 2, "connected": 1, "data_ready": 1}

    def test_reports_the_replay_clock(self):
        from simulation.clock import SimulationClock
        clock = SimulationClock()
        clock.join("checker")
        clock.publish_step(datetime(2024, 3, 1, 8, 15))
        with client(make_service()) as c:
            reported = c.get("/health").json()["clock"]
        assert reported["simulated"] is True
        assert reported["generation"] == 1
        assert reported["step_time"] == "2024-03-01T08:15:00"
        assert reported["pending"] == ["checker"], "the algorithm the replay is waiting on"

    def test_reports_a_wall_clock_run_as_unsimulated(self):
        with client(make_service()) as c:
            reported = c.get("/health").json()["clock"]
        assert reported["simulated"] is False
        assert reported["step_time"] is None


class TestDecisionsEndpoint:
    """The one endpoint that serves history rather than a snapshot."""

    @staticmethod
    def _record(count: int, algorithm: str = "AutoToggle", device: str = "shelly_plug") -> None:
        for index in range(count):
            DecisionLog().record(algorithm, device, "on" if index % 2 else "off")

    def test_serialised_surface_is_exactly_five_keys(self):
        self._record(1)
        with client(make_service()) as c:
            body = c.get("/decisions").json()
        assert set(body["decisions"][0]) == DECISION_KEYS

    def test_envelope_surface_is_exact(self):
        with client(make_service()) as c:
            assert set(c.get("/decisions").json()) == DECISIONS_ENVELOPE_KEYS

    def test_empty_log_is_200_and_not_404(self):
        """"No decisions yet" and "this EMS is too old to have the route" must stay
        distinguishable — that one status is the whole degradation contract for a client."""
        with client(make_service()) as c:
            response = c.get("/decisions")
        assert response.status_code == 200
        assert response.json()["count"] == 0
        assert response.json()["decisions"] == []

    def test_reports_decisions_oldest_first(self):
        self._record(3)
        with client(make_service()) as c:
            decisions = c.get("/decisions").json()["decisions"]
        assert [d["seq"] for d in decisions] == [1, 2, 3]
        assert [d["command"] for d in decisions] == ["off", "on", "off"]

    def test_field_names_match_the_csv_columns(self):
        """A consumer already reading algorithm_decisions.csv needs no second vocabulary."""
        from storage.csv_file import CsvFileBackend
        self._record(1)
        with client(make_service()) as c:
            decision = c.get("/decisions").json()["decisions"][0]
        assert set(CsvFileBackend.ALGORITHM_DECISIONS_HEADERS) <= set(decision)

    def test_command_is_served_verbatim_not_parsed(self):
        DecisionLog().record("AutoToggle", "shelly_plug", '{"setpoint": 21.5}')
        with client(make_service()) as c:
            command = c.get("/decisions").json()["decisions"][0]["command"]
        assert command == '{"setpoint": 21.5}'

    def test_records_reached_through_the_storage_manager(self):
        """The wiring that makes this endpoint and the CSV agree by construction."""
        from storage_manager.storage_manager import StorageManager
        StorageManager().write_algorithm_decision("AutoToggle", "shelly_plug", "on")
        with client(make_service()) as c:
            decisions = c.get("/decisions").json()["decisions"]
        assert [(d["algorithm"], d["device"], d["command"]) for d in decisions] == [
            ("AutoToggle", "shelly_plug", "on")
        ]

    def test_naive_step_time_round_trips_naive(self):
        from simulation.clock import SimulationClock
        SimulationClock().publish_step(datetime(2024, 1, 15, 10, 0))
        self._record(1)
        with client(make_service()) as c:
            assert c.get("/decisions").json()["decisions"][0]["timestamp"] == "2024-01-15T10:00:00"

    def test_two_polls_across_a_shared_timestamp_lose_nothing_and_repeat_nothing(self):
        """The acceptance test: the bug a timestamp cursor would have.

        Under speed=0 every decision in a timestep carries the identical committed step time.
        Polling on `seq` must see each decision exactly once across the boundary.
        """
        from simulation.clock import SimulationClock
        SimulationClock().publish_step(datetime(2024, 1, 15, 10, 0))
        self._record(2)
        with client(make_service()) as c:
            first = c.get("/decisions").json()
            assert len({d["timestamp"] for d in first["decisions"]}) == 1

            self._record(2)  # same step, same timestamp
            second = c.get(f"/decisions?after={first['next_cursor']}").json()

        seqs = [d["seq"] for d in first["decisions"]] + [d["seq"] for d in second["decisions"]]
        assert seqs == [1, 2, 3, 4]

    def test_after_is_exclusive(self):
        self._record(5)
        with client(make_service()) as c:
            assert [d["seq"] for d in c.get("/decisions?after=3").json()["decisions"]] == [4, 5]

    def test_has_more_and_next_cursor_drive_a_drain(self):
        self._record(10)
        seen, cursor = [], 0
        with client(make_service()) as c:
            while True:
                page = c.get(f"/decisions?after={cursor}&limit=4").json()
                seen.extend(d["seq"] for d in page["decisions"])
                cursor = page["next_cursor"]
                if not page["has_more"]:
                    break
        assert seen == list(range(1, 11))

    def test_limit_zero_seeks_to_head_without_delivering(self):
        """The capability probe: one cheap request that both proves the route exists and
        seeds a cursor, so a client starts streaming from now instead of backfilling."""
        self._record(5)
        with client(make_service()) as c:
            page = c.get("/decisions?after=-1&limit=0").json()
        assert page["decisions"] == []
        assert page["next_cursor"] == 5
        assert page["has_more"] is False

    def test_out_of_range_parameters_clamp_rather_than_reject(self):
        """A polling client must not be able to wedge itself on a value it computed."""
        self._record(3)
        with client(make_service()) as c:
            assert c.get("/decisions?after=-5").status_code == 200
            assert [d["seq"] for d in c.get("/decisions?after=-5").json()["decisions"]] == [1, 2, 3]
            assert c.get("/decisions?limit=999999").status_code == 200
            assert len(c.get("/decisions?limit=999999").json()["decisions"]) == 3

    def test_a_non_integer_parameter_is_422_not_500(self):
        with client(make_service()) as c:
            assert c.get("/decisions?after=abc").status_code == 422
            assert c.get("/decisions?limit=abc").status_code == 422

    def test_epoch_is_stable_across_requests(self):
        self._record(1)
        with client(make_service()) as c:
            assert c.get("/decisions").json()["epoch"] == c.get("/decisions").json()["epoch"]

    def test_missed_reports_what_fell_off_the_ring(self):
        from api.decisions import DecisionLog as Log
        from __metaclasses.singleton import Singleton
        Singleton._instances.pop(Log, None)
        Log(capacity=5)
        self._record(8)
        with client(make_service()) as c:
            page = c.get("/decisions?after=1").json()
        assert page["oldest_seq"] == 4
        assert page["missed"] == 2  # seq 2 and 3 are gone

    def test_is_get_only(self):
        with client(make_service()) as c:
            assert c.post("/decisions").status_code == 405

    def test_is_never_cached(self):
        with client(make_service()) as c:
            assert c.get("/decisions").headers["cache-control"] == "no-store"

    def test_health_reports_decision_counts(self):
        self._record(3)
        with client(make_service()) as c:
            counts = c.get("/health").json()["decisions"]
        assert counts["total"] == 3
        assert counts["retained"] == 3
        assert counts["capacity"] == DEFAULT_CAPACITY


class TestRedaction:
    """No response body ever carries a credential, from any endpoint.

    The service serialises an allowlist of named fields, never vars(obj). This matters
    because Device.__deepcopy__ deliberately *shares* the live connector rather than
    copying it, so any attribute walk over a device snapshot reaches
    MQTTConnector.password — a plain public attribute. This walks every endpoint rather
    than one field, because the point is that no future field can leak either.
    """
    PATHS = ("/health", "/devices", "/devices/meter", "/workers", "/decisions")

    @staticmethod
    def _wire_a_broker_with_a_password():
        connector = MQTTConnector(name="broker", host="mqtt.test", port=1883,
                                  version="3.1.1", username="ems", password=SECRET)
        device = StubDevice(name="meter", connector_options={"name": "broker", "protocol": "mqtt"})
        connector.inject_devices({"meter": device})
        DevicesManager().update_device(device)
        return connector

    def test_no_endpoint_leaks_a_connector_password(self):
        connector = self._wire_a_broker_with_a_password()
        service = make_service(supervisor=supervisor_with(make_supervised(target=connector)))
        with client(service) as c:
            for path in self.PATHS:
                body = c.get(path).text
                assert SECRET not in body, f"{path} leaked the connector password"
                assert "password" not in body.lower(), f"{path} exposed a password field"

    def test_no_endpoint_leaks_a_credential_hidden_in_device_options(self):
        """Every devices/*.schema.json types listener_options as a bare object, so an
        operator can and will put an api_key in there. Device options are configuration
        and may hold credentials; device *data* is telemetry and does not — that boundary
        is why /devices exposes `data` and never exposes the option dicts."""
        DevicesManager().update_device(StubDevice(
            name="meter",
            listener_options={"api_key": SECRET},
            controller_options={"token": SECRET},
        ))
        with client(make_service()) as c:
            for path in self.PATHS:
                assert SECRET not in c.get(path).text, f"{path} leaked a device option"

    def test_an_absent_password_does_not_leak_the_key_either(self):
        """${MQTT_PASSWORD} on an unset var resolves to None. Emitting "password": null
        still tells a reader the field exists."""
        connector = MQTTConnector(name="broker", host="mqtt.test", port=1883,
                                  version="3.1.1", username=None, password=None)
        service = make_service(supervisor=supervisor_with(make_supervised(target=connector)))
        with client(service) as c:
            assert "password" not in c.get("/workers").text.lower()

    def test_a_device_snapshot_can_reach_the_live_connector(self):
        """Guards the premise of this whole class rather than the code under test: if this
        ever fails, __deepcopy__ changed and the allowlist rationale needs rereading."""
        connector = self._wire_a_broker_with_a_password()
        snapshot = DevicesManager().get_device("meter")
        assert snapshot.connector is connector
        assert snapshot.connector.password == SECRET


class TestRoutesAreReadOnly:
    """DevicesAccess.control() is one careless route away from an actuation API."""

    def test_only_get_routes_are_registered(self):
        methods = set()
        for route in make_service().app.routes:
            methods |= getattr(route, "methods", set())
        assert methods <= {"GET", "HEAD"}

    def test_posting_to_a_read_endpoint_is_rejected(self):
        with client(make_service()) as c:
            assert c.post("/devices").status_code == 405

    def test_responses_are_not_cacheable(self):
        """These are live readings behind a reverse proxy that would otherwise be free to
        cache them — including a payload carrying a meter's equipment identifier."""
        with client(make_service()) as c:
            assert c.get("/devices").headers["cache-control"] == "no-store"


class TestSerialization:
    """device.data is whatever a plugin parsed; the encoder must never be the thing that
    discovers it cannot represent it."""

    def test_a_non_finite_float_becomes_a_string(self, devices_manager):
        """json.dumps emits bare NaN, which is not valid JSON — one divide-by-zero inside a
        device would break the viewer's JSON.parse rather than one field."""
        device = StubDevice(name="meter")
        device.data = {"nan": float("nan"), "inf": float("-inf"), "ok": 1.5}
        devices_manager.update_device(device)
        with client(make_service()) as c:
            data = c.get("/devices").json()["devices"][0]["data"]
        assert data == {"nan": "nan", "inf": "-inf", "ok": 1.5}

    def test_bytes_datetimes_and_sets_are_coerced(self, devices_manager):
        device = StubDevice(name="meter")
        device.data = {"raw": b"\xff hello", "at": datetime(2024, 3, 1, 8, 15), "tags": {"a"}}
        devices_manager.update_device(device)
        with client(make_service()) as c:
            data = c.get("/devices").json()["devices"][0]["data"]
        assert data["at"] == "2024-03-01T08:15:00"
        assert data["tags"] == ["a"]
        assert "hello" in data["raw"]

    def test_an_unknown_object_degrades_to_str_not_an_attribute_dump(self, devices_manager):
        class Opaque:
            def __init__(self):
                self.password = SECRET

            def __str__(self):
                return "opaque"

        device = StubDevice(name="meter")
        device.data = {"thing": Opaque()}
        devices_manager.update_device(device)
        with client(make_service()) as c:
            body = c.get("/devices").text
        assert SECRET not in body
        assert "opaque" in body

    def test_a_self_referencing_payload_terminates(self, devices_manager):
        """A cycle would otherwise recurse until the interpreter gives up, inside a request."""
        payload = {}
        payload["self"] = payload
        device = StubDevice(name="meter")
        device.data = payload
        devices_manager.update_device(device)
        with client(make_service()) as c:
            assert c.get("/devices").status_code == 200

    def test_non_string_dict_keys_are_stringified(self, devices_manager):
        device = StubDevice(name="meter")
        device.data = {1: "one"}
        devices_manager.update_device(device)
        with client(make_service()) as c:
            assert c.get("/devices").json()["devices"][0]["data"] == {"1": "one"}


class TestServiceLifecycle:
    """stop() must return a blocking start() inside the shutdown grace period —
    tests/test_shutdown.py's contract, applied to a real server on a real socket."""

    def test_stop_returns_the_server_thread(self):
        service = make_service(port=0)
        thread = run_in_thread(service.start)
        assert wait_until_serving(service), "server never came up"
        assert_stops(service, thread)

    def test_the_server_actually_answers_before_it_is_stopped(self):
        """An in-process TestClient cannot prove a socket was ever bound."""
        service = make_service(port=0)
        thread = run_in_thread(service.start)
        assert wait_until_serving(service)
        try:
            url = f"http://127.0.0.1:{service.bound_port}/health"
            with urllib.request.urlopen(url, timeout=STOP_TIMEOUT) as response:
                assert response.status == 200
        finally:
            assert_stops(service, thread)

    def test_stop_before_start_is_safe(self):
        """The uvicorn Server only exists once start() ran; MQTTConnector.stop() carries
        the same guard for the same reason."""
        service = make_service()
        service.stop()
        assert service.is_stopping()

    def test_stop_before_start_never_binds(self, caplog):
        service = make_service(port=0)
        service.stop()
        with caplog.at_level(logging.INFO):
            service.start()  # must return at once
        assert service.bound_port is None
        assert "not binding" in caplog.text

    def test_a_port_already_in_use_is_a_crash_not_a_silent_exit(self):
        """uvicorn calls sys.exit(1) when it cannot bind. SystemExit is a BaseException, so
        SupervisedWorker._run's `except Exception` misses it: the thread would die with no
        ERROR, no restart, and without ever reaching _finished.set() — a permanently
        unfinished worker nobody logged. Converting it puts it back under supervision."""
        hog = socket.socket()
        hog.bind(("127.0.0.1", 0))
        hog.listen()
        try:
            service = make_service(port=hog.getsockname()[1])
            with pytest.raises(RuntimeError, match="could not bind"):
                service.start()
        finally:
            hog.close()

    def test_a_bind_failure_is_supervised(self):
        """The end of the same argument: once it is an Exception, the supervisor sees it."""
        hog = socket.socket()
        hog.bind(("127.0.0.1", 0))
        hog.listen()
        try:
            service = make_service(port=hog.getsockname()[1])
            supervisor = Supervisor(RestartPolicy(enabled=False))
            worker = supervisor.supervise(service, "start")
            worker.join(STOP_TIMEOUT)
            assert worker.is_finished()
            assert worker.crashes == 1
            assert worker.completed_cleanly is False
        finally:
            hog.close()
            service.stop()

    def test_uvicorn_does_not_steal_the_signal_handlers(self):
        """main.py owns SIGTERM/SIGINT. uvicorn's capture_signals() is a no-op off the main
        thread, which is why start() suppresses nothing — do not 'fix' that with
        Server.install_signal_handlers (gone since 0.29) or Config(install_signal_handlers=
        False) (a TypeError inside a supervised start(), i.e. a restart loop)."""
        import signal

        before = signal.getsignal(signal.SIGINT)
        service = make_service(port=0)
        thread = run_in_thread(service.start)
        assert wait_until_serving(service)
        try:
            assert signal.getsignal(signal.SIGINT) is before
        finally:
            assert_stops(service, thread)
