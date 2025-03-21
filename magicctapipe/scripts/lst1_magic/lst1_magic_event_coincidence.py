#!/usr/bin/env python
# coding: utf-8

"""
This script searches for coincident events from LST-1 and MAGIC joint
observation data offline using their timestamps. It applies the
coincidence window to LST-1 events, and checks the coincidence within
the time offset region specified in the configuration file. Since the
optimal time offset changes depending on the telescope distance along
the pointing direction, it is recommended to input one subrun file for
LST-1 data, whose observation time is usually around 10 seconds so the
change of the distance is negligible.

The MAGIC standard stereo analysis discards the events when one of the
telescope images cannot survive the cleaning or fail to compute the DL1
parameters. However, it's possible to perform the stereo analysis if
LST-1 sees these events. Thus, it checks the coincidence for each
telescope combination (i.e., LST1 + M1 and LST1 + M2) and keeps the
MAGIC events even if they do not have their MAGIC-stereo counterparts.

The MAGIC-stereo events, observed during the LST-1 observation time
period but not coincident with any LST-1 events, are also saved in the
output file, but they are not yet used for the high level analysis.

Unless there is any particular reason, please use the default half width
300 ns for the coincidence window, which is optimized to reduce the
accidental coincidence rate as much as possible by keeping the number of
actual coincident events.

Please note that for the data taken before 12th June 2021, a coincidence
peak should be found around the time offset of -3.1 us, which can be
explained by the trigger time delays of both systems. For the data taken
after that date, however, there is an additional global offset appeared
and then the peak is shifted to the time offset of -6.5 us. Thus, it
would be needed to tune the offset scan region depending on the date
when data were taken. The reason of the shift is under investigation.

Usage:
$ python lst1_magic_event_coincidence.py
--input-file-lst dl1/LST-1/dl1_LST-1.Run03265.0040.h5
--input-dir-magic dl1/MAGIC
(--output-dir dl1_coincidence)
(--config-file config.yaml)
"""

import argparse
import logging
import sys
import time
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from astropy import units as u
from ctapipe.instrument import SubarrayDescription
from magicctapipe.io import (
    format_object,
    get_stereo_events,
    load_lst_dl1_data_file,
    load_magic_dl1_data_files,
    save_pandas_data_in_table,
)
from magicctapipe.io.io import TEL_NAMES

__all__ = ["event_coincidence"]

logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)

# The conversion factor from seconds to nanoseconds
SEC2NSEC = Decimal("1e9")

# The final digit of timestamps
TIME_ACCURACY = 100 * u.ns

# The telescope positions used in a simulation
TEL_POSITIONS = {
    1: [-8.09, 77.13, 0.78] * u.m,  # LST-1
    2: [39.3, -62.55, -0.97] * u.m,  # MAGIC-I
    3: [-31.21, -14.57, 0.2] * u.m,  # MAGIC-II
}


def event_coincidence(input_file_lst, input_dir_magic, output_dir, config):
    """
    Searches for coincident events from LST-1 and MAGIC joint
    observation data offline using their timestamps.

    Parameters
    ----------
    input_file_lst: str
        Path to an input LST-1 DL1 data file
    input_dir_magic: str
        Path to a directory where input MAGIC DL1 data files are stored
    output_dir: str
        Path to a directory where to save an output DL1 data file
    config: dict
        Configuration for the LST-1 + MAGIC combined analysis
    """

    config_coinc = config["event_coincidence"]

    # Load the input LST-1 DL1 data file
    logger.info(f"\nInput LST-1 DL1 data file: {input_file_lst}")

    event_data_lst, subarray_lst = load_lst_dl1_data_file(input_file_lst)

    # Load the input MAGIC DL1 data files
    logger.info(f"\nInput MAGIC directory: {input_dir_magic}")

    event_data_magic, subarray_magic = load_magic_dl1_data_files(input_dir_magic)

    # Exclude the parameters non-common to LST-1 and MAGIC data
    timestamp_type_lst = config_coinc["timestamp_type_lst"]
    logger.info(f"\nLST timestamp type: {timestamp_type_lst}")

    event_data_lst.rename(columns={timestamp_type_lst: "timestamp"}, inplace=True)

    params_lst = set(event_data_lst.columns) ^ set(["timestamp"])
    params_magic = set(event_data_magic.columns) ^ set(["time_sec", "time_nanosec"])
    params_non_common = list(params_lst ^ params_magic)

    event_data_lst.drop(params_non_common, axis=1, errors="ignore", inplace=True)
    event_data_magic.drop(params_non_common, axis=1, errors="ignore", inplace=True)

    # Prepare for the event coincidence
    window_half_width = config_coinc["window_half_width"]
    logger.info(f"\nCoincidence window half width: {window_half_width}")

    window_half_width = u.Quantity(window_half_width).to("ns")
    window_half_width = u.Quantity(window_half_width.round(), dtype=int)

    logger.info("\nTime offsets:")
    logger.info(format_object(config_coinc["time_offset"]))

    offset_start = u.Quantity(config_coinc["time_offset"]["start"])
    offset_stop = u.Quantity(config_coinc["time_offset"]["stop"])

    time_offsets = np.arange(
        start=offset_start.to_value("ns").round(),
        stop=offset_stop.to_value("ns").round(),
        step=TIME_ACCURACY.to_value("ns").round(),
    )

    time_offsets = u.Quantity(time_offsets.round(), unit="ns", dtype=int)

    event_data = pd.DataFrame()
    features = pd.DataFrame()
    profiles = pd.DataFrame(data={"time_offset": time_offsets.to_value("us").round(1)})

    # Arrange the LST timestamps. They are stored in the UNIX format in
    # units of seconds with 17 digits, 10 digits for the integral part
    # and 7 digits for the fractional part (up to 100 ns order). For the
    # coincidence search, however, it is too long to precisely find
    # coincident events if we keep using the default data type "float64"
    # due to the rounding issue. Thus, here we scale the timestamps to
    # the units of nanoseconds and then use the "int64" type, which can
    # keep a value up to ~20 digits. In order to precisely scale the
    # timestamps, here we use the `Decimal` module.

    timestamps_lst = [Decimal(str(time)) for time in event_data_lst["timestamp"]]
    timestamps_lst = np.array(timestamps_lst) * SEC2NSEC
    timestamps_lst = u.Quantity(timestamps_lst, unit="ns", dtype=int)

    # Loop over every telescope combination
    tel_ids = np.unique(event_data_magic.index.get_level_values("tel_id"))

    for tel_id in tel_ids:
        tel_name = TEL_NAMES[tel_id]
        df_magic = event_data_magic.query(f"tel_id == {tel_id}").copy()

        # Arrange the MAGIC timestamps as same as the LST-1 timestamps
        seconds = np.array([Decimal(str(time)) for time in df_magic["time_sec"]])
        nseconds = np.array([Decimal(str(time)) for time in df_magic["time_nanosec"]])

        timestamps_magic = seconds * SEC2NSEC + nseconds
        timestamps_magic = u.Quantity(timestamps_magic, unit="ns", dtype=int)

        df_magic["timestamp"] = timestamps_magic.to_value("s")
        df_magic.drop(["time_sec", "time_nanosec"], axis=1, inplace=True)

        # Extract the MAGIC events taken when LST-1 observed
        logger.info(f"\nExtracting the {tel_name} events taken when LST-1 observed...")

        time_lolim = timestamps_lst[0] + time_offsets[0] - window_half_width
        time_uplim = timestamps_lst[-1] + time_offsets[-1] + window_half_width

        cond_lolim = timestamps_magic >= time_lolim
        cond_uplim = timestamps_magic <= time_uplim

        mask = np.logical_and(cond_lolim, cond_uplim)
        n_events_magic = np.count_nonzero(mask)

        if n_events_magic == 0:
            logger.info(f"--> No {tel_name} events are found. Skipping...")
            continue

        logger.info(f"--> {n_events_magic} events are found.")

        df_magic = df_magic.iloc[mask]
        timestamps_magic = timestamps_magic[mask]

        # Start checking the event coincidence. The time offsets and the
        # coincidence window are applied to the LST-1 events, and the
        # MAGIC events existing in the window, including the edges, are
        # recognized as the coincident events. At first, we scan the
        # number of coincident events in each time offset and find the
        # offset maximizing the number of events. Then, we calculate the
        # average offset weighted by the number of events around the
        # maximizing offset. Finally, we again check the coincidence at
        # the average offset and then keep the coincident events.

        n_coincidences = []

        logger.info("\nChecking the event coincidence...")

        for time_offset in time_offsets:
            times_lolim = timestamps_lst + time_offset - window_half_width
            times_uplim = timestamps_lst + time_offset + window_half_width

            cond_lolim = timestamps_magic.value >= times_lolim[:, np.newaxis].value
            cond_uplim = timestamps_magic.value <= times_uplim[:, np.newaxis].value

            mask = np.logical_and(cond_lolim, cond_uplim)
            n_coincidence = np.count_nonzero(mask)

            logger.info(
                f"time offset: {time_offset.to('us'):.1f} --> {n_coincidence} events"
            )

            n_coincidences.append(n_coincidence)

        if not any(n_coincidences):
            logger.info("\nNo coincident events are found. Skipping...")
            continue

        n_coincidences = np.array(n_coincidences)

        # Sometimes there are more than one time offset maximizing the
        # number of coincidences, so here we calculate the mean of them
        offset_at_max = time_offsets[n_coincidences == n_coincidences.max()].mean()

        # The half width of the average region is defined as the "full"
        # width of the coincidence window, since the width of the
        # coincidence distribution becomes larger than that of the
        # coincidence window due to the uncertainty of the timestamps
        offset_lolim = offset_at_max - 2 * window_half_width
        offset_uplim = offset_at_max + 2 * window_half_width

        cond_lolim = time_offsets >= np.round(offset_lolim)
        cond_uplim = time_offsets <= np.round(offset_uplim)

        mask = np.logical_and(cond_lolim, cond_uplim)

        average_offset = np.average(time_offsets[mask], weights=n_coincidences[mask])
        average_offset = u.Quantity(average_offset.round(), dtype=int)

        logger.info(f"\nAverage offset: {average_offset.to('us'):.3f}")

        # Check again the coincidence at the average offset
        times_lolim = timestamps_lst + average_offset - window_half_width
        times_uplim = timestamps_lst + average_offset + window_half_width

        cond_lolim = timestamps_magic.value >= times_lolim[:, np.newaxis].value
        cond_uplim = timestamps_magic.value <= times_uplim[:, np.newaxis].value

        mask = np.logical_and(cond_lolim, cond_uplim)

        n_events_at_avg = np.count_nonzero(mask)
        percentage = 100 * n_events_at_avg / n_events_magic

        logger.info(f"--> Number of coincident events: {n_events_at_avg}")
        logger.info(f"--> Fraction over the {tel_name} events: {percentage:.1f}%")

        # Keep only the LST-1 events coincident with the MAGIC events,
        # and assign the MAGIC observation and event IDs to them
        indices_lst, indices_magic = np.where(mask)

        multi_indices_magic = df_magic.iloc[indices_magic].index
        obs_ids_magic = multi_indices_magic.get_level_values("obs_id_magic")
        event_ids_magic = multi_indices_magic.get_level_values("event_id_magic")

        df_lst = event_data_lst.iloc[indices_lst].copy()
        df_lst["obs_id_magic"] = obs_ids_magic
        df_lst["event_id_magic"] = event_ids_magic
        df_lst.reset_index(inplace=True)
        df_lst.set_index(["obs_id_magic", "event_id_magic", "tel_id"], inplace=True)

        # Assign also the LST-1 observation and event IDs to the MAGIC
        # events coincident with the LST-1 events
        obs_ids_lst = df_lst["obs_id_lst"].to_numpy()
        event_ids_lst = df_lst["event_id_lst"].to_numpy()

        df_magic.loc[multi_indices_magic, "obs_id_lst"] = obs_ids_lst
        df_magic.loc[multi_indices_magic, "event_id_lst"] = event_ids_lst

        # Arrange the data frames
        coincidence_id = "1" + str(tel_id)  # Combination of the telescope IDs

        df_feature = pd.DataFrame(
            data={
                "coincidence_id": [int(coincidence_id)],
                "window_half_width": [window_half_width.to_value("ns")],
                "unix_time": [df_lst["timestamp"].mean()],
                "pointing_alt_lst": [df_lst["pointing_alt"].mean()],
                "pointing_az_lst": [df_lst["pointing_az"].mean()],
                "pointing_alt_magic": [df_magic["pointing_alt"].mean()],
                "pointing_az_magic": [df_magic["pointing_az"].mean()],
                "average_offset": [average_offset.to_value("us")],
                "n_coincidence": [n_events_at_avg],
                "n_events_magic": [n_events_magic],
            }
        )

        df_profile = pd.DataFrame(
            data={
                "time_offset": time_offsets.to_value("us").round(1),
                f"n_coincidence_tel{coincidence_id}": n_coincidences,
            }
        )

        event_data = pd.concat([event_data, df_lst, df_magic])
        features = pd.concat([features, df_feature])
        profiles = profiles.merge(df_profile)

    if event_data.empty:
        logger.info("\nNo coincident events are found. Exiting...")
        sys.exit()

    event_data.sort_index(inplace=True)
    event_data.drop_duplicates(inplace=True)

    # It sometimes happen that even if it is a MAGIC-stereo event, only
    # M1 or M2 event is coincident with a LST-1 event. In that case we
    # keep both M1 and M2 events, since they are recognized as the same
    # shower event by the MAGIC-stereo hardware trigger.

    # We also keep the MAGIC-stereo events not coincident with any LST-1
    # events, since the stereo reconstruction is still feasible, but not
    # yet used for the high level analysis.

    group_mean = event_data.groupby(["obs_id_magic", "event_id_magic"]).mean()

    event_data["obs_id"] = group_mean["obs_id_lst"]
    event_data["event_id"] = group_mean["event_id_lst"]

    indices = event_data[event_data["obs_id"].isna()].index

    event_data.loc[indices, "obs_id"] = indices.get_level_values("obs_id_magic")
    event_data.loc[indices, "event_id"] = indices.get_level_values("event_id_magic")

    event_data.reset_index(inplace=True)
    event_data.set_index(["obs_id", "event_id", "tel_id"], inplace=True)
    event_data.sort_index(inplace=True)

    event_data = get_stereo_events(event_data)
    event_data.reset_index(inplace=True)

    event_data = event_data.astype({"obs_id": int, "event_id": int})

    # Save the data in an output file
    Path(output_dir).mkdir(exist_ok=True, parents=True)

    input_file_name = Path(input_file_lst).name

    output_file_name = input_file_name.replace("LST-1", "LST-1_MAGIC")
    output_file = f"{output_dir}/{output_file_name}"

    save_pandas_data_in_table(
        event_data, output_file, group_name="/events", table_name="parameters", mode="w"
    )

    save_pandas_data_in_table(
        features, output_file, group_name="/coincidence", table_name="feature", mode="a"
    )

    save_pandas_data_in_table(
        profiles, output_file, group_name="/coincidence", table_name="profile", mode="a"
    )

    # Create the subarray description with the telescope coordinates
    # relative to the center of the LST-1 and MAGIC positions
    tel_descriptions = {
        1: subarray_lst.tel[1],  # LST-1
        2: subarray_magic.tel[2],  # MAGIC-I
        3: subarray_magic.tel[3],  # MAGIC-II
    }

    subarray_lst1_magic = SubarrayDescription(
        "LST1-MAGIC-Array", TEL_POSITIONS, tel_descriptions
    )

    # Save the subarray description
    subarray_lst1_magic.to_hdf(output_file)

    logger.info(f"\nOutput file: {output_file}")


def main():
    start_time = time.time()

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--input-file-lst",
        "-l",
        dest="input_file_lst",
        type=str,
        required=True,
        help="Path to an input LST-1 DL1 data file",
    )

    parser.add_argument(
        "--input-dir-magic",
        "-m",
        dest="input_dir_magic",
        type=str,
        required=True,
        help="Path to a directory where input MAGIC DL1 data files are stored",
    )

    parser.add_argument(
        "--output-dir",
        "-o",
        dest="output_dir",
        type=str,
        default="./data",
        help="Path to a directory where to save an output DL1 data file",
    )

    parser.add_argument(
        "--config-file",
        "-c",
        dest="config_file",
        type=str,
        default="./config.yaml",
        help="Path to a configuration file",
    )

    args = parser.parse_args()

    with open(args.config_file, "rb") as f:
        config = yaml.safe_load(f)

    # Check the event coincidence
    event_coincidence(
        args.input_file_lst, args.input_dir_magic, args.output_dir, config
    )

    logger.info("\nDone.")

    process_time = time.time() - start_time
    logger.info(f"\nProcess time: {process_time:.0f} [sec]\n")


if __name__ == "__main__":
    main()
