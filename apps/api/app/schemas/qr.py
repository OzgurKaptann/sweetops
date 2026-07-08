from pydantic import BaseModel

from .common import BaseSchema


class QrResolveRequest(BaseModel):
    qr_token: str


class QrStore(BaseSchema):
    id: int
    name: str


class QrTable(BaseSchema):
    id: int
    name: str


class QrContextResponse(BaseSchema):
    store: QrStore
    table: QrTable
    context_version: int = 1
