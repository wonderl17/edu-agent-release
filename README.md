# edu-agent releases

Public orchestration for `shukezhinao/edu-agent` and `wonderl17/edu-agent`.
Application code, private build scripts and signing material stay outside this repository. Verified desktop installers are also available as public GitHub Release assets.

## Run a release

Cloud Production uses **Actions → Tencent Production**, with one fixed Tencent host and the canonical `shukezhinao/edu-agent` source. Only full commits in canonical `main` history are accepted. The workflow builds all Web/API/Worker components as one verified package; partial targets are unsupported and must not be silently widened. Production Web uses `/api` and the baked `shukezhinao-prod` environment label.

- `verify` checks an already healthy target and trusted source without building or deploying.
- `stage` requires an exact `source_sha` and explicit confirmation. It builds, uploads and fully validates a private package before the source-data freeze. The receiver may load images and unpack static files, but never touches services, queues or databases, and never records a Cloud version attempt. A correctly configured empty target returns `bootstrapReady=true`, `healthy=false`; only staging accepts that state.
- `deploy` builds and activates the complete package on a healthy Production target.
- `resume` requires the exact source and prior stage/release `previous_transfer_ticket`. It asks the target to copy the original private object to a new ticket, skips build/upload, and repeats full hash, backup, migration and runtime verification. Initial cutover activation requires a separate root-controlled data-import gate; bootstrap readiness is never health evidence.

Mutating operations require `confirm_release=yes`, `RELEASES_ENABLED=true` and `TENCENT_PRODUCTION_ENABLED=true`. Production credentials live in the separate `tencent-production-release` environment: existing read-only source App access plus a dedicated, forced-command SSH key and pinned host key. No GCP identity, Desktop signing material or long-lived cloud storage key enters this job. Target/project, protocol, image namespace and COS relay destination are fixed in code. No local build fallback is used.

**Actions → Release** now accepts only `desktop` and Desktop `resume`, preserving macOS/Windows signing and original COS/R2/private GitHub Release destinations, plus verified public installer copies. Both private source repositories remain available for these Desktop operations. Old `verify`, `cloud` and `migration-bootstrap` inputs cannot enter this workflow, and its GCP authentication/setup steps are removed. Retired private Cloud/Legacy workflows remain disabled. The former GCP controller code is retained only as historical implementation, not a current entry point.

Every 15 minutes, **Check source versions** checks canonical `main`. Root-version growth dispatches only `tencent-production.yml`; Desktop-version growth still dispatches `release.yml`. Cloud automatic publication additionally requires `TENCENT_PRODUCTION_AUTOMATIC_ENABLED=true`, which remains closed during preparation/cutover without disabling Desktop. Cloud starts above baseline `0.0.125`; Desktop retains baseline `0.0.121`. Attempt metadata prevents repeating failed or already queued versions. Stage metadata uses a separate task and cannot advance the Cloud cursor. Explicit recovery is required after a failed attempt.

During cutover, cancel any old queued/running Cloud attempts before freezing the source. A merged workflow does not rewrite an already running historical revision. Do not re-enable old GCP workflows or modify Desktop/CLIProxyAPI as part of this Cloud move.

GitHub [disables public scheduled workflows after 60 days without repository activity](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule). After a successful poll, the scheduler uses this public repository's `GITHUB_TOKEN` to initialize `config/scheduler-activity.json` with the current UTC date, then updates that date through a normal maintenance commit only after at least 30 days. Future dates are preserved. This file contains only the maintenance date; no private source or secrets are committed. The check runs in this public repository on a standard runner and does not use private-repository minutes.

## Tencent validation

**Actions → Tencent validation** manages only the fixed Tencent validation host. `verify` checks access without building or deploying; `deploy` requires `confirm_release=yes` and an exact canonical `main` history commit (or resolves `main` once). It builds private source in isolated containers and uploads a checksum-bound package to a private, expiring COS relay using a one-hour presigned PUT URL. A dedicated forced-command SSH identity controls the release; the target downloads only its fixed ticket-bound object, captures a fresh recoverable snapshot, applies additive database migrations, and verifies the running API, workers and Web. Source and build logs are never published. Failure logs are bounded to 128 KiB and stay in a fixed root-only directory on the validation host; cancellation stops execution and skips diagnostic uploads.

Relay credentials remain on the target, restricted to its dedicated release prefix without list/delete permissions. The runner never receives COS access keys; URLs and response bodies are excluded from logs. Transfer tickets bind source, version, size and SHA-256, expire, and are consumed once. The private bucket uses managed encryption and one-day lifecycle expiry. The single-PUT transport refuses packages above 5 GiB instead of silently falling back to slow SSH payload streaming.

For an interrupted or rolled-back release, select `resume`, supply the exact `source_sha` and its `previous_transfer_ticket`, and confirm explicitly. The controller verifies live target health and trusted source history/version, then asks the target to copy the original private object to a fresh ticket. It skips rebuilding and uploading. The target still checks the complete package hash, captures a fresh backup and applies/verifies the release. Missing/expired original objects fail without rebuilding or switching source. Original objects are eligible for at most one day; deployment tickets remain single-use. Candidate static files receive explicit readable modes, including candidates retained after a failed activation.

The separate `TENCENT_VALIDATION_ENABLED` and `TENCENT_VALIDATION_AUTOMATIC_ENABLED` environment variables gate manual deployment and scheduled version checks. Its baseline is `0.0.125`; failed scheduled versions require manual retry. An explicitly selected manual deployment may change the source SHA at the current version, guarded by the expected running SHA; downgrades are refused. A verified current source/version is left running without a redundant rebuild. This workflow shares only the existing App source-authentication environment and requests a read-only token for `shukezhinao/edu-agent`. It has no GCP/OIDC permissions, uses a separate concurrency group and non-production deployment metadata, and does not change Production DNS or services.

## Privacy and credentials

- A dedicated GitHub App is installed only on the two private source repositories. Read tokens fetch source; write tokens are issued only for private GitHub Release operations and expire automatically.
- Only the public controller is checked out by an Actions checkout step. Private source is fetched into a temporary directory and verified against its origin and trusted main history.
- Private stdout/stderr, exception messages and GitHub file commands never enter public logs, including failed commands. Public logs show fixed phases and status codes. Private temporary logs and source are removed after each run. Diagnose current Cloud failures in the target’s root-only release receipts/logs; public logs intentionally contain no build details.
- Desktop subprocess checkpoints add only fixed stage IDs and `start`/`success`/`failure` states after private execution. The trusted controller validates the latest 128 records from an 8 KiB private temporary channel, then removes it. Arguments, paths, environment values, child output and exception text are never included; diagnostic I/O failures do not change the release result. Product source and package metadata are unchanged.
- No Actions artifacts or Actions caches are uploaded. Existing COS, R2 and private GitHub Release uploads continue. Only `ShukeZhinao-<version>-mac-arm64.pkg`, `-mac-x64.pkg` and `-win-x64.exe` are copied from the selected private Release to this repository’s public Release; size and SHA-256 must match before and after upload. Source archives, logs, source maps, update packages and private release notes are excluded. Identical assets are reused; different bytes under the same name are rejected. Public tags target the controller commit, never the private source commit. The public write token stays outside private execution. A failed private publication remains failed even if its saved installers can be copied; cancellation stops the copy.
- Current Cloud workflows do not authenticate to GCP. Retired GCP resources and backups remain recovery material; restarting them requires explicit rollback authorization.
- Desktop/App credentials live in `production-release`; Tencent Production uses `tencent-production-release`, both restricted to `main`. No PR-triggered privileged workflow exists. Official setup actions are pinned to immutable commits.

Changing trusted source code or this controller can change what it publishes. Keep repository write access limited and review workflow/script changes as production access changes.

## Checks

`python3 -m unittest discover -s scripts -p 'test_*.py' -v`

Tests include success/failure output sentinels, injected Actions commands, cancellation cleanup, trusted-source checks and scheduler de-duplication. `Tencent Production / verify` requires live target health and checks source access without building or deploying a product. Actual product publication is a separate explicitly confirmed operation.
