from sqlalchemy import (
    Column,
    Integer,
    String,
    ForeignKey,
    DateTime,
    CheckConstraint,
    Index,
    text,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .base import Base


# Token lifecycle statuses. Kept as plain strings (matching the rest of the
# codebase, e.g. Order.status) rather than a DB enum so migrations stay simple.
# A CHECK constraint (see __table_args__) enforces this closed set at the DB
# level so application bugs can never persist an unknown status.
QR_TOKEN_STATUS_ACTIVE = "ACTIVE"
QR_TOKEN_STATUS_REVOKED = "REVOKED"

# Named DB objects — referenced from the Alembic migration too, so keep in sync.
STATUS_CHECK_NAME = "ck_table_qr_tokens_status"
ONE_ACTIVE_INDEX_NAME = "uq_table_qr_tokens_one_active_per_table"


class TableQrToken(Base):
    """
    A revocable, rotatable QR token bound to one physical table.

    Security model:
      * The raw token is NEVER stored. Only its SHA-256 hash (`token_hash`) is
        persisted, so a database leak cannot reveal usable tokens.
      * `token_prefix` is a short, non-secret fragment used only for operational
        support (identifying which physical sticker a record refers to).
      * More than one row may exist per table over time: rotation revokes the
        previous ACTIVE row and inserts a new one, preserving history.

    Cascading behavior (documented, deliberate):
      * `table_id` FK uses ON DELETE CASCADE. Tables are effectively permanent
        in this application, but if a table were ever physically removed its QR
        tokens become meaningless and are removed with it. Historical *token*
        lineage for a living table is preserved by keeping REVOKED rows, not by
        retaining tokens for deleted tables. Order/analytics lineage does not
        depend on this table — it uses the server-derived order.store_id /
        order.table_id columns.
      * `replaced_by_id` self-FK uses ON DELETE SET NULL so deleting a newer
        token never destroys the older lineage row.

    Database-enforced invariants (see __table_args__ and the migration):
      * `status` may only be 'ACTIVE' or 'REVOKED' (CHECK constraint). App-level
        validation is not trusted alone.
      * At most one ACTIVE token per table (partial unique index on `table_id`
        WHERE status = 'ACTIVE'). Multiple REVOKED historical rows per table are
        allowed. This makes the "one current trusted sticker per table" rule a
        hard database guarantee, so a race or a repeated `issue` can never leave
        two simultaneously-valid stickers on one physical table.
    """

    __tablename__ = "table_qr_tokens"

    __table_args__ = (
        CheckConstraint(
            f"status IN ('{QR_TOKEN_STATUS_ACTIVE}', '{QR_TOKEN_STATUS_REVOKED}')",
            name=STATUS_CHECK_NAME,
        ),
        # Partial unique index: only ACTIVE rows participate, so a table may keep
        # any number of REVOKED history rows but only ever one ACTIVE token.
        Index(
            ONE_ACTIVE_INDEX_NAME,
            "table_id",
            unique=True,
            postgresql_where=text(f"status = '{QR_TOKEN_STATUS_ACTIVE}'"),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    table_id = Column(
        Integer,
        ForeignKey("tables.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # SHA-256 hex digest of the raw token — 64 chars. Unique + indexed so
    # resolution is a single indexed lookup and duplicate hashes are impossible.
    token_hash = Column(String(64), unique=True, nullable=False, index=True)

    # Non-secret leading fragment of the raw token, for staff support / listing.
    token_prefix = Column(String(16), nullable=False, index=True)

    status = Column(
        String,
        nullable=False,
        default=QR_TOKEN_STATUS_ACTIVE,
        server_default=QR_TOKEN_STATUS_ACTIVE,
        index=True,
    )

    created_reason = Column(String, nullable=True)

    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    # Rotation lineage: points at the token that superseded this one.
    replaced_by_id = Column(
        Integer,
        ForeignKey("table_qr_tokens.id", ondelete="SET NULL"),
        nullable=True,
    )

    table = relationship("Table", back_populates="qr_tokens")
    replaced_by = relationship(
        "TableQrToken",
        remote_side=[id],
        foreign_keys=[replaced_by_id],
        uselist=False,
    )

    def __repr__(self) -> str:  # pragma: no cover — debug aid only
        return (
            f"<TableQrToken id={self.id} table_id={self.table_id} "
            f"prefix={self.token_prefix} status={self.status}>"
        )
