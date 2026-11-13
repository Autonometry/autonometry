"""Launch the read-only Autonometry observer for a TurtleBot 4 (README, "TurtleBot 4").

Arguments: ``namespace`` (the robot namespace, empty by default), ``params_file`` (default: this
package's ``config/observer.params.yaml``), ``recorder_socket`` and ``device_registry`` (override
the params file when given), ``respawn`` (default true: restart the observer if it dies).
"""

from pathlib import Path
from typing import Any

from ament_index_python.packages import get_package_share_directory
from launch import LaunchContext, LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def _observer(context: LaunchContext, *_: Any, **__: Any) -> list[Node]:
    overrides: dict[str, str] = {}
    for name in ("recorder_socket", "device_registry"):
        value = LaunchConfiguration(name).perform(context)
        if value:
            overrides[name] = value
    return [
        Node(
            package="autonometry_ros",
            executable="observer",
            name="autonometry_observer",
            namespace=LaunchConfiguration("namespace"),
            parameters=[LaunchConfiguration("params_file"), overrides],
            output="screen",
            # Nothing on the robot depends on the observer; if it dies, it is started again.
            respawn=LaunchConfiguration("respawn").perform(context).lower() == "true",
            respawn_delay=2.0,
        )
    ]


def generate_launch_description() -> LaunchDescription:
    share = Path(get_package_share_directory("autonometry_ros"))
    return LaunchDescription(
        [
            DeclareLaunchArgument("namespace", default_value=""),
            DeclareLaunchArgument(
                "params_file", default_value=str(share / "config" / "observer.params.yaml")
            ),
            DeclareLaunchArgument("recorder_socket", default_value=""),
            DeclareLaunchArgument("device_registry", default_value=""),
            DeclareLaunchArgument("respawn", default_value="true"),
            OpaqueFunction(function=_observer),
        ]
    )
