"""Keep otel_tracing in Odoo's server_wide_modules list.

The module has to load at process start, not when it is installed in a
database. post_load is what wraps HTTP and registers the post-fork
tracer init. Deployments that ship their own odoo.conf still pick the
module up because the image entrypoint calls this after writing the conf.
"""

from pathlib import Path

MODULE = "otel_tracing"


def ensure_server_wide_module(path):
    """Append otel_tracing to server_wide_modules in an odoo.conf file.

    Existing modules are preserved. A missing key becomes
    ``base,web,otel_tracing``. Commented lines are left alone.
    """
    conf_path = Path(path)
    text = conf_path.read_text() if conf_path.exists() else ""
    lines = text.splitlines()
    found = False
    updated = []
    for line in lines:
        stripped = line.strip()
        if (
            stripped
            and not stripped.startswith(";")
            and not stripped.startswith("#")
            and stripped.split("=", 1)[0].strip() == "server_wide_modules"
        ):
            key, _, value = line.partition("=")
            modules = [part.strip() for part in value.split(",") if part.strip()]
            if MODULE not in modules:
                modules.append(MODULE)
            updated.append(f"{key.strip()} = {','.join(modules)}")
            found = True
        else:
            updated.append(line)
    if not found:
        if updated and updated[-1] != "":
            updated.append("")
        updated.append(f"server_wide_modules = base,web,{MODULE}")
    conf_path.write_text("\n".join(updated) + "\n")
