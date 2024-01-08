#!/usr/bin/env python
# Copyright 2014-2019 The PySCF Developers. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Author: Qiming Sun <osirpt.sun@gmail.com>
# Modified by Xiaojie Wu <wxj6000@gmail.com>

import copy
import cupy
import numpy
from cupy import cublas
from pyscf import lib, scf, __config__
from pyscf.scf import dhf
from pyscf.df import df_jk, addons
from gpu4pyscf.lib import logger
from gpu4pyscf.lib.cupy_helper import contract, take_last2d, transpose_sum, load_library, get_avail_mem
from gpu4pyscf.dft import rks, numint
from gpu4pyscf.scf import hf
from gpu4pyscf.df import df, int3c2e

libcupy_helper = load_library('libcupy_helper')

def _pin_memory(array):
    mem = cupy.cuda.alloc_pinned_memory(array.nbytes)
    ret = numpy.frombuffer(mem, array.dtype, array.size).reshape(array.shape)
    ret[...] = array
    return ret

def init_workflow(mf, dm0=None):
    # build CDERI for omega = 0 and omega ! = 0
    def build_df():
        mf.with_df.build()
        if hasattr(mf, '_numint'):
            omega, _, _ = mf._numint.rsh_and_hybrid_coeff(mf.xc, spin=mf.mol.spin)
            if abs(omega) <= 1e-10: return
            key = '%.6f' % omega
            if key in mf.with_df._rsh_df:
                rsh_df = mf.with_df._rsh_df[key]
            else:
                rsh_df = mf.with_df._rsh_df[key] = copy.copy(mf.with_df).reset()
            rsh_df.build(omega=omega)
        return

    # pre-compute h1e and s1e and cderi for async workflow
    with lib.call_in_background(build_df) as build:
        build()
        mf.s1e = cupy.asarray(mf.get_ovlp(mf.mol))
        mf.h1e = cupy.asarray(mf.get_hcore(mf.mol))
        # for DFT object
        if hasattr(mf, '_numint'):
            ni = mf._numint
            rks.initialize_grids(mf, mf.mol, dm0)
            ni.build(mf.mol, mf.grids.coords)
            mf._numint.xcfuns = numint._init_xcfuns(mf.xc, dm0.ndim==3)
    dm0 = cupy.asarray(dm0)
    return

def _density_fit(mf, auxbasis=None, with_df=None, only_dfj=False):
    '''For the given SCF object, update the J, K matrix constructor with
    corresponding density fitting integrals.
    Args:
        mf : an SCF object
    Kwargs:
        auxbasis : str or basis dict
            Same format to the input attribute mol.basis.  If auxbasis is
            None, optimal auxiliary basis based on AO basis (if possible) or
            even-tempered Gaussian basis will be used.
        only_dfj : str
            Compute Coulomb integrals only and no approximation for HF
            exchange. Same to RIJONX in ORCA
    Returns:
        An SCF object with a modified J, K matrix constructor which uses density
        fitting integrals to compute J and K
    Examples:
    '''

    assert isinstance(mf, scf.hf.SCF)

    if with_df is None:
        if isinstance(mf, dhf.UHF):
            with_df = df.DF4C(mf.mol)
        else:
            with_df = df.DF(mf.mol)
        with_df.max_memory = mf.max_memory
        with_df.stdout = mf.stdout
        with_df.verbose = mf.verbose
        with_df.auxbasis = auxbasis

    if isinstance(mf, df_jk._DFHF):
        if mf.with_df is None:
            mf.with_df = with_df
        elif getattr(mf.with_df, 'auxbasis', None) != auxbasis:
            #logger.warn(mf, 'DF might have been initialized twice.')
            mf = copy.copy(mf)
            mf.with_df = with_df
            mf.only_dfj = only_dfj
        return mf

    dfmf = _DFHF(mf, with_df, only_dfj)
    return lib.set_class(dfmf, (_DFHF, mf.__class__))

class _DFHF(df_jk._DFHF):
    '''
    Density fitting SCF class
    Attributes for density-fitting SCF:
        auxbasis : str or basis dict
            Same format to the input attribute mol.basis.
            The default basis 'weigend+etb' means weigend-coulomb-fit basis
            for light elements and even-tempered basis for heavy elements.
        with_df : DF object
            Set mf.with_df = None to switch off density fitting mode.
    '''

    from gpu4pyscf.lib.utils import to_cpu, to_gpu, device

    _keys = {'rhoj', 'rhok', 'disp', 'screen_tol'}

    def __init__(self, mf, dfobj, only_dfj):
        self.__dict__.update(mf.__dict__)
        self._eri = None
        self.rhoj = None
        self.rhok = None
        self.direct_scf = False
        self.with_df = dfobj
        self.only_dfj = only_dfj
        self._keys = mf._keys.union(['with_df', 'only_dfj'])

    def undo_df(self):
        '''Remove the DFHF Mixin'''
        obj = lib.view(self, lib.drop_class(self.__class__, _DFHF))
        del obj.rhoj, obj.rhok, obj.with_df, obj.only_dfj
        return obj

    def reset(self, mol=None):
        self.with_df.reset(mol)
        return super().reset(mol)

    init_workflow = init_workflow

    def get_jk(self, mol=None, dm=None, hermi=1, with_j=True, with_k=True,
               omega=None):
        if dm is None: dm = self.make_rdm1()
        if self.with_df and self.only_dfj:
            vj = vk = None
            if with_j:
                vj, vk = self.with_df.get_jk(dm, hermi, True, False,
                                             self.direct_scf_tol, omega)
            if with_k:
                vk = super().get_jk(mol, dm, hermi, False, True, omega)[1]
        elif self.with_df:
            vj, vk = self.with_df.get_jk(dm, hermi, with_j, with_k,
                                         self.direct_scf_tol, omega)
        else:
            vj, vk = super().get_jk(mol, dm, hermi, with_j, with_k, omega)
        return vj, vk

    def nuc_grad_method(self):
        if isinstance(self, rks.RKS):
            from gpu4pyscf.df.grad import rks as rks_grad
            return rks_grad.Gradients(self)
        if isinstance(self, hf.RHF):
            from gpu4pyscf.df.grad import rhf as rhf_grad
            return rhf_grad.Gradients(self)
        raise NotImplementedError()

    def Hessian(self):
        from pyscf.dft.rks import KohnShamDFT
        from gpu4pyscf.df.hessian import rhf, rks
        if isinstance(self, scf.rhf.RHF):
            if isinstance(self, KohnShamDFT):
                return rks.Hessian(self)
            else:
                return rhf.Hessian(self)
        else:
            raise NotImplementedError

    @property
    def auxbasis(self):
        return getattr(self.with_df, 'auxbasis', None)

    def get_veff(self, mol=None, dm=None, dm_last=None, vhf_last=0, hermi=1):
        '''
        effective potential
        '''
        if mol is None: mol = self.mol
        if dm is None: dm = self.make_rdm1()

        # for DFT
        if isinstance(self, scf.hf.KohnShamDFT):
            return rks.get_veff(self, dm=dm)

        if self.direct_scf:
            ddm = cupy.asarray(dm) - dm_last
            vj, vk = self.get_jk(mol, ddm, hermi=hermi)
            return vhf_last + vj - vk * .5
        else:
            vj, vk = self.get_jk(mol, dm, hermi=hermi)
            return vj - vk * .5

    def energy_tot(self, dm, h1e, vhf=None):
        '''
        compute tot energy
        '''
        nuc = self.energy_nuc()
        e_tot = self.energy_elec(dm, h1e, vhf)[0] + nuc
        self.scf_summary['nuc'] = nuc.real
        return e_tot

    '''
    def to_cpu(self):
        obj = self.undo_df().to_cpu().density_fit()
        keys = dir(obj)
        obj.__dict__.update(self.__dict__)
        for key in set(dir(self)).difference(keys):
            print(key)
            delattr(obj, key)

        for key in keys:
            val = getattr(obj, key)
            if isinstance(val, cupy.ndarray):
                setattr(obj, key, cupy.asnumpy(val))
            elif hasattr(val, 'to_cpu'):
                setattr(obj, key, val.to_cpu())
        return obj
    '''

def get_jk(dfobj, dms_tag, hermi=1, with_j=True, with_k=True, direct_scf_tol=1e-14, omega=None):
    '''
    get jk with density fitting
    outputs and input are on the same device
    TODO: separate into three cases: j only, k only, j and k
    '''

    log = logger.new_logger(dfobj.mol, dfobj.verbose)
    out_shape = dms_tag.shape
    out_cupy = isinstance(dms_tag, cupy.ndarray)
    if not isinstance(dms_tag, cupy.ndarray):
        dms_tag = cupy.asarray(dms_tag)

    assert(with_j or with_k)
    if dms_tag is None: logger.error("dm is not given")
    nao = dms_tag.shape[-1]
    dms = dms_tag.reshape([-1,nao,nao])
    nset = dms.shape[0]
    t0 = log.init_timer()
    if dfobj._cderi is None:
        log.debug('CDERI not found, build...')
        dfobj.build(direct_scf_tol=direct_scf_tol, omega=omega)

    assert nao == dfobj.nao
    vj = None
    vk = None
    ao_idx = dfobj.intopt.sph_ao_idx
    dms = take_last2d(dms, ao_idx)

    t1 = log.timer_debug1('init jk', *t0)
    rows = dfobj.intopt.cderi_row
    cols = dfobj.intopt.cderi_col
    if with_j:
        dm_sparse = dms[:,rows,cols]
        dm_sparse[:, dfobj.intopt.cderi_diag] *= .5
        vj = cupy.zeros_like(dms)

    if with_k:
        vk = cupy.zeros_like(dms)

    # SCF K matrix with occ
    if nset == 1 and hasattr(dms_tag, 'occ_coeff'):
        occ_coeff = cupy.asarray(dms_tag.occ_coeff[ao_idx, :], order='C')
        nocc = occ_coeff.shape[1]
        blksize = dfobj.get_blksize(extra=nao*nocc)
        if with_j:
            vj_packed = cupy.zeros_like(dm_sparse)

        for cderi, cderi_sparse in dfobj.loop(blksize=blksize, unpack=with_k):
            # leading dimension is 1
            if with_j:
                rhoj = 2.0*dm_sparse.dot(cderi_sparse)
                vj_packed += cupy.dot(rhoj, cderi_sparse.T)
            if with_k:
                rhok = contract('Lij,jk->Lki', cderi, occ_coeff)
                #vk[0] += 2.0 * contract('Lki,Lkj->ij', rhok, rhok)
                cublas.syrk('T', rhok.reshape([-1,nao]), out=vk[0], alpha=2.0, beta=1.0, lower=True)
        if with_j:
            vj[:,rows,cols] = vj_packed
            vj[:,cols,rows] = vj_packed
        if with_k:
            vk[0][numpy.diag_indices(nao)] *= 0.5
            transpose_sum(vk)
    # CP-HF K matrix
    elif hasattr(dms_tag, 'mo1'):
        if with_j:
            vj_sparse = cupy.zeros_like(dm_sparse)
        mo1 = dms_tag.mo1[:,ao_idx,:]
        nocc = mo1.shape[2]
        # 2.0 due to rhok and rhok1, put it here for symmetry
        occ_coeff = dms_tag.occ_coeff[ao_idx,:] * 2.0
        blksize = dfobj.get_blksize(extra=2*nao*nocc)
        for cderi, cderi_sparse in dfobj.loop(blksize=blksize, unpack=with_k):
            if with_j:
                #vj += get_j(cderi_sparse)
                rhoj = 2.0*dm_sparse.dot(cderi_sparse)
                vj_sparse += cupy.dot(rhoj, cderi_sparse.T)
            if with_k:
                rhok = contract('Lij,jk->Lki', cderi, occ_coeff)
                for i in range(mo1.shape[0]):
                    rhok1 = contract('Lij,jk->Lki', cderi, mo1[i])
                    #vk[i] += contract('Lki,Lkj->ij', rhok, rhok1)
                    contract('Lki,Lkj->ij', rhok, rhok1, alpha=1.0, beta=1.0, out=vk[i])
        occ_coeff = rhok1 = rhok = mo1 = None
        if with_j:
            vj[:,rows,cols] = vj_sparse
            vj[:,cols,rows] = vj_sparse
        if with_k:
            #vk = vk + vk.transpose(0,2,1)
            transpose_sum(vk)
    # general K matrix with density matrix
    else:
        if with_j:
            vj_sparse = cupy.zeros_like(dm_sparse)
        blksize = dfobj.get_blksize()
        for cderi, cderi_sparse in dfobj.loop(blksize=blksize, unpack=with_k):
            if with_j:
                rhoj = 2.0*dm_sparse.dot(cderi_sparse)
                vj_sparse += cupy.dot(rhoj, cderi_sparse.T)
            if with_k:
                for k in range(nset):
                    rhok = contract('Lij,jk->Lki', cderi, dms[k])
                    vk[k] += contract('Lki,Lkj->ij', cderi, rhok)
        if with_j:
            vj[:,rows,cols] = vj_sparse
            vj[:,cols,rows] = vj_sparse
        rhok = None

    rev_ao_idx = dfobj.intopt.rev_ao_idx
    if with_j:
        vj = take_last2d(vj, rev_ao_idx)
        vj = vj.reshape(out_shape)
    if with_k:
        vk = take_last2d(vk, rev_ao_idx)
        vk = vk.reshape(out_shape)
    t1 = log.timer_debug1('vj and vk', *t1)
    if out_cupy:
        return vj, vk
    else:
        if vj is not None:
            vj = vj.get()
        if vk is not None:
            vk = vk.get()
        return vj, vk

def _get_jk(dfobj, dm, hermi=1, with_j=True, with_k=True,
            direct_scf_tol=getattr(__config__, 'scf_hf_SCF_direct_scf_tol', 1e-13),
            omega=None):
    if omega is None:
        return get_jk(dfobj, dm, hermi, with_j, with_k, direct_scf_tol)

    # A temporary treatment for RSH-DF integrals
    key = '%.6f' % omega
    if key in dfobj._rsh_df:
        rsh_df = dfobj._rsh_df[key]
    else:
        rsh_df = dfobj._rsh_df[key] = copy.copy(dfobj).reset()
        logger.info(dfobj, 'Create RSH-DF object %s for omega=%s', rsh_df, omega)

    with rsh_df.mol.with_range_coulomb(omega):
        return get_jk(rsh_df, dm, hermi, with_j, with_k, direct_scf_tol)

def get_j(dfobj, dm, hermi=1, direct_scf_tol=1e-13):
    intopt = getattr(dfobj, 'intopt', None)
    if intopt is None:
        dfobj.build(direct_scf_tol=direct_scf_tol)
        intopt = dfobj.intopt
    j2c = dfobj.j2c
    rhoj = int3c2e.get_j_int3c2e_pass1(intopt, dm)
    if dfobj.cd_low.tag == 'eig':
        rhoj = cupy.linalg.lstsq(j2c, rhoj)
    else:
        rhoj = cupy.linalg.solve(j2c, rhoj)

    rhoj *= 2.0
    vj = int3c2e.get_j_int3c2e_pass2(intopt, rhoj)
    return vj

density_fit = _density_fit
