"""The two-clock split and the lockstep barrier that keeps a backtest deterministic."""
import threading
from datetime import datetime

from simulation.clock import SimulationClock, Step
from tests.conftest import run_in_thread

T0 = datetime(2024, 1, 15, 10, 0, 0)
T1 = datetime(2024, 1, 15, 10, 15, 0)

def NEVER_STOPPING() -> bool:
    """The `is_stopping` callback for a clock test that never shuts down."""
    return False


class TestClocks:
    def test_starts_empty_and_unsimulated(self):
        clock = SimulationClock()
        assert clock.get_event_time() is None
        assert clock.get_step_time() is None
        assert clock.is_simulated() is False

    def test_event_and_step_clocks_are_independent(self):
        # The whole reason there are two: during a timestep's dispatch the event clock
        # is already at T while the step clock still holds the last committed step.
        clock = SimulationClock()
        clock.publish_step(T0)
        clock.set_event_time(T1)
        assert clock.get_event_time() == T1
        assert clock.get_step_time() == T0

    def test_event_time_falls_back_to_step_time(self):
        # A connector that only publishes steps still stamps readings sensibly.
        clock = SimulationClock()
        clock.publish_step(T0)
        assert clock.get_event_time() == T0

    def test_publish_step_marks_the_run_simulated(self):
        clock = SimulationClock()
        clock.publish_step(T0)
        assert clock.is_simulated() is True

    def test_start_simulation_precedes_any_step(self):
        # PseudoConnector declares this in __init__ so no algorithm can slip into the
        # wall-clock branch during the startup window.
        clock = SimulationClock()
        clock.start_simulation()
        assert clock.is_simulated() is True
        assert clock.get_step_time() is None

    def test_reset_clears_both_clocks_and_the_mode(self):
        clock = SimulationClock()
        clock.publish_step(T0)
        clock.set_event_time(T1)
        clock.reset()
        assert clock.get_event_time() is None
        assert clock.get_step_time() is None
        assert clock.is_simulated() is False


class TestParticipants:
    def test_join_and_leave(self):
        clock = SimulationClock()
        clock.join("algo")
        assert clock.participants() == ["algo"]
        clock.leave("algo")
        assert clock.participants() == []

    def test_leave_is_safe_for_an_unknown_name(self):
        SimulationClock().leave("never-joined")

    def test_joining_mid_run_is_not_held_responsible_for_past_steps(self):
        clock = SimulationClock()
        clock.publish_step(T0)
        clock.join("late")
        # Nothing pending: it never saw that step, so it must not block the replay.
        assert clock.wait_for_completion(0.05, NEVER_STOPPING) == []

    def test_re_joining_does_not_discard_a_pending_step(self):
        """An algorithm joins at construction and again on entering its loop; a step
        published in between must survive, or every backtest skips its first timestep."""
        clock = SimulationClock()
        clock.join("algo")
        clock.publish_step(T0)
        clock.join("algo")
        assert clock.wait_for_completion(0.05, NEVER_STOPPING) == ["algo"]
        assert clock.wait_for_step("algo", 0.5) == Step(generation=1, time=T0)

    def test_re_joining_after_leaving_registers_afresh(self):
        """The supervisor restarts a crashed algorithm; it is not answerable for the
        step it died on."""
        clock = SimulationClock()
        clock.join("algo")
        clock.publish_step(T0)
        clock.leave("algo")
        clock.join("algo")
        assert clock.wait_for_completion(0.05, NEVER_STOPPING) == []


class TestSteps:
    def test_no_step_when_not_simulated(self):
        assert SimulationClock().wait_for_step("algo", 0.01) is None

    def test_no_step_before_one_is_published(self):
        clock = SimulationClock()
        clock.start_simulation()
        clock.join("algo")
        assert clock.wait_for_step("algo", 0.01) is None

    def test_published_step_is_handed_out(self):
        clock = SimulationClock()
        clock.join("algo")
        clock.publish_step(T0)
        step = clock.wait_for_step("algo", 0.5)
        assert step == Step(generation=1, time=T0)

    def test_a_step_is_handed_out_once(self):
        clock = SimulationClock()
        clock.join("algo")
        clock.publish_step(T0)
        step = clock.wait_for_step("algo", 0.5)
        clock.ack("algo", step.generation)
        assert clock.wait_for_step("algo", 0.01) is None

    def test_each_participant_gets_the_same_step(self):
        clock = SimulationClock()
        clock.join("a")
        clock.join("b")
        clock.publish_step(T0)
        assert clock.wait_for_step("a", 0.5).generation == 1
        assert clock.wait_for_step("b", 0.5).generation == 1

    def test_waiting_blocks_until_a_step_arrives(self):
        clock = SimulationClock()
        clock.start_simulation()
        clock.join("algo")
        seen: list[Step] = []
        thread = run_in_thread(lambda: seen.append(clock.wait_for_step("algo", 2.0)))
        clock.publish_step(T0)
        thread.join(2.0)
        assert seen == [Step(generation=1, time=T0)]

    def test_reset_releases_a_waiting_participant(self):
        clock = SimulationClock()
        clock.start_simulation()
        clock.join("algo")
        seen: list = []
        thread = run_in_thread(lambda: seen.append(clock.wait_for_step("algo", 5.0)))
        clock.reset()
        thread.join(2.0)
        assert not thread.is_alive()
        assert seen == [None]

    def test_stale_ack_never_walks_the_marker_back(self):
        clock = SimulationClock()
        clock.join("algo")
        clock.publish_step(T0)
        clock.ack("algo", 1)
        clock.ack("algo", 0)  # a late ack from an earlier step
        assert clock.wait_for_completion(0.05, NEVER_STOPPING) == []


class TestBarrier:
    def test_no_participants_returns_immediately(self):
        clock = SimulationClock()
        clock.publish_step(T0)
        assert clock.wait_for_completion(5.0, NEVER_STOPPING) == []

    def test_blocks_until_acknowledged(self):
        clock = SimulationClock()
        clock.join("algo")
        clock.publish_step(T0)
        released = threading.Event()

        def worker():
            step = clock.wait_for_step("algo", 2.0)
            released.wait(2.0)
            clock.ack("algo", step.generation)

        run_in_thread(worker)
        assert clock.wait_for_completion(0.2, NEVER_STOPPING) == ["algo"]  # still working
        released.set()
        assert clock.wait_for_completion(2.0, NEVER_STOPPING) == []

    def test_names_the_laggards_on_timeout(self):
        clock = SimulationClock()
        clock.join("slow_one")
        clock.join("slow_two")
        clock.publish_step(T0)
        assert clock.wait_for_completion(0.05, NEVER_STOPPING) == ["slow_one", "slow_two"]

    def test_leaving_releases_the_barrier(self):
        # A crashing algorithm is restarted by the supervisor; it must not strand the replay.
        clock = SimulationClock()
        clock.join("algo")
        clock.publish_step(T0)
        run_in_thread(lambda: clock.leave("algo"))
        assert clock.wait_for_completion(2.0, NEVER_STOPPING) == []

    def test_stop_releases_the_barrier_without_blaming_anyone(self):
        clock = SimulationClock()
        clock.join("algo")
        clock.publish_step(T0)
        assert clock.wait_for_completion(5.0, lambda: True) == []

    def test_zero_timeout_disables_lockstep(self):
        clock = SimulationClock()
        clock.join("algo")
        clock.publish_step(T0)
        assert clock.wait_for_completion(0, NEVER_STOPPING) == []

    def test_none_timeout_waits_indefinitely(self):
        clock = SimulationClock()
        clock.join("algo")
        clock.publish_step(T0)
        done = threading.Event()
        run_in_thread(lambda: (clock.wait_for_completion(None, NEVER_STOPPING), done.set()))
        assert not done.wait(0.2)  # still waiting
        clock.ack("algo", 1)
        assert done.wait(2.0)


class TestSingleton:
    def test_same_instance_everywhere(self):
        SimulationClock().publish_step(T0)
        assert SimulationClock().get_step_time() == T0
