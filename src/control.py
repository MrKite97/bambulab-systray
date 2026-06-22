"""Printer control publisher -- the ONLY write/command surface of the app.

Publishes the fixed pause/resume/stop commands to ``device/<serial>/request``
via the already-connected paho client (the same request-topic path used by
``mqtt_client``'s pushall). The command set is a closed allowlist: nothing
outside pause/resume/stop can be sent, so no arbitrary printer mutation (e.g.
firmware/gcode injection) is possible (threat T-05-04). Read-only monitoring
otherwise still holds.

Security:
  * ``serial`` is the caller-supplied SELECTED printer's ``dev_id`` (Phase 8
    sets it) -- control only ever targets the user's own bound device, never a
    broadcast or third-party serial (T-05-05).
  * The access token / MQTT password is never referenced or logged here; only
    the command name + topic are logged at debug level (T-05-06, T-01-04).
"""

import json
import logging

logger = logging.getLogger(__name__)

# Closed allowlist: the only commands the app may ever publish (BACKEND.md §4).
# Any value outside this tuple is rejected BEFORE publishing.
_ALLOWED_COMMANDS = ("pause", "resume", "stop")


def publish_command(client, serial: str, command: str) -> None:
    """Publish a single pause/resume/stop command to the printer's request topic.

    Validation happens BEFORE the publish call: a command outside
    ``_ALLOWED_COMMANDS`` raises ``ValueError`` and never reaches the broker, so
    no arbitrary command can be injected into ``device/<serial>/request``. The
    payload is serialized to a JSON string (mirroring pushall), not a raw dict.
    """
    if command not in _ALLOWED_COMMANDS:
        raise ValueError(
            f"Unsupported control command: {command!r} "
            f"(allowed: {_ALLOWED_COMMANDS})"
        )
    topic = f"device/{serial}/request"
    payload = json.dumps({"print": {"command": command}})
    client.publish(topic, payload)
    logger.debug("Published control command %s to %s", command, topic)


def pause(client, serial: str) -> None:
    """Pause the active print on ``serial``."""
    publish_command(client, serial, "pause")


def resume(client, serial: str) -> None:
    """Resume the paused print on ``serial``."""
    publish_command(client, serial, "resume")


def stop(client, serial: str) -> None:
    """Stop (cancel) the active print on ``serial``."""
    publish_command(client, serial, "stop")
