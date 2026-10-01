"""Static checks on how the ROS 2 bridge is shipped: its image, the compose profile, CI, docs.

No Docker here: the real check is building `ros2/Dockerfile` for Humble and Jazzy (CI's `ros2`
job). These tests keep the pieces that have to agree with each other in step - the image and the
package layout, the compose service and `.env.example`, the docs and the node's topics.
"""

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROS2 = ROOT / "ros2"
DOCKERFILE = ROS2 / "Dockerfile"
ENTRYPOINT = ROS2 / "entrypoint.sh"
DOCS = ROOT / "docs" / "ros2.md"
DISTROS = ("humble", "jazzy")


def _compose_service(name: str) -> str:
    """The text of one service block in docker-compose.yml (two-space indented, under services)."""
    text = (ROOT / "docker-compose.yml").read_text()
    match = re.search(rf"^  {re.escape(name)}:\n((?:    .*\n|\s*\n)+)", text, re.M)
    assert match, f"docker-compose.yml has no {name} service"
    return match.group(1)


def _env_example() -> dict[str, str]:
    values = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key] = value.split("#", 1)[0].strip()
    return values


def _bridge_default_ws_url() -> str:
    match = re.search(
        r'^\s+ws_url: "([^"]+)"', (ROS2 / "mtrtk_bridge/config/bridge.yaml").read_text(), re.M
    )
    assert match
    return match.group(1)


def test_one_dockerfile_builds_either_distro_from_the_ros_base_image() -> None:
    text = DOCKERFILE.read_text()
    assert re.search(r"^ARG ROS_DISTRO=humble$", text, re.M)
    assert re.search(r"^FROM ros:\$\{ROS_DISTRO\}-ros-base$", text, re.M)
    # The ARG before FROM is out of scope after it: it has to be declared again to be used.
    assert text.index("ARG ROS_DISTRO\n") > text.index("FROM ")
    assert "ros-${ROS_DISTRO}-nmea-msgs" in text
    # websocket-client, not websockets (P7T3): checked at build time, not when the node starts.
    assert "RUN python3 -c 'import websocket'" in text


def test_the_image_copies_both_packages_and_keeps_its_install_tree_self_contained() -> None:
    text = DOCKERFILE.read_text()
    for pkg in ("mtrtk_msgs", "mtrtk_bridge"):
        assert (ROS2 / pkg / "package.xml").is_file()
        assert f"COPY ros2/{pkg} src/{pkg}" in text
    build = next(line for line in text.splitlines() if "colcon build" in line)
    # --symlink-install points install/ into build/ (generated messages, the python package):
    # deleting build/ afterwards would leave an image where `mtrtk_msgs` cannot be imported.
    assert "rm -rf build" in build
    assert "--symlink-install" not in build


def test_the_entrypoint_sources_ros_and_the_workspace_then_execs_the_command() -> None:
    text = DOCKERFILE.read_text()
    assert "COPY ros2/entrypoint.sh /entrypoint.sh" in text
    assert 'ENTRYPOINT ["/entrypoint.sh"]' in text
    assert 'CMD ["ros2", "launch", "mtrtk_bridge", "bridge.launch.py"]' in text
    script = ENTRYPOINT.read_text()
    assert script.startswith("#!/usr/bin/env bash\n")
    assert 'source "/opt/ros/${ROS_DISTRO}/setup.bash"' in script
    assert "source /ws/install/setup.bash" in script
    # exec: the launch process becomes PID 1, so `docker stop`'s signal reaches it ...
    assert script.rstrip().endswith('exec "$@"')
    # ... and that signal is SIGINT: `ros2 launch` as PID 1 ignores SIGTERM (seen on Humble and
    # Jazzy), so `docker stop` would wait out its grace period and SIGKILL it.
    assert re.search(r"^STOPSIGNAL SIGINT$", text, re.M)
    assert os.access(ENTRYPOINT, os.X_OK)
    mode = subprocess.run(
        ["git", "ls-files", "-s", "ros2/entrypoint.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if mode:  # tracked: a checkout must keep it executable as well
        assert mode.startswith("100755 "), mode


def test_the_compose_profile_builds_the_bridge_for_the_chosen_distro() -> None:
    svc = _compose_service("mtrtk-ros2")
    assert 'profiles: ["ros2"]' in svc
    assert "context: ." in svc
    assert "dockerfile: ros2/Dockerfile" in svc
    assert "ROS_DISTRO: ${ROS_DISTRO:-humble}" in svc
    assert "network_mode: host" in svc  # DDS discovery and the daemon's port on the host
    assert "ROS_DOMAIN_ID: ${ROS_DOMAIN_ID:-0}" in svc
    # The image's launch file reads $MTRTK_WS_URL; the default is the parameter file's.
    default_url = _bridge_default_ws_url()
    assert f"MTRTK_WS_URL: ${{MTRTK_WS_URL:-{default_url}}}" in svc
    # The base profile is untouched: plain `docker compose up` does not start the bridge.
    assert "profiles" not in _compose_service("mtrtk")


def test_env_example_documents_the_ros2_profile_with_the_compose_defaults() -> None:
    env = _env_example()
    assert env["ROS_DISTRO"] == "humble"
    assert env["ROS_DOMAIN_ID"] == "0"
    assert env["MTRTK_WS_URL"] == _bridge_default_ws_url()
    svc = _compose_service("mtrtk-ros2")
    for key in ("ROS_DISTRO", "ROS_DOMAIN_ID", "MTRTK_WS_URL"):
        assert f"${{{key}:-{env[key]}}}" in svc, key


def test_ci_builds_the_bridge_image_for_both_distros_without_pushing() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    match = re.search(r"^  ros2:\n((?:    .*\n|\s*\n)+)", ci, re.M)
    assert match, "ci.yml has no ros2 job"
    job = match.group(1)
    assert "distro: [humble, jazzy]" in job
    assert "file: ros2/Dockerfile" in job
    assert "build-args: ROS_DISTRO=${{ matrix.distro }}" in job
    assert "push: false" in job


def test_the_docs_name_every_topic_parameter_and_the_websocket_client() -> None:
    text = DOCS.read_text()
    node = (ROS2 / "mtrtk_bridge" / "mtrtk_bridge" / "node.py").read_text()
    topics = re.findall(r'create_publisher\(\w+, f"\{ns\}(/\w+)"', node)
    assert len(topics) == 8
    for topic in topics:
        assert f"`/mtrtk{topic}`" in text, topic
    params = re.findall(
        r"^    (\w+):", (ROS2 / "mtrtk_bridge/config/bridge.yaml").read_text(), re.M
    )
    assert params
    for param in params:
        assert f"`{param}`" in text, param
    assert "websocket-client" in text
    assert "python3-websockets" not in text
    for distro in DISTROS:
        assert f"ROS_DISTRO={distro}" in text
