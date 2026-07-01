import json
from urllib.parse import quote

from odoo.http import Response


def json_response(payload, status=200):
    return Response(
        json.dumps(payload, ensure_ascii=False),
        status=status,
        content_type="application/json; charset=utf-8",
    )


def _attachment_content_disposition(filename):
    ascii_filename = "".join(
        char if ord(char) < 128 and (char.isalnum() or char in {".", "-", "_"}) else "_"
        for char in filename
    ).strip("._") or "download.csv"
    encoded_filename = quote(filename, safe="")
    return (
        f'attachment; filename="{ascii_filename}"; '
        f"filename*=UTF-8''{encoded_filename}"
    )


def csv_response(content, filename, status=200):
    if isinstance(content, str):
        content = content.encode("utf-8-sig")

    return Response(
        content,
        status=status,
        headers=[
            ("Content-Type", "text/csv; charset=utf-8"),
            ("Content-Disposition", _attachment_content_disposition(filename)),
        ],
    )
