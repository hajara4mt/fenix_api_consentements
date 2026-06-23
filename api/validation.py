"""
Validation et normalisation du payload POST /grdf/droits-acces.

Principe de nommage (décision projet) :
  Les noms de champs sont IDENTIQUES entre la route, le pipeline et la table de
  stockage (droits_acces.parquet). On n'utilise donc PAS les alias "consentement"
  du brouillon de doc API, mais les noms canoniques du storage :
    - date_debut_droit_acces / date_fin_droit_acces  (et non *_consentement)
    - raison_sociale_du_titulaire                     (et non raison_sociale)

Règles métier (alignées sur declare_pce / NiFi) :
  - id_pce            : obligatoire, max 20 caractères (colonne varchar(20))
  - platform_code     : obligatoire, max 10 caractères (varchar(10)) ; non
    modifiable ensuite (absent de la whitelist PATCH)
  - raison_sociale_du_titulaire OU nom_titulaire : au moins l'un des deux
  - date_fin_droit_acces : > date_debut_droit_acces ET ≤ date_debut + 3 ans
  - au moins un des 4 périmètres à true
  - role_tiers N'EST PAS demandé ici : la route l'assigne automatiquement
    (cf. handler) à AUTORISE_CONTRAT_FOURNITURE.

validate_create() lève ValidationError au premier problème (fail-fast), avec le
nom du champ fautif pour construire la réponse 400 documentée.
"""

import math
import re
from datetime import datetime

from dateutil.relativedelta import relativedelta

# ----------------------------------------------------------------------
# Constantes de validation
# ----------------------------------------------------------------------

ID_PCE_MAX_LEN = 20          # colonne id_pce varchar(20)
PLATFORM_CODE_MAX_LEN = 10   # colonne platform_code varchar(10)
PARTNER_MAX_LEN = 50
# Le partenaire est restreint à une liste fermée (un seul aujourd'hui : ifpeb).
PARTENAIRES_AUTORISES = {"ifpeb"}
COURRIEL_MAX_LEN = 100
RAISON_SOCIALE_MAX_LEN = 200
NOM_TITULAIRE_MAX_LEN = 40
MAX_DROIT_ACCES_YEARS = 3    # date_fin ≤ date_debut + 3 ans (limite GRDF)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CODE_POSTAL_RE = re.compile(r"^\d{5}$")

# Périmètres : optionnels, avec valeurs par défaut (cf. doc API GRDF)
PERIMETER_FIELDS = (
    "perim_donnees_contractuelles",
    "perim_donnees_techniques",
    "perim_donnees_informatives",
    "perim_donnees_publiees",
)
PERIMETER_DEFAULTS = {
    "perim_donnees_contractuelles": True,
    "perim_donnees_techniques": True,
    "perim_donnees_informatives": False,
    "perim_donnees_publiees": True,
}


# ----------------------------------------------------------------------
# Exception de validation
# ----------------------------------------------------------------------

class ValidationError(Exception):
    """Erreur de validation d'un champ du payload. `champ` peut être None
    (erreur transverse, ex: 'au moins un périmètre')."""

    def __init__(self, champ, message: str):
        self.champ = champ
        self.message = message
        super().__init__(message)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _require(body: dict, champ: str):
    """Retourne la valeur du champ obligatoire, ou lève si absent/vide."""
    value = body.get(champ)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValidationError(champ, f"Le champ {champ} est obligatoire.")
    return value


def _parse_date(champ: str, value) -> datetime:
    """Parse une date YYYY-MM-DD, lève ValidationError si invalide."""
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d")
    except (ValueError, TypeError):
        raise ValidationError(
            champ, f"Le champ {champ} doit être une date au format YYYY-MM-DD."
        )


def _coerce_bool(champ: str, value) -> bool:
    """Convertit une valeur en booléen, lève si non interprétable."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "vrai", "1", "yes"):
            return True
        if v in ("false", "faux", "0", "no"):
            return False
    raise ValidationError(champ, f"Le champ {champ} doit être un booléen (true/false).")


def _non_empty_str(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


# ----------------------------------------------------------------------
# Validation principale
# ----------------------------------------------------------------------

def validate_create(body) -> tuple[str, dict]:
    """
    Valide le body du POST et le normalise en (id_pce, fields).

    `fields` utilise les NOMS CANONIQUES du storage (prêts pour
    registry_dao.insert). role_tiers, etat_droit_acces et date_creation sont
    ajoutés par le handler, PAS ici.

    Raises:
        ValidationError : premier champ invalide rencontré.

    Returns:
        (id_pce, fields) prêts à insérer.
    """
    if not isinstance(body, dict):
        raise ValidationError(None, "Le corps de la requête doit être un objet JSON.")

    # --- id_pce (varchar(20)) ---
    id_pce = str(_require(body, "id_pce")).strip()
    if len(id_pce) > ID_PCE_MAX_LEN:
        raise ValidationError(
            "id_pce", f"Le champ id_pce ne doit pas dépasser {ID_PCE_MAX_LEN} caractères."
        )

    # --- partner : restreint à PARTENAIRES_AUTORISES (normalisé en minuscules) ---
    partner = str(_require(body, "partner")).strip().lower()
    if partner not in PARTENAIRES_AUTORISES:
        raise ValidationError(
            "partner",
            "Le champ partner doit être l'un de : " + ", ".join(sorted(PARTENAIRES_AUTORISES)) + ".",
        )

    # --- platform_code (varchar(10), obligatoire, non modifiable ensuite) ---
    platform_code = str(_require(body, "platform_code")).strip()
    if len(platform_code) > PLATFORM_CODE_MAX_LEN:
        raise ValidationError(
            "platform_code",
            f"Le champ platform_code ne doit pas dépasser {PLATFORM_CODE_MAX_LEN} caractères.",
        )

    # --- courriel_titulaire ---
    courriel = str(_require(body, "courriel_titulaire")).strip()
    if len(courriel) > COURRIEL_MAX_LEN or not _EMAIL_RE.match(courriel):
        raise ValidationError(
            "courriel_titulaire",
            f"Le champ courriel_titulaire doit être un email valide (max {COURRIEL_MAX_LEN} caractères).",
        )

    # --- code_postal ---
    code_postal = str(_require(body, "code_postal")).strip()
    if not _CODE_POSTAL_RE.match(code_postal):
        raise ValidationError(
            "code_postal", "Le code postal doit contenir exactement 5 chiffres."
        )

    # --- dates du droit d'accès ---
    d_debut = _parse_date("date_debut_droit_acces", _require(body, "date_debut_droit_acces"))
    d_fin = _parse_date("date_fin_droit_acces", _require(body, "date_fin_droit_acces"))
    if d_fin <= d_debut:
        raise ValidationError(
            "date_fin_droit_acces",
            "La date_fin_droit_acces doit être strictement postérieure à date_debut_droit_acces.",
        )
    if d_fin > d_debut + relativedelta(years=MAX_DROIT_ACCES_YEARS):
        raise ValidationError(
            "date_fin_droit_acces",
            f"La date_fin_droit_acces ne peut pas dépasser date_debut_droit_acces + {MAX_DROIT_ACCES_YEARS} ans.",
        )

    # --- périmètre des données de conso ---
    perim_conso_debut = _parse_date(
        "perim_donnees_conso_debut", _require(body, "perim_donnees_conso_debut")
    )
    perim_conso_fin = _parse_date(
        "perim_donnees_conso_fin", _require(body, "perim_donnees_conso_fin")
    )

    # --- titulaire : au moins un des deux (raison sociale OU nom) ---
    raison_sociale = body.get("raison_sociale_du_titulaire")
    nom_titulaire = body.get("nom_titulaire")
    has_rs = _non_empty_str(raison_sociale)
    has_nom = _non_empty_str(nom_titulaire)
    if not has_rs and not has_nom:
        raise ValidationError(
            "raison_sociale_du_titulaire",
            "Au moins un des champs raison_sociale_du_titulaire ou nom_titulaire doit être renseigné.",
        )
    if has_rs and len(raison_sociale.strip()) > RAISON_SOCIALE_MAX_LEN:
        raise ValidationError(
            "raison_sociale_du_titulaire",
            f"Le champ raison_sociale_du_titulaire ne doit pas dépasser {RAISON_SOCIALE_MAX_LEN} caractères.",
        )
    if has_nom and len(nom_titulaire.strip()) > NOM_TITULAIRE_MAX_LEN:
        raise ValidationError(
            "nom_titulaire",
            f"Le champ nom_titulaire ne doit pas dépasser {NOM_TITULAIRE_MAX_LEN} caractères.",
        )

    # --- périmètres booléens (défauts + au moins un true) ---
    perimetres = {}
    for champ in PERIMETER_FIELDS:
        if champ in body and body[champ] is not None:
            perimetres[champ] = _coerce_bool(champ, body[champ])
        else:
            perimetres[champ] = PERIMETER_DEFAULTS[champ]
    if not any(perimetres.values()):
        raise ValidationError(
            None,
            "Au moins un périmètre (contractuelles, techniques, informatives, publiees) "
            "doit être à true.",
        )

    # --- record canonique (noms storage) ---
    fields = {
        "partner": partner,
        "platform_code": platform_code,
        "courriel_titulaire": courriel,
        "code_postal": code_postal,
        "date_debut_droit_acces": d_debut.strftime("%Y-%m-%d"),
        "date_fin_droit_acces": d_fin.strftime("%Y-%m-%d"),
        "perim_donnees_conso_debut": perim_conso_debut.strftime("%Y-%m-%d"),
        "perim_donnees_conso_fin": perim_conso_fin.strftime("%Y-%m-%d"),
        "raison_sociale_du_titulaire": raison_sociale.strip() if has_rs else None,
        "nom_titulaire": nom_titulaire.strip() if has_nom else None,
        **perimetres,
    }
    return id_pce, fields


# ----------------------------------------------------------------------
# Validation PATCH (partielle + règles croisées sur le merge)
# ----------------------------------------------------------------------

def _existing_str(existing: dict, key: str):
    """Valeur string non vide d'un champ existant, sinon None (gère NaN)."""
    v = existing.get(key)
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def _existing_bool(value) -> bool:
    """Lecture tolérante d'un booléen existant (bool / 'Vrai' / numpy / NaN)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "vrai", "1")
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, float):
        return False if math.isnan(value) else bool(value)
    if hasattr(value, "item"):
        return _existing_bool(value.item())
    return False


def validate_patch(provided: dict, existing: dict) -> dict:
    """
    Valide les champs FOURNIS (format) et les règles croisées sur le MERGE
    (champs fournis + enregistrement existant), puis renvoie le dict normalisé
    des champs à upserter.

    Le caller a déjà filtré : `provided` ne contient QUE des champs modifiables.

    Raises:
        ValidationError au premier problème.
    """
    out: dict = {}

    # --- courriel ---
    if "courriel_titulaire" in provided:
        courriel = str(provided["courriel_titulaire"] or "").strip()
        if not courriel or len(courriel) > COURRIEL_MAX_LEN or not _EMAIL_RE.match(courriel):
            raise ValidationError(
                "courriel_titulaire",
                f"Le champ courriel_titulaire doit être un email valide (max {COURRIEL_MAX_LEN} caractères).",
            )
        out["courriel_titulaire"] = courriel

    # --- code postal ---
    if "code_postal" in provided:
        cp = str(provided["code_postal"] or "").strip()
        if not _CODE_POSTAL_RE.match(cp):
            raise ValidationError("code_postal", "Le code postal doit contenir exactement 5 chiffres.")
        out["code_postal"] = cp

    # --- périmètre conso (dates) ---
    for champ in ("perim_donnees_conso_debut", "perim_donnees_conso_fin"):
        if champ in provided:
            out[champ] = _parse_date(champ, provided[champ]).strftime("%Y-%m-%d")

    # --- dates du droit d'accès (+ contrôle croisé sur le merge) ---
    if "date_debut_droit_acces" in provided:
        out["date_debut_droit_acces"] = _parse_date(
            "date_debut_droit_acces", provided["date_debut_droit_acces"]
        ).strftime("%Y-%m-%d")
    if "date_fin_droit_acces" in provided:
        out["date_fin_droit_acces"] = _parse_date(
            "date_fin_droit_acces", provided["date_fin_droit_acces"]
        ).strftime("%Y-%m-%d")

    if "date_debut_droit_acces" in provided or "date_fin_droit_acces" in provided:
        merged_debut = out.get("date_debut_droit_acces") or _existing_str(existing, "date_debut_droit_acces")
        merged_fin = out.get("date_fin_droit_acces") or _existing_str(existing, "date_fin_droit_acces")
        if merged_debut and merged_fin:
            dd = datetime.strptime(merged_debut, "%Y-%m-%d")
            df = datetime.strptime(merged_fin, "%Y-%m-%d")
            if df <= dd:
                raise ValidationError(
                    "date_fin_droit_acces",
                    "La date_fin_droit_acces doit être strictement postérieure à date_debut_droit_acces.",
                )
            if df > dd + relativedelta(years=MAX_DROIT_ACCES_YEARS):
                raise ValidationError(
                    "date_fin_droit_acces",
                    f"La date_fin_droit_acces ne peut pas dépasser date_debut_droit_acces + {MAX_DROIT_ACCES_YEARS} ans.",
                )

    # --- titulaire : au moins un sur le merge ---
    if "raison_sociale_du_titulaire" in provided or "nom_titulaire" in provided:
        rs = provided["raison_sociale_du_titulaire"] if "raison_sociale_du_titulaire" in provided \
            else _existing_str(existing, "raison_sociale_du_titulaire")
        nom = provided["nom_titulaire"] if "nom_titulaire" in provided \
            else _existing_str(existing, "nom_titulaire")
        rs_ok = _non_empty_str(rs)
        nom_ok = _non_empty_str(nom)
        if not rs_ok and not nom_ok:
            raise ValidationError(
                "raison_sociale_du_titulaire",
                "Au moins un des champs raison_sociale_du_titulaire ou nom_titulaire doit être renseigné.",
            )
        if "raison_sociale_du_titulaire" in provided:
            if rs_ok and len(rs.strip()) > RAISON_SOCIALE_MAX_LEN:
                raise ValidationError(
                    "raison_sociale_du_titulaire",
                    f"Le champ raison_sociale_du_titulaire ne doit pas dépasser {RAISON_SOCIALE_MAX_LEN} caractères.",
                )
            out["raison_sociale_du_titulaire"] = rs.strip() if rs_ok else None
        if "nom_titulaire" in provided:
            if nom_ok and len(nom.strip()) > NOM_TITULAIRE_MAX_LEN:
                raise ValidationError(
                    "nom_titulaire",
                    f"Le champ nom_titulaire ne doit pas dépasser {NOM_TITULAIRE_MAX_LEN} caractères.",
                )
            out["nom_titulaire"] = nom.strip() if nom_ok else None

    # --- périmètres bool : au moins un true sur le merge ---
    perim_provided = [c for c in PERIMETER_FIELDS if c in provided]
    if perim_provided:
        for champ in perim_provided:
            out[champ] = _coerce_bool(champ, provided[champ])
        merged_true = any(
            (out[champ] if champ in out else _existing_bool(existing.get(champ)))
            for champ in PERIMETER_FIELDS
        )
        if not merged_true:
            raise ValidationError(
                None,
                "Au moins un périmètre (contractuelles, techniques, informatives, publiees) "
                "doit être à true.",
            )

    return out
