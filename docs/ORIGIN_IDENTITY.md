# Origin identity for evidence links

This refines the read-only evidence boundary in [the design](DESIGN.md). Exported source
links target `github.com`; a matching owner/name suffix on another host is not that source.

Supported origins are HTTPS on `github.com` (default port or 443),
`ssh://git@github.com` (default port or 22), and `git@github.com:owner/name`.
An optional `.git` suffix or trailing slash is accepted. The full path must be exactly
the declared owner/name, compared case-insensitively.

Other hosts, local paths, SSH aliases, extra path segments and query/fragment URLs
remain unresolved, even when their last two segments match. Unsupported origins are
not copied into diagnostics, because a URL can contain credentials.

This verifies local origin configuration, not remote authenticity or freshness.
The checker still never fetches or authenticates a remote server. Existing checked-data
and Git environment isolation rules remain in force; a local origin/main ref can be stale.

`tests/test_check_origin_identity.py` exercises the actual checker with disposable real
Git repositories and fictional identities. It includes supported-form controls, wrong-host
and path-confusion controls, and sanitized diagnostics. No network or owner checkout is used.
