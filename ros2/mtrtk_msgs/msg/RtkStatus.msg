# RTK/correction state of the mtrtk rover, published once per receiver epoch.
# The relative position (rel_pos_*) is UBX-NAV-RELPOSNED: the rover relative to the base,
# so the vector and its heading point from the base to the rover.
std_msgs/Header header
uint8 CARR_NONE=0
uint8 CARR_FLOAT=1
uint8 CARR_FIXED=2
uint8 carr_soln            # CARR_*
uint8 fix_type             # u-blox fixType: 0 none, 1 DR only, 2 2D, 3 3D, 4 GNSS+DR, 5 time only
bool diff_soln
uint8 num_sv
float32 h_acc              # m, 1-sigma horizontal (NaN when unknown)
float32 v_acc              # m, 1-sigma vertical (NaN when unknown)
float32 pdop               # (NaN when unknown)
float32 hdop               # (NaN when unknown)
float32 corr_age           # s since the last RTCM frame was injected (NaN when none)
float32 corr_age_receiver  # s, receiver's own bucketed estimate (NaN when unknown)
float64 baseline           # m, base to rover distance (NaN when no RELPOSNED)
float64 rel_pos_n          # m, rover minus base, north (NaN when no RELPOSNED)
float64 rel_pos_e          # m, rover minus base, east (NaN when no RELPOSNED)
float64 rel_pos_d          # m, rover minus base, down (NaN when no RELPOSNED)
float32 rel_pos_heading    # deg, heading of the base->rover vector (NaN when invalid)
uint32 ref_station_id
uint32 rtcm_rx_total
uint32 rtcm_crc_failed
bool ntrip_connected
