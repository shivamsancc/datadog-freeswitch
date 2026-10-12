# Upstream contribution and link policy

This is the definitive description of how this repository relates to the official `integrations-extras` contribution.

## What is authoritative

| Item | Where | Notes |
|---|---|---|
| The contribution | `freeswitch/` in a checkout of `integrations-extras` (local branch `add-freeswitch-integration`) | What is reviewed and merged. Upstream-only edits also exist in `.github/CODEOWNERS` and `.github/workflows/test-all.yml`; they are **not** copied here. |
| The project copy | `integration/` in this repository | A snapshot kept identical to `freeswitch/` so the work, its tests and its documentation live next to the lab. |
| Lab | `lab/` | Not part of the contribution. |

**Editing `integration/` does not update `integrations-extras`, and editing `integrations-extras` does not update
`integration/`.** Nothing synchronizes them automatically. Never describe the copy as the official source.

Submodules and subtree are deliberately not used.

## Canonical URLs

| Purpose | URL | Status when this was written |
|---|---|---|
| Official upstream repository | `https://github.com/DataDog/integrations-extras` | Verified (reachable, default branch `master`) |
| Your fork | `https://github.com/shivamsancc/integrations-extras` | Verified (reachable; only `master`) |
| This project | `https://github.com/shivamsancc/datadog-freeswitch` | Not reachable unauthenticated, consistent with a private repository |
| Upstream contribution directory | `https://github.com/DataDog/integrations-extras/tree/master/freeswitch` | **404 until the contribution is merged** |
| A contribution file on the PR branch | `https://github.com/<fork-owner>/integrations-extras/blob/add-freeswitch-integration/freeswitch/README.md` | **404 until the branch is pushed to the fork**; the branch is currently local only |

Notes on the last two rows:

- A branch that exists only on a fork is not reachable under `DataDog/integrations-extras`. Once pushed, the branch is
  addressed on the fork (`shivamsancc/integrations-extras`), and after a PR is opened it appears on the PR page.
- `integration/README.md` is byte-identical to the upstream file and its reference links point at
  `https://github.com/DataDog/integrations-extras/blob/master/freeswitch/...` (the post-merge location). Those two links
  return 404 now and will resolve after the merge. They are intentionally **not** rewritten here, because that would make
  `integration/` drift from the contribution.

## Link rules

1. **Inside this repository**, use relative links computed from the Markdown file's own location: from the
   root README the lab manual is `lab/README.md`, from `lab/README.md` the root README is `../README.md`, and from
   `docs/` the integration README is `../integration/README.md`. Check them with the script in
   [Verification](#verification).
2. **To the official contribution**, use `DataDog/integrations-extras` explicitly, and only with paths and branches that
   exist. Do not guess; run `git ls-remote` or open the URL.
3. **To the fork**, use `shivamsancc/integrations-extras` only where the fork is genuinely meant (for example, pushing a
   PR branch). Never present a fork URL as the official upstream.
4. **Do not rewrite upstream links into local ones** when the purpose is to point at the official contribution.
5. **Inside `integration/`**, do not change anything unless the same change is also made in the upstream checkout.

## Comparing the copy with upstream

Run from the root of this repository, with `integrations-extras` checked out next to it:

```shell
diff -ru \
  --exclude=dist --exclude=.benchmarks --exclude=__pycache__ --exclude=.pytest_cache --exclude=.venv \
  --exclude=.mypy_cache --exclude=.ruff_cache --exclude=.coverage \
  ../integrations-extras/freeswitch integration
```

No output means they are identical. This command only reads both trees.

## Synchronizing without overwriting unrelated work

1. Decide the direction. Normally changes are made and reviewed in the **upstream checkout first**, because that is
   what is submitted. Review them there (`git status`, `git diff` inside `integrations-extras`).
2. Compare (above). Read the diff; do not copy blindly.
3. Copy only the files that should change, one by one or by explicit list, from `../integrations-extras/freeswitch/` to
   `integration/`. Avoid `rsync --delete`, which removes files that exist only in the destination.
4. Re-run the comparison; it must be empty again.
5. Run the checks in the next section from the correct location and record the results.

## Where each command runs

| Command | Run from | Why |
|---|---|---|
| `pytest tests` | `integration/` (Python 3.13 venv) | Uses the copy; needs no upstream checkout |
| `ddev -x test freeswitch`, `ddev -x test -s freeswitch` | the `integrations-extras` checkout | `ddev` derives the integration from the directory name `freeswitch/` |
| `ddev -x validate <name> freeswitch`, `ddev -x release build freeswitch` | the `integrations-extras` checkout | same; some validators need an `integrations-core` checkout |
| `diff -ru …` comparison | this repository's root | compares both trees |
| `git add` / `git commit` / `git push` for a PR | the `integrations-extras` checkout, on the PR branch | only that history goes upstream |
| lab commands (`lab/scripts/...`) | this repository's root | the scripts change to the root themselves |

## Preparing an upstream PR

From the `integrations-extras` checkout, on the contribution branch:

1. Confirm the comparison above is empty.
2. Run the validation set and record each result honestly; anything that needs an `integrations-core` checkout is an
   environment limitation, not a pass:

   ```shell
   ddev -x test freeswitch
   ddev -x test -s freeswitch
   ddev -x validate config freeswitch
   ddev -x validate models freeswitch
   ddev -x validate package freeswitch
   ddev -x validate readmes freeswitch
   ddev -x validate metadata freeswitch
   ```
3. Confirm the `.github/CODEOWNERS` and `.github/workflows/test-all.yml` entries upstream requires are present, and that no
   secrets, local configs or `dist/` artifacts are staged.
4. Push the branch to the fork and open the PR against `DataDog/integrations-extras:master`, following that repository's
   contributing guide.

Do not describe unrun checks as passed. The former empty-`host` defect is fixed (`instance_host` in
`config_models/validators.py`) and covered by normal regression tests, not an `xfail`.

## Verification

Check local Markdown links (read-only; run from this repository's root):

```shell
python3 - <<'PY'
import re, os, subprocess
bad = 0
for f in subprocess.check_output(["git", "ls-files", "*.md"], text=True).split():
    for target in re.findall(r"\]\(([^)\s#]+)(?:#[^)]*)?\)", open(f).read()):
        if re.match(r"(https?|mailto):", target):
            continue
        if not os.path.exists(os.path.normpath(os.path.join(os.path.dirname(f), target))):
            print("BROKEN", f, target); bad += 1
print("broken links:", bad)
PY
```

## Current local state

- The local branch `add-freeswitch-integration` points at the same commit as `master`; it has **no commits of its own**.
  The contribution is the untracked `freeswitch/` directory plus edits to `.github/CODEOWNERS` and
  `.github/workflows/test-all.yml`, all uncommitted.
- The branch does not exist on the fork (`git ls-remote --heads fork` lists only `master`), so no URL for it works yet.
- After the PR branch is pushed, file links take the form
  `https://github.com/shivamsancc/integrations-extras/blob/add-freeswitch-integration/freeswitch/<path>`; do not add
  them to any document before that push.

## Generated or one-off artifacts

`lab/docs/SUBMISSION_DIFF.patch` is a manually saved snapshot taken before the extended test campaign and the README
connection section. It is **stale**: it lacks `freeswitch/tests/test_campaign.py` and the later spec, validator and
`conf.yaml.example` edits. It is not referenced by any document or script and contains no secrets or absolute paths.
It is kept unchanged as a historical record. The authoritative source is Git, so regenerate a patch at PR time instead
of maintaining one. From the `integrations-extras` checkout, after the contribution is committed on the PR branch:

```shell
git diff master...add-freeswitch-integration > /tmp/freeswitch-submission.patch
```

Before the commit exists, `git diff` does not show the untracked `freeswitch/` directory; use the committed diff above.
