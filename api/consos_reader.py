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
    Lit le parquet consos_publiees d'un PCE.

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
