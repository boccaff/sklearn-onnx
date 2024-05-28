# SPDX-License-Identifier: Apache-2.0

import math
import numpy as np
from onnx.numpy_helper import from_array
from sklearn.gaussian_process.kernels import (
    ConstantKernel,
    DotProduct,
    ExpSineSquared,
    Matern,
    PairwiseKernel,
    Product,
    RationalQuadratic,
    RBF,
    Sum,
    WhiteKernel,
)
from ..algebra.complex_functions import onnx_squareform_pdist, onnx_cdist
from ..algebra.onnx_ops import (
    OnnxMul,
    OnnxMatMul,
    OnnxAdd,
    OnnxTranspose,
    OnnxDiv,
    OnnxExp,
    OnnxNeg,
    OnnxShape,
    OnnxSin,
    OnnxPow,
    OnnxSqueezeApi11,
    OnnxIdentity,
    OnnxReduceSumSquareApi18,
    OnnxReduceL2_typed,
    OnnxEyeLike,
    OnnxGather,
    OnnxConcat,
)
from ..algebra.custom_ops import OnnxCDist

try:
    from ..algebra.onnx_ops import OnnxConstantOfShape
except ImportError:
    OnnxConstantOfShape = None


def convert_kernel_diag(
    kernel, X, output_names=None, dtype=None, optim=None, op_version=None
):
    if op_version is None:
        raise RuntimeError("op_version must not be None.")
    if type(kernel) is Sum:
        return OnnxAdd(
            convert_kernel_diag(
                kernel.k1, X, dtype=dtype, optim=optim, op_version=op_version
            ),
            convert_kernel_diag(
                kernel.k2, X, dtype=dtype, optim=optim, op_version=op_version
            ),
            output_names=output_names,
            op_version=op_version,
        )

    if type(kernel) is Product:
        return OnnxMul(
            convert_kernel_diag(
                kernel.k1, X, dtype=dtype, optim=optim, op_version=op_version
            ),
            convert_kernel_diag(
                kernel.k2, X, dtype=dtype, optim=optim, op_version=op_version
            ),
            output_names=output_names,
            op_version=op_version,
        )

    if type(kernel) is ConstantKernel:
        onnx_zeros = _zero_vector_of_size(
            X, keepdims=0, dtype=dtype, op_version=op_version
        )
        return OnnxAdd(
            onnx_zeros,
            np.array([kernel.constant_value], dtype=dtype),
            output_names=output_names,
            op_version=op_version,
        )

    if type(kernel) in {RBF, ExpSineSquared, RationalQuadratic, Matern}:
        onnx_zeros = _zero_vector_of_size(
            X, keepdims=0, dtype=dtype, op_version=op_version
        )
        if isinstance(kernel, RBF):
            return OnnxAdd(
                onnx_zeros,
                np.array([1], dtype=dtype),
                output_names=output_names,
                op_version=op_version,
            )
        return OnnxAdd(
            onnx_zeros,
            np.array([1], dtype=dtype),
            output_names=output_names,
            op_version=op_version,
        )

    if type(kernel) is DotProduct:
        t_sigma_0 = py_make_float_array(kernel.sigma_0**2, dtype=dtype)
        return OnnxSqueezeApi11(
            OnnxAdd(
                OnnxReduceSumSquareApi18(X, axes=[1], op_version=op_version),
                t_sigma_0,
                op_version=op_version,
            ),
            output_names=output_names,
            axes=[1],
            op_version=op_version,
        )

    raise RuntimeError(
        "Unable to convert diag method for " "class {}.".format(type(kernel))
    )


def py_make_float_array(cst, dtype, as_tensor=False):
    if dtype not in (np.float32, np.float64):
        raise TypeError(
            "A float array must be of dtype "
            "np.float32 or np.float64 ({}).".format(dtype)
        )
    if isinstance(cst, np.ndarray):
        res = cst.astype(dtype)
    elif not isinstance(cst, (int, float, np.float32, np.float64, np.int32, np.int64)):
        raise TypeError("cst must be a number not {}".format(type(cst)))
    else:
        res = np.array([cst], dtype=dtype)
    return from_array(res) if as_tensor else res


def _convert_exp_sine_squared(
    X,
    Y,
    length_scale=1.2,
    periodicity=1.1,
    pi=math.pi,
    dtype=None,
    optim=None,
    op_version=None,
    **kwargs,
):
    """
    Implements the kernel ExpSineSquared.
    """
    if optim is None:
        dists = onnx_cdist(X, Y, metric="euclidean", dtype=dtype, op_version=op_version)
    elif optim == "cdist":
        dists = OnnxCDist(X, Y, metric="euclidean", op_version=op_version)
    else:
        raise ValueError("Unknown optimization '{}'.".format(optim))
    t_pi = py_make_float_array(pi, dtype=dtype)
    t_periodicity = py_make_float_array(periodicity, dtype)
    arg = OnnxMul(
        OnnxDiv(dists, t_periodicity, op_version=op_version),
        t_pi,
        op_version=op_version,
    )
    sin_of_arg = OnnxSin(arg, op_version=op_version)
    t_2 = py_make_float_array(2, dtype=dtype)
    t__2 = py_make_float_array(-2, dtype=dtype)
    t_length_scale = py_make_float_array(length_scale, dtype=dtype)
    K = OnnxExp(
        OnnxMul(
            OnnxPow(
                OnnxDiv(sin_of_arg, t_length_scale, op_version=op_version),
                t_2,
                op_version=op_version,
            ),
            t__2,
            op_version=op_version,
        ),
        op_version=op_version,
    )
    return OnnxIdentity(K, op_version=op_version, **kwargs)


def _convert_dot_product(X, Y, sigma_0=2.0, dtype=None, op_version=None, **kwargs):
    """
    Implements the kernel DotProduct.
    :math:`k(x_i,x_j)=\\sigma_0^2+x_i\\cdot x_j`.
    """
    # It only works in two dimensions.
    t_sigma_0 = py_make_float_array(sigma_0**2, dtype=dtype)
    if isinstance(Y, np.ndarray):
        tr = Y.T.astype(dtype)
    else:
        tr = OnnxTranspose(Y, perm=[1, 0], op_version=op_version)
    matm = OnnxMatMul(X, tr, op_version=op_version)
    K = OnnxAdd(matm, t_sigma_0, op_version=op_version)
    return OnnxIdentity(K, op_version=op_version, **kwargs)


def _convert_rational_quadratic(
    X, Y, length_scale=1.0, alpha=2.0, dtype=None, optim=None, op_version=None, **kwargs
):
    """
    Implements the kernel RationalQuadratic.
    :math:`k(x_i,x_j)=(1 + d(x_i, x_j)^2 / (2*\\alpha * l^2))^{-\\alpha}`.
    """
    if optim is None:
        dists = onnx_cdist(
            X, Y, dtype=dtype, metric="sqeuclidean", op_version=op_version
        )
    elif optim == "cdist":
        dists = OnnxCDist(X, Y, metric="sqeuclidean", op_version=op_version)
    else:
        raise ValueError("Unknown optimization '{}'.".format(optim))
    cst = length_scale**2 * alpha * 2
    t_cst = py_make_float_array(cst, dtype=dtype)
    tmp = OnnxDiv(dists, t_cst, op_version=op_version)
    t_one = py_make_float_array(1, dtype=dtype)
    base = OnnxAdd(tmp, t_one, op_version=op_version)
    t_alpha = py_make_float_array(-alpha, dtype=dtype)
    K = OnnxPow(base, t_alpha, op_version=op_version)
    return OnnxIdentity(K, op_version=op_version, **kwargs)


def _convert_pairwise_kernel(
    X,
    Y,
    metric=None,
    dtype=None,
    op_version=None,
    gamma=None,
    gamma_bounds=None,
    degree=None,
    coef0=None,
    optim=None,
    **kwargs,
):
    """
    Implements the kernel PairwiseKernel.

    * cosine: :math:`k(x_i,x_j)=\\frac{<x_i, x_j>}
        {\\parallel x_i \\parallel \\parallel x_j \\parallel}`

    See `KERNEL_PARAMS
    <https://github.com/scikit-learn/scikit-learn/blob/
    95119c13af77c76e150b753485c662b7c52a41a2/sklearn/
    metrics/pairwise.py#L1862>`_.
    """
    if metric == "cosine":
        if isinstance(Y, np.ndarray):
            ny = np.sqrt(np.sum(Y**2, axis=1, keepdims=True))
            norm_y = Y / ny
            norm_try = norm_y.T.astype(dtype)
        else:
            ny = OnnxReduceL2_typed(dtype, Y, axes=[1], op_version=op_version)
            norm_y = OnnxDiv(Y, ny, op_version=op_version)
            norm_try = OnnxTranspose(norm_y, perm=[1, 0], op_version=op_version)

        nx = OnnxReduceL2_typed(dtype, X, axes=[1], op_version=op_version)
        norm_x = OnnxDiv(X, nx, op_version=op_version)
        K = OnnxMatMul(norm_x, norm_try, op_version=op_version)
        return OnnxIdentity(K, op_version=op_version, **kwargs)
    raise NotImplementedError("Metric %r is not implemented." % metric)


def convert_kernel(
    kernel, X, output_names=None, x_train=None, dtype=None, optim=None, op_version=None
):
    if op_version is None:
        raise RuntimeError("op_version must not be None.")
    if type(kernel) is Sum:
        clop = OnnxAdd
    elif type(kernel) is Product:
        clop = OnnxMul
    else:
        clop = None
    if clop is not None:
        return clop(
            convert_kernel(
                kernel.k1,
                X,
                x_train=x_train,
                dtype=dtype,
                optim=optim,
                op_version=op_version,
            ),
            convert_kernel(
                kernel.k2,
                X,
                x_train=x_train,
                dtype=dtype,
                optim=optim,
                op_version=op_version,
            ),
            output_names=output_names,
            op_version=op_version,
        )

    if type(kernel) is ConstantKernel:
        # X and x_train should have the same number of features.
        onnx_zeros_x = _zero_vector_of_size(
            X, keepdims=1, dtype=dtype, op_version=op_version
        )
        if x_train is None:
            onnx_zeros_y = onnx_zeros_x
        else:
            onnx_zeros_y = _zero_vector_of_size(
                x_train, keepdims=1, dtype=dtype, op_version=op_version
            )

        tr = OnnxTranspose(onnx_zeros_y, perm=[1, 0], op_version=op_version)
        mat = OnnxMatMul(onnx_zeros_x, tr, op_version=op_version)
        return OnnxAdd(
            mat,
            np.array([kernel.constant_value], dtype=dtype),
            output_names=output_names,
            op_version=op_version,
        )

    if type(kernel) in (RBF, Matern):
        # length_scale = np.squeeze(length_scale).astype(float)
        if isinstance(kernel.length_scale, np.ndarray) and len(kernel.length_scale) > 0:
            const = kernel.length_scale.astype(dtype)
        else:
            const = np.array([kernel.length_scale], dtype=dtype)
        X_scaled = OnnxDiv(X, const, op_version=op_version)
        if x_train is None:
            dist = onnx_squareform_pdist(
                X_scaled,
                metric="sqeuclidean" if type(kernel) is RBF else "euclidean",
                dtype=dtype,
                op_version=op_version,
            )
        else:
            x_train_scaled = OnnxDiv(x_train, const, op_version=op_version)
            if optim is None:
                dist = onnx_cdist(
                    X_scaled,
                    x_train_scaled,
                    metric="sqeuclidean" if type(kernel) is RBF else "euclidean",
                    dtype=dtype,
                    op_version=op_version,
                )
            elif optim == "cdist":
                dist = OnnxCDist(
                    X_scaled,
                    x_train_scaled,
                    metric="sqeuclidean",
                    op_version=op_version,
                )
            else:
                raise ValueError("Unknown optimization '{}'.".format(optim))

        # see https://github.com/scikit-learn/scikit-learn/blob/main/sklearn/gaussian_process/kernels.py#L1719
        if type(kernel) is RBF:
            K = OnnxMul(dist, np.array([0.5], dtype=dtype), op_version=op_version)
            return OnnxExp(
                OnnxNeg(K, op_version=op_version),
                op_version=op_version,
                output_names=output_names,
            )
        # Matern
        if kernel.nu == 0.5:
            # K = np.exp(-dists
            return OnnxExp(
                OnnxNeg(dist, op_version=op_version),
                op_version=op_version,
                output_names=output_names,
            )

        if kernel.nu == 1.5:
            # K = dists * math.sqrt(3)
            # K = (1.0 + K) * np.exp(-K)
            K = OnnxMul(dist, np.array([3**0.5], dtype=dtype), op_version=op_version)
            exp_k = OnnxExp(OnnxNeg(K, op_version=op_version), op_version=op_version)
            k_1 = OnnxAdd(K, np.array([1], dtype=dtype), op_version=op_version)
            return OnnxMul(k_1, exp_k, output_names=output_names, op_version=op_version)

        if kernel.nu == 2.5:
            # K = dists * math.sqrt(5)
            # K = (1.0 + K + K**2 / 3.0) * np.exp(-K)
            K = OnnxMul(dist, np.array([5**0.5], dtype=dtype), op_version=op_version)
            exp_k = OnnxExp(OnnxNeg(K, op_version=op_version), op_version=op_version)
            k_12 = OnnxAdd(
                OnnxAdd(K, np.array([1], dtype=type), op_version=op_version),
                OnnxDiv(
                    OnnxMul(K, K, op_version=op_version),
                    np.array([3], dtype=dtype),
                    op_version=op_version,
                ),
                op_version=op_version,
            )
            return OnnxMul(
                k_12, exp_k, output_names=output_names, op_version=op_version
            )

        if kernel.nu == np.inf:
            # K = np.exp(-(dists**2) / 2.0)
            return OnnxExp(
                OnnxNeg(
                    OnnxDiv(
                        OnnxMul(dist, dist, op_version=op_version),
                        np.array([2], dtype=dtype),
                        op_version=op_version,
                    ),
                    op_version=op_version,
                ),
                op_version=op_version,
                output_names=output_names,
            )

        raise RuntimeError(
            f"The converter is not implemented for Matern(nu={kernel.nu}, ...)."
        )

    if type(kernel) is ExpSineSquared:
        if not isinstance(kernel.length_scale, (float, int)):
            raise NotImplementedError(
                "length_scale should be float not {}.".format(type(kernel.length_scale))
            )

        return _convert_exp_sine_squared(
            X,
            Y=X if x_train is None else x_train,
            length_scale=kernel.length_scale,
            periodicity=kernel.periodicity,
            dtype=dtype,
            output_names=output_names,
            optim=optim,
            op_version=op_version,
        )

    if type(kernel) is DotProduct:
        if not isinstance(kernel.sigma_0, (float, int)):
            raise NotImplementedError(
                "sigma_0 should be float not {}.".format(type(kernel.sigma_0))
            )

        if x_train is None:
            return _convert_dot_product(
                X,
                X,
                sigma_0=kernel.sigma_0,
                dtype=dtype,
                output_names=output_names,
                op_version=op_version,
            )
        else:
            if len(x_train.shape) != 2:
                raise NotImplementedError(
                    "Only DotProduct for two dimension train set is " "implemented."
                )
            return _convert_dot_product(
                X,
                x_train,
                sigma_0=kernel.sigma_0,
                dtype=dtype,
                output_names=output_names,
                op_version=op_version,
            )

    if type(kernel) is RationalQuadratic:
        if x_train is None:
            return _convert_rational_quadratic(
                X,
                X,
                length_scale=kernel.length_scale,
                dtype=dtype,
                alpha=kernel.alpha,
                output_names=output_names,
                optim=optim,
                op_version=op_version,
            )
        else:
            return _convert_rational_quadratic(
                X,
                x_train,
                length_scale=kernel.length_scale,
                dtype=dtype,
                alpha=kernel.alpha,
                output_names=output_names,
                optim=optim,
                op_version=op_version,
            )

    if type(kernel) is PairwiseKernel:
        if x_train is None:
            return _convert_pairwise_kernel(
                X,
                X,
                metric=kernel.metric,
                dtype=dtype,
                output_names=output_names,
                optim=optim,
                op_version=op_version,
            )
        else:
            return _convert_pairwise_kernel(
                X,
                x_train,
                metric=kernel.metric,
                dtype=dtype,
                output_names=output_names,
                optim=optim,
                op_version=op_version,
            )

    if type(kernel) is WhiteKernel:
        # X and x_train should have the same number of features.
        onnx_zeros_x = _zero_vector_of_size(
            X, keepdims=1, dtype=dtype, op_version=op_version
        )
        if x_train is None:
            onnx_zeros_y = onnx_zeros_x
        else:
            onnx_zeros_y = _zero_vector_of_size(
                x_train, keepdims=1, dtype=dtype, op_version=op_version
            )
        tr = OnnxTranspose(onnx_zeros_y, perm=[1, 0], op_version=op_version)
        mat = OnnxMatMul(onnx_zeros_x, tr, op_version=op_version)

        if x_train is not None:
            return OnnxIdentity(mat, op_version=op_version, output_names=output_names)

        return OnnxMul(
            OnnxEyeLike(mat, op_version=op_version),
            OnnxIdentity(
                np.array([kernel.noise_level], dtype=dtype), op_version=op_version
            ),
            op_version=op_version,
            output_names=output_names,
        )

    raise RuntimeError(
        "Unable to convert __call__ method for " "class {}.".format(type(kernel))
    )


def _zero_vector_of_size(
    X, output_names=None, axis=0, keepdims=None, dtype=None, op_version=None
):
    if op_version is None:
        raise RuntimeError("op_version must not be None.")
    if keepdims is None:
        raise ValueError("Default for keepdims is not allowed.")

    shape = OnnxShape(X, op_version=op_version)
    if axis == 0:
        dim = OnnxGather(shape, np.array([0], dtype=np.int64), op_version=op_version)
        new_shape = (
            OnnxConcat(
                dim, np.array([1], dtype=np.int64), axis=0, op_version=op_version
            )
            if keepdims
            else dim
        )
    else:
        dim = OnnxGather(shape, np.array([1], dtype=np.int64), op_version=op_version)
        new_shape = (
            OnnxConcat(
                np.array([1], dtype=np.int64), dim, axis=0, op_version=op_version
            )
            if keepdims
            else dim
        )

    return OnnxConstantOfShape(
        new_shape,
        output_names=output_names,
        op_version=op_version,
        value=from_array(np.array([0], dtype=dtype)),
    )
