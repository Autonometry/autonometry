"""Plays xr_teleoperate in motion mode: rt/arm_sdk with weight 1.0 from 6 to 8 s."""

from g1_fakes import HG, args, lowcmd, run, writer

dp, start = args()
out = writer(dp, "rt/arm_sdk", HG + "LowCmd_")


def step(elapsed: float) -> None:
    if 6.0 <= elapsed < 8.0:
        out.write(lowcmd(1.0))


run(start, 0.02, step)
