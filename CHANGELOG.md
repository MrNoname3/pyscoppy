# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it leaves
alpha.

## [Unreleased]

## [0.1.0-alpha] - 2026-06-22

First public alpha. Validated end to end on a real FHDM FScope-500K + Pico
running Scoppy firmware v18.

### Added
- Reverse-engineered Scoppy wire protocol (`pyscoppy/protocol.py`): framing,
  the v18 auth handshake, SYNC/SAMPLES decoders, FScope calibration, voltage
  ranges and the signal generator.
- A shared daemon (`scoppyd`) that owns the serial port and relays the live
  stream and controls over a local Unix socket, so a human (browser GUI), the
  CLI and an AI agent drive the same scope at once and stay in sync.
- Browser oscilloscope: YT/XY/FFT views, cursors, math channel, 15 measurements,
  CSV export, probe factors and a logic-analyzer mode.
- CLI (`python3 -m pyscoppy`): `up`, `daemon`, `gui`, `state`, `stream`,
  `grab --plot`, `set`, `info`.
- One-click launcher (`run.py`) and split launchers (`run-daemon.sh`,
  `run-gui.sh`).
- Hardware-free unit tests for the protocol, a `run-checks.sh` gate, and CI
  (GitHub/Gitea) running tests on Python 3.8–3.12 plus a strict type-check.
- Packaging (`pyproject.toml`) with a `pyscoppy` console entry point.

[Unreleased]: https://github.com/MrNoname3/pyscoppy/compare/v0.1.0-alpha...HEAD
[0.1.0-alpha]: https://github.com/MrNoname3/pyscoppy/releases/tag/v0.1.0-alpha
