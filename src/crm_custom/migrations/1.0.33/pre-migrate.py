def migrate(cr, version):
    cr.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'res_users' AND column_name = 'api_key'
        """
    )
    if not cr.fetchone():
        cr.execute(
            """
            ALTER TABLE res_users
            ADD COLUMN api_key VARCHAR
            """
        )

    cr.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'res_users' AND column_name = 'api_key_enabled'
        """
    )
    if not cr.fetchone():
        cr.execute(
            """
            ALTER TABLE res_users
            ADD COLUMN api_key_enabled BOOLEAN DEFAULT false
            """
        )
