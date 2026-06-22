"""Tests for src.control (the printer pause/resume/stop publish surface).

All MQTT is mocked -- no real broker, no TLS. A FakeMqttClient records each
published (topic, payload) tuple (the minimal shape borrowed from
tests/test_mqtt_client.py). These tests assert on *structure*: the exact request
topic, the exact literal JSON payload, and that any command outside the closed
allowlist is rejected BEFORE any publish call (threat T-05-04). No access token /
secret is present -- the serial is a structural fixture, not a credential.
"""

import json

import pytest

from src import control


class FakeMqttClient:
    """Records published (topic, payload) tuples -- only .publish is exercised."""

    def __init__(self):
        self.published = []  # list of (topic, payload)

    def publish(self, topic, payload=None, *args, **kwargs):
        self.published.append((topic, payload))


# --------------------------------------------------------------------------- #
# Allowed commands: exact topic + exact literal payload
# --------------------------------------------------------------------------- #


def test_publish_command_pause_sends_exact_json_to_request_topic():
    client = FakeMqttClient()
    control.publish_command(client, "00M00A", "pause")

    assert len(client.published) == 1
    topic, payload = client.published[0]
    assert topic == "device/00M00A/request"
    assert json.loads(payload) == {"print": {"command": "pause"}}


def test_publish_command_resume_sends_exact_json():
    client = FakeMqttClient()
    control.publish_command(client, "S", "resume")

    topic, payload = client.published[0]
    assert topic == "device/S/request"
    assert json.loads(payload) == {"print": {"command": "resume"}}


def test_publish_command_stop_sends_exact_json():
    client = FakeMqttClient()
    control.publish_command(client, "S", "stop")

    topic, payload = client.published[0]
    assert topic == "device/S/request"
    assert json.loads(payload) == {"print": {"command": "stop"}}


def test_publish_command_payload_is_json_string_not_dict():
    """Payload mirrors pushall: a JSON str/bytes, never a raw dict."""
    client = FakeMqttClient()
    control.publish_command(client, "S", "pause")

    _topic, payload = client.published[0]
    assert isinstance(payload, (str, bytes))
    assert not isinstance(payload, dict)


# --------------------------------------------------------------------------- #
# Closed allowlist: anything else raises ValueError and does NOT publish
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", ["start", "home", "", "PAUSE", "stop ", "gcode_line"])
def test_publish_command_rejects_unknown_command_without_publishing(bad):
    client = FakeMqttClient()
    with pytest.raises(ValueError):
        control.publish_command(client, "S", bad)
    # A rejected command must never reach the broker.
    assert client.published == []


# --------------------------------------------------------------------------- #
# Convenience wrappers delegate to publish_command
# --------------------------------------------------------------------------- #


def test_pause_wrapper_publishes_pause_payload():
    client = FakeMqttClient()
    control.pause(client, "S")
    topic, payload = client.published[0]
    assert topic == "device/S/request"
    assert json.loads(payload) == {"print": {"command": "pause"}}


def test_resume_wrapper_publishes_resume_payload():
    client = FakeMqttClient()
    control.resume(client, "S")
    _topic, payload = client.published[0]
    assert json.loads(payload) == {"print": {"command": "resume"}}


def test_stop_wrapper_publishes_stop_payload():
    client = FakeMqttClient()
    control.stop(client, "S")
    _topic, payload = client.published[0]
    assert json.loads(payload) == {"print": {"command": "stop"}}


def test_allowlist_is_exactly_pause_resume_stop():
    assert control._ALLOWED_COMMANDS == ("pause", "resume", "stop")
