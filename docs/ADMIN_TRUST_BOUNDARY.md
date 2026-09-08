# Admin trust boundary

The optional admin profile is a privileged control plane. It can change saved systems and profiles, manage SSH and TLS material, import or export backups, and start or stop application containers through the Docker socket.

Remote binding remains supported. The default `0.0.0.0:8082` publication has no
application login. Anyone who can reach the port can use the admin controls.
Do not expose the admin profile on an unrestricted guest LAN or the public
Internet.

## Network-boundary mode

`ADMIN_AUTH_MODE=network` is the default. The application does not ask for
credentials, require a configured browser origin, or restrict write controls in
this mode. Network reachability is authorization.

RFC1918 describes private IPv4 address ranges. It does not establish trust. A
guest network, shared office LAN, compromised device, or broad VPN can use
RFC1918 addresses while still containing clients that should not control the
app. If access must be limited, use one or more of these controls:

- a firewall rule that admits only the operator subnet or specific management hosts;
- a VPN whose members are trusted administrators;
- an authenticated reverse proxy while direct access to port `8082` remains blocked;
- an equivalent private management network.

Auto-stop limits exposure time, but it is not authentication. The Docker socket
and writable configuration mounts make reachability the authorization boundary
in this mode.

## Admin browser origin

`ADMIN_PUBLIC_ORIGIN` is optional in the default network mode. Without it,
browser mutations must match the request's own scheme, host, and port. Basic
mode requires an explicit value. Set it to the exact origin the browser shows
for the admin UI, with no path. For example,
`http://jbod-admin.example.test:8082` uses the default port, while
`https://jbod-admin.example.test` could be served by a reverse proxy.

Browser-initiated admin changes are accepted only when their `Origin` or
`Referer` header matches the effective origin. The admin service refuses to
start in Basic mode until the configured value is valid. A mismatch returns
`403 Cross-origin admin mutation rejected.` Headerless CLI and automation
requests remain available.

For the main UI on port `8080`, network mode allows reads and writes without a
login. Persistent mapping and alias changes, mapping imports, enclosure or drive
LED actions, and system locator changes are available to anyone who can reach
the port.

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

The admin container's image filesystem is read-only. Compose drops every Linux
capability, adds back only `CHOWN` and `FOWNER` for ownership-preserving restore,
uses the app-data GID as its primary group, and enables `no-new-privileges`.
Its writable host mounts are limited to config, data, and history state plus the
Docker socket. These controls reduce accidental filesystem reach, but they do
not weaken the socket's root-equivalent authority.

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

Use built-in Basic authentication when the clients that can reach the published
ports are broader than the people who should control the app:

```dotenv
ADMIN_AUTH_MODE=basic
ADMIN_AUTH_USERNAME=operator
ADMIN_AUTH_PASSWORD=replace-with-a-long-random-secret
APP_PUBLIC_ORIGIN=https://storage-ui.example.local
ADMIN_PUBLIC_ORIGIN=https://storage-admin.example.local
```

The same credentials protect main-UI mutation endpoints and all admin HTML,
static assets, and APIs. Main-UI inventory, history, SMART, export, and
import-preview reads remain anonymous. Main-UI `/livez`, `/healthz`, and the
configured metrics path also remain anonymous so container health checks and
Prometheus scraping continue to work.

Each main-UI page starts signed out. Its in-page sign-in verifies the shared
Basic credentials without changing application state. The browser holds the
credentials only in page memory, sends them only to same-origin verification
and mutation routes, and clears them on reload or sign-out. Separate tabs and
the dedicated Storage Fabric page require their own sign-in.

Basic credentials are only encoded, not encrypted. Use HTTPS through a reverse proxy or a private encrypted VPN. Do not expose Basic authentication over plaintext Internet transport. Keep the password in the ignored local `.env` or another deployment secret source, never in tracked configuration or command output.

Main-UI browser mutations are accepted only when their `Origin` or `Referer`
matches `APP_PUBLIC_ORIGIN`. Admin browser mutations use the separate
`ADMIN_PUBLIC_ORIGIN` setting (see
[Admin browser origin](#admin-browser-origin)) because the services normally
publish on different ports. Requests without either header remain available to
authenticated CLI and automation clients. A reverse proxy that replaces Basic
authentication with cookies must still provide its own CSRF controls and must
prevent direct access to the underlying service ports.

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
2. Use the default network mode only when everyone who can reach the port may control containers and read or replace application state.
3. Otherwise select `basic` or place an authenticated reverse proxy in front of the service and block direct port access.
4. In Basic mode, set both public origins to the exact addresses operators will use in their browsers.
5. Keep health and metrics reachability separate from privileged route reachability where the network design permits it.
6. Leave plaintext backup export disabled unless its risk is accepted for that deployment.
