# Vendored: day-trader-skill

`vendor/day-trader-skill/` is an **unchanged** copy of
https://github.com/Jakeschincariol/day-trader-skill (MIT License, (c) Jake Schincariol),
commit `3d0f4ef6986f4984b7c6bd46f7ff4f87744c3abd` (2026-10-20).

- No file in `vendor/` has been edited. Its own test suite passes as copied
  (`cd vendor/day-trader-skill && python3 -m unittest discover -s tests`: 315 tests OK).
- The wrapper in `signals/` runs these scripts as they are and reads their `--json` output.
- The license is kept at `vendor/day-trader-skill/LICENSE`.

To update: replace the folder with a newer copy, record the new commit here, and
re-run both test suites. The NYSE holiday calendar in the original scanner covers
2026-2027 only; the wrapper stops with a clear message in 2028 until it is updated.
