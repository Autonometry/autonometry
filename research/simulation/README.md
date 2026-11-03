# simulation/ — coverage study of the Autonomy Meter interval methods (Part 6a)

Lab-notebook record of a Monte Carlo study with KNOWN truth: a session-level generator of
robot-operation data (`simulate.py`), a driver that runs every interval method of the reference
package `autonomy_meter` v0.1.0 on simulated samples (`run_coverage.py`), a check of the
test-length planning rules (`run_planning_check.py`), an end-to-end pipeline smoke test through a
schema-valid JSONL event log (`smoke_test_pipeline.py`), and the figures (`make_figures.py`).
Every number below was produced by these scripts; the result tables are in `results/`.

## 1. What was run (exactly)

* Replicates: **1000 per cell** for every metric and method, 6 scenarios x 3 sample sizes (n = 5, 20, 80
  sessions) = 18 cells, 19 metric-method rows per replicate (`results/raw/<scenario>_n<n>.csv.gz`).
  Bootstrap intervals use **n_boot = 500**. No cell was reduced.
* Tasks per session: 4.47 on average (task median duration 300 s in 20-min sessions), so the
  task-rate sample sizes are about 22 / 89 / 357 tasks for n = 5 / 20 / 80 sessions (requested 20 / 100 / 400;
  realised values are in `coverage_summary.csv`, column `mean_n_tasks`).
* Seeds: base seed 20261030; cell seed = base + 1000 x scenario index + n index; replicate rng = `numpy.random.default_rng([cell_seed, rep])`;
  bootstrap seed = rep. Population truth: 40000 sessions per scenario, seed 20261807.
* Runtime: cells summed 40.4 CPU-min, run as 3 shard processes in parallel (about 13.5 min wall).
  `multiprocessing.Pool` was unavailable in the sandbox (named pipes denied), hence shards + `--summarise-only`.
* Software: numpy 2.4.6, pandas 2.3.3, scipy 1.17.1, statsmodels 0.15.0, lifelines 0.30.3.
* Speed-up with verified equivalence: the percentile cluster bootstrap for ratio-of-sums statistics was
  re-implemented vectorised (`simulate.boot_ratio_percentile`); with the same seed it reproduces
  `autonomy_meter.intervals.cluster_bootstrap(method='percentile')` exactly (max |diff| lo/hi =
  0.0e+00 / 0.0e+00). The package's bootstrap-t is reproduced exactly by
  `boot_mean_t_package_variant` (max |diff| 0.0e+00); see Section 6 for why a
  second, standard bootstrap-t (`boot_mean_t`) was also evaluated.
* Everything else calls the package functions directly: `poisson_exact_ci`, `nb_rate_ci`, `wilson_ci`,
  `clopper_pearson_ci`, `proportion_cluster_robust_ci`, `ratio_linearised_ci`, `wilson_effective_ci`,
  `dispersion_index`; gaps use lifelines `KaplanMeierFitter` + `median_survival_times` and `ExponentialFitter`
  as the package does.

## 2. Generator (`simulate.py`)

One session i (robot r, day d, calendar position c in [0, 1]):

| component | model | parameters used |
|---|---|---|
| active duration | LogNormal | median 1200 s, sigma 0.4 |
| rate multiplier | exp(b_r + b_d + e_i) x Gamma frailty x 2^(-h c) | b_r ~ N(0, 0.30^2), b_d ~ N(0, 0.30^2), e_i ~ N(0, 0.35^2) in (iv) only; Gamma(1/a, a) with a = 1.0 in (ii) only; h = 1 in (v) only |
| candidate interventions | Poisson process at lambda0 x multiplier, or Hawkes cluster process (branching ratio 0.6, exponential kernel, decay 60/h) in (iii) | lambda0 = 8 /active hour before task-type thinning and within-human-control thinning |
| tasks | sequential partition of the session, LogNormal durations | median 300 s, sigma 0.5; three task types with probabilities 0.5/0.35/0.15 and rate multipliers 0.7/1.0/2.0 (weighted mean 1) |
| event kinds | takeover 0.7, approval 0.2, hint 0.1 | takeover dwell LogNormal(median 40 s, sigma 0.7) x exp(0.5 b_r) in (iv), clipped at task end; approval dwell LogNormal(3 s, 0.5); hint 2 s |
| outcome | Bernoulli | p_fail by type 0.05/0.15/0.30 for autonomous tasks, 0.30/0.40/0.50 after an intervention |
| unknown time (vi) | random windows (MCAR) + dropout windows starting 0.5 s before a takeover (MNAR) | random target 6 % with Exponential(60 s) lengths; dropout probability 0.55 per takeover; realised unknown share 0.098 |

A candidate event during an ongoing human-control interval is dropped (the human already has control;
the loader would not count a takeover either). The informative dropout hides the takeover transition, so
the loader resumes the last known state (AI) after the gap and the remaining human time is
misattributed to AI — the generator reproduces this loader behaviour (verified session by session in the smoke test).

Truth = population value from a 40000-session draw of the hidden (complete) process; robot and day effects
are redrawn in every replicate, so the truth is the superpopulation value (a new deployment has new robots and days).
The gap-median truth is the Kaplan-Meier median of the population gap distribution (what the KM estimator converges to), and
ln2/rate is listed beside it to show the exponential model's identification bias under heterogeneity.

| scenario | share | share incl. approval dwell | rate /h | task rate | human min/task | gap median (h) | ln2/rate (h) | dispersion index | events/session | tasks/session | unknown share |
|---|---|---|---|---|---|---|---|---|---|---|---|
| i_baseline_poisson | 0.9368 | 0.9349 | 7.345 | 0.5747 | 0.3159 | 0.0935 | 0.0944 | 0.99 | 2.65 | 4.46 | 0.000 |
| ii_nb_overdispersion | 0.9406 | 0.9389 | 6.931 | 0.6476 | 0.2966 | 0.0768 | 0.1000 | 2.88 | 2.51 | 4.48 | 0.000 |
| iii_hawkes_bursty | 0.9497 | 0.9482 | 5.884 | 0.7042 | 0.2514 | 0.1024 | 0.1178 | 2.05 | 2.12 | 4.46 | 0.000 |
| iv_robot_day_clustering | 0.9303 | 0.9283 | 7.950 | 0.5721 | 0.3481 | 0.0790 | 0.0872 | 1.67 | 2.88 | 4.47 | 0.000 |
| v_drift_halving | 0.9529 | 0.9515 | 5.431 | 0.6626 | 0.2352 | 0.1248 | 0.1276 | 1.07 | 1.96 | 4.46 | 0.000 |
| vi_unknown_10pct | 0.9366 | 0.9347 | 7.386 | 0.5724 | 0.3169 | 0.0929 | 0.0939 | 0.98 | 2.67 | 4.47 | 0.098 |

Monte Carlo SE of the truths (chunk means): share <= 0.0024, rate <= 0.180 /h, task rate <= 0.0074, human min/task <= 0.0119
(largest in (iv) because the robot/day effects add between-chunk variance).
Realised dispersion indices: (ii) 2.88 (target ~3), (iii) 2.05, (iv) 1.67; log-rate ICC in (iv) = 0.30 for robot and 0.30 for day by construction
(0.09 / (0.09 + 0.09 + 0.1225)).
The time share in the generator counts takeover control time as human; the package's default variant (a) additionally
moves approval/hint dwell into human time, which lowers the truth by about 0.002 (column 2 vs 3). The coverage study
uses the generator definition consistently for estimator and truth; the smoke test compares both definitions.

## 3. Coverage results (95 % nominal; Monte Carlo SE <= 0.016 at coverage 0.95, 1000 replicates)

Figures: `figures/coverage_vs_n.png` (coverage vs n, one row per metric, one column per scenario, error bars = 95 % MC interval)
and `figures/width_vs_n.png` (mean width, log scale). Full table: `results/coverage_summary.csv`
(bias, relative bias, SD of the estimate, coverage, MC SE, miss-below/above, mean and median width, fraction undefined/infinite,
coverage of the observed-process limit). Ranking across scenarios (i)-(v): `results/method_ranking.csv`.

### (a) time autonomy share
| method | i n=5 | i n=20 | i n=80 | ii n=5 | ii n=20 | ii n=80 | iii n=5 | iii n=20 | iii n=80 | iv n=5 | iv n=20 | iv n=80 | v n=5 | v n=20 | v n=80 | vi n=5 | vi n=20 | vi n=80 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| boot_pct_robot | 0.783 | 0.791 | 0.788 | 0.769 | 0.798 | 0.800 | 0.757 | 0.803 | 0.800 | 0.746 | 0.740 | 0.806 | 0.761 | 0.804 | 0.816 | 0.363 | 0.060 | 0.000 |
| boot_pct_session | 0.809 | 0.916 | 0.925 | 0.786 | 0.896 | 0.953 | 0.793 | 0.897 | 0.939 | 0.758 | 0.770 | 0.673 | 0.793 | 0.915 | 0.951 | 0.381 | 0.087 | 0.000 |
| lin_t_robot | 0.913 | 0.950 | 0.940 | 0.885 | 0.937 | 0.952 | 0.881 | 0.933 | 0.952 | 0.885 | 0.903 | 0.953 | 0.889 | 0.947 | 0.954 | 0.547 | 0.249 | 0.007 |
| lin_t_session | 0.914 | 0.935 | 0.943 | 0.868 | 0.919 | 0.957 | 0.867 | 0.917 | 0.951 | 0.879 | 0.813 | 0.692 | 0.899 | 0.942 | 0.947 | 0.501 | 0.091 | 0.000 |
| outer_bounds_boot | 0.809 | 0.916 | 0.925 | 0.786 | 0.896 | 0.953 | 0.793 | 0.897 | 0.939 | 0.758 | 0.770 | 0.673 | 0.793 | 0.915 | 0.951 | 0.992 | 1.000 | 1.000 |
| wilson_eff_n | 0.996 | 1.000 | 1.000 | 0.966 | 0.978 | 0.992 | 0.991 | 0.997 | 0.999 | 0.947 | 0.939 | 0.865 | 0.996 | 1.000 | 1.000 | 1.000 | 1.000 | 0.330 |

### (c) interventions per active hour
| method | i n=5 | i n=20 | i n=80 | ii n=5 | ii n=20 | ii n=80 | iii n=5 | iii n=20 | iii n=80 | iv n=5 | iv n=20 | iv n=80 | v n=5 | v n=20 | v n=80 | vi n=5 | vi n=20 | vi n=80 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| boot_pct_robot | 0.774 | 0.777 | 0.801 | 0.755 | 0.794 | 0.791 | 0.787 | 0.805 | 0.789 | 0.694 | 0.727 | 0.786 | 0.792 | 0.812 | 0.798 | 0.495 | 0.103 | 0.001 |
| boot_pct_session | 0.825 | 0.905 | 0.949 | 0.794 | 0.912 | 0.943 | 0.817 | 0.912 | 0.932 | 0.713 | 0.799 | 0.682 | 0.839 | 0.927 | 0.939 | 0.500 | 0.148 | 0.000 |
| garwood_poisson | 0.953 | 0.947 | 0.956 | 0.771 | 0.740 | 0.716 | 0.867 | 0.845 | 0.834 | 0.812 | 0.723 | 0.580 | 0.969 | 0.951 | 0.944 | 0.778 | 0.212 | 0.000 |
| nb2_wald | 0.958 | 0.946 | 0.958 | 0.889 | 0.935 | 0.954 | 0.931 | 0.928 | 0.942 | 0.862 | 0.809 | 0.677 | 0.967 | 0.963 | 0.953 | 0.848 | 0.268 | 0.000 |
| package_default_rule | 0.959 | 0.947 | 0.956 | 0.864 | 0.910 | 0.943 | 0.915 | 0.913 | 0.929 | 0.852 | 0.803 | 0.663 | 0.969 | 0.952 | 0.945 | 0.770 | 0.217 | 0.000 |

### (b) task autonomy rate
| method | i n=5 | i n=20 | i n=80 | ii n=5 | ii n=20 | ii n=80 | iii n=5 | iii n=20 | iii n=80 | iv n=5 | iv n=20 | iv n=80 | v n=5 | v n=20 | v n=80 | vi n=5 | vi n=20 | vi n=80 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| clopper_pearson | 0.966 | 0.955 | 0.959 | 0.878 | 0.874 | 0.863 | 0.957 | 0.946 | 0.937 | 0.891 | 0.838 | 0.690 | 0.972 | 0.951 | 0.953 | 0.753 | 0.157 | 0.000 |
| cluster_glm_session | 0.874 | 0.928 | 0.944 | 0.831 | 0.924 | 0.952 | 0.879 | 0.939 | 0.942 | 0.805 | 0.837 | 0.733 | 0.881 | 0.920 | 0.940 | 0.560 | 0.104 | 0.000 |
| wilson | 0.952 | 0.945 | 0.957 | 0.836 | 0.854 | 0.853 | 0.934 | 0.941 | 0.931 | 0.851 | 0.815 | 0.673 | 0.960 | 0.942 | 0.943 | 0.700 | 0.137 | 0.000 |

### (e) human minutes per task
| method | i n=5 | i n=20 | i n=80 | ii n=5 | ii n=20 | ii n=80 | iii n=5 | iii n=20 | iii n=80 | iv n=5 | iv n=20 | iv n=80 | v n=5 | v n=20 | v n=80 | vi n=5 | vi n=20 | vi n=80 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| boot_pct_session | 0.803 | 0.912 | 0.933 | 0.776 | 0.902 | 0.954 | 0.797 | 0.903 | 0.942 | 0.760 | 0.780 | 0.686 | 0.797 | 0.910 | 0.946 | 0.332 | 0.046 | 0.000 |
| boot_t_session | 0.925 | 0.951 | 0.943 | 0.901 | 0.942 | 0.964 | 0.898 | 0.943 | 0.951 | 0.912 | 0.837 | 0.699 | 0.931 | 0.945 | 0.956 | 0.744 | 0.227 | 0.000 |
| boot_t_session_pkg_v010 | 0.877 | 0.856 | 0.830 | 0.856 | 0.853 | 0.854 | 0.860 | 0.868 | 0.852 | 0.848 | 0.708 | 0.541 | 0.882 | 0.860 | 0.862 | 0.695 | 0.135 | 0.000 |

### (d) median gap between interventions
| method | i n=5 | i n=20 | i n=80 | ii n=5 | ii n=20 | ii n=80 | iii n=5 | iii n=20 | iii n=80 | iv n=5 | iv n=20 | iv n=80 | v n=5 | v n=20 | v n=80 | vi n=5 | vi n=20 | vi n=80 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| exponential_wald | 0.922 | 0.940 | 0.955 | 0.932 | 0.580 | 0.179 | 0.926 | 0.847 | 0.547 | 0.869 | 0.735 | 0.530 | 0.936 | 0.959 | 0.948 | 0.997 | 0.384 | 0.000 |
| km_exp_greenwood | 0.949 | 0.949 | 0.952 | 0.870 | 0.809 | 0.809 | 0.927 | 0.892 | 0.885 | 0.882 | 0.796 | 0.670 | 0.956 | 0.947 | 0.944 | 0.947 | 0.535 | 0.009 |

Supplement (`results/supplement_robot_level.csv`, same datasets): intervals with the robot as the cluster unit.

| metric | method | scenario | n | bias | coverage | MC SE | mean width |
|---|---|---|---|---|---|---|---|
| rate | lin_t_robot | i_baseline_poisson | 5 | 0.092 | 0.935 | 0.008 | 11.5 |
| hmpt | lin_t_robot | i_baseline_poisson | 5 | 0.008 | 0.916 | 0.009 | 0.707 |
| task_rate | cluster_glm_robot | i_baseline_poisson | 5 | -0.006 | 0.837 | 0.012 | 0.369 |
| rate | lin_t_robot | i_baseline_poisson | 20 | 0.029 | 0.937 | 0.008 | 5.84 |
| hmpt | lin_t_robot | i_baseline_poisson | 20 | 0.000 | 0.939 | 0.008 | 0.376 |
| task_rate | cluster_glm_robot | i_baseline_poisson | 20 | -0.002 | 0.846 | 0.011 | 0.186 |
| rate | lin_t_robot | i_baseline_poisson | 80 | 0.047 | 0.952 | 0.007 | 2.94 |
| hmpt | lin_t_robot | i_baseline_poisson | 80 | 0.001 | 0.930 | 0.008 | 0.184 |
| task_rate | cluster_glm_robot | i_baseline_poisson | 80 | -0.002 | 0.849 | 0.011 | 0.0932 |
| rate | lin_t_robot | iv_robot_day_clustering | 5 | 0.480 | 0.889 | 0.010 | 14.9 |
| hmpt | lin_t_robot | iv_robot_day_clustering | 5 | 0.033 | 0.889 | 0.010 | 0.925 |
| task_rate | cluster_glm_robot | iv_robot_day_clustering | 5 | -0.019 | 0.762 | 0.013 | 0.406 |
| rate | lin_t_robot | iv_robot_day_clustering | 20 | 0.282 | 0.900 | 0.009 | 9.39 |
| hmpt | lin_t_robot | iv_robot_day_clustering | 20 | 0.014 | 0.899 | 0.010 | 0.585 |
| task_rate | cluster_glm_robot | iv_robot_day_clustering | 20 | -0.009 | 0.807 | 0.012 | 0.242 |
| rate | lin_t_robot | iv_robot_day_clustering | 80 | 0.316 | 0.945 | 0.007 | 7.17 |
| hmpt | lin_t_robot | iv_robot_day_clustering | 80 | 0.020 | 0.952 | 0.007 | 0.454 |
| task_rate | cluster_glm_robot | iv_robot_day_clustering | 80 | -0.011 | 0.851 | 0.011 | 0.175 |

### Mean 95 % interval widths (finite intervals)
Time share:
| method | i n=5 | i n=20 | i n=80 | ii n=5 | ii n=20 | ii n=80 | iii n=5 | iii n=20 | iii n=80 | iv n=5 | iv n=20 | iv n=80 | v n=5 | v n=20 | v n=80 | vi n=5 | vi n=20 | vi n=80 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| boot_pct_robot | 0.0761 | 0.0388 | 0.0191 | 0.0988 | 0.053 | 0.0259 | 0.0775 | 0.04 | 0.0203 | 0.0997 | 0.0614 | 0.0473 | 0.0667 | 0.0335 | 0.0169 | 0.0529 | 0.0264 | 0.0136 |
| boot_pct_session | 0.079 | 0.0462 | 0.0239 | 0.101 | 0.0626 | 0.0333 | 0.0788 | 0.0484 | 0.0252 | 0.0998 | 0.059 | 0.0313 | 0.0684 | 0.0402 | 0.0211 | 0.0531 | 0.0319 | 0.0171 |
| lin_t_robot | 0.135 | 0.0738 | 0.0365 | 0.177 | 0.101 | 0.0498 | 0.139 | 0.0762 | 0.0388 | 0.179 | 0.117 | 0.0908 | 0.118 | 0.0634 | 0.0323 | 0.0953 | 0.05 | 0.026 |
| lin_t_session | 0.125 | 0.0509 | 0.0246 | 0.162 | 0.0695 | 0.0343 | 0.126 | 0.0536 | 0.026 | 0.159 | 0.0653 | 0.0324 | 0.108 | 0.0444 | 0.0218 | 0.0857 | 0.0355 | 0.0176 |
| outer_bounds_boot | 0.079 | 0.0462 | 0.0239 | 0.101 | 0.0626 | 0.0333 | 0.0788 | 0.0484 | 0.0252 | 0.0998 | 0.059 | 0.0313 | 0.0684 | 0.0402 | 0.0211 | 0.189 | 0.15 | 0.125 |
| wilson_eff_n | 0.225 | 0.102 | 0.0497 | 0.235 | 0.103 | 0.0495 | 0.24 | 0.103 | 0.0489 | 0.227 | 0.103 | 0.0504 | 0.241 | 0.103 | 0.0488 | 0.287 | 0.111 | 0.0484 |

Interventions per hour:
| method | i n=5 | i n=20 | i n=80 | ii n=5 | ii n=20 | ii n=80 | iii n=5 | iii n=20 | iii n=80 | iv n=5 | iv n=20 | iv n=80 | v n=5 | v n=20 | v n=80 | vi n=5 | vi n=20 | vi n=80 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| boot_pct_robot | 6.52 | 3.08 | 1.54 | 10.2 | 5.31 | 2.75 | 8.11 | 3.99 | 1.99 | 8.32 | 4.93 | 3.75 | 5.72 | 2.79 | 1.39 | 5.67 | 2.55 | 1.29 |
| boot_pct_session | 6.7 | 3.74 | 1.93 | 10.3 | 6.31 | 3.41 | 8.17 | 4.74 | 2.47 | 8.45 | 5.11 | 2.73 | 5.88 | 3.35 | 1.73 | 5.67 | 3.16 | 1.63 |
| garwood_poisson | 8.65 | 4.12 | 2.02 | 8.19 | 3.97 | 1.96 | 7.7 | 3.68 | 1.8 | 9.09 | 4.32 | 2.13 | 7.5 | 3.56 | 1.74 | 7.42 | 3.47 | 1.69 |
| nb2_wald | 9.52 | 4.28 | 2.05 | 16.3 | 6.92 | 3.47 | 11.6 | 5.1 | 2.52 | 11.3 | 5.39 | 2.73 | 8.51 | 3.76 | 1.8 | 9.82 | 3.64 | 1.72 |
| package_default_rule | 8.94 | 4.16 | 2.02 | 10.9 | 6.28 | 3.41 | 9.1 | 4.73 | 2.45 | 10 | 5.14 | 2.62 | 7.75 | 3.64 | 1.74 | 7.65 | 3.53 | 1.69 |

## 4. Recommended defaults (method keeping coverage nearest 95 % across scenarios (i)-(v))

| metric | recommended default | evidence (coverage) | keep as secondary |
|---|---|---|---|
| (a) time share | **ratio-linearised t over sessions** (`ratio_linearised_ci`); when sessions nest in >= 3 robots, also report the linearised t over robots | session t: 0.914-0.943 in (i), 0.957 in (ii) n=80; percentile bootstrap 0.809 at n=5 in (i) and 0.786 in (ii); robot t 0.953 in (iv) n=80 where every session-level method is at 0.673-0.692 | cluster bootstrap percentile (n >= 20 only); Wilson effective-n only as the single-session fallback (over-covers at 0.996-1.000 under iid sessions, 1.9-2.5x wider, and drops to 0.865 under clustering) |
| (c) interventions / hour | **NB2 Wald** (`nb_rate_ci`, with its Poisson-exact fallback at the zero dispersion boundary) | (i) 0.946-0.958; (ii) 0.889/0.935/0.954; (iii) 0.942; Garwood 0.716 in (ii) and 0.834 in (iii) at n=80 | the package's dispersion switch (Garwood if index <= 1.5 else bootstrap) is slightly worse (0.864 in (ii) n=5) because the percentile bootstrap under-covers at small n (0.794); switching to NB2 instead of bootstrap fixes it |
| (b) task autonomy rate | **cluster-robust GLM (session clusters) when n >= 20 sessions; Wilson below** | GLM 0.924/0.952 in (ii) n=20/80 vs Wilson 0.854/0.853; at n=5 GLM 0.874 vs Wilson 0.952 | Clopper-Pearson (slightly conservative under (i), 0.966, but fails under heterogeneity like Wilson: 0.863 in (ii) n=80) |
| (e) human minutes / task | **cluster bootstrap-t with the closed-form cluster-robust SE, standard construction** (`boot_mean_t`) | 0.925-0.943 in (i), 0.964 in (ii) n=80; percentile 0.803 at n=5 | percentile bootstrap at n >= 20 (narrower, 0.90-0.95) |
| (d) gap median | **Kaplan-Meier median with exponential-Greenwood CI**, labelled descriptive | 0.949-0.952 in (i), 0.944 in (v); 0.809 in (ii) and 0.885 in (iii) | exponential-fit CI only as a model check: 0.179 in (ii) and 0.547 in (iii) at n=80 (the pooled gap distribution is not exponential) |

## 5. Where coverage fails

1. **Over-dispersion (ii) and bursts (iii).** Poisson exact (Garwood) covers 0.716 in (ii) and 0.834 in (iii) at n = 80
   and does not improve with n. Wilson/Clopper-Pearson for the task rate sit at 0.85-0.87 in (ii) for every n. The
   exponential gap model collapses (0.179 in (ii), n = 80) because its target ln2/rate = 0.1000 h differs from the
   population KM median 0.0768 h. The KM median interval also under-covers (0.809 in (ii), 0.885 in (iii))
   because gaps within a session are treated as independent.
2. **Small n.** Every percentile bootstrap under-covers at n = 5 (0.76-0.83); the cluster-robust GLM with 5 clusters covers 0.83-0.88.
   Closed-form t intervals (linearised t, bootstrap-t, NB2 Wald) are the small-n choices.
3. **Robot/day clustering (iv).** With sessions nested in 4 robots and ~n/4 days and the truth defined over new robots/days,
   all session-level intervals *lose* coverage as n grows (0.673-0.733 at n = 80): more sessions on the same 4 robots do not
   average out the robot effects. Only robot-level intervals hold (0.953 share, 0.945 rate, 0.952 human min/task at n = 80;
   the robot-cluster GLM for the task rate stays at 0.851 because 4 clusters are too few for the sandwich estimator), at 1.5-3x the width.
   The percentile bootstrap over 4 robots is useless (0.69-0.81). Implication for the standard: the reporting unit for uncertainty must be the
   highest level at which the claim generalises (robot or deployment), not the session, and with <= 4 such units a t interval with G-1 degrees of
   freedom is the only method here that did not fail.
4. **Drift (v).** No method is harmed: sessions sampled uniformly over the period estimate the period-average rate without bias
   (relative bias of the rate at n = 80: +0.002); the drift metric (g) is the right place to show the trend.
5. **Unknown time with informative dropout (vi).** All sampling intervals cover the *observed-process* limit at nominal rates but miss the
   truth: the hidden takeovers bias the share up by +3.7 %, the rate by -37 %, the task rate by +27 % and human minutes per task by -58 %.
   Only the bounds interval (unknown counted as human / as AI, then bootstrapped) covers the share truth (1.000). The standard must require the bounds,
   and the rate / human-minutes metrics have no upper bound when events can be hidden — this is a limit to state, not an interval to compute.

| metric | method | truth | observed-process limit | rel. bias | coverage of truth | coverage of observed limit |
|---|---|---|---|---|---|---|
| share | boot_pct_session | 0.937 | 0.971 | +0.037 | 0.000 | 0.932 |
| share | lin_t_session | 0.937 | 0.971 | +0.037 | 0.000 | 0.937 |
| share | wilson_eff_n | 0.937 | 0.971 | +0.037 | 0.330 | 1.000 |
| share | boot_pct_robot | 0.937 | 0.971 | +0.037 | 0.000 | 0.792 |
| share | lin_t_robot | 0.937 | 0.971 | +0.037 | 0.007 | 0.946 |
| share | outer_bounds_boot | 0.937 | 0.971 | +0.037 | 1.000 | 0.999 |
| rate | garwood_poisson | 7.386 | 4.625 | -0.372 | 0.000 | 0.946 |
| rate | nb2_wald | 7.386 | 4.625 | -0.372 | 0.000 | 0.950 |
| rate | boot_pct_session | 7.386 | 4.625 | -0.372 | 0.000 | 0.940 |
| rate | boot_pct_robot | 7.386 | 4.625 | -0.372 | 0.001 | 0.789 |
| rate | package_default_rule | 7.386 | 4.625 | -0.372 | 0.000 | 0.946 |
| task_rate | wilson | 0.572 | 0.727 | +0.269 | 0.000 | 0.939 |
| task_rate | clopper_pearson | 0.572 | 0.727 | +0.269 | 0.000 | 0.948 |
| task_rate | cluster_glm_session | 0.572 | 0.727 | +0.269 | 0.000 | 0.934 |
| hmpt | boot_pct_session | 0.317 | 0.133 | -0.580 | 0.000 | 0.928 |
| hmpt | boot_t_session | 0.317 | 0.133 | -0.580 | 0.000 | 0.939 |
| hmpt | boot_t_session_pkg_v010 | 0.317 | 0.133 | -0.580 | 0.000 | 0.848 |
| gap_median | km_exp_greenwood | 0.093 | 0.093 | +0.619 | 0.009 | 0.009 |
| gap_median | exponential_wald | 0.093 | 0.093 | +0.623 | 0.000 | 0.000 |

## 6. Issues found in `autonomy_meter` v0.1.0 (to feed back to the reference package)

1. `intervals.cluster_bootstrap(method='bootstrap_t')`: inside each resample the studentiser groups rows by the original
   `session_id`, so a session drawn k times is merged into one group (G_unique instead of G clusters). Coverage of the
   resulting interval is 0.830 (i, n = 80) and 0.854 (ii, n = 80) versus 0.943 / 0.964 for the standard construction
   (duplicates kept as distinct clusters). Fix: tag resampled clusters with the draw index before grouping.
2. `metrics.gaps_between_interventions`: `ExponentialFitter.confidence_interval_` is the confidence band of the survival function, not the
   interval of `lambda_`; the reported `mean_gap_ci` is therefore wrong. Use `fitter.summary` (`coef lower/upper 95%`) as done here.
3. `metrics.interventions_per_hour`: the dispersion switch "Garwood if index <= 1.5 else cluster bootstrap" is dominated by NB2 Wald
   (Section 4), mainly because the percentile bootstrap under-covers at n <= 20.

## 7. Planning rule check (`results/planning_zero_event_check.csv`)

True rate exactly at the threshold lambda_max = 0.1 /h; a zero count after T hours leads to the claim "rate < 0.1 /h at 95 %",
which is wrong at the threshold, so P(zero count) is the false-claim rate (nominal 0.05). 20,000 runs per cell, seed 20261030.
Poisson design T = `hours_for_zero_events(0.1)` = 29.96 h; NB design T = `hours_for_zero_events_nb(0.1, a)`.

| NB dispersion a | design | hours | shared frailty (1 unit) | 3 robots | 10 robots | iid 1 h sessions |
|---|---|---|---|---|---|---|
| 0.3 | nb_rule | 48.5 | 0.050 | 0.021 | 0.010 | 0.008 |
| 0.3 | poisson_rule_of_three | 30.0 | 0.120 | 0.072 | 0.057 | 0.054 |
| 0.5 | nb_rule | 69.4 | 0.049 | 0.010 | 0.003 | 0.001 |
| 0.5 | poisson_rule_of_three | 30.0 | 0.157 | 0.087 | 0.062 | 0.054 |
| 1.0 | nb_rule | 190.0 | 0.052 | 0.003 | 0.000 | 0.000 |
| 1.0 | poisson_rule_of_three | 30.0 | 0.246 | 0.126 | 0.075 | 0.058 |

Reading: with iid 1-h sessions the rule of three is nearly right (0.052-0.058) because per-session over-dispersion barely matters
at 0.1 expected events per session. When the frailty is shared by the whole test (one robot, one deployment) the rule of three is wrong
2.4-5x more often than nominal (0.118 at a = 0.3, 0.160 at a = 0.5, 0.250 at a = 1.0). `hours_for_zero_events_nb` is exact for the shared-frailty case (0.049-0.053)
and conservative otherwise. The planning text should say which structure it assumes. (Monte Carlo agrees with the closed form
prod_r (1 + a lambda T/R)^(-1/a) to within 2 MC SE in every cell.)

## 8. Pipeline smoke test (`smoke_test_pipeline.py`)

12 simulated sessions per scenario rendered as JSONL (`results/smoke_log_<scenario>.jsonl`; 3703 and 2663 events,
5-s heartbeats, unknown windows rendered as silence), `validate_events` -> 0 schema errors in both logs, `build_tables(Params(heartbeat_interval_s=5, gap_tolerance_s=5))`,
`full_report(n_boot=500, seed=0)` -> `results/smoke_report_<scenario>.json`. Per-session `ai_s`, `human_s`, `active_s` and takeover counts from the
pipeline equal the generator's observed tables exactly in (i); in (vi) they agree to within the 5-s heartbeat quantisation of the unknown windows.

| scenario | metric | pipeline estimate | 95 % CI | generator estimate | truth | truth in CI |
|---|---|---|---|---|---|---|
| i | time_autonomy_share_pkg_default(approval_dwell_as_human) | 0.9547 | [0.9292, 0.9757] | 0.9547 | 0.9349 | True |
| i | time_autonomy_share_takeover_only | 0.9576 | [0.9309, 0.9790] | 0.9576 | 0.9368 | True |
| i | task_autonomy_rate | 0.5000 | [0.3711, 0.6289] | 0.5000 | 0.5747 | True |
| i | interventions_per_hour | 8.3949 | [5.9974, 11.4315] | 8.3949 | 7.3453 | True |
| i | human_minutes_per_task | 0.2400 | [0.1238, 0.3769] | 0.2400 | 0.3159 | True |
| i | gap_median_h | 0.0896 | [0.0618, 0.1210] | nan | 0.0935 | True |
| vi | time_autonomy_share_pkg_default(approval_dwell_as_human) | 0.9688 | [0.9415, 0.9944] | 0.9687 | 0.9347 | False |
| vi | time_autonomy_share_takeover_only | 0.9706 | [0.9436, 0.9953] | 0.9704 | 0.9366 | False |
| vi | task_autonomy_rate | 0.7708 | [0.6346, 0.8669] | 0.7708 | 0.5724 | False |
| vi | interventions_per_hour | 3.4869 | [1.8017, 6.0910] | 3.5010 | 7.3855 | False |
| vi | human_minutes_per_task | 0.1340 | [0.0236, 0.2522] | 0.1340 | 0.3169 | False |
| vi | gap_median_h | 0.1875 | [0.1300, 0.4364] | nan | 0.0929 | False |

Baseline: all five metrics recover the population truth inside the pipeline's intervals (one sample of 12 sessions; not a coverage statement).
Scenario (vi): the pipeline reproduces the generator's biased observed values and the sampling intervals miss the truth, while the
package `outer_interval` for the share, [0.789, 0.995], contains the truth 0.9347; conformance is reported as `incomplete`
(unknown share 0.131 > 0.05).

## 9. Files

* `simulate.py` — generator, population truth, vectorised bootstraps (+ equality check), JSONL adapter
* `run_coverage.py` — Monte Carlo driver (resumable per cell; `--summarise-only`), writes `results/raw/`, `results/coverage_summary.csv`, `results/truth.csv`, `results/run_manifest.json`
* `run_supplement_robot_level.py` — robot-level intervals on the same datasets -> `results/supplement_robot_level.csv`
* `run_planning_check.py` -> `results/planning_zero_event_check.csv`
* `smoke_test_pipeline.py` -> `results/smoke_log_*.jsonl`, `results/smoke_report_*.json`, `results/smoke_comparison.csv`
* `make_figures.py` -> `figures/coverage_vs_n.png`, `figures/width_vs_n.png`
* `results/method_ranking.csv` — mean |coverage - 0.95|, minimum coverage and relative width per method over scenarios (i)-(v)

## 10. Sources

Statistical constructions are standard; the only external claims used here are the ones below. Tags: VERIFIED = checked in this session by
running code; CLAIM = standard reference located by URL but the page was not re-read in this session; UNKNOWN = not checked.

* Behaviour of `autonomy_meter` v0.1.0 functions (bootstrap-t grouping, `ExponentialFitter.confidence_interval_` content, dispersion switch) — observed by running the package in this session [VERIFIED].
* Hawkes AG (1971) Spectra of some self-exciting and mutually exciting point processes. Biometrika 58(1):83-90. https://doi.org/10.1093/biomet/58.1.83 — the cluster (immigrant/offspring) construction used in `hawkes_times` [CLAIM].
* Garwood F (1936) Fiducial limits for the Poisson distribution. Biometrika 28:437-442. https://doi.org/10.1093/biomet/28.3-4.437 — Poisson exact interval, via `autonomy_meter.intervals.poisson_exact_ci` [CLAIM].
* Hanley JA, Lippman-Hand A (1983) If nothing goes wrong, is everything all right? JAMA 249(13):1743-1745. https://jamanetwork.com/journals/jama/article-abstract/385438 — rule of three [CLAIM].
* lifelines documentation, `KaplanMeierFitter` and `ExponentialFitter` (parameter CIs in `summary`; `confidence_interval_` = survival-function band). https://lifelines.readthedocs.io/en/latest/fitters/univariate/KaplanMeierFitter.html , https://lifelines.readthedocs.io/en/latest/fitters/univariate/ExponentialFitter.html — attribute semantics confirmed by inspecting the fitted objects here [VERIFIED by code; page text not re-read].
* statsmodels `proportion_confint` (Wilson, Clopper-Pearson), `GLM(cov_type='cluster')`, `NegativeBinomial(loglike_method='nb2')`. https://www.statsmodels.org/stable/generated/statsmodels.stats.proportion.proportion_confint.html — used through the package [CLAIM].
* Efron B, Tibshirani RJ (1993) An Introduction to the Bootstrap. Chapman & Hall. https://doi.org/10.1007/978-1-4899-4541-9 — percentile and bootstrap-t intervals [CLAIM].
