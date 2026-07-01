from datetime import datetime, timedelta

from odoo import fields

THAILAND_OFFSET = timedelta(hours=7)


def to_thailand_time(value):
    if not value:
        return None

    if isinstance(value, datetime):
        utc_dt = value.replace(tzinfo=None) if value.tzinfo else value
    elif isinstance(value, str):
        utc_dt = fields.Datetime.from_string(value)
    else:
        utc_dt = value

    if not utc_dt:
        return None

    return utc_dt + THAILAND_OFFSET


def to_thailand_string(value):
    thailand_time = to_thailand_time(value)
    if not thailand_time:
        return False
    return fields.Datetime.to_string(thailand_time)
