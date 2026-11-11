# Deploy the FS PBX HOMER API with HTTPS

This guide adds a Let's Encrypt certificate and an IP-restricted Nginx proxy to
a dedicated Debian MON01 server. It supports FS PBX SIP traces, PCAP/TXT
downloads, and RTCP QoS. A monitoring outage still affects visibility only.

**TCP 80 serves only existing HTTP-01 challenge files** under
`/.well-known/acme-challenge/`. All other paths return 404; there is no homepage,
HOMER UI, or redirect. The challenge path stays available for automatic renewal.
Let's Encrypt does not publish stable validator IP ranges, so port 80 accepts
Internet connections; the restriction is by HTTP path, not validator source IP.

**TCP 443 reuses the shared PBX IPv4 source allowlist** from
`ALLOWED_PBX_SOURCES` in `/etc/fs-monitoring/pbx-access.env`. There is no second
IP list to select. It permits only these operations, and HOMER must also
authenticate the supplied token:

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v4/transactions/messages` | Connection test, SIP messages, correlated legs, TXT export inputs |
| POST | `/api/v4/transactions/export/pcap` | PCAP download |
| POST | `/api/v4/transactions/qos` | RTCP quality reports |

Existing hosts do not need to rename anything first. When `pbx-access.env` is
absent, the installer reads `/etc/fs-monitoring/hep-firewall.env` and accepts
`ALLOWED_HEP_SOURCES` as the legacy setting. The old firewall service/helper keep
working. Do not create a second inventory; distinct old and new files are rejected.
See [renaming an existing installation](FIREWALL.md#rename-an-existing-hep-firewall-installation)
to adopt the new names without re-entering IPs.

FS PBX creates TXT exports locally. Other HTTPS paths and methods are rejected.
Only `Auth-Token` authentication is forwarded; cookies, Bearer tokens, and query
parameters are not accepted through this proxy. This endpoint does not serve
the administrative HOMER UI.

The entry point is [ops/install-homer-api.sh](../ops/install-homer-api.sh), backed
by [ops/homer_api.py](../ops/homer_api.py). Run it from the complete repository,
not as a standalone downloaded shell file. **HOMER API-token authentication is
enabled on every installation by Compose.** HTTPS provisioning is a required
per-server setup step: the normal installer cannot infer a hostname, certificate
account, or shared PBX access configuration.

## 1. Prepare the host and DNS

Requirements:

- Debian 11, 12, or 13, systemd as PID 1, root access, and Python 3.
- An already configured monitoring stack with `.env` present and HOMER's HTTP
  port published only on `127.0.0.1` (normally port 8080).
- The shared PBX access environment file with a nonempty
  `ALLOWED_PBX_SOURCES` list. IPv4 addresses and CIDRs are reused as configured;
  unrestricted `0.0.0.0/0`, invalid entries, and ambiguous assignments are rejected.
- A dedicated Nginx installation. The helper owns `/etc/nginx/nginx.conf` and
  refuses an existing unmanaged configuration or symlink. Integrate separately
  through a reviewed change if the server already hosts other websites.
- Outbound access to Debian repositories, DNS, and Let's Encrypt HTTPS APIs.
- A hostname you control, for example `homer.example.com`.

Create a DNS **A record** pointing the hostname to MON01's public IPv4 address.
Use DNS-only mode if the provider offers an HTTP/CDN proxy: the allowlist uses
the directly connecting PBX address and does not trust forwarding headers.
Do not create an AAAA record for this setup. The installer binds one explicit
IPv4 address and creates no IPv6 listener. Remove stale AAAA records that would
send certificate validation elsewhere. Check that any CAA policy permits
Let's Encrypt.

The hostname must resolve correctly from the Internet and every PBX application
node. Behind NAT, use the locally assigned address for `--listen-address` and
forward public TCP 80/443 to it.

Keep `UI_BIND_ADDRESS=127.0.0.1`; changing it also affects Grafana, Prometheus,
Loki, Alertmanager, and Pushgateway. Keep HEP on its existing restricted 9060 path.

## 2. Configure provider and host firewalls

Apply this policy using the provider firewall and the host's existing
**persistent** firewall configuration before running the installer:

| MON01 destination | Allowed sources | Purpose |
|---|---|---|
| TCP 80 | Any IPv4 source | ACME challenge files only; other paths denied by Nginx |
| TCP 443 | The existing `ALLOWED_PBX_SOURCES` list | Deny every other source |
| UDP/TCP 9060 | Existing approved capture sources | Preserve the HEP allowlist |
| TCP 8080 and other monitoring UI/API ports | Loopback only | Never open these to provide HTTPS |
| SSH | Approved administrator/VPN sources | Preserve administrative access |

The same PBX addresses already authorized for HEP are authorized for the API.
Users' browser addresses do not belong in this list. If an application server
uses a different outbound address, it must be approved in the shared inventory
before it can use the API.

The installer configures Nginx's source restrictions; **it does not change host
or provider firewalls**. A host with INPUT DROP needs explicit permits. Nginx
runs on the host, so these rules belong in INPUT, not the HEP `DOCKER-USER`
chain. See [the firewall guide](FIREWALL.md).

For an existing iptables INPUT policy, these example immediate rules read the
approved sources from the same HEP file. Replace only MON01's documentation
address and interface, inspect rule order first, and implement the same policy
in the existing persistent firewall manager. These commands alone do not
survive a reboot:

```bash
cd /opt/monitoring
api_sources=$(sudo ./ops/install-homer-api.sh --show-sources) || exit 1
sudo iptables -I INPUT 1 -i eth0 -d 203.0.113.20/32 -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 1 -i eth0 -d 203.0.113.20/32 -p tcp --dport 443 -j DROP
for source in $api_sources; do
  sudo iptables -I INPUT 1 -i eth0 -d 203.0.113.20/32 -s "$source" -p tcp --dport 443 -j ACCEPT
done
```

Insertion puts the HTTPS source permits before the HTTPS drop, without changing
SSH, HEP, or unrelated ports. Repeating these commands adds duplicates. Do not
blindly save/restore the live Docker ruleset for persistence, or mix this example
with an independently managed nftables/UFW policy. Use the firewall system
already responsible for the host.

Audit public IPv6 separately. This installer creates no IPv6 exposure, but does
not change existing IPv6 policies or other services. IPv4 rules do not protect
IPv6 listeners.

## 3. Preview and install

Replace the example hostname and email. The installer reads the shared PBX
inventory and reuses HOMER's specific IPv4 HEP bind address from the rendered
Compose configuration (normally `HEP_BIND_ADDRESS` in `.env`). It does not execute
the inventory or `.env` as shell scripts.
If HEP binds to `0.0.0.0`, IPv6, or multiple addresses, supply
`--listen-address 203.0.113.20` with the intended local IPv4 address instead;
the installer stops rather than choosing an interface automatically.
Preview without changing the host (use sudo to read the protected HEP file):

```bash
cd /opt/monitoring
sudo ./ops/install-homer-api.sh \
  --hostname homer.example.com \
  --email admin@example.com \
  --preview
```

Install using the same command without `--preview`, as root:

```bash
cd /opt/monitoring
sudo ./ops/install-homer-api.sh \
  --hostname homer.example.com \
  --email admin@example.com
```

Use `--install-dir /your/deployment` for a custom stack directory and
`--homer-port 18080` if HOMER's loopback publication uses a nondefault port.
Use `--access-config /path/to/pbx-access.env` only if the shared PBX
inventory lives elsewhere. Missing or invalid inventories stop installation;
there is no fallback to unrestricted access.
Apply mode checks the Compose model and assigned address before changes.
Preview reads Compose if needed to resolve the listener and prints the final
configuration; it does not
prove DNS, firewall, port availability, upstream health, or certificate issuance.

Running the installer authorizes installation of missing Debian Nginx/Certbot
packages, acceptance of the Let's Encrypt subscriber agreement for the supplied
account, certificate issuance, managed Nginx configuration, and enabling/starting
Nginx and Certbot's renewal timer.

The first run installs the restrictive challenge-only configuration **before
package installation can start Nginx**. Certbot uses `certonly` with a webroot;
it does not edit Nginx. The script verifies the certificate chain and hostname,
tests the HTTPS configuration, then activates it. The distribution default site
is never included. Certificates are reused until renewal is due.

The script does not create HOMER tokens, edit `.env`, pull images, restart
containers, modify volumes, change PBXs, or alter firewalls. Failed first
issuance leaves only the challenge listener. Failed issuance on a repeat run
keeps existing HTTPS. Failed candidate validation or reload leaves/restores the
previous Nginx configuration.

## 4. Roll out API support and create a token

Compose sets `HOMER_COORDINATOR_API_SETTINGS_ENABLE_TOKEN_ACCESS=true` and uses
the `Auth-Token` header for every deployment. There is no `.env` opt-in and no
need to regenerate credentials. Enabling token authentication does not grant
anonymous access; callers still need a valid token.

For a **Git checkout**, apply the update on each monitoring server:

```bash
cd /opt/monitoring
git status --short
git pull --ff-only
./scripts/check-config.sh
docker compose up -d --no-deps --pull never homer
docker compose ps homer
```

Resolve local changes before pulling. `docker compose restart` alone does not
apply changed environment variables; `up -d` recreates HOMER when its Compose
configuration changes. `--no-deps --pull never` avoids rerunning volume ownership
initialization or pulling images for this configuration rollout.

For an installer-managed copy without Git, run the normal repository
`install.sh` update procedure in [Operations](OPERATIONS.md#update-the-deployment).
It preserves `.env` and local inventories, refreshes managed files, and
reconciles the stack, including the API setting. Unlike the targeted Git command,
the full installer also pulls pinned images and reconciles other changed services.
Do not copy `.env.example` over the existing `.env`.

This briefly interrupts capture/search; packets sent during the gap may be
lost. Named volumes remain intact. Live PBX calls, registrations, and media
continue independently. Plan the change accordingly.

HTTPS still needs the one-time setup above on every server and a dedicated
token below; copying Compose files cannot provision certificates for unspecified
hostnames or decide which PBXs to admit.

Using the existing SSH tunnel, sign in to HOMER and open **Settings → Auth
tokens**. Create a dedicated active integration token with the minimum
transaction-read privileges supported by the installed build. Choose an expiry
and rotation policy. The JWT signing secret, Flight token, and browser login
JWT are not this integration credential.

Keep the token in protected PBX settings/a password manager, never in Git,
shell history, or Nginx configuration. Source IP restrictions do not provide
per-tenant capture isolation.

## 5. Connect FS PBX

As a superadmin, open **System Settings → SIP Capture → Remote HOMER API**:

1. Set the base URL to `https://homer.example.com`, without `/api/v4`.
2. Enter the HOMER API token. Current FS PBX uses `Auth-Token` only.
3. Enable **Homer API Integration**.
4. Use **Test Connection**, then **Save** at the bottom of the tab. Testing does
   not save settings. Save also saves the SIP Capture section where permitted;
   review both sections before saving.

Every application node serving requests needs network access and certificate
trust. Do not disable certificate verification. SIP capture and RTCP reporting
remain separate prerequisites. An open QoS endpoint does not generate missing
data; see [RTCP quality](RTP_QUALITY.md).

## 6. Verify access and renewal

On MON01:

```bash
sudo nginx -t
systemctl is-enabled nginx.service certbot.timer
systemctl is-active nginx.service certbot.timer
systemctl list-timers certbot.timer
sudo certbot certificates
sudo certbot renew --cert-name fspbx-homer-api --dry-run
sudo env RENEWED_LINEAGE=/etc/letsencrypt/live/fspbx-homer-api \
  /etc/letsencrypt/renewal-hooks/deploy/fspbx-homer-api
```

The dry run exercises staging validation without replacing the live certificate.
It normally skips deploy hooks, so the last command separately tests the hook
against the current configuration. Do not claim renewal works until the dry run
succeeds on the deployed host.

From an approved PBX, check certificate trust and authentication rejection
without supplying a real token or call data:

```bash
curl --silent --show-error --output /dev/null --write-out '%{http_code}\n' \
  --header 'Content-Type: application/json' --data '{}' \
  https://homer.example.com/api/v4/transactions/messages
```

Expect `401`. Do not add `-k`. An approved source with a deliberately invalid
`Auth-Token` should also receive `401` from HOMER. From an unapproved source,
the firewall should prevent HTTPS connections. When separately testing Nginx
with network access permitted, an exact POST route carrying a nonempty dummy
token should return `403` and never reach HOMER.

Check that the HTTP homepage, HTTP API paths, unknown challenge files, and HTTPS
administration paths return `404` when reachable. Wrong methods, missing tokens,
and query parameters may be rejected earlier with `405`, `401`, and `400`.
The Certbot renewal dry run confirms that validators outside the PBX allowlist
can reach the HTTP challenge path.

Finish with FS PBX **Test Connection** and recent inbound, outbound, and
transferred calls. Verify SIP traces, PCAP/TXT downloads, QoS for a call with
known RTCP reports, and continued HEP ingest from existing PBXs. The connection
test checks the messages endpoint only, not the other operations or RTCP ingest.

## Repeat deployments and operations

Run the installer on each prepared MON01 with its own hostname and account
email. It reuses that host's HEP bind address, PBX source list, and certificate;
pass `--listen-address` when an explicit override is needed. No private
inventory is embedded in the repo. Changing the hostname or listen address
requires a reviewed migration rather than silently moving an existing endpoint.

After editing the shared list, use the normal PBX access update (keep using the
legacy file and `fspbx-hep-firewall.service` on hosts that have not renamed them):

```bash
sudoedit /etc/fs-monitoring/pbx-access.env
sudo systemctl restart fspbx-access.service
```

The updated PBX access helper also refreshes an already provisioned Nginx API allowlist
from the effective `ALLOWED_PBX_SOURCES` value. It validates Nginx before reloading,
without requesting a certificate or restarting HOMER. If the list changed while
Nginx was inactive, synchronization starts the proxy. The same happens on boot
when the PBX access service applies its rules. Without a managed API proxy, HEP behavior
is unchanged. API synchronization failures make the helper report failure; HEP
rules may already have been applied, while the previous API configuration is
retained. Inspect both before retrying.

To refresh just the proxy after an inventory edit:

```bash
sudo /opt/monitoring/ops/install-homer-api.sh --sync-allowlist
```

The shared source file removes duplicate selection. Separate provider and host
INPUT policies for port 443 still need to reflect the same list; the PBX access helper
does not manage those policies. Removed sources are denied by the refreshed
Nginx allowlist even if an old network permit remains. New sources also need
network permission before they can connect.

| Host path | Purpose |
|---|---|
| `/etc/nginx/nginx.conf` | Managed HTTP/HTTPS configuration and source allowlist |
| `/etc/fs-monitoring/homer-api-nginx.previous` | Previous changed configuration, mode 0600 |
| `/etc/letsencrypt/` | Account, certificates, private keys, renewal configuration |
| `/etc/letsencrypt/renewal-hooks/deploy/fspbx-homer-api` | Validate/reload Nginx after this lineage renews |
| `/var/www/fspbx-homer-acme/` | Dedicated HTTP-01 challenge webroot |

Certbot's timer uses the saved webroot configuration. The hook reloads Nginx
after this lineage renews and ignores other lineages. Monitor certificate
expiry independently and check `journalctl -u certbot.service`; an enabled
timer alone does not prove renewal works. Repository updates preserve `/etc`
state; rerun the HTTPS installer with the hostname/email (and any explicit
`--listen-address` override used originally) to apply proxy
changes. The Compose rollout above automatically applies HOMER API settings;
the host Nginx configuration is managed separately from container restarts.

If validation fails, inspect DNS A/AAAA/CAA records, inbound 80, provider/NAT
routing, and `/var/log/letsencrypt/letsencrypt.log`. API 403 suggests a source IP
mismatch. API 401 suggests invalid/expired/inactive credentials or disabled
token access. A 502/504 suggests an upstream or timeout problem.

## Rollback

Before updates, retain a protected copy of the working Nginx configuration and
back up `/etc/letsencrypt` with permissions and symlinks preserved. It contains
private keys: keep backups encrypted and outside Git. The installer does not
establish or test a backup/restore process.

To restore the immediately previous configuration, review it first: it may be
the initial HTTP-only configuration. Then:

```bash
sudo nginx -t -c /etc/fs-monitoring/homer-api-nginx.previous
sudo install -m 0644 /etc/fs-monitoring/homer-api-nginx.previous /etc/nginx/nginx.conf
sudo nginx -t
sudo systemctl reload nginx.service
```

To withdraw this dedicated endpoint, block inbound 443 and disable Nginx. If
this is the only certificate on the host, also disable the Certbot timer;
otherwise preserve renewal for other certificates. Keep HOMER UI access through
SSH. Do not delete volumes or certificate storage as a troubleshooting step.

## References

- [FS PBX integration guide](https://github.com/nemerald-voip/fspbx/blob/main/documentation/docs/tutorials/50-integrations/03-homer-call-history.md)
- [FS PBX API client](https://github.com/nemerald-voip/fspbx/blob/main/app/Services/HomerApiService.php)
- [HOMER token authentication](https://github.com/sipcapture/homer/blob/homer11/docs/UI_COORDINATOR_AUTH_AND_TOKENS.md)
- [Let's Encrypt HTTP-01](https://letsencrypt.org/docs/challenge-types/#http-01-challenge)
- [Let's Encrypt firewall requirements](https://letsencrypt.org/docs/integration-guide/#firewall-configuration)
- [Certbot renewal and hooks](https://eff-certbot.readthedocs.io/en/stable/using.html#renewing-certificates)
