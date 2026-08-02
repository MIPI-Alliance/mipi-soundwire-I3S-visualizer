<!--
Thanks for your contribution! Please complete the checklist below.
Non-members: the CLA Assistant bot will guide you through signing the CLA.
-->

## Description

<!-- What does this pull request change, and why? -->

## Related Issue(s)

<!-- e.g. Closes #123 -->

## Type of Change

- [ ] Bug fix
- [ ] New feature
- [ ] Documentation
- [ ] Refactor / maintenance

## Checklist

- [ ] I have read the [CONTRIBUTING](CONTRIBUTING.md) guidelines.
- [ ] My contribution is licensed under the project's **BSD 3-Clause License** with
      no additional legal terms.
- [ ] If I am a non-member, I have signed the [CLA](CLA.md) (the bot will prompt me).
- [ ] I have added or updated tests where applicable, and **CI is green** (full suite,
      goldens, and the perf gate).
- [ ] If any **golden** changed, I reviewed the diff and explained *why* (goldens are not
      regenerated blindly — see [DEVELOPMENT.md](../docs/DEVELOPMENT.md)).
- [ ] If I touched a **hot path**, I added or adjusted a perf ceiling in `test_perf.py`.
- [ ] I considered **cross-subsystem coupling** (contracts between decode / engine / UI /
      serialization), not just the code I changed.
- [ ] I have updated documentation where applicable.
