# gpu4pyscf is a plugin to use Nvidia GPU in PySCF package
#
# Copyright (C) 2022 Qiming Sun
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import sys
import numpy as np
import cupy
import ctypes
from gpu4pyscf.lib import logger
from gpu4pyscf.gto import mole
from gpu4pyscf.lib.cutensor import contract
from gpu4pyscf.lib.cusolver import eigh, cholesky  #NOQA

LMAX_ON_GPU = 6
DSOLVE_LINDEP = 1e-15

c2s_l = mole.get_cart2sph(lmax=LMAX_ON_GPU)
c2s_data = cupy.concatenate([x.ravel() for x in c2s_l])
c2s_offset = np.cumsum([0] + [x.shape[0]*x.shape[1] for x in c2s_l])

def load_library(libname):
    try:
        _loaderpath = os.path.dirname(__file__)
        return np.ctypeslib.load_library(libname, _loaderpath)
    except OSError:
        raise

libcupy_helper = load_library('libcupy_helper')

pinned_memory_pool = cupy.cuda.PinnedMemoryPool()
cupy.cuda.set_pinned_memory_allocator(pinned_memory_pool.malloc)
def pin_memory(array):
    mem = cupy.cuda.alloc_pinned_memory(array.nbytes)
    ret = np.frombuffer(mem, array.dtype, array.size).reshape(array.shape)
    ret[...] = array
    return ret

def release_gpu_stack():
    cupy.cuda.runtime.deviceSetLimit(0x00, 128)

def print_mem_info():
    mempool = cupy.get_default_memory_pool()
    cupy.get_default_memory_pool().free_all_blocks()
    cupy.get_default_pinned_memory_pool().free_all_blocks()
    mem_avail = cupy.cuda.runtime.memGetInfo()[0]
    total_mem = mempool.total_bytes()
    used_mem = mempool.used_bytes()
    mem_limit = mempool.get_limit()
    #stack_size_per_thread = cupy.cuda.runtime.deviceGetLimit(0x00)
    #mem_stack = stack_size_per_thread
    GB = 1024 * 1024 * 1024
    print(f'mem_avail: {mem_avail/GB:.3f} GB, total_mem: {total_mem/GB:.3f} GB, used_mem: {used_mem/GB:.3f} GB,mem_limt: {mem_limit/GB:.3f} GB')

def get_avail_mem():
    mempool = cupy.get_default_memory_pool()
    used_mem = mempool.used_bytes()
    mem_limit = mempool.get_limit()
    if(mem_limit != 0):
        return mem_limit - used_mem
    else:
        total_mem = mempool.total_bytes()
        # get memGetInfo() is slow
        mem_avail = cupy.cuda.runtime.memGetInfo()[0]
        return mem_avail + total_mem - used_mem

def device2host_2d(a_cpu, a_gpu, stream=None):
    if stream is None:
        stream = cupy.cuda.get_current_stream()
    libcupy_helper.async_d2h_2d(
        ctypes.cast(stream.ptr, ctypes.c_void_p),
        a_cpu.ctypes.data_as(ctypes.c_void_p),
        ctypes.c_int(a_cpu.strides[0]),
        ctypes.cast(a_gpu.data.ptr, ctypes.c_void_p),
        ctypes.c_int(a_gpu.strides[0]),
        ctypes.c_int(a_gpu.shape[0]),
        ctypes.c_int(a_gpu.shape[1]))

# define cupy array with tags
class CPArrayWithTag(cupy.ndarray):
    pass

def tag_array(a, **kwargs):
    ''' attach attributes to cupy ndarray'''
    t = cupy.asarray(a).view(CPArrayWithTag)
    if isinstance(a, CPArrayWithTag):
        t.__dict__.update(a.__dict__)
    t.__dict__.update(kwargs)
    return t

def unpack_tril(cderi_tril, cderi, stream=None):
    nao = cderi.shape[1]
    count = cderi_tril.shape[0]
    if stream is None:
        stream = cupy.cuda.get_current_stream()
    err = libcupy_helper.unpack_tril(
        ctypes.cast(stream.ptr, ctypes.c_void_p),
        ctypes.cast(cderi_tril.data.ptr, ctypes.c_void_p),
        ctypes.cast(cderi.data.ptr, ctypes.c_void_p),
        ctypes.c_int(nao),
        ctypes.c_int(count))
    if err != 0:
        raise RuntimeError('failed in unpack_tril kernel')
    return

def unpack_sparse(cderi_sparse, row, col, p0, p1, nao, out=None, stream=None):
    if stream is None:
        stream = cupy.cuda.get_current_stream()
    if out is None:
        out = cupy.zeros([nao,nao,p1-p0])
    nij = len(row)
    naux = cderi_sparse.shape[1]
    nao = out.shape[1]
    err = libcupy_helper.unpack_sparse(
        ctypes.cast(stream.ptr, ctypes.c_void_p),
        ctypes.cast(cderi_sparse.data.ptr, ctypes.c_void_p),
        ctypes.cast(row.data.ptr, ctypes.c_void_p),
        ctypes.cast(col.data.ptr, ctypes.c_void_p),
        ctypes.cast(out.data.ptr, ctypes.c_void_p),
        ctypes.c_int(nao),
        ctypes.c_int(nij),
        ctypes.c_int(naux),
        ctypes.c_int(p0),
        ctypes.c_int(p1)
    )
    if err != 0:
        raise RuntimeError('failed in unpack_sparse')
    return out

def add_sparse(a, b, indices):
    '''
    a[:,...,:np.ix_(indices, indices)] += b
    '''
    assert a.flags.c_contiguous
    assert b.flags.c_contiguous
    n = a.shape[-1]
    m = b.shape[-1]
    if a.ndim > 2:
        count = np.prod(a.shape[:-2])
    elif a.ndim == 2:
        count = 1
    else:
        raise RuntimeError('add_sparse only supports 2d or 3d tensor')
    stream = cupy.cuda.get_current_stream()
    err = libcupy_helper.add_sparse(
        ctypes.cast(stream.ptr, ctypes.c_void_p),
        ctypes.cast(a.data.ptr, ctypes.c_void_p),
        ctypes.cast(b.data.ptr, ctypes.c_void_p),
        ctypes.cast(indices.data.ptr, ctypes.c_void_p),
        ctypes.c_int(n),
        ctypes.c_int(m),
        ctypes.c_int(count)
    )
    if err != 0:
        raise RecursionError('failed in sparse_add2d')
    return a

def block_c2s_diag(ncart, nsph, angular, counts):
    '''
    constract a cartesian to spherical transformation of n shells
    '''

    nshells = np.sum(counts)
    cart2sph = cupy.zeros([ncart, nsph])
    rows = [0]
    cols = [0]
    offsets = []
    for l, count in zip(angular, counts):
        for _ in range(count):
            r, c = c2s_l[l].shape
            rows.append(rows[-1] + r)
            cols.append(cols[-1] + c)
            offsets.append(c2s_offset[l])
    rows = np.asarray(rows, dtype='int32')
    cols = np.asarray(cols, dtype='int32')
    offsets = np.asarray(offsets, dtype='int32')

    rows = cupy.asarray(rows, dtype='int32')
    cols = cupy.asarray(cols, dtype='int32')
    offsets = cupy.asarray(offsets, dtype='int32')

    stream = cupy.cuda.get_current_stream()
    err = libcupy_helper.block_diag(
        ctypes.cast(stream.ptr, ctypes.c_void_p),
        ctypes.cast(cart2sph.data.ptr, ctypes.c_void_p),
        ctypes.c_int(ncart),
        ctypes.c_int(nsph),
        ctypes.cast(c2s_data.data.ptr, ctypes.c_void_p),
        ctypes.c_int(nshells),
        ctypes.cast(offsets.data.ptr, ctypes.c_void_p),
        ctypes.cast(rows.data.ptr, ctypes.c_void_p),
        ctypes.cast(cols.data.ptr, ctypes.c_void_p),
    )
    if err != 0:
        raise RuntimeError('failed in block_diag kernel')
    return cart2sph

def block_diag(blocks, out=None):
    '''
    each block size is up to 16x16
    '''
    rows = np.cumsum(np.asarray([0] + [x.shape[0] for x in blocks]))
    cols = np.cumsum(np.asarray([0] + [x.shape[1] for x in blocks]))
    offsets = np.cumsum(np.asarray([0] + [x.shape[0]*x.shape[1] for x in blocks]))

    m, n = rows[-1], cols[-1]
    if out is None: out = cupy.zeros([m, n])
    rows = cupy.asarray(rows, dtype='int32')
    cols = cupy.asarray(cols, dtype='int32')
    offsets = cupy.asarray(offsets, dtype='int32')
    data = cupy.concatenate([x.ravel() for x in blocks])
    stream = cupy.cuda.get_current_stream()
    err = libcupy_helper.block_diag(
        ctypes.cast(stream.ptr, ctypes.c_void_p),
        ctypes.cast(out.data.ptr, ctypes.c_void_p),
        ctypes.c_int(m),
        ctypes.c_int(n),
        ctypes.cast(data.data.ptr, ctypes.c_void_p),
        ctypes.c_int(len(blocks)),
        ctypes.cast(offsets.data.ptr, ctypes.c_void_p),
        ctypes.cast(rows.data.ptr, ctypes.c_void_p),
        ctypes.cast(cols.data.ptr, ctypes.c_void_p),
    )
    if err != 0:
        raise RuntimeError('failed in block_diag kernel')
    return out

def take_last2d(a, indices, out=None):
    '''
    reorder the last 2 dimensions with 'indices', the first n-2 indices do not change
    shape in the last 2 dimensions have to be the same
    '''
    assert a.flags.c_contiguous
    assert a.shape[-1] == a.shape[-2]
    nao = a.shape[-1]
    if a.ndim == 2:
        count = 1
    else:
        count = np.prod(a.shape[:-2])
    if out is None:
        out = cupy.zeros_like(a)
    indices_int32 = cupy.asarray(indices, dtype='int32')
    stream = cupy.cuda.get_current_stream()
    err = libcupy_helper.take_last2d(
        ctypes.cast(stream.ptr, ctypes.c_void_p),
        ctypes.cast(out.data.ptr, ctypes.c_void_p),
        ctypes.cast(a.data.ptr, ctypes.c_void_p),
        ctypes.cast(indices_int32.data.ptr, ctypes.c_void_p),
        ctypes.c_int(count),
        ctypes.c_int(nao)
    )
    if err != 0:
        raise RuntimeError('failed in take_last2d kernel')
    return out

def transpose_sum(a, stream=None):
    '''
    return a + a.transpose(0,2,1)
    '''
    assert a.flags.c_contiguous
    assert a.ndim == 3
    n = a.shape[-1]
    count = a.shape[0]
    stream = cupy.cuda.get_current_stream()
    err = libcupy_helper.transpose_sum(
        ctypes.cast(stream.ptr, ctypes.c_void_p),
        ctypes.cast(a.data.ptr, ctypes.c_void_p),
        ctypes.c_int(n),
        ctypes.c_int(count)
    )
    if err != 0:
        raise RuntimeError('failed in transpose_sum kernel')
    return a

# for i > j of 2d mat, mat[j,i] = mat[i,j]
def hermi_triu(mat, hermi=1, inplace=True):
    '''
    Use the elements of the lower triangular part to fill the upper triangular part.
    See also pyscf.lib.hermi_triu
    '''
    if not inplace:
        mat = mat.copy('C')
    assert mat.flags.c_contiguous

    if mat.ndim == 2:
        n = mat.shape[0]
        counts = 1
    elif mat.ndim == 3:
        counts, n = mat.shape[:2]
    else:
        raise ValueError(f'dimension not supported {mat.ndim}')

    err = libcupy_helper.CPdsymm_triu(
        ctypes.cast(mat.data.ptr, ctypes.c_void_p),
        ctypes.c_int(n), ctypes.c_int(counts))
    if err != 0:
        raise RuntimeError('failed in symm_triu kernel')

    return mat

def cart2sph(t, axis=0, ang=1, out=None):
    '''
    transform 'axis' of a tensor from cartesian basis into spherical basis
    '''
    if(ang <= 1):
        if(out is not None): out[:] = t
        return t
    size = list(t.shape)
    c2s = c2s_l[ang]
    if(not t.flags['C_CONTIGUOUS']): t = cupy.asarray(t, order='C')
    li_size = c2s.shape
    nli = size[axis] // li_size[0]
    i0 = max(1, np.prod(size[:axis]))
    i3 = max(1, np.prod(size[axis+1:]))
    out_shape = size[:axis] + [nli*li_size[1]] + size[axis+1:]

    t_cart = t.reshape([i0*nli, li_size[0], i3])
    if(out is not None):
        out = out.reshape([i0*nli, li_size[1], i3])
    t_sph = contract('min,ip->mpn', t_cart, c2s, out=out)
    return t_sph.reshape(out_shape)

# a copy with modification from
# https://github.com/pyscf/pyscf/blob/9219058ac0a1bcdd8058166cad0fb9127b82e9bf/pyscf/lib/linalg_helper.py#L1536
def krylov(aop, b, x0=None, tol=1e-10, max_cycle=30, dot=cupy.dot,
           lindep=DSOLVE_LINDEP, callback=None, hermi=False,
           verbose=logger.WARN):
    r'''Krylov subspace method to solve  (1+a) x = b.  Ref:
    J. A. Pople et al, Int. J.  Quantum. Chem.  Symp. 13, 225 (1979).
    Args:
        aop : function(x) => array_like_x
            aop(x) to mimic the matrix vector multiplication :math:`\sum_{j}a_{ij} x_j`.
            The argument is a 1D array.  The returned value is a 1D array.
        b : a vector or a list of vectors
    Kwargs:
        x0 : 1D array
            Initial guess
        tol : float
            Tolerance to terminate the operation aop(x).
        max_cycle : int
            max number of iterations.
        lindep : float
            Linear dependency threshold.  The function is terminated when the
            smallest eigenvalue of the metric of the trial vectors is lower
            than this threshold.
        dot : function(x, y) => scalar
            Inner product
        callback : function(envs_dict) => None
            callback function takes one dict as the argument which is
            generated by the builtin function :func:`locals`, so that the
            callback function can access all local variables in the current
            envrionment.
    Returns:
        x : ndarray like b
    '''
    if isinstance(aop, cupy.ndarray) and aop.ndim == 2:
        return cupy.linalg.solve(aop+cupy.eye(aop.shape[0]), b)

    if isinstance(verbose, logger.Logger):
        log = verbose
    else:
        log = logger.Logger(sys.stdout, verbose)

    if not (isinstance(b, cupy.ndarray) and b.ndim == 1):
        b = cupy.asarray(b)

    if x0 is None:
        x1 = b
    else:
        b = b - (x0 + aop(x0))
        x1 = b
    if x1.ndim == 1:
        x1 = x1.reshape(1, x1.size)
    nroots, ndim = x1.shape

    # Not exactly QR, vectors are orthogonal but not normalized
    x1, rmat = _qr(x1, cupy.dot, lindep)
    for i in range(len(x1)):
        x1[i] *= rmat[i,i]

    innerprod = [cupy.dot(xi.conj(), xi).real for xi in x1]
    if innerprod:
        max_innerprod = max(innerprod)
    else:
        max_innerprod = 0
    if max_innerprod < lindep or max_innerprod < tol**2:
        if x0 is None:
            return cupy.zeros_like(b)
        else:
            return x0

    xs = []
    ax = []

    max_cycle = min(max_cycle, ndim)
    for cycle in range(max_cycle):
        axt = aop(x1)
        if axt.ndim == 1:
            axt = axt.reshape(1,ndim)
        xs.extend(x1)
        ax.extend(axt)
        if callable(callback):
            callback(cycle, xs, ax)

        x1 = axt.copy()
        for i in range(len(xs)):
            xsi = cupy.asarray(xs[i])
            for j, axj in enumerate(axt):
                x1[j] -= xsi * (cupy.dot(xsi.conj(), axj) / innerprod[i])
        axt = xsi = None

        max_innerprod = 0
        idx = []
        for i, xi in enumerate(x1):
            innerprod1 = cupy.dot(xi.conj(), xi).real
            max_innerprod = max(max_innerprod, innerprod1)
            if innerprod1 > lindep and innerprod1 > tol**2:
                idx.append(i)
                innerprod.append(innerprod1)
        log.debug('krylov cycle %d  r = %g', cycle, max_innerprod**.5)

        if max_innerprod < lindep or max_innerprod < tol**2:
            break

        x1 = x1[idx]

    xs = cupy.asarray(xs)
    ax = cupy.asarray(ax)
    nd = cycle + 1

    h = cupy.dot(xs, ax.T)

    # Add the contribution of I in (1+a)
    h += cupy.diag(cupy.asarray(innerprod[:nd]))
    g = cupy.zeros((nd,nroots), dtype=x1.dtype)

    if b.ndim == 1:
        g[0] = innerprod[0]
    else:
        ng = min(nd, nroots)
        g[:ng, :nroots] += cupy.dot(xs[:ng], b[:nroots].T)
        '''
        # Restore the first nroots vectors, which are array b or b-(1+a)x0
        for i in range(min(nd, nroots)):
            xsi = cupy.asarray(xs[i])
            for j in range(nroots):
                g[i,j] = cupy.dot(xsi.conj(), b[j])
        '''

    c = cupy.linalg.solve(h, g)
    x = _gen_x0(c, cupy.asarray(xs))
    if b.ndim == 1:
        x = x[0]

    if x0 is not None:
        x += x0
    return x

def _qr(xs, dot, lindep=1e-14):
    '''QR decomposition for a list of vectors (for linearly independent vectors only).
    xs = (r.T).dot(qs)
    '''
    nvec = len(xs)
    dtype = xs[0].dtype
    qs = cupy.empty((nvec,xs[0].size), dtype=dtype)
    rmat = cupy.empty((nvec,nvec), order='F', dtype=dtype)

    nv = 0
    for i in range(nvec):
        xi = cupy.array(xs[i], copy=True)
        rmat[:,nv] = 0
        rmat[nv,nv] = 1
        for j in range(nv):
            prod = dot(qs[j].conj(), xi)
            xi -= qs[j] * prod
            rmat[:,nv] -= rmat[:,j] * prod
        innerprod = dot(xi.conj(), xi).real
        norm = cupy.sqrt(innerprod)
        if innerprod > lindep:
            qs[nv] = xi/norm
            rmat[:nv+1,nv] /= norm
            nv += 1
    return qs[:nv], cupy.linalg.inv(rmat[:nv,:nv])

def _gen_x0(v, xs):
    return cupy.dot(v.T, xs)
