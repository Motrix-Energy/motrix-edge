"""OpenEMS Edge connector: URL/credential shape, endpoint synthesis, channel writes.

No importorskip — `requests` is in requirements.txt, and the connector subclasses
HttpApiConnector, so the transport is mocked exactly as tests/test_http_api_connector.py
mocks it: one MagicMock over `connector._session`.
"""
import logging
from unittest.mock import MagicMock

import requests
from requests.auth import HTTPBasicAuth

from connectors.http_api import HttpApiConnector
from connectors.openems import OpenemsConnector
from tests.conftest import StubDevice


def make_connector(**kwargs) -> OpenemsConnector:
    defaults = dict(name="edge", host="192.168.1.50")
    defaults.update(kwargs)
    return OpenemsConnector(**defaults)


def make_device(name: str = "sum", **listener) -> StubDevice:
    return StubDevice(name=name, listener_options={"component": "_sum", **listener})


def _response(text: str = "{}", status_code: int = 200) -> MagicMock:
    response = MagicMock()
    response.text = text
    response.status_code = status_code
    response.raise_for_status = MagicMock()
    return response


class TestConstruction:
    def test_base_url_is_built_from_host_and_port(self):
        assert make_connector(port=8084).base_url == "http://192.168.1.50:8084"

    def test_port_defaults_to_the_rest_controllers_8084(self):
        assert make_connector().base_url.endswith(":8084")

    def test_string_port_is_coerced(self):
        """Config validates the plugin schema BEFORE resolving ${VAR}, so a
        "${OPENEMS_PORT}" declared `integer` reaches the constructor as a str."""
        assert make_connector(port="8085").base_url == "http://192.168.1.50:8085"

    def test_https_scheme(self):
        assert make_connector(scheme="https").base_url.startswith("https://")

    def test_explicit_base_url_wins(self):
        connector = make_connector(base_url="https://edge.example.org/openems", host="ignored")
        assert connector.base_url == "https://edge.example.org/openems"

    def test_password_becomes_basic_auth_with_a_filler_username(self):
        """The Edge ignores the username and authenticates on the password, which is a
        user ROLE: guest | user | owner | admin."""
        connector = make_connector(password="admin")
        # Read off the built session, not connector._session: the parent builds one per run
        # of start(), so a restart never polls on the session its finally closed.
        assert connector._build_session().auth == HTTPBasicAuth("x", "admin")

    def test_it_is_an_http_api_connector(self):
        assert isinstance(make_connector(), HttpApiConnector)

    def test_inherited_options_are_forwarded(self):
        connector = make_connector(default_interval="15", timeout="5", verify_ssl="false")
        assert connector.default_interval == 15.0
        assert connector.timeout == 5.0
        assert connector.verify_ssl is False


class TestEndpointSynthesis:
    def test_channel_list_becomes_one_regex_alternation(self):
        """The Edge matches channelId with Pattern.matches, so a list is one request."""
        connector = make_connector()
        connector.inject_devices({"sum": make_device(channels=["EssSoc", "GridActivePower"])})
        assert connector._poll_tasks[0].endpoint == "/rest/channel/_sum/EssSoc|GridActivePower"

    def test_single_channel_string(self):
        connector = make_connector()
        connector.inject_devices({"ess": make_device("ess", channels="Soc")})
        assert connector._poll_tasks[0].endpoint == "/rest/channel/_sum/Soc"

    def test_no_channels_polls_them_all(self):
        connector = make_connector()
        connector.inject_devices({"sum": make_device()})
        assert connector._poll_tasks[0].endpoint == "/rest/channel/_sum/.*"

    def test_component_is_used_verbatim(self):
        connector = make_connector()
        device = StubDevice(name="ess", listener_options={"component": "ess0", "channels": "Soc"})
        connector.inject_devices({"ess": device})
        assert connector._poll_tasks[0].endpoint == "/rest/channel/ess0/Soc"

    def test_device_without_a_component_warns_and_is_skipped(self, caplog):
        connector = make_connector()
        with caplog.at_level(logging.WARNING):
            connector.inject_devices({"x": StubDevice(name="x")})
        assert any("has no listener_options.component" in r.message for r in caplog.records)
        assert connector._poll_tasks == []
        assert not any("listener_options.endpoint" in r.message for r in caplog.records), \
            "the parent's message names an option this connector's schema forbids"

    def test_interval_still_comes_from_the_inherited_plumbing(self):
        connector = make_connector(default_interval=42)
        connector.inject_devices({"a": make_device("a"), "b": make_device("b", interval=5)})
        assert [task.interval for task in connector._poll_tasks] == [42, 5]

    def test_write_only_device_is_excluded(self):
        connector = make_connector()
        device = StubDevice(name="ess", is_readable=False, is_writable=True,
                            listener_options={"component": "ess0"})
        connector.inject_devices({"ess": device})
        assert connector._poll_tasks == []


class TestPolling:
    def test_the_inherited_poll_loop_fetches_the_synthesized_url(self):
        connector = make_connector()
        connector._session = MagicMock()
        connector._session.request.return_value = _response(text='{"address":"_sum/EssSoc","value":63}')
        device = make_device(channels="EssSoc")
        device.receive = MagicMock(return_value=True)
        connector.inject_devices({"sum": device})

        connector._poll_device(connector._poll_tasks[0])

        assert connector._session.request.call_args.kwargs["url"] == "http://192.168.1.50:8084/rest/channel/_sum/EssSoc"
        device.receive.assert_called_once_with('{"address":"_sum/EssSoc","value":63}')

    def test_failure_isolation_is_inherited(self, caplog):
        connector = make_connector()
        connector._session = MagicMock()
        connector._session.request.side_effect = requests.Timeout("boom")
        connector.inject_devices({"sum": make_device()})

        with caplog.at_level(logging.ERROR):
            connector._poll_device(connector._poll_tasks[0])  # must not raise

        assert "sum" in connector._failed_devices


class TestSend:
    def _writable(self, **controller) -> StubDevice:
        return StubDevice(name="ess", is_writable=True, controller_options=controller)

    def _connector(self):
        connector = make_connector()
        connector._session = MagicMock()
        connector._session.post.return_value = _response()
        return connector

    def test_on_and_off_default_to_true_and_false(self):
        connector = self._connector()
        device = self._writable(component="io0", channel="Relay1")

        connector.send(device, "on")
        connector.send(device, "off")

        first, second = connector._session.post.call_args_list
        assert first.args[0] == "http://192.168.1.50:8084/rest/channel/io0/Relay1"
        assert first.kwargs["json"] == {"value": True}
        assert second.kwargs["json"] == {"value": False}

    def test_the_body_is_json_not_a_raw_payload(self):
        """The parent sends data=payload; the Edge requires an object with a 'value' key."""
        connector = self._connector()
        connector.send(self._writable(component="io0", channel="Relay1"), "on")
        kwargs = connector._session.post.call_args.kwargs
        assert "json" in kwargs and "data" not in kwargs

    def test_on_value_expresses_a_setpoint(self):
        """An ESS charging at 3 kW, while the algorithm still only says 'on'."""
        connector = self._connector()
        device = self._writable(component="ess0", channel="SetActivePowerEquals", on_value=-3000, off_value=0)

        connector.send(device, "on")
        connector.send(device, "off")

        assert [call.kwargs["json"] for call in connector._session.post.call_args_list] == [
            {"value": -3000}, {"value": 0},
        ]

    def test_a_numeric_command_is_written_as_a_number(self):
        connector = self._connector()
        connector.send(self._writable(component="ess0", channel="SetActivePowerEquals"), "-2500")
        assert connector._session.post.call_args.kwargs["json"] == {"value": -2500}

    def test_a_non_json_token_is_written_as_a_string(self):
        """A mode channel takes MANUAL_ON; json.loads on it must not escape send()."""
        connector = self._connector()
        connector.send(self._writable(component="ctrl0", channel="Mode"), "MANUAL_ON")
        assert connector._session.post.call_args.kwargs["json"] == {"value": "MANUAL_ON"}

    def test_send_before_start_warns_and_drops_the_command(self, caplog):
        """This override reads the session itself, and the parent now leaves it None outside
        a run of start() — an AttributeError on None, which is not one of the four types the
        handlers here name, so it escaped onto the algorithm's thread. The guard is
        HttpApiConnector._live_session(), inherited rather than copied."""
        connector = make_connector()
        assert connector._session is None

        with caplog.at_level(logging.WARNING):
            connector.send(self._writable(component="ess0", channel="SetActivePowerEquals"), "on")  # must not raise

        assert any("Not connected yet" in r.message and "ess" in r.message for r in caplog.records)

    def test_missing_routing_warns_and_posts_nothing(self, caplog):
        connector = self._connector()
        with caplog.at_level(logging.WARNING):
            connector.send(self._writable(component="ess0"), "on")
        assert any("no controller_options.component/channel" in r.message for r in caplog.records)
        connector._session.post.assert_not_called()

    def test_http_error_warns_and_does_not_raise(self, caplog):
        """send() runs on the ALGORITHM's thread and nothing in that chain catches."""
        connector = make_connector()
        connector._session = MagicMock()
        response = _response()
        response.raise_for_status.side_effect = requests.HTTPError(response=MagicMock(status_code=403))
        connector._session.post.return_value = response

        with caplog.at_level(logging.WARNING):
            connector.send(self._writable(component="io0", channel="Relay1"), "on")

        assert any("HTTP 403 setting io0/Relay1" in r.message for r in caplog.records)

    def test_request_exception_is_logged_not_raised(self, caplog):
        connector = make_connector()
        connector._session = MagicMock()
        connector._session.post.side_effect = requests.Timeout("boom")
        with caplog.at_level(logging.ERROR):
            connector.send(self._writable(component="io0", channel="Relay1"), "on")
        assert any("Error setting io0/Relay1" in r.message for r in caplog.records)


class TestParentSeam:
    """resolve_endpoint() was extracted from HttpApiConnector.inject_devices for this
    subclass; the parent must be behaviour-identical."""

    def test_the_parent_still_reads_the_declared_endpoint(self):
        connector = HttpApiConnector(name="http", base_url="http://api.example")
        assert connector.resolve_endpoint(StubDevice(listener_options={"endpoint": "/status"})) == "/status"

    def test_the_parent_warns_and_returns_none_without_one(self, caplog):
        connector = HttpApiConnector(name="http", base_url="http://api.example")
        with caplog.at_level(logging.WARNING):
            assert connector.resolve_endpoint(StubDevice(name="sensor")) is None
        assert any("has no listener_options.endpoint" in r.message for r in caplog.records)
