import logging
from unittest.mock import MagicMock, patch

import pytest
import requests
from requests.auth import HTTPBasicAuth

from connectors.http_api import HttpApiConnector
from tests.conftest import StubDevice


def make_http_connector(**kwargs) -> HttpApiConnector:
    defaults = dict(name="HTTP 1", base_url="http://api.example")
    defaults.update(kwargs)
    return HttpApiConnector(**defaults)


def _response(text: str = "{}", status_code: int = 200) -> MagicMock:
    """A stand-in for requests.Response with a no-op raise_for_status."""
    response = MagicMock()
    response.text = text
    response.status_code = status_code
    response.raise_for_status = MagicMock()
    return response


def stub_sessions(factory):
    """Patch requests.Session where http_api constructs one; `factory()` supplies each.

    Patched at the constructor rather than by replacing _build_session(), because that seam
    is itself what these tests assert on: stubbing it would let a connector that never calls
    it — the precise defect — fall through to a real Session, spin the poll loop against a
    hostname that does not resolve, and hang instead of failing. Build the connector inside
    the context, so a session wrongly built in __init__ is captured too.
    """
    return patch("connectors.http_api.requests.Session", side_effect=factory)


class TestSessionSetup:
    """Constructor wiring, asserted on the session _build_session() produces.

    These read the built session rather than connector._session because the session is no
    longer built in __init__: start() builds one per run and its finally closes it, so a
    restart gets a live one. _build_session() is the seam that kept this wiring assertable
    without driving the poll loop.
    """

    def test_base_url_trailing_slash_stripped(self):
        connector = make_http_connector(base_url="http://api.example/")
        assert connector.base_url == "http://api.example"

    def test_no_session_before_start(self):
        assert make_http_connector()._session is None

    def test_headers_merged_into_session(self):
        connector = make_http_connector(headers={"X-Api-Key": "abc"})
        assert connector._build_session().headers["X-Api-Key"] == "abc"

    def test_basic_auth_applied_when_provided(self):
        connector = make_http_connector(auth={"username": "u", "password": "p"})
        assert connector._build_session().auth == HTTPBasicAuth("u", "p")

    def test_no_auth_when_absent(self):
        connector = make_http_connector()
        assert connector._build_session().auth is None

    def test_verify_ssl_defaults_true(self):
        connector = make_http_connector()
        assert connector._build_session().verify is True

    def test_verify_ssl_false_applied(self):
        connector = make_http_connector(verify_ssl=False)
        assert connector._build_session().verify is False

    def test_every_call_builds_a_new_session(self):
        """The whole point: a restart must not be handed the session its predecessor closed."""
        connector = make_http_connector()
        assert connector._build_session() is not connector._build_session()


class TestPollTasks:
    """inject_devices() builds one poll task per readable device with an endpoint."""

    def test_readable_device_with_endpoint_becomes_task(self):
        connector = make_http_connector(default_interval=60)
        device = StubDevice(name="sensor", listener_options={"endpoint": "/status"})
        connector.inject_devices({"sensor": device})

        assert len(connector._poll_tasks) == 1
        task = connector._poll_tasks[0]
        assert task.device is device
        assert task.endpoint == "/status"
        assert task.interval == 60
        assert task.method == "GET"
        assert task.params is None
        assert task.body is None

    def test_write_only_device_excluded(self):
        connector = make_http_connector()
        device = StubDevice(
            name="switch", is_readable=False, is_writable=True,
            listener_options={"endpoint": "/should/be/ignored"},
        )
        connector.inject_devices({"switch": device})
        assert connector._poll_tasks == []

    def test_readable_device_without_endpoint_warns_and_is_skipped(self, caplog):
        connector = make_http_connector()
        device = StubDevice(name="sensor", listener_options={})
        with caplog.at_level(logging.WARNING):
            connector.inject_devices({"sensor": device})
        assert any("has no listener_options.endpoint" in r.message for r in caplog.records)
        assert connector._poll_tasks == []

    def test_interval_falls_back_to_default(self):
        connector = make_http_connector(default_interval=42)
        device = StubDevice(name="sensor", listener_options={"endpoint": "/status"})
        connector.inject_devices({"sensor": device})
        assert connector._poll_tasks[0].interval == 42

    def test_explicit_interval_used(self):
        connector = make_http_connector(default_interval=42)
        device = StubDevice(name="sensor", listener_options={"endpoint": "/status", "interval": 5})
        connector.inject_devices({"sensor": device})
        assert connector._poll_tasks[0].interval == 5

    def test_method_is_upper_cased(self):
        connector = make_http_connector()
        device = StubDevice(name="sensor", listener_options={"endpoint": "/status", "method": "post"})
        connector.inject_devices({"sensor": device})
        assert connector._poll_tasks[0].method == "POST"

    def test_params_and_body_captured(self):
        connector = make_http_connector()
        device = StubDevice(
            name="sensor",
            listener_options={"endpoint": "/status", "params": {"q": 1}, "body": {"k": "v"}},
        )
        connector.inject_devices({"sensor": device})
        task = connector._poll_tasks[0]
        assert task.params == {"q": 1}
        assert task.body == {"k": "v"}


class TestPollDevice:
    """_poll_device issues the request, hands response.text to the device, and tracks failures."""

    def _poll(self, connector, device):
        connector.inject_devices({device.name: device})
        connector._poll_device(connector._poll_tasks[0])

    def test_successful_poll_requests_correct_url_and_passes_text(self):
        connector = make_http_connector(timeout=30)
        connector._session = MagicMock()
        connector._session.request.return_value = _response(text="payload-body")
        device = StubDevice(name="sensor", listener_options={"endpoint": "/status"})
        device.receive = MagicMock()

        self._poll(connector, device)

        connector._session.request.assert_called_once_with(
            method="GET",
            url="http://api.example/status",
            params=None,
            json=None,
            timeout=30,
        )
        device.receive.assert_called_once_with("payload-body")

    def test_url_joins_with_single_slash(self):
        connector = make_http_connector(base_url="http://api.example/")
        connector._session = MagicMock()
        connector._session.request.return_value = _response()
        device = StubDevice(name="sensor", listener_options={"endpoint": "/status"})

        self._poll(connector, device)

        assert connector._session.request.call_args.kwargs["url"] == "http://api.example/status"

    def test_body_sent_as_json_kwarg(self):
        connector = make_http_connector()
        connector._session = MagicMock()
        connector._session.request.return_value = _response()
        device = StubDevice(name="sensor", listener_options={"endpoint": "/status", "body": {"k": "v"}})

        self._poll(connector, device)

        assert connector._session.request.call_args.kwargs["json"] == {"k": "v"}

    def test_http_error_marks_device_failed_and_warns(self, caplog):
        connector = make_http_connector()
        connector._session = MagicMock()
        response = _response()
        response.raise_for_status.side_effect = requests.HTTPError(response=MagicMock(status_code=503))
        connector._session.request.return_value = response
        device = StubDevice(name="sensor", listener_options={"endpoint": "/status"})
        device.receive = MagicMock()

        with caplog.at_level(logging.WARNING):
            self._poll(connector, device)

        assert "sensor" in connector._failed_devices
        assert any("HTTP 503 polling 'sensor'" in r.message for r in caplog.records)
        device.receive.assert_not_called()

    def test_request_exception_marks_device_failed_and_errors(self, caplog):
        connector = make_http_connector()
        connector._session = MagicMock()
        connector._session.request.side_effect = requests.Timeout("boom")
        device = StubDevice(name="sensor", listener_options={"endpoint": "/status"})

        with caplog.at_level(logging.ERROR):
            self._poll(connector, device)

        assert "sensor" in connector._failed_devices
        assert any("Error polling 'sensor'" in r.message for r in caplog.records)

    def test_device_recovers_after_failure(self, caplog):
        connector = make_http_connector()
        connector._session = MagicMock()
        connector._session.request.return_value = _response()
        device = StubDevice(name="sensor", listener_options={"endpoint": "/status"})
        connector.inject_devices({"sensor": device})
        connector._failed_devices.add("sensor")

        with caplog.at_level(logging.INFO):
            connector._poll_device(connector._poll_tasks[0])

        assert "sensor" not in connector._failed_devices
        assert any("Device 'sensor' recovered" in r.message for r in caplog.records)


class TestARaisingDevice:
    """A device bug must not spend this connector's restart budget.

    The try around the receive call catches only requests.* — a device's ValueError, KeyError
    or TypeError escapes start(), whose only wrapper is try/finally with no except.
    """

    def _poll(self, connector, device):
        connector.inject_devices({device.name: device})
        connector._poll_device(connector._poll_tasks[0])

    def _broken(self):
        connector = make_http_connector()
        connector._session = MagicMock()
        connector._session.request.return_value = _response(text="payload-body")
        device = StubDevice(name="sensor", listener_options={"endpoint": "/status"})
        device.receive = MagicMock(side_effect=ValueError("bad payload"))
        return connector, device

    def test_a_raising_device_does_not_end_the_session(self, caplog):
        connector, device = self._broken()
        with caplog.at_level(logging.ERROR):
            self._poll(connector, device)  # must not raise
        assert any("raised on a payload" in r.message for r in caplog.records)

    def test_a_raising_device_is_not_logged_as_recovered(self, caplog):
        connector, device = self._broken()
        connector._failed_devices.add(device.name)
        with caplog.at_level(logging.INFO):
            self._poll(connector, device)
        assert not any("recovered" in r.message for r in caplog.records)
        assert device.name in connector._failed_devices


class TestStart:
    """start() returns cleanly when idle; the populated loop is infinite and not driven here."""

    def test_no_poll_tasks_warns_idle_and_returns(self, caplog):
        connector = make_http_connector()
        connector._session = MagicMock()
        connector.inject_devices({})

        with caplog.at_level(logging.WARNING):
            connector.start()  # must return, not loop

        assert any("connector idle" in r.message for r in caplog.records)
        connector._session.request.assert_not_called()

    def test_idle_return_builds_no_session(self):
        """The idle guard runs before _build_session(), so nothing is opened to close."""
        connector = make_http_connector()
        connector._build_session = MagicMock()
        connector.inject_devices({})

        connector.start()

        connector._build_session.assert_not_called()

    def test_start_builds_and_closes_its_own_session(self):
        session = MagicMock()
        with stub_sessions(lambda: session):
            connector = make_http_connector()
            connector.inject_devices({"sensor": StubDevice(name="sensor", listener_options={"endpoint": "/status"})})
            session.request.side_effect = lambda **kwargs: (connector.stop(), _response())[1]

            connector.start()  # the single poll stops the loop from inside

        session.request.assert_called_once()
        session.close.assert_called_once()
        assert connector._session is None, "the closed session must not be left on the connector"


class TestRestart:
    """A restarted run polls on a session of its own.

    SupervisedWorker._run catches the crash and re-invokes the *same bound* start() on the
    *same instance*, so whatever start()'s finally tore down has to be rebuilt by the next
    run. With the session built once in __init__, run two polled through adapters close()
    had already released — which urllib3 survives by rebuilding its connection pools lazily.
    That accident, not a contract, is why this never showed up as a bug report.
    """

    def test_a_second_start_polls_on_a_live_session(self):
        sessions: list[MagicMock] = []
        polls: list[str] = []
        run = {"n": 0}

        def build_session() -> MagicMock:
            session = MagicMock()

            def poll(**kwargs):
                # The session's own state *at poll time* is the whole assertion: a run
                # polling through a session whose close() has already run is the defect.
                polls.append("closed" if session.close.called else "live")
                if run["n"] == 1:
                    # Run one ends the way the restart path is actually reached — a bug that
                    # is not a requests error escapes _poll_device's narrow handlers, and
                    # start()'s finally closes the session on the way out.
                    raise ValueError("a bug in the poll path")
                connector.stop()  # run two: one poll is enough, let the loop wind down
                return _response(text="payload-body")

            session.request.side_effect = poll
            sessions.append(session)
            return session

        with stub_sessions(build_session):
            connector = make_http_connector()
            device = StubDevice(name="sensor", listener_options={"endpoint": "/status"})
            device.receive = MagicMock()
            connector.inject_devices({"sensor": device})

            run["n"] = 1
            with pytest.raises(ValueError):
                connector.start()
            run["n"] = 2
            connector.start()  # what SupervisedWorker._run does next: the same bound method

        assert len(sessions) == 2, "the restart polled on run one's session instead of building its own"
        assert sessions[0] is not sessions[1]
        assert polls == ["live", "live"]
        sessions[0].close.assert_called_once()  # run one's finally
        sessions[1].close.assert_called_once()  # run two's, on a session of its own
        device.receive.assert_called_once_with("payload-body")


class TestSend:
    """send() POSTs the raw payload to the device's controller endpoint."""

    def test_send_posts_payload_to_controller_endpoint(self):
        connector = make_http_connector(timeout=30)
        connector._session = MagicMock()
        connector._session.request.return_value = _response()
        device = StubDevice(
            name="switch", is_writable=True, controller_options={"endpoint": "/cmd"},
        )

        connector.send(device, "on")

        connector._session.request.assert_called_once_with(
            method="POST",
            url="http://api.example/cmd",
            data="on",
            timeout=30,
        )

    def test_send_uses_configured_method(self):
        connector = make_http_connector()
        connector._session = MagicMock()
        connector._session.request.return_value = _response()
        device = StubDevice(
            name="switch", is_writable=True,
            controller_options={"endpoint": "/cmd", "method": "put"},
        )

        connector.send(device, "on")

        assert connector._session.request.call_args.kwargs["method"] == "PUT"

    def test_send_uses_data_not_json(self):
        connector = make_http_connector()
        connector._session = MagicMock()
        connector._session.request.return_value = _response()
        device = StubDevice(
            name="switch", is_writable=True, controller_options={"endpoint": "/cmd"},
        )

        connector.send(device, "on")

        kwargs = connector._session.request.call_args.kwargs
        assert kwargs["data"] == "on"
        assert "json" not in kwargs

    def test_send_without_controller_endpoint_warns_and_makes_no_request(self, caplog):
        connector = make_http_connector()
        connector._session = MagicMock()
        device = StubDevice(name="switch", is_writable=True, controller_options={})

        with caplog.at_level(logging.WARNING):
            connector.send(device, "on")

        assert any("has no controller_options.endpoint" in r.message for r in caplog.records)
        connector._session.request.assert_not_called()

    def test_send_before_start_warns_and_drops_the_command(self, caplog):
        """main constructs every worker before starting any of them, and send() runs on the
        ALGORITHM's thread — so a command can arrive before start() built the session. The
        raise this replaces was an AttributeError on None, counted as an algorithm crash."""
        connector = make_http_connector()
        device = StubDevice(
            name="switch", is_writable=True, controller_options={"endpoint": "/cmd"},
        )
        assert connector._session is None

        with caplog.at_level(logging.WARNING):
            connector.send(device, "on")  # must not raise

        assert any("Not connected yet" in r.message and "switch" in r.message for r in caplog.records)

    def test_send_between_two_supervised_runs_is_dropped(self, caplog):
        """start()'s finally clears the attribute as well as closing the session, so a
        command landing during the supervisor's backoff is dropped rather than POSTed
        through released adapters."""
        session = MagicMock()
        with stub_sessions(lambda: session):
            connector = make_http_connector()
            connector.inject_devices({"meter": StubDevice(name="meter", listener_options={"endpoint": "/m"})})
            session.request.side_effect = lambda **kwargs: (connector.stop(), _response())[1]
            connector.start()  # one poll, then the finally
        device = StubDevice(
            name="switch", is_writable=True, controller_options={"endpoint": "/cmd"},
        )

        with caplog.at_level(logging.WARNING):
            connector.send(device, "on")

        assert any("Not connected yet" in r.message for r in caplog.records)
        session.request.assert_called_once()  # the poll only — no command on the closed session

    def test_send_http_error_warns(self, caplog):
        connector = make_http_connector()
        connector._session = MagicMock()
        response = _response()
        response.raise_for_status.side_effect = requests.HTTPError(response=MagicMock(status_code=500))
        connector._session.request.return_value = response
        device = StubDevice(
            name="switch", is_writable=True, controller_options={"endpoint": "/cmd"},
        )

        with caplog.at_level(logging.WARNING):
            connector.send(device, "on")

        assert any("HTTP 500 sending to 'switch'" in r.message for r in caplog.records)

    def test_send_request_exception_logs_error(self, caplog):
        connector = make_http_connector()
        connector._session = MagicMock()
        connector._session.request.side_effect = requests.Timeout("boom")
        device = StubDevice(
            name="switch", is_writable=True, controller_options={"endpoint": "/cmd"},
        )

        with caplog.at_level(logging.ERROR):
            connector.send(device, "on")

        assert any("Error sending to 'switch'" in r.message for r in caplog.records)
