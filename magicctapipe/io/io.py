#!/usr/bin/env python
# coding: utf-8

import glob
import logging
import random

import numpy as np
import pandas as pd
import tables
from astropy import units as u
from astropy.io import fits
from astropy.table import QTable
from astropy.time import Time
from ctapipe.containers import EventType
from ctapipe.coordinates import CameraFrame
from ctapipe.instrument import SubarrayDescription
from lstchain.reco.utils import add_delta_t_key
from magicctapipe.utils import (
    calculate_dead_time_correction,
    calculate_mean_direction,
    transform_altaz_to_radec,
)
from pyirf.binning import join_bin_lo_hi
from pyirf.simulations import SimulatedEventsInfo
from pyirf.utils import calculate_source_fov_offset, calculate_theta

__all__ = [
    "check_feature_importance",
    "get_stereo_events",
    "get_events_at_random",
    "get_dl2_mean",
    "load_lst_data_file",
    "load_magic_data_files",
    "load_train_data_file",
    "load_mc_dl2_data_file",
    "load_dl2_data_file",
    "load_irf_files",
    "save_pandas_to_table",
]

logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)

# The LST nominal/effective focal lengths
NOMINAL_FOCLEN_LST = 28 * u.m
EFFECTIVE_FOCLEN_LST = 29.30565 * u.m

# The telescope IDs and names
TEL_NAMES = {1: "LST-1", 2: "MAGIC-I", 3: "MAGIC-II"}

# Use true Alt/Az directions for the pandas index in order to classify
# the events simulated by different telescope pointing directions but
# have the same observation ID
GROUP_INDEX = ["obs_id", "event_id", "true_alt", "true_az"]

# Here event weights are set to 1, meaning no weights.
# ToBeChecked: what weights are best for training RFs?
EVENT_WEIGHT = 1

# The telescope combination types
TEL_COMBINATIONS = {
    "m1_m2": [2, 3],
    "lst1_m1": [1, 2],
    "lst1_m2": [1, 3],
    "lst1_m1_m2": [1, 2, 3],
}


def check_feature_importance(estimator):
    """
    Checks the feature importance of trained RFs.

    Parameters
    ----------
    estimator: magicctapipe.reco.estimator
        Trained regressor or classifier
    """

    features = np.array(estimator.features)
    tel_ids = estimator.telescope_rfs.keys()

    for tel_id in tel_ids:

        logger.info(f"\nTelescope {tel_id} feature importance:")
        telescope_rf = estimator.telescope_rfs[tel_id]

        importances = telescope_rf.feature_importances_
        importances_sort = np.sort(importances)[::-1]
        indices_sort = np.argsort(importances)[::-1]
        features_sort = features[indices_sort]

        for feature, importance in zip(features_sort, importances_sort):
            logger.info(f"\t{feature}: {importance.round(5)}")


def get_stereo_events(event_data, quality_cuts=None):
    """
    Gets stereo events surviving specified quality cuts.

    The input data is supposed to have the index (obs_id, event_id) to
    group up the shower events.

    It adds the telescope multiplicity and combination types to the
    output data frame.

    Parameters
    ----------
    event_data: pandas.core.frame.DataFrame
        Pandas data frame of shower events
    quality_cuts: str
        Quality cuts applied to the input data

    Returns
    -------
    event_data_stereo: pandas.core.frame.DataFrame
        Pandas data frame of the stereo events surviving the cuts
    """

    event_data_stereo = event_data.copy()

    # Apply the quality cuts
    if quality_cuts is not None:
        logger.info(f"\nApplying the quality cuts:\n{quality_cuts}")
        event_data_stereo.query(quality_cuts, inplace=True)

    # Extract stereo events
    event_data_stereo["multiplicity"] = event_data_stereo.groupby(GROUP_INDEX).size()
    event_data_stereo.query("multiplicity == [2, 3]", inplace=True)

    n_events_total = len(event_data_stereo.groupby(GROUP_INDEX).size())
    logger.info(f"\nIn total {n_events_total} stereo events are found:")

    # Check the telescope combination types
    for combo_type, (tel_combo, tel_ids) in enumerate(TEL_COMBINATIONS.items()):

        df_events = event_data_stereo.query(
            f"(tel_id == {tel_ids}) & (multiplicity == {len(tel_ids)})"
        ).copy()

        df_events["multiplicity"] = df_events.groupby(GROUP_INDEX).size()
        df_events.query(f"multiplicity == {len(tel_ids)}", inplace=True)

        n_events = int(len(df_events.groupby(GROUP_INDEX).size()))
        percentage = np.round(100 * n_events / n_events_total, 1)

        logger.info(
            f"\t{tel_combo} (type {combo_type}): {n_events} events ({percentage}%)"
        )

        event_data_stereo.loc[df_events.index, "combo_type"] = combo_type

    return event_data_stereo


def get_events_at_random(event_data, n_random):
    """
    Extracts a given number of events at random.

    Parameters
    ----------
    event_data: pandas.core.frame.DataFrame
        Pandas data frame of shower events
    n_random:
        The number of events to be extracted at random

    Returns
    -------
    data_selected: pandas.core.frame.DataFrame
        Pandas data frame of the shower events randomly extracted
    """

    data_selected = pd.DataFrame()

    n_events = len(event_data.groupby(GROUP_INDEX).size())
    indices = random.sample(range(n_events), n_random)

    tel_ids = np.unique(event_data.index.get_level_values("tel_id"))

    for tel_id in tel_ids:
        df_events = event_data.query(f"tel_id == {tel_id}").copy()
        data_selected = data_selected.append(df_events.iloc[indices])

    data_selected.sort_index(inplace=True)

    return data_selected


def get_dl2_mean(event_data, weight_type="simple"):
    """
    Gets the mean DL2 parameters per shower event.

    The input data is supposed to have the index (obs_id, event_id) to
    group up the shower events.

    Parameters
    ----------
    event_data: pandas.core.frame.DataFrame
        Pandas data frame of shower events
    weight_type: str
        Type of the weights for the telescope-wise DL2 parameters -
        "simple" does not use any weights for calculations,
        "variance" uses the inverse of the RF variance, and
        "intensity" uses the linear-scale intensity parameter

    Returns
    -------
    event_data_mean: pandas.core.frame.DataFrame
        Pandas data frame of shower events with the mean parameters
    """

    is_simulation = "true_energy" in event_data.columns

    # Create a mean data frame
    if is_simulation:
        params = ["combo_type", "multiplicity", "true_energy", "true_alt", "true_az"]
    else:
        params = ["combo_type", "multiplicity", "timestamp"]

    event_data_mean = event_data[params].groupby(GROUP_INDEX).mean()

    # Calculate the mean pointing direction
    pointing_az_mean, pointing_alt_mean = calculate_mean_direction(
        event_data["pointing_az"], event_data["pointing_alt"]
    )

    event_data_mean["pointing_alt"] = pointing_alt_mean
    event_data_mean["pointing_az"] = pointing_az_mean

    # Define the weights for the DL2 parameters
    if weight_type == "simple":
        energy_weights = 1
        direction_weights = None
        gammaness_weights = 1

    elif weight_type == "variance":
        energy_weights = 1 / event_data["reco_energy_var"]
        direction_weights = 1 / event_data["reco_disp_var"]
        gammaness_weights = 1 / event_data["gammaness_var"]

    elif weight_type == "intensity":
        energy_weights = event_data["intensity"]
        direction_weights = event_data["intensity"]
        gammaness_weights = event_data["intensity"]

    df_events = pd.DataFrame(
        data={
            "energy_weight": energy_weights,
            "gammaness_weight": gammaness_weights,
            "weighted_energy": np.log10(event_data["reco_energy"]) * energy_weights,
            "weighted_gammaness": event_data["gammaness"] * gammaness_weights,
        }
    )

    # Calculate the mean DL2 parameters
    group_sum = df_events.groupby(GROUP_INDEX).sum()

    reco_energy_mean = 10 ** (group_sum["weighted_energy"] / group_sum["energy_weight"])
    gammaness_mean = group_sum["weighted_gammaness"] / group_sum["gammaness_weight"]

    reco_az_mean, reco_alt_mean = calculate_mean_direction(
        event_data["reco_az"], event_data["reco_alt"], direction_weights, unit="deg"
    )

    event_data_mean["reco_energy"] = reco_energy_mean
    event_data_mean["reco_alt"] = reco_alt_mean
    event_data_mean["reco_az"] = reco_az_mean
    event_data_mean["gammaness"] = gammaness_mean

    # Transform the Alt/Az to the RA/Dec coordinate
    if not is_simulation:

        timestamps_mean = Time(
            event_data_mean["timestamp"].to_numpy(), format="unix", scale="utc"
        )

        pointing_ra_mean, pointing_dec_mean = transform_altaz_to_radec(
            alt=u.Quantity(pointing_alt_mean.to_numpy(), u.rad),
            az=u.Quantity(pointing_az_mean.to_numpy(), u.rad),
            obs_time=timestamps_mean,
        )

        reco_ra_mean, reco_dec_mean = transform_altaz_to_radec(
            alt=u.Quantity(reco_alt_mean.to_numpy(), u.deg),
            az=u.Quantity(reco_az_mean.to_numpy(), u.deg),
            obs_time=timestamps_mean,
        )

        event_data_mean["pointing_ra"] = pointing_ra_mean
        event_data_mean["pointing_dec"] = pointing_dec_mean
        event_data_mean["reco_ra"] = reco_ra_mean
        event_data_mean["reco_dec"] = reco_dec_mean

    return event_data_mean


def load_lst_data_file(input_file):
    """
    Loads a LST-1 data file and arranges the contents for the event
    coincidence with MAGIC.

    Parameters
    ----------
    input_file: str
        Path to an input LST-1 data file

    Returns
    -------
    event_data: pandas.core.frame.DataFrame
        Pandas data frame of LST-1 events
    subarray: ctapipe.instrument.subarray.SubarrayDescription
        LST-1 subarray description
    """

    # Load the input file
    event_data = pd.read_hdf(
        input_file, key="dl1/event/telescope/parameters/LST_LSTCam"
    )

    event_data.set_index(["obs_id", "event_id", "tel_id"], inplace=True)
    event_data.sort_index(inplace=True)

    # Add the trigger time differences of consecutive events
    event_data = add_delta_t_key(event_data)

    # Exclude interleaved events
    event_data.query(f"event_type == {EventType.SUBARRAY.value}", inplace=True)

    # Exclude poorly reconstructed events
    event_data.dropna(
        subset=["intensity", "time_gradient", "alt_tel", "az_tel"], inplace=True
    )

    # Check the duplication of event IDs and exclude them.
    # ToBeChecked: if it still happens in recent data or not
    event_ids, counts = np.unique(
        event_data.index.get_level_values("event_id"), return_counts=True
    )

    if np.any(counts > 1):
        event_ids_dup = event_ids[counts > 1].tolist()
        event_data.query(f"event_id != {event_ids_dup}", inplace=True)

        logger.warning(
            f"WARNING: The duplications of the event IDs are found: {event_ids_dup}"
        )

    logger.info(f"LST-1: {len(event_data)} events")

    # Rename the columns
    event_data.rename(
        columns={
            "delta_t": "time_diff",
            "alt_tel": "pointing_alt",
            "az_tel": "pointing_az",
            "leakage_pixels_width_1": "pixels_width_1",
            "leakage_pixels_width_2": "pixels_width_2",
            "leakage_intensity_width_1": "intensity_width_1",
            "leakage_intensity_width_2": "intensity_width_2",
            "time_gradient": "slope",
        },
        inplace=True,
    )

    # Change the units of parameters
    optics = pd.read_hdf(input_file, key="configuration/instrument/telescope/optics")
    focal_length = optics["equivalent_focal_length"][0]

    event_data["length"] = focal_length * np.tan(np.deg2rad(event_data["length"]))
    event_data["width"] = focal_length * np.tan(np.deg2rad(event_data["width"]))

    event_data["phi"] = np.rad2deg(event_data["phi"])
    event_data["psi"] = np.rad2deg(event_data["psi"])

    # Read the subarray description
    subarray = SubarrayDescription.from_hdf(input_file)

    if focal_length == NOMINAL_FOCLEN_LST:
        # Set the effective focal length to the subarray
        subarray.tel[1].optics.equivalent_focal_length = EFFECTIVE_FOCLEN_LST
        subarray.tel[1].camera.geometry.frame = CameraFrame(
            focal_length=EFFECTIVE_FOCLEN_LST
        )

    return event_data, subarray


def load_magic_data_files(input_dir):
    """
    Loads MAGIC data files.

    Parameters
    ----------
    input_dir: str
        Path to a directory where input MAGIC data files are stored

    Returns
    -------
    event_data: pandas.core.frame.DataFrame
        Pandas data frame of MAGIC events
    subarray: ctapipe.instrument.subarray.SubarrayDescription
        MAGIC subarray description
    """

    # Find the input files
    file_mask = f"{input_dir}/dl1_*.h5"

    input_files = glob.glob(file_mask)
    input_files.sort()

    if len(input_files) == 0:
        raise FileNotFoundError(
            "Could not find MAGIC data files in the input directory."
        )

    # Load the input files
    logger.info("\nThe following files are found:")

    data_list = []

    for input_file in input_files:

        logger.info(input_file)

        df_events = pd.read_hdf(input_file, key="events/parameters")
        data_list.append(df_events)

    event_data = pd.concat(data_list)

    event_data.rename(
        columns={"obs_id": "obs_id_magic", "event_id": "event_id_magic"}, inplace=True
    )

    event_data.set_index(["obs_id_magic", "event_id_magic", "tel_id"], inplace=True)
    event_data.sort_index(inplace=True)

    tel_ids = np.unique(event_data.index.get_level_values("tel_id"))

    for tel_id in tel_ids:

        tel_name = TEL_NAMES.get(tel_id)
        n_events = len(event_data.query(f"tel_id == {tel_id}"))

        logger.info(f"{tel_name}: {n_events} events")

    # Read the subarray description from the first input file, assuming
    # that it is consistent with the others
    subarray = SubarrayDescription.from_hdf(input_files[0])

    return event_data, subarray


@u.quantity_input(offaxis_min=u.deg, offaxis_max=u.deg)
def load_train_data_file(
    input_file, offaxis_min=None, offaxis_max=None, true_event_class=None
):
    """
    Loads a DL1-stereo data file and separates the shower events per
    telescope combination type.

    Parameters
    ----------
    input_file: str
        Path to an input DL1-stereo data file
    offaxis_min: astropy.units.quantity.Quantity
        Minimum shower off-axis angle allowed
    offaxis_max: astropy.units.quantity.Quantity
        Maximum shower off-axis angle allowed
    true_event_class: int
        True event class of the input events

    Returns
    -------
    data_train: dict
        Pandas data frames of the shower events separated by the
        telescope combination types
    """

    event_data = pd.read_hdf(input_file, key="events/parameters")
    event_data.set_index(GROUP_INDEX + ["tel_id"], inplace=True)
    event_data.sort_index(inplace=True)

    if offaxis_min is not None:
        logger.info(f"Minimum off-axis angle allowed: {offaxis_min}")
        event_data.query(f"off_axis >= {offaxis_min.to_value(u.deg)}", inplace=True)

    if offaxis_max is not None:
        logger.info(f"Maximum off-axis angle allowed: {offaxis_max}")
        event_data.query(f"off_axis <= {offaxis_max.to_value(u.deg)}", inplace=True)

    if true_event_class is not None:
        event_data["true_event_class"] = true_event_class

    event_data["event_weight"] = EVENT_WEIGHT

    n_events_total = len(event_data.groupby(GROUP_INDEX).size())
    logger.info(f"\nIn total {n_events_total} stereo events are found:")

    data_train = {}

    for tel_combo, tel_ids in TEL_COMBINATIONS.items():

        df_events = event_data.query(
            f"(tel_id == {tel_ids}) & (multiplicity == {len(tel_ids)})"
        ).copy()

        df_events["multiplicity"] = df_events.groupby(GROUP_INDEX).size()
        df_events.query(f"multiplicity == {len(tel_ids)}", inplace=True)

        n_events = len(df_events.groupby(GROUP_INDEX).size())
        logger.info(f"\t{tel_combo}: {n_events} events")

        if n_events > 0:
            data_train[tel_combo] = df_events

    return data_train


def load_mc_dl2_data_file(input_file, quality_cuts, irf_type, dl2_weight):
    """
    Loads a MC DL2 data file and applies event selections.

    Parameters
    ----------
    input_file: str
        Path to an input MC DL2 data file
    quality_cuts: str
        Quality cuts applied to the input events
    irf_type: str
        Type of the IRFs which will be created -
        "software(_only_3tel)", "magic_only" or "hardware" are allowed
    dl2_weight: str
        Type of the weight for averaging telescope-wise DL2 parameters -
        "simple", "variance" or "intensity" are allowed

    Returns
    -------
    event_table: astropy.table.table.QTable
        Astropy table of MC DL2 events
    pointing: numpy.ndarray
        Telescope mean pointing direction (Zd, Az) in degree
    sim_info: pyirf.simulations.SimulatedEventsInfo
        Container of the simulation information
    """

    df_events = pd.read_hdf(input_file, key="events/parameters")
    df_events.set_index(["obs_id", "event_id", "tel_id"], inplace=True)
    df_events.sort_index(inplace=True)

    df_events = get_stereo_events(df_events, quality_cuts)

    # Select the events of the specified IRF type
    logger.info(f"\nExtracting the events of the '{irf_type}' type...")

    if irf_type == "software":
        df_events.query("(combo_type > 0) & (magic_stereo == True)", inplace=True)

    elif irf_type == "software_only_3tel":
        df_events.query("combo_type == 3", inplace=True)

    elif irf_type == "magic_only":
        df_events.query("combo_type == 0", inplace=True)

    elif irf_type != "hardware":
        raise KeyError(f"Unknown IRF type '{irf_type}'.")

    n_events = len(df_events.groupby(["obs_id", "event_id"]).size())
    logger.info(f"--> {n_events} stereo events")

    # Compute the mean of the DL2 parameters
    logger.info(f"\nDL2 weight type: {dl2_weight}")

    df_dl2_mean = get_dl2_mean(df_events, dl2_weight)
    df_dl2_mean.reset_index(inplace=True)

    # Convert the pandas data frame to the astropy QTable
    event_table = QTable.from_pandas(df_dl2_mean)

    event_table["pointing_alt"] *= u.rad
    event_table["pointing_az"] *= u.rad
    event_table["true_alt"] *= u.deg
    event_table["true_az"] *= u.deg
    event_table["reco_alt"] *= u.deg
    event_table["reco_az"] *= u.deg
    event_table["true_energy"] *= u.TeV
    event_table["reco_energy"] *= u.TeV

    event_table["theta"] = calculate_theta(
        events=event_table,
        assumed_source_az=event_table["true_az"],
        assumed_source_alt=event_table["true_alt"],
    )

    event_table["true_source_fov_offset"] = calculate_source_fov_offset(event_table)
    event_table["reco_source_fov_offset"] = calculate_source_fov_offset(
        event_table, prefix="reco"
    )

    pointing_zd = np.mean(90 - event_table["pointing_alt"].to_value(u.deg))
    pointing_az = np.mean(event_table["pointing_az"].to_value(u.deg))
    pointing = np.array([pointing_zd.round(3), pointing_az.round(3)])

    # Load the simulation configuration
    sim_config = pd.read_hdf(input_file, key="simulation/config")

    n_total_showers = (
        sim_config["num_showers"][0]
        * sim_config["shower_reuse"][0]
        * len(np.unique(event_table["obs_id"]))
    )

    sim_info = SimulatedEventsInfo(
        n_showers=n_total_showers,
        energy_min=u.Quantity(sim_config["energy_range_min"][0], u.TeV),
        energy_max=u.Quantity(sim_config["energy_range_max"][0], u.TeV),
        max_impact=u.Quantity(sim_config["max_scatter_range"][0], u.m),
        spectral_index=sim_config["spectral_index"][0],
        viewcone=u.Quantity(sim_config["max_viewcone_radius"][0], u.deg),
    )

    return event_table, pointing, sim_info


def load_dl2_data_file(input_file, quality_cuts, irf_type, dl2_weight):
    """
    Loads a DL2 data file.

    Parameters
    ----------
    input_file: str
        Path to an input DL2 data file
    quality_cuts: str
        Quality cuts applied to the input events
    irf_type: str
        Type of the IRFs which will be created -
        "software(_only_3tel)", "magic_only" or "hardware" are allowed
    dl2_weight: str
        Type of the weight for averaging telescope-wise DL2 parameters -
        "simple", "variance" or "intensity" are allowed

    Returns
    -------
    event_table: astropy.table.table.QTable
        Astropy table of DL2 events
    deadc: float
        Dead time correction factor
    """

    df_events = pd.read_hdf(input_file, key="events/parameters")
    df_events.set_index(["obs_id", "event_id", "tel_id"], inplace=True)
    df_events.sort_index(inplace=True)

    df_events = get_stereo_events(df_events, quality_cuts)

    # Select the events of the specified IRF type
    logger.info(f'\nExtracting the events of the "{irf_type}" type...')

    if irf_type == "software":
        df_events.query("combo_type > 0", inplace=True)

    elif irf_type == "software_only_3tel":
        df_events.query("combo_type == 3", inplace=True)

    elif irf_type == "magic_only":
        df_events.query("combo_type == 0", inplace=True)

    elif irf_type == "hardware":
        logger.warning(
            "WARNING: Please confirm that this IRF type is correct for the input data, "
            "since the hardware trigger between LST-1 and MAGIC may NOT be used."
        )

    n_events = len(df_events.groupby(["obs_id", "event_id"]).size())
    logger.info(f"--> {n_events} stereo events")

    # Calculate the dead time correction factor
    deadc = calculate_dead_time_correction(df_events)

    # Compute the mean of the DL2 parameters
    df_dl2_mean = get_dl2_mean(df_events, dl2_weight)
    df_dl2_mean.reset_index(inplace=True)

    # Convert the pandas data frame to the astropy QTable
    event_table = QTable.from_pandas(df_dl2_mean)

    event_table["pointing_alt"] *= u.rad
    event_table["pointing_az"] *= u.rad
    event_table["pointing_ra"] *= u.deg
    event_table["pointing_dec"] *= u.deg
    event_table["reco_alt"] *= u.deg
    event_table["reco_az"] *= u.deg
    event_table["reco_ra"] *= u.deg
    event_table["reco_dec"] *= u.deg
    event_table["reco_energy"] *= u.TeV
    event_table["timestamp"] *= u.s

    return event_table, deadc


def load_irf_files(input_dir_irf):
    """
    Loads input IRF files and checks the consistency.

    Parameters
    ----------
    input_dir_irf: str
        Path to a directory where input IRF files are stored

    Returns
    -------
    irf_data: dict
        Combined IRF data
    extra_header: dict
        Extra header of input IRF files
    """

    irf_file_mask = f"{input_dir_irf}/irf_*.fits.gz"

    input_files_irf = glob.glob(irf_file_mask)
    input_files_irf.sort()

    n_input_files = len(input_files_irf)

    if n_input_files == 0:
        raise FileNotFoundError("Could not find IRF files in the input directory.")

    extra_header = {
        "TELESCOP": [],
        "INSTRUME": [],
        "FOVALIGN": [],
        "QUAL_CUT": [],
        "IRF_TYPE": [],
        "DL2_WEIG": [],
        "GH_CUT": [],
        "GH_EFF": [],
        "GH_MIN": [],
        "GH_MAX": [],
        "RAD_MAX": [],
        "TH_EFF": [],
        "TH_MIN": [],
        "TH_MAX": [],
    }

    irf_data = {
        "grid_point": [],
        "effective_area": [],
        "energy_dispersion": [],
        "background": [],
        "gh_cuts": [],
        "rad_max": [],
        "energy_bins": [],
        "fov_offset_bins": [],
        "migration_bins": [],
        "bkg_fov_offset_bins": [],
    }

    logger.info("\nThe following files are found:")

    for input_file in input_files_irf:

        logger.info(input_file)
        hdus_irf = fits.open(input_file)

        header = hdus_irf["EFFECTIVE AREA"].header

        for key in extra_header.keys():
            if key in header:
                extra_header[key].append(header[key])

        # Read the grid point
        coszd = np.cos(np.deg2rad(header["PNT_ZD"]))
        azimuth = np.deg2rad(header["PNT_AZ"])
        grid_point = [coszd, azimuth]

        # Read the IRF data
        aeff_data = hdus_irf["EFFECTIVE AREA"].data[0]
        edisp_data = hdus_irf["ENERGY DISPERSION"].data[0]

        energy_bins = join_bin_lo_hi(aeff_data["ENERG_LO"], aeff_data["ENERG_HI"])
        fov_offset_bins = join_bin_lo_hi(aeff_data["THETA_LO"], aeff_data["THETA_HI"])
        migration_bins = join_bin_lo_hi(edisp_data["MIGRA_LO"], edisp_data["MIGRA_HI"])

        irf_data["grid_point"].append(grid_point)
        irf_data["effective_area"].append(aeff_data["EFFAREA"])
        irf_data["energy_dispersion"].append(np.swapaxes(edisp_data["MATRIX"], 0, 2))
        irf_data["energy_bins"].append(energy_bins)
        irf_data["fov_offset_bins"].append(fov_offset_bins)
        irf_data["migration_bins"].append(migration_bins)

        if "BACKGROUND" in hdus_irf:
            bkg_data = hdus_irf["BACKGROUND"].data[0]
            bkg_fov_offset_bins = join_bin_lo_hi(
                bkg_data["THETA_LO"], bkg_data["THETA_HI"]
            )

            irf_data["background"].append(bkg_data["BKG"])
            irf_data["bkg_fov_offset_bins"].append(bkg_fov_offset_bins)

        if "GH_CUTS" in hdus_irf:
            ghcuts_data = hdus_irf["GH_CUTS"].data[0]
            irf_data["gh_cuts"].append(ghcuts_data["GH_CUTS"])

        if "RAD_MAX" in hdus_irf:
            radmax_data = hdus_irf["RAD_MAX"].data[0]
            irf_data["rad_max"].append(radmax_data["RAD_MAX"])

    # Check the IRF data consistency
    for key in irf_data.keys():

        irf_data[key] = np.array(irf_data[key])
        n_data = len(irf_data[key])

        if (n_data != 0) and (n_data != n_input_files):
            raise ValueError(
                f"The number of '{key}' data (= {n_data}) does not match "
                f"with that of the input IRF files (= {n_input_files})."
            )

        if "bins" in key:
            unique_bins = np.unique(irf_data[key], axis=0)
            n_unique_bins = len(unique_bins)

            if n_unique_bins == 1:
                irf_data[key] = unique_bins[0]

            elif n_unique_bins > 1:
                raise ValueError(f"The '{key}' of the input IRF files does not match.")

    # Check the header consistency
    for key in list(extra_header.keys()):

        n_data = len(extra_header[key])
        unique_values = np.unique(extra_header[key])

        if n_data == 0:
            extra_header.pop(key)

        elif (n_data != n_input_files) or len(unique_values) > 1:
            raise ValueError(
                "The configurations of the input IRF files do not match, "
                "at least the setting '{key}'."
            )
        else:
            extra_header[key] = unique_values[0]

    # Set the units to the IRF data
    irf_data["effective_area"] *= u.m**2
    irf_data["background"] *= u.Unit("MeV-1 s-1 sr-1")
    irf_data["rad_max"] *= u.deg
    irf_data["energy_bins"] *= u.TeV
    irf_data["fov_offset_bins"] *= u.deg
    irf_data["bkg_fov_offset_bins"] *= u.deg

    return irf_data, extra_header


def save_pandas_to_table(data, output_file, group_name, table_name, mode="w"):
    """
    Saves a pandas data frame in a table.

    Parameters
    ----------
    data: pandas.core.frame.DataFrame
        Pandas data frame
    output_file: str
        Path to an output HDF file
    group_name: str
        Group name of the output table
    table_name: str
        Name of the output table
    mode: str
        Mode of saving the data if a file already exists at the output
        file path, "w" for overwriting the file with the new table, and
        "a" for appending the table to the file
    """

    params = data.dtypes.index
    dtypes = data.dtypes.values

    data_array = np.array(
        [tuple(array) for array in data.to_numpy()],
        dtype=np.dtype([(param, dtype) for param, dtype in zip(params, dtypes)]),
    )

    with tables.open_file(output_file, mode=mode) as f_out:
        f_out.create_table(group_name, table_name, createparents=True, obj=data_array)
