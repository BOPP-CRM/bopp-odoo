def migrate(cr, version):
    cr.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'partner' AND column_name = 'api_monthly_limit'
        """
    )
    if not cr.fetchone():
        cr.execute(
            """
            ALTER TABLE partner
            ADD COLUMN api_monthly_limit INTEGER DEFAULT 0
            """
        )
