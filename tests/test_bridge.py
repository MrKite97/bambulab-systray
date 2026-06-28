"""Unit tests for src.bridge: the Api (js_api) dispatcher + serialize_state.

All tests run WITHOUT a real window: the Api delegates to plain injected
callables (lambdas / Mock), and serialize_state is pure. The two load-bearing
invariants asserted here are (1) every Api method forwards to its injected
handler with the page's arguments, and (2) the serialized state object matches
the panel.html contract AND never contains a secret (T-07-01 / T-07-02).
"""

from unittest.mock import Mock

import pytest

from src import bridge
from src.bridge import Api, serialize_state
from src.state import PrintState
from src.status import ConnectionStatus
from src.version import __version__


def _running_state():
    """A hand-built RUNNING PrintState matching the plan's example."""
    return PrintState(
        gcode_state="RUNNING",
        mc_percent=63,
        mc_remaining_time=67,  # MINUTES -> "nog 1 u 7 min"
        layer_num=132,
        total_layer_num=198,
        subtask_name="3DBenchy.gcode",
        gcode_file="other.gcode",
        nozzle_temper=215.0,
        nozzle_target_temper=215.0,
        bed_temper=60.0,
        bed_target_temper=60.0,
    )


# --------------------------------------------------------------------------- #
# serialize_state                                                             #
# --------------------------------------------------------------------------- #


def test_serialize_state_maps_running_print():
    state = _running_state()
    out = serialize_state(
        state, ConnectionStatus.CONNECTED, logged_in=True, theme="dark"
    )
    assert out["status"] == "printing"
    assert out["pct"] == 63
    assert out["layer"] == 132
    assert out["totalLayer"] == 198
    assert out["file"] == "3DBenchy.gcode"
    assert out["nozzle"] == 215.0
    assert out["nozzleTarget"] == 215.0
    assert out["bed"] == 60.0
    assert out["bedTarget"] == 60.0
    assert out["etaLabel"] == "nog 1 u 7 min"
    assert out["loggedIn"] is True
    assert out["theme"] == "dark"
    assert out["printerName"] == ""
    assert "authStep" in out


def test_serialize_state_eta_minutes_only():
    state = PrintState(gcode_state="RUNNING", mc_percent=10, mc_remaining_time=7)
    out = serialize_state(state, ConnectionStatus.CONNECTED, logged_in=True)
    assert out["etaLabel"] == "nog 7 min"


def test_serialize_state_printer_name_keyword_outputs_printerName():
    state = _running_state()
    out = serialize_state(
        state, ConnectionStatus.CONNECTED, logged_in=True, printer_name="X1 Carbon"
    )
    assert out["printerName"] == "X1 Carbon"


def test_serialize_state_printerName_defaults_empty():
    state = _running_state()
    out = serialize_state(state, ConnectionStatus.CONNECTED, logged_in=True)
    assert "printerName" in out
    assert out["printerName"] == ""


def test_serialize_state_idle_maps_neutral_to_idle():
    state = PrintState(gcode_state="IDLE", mc_percent=0, mc_remaining_time=0)
    out = serialize_state(state, ConnectionStatus.CONNECTED, logged_in=True)
    assert out["status"] == "idle"


def test_serialize_state_logged_out_defaults_authstep_login():
    state = PrintState()
    out = serialize_state(state, ConnectionStatus.CONNECTED, logged_in=False)
    assert out["loggedIn"] is False
    assert out["authStep"] == "login"


def test_serialize_state_file_prefers_subtask_name():
    state = PrintState(
        gcode_state="RUNNING", subtask_name="model.3mf", gcode_file="raw.gcode"
    )
    out = serialize_state(state, ConnectionStatus.CONNECTED, logged_in=True)
    assert out["file"] == "model.3mf"


def test_serialize_state_file_falls_back_to_gcode_file():
    state = PrintState(gcode_state="RUNNING", subtask_name="", gcode_file="raw.gcode")
    out = serialize_state(state, ConnectionStatus.CONNECTED, logged_in=True)
    assert out["file"] == "raw.gcode"


def test_serialize_state_default_theme_is_a_string():
    # theme=None -> falls back to detect_windows_theme() which returns dark/light.
    state = _running_state()
    out = serialize_state(state, ConnectionStatus.CONNECTED, logged_in=True)
    assert out["theme"] in ("dark", "light")


def test_serialize_state_no_secret_keys():
    state = _running_state()
    out = serialize_state(
        state, ConnectionStatus.CONNECTED, logged_in=True, printer_name="X1"
    )
    for forbidden in ("token", "password", "accessToken", "access_token"):
        assert forbidden not in out


def test_serialize_state_auth_step_override():
    state = PrintState()
    out = serialize_state(
        state, ConnectionStatus.CONNECTED, logged_in=False, auth_step="code"
    )
    assert out["authStep"] == "code"


def test_serialize_state_carries_static_version():
    # The version is a static module constant (single source in src/version.py),
    # present on the serialized state so the panel paints it on first load (D-10).
    out = serialize_state(_running_state(), ConnectionStatus.CONNECTED, logged_in=True)
    assert out["version"] == __version__
    assert out["version"] == "2.1.2"
    assert out["version"]  # not empty


def test_get_initial_state_no_provider_carries_version():
    # The no-provider fallback also runs through serialize_state, so it inherits
    # the version field with no separate injection.
    out = Api().get_initial_state()
    assert out["version"] == __version__


# --------------------------------------------------------------------------- #
# Api delegation                                                              #
# --------------------------------------------------------------------------- #


def test_api_hide_calls_handler_once():
    hide = Mock()
    api = Api(handlers={"hide": hide})
    api.hide()
    hide.assert_called_once_with()


def test_api_login_submit_forwards_args():
    h = Mock()
    api = Api(handlers={"login_submit": h})
    api.login_submit("a@b.c", "pw")
    h.assert_called_once_with("a@b.c", "pw")


def test_api_submit_code_forwards():
    h = Mock()
    api = Api(handlers={"submit_code": h})
    api.submit_code("123456")
    h.assert_called_once_with("123456")


def test_api_resend_code_forwards():
    h = Mock()
    api = Api(handlers={"resend_code": h})
    api.resend_code()
    h.assert_called_once_with()


def test_api_select_printer_forwards():
    h = Mock()
    api = Api(handlers={"select_printer": h})
    api.select_printer("dev1")
    h.assert_called_once_with("dev1")


def test_api_logout_forwards():
    h = Mock()
    api = Api(handlers={"logout": h})
    api.logout()
    h.assert_called_once_with()


def test_api_control_forwards():
    h = Mock()
    api = Api(handlers={"control": h})
    api.control("pause")
    h.assert_called_once_with("pause")


def test_api_missing_handler_is_tolerated():
    api = Api(handlers={})
    # None of these should raise even though no handler is present.
    api.hide()
    api.login_submit("a", "b")
    api.submit_code("1")
    api.resend_code()
    api.select_printer("d")
    api.logout()
    api.control("stop")


def test_api_no_handlers_arg_is_tolerated():
    api = Api()
    api.hide()  # must not raise


def test_api_get_initial_state_uses_state_provider():
    state = _running_state()
    provider = lambda: (state, ConnectionStatus.CONNECTED, True, "light")
    api = Api(state_provider=provider)
    out = api.get_initial_state()
    assert out["status"] == "printing"
    assert out["theme"] == "light"
    assert out["loggedIn"] is True


def test_api_get_initial_state_has_no_secret():
    state = _running_state()
    provider = lambda: (state, ConnectionStatus.CONNECTED, True, "dark")
    api = Api(state_provider=provider)
    out = api.get_initial_state()
    for forbidden in ("token", "password", "accessToken"):
        assert forbidden not in out


def test_api_get_initial_state_without_provider_returns_logged_out():
    api = Api()
    out = api.get_initial_state()
    assert out["loggedIn"] is False
    assert out["authStep"] == "login"


def test_api_get_initial_state_accepts_prebuilt_dict_provider():
    provider = lambda: {"loggedIn": True, "status": "paused"}
    api = Api(state_provider=provider)
    out = api.get_initial_state()
    assert out["loggedIn"] is True
    assert out["status"] == "paused"


def test_api_apply_update_calls_handler_once():
    # "Nu bijwerken" (UPD-06): the Api method dispatches to the wired handler.
    fn = Mock()
    api = Api(handlers={"apply_update": fn})
    api.apply_update()
    fn.assert_called_once_with()


def test_apply_update_registered_in_methods():
    assert "apply_update" in bridge._METHODS
    assert hasattr(Api(), "apply_update")


def test_api_handlers_object_attribute_style():
    # handlers may be an object exposing the methods, not just a dict.
    class H:
        def __init__(self):
            self.called = None

        def control(self, cmd):
            self.called = cmd

    h = H()
    api = Api(handlers=h)
    api.control("resume")
    assert h.called == "resume"


def test_bridge_module_does_not_import_webview():
    import src.bridge as bridge_mod
    import inspect

    src = inspect.getsource(bridge_mod)
    assert "import webview" not in src


# --------------------------------------------------------------------------- #
# Phase 13-03: the five update Api methods + autoUpdateEnabled seed            #
# --------------------------------------------------------------------------- #


def test_api_check_for_update_now_forwards():
    h = Mock()
    api = Api(handlers={"check_for_update_now": h})
    api.check_for_update_now()
    h.assert_called_once_with()


def test_api_skip_update_version_forwards():
    h = Mock()
    api = Api(handlers={"skip_update_version": h})
    api.skip_update_version("2.3.0")
    h.assert_called_once_with("2.3.0")


def test_api_dismiss_update_forwards():
    h = Mock()
    api = Api(handlers={"dismiss_update": h})
    api.dismiss_update()
    h.assert_called_once_with()


def test_api_set_auto_update_forwards():
    h = Mock()
    api = Api(handlers={"set_auto_update": h})
    api.set_auto_update(False)
    h.assert_called_once_with(False)


def test_api_open_release_page_forwards():
    h = Mock()
    api = Api(handlers={"open_release_page": h})
    api.open_release_page("https://x/rel")
    h.assert_called_once_with("https://x/rel")


def test_all_five_update_methods_in_methods_tuple():
    from src.bridge import _METHODS

    for name in (
        "check_for_update_now",
        "skip_update_version",
        "dismiss_update",
        "set_auto_update",
        "open_release_page",
    ):
        assert name in _METHODS


def test_update_methods_missing_handler_tolerated():
    api = Api(handlers={})
    # None of these should raise with no handler present.
    api.check_for_update_now()
    api.skip_update_version("1.0.0")
    api.dismiss_update()
    api.set_auto_update(True)
    api.open_release_page("https://x")


def test_serialize_state_emits_autoUpdateEnabled_default_true():
    out = serialize_state(_running_state(), ConnectionStatus.CONNECTED, logged_in=True)
    assert out["autoUpdateEnabled"] is True


def test_serialize_state_autoUpdateEnabled_false_when_passed():
    out = serialize_state(
        _running_state(),
        ConnectionStatus.CONNECTED,
        logged_in=True,
        auto_update_enabled=False,
    )
    assert out["autoUpdateEnabled"] is False
