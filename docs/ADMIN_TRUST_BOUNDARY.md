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

## Basic authentication mode

Use built-in Basic authentication when the clients that can reach the admin port are broader than the trusted-operator population:

```dotenv
ADMIN_AUTH_MODE=basic
ADMIN_AUTH_USERNAME=operator
ADMIN_AUTH_PASSWORD=replace-with-a-long-random-secret
```

Basic authentication protects the admin HTML, static assets, and management APIs. `/livez`, `/healthz`, and the configured metrics path remain anonymous so container health checks and Prometheus scraping continue to work.

Basic credentials are only encoded, not encrypted. Use HTTPS through a reverse proxy or a private encrypted VPN. Do not expose Basic authentication over plaintext Internet transport. Keep the password in the ignored local `.env` or another deployment secret source, never in tracked configuration or command output.

Browser mutation requests are accepted only when their `Origin` or `Referer` matches the admin service origin or `ADMIN_PUBLIC_ORIGIN`. Requests without either header remain available to CLI and automation clients. A reverse proxy that replaces Basic authentication with cookies must still provide its own CSRF controls and must prevent direct access to port `8082`.

## Backup export policy

Credential-bearing backup export is encrypted by default in the admin UI, and the API rejects unsanitized plaintext backup export unless the operator explicitly enables it:

```dotenv
ADMIN_ALLOW_PLAINTEXT_BACKUP_EXPORT=true
```

That override is for a separately protected trusted-operator deployment. Debug bundles may remain unencrypted while secret scrubbing is enabled. Turning off both encryption and secret scrubbing requires the same explicit plaintext override.

## Deployment check

Before starting the admin profile:

1. Confirm who can route to the published admin port.
2. Choose `network` only when that entire population is trusted to control containers and read or replace application state.
3. Otherwise select `basic` or place an authenticated reverse proxy in front of the service and block direct port access.
4. Keep health and metrics reachability separate from privileged route reachability where the network design permits it.
5. Leave plaintext backup export disabled unless its risk is accepted for that deployment.
