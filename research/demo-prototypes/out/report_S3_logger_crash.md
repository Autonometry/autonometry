# S3 · Logger crashes on r2 (days 4-5)

Trust level: L2 with gaps  ·  active hours 72.1  ·  interventions 151

| Metric | Estimate [95% CI] | Method |
|---|---|---|
| Time autonomy share | 94.4% [93.2%, 95.6%] | ratio_linearised_t(robot) |
| Task autonomy rate | 90.1% [87.6%, 92.5%] of 1463 tasks | cluster_robust_glm |
| Interventions per active hour | 2.10 [1.62, 2.69] | nb2_wald |
| Human minutes per task | 0.07 [0.05, 0.09] | cluster bootstrap-t |
| Unmeasured time | 3.1% (complete) | bounds [91.3%, 94.6%] |

Ground truth (hidden from the meter): time autonomy 93.8%

## What the report suggests

1. The meter could not see 3.1% of the time. True time autonomy is somewhere between 91.3% (all gaps human) and 94.6% (all gaps AI); quote the outer interval [81.8%, 96.1%].
2. r4 needs help 2.0x as often as the other robots (3.0/h vs 1.5/h). Check it first.
3. Planned bin refills are 57.2% of all human time. Automating them would lift time autonomy from 94.4% to about 97.6%.
4. Only 90.1% of tasks finished with zero human help, although 94.4% of time was AI. Buyers paying per task should use the task rate.
5. Robot r2 alone: The meter could not see 12.4% of the time on r2. True time autonomy is somewhere between 82.4% (all gaps human) and 95.7% (all gaps AI); quote the outer interval [63.2%, 97.2%].
6. Robot r2 alone: Above the 5 % unknown limit: the report is INCOMPLETE. Fix the logger before publishing any autonomy number.
