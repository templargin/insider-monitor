"""Render docs/ from data/ files. Idempotent."""
from sitegen import generate

if __name__ == "__main__":
    s = generate.generate()
    print(f"Built {s['pages_built']} daily pages and {s['companies_built']} company pages.")
