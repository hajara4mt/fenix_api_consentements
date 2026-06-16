"""
Traduction des codes d'erreur ADICT (GRDF) en messages métier (cf. MD, GET).

Les codes ADICT sont internes — le métier ne voit que le message traduit.

⚠️ LIMITATION (à connaître) : le pipeline ne stocke PAS aujourd'hui de champ
« code ADICT » propre. `declare_pce` enregistre `code_retour_grdf_declare`
(code HTTP) et `message_erreur_declare` (extrait brut du body GRDF). La
résolution se fait donc en best-effort : on cherche un code connu dans le
message stocké. Pour une traduction fiable, le pipeline devrait stocker un champ
dédié (ex: `code_adict_declare`) — voir resolve_message_erreur().
"""

from typing import Optional

# code ADICT interne → message métier (table du MD)
ADICT_CODE_TO_MESSAGE = {
    "1000003": "Compteur non accrédité pour la collecte de données",
    "1000007": "Compteur plus accrédité, autorisation expirée",
    "1000008": "Aucune donnée disponible pour ce compteur",
    "2000100": "Traitement en cours, retry automatique prévu",
    "Operation Denied": "Déclaration rejetée par le distributeur",
    "ERRI101637": "Timeout ADICT, retry automatique prévu",
}

MESSAGE_ECHEC_RESILIATION = (
    "La résiliation auprès du distributeur a échoué. Contacter le support"
)
MESSAGE_INCONNU = "Erreur inattendue, contacter le support"


def translate_adict(code) -> Optional[str]:
    """Traduit un code ADICT en message métier. None si code vide."""
    if code is None:
        return None
    code = str(code).strip()
    if not code:
        return None
    return ADICT_CODE_TO_MESSAGE.get(code, MESSAGE_INCONNU)


def resolve_message_erreur(record: dict) -> Optional[str]:
    """
    Construit le message_erreur exposé pour un PCE, à partir de ce que le
    pipeline a stocké.

    Ordre de résolution :
      1. champ code ADICT propre s'il existe un jour (`code_adict_declare`)
      2. best-effort : repérer un code ADICT connu dans `message_erreur_declare`
      3. message brut présent mais aucun code connu → message générique inconnu
      4. aucun indice d'erreur → None
    """
    # 1) champ propre (futur-proof, pas encore alimenté par le pipeline)
    code = record.get("code_adict_declare") or record.get("code_adict")
    if code:
        return translate_adict(code)

    # 2) best-effort sur le message brut stocké par declare_pce
    raw = record.get("message_erreur_declare")
    if raw:
        raw_str = str(raw)
        for known_code, message in ADICT_CODE_TO_MESSAGE.items():
            if known_code in raw_str:
                return message
        # 3) erreur présente mais code non reconnu
        return MESSAGE_INCONNU

    # 4) pas d'erreur
    return None
