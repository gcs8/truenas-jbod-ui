# Admin trust boundary

The optional admin profile is a privileged control plane. It can change saved systems and profiles, manage SSH and TLS material, import or export backups, and start or stop application containers through the Docker socket.

Remote binding remains supported. The default `0.0.0.0:8082` publication assumes every client that can reach the port is a trusted operator. The admin profile is not safe on an unrestricted guest LAN or the public Internet.

## Network-boundary mode

`ADMIN_AUTH_MODE=network` is the compatibility default. The application does not ask for credentials in this mode. Deployment must limit reachability to trusted operators with at least one of these controls:

- a firewall rule that admits only the operator subnet or specific management hosts;
- a VPN whose members are trusted administrators;
- an authenticated reverse proxy while direct access to port `8082` remains blocked;
- an equivalent private management network.

Auto-stop limits exposure time, but it is not authentication. The Docker socket and writable configuration mounts make reachability the authorization boundary in this mode.

For the main UI on port `8080`, network mode is read-only. Inventory, history,
SMART, export, and import-preview requests remain available, including the
read-only POST routes they use. Persistent mapping and alias changes, confirmed
mapping imports, enclosure or drive LED actions, and system locator changes
return `403` until Basic authentication is enabled.

## Docker socket authority

The runtime client deliberately uses only these Docker API operations for the
configured application container names:

- `GET /containers/json?all=1`
- `POST /containers/{name}/start`
- `POST /containers/{name}/stop`
- `POST /containers/{name}/restart`

That narrow client behavior does not make a raw Docker socket narrow. A process
that can send arbitrary requests to the mounted socket has root-equivalent
authority over the Docker host. The Compose files therefore mount the socket
only into the explicitly started, explicitly root `enclosure-admin` service;
UI, history, and one-shot backup services receive no socket.

A generic Docker socket proxy was evaluated but is not enabled by default.
Method/category switches broad enough to permit container start, stop, and
restart also expose operations beyond this client's four routes. Such a proxy
does not enforce the configured container-name allowlist and would provide
misleading isolation. A custom allowlisting proxy would need to validate the
HTTP method, exact path, configured container name, and query parameters before
forwarding each request. Until that separately reviewed boundary exists, treat
admin compromise as Docker-host compromise and rely on explicit profile start,
auto-stop, authentication/network restrictions, and the absence of the socket
from every other service.

## Basic authentication mode

Use built-in Basic authentication to enable authenticated main-UI mutations or
when the clients that can reach the admin port are broader than the
trusted-operator population:

```dotenv
ADMIN_AUTH_MODE=basic
ADMIN_AUTH_USERNAME=operator
ADMIN_AUTH_PASSWORD=replace-with-a-long-random-secret
APP_PUBLIC_ORIGIN=https://storage-ui.example.local
```

The same credentials protect main-UI mutation endpoints and all admin HTML,
static assets, and APIs. Main-UI inventory, history, SMART, export, and
import-preview reads remain anonymous. Main-UI `/livez`, `/healthz`, and the
configured metrics path also remain anonymous so container health checks and
Prometheus scraping continue to work.

Basic credentials are only encoded, not encrypted. Use HTTPS through a reverse proxy or a private encrypted VPN. Do not expose Basic authentication over plaintext Internet transport. Keep the password in the ignored local `.env` or another deployment secret source, never in tracked configuration or command output.

Main-UI browser mutations are accepted only when their `Origin` or `Referer`
matches `APP_PUBLIC_ORIGIN`. Admin browser mutations use the separate
`ADMIN_PUBLIC_ORIGIN` setting because the services normally publish on different
ports. Requests without either header remain available to authenticated CLI and
automation clients. A reverse proxy that replaces Basic authentication with
cookies must still provide its own CSRF controls and must prevent direct access
to the underlying service ports.

## Backup export policy

Credential-bearing backup export is encrypted by default in the admin UI, and the API rejects unsanitized plaintext backup export unless the operator explicitly enables it:

```dotenv
ADMIN_ALLOW_PLAINTEXT_BACKUP_EXPORT=true
```

That override is for a separately protected trusted-operator deployment. Debug bundles may remain unencrypted while secret scrubbing is enabled. Turning off both encryption and secret scrubbing requires the same explicit plaintext override.

Scheduled state backups have no plaintext mode. They read a private regular
passphrase file at execution time and use an in-process authenticated encryption
envelope, so the passphrase is never placed in generated unit files, subprocess
arguments, logs, status payloads, or metrics. The schedule is disabled by
default and refuses to run unless the operator explicitly configures its
destination, status file, retention count, included groups, and passphrase-file
reference. A host timer invokes a separate one-shot container with no network,
published port, or Docker socket. The admin sidecar retains its default
`ADMIN_AUTO_STOP_SECONDS=3600` boundary.

## Deployment check

Before starting the admin profile:

1. Confirm who can route to the published admin port.
2. Choose `network` only when that entire population is trusted to control containers and read or replace application state.
3. Otherwise select `basic` or place an authenticated reverse proxy in front of the service and block direct port access.
4. Keep health and metrics reachability separate from privileged route reachability where the network design permits it.
5. Leave plaintext backup export disabled unless its risk is accepted for that deployment.
