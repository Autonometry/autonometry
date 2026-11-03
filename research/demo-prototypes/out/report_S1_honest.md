# S1 · Honest pilot (4 arms, 10 days)

Trust level: L3 passed  ·  active hours 74.5  ·  interventions 153

| Metric | Estimate [95% CI] | Method |
|---|---|---|
| Time autonomy share | 94.4% [93.3%, 95.5%] | ratio_linearised_t(robot) |
| Task autonomy rate | 90.3% [87.9%, 92.7%] of 1515 tasks | cluster_robust_glm |
| Interventions per active hour | 2.05 [1.59, 2.65] | nb2_wald |
| Human minutes per task | 0.07 [0.05, 0.09] | cluster bootstrap-t |
| Unmeasured time | 0.0% (complete) | bounds [94.4%, 94.4%] |

Ground truth (hidden from the meter): time autonomy 94.4%

## What the report suggests

1. For this pilot the time share is 94.4% [93.8%, 95.0%] (40 sessions). For a claim about the robot model only 4 robots back it: [93.3%, 95.5%]. Add robots, not hours, to tighten it.
2. r4 needs help 2.0x as often as the other robots (3.0/h vs 1.5/h). Check it first.
3. Planned bin refills are 57.5% of all human time. Automating them would lift time autonomy from 94.4% to about 97.6%.
4. Only 90.3% of tasks finished with zero human help, although 94.4% of time was AI. Buyers paying per task should use the task rate.
