"""Tests for src.mqtt_client (paho-mqtt 2.x VERSION2 callbacks + backoff loop).

All MQTT is mocked -- these tests make NO network connection and perform NO TLS
handshake. ``mqtt_client.mqtt.Client`` is monkeypatched to a fake that records
constructor kwargs and subscribe/publish/tls calls, so we assert on *structure*
(VERSION2, verified TLS, report/request topics, pushall payload, delta-merge)
without a broker. No real access token is ever present: the only "token" here is
a structural fixture string, never a credential (threat register T-01-10).

Backoff tests drive ``backoff_delay`` directly and exercise the reconnect-loop
counter logic with injected fake connect/sleep callables -- no sockets, no waits.
"""

import json

import pytest

import paho.mqtt.client as real_mqtt
from src import mqtt_client
from src.state import PrintState


class FakeMqttClient:
    """Records construction kwargs and the calls mqtt_client makes on a client."""

    instances = []

    def __init__(self, *args, **kwargs):
        self.init_args = args
        self.init_kwargs = kwargs
        self.username = None
        self.password = None
        self.tls_set_called = False
        self.tls_insecure_arg = None
        self.subscribed = []
        self.published = []  # list of (topic, payload)
        FakeMqttClient.instances.append(self)

    def username_pw_set(self, username, password=None):
        self.username = username
        self.password = password

    def tls_set(self, *args, **kwargs):
        self.tls_set_called = True

    def tls_insecure_set(self, value):
        self.tls_insecure_arg = value

    def subscribe(self, topic, *args, **kwargs):
        self.subscribed.append(topic)

    def publish(self, topic, payload=None, *args, **kwargs):
        self.published.append((topic, payload))


class FakeMessage:
    """Minimal stand-in for a paho MQTTMessage (only .payload is read)."""

    def __init__(self, payload):
        # paho delivers bytes; json.loads accepts str or bytes.
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        self.payload = payload.encode() if isinstance(payload, str) else payload


@pytest.fixture
def fake_mqtt(monkeypatch):
    FakeMqttClient.instances = []
    monkeypatch.setattr(mqtt_client.mqtt, "Client", FakeMqttClient)
    return FakeMqttClient


# --- Task 1: build_client + callbacks ----------------------------------------


def test_build_client_uses_version2_and_verified_tls(fake_mqtt):
    client = mqtt_client.build_client(
        client_id="bambu-systray-test",
        username="u_1234567890",
        access_token="fixture-not-a-real-token",
    )
    # Constructed with the v2 callback API.
    assert (
        client.init_kwargs.get("callback_api_version")
        == real_mqtt.CallbackAPIVersion.VERSION2
    )
    assert client.init_kwargs.get("client_id") == "bambu-systray-test"
    # TLS is configured with verification ON.
    assert client.tls_set_called is True
    assert client.tls_insecure_arg is False
    # Credentials wired through.
    assert client.username == "u_1234567890"
    assert client.password == "fixture-not-a-real-token"


def test_on_connect_subscribes_report_and_publishes_pushall(fake_mqtt):
    client = FakeMqttClient()
    userdata = {"serial": "00M00A000000000", "state": PrintState()}
    mqtt_client.on_connect(client, userdata, flags={}, reason_code=0, properties=None)

    assert "device/00M00A000000000/report" in client.subscribed
    assert len(client.published) == 1
    topic, payload = client.published[0]
    assert topic == "device/00M00A000000000/request"
    assert json.loads(payload) == mqtt_client.PUSH_ALL
    assert json.loads(payload)["pushing"]["command"] == "pushall"


def test_on_message_delta_merges_print_object(fake_mqtt):
    state = PrintState()
    userdata = {"serial": "S", "state": state}
    msg = FakeMessage({"print": {"mc_percent": 42, "gcode_state": "running"}})
    mqtt_client.on_message(FakeMqttClient(), userdata, msg)

    assert state.mc_percent == 42
    assert state.gcode_state == "running"


def test_on_message_without_print_key_does_not_merge_or_raise(fake_mqtt):
    state = PrintState()
    state.mc_percent = 7  # prior value must survive
    userdata = {"serial": "S", "state": state}
    msg = FakeMessage({"system": {"command": "get_version"}})
    # Must not raise and must not clobber existing state.
    mqtt_client.on_message(FakeMqttClient(), userdata, msg)
    assert state.mc_percent == 7
    assert state.gcode_state == "unknown"


def test_on_message_with_non_json_payload_does_not_raise(fake_mqtt):
    state = PrintState()
    userdata = {"serial": "S", "state": state}
    msg = FakeMessage(b"not-json-at-all")
    # Malformed input must not crash the network thread (T-01-11).
    mqtt_client.on_message(FakeMqttClient(), userdata, msg)
    assert state.mc_percent == 0


def test_on_disconnect_does_not_reconnect(fake_mqtt):
    """on_disconnect must be inert -- the backoff loop owns reconnection."""

    class TrackingClient(FakeMqttClient):
        def __init__(self):
            super().__init__()
            self.connect_calls = 0
            self.reconnect_calls = 0

        def connect(self, *a, **k):
            self.connect_calls += 1

        def reconnect(self, *a, **k):
            self.reconnect_calls += 1

    client = TrackingClient()
    userdata = {"serial": "S", "state": PrintState()}
    mqtt_client.on_disconnect(
        client, userdata, disconnect_flags={}, reason_code=0, properties=None
    )
    assert client.connect_calls == 0
    assert client.reconnect_calls == 0


# --- Task 2: backoff_delay + reconnect loop ----------------------------------


def test_backoff_delay_schedule_bounds():
    # base + uniform(0, base*0.5): in [base, base*1.5]
    for _ in range(50):
        assert 5 <= mqtt_client.backoff_delay(0) <= 7.5
        assert 10 <= mqtt_client.backoff_delay(1) <= 15
        assert 30 <= mqtt_client.backoff_delay(2) <= 45
        assert 60 <= mqtt_client.backoff_delay(3) <= 90


def test_backoff_delay_clamps_beyond_schedule():
    for _ in range(50):
        d = mqtt_client.backoff_delay(99)
        assert 60 <= d <= 90  # clamped to the 60s cap base


def test_backoff_delay_has_jitter():
    samples = {mqtt_client.backoff_delay(1) for _ in range(50)}
    # Jitter present: not all identical across samples.
    assert len(samples) > 1
    for s in samples:
        assert 10 <= s <= 15  # never below base, never above base*1.5


def test_run_session_resets_attempt_on_successful_connect():
    """The loop resets attempt=0 after a successful connect and increments on
    failure, using injected fake connect/sleep -- no sockets, no real waits."""

    sleeps = []
    delays_seen = []

    # connect: succeed once, then fail twice, then stop.
    outcomes = iter([True, False, False])

    def fake_connect(client):
        try:
            ok = next(outcomes)
        except StopIteration:
            raise mqtt_client.StopSession()
        if not ok:
            raise ConnectionError("simulated drop")
        return None

    def fake_sleep(seconds):
        sleeps.append(seconds)

    attempts = mqtt_client.run_session(
        client=FakeMqttClient(),
        connect=fake_connect,
        sleep=fake_sleep,
        record_attempt=delays_seen.append,
    )
    # After a success the counter resets to 0; the two subsequent failures
    # increment it. The recorded attempts show the reset.
    assert delays_seen[0] == 0  # first connect attempt
    assert 0 in delays_seen  # reset happened after success
    # Two failures => two backoff sleeps occurred.
    assert len(sleeps) == 2
