"""Regression tests for F-1: deepcopy of devices must work on every
concrete Device class, not just those with a hand-rolled workaround.

threading.Event holds locks that cannot be deep-copied; Device.__deepcopy__
in the base class replaces them with fresh equal-state events and shares the
connector back-reference instead of copying the live transport.
"""
from copy import deepcopy

import pytest

from connectors.mqtt import MQTTConnector
from devices_manager.devices_manager import DevicesManager
from tests.conftest import make_p1 as _make_p1, make_pseudo as _make_pseudo, make_shelly

def _make_shelly():
    return make_shelly(listener_options={"pattern": "shellies/.*"})


@pytest.fixture(params=[_make_p1, _make_shelly, _make_pseudo], ids=["P1", "ShellyPlug", "Pseudo"])
def device(request):
    return request.param()


class TestDeviceDeepcopy:
    def test_deepcopy_succeeds(self, device):
        # The original F-1 bug: TypeError: cannot pickle '_thread.lock' object
        copy = deepcopy(device)
        assert copy.name == device.name
        assert copy is not device

    def test_event_state_preserved(self, device):
        device.mark_data_ready()
        device.mark_connected()
        copy = deepcopy(device)
        assert copy.is_data_ready() is True
        assert copy.wait_until_connected(timeout=0) is True

    def test_readiness_events_are_shared_with_the_live_device(self, device):
        """A snapshot's readiness must track the live device, not the moment it was taken.

        The events are how a connector *signals* a device, so a copied one is a handle
        nothing will ever set: `Algorithm._wait_for_required_devices` waits on a snapshot,
        and with independent events it timed out on every required device however ready
        that device was."""
        copy = deepcopy(device)
        assert copy.is_data_ready() is False

        device.mark_data_ready()  # the connector signals the *live* device

        assert copy.is_data_ready() is True
        assert copy.wait_until_ready(timeout=0) is True

    def test_data_is_independent(self, device):
        device.data = {"power": 100}
        copy = deepcopy(device)
        copy.data["power"] = 999
        assert device.data["power"] == 100


class TestMQTTProductionPath:
    """Deep-copying a device attached to a started MQTT connector must not
    traverse into the paho client (which holds unpicklable locks)."""

    def test_get_devices_with_live_mqtt_connector(self):
        from paho.mqtt.client import Client
        from paho.mqtt.enums import CallbackAPIVersion

        connector = MQTTConnector(name="mqtt_1", host="localhost", port=1883, version="3.1.1")
        # Instantiating the paho client is what creates the locks — no broker needed
        connector.mqtt_client = Client(callback_api_version=CallbackAPIVersion.VERSION2, client_id="EMS")

        device = _make_p1()
        connector.inject_devices({device.name: device})

        dm = DevicesManager()
        dm.update_device(device)

        snapshot = dm.get_devices()[device.name]
        assert snapshot is not device
        # The connector is shared, not copied: control() reaches the live transport
        assert snapshot.connector is connector
