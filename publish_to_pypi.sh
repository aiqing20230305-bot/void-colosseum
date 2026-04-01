#!/usr/bin/env bash
# publish_to_pypi.sh — Build and publish void-colosseum to PyPI
# Usage: bash publish_to_pypi.sh
# Requires: pip install build twine

set -e

echo "=== Void Colosseum PyPI Publisher ==="
echo ""

# Check prerequisites
if ! python3 -m build --version &>/dev/null; then
    echo "Installing build..."
    pip install --upgrade build
fi

if ! twine --version &>/dev/null; then
    echo "Installing twine..."
    pip install --upgrade twine
fi

# Clean previous builds
echo "Cleaning dist/..."
rm -rf dist/ build/ *.egg-info

# Build
echo "Building source distribution and wheel..."
python3 -m build

echo ""
echo "Build complete. Contents of dist/:"
ls -lh dist/

echo ""
echo "Checking distribution with twine..."
twine check dist/*

echo ""
echo "Ready to upload to PyPI."
echo ""
echo "To publish to TEST PyPI first (recommended):"
echo "  twine upload --repository testpypi dist/*"
echo "  pip install --index-url https://test.pypi.org/simple/ void-colosseum"
echo ""
read -p "Upload to PRODUCTION PyPI? (y/N) " confirm
if [[ "$confirm" == "y" || "$confirm" == "Y" ]]; then
    twine upload dist/*
    echo ""
    echo "Published! View at: https://pypi.org/project/void-colosseum/"
    echo "Install with: pip install void-colosseum"
else
    echo "Skipped production upload. Run manually: twine upload dist/*"
fi
