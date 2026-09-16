"""The two LoRaWAN profile tables are independent by design; these pin what must agree.

connectors/lorawan.py holds topic trees and downlink bodies, devices/lora.py holds envelope
read paths, and the two sets are disjoint — so there is nothing to share and no shared module
to put in parsers/. Neither package may import the other (see devices/modbus_meter.py's WORDS
comment for the same rule and the same remedy). What must stay in lockstep is only the set of
profile *names* an operator can write in config.json, plus the constant that has to mean the
same thing to an algorithm.

No importorskip, deliberately: neither connectors/lorawan.py (paho is core) nor devices/lora.py
(stdlib only) needs an extra, so this file runs in a core-only checkout. The equivalent joint
for connectors/lora.py — the `native` envelope, which does need pyserial — is pinned in
tests/test_lora_connector.py, behind that file's importorskip.
"""
from connectors.lorawan import PROFILES as CONNECTOR_PROFILES
from devices.lora import ROLE_ENERGY_IMPORT_KWH as LORA_ROLE, PROFILES as DEVICE_PROFILES
from devices.modbus_meter import ROLE_ENERGY_IMPORT_KWH as MODBUS_ROLE


def test_every_connector_profile_has_a_device_envelope():
    """An operator writes the same name on both sides: `profile: chirpstack` on the connector
    and `profile: chirpstack` on each device behind it. A name the device did not know would
    silently fall back to chirpstack and decode another server's envelope."""
    assert set(CONNECTOR_PROFILES) <= set(DEVICE_PROFILES)


def test_the_device_side_carries_the_serial_envelope_too():
    """`native` has no connector-side entry on purpose — a serial radio has no topic tree."""
    assert "native" in DEVICE_PROFILES
    assert "native" not in CONNECTOR_PROFILES


def test_every_device_envelope_locates_a_payload():
    """`payload` is the one path that must be right; everything else degrades to None."""
    for name, envelope in DEVICE_PROFILES.items():
        assert envelope.payload, f"profile '{name}' cannot find an application payload"


def test_every_connector_profile_can_route_and_send():
    for name, profile in CONNECTOR_PROFILES.items():
        assert profile.uplink_topic, f"profile '{name}' has no uplink topic"
        assert profile.downlink_topic, f"profile '{name}' has no downlink topic"
        assert profile.identifier in ("dev_eui", "device_id"), f"profile '{name}' keys on nothing"


def test_the_energy_role_means_the_same_thing_across_device_kinds():
    """AutoToggle sums every EnergyMeter it can see without knowing which kind produced it."""
    assert LORA_ROLE == MODBUS_ROLE
