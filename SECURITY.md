# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub private vulnerability reporting:
<https://github.com/Autonometry/autonometry/security/advisories/new>
(repository **Security** tab, then **Report a vulnerability**).

Do not open a public issue or pull request for a vulnerability, and do not include real robot logs, keys or personal data in a report.

We aim to acknowledge a report within 5 working days and to tell you what we plan to do within 14 days.

## Supported versions

Autonometry is pre-alpha. Only the latest release and the `main` branch receive security fixes.

## Scope

In scope: the verifier and recorder (tamper evidence, signing keys, hash chains), parsing of logs and configuration, file and network handling in the `autonometry` package, and the release pipeline.
Out of scope: weaknesses in a robot's own software, and findings that need an attacker who already controls the machine that records the log.
