#!/bin/sh
# Fetch cdd_utils at a pinned commit, apply our fixes, and install Python deps.
set -eu
cd "$(dirname "$0")"

CDD_UTILS_COMMIT=da10c76ff3d10ee5aa0208e7cf6245f3f447ab71

if [ ! -d third_party/cdd_utils ]; then
    git clone -q https://github.com/fshoocn/cdd_utils third_party/cdd_utils
    git -C third_party/cdd_utils checkout -q "$CDD_UTILS_COMMIT"
    git -C third_party/cdd_utils apply ../../patches/cdd_utils.patch
fi

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
echo "ready: .venv/bin/python cdd2odx.py <input.cdd> <output.pdx>"
