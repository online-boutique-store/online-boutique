#!/usr/bin/env bash
# Isolated tooling env for the gate's helper scripts (PyYAML is REQUIRED, never skipped).
set -euo pipefail
VENV="${GATE_TOOLS_VENV:-/tmp/ob-gate-tools}"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet "PyYAML==6.0.2"
"$VENV/bin/python" -c "import yaml; print('PyYAML', yaml.__version__)"
