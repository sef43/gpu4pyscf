from . import lib, grad, hessian, solvent, scf, dft
__version__ = '0.6.14'

# monkey patch libxc reference due to a bug in nvcc
from pyscf.dft import libxc
libxc.__reference__ = 'unable to decode the reference due to https://github.com/NVIDIA/cuda-python/issues/29'
