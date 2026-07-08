import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core import messages
from app.core.db import get_db
from app.schemas.qr import (
    QrResolveRequest,
    QrContextResponse,
    QrStore,
    QrTable,
)
from app.services.qr_token_service import (
    resolve_token,
    token_prefix,
    QrTableUnavailable,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/public/qr-context", tags=["Public QR Context"])


@router.post("/resolve", response_model=QrContextResponse)
def resolve_qr_context(
    body: QrResolveRequest,
    db: Session = Depends(get_db),
) -> QrContextResponse:
    """
    Resolve an opaque QR token to trustworthy store/table context.

    The client never supplies store or table ids — they are derived here from
    the token. Invalid, unknown, malformed and revoked tokens all return the
    same public error so a probing client cannot learn whether a token ever
    existed. Only the non-secret prefix is ever logged.
    """
    try:
        ctx = resolve_token(db, body.qr_token, touch=True)
    except QrTableUnavailable:
        db.commit()
        logger.info(
            "qr_resolve_unavailable prefix=%s", token_prefix(body.qr_token or "")
        )
        raise HTTPException(status_code=409, detail=messages.QR_UNAVAILABLE)

    if ctx is None:
        logger.info(
            "qr_resolve_invalid prefix=%s", token_prefix(body.qr_token or "")
        )
        raise HTTPException(status_code=404, detail=messages.QR_INVALID)

    db.commit()  # persist last_used_at
    logger.info(
        "qr_resolve_ok prefix=%s store_id=%s table_id=%s",
        token_prefix(body.qr_token or ""),
        ctx.store_id,
        ctx.table_id,
    )
    return QrContextResponse(
        store=QrStore(id=ctx.store_id, name=ctx.store_name),
        table=QrTable(id=ctx.table_id, name=ctx.table_name),
        context_version=ctx.context_version,
    )
