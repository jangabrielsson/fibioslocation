#!/usr/bin/env bash
# publish.sh - Build and publish fibioslocation to PyPI
# Usage:
#   ./publish.sh           # upload to PyPI
#   ./publish.sh --test    # upload to TestPyPI instead

set -euo pipefail

TESTPYPI=false
if [[ "${1:-}" == "--test" ]]; then
  TESTPYPI=true
fi

# ── 1. Set up a local venv for build tools ────────────────────────────────────
VENV_DIR=".venv-publish"
if [[ ! -d "$VENV_DIR" ]]; then
  echo "Creating build venv at $VENV_DIR..."
  python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"
pip install --quiet --upgrade build twine

# ── 2. Clean previous build artefacts ────────────────────────────────────────
echo "Cleaning old build artefacts..."
rm -rf dist/ build/ *.egg-info

# ── 3. Build source distribution and wheel ───────────────────────────────────
echo "Building package..."
python -m build

echo ""
echo "Built artefacts:"
ls -lh dist/

# ── 4. Upload ─────────────────────────────────────────────────────────────────
if $TESTPYPI; then
  echo ""
  echo "Uploading to TestPyPI..."
  python -m twine upload --repository testpypi dist/*
  echo ""
  echo "Install from TestPyPI with:"
  echo "  pip install --index-url https://test.pypi.org/simple/ fibioslocation"
else
  echo ""
  echo "Uploading to PyPI..."
  python -m twine upload dist/*
  echo ""
  echo "Install with:"
  echo "  pip install fibioslocation"
fi
