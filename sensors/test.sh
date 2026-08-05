#!/bin/sh
set -e
pytest --cov=runner --cov=bootstrap --cov=cli --cov-report=xml --cov-report=json --cov-report=term-missing
diff-cover coverage.xml --compare-branch=main --fail-under=100
python3 sensors/_coverage_floor.py
