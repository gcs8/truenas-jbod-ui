from __future__ import annotations

import base64
import shlex


def build_quantastor_cli_invocation(
    subcommand: str,
    *,
    server_spec: str | None = None,
) -> tuple[str, str | None]:
    args = ["/usr/bin/qs", subcommand, "--json"]
    if not server_spec:
        return shlex.join(args), None

    command = (
        "IFS= read -r qs_server_b64 || exit 64; "
        "QS_SERVER=$(printf '%s' \"$qs_server_b64\" | /usr/bin/base64 --decode) || exit 64; "
        f"export QS_SERVER; exec {shlex.join(args)}"
    )
    stdin_data = f"{base64.b64encode(server_spec.encode('utf-8')).decode('ascii')}\n"
    return command, stdin_data
