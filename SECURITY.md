# Security Policy

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Report vulnerabilities via GitHub's private disclosure feature:
**[Report a vulnerability](https://github.com/ToscanAI/orchemist/security/advisories/new)**

Or email: **conny.lazo@gmail.com** with subject line `[SECURITY] orchemist — <brief description>`

## Response SLA

| Severity | Acknowledgement | Patch target |
|----------|----------------|--------------|
| Critical | 48 hours | 7 days |
| High | 48 hours | 14 days |
| Medium/Low | 5 business days | Next release |

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.8.x (latest) | ✅ |
| < 0.8.0 | ❌ |

## Scope

In scope: the orchestration engine CLI, sequencer, template parsing, file guard, executor modules.

Out of scope: vulnerabilities in third-party dependencies (report upstream), issues requiring physical access to the machine.
