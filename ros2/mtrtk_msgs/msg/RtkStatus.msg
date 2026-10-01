# RTK/correction state of the mtrtk rover, published once per receiver epoch.
std_msgs/Header header
uint8 CARR_NONE=0
uint8 CARR_FLOAT=1
uint8 CARR_FIXED=2
uint8 carr_soln            # CARR_*
uint8 fix_type             # u-blox fixType: 0 none, 2 2D, 3 3D, 4 GNSS+DR, 5 time only
bool diff_soln
uint8 num_sv
float32 h_acc              # m, 1-sigma horizontal
float32 v_acc              # m, 1-sigma vertical
float32 pdop
float32 hdop
float32 corr_age           # s since the last RTCM frame was injected (NaN when none)
float32 corr_age_receiver  # s, receiver's own bucketed estimate (NaN when unknown)
float64 baseline           # m to the base (NaN when no RELPOSNED)
float64 rel_pos_n          # m
float64 rel_pos_e          # m
float64 rel_pos_d          # m
float32 heading_to_base    # deg (NaN when invalid)
uint32 ref_station_id
uint32 rtcm_rx_total
uint32 rtcm_crc_failed
bool ntrip_connected
