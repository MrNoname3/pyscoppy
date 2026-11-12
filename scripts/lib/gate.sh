# shellcheck shell=bash
# The release gate's shared steps, sourced by scripts/check.sh, which calls
# them in order and adds the repository's own steps between them.
# Managed by repo-template: changes here are overwritten by its next update.

VENV=.venv

step() { printf '\n=== %s ===\n' "$1"; }

# The repo-local toolchain (scripts/ensure-venv.sh), and the versions it runs.
gate_toolchain() {
  step "toolchain"
  ./scripts/ensure-venv.sh
  # The interpreter .venv is built on: the OS Python or uv's download in .python/.
  "$VENV/bin/python" -c 'import os, sys
base = os.path.realpath(sys._base_executable)
print("Python", sys.version.split()[0], "from",
      os.path.relpath(base) if base.startswith(os.getcwd() + os.sep) else base)'
  "$VENV/bin/shellcheck" --version | sed -n 2p
  if [ -x "$VENV/bin/ruff" ]; then "$VENV/bin/ruff" --version; fi
  if [ -x "$VENV/bin/pyright" ]; then "$VENV/bin/pyright" --version; fi
}

# The Python tools the toolchain has (see pyproject.toml for their settings):
# ruff's lint where ruff is pinned, pyright's type check where pyright is.
gate_python() {
  if [ -x "$VENV/bin/ruff" ]; then
    step "ruff"
    "$VENV/bin/ruff" check .
  fi
  if [ -x "$VENV/bin/pyright" ]; then
    step "pyright"
    "$VENV/bin/pyright"
  fi
}

# Every tracked shell script: *.sh files and files with a shell shebang. Jinja
# templates are not shell until they are rendered.
gate_shellcheck() {
  step "shellcheck"
  local shell_scripts
  mapfile -t shell_scripts < <(git ls-files ':(exclude)*.j2' ':(exclude)*.jinja' | while IFS= read -r f; do
    case "$f" in
      *.sh) echo "$f" ;;
      *) head -n 1 -- "$f" 2>/dev/null | grep -qE '^#!.*\b(ba|da)?sh\b' && echo "$f" ;;
    esac
  done)
  "$VENV/bin/shellcheck" "${shell_scripts[@]}"
  echo "OK (${#shell_scripts[@]} scripts)"
}

# The secret scan over tracked files. gate_secret_scan runs all of it; a
# repository with checks of its own calls secret_scan_begin, scan and
# secret_scan_end around them instead. Extended regexes in .secret-allow are
# cut out of a matching line before its pattern is tried again, so an allowed
# value does not hide another one on the same line; the untracked
# .secret-patterns.local adds patterns that must not be committed themselves.
SECRET_SCAN_FAIL=0

scan() { # scan <label> <pattern-ERE> [allowlist-ERE]
  local label="$1" pattern="$2" allow="${3:-}" hits
  hits="$(git grep -nEI -e "$pattern" -- \
    ':(exclude)scripts/lib/gate.sh' ':(exclude)scripts/check.sh' ':(exclude).secret-allow' || true)"
  if [ -f .secret-allow ]; then
    local extra
    extra="$(grep -vE '^[[:space:]]*(#|$)' .secret-allow | paste -sd '|' || true)"
    allow="${allow:+$allow${extra:+|}}$extra"
  fi
  if [ -n "$allow" ] && [ -n "$hits" ]; then
    hits="$(printf '%s\n' "$hits" | "$VENV/bin/python" -c '
import re, sys
pattern, allow = re.compile(sys.argv[1]), re.compile(sys.argv[2])
for line in sys.stdin:
    if pattern.search(allow.sub("", line.rstrip("\n"))):
        print(line, end="")' "$pattern" "$allow")"
  fi
  if [ -n "$hits" ]; then
    printf '%s\n' "$hits"
    echo "FAIL: possible secret in tracked files ($label)"
    SECRET_SCAN_FAIL=1
  fi
}

secret_scan_begin() {
  step "secret scan (tracked files)"
  SECRET_SCAN_FAIL=0
  scan "private key block" 'BEGIN [A-Z ]*PRIVATE KEY'
  # shellcheck disable=SC2016  # the literal dollar signs of crypt(3) hashes
  scan "password hash" '\$(1|2[aby]|5|6|y)\$[./A-Za-z0-9]{2,}\$'
  scan "Google OAuth token" '\b(1//0[A-Za-z0-9_-]{20,}|ya29\.[A-Za-z0-9_-]{20,})'
  scan "JSON web token" '\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}'
  scan "API token" '\b(ghp_|github_pat_|glpat-|xox[abp]-)[A-Za-z0-9_-]{16,}'
  # Machine identifiers. Allowed: the private and documentation ranges, well-known
  # resolvers, container networks, link-local addresses and the four-part
  # version pins of requirements files anywhere, which say nothing about a LAN.
  scan "IPv4 address" '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b' \
    '0\.0\.0\.0|127\.0\.[01]\.1|127\.0\.0\.0/8|192\.168\.0\.0/16|10\.0\.0\.0/8|100\.64\.0\.0/10|\b(192\.0\.2|198\.51\.100|203\.0\.113)\.[0-9]+\b|\b(1\.1\.1\.1|1\.0\.0\.1|8\.8\.8\.8|8\.8\.4\.4|9\.9\.9\.9)\b|\b172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+|\b169\.254\.[0-9]+\.[0-9]+|^([^:]*/)?requirements[^:/]*:[0-9]+:[A-Za-z0-9_.-]+==[0-9.]+$'
  scan "MAC address" '\b([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b' 'aa:bb:cc:dd:ee:ff'
  scan "UUID" '\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b'
  if [ -f .secret-patterns.local ]; then
    local p
    while IFS= read -r p; do
      case "$p" in ''|'#'*) continue ;; esac
      scan "local pattern" "$p"
    done < .secret-patterns.local
  fi
}

secret_scan_end() {
  if [ "$SECRET_SCAN_FAIL" -ne 0 ]; then
    echo
    echo "Secret scan failed. False positive? Add an extended regex for it to .secret-allow."
    exit 1
  fi
  echo "OK"
}

gate_secret_scan() {
  secret_scan_begin
  secret_scan_end
}

# The last step of every gate: no tracked file may still hold the conflict
# markers a merge or a template update left in it.
gate_done() {
  step "merge conflict markers"
  local markers
  markers="$(git grep -nE -e '^(<<<<<<<|>>>>>>>) ' -- ':(exclude)scripts/lib/gate.sh' || true)"
  if [ -n "$markers" ]; then
    printf '%s\n' "$markers"
    echo "FAIL: unresolved merge conflict in tracked files"
    exit 1
  fi
  echo "OK"
  printf '\nAll checks passed.\n'
}
