# Restrict monitoring access to approved PBXs

HOMER publishes HEP UDP and TCP 9060 on `HEP_BIND_ADDRESS`. The default is
`0.0.0.0`, so Docker accepts traffic to that published port from external IPv4
sources unless another network control blocks it.

The installer does not change firewall rules. Configure the allowlist before
sending production HEP traffic.

FS PBX's HOMER API uses a separate host HTTPS listener. Its
[deployment guide](HOMER_API.md#2-configure-provider-and-host-firewalls) covers
TCP 443 source restrictions and a TCP 80 listener that serves only Let's Encrypt
challenges. The API installer reuses `ALLOWED_PBX_SOURCES`; when this helper runs,
it also refreshes an already installed, managed Nginx API allowlist. The HEP
iptables rules below do not themselves open or filter the host's 80/443 ports.

New deployments use `/etc/fs-monitoring/pbx-access.env`, the
`ALLOWED_PBX_SOURCES` setting, and `fspbx-access.service`. Existing hosts can keep
`hep-firewall.env`, `ALLOWED_HEP_SOURCES`, and `fspbx-hep-firewall.service`:
the old helper delegates to the new one, and the API installer automatically
reads the legacy inventory when the new file is absent. No IP re-entry is needed.
Do not maintain both inventories or enable both service names. The internal
`FSPBX-HEP` chain keeps its name because it still filters HEP traffic specifically.

## Recommended layers

Use the layers available in the environment:

1. **Provider firewall:** allow UDP/TCP 9060 only from each PBX public IP or VPN
   subnet. This remains effective while Docker starts and before host services
   run.
2. **VPN bind:** when every PBX uses a VPN, set `HEP_BIND_ADDRESS` to MON01's VPN
   address instead of `0.0.0.0`.
3. **Docker-aware host filter:** optionally apply the repository's IPv4
   `DOCKER-USER` allowlist as a second layer.

Do not rely on a plain UFW `deny 9060` rule without testing. Docker explains
that published container traffic can be diverted before UFW's normal INPUT and
OUTPUT processing. See Docker's
[packet-filtering and firewall documentation](https://docs.docker.com/engine/network/packet-filtering-firewalls/).

## Option A: bind HEP to a VPN address

If MON01 owns VPN address `10.20.0.10`, edit `/opt/monitoring/.env`:

```dotenv
HEP_BIND_ADDRESS=10.20.0.10
```

Validate and recreate HOMER:

```bash
cd /opt/monitoring
./scripts/check-config.sh
docker compose up -d homer
docker compose port homer 9060/udp
docker compose port homer 9060/tcp
```

Confirm the address exists on the host before reconciling. A wrong bind address
prevents the container port from being published.

## Option B: apply an IPv4 source allowlist now

The included helper creates a dedicated `FSPBX-HEP` chain and jumps to it from
Docker's `DOCKER-USER` chain. It allows listed sources to the original published
UDP/TCP port 9060, drops every other IPv4 source for that port, and returns
unrelated Docker traffic unchanged.

Docker performs DNAT before packets reach `DOCKER-USER`, so the helper uses the
conntrack original-destination port as described in Docker's
[iptables documentation](https://docs.docker.com/engine/network/firewall-iptables/).

Find the interface that receives PBX traffic:

```bash
ip route show default
ip -brief address
```

Assume the interface is `eth0`, PBX1 is `203.0.113.10`, and PBX2 is
`198.51.100.24`. Those are documentation addresses—replace all three values.

Apply the rules immediately:

```bash
cd /opt/monitoring
sudo env \
  EXTERNAL_INTERFACE=eth0 \
  ALLOWED_PBX_SOURCES='203.0.113.10/32 198.51.100.24/32' \
  HEP_PORT=9060 \
  ./ops/apply-pbx-access.sh
```

For a whole VPN subnet, use a CIDR such as `10.20.0.0/24`. Use the smallest
source ranges that match the actual PBXs.

The helper requires Docker's iptables backend and an existing `DOCKER-USER`
chain. It exits without applying rules when that chain is unavailable. If Docker
uses its nftables backend, use a provider firewall, bind HEP to a private/VPN
address, or implement a reviewed nftables forward-chain policy using Docker's
[nftables guidance](https://docs.docker.com/engine/network/firewall-nftables/).

## Make the host allowlist persistent

Provider firewall rules are the preferred persistent control. To also reapply
the repository helper after Docker starts, install its opt-in systemd unit:

These are fresh-setup commands. Existing installations should keep their current
inventory and unit, or follow the rename procedure below.

```bash
cd /opt/monitoring
sudo install -d -m 0750 /etc/fs-monitoring
sudo install -m 0640 ops/pbx-access.env.example \
  /etc/fs-monitoring/pbx-access.env
sudoedit /etc/fs-monitoring/pbx-access.env
sudo install -m 0644 systemd/fspbx-access.service \
  /etc/systemd/system/fspbx-access.service
sudo systemctl daemon-reload
sudo systemctl enable --now fspbx-access.service
```

Replace every example value before starting the service. The unit runs after
Docker, is ordered before the boot reconciler when both start together, and
reapplies the dedicated chain when the Docker service is restarted.

After changing the allowlist:

```bash
sudoedit /etc/fs-monitoring/pbx-access.env
sudo systemctl restart fspbx-access.service
```

## Rename an existing HEP firewall installation

Renaming is optional; repository updates keep the old paths working. To adopt
the new names, first verify that `/etc/fs-monitoring/pbx-access.env` does not
already exist and that only the legacy service is enabled. Keep the existing
file's contents and permissions; `ALLOWED_HEP_SOURCES` remains an accepted alias,
so no addresses need to be typed again.

Apply during an access-policy maintenance window. The old unit has no `ExecStop`,
so disabling it leaves the active HEP rules in place. Run these commands in order,
stopping on any failure:

```bash
cd /opt/monitoring
sudo install -m 0644 systemd/fspbx-access.service /etc/systemd/system/fspbx-access.service
sudo systemctl disable --now fspbx-hep-firewall.service
sudo mv -T --no-clobber /etc/fs-monitoring/hep-firewall.env /etc/fs-monitoring/pbx-access.env
sudo systemctl daemon-reload
sudo systemctl enable --now fspbx-access.service
sudo systemctl status fspbx-access.service --no-pager
```

The next rule application also refreshes a provisioned API proxy. Verify the
rules and API access below before considering migration complete. To roll back
the naming change, disable the new unit, move the inventory back without
overwriting another file, and re-enable the legacy unit. No Docker restart is
needed. If desired, rename the variable to `ALLOWED_PBX_SOURCES` in place later;
do not define both variables.

## Add another PBX to an existing allowlist

Keep every previously allowed source and add the new PBX's source address as
seen by MON01 (its VPN address or, when NAT is involved, the NAT egress address).
Update the provider firewall and any host allowlist in use for the chosen HEP
transport on port 9060.

If the persistent `fspbx-access.service` above is already installed, edit
its existing environment file and append the new source with `/32` to
`ALLOWED_PBX_SOURCES`, then restart the firewall service:

```bash
sudoedit /etc/fs-monitoring/pbx-access.env
sudo systemctl restart fspbx-access.service
```

Do not copy the example environment file over the existing inventory. If using
the helper directly, pass the full existing source list plus the new source:
the helper replaces its dedicated chain's rules on each application. If no host
helper is installed, update the existing provider/VPN controls rather than
assuming this systemd service exists. These are firewall changes; apply them
through the environment's authorized change procedure.

Verify a new test call from the new PBX and continued capture from an existing
PBX in HOMER. No HOMER container restart is needed for a source-allowlist update.

With the API proxy provisioned and the updated helper installed, the same
firewall-service restart validates and reloads its Nginx source allowlist.
If the list changed while Nginx was inactive, synchronization starts the proxy.
If that synchronization fails, the service reports an error even though HEP
rules may already have changed. Inspect its journal and Nginx before retrying.
Keep the provider and host INPUT rules for HTTPS aligned with the shared source
list; those network policies are not modified by this PBX access helper.

## Verify

Inspect the active host rules:

```bash
sudo iptables -n -v -L FSPBX-HEP --line-numbers
sudo iptables -n -v -L DOCKER-USER --line-numbers
sudo systemctl status fspbx-access.service --no-pager
```

Confirm Docker's published addresses:

```bash
cd /opt/monitoring
docker compose port homer 9060/udp
docker compose port homer 9060/tcp
ss -lnptu | grep ':9060'
```

Then verify all of the following from real network locations:

- every approved PBX continues to appear in HOMER;
- an unapproved IPv4 source cannot reach TCP 9060;
- rule packet counters increase for allowed and denied tests;
- unrelated Docker-published services still behave as expected.

UDP reachability tests alone can be ambiguous because UDP has no connection
handshake. A real HEP packet and HOMER ingest confirmation are stronger tests.

## IPv6 warning

The helper is deliberately IPv4-only. Depending on Docker and host IPv6
configuration, a published port may also be reachable through IPv6. Audit the
host and provider firewall explicitly. If IPv6 HEP is not required, block it at
the provider edge or bind HEP only to the intended IPv4/VPN address. If it is
required, add a separately reviewed IPv6 allowlist; do not assume these IPv4
rules protect it.

## Roll back the host helper

Removing the host rule exposes HEP again wherever the provider firewall and bind
address allow it. Confirm another protection layer before rollback.
`--remove` removes only the HEP chain; it leaves the managed API restrictions
in place. Use the legacy service name below if that is the one installed.

For a one-off rule:

```bash
cd /opt/monitoring
sudo env EXTERNAL_INTERFACE=eth0 ./ops/apply-pbx-access.sh --remove
```

For the persistent unit:

```bash
sudo systemctl disable --now fspbx-access.service
cd /opt/monitoring
sudo env EXTERNAL_INTERFACE=eth0 ./ops/apply-pbx-access.sh --remove
```

Replace `eth0` with the interface used when the rules were installed.
