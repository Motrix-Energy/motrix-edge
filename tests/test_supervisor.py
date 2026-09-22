"""Thread supervision: a worker that dies is logged loudly and restarted, bounded."""

import logging
from threading import Event

import pytest

from supervisor.supervisor import RestartPolicy, Supervisor, SupervisedWorker

# Fast policy so the whole file stays well under a second
FAST = RestartPolicy(backoff_seconds=0.01, max_backoff_seconds=0.02)


class StubWorker:
    """Runs `behaviour(call_number)` each time the supervisor invokes run()."""

    def __init__(self, behaviour, name="stub_worker"):
        self.name = name
        self.behaviour = behaviour
        self.calls = 0
        self.stop_calls = 0
        self._stop_event = Event()

    def run(self) -> None:
        self.calls += 1
        self.behaviour(self)

    def stop(self) -> None:
        self.stop_calls += 1
        self._stop_event.set()


def make_worker(behaviour, policy=FAST, name="stub_worker") -> tuple[StubWorker, SupervisedWorker]:
    stub = StubWorker(behaviour, name=name)
    return stub, SupervisedWorker(stub, "run", policy)


def run_until_finished(supervised: SupervisedWorker, timeout: float = 2.0) -> None:
    supervised.start()
    supervised.join(timeout)
    assert supervised.is_finished(), "worker did not settle within the timeout"


class TestCleanReturn:
    def test_normal_return_is_not_a_crash(self, caplog):
        """A target that returns is finished, never restarted — the normal path for
        a completed replay, the LoRa stub, and an idle poller."""
        stub, supervised = make_worker(lambda w: None)
        with caplog.at_level(logging.DEBUG):
            run_until_finished(supervised)
        assert stub.calls == 1
        assert supervised.restarts == 0
        assert supervised.crashes == 0
        assert any("finished" in r.message for r in caplog.records)
        assert not any(r.levelno >= logging.ERROR for r in caplog.records)


class TestAWorkerThatExits:
    """`_finished` must be set however `_run` leaves, which is why it lives in a finally.

    A worker whose target calls sys.exit() raises SystemExit — a BaseException, so the
    `except Exception` never saw it and the bare `self._finished.set()` after the loop was
    never reached. main() then polled `all(worker.is_finished())` over a connector that
    would never finish: the EMS neither restarted nor exited until SIGTERM.
    """

    def test_system_exit_finishes_the_worker_and_is_not_restarted(self, caplog):
        def exits(worker):
            raise SystemExit(3)

        stub, supervised = make_worker(exits)
        with caplog.at_level(logging.DEBUG):
            run_until_finished(supervised)  # asserts is_finished()

        assert stub.calls == 1  # deliberately not restarted
        assert supervised.restarts == 0
        assert supervised.crashes == 1  # counted, so /workers reports both paths alike
        assert supervised.completed_cleanly is False

        critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
        assert len(critical) == 1
        assert "SystemExit(3)" in critical[0].message
        assert "not restarting" in critical[0].message

    # The KeyboardInterrupt genuinely escapes the thread — that is the property under
    # test — so threading.excepthook reports it and pytest turns that into a warning.
    @pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
    def test_a_base_exception_still_finishes_the_worker(self):
        """The reason it is a `finally` and not a sixth `except` clause: nothing here
        catches a KeyboardInterrupt on a worker thread, and it must still not leave a
        worker that main will wait on forever."""
        def interrupts(worker):
            raise KeyboardInterrupt

        stub, supervised = make_worker(interrupts)
        supervised.start()
        supervised.join(2.0)
        assert supervised.is_finished()
        assert supervised.completed_cleanly is False


class TestCrashHandling:
    def test_crash_is_logged_with_traceback_then_restarted(self, caplog):
        def behaviour(worker):
            if worker.calls == 1:
                raise RuntimeError("boom")

        stub, supervised = make_worker(behaviour)
        with caplog.at_level(logging.DEBUG):
            run_until_finished(supervised)

        assert stub.calls == 2  # crashed once, restarted, then returned cleanly
        assert supervised.crashes == 1
        assert supervised.restarts == 1

        crash_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(crash_records) == 1
        assert "crashed" in crash_records[0].message
        assert crash_records[0].exc_info is not None  # full traceback, not just a message
        assert any("Restarting worker" in r.message for r in caplog.records if r.levelno == logging.WARNING)

    def test_restart_limit_gives_up_with_critical(self, caplog):
        def always_crash(worker):
            raise RuntimeError("boom")

        policy = RestartPolicy(max_restarts=2, backoff_seconds=0.01, max_backoff_seconds=0.02)
        stub, supervised = make_worker(always_crash, policy=policy)
        with caplog.at_level(logging.DEBUG):
            run_until_finished(supervised)

        assert stub.calls == 3  # initial run + 2 restarts
        assert supervised.restarts == 2
        criticals = [r for r in caplog.records if r.levelno == logging.CRITICAL]
        assert len(criticals) == 1
        assert "giving up" in criticals[0].message

    def test_restart_disabled_stays_down(self, caplog):
        def always_crash(worker):
            raise RuntimeError("boom")

        stub, supervised = make_worker(always_crash, policy=RestartPolicy(enabled=False))
        with caplog.at_level(logging.DEBUG):
            run_until_finished(supervised)

        assert stub.calls == 1
        assert supervised.restarts == 0
        assert any("restart is disabled" in r.message for r in caplog.records if r.levelno == logging.CRITICAL)


class TestStop:
    def test_request_stop_calls_worker_stop(self):
        def block_until_stopped(worker):
            worker._stop_event.wait(2)

        stub, supervised = make_worker(block_until_stopped)
        supervised.start()
        supervised.request_stop()
        supervised.join(2)

        assert stub.stop_calls == 1
        assert supervised.is_finished()
        assert not supervised.is_alive()

    def test_stop_during_backoff_prevents_restart(self, caplog):
        """A crash followed by a stop must not resurrect the worker."""
        crashed = Event()

        def always_crash(worker):
            crashed.set()
            raise RuntimeError("boom")

        policy = RestartPolicy(backoff_seconds=1.0)  # long enough to stop mid-backoff
        stub, supervised = make_worker(always_crash, policy=policy)
        with caplog.at_level(logging.DEBUG):
            supervised.start()
            assert crashed.wait(2)
            supervised.request_stop()
            supervised.join(2)

        assert supervised.is_finished()
        assert stub.calls == 1  # never restarted
        assert any("not restarting" in r.message for r in caplog.records)

    def test_worker_without_stop_is_reported(self, caplog):
        class NoStop:
            name = "no_stop"

            def run(self):
                return None

        supervised = SupervisedWorker(NoStop(), "run", FAST)
        with caplog.at_level(logging.DEBUG):
            supervised.start()
            supervised.join(2)
            supervised.request_stop()

        assert any("has no stop()" in r.message for r in caplog.records if r.levelno == logging.WARNING)

    def test_stop_raising_does_not_break_shutdown(self, caplog):
        class RaisingStop:
            name = "raising"

            def run(self):
                return None

            def stop(self):
                raise RuntimeError("stop failed")

        supervised = SupervisedWorker(RaisingStop(), "run", FAST)
        with caplog.at_level(logging.DEBUG):
            supervised.start()
            supervised.join(2)
            supervised.request_stop()  # must not propagate

        assert any("raised while stopping" in r.message for r in caplog.records)


class TestSupervisor:
    def test_supervise_all_starts_every_worker(self):
        workers = {StubWorker(lambda w: None, name=f"w{i}") for i in range(3)}
        supervisor = Supervisor(FAST)
        supervised = supervisor.supervise_all(workers, "run")

        assert len(supervised) == 3
        assert {w.name for w in supervised} == {"w0", "w1", "w2"}
        for worker in supervised:
            worker.join(2)
        assert all(w.calls == 1 for w in workers)

    def test_stop_all_returns_stragglers(self):
        """A worker that ignores stop() is reported, not waited on forever."""
        deaf_done = Event()

        class Deaf:
            name = "deaf"

            def run(self):
                deaf_done.wait(5)

            def stop(self):
                pass  # deliberately ignores the request

        obedient = StubWorker(lambda w: w._stop_event.wait(5), name="obedient")

        supervisor = Supervisor(FAST)
        supervisor.supervise(obedient, "run")
        supervisor.supervise(Deaf(), "run")
        try:
            stragglers = supervisor.stop_all(timeout=0.2)
            assert [w.name for w in stragglers] == ["deaf"]
            assert obedient.stop_calls == 1
        finally:
            deaf_done.set()  # let the daemon thread finish so pytest exits clean

    def test_stop_all_returns_empty_when_all_stop(self):
        supervisor = Supervisor(FAST)
        workers = [StubWorker(lambda w: w._stop_event.wait(5), name=f"w{i}") for i in range(3)]
        for worker in workers:
            supervisor.supervise(worker, "run")
        assert supervisor.stop_all(timeout=2) == []


class TestRestartPolicy:
    def test_defaults(self):
        policy = RestartPolicy()
        assert policy.enabled is True
        assert policy.max_restarts == 5
        assert policy.backoff_seconds == 1.0
        assert policy.max_backoff_seconds == 60.0

    def test_from_runtime_maps_config_keys(self):
        policy = RestartPolicy.from_runtime({
            "restart": False,
            "max_restarts": 2,
            "backoff_seconds": 3,
            "max_backoff_seconds": 30,
            "shutdown_timeout_seconds": 99,  # not a restart concern, ignored
        })
        assert policy == RestartPolicy(enabled=False, max_restarts=2, backoff_seconds=3.0, max_backoff_seconds=30.0)

    def test_from_runtime_falls_back_to_defaults(self):
        assert RestartPolicy.from_runtime({}) == RestartPolicy()
