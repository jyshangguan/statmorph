"""
Python code for calculating non-parametric morphological diagnostics
of galaxy images.
"""
# Author: Vicente Rodriguez-Gomez <vrg@jhu.edu>
# Licensed under a 3-Clause BSD License.

import numpy as np
import time
import scipy.optimize as opt
import scipy.ndimage as ndi
import skimage.measure
import skimage.transform
import skimage.feature
import skimage.morphology
from astropy.utils import lazyproperty
from astropy.stats import sigma_clipped_stats
from astropy.modeling import models, fitting
import warnings
import photutils

__all__ = ['SourceMorphology', 'source_morphology']

def _quantile(sorted_values, q):
    """
    For a sorted (in increasing order) 1-d array, return the
    value corresponding to the quantile ``q``.
    """
    assert((q >= 0) and (q <= 1))
    if q == 1:
        return sorted_values[-1]
    else:
        return sorted_values[int(q*len(sorted_values))]

def _mode(a, axis=None):
    """
    Takes a masked array as input and returns the "mode"
    as defined in Bertin & Arnouts (1996):
    mode = 2.5 * median - 1.5 * mean
    """
    return 2.5*np.ma.median(a, axis=axis) - 1.5*np.ma.mean(a, axis=axis)

def _aperture_area(ap, mask, **kwargs):
    """
    Calculate the area of a photutils aperture object,
    excluding masked pixels.
    """
    return ap.do_photometry(np.float64(~mask), **kwargs)[0][0]

def _aperture_mean_nomask(ap, image, **kwargs):
    """
    Calculate the mean flux of an image for a given photutils
    aperture object. Note that we do not use ``_aperture_area``
    here. Instead, we divide by the full area of the
    aperture, regardless of masked and out-of-range pixels.
    This avoids problems when the aperture is larger than the
    region of interest.
    """
    return ap.do_photometry(image, **kwargs)[0][0] / ap.area()

def _fraction_of_total_function(r, image, center, fraction, total_sum):
    """
    Helper function to calculate ``_radius_at_fraction_of_total``.
    """
    ap = photutils.CircularAperture(center, r)
    # Force flux sum to be positive:
    ap_sum = np.abs(ap.do_photometry(image, method='exact')[0][0])

    return ap_sum / total_sum - fraction

def _radius_at_fraction_of_total(image, center, r_total, fraction):
    """
    Return the radius (in pixels) of a concentric circle that
    contains a given fraction of the light within ``r_total``.
    """
    flag = 0  # flag=1 indicates a problem

    # Make sure that center is at center of pixel (otherwise,
    # very compact objects can become problematic):
    center = np.floor(center) + 0.5

    ap_total = photutils.CircularAperture(center, r_total)

    total_sum = ap_total.do_photometry(image, method='exact')[0][0]
    if total_sum <= 0:
        print('Warning: total flux sum is not positive.')
        flag = 1
        return np.nan, flag

    # Find appropriate range for root finder
    npoints = 100
    r_inner = min(0.5, 0.2*r_total)  # just the central pixel
    assert(r_total > r_inner)
    r_grid = np.linspace(r_inner, r_total, num=npoints)
    r_min, r_max = None, None
    i = 0  # initial value
    while True:
        if i >= npoints:
            raise Exception('Root not found within range.')
        r = r_grid[i]
        curval = _fraction_of_total_function(r, image, center, fraction, total_sum)
        if curval == 0:
            print('Warning: found root by pure chance!')
            return r, flag
        elif curval < 0:
            r_min = r
        elif curval > 0:
            if r_min is None:
                print('Warning: r_min is not defined yet.')
                flag = 1
            else:
                r_max = r
                break
        i += 1

    r = opt.brentq(_fraction_of_total_function, r_min, r_max,
                   args=(image, center, fraction, total_sum), xtol=1e-6)

    return r, flag


class SourceMorphology(object):
    """
    Class to measure the morphological parameters of a single labeled
    source. The parameters can be accessed as attributes or keys.

    Parameters
    ----------
    image : array-like
        A 2D image containing the sources of interest.
        The image must already be background-subtracted.
    segmap : array-like (int) or ``photutils.SegmentationImage``
        A 2D segmentation map where different sources are 
        labeled with different positive integer values.
        A value of zero is reserved for the background.
    label : int
        A label indicating the source of interest.
    mask : array-like (bool), optional
        A 2D array with the same size as ``image``, where pixels
        set to ``True`` are ignored from all calculations.
    variance : array-like, optional
        A 2D array with the same size as ``image`` with the
        values of the variance (RMS^2). This is required
        in order to calculate the signal-to-noise correctly.
    cutout_extent : float, optional
        The target fractional size of the data cutout relative to
        the minimal bounding box containing the source. The value
        must be >= 1.
    remove_outliers : bool, optional
        If ``True``, remove outlying pixels as described in Lotz et al.
        (2004), using the parameter ``n_sigma_outlier``.
    n_sigma_outlier : scalar, optional
        The number of standard deviations that define a pixel as an
        outlier, relative to its 8 neighbors. This parameter only
        takes effect when ``remove_outliers`` is ``True``. The default
        value is 10.
    annulus_width : float, optional
        The width (in pixels) of the annuli used to calculate the
        Petrosian radius and other quantities. The default value is 1.0.
    eta : float, optional
        The Petrosian ``eta`` parameter used to define the Petrosian
        radius. For a circular or elliptical aperture at the Petrosian
        radius, the mean flux at the edge of the aperture divided by
        the mean flux within the aperture is equal to ``eta``. The
        default value is typically set to 0.2 (Petrosian 1976).
    petro_fraction_gini : float, optional
        In the Gini calculation, this is the fraction of the Petrosian
        "radius" used as a smoothing scale in order to define the pixels
        that belong to the galaxy. The default value is 0.2.
    border_size : int, optional
        The number of pixels that are skipped from each border of the
        "postage stamp" image cutout when finding the skybox. The
        default is 5 pixels.
    skybox_size : int, optional
        The size in pixels of the (square) "skybox" used to measure
        properties of the image background.
    petro_extent_circ : float, optional
        The radius of the circular aperture used for the asymmetry
        calculation, in units of the circular Petrosian radius. The
        default value is 1.5.
    petro_fraction_cas : float, optional
        In the CAS calculations, this is the fraction of the Petrosian
        radius used as a smoothing scale. The default value is 0.25.
    boxcar_size_mid : float, optional
        In the MID calculations, this is the size (in pixels)
        of the constant kernel used to regularize the MID segmap.
        The default value is 3.0.
    niter_bh_mid : int, optional
        When calculating the multimode statistic, this is the number of
        iterations in the basin-hopping stage of the maximization.
        A value of at least 100 is recommended for "production" runs,
        but it can be time-consuming. The default value is 5.
    sigma_mid : float, optional
        In the MID calculations, this is the smoothing scale (in pixels)
        used to compute the intensity (I) statistic. The default is 1.0.
    petro_extent_ellip : float, optional
        The inner radius, in units of the Petrosian "radius", of the
        elliptical aperture used to estimate the sky background in the
        shape asymmetry calculation. The default value is 1.5.
    boxcar_size_shape_asym : float, optional
        When calculating the shape asymmetry segmap, this is the size
        (in pixels) of the constant kernel used to regularize the segmap.
        The default value is 3.0.
    segmap_overlap_ratio : float, optional
        The minimum ratio between the area of the intersection of
        all 3 segmaps and the area of the largest segmap in order to
        have a good measurement.

    References
    ----------
    See `README.md` for a list of references.

    """
    def __init__(self, image, segmap, label, mask=None, variance=None,
                 cutout_extent=1.5, remove_outliers=True, n_sigma_outlier=10,
                 annulus_width=1.0, eta=0.2, petro_fraction_gini=0.2,
                 border_size=4, skybox_size=32, petro_extent_circ=1.5,
                 petro_fraction_cas=0.25, boxcar_size_mid=3.0,
                 niter_bh_mid=5, sigma_mid=1.0, petro_extent_ellip=2.0,
                 boxcar_size_shape_asym=3.0, segmap_overlap_ratio=0.5):
        self._variance = variance
        self._cutout_extent = cutout_extent
        self._remove_outliers = remove_outliers
        self._n_sigma_outlier = n_sigma_outlier
        self._annulus_width = annulus_width
        self._eta = eta
        self._petro_fraction_gini = petro_fraction_gini
        self._border_size = border_size
        self._skybox_size = skybox_size
        self._petro_extent_circ = petro_extent_circ
        self._petro_fraction_cas = petro_fraction_cas
        self._boxcar_size_mid = boxcar_size_mid
        self._niter_bh_mid = niter_bh_mid
        self._sigma_mid = sigma_mid
        self._petro_extent_ellip = petro_extent_ellip
        self._boxcar_size_shape_asym = boxcar_size_shape_asym
        self._segmap_overlap_ratio = segmap_overlap_ratio

        # If there are nan or inf values, set them to zero and
        # add them to the mask.
        locs_invalid = ~np.isfinite(image)
        if variance is not None:
            locs_invalid = locs_invalid | ~np.isfinite(variance)
            variance[locs_invalid] = 0.0
        image[locs_invalid] = 0.0
        if mask is None:
            mask = np.zeros(image.shape, dtype=np.bool8)
        mask = mask | locs_invalid

        # Before doing anything else, remove "bad pixels" (outliers):
        self.num_badpixels = -1
        if self._remove_outliers:
            image, self.num_badpixels = self._remove_badpixels(image)

        # The following object stores some important data:
        self._props = photutils.SourceProperties(image, segmap, label, mask=mask)

        # The following properties may be modified by other functions:
        self.flag = 0  # attempts to flag bad measurements
        self.flag_sersic = 0  # attempts to flag bad Sersic fits

        # Position of the "postage stamp" cutout:
        self._xmin_stamp = self._slice_stamp[1].start
        self._ymin_stamp = self._slice_stamp[0].start
        self._xmax_stamp = self._slice_stamp[1].stop - 1
        self._ymax_stamp = self._slice_stamp[0].stop - 1

        # Centroid of the source relative to the "postage stamp" cutout:
        self._xc_stamp = self._props.xcentroid.value - self._xmin_stamp
        self._yc_stamp = self._props.ycentroid.value - self._ymin_stamp

        # Print warning if centroid is masked:
        ic, jc = int(self._yc_stamp), int(self._xc_stamp)
        if self._cutout_stamp_maskzeroed[ic, jc] == 0:
            print('Warning: Centroid is masked.')
            self.flag = 1

        # Position of the brightest pixel relative to the cutout:
        self._x_maxval_stamp = self._props.maxval_xpos.value - self._slice_stamp[1].start
        self._y_maxval_stamp = self._props.maxval_ypos.value - self._slice_stamp[0].start

        # For now, evaluate all "lazy" properties during initialization:
        self._calculate_morphology()

        # Check segmaps and set flag=1 if they are very different
        self._check_segmaps()

    def __getitem__(self, key):
        return getattr(self, key)

    def _remove_badpixels(self, image):
        """
        Remove outliers (bad pixels) as described in Lotz et al. (2004).

        Notes
        -----
        ndi.generic_filter(image, np.std, ...) is too slow,
        so we do a workaround using ndi.convolve.
        """
        # Pixel weights, excluding central pixel.
        w = np.array([
            [1, 1, 1],
            [1, 0, 1],
            [1, 1, 1]], dtype=np.float64)
        w = w / np.sum(w)
        
        # Use the fact that var(x) = <x^2> - <x>^2.
        local_mean = ndi.convolve(image, w)
        local_mean2 = ndi.convolve(image**2, w)
        local_std = np.sqrt(local_mean2 - local_mean**2)

        # Set "bad pixels" to zero.
        bad_pixels = (np.abs(image - local_mean) >
                      self._n_sigma_outlier * local_std)
        image[bad_pixels] = 0.0
        num_badpixels = np.sum(bad_pixels)

        return image, num_badpixels

    def _calculate_morphology(self):
        """
        Calculate all morphological parameters, which are stored
        as "lazy" properties.
        """
        quantities = [
            'rpetro_circ',
            'rpetro_ellip',
            'gini',
            'm20',
            'sn_per_pixel',
            'concentration',
            'asymmetry',
            'smoothness',
            'multimode',
            'intensity',
            'deviation',
            'half_light_radius',
            'rmax',
            'outer_asymmetry',
            'shape_asymmetry',
            'sersic_index',
        ]
        for q in quantities:
            tmp = self[q]

    def _check_segmaps(self):
        """
        Compare Gini segmap, MID segmap and shape asymmetry segmap,
        and set flag=1 if they are very different from each other.
        """
        area_max = max(np.sum(self._segmap_gini),
                       np.sum(self._segmap_mid),
                       np.sum(self._segmap_shape_asym))
        area_overlap = np.sum(self._segmap_gini &
                              self._segmap_mid &
                              self._segmap_shape_asym)
        if area_max == 0:
            print('Warning: segmaps are empty!')
            self.flag = 1
            return

        area_ratio = area_overlap / float(area_max)
        if area_ratio < self._segmap_overlap_ratio:
            self.flag = 1

    @lazyproperty
    def _slice_stamp(self):
        """
        Attempt to create a square slice (centered at the centroid)
        that is a bit larger than the minimal bounding box containing
        the main labeled segment. Note that the cutout may not be
        square when the source is close to a border of the original image.
        """
        # Maximum distance to any side of the bounding box
        yc, xc = np.int64(self._props.centroid.value)
        ymin, xmin, ymax, xmax = np.int64(self._props.bbox.value)
        dist = max(xmax-xc, xc-xmin, ymax-yc, yc-ymin)

        # Add some extra space in each dimension
        assert(self._cutout_extent >= 1.0)
        dist = int(dist * self._cutout_extent)

        # Make cutout
        ny, nx = self._props._data.shape
        slice_stamp = (slice(max(0, yc-dist), min(ny, yc+dist+1)),
                       slice(max(0, xc-dist), min(nx, xc+dist+1)))

        return slice_stamp

    @lazyproperty
    def _mask_stamp(self):
        """
        Create a total binary mask for the "postage stamp".
        Pixels belonging to other sources (as well as pixels masked
        using the ``mask`` keyword argument) are set to ``True``,
        but the background (segmap == 0) is left alone.
        """
        segmap_stamp = self._props._segment_img.data[self._slice_stamp]
        mask_stamp = (segmap_stamp != 0) & (segmap_stamp != self._props.label)
        if self._props._mask is not None:
            mask_stamp = mask_stamp | self._props._mask[self._slice_stamp]
        return mask_stamp

    @lazyproperty
    def _mask_stamp_no_bg(self):
        """
        Similar to ``_mask_stamp``, but also mask the background.
        """
        segmap_stamp = self._props._segment_img.data[self._slice_stamp]
        mask_stamp = segmap_stamp != self._props.label
        if self._props._mask is not None:
            mask_stamp = mask_stamp | self._props._mask[self._slice_stamp]
        return mask_stamp

    @lazyproperty
    def _cutout_stamp_maskzeroed(self):
        """
        Return a data cutout with its shape and position determined
        by ``_slice_stamp``. Pixels belonging to other sources
        (as well as pixels where ``mask`` == 1) are set to zero,
        but the background is left alone.
        """
        cutout_stamp = np.where(~self._mask_stamp,
                                self._props._data[self._slice_stamp], 0.0)
        return cutout_stamp

    @lazyproperty
    def _cutout_stamp_maskzeroed_no_bg(self):
        """
        Like ``_cutout_stamp_maskzeroed``, but also mask the
        background.
        """
        return np.where(~self._mask_stamp_no_bg,
                        self._cutout_stamp_maskzeroed, 0.0)

    @lazyproperty
    def _sorted_pixelvals_stamp_no_bg(self):
        """
        The sorted pixel values of the (zero-masked) postage stamp,
        excluding masked values and the background.
        """
        return np.sort(self._cutout_stamp_maskzeroed[~self._mask_stamp_no_bg])

    @lazyproperty
    def _cutout_stamp_maskzeroed_no_bg_nonnegative(self):
        """
        Same as ``_cutout_stamp_maskzeroed_no_bg``, but masking
        negative pixels.
        """
        image = self._cutout_stamp_maskzeroed_no_bg
        return np.where(image > 0, image, 0.0)

    @lazyproperty
    def _sorted_pixelvals_stamp_no_bg_nonnegative(self):
        """
        Same as ``_sorted_pixelvals_stamp_no_bg``, but masking
        negative pixels.
        """
        image = self._cutout_stamp_maskzeroed_no_bg_nonnegative
        return np.sort(image[~self._mask_stamp_no_bg])

    @lazyproperty
    def _sersic_model(self):
        """
        Fit a 2D Sersic profile using Astropy's model fitting library.
        """
        i, j = int(self._yc_stamp), int(self._xc_stamp)
        z = self._cutout_stamp_maskzeroed
        ny, nx = z.shape
        y, x = np.mgrid[0:ny, 0:nx]

        # Initial guess
        sersic_init = models.Sersic2D(
            amplitude=z[i, j],
            r_eff=self.half_light_radius, n=2.5,
            x_0=self._xc_stamp, y_0=self._yc_stamp,
            ellip=self._props.ellipticity.value,
            theta=self._props.orientation.value)
        
        # Try to fit model
        fit_sersic = fitting.LevMarLSQFitter()
        with warnings.catch_warnings(record=True) as w:
            start = time.time()
            sersic_model = fit_sersic(sersic_init, x, y, z)
            print('Fitting time: %g s' % (time.time() - start))
            if len(w) > 0:
                print('[sersic] Warning: The fit may be unsuccessful.')
                self.flag_sersic = 1
            return sersic_model

        assert(False)

    @lazyproperty
    def sersic_index(self):
        """
        The Sersic index.
        """
        return self._sersic_model.n.value

    @lazyproperty
    def _diagonal_distance(self):
        """
        Return the diagonal distance (in pixels) of the postage stamp.
        This is used as an upper bound in some calculations.
        """
        ny, nx = self._cutout_stamp_maskzeroed.shape
        return np.sqrt(nx**2 + ny**2)

    def _petrosian_function_circ(self, r, center):
        """
        Helper function to calculate the circular Petrosian radius.
        
        For a given radius ``r``, return the ratio of the mean flux
        over a circular annulus divided by the mean flux within the
        circle, minus "eta" (eq. 4 from Lotz et al. 2004). The root
        of this function is the Petrosian radius.
        """
        image = self._cutout_stamp_maskzeroed

        r_in = r - 0.5 * self._annulus_width
        r_out = r + 0.5 * self._annulus_width
        assert(r_in >= 0)

        circ_annulus = photutils.CircularAnnulus(center, r_in, r_out)
        circ_aperture = photutils.CircularAperture(center, r)

        # Force mean fluxes to be positive:
        circ_annulus_mean_flux = np.abs(_aperture_mean_nomask(
            circ_annulus, image, method='exact'))
        circ_aperture_mean_flux = np.abs(_aperture_mean_nomask(
            circ_aperture, image, method='exact'))
        
        if circ_aperture_mean_flux == 0:
            ratio = 1.0
            self.flag = 1
        else:
            ratio = circ_annulus_mean_flux / circ_aperture_mean_flux

        return ratio - self._eta

    def _rpetro_circ_generic(self, center):
        """
        Compute the Petrosian radius for concentric circular apertures.

        Notes
        -----
        The so-called "curve of growth" is not always monotonic,
        e.g., when there is a bright, unlabeled and unmasked
        secondary source in the image, so we cannot just apply a
        root-finding algorithm over the full interval.
        Instead, we proceed in two stages: first we do a coarse,
        brute-force search for an appropriate interval (that
        contains a root), and then we apply the root-finder.

        """
        # Find appropriate range for root finder
        npoints = 100
        r_inner = self._annulus_width
        r_outer = self._diagonal_distance
        assert(r_inner < r_outer)
        dr = (r_outer - r_inner) / float(npoints-1)
        r_min, r_max = None, None
        r = r_inner  # initial value
        while True:
            if r >= r_outer:
                print('[rpetro_circ] Warning: rpetro larger than cutout.')
                self.flag = 1
            curval = self._petrosian_function_circ(r, center)
            if curval == 0:
                print('[rpetro_circ] Warning: we found rpetro by pure chance!')
                return r
            elif curval > 0:
                r_min = r
            elif curval < 0:
                if r_min is None:
                    print('[rpetro_circ] Warning: r_min is not defined yet.')
                    self.flag = 1
                else:
                    r_max = r
                    break
            r += dr

        rpetro_circ = opt.brentq(self._petrosian_function_circ, 
                                 r_min, r_max, args=(center,), xtol=1e-6)

        return rpetro_circ

    @lazyproperty
    def _rpetro_circ_centroid(self):
        """
        Calculate the Petrosian radius with respect to the centroid.
        This is only used as a preliminary value for the asymmetry
        calculation.
        """
        center = np.array([self._xc_stamp, self._yc_stamp])
        return self._rpetro_circ_generic(center)

    @lazyproperty
    def rpetro_circ(self):
        """
        Calculate the Petrosian radius with respect to the point
        that minimizes the asymmetry.
        """
        return self._rpetro_circ_generic(self._asymmetry_center)

    def _petrosian_function_ellip(self, a, center, elongation, theta):
        """
        Helper function to calculate the Petrosian "radius".
        
        For the ellipse with semi-major axis ``a``, return the
        ratio of the mean flux over an elliptical annulus
        divided by the mean flux within the ellipse,
        minus "eta" (eq. 4 from Lotz et al. 2004). The root of
        this function is the Petrosian "radius".
        
        """
        image = self._cutout_stamp_maskzeroed

        b = a / elongation
        a_in = a - 0.5 * self._annulus_width
        a_out = a + 0.5 * self._annulus_width
        assert(a_in >= 0)

        b_out = a_out / elongation

        ellip_annulus = photutils.EllipticalAnnulus(
            center, a_in, a_out, b_out, theta)
        ellip_aperture = photutils.EllipticalAperture(
            center, a, b, theta)

        # Force mean fluxes to be positive:
        ellip_annulus_mean_flux = np.abs(_aperture_mean_nomask(
            ellip_annulus, image, method='exact'))
        ellip_aperture_mean_flux = np.abs(_aperture_mean_nomask(
            ellip_aperture, image, method='exact'))

        if ellip_aperture_mean_flux == 0:
            ratio = 1.0
            self.flag = 1
        else:
            ratio = ellip_annulus_mean_flux / ellip_aperture_mean_flux

        return ratio - self._eta

    def _rpetro_ellip_generic(self, center, elongation, theta):
        """
        Compute the Petrosian "radius" (actually the semi-major axis)
        for concentric elliptical apertures.
        
        Notes
        -----
        The so-called "curve of growth" is not always monotonic,
        e.g., when there is a bright, unlabeled and unmasked
        secondary source in the image, so we cannot just apply a
        root-finding algorithm over the full interval.
        Instead, we proceed in two stages: first we do a coarse,
        brute-force search for an appropriate interval (that
        contains a root), and then we apply the root-finder.

        """
        # Find appropriate range for root finder
        npoints = 100
        a_inner = self._annulus_width
        a_outer = self._diagonal_distance
        assert(a_inner < a_outer)
        da = (a_outer - a_inner) / float(npoints-1)
        a_min, a_max = None, None
        a = a_inner  # initial value
        while True:
            if a >= a_outer:
                print('[rpetro_ellip] Warning: rpetro larger than cutout.')
                self.flag = 1
            curval = self._petrosian_function_ellip(a, center, elongation, theta)
            if curval == 0:
                print('[rpetro_ellip] Warning: we found rpetro by pure chance!')
                return a
            elif curval > 0:
                a_min = a
            elif curval < 0:
                if a_min is None:
                    print('[rpetro_ellip] Warning: a_min is not defined yet.')
                    self.flag = 1
                else:
                    a_max = a
                    break
            a += da

        rpetro_ellip = opt.brentq(self._petrosian_function_ellip, a_min, a_max,
                                  args=(center, elongation, theta,), xtol=1e-6)

        return rpetro_ellip

    @lazyproperty
    def rpetro_ellip(self):
        """
        Return the elliptical Petrosian "radius", calculated with
        respect to the point that minimizes the asymmetry.
        """
        return self._rpetro_ellip_generic(
            self._asymmetry_center, self._asymmetry_elongation,
            self._asymmetry_orientation)


    #######################
    # Gini-M20 statistics #
    #######################

    @lazyproperty
    def _segmap_gini(self):
        """
        Create a new segmentation map (relative to the "Gini" cutout)
        based on the Petrosian "radius".
        """
        # Smooth image
        petro_sigma = self._petro_fraction_gini * self.rpetro_ellip
        cutout_smooth = ndi.gaussian_filter(self._cutout_stamp_maskzeroed, petro_sigma)

        # Use mean flux at the Petrosian "radius" as threshold
        a_in = self.rpetro_ellip - 0.5 * self._annulus_width
        a_out = self.rpetro_ellip + 0.5 * self._annulus_width
        b_out = a_out / self._props.elongation.value
        theta = self._props.orientation.value
        ellip_annulus = photutils.EllipticalAnnulus(
            (self._xc_stamp, self._yc_stamp), a_in, a_out, b_out, theta)
        ellip_annulus_mean_flux = _aperture_mean_nomask(
            ellip_annulus, cutout_smooth, method='exact')

        above_threshold = cutout_smooth >= ellip_annulus_mean_flux

        # Grow regions with 8-connected neighbor "footprint"
        s = ndi.generate_binary_structure(2, 2)
        labeled_array, num_features = ndi.label(above_threshold, structure=s)
        
        # In some rare cases (e.g., the disastrous J020218.5+672123_g.fits.gz),
        # this results in an empty segmap, so there is nothing to do.
        if num_features == 0:
            print('[segmap_gini] Warning: empty Gini segmap!')
            self.flag = 1
            assert(np.sum(above_threshold) == 0)  # sanity check
            return above_threshold

        # If more than one region, activate the "bad measurement" flag
        # and only keep segment that contains the brightest pixel.
        if num_features > 1:
            self.flag = 1
            ic, jc = np.argwhere(cutout_smooth == np.max(cutout_smooth))[0]
            assert(labeled_array[ic, jc] != 0)
            segmap = labeled_array == labeled_array[ic, jc]
        else:
            segmap = above_threshold

        return segmap

    @lazyproperty
    def gini(self):
        """
        Calculate the Gini coefficient as described in Lotz et al. (2004).
        """
        image = self._cutout_stamp_maskzeroed.flatten()
        segmap = self._segmap_gini.flatten()

        sorted_pixelvals = np.sort(np.abs(image[segmap]))
        n = len(sorted_pixelvals)
        if n <= 1 or np.sum(sorted_pixelvals) == 0:
            self.flag = 1
            return -99.0  # invalid
        
        indices = np.arange(1, n+1)  # start at i=1
        gini = (np.sum((2*indices-n-1) * sorted_pixelvals) /
                (float(n-1) * np.sum(sorted_pixelvals)))

        return gini

    @lazyproperty
    def m20(self):
        """
        Calculate the M_20 coefficient as described in Lotz et al. (2004).
        """
        if np.sum(self._segmap_gini) == 0:
            return -99.0  # invalid

        # Use the same region as in the Gini calculation
        image = np.where(self._segmap_gini, self._cutout_stamp_maskzeroed, 0.0)
        image = np.float64(image)  # skimage wants double

        # Calculate centroid
        m = skimage.measure.moments(image, order=1)
        yc = m[0, 1] / m[0, 0]
        xc = m[1, 0] / m[0, 0]

        # Calculate second total central moment
        mc = skimage.measure.moments_central(image, yc, xc, order=3)
        second_moment = mc[0, 2] + mc[2, 0]

        # Calculate threshold pixel value
        sorted_pixelvals = np.sort(image.flatten())
        flux_fraction = np.cumsum(sorted_pixelvals) / np.sum(sorted_pixelvals)
        sorted_pixelvals_20 = sorted_pixelvals[flux_fraction >= 0.8]
        if len(sorted_pixelvals_20) == 0:
            # This can happen when there are very few pixels.
            self.flag = 1
            return -99.0  # invalid
        threshold = sorted_pixelvals_20[0]

        # Calculate second moment of the brightest pixels
        image_20 = np.where(image >= threshold, image, 0.0)
        mc_20 = skimage.measure.moments_central(image_20, yc, xc, order=3)
        second_moment_20 = mc_20[0, 2] + mc_20[2, 0]

        if second_moment == 0:
            m20 = -99.0  # invalid
        else:
            m20 = np.log10(second_moment_20 / second_moment)

        return m20

    @lazyproperty
    def sn_per_pixel(self):
        """
        Calculate the signal-to-noise per pixel using the Petrosian segmap.
        """
        locs = self._segmap_gini & (self._cutout_stamp_maskzeroed >= 0)
        pixelvals = self._cutout_stamp_maskzeroed[locs]
        if self._variance is None:
            variance = np.zeros_like(pixelvals)
        else:
            variance = self._variance[self._slice_stamp]
            if np.any(variance < 0):
                print('[sn_per_pixel] Warning: Some negative variance values.')
                variance = np.abs(variance)

        snp = np.mean(
            pixelvals / np.sqrt(variance[locs] + self._sky_sigma**2))
        if not np.isfinite(snp):
            self.flag = 1
            snp = -99.0  # invalid

        return snp

    ##################
    # CAS statistics #
    ##################

    @lazyproperty
    def _slice_skybox(self):
        """
        Try to find a region of the sky that only contains background.
        
        In principle, a more accurate approach could be adopted
        (e.g. Shi et al. 2009, ApJ, 697, 1764).
        """
        segmap = self._props._segment_img.data[self._slice_stamp]
        ny, nx = segmap.shape
        mask = np.zeros(segmap.shape, dtype=np.bool8)
        if self._props._mask is not None:
            mask = self._props._mask[self._slice_stamp]

        # If segmap is smaller than borders, there is nothing to do.
        # This will result in NaN values for skybox calculations.
        if (nx < 2 * self._border_size) or (ny < 2 * self._border_size):
            self.flag = 1
            print('[skybox] Warning: original segmap is too small.')
            return (slice(0, 0), slice(0, 0))

        while True:
            for i in range(self._border_size,
                           ny - self._border_size - self._skybox_size):
                for j in range(self._border_size,
                               nx - self._border_size - self._skybox_size):
                    boxslice = (slice(i, i + self._skybox_size),
                                slice(j, j + self._skybox_size))
                    if np.all(segmap[boxslice] == 0) and np.all(~mask[boxslice]):
                        return boxslice

            # If we got here, a skybox of the given size was not found.
            if self._skybox_size < self._border_size:
                self.flag = 1
                print('[skybox] Warning: forcing skybox to lower-left corner.')
                boxslice = (
                    slice(self._border_size, self._border_size + self._skybox_size),
                    slice(self._border_size, self._border_size + self._skybox_size))
                return boxslice
            else:
                self._skybox_size //= 2
                print('[skybox] Warning: Reducing skybox size to %d.' % (
                    self._skybox_size))

        # Should not reach this point.
        assert(False)

    @lazyproperty
    def _sky_mean(self):
        """
        Mean background value. Can be NaN when there is no skybox.
        """
        bkg = self._cutout_stamp_maskzeroed[self._slice_skybox]
        if bkg.size == 0:
            return np.nan

        return np.mean(bkg)

    @lazyproperty
    def _sky_median(self):
        """
        Median background value. Can be NaN when there is no skybox.
        """
        bkg = self._cutout_stamp_maskzeroed[self._slice_skybox]
        if bkg.size == 0:
            return np.nan

        return np.median(bkg)

    @lazyproperty
    def _sky_sigma(self):
        """
        Standard deviation of the background. Can be NaN when there
        is no skybox.
        """
        bkg = self._cutout_stamp_maskzeroed[self._slice_skybox]
        if bkg.size == 0:
            return np.nan

        return np.std(bkg)

    @lazyproperty
    def _sky_asymmetry(self):
        """
        Asymmetry of the background. Can be NaN when there is no
        skybox. Note the peculiar normalization.
        """
        bkg = self._cutout_stamp_maskzeroed[self._slice_skybox]
        bkg_180 = bkg[::-1, ::-1]
        if bkg.size == 0:
            return np.nan

        return np.sum(np.abs(bkg_180 - bkg)) / float(bkg.size)

    @lazyproperty
    def _sky_smoothness(self):
        """
        Smoothness of the background. Can be NaN when there is
        no skybox. Note the peculiar normalization.
        """
        bkg = self._cutout_stamp_maskzeroed[self._slice_skybox]
        if bkg.size == 0:
            return np.nan

        # If the smoothing "boxcar" is larger than the skybox itself,
        # this just sets all values equal to the mean:
        boxcar_size = int(self._petro_fraction_cas * self.rpetro_circ)
        bkg_smooth = ndi.uniform_filter(bkg, size=boxcar_size)

        return np.sum(np.abs(bkg_smooth - bkg)) / float(bkg.size)

    def _asymmetry_function(self, center, image, kind):
        """
        Helper function to determine the asymmetry and center of asymmetry.
        The idea is to minimize the output of this function.
        
        Parameters
        ----------
        center : tuple or array-like
            The (x,y) position of the center.
        image : array-like
            The 2D image.
        kind : {'cas', 'outer', 'shape'}
            Whether to calculate the traditional CAS asymmetry (default),
            outer asymmetry or shape asymmetry.
        
        Returns
        -------
        asym : The asymmetry statistic for the given center.

        """
        image = np.float64(image)  # skimage wants double
        ny, nx = image.shape
        xc, yc = center

        if xc < 0 or xc >= nx or yc < 0 or yc >= ny:
            print('[asym_center] Warning: minimizer tried to exit bounds.')
            self.flag = 1
            # Return high value to keep minimizer within range:
            return 100.0

        # Rotate around given center
        image_180 = skimage.transform.rotate(image, 180.0, center=center)

        # Apply symmetric mask
        mask = self._mask_stamp.copy()
        mask_180 = skimage.transform.rotate(mask, 180.0, center=center)
        mask_180 = mask_180 >= 0.5  # convert back to bool
        mask_symmetric = mask | mask_180
        image = np.where(~mask_symmetric, image, 0.0)
        image_180 = np.where(~mask_symmetric, image_180, 0.0)

        # Create aperture for the chosen kind of asymmetry
        if kind == 'cas':
            r = self._petro_extent_circ * self._rpetro_circ_centroid
            ap = photutils.CircularAperture(center, r)
        elif kind == 'outer':
            r_in = self.half_light_radius
            r_out = self.rmax
            if np.isnan(r_in) or np.isnan(r_out) or (r_in <= 0) or (r_out <= 0):
                self.flag = 1
                return -99.0  # invalid
            ap = photutils.CircularAnnulus(center, r_in, r_out)
        elif kind == 'shape':
            if np.isnan(self.rmax) or (self.rmax <= 0):
                self.flag = 1
                return -99.0  # invalid
            ap = photutils.CircularAperture(center, self.rmax)
        else:
            raise Exception('Asymmetry kind not understood:', kind)

        # Apply eq. 10 from Lotz et al. (2004)
        ap_abs_sum = ap.do_photometry(np.abs(image), method='exact')[0][0]
        ap_abs_diff = ap.do_photometry(np.abs(image_180-image), method='exact')[0][0]

        if ap_abs_sum == 0.0:
            self.flag = 1
            return -99.0  # invalid

        if kind == 'shape':
            # The shape asymmetry of the background is zero
            asym = ap_abs_diff / ap_abs_sum
        else:
            if np.isfinite(self._sky_asymmetry):
                ap_area = _aperture_area(ap, mask_symmetric)
                asym = (ap_abs_diff - ap_area*self._sky_asymmetry) / ap_abs_sum
            else:
                asym = ap_abs_diff / ap_abs_sum
                
        return asym

    @lazyproperty
    def _asymmetry_center(self):
        """
        Find the position of the central pixel (relative to the
        "postage stamp" cutout) that minimizes the (CAS) asymmetry.
        """
        center_0 = np.array([self._xc_stamp, self._yc_stamp])  # initial guess
        center_asym = opt.fmin(self._asymmetry_function, center_0,
                               args=(self._cutout_stamp_maskzeroed, 'cas'),
                               xtol=1e-6, disp=0)

        # Print warning if center is masked
        ic, jc = int(center_asym[1]), int(center_asym[0])
        if self._cutout_stamp_maskzeroed[ic, jc] == 0:
            print('[asym_center] Warning: Asymmetry center is masked.')
            self.flag = 1

        return center_asym

    @lazyproperty
    def _asymmetry_covariance(self):
        """
        The covariance matrix of a Gaussian function that has the same
        second-order moments as the source, using ``_asymmetry_center``
        as the center.
        """
        # skimage wants double precision:
        image = np.float64(self._cutout_stamp_maskzeroed_no_bg)
        # Ignore negative values (otherwise eigvals can be negative):
        image = np.where(image > 0, image, 0.0)

        # Calculate moments w.r.t. asymmetry center
        xc, yc = self._asymmetry_center
        mc = skimage.measure.moments_central(image, yc, xc, order=3)
        assert(mc[0, 0] > 0)

        covariance = np.array([
            [mc[2, 0], mc[1, 1]],
            [mc[1, 1], mc[0, 2]]])
        covariance /= mc[0, 0]  # normalize
        
        # This checks the covariance matrix and modifies it in the
        # case of "infinitely thin" sources by iteratively increasing
        # the diagonal elements:
        covariance = self._props._check_covariance(covariance)
        
        return covariance

    @lazyproperty
    def _asymmetry_eigvals(self):
        """
        The ordered (largest first) eigenvalues of the covariance
        matrix, which correspond to the *squared* semimajor and
        semiminor axes.
        """
        eigvals = np.linalg.eigvals(self._asymmetry_covariance)
        eigvals = np.sort(eigvals)[::-1]  # largest first
        assert(np.all(eigvals > 0))
        
        return eigvals

    @lazyproperty
    def _asymmetry_ellipticity(self):
        """
        The elongation of (the Gaussian function that has the same
        second-order moments as) the source, using ``_asymmetry_center``
        as the center.
        """
        a = np.sqrt(self._asymmetry_eigvals[0])
        b = np.sqrt(self._asymmetry_eigvals[1])

        return 1.0 - (b / a)

    @lazyproperty
    def _asymmetry_elongation(self):
        """
        The elongation of (the Gaussian function that has the same
        second-order moments as) the source, using ``_asymmetry_center``
        as the center.
        """
        a = np.sqrt(self._asymmetry_eigvals[0])
        b = np.sqrt(self._asymmetry_eigvals[1])

        return a / b

    @lazyproperty
    def _asymmetry_orientation(self):
        """
        The orientation (in radians) of the source, using
        ``_asymmetry_center`` as the center.
        """
        A, B, B, C = self._asymmetry_covariance.flat
        
        # This comes from the equation of a rotated ellipse:
        theta = 0.5 * np.arctan2(2.0 * B, A - C)
        
        return theta

    @lazyproperty
    def asymmetry(self):
        """
        Calculate asymmetry as described in Lotz et al. (2004).
        """
        image = self._cutout_stamp_maskzeroed
        asym = self._asymmetry_function(self._asymmetry_center,
                                        image, 'cas')
        
        return asym

    def _radius_at_fraction_of_total_cas(self, fraction):
        """
        Specialization of ``_radius_at_fraction_of_total`` for
        the CAS calculations.
        """
        image = self._cutout_stamp_maskzeroed
        center = self._asymmetry_center
        r_upper = self._petro_extent_circ * self.rpetro_circ
        
        r, flag = _radius_at_fraction_of_total(image, center, r_upper, fraction)
        self.flag = max(self.flag, flag)
        
        if np.isnan(r) or (r <= 0.0):
            self.flag = 1
            r = -99.0  # invalid
        
        return r

    @lazyproperty
    def r_20(self):
        """
        The radius that contains 20% of the light.
        """
        return self._radius_at_fraction_of_total_cas(0.2)

    @lazyproperty
    def r_80(self):
        """
        The radius that contains 80% of the light.
        """
        return self._radius_at_fraction_of_total_cas(0.8)

    @lazyproperty
    def concentration(self):
        """
        Calculate concentration as described in Lotz et al. (2004).
        """
        if (self.r_20 == -99.0) or (self.r_80 == -99.0):
            C = -99.0  # invalid
        else:
            C = 5.0 * np.log10(self.r_80 / self.r_20)
        
        return C

    @lazyproperty
    def smoothness(self):
        """
        Calculate smoothness (a.k.a. clumpiness) as described in
        Lotz et al. (2004).
        """
        r = self._petro_extent_circ * self.rpetro_circ
        ap = photutils.CircularAperture(self._asymmetry_center, r)

        image = self._cutout_stamp_maskzeroed
        
        boxcar_size = int(self._petro_fraction_cas * self.rpetro_circ)
        image_smooth = ndi.uniform_filter(image, size=boxcar_size)
        
        ap_abs_flux = ap.do_photometry(np.abs(image), method='exact')[0][0]
        ap_abs_diff = ap.do_photometry(
            np.abs(image_smooth - image), method='exact')[0][0]
        if np.isfinite(self._sky_smoothness):
            S = (ap_abs_diff - ap.area()*self._sky_smoothness) / ap_abs_flux
        else:
            S = -99.0  # invalid

        if not np.isfinite(S):
            self.flag = 1
            return -99.0  # invalid

        return S

    ##################
    # MID statistics #
    ##################

    def _segmap_mid_main_clump(self, q):
        """
        For a given quantile ``q``, return a boolean array indicating
        the locations of pixels above ``q`` (within the original segment)
        that are also part of the "main" clump.
        """
        threshold = _quantile(self._sorted_pixelvals_stamp_no_bg_nonnegative, q)
        above_threshold = self._cutout_stamp_maskzeroed_no_bg_nonnegative >= threshold

        # Instead of assuming that the main segment is at the center
        # of the stamp, use the position of the brightest pixel:
        ic = int(self._y_maxval_stamp)
        jc = int(self._x_maxval_stamp)

        # Grow regions using 8-connected neighbor "footprint"
        s = ndi.generate_binary_structure(2, 2)
        labeled_array, num_features = ndi.label(above_threshold, structure=s)

        # Sanity check (brightest pixel should be part of the main clump):
        assert(labeled_array[ic, jc] != 0)

        return labeled_array == labeled_array[ic, jc]

    def _segmap_mid_function(self, q):
        """
        Helper function to calculate the MID segmap.
        
        For a given quantile ``q``, return the ratio of the mean flux of
        pixels at the level of ``q`` (within the main clump) divided by
        the mean of pixels above ``q`` (within the main clump).
        """
        locs_main_clump = self._segmap_mid_main_clump(q)
        
        mean_flux_main_clump = np.mean(
            self._cutout_stamp_maskzeroed_no_bg_nonnegative[locs_main_clump])
        mean_flux_new_pixels = _quantile(
            self._sorted_pixelvals_stamp_no_bg_nonnegative, q)

        if mean_flux_main_clump == 0:
            ratio = 1.0
            self.flag = 1
        else:
            ratio = mean_flux_new_pixels / mean_flux_main_clump

        return ratio - self._eta

    @lazyproperty
    def _segmap_mid(self):
        """
        Create a new segmentation map as described in Section 4.3 from
        Freeman et al. (2013).
        
        Notes
        -----
        This implementation improves upon previous ones by making
        the MID segmap independent from the number of quantiles
        used in the calculation, as well as other parameters.
        """
        num_pixelvals = len(self._sorted_pixelvals_stamp_no_bg_nonnegative)
        
        # In some rare cases (as a consequence of an erroneous
        # initial segmap, as in J095553.0+694048_g.fits.gz),
        # the MID segmap is technically undefined because the
        # mean flux of "newly added" pixels never reaches the
        # target value, at least within the original segmap.
        # In these cases we simply assume that the MID segmap
        # is the original segmap (and turn the "bad measurement"
        # flag on).
        if self._segmap_mid_function(0.0) > 0.0:
            self.flag = 1
            print('[segmap_mid] Warning: using original segmap.')
            return ~self._mask_stamp_no_bg
        
        # Find appropriate quantile using numerical solver
        q_min = 0.0
        q_max = 1.0
        xtol = 1.0 / float(num_pixelvals)
        q = opt.brentq(self._segmap_mid_function, q_min, q_max, xtol=xtol)

        locs_main_clump = self._segmap_mid_main_clump(q)

        # Regularize a bit the shape of the segmap:
        segmap_float = ndi.uniform_filter(
            np.float64(locs_main_clump), size=self._boxcar_size_mid)
        segmap = segmap_float > 0.5

        # Make sure that brightest pixel is in segmap
        ic = int(self._y_maxval_stamp)
        jc = int(self._x_maxval_stamp)
        if ~segmap[ic, jc]:
            print('[segmap_mid] Warning: adding brightest pixel to segmap.')
            segmap[ic, jc] = True
            self.flag = 1

        # Grow regions with 8-connected neighbor "footprint"
        s = ndi.generate_binary_structure(2, 2)
        labeled_array, num_features = ndi.label(segmap, structure=s)

        return labeled_array == labeled_array[ic, jc]

    @lazyproperty
    def _cutout_mid(self):
        """
        Apply the MID segmap to the postage stamp cutout
        and set negative pixels to zero.
        """
        image = np.where(self._segmap_mid,
                         self._cutout_stamp_maskzeroed_no_bg_nonnegative, 0.0)
        return image

    @lazyproperty
    def _sorted_pixelvals_mid(self):
        """
        Just the sorted pixel values of the MID cutout.
        """
        image = self._cutout_mid
        return np.sort(image[~self._mask_stamp_no_bg])

    def _multimode_function(self, q):
        """
        Helper function to calculate the multimode statistic.
        Returns the sorted "areas" of the clumps at quantile ``q``.
        """
        threshold = _quantile(self._sorted_pixelvals_mid, q)
        above_threshold = self._cutout_mid >= threshold

        # Neighbor "footprint" for growing regions, including corners:
        s = ndi.generate_binary_structure(2, 2)

        labeled_array, num_features = ndi.label(above_threshold, structure=s)

        # Zero is reserved for non-labeled pixels:
        labeled_array_nonzero = labeled_array[labeled_array != 0]
        labels, counts = np.unique(labeled_array_nonzero, return_counts=True)
        sorted_counts = np.sort(counts)[::-1]

        return sorted_counts

    def _multimode_ratio(self, q):
        """
        For a given quantile ``q``, return the "ratio" (A2/A1)*A2
        multiplied by -1, which is used for minimization.
        """
        invalid = np.sum(self._cutout_mid)  # high "energy" for basin-hopping
        if (q < 0) or (q > 1):
            ratio = invalid
        else:
            sorted_counts = self._multimode_function(q)
            if len(sorted_counts) == 1:
                ratio = invalid
            else:
                ratio = -1.0 * float(sorted_counts[1])**2 / float(sorted_counts[0])

        return ratio

    @lazyproperty
    def multimode(self):
        """
        Calculate the multimode (M) statistic as described in
        Freeman et al. (2013) and Peth et al. (2016).
        
        Notes
        -----
        In the original publication, Freeman et al. (2013)
        recommends using the more robust quantity (A2/A1)*A2,
        while Peth et al. (2016) recommends using the
        size-independent quantity A2/A1. Here we take a mixed
        approach (which, incidentally, is also what Mike Peth's
        IDL implementation actually does): we maximize the
        quantity (A2/A1)*A2 (as a function of the brightness
        threshold) but ultimately define the M statistic
        as the corresponding A2/A1 value.
        
        The original IDL implementation only explores quantiles
        in the range [0.5, 1.0], at least with the default settings.
        While this might be useful in practice, in theory the
        maximum (A2/A1)*A2 value could also happen in the quantile
        range [0.0, 0.5], so here we take a safer, more general
        approach and search over [0.0, 1.0].
        
        In practice, the quantity (A2/A1)*A2 is tricky to optimize.
        We improve over previous implementations by doing so
        in two stages: starting with a brute-force search
        over a relatively coarse array of quantiles, as in the
        original implementation, followed by a finer search using
        the basin-hopping method. This should do a better job of
        finding the global maximum.
        
        """
        q_min = 0.0
        q_max = 1.0

        # STAGE 1: brute-force

        # We start with a relatively coarse separation between the
        # quantiles, equal to the value used in the original IDL
        # implementation. If every calculated ratio is invalid, we
        # try a smaller size.
        mid_stepsize = 0.02

        while True:
            quantile_array = np.arange(q_min, q_max, mid_stepsize)
            ratio_array = np.zeros_like(quantile_array)
            for k, q in enumerate(quantile_array):
                ratio_array[k] = self._multimode_ratio(q)
            k_min = np.argmin(ratio_array)
            q0 = quantile_array[k_min]
            ratio_min = ratio_array[k_min]
            if ratio_min < 0:  # valid "ratios" should be negative
                break
            elif mid_stepsize < 1e-3:
                print('[M statistic] Warning: Single clump!')
                # This usually happens when the source is a star,
                # so we turn on the "bad measurement" flag.
                self.flag = 1
                return 0.0
            else:
                mid_stepsize = mid_stepsize / 2.0
                print('[M statistic] Warning: Reduced stepsize to %g.' % (mid_stepsize))

        # STAGE 2: basin-hopping method

        # The results seem quite robust to changes in this parameter,
        # so I leave it hardcoded for now:
        mid_bh_rel_temp = 0.5

        temperature = -1.0 * mid_bh_rel_temp * ratio_min
        res = opt.basinhopping(self._multimode_ratio, q0,
            minimizer_kwargs={"method": "Nelder-Mead"},
            niter=self._niter_bh_mid, T=temperature, stepsize=mid_stepsize,
            interval=self._niter_bh_mid/2, disp=False)
        q_final = res.x[0]

        # Finally, return A2/A1 instead of (A2/A1)*A2
        sorted_counts = self._multimode_function(q_final)

        return float(sorted_counts[1]) / float(sorted_counts[0])

    @lazyproperty
    def _cutout_mid_smooth(self):
        """
        Just a Gaussian-smoothed version of the zero-masked image used
        in the MID calculations.
        """
        image_smooth = ndi.gaussian_filter(self._cutout_mid, self._sigma_mid)
        return image_smooth

    @lazyproperty
    def _watershed_mid(self):
        """
        This replaces the "i_clump" routine from the original IDL code.
        The main difference is that we do not place a limit on the
        number of labeled regions (previously limited to 100 regions).
        This is also much faster, thanks to the highly optimized
        "peak_local_max" and "watershed" skimage routines.
        Returns a labeled array indicating regions around local maxima.
        """
        peaks = skimage.feature.peak_local_max(
            self._cutout_mid_smooth, indices=True, num_peaks=np.inf)
        num_peaks = peaks.shape[0]
        # The zero label is reserved for the background:
        peak_labels = np.arange(1, num_peaks+1, dtype=np.int64)
        ypeak, xpeak = peaks.T

        markers = np.zeros(self._cutout_mid_smooth.shape, dtype=np.int64)
        markers[ypeak, xpeak] = peak_labels

        mask = self._cutout_mid_smooth > 0
        labeled_array = skimage.morphology.watershed(
            -self._cutout_mid_smooth, markers, connectivity=2, mask=mask)

        return labeled_array, peak_labels, xpeak, ypeak

    @lazyproperty
    def _intensity_sums(self):
        """
        Helper function to calculate the intensity (I) and
        deviation (D) statistics.
        """
        labeled_array, peak_labels, xpeak, ypeak = self._watershed_mid
        num_peaks = len(peak_labels)
        
        flux_sums = np.zeros(num_peaks, dtype=np.float64)
        for k, label in enumerate(peak_labels):
            locs = labeled_array == label
            flux_sums[k] = np.sum(self._cutout_mid_smooth[locs])
        sid = np.argsort(flux_sums)[::-1]
        sorted_flux_sums = flux_sums[sid]
        sorted_xpeak = xpeak[sid]
        sorted_ypeak = ypeak[sid]
        
        return sorted_flux_sums, sorted_xpeak, sorted_ypeak

    @lazyproperty
    def intensity(self):
        """
        Calculate the intensity (I) statistic as described in
        Peth et al. (2016).
        """
        sorted_flux_sums, sorted_xpeak, sorted_ypeak = self._intensity_sums
        if len(sorted_flux_sums) <= 1:
            # Unlike the M=0 cases, there seem to be some legitimate
            # I=0 cases, so we do not turn on the "bad measurement" flag.
            return 0.0
        else:
            return sorted_flux_sums[1] / sorted_flux_sums[0]

    @lazyproperty
    def deviation(self):
        """
        Calculate the deviation (D) statistic as described in
        Peth et al. (2016).
        """
        image = np.float64(self._cutout_mid)  # skimage wants double
        
        sorted_flux_sums, sorted_xpeak, sorted_ypeak = self._intensity_sums
        if len(sorted_flux_sums) == 0:
            print('[deviation] Warning: There are no peaks')
            self.flag = 1
            return -99.0  # invalid

        xp = sorted_xpeak[0] + 0.5  # center of pixel
        yp = sorted_ypeak[0] + 0.5
        
        # Calculate centroid
        m = skimage.measure.moments(image, order=1)
        yc = m[0, 1] / m[0, 0]
        xc = m[1, 0] / m[0, 0]

        area = np.sum(self._segmap_mid)
        D = np.sqrt(np.pi/area) * np.sqrt((xp-xc)**2 + (yp-yc)**2)

        if not np.isfinite(D):
            self.flag = 1
            return -99.0  # invalid

        return D

    ###################
    # SHAPE ASYMMETRY #
    ###################

    @lazyproperty
    def half_light_radius(self):
        """
        The radius of a circular aperture containing 50% of the light,
        assuming that the center is the point that minimizes the
        asymmetry and that the total is at ``rmax``.
        """
        image = self._cutout_stamp_maskzeroed
        center = self._asymmetry_center

        if self.rmax == 0:
            r = 0.0
        else:
            r, flag = _radius_at_fraction_of_total(image, center, self.rmax, 0.5)
            self.flag = max(self.flag, flag)
        
        # In theory, this return value can also be NaN
        return r

    @lazyproperty
    def _segmap_shape_asym(self):
        """
        Construct a binary detection mask as described in Section 3.1
        from Pawlik et al. (2016).
        
        Notes
        -----
        The original algorithm assumes that a circular aperture with
        inner and outer radii equal to 20 and 40 times the FWHM is
        representative of the background. In order to avoid amplifying
        uncertainties from the central regions of the galaxy profile,
        we assume that such an aperture has inner and outer radii
        equal to 2 and 4 times the elliptical Petrosian "radius"
        (this can be changed by the user). Also, we define the center
        as the pixel that minimizes the asymmetry, instead of the
        brightest pixel.

        """
        ny, nx = self._cutout_stamp_maskzeroed.shape

        # Center at (center of) pixel that minimizes asymmetry
        center = np.floor(self._asymmetry_center) + 0.5

        # Create a circular annulus around the center
        # that only contains background sky (hopefully).
        r_in = self._petro_extent_ellip * self.rpetro_ellip
        r_out = 2.0 * self._petro_extent_ellip * self.rpetro_ellip
        circ_annulus = photutils.CircularAnnulus(center, r_in, r_out)

        # Convert circular annulus aperture to binary mask
        circ_annulus_mask = circ_annulus.to_mask(method='center')[0]
        # With the same shape as the postage stamp
        circ_annulus_mask = circ_annulus_mask.to_image((ny, nx))
        # Invert mask and exclude other sources
        total_mask = self._mask_stamp | np.logical_not(circ_annulus_mask)

        # If sky area is too small (e.g., if annulus is outside the
        # image), use skybox instead.
        if np.sum(~total_mask) < self._skybox_size**2:
            print('[shape_asym] Warning: using skybox for background.')
            total_mask = np.ones((ny, nx), dtype=np.bool8)
            total_mask[self._slice_skybox] = False
            # However, if skybox is undefined, there is nothing to do.
            if np.sum(~total_mask) == 0:
                print('[shape_asym] Warning: asymmetry segmap undefined.')
                self.flag = 1
                return ~self._mask_stamp_no_bg

        # Do sigma-clipping until convergence
        mean, median, std = sigma_clipped_stats(
            self._cutout_stamp_maskzeroed, mask=total_mask, sigma=3.0,
            iters=None, cenfunc=_mode)

        # Mode as defined in Bertin & Arnouts (1996)
        mode = 2.5*median - 1.5*mean
        threshold = mode + std

        # Smooth image slightly and apply 1-sigma threshold
        image_smooth = ndi.uniform_filter(
            self._cutout_stamp_maskzeroed, size=self._boxcar_size_shape_asym)
        above_threshold = image_smooth >= threshold

        # Make sure that brightest pixel (of smoothed image) is in segmap
        ic, jc = np.argwhere(image_smooth == np.max(image_smooth))[0]
        if ~above_threshold[ic, jc]:
            print('[shape_asym] Warning: adding brightest pixel to segmap.')
            above_threshold[ic, jc] = True
            self.flag = 1

        # Grow regions with 8-connected neighbor "footprint"
        s = ndi.generate_binary_structure(2, 2)
        labeled_array, num_features = ndi.label(above_threshold, structure=s)

        return labeled_array == labeled_array[ic, jc]

    @lazyproperty
    def rmax(self):
        """
        Return the distance (in pixels) from the pixel that minimizes
        the asymmetry to the edge of the main source segment, similar 
        to Pawlik et al. (2016).
        """
        image = self._cutout_stamp_maskzeroed
        ny, nx = image.shape

        # Center at (center of) pixel that minimizes asymmetry
        xc, yc = np.floor(self._asymmetry_center) + 0.5

        # Distances from all pixels to the center
        ypos, xpos = np.mgrid[0:ny, 0:nx] + 0.5  # center of pixel
        distances = np.sqrt((ypos-yc)**2 + (xpos-xc)**2)
        
        # Only consider pixels within the segmap.
        rmax = np.max(distances[self._segmap_shape_asym])
        
        if rmax < 1:
            assert(rmax == 0)  # this should be the only possibility
            print('[rmax] Warning: rmax = %g' % (rmax))
            self.flag = 1
        
        return rmax

    @lazyproperty
    def _outer_asymmetry_center(self):
        """
        Find the position of the central pixel (relative to the
        "postage stamp" cutout) that minimizes the outer asymmetry.
        """
        image = self._cutout_stamp_maskzeroed
        
        # Initial guess
        center_0 = self._asymmetry_center
        
        center_asym = opt.fmin(self._asymmetry_function, center_0,
                               args=(image, 'outer'), xtol=1e-6, disp=0)

        return center_asym

    @lazyproperty
    def outer_asymmetry(self):
        """
        Calculate outer asymmetry as described in Pawlik et al. (2016).
        """
        image = self._cutout_stamp_maskzeroed
        asym = self._asymmetry_function(self._outer_asymmetry_center,
                                        image, 'outer')
        
        return asym

    @lazyproperty
    def _shape_asymmetry_center(self):
        """
        Find the position of the central pixel (relative to the
        "postage stamp" cutout) that minimizes the shape asymmetry.
        """
        image = np.where(self._segmap_shape_asym, 1.0, 0.0)

        # Initial guess
        center_0 = self._asymmetry_center
        
        # Find minimum at pixel precision (xtol=1)
        center_asym = opt.fmin(self._asymmetry_function, center_0,
                               args=(image, 'shape'), xtol=1e-6, disp=0)

        return center_asym

    @lazyproperty
    def shape_asymmetry(self):
        """
        Calculate shape asymmetry as described in Pawlik et al. (2016).
        """
        image = np.where(self._segmap_shape_asym, 1.0, 0.0)
        asym = self._asymmetry_function(self._shape_asymmetry_center,
                                        image, 'shape')

        return asym


def source_morphology(image, segmap, **kwargs):
    """
    Calculate the morphological parameters of all sources in ``image``
    as labeled by ``segmap``.
    
    Parameters
    ----------
    image : array-like
        A 2D image containing the sources of interest.
        The image must already be background-subtracted.
    segmap : array-like (int) or `photutils.SegmentationImage`
        A 2D segmentation map where different sources are 
        labeled with different positive integer values.
        A value of zero is reserved for the background.

    Other parameters
    ----------------
    kwargs : `~statmorph.SourceMorphology` properties.

    Returns
    -------
    sources_morph : list
        A list of `SourceMorphology` objects, one for each
        source. The morphological parameters can be accessed
        as attributes or keys.

    See Also
    --------
    `SourceMorphology` : Class to measure morphological parameters.
    
    Examples
    --------
    See `README.md` for a usage example.
    
    References
    ----------
    See `README.md` for a list of references.

    """
    assert(image.shape == segmap.shape)
    if not isinstance(segmap, photutils.SegmentationImage):
        segmap = photutils.SegmentationImage(segmap)

    sources_morph = []
    for label in segmap.labels:
        sources_morph.append(SourceMorphology(image, segmap, label, **kwargs))

    return sources_morph

