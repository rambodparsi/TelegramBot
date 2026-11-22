"""LangGraph checkpoint saver backed by the D1 binding, without SQLAlchemy.

Subclasses the official ``WorkerCloudflareD1Saver`` and replaces only its connection
layer. Two deliberate differences from upstream:

* No ``sqlalchemy_cloudflare_d1`` import (it costs more CPU than the Free plan allows
  per request).
* Query failures raise ``D1Error`` instead of being silently reported as
  ``success=False``, so a lost checkpoint can never go unnoticed.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Optional, Sequence

from langgraph_checkpoint_cloudflare_d1.models import D1QueryResult, D1Response
from langgraph_checkpoint_cloudflare_d1.worker import WorkerCloudflareD1Saver

from storage import d1


class D1CheckpointSaver(WorkerCloudflareD1Saver):
    def __init__(self, db: Any, *, serde: Any = None) -> None:
        # Skip WorkerCloudflareD1Saver.__init__ (it imports SQLAlchemy); run the
        # BaseCheckpointSaver initializer directly.
        super(WorkerCloudflareD1Saver, self).__init__(serde=serde)
        self.enable_logging = False
        self._db = db
        # Tables are created once per saver instance (CREATE TABLE IF NOT EXISTS);
        # the app caches one saver per isolate, so this runs once per isolate.
        self.is_setup = False

    async def _execute_query(
        self, query: str, params: Optional[Sequence[Any]] = None
    ) -> D1Response:
        formatted: list[Any] = []
        for p in params or ():
            if isinstance(p, (dict, list)):
                formatted.append(json.dumps(p, separators=(",", ":")))
            elif isinstance(p, bytes):
                # Same encoding as upstream: BLOBs are stored as base64 text.
                formatted.append(base64.b64encode(p).decode("utf-8"))
            else:
                formatted.append(p)
        rows = await d1.query(self._db, query, formatted)
        return D1Response(success=True, result=[D1QueryResult(results=rows, success=True)])
