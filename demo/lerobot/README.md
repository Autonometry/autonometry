# LeRobot demo

Measures a real DAgger rollout of an SO-101-class arm (`robot_type: so_follower`) from its public
dataset, the one dossier 14.6 analysed (research/lerobot-so101):
[`jogarulfo/rollout_dagger_2026-10-19_store_pvc_pieces_bbox_all_v6_20261019_081024`](https://huggingface.co/datasets/jogarulfo/rollout_dagger_2026-10-19_store_pvc_pieces_bbox_all_v6_20261019_081024)
(Apache-2.0), pinned at revision `c4014132f7ca`.

```
uv sync --all-extras
uv run python demo/lerobot/run_demo.py          # writes demo/lerobot/run/
```

`run_demo.py` uses the standard library only for the download (urllib, HTTPS, timeouts, retries
with backoff). It lists `meta/` and `data/` at the pinned revision and checks every path. It
keeps `meta/` and the first 3 data parquet files (the dataset has one), and no video. It prints
the total size, about 2.6 MB, and stops before downloading anything when the total is above
`--max-mb` (100). Then it runs `autonometry lerobot import` with `config/` and
`autonometry report`. Expected result:

| | Value |
|---|---|
| sessions (one per episode) | 18, 43,051 frames at 30 fps, 23.9 min |
| time autonomy share | 0.779, 95 % CI [0.668, 0.891] (ratio-linearised t, 18 sessions as clusters) |
| human share (`human_local`) | 0.221 |
| interventions | 20 (50.2 per active hour) |
| task autonomy rate | 6 of 18 episodes |
| trust level | L1 (rebuilt timestamps), report not clean |

The dataset names neither its policy nor its teleoperator, so `config/devices.json` declares a
policy `demo_dagger_policy` and the SO-101 leader arm (local). The demo passes `--policy` and
`--teleop` to match them. A remote teleoperator would move the human time to `human_remote`
and leave the share unchanged. `--start 2026-10-19T06:10:24Z` is the time in the dataset's name
(08:10:24, local) read as UTC+2, which the Hub's commit at 06:37:09Z supports. It only places
the sessions in time; no metric depends on it.

What the numbers do not show: with `record_autonomous=true`, DAgger records autonomous and
correction frames, but never the pauses before a correction or the resets between episodes. Each
episode is therefore its own session, and every session declares `unobserved: ["safety_stops",
"human_inputs"]`; the report prints that beside each metric.
