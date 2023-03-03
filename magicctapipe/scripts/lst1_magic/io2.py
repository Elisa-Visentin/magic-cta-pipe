#!/usr/bin/env python
# coding: utf-8
from astropy import units as u



# The telescope IDs and names
TEL_NAMES = {1: "LST-1", 2:"LST-2", 3: "LST-3", 4: "LST-4", 5: "MAGIC-I", 6: "MAGIC-II"}

# The telescope combination types
TEL_COMBINATIONS = {
    "M1_M2": [5, 6],  # combo_type = 0
    "LST1_M1": [1, 5],  # combo_type = 1
    "LST1_M2": [1, 6],  # combo_type = 2
    "LST1_LST3": [1, 3],  # combo_type = 3
    "LST3_M1": [3, 5],  # combo_type = 4
    "LST3_M2": [3, 6],  # combo_type = 5
    "LST1_M1_M2": [1, 5, 6],  # combo_type = 6
    "LST1_LST3_M1": [1, 3, 5],  # combo_type = 7
    "LST1_LST3_M2": [1, 3, 6],  # combo_type = 8
    "LST3_M1_M2": [3, 5, 6],  # combo_type = 9
    "LST1_LST3_M1_M2": [1, 3, 5, 6],  # combo_type = 10
    "LST1_LST2": [1, 2],
    "LST1_LST4": [1, 4],
    "LST2_LST3": [2, 3],
    "LST2_LST4": [2, 4],
    "LST2_M1": [2, 5],
    "LST2_M2": [2, 6],
    "LST3_LST4": [3, 4],
    "LST4_M1": [4, 5],
    "LST4_M2": [4, 6],
    "LST1_LST2_LST3": [1, 2, 3],
    "LST1_LST2_LST4": [1, 2, 4],
    "LST1_LST2_M1": [1, 2, 5],
    "LST1_LST2_M2": [1, 2, 6],
    "LST1_LST3_LST4": [1, 3, 4],
    "LST1_LST4_M1": [1, 4, 5],
    "LST1_LST4_M2": [1, 4, 6],
    "LST2_LST3_LST4": [2, 3, 4],
    "LST2_LST3_M1": [2, 3, 5],
    "LST2_LST3_M2": [2, 3, 6],
    "LST2_LST4_M1": [2, 4, 5],
    "LST2_LST4_M2": [2, 4, 6],
    "LST2_M1_M2": [2, 5, 6],
    "LST3_LST4_M1": [3, 4, 5],
    "LST3_LST4_M2": [3, 4, 6],
    "LST4_M1_M2": [4, 5, 6],
    "LST1_LST2_LST3_LST4": [1, 2, 3, 4],
    "LST1_LST2_LST3_M1": [1, 2, 3, 5],
    "LST1_LST2_LST3_M2": [1, 2, 3, 6],
    "LST1_LST2_LST4_M1": [1, 2, 4, 5],
    "LST1_LST2_LST4_M2": [1, 2, 4, 6],
    "LST1_LST2_M1_M2": [1, 2, 5, 6],
    "LST1_LST3_LST4_M1": [1, 3, 4, 5],
    "LST1_LST3_LST4_M2": [1, 3, 4, 6],
    "LST1_LST4_M1_M2": [1, 4, 5, 6],
    "LST2_LST3_LST4_M1": [2, 3, 4, 5],
    "LST2_LST3_LST4_M2": [2, 3, 4, 6],
    "LST2_LST3_M1_M2": [2, 3, 5, 6],
    "LST2_LST4_M1_M2": [2, 4, 5, 6],
    "LST3_LST4_M1_M2": [3, 4, 5, 6],
    "LST1_LST2_LST3_LST4_M1": [1, 2, 3, 4, 5],
    "LST1_LST2_LST3_LST4_M2": [1, 2, 3, 4, 6],
    "LST1_LST3_LST4_M1_M2": [1, 3, 4, 5, 6],
    "LST2_LST3_LST4_M1_M2": [2, 3, 4, 5, 6],
    "LST1_LST2_LST3_M1_M2": [1, 2, 3, 5, 6],
    "LST1_LST2_LST4_M1_M2": [1, 2, 4, 5, 6],
    "LST1_LST2_LST3_LST4_M1_M2": [1, 2, 3, 4, 5, 6],
    
    
    
}

# The pandas multi index to classify the events simulated by different
# telescope pointing directions but have the same observation ID
GROUP_INDEX_TRAIN = ["obs_id", "event_id", "true_alt", "true_az"]

# The LST nominal and effective focal lengths
NOMINAL_FOCLEN_LST = 28* u.m
EFFECTIVE_FOCLEN_LST = 29.30565* u.m

# The upper limit of the trigger time differences of consecutive events,
# used when calculating the ON time and dead time correction factor
TIME_DIFF_UPLIM = 0.1 * u.s

# The LST-1 and MAGIC readout dead times
DEAD_TIME_LST = 7.6 * u.us
DEAD_TIME_MAGIC = 26 * u.us


