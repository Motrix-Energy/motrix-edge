"""Backtest determinism: correct event stamps, and a replay that runs lockstep.

Two defects this pins down. Storage used to stamp every replayed reading with the
*previous* timestep (and the first with wall-clock now()), because the single clock was
advanced after dispatch. And publishing a step was followed by a 0.01s sleep rather than
a barrier, so a main() slower than that let the replay run ahead and silently skip
timesteps.
"""
import logging
import threading
from datetime import datetime, timedelta

import pytest

from api.algorithm import Algorithm
from api.storage_backend import StorageBackend
from connectors.pseudo import PseudoConnector
from devices.pseudo import Pseudo
from devices_manager.devices_manager import DevicesManager
from simulation.clock import SimulationClock
from supervisor.supervisor import RestartPolicy, Supervisor
from tests.conftest import make_pseudo, run_in_thread, wait_until, write_replay as write_replay_csv

START = datetime(2024, 1, 15, 10, 0, 0)
STEP = timedelta(minutes=15)
JOIN_TIMEOUT = 5.0


def timesteps(count: int) -> list[datetime]:
    return [START + i * STEP for i in range(count)]


def write_replay(path, rows: list[tuple[datetime, str]]) -> str:
    """rows are (timestamp, device_name); payload carries the timestamp for traceability."""
    return write_replay_csv(path, [
        (moment.isoformat(), device_name, "sensors/x", f'{{"at": "{moment.isoformat()}"}}')
        for moment, device_name in rows
    ], quoted=True)


def make_device(name: str) -> Pseudo:
    return make_pseudo(name, connector_options={"name": "c", "protocol": "pseudo"})


def wire(replay_file: str, device_names: list[str], **kwargs) -> tuple[PseudoConnector, dict]:
    devices = {name: make_device(name) for name in device_names}
    connector = PseudoConnector("c", replay_file=replay_file, speed=0, **kwargs)
    connector.inject_devices(devices)
    for device in devices.values():
        DevicesManager().update_device(device)
    return connector, devices


class ClockRecordingBackend(StorageBackend):
    """Records which clock each write saw, which is the whole question here."""

    def __init__(self, name: str = "recorder") -> None:
        super().__init__(name)
        self.readings: list[tuple[str, datetime, datetime | None]] = []
        self.decisions: list[tuple[str, datetime]] = []

    def write_device_data(self, device, data) -> None:
        self.readings.append((device.name, self._data_timestamp(), SimulationClock().get_step_time()))

    def write_algorithm_decision(self, algorithm, device, command) -> None:
        self.decisions.append((command, self._decision_timestamp()))

    def read(self, device, start, end) -> list[dict]:
        return []


class SteppingAlgorithm(Algorithm):
    """Records the simulated moment of every step, optionally slowly."""

    def __init__(self, name, devices_manager, work_seconds: float = 0.0, **kwargs):
        super().__init__(name, devices_manager, **kwargs)
        self.work_seconds = work_seconds
        self.steps: list[datetime] = []

    def main(self) -> None:
        super().main()
        moment = self.devices_manager.get_simulation_time()
        if moment is None:
            return  # the replay ended and we fell back to the wall-clock cadence
        if self.work_seconds:
            threading.Event().wait(self.work_seconds)
        self.steps.append(moment)


def await_join(name: str) -> None:
    """Block until the algorithm has registered, so a published step cannot precede it."""
    if not wait_until(lambda: name in SimulationClock().participants(), JOIN_TIMEOUT):
        pytest.fail(f"algorithm '{name}' never joined the clock")


class TestEventStamps:
    def test_every_reading_is_stamped_with_its_own_timestep(self, tmp_path, devices_manager, storage_manager):
        """The regression: readings used to be filed under the previous timestep."""
        moments = timesteps(4)
        rows = [(moment, name) for moment in moments for name in ("a", "b")]
        connector, _ = wire(write_replay(tmp_path / "r.csv", rows), ["a", "b"])
        backend = ClockRecordingBackend()
        storage_manager.register(backend)

        connector.start()

        assert [stamp for _, stamp, _ in backend.readings] == [moment for moment, _ in rows]

    def test_the_first_reading_is_not_wall_clock(self, tmp_path, devices_manager, storage_manager):
        """It used to fall back to datetime.now() — a 2026 point among 2024 data."""
        connector, _ = wire(write_replay(tmp_path / "r.csv", [(START, "a")]), ["a"])
        backend = ClockRecordingBackend()
        storage_manager.register(backend)

        connector.start()

        assert backend.readings[0][1] == START

    def test_the_step_clock_lags_dispatch_within_a_timestep(self, tmp_path, devices_manager, storage_manager):
        """Timestep atomicity: the step only commits once every device of it is updated,
        so an algorithm woken by it never sees a half-updated device set."""
        moments = timesteps(3)
        rows = [(moment, name) for moment in moments for name in ("a", "b")]
        connector, _ = wire(write_replay(tmp_path / "r.csv", rows), ["a", "b"])
        backend = ClockRecordingBackend()
        storage_manager.register(backend)

        connector.start()

        # Both writes of timestep i see the step clock still holding timestep i-1.
        step_clocks = [step for _, _, step in backend.readings]
        assert step_clocks == [None, None, moments[0], moments[0], moments[1], moments[1]]

    def test_unknown_device_as_the_last_row_still_commits_the_timestep(self, tmp_path, devices_manager):
        """The `continue` used to jump the boundary block, stalling every algorithm."""
        rows = [(START, "a"), (START, "ghost"), (START + STEP, "a")]
        connector, _ = wire(write_replay(tmp_path / "r.csv", rows), ["a"])
        published: list[datetime] = []
        original = SimulationClock().publish_step
        SimulationClock().publish_step = lambda moment: (published.append(moment), original(moment))[1]

        connector.start()

        assert published == [START, START + STEP]


class TestLockstep:
    def test_no_timestep_is_skipped_by_a_slow_algorithm(self, tmp_path, devices_manager):
        """Without the barrier the replay outruns main() and the algorithm sees only
        whatever the clock happens to hold when it next looks."""
        moments = timesteps(5)
        connector, _ = wire(write_replay(tmp_path / "r.csv", [(m, "a") for m in moments]), ["a"])
        algo = SteppingAlgorithm("algo", devices_manager, work_seconds=0.05)
        algo_thread = run_in_thread(algo.loop)
        await_join("algo")

        connector.start()
        algo.stop()
        algo_thread.join(5.0)

        assert algo.steps == moments

    def test_the_first_timestep_is_not_missed_by_a_late_starting_algorithm(self, tmp_path, devices_manager):
        """main.py supervises connectors before algorithms, so the replay can reach its
        first step before an algorithm's thread is running. Being a participant from
        construction — not from the first loop iteration — is what closes that window."""
        moments = timesteps(4)
        connector, _ = wire(write_replay(tmp_path / "r.csv", [(m, "a") for m in moments]), ["a"])
        algo = SteppingAlgorithm("algo", devices_manager)  # constructed, not yet looping

        connector_thread = run_in_thread(connector.start)
        # Wait for the replay to actually reach the barrier rather than guessing at it:
        # a published step with no participants is exactly the window under test.
        assert wait_until(lambda: SimulationClock().get_step_time() is not None, JOIN_TIMEOUT)
        algo_thread = run_in_thread(algo.loop)
        connector_thread.join(10.0)
        algo.stop()
        algo_thread.join(5.0)

        assert algo.steps == moments

    def test_replay_without_algorithms_does_not_block(self, tmp_path, devices_manager):
        moments = timesteps(20)
        connector, _ = wire(write_replay(tmp_path / "r.csv", [(m, "a") for m in moments]), ["a"])

        started = datetime.now()
        connector.start()

        assert (datetime.now() - started).total_seconds() < 2.0

    def test_a_stopped_algorithm_releases_the_replay(self, tmp_path, devices_manager):
        moments = timesteps(4)
        connector, _ = wire(write_replay(tmp_path / "r.csv", [(m, "a") for m in moments]), ["a"],
                            step_timeout_seconds=1.0)
        algo = SteppingAlgorithm("algo", devices_manager)
        algo_thread = run_in_thread(algo.loop)
        await_join("algo")
        algo.stop()
        algo_thread.join(5.0)

        started = datetime.now()
        connector.start()  # must not sit out the per-step timeout on every step

        assert (datetime.now() - started).total_seconds() < 5.0

    def test_timeout_advances_the_replay_and_names_the_laggard(self, tmp_path, devices_manager, caplog):
        moments = timesteps(3)
        connector, _ = wire(write_replay(tmp_path / "r.csv", [(m, "a") for m in moments]), ["a"],
                            step_timeout_seconds=0.05)
        wedged = threading.Event()
        algo = SteppingAlgorithm("algo", devices_manager)
        algo.main = lambda: wedged.wait(30.0)  # never finishes its step
        algo_thread = run_in_thread(algo.loop)
        await_join("algo")

        try:
            with caplog.at_level(logging.WARNING):
                connector.start()  # must finish rather than hang
        finally:
            wedged.set()
            algo.stop()
            algo_thread.join(5.0)

        levels = [r.levelno for r in caplog.records if "did not finish step" in r.message]
        assert levels[:3] == [logging.ERROR, logging.WARNING, logging.WARNING]
        assert "['algo']" in caplog.text

    def test_zero_timeout_disables_lockstep(self, tmp_path, devices_manager):
        moments = timesteps(3)
        connector, _ = wire(write_replay(tmp_path / "r.csv", [(m, "a") for m in moments]), ["a"],
                            step_timeout_seconds=0)
        wedged = threading.Event()
        algo = SteppingAlgorithm("algo", devices_manager)
        algo.main = lambda: wedged.wait(30.0)
        algo_thread = run_in_thread(algo.loop)
        await_join("algo")

        try:
            started = datetime.now()
            connector.start()  # fire and forget: never waits for the wedged algorithm
            assert (datetime.now() - started).total_seconds() < 2.0
        finally:
            wedged.set()
            algo.stop()
            algo_thread.join(5.0)


class TestDecisionStamps:
    def test_decisions_carry_the_step_being_processed(self, tmp_path, devices_manager, storage_manager):
        moments = timesteps(3)
        connector, devices = wire(write_replay(tmp_path / "r.csv", [(m, "a") for m in moments]), ["a"])
        backend = ClockRecordingBackend()
        storage_manager.register(backend)

        class Deciding(SteppingAlgorithm):
            def main(self) -> None:
                super().main()
                if self.devices_manager.get_simulation_time() is not None:
                    self.control_device(devices["a"], "on")

        algo = Deciding("algo", devices_manager)
        algo_thread = run_in_thread(algo.loop)
        await_join("algo")

        connector.start()
        algo.stop()
        algo_thread.join(5.0)

        assert [stamp for _, stamp in backend.decisions] == moments


class TestReplayEnd:
    def test_completion_clears_both_clocks(self, tmp_path, devices_manager):
        connector, _ = wire(write_replay(tmp_path / "r.csv", [(START, "a")]), ["a"])
        connector.start()
        clock = SimulationClock()
        assert clock.get_step_time() is None
        assert clock.get_event_time() is None
        assert clock.is_simulated() is False


class CrashingAlgorithm(SteppingAlgorithm):
    """Records every step it is handed, then raises on the ones listed in `crash_on`
    (zero-based positions in the order it was handed them) — or on every step."""

    def __init__(self, name, devices_manager, crash_on: set[int] | None = None, **kwargs):
        super().__init__(name, devices_manager, **kwargs)
        self.crash_on = crash_on
        self.calls: list[datetime | None] = []  # every main(), replayed or wall-clock

    def main(self) -> None:
        self.calls.append(self.devices_manager.get_simulation_time())
        super().main()
        if self.devices_manager.get_simulation_time() is None:
            return
        if self.crash_on is None or len(self.steps) - 1 in self.crash_on:
            raise RuntimeError(f"crash on step {len(self.steps) - 1}")


def replay_under_supervision(connector, algorithm, policy, timeout: float = 10.0) -> None:
    """Run the replay to its end with the algorithm supervised as main.py does, then stop."""
    supervisor = Supervisor(policy)
    supervisor.supervise(algorithm, "loop")
    replay = run_in_thread(connector.start)
    replay.join(timeout)
    stranded = replay.is_alive()
    supervisor.stop_all(timeout=5.0)
    if stranded:
        connector.stop()
        replay.join(5.0)
        pytest.fail("the replay never finished: a crashed algorithm stranded it")


class TestACrashedAlgorithmIsWaitedFor:
    """A crash costs a backtest time, never a timestep.

    A crashing main() used to take the algorithm off the barrier until the supervisor
    restarted it, so at speed 0 the replay ran on alone through the backoff and the
    restarted algorithm resumed wherever the replay happened to be: how many timesteps it
    never saw depended on wall-clock timing. It now stays a participant through the backoff,
    the replay waits at the next timestep, and only a worker the supervisor gives up on
    leaves — so a crash can hold a replay up but never strand it.
    """

    def test_no_timestep_is_skipped_across_a_crash(self, tmp_path, devices_manager, caplog):
        moments = timesteps(6)
        connector, _ = wire(write_replay(tmp_path / "r.csv", [(m, "a") for m in moments]), ["a"],
                            step_timeout_seconds=10.0)
        algo = CrashingAlgorithm("algo", devices_manager, crash_on={1})
        # A backoff far longer than the rest of the replay takes at speed 0.
        with caplog.at_level(logging.WARNING):
            replay_under_supervision(connector, algo, RestartPolicy(backoff_seconds=0.5))

        # Every timestep exactly once: the one it died on is not re-run, none is skipped.
        assert algo.steps == moments
        assert not any("did not finish step" in record.message for record in caplog.records)

    def test_a_restart_budget_spent_on_every_step_does_not_strand_the_replay(self, tmp_path, devices_manager):
        moments = timesteps(6)
        connector, _ = wire(write_replay(tmp_path / "r.csv", [(m, "a") for m in moments]), ["a"],
                            step_timeout_seconds=None)  # would wait forever for a participant that never leaves
        algo = CrashingAlgorithm("algo", devices_manager)
        replay_under_supervision(connector, algo, RestartPolicy(max_restarts=2, backoff_seconds=0.05))

        # The first run and its two restarts each took the next timestep in turn; then the
        # supervisor gave up, retired it, and the replay finished without it.
        assert algo.steps == moments[:3]
        assert "algo" not in SimulationClock().participants()

    def test_disabled_restarts_release_the_replay_at_the_first_crash(self, tmp_path, devices_manager):
        moments = timesteps(4)
        connector, _ = wire(write_replay(tmp_path / "r.csv", [(m, "a") for m in moments]), ["a"],
                            step_timeout_seconds=None)
        algo = CrashingAlgorithm("algo", devices_manager, crash_on={0})
        replay_under_supervision(connector, algo, RestartPolicy(enabled=False))
        assert algo.steps == moments[:1]

    def test_a_crashed_algorithm_is_a_participant_until_retired(self, tmp_path, devices_manager):
        """The state the replay waits on, observed directly: down in its backoff, the
        algorithm is still owed the next timestep."""
        moments = timesteps(3)
        connector, _ = wire(write_replay(tmp_path / "r.csv", [(m, "a") for m in moments]), ["a"],
                            step_timeout_seconds=None)
        algo = CrashingAlgorithm("algo", devices_manager, crash_on={0})
        supervisor = Supervisor(RestartPolicy(backoff_seconds=30.0))  # stays down for the test
        supervisor.supervise(algo, "loop")
        replay = run_in_thread(connector.start)
        try:
            clock = SimulationClock()
            assert wait_until(lambda: clock.generation() == 2, JOIN_TIMEOUT), "the replay did not publish the next step"
            assert clock.pending() == ["algo"]  # the replay is waiting for the restart
            assert replay.is_alive()
        finally:
            supervisor.stop_all(timeout=5.0)  # a stop during backoff: retired, and the replay released
            replay.join(JOIN_TIMEOUT)
        assert not replay.is_alive()
        assert "algo" not in SimulationClock().participants()

    def test_a_crash_on_the_final_timestep_adds_no_wall_clock_step(self, tmp_path, devices_manager):
        """The restarted loop() continues the same replay, so it must know it was being
        replayed. It used to start that bookkeeping afresh: restarting into the clock the
        finished replay had reset, it took itself for live and ran one more main() stamped
        with the wall clock — or, with another connector keeping main alive, ran live for good."""
        moments = timesteps(4)
        connector, _ = wire(write_replay(tmp_path / "r.csv", [(m, "a") for m in moments]), ["a"],
                            step_timeout_seconds=10.0)
        algo = CrashingAlgorithm("algo", devices_manager, crash_on={3})
        supervisor = Supervisor(RestartPolicy(backoff_seconds=0.05))  # restarts before main would notice the end
        worker = supervisor.supervise(algo, "loop")
        try:
            connector.start()
            # The restart must find the replay over and return, not settle into a live cadence.
            assert wait_until(worker.is_finished, JOIN_TIMEOUT), "the restarted algorithm kept running after the replay"
        finally:
            supervisor.stop_all(timeout=5.0)

        assert algo.steps == moments
        assert None not in algo.calls, "a main() ran on the wall clock after the replay ended"
        assert worker.restarts == 1

