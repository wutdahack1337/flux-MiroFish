"""
common — Shared bootstrap for backend scripts.
Path setup, dotenv loading, and common imports.
"""

import os
import sys

scripts_dir = os.path.dirname(os.path.abspath(__file__))
backend_dir = os.path.abspath(os.path.join(scripts_dir, '..'))
project_root = os.path.abspath(os.path.join(backend_dir, '..'))

sys.path.insert(0, scripts_dir)
sys.path.insert(0, backend_dir)

from dotenv import load_dotenv

_env_file = os.path.join(project_root, '.env')
if os.path.exists(_env_file):
    load_dotenv(_env_file)
else:
    _backend_env = os.path.join(backend_dir, '.env')
    if os.path.exists(_backend_env):
        load_dotenv(_backend_env)

from app.utils.llm_client import LLMClient  # noqa: E402, F401
from app.config import Config  # noqa: E402, F401


def resolve_path(path, base=None):
    """Make a path absolute, resolving relative paths against base (default: project_root)."""
    if os.path.isabs(path):
        return path
    return os.path.join(base or project_root, path)
