"""Launch the bridge with the packaged parameters (or `params:=<file>`).

`ws_url` comes, in order, from `ws_url:=...`, from `$MTRTK_WS_URL` (what the compose profile
sets), else from the parameter file - so a file's own `ws_url` is not overridden by a default.
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchContext, LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def _bridge(context: LaunchContext) -> list[Node]:
    params: list[str | dict[str, str]] = [LaunchConfiguration("params").perform(context)]
    ws_url = LaunchConfiguration("ws_url").perform(context) or os.environ.get("MTRTK_WS_URL", "")
    if ws_url:
        params.append({"ws_url": ws_url})
    return [
        Node(
            package="mtrtk_bridge",
            executable="mtrtk_bridge",
            name="mtrtk_bridge",
            output="screen",
            parameters=params,
        )
    ]


def generate_launch_description() -> LaunchDescription:
    default_params = os.path.join(
        get_package_share_directory("mtrtk_bridge"), "config", "bridge.yaml"
    )
    return LaunchDescription(
        [
            DeclareLaunchArgument("params", default_value=default_params),
            DeclareLaunchArgument("ws_url", default_value=""),
            OpaqueFunction(function=_bridge),
        ]
    )
