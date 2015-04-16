import numpy as np
import numpy.ma as ma
from scipy.misc import logsumexp
from scipy import linalg as la
from scipy.cluster.vq import kmeans, vq

import multiprocessing as mp


def optimized_kde(data, pool=None, kde=None, max_samples=None, **kwargs):
    """
    Iteratively run a k-means clustering algorithm, estimating the distibution
    of each identified cluster with an independent kernel density estimate.
    Starting with k = 1, the distribution is estimated and the Bayes
    Information criterion (BIC) is calculated.  k is increased until the BIC
    stops increasing.  ``kwargs`` are passed to ``ClusteredKDE``.  Returns the
    KDE with the best BIC.

    :param data:
        An N x ndim array, containing N samples from the target distribution.

    :param pool: (optional)
        A pool of processes with `map` function to use.

    :param kde: (optional)
        An old KDE to update, instead of starting a new one from scratch.

    :param max_samples: (optional)
        The maximum number of samples to use for constructing or updating the kde.
        If a KDE is supplied and adding the samples from `data` will go over this,
        old samples are thinned by factors of two until under the limit.

    """
    # Trim data if too many samples were given
    n_new = len(data)
    if max_samples is not None and max_samples <= n_new:
        data = data[:max_samples]

    else:
        # Combine data, thinning old data if we need room
        if kde is not None:
            old_data = kde._color(kde._data)

            if max_samples is not None:
                N = len(old_data) + n_new

                while N > max_samples:
                    old_data = old_data[::2]
                    N = len(old_data) + n_new

            data = np.concatenate((old_data, data))

    best_bic = -np.inf
    best_kde = None

    k = 1
    while True:
        try:
            kde = ClusteredKDE(data, k, **kwargs)
            bic = kde.bic(pool=pool)
        except la.LinAlgError:
            bic = -np.inf

        if (bic > best_bic):
            best_kde = kde
            best_bic = bic
        else:
            break
        k += 1

    return best_kde


class ClusteredKDE(object):
    """
    Run a k-means clustering algorithm, estimating the distibution of each
    identified cluster with an independent kernel density estimate.  The full
    distibution is then estimated by combining the individual KDE's, weighted
    by the fraction of samples assigned to each cluster.

    :param data:
        An N x ndim array, containing N samples from the target distribution.

    :param k:
        The number of clusters to use in the k-means clustering.

    """
    def __init__(self, data, k=1):
        N, dim = data.shape
        self._N = N
        self._dim = dim
        self._k = k

        self._mean = np.mean(data, axis=0)
        self._std = np.std(data, axis=0)
        self._data = self._whiten(data)

        self._centroids, _ = kmeans(self._data, k)
        self._assignments, _ = vq(self._data, self._centroids)

        self._kdes = [KDE(self._data[self._assignments == c])
                      for c in range(k)]
        self._logweights = np.log(
            [np.sum(self._assignments == c)/float(self._N) for c in range(k)])

    def draw(self, N=1):
        # Draws clusters randomly with the assigned weights
        cumulative_weights = np.cumsum(np.exp(self._logweights))
        clusters = np.searchsorted(cumulative_weights, np.random.rand(N))

        draws = np.empty((N, self._dim))
        for c in xrange(self._k):
            sel = clusters == c
            draws[sel] = self._kdes[c].draw(np.sum(sel))

        return self._color(draws)

    def _whitened_logpdf(self, X, pool=None):
        logpdfs = [logweight + kde(X, pool=pool)
                   for logweight, kde in zip(self._logweights, self._kdes)]
        if len(X.shape) == 1:
            return logsumexp(logpdfs)
        else:
            return logsumexp(logpdfs, axis=0)

    def logpdf(self, X, pool=None):
        return self._whitened_logpdf(self._whiten(X), pool=pool)

    def _whiten(self, data):
        return (data - self._mean)/self._std

    def _color(self, data):
        return data * self._std + self._mean

    def bic(self, pool=None):
        log_l = np.sum(self._whitened_logpdf(self._data, pool=pool))

        # Determine the total number of parameters in clustered-KDE
        # Account for centroid locations
        nparams = self._k * self._dim

        # One for each cluster, minus one for constraint that all sum to unity
        nparams += self._k - 1

        # Separate kernel covariances for each cluster
        nparams += self._k * (self._dim + 1) * self._dim/2.0

        return log_l - nparams/2.0 * np.log(self._N)

    def size(self):
        return self._N

    __call__ = logpdf

    __len__ = size


class KDE(object):
    """
    A Gaussian kernel density estimator that provides means for evaluating
    the estimated probability density function, and drawing additional samples
    from the estimated distribution.  Cholesky decomposition of the covariance
    makes this class a bit more stable than the scipy KDE.

    :param data:
        An N x ndim array, containing N samples from the target distribution.

    """
    def __init__(self, data):
        N, dim = data.shape
        self._N = N
        self._dim = dim
        self._data = data

        self._mean = np.mean(data, axis=0)
        self._cov = oas_cov(data)

        self._set_bandwidth()

    def __enter__(self):
        return self

    def _set_bandwidth(self):
        """
        Use Scott's rule to set the kernel bandwidth.  Also store Cholesky
        decomposition for later.
        """
        if self._N > 0:
            self._kernel_cov = self._cov * self._N ** (-2./(self._dim + 4))

            # Used to evaluate PDF with cho_solve()
            self._cho_factor = la.cho_factor(self._kernel_cov)

            # Make sure the estimated PDF integrates to 1.0
            self._lognorm = self._dim/2.0 * np.log(2.0*np.pi) + np.log(self._N) +\
                np.sum(np.log(np.diag(self._cho_factor[0])))

        else:
            self._lognorm = -np.inf

    def draw(self, N=1):
        """
        Draw samples from the estimated distribution.
        """
        # Return nothing if this is an empty KDE
        if self._N == 0:
            return []

        # Draw vanilla samples from a zero-mean multivariate Gaussian
        X = np.random.multivariate_normal(np.zeros(self._dim),
                                          self._kernel_cov, size=N)

        # Pick N random kernels as means
        kernels = np.random.randint(0, self._N, N)

        # Shift vanilla draws to be about chosen kernels
        return self._data[kernels] + X

    def logpdf(self, X, pool=None):
        X = np.atleast_2d(X)

        N, dim = X.shape
        assert dim == self._dim

        # Apply across the pool if it exists
        if pool:
            M = pool.map
        else:
            M = map

        # Return -inf if this is an empty KDE
        if self._N == 0:
            results = np.zeros(len(X)) - np.inf

        else:
            args = [(x, self._data, self._cho_factor) for x in X]
            results = M(_evaluate_point_logpdf, args)

        # Normalize and return
        return np.array(results) - self._lognorm

    __call__ = logpdf


def unique_spaces(mask):
    ncols = mask.shape[1]
    dtype = mask.dtype.descr * ncols
    struct = mask.view(dtype)

    uniq = np.unique(struct)
    uniq = uniq.view(mask.dtype).reshape(-1, ncols)
    return ~uniq

class TransdimensionalKDE(object):
    """
    A Gaussian kernel density estimator that provides means for evaluating
    the estimated probability density function, and drawing additional samples
    from the estimated distribution.  Cholesky decomposition of the covariance
    makes this class a bit more stable than the scipy KDE.

    :param data:
        An N x ndim array, containing N samples from the target distribution.

    """
    def __init__(self, data, pool=None):
        N, max_dim = data.shape
        self._N = N
        self._max_dim = max_dim
        self._data = data

        self._spaces = unique_spaces(data.mask)

        self._kdes = []
        weights = []
        for space in self._spaces:
            sel = np.all(~data.mask == space, axis=1)
            N = np.sum(sel)
            X = data[sel]
            X = X[~X.mask].reshape((N, -1))
            self._kdes.append(optimized_kde(X, pool=pool))
            weights.append(N/float(self._N))

        self._logweights = np.log(np.array(weights))

    def draw(self, N=1):
        """
        Draw samples from the estimated distribution.
        """
        # Draws spaces randomly with the assigned weights
        cumulative_weights = np.cumsum(np.exp(self._logweights))
        space_inds = np.searchsorted(cumulative_weights, np.random.rand(N))

        draws = ma.masked_values(np.zeros((N, self._max_dim)), 0)
        for s in xrange(len(self._spaces)):
            sel = space_inds == s
            n = np.sum(sel)
            if n > 0:
                draws[sel, self._spaces[s]] = self._kdes[s].draw(n)

        return draws

    def logpdf(self, X, pool=None):
        logpdfs = []
        for logweight, space, kde in zip(self._logweights, self._spaces, self._kdes):
            if np.all(space == ~X.mask):
                logpdfs.append(logweight + kde(X[space], pool=pool))

        return logsumexp(logpdfs, axis=0)

    __call__ = logpdf


def _evaluate_point_logpdf(args):
    """
    Evaluate the Gaussian KDE at a given point ``x''.  This lives
    outside the KDE method to allow for parallelization using
    ``multipocessing``. Since the ``map`` function only allows single-argument
    functions, the following arguments to be packed into a single tuple.

    :param x:
    The point to evaluate the KDE at.

    :param data:
    The N x dim array of data used to construct the KDE.

    :param cho_factor:
    A Cholesky decomposition of the kernel covariance matrix.

    """
    x, data, cho_factor = args

    # Use Cholesky decomposition to avoid direct inversion of covariance matrix
    diff = data - x
    tdiff = la.cho_solve(cho_factor, diff.T, check_finite=False).T
    diff *= tdiff

    # Work in the log to avoid large numbers
    return logsumexp(-np.sum(diff, axis=1)/2.0)


def oas_cov(X):
    """
    Estimate the covariance matrix using the Oracle Approximating Shrinkage
    algorithm, returning

    (1 - shrinkage)*cov + shrinkage * mu * np.identity(ndim)

    where mu = trace(cov) / ndim.  This ensures the covariance matrix estimate
    is well behaved for small sample sizes.

    :param X:
        An N x ndim array, containing N samples from the target distribution.


    This follows the implementation in ``scikit-learn``
    (https://github.com/scikit-learn/scikit-learn/blob/31c5497/sklearn/covariance/shrunk_covariance_.py)
    """
    X = np.asarray(X)
    N, ndim = X.shape

    emperical_cov = np.cov(X, rowvar=0)
    mu = np.trace(emperical_cov) / ndim

    alpha = np.mean(emperical_cov * emperical_cov)
    num = alpha + mu * mu
    den = (N + 1.) * (alpha - (mu * mu) / ndim)

    shrinkage = min(num / den, 1.)
    shrunk_cov = (1. - shrinkage) * emperical_cov
    shrunk_cov.flat[::ndim + 1] += shrinkage * mu

    return shrunk_cov
