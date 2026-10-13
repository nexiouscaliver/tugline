# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - unreleased until tagged

### Added

- Repository skeleton: MIT license, uv-based packaging with hatchling, src
  layout, CI on Python 3.10 and 3.14, release workflow using PyPI Trusted
  Publishing.
- Core library: append-only spool with delivery marks, arming and
  idle-threshold policy, one-shot ntfy publisher with a bearer token file,
  daemon with retry and backoff publishing plus long-poll reply capture,
  TOML config with loud validation of every key.
- `tug` CLI: `notify`, `arm`, `disarm`, `test`, `status`, `daemon`
  (with `--once`), `version`, and the adapter entries `hook-zcode`,
  `install zcode`, `uninstall zcode`, `hook-claude`.
- zcode adapter: hook entry, byte-preserving installer and uninstaller for
  `~/.zcode/cli/config.json`, and a bounded fail-closed approve flow behind
  the approve toggle (off by default).
- Claude Code adapter: hook entry, append-only installer and uninstaller
  for `~/.claude/settings.json`, and a bounded fail-closed approve flow
  behind the approve toggle (off by default).
- Documentation set: README, quickstart, channels playbook, adapter
  guides, Codex and Aider recipes, always-on hosting sketch, comparison
  table, security policy, contributing guide.

## [0.0.1]

- Repository skeleton: MIT license, uv-based packaging with hatchling, src
  layout; `tug` CLI entry point with `--version` and a `hello` smoke
  command; GitHub Actions CI with uv on a Python 3.10 and 3.14 matrix;
  release workflow for PyPI Trusted Publishing on `v*` tags.
