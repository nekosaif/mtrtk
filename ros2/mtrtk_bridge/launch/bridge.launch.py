"""Launch the bridge with the packaged parameters (or `params:=<file>`).

`ws_url` comes, in order, from `ws_url:=...`, from `$MTRTK_WS_URL` (what the compose profile
sets), else from the parameter file - so a file's own `ws_url` is not overridden by a default.
Every other parameter can be given as `name:=value` too; an empty one keeps the file's value.

The web token is never made a parameter (any node on the DDS domain can read parameters): it
comes from `token:=`, `$MTRTK_WS_TOKEN`, or a `?token=` taken out of the URL, and reaches the
node in its environment, which the node sends as an `Authorization: Bearer` header.
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchContext, LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

from mtrtk_bridge.link import split_token

PASSTHROUGH = ("frame_id", "namespace", "nmea_tcp", "reconnect_s", "stale_s")
NUMBERS = ("reconnect_s", "stale_s")


def _value(name: str, text: str) -> str | float:
    if name in NUMBERS:
        try:
            return float(text)
        except ValueError:
            return text  # the node logs the bad value and uses its default
    return text


def _bridge(context: LaunchContext) -> list[Node]:
    params: list[str | dict[str, str | float]] = [LaunchConfiguration("params").perform(context)]
    overrides: dict[str, str | float] = {}
    token = LaunchConfiguration("token").perform(context) or os.environ.get("MTRTK_WS_TOKEN", "")
    ws_url = LaunchConfiguration("ws_url").perform(context) or os.environ.get("MTRTK_WS_URL", "")
    if ws_url:
        try:
            ws_url, url_token = split_token(ws_url)
        except ValueError:
            url_token = ""  # the node reports the URL it cannot dial
        token = token or url_token
        overrides["ws_url"] = ws_url
    for name in PASSTHROUGH:
        text = LaunchConfiguration(name).perform(context)
        if text:
            overrides[name] = _value(name, text)
    if overrides:
        params.append(overrides)
    env = {"MTRTK_WS_TOKEN": token} if token else None
    return [
        Node(
            package="mtrtk_bridge",
            executable="mtrtk_bridge",
            name="mtrtk_bridge",
            output="screen",
            parameters=params,
            additional_env=env,
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
            DeclareLaunchArgument("token", default_value=""),
            *(DeclareLaunchArgument(name, default_value="") for name in PASSTHROUGH),
            OpaqueFunction(function=_bridge),
        ]
    )
