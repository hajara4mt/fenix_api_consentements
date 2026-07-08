"""
Validation du body POST /enedis/consent.

Aligné sur le schéma réel de la table Delta `pdl` (cf. shared/schemas.SCHEMA_PDL) :
  - noms canoniques du storage : date_debut_autorisation / date_fin_autorisation
  - booléens stockés en strings "true"/"false" (enum MySQL) → on accepte le bool
    JSON et on normalise en "true"/"false"
  - id_pdl : exactement 14 chiffres (varchar(14))
  - longueurs varchar, civilite M/MME

validate_consent() lève ValidationError au premier problème et renvoie un dict
de champs normalisés (noms + types prêts pour la ligne pdl).
"""

from datetime import date, datetime

import pandas as pd

# ----------------------------------------------------------------------
# Constantes (alignées schéma pdl)
# ----------------------------------------------------------------------

ID_PDL_MAX_LEN = 14          # colonne id_pdl varchar(14)
PLATFORM_CODE_MAX_LEN = 10   # colonne platform_code varchar(10)
CIVILITES = {"M", "MME"}

# Le partenaire est restreint à une liste fermée (un seul aujourd'hui : ifpeb).
PARTENAIRES_AUTORISES = {"ifpeb"}
PARTNER_MAX_LEN = 100
RAISON_SOCIALE_MAX_LEN = 100
CIVILITE_MAX_LEN = 20
NOM_MAX_LEN = 50
PRENOM_MAX_LEN = 50

# Schéma STRICT du body (champ hors liste → CHAMP_INCONNU)
ALLOWED_CONSENT_FIELDS = {
    "id_pdl",
    "partner",
    "platform_code",
    "date_signature_mandat",
    "date_debut_autorisation",
    "date_fin_autorisation",
    "raison_sociale",
    "civilite",
    "nom",
    "prenom",
    "injection",
    "soutirage",
    "get_cdc",
    "get_dm",
}


class ValidationError(Exception):
    def __init__(self, champ, message: str):
        self.champ = champ
        self.message = message
        super().__init__(message)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _require(body: dict, champ: str):
    value = body.get(champ)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValidationError(champ, f"Le champ {champ} est obligatoire.")
    return value


def _parse_date(champ: str, value) -> date:
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        raise ValidationError(champ, f"Le champ {champ} doit être une date au format YYYY-MM-DD.")


def _to_bool_str(champ: str, value) -> str:
    """JSON bool / string → "true" ou "false" (enum de stockage)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "vrai"):
            return "true"
        if v in ("false", "0", "no", "faux"):
            return "false"
    raise ValidationError(champ, f"Le champ {champ} doit être un booléen (true/false).")


def _opt_bool_str(body: dict, champ: str, default: str) -> str:
    if champ in body and body[champ] is not None:
        return _to_bool_str(champ, body[champ])
    return default


# ----------------------------------------------------------------------
# Validation principale
# ----------------------------------------------------------------------

def validate_consent(body) -> tuple[str, dict]:
    """Valide le body et renvoie (id_pdl, fields) prêts pour la ligne pdl."""
    if not isinstance(body, dict):
        raise ValidationError(None, "Le corps de la requête doit être un objet JSON.")

    # --- id_pdl : varchar(14) → non vide, max 14 caractères (pas un format numérique) ---
    id_pdl = str(_require(body, "id_pdl")).strip()
    if len(id_pdl) > ID_PDL_MAX_LEN:
        raise ValidationError("id_pdl", f"Le champ id_pdl ne doit pas dépasser {ID_PDL_MAX_LEN} caractères.")

    # --- partner : restreint à PARTENAIRES_AUTORISES (minuscules), comme GRDF ---
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

    # --- dates ---
    d_signature = _parse_date("date_signature_mandat", _require(body, "date_signature_mandat"))
    d_debut = _parse_date("date_debut_autorisation", _require(body, "date_debut_autorisation"))
    d_fin = _parse_date("date_fin_autorisation", _require(body, "date_fin_autorisation"))
    if d_fin <= d_debut:
        raise ValidationError(
            "date_fin_autorisation",
            "La date_fin_autorisation doit être strictement postérieure à date_debut_autorisation.",
        )

    # --- titulaire : au moins un de raison_sociale / nom (PM → raison sociale,
    #     PP → nom). prenom est OPTIONNEL. ---
    rs_raw = body.get("raison_sociale")
    nom_raw = body.get("nom")
    prenom_raw = body.get("prenom")
    has_rs = isinstance(rs_raw, str) and rs_raw.strip()
    has_nom = isinstance(nom_raw, str) and nom_raw.strip()
    if not has_rs and not has_nom:
        raise ValidationError(
            "raison_sociale",
            "Au moins un des champs raison_sociale ou nom doit être renseigné.",
        )
    raison_sociale = rs_raw.strip() if has_rs else None
    if raison_sociale is not None and len(raison_sociale) > RAISON_SOCIALE_MAX_LEN:
        raise ValidationError("raison_sociale", f"Le champ raison_sociale ne doit pas dépasser {RAISON_SOCIALE_MAX_LEN} caractères.")
    nom = nom_raw.strip() if has_nom else None
    if nom is not None and len(nom) > NOM_MAX_LEN:
        raise ValidationError("nom", f"Le champ nom ne doit pas dépasser {NOM_MAX_LEN} caractères.")
    prenom = prenom_raw.strip() if (isinstance(prenom_raw, str) and prenom_raw.strip()) else None
    if prenom is not None and len(prenom) > PRENOM_MAX_LEN:
        raise ValidationError("prenom", f"Le champ prenom ne doit pas dépasser {PRENOM_MAX_LEN} caractères.")

    # --- civilite (optionnelle, M/MME) ---
    civilite_raw = body.get("civilite")
    if civilite_raw is not None and str(civilite_raw).strip():
        civilite = str(civilite_raw).strip().upper()
        if civilite not in CIVILITES:
            raise ValidationError("civilite", "Le champ civilite doit être 'M' ou 'MME'.")
    else:
        civilite = None

    # --- booléens (string "true"/"false") ---
    injection = _to_bool_str("injection", _require(body, "injection"))
    soutirage = _to_bool_str("soutirage", _require(body, "soutirage"))
    get_cdc = _opt_bool_str(body, "get_cdc", "true")
    get_dm = _opt_bool_str(body, "get_dm", "true")

    # --- règles métier ---
    if injection == "false" and soutirage == "false":
        raise ValidationError(None, "Au moins un des champs soutirage / injection doit être à true.")
    if get_cdc == "false" and get_dm == "false":
        raise ValidationError(None, "Au moins un des champs get_cdc / get_dm doit être à true.")

    fields = {
        "id_pdl": id_pdl,
        "partner": partner,
        "platform_code": platform_code,
        "date_signature_mandat": d_signature,
        "date_debut_autorisation": d_debut,
        "date_fin_autorisation": d_fin,
        "raison_sociale": raison_sociale,
        "civilite": civilite,
        "nom": nom,
        "prenom": prenom,
        "injection": injection,
        "soutirage": soutirage,
        "get_cdc": get_cdc,
        "get_dm": get_dm,
    }
    return id_pdl, fields


# ----------------------------------------------------------------------
# Validation PATCH (partielle + règles croisées sur le merge)
# ----------------------------------------------------------------------

# Champs modifiables par PATCH (le reste = id_pdl, partner, système → non modifiable).
MODIFIABLE_CONSENT_FIELDS = {
    "date_signature_mandat",
    "date_debut_autorisation",
    "date_fin_autorisation",
    "raison_sociale",
    "civilite",
    "nom",
    "prenom",
    "injection",
    "soutirage",
    "get_cdc",
    "get_dm",
}
DATE_FIELDS = {"date_signature_mandat", "date_debut_autorisation", "date_fin_autorisation"}
BOOL_FIELDS = {"injection", "soutirage", "get_cdc", "get_dm"}
STRING_FIELDS = {"raison_sociale", "civilite", "nom", "prenom"}


def _existing_str(existing: dict, key: str):
    v = existing.get(key)
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def _existing_date(existing: dict, key: str):
    v = existing.get(key)
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if hasattr(v, "date"):  # datetime / Timestamp
        try:
            return v.date()
        except (ValueError, TypeError):
            pass
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _existing_bool_str(existing: dict, key: str):
    v = existing.get(key)
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower()
    return None


def validate_patch_consent(provided: dict, existing: dict) -> dict:
    """Valide les champs fournis + règles croisées sur le merge. Retourne le dict
    normalisé {col: valeur} (dates→date, booléens→"true"/"false", strings→str/None)."""
    out: dict = {}

    # --- dates ---
    for champ in ("date_signature_mandat", "date_debut_autorisation", "date_fin_autorisation"):
        if champ in provided:
            out[champ] = _parse_date(champ, provided[champ])
    if "date_debut_autorisation" in provided or "date_fin_autorisation" in provided:
        debut = out.get("date_debut_autorisation") or _existing_date(existing, "date_debut_autorisation")
        fin = out.get("date_fin_autorisation") or _existing_date(existing, "date_fin_autorisation")
        if debut and fin and fin <= debut:
            raise ValidationError(
                "date_fin_autorisation",
                "La date_fin_autorisation doit être strictement postérieure à date_debut_autorisation.",
            )

    # --- civilite ---
    if "civilite" in provided:
        cv = provided["civilite"]
        if isinstance(cv, str) and cv.strip():
            cv = cv.strip().upper()
            if cv not in CIVILITES:
                raise ValidationError("civilite", "Le champ civilite doit être 'M' ou 'MME'.")
            out["civilite"] = cv
        else:
            out["civilite"] = None

    # --- raison_sociale / nom (+ ≥1 sur le merge) ---
    if "raison_sociale" in provided:
        rs = provided["raison_sociale"]
        rs = rs.strip() if isinstance(rs, str) and rs.strip() else None
        if rs is not None and len(rs) > RAISON_SOCIALE_MAX_LEN:
            raise ValidationError("raison_sociale", f"Le champ raison_sociale ne doit pas dépasser {RAISON_SOCIALE_MAX_LEN} caractères.")
        out["raison_sociale"] = rs
    if "nom" in provided:
        nom = provided["nom"]
        nom = nom.strip() if isinstance(nom, str) and nom.strip() else None
        if nom is not None and len(nom) > NOM_MAX_LEN:
            raise ValidationError("nom", f"Le champ nom ne doit pas dépasser {NOM_MAX_LEN} caractères.")
        out["nom"] = nom
    if "raison_sociale" in provided or "nom" in provided:
        merged_rs = out["raison_sociale"] if "raison_sociale" in out else _existing_str(existing, "raison_sociale")
        merged_nom = out["nom"] if "nom" in out else _existing_str(existing, "nom")
        if not merged_rs and not merged_nom:
            raise ValidationError("raison_sociale", "Au moins un des champs raison_sociale ou nom doit être renseigné.")

    # --- prenom (optionnel) ---
    if "prenom" in provided:
        pr = provided["prenom"]
        pr = pr.strip() if isinstance(pr, str) and pr.strip() else None
        if pr is not None and len(pr) > PRENOM_MAX_LEN:
            raise ValidationError("prenom", f"Le champ prenom ne doit pas dépasser {PRENOM_MAX_LEN} caractères.")
        out["prenom"] = pr

    # --- booléens (+ règles ≥1 sur le merge) ---
    for champ in BOOL_FIELDS:
        if champ in provided:
            out[champ] = _to_bool_str(champ, provided[champ])
    if "soutirage" in provided or "injection" in provided:
        s = out["soutirage"] if "soutirage" in out else _existing_bool_str(existing, "soutirage")
        i = out["injection"] if "injection" in out else _existing_bool_str(existing, "injection")
        if s != "true" and i != "true":
            raise ValidationError(None, "Au moins un des champs soutirage / injection doit être à true.")
    if "get_cdc" in provided or "get_dm" in provided:
        c = out["get_cdc"] if "get_cdc" in out else _existing_bool_str(existing, "get_cdc")
        d = out["get_dm"] if "get_dm" in out else _existing_bool_str(existing, "get_dm")
        if c != "true" and d != "true":
            raise ValidationError(None, "Au moins un des champs get_cdc / get_dm doit être à true.")

    return out
