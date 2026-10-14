import sys

try:
    import pydevd_pycharm

    pydevd_pycharm.settrace(
        "host.docker.internal",
        port=5678,
        stdout_to_server=True,
        stderr_to_server=True,
        suspend=False,
    )
except (ConnectionRefusedError, OSError) as exc:
    print(
        f"PyDevD debugger not listening on host.docker.internal:5678 ({exc}); "
        "starting Odoo without remote debug.",
        file=sys.stderr,
    )

from odoo.cli import main

main()
