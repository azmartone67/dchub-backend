"""Project-wide JSON encoder. Handles Decimal, datetime, UUID, bytes, set."""
from __future__ import annotations

import datetime
import decimal
import json
import uuid
from typing import Any

from fastapi.responses import JSONResponse


class DCHubJSONEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, decimal.Decimal):
            return float(o)
        if isinstance(o, (datetime.datetime, datetime.date)):
            return o.isoformat()
        if isinstance(o, datetime.timedelta):
            return o.total_seconds()
        if isinstance(o, uuid.UUID):
            return str(o)
        if isinstance(o, (bytes, bytearray)):
            return o.decode("utf-8", errors="replace")
        if isinstance(o, set):
            return list(o)
        return super().default(o)


class DCHubJSONResponse(JSONResponse):
    """JSONResponse using DCHubJSONEncoder. Set as FastAPI default_response_class."""

    def render(self, content: Any) -> bytes:
        return json.dumps(
            content,
            cls=DCHubJSONEncoder,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
