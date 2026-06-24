"""The JS<->Python bridge core: the ``Api`` js_api object + ``serialize_state``.

This module is the Python side of the panel round-trip (FLY-03). It has NO GUI
dependency -- it never imports ``webview``. The ``Api`` is a plain object handed
to pywebview's ``create_window(js_api=...)``; the page calls
``window.pywebview.api.<method>(...)`` and each method forwards to an INJECTED
handler so the whole bridge is unit-testable with plain callables (Phases 8/9
plug their real handlers into the same seam).

``serialize_state`` turns a :class:`~src.state.PrintState` + connection/auth
status into the exact ``state`` object the panel's ``applyState`` consumes
(keys defined in ``src/web/panel.html``). Two security invariants hold by
construction:

- The serialized dict contains ONLY non-secret, display-derived values. It never
  reads or emits a token/password/accessToken key (T-07-01). PrintState itself
  carries no secret.
- The ``Api`` delegates login/code arguments straight to the injected handler and
  logs nothing -- no credential is ever recorded here (T-07-02).
"""

from src.render import detect_windows_theme, status_from_gcode_state
from src.state import PrintState
from src.status import ConnectionStatus

# The js_api method names the page calls (window.pywebview.api.<name>). This is
# the LOCKED set from panel.html; Phases 8/9 supply real handlers behind them.
_METHODS = (
    "hide",
    "get_initial_state",
    "login_submit",
    "submit_code",
    "resend_code",
    "select_printer",
    "open_printer_select",
    "logout",
    "control",
    "resize",
)


def _eta_label(minutes: int) -> str:
    """Format remaining MINUTES as the panel's ETA wording.

    ``mc_remaining_time`` is an integer count of MINUTES (see state.py). The
    progress panel shows "nog {h} u {m} min" when an hour or more remains, else
    "nog {m} min". Negative inputs are clamped to 0.
    """
    minutes = max(0, int(minutes))
    if minutes >= 60:
        return f"nog {minutes // 60} u {minutes % 60} min"
    return f"nog {minutes} min"


def serialize_state(
    state: PrintState,
    connection: ConnectionStatus,
    *,
    logged_in: bool,
    auth_step: str = "login",
    theme: str | None = None,
    printer_name: str = "",
) -> dict:
    """Turn PrintState + connection/auth status into the panel ``state`` object.

    Output keys mirror panel.html's ``applyState`` contract: ``loggedIn``,
    ``authStep``, ``status``, ``pct``, ``nozzle``/``nozzleTarget``,
    ``bed``/``bedTarget``, ``layer``/``totalLayer``, ``file``, ``etaLabel``,
    ``printerName``, ``theme``.

    - ``status`` comes from :func:`render.status_from_gcode_state`; its
      "neutral" key is mapped to the page's "idle" status key.
    - ``file`` prefers ``subtask_name`` and falls back to ``gcode_file`` (both
      are already basenames).
    - ``printer_name`` is output as ``printerName`` (default "" this phase;
      Phases 8/9 pass the real name from ``auth.enrich_devices``).
    - ``connection`` is accepted for the full contract / future use; the page's
      logged-in/auth gating is driven by ``logged_in`` + ``auth_step`` here.

    SECURITY: no token/password/accessToken key is ever read or emitted.
    """
    status = status_from_gcode_state(state.gcode_state)
    if status == "neutral":
        status = "idle"  # the panel's idle status key

    file_name = state.subtask_name or state.gcode_file

    return {
        "loggedIn": bool(logged_in),
        "authStep": auth_step,
        "status": status,
        "pct": state.mc_percent,
        "nozzle": state.nozzle_temper,
        "nozzleTarget": state.nozzle_target_temper,
        "bed": state.bed_temper,
        "bedTarget": state.bed_target_temper,
        "layer": state.layer_num,
        "totalLayer": state.total_layer_num,
        "file": file_name,
        "etaLabel": _eta_label(state.mc_remaining_time),
        "printerName": printer_name,
        "theme": theme or detect_windows_theme(),
    }


def _resolve_handler(handlers, name):
    """Return the callable for ``name`` from a dict or object, or None.

    ``handlers`` may be a mapping (``handlers["control"]``) or an object exposing
    the method as an attribute (``handlers.control``). A missing/None handler
    yields None so the Api can tolerate absent handlers (stub-friendly).
    """
    if handlers is None:
        return None
    if isinstance(handlers, dict):
        fn = handlers.get(name)
    else:
        fn = getattr(handlers, name, None)
    return fn if callable(fn) else None


class Api:
    """The js_api object exposed to the page via ``create_window(js_api=...)``.

    Each public method is one of the LOCKED panel.html actions and simply
    forwards to its injected handler (if present). Absent handlers are tolerated
    so this phase can wire stubs and Phases 8/9 fill them in. No credential is
    logged; arguments are passed straight through.

    ``state_provider`` is an optional zero-arg callable returning either a
    ``(PrintState, ConnectionStatus, logged_in, theme)`` tuple or a prebuilt
    state dict; :meth:`get_initial_state` uses it to seed the page on
    ``pywebviewready``.
    """

    def __init__(self, handlers=None, *, state_provider=None):
        self._handlers = handlers
        self._state_provider = state_provider

    def _call(self, name, *args):
        fn = _resolve_handler(self._handlers, name)
        if fn is not None:
            return fn(*args)
        return None

    # --- page actions (page -> Python) ------------------------------------ #

    def hide(self):
        return self._call("hide")

    def login_submit(self, email, password):
        # Forward raw; never log or store the password here (T-07-02).
        return self._call("login_submit", email, password)

    def submit_code(self, code):
        return self._call("submit_code", code)

    def resend_code(self):
        return self._call("resend_code")

    def select_printer(self, device_id):
        return self._call("select_printer", device_id)

    def open_printer_select(self):
        # Gear button: re-fetch the bound device list and show the select screen
        # while logged in (no secret crosses; only display rows are pushed back).
        return self._call("open_printer_select")

    def logout(self):
        return self._call("logout")

    def control(self, command):
        # The pause/resume/stop allowlist is enforced in control.py (Phase 5)
        # and re-checked when wired in Phase 9 (T-07-05).
        return self._call("control", command)

    def resize(self, height):
        # Pure window-geometry hint from the page: resize the flyout to fit its
        # rendered content height (no secret, no printer interaction).
        return self._call("resize", height)

    # --- initial pull (Python -> page seed) ------------------------------- #

    def get_initial_state(self):
        """Return the initial panel state dict (never contains a secret).

        Uses the injected ``state_provider``; if it yields a tuple it is run
        through :func:`serialize_state`, if it yields a dict it is returned as-is.
        With no provider, returns a logged-out login state.
        """
        if self._state_provider is None:
            return serialize_state(
                PrintState(), ConnectionStatus.DISCONNECTED, logged_in=False
            )
        result = self._state_provider()
        if isinstance(result, dict):
            return result
        state, connection, logged_in, theme = result
        return serialize_state(
            state, connection, logged_in=logged_in, theme=theme
        )
