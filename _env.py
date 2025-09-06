import os
from pathlib import Path
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

def load_env_from_dotenv():
    dotenv = os.getenv("DOTENV_FILE")
    if dotenv and load_dotenv:
        load_dotenv(dotenv)

def get_path(key, default=None) -> str:
    v = os.getenv(key, default)
    return str(Path(v)) if v else ""
