# S2 · Hidden remote operator on r4 (days 6-10)

Trust level: L2 (L3 failed)  ·  active hours 74.5  ·  interventions 157

| Metric | Estimate [95% CI] | Method |
|---|---|---|
| Time autonomy share | 94.4% [93.4%, 95.4%] | ratio_linearised_t(robot) |
| Task autonomy rate | 90.1% [87.7%, 92.5%] of 1511 tasks | cluster_robust_glm |
| Interventions per active hour | 2.11 [1.64, 2.71] | nb2_wald |
| Human minutes per task | 0.07 [0.05, 0.09] | cluster bootstrap-t |
| Unmeasured time | 0.0% (complete) | bounds [94.4%, 94.4%] |

Ground truth (hidden from the meter): time autonomy 93.4%

## What the report suggests

1. Teleop station logged 46 min of remote control that the robots logged as AI (1.0% of active time, mostly on r4). Report downgraded from L3 to L2; witness-adjusted time autonomy is about 93.4%, not 94.4%.
2. r4 needs help 2.2x as often as the other robots (3.3/h vs 1.5/h). Check it first.
3. Planned bin refills are 56.4% of all human time. Automating them would lift time autonomy from 94.4% to about 97.6%.
4. Only 90.1% of tasks finished with zero human help, although 94.4% of time was AI. Buyers paying per task should use the task rate.
