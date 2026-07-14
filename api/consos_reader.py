"""
Lecture des consommations publiées (Silver) pour la route /consommations.

Lit le parquet partitionné par PCE :
  {GRDF_ROOT_FOLDER}/silver/consos_publiees/id_pce={sensor_id}/data.parquet

C'est le MÊME fichier que produit le pipeline (adls_writer.write_silver_consos
source="publiees"). On lit les intervalles BRUTS, sans agrégation.

Auth : DefaultAzureCredential (az login en local, identité managée en prod),
comme registry_dao.
"""

import io
import logging
import os
from typing import Optional

import pyarrow.parquet as pq
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

from shared.config import Config

logger = logging.getLogger(__name__)

_credential: Optional[DefaultAzureCredential] = None


def _get_credential() -> DefaultAzureCredential:
    global _credential
    if _credential is None:
        _credential = DefaultAzureCredential()
    return _credential


def _blob_service() -> BlobServiceClient:
    """Connection string si fournie (STORAGE_CONNECTION_STRING), sinon DefaultAzureCredential."""
    conn = os.environ.get("STORAGE_CONNECTION_STRING") or os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if conn:
        return BlobServiceClient.from_connection_string(conn)
    return BlobServiceClient(
        account_url=Config.storage_account_url(),
        credential=_get_credential(),
    )


def _consos_publiees_blob_path(sensor_id: str) -> str:
    """Chemin du parquet consos_publiees d'un PCE (sensor_id = id_pce brut)."""
    return f"{Config.GRDF_ROOT_FOLDER}/silver/consos_publiees/id_pce={sensor_id}/data.parquet"


def read_consos_publiees(sensor_id: str) -> Optional[list[dict]]:
    """
    Lit le parquet consos_publiees d'un PCE (GRDF).

    Returns:
        list[dict] des intervalles (peut être vide si parquet présent mais vide),
        None si le parquet n'existe pas (→ 404 côté handler).
    """
    blob_client = _blob_service().get_blob_client(
        container=Config.CONTAINER_NAME,
        blob=_consos_publiees_blob_path(sensor_id),
    )
    try:
        data = blob_client.download_blob().readall()
    except ResourceNotFoundError:
        logger.info("Aucun parquet consos_publiees pour sensor_id=%s", sensor_id)
        return None

    table = pq.read_table(io.BytesIO(data))
    rows = table.to_pandas().to_dict(orient="records")
    logger.info("consos_publiees lues pour %s : %d intervalle(s)", sensor_id, len(rows))
    return rows


def _iso_date(value) -> Optional[str]:
    """Normalise une date Delta (date32/Timestamp/date/str) en 'YYYY-MM-DD'. None si vide."""
    import pandas as pd

    if value is None:
        return None
    try:
        ts = pd.to_datetime(value, errors="coerce")
    except (ValueError, TypeError):
        return None
    if pd.isna(ts):
        return None
    return ts.date().isoformat()


def read_consos_enedis(sensor_id: str) -> Optional[list[dict]]:
    """
    Lit les mesures Enedis (table Delta `donnees_mesures`) d'un PDL et ne garde
    que les lignes agrégées `label == "TOTAL"` (une valeur de conso par période).

    Mappe vers la forme COMMUNE {date_debut, date_fin, consommation} : la valeur
    `val` est renvoyée BRUTE (aucune transformation, aucun parsing). L'unité n'est
    pas stockée → le handler applique "kWh" par défaut, comme GRDF.

    sensor_id = id_pdl (14 chiffres). Filtre sur `id_pdl` (clé de partition).

    Returns:
        list[dict] (peut être vide), None si aucune mesure pour ce PDL ou table
        absente (→ 404 côté handler).
    """
    from shared import adls_client  # import tardif (dépend de settings/deltalake)

    try:
        df = adls_client.read_table_filtered("donnees_mesures", "id_pdl", "=", sensor_id)
    except Exception as exc:  # table absente, backend indisponible, etc.
        logger.info("Lecture donnees_mesures impossible pour id_pdl=%s : %s", sensor_id, exc)
        return None

    if df is None or len(df) == 0:
        return None

    total = df[df["label"] == "TOTAL"]
    rows = [
        {
            "date_debut": _iso_date(r.get("date_debut")),
            "date_fin": _iso_date(r.get("date_fin")),
            "consommation": r.get("val"),
        }
        for _, r in total.iterrows()
    ]
    logger.info("donnees_mesures TOTAL pour id_pdl=%s : %d ligne(s)", sensor_id, len(rows))
    return rows
