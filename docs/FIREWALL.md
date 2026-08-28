# Restrict HEP traffic to approved PBXs

HOMER publishes HEP UDP and TCP 9060 on `HEP_BIND_ADDRESS`. The default is
`0.0.0.0`, so Docker accepts traffic to that published port from external IPv4
sources unless another network control blocks it.

The installer does not change firewall rules. Configure the allowlist before
sending production HEP traffic.

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
  ALLOWED_HEP_SOURCES='203.0.113.10/32 198.51.100.24/32' \
  HEP_PORT=9060 \
  ./ops/apply-hep-firewall.sh
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

```bash
cd /opt/monitoring
sudo install -d -m 0750 /etc/fs-monitoring
sudo install -m 0640 ops/hep-firewall.env.example \
  /etc/fs-monitoring/hep-firewall.env
sudoedit /etc/fs-monitoring/hep-firewall.env
sudo install -m 0644 systemd/fspbx-hep-firewall.service \
  /etc/systemd/system/fspbx-hep-firewall.service
sudo systemctl daemon-reload
sudo systemctl enable --now fspbx-hep-firewall.service
```

Replace every example value before starting the service. The unit runs after
Docker, is ordered before the boot reconciler when both start together, and
reapplies the dedicated chain when the Docker service is restarted.

After changing the allowlist:

```bash
sudoedit /etc/fs-monitoring/hep-firewall.env
sudo systemctl reload fspbx-hep-firewall.service
```

## Verify

Inspect the active host rules:

```bash
sudo iptables -n -v -L FSPBX-HEP --line-numbers
sudo iptables -n -v -L DOCKER-USER --line-numbers
sudo systemctl status fspbx-hep-firewall.service --no-pager
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

For a one-off rule:

```bash
cd /opt/monitoring
sudo env EXTERNAL_INTERFACE=eth0 ./ops/apply-hep-firewall.sh --remove
```

For the persistent unit:

```bash
sudo systemctl disable --now fspbx-hep-firewall.service
cd /opt/monitoring
sudo env EXTERNAL_INTERFACE=eth0 ./ops/apply-hep-firewall.sh --remove
```

Replace `eth0` with the interface used when the rules were installed.
