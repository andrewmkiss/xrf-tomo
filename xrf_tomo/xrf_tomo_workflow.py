import tomopy  # this is supposed to be imported before numpy
import os

# import dxchange
import h5py
import numpy as np
import pandas as pd

from scipy.ndimage import center_of_mass
import skimage.io as io
import skimage.transform as tf
from skimage.registration import phase_cross_correlation

from tomopy.util.misc import write_tiff

from pyxrf.api_dev import make_hdf, dask_client_create, fit_pixel_data_and_save


def _process_fn(fn, *, fn_dir="."):
    """
    Returns normalized absolute path to the file. If ``fn`` is an absolute path,
    then ``fn_dir`` is ignored.
    """
    fn = os.path.expanduser(fn)
    if not os.path.isabs(fn):
        fn_dir = os.path.expanduser(fn_dir)
        fn = os.path.join(fn, fn_dir)
    fn = os.path.abspath(fn)
    fn = os.path.normpath(fn)
    return fn


def _process_dir(fn_dir="."):
    """
    Returns normalized absolute path to directory.
    """
    fn_dir = os.path.expanduser(fn_dir)
    fn_dir = os.path.abspath(fn_dir)
    fn_dir = os.path.normpath(fn_dir)
    return fn_dir


def grab_proj(start, end=None, *, wd="."):
    """
    Get the projections from the data broker
    """
    wd = _process_dir(wd)
    make_hdf(start, end=end, wd=wd)


def read_logfile(fn, *, wd="."):
    """
    Read the log file and return pandas dataframe.

    Parameters
    ----------
    fn: str
        Name of the log file.
    wd: str
        Directory that contains the log file. If ``fn`` is absolute path, then ``wd`` is ignored.
    """
    fn = _process_fn(fn, fn_dir=wd)
    return pd.read_csv(fn, sep=",")


def process_proj(*, wd=".", fn_param=None, fn_log="tomo_info.dat", ic_name="sclr_i0", save_tiff=False):
    """
    Process the projections. ``wd`` is the directory that contains raw .h5 files,
    the parameter file and the log file. If ``fn_param`` and/or ``fn_log`` are relative
    paths, then ``wd`` is considered a root directory.
    """
    if fn_param is None:
        raise ValueError("The name of the file with fitting parameters ('fn_param') is not specified")

    # Check the working directory and go to it
    wd = _process_dir(wd)
    fn_param = _process_fn(fn_param, fn_dir=wd)
    fn_log = _process_fn(fn_log, fn_dir=wd)

    # Read from logfile
    log = read_logfile(fn_log, wd=wd)

    # Filter results
    log = log[log["Use"] == "x"]

    # Identify the files
    ls = list(log["Filename"])
    N = len(ls)

    # TODO: Add a check for the number of projections and if it matches theta

    # SPECTRUM FITTING

    # Create dask client
    client = dask_client_create()

    i = 0
    for f in ls:
        i = i + 1
        print("Fitting spectra: %04d/%04d" % (i, N), end="\r")
        fit_pixel_data_and_save(
            wd, f, param_file_name=fn_param, scaler_name=ic_name, save_tiff=save_tiff, dask_client=client
        )

    # Close the dask client
    client.close()

    print("Fitting spectra is completed")


def make_single_hdf(
    fn, *, fn_log="tomo_info.dat", wd_src=".", wd_dest=".", convert_theta=False, include_raw_data=False
):
    """
    Change to the working directory

    Parameters
    ----------
    fn: str
        Name of the HDF5 file to create. If ``fn`` is an absolute path, then ``wd_dest`` is ignored.
    fn_log: str
        Name of the log file. Specify absolute path if the location of the file is different from ``wd_src``.
    wd_src: str
        The directory that contains source files including ``fn_log`` and HDF5 files.
    wd_dest: str
        The directory where the single HDF5 file is created. If ``fn`` is an absolute path,
        then ``wd_dest`` is ignored.
    convert_theta: bool
        True - convert Theta from mdeg to deg by dividing by 1000, False - Theta is already deg
    include_raw_data: bool
        True - copy raw ('sum') data to the single HDF5 file, False - copy only fitted data (saves disk space)
    """
    wd_src = _process_dir(wd_src)
    fn_log = _process_fn(fn_log, fn_dir=wd_src)
    wd_dest = _process_dir(wd_dest)
    fn = _process_fn(fn, fn_dir=wd_dest)

    # Read from logfile
    log = read_logfile(fn_log, wd=wd_src)

    # Filter and sort results
    log = log[log["Use"] == "x"]
    log = log.sort_values(by=["Theta"])

    th = log["Theta"].values
    num = log.shape[0]

    # Create a blank h5 file
    with h5py.File(fn, "w") as f:
        # Make default layout
        # Change this to a single function to create the layout
        f.create_group("exchange")
        f.create_group("measurement")
        f.create_group("instrument")
        f.create_group("provenance")
        f.create_group("reconstruction")
        f.create_group("reconstruction/fitting")
        f.create_group("reconstruction/recon")

        # Load the data
        flag_first = True
        for i in range(num):
            fn_src = log.loc[log["Theta"] == th[i], "Filename"].values[0]
            print("Collecting data...%04d/%04d (file '%s')" % (i + 1, num, fn), end="\n")
            with h5py.File(os.path.join(wd_src, fn_src), "r") as tmp_f:
                if flag_first:
                    if include_raw_data:
                        raw = tmp_f["xrfmap"]["detsum"]["counts"]
                        raw = np.expand_dims(raw, axis=0)
                    xrf_fit = tmp_f["xrfmap"]["detsum"]["xrf_fit"]
                    xrf_fit = np.expand_dims(xrf_fit, axis=0)
                    xrf_fit_names = np.array(tmp_f["xrfmap"]["detsum"]["xrf_fit_name"])
                    x = tmp_f["xrfmap"]["positions"]["pos"][1, :]
                    x = np.expand_dims(x, axis=0)
                    y = tmp_f["xrfmap"]["positions"]["pos"][0, :]
                    y = np.expand_dims(y, axis=0)
                    i0 = tmp_f["xrfmap"]["scalers"]["val"][:, :, 0]
                    i0 = np.expand_dims(i0, axis=0)

                    if include_raw_data:
                        f_raw = f.create_dataset(
                            "/exchange/raw", data=raw, maxshape=(num, *raw.shape[1:]), compression="gzip"
                        )
                        f_raw.resize(num, axis=0)
                    if convert_theta:
                        f.create_dataset("/exchange/theta", data=th / 1000)  # mdeg -> deg
                    else:
                        f.create_dataset("/exchange/theta", data=th)
                    f_x = f.create_dataset("/exchange/x", data=x, maxshape=(num, *x.shape[1:]), compression="gzip")
                    f_x.resize(num, axis=0)
                    f_y = f.create_dataset("/exchange/y", data=y, maxshape=(num, *y.shape[1:]), compression="gzip")
                    f_y.resize(num, axis=0)
                    f_i0 = f.create_dataset(
                        "/exchange/i0", data=i0, maxshape=(num, *i0.shape[1:]), compression="gzip"
                    )
                    f_i0.resize(num, axis=0)
                    f_fit = f.create_dataset(
                        "/reconstruction/fitting/data",
                        data=xrf_fit,
                        maxshape=(num, *xrf_fit.shape[1:]),
                        compression="gzip",
                    )
                    f_fit.resize(num, axis=0)
                    f.create_dataset("/reconstruction/fitting/elements", data=xrf_fit_names)

                    flag_first = False
                else:
                    if include_raw_data:
                        f_raw[i, :, :, :] = tmp_f["xrfmap"]["detsum"]["counts"]
                    f_fit[i, :, :, :] = tmp_f["xrfmap"]["detsum"]["xrf_fit"]
                    f_x[i, :, :] = tmp_f["xrfmap"]["positions"]["pos"][1, :]
                    f_y[i, :, :] = tmp_f["xrfmap"]["positions"]["pos"][0, :]
                    f_i0[i, :, :] = tmp_f["xrfmap"]["scalers"]["val"][:, :, 0]


def align_proj_com(fn, element="all", *, path="."):
    """
    Compute centers of mass of images and alignment ('delx' and 'dely') based on center of mass.
    If ``fn`` is an absolute path, then ``path`` is ignored.
    """

    fn = _process_fn(fn, fn_dir=path)

    with h5py.File(fn, "r+") as f:
        com = list([])

        N_th = f["reconstruction"]["fitting"]["data"].shape[0]
        N_el = f["reconstruction"]["fitting"]["data"].shape[1]
        for i in range(N_th):
            # Load an image
            I_tmp = np.squeeze(f["reconstruction"]["fitting"]["data"][i, :, :, :])

            # Choose the element to look at
            II = np.zeros(I_tmp.shape[1:])
            if element == "all":
                # then sum all
                II = np.sum(I_tmp, axis=0)
            else:
                # look at only that element
                for ii in range(N_el):
                    if element in f["reconstruction"]["fitting"]["elements"][ii]:
                        II = II + f["reconstruction"]["fitting"]["data"][i, ii, :, :]

            # Normalize by i0
            I0 = f["exchange"]["i0"][i]
            If = II / I0

            # need to remove any possible divide by zero, nan, inf conditions
            If = tomopy.misc.corr.remove_nan(If, val=0)

            # Calculate the center of mass of each image
            tmp_com = list(center_of_mass(If))
            if np.isfinite(tmp_com[0]) is False:
                tmp_com[0] = If.shape[0] / 2
            if np.isfinite(tmp_com[1]) is False:
                tmp_com[1] = If.shape[1] / 2
            com.append(tmp_com)

        # Write COM to h5
        try:
            f.create_dataset("reconstruction/recon/center_of_mass", data=com)
        except Exception:
            dset = f["reconstruction"]["recon"]["center_of_mass"]
            dset[...] = com

        # Calculate shift
        com = np.array(com)
        x0 = If.shape[1] / 2
        delx = -1 * np.round(com[:, 1] - x0)
        y0 = If.shape[0] / 2
        dely = 1 * np.round(com[:, 0] - y0)

        # Write shift
        try:
            f.create_dataset("reconstruction/recon/del_x", data=delx)
        except Exception:
            dset = f["reconstruction"]["recon"]["del_x"]
            dset[...] = delx
        try:
            f.create_dataset("reconstruction/recon/del_y", data=dely)
        except Exception:
            dset = f["reconstruction"]["recon"]["del_y"]
            dset[...] = dely


# # Don't use this one. Use one below in testing
# def find_rotation_center(fn, element="all"):
#     # Load the file
#     with h5py.File(fn, "r+") as f:
#         # com = list([])

#         N_th = f["reconstruction"]["fitting"]["data"].shape[0]
#         N_el = f["reconstruction"]["fitting"]["data"].shape[1]
#         for i in range(N_th):
#             # Load an image
#             I_tmp = np.squeeze(f["reconstruction"]["fitting"]["data"][i, :, :, :])

#             # Choose the element to look at
#             II = np.zeros(I_tmp.shape[1:])
#             if element == "all":
#                 # then sum all
#                 II = np.sum(I_tmp, axis=0)
#             # for ii in range(N_el):
#             #     if ('compton' in f['reconstruction']['fitting']['elements'][ii]):
#             #         continue
#             #     if ('bkg' in f['reconstruction']['fitting']['elements'][ii]):
#             #         continue
#             #     if ('adjust' in f['reconstruction']['fitting']['elements'][ii]):
#             #         continue
#             #     if ('elastic' in f['reconstruction']['fitting']['elements'][ii]):
#             #         continue
#             #     else:
#             #         II = II + f['reconstruction']['fitting']['data'][i, ii, :, :]
#             else:
#                 # look at only that element
#                 for ii in range(N_el):
#                     if element in f["reconstruction"]["fitting"]["elements"][ii]:
#                         II = II + f["reconstruction"]["fitting"]["data"][i, ii, :, :]

#             # Normalize by i0
#             I0 = f["exchange"]["i0"][i]
#             If = II / I0

#             # need to remove any possible divide by zero, nan, inf conditions
#             If = tomopy.misc.corr.remove_nan(If, val=0)

#             # Shift values
#             # try:
#             #     delx = f["reconstruction"]["recon"]["del_x"]
#             # except Exception:
#             #     delx = 0

#             # for i in range(num):
#             #     sino[i, :] = np.roll(sino[i, :], np.int(delx[i]))


# def load_images():
#     # Load the file
#     with h5py.File(fn, "r+") as f:
#         # com = list([])

#         N_th = f["reconstruction"]["fitting"]["data"].shape[0]
#         N_el = f["reconstruction"]["fitting"]["data"].shape[1]
#         for i in range(N_th):
#             # Load an image
#             I_tmp = np.squeeze(f["reconstruction"]["fitting"]["data"][i, :, :, :])

#             # Choose the element to look at
#             II = np.zeros(I_tmp.shape[1:])
#             if element == "all":
#                 # then sum all
#                 II = np.sum(I_tmp, axis=0)
#             # for ii in range(N_el):
#             #     if ('compton' in f['reconstruction']['fitting']['elements'][ii]):
#             #         continue
#             #     if ('bkg' in f['reconstruction']['fitting']['elements'][ii]):
#             #         continue
#             #     if ('adjust' in f['reconstruction']['fitting']['elements'][ii]):
#             #         continue
#             #     if ('elastic' in f['reconstruction']['fitting']['elements'][ii]):
#             #         continue
#             #     else:
#             #         II = II + f['reconstruction']['fitting']['data'][i, ii, :, :]
#             else:
#                 # look at only that element
#                 for ii in range(N_el):
#                     if element in f["reconstruction"]["fitting"]["elements"][ii]:
#                         II = II + f["reconstruction"]["fitting"]["data"][i, ii, :, :]

#             # Normalize by i0
#             I0 = f["exchange"]["i0"][i]
#             If = II / I0

#             # need to remove any possible divide by zero, nan, inf conditions
#             If = tomopy.misc.corr.remove_nan(If, val=0)


# ####################
# # testing
# def moving_translate_alignment():
#     proj = f["/reconstruction/fitting/data"][:, 4, :, :]
#     for i in np.arange(45, 132 - 1):
#         shift, _, _ = register_translation(proj[i, :, :], proj[i + 1, :, :])
#         dy, dx = shift
#         print(shift)
#         II = proj[i + 1, :, :]
#         II = fourier_shift(np.fft.fftn(II), shift)
#         II = np.fft.ifftn(II)
#         proj[i + 1, :, :] = II
#     io.imsave("Ni.tif", proj)


def get_elements(fn, *, path=".", ret=False):
    """
    Returns the list of elements loaded from the single HDF5 file.
    If ``fn`` is absolute path, then ``path`` is ignored.
    """

    fn = _process_fn(fn, fn_dir=path)

    with h5py.File(fn, "r") as f:
        elements = f["/reconstruction/fitting/elements"]

        elements = [_.decode() for _ in elements]
        if ret:
            return elements
        else:
            print(f"Elements: {elements}")


def get_recon_elements(fn, *, path=".", ret=False):
    """
    Returns the list of elements for which reconstructed volume is available in the single HDF5 file.
    If ``fn`` is absolute path, then ``path`` is ignored.
    """

    fn = _process_fn(fn, fn_dir=path)

    with h5py.File(fn, "r") as f:
        elements = f["reconstruction/recon/volume_elements"]

        elements = [_.decode() for _ in elements]
        if ret:
            return elements
        else:
            print(f"Reconstructed elements: {elements}")


def find_element(el, *, elements, select_all_elements="all"):
    """
    Find element (e.g. ``'Ca'``) or an emission line (e.g. ``'Ca_K'``) in the list of emission lines
    (e.g. ``['Ca_K', 'Si_K]``).  The function returns the index of the first element
    of the list ``elements`` that starts from ``el``.

    Parameters
    ----------
    el: str
        Element
    elements: list(str)
        The list of elements
    select_all_elements: str
        If ``el`` is equal to ``select_all_elements``, then return the total number of elements in the list

    Returns
    -------
    int or None
        Returns integer index of the element if an element is found, the number of elements in the list if
        ``el == select_all_elements`` or ``None`` if the element is not found.
    """

    if el == select_all_elements:
        return len(elements)

    el_ind = None
    for i, elem in enumerate(elements):
        if elem.startswith(el):
            el_ind = i
            break

    if el_ind is None:
        raise IndexError(f"Element '{el}' is not found in the list {elements}")

    return el_ind


# Overwriting the align_seq from tomopy
def align_seq(
    prj,
    ang,
    fdir=".",
    iters=10,
    pad=(0, 0),
    blur=True,
    center=None,
    algorithm="sirt",
    upsample_factor=10,
    rin=0.5,
    rout=0.8,
    save=False,
    debug=True,
):
    """
    Aligns the projection image stack using the sequential
    re-projection algorithm :cite:`Gursoy:17`.

    Parameters
    ----------
    prj : ndarray
        3D stack of projection images. The first dimension
        is projection axis, second and third dimensions are
        the x- and y-axes of the projection image, respectively.
    ang : ndarray
        Projection angles in radians as an array.
    iters : scalar, optional
        Number of iterations of the algorithm.
    pad : list-like, optional
        Padding for projection images in x and y-axes.
    blur : bool, optional
        Blurs the edge of the image before registration.
    center: array, optional
        Location of rotation axis.
    algorithm : {str, function}
        One of the following string values.

        'art'
            Algebraic reconstruction technique :cite:`Kak:98`.
        'gridrec'
            Fourier grid reconstruction algorithm :cite:`Dowd:99`,
            :cite:`Rivers:06`.
        'mlem'
            Maximum-likelihood expectation maximization algorithm
            :cite:`Dempster:77`.
        'sirt'
            Simultaneous algebraic reconstruction technique.
        'tv'
            Total Variation reconstruction technique
            :cite:`Chambolle:11`.
        'grad'
            Gradient descent method with a constant step size

    upsample_factor : integer, optional
        The upsampling factor. Registration accuracy is
        inversely propotional to upsample_factor.
    rin : scalar, optional
        The inner radius of blur function. Pixels inside
        rin is set to one.
    rout : scalar, optional
        The outer radius of blur function. Pixels outside
        rout is set to zero.
    save : bool, optional
        Saves projections and corresponding reconstruction
        for each algorithm iteration.
    debug : book, optional
        Provides debugging info such as iterations and error.

    Returns
    -------
    ndarray
        3D stack of projection images with jitter.
    ndarray
        Error array for each iteration.
    """

    # Needs scaling for skimage float operations.
    prj, scl = tomopy.prep.alignment.scale(prj)

    # Shift arrays
    sx = np.zeros((prj.shape[0]))
    sy = np.zeros((prj.shape[0]))

    conv = np.zeros((iters))

    # Pad images.
    npad = ((0, 0), (pad[1], pad[1]), (pad[0], pad[0]))
    prj = np.pad(prj, npad, mode="constant", constant_values=0)

    # Register each image frame-by-frame.
    for n in range(iters):
        # Reconstruct image.
        rec = tomopy.recon(prj, ang, center=center, algorithm=algorithm)

        # Re-project data and obtain simulated data.
        sim = tomopy.project(rec, ang, center=center, pad=False)

        # Blur edges.
        if blur:
            _prj = tomopy.blur_edges(prj, rin, rout)
            _sim = tomopy.blur_edges(sim, rin, rout)
        else:
            _prj = prj
            _sim = sim

        # Initialize error matrix per iteration.
        err = np.zeros((prj.shape[0]))

        # For each projection
        for m in range(prj.shape[0]):

            # Register current projection in sub-pixel precision
            shift, error, diffphase = phase_cross_correlation(_prj[m], _sim[m], upsample_factor=upsample_factor)
            err[m] = np.sqrt(shift[0] * shift[0] + shift[1] * shift[1])
            sx[m] += shift[0]
            sy[m] += shift[1]

            # Register current image with the simulated one
            tform = tf.SimilarityTransform(translation=(shift[1], shift[0]))
            prj[m] = tf.warp(prj[m], tform, order=5)

        if debug:
            print("iter=" + str(n) + ", err=" + str(np.linalg.norm(err)))
            conv[n] = np.linalg.norm(err)

        if save:
            write_tiff(prj, fdir + "/tmp/iters/prj", n)
            write_tiff(sim, fdir + "/tmp/iters/sim", n)
            write_tiff(rec, fdir + "/tmp/iters/rec", n)

    # Re-normalize data
    prj *= scl
    return prj, sx, sy, conv


def find_alignment(fn, el, *, path="."):

    path = _process_dir(path)
    fn = _process_fn(fn, fn_dir=path)

    elements = get_elements(fn, ret=True, path=path)
    try:
        el_ind = find_element(el, elements=elements)
    except IndexError as ex:
        print(f"Exception: {ex}.")
        return

    with h5py.File(fn, "a") as f:
        proj = np.copy(f["/reconstruction/recon/proj"][:, el_ind, :, :])
        proj = np.swapaxes(proj, 1, 2)
        th = np.copy(f["/exchange/theta"])

        # tomopy has an alignment method to reconstruct, back project, align, loop
        # aligned_proj, shift_y, shift_x, err = tomopy.prep.alignment.align_seq(proj, np.deg2rad(th))
        aligned_proj, shift_y, shift_x, err = align_seq(proj, np.deg2rad(th))

        # Write shift
        try:
            f.create_dataset("reconstruction/recon/del_x", data=shift_x)
        except Exception:
            dset = f["reconstruction"]["recon"]["del_x"]
            dset[...] = shift_x
        try:
            f.create_dataset("reconstruction/recon/del_y", data=shift_y)
        except Exception:
            dset = f["reconstruction"]["recon"]["del_y"]
            dset[...] = shift_y


def normalize_projections(fn, *, path="."):

    path = _process_dir(path)
    fn = _process_fn(fn, fn_dir=path)

    N = len(get_elements(fn, ret=True, path=path))

    with h5py.File(fn, "a") as f:
        proj = f["/reconstruction/fitting/data"]
        i0 = f["/exchange/i0"]

        try:
            f.create_dataset("reconstruction/recon/proj", data=proj, compression="gzip")
            dset = f["reconstruction"]["recon"]["proj"]
        except Exception:
            dset = f["reconstruction"]["recon"]["proj"]
            dset[...] = proj

        for i in range(N):
            II = dset[:, i, :, :]
            Inorm = II / i0
            Inorm = tomopy.misc.corr.remove_nan(Inorm, val=0)
            dset[:, i, :, :] = Inorm


def shift_projections(fn, *, path=".", read_only=True):

    path = _process_dir(path)
    fn = _process_fn(fn, fn_dir=path)

    f_str = "r" if read_only else "a"

    N = len(get_elements(fn, ret=True, path=path))

    with h5py.File(fn, f_str) as f:
        if read_only:
            proj = np.copy(f["/reconstruction/recon/proj"])
        else:
            proj = f["/reconstruction/recon/proj"]
        dx = f["reconstruction"]["recon"]["del_x"]
        dy = f["reconstruction"]["recon"]["del_y"]

        for i in range(N):
            II = proj[:, i, :, :]
            shift_proj = tomopy.prep.alignment.shift_images(II, dx, dy)
            proj[:, i, :, :] = shift_proj

    if read_only:
        return proj


def find_center(fn, el, *, path="."):

    path = _process_dir(path)
    fn = _process_fn(fn, fn_dir=path)

    elements = get_elements(fn, ret=True, path=path)
    try:
        el_ind = find_element(el, elements=elements)
    except IndexError as ex:
        print(f"Exception: {ex}.")
        return

    with h5py.File(fn, "a") as f:
        proj = np.copy(f["/reconstruction/recon/proj"])
        proj = np.squeeze(proj[:, el_ind, :, :])
        # proj = np.swapaxes(proj, 1, 2)
        th = np.deg2rad(np.copy(f["/exchange/theta"]))

        guess = proj.shape[2] / 2
        print(guess)
        rot_center = tomopy.find_center(proj, th, init=guess, ind=0, tol=0.5)

        # Write center
        try:
            f.create_dataset("reconstruction/recon/rot_center", data=rot_center)
        except Exception:
            dset = f["reconstruction"]["recon"]["rot_center"]
            dset[...] = rot_center

    print(f"Center of rotation found at {rot_center}")


def make_volume(fn, *, path=".", algorithm="gridrec", rotation_center=None):
    """
    Performs reconstruction using specified algorithm from ``tomopy``. The data is loaded from a single
    HDF5 file. The results are saved to the same file.

    Parameters
    ----------
        fn: str
            Name of the single HDF5 file
        path: str
            Absolute or relative path to the HDF5 file
        algorithm: str
            Name of ``tomopy`` reconstruction algorithm
        rotation_center: float or None
            Overrides `rot_center` from HDF5 file. May be useful if the rotation center can not be estimated.
    """

    path = _process_dir(path)
    fn = _process_fn(fn, fn_dir=path)

    elements = get_elements(fn, ret=True, path=path)

    with h5py.File(fn, "a") as f:
        proj = f["/reconstruction/recon/proj"]
        # Convert from mdeg to radians
        th = np.deg2rad(np.copy(f["/exchange/theta"]))
        rot_center = float(f["reconstruction/recon/rot_center"][0]) if rotation_center is None else rotation_center
        print(f"th = {th}")
        print(f"rot_center = {rot_center}")

        # need to set this up for each element... :-(
        recon_names = []
        recon = None
        for i, el in enumerate(elements):
            # do things
            # Need to check if scattered or garbage fitting and skip
            if el in ["compton", "elastic", "snip_bkg", "r_factor", "sel_cnt"]:
                continue

            el_proj = proj[:, i, :, :]
            # el_proj = np.swapaxes(np.copy(el_proj), 1, 2)
            el_recon = tomopy.recon(el_proj, th, center=rot_center, algorithm=algorithm, sinogram_order=False)
            el_recon = np.clip(el_recon, a_min=0, a_max=None)
            if recon is None:
                recon = np.copy(el_recon)
                # need to make 4-D, add an axis
                recon = np.expand_dims(recon, 0)
            else:
                recon = np.append(recon, np.expand_dims(el_recon, 0), axis=0)
            recon_names.append(elements[i])

        if recon is None:
            print("No reconstructed data is available")
        else:
            try:
                f.create_dataset("reconstruction/recon/volume", data=recon)
            except Exception:
                dset = f["reconstruction"]["recon"]["volume"]
                dset[...] = recon
            try:
                f.create_dataset("reconstruction/recon/volume_elements", data=recon_names)
            except Exception:
                dset = f["reconstruction"]["recon"]["volume_elements"]
                dset[...] = recon_names


def make_volume_svmbir(
    fn, *, path=".", center_offset=None, T=0.1, p=1.1, sharpness=4.0, snr_db=20.0, max_iterations=500
):
    """
    Performs reconstruction using ``svmbir`` algorithm. The data is loaded from a single
    HDF5 file. The results are saved to the same file.

    Parameters
    ----------
        fn: str
            Name of the single HDF5 file
        path: str
            Absolute or relative path to the HDF5 file
        center_offset: float or None
            Displacement of the rotation center from the center of the image. If ``None``, then it is
            computed based on ``rot_center`` from HDF5 file. Specifying the offset may be useful if
            the rotation center can not be estimated automatically. This is the parameter of ``svmbir``.
        T, p, sharpness, snr_db: float
            Parameters of ``svmbir`` algorithm.
    """

    try:
        import svmbir
    except ImportError as ex:
        raise ImportError("'svmbir' package is not installed: {ex}") from ex

    path = _process_dir(path)
    fn = _process_fn(fn, fn_dir=path)

    elements = get_elements(fn, ret=True, path=path)

    with h5py.File(fn, "a") as f:
        proj = f["/reconstruction/recon/proj"]
        # Convert from mdeg to radians
        th = np.deg2rad(np.copy(f["/exchange/theta"]))
        if center_offset is None:
            rot_center = float(f["reconstruction/recon/rot_center"][0])
            center_offset = proj.shape[3] / 2 - rot_center
        else:
            rot_center = proj.shape[3] / 2 + center_offset
        print(f"th = {th}")
        print(f"rot_center = {rot_center}  center_offset = {center_offset}")

        # need to set this up for each element... :-(
        recon_names = []
        recon = None
        for i, el in enumerate(elements):
            # do things
            # Need to check if scattered or garbage fitting and skip
            if el in ["compton", "elastic", "snip_bkg", "r_factor", "sel_cnt"]:
                continue

            el_proj = proj[:, i, :, :]
            # el_proj = np.swapaxes(np.copy(el_proj), 1, 2)
            el_recon = svmbir.recon(
                el_proj,
                th,
                center_offset=center_offset,
                T=T,
                p=p,
                sharpness=sharpness,
                snr_db=snr_db,
                max_iterations=max_iterations,
            )
            el_recon = np.swapaxes(np.copy(el_recon), 1, 2)
            if recon is None:
                recon = np.copy(el_recon)
                # need to make 4-D, add an axis
                recon = np.expand_dims(recon, 0)
            else:
                recon = np.append(recon, np.expand_dims(el_recon, 0), axis=0)
            recon_names.append(elements[i])

        if recon is None:
            print("No reconstructed data is available")
        else:
            try:
                f.create_dataset("reconstruction/recon/volume", data=recon)
            except Exception:
                dset = f["reconstruction"]["recon"]["volume"]
                dset[...] = recon
            try:
                f.create_dataset("reconstruction/recon/volume_elements", data=recon_names)
            except Exception:
                dset = f["reconstruction"]["recon"]["volume_elements"]
                dset[...] = recon_names


def export_tiff_projs(fn, *, fn_dir=".", tiff_dir=".", el="all", raw=True):
    """
    Save projections as a stacked TIFF file.

    Parameters
    ----------
    fn: str
        Absolute or relative name of the HDF5 file.
    fn_dir: str
        Directory that contains file ``fn``. If ``fn`` is absolute path then ``fn_dir`` is ignored.
    tiff_dir: str
        Directory where the created TIFF files are placed.
    el: str
        Element or emission line to save to save.
    raw: boolean
        Select if the raw or processed data should be saved.
    """

    fn_dir = _process_dir(fn_dir)
    fn = _process_fn(fn, fn_dir=fn_dir)
    tiff_dir = _process_dir(tiff_dir)

    elements = get_elements(fn, ret=True, path=fn_dir)
    try:
        el_ind = find_element(el, elements=elements)
    except IndexError as ex:
        print(f"Exception: {ex}.")
        return

    # Create the directory for TIFF files
    os.makedirs(tiff_dir, exist_ok=True)

    with h5py.File(fn, "r") as f:
        if raw:
            proj = f["reconstruction/fitting/data"]
        else:
            proj = f["reconstruction/recon/proj"]

        if el_ind == len(elements):
            for i, elem in enumerate(elements):
                io.imsave(os.path.join(tiff_dir, f"proj_{elem}.tif"), proj[:, i, :, :])
        else:
            io.imsave(os.path.join(tiff_dir, f"proj_{elements[el_ind]}.tif"), proj[:, el_ind, :, :])


def export_tiff_volumes(fn, *, fn_dir=".", tiff_dir=".", el="all"):
    """
    Save reconstructed slices as a stacked TIFF file.

    Parameters
    ----------
    fn: str
        Absolute or relative name of the HDF5 file.
    fn_dir: str
        Directory that contains file ``fn``. If ``fn`` is absolute path then ``fn_dir`` is ignored.
    tiff_dir: str
        Directory where the created TIFF files are placed.
    el: str
        Element or emission line to save to save.
    """

    fn_dir = _process_dir(fn_dir)
    fn = _process_fn(fn, fn_dir=fn_dir)
    tiff_dir = _process_dir(tiff_dir)

    elements = get_recon_elements(fn, ret=True, path=fn_dir)
    try:
        el_ind = find_element(el, elements=elements)
    except IndexError as ex:
        print(f"Exception: {ex}.")
        return

    # Create the directory for TIFF files
    os.makedirs(tiff_dir, exist_ok=True)

    with h5py.File(fn, "r") as f:
        recon = f["reconstruction/recon/volume"]

        if el_ind == len(elements):
            for i, elem in enumerate(elements):
                io.imsave(os.path.join(tiff_dir, f"vol_{elem}.tif"), recon[i, :, :, :])
        else:
            io.imsave(os.path.join(tiff_dir, f"vol_{elements[el_ind]}.tif"), recon[el_ind, :, :, :])
