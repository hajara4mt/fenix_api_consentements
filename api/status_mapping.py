"""
Mapping état interne (droits_acces.parquet) → statut exposé par l'API.

L'API expose 5 statuts (décision projet) :
    nouveau · A valider · Active · Refusée · Obsolète

Le storage utilise un cycle de vie à 8 états (cf. registry_dao.DROIT_STATES).
Les 5 ci-dessus sont des états internes exposés tels quels ; les 3 restants
sont repliés dessus :
    - A revérifier → A valider   (échec déclaration, toujours en cours côté métier)
    - Révoquée     → Obsolète    (consentement retiré → PCE inactif)
    - résilié      → Obsolète    (résilié côté GRDF → PCE inactif)

⚠️ La casse est significative (cf. registry_dao) : on conserve l'orthographe
exacte des états internes.

Utilisé pour la réponse 409 (PCE déjà existant), et plus tard pour le
GET /grdf/droits-acces/{id_pce}.
"""

# Les 5 statuts exposés par l'API GRDF (ordre d'affichage / valeurs acceptées en filtre).
STATUTS_EXPOSES = ("nouveau", "A valider", "Active", "Refusée", "Obsolète")

# état interne → statut exposé (parmi les 5)
ETAT_TO_STATUT = {
    # exposés tels quels
    "nouveau": "nouveau",
    "A valider": "A valider",
    "Active": "Active",
    "Refusée": "Refusée",
    "Obsolète": "Obsolète",
    # repliés sur l'un des 5
    "A revérifier": "A valider",
    "Révoquée": "Obsolète",
    "résilié": "Obsolète",
}


def to_statut_expose(etat) -> str:
    """Traduit un état interne en statut exposé. Défaut prudent : 'nouveau'."""
    if not etat:
        return "nouveau"
    return ETAT_TO_STATUT.get(str(etat), "nouveau")
