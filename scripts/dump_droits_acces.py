"""
Relecture du registre droits_acces.parquet (debug local).

À lancer APRÈS un POST pour vérifier l'écriture réelle dans le parquet.
Utilise EXACTEMENT la même config + credential que la route :
  - DefaultAzureCredential (ton `az login`)
  - le chemin pointé par GRDF_ROOT_FOLDER (donc grdf-test/ en sandbox)

Usage (depuis la racine du projet fenix-api) :
  python scripts/dump_droits_acces.py             # liste tous les PCE
  python scripts/dump_droits_acces.py <id_pce>    # détail d'un PCE
"""

import json
import os
import sys

# Rendre `shared` importable quand on lance le script depuis scripts/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import registry_dao          # noqa: E402
from shared.config import Config          # noqa: E402


def main() -> None:
    print(
        f"Lecture de {Config.CONTAINER_NAME}/{Config.droits_acces_blob_path()} "
        f"(compte {Config.STORAGE_ACCOUNT_NAME})\n"
    )

    # Détail d'un PCE précis
    if len(sys.argv) > 1:
        id_pce = sys.argv[1]
        record = registry_dao.get(id_pce)
        if record:
            print(json.dumps(record, indent=2, ensure_ascii=False, default=str))
        else:
            print(f"PCE {id_pce!r} introuvable")
        return

    # Liste complète
    rows = registry_dao.list_all()
    print(f"{len(rows)} ligne(s) dans le registre :\n")
    for r in rows:
        print(
            f"  - {r.get('id_pce')} | etat={r.get('etat_droit_acces')!r} "
            f"| role_tiers={r.get('role_tiers')!r} "
            f"| raison_sociale={r.get('raison_sociale_du_titulaire')!r} "
            f"| maj={r.get('derniere_maj')}"
        )


if __name__ == "__main__":
    main()
