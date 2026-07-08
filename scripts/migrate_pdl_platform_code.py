"""
Migration one-shot : ajoute la colonne `platform_code` à la table Delta `pdl`.

Contexte
--------
La table `pdl` existait avec 21 colonnes. On ajoute `platform_code`
(varchar(10), NOT NULL). Comme la colonne est NOT NULL, les lignes PDL
existantes (qui ne l'ont pas) doivent être **backfillées** avec une valeur :
c'est BACKFILL_VALUE ci-dessous (à régler AVANT de lancer).

Ce que fait le script
---------------------
1. Lit toute la table `pdl`.
2. Si `platform_code` est déjà là → ne fait rien (idempotent).
3. Sinon : ajoute la colonne = BACKFILL_VALUE pour toutes les lignes existantes,
   réordonne selon SCHEMA_PDL, valide, et réécrit la table en `overwrite`
   (schéma + données), ce qui fait évoluer le schéma Delta.

Usage
-----
    # 1) Aperçu (ne modifie RIEN) :
    python scripts/migrate_pdl_platform_code.py

    # 2) Exécution réelle :
    python scripts/migrate_pdl_platform_code.py --apply

Pré-requis : connexion ADLS configurée (AZURE_STORAGE_CONNECTION_STRING via
local.settings.json / env), comme pour les autres scripts.
"""

import sys
from pathlib import Path

# Permettre l'import de shared/ depuis la racine du projet
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pyarrow as pa
from deltalake import write_deltalake

from shared import adls_client
from shared.schemas import SCHEMA_PDL, list_columns, validate_dataframe
from shared.settings import get_silver_table_uri, get_storage_options


# ⚠️ À RÉGLER AVANT DE LANCER : valeur donnée aux PDL existants (≤ 10 caractères).
# C'est la valeur "plateforme d'origine" attribuée aux consentements créés AVANT
# l'ajout du champ. Mets le vrai code si tu le connais, sinon un repère explicite.
BACKFILL_VALUE = "LEGACY"

COLUMN = "platform_code"


def main(apply: bool) -> int:
    if len(BACKFILL_VALUE) > 10:
        print(f"❌ BACKFILL_VALUE='{BACKFILL_VALUE}' dépasse 10 caractères. Corrige-le.")
        return 1

    df = adls_client.read_table("pdl")
    n = len(df)
    print(f"Table pdl : {n} ligne(s), {len(df.columns)} colonne(s).")

    if COLUMN in df.columns:
        print(f"✅ La colonne '{COLUMN}' existe déjà — rien à faire (migration déjà appliquée).")
        return 0

    # Backfill des lignes existantes
    df[COLUMN] = BACKFILL_VALUE
    # Réordonner exactement comme le schéma cible
    df = df[list_columns("pdl")]

    print(f"→ Ajout de '{COLUMN}' = '{BACKFILL_VALUE}' sur {n} ligne(s) existante(s).")
    validate_dataframe(df, "pdl", strict=True)
    print("→ Validation du schéma : OK.")

    if not apply:
        print("\n(DRY-RUN) Aucune écriture. Relance avec --apply pour migrer réellement.")
        return 0

    table = pa.Table.from_pandas(df, schema=SCHEMA_PDL, preserve_index=False)
    write_deltalake(
        get_silver_table_uri("pdl"),
        table,
        mode="overwrite",
        schema_mode="overwrite",
        storage_options=get_storage_options(),
    )
    print(f"✅ Migration appliquée : table pdl réécrite avec la colonne '{COLUMN}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv))
