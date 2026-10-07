"""Plugin settings resolution, shared by the provider and the dashboard API.

Both entry points need the same answer to "which server do I talk to", and they
are loaded by different machinery: the provider is imported as a package
(``from .settings import ...`` works) while the dashboard's ``plugin_api.py`` is
loaded from its file path with no parent package, so it cannot use a relative
import. Keeping the resolution in one module means the two can agree without
``plugin_api.py`` having to reach into ``sys.path``.

Precedence, lowest to highest:

1. built-in defaults (loopback, 19528)
2. per-profile ``hyatlas.json``
3. ``config.yaml`` — the legacy ``plugins.hyatlas`` block, then
   ``plugins.entries.hyatlas.settings`` (what the Desktop settings form writes),
   layered key by key so a half-filled form cannot shadow the other block
4. ``HYATLAS_*`` environment variables
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)

# One definition of every setting, consumed by both the provider's
# ``get_config_schema()`` (what ``hermes memory setup`` renders) and the tests
# that pin ``plugin.yaml``'s ``config_schema``. Keeping these in separate places
# is how the two drifted: the manifest listed nine settings while the provider
# offered five, so four were invisible to setup.
#
# ``plugin.yaml`` is static YAML the manifest parser reads, so it cannot be
# generated from this — a test asserts the two agree instead.
SCHEMA: Tuple[Dict[str, Any], ...] = (
    {
        "key": "server_host", "type": "str", "default": "127.0.0.1",
        "label": "Server host", "description": "HyAtlas v4 server host",
    },
    {
        "key": "server_port", "type": "int", "default": 19528,
        "label": "Server port", "description": "HyAtlas v4 server port",
    },
    {
        "key": "user_id", "type": "str", "default": "default",
        "label": "User ID", "description": "Default user_id for memory scoping",
    },
    {
        "key": "agent_id", "type": "str", "default": "default",
        "label": "Agent ID",
        "description": "Default agent_id (overridden by agent identity per session)",
    },
    {
        "key": "auto_start", "type": "bool", "default": False,
        "label": "Auto-start server", "choices": [True, False],
        "description": "Spawn the hyatlas-go binary when the server is unreachable",
    },
    {
        "key": "binary_path", "type": "str", "default": "",
        "label": "Binary path",
        "description": "Path to hyatlas-go (empty = discover from PATH)",
    },
    {
        "key": "launcher_path", "type": "str", "default": "",
        "label": "Launcher script",
        "description": "Optional path to a hyatlas-go.ps1 that owns the server "
                       "environment (Windows; empty = spawn the binary directly)",
    },
    {
        "key": "request_timeout", "type": "float", "default": 15.0,
        "label": "Request timeout (s)",
        "description": "HTTP timeout for server calls",
    },
    {
        "key": "data_dir", "type": "str", "default": "",
        "label": "Data directory",
        "description": "Where the server keeps its vector store and graph; used "
                       "by `hermes backup` to include provider state. Empty = "
                       "HYATLAS_GO_DATA, then the conventional defaults.",
    },
    {
        "key": "mode", "type": "str", "default": "",
        "label": "Extraction mode", "choices": ["", "lite", "pro", "ultra"],
        "description": "Passed to a spawned server as HYATLAS_MODE. lite makes "
                       "no LLM call, so conversation text never leaves the "
                       "machine; pro extracts synchronously; ultra extracts in "
                       "the background and fills all 7 layers. Empty = the "
                       "server's own default (ultra). The server's "
                       "/api/v1/status `mode` field is authoritative at "
                       "runtime, since a manually started server may differ.",
    },
)

# The server treats an unrecognised HYATLAS_MODE as fatal rather than falling
# back, so an invalid value here would make a spawned server die on boot with a
# log line nobody reads. Validating in the plugin turns that into a message
# naming the three valid modes before anything is started.
VALID_MODES = ("lite", "pro", "ultra")

KEYS = tuple(f["key"] for f in SCHEMA)

DEFAULTS: Dict[str, Any] = {f["key"]: f["default"] for f in SCHEMA}


def config_schema() -> Tuple[Dict[str, Any], ...]:
    """The provider config schema, derived from :data:`SCHEMA`.

    Shaped for ``MemoryProvider.get_config_schema()``: ``key``, ``description``,
    ``default``, plus ``choices`` where declared. The ``label`` and ``type``
    fields belong to the manifest's ``config_schema``, which the Desktop
    settings form reads, so they are dropped here rather than duplicated.
    """
    out = []
    for f in SCHEMA:
        item: Dict[str, Any] = {
            "key": f["key"],
            "description": f["description"],
            "default": f["default"],
        }
        if "choices" in f:
            item["choices"] = f["choices"]
        out.append(item)
    return tuple(out)

# env var -> (settings key, cast)
ENV: Tuple[Tuple[str, str, Any], ...] = (
    ("HYATLAS_SERVER_HOST", "server_host", str),
    ("HYATLAS_SERVER_PORT", "server_port", int),
    ("HYATLAS_USER_ID", "user_id", str),
    ("HYATLAS_AGENT_ID", "agent_id", str),
    ("HYATLAS_AUTO_START", "auto_start", lambda v: v.lower() in ("1", "true", "yes")),
    ("HYATLAS_BINARY_PATH", "binary_path", str),
    ("HYATLAS_LAUNCHER_PATH", "launcher_path", str),
    ("HYATLAS_REQUEST_TIMEOUT", "request_timeout", float),
    ("HYATLAS_GO_DATA", "data_dir", str),
    ("HYATLAS_MODE", "mode", lambda v: v.lower()),
)

# v3.5-era names that must not bleed into v4 settings.
LEGACY_ENV = ("HY_MEMORY_HOST", "HY_MEMORY_PORT")


def home() -> Path:
    """The Hermes home the plugin is operating against."""
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))


def load() -> Dict[str, Any]:
    """Resolve settings across all four layers."""
    cfg: Dict[str, Any] = dict(DEFAULTS)

    profile = home() / "hyatlas.json"
    if profile.exists():
        try:
            raw = json.loads(profile.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k in KEYS:
                    if k in raw:
                        cfg[k] = raw[k]
            else:
                logger.debug("ignoring %s: not a mapping", profile)
        except (json.JSONDecodeError, OSError) as e:
            logger.debug("ignoring %s: %s", profile, e)

    try:
        import yaml  # hermes core dependency

        cfg_path = home() / "config.yaml"
        if cfg_path.exists():
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            plugins = data.get("plugins") or {}
            for source in (
                plugins.get("hyatlas") or {},
                ((plugins.get("entries") or {}).get("hyatlas") or {}).get("settings") or {},
            ):
                if not isinstance(source, dict):
                    continue
                for k in KEYS:
                    if source.get(k) is not None:
                        cfg[k] = source[k]
    except Exception as e:  # noqa: BLE001 — a bad config read must not break plugin load
        logger.debug("ignoring config.yaml settings: %s", e)

    for env_key, key, cast in ENV:
        v = os.environ.get(env_key, "").strip()
        if not v:
            continue
        try:
            cfg[key] = cast(v)
        except (TypeError, ValueError) as e:
            logger.debug("ignoring %s=%r: %s", env_key, v, e)

    for legacy in LEGACY_ENV:
        if legacy in os.environ:
            logger.debug("ignoring legacy env %s — use HYATLAS_SERVER_* instead", legacy)

    return cfg


def mode(cfg: Dict[str, Any] | None = None) -> str:
    """The configured extraction mode, or "" to let the server decide.

    Rejects a value the server would fatally refuse, naming the valid set —
    a spawned server that dies on boot reports it only in a log file.
    """
    v = str((cfg or load()).get("mode") or "").strip().lower()
    if not v:
        return ""
    if v not in VALID_MODES:
        raise ValueError(
            f"invalid hyatlas mode {v!r}; valid modes are {', '.join(VALID_MODES)} "
            f"(or leave it empty for the server default)"
        )
    return v


def base_url(cfg: Dict[str, Any] | None = None) -> str:
    """The server origin, honoring the configured host and port."""
    cfg = cfg or load()
    return f"http://{cfg.get('server_host') or '127.0.0.1'}:{cfg.get('server_port') or 19528}"
