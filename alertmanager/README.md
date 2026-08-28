# Alert delivery

Prometheus sends alerts to Alertmanager, but the shipped configuration uses
no-op receivers. Alerts appear in Alertmanager without sending email, chat,
webhook, or paging notifications.

## Create a local configuration

Do not put real delivery credentials in the tracked `alertmanager.yml`.
Create the ignored local file instead:

```bash
cd /opt/monitoring
install -o 65534 -g 65534 -m 0600 \
  alertmanager/alertmanager.yml alertmanager/alertmanager.local.yml
sudoedit alertmanager/alertmanager.local.yml
chown 65534:65534 alertmanager/alertmanager.local.yml
chmod 0600 alertmanager/alertmanager.local.yml
```

The container runs as UID/GID `65534`, so that ownership lets Alertmanager read
the file without making its credentials readable to other host users.

Set this value in `/opt/monitoring/.env`:

```dotenv
ALERTMANAGER_CONFIG_PATH=./alertmanager/alertmanager.local.yml
```

Add the required email, Slack, Microsoft Teams, PagerDuty, or generic webhook
receivers and routes. Keep warning and critical routing deliberate; do not make
every alert page the same person without considering urgency and grouping.

Apply and inspect the result:

```bash
cd /opt/monitoring
./scripts/check-config.sh
docker compose up -d alertmanager
docker compose logs --tail=100 alertmanager
```

Send a controlled test alert before relying on delivery. Confirm resolution
notifications, grouping, inhibit rules, and escalation behavior.

## Credential rules

- Keep `alertmanager.local.yml` mode `0600` and owned by UID/GID `65534`.
- It is ignored by Git, but still verify `git status` before every commit.
- Prefer mounted secret files when the receiver supports them.
- Never paste notification credentials into issues, pull requests, CI logs, or
  AI conversations.
- Back up the local file encrypted and outside MON01.
- Rotate credentials immediately after suspected exposure.
