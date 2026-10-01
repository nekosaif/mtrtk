# mtrtk ROS 2 bridge (mtrtk_msgs + mtrtk_bridge), one file for both distros:
#   docker build -f ros2/Dockerfile --build-arg ROS_DISTRO=humble .   # Ubuntu 22.04 / JetPack 6
#   docker build -f ros2/Dockerfile --build-arg ROS_DISTRO=jazzy .    # Ubuntu 24.04
# Build context is the repository root (docker-compose.yml's ros2 profile does this).
ARG ROS_DISTRO=humble
FROM ros:${ROS_DISTRO}-ros-base
ARG ROS_DISTRO
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends ros-${ROS_DISTRO}-nmea-msgs python3-websocket python3-pip && rm -rf /var/lib/apt/lists/*
# websocket-client, not websockets (P7T3 ruling): fail the build, not the container, if it is missing
RUN python3 -c 'import websocket'
WORKDIR /ws
COPY ros2/mtrtk_msgs src/mtrtk_msgs
COPY ros2/mtrtk_bridge src/mtrtk_bridge
# No --symlink-install: install/ would then point into build/ (generated message code, the
# python package), and build/ is deleted to keep the image small.
RUN . /opt/ros/${ROS_DISTRO}/setup.sh && colcon build --cmake-args -DCMAKE_BUILD_TYPE=Release && rm -rf build log
COPY ros2/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
# `ros2 launch` runs as PID 1: it shuts down cleanly on SIGINT but ignores docker's default
# SIGTERM there, so `docker stop` would wait out its grace period and SIGKILL the node.
STOPSIGNAL SIGINT
CMD ["ros2", "launch", "mtrtk_bridge", "bridge.launch.py"]
