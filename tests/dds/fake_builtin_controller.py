"""Plays Unitree's built-in controller: rt/sportmodestate, damping (fsm 1) from 9 to 11 s."""

from g1_fakes import HG, T, args, run, writer

dp, start = args()
out = writer(dp, "rt/sportmodestate", HG + "SportModeState_")
state = T[HG + "SportModeState_"]


def step(elapsed: float) -> None:
    fsm_id = 1 if 9.0 <= elapsed < 11.0 else 500
    out.write(state(fsm_id, 0, 0, 0.0))


run(start, 0.02, step)
