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

import ipaddress
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

logger = logging.getLogger(__name__)

# One definition of every setting, consumed by both the provider's
# ``get_config_schema()`` (what ``hermes memory setup`` renders) and the tests
# that pin ``plugin.yaml``'s ``config_schema``. Keeping these in separate places
# is how the two drifted: the manifest listed nine settings while the provider
# offered five, so four were invisible to setup.
#
# ``plugin.yaml`` is static YAML the manifest parser reads, so it cannot be
# generated from this — a test asserts the two agree instead.
# The three mode menu items, in the order ``hermes memory setup`` shows them
# (ultra first, so it is the default). The wizard stores the whole string; every
# reader reduces it to its first word with :func:`normalize_mode`.
MODE_CHOICES = (
    "ultra — recommended: one AI call per turn, plus background consolidation, "
    "a knowledge graph and patterns",
    "pro — one AI call per conversation turn to extract facts; nothing runs in "
    "the background",
    "lite — no AI calls at all: everything stays on this machine (local BGE "
    "embeddings, keyword + vector search)",
)

# The Nous Portal endpoint and a free model on it: what Enter gives in the wizard.
DEFAULT_LLM_BASE = "https://inference-api.nousresearch.com/v1"
DEFAULT_LLM_MODEL = "meituan/longcat-2.5-preview:free"

# The setup wizard asks every field of ONBOARDING once, in this order, and nothing
# else: five plain questions a new user can answer. Keep each description short:
# it is the prompt text. Long explanations belong in the README, not in a prompt.
ONBOARDING: Tuple[Dict[str, Any], ...] = (
    {
        "key": "mode", "type": "str", "default": MODE_CHOICES[0],
        "label": "Mode", "choices": list(MODE_CHOICES),
        "description": "How much should HyAtlas think? (your memories always stay on "
                       "this machine)",
    },
    {
        "key": "llm_base", "type": "str", "default": DEFAULT_LLM_BASE,
        "label": "AI endpoint",
        "description": "AI endpoint (Enter = Nous Portal; not used in lite)",
    },
    {
        "key": "llm_key", "type": "str", "default": "", "secret": True,
        "env_var": "HYATLAS_LLM_KEY",
        "label": "AI API key",
        "description": "API key for that endpoint (saved in Hermes's .env; Enter to "
                       "skip in lite)",
    },
    {
        "key": "llm_model", "type": "str", "default": DEFAULT_LLM_MODEL,
        "label": "AI model",
        "description": "Model (Enter = a free Nous Portal model; not used in lite)",
    },
    {
        # A plain text answer, not a menu or a toggle: a y/N prompt is how every
        # other memory provider asks this. Every reader goes through truthy().
        "key": "auto_start", "type": "str", "default": "n",
        "label": "Auto-start server",
        "description": "Auto-start the HyAtlas server with Hermes? (y/N)",
    },
)

# Everything the wizard does not ask. These stay in plugin.yaml, so the Desktop
# settings form shows them, and are read and written with
# ``hermes hyatlas settings``. ``help`` is the one-line plain explanation that
# command prints; it is not part of the wizard or the manifest.
ADVANCED: Tuple[Dict[str, Any], ...] = (
    {
        "key": "sync", "type": "str", "default": "default",
        "label": "Advanced: block on extraction", "choices": ["default", "on", "off"],
        "description": "Advanced: wait for extraction before a reply finishes?",
        "help": "whether a reply waits for memory extraction; leave it on default",
    },
    {
        "key": "server_host", "type": "str", "default": "127.0.0.1",
        "label": "Advanced: server address",
        "description": "Advanced: where the HyAtlas server runs",
        "help": "where the HyAtlas server runs; keep 127.0.0.1 unless it is on another machine",
    },
    {
        "key": "server_port", "type": "int", "default": 19528,
        "label": "Advanced: server port", "description": "Advanced: server port",
        "help": "the port the HyAtlas server listens on; change it only if 19528 is taken",
    },
    {
        "key": "user_id", "type": "str", "default": "default",
        "label": "Advanced: user ID", "description": "Advanced: whose memories these are",
        "help": "the name your memories are filed under; use one per person",
    },
    {
        "key": "agent_id", "type": "str", "default": "default",
        "label": "Advanced: agent ID", "description": "Advanced: default agent name",
        "help": "the default agent name memories are tagged with",
    },
    {
        "key": "binary_path", "type": "str", "default": "",
        "label": "Advanced: binary path",
        "description": "Advanced: path to hyatlas-go (empty = find it on PATH)",
        "help": "where the hyatlas-go program is; empty means look for it on PATH",
    },
    {
        "key": "launcher_path", "type": "str", "default": "",
        "label": "Advanced: launcher script",
        "description": "Advanced: Windows launcher script (empty = run the binary)",
        "help": "a Windows launcher script to start the server with; empty runs the program directly",
    },
    {
        "key": "data_dir", "type": "str", "default": "",
        "label": "Advanced: data folder",
        "description": "Advanced: server data folder (empty = the usual default)",
        "help": "the folder where the server keeps its memories; empty uses the usual place",
    },
    {
        "key": "request_timeout", "type": "float", "default": 15.0,
        "label": "Advanced: request timeout (s)",
        "description": "Advanced: seconds to wait for the server",
        "help": "how many seconds to wait for the server before giving up",
    },
)

# Every setting, in wizard order and then the advanced ones. plugin.yaml declares
# all of them; ``get_config_schema()`` offers only ONBOARDING.
SCHEMA: Tuple[Dict[str, Any], ...] = ONBOARDING + ADVANCED

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
    """The provider config schema, derived from :data:`ONBOARDING`.

    Shaped for ``MemoryProvider.get_config_schema()``: ``key``, ``description``,
    ``default``, plus the wizard-relevant fields. ``label`` and ``type`` belong to
    the manifest's ``config_schema``, which the Desktop settings form reads, so
    they are dropped here rather than duplicated.
    """
    out = []
    for f in ONBOARDING:
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

def truthy(v: Any) -> bool:
    """Interpret a config value as a boolean.

    Accepts y/yes/true/on/1 in any case as True and n/no/false/off/0/"" as False;
    anything else is False. The setup wizard stores the user's typed y/n answer.

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
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


# env var -> (settings key, cast)
ENV: Tuple[Tuple[str, str, Any], ...] = (
    ("HYATLAS_SERVER_HOST", "server_host", str),
    ("HYATLAS_SERVER_PORT", "server_port", int),
    ("HYATLAS_USER_ID", "user_id", str),
    ("HYATLAS_AGENT_ID", "agent_id", str),
    ("HYATLAS_AUTO_START", "auto_start", truthy),
    ("HYATLAS_BINARY_PATH", "binary_path", str),
    ("HYATLAS_LAUNCHER_PATH", "launcher_path", str),
    ("HYATLAS_REQUEST_TIMEOUT", "request_timeout", float),
    ("HYATLAS_GO_DATA", "data_dir", str),
    ("HYATLAS_MODE", "mode", lambda v: normalize_mode(v)),
    ("HYATLAS_SYNC_EXTRACT", "sync", lambda v: _SYNC_ALIASES.get(v.lower(), v.lower())),
    # Non-secret: safe to land in hyatlas.json. The key is secret and is read
    # straight from the environment by the server, never persisted by us.
    ("HYATLAS_LLM_BASE", "llm_base", str),
    ("HYATLAS_LLM_MODEL", "llm_model", str),
)

# The declared type of each key, so a JSON/YAML string ("19528") is interpreted
# the same way an environment variable is. Derived from ENV, never restated.
CASTS = {key: cast for _, key, cast in ENV}

# v3.5-era names that must not bleed into the current settings.
LEGACY_ENV = ("HY_MEMORY_HOST", "HY_MEMORY_PORT")


def home() -> Path:
    """The Hermes home the plugin is operating against.

    HERMES_HOME when it is non-blank, else ~/.hermes. An empty HERMES_HOME counts
    as unset, as in hermes_constants.get_hermes_home(); taking "" as a path would
    quietly put config and logs in the working directory.
    """
    val = os.environ.get("HERMES_HOME", "").strip()
    return Path(val) if val else Path.home() / ".hermes"


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


# Profile files already reported as unreadable, so the warning appears once per
# file and not on every settings lookup (load runs many times per session).
_WARNED_UNREADABLE: set = set()


def _warn_unreadable(profile: Path, why: str) -> None:
    """Warn once that ``profile`` could not be read and its values are being ignored."""
    key = str(profile)
    if key in _WARNED_UNREADABLE:
        return
    _WARNED_UNREADABLE.add(key)
    logger.warning(
        "hyatlas: ignoring %s (%s); fix or delete the file. `hermes hyatlas settings set` "
        "saves the old one as hyatlas.json.bad before writing a new one.", profile, why)


def load() -> Dict[str, Any]:
    """Resolve settings across all four layers."""
    return load_with_sources()[0]


def load_with_sources() -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Resolve settings across all four layers, and say where each value came from.

    The second item maps every key to ``default``, ``hyatlas.json``, ``config.yaml``
    or ``env`` (the layer whose value won), for ``hermes hyatlas settings``.
    """
    cfg: Dict[str, Any] = dict(DEFAULTS)
    src: Dict[str, str] = {k: "default" for k in KEYS}

    profile = home() / "hyatlas.json"
    if profile.exists():
        try:
            # utf-8-sig: Notepad and PowerShell write a BOM, which plain utf-8 rejects.
            raw = json.loads(profile.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                for k in KEYS:
                    if k in raw:
                        got = coerce(k, raw[k], CASTS.get(k))
                        if got is not _SKIP:
                            cfg[k] = got
                            src[k] = "hyatlas.json"
            else:
                _warn_unreadable(profile, "it is not a JSON object")
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            _warn_unreadable(profile, type(e).__name__)

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
                        src[k] = "config.yaml"
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
        src[key] = "env"

    for legacy in LEGACY_ENV:
        if legacy in os.environ:
            logger.debug("ignoring legacy env %s — use HYATLAS_SERVER_* instead", legacy)

    # The wizard stores the typed y/N answer and the default is "n"; readers get a bool.
    cfg["auto_start"] = truthy(cfg.get("auto_start"))
    return cfg, src


def normalize_mode(v: Any) -> str:
    """Reduce a stored mode to ``ultra``, ``pro`` or ``lite``, or "" for unset.

    The setup wizard stores the whole menu line ("ultra — recommended: ..."),
    while an older config or an environment variable holds the bare word. Both mean
    the same thing, so every reader goes through here. An unknown word is returned
    lower-cased so :func:`mode` can reject it by name.
    """
    s = str(v or "").strip().lower()
    m = re.match(r"[a-z0-9_]+", s)
    return m.group(0) if m else s


def mode(cfg: Dict[str, Any] | None = None) -> str:
    """The configured extraction mode, or "" to let the server decide.

    Rejects a value the server would fatally refuse, naming the valid set —
    a spawned server that dies on boot reports it only in a log file.
    """
    v = normalize_mode((cfg or load()).get("mode"))
    if not v:
        return ""
    if v not in VALID_MODES:
        raise ValueError(
            f"invalid hyatlas mode {v!r}; valid modes are {', '.join(VALID_MODES)} "
            f"(or leave it empty for the server default)"
        )
    return v


def sync(cfg: Dict[str, Any] | None = None) -> str:
    """Whether a write blocks on extraction: "on", "off", or "" to follow the mode.

    Empty is the meaningful default — the server then applies the mode's own
    behaviour (pro blocks, ultra does not) rather than this plugin guessing at it.
    """
    v = str((cfg or load()).get("sync") or "").strip().lower()
    if not v or v == "default":
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


def is_loopback_host(host: str) -> bool:
    """True when ``host`` is this machine: localhost, 127.0.0.0/8 or ::1.

    A name other than ``localhost`` is not resolved, since a name can point
    anywhere; only a literal loopback address or ``localhost`` counts.
    """
    h = str(host or "").strip().strip("[]").lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


# Settings only a server that Hermes starts itself receives (as environment
# variables); a server started some other way keeps whatever it was started with.
SPAWNED_ONLY_KEYS = ("mode", "llm_base", "llm_model", "sync", "data_dir")

# The one-sentence explanation of that, shared by the doctor and
# ``hermes hyatlas settings set`` so the two cannot say different things.
SPAWNED_ONLY_NOTE = (
    "Hermes passes these only to a server it starts. Your server is started separately: "
    "set HYATLAS_LLM_MODEL / HYATLAS_LLM_BASE / HYATLAS_MODE where it starts (e.g. "
    "hyatlas-go.local.ps1), or turn on auto start with `hermes hyatlas settings set "
    "auto_start y`."
)

# Settings ``hermes hyatlas settings set`` refuses: the key is a secret and lives in
# Hermes's .env, never in hyatlas.json.
SECRET_KEYS = ("llm_key",)


def parse_value(key: str, raw: str) -> Any:
    """Validate and cast one typed setting value; raise ValueError naming the problem.

    Mode goes through :func:`normalize_mode` and must be a valid mode, sync must be
    a value the server accepts, ints and floats are parsed, and the auto_start
    answer is read with :func:`truthy`. Other text is kept as typed.
    """
    if key in SECRET_KEYS:
        raise ValueError(f"{key} is a secret; use `hermes memory setup` to set it")
    if key not in KEYS:
        names = ", ".join(k for k in KEYS if k not in SECRET_KEYS)
        raise ValueError(f"unknown setting {key!r}; the settings are {names}")
    text = str(raw).strip()
    if key == "mode":
        return mode({"mode": text}) if text else ""
    if key == "sync":
        return sync({"sync": text}) or "default"
    if key == "auto_start":
        return truthy(text)
    kind = next(f["type"] for f in SCHEMA if f["key"] == key)
    if kind == "int":
        try:
            return int(text)
        except ValueError:
            raise ValueError(f"{key} must be a whole number, got {text!r}") from None
    if kind == "float":
        try:
            return float(text)
        except ValueError:
            raise ValueError(f"{key} must be a number, got {text!r}") from None
    return text


def save_profile(
    updates: Dict[str, Any],
    hermes_home: "str | Path | None" = None,
    warn: Callable[[str], None] = logger.warning,
) -> Path:
    """Merge ``updates`` into ``hyatlas.json`` and return its path.

    Keys already in the file stay, so running setup again never wipes an advanced
    value. A secret is never written. A file that exists but is not a JSON object
    (a typo, a half-written edit) is not overwritten: it is renamed to
    ``hyatlas.json.bad``, replacing an older one, the new file holds only the
    merged answers, and ``warn`` is called with a message naming the ``.bad`` file.
    The write goes to a temporary file in the same folder and replaces the real one
    in a single step, so a crash never leaves half a file. Raises OSError when the
    file cannot be written.
    """
    path = (Path(hermes_home) if hermes_home else home()) / "hyatlas.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    current: Dict[str, Any] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raw = None
        if isinstance(raw, dict):
            current = raw
        else:
            bad = path.with_name(path.name + ".bad")
            os.replace(path, bad)
            warn(f"hyatlas: {path} was not valid JSON; it was kept as {bad} and a new "
                 "file was written.")
    merged = {**current, **updates}
    for secret in SECRET_KEYS:
        merged.pop(secret, None)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except (OSError, AttributeError):
            pass  # Windows
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path
