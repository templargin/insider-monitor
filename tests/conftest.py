import pathlib
import sys

# Tests import `scraper` / `sitegen` from the repo root.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
