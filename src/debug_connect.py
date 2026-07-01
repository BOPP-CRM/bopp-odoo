import pydevd_pycharm

pydevd_pycharm.settrace(
    "host.docker.internal",
    port=5678,
    stdout_to_server=True,
    stderr_to_server=True,
    suspend=False,
)

from odoo.cli import main

main()