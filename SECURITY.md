# Security policy

Do not open a public issue containing credentials, private network details, SIP
captures, or logs. Report suspected vulnerabilities privately through GitHub's
private vulnerability reporting feature when it is enabled for this repository.

Never commit `.env`, Alertmanager notification credentials, ESL passwords, VPN
keys, packet captures, or populated infrastructure inventories. Rotate a secret
immediately if it is exposed, including if it is later removed from Git history.

Deploy published version tags rather than `main`. Repository administrators
should enable immutable GitHub releases and branch protection for `main`.
