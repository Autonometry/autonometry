# Cyclone DDS compatibility check (14 November 2026)

Question: can an observer read a Unitree G1's DDS traffic, and name the writer of every sample,
without disturbing the robot's programs? Unitree's SDKs use Cyclone DDS 0.10.2
(`unitree_sdk2/thirdparty/include/dds/version.h`, `unitree_sdk2_python/setup.py`).

- `g1types.py`: `unitree_hg` LowCmd_ and MotorCmd_, and `unitree_api` Request_ with its header, written
  from the field lists in unitree_sdk2_python's IDL files (commit 814556d15970).
- `robot_side_publisher.py WEIGHT`: plays a program on the robot side; writes `rt/arm_sdk` with the given
  weight in `motor_cmd[29].q` every 20 ms for 5 s, and a SetVelocity request (api 7105) on
  `rt/api/sport/request` every 0.5 s.
- `observer_probe.py`: best-effort readers with keep-last 64, plus the built-in DCPSParticipant and
  DCPSPublication readers; maps each sample's `publication_handle` to its writer and prints every
  participant's properties.

Run on one Linux machine over loopback (Ubuntu 24.04, x86-64), all on DDS domain 7:

```
export CYCLONEDDS_URI='<CycloneDDS><Domain id="any"><General><Interfaces><NetworkInterface name="lo"/></Interfaces><AllowMulticast>false</AllowMulticast></General><Discovery><Peers><Peer address="127.0.0.1"/></Peers><ParticipantIndex>auto</ParticipantIndex></Discovery></Domain></CycloneDDS>'
python robot_side_publisher.py 0.5 &      # robot side
python observer_probe.py                   # observer
```

Builds: Cyclone DDS C 0.10.2 and 0.10.5 from their release tags (CMake, about 20 s each);
the Python binding 0.10.2 (Python 3.10) and 0.10.5 (Python 3.12) built from source with
`CYCLONEDDS_HOME`; 0.10.5 and 11.1.0 Python 3.10 and 3.12 wheels from PyPI.

| Robot side | Observer | Result |
|---|---|---|
| 0.10.5 | 0.10.5 | samples read and mapped to their writers |
| 0.10.5 (two writers) | 0.10.5 on Python 3.12 | both writers told apart; weights 0.5 and 1.0 read |
| **0.10.2** (Unitree's version) | **0.10.5 on Python 3.12** | samples, writer and weight 0.25 read; publisher unharmed |
| 0.10.5 | 11.1.0 | **publisher crashed** (segmentation fault, exit 139) after `ddsi_xt_type_init_impl with invalid type object`; nothing read |
| **0.10.2** | **11.1.0** | **publisher crashed** the same way |
| 11.1.0 | 11.1.0 | samples read |

Every Cyclone participant announced `__Hostname`, `__Pid` and `__ProcessName` (here `python` for all
Python programs), and each data sample's `publication_handle` equalled the instance handle of its
writer's DCPSPublication sample.

Conclusion: the observer must use the Cyclone DDS 0.10 line, and a Python program's script name has
to be read from its process id on the same computer.
