def migrate(cr, version):
    cr.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'partner' AND column_name = 'ui_warranty_enabled'
        """
    )
    if not cr.fetchone():
        cr.execute(
            """
            ALTER TABLE partner
            ADD COLUMN ui_warranty_enabled BOOLEAN DEFAULT FALSE
            """
        )
