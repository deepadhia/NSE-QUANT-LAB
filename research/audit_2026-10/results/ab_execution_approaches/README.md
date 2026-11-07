# NOT AUTHORITATIVE (2026-11-07)

These files come from `ab_execution_approaches.py`, which uses its own copy of the engine loop. Arm 0 (baseline) is valid (real engine); Arms 1-3 are not: the same modes run through the real engine give 21.43 / 21.31 / 20.50 (Approach 3), not 22.06 / 22.06 / 22.18. See `docs/RESULTS_REGISTER.md` R11-R19 and `../head_2026-11-07/summary_all.csv`.
