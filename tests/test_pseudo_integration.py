"""End-to-end integration test for the pseudo connector and device system."""

import logging
from unittest.mock import MagicMock

from connectors.pseudo import PseudoConnector
from tests.conftest import (
    StubDevice, build_p1_telegram as build_telegram, make_p1, make_pseudo, write_replay as write_csv,
)


class TestPseudoIntegration:
    def test_full_replay_wiring(self, tmp_path, devices_manager):
        """Replay CSV through PseudoConnector → PseudoDevices → DevicesManager."""
        csv_file = tmp_path / "replay.csv"
        write_csv(csv_file, [
            ("2024-01-15T10:00:00", "sensor_a", "sensors/a", '{"power": 1500}'),
            ("2024-01-15T10:00:05", "actuator_b", "actuators/b", '{"status": "on"}'),
            ("2024-01-15T10:00:10", "sensor_a", "sensors/a", '{"power": 1450}'),
        ])

        # Create pseudo devices
        sensor = make_pseudo("sensor_a")
        actuator = make_pseudo("actuator_b")

        # Register in DevicesManager
        devices_manager.update_device(sensor)
        devices_manager.update_device(actuator)

        # Wire connector
        connector = PseudoConnector("pseudo_conn", replay_file=str(csv_file), speed=0)
        devices = {"sensor_a": sensor, "actuator_b": actuator}
        connector.inject_devices(devices)

        # Run instant replay
        connector.start()

        # Verify devices have data and are ready
        assert sensor.is_data_ready()
        assert actuator.is_data_ready()
        assert sensor._connected_event.is_set()
        assert actuator._connected_event.is_set()

        # Verify last data was stored (sensor_a got 2 updates, last one wins)
        assert sensor.data["parsed"] == {"power": 1450}
        assert sensor.data["topic"] == "sensors/a"

        assert actuator.data["parsed"] == {"status": "on"}
        assert actuator.data["topic"] == "actuators/b"

        # Verify DevicesManager has updated data
        dm_sensor = devices_manager.get_device("sensor_a")
        assert dm_sensor.data["parsed"] == {"power": 1450}

        dm_actuator = devices_manager.get_device("actuator_b")
        assert dm_actuator.data["parsed"] == {"status": "on"}

    def test_control_through_pseudo_connector(self, tmp_path, devices_manager):
        """PseudoDevice.control() delegates to PseudoConnector.send() which logs."""
        csv_file = tmp_path / "replay.csv"
        control_log = tmp_path / "control.log"
        write_csv(csv_file, [
            ("2024-01-15T10:00:00", "plug", "t", "data"),
        ])

        plug = make_pseudo("plug")
        devices_manager.update_device(plug)

        connector = PseudoConnector(
            "pseudo_conn",
            replay_file=str(csv_file),
            speed=0,
            control_log=str(control_log),
        )
        connector.inject_devices({"plug": plug})
        connector.start()

        # Now control the device — this should log via connector
        assert plug.control("turn_on") is True

        content = control_log.read_text()
        assert "plug,turn_on" in content


def write_csv_quoted(path, rows):
    """Write a replay file with proper CSV quoting, so a payload may contain commas,
    quotes or embedded CRLF — which `write_csv` above cannot express."""
    return write_csv(path, rows, quoted=True)


class TestRawPayloadFidelity:
    """The replay hands devices the payload hardware would have sent, byte for byte."""

    def test_multiline_payload_survives_the_csv_round_trip(self, tmp_path):
        payload = build_telegram("1-0:1.8.1(001234.567*kWh)\r\n")
        csv_file = tmp_path / "replay.csv"
        write_csv_quoted(csv_file, [("2024-01-15T10:00:00", "meter", "p1/data", payload)])

        device = StubDevice(name="meter")
        device.receive = MagicMock()
        connector = PseudoConnector("c", replay_file=str(csv_file), speed=0)
        connector.inject_devices({"meter": device})
        connector.start()

        # Verbatim, including the internal line breaks and the terminating CRLF that
        # the parser needs and that a .strip() used to remove.
        device.receive.assert_called_once_with("p1/data", payload)


class TestEmulatedProtocol:
    """A replay stands in for a transport, so real device classes can be backtested."""

    def _p1(self, protocol: str):
        return make_p1("meter", protocol=protocol, listener_options={})

    def test_p1_telegram_replays_end_to_end(self, tmp_path, devices_manager):
        telegram = build_telegram(
            "1-0:1.8.1(001234.567*kWh)\r\n"
            "1-0:1.7.0(00.512*kW)\r\n"
            "0-1:24.2.3(101209112500W)(12785.123*m3)\r\n"
        )
        csv_file = tmp_path / "replay.csv"
        write_csv_quoted(csv_file, [("2024-01-15T10:00:00", "meter", "p1/data", telegram)])

        # Config injects the emulated protocol; here we pass the resolved value directly.
        meter = self._p1("mqtt")
        devices_manager.update_device(meter)
        connector = PseudoConnector("c", replay_file=str(csv_file), speed=0, emulates="mqtt")
        connector.inject_devices({"meter": meter})

        connector.start()

        assert meter.data != {}
        assert meter.get_metrics() == {
            "energy_import_t1_kwh": 1234.567,
            "power_import_kw": 0.512,
            "gas_m3": 12785.123,
        }

    def test_without_emulates_the_device_refuses_the_payload(self, tmp_path, devices_manager, caplog):
        """The failure this option exists to remove: a device sees protocol "pseudo", finds
        no branch for it, and refuses every row.

        The refusal is now stated once at construction, on the main thread, rather than
        discovered per row on the connector's. It used to reach the log only because
        `P1.receive` raised `NotImplementedError` and `PseudoConnector.start` caught it as
        "Error replaying entry" — which said a replay entry was bad when the entry was fine
        and the wiring was not, and which said nothing at all behind a connector whose
        dispatch has no `except`.
        """
        csv_file = tmp_path / "replay.csv"
        write_csv_quoted(csv_file, [("2024-01-15T10:00:00", "meter", "p1/data", "anything")])

        with caplog.at_level(logging.ERROR):
            meter = self._p1("pseudo")  # what Config injects with no `emulates`
        startup_errors = [r.message for r in caplog.records if r.levelname == "ERROR"]
        assert len(startup_errors) == 1
        assert "emulates" in startup_errors[0], "the message must name the key that fixes it"

        connector = PseudoConnector("c", replay_file=str(csv_file), speed=0)
        connector.inject_devices({"meter": meter})

        caplog.clear()
        with caplog.at_level(logging.ERROR):
            connector.start()

        assert meter.data == {}
        assert "Error replaying entry" not in caplog.text, (
            "the replay file is fine; blaming the entry sent operators to the wrong file"
        )
        assert [r for r in caplog.records if r.levelname == "ERROR"] == []
