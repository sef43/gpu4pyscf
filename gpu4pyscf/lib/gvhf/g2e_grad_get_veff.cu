template<int NROOTS, int GOUTSIZE>
__global__
static void GINTrestricted_get_veff_kernel(GINTEnvVars envs,
                                           JKMatrix jk,
                                           BasisProdOffsets offsets) {

  int ntasks_ij = offsets.ntasks_ij;
  int ntasks_kl = offsets.ntasks_kl;
  int task_ij = blockIdx.x * blockDim.x + threadIdx.x;
  int task_kl = blockIdx.y * blockDim.y + threadIdx.y;
  int task_id = threadIdx.y * THREADSX + threadIdx.x;

  if (task_ij >= ntasks_ij || task_kl >= ntasks_kl) {
    return;
  }

  int * ao_loc = c_bpcache.ao_loc;
  int bas_ij = offsets.bas_ij + task_ij;
  int bas_kl = offsets.bas_kl + task_kl;

  int nao = jk.nao;

  int i, j, k, l, f;
  double d_ij, d_kl, d_ik, d_il, d_jk, d_jl;

  double norm = envs.fac;

  int nprim_ij = envs.nprim_ij;
  int nprim_kl = envs.nprim_kl;
  int prim_ij = offsets.primitive_ij + task_ij * nprim_ij;
  int prim_kl = offsets.primitive_kl + task_kl * nprim_kl;
  int * bas_pair2bra = c_bpcache.bas_pair2bra;
  int * bas_pair2ket = c_bpcache.bas_pair2ket;

  int ish = bas_pair2bra[bas_ij];
  int jsh = bas_pair2ket[bas_ij];
  int ksh = bas_pair2bra[bas_kl];
  int lsh = bas_pair2ket[bas_kl];

  int i0 = ao_loc[ish];
  int i1 = ao_loc[ish + 1];
  int j0 = ao_loc[jsh];
  int j1 = ao_loc[jsh + 1];
  int k0 = ao_loc[ksh];
  int k1 = ao_loc[ksh + 1];
  int l0 = ao_loc[lsh];
  int l1 = ao_loc[lsh + 1];

  double * vj = jk.vj;
  double * dm = jk.dm;
  double s_ix, s_iy, s_iz, s_jx, s_jy, s_jz;

  double uw[NROOTS * 2];
  double g[GOUTSIZE];

  __shared__ double shell_contracted[6 * THREADS];

  double* __restrict__ a12 = c_bpcache.a12;
  double* __restrict__ x12 = c_bpcache.x12;
  double* __restrict__ y12 = c_bpcache.y12;
  double* __restrict__ z12 = c_bpcache.z12;
  double * __restrict__ i_exponent = c_bpcache.a1;
  double * __restrict__ j_exponent = c_bpcache.a2;

  int ij, kl;
  int as_ish, as_jsh, as_ksh, as_lsh;
  if (envs.ibase) {
    as_ish = ish;
    as_jsh = jsh;
  } else {
    as_ish = jsh;
    as_jsh = ish;
  }
  if (envs.kbase) {
    as_ksh = ksh;
    as_lsh = lsh;
  } else {
    as_ksh = lsh;
    as_lsh = ksh;
  }

  for (ij = prim_ij; ij < prim_ij + nprim_ij; ++ij) {
    double ai = i_exponent[ij];
    double aj = j_exponent[ij];
    double aij = a12[ij];
    double xij = x12[ij];
    double yij = y12[ij];
    double zij = z12[ij];
    for (kl = prim_kl; kl < prim_kl + nprim_kl; ++kl) {
      double akl = a12[kl];
      double xkl = x12[kl];
      double ykl = y12[kl];
      double zkl = z12[kl];
      double xijxkl = xij - xkl;
      double yijykl = yij - ykl;
      double zijzkl = zij - zkl;
      double aijkl = aij + akl;
      double a1 = aij * akl;
      double a0 = a1 / aijkl;
      double x = a0 * (xijxkl * xijxkl + yijykl * yijykl + zijzkl * zijzkl);

      if constexpr(NROOTS==3) {
        GINTrys_root3(x, uw);
      } else if constexpr(NROOTS==4) {
        GINTrys_root4(x, uw);
      } else if constexpr(NROOTS==5) {
        GINTrys_root5(x, uw);
      } else if constexpr(NROOTS==6) {
        GINTrys_root6(x, uw);
      } else if constexpr(NROOTS==7) {
        GINTrys_root7(x, uw);
      } else if constexpr(NROOTS==8) {
        GINTrys_root8(x, uw);
      } else if constexpr(NROOTS==9) {
        GINTrys_root9(x, uw);
      }

      GINTg0_2e_2d4d<NROOTS>(envs, g, uw, norm,
                             as_ish, as_jsh, as_ksh, as_lsh, ij, kl);

      for (f = 0, l = l0; l < l1; ++l) {
        for (k = k0; k < k1; ++k) {
          d_kl = dm[k + nao * l];
          for (j = j0; j < j1; ++j) {
            d_jl = dm[j + nao * l];
            d_jk = dm[j + nao * k];
            for (i = i0; i < i1; ++i, ++f) {
              d_ij = dm[i + nao * j];
              d_ik = dm[i + nao * k];
              d_il = dm[i + nao * l];

              GINTgout2e_nabla1i_per_function<NROOTS>(envs, g, ai, aj, f,
                                                      &s_ix, &s_iy, &s_iz,
                                                      &s_jx, &s_jy,
                                                      &s_jz);

              double j_dm_component = 2 * d_kl * d_ij;
              double i_dm_component =
                  j_dm_component - 0.5 * (d_ik * d_jl + d_il * d_jk);
              j_dm_component -= 0.5 * (d_jk * d_il + d_jl * d_ik);

              shell_contracted[          task_id] += s_ix * i_dm_component;
              shell_contracted[  THREADS+task_id] += s_iy * i_dm_component;
              shell_contracted[2*THREADS+task_id] += s_iz * i_dm_component;
              shell_contracted[3*THREADS+task_id] += s_jx * j_dm_component;
              shell_contracted[4*THREADS+task_id] += s_jy * j_dm_component;
              shell_contracted[5*THREADS+task_id] += s_jz * j_dm_component;
            }
          }
        }
      }
    }
  }

  atomicAdd(vj+ish*3  , shell_contracted[          task_id]);
  atomicAdd(vj+ish*3+1, shell_contracted[  THREADS+task_id]);
  atomicAdd(vj+ish*3+2, shell_contracted[2*THREADS+task_id]);
  atomicAdd(vj+jsh*3  , shell_contracted[3*THREADS+task_id]);
  atomicAdd(vj+jsh*3+1, shell_contracted[4*THREADS+task_id]);
  atomicAdd(vj+jsh*3+2, shell_contracted[5*THREADS+task_id]);
}


__global__
static void
GINTint2e_jk_kernel_nabla1i_0000(GINTEnvVars envs, JKMatrix jk, BasisProdOffsets offsets) {
  int ntasks_ij = offsets.ntasks_ij;
  int ntasks_kl = offsets.ntasks_kl;
  int task_ij = blockIdx.x * blockDim.x + threadIdx.x;
  int task_kl = blockIdx.y * blockDim.y + threadIdx.y;
  if (task_ij >= ntasks_ij || task_kl >= ntasks_kl) {
    return;
  }
  int bas_ij = offsets.bas_ij + task_ij;
  int bas_kl = offsets.bas_kl + task_kl;
  double norm = envs.fac;

  int nprim_ij = envs.nprim_ij;
  int nprim_kl = envs.nprim_kl;
  int prim_ij = offsets.primitive_ij + task_ij * nprim_ij;
  int prim_kl = offsets.primitive_kl + task_kl * nprim_kl;
  int * bas_pair2bra = c_bpcache.bas_pair2bra;
  int * bas_pair2ket = c_bpcache.bas_pair2ket;
  int * ao_loc = c_bpcache.ao_loc;
  int ish = bas_pair2bra[bas_ij];
  int jsh = bas_pair2ket[bas_ij];
  int ksh = bas_pair2bra[bas_kl];
  int lsh = bas_pair2ket[bas_kl];
  int i = ao_loc[ish];
  int j = ao_loc[jsh];
  int k = ao_loc[ksh];
  int l = ao_loc[lsh];

  int nbas = c_bpcache.nbas;
  double * __restrict__ bas_x = c_bpcache.bas_coords;
  double * __restrict__ bas_y = bas_x + nbas;
  double * __restrict__ bas_z = bas_y + nbas;

  double xi = bas_x[ish];
  double yi = bas_y[ish];
  double zi = bas_z[ish];

  double xj = bas_x[jsh];
  double yj = bas_y[jsh];
  double zj = bas_z[jsh];

  double * __restrict__ a12 = c_bpcache.a12;
  double * __restrict__ e12 = c_bpcache.e12;
  double * __restrict__ x12 = c_bpcache.x12;
  double * __restrict__ y12 = c_bpcache.y12;
  double * __restrict__ z12 = c_bpcache.z12;
  double * __restrict__ i_exponent = c_bpcache.a1;
  double * __restrict__ j_exponent = c_bpcache.a2;

  int ij, kl;
  double gout0 = 0, gout0_prime = 0;
  double gout1 = 0, gout1_prime = 0;
  double gout2 = 0, gout2_prime = 0;

  for (ij = prim_ij; ij < prim_ij + nprim_ij; ++ij) {
    double ai = i_exponent[ij];
    double aj = j_exponent[ij];
    double aij = a12[ij];
    double eij = e12[ij];
    double xij = x12[ij];
    double yij = y12[ij];
    double zij = z12[ij];
    for (kl = prim_kl; kl < prim_kl + nprim_kl; ++kl) {
      double akl = a12[kl];
      double ekl = e12[kl];
      double xkl = x12[kl];
      double ykl = y12[kl];
      double zkl = z12[kl];
      double xijxkl = xij - xkl;
      double yijykl = yij - ykl;
      double zijzkl = zij - zkl;
      double aijkl = aij + akl;
      double a1 = aij * akl;
      double a0 = a1 / aijkl;
      double x = a0 * (xijxkl * xijxkl + yijykl * yijykl + zijzkl * zijzkl);
      double fac = norm * eij * ekl / (sqrt(aijkl) * a1);

      double root0, weight0;

      if (x < 3.e-7) {
        root0 = 0.5;
        weight0 = 1.;
      } else {
        double tt = sqrt(x);
        double fmt0 = SQRTPIE4 / tt * erf(tt);
        weight0 = fmt0;
        double e = exp(-x);
        double b = .5 / x;
        double fmt1 = b * (fmt0 - e);
        root0 = fmt1 / (fmt0 - fmt1);
      }

      double u2 = a0 * root0;
      double tmp2 = akl * u2 / (u2 * aijkl + a1);
      double c00x = xij - xi - tmp2 * xijxkl;
      double c00y = yij - yi - tmp2 * yijykl;
      double c00z = zij - zi - tmp2 * zijzkl;

      double c00x_prime = xij - xj - tmp2 * xijxkl;
      double c00y_prime = yij - yj - tmp2 * yijykl;
      double c00z_prime = zij - zj - tmp2 * zijzkl;

      double g_0 = 1;
      double g_1 = c00x;
      double g_2 = 1;
      double g_3 = c00y;
      double g_4 = fac * weight0;
      double g_5 = g_4 * c00z;
      double g_6 = 2.0 * ai;

      double g_1_prime = c00x_prime;
      double g_3_prime = c00y_prime;
      double g_5_prime = g_4 * c00z_prime;
      double g_6_prime = 2.0 * aj;

      gout0 += g_1 * g_2 * g_4 * g_6;
      gout1 += g_0 * g_3 * g_4 * g_6;
      gout2 += g_0 * g_2 * g_5 * g_6;

      gout0_prime += g_1_prime * g_2 * g_4 * g_6_prime;
      gout1_prime += g_0 * g_3_prime * g_4 * g_6_prime;
      gout2_prime += g_0 * g_2 * g_5_prime * g_6_prime;
    }
  }

  int nao = jk.nao;
  double * __restrict__ dm = jk.dm;
  double * __restrict__ vj = jk.vj;

  double d_ik = dm[i + nao * k];
  double d_il = dm[i + nao * l];
  double d_jl = dm[j + nao * l];
  double d_jk = dm[j + nao * k];

  double j_dm_component = 2 * dm[k + nao * l] * dm[i + nao * j];
  double i_dm_component =
      j_dm_component - 0.5 * (d_ik * d_jl + d_il * d_jk);
  j_dm_component -= 0.5 * (d_jk * d_il + d_jl * d_ik);

  atomicAdd(vj+ish*3  , gout0       * i_dm_component);
  atomicAdd(vj+ish*3+1, gout1       * i_dm_component);
  atomicAdd(vj+ish*3+2, gout2       * i_dm_component);
  atomicAdd(vj+jsh*3  , gout0_prime * j_dm_component);
  atomicAdd(vj+jsh*3+1, gout1_prime * j_dm_component);
  atomicAdd(vj+jsh*3+2, gout2_prime * j_dm_component);
}