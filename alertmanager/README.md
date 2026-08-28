# Alert delivery

The shipped configuration groups and displays alerts but intentionally sends no
external notifications. Add an email, Slack, Microsoft Teams, PagerDuty, or
generic webhook configuration to the `default` and `critical` receivers in
`alertmanager.yml`, then run:

```bash
docker compose restart alertmanager
```

Never commit notification credentials. Prefer a mounted secret file or a local
override file readable only by root.
