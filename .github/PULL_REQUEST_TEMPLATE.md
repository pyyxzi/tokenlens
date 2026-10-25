## Summary

<!-- What does this PR change, and why? One or two sentences. -->

## Type of change

- [ ] Bug fix
- [ ] New data source adapter
- [ ] UI / UX
- [ ] Performance
- [ ] Docs / tooling
- [ ] Other

## Checklist

- [ ] `python -m unittest discover -s backend/tests` passes
- [ ] New parsing logic is covered by tests with **synthetic** log fixtures (no real personal logs)
- [ ] Cost/count semantics documented in [backend/tokenlens/SOURCES.md](../backend/tokenlens/SOURCES.md) if a data source's accounting changed
- [ ] No personal data (paths, keys, usage of my own machine) in the diff or fixtures

## Notes for reviewers

<!-- Anything subtle: dedup keys, cache-token normalization, cost kind (actual vs estimated), migration concerns. -->
