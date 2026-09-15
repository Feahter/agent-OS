# Security Policy

## Supported versions

| Version | Support |
| --- | --- |
| Latest `0.0.x` release | Best-effort security fixes during the controlled pilot |
| Older releases and unreleased commits | Unsupported; reproduce on the latest release before reporting |

Agent OS is pre-alpha. Security support does not imply production, multi-tenant, sandbox or credential-vault guarantees. See the [threat model](docs/threat-model.md) for trust boundaries and residual risk.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/Feahter/agent-OS/security/advisories/new). Do not open a public issue containing exploit steps, credentials, private repository content or unredacted Agent/Orca output.

Include the affected version/commit, operating system, Adapter and version, minimum reproduction, impact, whether a model or external service was called, and redacted logs. Use synthetic repositories and credentials whenever possible.

The project handles reports on a best-effort basis. The target is an initial acknowledgment within five business days, followed by scope confirmation, remediation or a documented residual-risk decision. There is no guaranteed SLA or bug bounty. Please coordinate public disclosure until a fix or mitigation is available.

## Out of scope

- Damage caused by granting an Agent broader OS, network, cloud or repository permissions than the documented policy.
- A malicious actor who already controls the same OS user and can modify both runtime code and state.
- Unsupported Adapter versions/platforms, cross-host filesystems and multi-tenant deployments.
- Model quality issues without a security boundary violation.
- Denial of service that only consumes the reporting user's explicitly authorized local resources or model budget.
