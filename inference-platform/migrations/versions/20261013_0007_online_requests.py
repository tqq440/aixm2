"""新增独立在线请求元数据表，不修改批任务或登记模型。"""

from alembic import op
import sqlalchemy as sa

revision = "20261013_0007"
down_revision = "20261013_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "online_requests",
        sa.Column("online_request_id", sa.String(32), nullable=False),
        sa.Column("caller_id", sa.String(64), nullable=False),
        sa.Column("model_id", sa.String(64), nullable=False),
        sa.Column("model_version", sa.String(64), nullable=False),
        sa.Column("instance_id", sa.String(64), nullable=True),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="accepted"),
        sa.Column("permit_state", sa.String(16), nullable=False, server_default="waiting"),
        sa.Column("accepted_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("deadline_at", sa.DateTime(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=False),
        sa.Column("first_token_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("output_characters", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("finish_reason", sa.String(64), nullable=True),
        sa.Column("error_json", sa.JSON(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cancellation_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("length(online_request_id) = 32", name="online_request_id_length"),
        sa.CheckConstraint("state IN ('accepted', 'waiting_instance', 'streaming', 'completed', 'failed', 'cancelled')", name="state_values"),
        sa.CheckConstraint("permit_state IN ('waiting', 'held', 'released', 'uncertain')", name="permit_state_values"),
        sa.CheckConstraint("output_characters >= 0", name="output_characters_nonnegative"),
        sa.PrimaryKeyConstraint("online_request_id"),
    )
    op.create_index("ix_online_requests_caller_accepted", "online_requests", ["caller_id", "accepted_at", "online_request_id"])
    op.create_index("ix_online_requests_instance_permit", "online_requests", ["instance_id", "permit_state"])
    op.create_index("ix_online_requests_state_lease", "online_requests", ["state", "lease_expires_at"])


def downgrade() -> None:
    op.drop_table("online_requests")
