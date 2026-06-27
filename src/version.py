"""The single canonical version for Bambu Lab Systray.

``__version__`` is the ONLY version literal in the repo (D-01). Three consumers
read it: the PyInstaller spec (exe metadata), the flyout panel footer, and
Phase 13's updater (via the compare helper here). A reviewer can grep the repo
and find exactly one version string.

Pure module: no I/O, no threads -- trivially unit-testable, matching the
render.py / status.py / state.py style.
"""

from packaging.version import Version, parse as _parse

# The ONE version literal. PEP 440 / semver, NO leading "v"
# (the "v" prefix is a git-tag/Release convention only -- D-02).
__version__ = "2.1.0"


def parse_version(value: str | None = None) -> Version:
    """Parse a version string into a real packaging Version (D-04).

    Tolerates an optional leading 'v'/'V' on either side so a tag like
    'v2.1.0' compares equal to the in-code '2.1.0'. Defaults to __version__.
    Never use string '>'/'!=' to compare versions -- use the Version objects
    this returns (so 2.10.0 > 2.9.0 is correct).
    """
    raw = __version__ if value is None else str(value)
    return _parse(raw.lstrip("vV"))


def version_tuple(value: str | None = None) -> tuple[int, int, int, int]:
    """Return a 4-int (major, minor, micro, build) tuple for VSVersionInfo (D-06).

    Pads missing release components with 0 to length 4 and truncates extras,
    so '2.1.0' -> (2, 1, 0, 0). Tolerates a leading 'v'/'V'.
    """
    release = parse_version(value).release  # e.g. (2, 1, 0)
    padded = (tuple(release) + (0, 0, 0, 0))[:4]
    return tuple(int(n) for n in padded)
