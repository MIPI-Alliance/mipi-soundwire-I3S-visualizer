# Contributing

Thank you for helping with SWI3S Studio.

## The most useful contribution: use it, and tell us what you find

**Please help by using SWI3S Studio on your own buses and captures, and by
[opening an issue](https://github.com/MIPI-Alliance/mipi-soundwire-I3S-visualizer/issues/new/choose)
for any problem you run into.** A decode that disagrees with what the bus did, a crash, a
pane that shows something you did not expect, a capture format it cannot open, a step in
the [User Guide](docs/USER_GUIDE.md) that does not match the app: all of these are worth
reporting, even when you are not sure it is a bug. Reports from real buses are what this project needs most, and they are the
contributions it is least able to produce for itself.

Choose **Bug report** for a problem and **Feature request** for something the tool
should do. A good report says:

- **the version**, from the window title or **Help ▸ About SWI3S Studio**, and how you
  run it (`run.sh`, `run.ps1`, a manual setup or a packaged build);
- **the platform**: operating system, and the Python version for a source install;
- **what you opened**: the mode, the capture format (Logic 2 `.sal`, CSV, `.vcd`, a
  Tektronix export, ...), the PHY, and the column count if you know it;
- **what you did, what you expected, and what happened instead.** A screenshot helps for
  anything on screen, and the full text of any error message helps for everything else.

**Issues are public. Attach only files you are entitled to publish.** A capture or a
config can carry a device's register contents or a product's bus configuration. If you
can reproduce the problem on a demo capture (**File ▸ Open Demo Capture**) or on a
Visualizer config you can share, attach that instead. Otherwise describe the capture
(format, length, sample rate, PHY, column count) and a maintainer will tell you what else
would help.

If you think the tool disagrees with the SWI3S specification, name the section and the
behaviour you expected. Do **not** report security vulnerabilities as issues: follow
the [security policy](https://github.com/MIPI-Alliance/mipi-soundwire-I3S-visualizer/security/policy)
instead.

## Pull requests

Pull requests are welcome too, though the project gains more from reports. A few things
make a PR easier to take:

- **For anything bigger than a small fix, open an issue first**, so the change can be
  agreed before you spend time on it. The maintainers often have related work in
  progress, and a short discussion avoids two people changing the same code.
- **Keep it focused**: one problem per PR, with a test that fails without the change.
- **Do not change version numbers or release notes.** A maintainer sets the version when a
  release is made ([docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)).

The rest of this file covers the license and CLA, how a pull request is reviewed, and
this project's rules for working in the code.

<!-- The next four sections are adapted from the MIPI-Alliance organisation default
     (MIPI-Alliance/.github, CONTRIBUTING.md). Compare with it when that file changes. -->

## License of contributions

Every contribution is accepted **inbound under the project's own license**, BSD 3-Clause
(see [`LICENSE.md`](LICENSE.md)). By contributing you agree your contribution is licensed
under those terms. Contributions that attach **additional legal terms** will be rejected;
the Maintainer may ask you to resubmit without them.

## The CLA

Contributors from MIPI member companies contribute under the project license.
Everyone else is welcome too, but must first sign the project
[Contributor License Agreement](.github/CLA.md) (Policy §4.1.4.2). The CLA Assistant
checks this on every pull request; to sign, comment on your PR exactly:
`I have read the CLA Document and I hereby sign the CLA`.

## Submitting a pull request

1. Fork the repository and create a topic branch from `main`.
2. Make focused commits; add or update tests where applicable and keep the suite green.
3. Open a pull request against `main` and complete the checklist.

## Review and merge

- PRs to `main` require at least **one approving review** (Policy Annex D); reviewers do
  not review their own work, and authors cannot approve their own PR.
- All required checks, including `config-check` and the CLA check, must pass before a
  Maintainer merges. The source branch is deleted on merge.

## SWI3S Studio: working in the code

- **Setup and tests.** The [README](README.md) covers setup (`run.sh` / `run.ps1`, or the
  manual steps) and the test commands. `bash tools/setup_dev.sh` (`.\tools\setup_dev.ps1`
  on Windows) installs what testing needs, `requirements-dev.txt`, and builds the decode
  core. Before opening a PR, run
  `bash tests/gate.sh --quick` (`.\tests\gate.ps1 --quick` on Windows). It rebuilds the
  native decode core and checks its ABI, then runs the test suite, `ruff`, the `mypy`
  check, the leak scan and the screenshots check (every mode in light and dark, checked for
  wrong-theme panes, widgets a theme switch missed, faint text and clipped labels); it skips
  only the perf gate and the one-process-per-file pass.
  On a pull request, CI runs the test suite on Linux, macOS and Windows, and the perf,
  coverage and lint jobs on Linux. If you run `pytest` directly after changing anything
  under `native/`, rebuild the decode core first. See
  [docs/TESTING.md](docs/TESTING.md) for how the suites are organised.
- **Docs change with the code.** If a change makes a sentence in the README, the User
  Guide or another doc wrong, correct it in the same PR.
- **Do not edit the SWI3S reference model.** `swi3s_studio/swviz/models/dataport.py` and
  `flow_control_port.py` are the reference model published with the SWI3S
  specification, and they change only as whole-file drops from their authors. If you find
  a defect in them, open an issue
  ([docs/DEVELOPMENT.md](docs/DEVELOPMENT.md#the-swviz-reference-model-is-a-published-spec-deliverable)).
- **Golden files are regenerated only with a reviewed diff**; say in the PR why the output
  changed.
- **Nothing in the tree may identify a machine, a person's setup or a third party**: no
  user names or home paths, host names, internal tool names, customer or partner names,
  and no capture that is not cleared for publication. `python3 tools/leak_scan.py` checks
  the structural cases; the rest is up to you and the reviewer.

## SWI3S Studio: tracking technical debt

Known shortcomings that are deliberately left for later are tracked as issues with the
`tech-debt` label, so that they stay visible between releases. Maintainers open them with
the blank issue form and the label; anyone can find them through the label filter.
