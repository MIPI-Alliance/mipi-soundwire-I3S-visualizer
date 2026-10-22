# Project Governance

This project's governance follows the Standard Governance Model in Annex D of the
*MIPI Policy and Procedures for Software Development Projects*.

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
[CLA](.github/CLA.md). [CONTRIBUTING.md](CONTRIBUTING.md) describes how to report
problems and how to submit changes.

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

In this project, a release that adds any user-visible functionality is a **minor**
release, and **sub-minor** releases carry bug fixes only. This is narrower than Annex
D.4, which also allows less-noteworthy additions in a sub-minor release: it means a user
can take a sub-minor update without checking it for new behaviour. A release that
contains only security fixes takes a security tag (below) rather than a new number.

### Branching and Tags
- New major and minor versions are tagged from `main`, creating a branch named
  `vmajor.minor` at the same time. Sub-minor releases are tagged on that branch.
- Each release has a tag named `branch.subminor`, where `subminor` starts at `0`: the
  first release of the `v3.1` branch is `v3.1.0`, its first fix release `v3.1.1`.
- A release that contains only security fixes is tagged `vmajor.minor.subminor-securityN`,
  where `N` starts at 1 (Annex D.6.6).
- A pre-release tag, `vmajor.minor.subminor-rcN`, may mark a build for testing. It is not
  a release and is not signed or supported.
- Each release tag **shall be signed** by the Maintainer using
  their own OpenPGP key. The Maintainer's key fingerprint should be conveyed
  wherever the software is distributed.
- The supported versions, down to the minor version that receives fixes, are listed in
  the [README](README.md#releases).

## Relationship to MIPI Specifications

This project may be revised independently of any related MIPI specification. No
dependency or relationship should be assumed between a code version and a
specification version that happen to share the same number.

### Citing a release

A specification, application note or other document that depends on a particular
version of this software cites it by its signed release tag and this repository, for
example "SWI3S Studio `v3.0.20`, https://github.com/MIPI-Alliance/mipi-soundwire-I3S-visualizer".
The tag identifies the exact code, and the release of the same name on GitHub carries its
release notes. Cite a release tag, never a branch (including `main`) or a pre-release tag,
because those move or are replaced. Where a document needs the newest fixes of a minor
version rather than one release, it cites the minor version (for example "SWI3S Studio 3.1,
latest sub-minor release") and the reader takes the newest tag on that branch.

## Security

Security issues are handled per the MIPI Alliance organisation-level security policy,
reachable from this repository's **Security** tab, and Annex D.6 of
the MIPI policy.
