import base64
import io
import mimetypes
import os
import uuid

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from odoo.exceptions import ValidationError
from PIL import Image


def get_s3_settings():
    return {
        "access_key": os.getenv("AWS_ACCESS_KEY_ID"),
        "secret_key": os.getenv("AWS_SECRET_ACCESS_KEY"),
        "bucket": os.getenv("AWS_S3_BUCKET_NAME"),
        "region": os.getenv("AWS_S3_REGION", "ap-southeast-1"),
        "public_url": (os.getenv("AWS_S3_PUBLIC_URL") or "").rstrip("/"),
        "prefix": (os.getenv("AWS_S3_PREFIX") or "crm").strip("/"),
    }


def _validate_s3_settings(settings):
    missing = [
        name
        for name, value in (
            ("AWS_ACCESS_KEY_ID", settings["access_key"]),
            ("AWS_SECRET_ACCESS_KEY", settings["secret_key"]),
            ("AWS_S3_BUCKET_NAME", settings["bucket"]),
        )
        if not value
    ]
    if missing:
        raise ValidationError(
            "S3 is not configured. Missing environment variables: "
            + ", ".join(missing)
        )


def _build_object_key(folder, extension):
    settings = get_s3_settings()
    prefix = settings["prefix"]
    safe_folder = folder.strip("/")
    filename = f"{uuid.uuid4().hex}{extension}"
    if prefix and safe_folder:
        return f"{prefix}/{safe_folder}/{filename}"
    if prefix:
        return f"{prefix}/{filename}"
    if safe_folder:
        return f"{safe_folder}/{filename}"
    return filename


def _build_public_url(bucket, region, object_key):
    settings = get_s3_settings()
    if settings["public_url"]:
        return f"{settings['public_url']}/{object_key}"

    return f"https://{bucket}.s3.{region}.amazonaws.com/{object_key}"


def _normalize_base64_string(image_data):
    if not isinstance(image_data, str):
        return image_data

    normalized = image_data.strip()
    if normalized.startswith("data:"):
        _, _, normalized = normalized.partition(",")
    return normalized.strip()


def _decode_image_data(image_data):
    if not image_data:
        return b"", "image/jpeg", ".jpg"

    if isinstance(image_data, memoryview):
        image_data = image_data.tobytes()

    if isinstance(image_data, str):
        if image_data.startswith("http://") or image_data.startswith("https://"):
            return None, None, None
        try:
            image_bytes = base64.b64decode(_normalize_base64_string(image_data))
        except Exception as error:
            raise ValidationError("รูปภาพ base64 ไม่ถูกต้อง") from error
    else:
        image_bytes = base64.b64decode(image_data)

    image = Image.open(io.BytesIO(image_bytes))
    image_format = (image.format or "JPEG").upper()
    extension = mimetypes.guess_extension(f"image/{image_format.lower()}") or ".jpg"
    if extension == ".jpe":
        extension = ".jpg"

    mime_type = Image.MIME.get(image_format, "image/jpeg")
    return image_bytes, mime_type, extension


def _resize_image(image_bytes, max_width, max_height):
    image = Image.open(io.BytesIO(image_bytes))
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGB")

    image.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)

    output = io.BytesIO()
    save_format = image.format or "JPEG"
    if save_format.upper() not in Image.MIME:
        save_format = "JPEG"

    if save_format.upper() == "JPEG" and image.mode == "RGBA":
        image = image.convert("RGB")

    image.save(output, format=save_format, quality=85, optimize=True)
    mime_type = Image.MIME.get(save_format.upper(), "image/jpeg")
    extension = mimetypes.guess_extension(mime_type) or ".jpg"
    if extension == ".jpe":
        extension = ".jpg"

    return output.getvalue(), mime_type, extension


def upload_image_base64(
    image_data,
    folder,
    max_width=1920,
    max_height=1920,
):
    decoded = _decode_image_data(image_data)
    if decoded[0] is None:
        return image_data

    image_bytes, mime_type, extension = decoded
    if not image_bytes:
        return False

    image_bytes, mime_type, extension = _resize_image(
        image_bytes,
        max_width,
        max_height,
    )

    settings = get_s3_settings()
    _validate_s3_settings(settings)

    object_key = _build_object_key(folder, extension)
    client = boto3.client(
        "s3",
        aws_access_key_id=settings["access_key"],
        aws_secret_access_key=settings["secret_key"],
        region_name=settings["region"],
    )

    try:
        client.put_object(
            Bucket=settings["bucket"],
            Key=object_key,
            Body=image_bytes,
            ContentType=mime_type,
        )
    except (BotoCoreError, ClientError) as error:
        raise ValidationError(f"Failed to upload image to S3: {error}") from error

    return _build_public_url(settings["bucket"], settings["region"], object_key)
