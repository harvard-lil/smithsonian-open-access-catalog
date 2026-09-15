"""boto3 clients and the few S3 calls the pipeline makes outside DuckDB."""

import logging
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig
from botocore import UNSIGNED
from botocore.client import BaseClient
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from smithsonian_open_access_catalog.config import Config

logger = logging.getLogger(__name__)

# The bucket policy grants PutObject but not AbortMultipartUpload, so upload in one PUT
SINGLE_PUT_THRESHOLD = 5 * 1024**3


class UploadError(Exception):
    pass


def anonymous_client(config: Config, max_pool: int) -> BaseClient:
    return boto3.client(
        's3',
        region_name=config.region,
        endpoint_url=config.endpoint_url,
        config=BotoConfig(
            signature_version=UNSIGNED,
            retries={'mode': 'adaptive', 'max_attempts': 10},
            s3={'addressing_style': 'path'},
            max_pool_connections=max_pool,
            connect_timeout=10,
            read_timeout=60,
        ),
    )


def upload_client(config: Config) -> BaseClient:
    return boto3.client(
        's3',
        region_name=config.region,
        endpoint_url=config.endpoint_url,
        config=BotoConfig(
            retries={'mode': 'standard', 'max_attempts': 5},
            s3={'addressing_style': 'path'},
        ),
    )


def download_if_exists(client: BaseClient, bucket: str, key: str, destination: Path) -> bool:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        client.download_file(bucket, key, str(destination))
    except ClientError as error:
        if error.response['Error']['Code'] in ('404', 'NoSuchKey', 'NotFound'):
            return False
        raise
    return True


def upload_file(client: BaseClient, bucket: str, key: str, path: Path) -> None:
    client.upload_file(
        str(path),
        bucket,
        key,
        Config=TransferConfig(multipart_threshold=SINGLE_PUT_THRESHOLD),
    )
    stored = client.head_object(Bucket=bucket, Key=key)['ContentLength']
    expected = path.stat().st_size
    if stored != expected:
        raise UploadError(f's3://{bucket}/{key}: stored {stored} bytes, expected {expected}')
