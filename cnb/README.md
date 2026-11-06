# Private image exporter identity

BuildKit may complete an image push and registry-cache export without writing the
optional `containerimage.config.digest` metadata key. This is an exporter metadata
variant, not evidence that the build failed or permission to omit image identity.

The builder preserves the existing explicit config-digest path. If that key is
absent, it reads only the exact pushed `containerimage.digest` reference, hashes
the raw OCI/Docker single-image manifest, validates its image-config descriptor,
then fetches and hashes the exact config blob from the fixed CNB registry. The
config must be Linux/amd64 and carry the selected source/version labels. Optional
descriptor annotations must agree; indices, cache descriptors, unknown media
types, conflicting metadata and mismatched bytes fail before a release receipt.
Schema 2/3 receipt fields and downstream receiver validation are unchanged.

The registry request uses the ephemeral Docker login in memory and a pull-only
scope for the selected image repository. Authentication stays in HTTP headers;
redirects are refused. Image/config bytes and private diagnostics must stay in
the private CNB job, never public Actions output or artifacts.

Offline coverage: `python3 -m unittest discover -s scripts -p 'test_cnb_*.py'`.
