"""
Creates the complete Item Master App schema in one pass — every table,
primary key, foreign key, index, and the two required seed rows
(dept_mapping_config, dept_push_approvals). Supersedes the 31 incremental
"add_*.py" migration scripts that built this schema up piece by piece
during development; this file reflects their combined end state, generated
by introspecting the live database's actual schema (not hand-merged from
the old scripts) so it's guaranteed to match exactly what the app expects.

Every statement is guarded (IF NOT EXISTS), so this is safe to run against
an empty database, an already-current one, or (if a future schema change
adds a 002_*.py migration) one that's only partially caught up.
"""

import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_engine

STATEMENTS = [
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'change_discard_notices')
BEGIN
    CREATE TABLE dbo.change_discard_notices (
        id                     INT IDENTITY(1,1) NOT NULL,
        entity_type            VARCHAR(20) NOT NULL,
        entity_label           NVARCHAR(400) NOT NULL,
        originally_staged_by   NVARCHAR(200),
        reason                 NVARCHAR(400) NOT NULL,
        triggered_by           NVARCHAR(200),
        triggered_at           DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        dismissed              BIT NOT NULL DEFAULT 0,
        CONSTRAINT PK_change_discard_notices PRIMARY KEY (id)
    );
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'deleted_upcs')
BEGIN
    CREATE TABLE dbo.deleted_upcs (
        upc                    VARCHAR(12) NOT NULL,
        deleted_by             VARCHAR(100),
        deleted_at             DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        description            NVARCHAR(500),
        department             NVARCHAR(200),
        category               NVARCHAR(200),
        subcategory            NVARCHAR(200),
        brand                  NVARCHAR(200),
        pack                   NVARCHAR(100),
        size                   NVARCHAR(100),
        uom                    NVARCHAR(50),
        CONSTRAINT PK_deleted_upcs PRIMARY KEY (upc)
    );
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'dept_mapping_broken_out_claims')
BEGIN
    CREATE TABLE dbo.dept_mapping_broken_out_claims (
        combo_id               INT NOT NULL,
        claimed_by             NVARCHAR(200) NOT NULL,
        claimed_at             DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        last_activity_at       DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_dept_mapping_broken_out_claims PRIMARY KEY (combo_id)
    );
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'dept_mapping_combo_agreements')
BEGIN
    CREATE TABLE dbo.dept_mapping_combo_agreements (
        combo_id               INT NOT NULL,
        department             NVARCHAR(200) NOT NULL,
        agreed_by              NVARCHAR(200) NOT NULL,
        agreed_at              DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_dept_mapping_combo_agreements PRIMARY KEY (agreed_by, combo_id, department)
    );
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'dept_mapping_combo_suggestions')
BEGIN
    CREATE TABLE dbo.dept_mapping_combo_suggestions (
        combo_id               INT NOT NULL,
        staged_by              NVARCHAR(200) NOT NULL,
        department             NVARCHAR(200) NOT NULL,
        tier                   VARCHAR(20),
        source_key             VARCHAR(50),
        label                  NVARCHAR(400),
        n_upcs_total           INT,
        suggested_at           DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_dept_mapping_combo_suggestions PRIMARY KEY (combo_id, staged_by)
    );
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'dept_mapping_combo_upcs')
BEGIN
    CREATE TABLE dbo.dept_mapping_combo_upcs (
        combo_id               BIGINT NOT NULL,
        upc                    VARCHAR(12) NOT NULL,
        is_evidence            BIT NOT NULL DEFAULT 0,
        p1_department          NVARCHAR(200),
        CONSTRAINT PK_dept_mapping_combo_upcs PRIMARY KEY (combo_id, upc)
    );
    CREATE INDEX IX_dept_mapping_combo_upcs_upc ON dbo.dept_mapping_combo_upcs(upc);
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'dept_mapping_combo_upcs_snapshot')
BEGIN
    CREATE TABLE dbo.dept_mapping_combo_upcs_snapshot (
        snapshot_id            INT NOT NULL,
        combo_id               BIGINT NOT NULL,
        upc                    VARCHAR(20) NOT NULL,
        is_evidence            BIT,
        p1_department          NVARCHAR(200)
    );
    CREATE INDEX IX_combo_upcs_snapshot_snapshot_id ON dbo.dept_mapping_combo_upcs_snapshot(snapshot_id);
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'dept_mapping_combos')
BEGIN
    CREATE TABLE dbo.dept_mapping_combos (
        combo_id               BIGINT IDENTITY(1,1) NOT NULL,
        source_key             VARCHAR(50) NOT NULL,
        raw_department         NVARCHAR(200) NOT NULL,
        raw_category           NVARCHAR(200) NOT NULL,
        raw_subcategory        NVARCHAR(200) NOT NULL,
        n_upcs_total           INT NOT NULL DEFAULT 0,
        n_evidence             INT NOT NULL DEFAULT 0,
        purity                 FLOAT,
        majority_department    NVARCHAR(200),
        suggested_department   NVARCHAR(200),
        tier                   VARCHAR(20) NOT NULL DEFAULT 'unmatched',
        resolved_via           NVARCHAR(400),
        chain_round            TINYINT,
        is_strict              BIT NOT NULL DEFAULT 0,
        manual_department      NVARCHAR(200),
        approved               BIT NOT NULL DEFAULT 0,
        rejected               BIT NOT NULL DEFAULT 0,
        decision_state         VARCHAR(30) NOT NULL DEFAULT 'not_reviewed',
        decided_department     NVARCHAR(200),
        decided_via            VARCHAR(30),
        is_new_this_run        BIT NOT NULL DEFAULT 1,
        is_stale               BIT NOT NULL DEFAULT 0,
        first_seen_at          DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        last_computed_at       DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        last_decided_at        DATETIME2,
        last_decided_by        VARCHAR(100),
        runner_up_department   NVARCHAR(200),
        runner_up_share        FLOAT,
        pushed_by              NVARCHAR(200),
        pushed_at              DATETIME2,
        CONSTRAINT PK_dept_mapping_combos PRIMARY KEY (combo_id)
    );
    CREATE INDEX IX_dept_mapping_combos_source ON dbo.dept_mapping_combos(source_key);
    CREATE INDEX IX_dept_mapping_combos_tier ON dbo.dept_mapping_combos(tier,decision_state);
    CREATE UNIQUE INDEX UQ_dept_mapping_combo ON dbo.dept_mapping_combos(source_key,raw_department,raw_category,raw_subcategory);
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'dept_mapping_combos_snapshot')
BEGIN
    CREATE TABLE dbo.dept_mapping_combos_snapshot (
        snapshot_id            INT NOT NULL,
        combo_id               BIGINT NOT NULL,
        source_key             VARCHAR(50),
        raw_department         NVARCHAR(200),
        raw_category           NVARCHAR(200),
        raw_subcategory        NVARCHAR(200),
        tier                   VARCHAR(20),
        purity                 FLOAT,
        n_evidence             INT,
        resolved_via           NVARCHAR(400),
        decision_state         VARCHAR(30),
        decided_department     NVARCHAR(200),
        decided_via            VARCHAR(30),
        majority_department    NVARCHAR(200),
        chain_round            INT,
        is_strict              BIT,
        manual_department      NVARCHAR(200),
        approved               BIT,
        rejected               BIT,
        is_new_this_run        BIT,
        is_stale               BIT,
        first_seen_at          DATETIME2,
        last_computed_at       DATETIME2,
        last_decided_at        DATETIME2,
        last_decided_by        VARCHAR(100),
        runner_up_department   NVARCHAR(200),
        runner_up_share        FLOAT,
        n_upcs_total           INT,
        suggested_department   NVARCHAR(200),
        CONSTRAINT PK_dept_mapping_combos_snapshot PRIMARY KEY (combo_id, snapshot_id)
    );
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'dept_mapping_config')
BEGIN
    CREATE TABLE dbo.dept_mapping_config (
        config_id              INT NOT NULL DEFAULT 1,
        min_purity             FLOAT NOT NULL DEFAULT 0.90,
        min_sample             INT NOT NULL DEFAULT 5,
        max_chain_rounds       TINYINT NOT NULL DEFAULT 3,
        cat_match2_min_siblings INT NOT NULL DEFAULT 2,
        brand_combo_min_sample INT NOT NULL DEFAULT 5,
        brand_combo_min_purity FLOAT NOT NULL DEFAULT 0.90,
        brand_item_min_sample  INT NOT NULL DEFAULT 1,
        brand_item_min_purity  FLOAT NOT NULL DEFAULT 0.75,
        brand_category_consensus_min_sample INT NOT NULL DEFAULT 15,
        brand_keyword_conflict_max_fraction FLOAT NOT NULL DEFAULT 0.20,
        CONSTRAINT PK_dept_mapping_config PRIMARY KEY (config_id),
        CONSTRAINT CK_dept_mapping_config_single_row CHECK (config_id = 1)
    );
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'dept_mapping_departments')
BEGIN
    CREATE TABLE dbo.dept_mapping_departments (
        department             NVARCHAR(200) NOT NULL,
        source_type            VARCHAR(20) NOT NULL,
        added_at               DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_dept_mapping_departments PRIMARY KEY (department)
    );
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'dept_mapping_keyword_rules')
BEGIN
    CREATE TABLE dbo.dept_mapping_keyword_rules (
        rule_id                INT IDENTITY(1,1) NOT NULL,
        keyword                NVARCHAR(200) NOT NULL,
        match_field            VARCHAR(20) NOT NULL,
        department             NVARCHAR(200) NOT NULL,
        CONSTRAINT PK_dept_mapping_keyword_rules PRIMARY KEY (rule_id)
    );
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'dept_mapping_pending_changes')
BEGIN
    CREATE TABLE dbo.dept_mapping_pending_changes (
        combo_id               BIGINT NOT NULL,
        tier                   VARCHAR(20),
        department             NVARCHAR(200) NOT NULL,
        source_key             VARCHAR(50) NOT NULL,
        label                  NVARCHAR(600) NOT NULL,
        n_upcs_total           INT NOT NULL,
        staged_by              VARCHAR(100) NOT NULL,
        staged_at              DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        is_saved               BIT NOT NULL DEFAULT 0,
        agreed_by              NVARCHAR(200),
        agreed_at              DATETIME2,
        overridden_by          NVARCHAR(200),
        overridden_at          DATETIME2,
        CONSTRAINT PK_dept_mapping_pending_changes PRIMARY KEY (combo_id)
    );
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'dept_mapping_pending_changes_snapshot')
BEGIN
    CREATE TABLE dbo.dept_mapping_pending_changes_snapshot (
        snapshot_id            INT NOT NULL,
        combo_id               BIGINT NOT NULL,
        tier                   VARCHAR(20),
        department             NVARCHAR(200),
        source_key             VARCHAR(50),
        label                  NVARCHAR(600),
        n_upcs_total           INT,
        staged_by              VARCHAR(100),
        staged_at              DATETIME2,
        is_saved               BIT
    );
    CREATE INDEX IX_dept_pending_snapshot_snapshot_id ON dbo.dept_mapping_pending_changes_snapshot(snapshot_id);
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'dept_mapping_pending_upc_changes')
BEGIN
    CREATE TABLE dbo.dept_mapping_pending_upc_changes (
        upc                    VARCHAR(20) NOT NULL,
        combo_id               BIGINT NOT NULL,
        department             NVARCHAR(200) NOT NULL,
        label                  NVARCHAR(600) NOT NULL,
        description            NVARCHAR(400),
        source_key             VARCHAR(50) NOT NULL,
        staged_by              VARCHAR(100) NOT NULL,
        staged_at              DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        is_saved               BIT NOT NULL DEFAULT 0,
        agreed_by              NVARCHAR(200),
        agreed_at              DATETIME2,
        overridden_by          NVARCHAR(200),
        overridden_at          DATETIME2,
        revised_by             NVARCHAR(200),
        revised_at             DATETIME2,
        CONSTRAINT PK_dept_mapping_pending_upc_changes PRIMARY KEY (staged_by, upc)
    );
    CREATE INDEX IX_dept_mapping_pending_upc_changes_combo_id ON dbo.dept_mapping_pending_upc_changes(combo_id);
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'dept_mapping_pending_upc_changes_snapshot')
BEGIN
    CREATE TABLE dbo.dept_mapping_pending_upc_changes_snapshot (
        snapshot_id            INT NOT NULL,
        upc                    VARCHAR(20) NOT NULL,
        combo_id               BIGINT,
        department             NVARCHAR(200),
        label                  NVARCHAR(600),
        description            NVARCHAR(400),
        source_key             VARCHAR(50),
        staged_by              VARCHAR(100),
        staged_at              DATETIME2,
        is_saved               BIT
    );
    CREATE INDEX IX_dept_upc_pending_snapshot_snapshot_id ON dbo.dept_mapping_pending_upc_changes_snapshot(snapshot_id);
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'dept_mapping_recent_moves')
BEGIN
    CREATE TABLE dbo.dept_mapping_recent_moves (
        move_id                BIGINT IDENTITY(1,1) NOT NULL,
        combo_id               BIGINT NOT NULL,
        source_key             VARCHAR(50) NOT NULL,
        label                  NVARCHAR(600) NOT NULL,
        n_upcs_total           INT NOT NULL,
        description            NVARCHAR(200) NOT NULL,
        snapshot_json          NVARCHAR(MAX) NOT NULL,
        created_by             VARCHAR(100) NOT NULL,
        created_at             DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_dept_mapping_recent_moves PRIMARY KEY (move_id)
    );
    CREATE INDEX IX_dept_mapping_recent_moves_combo_id ON dbo.dept_mapping_recent_moves(combo_id);
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'dept_mapping_snapshots')
BEGIN
    CREATE TABLE dbo.dept_mapping_snapshots (
        snapshot_id            INT IDENTITY(1,1) NOT NULL,
        snapshot_month         CHAR(7) NOT NULL,
        taken_at               DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        item_count             INT NOT NULL,
        combo_count            INT NOT NULL,
        label                  NVARCHAR(200),
        taken_by               VARCHAR(100),
        CONSTRAINT PK_dept_mapping_snapshots PRIMARY KEY (snapshot_id)
    );
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'dept_mapping_strict_departments')
BEGIN
    CREATE TABLE dbo.dept_mapping_strict_departments (
        strict_id              INT IDENTITY(1,1) NOT NULL,
        source_key             VARCHAR(50) NOT NULL,
        old_department         NVARCHAR(200) NOT NULL,
        trust_direct_evidence  BIT NOT NULL DEFAULT 0,
        updated_by             VARCHAR(100),
        updated_at             DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_dept_mapping_strict_departments PRIMARY KEY (strict_id)
    );
    CREATE UNIQUE INDEX UQ_dept_mapping_strict_department ON dbo.dept_mapping_strict_departments(source_key,old_department);
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'dept_mapping_undo_requests')
BEGIN
    CREATE TABLE dbo.dept_mapping_undo_requests (
        entity_type            VARCHAR(10) NOT NULL,
        entity_id              NVARCHAR(50) NOT NULL,
        requested_by           NVARCHAR(200) NOT NULL,
        requested_at           DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_dept_mapping_undo_requests PRIMARY KEY (entity_id, entity_type, requested_by)
    );
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'dept_mapping_unmatched_defaults')
BEGIN
    CREATE TABLE dbo.dept_mapping_unmatched_defaults (
        default_id             INT IDENTITY(1,1) NOT NULL,
        source_key             VARCHAR(50),
        old_department         NVARCHAR(200) NOT NULL,
        new_department         NVARCHAR(200) NOT NULL,
        updated_by             VARCHAR(100),
        updated_at             DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_dept_mapping_unmatched_defaults PRIMARY KEY (default_id)
    );
    CREATE UNIQUE INDEX UQ_dept_mapping_unmatched_default ON dbo.dept_mapping_unmatched_defaults(source_key,old_department);
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'dept_mapping_upc_agreements')
BEGIN
    CREATE TABLE dbo.dept_mapping_upc_agreements (
        upc                    VARCHAR(20) NOT NULL,
        department             NVARCHAR(200) NOT NULL,
        agreed_by              NVARCHAR(200) NOT NULL,
        agreed_at              DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_dept_mapping_upc_agreements PRIMARY KEY (agreed_by, department, upc)
    );
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'dept_mapping_upc_change_suggestions')
BEGIN
    CREATE TABLE dbo.dept_mapping_upc_change_suggestions (
        upc                    VARCHAR(20) NOT NULL,
        suggested_by           NVARCHAR(200) NOT NULL,
        department             NVARCHAR(200) NOT NULL,
        combo_id               INT,
        label                  NVARCHAR(400),
        description            NVARCHAR(400),
        source_key             VARCHAR(50),
        suggested_at           DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_dept_mapping_upc_change_suggestions PRIMARY KEY (suggested_by, upc)
    );
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'dept_mapping_upc_overrides')
BEGIN
    CREATE TABLE dbo.dept_mapping_upc_overrides (
        upc                    VARCHAR(12) NOT NULL,
        combo_id               BIGINT NOT NULL,
        suggested_department   NVARCHAR(200),
        suggested_via          NVARCHAR(300),
        department             NVARCHAR(200),
        decided_via            NVARCHAR(300) NOT NULL DEFAULT 'not_reviewed',
        updated_by             VARCHAR(100),
        updated_at             DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        pushed_by              NVARCHAR(200),
        pushed_at              DATETIME2,
        CONSTRAINT PK_dept_mapping_upc_overrides PRIMARY KEY (upc)
    );
    CREATE INDEX IX_dept_mapping_upc_overrides_combo ON dbo.dept_mapping_upc_overrides(combo_id);
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'dept_mapping_upc_overrides_snapshot')
BEGIN
    CREATE TABLE dbo.dept_mapping_upc_overrides_snapshot (
        snapshot_id            INT NOT NULL,
        upc                    VARCHAR(12) NOT NULL,
        combo_id               BIGINT,
        department             NVARCHAR(200),
        decided_via            NVARCHAR(300),
        suggested_department   NVARCHAR(200),
        suggested_via          NVARCHAR(300),
        updated_by             VARCHAR(100),
        updated_at             DATETIME2,
        CONSTRAINT PK_dept_mapping_upc_overrides_snapshot PRIMARY KEY (snapshot_id, upc)
    );
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'dept_push_approvals')
BEGIN
    CREATE TABLE dbo.dept_push_approvals (
        id                     INT NOT NULL,
        approvals              NVARCHAR(MAX),
        CONSTRAINT PK_dept_push_approvals PRIMARY KEY (id)
    );
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'sources')
BEGIN
    CREATE TABLE dbo.sources (
        source_key             VARCHAR(50) NOT NULL,
        source_label           VARCHAR(100) NOT NULL,
        enabled                BIT NOT NULL DEFAULT 1,
        priority_rank          INT NOT NULL,
        file_keyword           VARCHAR(200) NOT NULL,
        sheet_name             VARCHAR(100),
        header_row             INT NOT NULL DEFAULT 1,
        upc_column             VARCHAR(100) NOT NULL,
        upc_suffix_column      VARCHAR(100),
        strip_trailing_digits  INT NOT NULL DEFAULT 0,
        department_column      VARCHAR(100),
        category_column        VARCHAR(100),
        subcategory_column     VARCHAR(100),
        brand_column           VARCHAR(100),
        description_column     VARCHAR(100),
        notes                  VARCHAR(500),
        created_at             DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        updated_at             DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        exclude_column         VARCHAR(100),
        exclude_values         VARCHAR(500),
        blank_brand_when_equals VARCHAR(200),
        blank_department_when_equals VARCHAR(200),
        blank_department_default VARCHAR(200),
        brand_suffix_match     VARCHAR(50),
        brand_suffix_result    VARCHAR(50),
        dedup_deprioritize_brand_value VARCHAR(200),
        strip_leading_code_fields VARCHAR(100),
        pack_column            VARCHAR(100),
        size_column            VARCHAR(100),
        uom_column             VARCHAR(100),
        size_format            VARCHAR(30) NOT NULL DEFAULT 'plain',
        uom_aliases            VARCHAR(500),
        CONSTRAINT PK_sources PRIMARY KEY (source_key)
    );
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'ingestion_log')
BEGIN
    CREATE TABLE dbo.ingestion_log (
        id                     BIGINT IDENTITY(1,1) NOT NULL,
        source_key             VARCHAR(50) NOT NULL,
        uploaded_at            DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        uploaded_by            VARCHAR(100),
        original_filename      VARCHAR(300),
        rows_parsed            INT NOT NULL,
        rows_staged            INT NOT NULL,
        dropped_invalid_upc    INT NOT NULL DEFAULT 0,
        dropped_duplicate_upc  INT NOT NULL DEFAULT 0,
        CONSTRAINT PK_ingestion_log PRIMARY KEY (id),
        CONSTRAINT FK__ingestion__sourc__6BE40491 FOREIGN KEY (source_key) REFERENCES dbo.sources(source_key)
    );
    CREATE INDEX ix_ingestion_log_source ON dbo.ingestion_log(source_key,uploaded_at);
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'ingestion_rejected_rows')
BEGIN
    CREATE TABLE dbo.ingestion_rejected_rows (
        id                     BIGINT IDENTITY(1,1) NOT NULL,
        log_id                 BIGINT NOT NULL,
        source_key             VARCHAR(50) NOT NULL,
        reason                 VARCHAR(30) NOT NULL,
        raw_upc                VARCHAR(200),
        department             VARCHAR(200),
        category               VARCHAR(200),
        subcategory            VARCHAR(200),
        brand                  VARCHAR(200),
        description            VARCHAR(500),
        CONSTRAINT PK_ingestion_rejected_rows PRIMARY KEY (id),
        CONSTRAINT FK__ingestion__log_i__719CDDE7 FOREIGN KEY (log_id) REFERENCES dbo.ingestion_log(id)
    );
    CREATE INDEX ix_ingestion_rejected_log ON dbo.ingestion_rejected_rows(log_id);
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'item_master_pending_changes')
BEGIN
    CREATE TABLE dbo.item_master_pending_changes (
        upc                    VARCHAR(20) NOT NULL,
        change_type            VARCHAR(10) NOT NULL,
        description            NVARCHAR(400),
        department             NVARCHAR(200),
        category               NVARCHAR(200),
        subcategory            NVARCHAR(200),
        brand                  NVARCHAR(200),
        is_saved               BIT NOT NULL DEFAULT 0,
        staged_by              VARCHAR(100) NOT NULL,
        staged_at              DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        source_key             VARCHAR(50),
        pack                   NVARCHAR(100),
        size                   NVARCHAR(100),
        uom                    NVARCHAR(50),
        CONSTRAINT PK_item_master_pending_changes PRIMARY KEY (upc)
    );
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'item_master_pending_changes_snapshot')
BEGIN
    CREATE TABLE dbo.item_master_pending_changes_snapshot (
        snapshot_id            INT NOT NULL,
        upc                    VARCHAR(20) NOT NULL,
        change_type            VARCHAR(10) NOT NULL,
        description            NVARCHAR(400),
        department             NVARCHAR(200),
        category               NVARCHAR(200),
        subcategory            NVARCHAR(200),
        brand                  NVARCHAR(200),
        is_saved               BIT,
        staged_by              VARCHAR(100),
        staged_at              DATETIME2,
        source_key             VARCHAR(50),
        pack                   NVARCHAR(100),
        size                   NVARCHAR(100),
        uom                    NVARCHAR(50)
    );
    CREATE INDEX IX_item_master_pending_snapshot_snapshot_id ON dbo.item_master_pending_changes_snapshot(snapshot_id);
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'items')
BEGIN
    CREATE TABLE dbo.items (
        upc                    VARCHAR(12) NOT NULL,
        description            NVARCHAR(500) NOT NULL,
        department             NVARCHAR(200),
        category               NVARCHAR(200),
        subcategory            NVARCHAR(200),
        brand                  NVARCHAR(200),
        source_key             VARCHAR(50),
        created_at             DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        updated_at             DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        pack                   NVARCHAR(100),
        size                   NVARCHAR(100),
        uom                    NVARCHAR(50),
        CONSTRAINT PK_items PRIMARY KEY (upc)
    );
    CREATE INDEX ix_items_brand ON dbo.items(brand);
    CREATE INDEX ix_items_department ON dbo.items(department);
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'items_snapshot')
BEGIN
    CREATE TABLE dbo.items_snapshot (
        snapshot_id            INT NOT NULL,
        upc                    VARCHAR(12) NOT NULL,
        description            NVARCHAR(500),
        department             NVARCHAR(200),
        category               NVARCHAR(200),
        subcategory            NVARCHAR(200),
        brand                  NVARCHAR(200),
        source_key             VARCHAR(50),
        pack                   NVARCHAR(100),
        size                   NVARCHAR(100),
        uom                    NVARCHAR(50),
        CONSTRAINT PK_items_snapshot PRIMARY KEY (snapshot_id, upc)
    );
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'items_staged')
BEGIN
    CREATE TABLE dbo.items_staged (
        upc                    VARCHAR(12) NOT NULL,
        description            NVARCHAR(500),
        department             NVARCHAR(200),
        category               NVARCHAR(200),
        subcategory            NVARCHAR(200),
        brand                  NVARCHAR(200),
        source_key             VARCHAR(50),
        pack                   NVARCHAR(100),
        size                   NVARCHAR(100),
        uom                    NVARCHAR(50),
        CONSTRAINT PK_items_staged PRIMARY KEY (upc)
    );
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'manual_overrides')
BEGIN
    CREATE TABLE dbo.manual_overrides (
        upc                    VARCHAR(12) NOT NULL,
        description            NVARCHAR(500),
        department             NVARCHAR(200),
        category               NVARCHAR(200),
        subcategory            NVARCHAR(200),
        brand                  NVARCHAR(200),
        updated_by             VARCHAR(100),
        updated_at             DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        pack                   NVARCHAR(100),
        size                   NVARCHAR(100),
        uom                    NVARCHAR(50),
        CONSTRAINT PK_manual_overrides PRIMARY KEY (upc)
    );
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'merge_compute_meta')
BEGIN
    CREATE TABLE dbo.merge_compute_meta (
        computed_at            DATETIME2 NOT NULL,
        computed_by            VARCHAR(100),
        item_count             INT NOT NULL,
        overrides_applied      INT NOT NULL,
        deleted_excluded       INT NOT NULL,
        added_count            INT,
        changed_count          INT,
        removed_count          INT,
        changed_by_field       NVARCHAR(MAX),
        changed_by_source      NVARCHAR(MAX),
        approvals              NVARCHAR(MAX)
    );
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'merge_log')
BEGIN
    CREATE TABLE dbo.merge_log (
        id                     INT IDENTITY(1,1) NOT NULL,
        merged_at              DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        merged_by              VARCHAR(100),
        upc_count              INT,
        added_count            INT,
        changed_count          INT,
        removed_count          INT,
        overrides_applied      INT,
        deleted_excluded       INT,
        source_breakdown       NVARCHAR(MAX),
        changed_by_field       NVARCHAR(MAX),
        changed_by_source      NVARCHAR(MAX),
        CONSTRAINT PK_merge_log PRIMARY KEY (id)
    );
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'raw_items')
BEGIN
    CREATE TABLE dbo.raw_items (
        upc                    VARCHAR(12) NOT NULL,
        source_key             VARCHAR(50) NOT NULL,
        department             VARCHAR(200),
        category               VARCHAR(200),
        subcategory            VARCHAR(200),
        brand                  VARCHAR(200),
        description            VARCHAR(500),
        loaded_at              DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        pack                   VARCHAR(100),
        size                   VARCHAR(100),
        uom                    VARCHAR(50),
        CONSTRAINT PK_raw_items PRIMARY KEY (source_key, upc),
        CONSTRAINT FK__raw_items__sourc__6442E2C9 FOREIGN KEY (source_key) REFERENCES dbo.sources(source_key)
    );
    CREATE INDEX ix_raw_items_source ON dbo.raw_items(source_key);
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'source_pending_changes')
BEGIN
    CREATE TABLE dbo.source_pending_changes (
        source_key             VARCHAR(50) NOT NULL,
        change_type            VARCHAR(10) NOT NULL,
        apply_now              BIT NOT NULL DEFAULT 0,
        source_label           VARCHAR(100),
        enabled                BIT,
        priority_rank          INT,
        file_keyword           VARCHAR(200),
        sheet_name             VARCHAR(100),
        header_row             INT,
        upc_column             VARCHAR(100),
        upc_suffix_column      VARCHAR(100),
        strip_trailing_digits  INT,
        department_column      VARCHAR(100),
        category_column        VARCHAR(100),
        subcategory_column     VARCHAR(100),
        brand_column           VARCHAR(100),
        description_column     VARCHAR(100),
        pack_column            VARCHAR(100),
        size_column            VARCHAR(100),
        size_format            VARCHAR(30),
        uom_column             VARCHAR(100),
        exclude_column         VARCHAR(100),
        exclude_values         VARCHAR(500),
        blank_brand_when_equals VARCHAR(200),
        blank_department_when_equals VARCHAR(200),
        blank_department_default VARCHAR(200),
        brand_suffix_match     VARCHAR(50),
        brand_suffix_result    VARCHAR(50),
        dedup_deprioritize_brand_value VARCHAR(200),
        strip_leading_code_fields VARCHAR(200),
        notes                  VARCHAR(500),
        staged_by              VARCHAR(100),
        staged_at              DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        uom_aliases            VARCHAR(500),
        CONSTRAINT PK_source_pending_changes PRIMARY KEY (source_key)
    );
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'source_pending_changes_snapshot')
BEGIN
    CREATE TABLE dbo.source_pending_changes_snapshot (
        snapshot_id            INT NOT NULL,
        source_key             VARCHAR(50) NOT NULL,
        change_type            VARCHAR(10) NOT NULL,
        apply_now              BIT NOT NULL,
        source_label           VARCHAR(100),
        enabled                BIT,
        priority_rank          INT,
        file_keyword           VARCHAR(200),
        sheet_name             VARCHAR(100),
        header_row             INT,
        upc_column             VARCHAR(100),
        upc_suffix_column      VARCHAR(100),
        strip_trailing_digits  INT,
        department_column      VARCHAR(100),
        category_column        VARCHAR(100),
        subcategory_column     VARCHAR(100),
        brand_column           VARCHAR(100),
        description_column     VARCHAR(100),
        pack_column            VARCHAR(100),
        size_column            VARCHAR(100),
        size_format            VARCHAR(30),
        uom_column             VARCHAR(100),
        exclude_column         VARCHAR(100),
        exclude_values         VARCHAR(500),
        blank_brand_when_equals VARCHAR(200),
        blank_department_when_equals VARCHAR(200),
        blank_department_default VARCHAR(200),
        brand_suffix_match     VARCHAR(50),
        brand_suffix_result    VARCHAR(50),
        dedup_deprioritize_brand_value VARCHAR(200),
        strip_leading_code_fields VARCHAR(200),
        notes                  VARCHAR(500),
        staged_by              VARCHAR(100),
        staged_at              DATETIME2,
        uom_aliases            VARCHAR(500)
    );
    CREATE INDEX IX_source_pending_snapshot_snapshot_id ON dbo.source_pending_changes_snapshot(snapshot_id);
END
    """,
    """
IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'source_raw_uploads')
BEGIN
    CREATE TABLE dbo.source_raw_uploads (
        source_key             VARCHAR(50) NOT NULL,
        filename               NVARCHAR(400),
        raw_csv                NVARCHAR(MAX) NOT NULL,
        uploaded_by            VARCHAR(100),
        uploaded_at            DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_source_raw_uploads PRIMARY KEY (source_key),
        CONSTRAINT FK__source_ra__sourc__68D28DBC FOREIGN KEY (source_key) REFERENCES dbo.sources(source_key)
    );
END
    """,
    """
IF NOT EXISTS (SELECT * FROM dbo.dept_mapping_config WHERE config_id = 1)
    INSERT INTO dbo.dept_mapping_config (config_id) VALUES (1);

IF NOT EXISTS (SELECT * FROM dbo.dept_push_approvals WHERE id = 1)
    INSERT INTO dbo.dept_push_approvals (id, approvals) VALUES (1, '[]');
    """
]


def main():
    engine = get_engine()
    with engine.begin() as conn:
        for statement in STATEMENTS:
            conn.execute(text(statement))
    print("Schema is ready: all tables, indexes, and seed rows created (or already present).")


if __name__ == "__main__":
    main()
