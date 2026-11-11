"""Resolve project files independently of the working directory."""
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent
RESOURCE_ROOT = PACKAGE_ROOT / 'resources'
