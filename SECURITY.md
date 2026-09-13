# Security Policy

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

If you discover a security vulnerability in Jeanclode, please report it responsibly by emailing:

**<adam.saimi@jeanclode.com>**

### What to Include

- A description of the vulnerability
- Steps to reproduce the issue
- Potential impact assessment
- Any suggested fixes (optional)

### Response Timeline

| Action | Timeframe |
|--------|-----------|
| Acknowledgment of report | Within 48 hours |
| Initial assessment | Within 1 week |
| Fix development | Depends on severity |
| Public disclosure | After fix is released |

We will work with you to understand the issue and coordinate disclosure. We ask that you:

- Allow reasonable time for us to address the issue before public disclosure
- Make a good faith effort to avoid privacy violations, data destruction, or service disruption
- Do not access or modify other tenants' data

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest release | Yes |
| Previous releases | Security fixes only |

## Security Architecture

Jeanclode runs agents against tenant repositories on their behalf, which is security-sensitive by nature. Key security measures include:

- **Per-tenant, ephemeral containers** — each execution (triage, fix, review) runs in an isolated container that is destroyed after completion
- **Credential injection via sidecar** — the `security-proxy` sidecar injects git provider credentials into agent HTTP traffic; the agent subprocess itself never has direct access to the raw tokens
- **Short-lived, scoped tokens** — GitHub installation tokens and GitLab group access tokens are minted per dispatch and scoped to the tenant's own repos
- **No cross-tenant leakage** — tokens and secrets for one tenant are never exposed to another tenant's container

For more details, see [ADR-003: Git Provider Authentication](./docs/adr/003-git-provider-authentication.md).

## Scope

The following are in scope for security reports:

- Authentication and authorization bypass
- Injection vulnerabilities (SQL, command, template)
- Container escape or isolation bypass
- Cross-tenant data or credential exposure
- Exposure of secrets, tokens, or credentials
- Cross-site scripting (XSS) or cross-site request forgery (CSRF)

The following are **out of scope**:

- Issues in third-party dependencies (report to the upstream project)
- Social engineering attacks
- Denial of service (DoS) attacks
- Issues requiring physical access

## Acknowledgments

We appreciate the security research community's efforts in helping keep Jeanclode and its users safe. Responsible reporters will be credited in release notes (unless they prefer to remain anonymous).
