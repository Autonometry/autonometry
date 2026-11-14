"""Plays a policy: SetVelocity (7105) every 0.25 s from 13 to 15 s; at 16 s it asks Unitree's arm
action service for preset 18 (7106), and at 18.5 s releases it (action 99)."""

from g1_fakes import API, args, request, run, writer

dp, start = args()
sport = writer(dp, "rt/api/sport/request", API + "Request_")
arm = writer(dp, "rt/api/arm/request", API + "Request_")
sent: set[str] = set()


def once(name: str, elapsed: float, at: float) -> bool:
    if elapsed >= at and name not in sent:
        sent.add(name)
        return True
    return False


def step(elapsed: float) -> None:
    if 13.0 <= elapsed < 15.0:
        sport.write(
            request(int(elapsed * 100), 7105, {"velocity": [0.3, 0.0, 0.0], "duration": 1.0})
        )
    if once("preset", elapsed, 16.0):
        arm.write(request(1, 7106, {"action_id": 18}))
    if once("release", elapsed, 18.5):
        arm.write(request(2, 7106, {"action_id": 99}))


run(start, 0.25, step)
