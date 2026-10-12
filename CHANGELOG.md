# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Repository skeleton: MIT license, uv-based packaging with hatchling, src layout.
- `tug` CLI entry point with `--version` and a `hello` smoke command.
- CI workflow: GitHub Actions with uv, Python 3.10 and 3.14 matrix.
- Release workflow: PyPI Trusted Publishing on `v*` tags (pending the Trusted
  Publisher registration described in the workflow header).
