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

# modified by Xiaojie Wu (wxj6000@gmail.com)
import numpy
import cupy

from pyscf import lib
from pyscf.dft import rks

from gpu4pyscf import scf
from gpu4pyscf.lib import logger
from gpu4pyscf.dft import numint, gen_grid
from gpu4pyscf.scf.hf import RHF
from gpu4pyscf.lib.cupy_helper import load_library, tag_array

libcupy_helper = load_library('libcupy_helper')

LINEAR_DEP_THR = 1e-12

def prune_small_rho_grids_(ks, mol, dm, grids):
    rho = ks._numint.get_rho(mol, dm, grids, ks.max_memory)

    threshold = ks.small_rho_cutoff
    '''Prune grids if the electron density on the grid is small'''
    if threshold == 0:
        return grids
    mol = grids.mol

    n = cupy.dot(rho, grids.weights)
    if abs(n-mol.nelectron) < gen_grid.NELEC_ERROR_TOL*n:
        rho *= grids.weights
        idx = cupy.abs(rho) > threshold / grids.weights.size

        logger.debug(grids, 'Drop grids %d', grids.weights.size - cupy.count_nonzero(idx))
        grids.coords  = cupy.asarray(grids.coords [idx], order='C')
        grids.weights = cupy.asarray(grids.weights[idx], order='C')
        if grids.alignment:
            padding = gen_grid._padding_size(grids.size, grids.alignment)
            logger.debug(ks, 'prune_by_density_: %d padding grids', padding)
            if padding > 0:
                pad = cupy.array(padding * [[1e4, 1e4, 1e4]])
                grids.coords = cupy.vstack(
                        [grids.coords, pad])
                grids.weights = cupy.hstack([grids.weights, cupy.zeros(padding)])
        # make_mask has to be executed on cpu for now.
        #grids.non0tab = grids.make_mask(mol, grids.coords)
        #grids.screen_index = grids.non0tab
        #if ks._numint.use_sparsity:
        #    ks._numint.build(mol, grids.coords)
    return grids

def initialize_grids(ks, mol=None, dm=None):
    # Initialize self.grids the first time call get_veff
    if mol is None: mol = ks.mol
    if ks.grids.coords is None:
        t0 = logger.init_timer(ks)
        ks.grids.build()
        #ks.grids.build(with_non0tab=True)
        ks.grids.weights = cupy.asarray(ks.grids.weights)
        ks.grids.coords = cupy.asarray(ks.grids.coords)
        if (ks.small_rho_cutoff > 1e-20 and
            # dm.ndim == 2 indicates ground state
            isinstance(dm, cupy.ndarray) and dm.ndim == 2):
            # Filter grids the first time setup grids
            ks.grids = prune_small_rho_grids_(ks, ks.mol, dm, ks.grids)
        t0 = logger.timer_debug1(ks, 'setting up grids', *t0)
        is_nlc = ks.nlc or ks._numint.libxc.is_nlc(ks.xc)
        if is_nlc and ks.nlcgrids.coords is None:
            if ks.nlcgrids.coords is None:
                t0 = logger.init_timer(ks)
                #ks.nlcgrids.build(with_non0tab=True)
                ks.nlcgrids.build()
                ks.nlcgrids.weights = cupy.asarray(ks.nlcgrids.weights)
                ks.nlcgrids.coords = cupy.asarray(ks.nlcgrids.coords)
                if (ks.small_rho_cutoff > 1e-20 and
                    # dm.ndim == 2 indicates ground state
                    isinstance(dm, cupy.ndarray) and dm.ndim == 2):
                    # Filter grids the first time setup grids
                    ks.nlcgrids = prune_small_rho_grids_(ks, ks.mol, dm, ks.nlcgrids)
                t0 = logger.timer_debug1(ks, 'setting up nlc grids', *t0)
    return ks

def get_veff(ks, mol=None, dm=None, dm_last=0, vhf_last=0, hermi=1):
    '''Coulomb + XC functionals
    .. note::
        This function will modify the input ks object.
    Args:
        ks : an instance of :class:`RKS`
            XC functional are controlled by ks.xc attribute.  Attribute
            ks.grids might be initialized.
        dm : ndarray or list of ndarrays
            A density matrix or a list of density matrices
    Kwargs:
        dm_last : ndarray or a list of ndarrays or 0
            The density matrix baseline.  If not 0, this function computes the
            increment of HF potential w.r.t. the reference HF potential matrix.
        vhf_last : ndarray or a list of ndarrays or 0
            The reference Vxc potential matrix.
        hermi : int
            Whether J, K matrix is hermitian
            | 0 : no hermitian or symmetric
            | 1 : hermitian
            | 2 : anti-hermitian
    Returns:
        matrix Veff = J + Vxc.  Veff can be a list matrices, if the input
        dm is a list of density matrices.
    '''

    if mol is None: mol = ks.mol
    if dm is None: dm = ks.make_rdm1()
    t0 = logger.init_timer(ks)
    initialize_grids(ks, mol, dm)

    if hasattr(ks, 'screen_tol') and ks.screen_tol is not None:
        ks.direct_scf_tol = ks.screen_tol
    ground_state = (isinstance(dm, cupy.ndarray) and dm.ndim == 2)

    ni = ks._numint
    if hermi == 2:  # because rho = 0
        n, exc, vxc = 0, 0, 0
    else:
        max_memory = ks.max_memory - lib.current_memory()[0]
        n, exc, vxc = ni.nr_rks(mol, ks.grids, ks.xc, dm, max_memory=max_memory)
        if ks.nlc or ni.libxc.is_nlc(ks.xc):
            if ni.libxc.is_nlc(ks.xc):
                xc = ks.xc
            else:
                assert ni.libxc.is_nlc(ks.nlc)
                xc = ks.nlc
            n, enlc, vnlc = ni.nr_nlc_vxc(mol, ks.nlcgrids, xc, dm,
                                          max_memory=max_memory)

            exc += enlc
            vxc += vnlc
        #logger.debug(ks, 'nelec by numeric integration = %s', n)
    t0 = logger.timer_debug1(ks, 'vxc tot', *t0)

    #enabling range-separated hybrids
    omega, alpha, hyb = ni.rsh_and_hybrid_coeff(ks.xc, spin=mol.spin)
    if abs(hyb) < 1e-10 and abs(alpha) < 1e-10:
        vk = None
        if (ks._eri is None and ks.direct_scf and
            getattr(vhf_last, 'vj', None) is not None):
            ddm = cupy.asarray(dm) - cupy.asarray(dm_last)
            vj = ks.get_j(mol, ddm, hermi)
            vj += vhf_last.vj
        else:
            vj = ks.get_j(mol, dm, hermi)

        vxc += vj
    else:
        if (ks._eri is None and ks.direct_scf and
            getattr(vhf_last, 'vk', None) is not None):
            ddm = cupy.asarray(dm) - cupy.asarray(dm_last)
            vj, vk = ks.get_jk(mol, ddm, hermi)
            vk *= hyb
            if abs(omega) > 1e-10:  # For range separated Coulomb operator
                vklr = ks.get_k(mol, ddm, hermi, omega=omega)
                vklr *= (alpha - hyb)
                vk += vklr
            vj += vhf_last.vj
            vk += vhf_last.vk
        else:
            vj, vk = ks.get_jk(mol, dm, hermi)
            vk *= hyb
            if abs(omega) > 1e-10:
                vklr = ks.get_k(mol, dm, hermi, omega=omega)
                vklr *= (alpha - hyb)
                vk += vklr
        vxc += vj - vk * .5
        if ground_state:
            exc -= cupy.einsum('ij,ji', dm, vk).real * .5 * .5

    if ground_state:
        ecoul = cupy.einsum('ij,ji', dm, vj).real * .5
    else:
        ecoul = None
    t0 = logger.timer_debug1(ks, 'jk total', *t0)
    vxc = tag_array(vxc, ecoul=ecoul, exc=exc, vj=vj, vk=vk)
    return vxc

def energy_elec(ks, dm=None, h1e=None, vhf=None):
    r'''Electronic part of RKS energy.

    Note this function has side effects which cause mf.scf_summary updated.

    Args:
        ks : an instance of DFT class

        dm : 2D ndarray
            one-partical density matrix
        h1e : 2D ndarray
            Core hamiltonian

    Returns:
        RKS electronic energy and the 2-electron contribution
    '''
    if dm is None: dm = ks.make_rdm1()
    if h1e is None: h1e = ks.get_hcore()
    if vhf is None: vhf = ks.get_veff(ks.mol, dm)
    e1 = cupy.einsum('ij,ji->', h1e, dm).real
    ecoul = vhf.ecoul.real
    exc = vhf.exc.real
    e2 = ecoul + exc
    ks.scf_summary['e1'] = e1
    ks.scf_summary['coul'] = ecoul
    ks.scf_summary['exc'] = exc
    logger.debug(ks, 'E1 = %s  Ecoul = %s  Exc = %s', e1, ecoul, exc)
    return e1+e2, e2

class RKS(scf.hf.RHF, rks.RKS):
    from gpu4pyscf.lib.utils import to_cpu, to_gpu, device

    _keys = {'disp'}

    def __init__(self, mol, xc='LDA,VWN', disp=None):
        super().__init__(mol, xc)
        self._numint = numint.NumInt(xc=xc)
        self.disp = disp
        self.screen_tol = 1e-14

        grids_level = self.grids.level
        self.grids = gen_grid.Grids(mol)
        self.grids.level = grids_level

        nlcgrids_level = self.nlcgrids.level
        self.nlcgrids = gen_grid.Grids(mol)
        self.nlcgrids.level = nlcgrids_level

    def get_dispersion(self):
        if self.disp is None:
            return 0.0

        if self.disp[:2].upper() == 'D3':
            # multi-threads in DFTD3 conflicts with PyTorch, set it to be 1 for safty
            from pyscf import lib
            with lib.with_omp_threads(1):
                import dftd3.pyscf as disp
                d3 = disp.DFTD3Dispersion(self.mol, xc=self.xc, version=self.disp)
                e_d3, _ = d3.kernel()
            return e_d3

        if self.disp[:2].upper() == 'D4':
            from pyscf.data.elements import charge
            atoms = numpy.array([ charge(a[0]) for a in self.mol._atom])
            coords = self.mol.atom_coords()
            from pyscf import lib
            with lib.with_omp_threads(1):
                from dftd4.interface import DampingParam, DispersionModel
                model = DispersionModel(atoms, coords)
                res = model.get_dispersion(DampingParam(method=self.xc), grad=False)
            return res.get("energy")

    def reset(self, mol=None):
        super().reset(mol)
        self.grids.reset(mol)
        self.nlcgrids.reset(mol)
        self._numint.gdftopt = None
        return self

    def nuc_grad_method(self):
        from gpu4pyscf.grad import rks as rks_grad
        return rks_grad.Gradients(self)

    energy_elec = energy_elec
    get_jk = RHF.get_jk
    get_veff = get_veff
    _eigh = RHF._eigh
