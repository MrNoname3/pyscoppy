# Contributing to pyscoppy

Thanks for taking a look! This is an early-alpha hobby project, and the single
most useful contribution is **testing it on hardware I don't have** — other
boards, firmware versions and operating systems. Bug reports with your setup
details (see the issue template) are genuinely valuable.

## Getting started

No build step and no dependencies — it's plain stdlib Python (3.8+) on Linux.

```bash
git clone https://github.com/MrNoname3/pyscoppy.git
cd pyscoppy
python3 run.py        # daemon + GUI -> http://127.0.0.1:8077
```

See [README.md](README.md) for how to run it, and [AGENTS.md](AGENTS.md) for the
full operating guide (what you can set, how to read the data, scope vs logic
analyzer). If you touch the wire protocol, read [PROTOCOL.md](PROTOCOL.md).

## Before you push

Run the same checks CI does:

```bash
./run-checks.sh          # byte-compile + unit tests + pyright (strict)
```

`pyright` is optional locally (the script skips it with a hint if it isn't
installed: `pip install pyright`), but CI enforces it, so keep it green.

## Conventions

- **Stdlib only.** No third-party runtime packages, ever — it has to run anywhere
  without a venv or pip. `pyright` is the one dev-only tool, never imported at
  runtime.
- **Strict typing.** The whole package type-checks clean under Pyright/Pylance
  strict (`pyrightconfig.json` covers `pyscoppy`, `run.py` and `tests`).
- **Tests are hardware-free.** The protocol is pure encode/decode, so it's
  unit-testable without a Pico. When you pin down a new protocol fact, add a
  regression test in [tests/](tests/) — those facts were expensive to find and
  easy to break silently.
- **Match the surrounding style**, and keep code and docs in English.
- Update [PROTOCOL.md](PROTOCOL.md)'s validation status when you confirm or
  refute something on real hardware.

## Submitting changes

Branch off `main`, keep commits focused, and make sure `./run-checks.sh` passes
and CI is green. PRs and issues are welcome.
