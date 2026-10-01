"""0012: A2A production path — projects/products/tags, model policy,
execution events, human inbox, artifacts (STEA-004 plan 2026-10-01 §9/§17/§25/§34).

Adds:
- projects / products (+ work_items.project_id FK nullable, products.product_id on projects)
- tags / tag_links (generic additive semantic tags)
- model_policies (scope: system|agent|project|work_item)
- execution_tasks.transport_state + correlation columns (DISPATCHING/RUNNING/...)
- execution_events (live activity persistence per ADR-0014)
- human_inbox_items (durable inbox per ADR-0017)
- artifacts (DB-authoritative Markdown handoffs per ADR-0017)

Additive only. SQLite-compatible (unit tests) AND PostgreSQL-checked via the
migration/model parity + chain tests.

Revision ID: 0012_a2a_production_path
Revises: 0011_bootstrap_requests
Create Date: 2026-10-01
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_a2a_production_path"
down_revision = "0011_bootstrap_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    wi_cols = {c["name"] for c in sa.inspect(bind).get_columns("work_items")}

    # ---- projects / products ------------------------------------------------
    op.create_table(
        "products",
        sa.Column("product_id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=96), nullable=False, unique=True),
        sa.Column("business_summary", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("default_model_policy_json", sa.Text(), nullable=True),
        sa.Column("context_notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_products_slug", "products", ["slug"])

    op.create_table(
        "projects",
        sa.Column("project_id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=96), nullable=False, unique=True),
        sa.Column("product_id", sa.String(length=36),
                  sa.ForeignKey("products.product_id", ondelete="SET NULL"), nullable=True),
        sa.Column("repos", sa.Text(), nullable=True),
        sa.Column("jira_project_key", sa.String(length=32), nullable=True, index=True),
        sa.Column("default_model_policy_json", sa.Text(), nullable=True),
        sa.Column("context_notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_projects_slug", "projects", ["slug"])
    op.create_index("ix_projects_product", "projects", ["product_id"])

    if "project_id" not in wi_cols:
        with op.batch_alter_table("work_items") as batch:
            batch.add_column(sa.Column("project_id", sa.String(length=36),
                                       sa.ForeignKey("projects.project_id", ondelete="SET NULL"),
                                       nullable=True))
    op.create_index("ix_work_items_project", "work_items", ["project_id"])

    # ---- tags ----------------------------------------------------------------
    op.create_table(
        "tags",
        sa.Column("tag_id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=96), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "tag_links",
        sa.Column("link_id", sa.String(length=36), primary_key=True),
        sa.Column("tag_id", sa.String(length=36),
                  sa.ForeignKey("tags.tag_id", ondelete="CASCADE"), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),  # work_item|project|product
        sa.Column("scope_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tag_id", "scope", "scope_id", name="uq_tag_link"),
    )
    op.create_index("ix_tag_links_scope", "tag_links", ["scope", "scope_id"])

    # ---- model policy --------------------------------------------------------
    op.create_table(
        "model_policies",
        sa.Column("policy_id", sa.String(length=36), primary_key=True),
        sa.Column("scope", sa.String(length=16), nullable=False),  # system|agent|project|work_item
        sa.Column("scope_id", sa.String(length=36), nullable=True),  # NULL for system
        sa.Column("inference_policy", sa.String(length=24), nullable=False,
                  server_default="local-preferred"),  # local-only|local-preferred|any
        sa.Column("preferred_model", sa.String(length=128), nullable=True),
        sa.Column("cloud_fallback", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_by", sa.String(length=128), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("scope", "scope_id", name="uq_model_policy_scope"),
    )

    # ---- execution task transport state --------------------------------------
    et_cols = {c["name"] for c in sa.inspect(bind).get_columns("execution_tasks")}
    new_et_cols = [
        sa.Column("transport_state", sa.String(length=16), nullable=True),  # DISPATCHING|RUNNING
        sa.Column("assignment_id", sa.String(length=36), nullable=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=True, index=True),
        sa.Column("model_policy_json", sa.Text(), nullable=True),
        sa.Column("effective_model", sa.String(length=128), nullable=True),
        sa.Column("model_resolution_reason", sa.String(length=32), nullable=True),
        sa.Column("ttft_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failover_attempt", sa.Integer(), nullable=False, server_default="0"),
    ]
    with op.batch_alter_table("execution_tasks") as batch:
        for c in new_et_cols:
            if c.name not in et_cols:
                batch.add_column(c)

    # ---- execution events (live activity) ------------------------------------
    op.create_table(
        "execution_events",
        sa.Column("event_pk", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(length=36),
                  sa.ForeignKey("execution_tasks.task_id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.UniqueConstraint("task_id", "seq", name="uq_execution_event_seq"),
    )
    op.create_index("ix_exec_events_task", "execution_events", ["task_id", "seq"])

    # ---- human inbox ----------------------------------------------------------
    op.create_table(
        "human_inbox_items",
        sa.Column("item_id", sa.String(length=36), primary_key=True),
        sa.Column("item_class", sa.String(length=24), nullable=False,
                  server_default="ACTION_REQUIRED"),  # ACTION_REQUIRED|FYI|STALE
        sa.Column("severity", sa.String(length=16), nullable=False, server_default="medium"),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.String(120), nullable=True),
        sa.Column("agent_id", sa.String(length=36),
                  sa.ForeignKey("agents.agent_id", ondelete="SET NULL"), nullable=True),
        sa.Column("work_item_id", sa.String(length=36),
                  sa.ForeignKey("work_items.work_item_id", ondelete="SET NULL"), nullable=True),
        sa.Column("execution_session_id", sa.String(length=36), nullable=True),
        sa.Column("project_id", sa.String(length=36),
                  sa.ForeignKey("projects.project_id", ondelete="SET NULL"), nullable=True),
        sa.Column("product_id", sa.String(length=36),
                  sa.ForeignKey("products.product_id", ondelete="SET NULL"), nullable=True),
        sa.Column("jira_issue_key", sa.String(length=64), nullable=True),
        sa.Column("correlation_id", sa.String(length=128), nullable=True, index=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_force", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_inbox_created", "human_inbox_items", ["created_at"])
    op.create_index("ix_inbox_work_item", "human_inbox_items", ["work_item_id"])

    # ---- artifacts -------------------------------------------------------------
    op.create_table(
        "artifacts",
        sa.Column("artifact_id", sa.String(length=36), primary_key=True),
        sa.Column("work_item_id", sa.String(length=36),
                  sa.ForeignKey("work_items.work_item_id", ondelete="SET NULL"), nullable=True),
        sa.Column("execution_session_id", sa.String(length=36), nullable=True),
        sa.Column("agent_id", sa.String(length=36),
                  sa.ForeignKey("agents.agent_id", ondelete="SET NULL"), nullable=True),
        sa.Column("project_id", sa.String(length=36),
                  sa.ForeignKey("projects.project_id", ondelete="SET NULL"), nullable=True),
        sa.Column("product_id", sa.String(length=36),
                  sa.ForeignKey("products.product_id", ondelete="SET NULL"), nullable=True),
        sa.Column("jira_issue_key", sa.String(length=64), nullable=True),
        sa.Column("artifact_type", sa.String(length=32), nullable=False, server_default="handoff"),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("bluf", sa.String(length=512), nullable=True),
        sa.Column("mime_type", sa.String(length=64), nullable=False, server_default="text/markdown"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=True),
    )
    op.create_index("ix_artifacts_work_item", "artifacts", ["work_item_id"])

    # ---- system default model policy row ---------------------------------------
    # Prefer SQL DDL (portable across SQLite/PG); fall back to Core insert for
    # the boolean literal.
    seed = sa.text(
        "INSERT INTO model_policies (policy_id, scope, scope_id, inference_policy, "
        "preferred_model, cloud_fallback, updated_by, updated_at) "
        "SELECT 'sys-model-policy-default', 'system', NULL, 'local-preferred', "
        "'qwen3.8-flash-next', :cloud_ok, 'adr-0018-seed', CURRENT_TIMESTAMP "
        "WHERE NOT EXISTS (SELECT 1 FROM model_policies WHERE scope = 'system')"
    )
    bind.execute(seed, {"cloud_ok": True})


def downgrade() -> None:
    # drop_table removes dependent indexes in PG; drop indexes BEFORE tables
    # only where they live on surviving tables.
    op.drop_table("artifacts")
    op.drop_index("ix_inbox_work_item", table_name="human_inbox_items")
    op.drop_index("ix_inbox_created", table_name="human_inbox_items")
    op.drop_table("human_inbox_items")
    op.drop_table("execution_events")
    et_cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("execution_tasks")}
    with op.batch_alter_table("execution_tasks") as batch:
        for name in ("transport_state", "assignment_id", "idempotency_key",
                     "model_policy_json", "effective_model", "model_resolution_reason",
                     "ttft_deadline_at", "failover_attempt"):
            if name in et_cols:
                batch.drop_column(name)
    op.drop_table("model_policies")
    op.drop_index("ix_tag_links_scope", table_name="tag_links")
    op.drop_table("tag_links")
    op.drop_table("tags")
    op.drop_index("ix_work_items_project", table_name="work_items")
    wi_cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("work_items")}
    if "project_id" in wi_cols:
        with op.batch_alter_table("work_items") as batch:
            batch.drop_column("project_id")
    op.drop_table("projects")
    op.drop_table("products")