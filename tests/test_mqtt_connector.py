import logging
from unittest.mock import MagicMock, patch

from paho.mqtt.enums import MQTTProtocolVersion

from connectors.mqtt import MQTTConnector
from tests.conftest import StubDevice


def make_mqtt_connector(**kwargs) -> MQTTConnector:
    defaults = dict(name="MQTT 1", host="broker.example", port=1883, version="3.1.1")
    defaults.update(kwargs)
    return MQTTConnector(**defaults)


class TestClientId:
    """F-4: client id must be derived from the connector name, not a shared 'EMS'."""

    @patch("connectors.mqtt.Client")
    def test_client_id_defaults_to_connector_name(self, MockClient):
        connector = make_mqtt_connector(name="MQTT 1")
        connector.start()
        MockClient.assert_called_once()
        assert MockClient.call_args.kwargs["client_id"] == "MQTT 1"

    @patch("connectors.mqtt.Client")
    def test_client_id_is_not_hardcoded_ems(self, MockClient):
        connector = make_mqtt_connector(name="broker-a")
        connector.start()
        assert MockClient.call_args.kwargs["client_id"] != "EMS"

    @patch("connectors.mqtt.Client")
    def test_explicit_client_id_overrides_name(self, MockClient):
        connector = make_mqtt_connector(name="MQTT 1", client_id="custom-id")
        connector.start()
        assert MockClient.call_args.kwargs["client_id"] == "custom-id"

    @patch("connectors.mqtt.Client")
    def test_distinct_connectors_get_distinct_client_ids(self, MockClient):
        make_mqtt_connector(name="broker-a").start()
        make_mqtt_connector(name="broker-b").start()
        client_ids = [call.kwargs["client_id"] for call in MockClient.call_args_list]
        assert client_ids == ["broker-a", "broker-b"]


class TestAuth:
    """F-4: username/password authentication."""

    @patch("connectors.mqtt.Client")
    def test_username_password_set_when_provided(self, MockClient):
        client = MockClient.return_value
        connector = make_mqtt_connector(username="user", password="secret")
        connector.start()
        client.username_pw_set.assert_called_once_with("user", "secret")

    @patch("connectors.mqtt.Client")
    def test_username_pw_not_set_when_absent(self, MockClient):
        client = MockClient.return_value
        connector = make_mqtt_connector()
        connector.start()
        client.username_pw_set.assert_not_called()


class TestTLS:
    """F-4: TLS support, including mutual (client-certificate) TLS."""

    @patch("connectors.mqtt.Client")
    def test_tls_set_when_tls_true(self, MockClient):
        client = MockClient.return_value
        connector = make_mqtt_connector(tls=True)
        connector.start()
        client.tls_set.assert_called_once_with(ca_certs=None, certfile=None, keyfile=None)

    @patch("connectors.mqtt.Client")
    def test_tls_set_with_ca_certs(self, MockClient):
        client = MockClient.return_value
        connector = make_mqtt_connector(ca_certs="/etc/ssl/ca.pem")
        connector.start()
        client.tls_set.assert_called_once_with(ca_certs="/etc/ssl/ca.pem", certfile=None, keyfile=None)

    @patch("connectors.mqtt.Client")
    def test_mutual_tls_passes_client_cert_and_key(self, MockClient):
        client = MockClient.return_value
        connector = make_mqtt_connector(certfile="/etc/ssl/client.crt", keyfile="/etc/ssl/client.key")
        connector.start()
        client.tls_set.assert_called_once_with(
            ca_certs=None, certfile="/etc/ssl/client.crt", keyfile="/etc/ssl/client.key"
        )

    @patch("connectors.mqtt.Client")
    def test_tls_not_set_when_unconfigured(self, MockClient):
        client = MockClient.return_value
        connector = make_mqtt_connector()
        connector.start()
        client.tls_set.assert_not_called()


class TestSubscriptions:
    """F-4: subscribe to device-declared topic filters, never the blanket '#'."""

    def test_subscribes_to_declared_filters_not_hash(self):
        connector = make_mqtt_connector()
        d1 = StubDevice(name="p1", listener_options={"pattern": "p1/.*", "subscription": "p1/#"})
        d2 = StubDevice(
            name="shelly",
            listener_options={"pattern": "shellies/.*", "subscription": ["shellies/relay/0", "shellies/relay/1"]},
        )
        connector.inject_devices({"p1": d1, "shelly": d2})

        client = MagicMock()
        connector.mqtt_client = client
        connector._on_connect(client, None, None, MagicMock(), None)

        client.subscribe.assert_called_once()
        subscribed_topics = {topic for topic, qos in client.subscribe.call_args.args[0]}
        assert subscribed_topics == {"p1/#", "shellies/relay/0", "shellies/relay/1"}
        assert "#" not in subscribed_topics

    def test_readable_device_without_subscription_warns_and_is_skipped(self, caplog):
        connector = make_mqtt_connector()
        device = StubDevice(name="p1", listener_options={"pattern": "p1/.*"})
        with caplog.at_level(logging.WARNING):
            connector.inject_devices({"p1": device})
        assert any("has no listener_options.subscription" in r.message for r in caplog.records)
        assert connector.subscriptions == set()

    def test_on_connect_does_not_fall_back_to_hash_when_no_subscriptions(self, caplog):
        connector = make_mqtt_connector()
        connector.inject_devices({})
        client = MagicMock()
        connector.mqtt_client = client
        with caplog.at_level(logging.WARNING):
            connector._on_connect(client, None, None, MagicMock(), None)
        client.subscribe.assert_not_called()
        assert any("No subscription filters declared" in r.message for r in caplog.records)

    def test_write_only_device_subscription_is_ignored(self):
        connector = make_mqtt_connector()
        device = StubDevice(
            name="switch", is_readable=False, is_writable=True,
            listener_options={"subscription": "should/be/ignored"},
        )
        connector.inject_devices({"switch": device})
        assert connector.subscriptions == set()

    def test_pattern_and_subscription_are_independent(self):
        connector = make_mqtt_connector()
        device = StubDevice(name="p1", listener_options={"pattern": "p1/.*", "subscription": "p1/#"})
        connector.inject_devices({"p1": device})
        # the routing regex is compiled into callbacks; the MQTT filter is kept separately
        assert any(regex.pattern == "p1/.*" for regex in connector.callbacks)
        assert connector.subscriptions == {"p1/#"}


class TestSend:
    """Regression: send() publishes to the device's controller topic."""

    def test_send_publishes_to_controller_topic(self):
        connector = make_mqtt_connector()
        client = MagicMock()
        connector.mqtt_client = client
        device = StubDevice(name="switch", is_writable=True, controller_options={"topic": "cmd/switch"})
        connector.send(device, "on")
        client.publish.assert_called_once_with("cmd/switch", "on")

    def test_missing_topic_warns_and_publishes_nothing(self, caplog):
        """send() runs on the ALGORITHM's thread and nothing in that chain catches, so
        publish(None, ...) used to raise paho's ValueError straight into a supervised
        algorithm worker and spend its restart budget."""
        connector = make_mqtt_connector()
        client = MagicMock()
        connector.mqtt_client = client
        device = StubDevice(name="switch", is_writable=True)
        with caplog.at_level(logging.WARNING):
            connector.send(device, "on")  # must not raise
        assert any("has no controller_options.topic" in r.message for r in caplog.records)
        client.publish.assert_not_called()

    def test_send_before_start_warns_instead_of_attribute_error(self, caplog):
        """main constructs every worker before starting any of them, so an algorithm can
        fire a control before start() has built mqtt_client."""
        connector = make_mqtt_connector()
        device = StubDevice(name="switch", is_writable=True, controller_options={"topic": "cmd/switch"})
        with caplog.at_level(logging.WARNING):
            connector.send(device, "on")  # must not raise AttributeError
        assert any("Not connected yet" in r.message for r in caplog.records)

    def test_transport_error_is_logged_not_raised(self, caplog):
        connector = make_mqtt_connector()
        client = MagicMock()
        client.publish.side_effect = OSError("broker gone")
        connector.mqtt_client = client
        device = StubDevice(name="switch", is_writable=True, controller_options={"topic": "cmd/switch"})
        with caplog.at_level(logging.ERROR):
            connector.send(device, "on")  # must not raise
        assert any("Error sending 'on' to 'switch'" in r.message for r in caplog.records)


class TestProtocolVersion:
    """MQTTVersion is a StrEnum, so an unknown value raises ValueError — and
    create_classes catches only AttributeError/ModuleNotFoundError/TypeError, so it would
    escape and kill the process rather than skip the connector."""

    def test_unset_env_reference_falls_back_to_the_default(self, caplog):
        """`"version": "${MQTT_VERSION}"` with the variable unset arrives as None."""
        with caplog.at_level(logging.WARNING):
            connector = make_mqtt_connector(version=None)  # must not raise
        assert any("Unknown MQTT version" in r.message for r in caplog.records)
        assert connector.protocol == MQTTProtocolVersion.MQTTv311

    def test_typo_falls_back_to_the_default(self, caplog):
        with caplog.at_level(logging.WARNING):
            connector = make_mqtt_connector(version="3.1.2")
        assert any("Unknown MQTT version" in r.message for r in caplog.records)
        assert connector.protocol == MQTTProtocolVersion.MQTTv311

    def test_declared_versions_are_honoured(self):
        assert make_mqtt_connector(version="5.0.0").protocol == MQTTProtocolVersion.MQTTv5
        assert make_mqtt_connector(version="3.1.0").protocol == MQTTProtocolVersion.MQTTv31


class TestOptionCoercion:
    """Config validates the plugin schema BEFORE resolving ${VAR}, so every scalar can arrive
    as a str. These two used to reach the transport uncoerced."""

    def test_a_string_port_is_coerced(self):
        """paho compares the port against an int inside connect(), so a "${MQTT_PORT}" was a
        TypeError on the supervised worker thread rather than a config warning."""
        assert make_mqtt_connector(port="8883").port == 8883

    def test_an_unresolved_port_falls_back_to_the_default(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert make_mqtt_connector(port=None).port == 1883

    def test_a_string_false_disables_tls(self):
        """The failure this prevents is silent, not loud: tls is only read for truthiness, so
        an operator disabling it through an environment variable got TLS anyway."""
        assert make_mqtt_connector(tls="false").tls is False
        assert make_mqtt_connector(tls="true").tls is True

    @patch("connectors.mqtt.Client")
    def test_a_string_false_does_not_reach_tls_set(self, MockClient):
        client = MockClient.return_value
        make_mqtt_connector(tls="false").start()
        client.tls_set.assert_not_called()


class TestStart:
    @patch("connectors.mqtt.Client")
    def test_no_host_returns_idle(self, MockClient, caplog):
        """paho raises ValueError('Invalid host') on an empty host, which the supervisor
        counts as a crash — and once the restart budget is spent the worker is finished,
        which is what main waits on. An unset ${MQTT_HOST} took the whole EMS down."""
        connector = make_mqtt_connector(host="")
        with caplog.at_level(logging.ERROR):
            connector.start()  # must return, not loop and not raise
        assert any("No host configured" in r.message for r in caplog.records)
        MockClient.assert_not_called()


class TestSeamDefaults:
    """resolve_listener() and resolve_downlink() were extracted from inject_devices() and
    send() so connectors/lorawan.py can subclass this one. The parent must be
    behaviour-identical — these pin the default bodies."""

    def test_resolve_listener_returns_the_declared_options(self):
        connector = make_mqtt_connector()
        device = StubDevice(listener_options={"subscription": "p1/#", "pattern": "p1/.*"})
        assert connector.resolve_listener(device) == ("p1/#", "p1/.*")

    def test_resolve_listener_returns_none_for_each_absent_option(self):
        connector = make_mqtt_connector()
        assert connector.resolve_listener(StubDevice()) == (None, None)

    def test_a_missing_pattern_alone_does_not_warn(self, caplog):
        """The asymmetry is deliberate and pre-existing: a device may be routed by
        subscription alone, so only a missing subscription is worth a warning."""
        connector = make_mqtt_connector()
        with caplog.at_level(logging.WARNING):
            connector.inject_devices({"p1": StubDevice(name="p1", listener_options={"subscription": "p1/#"})})
        assert not any("pattern" in r.message for r in caplog.records)

    def test_resolve_downlink_returns_the_topic_and_the_payload_unchanged(self):
        connector = make_mqtt_connector()
        device = StubDevice(is_writable=True, controller_options={"topic": "cmd/switch"})
        assert connector.resolve_downlink(device, "on") == ("cmd/switch", "on")

    def test_duplicate_routing_patterns_are_reported(self, caplog):
        """re.compile is memoised, so an equal pattern string is the same dict key: the
        second device would silently replace the first and one of them would receive
        nothing for the life of the run."""
        connector = make_mqtt_connector()
        listener = {"pattern": "meter/.*", "subscription": "meter/#"}
        with caplog.at_level(logging.ERROR):
            connector.inject_devices({
                "a": StubDevice(name="a", listener_options=dict(listener)),
                "b": StubDevice(name="b", listener_options=dict(listener)),
            })
        assert any("same routing pattern" in r.message for r in caplog.records)
