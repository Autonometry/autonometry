"""Plays Unitree's arm action service running preset 18: rt/arm_sdk with weight 1.0 from 16.2 to
17.5 s, and rt/arm/action/state holding from 16 to 18 s, released after."""

import json

from g1_fakes import HG, T, args, lowcmd, run, writer

dp, start = args()
arm = writer(dp, "rt/arm_sdk", HG + "LowCmd_")
state = writer(dp, "rt/arm/action/state", "std_msgs::msg::dds_::String_")
text = T["std_msgs::msg::dds_::String_"]


def step(elapsed: float) -> None:
    if 16.2 <= elapsed < 17.5:
        arm.write(lowcmd(1.0))
    if elapsed >= 15.0:
        holding = 16.0 <= elapsed < 18.0
        state.write(text(json.dumps({"holding": holding, "id": 18, "name": "preset_18"})))


run(start, 0.05, step)
