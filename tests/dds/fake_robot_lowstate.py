"""Plays the robot's rt/lowstate with the handheld remote's bytes: the left stick forward from 3
to 5 s, then L2 + B (Unitree's emergency stop) from 9 to 9.4 s."""

from g1_fakes import HG, args, lowstate, remote, run, writer

L2_B = (1 << 5) | (1 << 9)
dp, start = args()
out = writer(dp, "rt/lowstate", HG + "LowState_")


def step(elapsed: float) -> None:
    ly = 0.5 if 3.0 <= elapsed < 5.0 else 0.0
    buttons = L2_B if 9.0 <= elapsed < 9.4 else 0
    out.write(lowstate(remote(ly=ly, buttons=buttons)))


run(start, 0.02, step)
