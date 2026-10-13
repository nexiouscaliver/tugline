"""Client configuration.

The config file is ~/.tugline/config.toml, unless TUGLINE_CONFIG points
somewhere else. On first run (file absent) a documented default
template is written, then loaded.

Supported sections and keys, all optional per key (drop a key to get
its default, but keep the section headers):

[server]
url          = "http://127.0.0.1:2586"   # ntfy base URL
token_file   = "~/.tugline/credentials"  # one line: the bearer token
approve_port = 2587                      # loopback port for the approve receiver
external_url = ""                        # base the approve buttons point at (empty keeps url)

[topics]
main  = "tug"        # routine notifications
warn  = "tug-warn"   # permission requests and urgent pings
reply = "tug-reply"  # topic the daemon polls for phone replies

[policy]
idle_seconds = 600   # a Stop longer than this notifies even when not armed

[toggles]
notify  = true    # master switch for publishing notifications
replies = true    # reply polling
approve = false   # approve action buttons; needs the daemon receiver and a reachable external_url

Anything unknown, missing a section header, or typed wrong is a loud
one-line ConfigError. Python 3.10 ships no tomllib, so a small built-in
parser covering this file's syntax is the fallback there.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None

DEFAULT_CONFIG_PATH = os.path.join("~", ".tugline", "config.toml")

DEFAULT_TEMPLATE = """\
# tugline client configuration.
#
# Sections are required; keys are optional (defaults shown).
# Unknown sections or keys are errors, so typos get caught early.

[server]
# ntfy base URL.
url = "http://127.0.0.1:2586"
# File holding the ntfy bearer token, one line. Never printed anywhere.
token_file = "~/.tugline/credentials"
# Loopback port the daemon's approve receiver listens on (the buttons
# target it; 0 picks a free port, which tests use).
approve_port = 2587
# Base URL the approve buttons point at when it differs from url, for
# example a tailnet serve host that proxies /approvals back to this
# machine. Empty keeps url.
external_url = ""

[topics]
# main: routine notifications. warn: permission requests, urgent pings.
# reply: topic the daemon polls for replies sent from the phone.
main = "tug"
warn = "tug-warn"
reply = "tug-reply"

[policy]
# A Stop event longer than this many seconds notifies even when the
# session is not armed. tug arm / tug disarm control the armed marker.
idle_seconds = 600

[toggles]
# Master switches. Turning one off restores the behavior from before
# tugline was installed: nothing published, nothing polled.
notify = true
replies = true
# Approve action buttons on notifications. Not wired up yet.
approve = false
"""

_KNOWN = {
    "server": {
        "url": str,
        "token_file": str,
        "approve_port": int,
        "external_url": str,
    },
    "topics": {"main": str, "warn": str, "reply": str},
    "policy": {"idle_seconds": (int, float)},
    "toggles": {"notify": bool, "replies": bool, "approve": bool},
}

_DEFAULTS = {
    "server": {
        "url": "http://127.0.0.1:2586",
        "token_file": "~/.tugline/credentials",
        "approve_port": 2587,
        "external_url": "",
    },
    "topics": {"main": "tug", "warn": "tug-warn", "reply": "tug-reply"},
    "policy": {"idle_seconds": 600},
    "toggles": {"notify": True, "replies": True, "approve": False},
}


class ConfigError(Exception):
    """One-line configuration problem, safe to print."""


@dataclass
class ServerConfig:
    url: str
    token_file: str
    approve_port: int = 2587
    external_url: str = ""

    @property
    def action_base(self):
        """Base URL the approve buttons point at: external_url when it
        is set (a host that routes /approvals back to this machine's
        receiver), the ntfy url otherwise."""
        return (self.external_url or self.url).rstrip("/")


@dataclass
class TopicsConfig:
    main: str
    warn: str
    reply: str


@dataclass
class PolicyConfig:
    idle_seconds: float


@dataclass
class TogglesConfig:
    notify: bool
    replies: bool
    approve: bool


@dataclass
class Config:
    server: ServerConfig
    topics: TopicsConfig
    policy: PolicyConfig
    toggles: TogglesConfig
    path: str


def _resolve_path(path=None):
    if path:
        return os.path.expanduser(path)
    env = os.environ.get("TUGLINE_CONFIG")
    if env:
        return os.path.expanduser(env)
    return os.path.expanduser(DEFAULT_CONFIG_PATH)


def load_config(path=None) -> Config:
    """Load config from path, TUGLINE_CONFIG, or the default location."""
    target = _resolve_path(path)
    if not os.path.exists(target):
        _write_template(target)
    try:
        with open(target, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise ConfigError("cannot read config %s: %s" % (target, exc)) from None

    data = _loads(text)
    return _build(data, target)


def _write_template(target):
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(DEFAULT_TEMPLATE)
    os.replace(tmp, target)


def _loads(text):
    if tomllib is not None:
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError("config is not valid TOML: %s" % exc) from None
    return _mini_toml_loads(text)


def _mini_toml_loads(text):
    """Parse the subset of TOML this config uses. Fallback for 3.10."""
    data = {}
    section = None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            end = line.find("]")
            name = line[1:end].strip() if end != -1 else ""
            tail = line[end + 1 :].strip() if end != -1 else line[1:]
            if not name or "=" in name or (tail and not tail.startswith("#")):
                raise ConfigError("config line %d: bad section header" % lineno)
            section = name
            data.setdefault(section, {})
            continue
        if "=" not in line:
            raise ConfigError("config line %d: cannot parse %r" % (lineno, line))
        key, _, rest = line.partition("=")
        key = key.strip().strip('"')
        if not key:
            raise ConfigError("config line %d: empty key" % lineno)
        value = _mini_value(rest.strip(), lineno)
        data.setdefault(section or "", {})[key] = value
    return data


def _mini_value(text, lineno):
    if not text:
        raise ConfigError("config line %d: missing value" % lineno)
    if text.startswith('"'):
        return _mini_string(text, lineno)
    # strip a trailing comment on bare values
    for cut in (" #", "\t#"):
        if cut in text:
            text = text.split(cut, 1)[0].strip()
    if text == "true":
        return True
    if text == "false":
        return False
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            pass
    raise ConfigError("config line %d: cannot parse value %r" % (lineno, text))


def _mini_string(text, lineno):
    out = []
    i = 1
    while i < len(text):
        ch = text[i]
        if ch == '"':
            tail = text[i + 1 :].strip()
            if tail and not tail.startswith("#"):
                raise ConfigError(
                    "config line %d: trailing junk after string" % lineno
                )
            return "".join(out)
        if ch == "\\":
            i += 1
            if i >= len(text):
                break
            esc = text[i]
            out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(esc, esc))
        else:
            out.append(ch)
        i += 1
    raise ConfigError("config line %d: unterminated string" % lineno)


def _build(data, path):
    for name in _KNOWN:
        if name not in data:
            raise ConfigError("config missing [%s] section: %s" % (name, path))
    for name in data:
        if name not in _KNOWN:
            raise ConfigError("config has unknown [%s] section: %s" % (name, path))
        if not isinstance(data[name], dict):
            raise ConfigError("config [%s] must be a table: %s" % (name, path))

    server = _section(data, "server", path)
    topics = _section(data, "topics", path)
    policy = _section(data, "policy", path)
    toggles = _section(data, "toggles", path)

    url = _check_type(server, "server", "url", str, path)
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ConfigError("config [server] url must start with http:// or https://: %s" % path)
    token_file = _check_type(server, "server", "token_file", str, path)
    approve_port = _check_type(server, "server", "approve_port", int, path)
    if not 0 <= approve_port <= 65535:
        raise ConfigError(
            "config [server] approve_port must be between 0 and 65535: %s" % path
        )
    external_url = _check_type(server, "server", "external_url", str, path)
    if external_url and not (
        external_url.startswith("http://")
        or external_url.startswith("https://")
    ):
        raise ConfigError(
            "config [server] external_url must start with http:// or "
            "https://, or be empty: %s" % path
        )
    for key in ("main", "warn", "reply"):
        value = _check_type(topics, "topics", key, str, path)
        if not value:
            raise ConfigError("config [topics] %s must not be empty: %s" % (key, path))
    idle = _check_type(policy, "policy", "idle_seconds", (int, float), path)
    if isinstance(idle, bool) or idle <= 0:
        raise ConfigError("config [policy] idle_seconds must be a positive number: %s" % path)
    for key in ("notify", "replies", "approve"):
        _check_type(toggles, "toggles", key, bool, path)

    return Config(
        server=ServerConfig(
            url=url,
            token_file=os.path.expanduser(token_file),
            approve_port=approve_port,
            external_url=external_url,
        ),
        topics=TopicsConfig(
            main=topics["main"], warn=topics["warn"], reply=topics["reply"]
        ),
        policy=PolicyConfig(idle_seconds=float(idle)),
        toggles=TogglesConfig(
            notify=toggles["notify"],
            replies=toggles["replies"],
            approve=toggles["approve"],
        ),
        path=path,
    )


def _section(data, name, path):
    merged = dict(_DEFAULTS[name])
    for key, value in data[name].items():
        if key not in _KNOWN[name]:
            raise ConfigError(
                "config [%s] has unknown key %r: %s" % (name, key, path)
            )
        merged[key] = value
    return merged


def _check_type(section, table, key, types, path):
    value = section[key]
    if not isinstance(value, types) or isinstance(value, bool) and types is not bool:
        want = types if isinstance(types, tuple) else (types,)
        names = "/".join(t.__name__ for t in want)
        raise ConfigError(
            "config [%s] %s must be %s, got %r: %s"
            % (table, key, names, value, path)
        )
    return value
