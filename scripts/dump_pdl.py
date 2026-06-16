"""
Lecture de la table Delta `pdl` Enedis (debug local) — équivalent enedis de
scripts/dump_droits_acces.py.

Vérifie que fenix-api peut lire le stockage Delta Enedis (vendoré).

Usage (depuis la racine du projet fenix-api) :
  python scripts/dump_pdl.py
"""

import collections
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# Charger local.settings.json dans l'env + alias connection string Enedis
_settings = os.path.join(_ROOT, "local.settings.json")
if os.path.exists(_settings):
    for _k, _v in (json.load(open(_settings, encoding="utf-8")).get("Values") or {}).items():
        if not _k.startswith("_"):
            os.environ.setdefault(_k, str(_v))
if not os.environ.get("AZURE_STORAGE_CONNECTION_STRING") and os.environ.get("STORAGE_CONNECTION_STRING"):
    os.environ["AZURE_STORAGE_CONNECTION_STRING"] = os.environ["STORAGE_CONNECTION_STRING"]

from shared import adls_client  # noqa: E402


def main() -> None:
    df = adls_client.read_table("pdl")
    print(f"Table Delta pdl : {len(df)} ligne(s), {len(df.columns)} colonnes")
    if "statut" in df.columns:
        print("statuts :", dict(collections.Counter(df["statut"].astype(str))))
    print("colonnes :", list(df.columns))


if __name__ == "__main__":
    main()
