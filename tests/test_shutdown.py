"""Graceful shutdown: stop() unblocks every long-running worker, storage closes."""

import logging
from datetime import datetime, timedelta
from threading import Event
from unittest.mock import MagicMock, patch

import pytest

from api.stoppable import Stoppable
from connectors.http_api import HttpApiConnector
from connectors.lorawan import LoRaWANConnector
from connectors.mqtt import MQTTConnector
from connectors.pseudo import PseudoConnector
from simulation.clock import SimulationClock
from tests.conftest import (
    STOP_TIMEOUT, StubAlgorithm, StubDevice, StubStorageBackend,
    assert_stops, make_devices_access, make_pseudo, run_in_thread, wait_until, write_replay,
)


class TestStoppable:
    def test_stop_is_idempotent_and_observable(self):
        class Worker(Stoppable):
            pass

        worker = Worker()
        assert not worker.is_stopping()
        assert worker.wait_stop(0.01) is False  # timed out, no stop requested
        worker.stop()
        worker.stop()
        assert worker.is_stopping()
        assert worker.wait_stop(0.01) is True  # returns at once

    def test_connectors_and_algorithms_are_stoppable(self, make_connector):
        assert isinstance(make_connector(), Stoppable)
        assert isinstance(StubAlgorithm("a", make_devices_access()), Stoppable)



class TestAlgorithmLoop:
    def test_wall_clock_loop_returns_on_stop(self):
        algo = StubAlgorithm("algo", make_devices_access(), delay_seconds=30)
        thread = run_in_thread(algo.loop)
        assert algo.ran.wait(STOP_TIMEOUT)
        # stop() must cut the 30s delay short, not wait it out
        assert_stops(algo, thread)

    def test_simulated_loop_steps_and_returns_on_stop(self):
        clock = SimulationClock()
        clock.start_simulation()
        algo = StubAlgorithm("algo", make_devices_access())
        thread = run_in_thread(algo.loop)
        # Publish only once the algorithm is a participant, or it joins at the new
        # generation and is correctly considered to have missed nothing.
        assert wait_until(lambda: "algo" in clock.participants())
        clock.publish_step(datetime(2024, 1, 1))
        assert algo.ran.wait(STOP_TIMEOUT)
        assert_stops(algo, thread)

    def test_simulated_loop_leaves_the_clock_on_exit(self):
        """A departing algorithm must release a replay waiting on it."""
        clock = SimulationClock()
        clock.start_simulation()
        algo = StubAlgorithm("algo", make_devices_access())
        thread = run_in_thread(algo.loop)
        assert wait_until(lambda: "algo" in clock.participants())
        assert_stops(algo, thread)
        assert clock.participants() == []

    def test_stop_before_start_skips_main_entirely(self):
        algo = StubAlgorithm("algo", make_devices_access())
        algo.stop()
        algo.loop()  # must return immediately
        assert algo.main_calls == 0

    def test_wait_forever_readiness_gate_is_interruptible(self):
        """wait_for_devices_timeout=None used to block shutdown forever."""
        device = StubDevice(name="meter")  # never marked ready
        algo = StubAlgorithm("algo", make_devices_access({"meter": device}),
                             required_devices=["meter"], wait_for_devices_timeout=None)
        thread = run_in_thread(algo.loop)
        assert_stops(algo, thread)
        assert algo.main_calls == 0  # stopped while still gating


class TestPseudoConnectorStop:
    def _slow_replay(self, tmp_path):
        start = datetime(2024, 1, 15, 10, 0, 0)
        csv_file = tmp_path / "replay.csv"
        write_replay(csv_file, [
            ((start + timedelta(seconds=i * 30)).isoformat(), "sensor", "sensors/a", "{}")
            for i in range(5)
        ])
        return csv_file

    def _wire(self, csv_file):
        device = make_pseudo("sensor", connector_options={"name": "c", "protocol": "pseudo"})
        connector = PseudoConnector("c", replay_file=str(csv_file), speed=1)
        connector.inject_devices({"sensor": device})
        return connector, device

    def test_stop_interrupts_the_replay_sleep(self, tmp_path, devices_manager, caplog):
        connector, device = self._wire(self._slow_replay(tmp_path))
        with caplog.at_level(logging.INFO):
            thread = run_in_thread(connector.start)
            assert device.wait_until_ready(STOP_TIMEOUT)  # first entry replayed
            # the next entry is 30 simulated seconds away — stop must not wait for it
            assert_stops(connector, thread)
        assert any("Replay stopped" in r.message for r in caplog.records)

    def test_stop_clears_simulation_time(self, tmp_path, devices_manager):
        connector, device = self._wire(self._slow_replay(tmp_path))
        thread = run_in_thread(connector.start)
        assert device.wait_until_ready(STOP_TIMEOUT)
        assert_stops(connector, thread)
        assert devices_manager.get_simulation_time() is None

    def test_looping_replay_stops(self, tmp_path, devices_manager):
        """loop=True is the only truly infinite pseudo run."""
        csv_file = tmp_path / "replay.csv"
        csv_file.write_text("timestamp,device_name,topic,payload\n2024-01-15T10:00:00,sensor,t,{}\n")
        device = make_pseudo("sensor", connector_options={"name": "c", "protocol": "pseudo"})
        connector = PseudoConnector("c", replay_file=str(csv_file), speed=0, loop=True)
        connector.inject_devices({"sensor": device})
        thread = run_in_thread(connector.start)
        assert device.wait_until_ready(STOP_TIMEOUT)
        assert_stops(connector, thread)


class TestHttpApiConnectorStop:
    def _wire(self):
        device = StubDevice(name="meter", listener_options={"endpoint": "/meter", "interval": 30})
        connector = HttpApiConnector(name="HTTP 1", base_url="http://api.example")
        connector.inject_devices({"meter": device})
        # Mocked at _build_session() rather than by assigning _session: start() builds its
        # own session per run — so a supervisor restart never polls on the closed one — and
        # would overwrite anything planted on the attribute beforehand.
        session = MagicMock()
        session.request.return_value = MagicMock(text="{}", status_code=200)
        connector._build_session = MagicMock(return_value=session)
        return connector, device, session

    def test_poll_loop_returns_on_stop(self, caplog):
        connector, device, _ = self._wire()
        with caplog.at_level(logging.INFO):
            thread = run_in_thread(connector.start)
            assert device.wait_until_ready(STOP_TIMEOUT)  # polled once
            # the next poll is 30s away — stop must interrupt the wait
            assert_stops(connector, thread)
        assert any("Polling stopped" in r.message for r in caplog.records)

    def test_session_is_closed_when_the_loop_ends(self):
        connector, device, session = self._wire()
        thread = run_in_thread(connector.start)
        assert device.wait_until_ready(STOP_TIMEOUT)
        assert_stops(connector, thread)
        session.close.assert_called_once()


class TestMqttConnectorStop:
    @patch("connectors.mqtt.Client")
    def test_stop_disconnects_the_client(self, MockClient):
        connector = MQTTConnector(name="MQTT 1", host="broker.example", port=1883, version="3.1.1")
        connector.start()
        connector.stop()
        MockClient.return_value.disconnect.assert_called_once()
        assert connector.is_stopping()

    def test_stop_before_start_is_safe(self):
        """mqtt_client only exists after start(); stopping earlier must not raise."""
        connector = MQTTConnector(name="MQTT 1", host="broker.example", port=1883, version="3.1.1")
        connector.stop()  # no client yet
        assert connector.is_stopping()

    @patch("connectors.mqtt.Client")
    def test_stop_during_connect_retry_skips_the_network_loop(self, MockClient, caplog):
        """A stop while retrying a refused connection must not enter loop_forever."""
        client = MockClient.return_value
        client.connect.side_effect = OSError("refused")
        connector = MQTTConnector(name="MQTT 1", host="broker.example", port=1883, version="3.1.1")

        with caplog.at_level(logging.INFO):
            thread = run_in_thread(connector.start)
            assert_stops(connector, thread)

        client.loop_forever.assert_not_called()
        assert any("Stop requested before connection" in r.message for r in caplog.records)


class TestLoRaWANConnectorStop:
    """connectors/lora.py — the serial half — is deliberately NOT imported in this file: it
    needs pyserial, and a module-top import of an extra-dependent connector here would abort
    collection for the whole suite on a core-only checkout. Its stop() is covered by
    tests/test_lora_connector.py, behind that file's importorskip. The LoRaWAN connector needs
    no extra (paho is core), so it belongs here, and it inherits MQTTConnector's stop()."""

    def test_it_inherits_the_cooperative_stop(self):
        connector = LoRaWANConnector("lorawan_1", host="lns.example")
        connector.stop()
        assert connector.is_stopping()

    def test_stop_before_start_is_a_no_op(self):
        LoRaWANConnector("lorawan_1", host="lns.example").stop()  # mqtt_client does not exist yet


class TestStorageClose:
    def test_close_all_closes_every_backend(self, storage_manager):
        backends = [StubStorageBackend(f"s{i}") for i in range(3)]
        for backend in backends:
            backend.close = MagicMock()
            storage_manager.register(backend)
        storage_manager.close_all()
        for backend in backends:
            backend.close.assert_called_once()

    def test_close_failure_is_isolated(self, storage_manager, caplog):
        broken, healthy = StubStorageBackend("broken"), StubStorageBackend("healthy")
        broken.close = MagicMock(side_effect=RuntimeError("disk gone"))
        healthy.close = MagicMock()
        storage_manager.register(broken)
        storage_manager.register(healthy)

        with caplog.at_level(logging.ERROR):
            storage_manager.close_all()  # must not raise

        healthy.close.assert_called_once()
        assert any("failed on close" in r.message for r in caplog.records)

    def test_close_all_is_idempotent(self, storage_manager):
        """main calls it from a finally, so a second call must be harmless."""
        backend = StubStorageBackend("s")
        backend.close = MagicMock()
        storage_manager.register(backend)
        storage_manager.close_all()
        storage_manager.close_all()
        backend.close.assert_called_once()

    def test_close_all_with_no_backends(self, storage_manager):
        storage_manager.close_all()  # no backends configured — must be a no-op

    def test_base_backend_close_is_a_no_op(self):
        """Concrete on the ABC so pre-existing backends keep working."""
        StubStorageBackend("s").close()


class TestMainShutdown:
    """main() must always run the shutdown sequence, including on failure."""

    @pytest.fixture
    def app(self, main_app):
        return main_app

    def test_no_connectors_exits_and_closes_storage(self, app):
        app.STORAGE_MANAGER.close_all = MagicMock()
        app.main()
        app.STORAGE_MANAGER.close_all.assert_called_once()

    def test_storage_is_closed_even_when_startup_raises(self, app, caplog):
        app.STORAGE_MANAGER.close_all = MagicMock()
        app.create_classes = MagicMock(side_effect=[set(), RuntimeError("boom")])
        with pytest.raises(RuntimeError):
            app.main()
        app.STORAGE_MANAGER.close_all.assert_called_once()

    def test_signal_handler_only_sets_the_event(self, app):
        """No logging inside the handler — it can deadlock against the logging lock."""
        assert not app.SHUTDOWN_EVENT.is_set()
        app._request_shutdown(15, None)
        assert app.SHUTDOWN_EVENT.is_set()
        assert app.SIGNAL_RECEIVED == 15

    def test_stragglers_are_reported_not_waited_on(self, app, caplog):
        deaf_done = Event()

        class Deaf:
            name = "deaf"

            def start(self):
                deaf_done.wait(5)

            def stop(self):
                pass  # ignores the request

        app.SUPERVISOR.supervise(Deaf(), "start")
        try:
            with caplog.at_level(logging.WARNING):
                app.shutdown()
            assert any("did not stop within" in r.message for r in caplog.records)
        finally:
            deaf_done.set()
