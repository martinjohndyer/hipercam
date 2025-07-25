import copy
import os
import signal
import sys
import warnings

import numpy as np
from astropy.stats import sigma_clip
from astropy.wcs import wcs
from trm import cline
from trm.cline import Cline

import hipercam as hcam

HAS_REPROJECT = True
try:
    from reproject import reproject_adaptive, reproject_exact, reproject_interp
except (ImportError, ModuleNotFoundError):
    HAS_REPROJECT = False


try:
    import bottleneck as bn

    meanfunc = bn.nanmean
    medianfunc = bn.nanmedian
except ImportError:
    meanfunc = np.nanmean
    medianfunc = np.nanmedian

__all__ = [
    "shiftadd",
]


class CleanUp:
    """
    Context manager to handle temporary files
    """

    def __init__(self, flist, temp):
        self.flist = flist
        self.temp = temp

    def _sigint_handler(self, signal_received, frame):
        print("\nshiftadd aborted")
        sys.exit(1)

    def __enter__(self):
        signal.signal(signal.SIGINT, self._sigint_handler)

    def __exit__(self, type, value, traceback):
        if self.temp:
            with open(self.flist) as fp:
                for line in fp:
                    os.remove(line.strip())
            os.remove(self.flist)
            print("temporary files removed")


def new_wcs(wbase, dx, dy):
    """
    Shifts an image by dx, dy
    """
    wnew = copy.deepcopy(wbase)
    wnew.wcs.crpix = [-dx, -dy]
    return wnew


def wcs_from_header(mccd):
    """
    Try and make a WCS object from the header of an MCCD.
    """
    header = mccd.head

    # find pixel limits of all windows
    for cnam, ccd in mccd.items():
        for n, wind in enumerate(ccd.values()):
            if n == 0:
                llxmin = wind.llx
                llymin = wind.lly
                urxmax = wind.urx
                urymax = wind.ury
                xbin = wind.xbin
                ybin = wind.ybin
            else:
                # Track overall dimensions
                llxmin = min(llxmin, wind.llx)
                llymin = min(llymin, wind.lly)
                urxmax = max(urxmax, wind.urx)
                urymax = max(urymax, wind.ury)

    ZEROPOINT = 209.7  # rotator zeropoint
    SCALE = 0.081  # pixel scale, "/unbinned pixel

    # ra, dec in degrees at rotator centre
    ra = header["RADEG"]
    dec = header["DECDEG"]

    # position angle, degrees
    pa = header["INSTRPA"] - ZEROPOINT
    x0, y0 = (
        (1020.0 - llxmin + 1) / xbin,
        (524.0 - llymin + 1) / ybin,
    )
    w = wcs.WCS(naxis=2)
    w.wcs.crpix = [x0, y0]
    w.wcs.crval = [ra, dec]
    cpa = np.cos(np.radians(pa))
    spa = np.sin(np.radians(pa))
    cd = np.array([[xbin * cpa, ybin * spa], [-xbin * spa, ybin * cpa]])
    cd *= SCALE / 3600
    w.wcs.cd = cd
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.cunit = ["deg", "deg"]
    return w


def shiftadd(args=None):
    """
    ``shiftadd [source]  (run first last twait tmax | flist) rfilen refccd
    fthresh reprmethod [(reprorder | consflux reprkernel kwidth regwidth)]
    trim ([ncol nrow]) method [(sigma maxiters)] [overwrite] output``

    Averages images from a run using mean combination, but shifting each
    image based on the positions of stars.

    Parameters:

        source : str [hidden]
           Data source, five options:

              | 'hs' : HiPERCAM server
              | 'hl' : local HiPERCAM FITS file
              | 'us' : ULTRACAM server
              | 'ul' : local ULTRACAM .xml/.dat files
              | 'hf' : list of HiPERCAM hcm FITS-format files

           'hf' is used to look at sets of frames generated by 'grab'
           or converted from foreign data formats. The standard
           start-off default for ``source`` can be set using the
           environment variable HIPERCAM_DEFAULT_SOURCE. e.g. in bash
           :code:`export HIPERCAM_DEFAULT_SOURCE="us"` would ensure it
           always started with the ULTRACAM server by default. If
           unspecified, it defaults to 'hl'.

        run : str [if source ends 's' or 'l']
           run number to access, e.g. 'run034'

        flist : str [if source ends 'f']
           name of file list

        first : int [if source ends 's' or 'l']
           exposure number to start from. 1 = first frame ('0' is
           not supported).

        last : int [if source ends 's' or 'l']
           last exposure number must be >= first, or 0 for the lot

        twait : float [if source ends 's' or 'l'; hidden]
           time to wait between attempts to find a new exposure, seconds.

        tmax : float [if source ends 's' or 'l'; hidden]
           maximum time to wait between attempts to find a new exposure,
           seconds.

        rfilen : str
            name of reduce file.

        refccd : str
            reference CCD to use for finding offsets

        fthresh : float
            maximum FWHM to allow, in unbinned pixels on the reference CCD.
            frames with FWHM > fthresh will be ignored.

        reprmethod : str
            Method to use for reprojecting the data, three options:

                | 'interp': the fastest, but does not conserve flux
                | 'adaptive': slower, but conserves flux
                | 'exact': the slowest but most accurate, only available
                  if the input data contains WCS info (currently HiPERCAM only)

            The default is 'adaptive'.
            See https://reproject.readthedocs.io for details.

        reprorder : int [if reprmethod is 'interp'; hidden]
            Order of interpolation to use. 0 is nearest neighbour, 1 is
            bilinear, 2 is quadratic, 3 is cubic. 1 is the default.

        consflux : bool [if reprmethod is 'adaptive'; hidden]
            conserve flux when reprojecting. True is the default.

        reprkernel : str [if reprmethod is 'adaptive'; hidden]
            The averaging kernel to use. Allowed values are 'Hann' and 'Gaussian'.
            The Gaussian kernel produces better photometric accuracy and stronger
            anti-aliasing at the cost of some blurring (on the scale of a few
            pixels). If not specified, the Gaussain kernel is used by default.

        kwidth : float [if reprmethod is 'adaptive'; hidden]
            The width of the kernel in pixels, expressed as the standard
            deviation of the Gaussian kernel. Is not used for the Hann kernel.
            Reducing this width may introduce photometric errors or leave input
            pixels under-sampled, while increasing it may improve the degree of
            anti-aliasing but will increase blurring of the output image.
            If this width is changed from the default, a proportional change
            should be made to the value of regwidth to maintain an
            equivalent degree of photometric accuracy. Default is 1.3.

        regwidth : float [if reprmethod is 'adaptive'; hidden]
            The width in pixels of the output-image region which, when
            transformed to the input plane, defines the region to be sampled
            for each output pixel.  Used only for the Gaussian kernel, which
            otherwise has infinite extent. This value sets a trade-off between
            accuracy and computation time, with better accuracy at higher values.
            The default value of 4, with the default kernel width,
            should limit the most extreme errors to less than one percent.
            Higher values will offer even more photometric accuracy.

        trim : bool
           True to trim columns and/or rows off the edges of windows nearest
           the readout. Useful for ULTRACAM particularly.

        ncol : int [if trim, hidden]
           Number of columns to remove (on left of left-hand window, and right
           of right-hand windows)

        nrow : int [if trim, hidden]
           Number of rows to remove (bottom of windows)

        method : str [hidden, defaults to 'm']
           'm' for median, 'c' for clipped mean.

        sigma : float [hidden; if method == 'c']
           With clipped mean combination, pixels that deviate by more than
           sigma RMS from the mean are kicked out. This is carried out in an
           iterative manner. sigma <= 0 implies no rejection, just a straight
           average. sigma=3 is typical.

        maxiters : int [hidden; if method == 'c']
            Maximum number of iterations in sigma clipping. 3 is typical.

        overwrite : bool [hidden]
           overwrite any pre-existing output files

        output  : string
           output file
    """
    # can we run at all (need reproject)?
    if not HAS_REPROJECT:
        raise hcam.HipercamError("reproject module not available, cannot run shiftadd")

    command, args = cline.script_args(args)
    # get the inputs
    with Cline("HIPERCAM_ENV", ".hipercam", command, args) as cl:
        # register parameters
        cl.register("source", Cline.GLOBAL, Cline.HIDE)
        cl.register("run", Cline.GLOBAL, Cline.PROMPT)
        cl.register("first", Cline.LOCAL, Cline.PROMPT)
        cl.register("last", Cline.LOCAL, Cline.PROMPT)
        cl.register("twait", Cline.LOCAL, Cline.HIDE)
        cl.register("tmax", Cline.LOCAL, Cline.HIDE)
        cl.register("flist", Cline.LOCAL, Cline.PROMPT)

        cl.register("rfilen", Cline.LOCAL, Cline.PROMPT)
        cl.register("refccd", Cline.LOCAL, Cline.PROMPT)
        cl.register("fthresh", Cline.LOCAL, Cline.HIDE)

        cl.register("reprmethod", Cline.LOCAL, Cline.PROMPT)
        cl.register("reprorder", Cline.LOCAL, Cline.HIDE)
        cl.register("consflux", Cline.LOCAL, Cline.HIDE)
        cl.register("reprkernel", Cline.LOCAL, Cline.HIDE)
        cl.register("kwidth", Cline.LOCAL, Cline.HIDE)
        cl.register("regwidth", Cline.LOCAL, Cline.HIDE)

        cl.register("trim", Cline.GLOBAL, Cline.PROMPT)
        cl.register("ncol", Cline.GLOBAL, Cline.HIDE)
        cl.register("nrow", Cline.GLOBAL, Cline.HIDE)

        cl.register("method", Cline.LOCAL, Cline.HIDE)
        cl.register("sigma", Cline.LOCAL, Cline.HIDE)
        cl.register("maxiters", Cline.LOCAL, Cline.HIDE)
        cl.register("overwrite", Cline.LOCAL, Cline.HIDE)
        cl.register("output", Cline.LOCAL, Cline.PROMPT)

        # get inputs
        default_source = os.environ.get("HIPERCAM_DEFAULT_SOURCE", "hl")
        source = cl.get_value(
            "source",
            "data source [hs, hl, us, ul, hf]",
            default_source,
            lvals=("hs", "hl", "us", "ul", "hf"),
        )
        # set a flag
        server_or_local = source.endswith("s") or source.endswith("l")

        if server_or_local:
            resource = cl.get_value("run", "run name", "run005")

            first = cl.get_value("first", "first frame to average", 1, 1)
            last = cl.get_value("last", "last frame to average", first, 0)

            twait = cl.get_value(
                "twait", "time to wait for a new frame [secs]", 1.0, 0.0
            )
            tmax = cl.get_value(
                "tmax", "maximum time to wait for a new frame [secs]", 10.0, 0.0
            )

        else:
            resource = cl.get_value(
                "flist", "file list", cline.Fname("files.lis", hcam.LIST)
            )
            first = 1

        rfilen = cl.get_value(
            "rfilen", "reduce file", cline.Fname("reduce.red", hcam.RED)
        )

        ref_cnam = cl.get_value(
            "refccd", "reference CCD to use to measure offsets", "3"
        )

        fthresh = cl.get_value(
            "fthresh",
            "maximum FWHM to allow, -ve to accept all",
            -1.0,
        )

        reprmethod = cl.get_value(
            "reprmethod",
            "Method to use for reprojecting the data",
            "adaptive",
            lvals=("interp", "adaptive", "exact"),
        )
        if reprmethod == "interp":
            reprorder = cl.get_value(
                "reprorder",
                "Order of interpolation to use (0=nearest neighbour)",
                1,
                0,
            )
        elif reprmethod == "adaptive":
            consflux = cl.get_value("consflux", "conserve flux when reprojecting", True)
            reprkernel = cl.get_value(
                "reprkernel",
                "The averaging kernel to use",
                "Gaussian",
                lvals=("Hann", "Gaussian"),
            )
            if reprkernel == "Gaussian":
                kwidth = cl.get_value(
                    "kwidth",
                    "The width of the kernel in pixels",
                    1.3,
                    0.0,
                )
                regwidth = cl.get_value(
                    "regwidth",
                    "The width in pixels of the output-image region",
                    4,
                    0.0,
                )

        trim = cl.get_value("trim", "do you want to trim edges of windows?", True)
        if trim:
            ncol = cl.get_value("ncol", "number of columns to trim from windows", 0)
            nrow = cl.get_value("nrow", "number of rows to trim from windows", 0)

        cl.set_default("method", "m")
        method = cl.get_value(
            "method", "c(lipped mean), m(edian)", "c", lvals=("c", "m")
        )

        if method == "c":
            sigma = cl.get_value("sigma", "number of RMS deviations to clip", 3.0)
            maxiters = cl.get_value(
                "maxiters", "maximum number of clipping iterations", 3
            )

        overwrite = cl.get_value(
            "overwrite", "overwrite any pre-existing files on output", False
        )
        outfile = cl.get_value(
            "output",
            "output file",
            cline.Fname(
                "hcam",
                hcam.HCAM,
                cline.Fname.NEW if overwrite else cline.Fname.NOCLOBBER,
            ),
        )

    # inputs done with.
    rfile = hcam.reduction.Rfile.read(rfilen)
    if server_or_local:
        print("\nCalling 'grab' ...")

        # Build argument list
        args = [None, "prompt", source, "yes", resource]
        if server_or_local:
            args += [str(first), str(last), str(twait), str(tmax)]
        if trim:
            args += ["yes", str(ncol), str(nrow)]
        else:
            args += ["no"]
        calsec = rfile["calibration"]
        args += [
            "none" if not calsec["bias"] else calsec["bias"],
            "none" if not calsec["dark"] else calsec["dark"],
            "none" if not calsec["flat"] else calsec["flat"],
        ]
        if not calsec["fmap"]:
            args += ["none", "f32"]
        else:
            args += [
                calsec["fmap"],
                calsec["fpair"],
                str(calsec["nhalf"]),
                str(calsec["rmin"]),
                str(calsec["rmax"]),
                "false",
                "f32",
            ]
        resource = hcam.scripts.grab(args)

    # at this point 'resource' is a list of files, no matter the input
    # method.
    with CleanUp(resource, server_or_local):
        # First we want to calculate how offset each frame is relative to the apertures
        print(f"calculating pixel shifts using CCD {ref_cnam}")
        offsets = []
        fwhm_values = []
        mjds = []
        xoff, yoff = 0.0, 0.0
        with hcam.spooler.HcamListSpool(resource) as spool:
            # Note we don't just open the ref_ccd, we need the full mccd for `initial_checks`
            for nf, mccd in enumerate(spool):
                print("fitting apertures to frame", nf + first)
                ccd = mccd[ref_cnam]

                # If the ref CCD has no data in this frame (e.g. due to nskip>1)
                # then we can't calculate an offset, so you need to use
                # a different CCD as a reference
                if not ccd.is_data():
                    if not any([mccd[cnam].is_data() for cnam in mccd]):
                        # this frame has no data in any CCD (?), so we can safely skip it
                        continue
                    raise hcam.HipercamError(
                        f"frame {nf + first} has no data in CCD {ref_cnam}, "
                        f"so cannot be used as refccd"
                    )

                # Find which window contains each aperture
                ccdwin = {}
                for apnam, aper in rfile.aper[ref_cnam].items():
                    for wnam, wind in ccd.items():
                        if wind.distance(aper.x, aper.y) > 0:
                            ccdwin[apnam] = wnam
                            break
                        else:
                            ccdwin[apnam] = None

                # Reposition the apertures on the reference CCD
                store = {"mfwhm": -1.0, "mbeta": -1.0}
                read, gain, ok = hcam.reduction.initial_checks(mccd, rfile)
                hcam.reduction.moveApers(
                    ref_cnam,
                    ccd,
                    ccd,
                    read[ref_cnam],
                    gain[ref_cnam],
                    ccdwin,
                    rfile,
                    store,
                )

                # Store the mean FWHM, as well as the image date
                fwhm_values.append(store["mfwhm"])
                mjds.append(mccd.head["MJDUTC"])

                # Find the mean shifts (only using reference stars)
                dx = np.mean(
                    [
                        store[apnam]["dx"]
                        for apnam in rfile.aper[ref_cnam]
                        if rfile.aper[ref_cnam][apnam].ref
                    ]
                )
                dy = np.mean(
                    [
                        store[apnam]["dy"]
                        for apnam in rfile.aper[ref_cnam]
                        if rfile.aper[ref_cnam][apnam].ref
                    ]
                )

                # Store the offsets relative to the previous frame
                xoff += dx
                yoff += dy
                offsets.append((xoff, yoff))

        # Offsets are now defined w.r.t to the positions in the
        # aperture file for first image, and then w.r.t to
        # previous file for subsequent images. For each CCD,
        # we should center the offsets on 0,0. This minimises the
        # risks of having unsampled pixels in the full frame data.
        offsets = np.array(offsets)
        offsets -= offsets.mean(axis=0)

        # Now find the CCD with the smallest offset from
        # the mean and subtract its offset from all frames,
        # so we guarantee one frame is centred on exactly (0, 0)
        total_offsets = np.sum(np.abs(offsets), axis=1)
        offsets -= offsets[np.argmin(total_offsets)]

        # Use the first file as a template
        with open(resource) as f:
            first_frame = f.readline().strip()
            output_mccd = hcam.MCCD.read(first_frame)
        try:
            # make real celestial WCS from header
            orig_wcs = wcs_from_header(output_mccd)
        except Exception as err:
            # fallback to basic WCS which can be used for any method except 'exact'
            if reprmethod == "exact":
                raise hcam.HipercamError(
                    "failed to create WCS from header, cannot use 'exact' reprojection method"
                ) from err
            # start with basic WCS, CRDELT1, no offsets
            orig_wcs = wcs.WCS(naxis=2)

        # Now process each file CCD by CCD to reduce the memory footprint
        header_string = "nframes="
        for cnam in output_mccd:
            print(f"stacking CCD {cnam}")
            arrs = []
            nframes_used = 0
            with hcam.spooler.HcamListSpool(resource, cnam) as spool:
                # Here we only open the CCD we're interested in
                for nf, ccd in enumerate(spool):
                    if not ccd.is_data():
                        continue

                    # Skip if FWHM is above threshold
                    if fthresh > 0 and fwhm_values[nf] > fthresh:
                        print(
                            f"skipping frame {nf + first}",
                            f"(FWHM too large ({fwhm_values[nf]:.1f} > {fthresh:.1f}))",
                        )
                        continue

                    print("resampling frame", nf + first)
                    nframes_used += 1

                    # Find calculated offset
                    frame_offset_x, frame_offset_y = offsets[nf]

                    # Find output shape to resample each window onto (binned)
                    # full frame array
                    output_shape = (
                        ccd.nytot // ccd.head.ybin,
                        ccd.nxtot // ccd.head.xbin,
                    )

                    # Go through each window in the CCD
                    for wnam, wind in ccd.items():
                        # Make WCS to reproject data onto full frame
                        # (binned) array
                        pixel_wcs = copy.deepcopy(orig_wcs)
                        crval1, crval2 = pixel_wcs.wcs.crpix
                        window_offset_x = wind.llx // wind.xbin
                        window_offset_y = wind.lly // wind.ybin
                        pixel_wcs.wcs.crpix = (
                            crval1 + frame_offset_x / wind.xbin - window_offset_x,
                            crval2 + frame_offset_y / wind.ybin - window_offset_y,
                        )

                        # Carry out the re-projection
                        if reprmethod == "interp":
                            reprojected_data, _ = reproject_interp(
                                (wind.data, pixel_wcs),
                                orig_wcs,
                                output_shape,
                                order=reprorder,
                            )
                        elif reprmethod == "exact":
                            reprojected_data, _ = reproject_exact(
                                (wind.data, pixel_wcs),
                                orig_wcs,
                                output_shape,
                            )
                        # OK - we are using adaptive then
                        # note we force boundary_mode to 'nearest'
                        # to avoid NaNs around the edge of the output
                        elif reprkernel == "Hann":
                            reprojected_data, _ = reproject_adaptive(
                                (wind.data, pixel_wcs),
                                orig_wcs,
                                output_shape,
                                conserve_flux=consflux,
                                kernel="Hann",
                                boundary_mode="nearest",
                            )
                        else:
                            reprojected_data, _ = reproject_adaptive(
                                (wind.data, pixel_wcs),
                                orig_wcs,
                                output_shape,
                                conserve_flux=consflux,
                                kernel_width=kwidth,
                                sample_region_width=regwidth,
                                kernel="Gaussian",
                                boundary_mode="nearest",
                            )

                        # We need to mask the area outside of the offset window with NaNs,
                        # so there's no bleedover into other windows when stacking
                        # when using 'nearest' interpolation for adaptive
                        mask = np.zeros_like(reprojected_data, dtype=bool)
                        xstart = wind.llx // wind.xbin
                        xend = xstart + wind.nx
                        ystart = wind.lly // wind.ybin
                        yend = ystart + wind.ny
                        mask[
                            ystart - int(frame_offset_y) : yend - int(frame_offset_y),
                            xstart - int(frame_offset_x) : xend - int(frame_offset_x),
                        ] = True
                        reprojected_data[~mask] = np.nan

                        # Save the FF reprojected data
                        arrs.append(reprojected_data)

            # Average over the stack of FF images
            print(f"combining {nframes_used} frames for CCD {cnam}")
            header_string += f"CCD{cnam}({nframes_used:d}),"

            if len(arrs) == 0 or nframes_used == 0:
                raise hcam.HipercamError(
                    f"found no data for CCD {cnam} in the selected frames"
                )

            arr3d = np.stack(arrs)

            with warnings.catch_warnings():
                # ignore warnings about all-nan slices
                # (we set NaNs outside of the windows)
                warnings.filterwarnings(
                    "ignore",
                    category=RuntimeWarning,
                    message="All-NaN slice encountered",
                )
                if method == "m":
                    stack = medianfunc(arr3d, axis=0)
                elif method == "c" and sigma > 0:
                    # clipped mean
                    mask = sigma_clip(
                        arr3d,
                        sigma_lower=sigma,
                        sigma_upper=sigma,
                        axis=0,
                        copy=False,
                        maxiters=maxiters,
                        cenfunc="mean",
                        stdfunc="std",
                        masked=True,
                    )
                    # fill mask with nans
                    arr3d[mask.mask] = np.nan
                    stack = meanfunc(arr3d, axis=0)
                else:
                    # simple mean
                    stack = meanfunc(arr3d, axis=0)

            # Crop the FF images back into the individual windows
            for wnam, wind in output_mccd[cnam].items():
                xstart = wind.llx // wind.xbin
                xend = xstart + wind.nx
                ystart = wind.lly // wind.ybin
                yend = ystart + wind.ny
                crop = stack[ystart:yend, xstart:xend]

                # check for NaNs in the cropped data
                if np.isnan(crop).any():
                    # The pipeline can't really handle NaNs, so we raise an error
                    raise hcam.HipercamError(
                        f"NaN values detected in combined data for CCD {cnam}, window {wnam}"
                    )

                output_mccd[cnam][wnam].data = crop

        # Add history and other keywords to the header
        output_mccd.head.add_history("Result of shiftadd")
        reprmethod_string = reprmethod
        if reprmethod == "interp":
            reprmethod_string += f" ({reprorder})"
        elif reprmethod == "adaptive":
            if reprkernel == "Gaussian":
                reprmethod_string += (
                    f" ({reprkernel},{kwidth:.1f},{regwidth:.1f},{consflux})"
                )
            else:
                reprmethod_string += f" ({reprkernel},{consflux})"
        output_mccd.head.add_history("Reproject method: " + reprmethod_string)
        if method == "m":
            output_mccd.head.add_history("Median stack: " + header_string[:-1])
        else:
            output_mccd.head.add_history(
                f"Clipped mean stack ({sigma:.1f} sigma): " + header_string[:-1]
            )
        mid_time = np.min(mjds) + 0.5 * (np.max(mjds) - np.min(mjds))
        output_mccd.head["MJDUTC"] = mid_time

        # Write out
        output_mccd.write(outfile, overwrite=overwrite)
        print(f"Written {outfile}")
