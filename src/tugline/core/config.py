"""Client configuration.

The config file is <tugline home>/config.toml (~/.tugline unless
TUGLINE_HOME moves it), unless TUGLINE_CONFIG points somewhere else.
On first run (file absent) a documented default template is written,
then loaded.

Supported sections and keys, all optional per key (drop a key to get
its default, but keep the section headers):

[server]
url          = "http://127.0.0.1:2586"   # ntfy base URL
token_file   = "<home>/credentials"      # one line: the bearer token
approve_port = 2587                      # loopback port for the approve receiver
external_url = ""                        # base the approve buttons point at (empty keeps url)
console_port = 2588                      # loopback port for the console

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
context_titles = true  # session context in notification titles and bodies ("[claude] project - turn finished")
console = true    # the tug console command
wake    = true    # wake idle sessions the moment a bound reply lands

[wake]
# Wake-on-reply: when a reply bound to a session lands while that
# session looks idle (its last spooled event is a stop or a session
# start), the daemon spawns the adapter's driver with the reply body
# as the prompt, resuming the session at once instead of waiting for
# the next turn. Off restores the pre-wake behavior exactly.
adapters = ["claude", "zcode"]  # adapters with a wake driver
dry_run = true         # log and record the exact command without spawning
timeout_seconds = 1800 # one wake is killed after this many seconds
max_concurrent = 1     # global cap on wakes running at once
claude_command = [
  "/Users/shahil/.local/bin/claude",
  "--resume",
  "{session}",
  "-p",
  "{body}",
]
zcode_command = [
  "node",
  "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs",
  "--resume",
  "{session}",
  "-p",
  "{body}",
  "--cwd",
  "{cwd}",
]

Anything unknown, missing a section header, or typed wrong is a loud
one-line ConfigError. The [wake] section is optional: a config from
before the feature keeps loading with the defaults above. Python 3.10
ships no tomllib, so a small built-in parser covering this file's
syntax (including single-line arrays of strings) is the fallback there.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from tugline.core import paths
from tugline.core.wake import DEFAULT_CLAUDE_COMMAND, DEFAULT_ZCODE_COMMAND

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None

DEFAULT_CONFIG_PATH = os.path.join("~", ".tugline", "config.toml")
TOKEN_FILE_PLACEHOLDER = "__TUGLINE_TOKEN_FILE__"

DEFAULT_TEMPLATE = """\
# tugline client configuration.
#
# Sections are required; keys are optional (defaults shown).
# Unknown sections or keys are errors, so typos get caught early.

[server]
# ntfy base URL.
url = "http://127.0.0.1:2586"
# File holding the ntfy bearer token, one line. Never printed anywhere.
token_file = "__TUGLINE_TOKEN_FILE__"
# Loopback port the daemon's approve receiver listens on (the buttons
# target it; 0 picks a free port, which tests use).
approve_port = 2587
# Base URL the approve buttons point at when it differs from url, for
# example a tailnet serve host that proxies /approvals back to this
# machine. Empty keeps url.
external_url = ""
# Loopback port the console serves on (0 picks a free port, which
# tests use).
console_port = 2588

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
# Approve action buttons on notifications; needs the daemon receiver
# and a reachable external_url.
approve = false
# Session context (harness, project, session, finished vs waiting) in
# adapter notification titles and bodies. Off restores the plain strings.
context_titles = true
# The tug console: a loopback web view over this same home.
console = true
# Wake idle sessions the moment a reply bound to them lands (see
# [wake]). Off restores the pre-wake behavior exactly.
wake = true

[wake]
# Wake-on-reply. When a reply bound to a session lands while that
# session looks idle (its last spooled event is a stop or a session
# start), the daemon spawns the adapter's driver with the reply body
# as the prompt, resuming the session on the spot.
# Adapters with a wake driver.
adapters = ["claude", "zcode"]
# Safe-live-proof mode: log and record the exact command without
# spawning anything. Flip to false after checking the wake journal.
dry_run = true
# One wake is killed after this many seconds.
timeout_seconds = 1800
# Global cap on wakes running at once.
max_concurrent = 1
# The claude driver command. {session} and {body} are substituted at
# dispatch; the flag order below is verified against claude --help
# (v2.1.299: -r, --resume [value]; -p, --print).
claude_command = [
  "/Users/shahil/.local/bin/claude",
  "--resume",
  "{session}",
  "-p",
  "{body}",
]
# The zcode driver command, run through node (the app ships a .cjs
# bundle, not an executable). {session}, {body}, and {cwd} are
# substituted at dispatch; the flags are verified against the
# bundled CLI's own help (v0.16.9: -p, --resume, --cwd; --mode left
# at its documented --prompt default, yolo). The path is the
# ZCode.app-bundled CLI, version-locked to the installed app.
zcode_command = [
  "node",
  "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs",
  "--resume",
  "{session}",
  "-p",
  "{body}",
  "--cwd",
  "{cwd}",
]
"""

_KNOWN = {
    "server": {
        "url": str,
        "token_file": str,
        "approve_port": int,
        "external_url": str,
        "console_port": int,
    },
    "topics": {"main": str, "warn": str, "reply": str},
    "policy": {"idle_seconds": (int, float)},
    "toggles": {
        "notify": bool,
        "replies": bool,
        "approve": bool,
        "context_titles": bool,
        "console": bool,
        "wake": bool,
    },
    # optional section: absent means the defaults (configs written
    # before the wake feature keep loading unchanged)
    "wake": {
        "adapters": list,
        "dry_run": bool,
        "timeout_seconds": (int, float),
        "max_concurrent": int,
        "claude_command": list,
        "zcode_command": list,
    },
}

_DEFAULTS = {
    "server": {
        "url": "http://127.0.0.1:2586",
        "token_file": "~/.tugline/credentials",
        "approve_port": 2587,
        "external_url": "",
        "console_port": 2588,
    },
    "topics": {"main": "tug", "warn": "tug-warn", "reply": "tug-reply"},
    "policy": {"idle_seconds": 600},
    "toggles": {
        "notify": True,
        "replies": True,
        "approve": False,
        "context_titles": True,
        "console": True,
        "wake": True,
    },
    "wake": {
        "adapters": ["claude", "zcode"],
        "dry_run": True,
        "timeout_seconds": 1800,
        "max_concurrent": 1,
        "claude_command": DEFAULT_CLAUDE_COMMAND,
        "zcode_command": DEFAULT_ZCODE_COMMAND,
    },
}

# The wake section is optional; sections that must be present.
_REQUIRED = ("server", "topics", "policy", "toggles")


class ConfigError(Exception):
    """One-line configuration problem, safe to print."""


@dataclass
class ServerConfig:
    url: str
    token_file: str
    approve_port: int = 2587
    external_url: str = ""
    console_port: int = 2588

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
    context_titles: bool = True
    console: bool = True
    wake: bool = True


@dataclass
class WakeConfig:
    """Wake-on-reply settings (the optional [wake] section).

    claude_command and zcode_command are argv templates: {session}
    and {body} are substituted at dispatch time, plus {cwd} (the
    session's project directory) for the zcode --cwd flag."""

    adapters: list = None
    dry_run: bool = True
    timeout_seconds: float = 1800.0
    max_concurrent: int = 1
    claude_command: list = None
    zcode_command: list = None

    def __post_init__(self):
        if self.adapters is None:
            self.adapters = ["claude", "zcode"]
        if self.claude_command is None:
            self.claude_command = list(DEFAULT_CLAUDE_COMMAND)
        if self.zcode_command is None:
            self.zcode_command = list(DEFAULT_ZCODE_COMMAND)


@dataclass
class Config:
    server: ServerConfig
    topics: TopicsConfig
    policy: PolicyConfig
    toggles: TogglesConfig
    path: str
    wake: WakeConfig = field(default_factory=WakeConfig)


def _resolve_path(path=None):
    if path:
        return os.path.expanduser(path)
    env = os.environ.get("TUGLINE_CONFIG")
    if env:
        return os.path.expanduser(env)
    return os.path.join(paths.home_root(), "config.toml")


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
    token_default = os.path.join(paths.home_root(), "credentials")
    text = DEFAULT_TEMPLATE.replace(TOKEN_FILE_PLACEHOLDER, token_default)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
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
    lines = text.splitlines()
    lineno = 0
    while lineno < len(lines):
        start = lineno + 1
        line = lines[lineno].strip()
        lineno += 1
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            end = line.find("]")
            name = line[1:end].strip() if end != -1 else ""
            tail = line[end + 1 :].strip() if end != -1 else line[1:]
            if not name or "=" in name or (tail and not tail.startswith("#")):
                raise ConfigError("config line %d: bad section header" % start)
            section = name
            data.setdefault(section, {})
            continue
        if "=" not in line:
            raise ConfigError("config line %d: cannot parse %r" % (start, line))
        key, _, rest = line.partition("=")
        key = key.strip().strip('"')
        if not key:
            raise ConfigError("config line %d: empty key" % start)
        value = rest.strip()
        if value.startswith("[") and "]" not in value:
            # a multiline array: join until the bracket closes
            while "]" not in value and lineno < len(lines):
                cont = lines[lineno].strip()
                lineno += 1
                if not cont or cont.startswith("#"):
                    continue
                value += " " + cont
        parsed = _mini_value(value, start)
        data.setdefault(section or "", {})[key] = parsed
    return data


def _mini_value(text, lineno):
    if not text:
        raise ConfigError("config line %d: missing value" % lineno)
    if text.startswith('"'):
        return _mini_string(text, lineno)
    if text.startswith("["):
        return _mini_array(text, lineno)
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


def _mini_array(text, lineno):
    """A flat array of quoted strings or bare words, single line only.

    Covers exactly what the [wake] section uses (adapters,
    claude_command). Nested arrays, inline tables, and multiline
    brackets stay errors: loud and early beats half-parsed."""
    end = text.find("]")
    if end == -1:
        raise ConfigError("config line %d: unterminated array" % lineno)
    tail = text[end + 1 :].strip()
    if tail and not tail.startswith("#"):
        raise ConfigError(
            "config line %d: trailing junk after array" % lineno
        )
    inner = text[1:end].strip()
    if not inner:
        raise ConfigError("config line %d: empty array" % lineno)
    if inner.endswith(","):
        inner = inner[:-1].strip()  # tolerate a trailing comma
    items = []
    for part in inner.split(","):
        part = part.strip()
        for cut in (" #", "\t#"):
            if cut in part:
                part = part.split(cut, 1)[0].strip()
        if not part:
            raise ConfigError(
                "config line %d: empty array element" % lineno
            )
        if part.startswith('"'):
            items.append(_mini_string(part, lineno))
        elif part == "true":
            items.append(True)
        elif part == "false":
            items.append(False)
        else:
            for cast in (int, float):
                try:
                    items.append(cast(part))
                    break
                except ValueError:
                    pass
            else:
                raise ConfigError(
                    "config line %d: cannot parse array value %r"
                    % (lineno, part)
                )
    return items


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
    for name in _REQUIRED:
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
    # [wake] is optional: absent means defaults (pre-wake configs keep
    # loading), present means every key is still checked
    wake = _section({"wake": data.get("wake", {})}, "wake", path)

    url = _check_type(server, "server", "url", str, path)
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ConfigError("config [server] url must start with http:// or https://: %s" % path)
    token_file = _check_type(server, "server", "token_file", str, path)
    approve_port = _check_type(server, "server", "approve_port", int, path)
    if not 0 <= approve_port <= 65535:
        raise ConfigError(
            "config [server] approve_port must be between 0 and 65535: %s" % path
        )
    console_port = _check_type(server, "server", "console_port", int, path)
    if not 0 <= console_port <= 65535:
        raise ConfigError(
            "config [server] console_port must be between 0 and 65535: %s" % path
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
    for key in ("notify", "replies", "approve", "context_titles", "console",
                "wake"):
        _check_type(toggles, "toggles", key, bool, path)

    adapters = _check_wake_strings(wake, "adapters", path)
    for key in ("claude_command", "zcode_command"):
        command = _check_wake_strings(wake, key, path)
        if not any("{session}" == part for part in command) or not any(
            "{body}" == part for part in command
        ):
            raise ConfigError(
                "config [wake] %s needs a {session} and a {body}"
                " argv entry: %s" % (key, path)
            )
    timeout = _check_type(wake, "wake", "timeout_seconds", (int, float), path)
    if isinstance(timeout, bool) or timeout <= 0:
        raise ConfigError(
            "config [wake] timeout_seconds must be a positive number: %s" % path
        )
    max_concurrent = _check_type(wake, "wake", "max_concurrent", int, path)
    if isinstance(max_concurrent, bool) or max_concurrent < 1:
        raise ConfigError(
            "config [wake] max_concurrent must be at least 1: %s" % path
        )
    dry_run = _check_type(wake, "wake", "dry_run", bool, path)

    return Config(
        server=ServerConfig(
            url=url,
            token_file=os.path.expanduser(token_file),
            approve_port=approve_port,
            external_url=external_url,
            console_port=console_port,
        ),
        topics=TopicsConfig(
            main=topics["main"], warn=topics["warn"], reply=topics["reply"]
        ),
        policy=PolicyConfig(idle_seconds=float(idle)),
        toggles=TogglesConfig(
            notify=toggles["notify"],
            replies=toggles["replies"],
            approve=toggles["approve"],
            context_titles=toggles["context_titles"],
            console=toggles["console"],
            wake=toggles["wake"],
        ),
        wake=WakeConfig(
            adapters=adapters,
            dry_run=dry_run,
            timeout_seconds=float(timeout),
            max_concurrent=max_concurrent,
            claude_command=list(wake["claude_command"]),
            zcode_command=list(wake["zcode_command"]),
        ),
        path=path,
    )


def _check_wake_strings(section, key, path) -> list:
    """One [wake] list key checked as a non-empty list of strings."""
    value = section[key]
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) for item in value
    ):
        raise ConfigError(
            "config [wake] %s must be a non-empty list of strings: %s"
            % (key, path)
        )
    return list(value)


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
