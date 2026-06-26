# Security Policy

This project is a MIPI Alliance Open Source Software (OSS) project and follows the
security process defined in the *MIPI Policy and Procedures for Software Development
Projects* (Annex D.6).

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, report potential security issues by email to:

- **software-security@mipi.org**

Your report may optionally include a patch that fixes the identified issue.

## What to Include

To help us evaluate and triage the report quickly, please include where possible:

- A description of the vulnerability and its potential impact.
- Steps to reproduce, or a proof of concept.
- Affected version(s), tag(s), or commit(s).
- Any known mitigations or workarounds.

## Our Process

The project Security Team (or, if none is designated, the project Maintainer) will:

1. Evaluate each report and determine its seriousness, taking the project's known
   uses into account.
2. Where warranted, prepare a fix in a timely manner and coordinate disclosure.
3. Notify the security contacts of any known external projects that incorporate
   portions of this software before public disclosure, where appropriate.

No information about a given security issue will be released publicly before the
corresponding coordinated security release, except in zero-day situations where the
vulnerability is already publicly known.

Security releases are tagged using the form `major.minor.subminor-securityX`
(for example, `1.2.3-security1`).
