"""The readiness gate: an algorithm waits for its required devices, then proceeds anyway.

Every case here is "does not raise" *and* something observable in the log — the gate's
whole contract is that it never blocks startup permanently and always says why.
"""
import logging
from threading import Event
from time import monotonic

import pytest

from tests.conftest import STOP_TIMEOUT, StubAlgorithm, StubDevice, assert_stops, make_devices_access, run_in_thread


def ready_device(name: str) -> StubDevice:
    device = StubDevice(name=name)
    device.mark_data_ready()
    device.mark_connected()
    return device


class TestAlgorithmWait:
    def test_no_required_devices_short_circuits(self):
        manager = make_devices_access()
        algo = StubAlgorithm("test", manager, required_devices=[])

        algo._wait_for_required_devices()

        # Not merely "did not raise": with nothing required it must not even ask.
        manager.get_devices.assert_not_called()

    def test_missing_device_is_reported_and_the_gate_opens(self, caplog):
        manager = make_devices_access(devices={})
        algo = StubAlgorithm("test", manager, required_devices=["missing_device"])

        with caplog.at_level(logging.ERROR):
            algo._wait_for_required_devices()

        assert any("Required device 'missing_device' not found" in r.message for r in caplog.records)

    def test_ready_device_passes_without_a_timeout_warning(self, caplog):
        manager = make_devices_access(devices={"meter": ready_device("meter")})
        algo = StubAlgorithm("test", manager, required_devices=["meter"], wait_for_devices_timeout=1.0)

        with caplog.at_level(logging.INFO):
            algo._wait_for_required_devices()

        assert any("Waiting for 'meter'" in r.message for r in caplog.records)
        assert not any("Timeout waiting" in r.message for r in caplog.records)

    def test_unready_device_times_out_and_the_gate_still_opens(self, caplog):
        manager = make_devices_access(devices={"meter": StubDevice(name="meter")})
        algo = StubAlgorithm("test", manager, required_devices=["meter"], wait_for_devices_timeout=0.01)

        with caplog.at_level(logging.WARNING):
            algo._wait_for_required_devices()

        assert any("Timeout waiting for 'meter', proceeding anyway" in r.message for r in caplog.records)

    def test_every_required_device_is_waited_on(self, caplog):
        manager = make_devices_access(devices={"meter": ready_device("meter"), "plug": ready_device("plug")})
        algo = StubAlgorithm("test", manager, required_devices=["meter", "plug"], wait_for_devices_timeout=1.0)

        with caplog.at_level(logging.INFO):
            algo._wait_for_required_devices()

        waited = {r.message for r in caplog.records if "Waiting for" in r.message}
        assert waited == {"Waiting for 'meter'...", "Waiting for 'plug'..."}
        assert not any("Timeout waiting" in r.message for r in caplog.records)

    def test_one_laggard_does_not_suppress_the_others(self, caplog):
        """The ready device must still be waited on, and only the laggard warns."""
        manager = make_devices_access(devices={"meter": ready_device("meter"), "plug": StubDevice(name="plug")})
        algo = StubAlgorithm("test", manager, required_devices=["meter", "plug"], wait_for_devices_timeout=0.01)

        with caplog.at_level(logging.INFO):
            algo._wait_for_required_devices()

        timeouts = [r.message for r in caplog.records if "Timeout waiting" in r.message]
        assert timeouts == ["Timeout waiting for 'plug', proceeding anyway"]


class TestStopCutsTheGateShort:
    """The gate is where a process with absent hardware spends its startup, so a stop
    arriving mid-wait has to be noticed inside a poll slice rather than at the deadline."""

    @staticmethod
    def entered_gate(device: StubDevice) -> Event:
        """An Event set when the gate actually reaches `device`'s readiness wait.

        Without it, a stop() winning the race to the thread would be answered by the
        is_stopping() check on the way in, and the test would pass having waited on nothing.
        """
        entered = Event()
        underlying = device.wait_until_ready

        def wait_until_ready(timeout=None):
            entered.set()
            return underlying(timeout=timeout)

        device.wait_until_ready = wait_until_ready
        return entered

    def test_stop_cuts_a_finite_timeout_short(self, caplog):
        """A timeout far above the assert's: only polling can return inside it."""
        device = StubDevice(name="meter")
        entered = self.entered_gate(device)
        algo = StubAlgorithm("test", make_devices_access(devices={"meter": device}),
                             required_devices=["meter"], wait_for_devices_timeout=3600.)

        with caplog.at_level(logging.WARNING):
            thread = run_in_thread(algo._wait_for_required_devices)
            assert entered.wait(STOP_TIMEOUT), "the gate never reached the readiness wait"
            assert_stops(algo, thread)

        # A shutdown is not a timeout. The device never became ready, but by then nobody
        # was waiting for it, and saying otherwise sends people hunting for dead hardware.
        assert not any("Timeout waiting" in r.message for r in caplog.records)

    def test_a_finite_timeout_still_runs_its_full_length(self, caplog, monkeypatch):
        """Slicing must not cut the wait down to a single slice.

        The poll interval is shrunk so that spanning several slices costs milliseconds;
        what matters is that the gate spans more than one of them.
        """
        monkeypatch.setattr("api.algorithm._READINESS_POLL_SECONDS", 0.01)
        algo = StubAlgorithm("test", make_devices_access(devices={"meter": StubDevice(name="meter")}),
                             required_devices=["meter"], wait_for_devices_timeout=0.05)

        started = monotonic()
        with caplog.at_level(logging.WARNING):
            algo._wait_for_required_devices()
        elapsed = monotonic() - started

        # 2 x 0.05, one wait for ready and one for connected, less a scheduler tolerance.
        assert elapsed >= 0.08, f"gave up after {elapsed:.3f}s of a 0.05s timeout, twice"
        assert any("Timeout waiting for 'meter', proceeding anyway" in r.message for r in caplog.records)


@pytest.mark.parametrize("required", [[], ["absent"]])
def test_gate_never_raises(required):
    """The contract in one line: whatever the config asks for, startup continues."""
    StubAlgorithm("test", make_devices_access(), required_devices=required,
                  wait_for_devices_timeout=0.01)._wait_for_required_devices()
