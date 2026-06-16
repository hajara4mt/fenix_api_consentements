"""
Suppression d'un PCE du registre droits_acces.parquet (nettoyage local).

Retire la ligne du MÊME parquet silver que la route (grdf/silver/droits_acces.parquet),
via registry_dao.delete → écriture protégée par lease ADLS.

⚠️ À utiliser pour nettoyer les PCE de test créés en local. La suppression est
définitive (en prod, on préférerait marquer etat_droit_acces='Obsolète').

Usage (depuis la racine du projet fenix-api) :
  python scripts/delete_pce.py <id_pce>
"""

import os
import sys

# Rendre `shared` importable quand on lance le script depuis scripts/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import registry_dao          # noqa: E402
from shared.config import Config          # noqa: E402


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage : python scripts/delete_pce.py <id_pce>")
        sys.exit(1)

    id_pce = sys.argv[1]
    target = f"{Config.CONTAINER_NAME}/{Config.droits_acces_blob_path()}"
    print(f"Cible : {target} (compte {Config.STORAGE_ACCOUNT_NAME})")

    record = registry_dao.get(id_pce)
    if not record:
        print(f"PCE {id_pce!r} introuvable — rien à supprimer.")
        return

    print(
        f"PCE trouvé : etat={record.get('etat_droit_acces')!r}, "
        f"raison_sociale={record.get('raison_sociale_du_titulaire')!r}"
    )
    registry_dao.delete(id_pce)
    print(f"✅ PCE {id_pce!r} supprimé du parquet silver.")


if __name__ == "__main__":
    main()
