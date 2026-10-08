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
        "label": "Auto-start server",
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
        "description": "Windows only. Optional hyatlas-go.ps1 that `hermes hyatlas "
                       "start|stop` runs instead of spawning the binary. Run as "
                       "given, so only point it at a script you trust. Empty = "
                       "spawn the binary directly.",
    },
    {
        "key": "request_timeout", "type": "float", "default": 15.0,
        "label": "Request timeout (s)",
        "description": "HTTP timeout for server calls",
    },
    {
        "key": "data_dir", "type": "str", "default": "",
        "label": "Data directory",
        "description": "Server data directory (vector store, graph). Passed to a "
                       "spawned server as HYATLAS_GO_DATA and used by `hermes "
                       "backup`. Empty = HYATLAS_GO_DATA, then the conventional "
                       "defaults.",
    },
    {
        "key": "llm_base", "type": "str", "default": "",
        "label": "LLM endpoint",
        "description": "Base URL of any OpenAI-compatible chat-completions API, "
                       "used for fact extraction and (in ultra) consolidation. "
                       "Forwarded to the server as HYATLAS_LLM_BASE. Not used in "
                       "lite mode, which makes no LLM call. Leave empty to use "
                       "the server's default.",
    },
    {
        "key": "llm_model", "type": "str", "default": "",
        "label": "LLM model",
        "description": "Model id at that endpoint, e.g. a `:free` tier. Forwarded "
                       "as HYATLAS_LLM_MODEL. Leave empty for the server default.",
    },
    {
        "key": "llm_key", "type": "str", "default": "", "secret": True,
        "env_var": "HYATLAS_LLM_KEY",
        "label": "LLM API key",
        "description": "API key for the endpoint above. The setup wizard stores it "
                       "in Hermes' .env (0600) as HYATLAS_LLM_KEY, never in "
                       "hyatlas.json. The plugin never reads or logs it. Not "
                       "needed in lite mode.",
    },
    {
        "key": "mode", "type": "str", "default": "",
        "label": "Extraction mode", "choices": ["", "lite", "pro", "ultra"],
        "description": "Passed to a spawned server as HYATLAS_MODE. lite makes "
                       "no LLM call, so no conversation text goes to an LLM; "
                       "pro sends each write to the LLM endpoint and reasons "
                       "within that turn; ultra also runs a periodic consolidation "
                       "pass over stored facts. Empty = the server's own default "
                       "(ultra). The server's /api/v1/status `mode` field is "
                       "authoritative at runtime, since a manually started server "
                       "may differ.",
    },
    {
        "key": "sync", "type": "str", "default": "",
        "label": "Block on extraction", "choices": ["", "on", "off"],
        "description": "Passed to a spawned server as HYATLAS_SYNC_EXTRACT. on "
                       "makes the write wait for extraction and report done or "
                       "failed; off returns immediately and extracts behind it. "
                       "Empty means off for a server this plugin spawns, so a "
                       "Hermes turn never waits on the LLM. This is a latency "
                       "choice and does not change what the mode can reason about.",
    },
)

# The server treats an unrecognised HYATLAS_MODE as fatal rather than falling
# back, so an invalid value here would make a spawned server die on boot with a
# log line nobody reads. Validating in the plugin turns that into a message
# naming the three valid modes before anything is started.
VALID_MODES = ("lite", "pro", "ultra")

# The sync knob is separate from the mode: capability follows the mode, latency
# follows this. Validated here for the same reason as mode — the server treats an
# unknown value as fatal, and a spawned server that dies on boot reports it only
# in a log file.
# Alias sets must match the server's ParseSync exactly. Two validators that
# disagree on one setting means the Desktop form accepts a value the server then
# treats as fatal — or rejects one the server would have honored.
VALID_SYNC = ("on", "off")
_SYNC_ALIASES = {
    "on": "on", "true": "on", "1": "on", "yes": "on",
    "off": "off", "false": "off", "0": "off", "no": "off",
}

KEYS = tuple(f["key"] for f in SCHEMA)

DEFAULTS: Dict[str, Any] = {f["key"]: f["default"] for f in SCHEMA}


# Fields the setup wizard reads. Anything else in SCHEMA is documentation for
# the manifest and must not leak into get_config_schema().
#
# `secret`, `env_var` and `url` are NOT cosmetic: hermes_cli.memory_setup masks
# secret prompts, routes their value to .env via env_var instead of the provider
# JSON, and prints `url` as "Get yours at ...". Dropping them silently turns an
# API key into a plaintext field in hyatlas.json, so they are passed through.
_WIZARD_FIELDS = ("choices", "secret", "env_var", "url", "when", "default_from")


def config_schema() -> Tuple[Dict[str, Any], ...]:
    """The provider config schema, derived from :data:`SCHEMA`.

    Shaped for ``MemoryProvider.get_config_schema()``: ``key``, ``description``,
    ``default``, plus the wizard-relevant fields. ``label`` and ``type`` belong to
    the manifest's ``config_schema``, which the Desktop settings form reads, so
    they are dropped here rather than duplicated.
    """
    out = []
    for f in SCHEMA:
        item: Dict[str, Any] = {
            "key": f["key"],
            "description": f["description"],
            "default": f["default"],
        }
        for extra in _WIZARD_FIELDS:
            if extra in f:
                item[extra] = f[extra]
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
    ("HYATLAS_SYNC_EXTRACT", "sync", lambda v: _SYNC_ALIASES.get(v.lower(), v.lower())),
    # Non-secret: safe to land in hyatlas.json. The key is secret and is read
    # straight from the environment by the server, never persisted by us.
    ("HYATLAS_LLM_BASE", "llm_base", str),
    ("HYATLAS_LLM_MODEL", "llm_model", str),
)

# The declared type of each key, so a JSON/YAML string ("19528") is interpreted
# the same way an environment variable is. Derived from ENV, never restated.
CASTS = {key: cast for _, key, cast in ENV}

# v3.5-era names that must not bleed into v4 settings.
LEGACY_ENV = ("HY_MEMORY_HOST", "HY_MEMORY_PORT")


def home() -> Path:
    """The Hermes home the plugin is operating against."""
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))


class _Skip:
    """Sentinel: a raw value that cannot be interpreted, so keep the default."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "<skip>"


_SKIP = _Skip()


def coerce(key: str, value: Any, cast: Any = None) -> Any:
    """Interpret one raw config value as the setting ``key``.

    Shared by all three input layers (hyatlas.json, config.yaml, environment) so
    a null or a mistyped value cannot survive into ``cfg``. That matters because
    consumers use ``cfg.get(k, default)``, whose fallback only fires when the key
    is ABSENT — a present-but-null key yields None, and ``float(None)`` then
    raises inside the plugin instead of using the documented default.

    ``cast`` is the ENV layer's string parser. It is applied only to strings, so
    an already-typed value from JSON or YAML (``19528``, ``true``) passes through
    rather than hitting a ``str``-shaped callable. Anything uninterpretable
    returns _SKIP and the caller keeps the default.
    """
    if value is None:
        return _SKIP
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return _SKIP
        if cast is None:
            return s
        try:
            return cast(s)
        except (TypeError, ValueError):
            return _SKIP
    return value


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
                        got = coerce(k, raw[k], CASTS.get(k))
                        if got is not _SKIP:
                            cfg[k] = got
            else:
                logger.debug("ignoring %s: not a mapping", profile)
        except (json.JSONDecodeError, OSError) as e:
            logger.debug("ignoring %s: %s", profile, e)

    try:
        try:
            # Current Hermes parses YAML with ruamel through hermes_yaml and no
            # longer ships PyYAML; without this the config.yaml layer would be
            # silently skipped.
            import hermes_yaml as yaml
        except ImportError:
            import yaml  # older Hermes releases

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
                    got = coerce(k, source.get(k), CASTS.get(k))
                    if got is not _SKIP:
                        cfg[k] = got
    except Exception as e:  # noqa: BLE001 — a bad config read must not break plugin load
        logger.debug("ignoring config.yaml settings: %s", e)

    for env_key, key, cast in ENV:
        v = os.environ.get(env_key, "").strip()
        if not v:
            continue
        got = coerce(key, v, cast)
        if got is _SKIP:
            logger.debug("ignoring %s=%r: not a valid %s", env_key, v, key)
            continue
        cfg[key] = got

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


def truthy(v: Any) -> bool:
    """Interpret a config value as a boolean.

    A bool field must not declare `choices`: the desktop bridge classifies any
    field with choices as a `select`, which coerces its value to a string, and
    then ``"false"`` is truthy in Python — so picking "false" in the settings
    panel turned auto-start ON. Coercing here also covers a hyatlas.json written
    before that was fixed.
    """
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def sync(cfg: Dict[str, Any] | None = None) -> str:
    """Whether a write blocks on extraction: "on", "off", or "" to follow the mode.

    Empty is the meaningful default — the server then applies the mode's own
    behaviour (pro blocks, ultra does not) rather than this plugin guessing at it.
    """
    v = str((cfg or load()).get("sync") or "").strip().lower()
    if not v:
        return ""
    if v not in _SYNC_ALIASES:
        raise ValueError(
            f"invalid hyatlas sync {v!r}; valid values are {', '.join(VALID_SYNC)} "
            f"(or leave it empty for the default)"
        )
    return _SYNC_ALIASES[v]


def base_url(cfg: Dict[str, Any] | None = None) -> str:
    """The server origin, honoring the configured host and port."""
    cfg = cfg or load()
    return f"http://{cfg.get('server_host') or '127.0.0.1'}:{cfg.get('server_port') or 19528}"
