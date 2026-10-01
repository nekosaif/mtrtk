# Rising-edge time mark from the receiver's EXTINT pin (camera shutter feedback).
std_msgs/Header header     # stamp = the pulse time (UTC) when known, else receive time
uint8 channel
uint32 count
uint32 gps_week
float64 gps_tow            # s
bool time_valid            # stamp is the pulse time
uint32 acc_est_ns
