# edu-agent releases

Public orchestration for `shukezhinao/edu-agent` and `wonderl17/edu-agent`.
Application code, build scripts, signing material and installation packages stay outside this repository.

## Run a release

Open **Actions → Release → Run workflow**, select the private source repository and operation, and optionally enter a complete source commit SHA. Only commits in that repository's `main` history are accepted. Leave `confirm_release=no` for `verify`; select `yes` explicitly for `cloud`, `desktop` or `resume`.

`cloud` preserves the existing `all`, `web`, `api` and `worker` targets and migration options. `desktop` builds and signs the existing macOS/Windows packages once, mirrors their original bytes to R2 and publishes to the established COS and private GitHub Release destinations. `resume` restores the original desktop packages without rebuilding.

Every 15 minutes, **Check source versions** checks the canonical `shukezhinao/edu-agent` main branch. Increasing the root package version queues a cloud release; increasing the desktop package version queues a desktop release. `wonderl17/edu-agent` remains available as a manual source to avoid duplicate releases to the same Production environment. The migration baseline is `0.0.121`; existing versions are not republished. GitHub can delay scheduled runs.

Deployment metadata records attempted versions. Failed attempts require an explicit manual retry; they are not retried every poll. Scheduled releases and manual releases share concurrency groups, including desktop recovery. Disabling the `RELEASES_ENABLED` repository variable stops automatic and product release runs while allowing verification.

## Privacy and credentials

- A dedicated GitHub App is installed only on the two private source repositories. Read tokens fetch source; write tokens are issued only for private GitHub Release operations and expire automatically.
- Only the public controller is checked out by an Actions checkout step. Private source is fetched into a temporary directory and verified against its origin and trusted main history.
- Private stdout/stderr, exception messages and GitHub file commands never enter public logs, including failed commands. Public logs show fixed phases and status codes. Private temporary logs and source are removed after each run. Diagnose cloud failures in the private Cloud Build/Cloud Run consoles; public logs intentionally contain no build details.
- No Actions artifacts or Actions caches are uploaded. Packages go directly to the existing authorized release destinations; private GitHub Releases are explicitly addressed to the source repository.
- GCP uses short-lived OIDC credentials restricted to this repository's main branch and exact release workflow. No GCP service-account key is stored here. Credentials are generated outside the private source upload directory.
- Production credentials live in the `production-release` environment, restricted to `main`. No PR-triggered privileged workflow exists. Official setup actions are pinned to immutable commits.

Changing trusted source code or this controller can change what it publishes. Keep repository write access limited and review workflow/script changes as production access changes.

## Checks

`python3 -m unittest discover -s scripts -p 'test_*.py' -v`

Tests include success/failure output sentinels, injected Actions commands, cancellation cleanup, trusted-source checks and scheduler de-duplication. `verify` checks live source access and configuration without building or deploying a product. Actual product publication is a separate explicitly confirmed operation.
