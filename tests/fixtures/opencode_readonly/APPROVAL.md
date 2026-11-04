# Baseline approval policy

Any change to `opencode.json` or `baseline.json` is a security-relevant change.

Required process:

1. Open a dedicated change with the old/new SHA-256 and a permission diff.
2. Review every `deny -> allow` change explicitly; deny rules must not be weakened
   without a separate security review.
3. Run `python3 -m unittest tests.test_opencode_baseline -v`.
4. Record `approved_by`, `approved_at`, `reason` in `baseline.json`.
5. Commit the config, baseline and approval note together. Do not amend or rewrite
   historical baseline commits.

The automated test checks the config hash and the final effect of every required
deny rule. It is a tripwire, not a substitute for human approval.
