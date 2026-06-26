# Project Governance

This project is a MIPI Alliance Open Source Software (OSS) project. Its governance
follows the Standard Governance Model in Annex D of the *MIPI Policy and Procedures
for Software Development Projects*.

## Project Roles

### Maintainer
The Maintainer is responsible for accepting contributions, managing branches, and
making releases of the project. The Maintainer may also act as a Reviewer or a
Developer. The current code owners are listed in [CODEOWNERS](CODEOWNERS).

### Reviewer
Reviewers review patches submitted by Developers before they are merged by the
Maintainer. No Reviewer shall review their own work.

### Developer
Developers submit patches (via GitHub pull requests) with the intent of having them
merged into the project repository. Developers may also act as Reviewers.

## Accepting Contributions

The Maintainer may accept contributions provided under the project's license
(BSD 3-Clause). Contributions should be reviewed by one or more Reviewers before
acceptance, and code should always be made reviewable before being merged.

There is no difference in the acceptance process between contributions from MIPI
Member companies and non-members, except that non-members must sign the project
[CLA](.github/CLA.md). See [CONTRIBUTING.md](.github/CONTRIBUTING.md).

## Managing Releases

The Maintainer — or a member of the owning MIPI Working Group or the MIPI Software
Working Group — may propose a new release. Releases are typically made when:

1. Unreleased code fixes an important bug, or
2. Unreleased code adds new functionality useful to project users (such
   functionality should have smoketests that pass before release).

Minor and sub-minor releases may be made at the Maintainer's discretion.

## Version Numbering

Version numbers consist of `major.minor.subminor` (for example, `1.0.0`).

- **Major** releases are made for changes incompatible with previous interfaces.
- **Minor** releases are made when new, important functionality is added.
- **Sub-minor** releases are made for bug fixes or less-noteworthy additions.

Backwards compatibility is maintained within a given major release.

### Branching and Tags
- New major and minor versions are tagged from `main`, creating a branch named
  `vmajor.minor` at the same time. Sub-minor releases are tagged on that branch.
- Each release has a tag named `branch.subminor`, where `subminor` starts at `0`.
- For public releases, each release tag **shall be signed** by the Maintainer using
  their own OpenPGP key. The Maintainer's key fingerprint should be conveyed
  wherever the software is distributed.

## Relationship to MIPI Specifications

This project may be revised independently of any related MIPI specification. No
dependency or relationship should be assumed between a code version and a
specification version that happen to share the same number.

## Security

Security issues are handled per [SECURITY.md](.github/SECURITY.md) and Annex D.6 of
the MIPI policy.
