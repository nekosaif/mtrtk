"""Static checks on how the ROS 2 bridge is shipped: its image, the compose profile, CI, docs.

No Docker here: the real check is building `ros2/Dockerfile` for Humble and Jazzy (CI's `ros2`
job). These tests keep the pieces that have to agree with each other in step - the image and the
package layout, the compose service and `.env.example`, the docs and the node's topics.
"""

import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from mtrtk.core.state import Attitude, ReceiverState
from mtrtk.web.ws import epoch_message

ROOT = Path(__file__).resolve().parents[2]
ROS2 = ROOT / "ros2"
DOCKERFILE = ROS2 / "Dockerfile"
ENTRYPOINT = ROS2 / "entrypoint.sh"
DOCS = ROOT / "docs" / "ros2.md"
FASTDDS_PROFILE = ROS2 / "fastdds.xml"
DISTROS = ("humble", "jazzy")
# What docs/ros2.md and the README say while the daemon's epochs carry no attitude (see below).
ATTITUDE_PENDING = "The daemon does not send attitude on its WebSocket yet"


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


def _apt_install_line() -> str:
    """The Dockerfile's `apt-get install` RUN line: a package named only in a comment is not
    installed, and the image still builds (ament_python needs nothing at build time)."""
    lines = [
        line
        for line in DOCKERFILE.read_text().splitlines()
        if line.startswith("RUN ") and "apt-get install" in line
    ]
    assert len(lines) == 1, lines
    return lines[0]


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
    apt = _apt_install_line().split()
    assert "ros-${ROS_DISTRO}-nmea-msgs" in apt
    assert "python3-websocket" in apt
    # websocket-client, not websockets (P7T3): checked at build time, not when the node starts.
    assert re.search(r"^RUN python3 -c 'import websocket'$", text, re.M)


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
    # set -e: a failed `source /ws/install/setup.bash` stops the container instead of running
    # ros2 launch against a missing workspace.
    assert re.search(r"^set -e$", script, re.M)
    assert 'source "/opt/ros/${ROS_DISTRO}/setup.bash"' in script
    assert "source /ws/install/setup.bash" in script
    # exec: the launch process becomes PID 1, so `docker stop`'s signal reaches it.
    assert script.rstrip().endswith('exec "$@"')
    assert os.access(ENTRYPOINT, os.X_OK)


def test_the_entrypoint_stays_executable_in_git() -> None:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-s", "ros2/entrypoint.sh"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        pytest.skip("git is not installed")
    if result.returncode != 0 or not result.stdout:
        pytest.skip("not a git work tree, or ros2/entrypoint.sh is not tracked")
    # A checkout must keep it executable as well as this tree.
    assert result.stdout.startswith("100755 "), result.stdout


def test_docker_stop_reaches_ros2_launch_as_sigint() -> None:
    # `ros2 launch` as PID 1 ignores SIGTERM (seen on Humble and Jazzy), so `docker stop` would
    # wait out its grace period and SIGKILL it.
    assert re.search(r"^STOPSIGNAL SIGINT$", DOCKERFILE.read_text(), re.M)


def test_the_image_talks_dds_over_udp_only() -> None:
    """Fast DDS sends to a peer on the same host over shared memory in /dev/shm. The container's
    /dev/shm is private, so a native node or another container would discover the topics but
    never get a sample. The image's default profile turns shared memory off: UDPv4 only."""
    text = DOCKERFILE.read_text()
    assert "COPY ros2/fastdds.xml /etc/mtrtk/fastdds.xml" in text
    # Humble's Fast DDS 2.6 reads FASTRTPS_*, Jazzy's 2.14 prefers FASTDDS_*: set both.
    for var in ("FASTRTPS_DEFAULT_PROFILES_FILE", "FASTDDS_DEFAULT_PROFILES_FILE"):
        assert re.search(rf"^ENV {var}=/etc/mtrtk/fastdds\.xml$", text, re.M), var
    root = ET.parse(FASTDDS_PROFILE).getroot()
    ns = {"f": "http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles"}
    kinds = {
        d.findtext("f:transport_id", namespaces=ns): d.findtext("f:type", namespaces=ns)
        for d in root.iterfind(".//f:transport_descriptor", ns)
    }
    assert kinds and set(kinds.values()) == {"UDPv4"}, kinds
    participants = root.findall(".//f:participant", ns)
    assert len(participants) == 1
    participant = participants[0]
    assert participant.get("is_default_profile") == "true"
    assert participant.findtext("f:rtps/f:useBuiltinTransports", namespaces=ns) == "false"
    used = [e.text for e in participant.iterfind("f:rtps/f:userTransports/f:transport_id", ns)]
    assert used and set(used) <= set(kinds), used


def test_the_compose_profile_builds_the_bridge_for_the_chosen_distro() -> None:
    svc = _compose_service("mtrtk-ros2")
    assert 'profiles: ["ros2"]' in svc
    assert "context: ." in svc
    assert "dockerfile: ros2/Dockerfile" in svc
    assert "ROS_DISTRO: ${ROS_DISTRO:-humble}" in svc
    assert "image: ghcr.io/nekosaif/mtrtk-ros2:${ROS_DISTRO:-humble}" in svc
    assert "container_name: mtrtk-ros2" in svc
    assert "network_mode: host" in svc  # DDS discovery and the daemon's port on the host
    assert "restart: unless-stopped" in svc
    assert "depends_on: [mtrtk]" in svc
    assert "ROS_DOMAIN_ID: ${ROS_DOMAIN_ID:-0}" in svc
    # The image's launch file reads $MTRTK_WS_URL; the default is the parameter file's.
    default_url = _bridge_default_ws_url()
    assert f"MTRTK_WS_URL: ${{MTRTK_WS_URL:-{default_url}}}" in svc
    # The web token rides the environment, never a ROS parameter (readable on the domain).
    assert "MTRTK_WS_TOKEN: ${MTRTK_WS_TOKEN:-}" in svc
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
    # JetPack 6 is arm64: an apt package or colcon step missing there fails CI, not the robot.
    assert "docker/setup-qemu-action@v3" in job
    assert "platforms: linux/amd64,linux/arm64" in job
    assert "scope=ros2-${{ matrix.distro }}" in job
    # ... and the amd64 build is loaded and run, not only built.
    assert "load: true" in job
    assert "/smoke/smoke.sh" in job and (ROOT / "ros2" / "smoke.sh").exists()


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


def test_the_readme_names_the_bridges_websocket_client() -> None:
    readme = (ROOT / "README.md").read_text()
    assert "websocket-client" in readme
    assert "python3-websockets" not in readme


def test_a_native_colcon_build_leaves_nothing_to_commit_lint_or_ship() -> None:
    # `cd ros2 && colcon build` (docs/ros2.md) writes these; ruff follows .gitignore as well.
    for name in (".gitignore", ".dockerignore"):
        lines = (ROOT / name).read_text().splitlines()
        for path in ("ros2/build/", "ros2/install/", "ros2/log/"):
            assert path in lines, (name, path)


def test_the_attitude_topics_are_documented_as_the_daemon_sends_them() -> None:
    """/mtrtk/imu and /mtrtk/heading publish only from an epoch's fresh attitude. The docs sell
    them exactly when the bridge, asking for its own topics, gets one from the daemon's epoch."""
    import sys

    sys.path.insert(0, str(ROS2 / "mtrtk_bridge"))
    from mtrtk_bridge.convert import EpochAccumulator
    from mtrtk_bridge.link import WS_TOPICS

    state = ReceiverState()
    state.attitude = Attitude(roll_deg=1.0, pitch_deg=2.0, heading_deg=90.0, source="sbg")
    acc = EpochAccumulator()
    acc.ingest(epoch_message(state, WS_TOPICS))
    publishes = acc.fresh_attitude is not None
    docs = DOCS.read_text()
    readme = (ROOT / "README.md").read_text()
    if publishes:  # the bridge gets the attitude: no caveat, and the README says so
        assert ATTITUDE_PENDING not in docs
        assert "`/mtrtk/heading` from an INS rover's attitude" in readme
    else:
        assert ATTITUDE_PENDING in docs
        assert "INS attitude" not in readme
