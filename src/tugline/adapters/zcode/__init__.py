"""zcode adapter: inline hook entry, installer, and bounded approve module.

The zcode harness runs hooks inline, so this package is built around a
few hard rules from the live probe:

- the hook entry always exits 0, fast, and keeps stdout empty unless the
  approve path prints a permission decision
- payload keys arrive in two spellings and are normalized by the core
  payload module before anything else sees them
- the installer edits ~/.zcode/cli/config.json by exact-text splicing so
  every byte that was there before is still there after
"""

MARKER = "tugline:zcode"
