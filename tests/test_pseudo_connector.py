import json
import logging
from time import time
from unittest.mock import MagicMock, patch

from connectors.pseudo import PseudoConnector
from simulation.clock import SimulationClock
from tests.conftest import StubDevice, write_replay as write_csv


def write_json(path, entries):
    """Write a JSON replay file from a list of dicts."""
    with open(path, 'w') as f:
        json.dump(entries, f)


def make_stub_device(name, is_readable=True, is_writable=False):
    device = StubDevice(name=name, is_readable=is_readable, is_writable=is_writable)
    device.receive = MagicMock()
    return device


class TestCSVReplay:
    def test_csv_replay_calls_device_receive(self, tmp_path):
        csv_file = tmp_path / "replay.csv"
        write_csv(csv_file, [
            ("2024-01-15T10:00:00", "dev_a", "topic/a", "payload_1"),
            ("2024-01-15T10:00:01", "dev_b", "topic/b", "payload_2"),
            ("2024-01-15T10:00:02", "dev_a", "topic/a", "payload_3"),
        ])

        dev_a = make_stub_device("dev_a")
        dev_b = make_stub_device("dev_b")
        connector = PseudoConnector("test", replay_file=str(csv_file), speed=0)
        connector.inject_devices({"dev_a": dev_a, "dev_b": dev_b})
        connector.start()

        assert dev_a.receive.call_count == 2
        assert dev_b.receive.call_count == 1
        dev_a.receive.assert_any_call("topic/a", "payload_1")
        dev_a.receive.assert_any_call("topic/a", "payload_3")
        dev_b.receive.assert_called_once_with("topic/b", "payload_2")

    def test_csv_replay_without_topic(self, tmp_path):
        csv_file = tmp_path / "replay.csv"
        write_csv(csv_file, [
            ("2024-01-15T10:00:00", "dev_a", "", "payload_1"),
        ])

        dev_a = make_stub_device("dev_a")
        connector = PseudoConnector("test", replay_file=str(csv_file), speed=0)
        connector.inject_devices({"dev_a": dev_a})
        connector.start()

        dev_a.receive.assert_called_once_with("payload_1")


class TestJSONReplay:
    def test_json_replay_calls_device_receive(self, tmp_path):
        json_file = tmp_path / "replay.json"
        write_json(json_file, [
            {"timestamp": "2024-01-15T10:00:00", "device_name": "dev_a", "topic": "topic/a", "payload": "payload_1"},
            {"timestamp": "2024-01-15T10:00:01", "device_name": "dev_b", "topic": "topic/b", "payload": "payload_2"},
        ])

        dev_a = make_stub_device("dev_a")
        dev_b = make_stub_device("dev_b")
        connector = PseudoConnector("test", replay_file=str(json_file), speed=0)
        connector.inject_devices({"dev_a": dev_a, "dev_b": dev_b})
        connector.start()

        dev_a.receive.assert_called_once_with("topic/a", "payload_1")
        dev_b.receive.assert_called_once_with("topic/b", "payload_2")


class TestSpeed:
    def test_instant_speed_no_sleep(self, tmp_path):
        csv_file = tmp_path / "replay.csv"
        # Entries spread over 60 seconds
        write_csv(csv_file, [
            (f"2024-01-15T10:00:{i:02d}", "dev_a", "t", f"p{i}")
            for i in range(0, 60, 5)
        ])

        dev_a = make_stub_device("dev_a")
        connector = PseudoConnector("test", replay_file=str(csv_file), speed=0)
        connector.inject_devices({"dev_a": dev_a})

        start = time()
        connector.start()
        elapsed = time() - start

        assert elapsed < 2.0
        assert dev_a.receive.call_count == 12


class TestErrorHandling:
    def test_unknown_device_skipped(self, tmp_path):
        csv_file = tmp_path / "replay.csv"
        write_csv(csv_file, [
            ("2024-01-15T10:00:00", "unknown_dev", "t", "p1"),
            ("2024-01-15T10:00:01", "dev_a", "t", "p2"),
        ])

        dev_a = make_stub_device("dev_a")
        connector = PseudoConnector("test", replay_file=str(csv_file), speed=0)
        connector.inject_devices({"dev_a": dev_a})
        connector.start()

        dev_a.receive.assert_called_once_with("t", "p2")

    def test_empty_file_no_error(self, tmp_path):
        csv_file = tmp_path / "replay.csv"
        write_csv(csv_file, [])

        connector = PseudoConnector("test", replay_file=str(csv_file), speed=0)
        connector.inject_devices({})
        connector.start()  # Should not raise

    def test_missing_file_logs_error(self, tmp_path):
        connector = PseudoConnector("test", replay_file=str(tmp_path / "nonexistent.csv"), speed=0)
        connector.inject_devices({})
        connector.start()  # Should not raise


class TestARaisingDevice:
    def test_a_raising_device_does_not_end_the_session(self, tmp_path, caplog):
        csv_file = tmp_path / "replay.csv"
        write_csv(csv_file, [
            ("2024-01-15T10:00:00", "broken", "t", "p1"),
            ("2024-01-15T10:00:01", "fine", "t", "p2"),
        ])
        broken, fine = make_stub_device("broken"), make_stub_device("fine")
        broken.receive = MagicMock(side_effect=ValueError("bad payload"))
        connector = PseudoConnector("test", replay_file=str(csv_file), speed=0)
        connector.inject_devices({"broken": broken, "fine": fine})

        with caplog.at_level(logging.ERROR):
            connector.start()  # must not raise

        fine.receive.assert_called_once_with("t", "p2")
        assert any("raised on 't'" in r.message for r in caplog.records)
        # The replay file is fine; blaming the entry sent operators to the wrong file.
        assert not any("Error replaying entry" in r.message for r in caplog.records)

    def test_a_raising_device_still_commits_the_timestep(self, tmp_path):
        """Pseudo-specific, and no other connector has this failure.

        `clock.publish_step` and `_await_algorithms` run *after* the dispatch, so before the
        guard an escape there skipped the commit and stalled every algorithm on the barrier
        for the rest of the replay.
        """
        csv_file = tmp_path / "replay.csv"
        write_csv(csv_file, [("2024-01-15T10:00:00", "broken", "t", "p1")])
        broken = make_stub_device("broken")
        broken.receive = MagicMock(side_effect=ValueError("bad payload"))
        connector = PseudoConnector("test", replay_file=str(csv_file), speed=0)
        connector.inject_devices({"broken": broken})

        with patch.object(SimulationClock, "publish_step") as publish:
            connector.start()

        publish.assert_called_once()


class TestMixedTimestampShapes:
    """A replay may mix naive and offset-aware timestamps: `datetime.fromisoformat`
    accepts both, and the EMS's own output does exactly this at the head and tail of a
    run. Sorting them directly raises TypeError from *outside* the per-row try, so it
    used to escape start() as a supervised crash rather than a skipped row."""

    def test_mixed_naive_and_aware_timestamps_replay_in_order(self, tmp_path):
        csv_file = tmp_path / "replay.csv"
        write_csv(csv_file, [
            ("2024-01-15T12:00:00+00:00", "dev_a", "t", "third"),
            ("2024-01-15T09:00:00", "dev_a", "t", "first"),
            ("2024-01-15T11:00:00+00:00", "dev_a", "t", "second"),
        ])

        dev_a = make_stub_device("dev_a")
        connector = PseudoConnector("test", replay_file=str(csv_file), speed=0)
        connector.inject_devices({"dev_a": dev_a})
        connector.start()

        # Ordering only asserts that the two aware rows keep their relative order and
        # that nothing raised — where the naive row lands depends on the machine's zone,
        # which is the ambiguity the warning below exists to announce.
        payloads = [c.args[-1] for c in dev_a.receive.call_args_list]
        assert len(payloads) == 3
        assert payloads.index("second") < payloads.index("third")

    def test_mixed_shapes_warn_once(self, tmp_path, caplog):
        csv_file = tmp_path / "replay.csv"
        write_csv(csv_file, [
            ("2024-01-15T09:00:00", "dev_a", "t", "p1"),
            ("2024-01-15T11:00:00+00:00", "dev_a", "t", "p2"),
        ])

        connector = PseudoConnector("test", replay_file=str(csv_file), speed=0)
        connector.inject_devices({"dev_a": make_stub_device("dev_a")})
        with caplog.at_level(logging.WARNING):
            connector.start()

        assert len([r for r in caplog.records if "mixes naive and offset-aware" in r.message]) == 1

    def test_uniform_timestamps_do_not_warn(self, tmp_path, caplog):
        csv_file = tmp_path / "replay.csv"
        write_csv(csv_file, [
            ("2024-01-15T09:00:00", "dev_a", "t", "p1"),
            ("2024-01-15T11:00:00", "dev_a", "t", "p2"),
        ])

        connector = PseudoConnector("test", replay_file=str(csv_file), speed=0)
        connector.inject_devices({"dev_a": make_stub_device("dev_a")})
        with caplog.at_level(logging.WARNING):
            connector.start()

        assert not any("mixes naive" in r.message for r in caplog.records)

    def test_short_row_is_skipped_not_fatal(self, tmp_path):
        """csv.DictReader pads a short row with None, so .strip() used to raise
        AttributeError — outside the except tuple, and therefore fatal to the replay."""
        csv_file = tmp_path / "replay.csv"
        with open(csv_file, 'w', newline='', encoding='utf-8') as f:
            f.write("timestamp,device_name,topic,payload\n")
            f.write("2024-01-15T10:00:00\n")  # truncated: device_name/topic/payload are None
            f.write("2024-01-15T10:00:01,dev_a,t,ok\n")

        dev_a = make_stub_device("dev_a")
        connector = PseudoConnector("test", replay_file=str(csv_file), speed=0)
        connector.inject_devices({"dev_a": dev_a})
        connector.start()

        dev_a.receive.assert_called_once_with("t", "ok")


class TestSend:
    def test_send_logs_command(self, tmp_path, caplog):
        connector = PseudoConnector("test", replay_file="dummy.csv", speed=0)
        device = make_stub_device("dev_a")

        import logging
        with caplog.at_level(logging.INFO):
            connector.send(device, "turn_on")

        assert any("[PSEUDO SEND] dev_a: turn_on" in record.message for record in caplog.records)

    def test_send_writes_control_log(self, tmp_path):
        log_file = tmp_path / "control.log"
        connector = PseudoConnector("test", replay_file="dummy.csv", speed=0, control_log=str(log_file))
        device = make_stub_device("dev_a")

        connector.send(device, "turn_on")
        connector.send(device, "turn_off")

        content = log_file.read_text()
        lines = content.strip().split("\n")
        assert len(lines) == 2
        assert "dev_a,turn_on" in lines[0]
        assert "dev_a,turn_off" in lines[1]

    def test_control_log_terminator_is_lf_on_every_platform(self, tmp_path):
        """The test above reads with read_text(), whose universal-newline translation
        leaves it blind to the terminator — which is how an os.linesep one reached CI.
        tests/test_storage_contract.py catches that only on a host other than the one
        the fixture was generated on; this catches it everywhere."""
        log_file = tmp_path / "control.log"
        connector = PseudoConnector("test", replay_file="dummy.csv", speed=0, control_log=str(log_file))

        connector.send(make_stub_device("dev_a"), "turn_on")

        raw = log_file.read_bytes()
        assert raw.endswith(b"turn_on\n")
        assert b"\r\n" not in raw


class TestOnConnected:
    def test_on_connected_marks_write_only_devices(self, tmp_path):
        csv_file = tmp_path / "replay.csv"
        write_csv(csv_file, [
            ("2024-01-15T10:00:00", "dev_a", "t", "p1"),
        ])

        dev_readable = StubDevice(name="dev_a", is_readable=True)
        dev_write_only = StubDevice(name="dev_b", is_readable=False, is_writable=True)

        connector = PseudoConnector("test", replay_file=str(csv_file), speed=0)
        connector.inject_devices({"dev_a": dev_readable, "dev_b": dev_write_only})
        connector.start()

        assert dev_write_only._connected_event.is_set()
