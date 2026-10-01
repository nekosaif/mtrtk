#!/bin/bash
# Smoke test run inside a built bridge image (CI's ros2 job, or by hand):
#   docker run --rm -v "$PWD/ros2:/smoke:ro" --entrypoint bash mtrtk-ros2:<distro> /smoke/smoke.sh
# It imports the node and the messages, then launches the bridge with no daemon to talk to and
# sends SIGINT to the whole process group, as a terminal Ctrl-C does (ros2 launch forwards a
# second SIGINT to the node): the node must shut down cleanly, with no traceback.
set -o pipefail
source "/opt/ros/$ROS_DISTRO/setup.bash"
source /ws/install/setup.bash
fail() { echo "smoke: $*" >&2; exit 1; }

python3 -c 'import mtrtk_bridge.node, mtrtk_msgs.msg' || fail "the node or the messages do not import"
ros2 interface show mtrtk_msgs/msg/RtkStatus >/dev/null || fail "mtrtk_msgs/RtkStatus is missing"
ros2 interface show mtrtk_msgs/msg/TimeMark >/dev/null || fail "mtrtk_msgs/TimeMark is missing"

log=$(mktemp)
set -m  # the launch gets a process group of its own, so the group can be signalled
MTRTK_WS_URL='ws://127.0.0.1:9/ws?token=smoke' ros2 launch mtrtk_bridge bridge.launch.py \
  frame_id:=smoke stale_s:=1 >"$log" 2>&1 &
pid=$!
for _ in $(seq 1 30); do grep -q "publishing no fix" "$log" && break; sleep 0.5; done
grep -q "publishing no fix" "$log" || { cat "$log"; fail "the node never ran its watchdog"; }
kill -INT -- "-$pid"
for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
kill -0 "$pid" 2>/dev/null && { kill -KILL -- "-$pid"; cat "$log"; fail "launch did not exit on SIGINT"; }
wait "$pid"
rc=$?
cat "$log"
[ "$rc" -eq 0 ] || fail "ros2 launch exited with $rc"
grep -q "Traceback" "$log" && fail "a traceback on shutdown"
grep -q "process has finished cleanly" "$log" || fail "the node did not finish cleanly"
grep -q "token=smoke" "$log" && fail "the token reached the log"
echo "smoke: ok"
