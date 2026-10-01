# Rising-edge time mark from the receiver's EXTINT pin (camera shutter feedback).
# week/tow are UBX-TIM-TM2 wnR/towR as the receiver reports them, in time_base: they are GPS
# time only when time_base is TIME_BASE_GNSS on a GPS time grid. header.stamp is UTC.
uint8 TIME_BASE_RECEIVER=0
uint8 TIME_BASE_GNSS=1
uint8 TIME_BASE_UTC=2
std_msgs/Header header     # stamp = the pulse time (UTC) when known, else receive time
uint8 channel
uint32 count
uint8 time_base            # TIME_BASE_*
uint32 week                # rising-edge week number, in time_base (0 when unknown)
float64 tow                # s, rising-edge time of week, in time_base (NaN when unknown)
bool time_valid            # stamp is the pulse time
uint32 acc_est_ns
