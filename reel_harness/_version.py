"""Single source of truth for the package version (PEP 440). Every other
place that needs a version string -- pyproject.toml's [project].version,
`reel-harness --version`, the `/status` API, the release manifest, and
docs -- must read from here (or, for pyproject.toml, be kept manually in
sync and verified by tests/unit/test_version.py) rather than hardcoding
its own copy.

Release candidates use the PEP 440 `rcN` suffix (e.g. "0.1.0rc1"); the git
tag for the same release prefixes it with "v" (e.g. "v0.1.0rc1") -- the
same identifier, not a second format.
"""

__version__ = "0.1.0"
