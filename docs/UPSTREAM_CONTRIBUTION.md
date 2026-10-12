# Upstream contribution

The official integration contribution targets `DataDog/integrations-extras`, directory `freeswitch/`. This repository's
`integration/` directory is a maintained copy of it; it does not replace the upstream review process.

## Relationship

| Item | Location |
|---|---|
| Upstream working copy (what gets submitted) | `integrations-extras/freeswitch/` |
| Project copy (visibility and unified history) | `integration/` in this repository |
| Upstream-only changes | `.github/CODEOWNERS` and `.github/workflows/test-all.yml` in `integrations-extras`; **not** copied here |

Do not use submodules or subtree for this: the copy is deliberately a plain, reviewable directory.

## Keeping the copy aligned

1. Make the change in one place, then mirror it in the other the same day.
2. Verify no drift (no output means identical):

   ```shell
   diff -ru \
     --exclude=dist --exclude=.benchmarks --exclude=__pycache__ --exclude=.pytest_cache --exclude=.venv \
     --exclude=.mypy_cache --exclude=.ruff_cache --exclude=.coverage \
     ../integrations-extras/freeswitch integration
   ```

3. If the copy changed, update `docs/` references only when behavior or commands changed.

## Preparing an upstream PR

From the `integrations-extras` checkout, on the feature branch:

1. Confirm `freeswitch/` equals `integration/` (drift check above).
2. Run the validation set:

   ```shell
   ddev -x test freeswitch              # unit and scenario tests
   ddev -x test -s freeswitch           # format, lint, mypy
   ddev -x validate config freeswitch
   ddev -x validate models freeswitch
   ddev -x validate package freeswitch
   ddev -x validate readmes freeswitch
   ddev -x validate metadata freeswitch
   ```

   Anything that needs an `integrations-core` checkout is reported as an environment limitation, not as a pass.
3. Confirm `.github/CODEOWNERS` and `.github/workflows/test-all.yml` contain the integration entries that upstream
   requires, and that no secrets, local configs or `dist/` artifacts are staged.
4. Review the diff for the whole change (`git diff` and `git status`), then commit and open the PR from there following
   the `integrations-extras` contribution guide.

## Before submitting

- Record which Agent and Python versions the live validation used, and what remained unverified.
- Resolve or explicitly disclose open defects. As of this writing the empty-`host` validation gap is open and tracked by
  a strict `xfail` test.
- Do not describe unrun checks as passed.
