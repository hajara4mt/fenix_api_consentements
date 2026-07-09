"""
Exposition de l'erreur GRDF BRUTE stockée par le pipeline (aucun mapping).

`declare_pce` enregistre dans `message_erreur_declare` le body renvoyé par GRDF,
sérialisé en JSON (ex: {"code_statut_traitement": "...",
"message_retour_traitement": "..."}). On le renvoie tel quel au métier — pas de
traduction ADICT, exactement comme Enedis expose son `message_retour_traitement`.
"""

import json


def resolve_message_erreur(record: dict):
    """
    Construit le `message_erreur` exposé pour un PCE, à partir de ce que le
    pipeline a stocké dans `message_erreur_declare`, SANS aucun mapping.

    Renvoie :
      - l'objet GRDF brut (dict) quand le message stocké est du JSON, ex :
        {"code_statut_traitement": "2000000010",
         "message_retour_traitement": "Une erreur technique est survenue."}
      - une chaîne brute pour un message texte simple (ex: "Champ manquant : ...")
      - None si aucune erreur n'est stockée
    """
    raw = record.get("message_erreur_declare")
    # None ou NaN (float non-égal à lui-même)
    if raw is None or (isinstance(raw, float) and raw != raw):
        return None
    if isinstance(raw, (dict, list)):
        return raw
    s = str(raw).strip()
    if not s or s.lower() == "null":
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return s
