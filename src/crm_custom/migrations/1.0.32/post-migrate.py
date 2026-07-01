def migrate(cr, version):
    cr.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'res_users' AND column_name = 'portal_role'
        """
    )
    if not cr.fetchone():
        cr.execute(
            """
            ALTER TABLE res_users
            ADD COLUMN portal_role VARCHAR
            """
        )
        cr.execute(
            """
            UPDATE res_users
            SET portal_role = 'admin'
            WHERE is_partner_portal = true
            """
        )

    cr.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'partner_portal_invite' AND column_name = 'portal_role'
        """
    )
    if not cr.fetchone():
        cr.execute(
            """
            ALTER TABLE partner_portal_invite
            ADD COLUMN portal_role VARCHAR DEFAULT 'admin'
            """
        )
        cr.execute(
            """
            UPDATE partner_portal_invite
            SET portal_role = 'admin'
            WHERE portal_role IS NULL
            """
        )
