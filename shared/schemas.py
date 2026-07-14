"""
schemas.py
──────────────────────────────────────────────────────────────────────────
Définition des schémas Delta Silver pour le pipeline ENEDIS FENIX.
Calque exact du schéma MySQL `enedis` actuel, avec 3 couches de garanties :

  1. ENUMS Python (StatutPDL, BoolStr, EtatActivation, ...) qui définissent
     les valeurs autorisées par MySQL.

  2. Schémas PyArrow avec MÉTADONNÉES (mysql_type, max_length, enum_values)
     qui conservent les contraintes MySQL dans le schéma lui-même.

  3. Fonction `validate_dataframe()` qui vérifie un DataFrame avant écriture
     Delta : longueurs varchar, valeurs enum, NOT NULL, etc.

Auteur : pipeline-enedis-fenix
"""

from enum import Enum
import pyarrow as pa


# ═══════════════════════════════════════════════════════════════════════════
# COUCHE 1 — ENUMS PYTHON
# Définissent les valeurs autorisées par les colonnes ENUM de MySQL.
# Utilisez ces constantes dans le code F1/F2/F4 pour éviter les typos.
# ═══════════════════════════════════════════════════════════════════════════


class BoolStr(str, Enum):
    """Équivalent des enum('true','false') de MySQL."""
    TRUE = "true"
    FALSE = "false"


class StatutPDL(str, Enum):
    """Valeurs autorisées pour pdl.statut."""
    NOUVEAU = "nouveau"
    PROCESSING = "processing"
    TRAITE = "traite"
    ERREUR = "erreur"
    PARTIEL = "partiel"
    REVOQUE = "revoque"
    RESILIE = "résilié"


class EtatActivation(str, Enum):
    """Valeurs typiques pour services_souscrits.etat_activation
    (ENEDIS définit ces valeurs côté SGE)."""
    ACTIF = "ACTIF"
    DEMANDE = "DEMANDE"
    ANNULE = "ANNULE"
    RESILIE = "RESILIE"


class Segment(str, Enum):
    """Segments clients ENEDIS (dtc.segment)."""
    C1 = "C1"
    C2 = "C2"
    C3 = "C3"
    C4 = "C4"
    C5 = "C5"
    # Variantes P1-P4 vues dans le code Groovy NiFi
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class MesuresTypeCode(str, Enum):
    """services_souscrits.mesures_type_code et demandes_historique.mesures_type_code."""
    CDC = "CDC"        # Courbe De Charge
    IDX = "IDX"        # Index
    PMAX = "PMAX"      # Puissance MAX
    COURBES = "COURBES"  # Variante observée dans le flow NiFi (R63)
    INDEX = "INDEX"      # Variante observée dans le flow NiFi (R64)


class CodePrestationServices(str, Enum):
    """services_souscrits.code_prestation — DÉRIVÉ par NiFi
    Valeurs observées dans la base : F300C, F305, F305A, F300B, P300B, P305A."""
    F300B = "F300B"   # CDC C1-C4 corrigée ou brute
    F300C = "F300C"   # CDC tous segments, PT30M
    F305 = "F305"     # IDX tous segments
    F305A = "F305A"   # IDX C1-C4
    P300B = "P300B"   # CDC P1-P3
    P305A = "P305A"   # IDX P1-P3


class CodeFlux(str, Enum):
    """services_souscrits.code_flux — DÉRIVÉ par NiFi."""
    R50 = "R50"       # Courbe charge C5 30 min
    R151 = "R151"     # Index C5
    R4Q = "R4Q"       # CDC C1-C4 (variante)
    R4H = "R4H"       # CDC C1-C4 (variante)
    R171 = "R171"     # Index C1-C4


class CodePrestationDemande(str, Enum):
    """demandes_historique.code_prestation — DIFFÉRENT de services_souscrits !
    Valeurs observées en base."""
    R63_BRUTE_SOUTIRAGE = "R63_BRUTE_SOUTIRAGE"
    R63_BRUTE_INJECTION = "R63_BRUTE_INJECTION"
    R63_CORRIGEE_SOUTIRAGE = "R63_CORRIGEE_SOUTIRAGE"
    R63_CORRIGEE_INJECTION = "R63_CORRIGEE_INJECTION"
    R64_BRUTE_SOUTIRAGE = "R64_BRUTE_SOUTIRAGE"
    R64_BRUTE_INJECTION = "R64_BRUTE_INJECTION"
    R66_PMAX_SOUTIRAGE = "R66_PMAX_SOUTIRAGE"


class ServiceSouscritType(str, Enum):
    """services_souscrits.service_souscrit_type
    NiFi ne stocke que TRANSREC (filtré à l'INSERT IGNORE)."""
    TRANSREC = "TRANSREC"


# ═══════════════════════════════════════════════════════════════════════════
# Helper pour construire les métadonnées de manière concise
# ═══════════════════════════════════════════════════════════════════════════


def _meta(mysql_type: str, max_length: int | None = None,
          enum_values: list[str] | None = None,
          default: str | None = None) -> dict:
    """Construit un dict de métadonnées pour pa.field(metadata=...).
    Les valeurs sont sérialisées en bytes (exigence PyArrow)."""
    md = {"mysql_type": mysql_type}
    if max_length is not None:
        md["max_length"] = str(max_length)
    if enum_values is not None:
        md["enum_values"] = ",".join(enum_values)
    if default is not None:
        md["default"] = default
    # PyArrow attend des bytes
    return {k.encode(): v.encode() for k, v in md.items()}


# ═══════════════════════════════════════════════════════════════════════════
# COUCHE 2 — SCHÉMAS PYARROW AVEC MÉTADONNÉES
# ═══════════════════════════════════════════════════════════════════════════


# ─────────────────────────────────────────────────────────────────────────
# Table 1 : pdl  (22 colonnes)
# ─────────────────────────────────────────────────────────────────────────
SCHEMA_PDL = pa.schema([
    # Identifiants & métier (remplis par source externe)
    pa.field("id_pdl",                  pa.string(), nullable=False,
             metadata=_meta("varchar(14)", max_length=14)),
    pa.field("partner",                 pa.string(), nullable=True,
             metadata=_meta("varchar(100)", max_length=100)),
    # platform_code : code de la plateforme d'origine (métadonnée, saisi à la
    # création). NOT NULL + non modifiable ensuite (pendant API). Équivalent du
    # platform_code GRDF.
    pa.field("platform_code",           pa.string(), nullable=False,
             metadata=_meta("varchar(10)", max_length=10)),
    pa.field("date_signature_mandat",   pa.date32(), nullable=True,
             metadata=_meta("date")),
    pa.field("date_debut_autorisation", pa.date32(), nullable=True,
             metadata=_meta("date")),
    pa.field("date_fin_autorisation",   pa.date32(), nullable=True,
             metadata=_meta("date")),  # envoyée à ENEDIS dans l'activation
    pa.field("raison_sociale",          pa.string(), nullable=True,
             metadata=_meta("varchar(100)", max_length=100)),
    pa.field("civilite",                pa.string(), nullable=True,
             metadata=_meta("varchar(20)", max_length=20)),
    pa.field("nom",                     pa.string(), nullable=True,
             metadata=_meta("varchar(50)", max_length=50)),
    pa.field("prenom",                  pa.string(), nullable=True,
             metadata=_meta("varchar(50)", max_length=50)),

    # Horodatages (auto MySQL)
    pa.field("date_creation",           pa.timestamp("us"), nullable=True,
             metadata=_meta("datetime", default="CURRENT_TIMESTAMP")),
    pa.field("date_modification",       pa.timestamp("us"), nullable=True,
             metadata=_meta("datetime",
                            default="CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")),

    # Choix métier (source externe)
    pa.field("injection",               pa.string(), nullable=True,
             metadata=_meta("enum('true','false')",
                            enum_values=[e.value for e in BoolStr])),
    pa.field("soutirage",               pa.string(), nullable=True,
             metadata=_meta("enum('true','false')",
                            enum_values=[e.value for e in BoolStr])),
    pa.field("get_cdc",                 pa.string(), nullable=False,
             metadata=_meta("enum('true','false')",
                            enum_values=[e.value for e in BoolStr],
                            default="true")),
    pa.field("get_dm",                  pa.string(), nullable=False,
             metadata=_meta("enum('true','false')",
                            enum_values=[e.value for e in BoolStr],
                            default="true")),

    # Calculés par F1 depuis les DTC
    pa.field("statut_cdc",              pa.string(), nullable=True,
             metadata=_meta("enum('true','false')",
                            enum_values=[e.value for e in BoolStr])),
    pa.field("statut_dm",               pa.string(), nullable=True,
             metadata=_meta("enum('true','false')",
                            enum_values=[e.value for e in BoolStr])),

    # Rempli par F5 (parser R66)
    pa.field("date_premiere_valeur_dm", pa.date32(), nullable=True,
             metadata=_meta("date")),

    # Workflow F1
    pa.field("statut",                  pa.string(), nullable=False,
             metadata=_meta(
                 "enum('nouveau','traite','revoque','erreur','partiel','processing','résilié')",
                 enum_values=[e.value for e in StatutPDL],
                 default="nouveau")),
    pa.field("commentaire",             pa.string(), nullable=True,
             metadata=_meta("varchar(50)", max_length=50)),

    # 🆕 Colonne erreur — JSON court de la dernière erreur ENEDIS rencontrée
    # Format : {"code_statut_traitement":"...","message_retour_traitement":"..."}
    # Mise à jour par F1/F2/F4 en cas d'erreur ; remise à NULL en cas de succès ultérieur.
    # ⚠️ Déviation volontaire du calque MySQL strict.
    pa.field("erreur",                  pa.string(), nullable=True,
             metadata=_meta("string (JSON)",
                            default="null")),
])


# ─────────────────────────────────────────────────────────────────────────
# Table 2 : dtc  (22 colonnes)
# ─────────────────────────────────────────────────────────────────────────
SCHEMA_DTC = pa.schema([
    pa.field("id_pdl",                          pa.string(), nullable=False,
             metadata=_meta("varchar(14)", max_length=14)),

    # Données générales
    pa.field("etat_contractuel",                pa.string(), nullable=True,
             metadata=_meta("varchar(6)", max_length=6)),
    pa.field("segment",                         pa.string(), nullable=True,
             metadata=_meta("varchar(5)", max_length=5,
                            enum_values=[e.value for e in Segment])),
    pa.field("niveau_ouverture_services",       pa.int64(),  nullable=True,
             metadata=_meta("int(11)")),

    # Compteur physique
    pa.field("matricule",                       pa.string(), nullable=True,
             metadata=_meta("varchar(50)", max_length=50)),
    pa.field("type_compteur",                   pa.string(), nullable=True,
             metadata=_meta("varchar(6)", max_length=6)),

    # Alimentation
    pa.field("tension_livraison",               pa.string(), nullable=True,
             metadata=_meta("varchar(10)", max_length=10)),

    # Situation contractuelle
    pa.field("puissance_souscrite_max_valeur",  pa.string(), nullable=True,
             metadata=_meta("varchar(10)", max_length=10)),
    pa.field("puissance_souscrite_max_unite",   pa.string(), nullable=True,
             metadata=_meta("varchar(4)", max_length=4)),
    pa.field("longue_utilisation_contexte",     pa.string(), nullable=True,
             metadata=_meta("varchar(4)", max_length=4)),

    # Caractéristiques de relève
    pa.field("mode_traitement",                 pa.string(), nullable=True,
             metadata=_meta("varchar(5)", max_length=5)),
    pa.field("periodicite_releve",              pa.string(), nullable=True,
             metadata=_meta("varchar(6)", max_length=6)),
    pa.field("plage_releve",                    pa.string(), nullable=True,
             metadata=_meta("varchar(4)", max_length=4)),
    pa.field("mode_releve",                     pa.string(), nullable=True,
             metadata=_meta("varchar(4)", max_length=4)),
    pa.field("media_releve",                    pa.string(), nullable=True,
             metadata=_meta("varchar(3)", max_length=3)),

    # Adresse
    pa.field("escalier_etage_appartement",      pa.string(), nullable=True,
             metadata=_meta("varchar(255)", max_length=255)),
    pa.field("batiment",                        pa.string(), nullable=True,
             metadata=_meta("varchar(255)", max_length=255)),
    pa.field("numero_voie",                     pa.string(), nullable=True,
             metadata=_meta("varchar(255)", max_length=255)),
    pa.field("lieu_dit",                        pa.string(), nullable=True,
             metadata=_meta("varchar(255)", max_length=255)),
    pa.field("code_postal",                     pa.string(), nullable=True,
             metadata=_meta("varchar(5)", max_length=5)),
    pa.field("commune",                         pa.string(), nullable=True,
             metadata=_meta("varchar(255)", max_length=255)),
    pa.field("code_insee",                      pa.string(), nullable=True,
             metadata=_meta("varchar(5)", max_length=5)),
])


# ─────────────────────────────────────────────────────────────────────────
# Table 3 : services_souscrits  (14 colonnes)
# PK composite : (id_pdl, id_service_souscrit)
# ─────────────────────────────────────────────────────────────────────────
SCHEMA_SERVICES_SOUSCRITS = pa.schema([
    # PK composite
    pa.field("id_pdl",                   pa.string(), nullable=False,
             metadata=_meta("varchar(14)", max_length=14)),
    pa.field("id_service_souscrit",      pa.string(), nullable=False,
             metadata=_meta("varchar(10)", max_length=10)),

    # DÉRIVÉS par NiFi (pas reçus d'ENEDIS)
    pa.field("code_prestation",          pa.string(), nullable=True,
             metadata=_meta("varchar(5)", max_length=5,
                            enum_values=[e.value for e in CodePrestationServices])),
    pa.field("code_flux",                pa.string(), nullable=True,
             metadata=_meta("varchar(4)", max_length=4,
                            enum_values=[e.value for e in CodeFlux])),

    # Identifiants reçus d'ENEDIS
    pa.field("service_souscrit_type",    pa.string(), nullable=True,
             metadata=_meta("varchar(8)", max_length=8,
                            enum_values=[e.value for e in ServiceSouscritType])),

    # Caractéristiques de la mesure (reçus d'ENEDIS)
    pa.field("mesures_type_code",        pa.string(), nullable=True,
             metadata=_meta("varchar(3)", max_length=3,
                            enum_values=[e.value for e in MesuresTypeCode])),
    pa.field("mesures_pas",              pa.string(), nullable=True,
             metadata=_meta("varchar(5)", max_length=5)),
    pa.field("mesures_corrigees",        pa.string(), nullable=True,
             metadata=_meta("enum('true','false')",
                            enum_values=[e.value for e in BoolStr])),
    pa.field("periodicite_transmission", pa.string(), nullable=True,
             metadata=_meta("varchar(3)", max_length=3)),

    # Périmètre et état
    pa.field("etat_activation",          pa.string(), nullable=True,
             metadata=_meta("varchar(7)", max_length=7,
                            enum_values=[e.value for e in EtatActivation])),
    pa.field("date_debut",               pa.date32(), nullable=True,
             metadata=_meta("date")),
    pa.field("date_fin",                 pa.date32(), nullable=True,
             metadata=_meta("date")),
    pa.field("soutirage",                pa.string(), nullable=True,
             metadata=_meta("enum('true','false')",
                            enum_values=[e.value for e in BoolStr])),
    pa.field("injection",                pa.string(), nullable=True,
             metadata=_meta("enum('true','false')",
                            enum_values=[e.value for e in BoolStr])),
])


# ─────────────────────────────────────────────────────────────────────────
# Table 4 : demandes_historique  (11 colonnes)
# PK : id_affaire seul
# ─────────────────────────────────────────────────────────────────────────
SCHEMA_DEMANDES_HISTORIQUE = pa.schema([
    # PK
    pa.field("id_affaire",              pa.string(), nullable=False,
             metadata=_meta("varchar(50)", max_length=50)),

    # FK informelle vers pdl
    pa.field("id_pdl",                  pa.string(), nullable=False,
             metadata=_meta("varchar(14)", max_length=14)),

    # Envoyés à ENEDIS dans la demande
    pa.field("code_prestation",         pa.string(), nullable=True,
             metadata=_meta("varchar(50)", max_length=50,
                            enum_values=[e.value for e in CodePrestationDemande])),
    pa.field("mesures_corrigees",       pa.string(), nullable=True,
             metadata=_meta("enum('true','false')",
                            enum_values=[e.value for e in BoolStr])),
    pa.field("mesures_type_code",       pa.string(), nullable=True,
             metadata=_meta("varchar(50)", max_length=50,
                            enum_values=[e.value for e in MesuresTypeCode])),
    pa.field("mesures_pas",             pa.string(), nullable=True,
             metadata=_meta("varchar(10)", max_length=10)),
    pa.field("date_debut_demandee",     pa.date32(), nullable=True,
             metadata=_meta("date")),
    pa.field("date_fin_demandee",       pa.date32(), nullable=True,
             metadata=_meta("date")),

    # Auto MySQL
    pa.field("datetime_requete",        pa.timestamp("us"), nullable=True,
             metadata=_meta("datetime", default="CURRENT_TIMESTAMP")),

    # Mis à jour par F5 à la réception du fichier
    # ⚠️ date_premiere_valeur est un datetime/timestamp (et non un date32 comme
    # date_debut_demandee/date_fin_demandee) — c'est volontaire, calque MySQL.
    pa.field("date_premiere_valeur",    pa.timestamp("us"), nullable=True,
             metadata=_meta("datetime")),

    # CORRECTION : nullable=True pour rester aligné MySQL (Null=YES, default=false)
    pa.field("fichier_traite",          pa.string(), nullable=True,
             metadata=_meta("enum('true','false')",
                            enum_values=[e.value for e in BoolStr],
                            default="false")),
])


# ─────────────────────────────────────────────────────────────────────────
# Table 5 : log_erreur  (5 colonnes, append-only)
# ─────────────────────────────────────────────────────────────────────────
SCHEMA_LOG_ERREUR = pa.schema([
    pa.field("id_pdl",          pa.string(),         nullable=False,
             metadata=_meta("varchar(14)", max_length=14)),
    pa.field("code_erreur",     pa.string(),         nullable=True,
             metadata=_meta("varchar")),  # SGT401, SGT4G3, SGT570, ...
    pa.field("message_erreur",  pa.string(),         nullable=True,
             metadata=_meta("text")),
    pa.field("timestamp",       pa.timestamp("us"),  nullable=True,
             metadata=_meta("datetime")),
    pa.field("service_appele",  pa.string(),         nullable=True,
             metadata=_meta("varchar")),
])


# ─────────────────────────────────────────────────────────────────────────
# Table 6 : donnees_mesures  (F3 — ConsultationMesures dimanche 1h)
# Format pivot Energisme + bornes de période (id_pdl / date_debut / date_fin)
# ─────────────────────────────────────────────────────────────────────────
#
# Fidèle au flux NiFi "Convert to Kafka Format" (label, val, ts, fmt, uuid, misc),
# ENRICHI de id_pdl (partition + clé MERGE) et des bornes réelles de période
# date_debut/date_fin — indispensables pour présenter la conso PAR PÉRIODE
# (le pivot Kafka strict n'avait que `ts`). Doit rester synchronisé avec
# pipeline_enedis_F1/shared/schemas.py (source de vérité).
#
# Path Silver : fenixlake/enedis/silver/donnees_mesures/
# Partitionnement Delta : id_pdl (1 sous-dossier par compteur)
# Clé d'unicité / MERGE keep-latest : (id_pdl, label, ts)
SCHEMA_DONNEES_MESURES = pa.schema([
    # ── Compteur : clé de partition + clé de MERGE keep-latest ──
    pa.field("id_pdl",         pa.string(),         nullable=False,
             metadata=_meta("varchar(14)", max_length=14)),

    # ── champs iso format pivot Energisme (ce que Kafka recevait) ──
    pa.field("label",          pa.string(),         nullable=True,
             metadata=_meta("varchar(100)", max_length=100)),  # "TURPE_HCB", "FRN_P", "TOTAL"
    pa.field("val",            pa.float64(),        nullable=True,
             metadata=_meta("double")),
    pa.field("ts",             pa.timestamp("us"),  nullable=True,
             metadata=_meta("datetime")),     # dateFin - 1 jour
    # 🆕 Bornes réelles de la période de mesure (conservées depuis le XML CM).
    pa.field("date_debut",     pa.date32(),         nullable=True,
             metadata=_meta("date")),         # dateDebut de la période
    pa.field("date_fin",       pa.date32(),         nullable=True,
             metadata=_meta("date")),         # dateFin de la période
    pa.field("fmt",            pa.int32(),          nullable=True,
             metadata=_meta("int")),          # toujours 1
    pa.field("uuid",           pa.string(),         nullable=True,
             metadata=_meta("varchar(50)", max_length=50)),    # "enedis_CM_<pdl>"
    pa.field("misc",           pa.string(),         nullable=True,
             metadata=_meta("varchar(20)", max_length=20)),    # INITIALE/RECTIFIEE/TOTAL_TURPE/TOTAL_FRN

    # ── Audit ──
    pa.field("ingestion_ts",   pa.timestamp("us"),  nullable=True,
             metadata=_meta("datetime")),     # timestamp précis d'écriture
])


# ═══════════════════════════════════════════════════════════════════════════
# Registre des schémas
# ═══════════════════════════════════════════════════════════════════════════


SCHEMAS = {
    "pdl":                  SCHEMA_PDL,
    "dtc":                  SCHEMA_DTC,
    "services_souscrits":   SCHEMA_SERVICES_SOUSCRITS,
    "demandes_historique":  SCHEMA_DEMANDES_HISTORIQUE,
    "log_erreur":           SCHEMA_LOG_ERREUR,
    "donnees_mesures":      SCHEMA_DONNEES_MESURES,
}


# ═══════════════════════════════════════════════════════════════════════════
# COUCHE 3 — VALIDATOR
# Vérifie un DataFrame (ou un pa.Table) avant écriture Delta :
#   - colonnes attendues présentes
#   - pas de colonnes inattendues
#   - NOT NULL respecté
#   - longueur varchar respectée
#   - valeurs enum respectées
# ═══════════════════════════════════════════════════════════════════════════


class ValidationError(Exception):
    """Levée quand un DataFrame ne respecte pas le schéma cible."""
    pass


def _decode_metadata(field: pa.Field) -> dict:
    """Décode les métadonnées bytes d'un pa.Field en dict str→str."""
    if field.metadata is None:
        return {}
    return {k.decode(): v.decode() for k, v in field.metadata.items()}


def validate_dataframe(df, table_name: str, strict: bool = True) -> list[str]:
    """
    Valide un DataFrame Pandas contre le schéma cible.

    Paramètres
    ----------
    df : pandas.DataFrame
        Le DataFrame à valider avant écriture Delta.
    table_name : str
        Nom de la table cible ('pdl', 'dtc', ...).
    strict : bool
        Si True, lève ValidationError dès la première erreur.
        Si False, retourne la liste de toutes les erreurs.

    Retour
    ------
    list[str]
        Liste des erreurs détectées (vide si tout est OK).
    """
    if table_name not in SCHEMAS:
        raise ValueError(
            f"Table inconnue : '{table_name}'. "
            f"Tables disponibles : {list(SCHEMAS.keys())}"
        )

    schema = SCHEMAS[table_name]
    expected_columns = {field.name for field in schema}
    actual_columns = set(df.columns)
    errors: list[str] = []

    # 1. Colonnes manquantes
    missing = expected_columns - actual_columns
    if missing:
        errors.append(f"Colonnes manquantes : {sorted(missing)}")

    # 2. Colonnes en trop
    extra = actual_columns - expected_columns
    if extra:
        errors.append(f"Colonnes inattendues : {sorted(extra)}")

    # 3. Pour chaque champ du schéma, vérifications fines
    for field in schema:
        if field.name not in df.columns:
            continue  # déjà signalé ci-dessus

        col = df[field.name]
        meta = _decode_metadata(field)

        # 3.a NOT NULL
        if not field.nullable:
            n_null = col.isna().sum()
            if n_null > 0:
                errors.append(
                    f"Colonne '{field.name}' : {n_null} valeur(s) NULL "
                    f"interdite(s) (NOT NULL)."
                )

        # 3.b Longueur varchar
        if "max_length" in meta and pa.types.is_string(field.type):
            max_len = int(meta["max_length"])
            non_null = col.dropna().astype(str)
            too_long = non_null[non_null.str.len() > max_len]
            if len(too_long) > 0:
                errors.append(
                    f"Colonne '{field.name}' : {len(too_long)} valeur(s) "
                    f"dépassent max_length={max_len}. "
                    f"Ex : '{too_long.iloc[0]}' (len={len(too_long.iloc[0])})."
                )

        # 3.c Enum
        if "enum_values" in meta and pa.types.is_string(field.type):
            allowed = set(meta["enum_values"].split(","))
            non_null = col.dropna().astype(str)
            invalid = non_null[~non_null.isin(allowed)]
            if len(invalid) > 0:
                errors.append(
                    f"Colonne '{field.name}' : {len(invalid)} valeur(s) "
                    f"hors enum {sorted(allowed)}. "
                    f"Ex : '{invalid.iloc[0]}'."
                )

    if strict and errors:
        raise ValidationError(
            f"❌ Validation échouée pour la table '{table_name}' :\n  - "
            + "\n  - ".join(errors)
        )

    return errors


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def list_columns(table_name: str) -> list[str]:
    """Retourne la liste des noms de colonnes d'une table."""
    return [field.name for field in SCHEMAS[table_name]]


def get_schema(table_name: str) -> pa.Schema:
    """Retourne le schéma PyArrow d'une table par son nom."""
    if table_name not in SCHEMAS:
        raise ValueError(
            f"Table inconnue : '{table_name}'. "
            f"Tables disponibles : {list(SCHEMAS.keys())}"
        )
    return SCHEMAS[table_name]


def print_schema(table_name: str) -> None:
    """Affiche le schéma d'une table de façon lisible avec ses métadonnées."""
    schema = get_schema(table_name)
    print(f"\n📋 Schéma de la table '{table_name}' ({len(schema)} colonnes)\n")
    for field in schema:
        nullable = "NULL OK " if field.nullable else "NOT NULL"
        meta = _decode_metadata(field)
        meta_str = ""
        if "mysql_type" in meta:
            meta_str = f"  ← MySQL: {meta['mysql_type']}"
        if "enum_values" in meta:
            meta_str += f"  [{meta['enum_values']}]"
        if "default" in meta:
            meta_str += f"  default={meta['default']}"
        print(f"  • {field.name:<35} {str(field.type):<20} [{nullable}]{meta_str}")


if __name__ == "__main__":
    # Affichage des 5 schémas si on lance directement le fichier
    for table_name in SCHEMAS:
        print_schema(table_name)