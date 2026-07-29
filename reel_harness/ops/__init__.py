"""Operations tooling for running Reel Harness in production: preflight
diagnostics, config fingerprinting, DB/storage backup and verification,
the runtime supervisor, metrics, incident bundles, and live-verification
orchestration. Nothing in `reel_harness/pipeline`, `reel_harness/worker`,
or `reel_harness/providers` may import from here -- this package depends
on the rest of the application, never the other way around."""
