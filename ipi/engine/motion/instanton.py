"""
Contains classes for instanton  calculations.

Algorithms implemented by Yair Litman and Mariana Rossi, 2017
"""

# This file is part of i-PI.
# i-PI Copyright (C) 2014-2015 i-PI developers
# See the "licenses" directory for full license information.


import numpy as np
import time
import sys

from ipi.engine.beads import Beads
from ipi.engine.normalmodes import NormalModes
from ipi.engine.motion import Motion
from ipi.utils.depend import dstrip, dobject
from ipi.utils.softexit import softexit
from ipi.utils.messages import verbosity, info
from ipi.utils import units, nmtransform
from ipi.utils.mintools import nichols, Powell, L_BFGS
from ipi.utils.instools import (
    banded_hessian,
    invmul_banded,
    red2comp,
    get_imvector,
    print_instanton_geo,
)
from ipi.utils.instools import print_instanton_hess, diag_banded, ms_pathway
from ipi.utils.hesstools import get_hessian, clean_hessian, get_dynmat

__all__ = ["InstantonMotion"]


# ALBERTO:
# - resolve hessian problem, implement sparse and dense spring hessian
# add boolean option hessian mode:
# add boolean option sparse in:
#    get_hessian
#    update_hessian
#    print_hessian
#    red2comp
# test ch4hcbe with dense hessian
# test 1D double well with Ohmic
# test 1D double well with Ohmic +SD

# - code spline and integration to obtain g
# test 2x*1D double well with Ohmic +SD


class InstantonMotion(Motion):
    """Instanton motion class.

    Attributes:
        mode: minimization algorithm to use
        biggest_step: max allowed step size
        old_force: force on previous step
        hessian:

        mode= type of instanton calculation
        tolerances:
            energy: change in energy tolerance for ending minimization
            force: force/change in force tolerance foe ending minimization
            position: change in position tolerance for ending minimization}
        biggest_step: The maximum step size during the optimization.
        old_pos: The previous step positions during the optimization.
        old_pot: The previous step potential energy during the optimization
        old_force:  The previous step force during the optimization
        opt: The geometry optimization algorithm to be used
        discretization: Allows for non uniform time discretization
        alt_out: (Alternative output) Prints different formatting of outputs for geometry, hessian and bead potential energies.
        All quantities are also accessible from typical i-pi output infrastructure. Default to 1, which prints
        every step. -1 will suppress the output (except the last one). Any other positive number will set the frequency (in steps) with
        which the quantities are written to file.
        prefix: Prefix of the output files.
        delta: Initial stretch amplitude.
        hessian_init: Boolean which decides whether the initial hessian is going to be computed.
        hessian: Stored  Hessian matrix
        hessian_update: The way to update the hessian after each movement
        hessian_asr: Removes the zero frequency vibrational modes depending on the symmerty of the system.
        glist_lbfgs: List of previous gradients (g_n+1 - g_n) for L-BFGS. Number of entries = corrections_lbfgs
        qlist_lbfgs: List of previous positions (x_n+1 - x_n) for L-BFGS. Number of entries = corrections_lbfgs
        scale_lbfgs: Scale choice for the initial hessian.
        corrections_lbfgs: Number of corrections to be stored for L-BFGS
        ls_options: Options for line search methods.
        hessian_final:  Boolean which decides whether the hessian after the optimization will be computed.
        energy_shift: zero of energy (usually it corresponds to reactant state)
    """

    def __init__(
        self,
        fixcom=False,
        fixatoms=None,
        mode="None",
        tolerances={"energy": 1e-5, "force": 1e-4, "position": 1e-3},
        biggest_step=0.3,
        old_pos=np.zeros(0, float),
        old_pot=np.zeros(0, float),
        old_force=np.zeros(0, float),
        opt="None",
        max_e=0.0,
        max_ms=0.0,
        discretization=np.zeros(0, float),
        alt_out=1,
        prefix="instanton",
        delta=np.zeros(0, float),
        hessian_init=None,
        hessian=np.eye(0, 0, 0, float),
        hessian_update=None,
        hessian_asr=None,
        qlist_lbfgs=np.zeros(0, float),
        glist_lbfgs=np.zeros(0, float),
        scale_lbfgs=1,
        corrections_lbfgs=5,
        ls_options={"tolerance": 1e-1, "iter": 100},
        old_direction=np.zeros(0, float),
        hessian_final="False",
        energy_shift=np.zeros(0, float),
        friction=False,
        z_friction=np.zeros(0, float),
    ):
        """Initialises InstantonMotion."""

        super(InstantonMotion, self).__init__(fixcom=fixcom, fixatoms=fixatoms)

        self.options = {}  # Optimization options
        self.optarrays = {}  # Optimization arrays

        # Optimization mode
        self.options["mode"] = mode

        # Generic optimization
        # self.big_step = biggest_step
        # self.tolerances = tolerances

        self.options["tolerances"] = tolerances
        self.options["save"] = alt_out
        self.options["prefix"] = prefix
        self.options["hessian_final"] = hessian_final

        self.options["max_e"] = max_e
        self.options["max_ms"] = max_ms
        self.options["discretization"] = discretization
        self.options["friction"] = friction
        self.options["z_friction"] = z_friction
        self.optarrays["big_step"] = biggest_step
        self.optarrays["energy_shift"] = energy_shift
        self.optarrays["delta"] = delta
        self.optarrays["old_x"] = old_pos
        self.optarrays["old_u"] = old_pot
        self.optarrays["old_f"] = old_force

        # We set the default optimization algorithm depending on the mode.
        if mode == "rate":
            if opt == "None":
                opt = "nichols"
            self.options["opt"] = opt

        elif mode == "splitting":
            if opt == "None":
                opt = "lbfgs"
            self.options["opt"] = opt

        if (
            self.options["opt"] == "nichols"
            or self.options["opt"] == "NR"
            or self.options["opt"] == "lanczos"
        ):

            self.options["hessian_update"] = hessian_update
            self.options["hessian_asr"] = hessian_asr
            self.options["hessian_init"] = hessian_init
            self.optarrays["hessian"] = hessian

            if self.options["opt"] == "nichols":
                self.optimizer = NicholsOptimizer()
            elif self.options["opt"] == "NR":
                self.optimizer = NROptimizer()
            else:
                self.optimizer = LanczosOptimizer()

        elif self.options["opt"] == "lbfgs":
            self.optimizer = LBFGSOptimizer()
            self.optarrays["hessian"] = hessian  # Only for initial (to spread) or final
            self.options["hessian_asr"] = hessian_asr

            self.options["corrections"] = corrections_lbfgs
            self.options["scale"] = scale_lbfgs
            self.options["ls_options"] = ls_options

            self.optarrays["qlist"] = qlist_lbfgs
            self.optarrays["glist"] = glist_lbfgs
            self.optarrays["d"] = old_direction

        if self.options["opt"] == "NR":
            info(
                "Note that we need scipy to use NR. If storage and diagonalization of the full hessian is not a "
                "problem use nichols even though it may not be as efficient.",
                verbosity.low,
            )

    def bind(self, ens, beads, nm, cell, bforce, prng, omaker):
        """Binds beads, cell, bforce and prng to InstantonMotion

        Args:
        beads: The beads object from whcih the bead positions are taken.
        nm: A normal modes object used to do the normal modes transformation.
        cell: The cell object from which the system box is taken.
        bforce: The forcefield object from which the force and virial are taken.
        prng: The random number generator object which controls random number generation.
        """

        super(InstantonMotion, self).bind(ens, beads, nm, cell, bforce, prng, omaker)

        # Redefine normal modes
        self.nm = NormalModes()
        if self.options["mode"] == "rate":
            self.nm.bind(
                self.ensemble, self, Beads(self.beads.natoms, self.beads.nbeads * 2)
            )
        elif self.options["mode"] == "splitting":
            self.nm.bind(
                self.ensemble, self, Beads(self.beads.natoms, self.beads.nbeads)
            )

        # Binds optimizer

        self.optimizer.bind(self)

    def step(self, step=None):
        self.optimizer.step(step)


class Fix(object):
    """Class that applies a fixatoms type constrain"""

    def __init__(self, fixatoms, beads, nbeads=None):

        self.natoms = beads.natoms
        if nbeads is None:
            self.nbeads = beads.nbeads
        else:
            self.nbeads = nbeads

        self.fixatoms = fixatoms

        self.mask0 = np.delete(np.arange(self.natoms), self.fixatoms)

        mask1 = np.ones(3 * self.natoms, dtype=bool)
        for i in range(3):
            mask1[3 * self.fixatoms + i] = False
        self.mask1 = np.arange(3 * self.natoms)[mask1]

        mask2 = np.tile(mask1, self.nbeads)
        self.mask2 = np.arange(3 * self.natoms * self.nbeads)[mask2]

        self.fixbeads = Beads(beads.natoms - len(fixatoms), beads.nbeads)
        self.fixbeads.q[:] = self.get_active_vector(beads.copy().q, 1)
        self.fixbeads.m[:] = self.get_active_vector(beads.copy().m, 0)
        self.fixbeads.names[:] = self.get_active_vector(beads.copy().names, 0)

    def get_mask(self, m):

        if m == 0:
            return self.mask0
        elif m == 1:
            return self.mask1
        elif m == 2:
            return self.mask2
        else:
            raise ValueError("Mask number not valid")

    def get_active_array(self, arrays):
        """Functions that gets the subarray corresponding to the active degrees-of-freedom of the
        full dimensional array"""

        activearrays = {}
        for key in arrays:

            if (
                key == "old_u"
                or key == "big_step"
                or key == "delta"
                or key == "energy_shift"
                or key == "initial_hessian"
            ):
                t = -1
            elif key == "old_x" or key == "old_f" or key == "d":
                t = 1
            elif key == "hessian" or key == "eta":
                t = 2
            elif key == "qlist" or key == "glist":
                t = 3
            else:
                raise ValueError(
                    "@get_active_array: There is an array that we can't recognize"
                )

            activearrays[key] = self.get_active_vector(arrays[key], t)

        return activearrays

    def get_full_vector(self, vector, t):
        """Set 0 the degrees of freedom (dof) corresponding to the fix atoms
        IN:
            fixatoms   indexes of the fixed atoms
            vector     vector to be reduced
            t          type of array:
                type=-1 : do nothing
                type=0 : names (natoms )
                type=1 : pos , force or m3 (nbeads,dof)
                type=2 : hessian (dof, nbeads*dof)
                type=3 : qlist or glist (corrections, nbeads*dof)
        OUT:
            clean_vector  reduced vector
        """
        if len(self.fixatoms) == 0 or t == -1:
            return vector

        if t == 1:

            full_vector = np.zeros((self.nbeads, 3 * self.natoms))
            full_vector[:, self.get_mask(1)] = vector

            return full_vector

        elif t == 2:

            full_vector = np.zeros((3 * self.natoms, 3 * self.natoms * self.nbeads))

            ii = 0
            for i in self.get_mask(1):
                full_vector[i, self.get_mask(2)] = vector[ii]
                ii += 1

            return full_vector

        elif t == 3:

            full_vector = np.zeros((vector.shape[0], 3 * self.natoms * self.nbeads))
            full_vector[:, self.fix.get_mask(2)] = vector

            return full_vector

        else:

            raise ValueError("@apply_fix_atoms: type number is not valid")

    def get_active_vector(self, vector, t):
        """Delete the degrees of freedom (dof) corresponding to the fix atoms
        IN:
            fixatoms   indexes of the fixed atoms
            vector     vector to be reduced
            t          type of array:
                type=-1 : do nothing
                type=0 : names (natoms )
                type=1 : pos , force or m3 (nbeads,dof)
                type=2 : hessian (dof, nbeads*dof)
                type=3 : qlist or glist (corrections, nbeads*dof)
        OUT:
            clean_vector  reduced vector
        """
        if len(self.fixatoms) == 0 or t == -1:
            return vector
        if t == 0:
            return vector[self.mask0]
        elif t == 1:
            return vector[:, self.mask1]
        elif t == 2:
            aux = vector[self.mask1]
            return aux[:, self.mask2]
        elif t == 3:
            return vector[:, self.mask2]
        else:
            raise ValueError("@apply_fix_atoms: type number is not valid")


class PesMapper(object):

    """Creation of the multi-dimennsional function to compute the physical potential and forces

    Attributes:
        dbeads:  copy of the bead object
        dcell:   copy of the cell object
        dforces: copy of the forces object
    """

    def __init__(self):
        self.fcount = 0
        pass

    def bind(self, mapper):

        self.dbeads = mapper.beads.copy()
        self.dcell = mapper.cell.copy()
        self.dforces = mapper.forces.copy(self.dbeads, self.dcell)
        self.nm = mapper.nm
        self.fix = mapper.fix
        self.coef = mapper.coef
        self.friction = mapper.friction
        if self.friction:
            self.z_friction = mapper.z_friction

        max_ms = mapper.options["max_ms"]
        max_e = mapper.options["max_e"]

        if max_ms > 0 or max_e > 0:
            self.spline = True

            if max_ms > 0:
                self.max_ms = max_ms
            else:
                self.max_ms = 1000000
            if max_e > 0:
                self.max_e = max_e
            else:
                self.max_e = 10000000
        else:
            self.spline = False

    def set_pos(self, x):
        """Set the positions """
        self.dbeads.q = x

    def save(self, e, g, eta=None):
        """ Stores potential and forces in this class for convenience """
        self.pot = e
        self.f = -g
        self.eta = eta

    def compute_friction_terms(self):
        """ Computes friction component of the energy and gradient """

        z = self.z_friction / self.z_friction[1]
        s = self.eta

        # g   = obtain_g(self.dbeads, self.eta) #Implement This
        # s   = obtain_s(self.dbeads, self.eta) #Implement THIS
        gq = self.dbeads.q  # For now
        gq = np.concatenate((gq, np.flipud(gq)), axis=0)
        gq_k = self.nm.transform.b2nm(gq)

        z_k = np.multiply(self.nm.get_omegak(), self.z_friction).reshape(-1, 1)

        e_f = (0.5 * z_k * gq_k ** 2).sum()
        e = np.zeros(self.dbeads.nbeads)
        e[0] = e_f  # We can't do a meaningfull bead assigment

        f = self.nm.transform.nm2b(z_k * gq_k)[: self.dbeads.nbeads, :]

        g = np.zeros(f.shape)
        for i in range(self.dbeads.nbeads):
            g[i, :] = np.dot(s[i], f[i])
        return e, g

    def __call__(self, x, new_disc=True):
        """ Computes energy and gradient for optimization step"""
        self.fcount += 1
        full_q = x.copy()
        full_mspath = ms_pathway(full_q, self.dbeads.m3)

        if self.spline:
            try:
                from scipy.interpolate import interp1d
            except ImportError:
                softexit.trigger("Scipy required to use  max_ms >0")

            indexes = list()
            indexes.append(0)
            old_index = 0
            for i in range(1, self.dbeads.nbeads):
                if (full_mspath[i] - full_mspath[old_index] > self.max_ms) or (
                    np.absolute(self.pot[i] - self.pot[old_index]) > self.max_e
                ):
                    indexes.append(i)
                    old_index = i
            if self.dbeads.nbeads - 1 not in indexes:
                indexes.append(self.dbeads.nbeads - 1)
            info(
                "The reduced RP for this step has {} beads.".format(len(indexes)),
                verbosity.low,
            )
            if len(indexes) <= 2:
                softexit.trigger(
                    "Too few beads fulfill criteria. Please reduce max_ms or max_e"
                )
        else:
            indexes = np.arange(self.dbeads.nbeads)

        # Create reduced bead and force objects
        reduced_b = Beads(self.dbeads.natoms, len(indexes))
        reduced_b.q[:] = full_q[indexes]
        reduced_b.m[:] = self.dbeads.m
        reduced_b.names[:] = self.dbeads.names

        reduced_cell = self.dcell.copy()
        reduced_forces = self.dforces.copy(reduced_b, reduced_cell)

        # Evaluate energy and forces (and maybe friction)
        rpots = reduced_forces.pots  # reduced energy
        rforces = reduced_forces.f  # reduced gradient

        if self.spline:
            red_mspath = full_mspath[indexes]
            spline = interp1d(red_mspath, rpots.T, kind="cubic")
            full_pot = spline(full_mspath).T
            spline = interp1d(red_mspath, rforces.T, kind="cubic")
            full_forces = spline(full_mspath).T
        else:
            full_pot = rpots
            full_forces = rforces

        # This forces the update of the forces
        self.dbeads.q[:] = x[:]
        self.dforces.transfer_forces_manual([full_q], [full_pot], [full_forces])

        if self.friction:
            # ALBERTO: The following has to be joined to the json implementation for the
            # communication of the extras strings
            print("\n ALBERTO2 get friction from forces object\n")
            # print("\n pick only the tensor corresponding to the  RP frequencies")
            red_eta = np.zeros(
                (self.dbeads.nbeads, self.dbeads.natoms * 3, self.dbeads.natoms * 3)
            )

            # Interpolate if necessary to get full pot and forces
            if self.spline:
                red_mspath = full_mspath[indexes]
                spline = interp1d(red_mspath, red_eta.T, kind="cubic")
                full_eta = spline(full_mspath).T
            else:
                full_eta = red_eta

        else:
            full_eta = None

        self.save(full_pot, -full_forces, full_eta)
        return self.evaluate()

    def evaluate(self):
        """Evaluate the energy and forces including:
        - non uniform discretization
        - friction term (if required)
        """

        e = self.pot.copy()
        g = -self.f.copy()

        if self.friction:
            e_friction, g_friction = self.compute_friction_terms()
            e += e_friction
            g += g_friction

        e = e * (self.coef[1:] + self.coef[:-1]) / 2
        g = g * (self.coef[1:] + self.coef[:-1]) / 2

        return e, g


class SpringMapper(object):
    """Creation of the multi-dimensional function to compute full or half ring polymer potential
    and forces.
    """

    def __init__(self):

        self.pot = None
        self.f = None
        pass

    def bind(self, mapper):

        self.temp = mapper.temp
        self.fix = mapper.fix
        self.coef = mapper.coef
        self.dbeads = mapper.beads.copy()
        self.nm = mapper.nm

        self.omega2 = self.nm.omegan2

        # Computes the spring hessian if the optimization modes requires it
        if (
            mapper.options["opt"] == "nichols"
            or mapper.options["opt"] == "NR"
            or mapper.options["opt"] == "lanczos"
        ):
            self.h = self.spring_hessian(
                natoms=self.fix.fixbeads.natoms,
                nbeads=self.fix.fixbeads.nbeads,
                m3=self.fix.fixbeads.m3[0],
                omega2=self.omega2,
                coef=self.coef,
            )

    def save(self, e, g):
        """ Stores potential and forces in this class for convenience """
        self.pot = e
        self.f = -g

    def __call__(self, x, ret=True, new_disc=True):
        """Computes spring energy and gradient for instanton optimization step"""

        if new_disc:
            coef = self.coef
        elif new_disc == "one":
            coef = np.ones(self.coef.shape)
        else:
            coef = new_disc.reshape(self.coef.shape)

        if x.shape[0] == 1:  # only one bead
            self.dbeads.q = x
            e = 0.0
            g = np.zeros(x.shape[1])
            self.save(e, g)

        else:
            self.dbeads.q = x
            e = 0.00
            g = np.zeros(self.dbeads.q.shape, float)

            # OLD reference
            # for i in range(self.dbeads.nbeads - 1):
            #    dq = self.dbeads.q[i + 1, :] - self.dbeads.q[i, :]
            #    e += self.omega2 * 0.5 * np.dot(self.dbeads.m3[0] * dq, dq)
            # for i in range(0, self.dbeads.nbeads - 1):
            #    g[i, :] += self.dbeads.m3[i, :] * self.omega2 * (self.dbeads.q[i, :] - self.dbeads.q[i + 1, :])
            # for i in range(1, self.dbeads.nbeads):
            #    g[i, :] += self.dbeads.m3[i, :] * self.omega2 * (self.dbeads.q[i, :] - self.dbeads.q[i - 1, :])

            # With new discretization
            for i in range(self.dbeads.nbeads - 1):
                dq = (self.dbeads.q[i + 1, :] - self.dbeads.q[i, :]) / np.sqrt(
                    coef[i + 1]
                )  # coef[0] and coef[-1] do not enter
                e += self.omega2 * 0.5 * np.dot(self.dbeads.m3[0] * dq, dq)
            for i in range(0, self.dbeads.nbeads - 1):
                g[i, :] += (
                    self.dbeads.m3[i, :]
                    * self.omega2
                    * (
                        self.dbeads.q[i, :] / coef[i + 1]
                        - self.dbeads.q[i + 1, :] / coef[i + 1]
                    )
                )
            for i in range(1, self.dbeads.nbeads):
                g[i, :] += (
                    self.dbeads.m3[i, :]
                    * self.omega2
                    * (
                        self.dbeads.q[i, :] / coef[i]
                        - self.dbeads.q[i - 1, :] / coef[i]
                    )
                )

            self.save(e, g)

        if ret:
            return e, g

    @staticmethod
    def spring_hessian(natoms, nbeads, m3, omega2, mode="half", coef=None):
        """Compute the 'spring hessian'

        OUT    h       = hessian with only the spring terms ('spring hessian')
        """
        if coef is None:
            coef = np.ones(nbeads + 1).reshape(-1, 1)

        # Check size of discretization:
        if coef.size != nbeads + 1:
            print("@spring_hessian: discretization size error")
            sys.exit()

        info(" @spring_hessian", verbosity.high)
        ii = natoms * 3
        h = np.zeros([ii * nbeads, ii * nbeads])

        if nbeads == 1:
            return h

        # Diagonal
        h_sp = m3 * omega2
        diag1 = np.diag(h_sp)
        # diag2 = np.diag(2.0 * h_sp)

        if mode == "half":
            i = 0
            h[i * ii : (i + 1) * ii, i * ii : (i + 1) * ii] += diag1 / coef[1]
            i = nbeads - 1
            h[i * ii : (i + 1) * ii, i * ii : (i + 1) * ii] += diag1 / coef[-2]
            for i in range(1, nbeads - 1):
                h[i * ii : (i + 1) * ii, i * ii : (i + 1) * ii] += diag1 * (
                    1.0 / coef[i] + 1.0 / coef[i + 1]
                )
        elif mode == "splitting" or mode == "full":
            for i in range(0, nbeads):
                h[i * ii : (i + 1) * ii, i * ii : (i + 1) * ii] += diag1 * (
                    1.0 / coef[i] + 1.0 / coef[i + 1]
                )
        else:
            raise ValueError("We can't compute the spring hessian.")

        # Non-Diagonal
        ndiag = np.diag(-h_sp)
        # Quasi-band
        for i in range(0, nbeads - 1):
            h[i * ii : (i + 1) * ii, (i + 1) * ii : (i + 2) * ii] += ndiag * (
                1.0 / coef[i + 1]
            )
            h[(i + 1) * ii : (i + 2) * ii, i * ii : (i + 1) * ii] += ndiag * (
                1.0 / coef[i + 1]
            )

        # Corner
        if mode == "full":
            h[0:ii, (nbeads - 1) * ii : (nbeads) * ii] += ndiag / coef[0]
            h[(nbeads - 1) * ii : (nbeads) * ii, 0:ii] += ndiag / coef[0]

        return h


class Mapper(object):
    """Creation of the multi-dimensional function that is the proxy between all the energy and force components and the optimization algorithm.
    It also handles fixatoms"""

    def __init__(self, esum=False):

        self.sm = SpringMapper()
        self.gm = PesMapper()
        self.esum = esum

    def initialize(self, q, forces):

        print("\nALBERTO1 get friction from forces object\n")
        eta = np.zeros((q.shape[0], q.shape[1], q.shape[1]))

        self.gm.save(forces.pots, -forces.f, eta)
        e1, g1 = self.gm.evaluate()
        e2, g2 = self.sm(q)

        g = self.fix.get_active_vector(g1 + g2, 1)
        e = np.sum(e1 + e2)

        self.save(e, g)

    def save(self, e, g):
        self.pot = e
        self.f = -g

    def bind(self, dumop):

        self.temp = dumop.temp
        self.beads = dumop.beads
        self.forces = dumop.forces
        self.cell = dumop.cell
        self.nm = dumop.nm

        self.fixatoms = dumop.fixatoms
        self.fix = dumop.fix
        self.fixbeads = self.fix.fixbeads

        self.options = dumop.options

        self.coef = np.ones(self.beads.nbeads + 1).reshape(-1, 1)
        self.set_coef(self.options["discretization"])

        self.friction = self.options["friction"]
        if self.friction:
            self.set_z_friction(self.options["z_friction"])

        self.sm.bind(self)
        self.gm.bind(self)

    def set_coef(self, coef):
        """ Sets coeficients for non-uniform instanton calculation """
        self.coef[:] = coef.reshape(-1, 1)

    def set_z_friction(self, z_friction):
        """Sets the scaling factors corresponding to frequency dependence of the friction """
        try:
            from scipy.interpolate import interp1d
        except ImportError:
            softexit.trigger(
                "Scipy required to use friction in a instanton calculation"
            )

        freq = units.unit_to_internal("frequency", "inversecm", z_friction[:, 0])
        spline = interp1d(
            freq, z_friction[:, 1], kind="cubic", fill_value=0.0, bounds_error=False
        )
        self.z_friction = spline(2 * self.nm.get_omegak())

    def __call__(self, x, mode="all", apply_fix=True, new_disc=True, ret=True):

        if mode == "all":
            e1, g1 = self.sm(x, new_disc)
            e2, g2 = self.gm(x, new_disc)
            e = e1 + e2
            g = np.add(g1, g2)

        elif mode == "physical":
            e, g = self.gm(x, new_disc)
        elif mode == "springs":
            e, g = self.sm(x, new_disc)
        else:
            softexit.trigger("Mode not recognized when calling  FullMapper")

        if apply_fix:
            g = self.fix.get_active_vector(g, 1)

        if mode == "all":
            self.save(np.sum(e), g)

        if self.esum:
            e = np.sum(e)

        if ret:
            return e, g


class DummyOptimizer(dobject):
    """ Dummy class for all optimization classes """

    def __init__(self):
        """Initializes object for PesMapper (physical potential, forces and hessian)
        and SpringMapper ( spring potential,forces and hessian)"""

        self.options = {}  # Optimization options
        self.optarrays = {}  # Optimization arrays

        self.mapper = Mapper()

        self.exit = False
        self.init = False

    def bind(self, geop):
        """
        Bind optimization options and call bind function of Mappers (get beads, cell,forces)
        check whether force size,  Hessian size from  match system size
        """

        self.beads = geop.beads
        self.cell = geop.cell
        self.forces = geop.forces
        self.fixcom = geop.fixcom
        self.fixatoms = geop.fixatoms

        self.fix = Fix(self.fixatoms, self.beads, self.beads.nbeads)
        self.nm = geop.nm

        self.output_maker = geop.output_maker

        # The resize action must be done before the bind

        if geop.optarrays["old_x"].size != self.beads.q.size:
            if geop.optarrays["old_x"].size == 0:
                geop.optarrays["old_x"] = np.zeros(
                    (self.beads.nbeads, 3 * self.beads.natoms), float
                )
            else:
                raise ValueError("Old positions size does not match system size")
        if geop.optarrays["old_u"].size != self.beads.nbeads:
            if geop.optarrays["old_u"].size == 0:
                geop.optarrays["old_u"] = np.zeros(self.beads.nbeads, float)
            else:
                raise ValueError("Old potential energy size does not match system size")
        if geop.optarrays["old_f"].size != self.beads.q.size:
            if geop.optarrays["old_f"].size == 0:
                geop.optarrays["old_f"] = np.zeros(
                    (self.beads.nbeads, 3 * self.beads.natoms), float
                )
            else:
                raise ValueError("Old forces size does not match system size")

        # Temperature
        self.temp = geop.ensemble.temp
        if geop.ensemble.temp == -1.0 or geop.ensemble.temp == 1.0:
            # This is due to a little inconsistency on the default value
            if self.beads.nbeads != 1:
                raise ValueError(
                    "Temperature must be specified for an Instanton calculation "
                )

        # Optimization mode
        self.options["mode"] = geop.options["mode"]

        # Generic optimization
        if geop.options["discretization"].size != self.beads.nbeads + 1:
            if geop.options["discretization"].size == 0:
                geop.options["discretization"] = np.ones(self.beads.nbeads + 1, float)
            else:
                raise ValueError(
                    "Discretization coefficients  does not match system size"
                )

        self.options["max_ms"] = geop.options["max_ms"]
        self.options["max_e"] = geop.options["max_e"]
        self.options["discretization"] = geop.options["discretization"]
        self.options["friction"] = geop.options["friction"]

        if self.options["friction"]:
            if len(geop.options["z_friction"]) == 0:
                geop.options["z_friction"] = np.ones((11, 2)) * 3  # ALBERTO
            self.options["z_friction"] = geop.options["z_friction"]

        self.options["tolerances"] = geop.options["tolerances"]
        self.optarrays["big_step"] = geop.optarrays["big_step"]
        self.optarrays["old_x"] = geop.optarrays["old_x"]
        self.optarrays["old_u"] = geop.optarrays["old_u"]
        self.optarrays["old_f"] = geop.optarrays["old_f"]
        self.options["opt"] = geop.options["opt"]  # optimization algorithm

        # Generic instanton
        self.options["save"] = geop.options["save"]
        self.options["prefix"] = geop.options["prefix"]
        self.optarrays["delta"] = geop.optarrays["delta"]
        self.options["hessian_final"] = geop.options["hessian_final"]
        self.optarrays["energy_shift"] = geop.optarrays["energy_shift"]

        # self.fix = Fix(geop.beads.natoms, geop.fixatoms, geop.beads.nbeads)

        self.mapper.bind(self)

    def initial_geo(self):
        """ Generates the initial instanton geometry by stretching the transitions-state geometry along the mode with imaginary frequency """

        info(
            " @GEOP: We stretch the initial geometry with an 'amplitud' of {:4.2f}".format(
                self.optarrays["delta"]
            ),
            verbosity.low,
        )

        fix_onebead = Fix(self.fixatoms, self.beads, 1)
        active_hessian = fix_onebead.get_active_vector(
            self.optarrays["initial_hessian"], 2
        )
        active_imvector = get_imvector(
            active_hessian, fix_onebead.fixbeads.m3[0].flatten()
        )
        imvector = fix_onebead.get_full_vector(active_imvector, 1).flatten()

        for i in range(self.beads.nbeads):
            self.beads.q[i, :] += (
                self.optarrays["delta"]
                * np.cos(i * np.pi / float(self.beads.nbeads - 1))
                * imvector[:]
            )

    def exitstep(self, d_x_max, step):
        """ Exits the simulation step. Computes time, checks for convergence. """
        self.qtime += time.time()

        tolerances = self.options["tolerances"]
        d_u = self.forces.pot - self.optarrays["old_u"].sum()

        active_force = self.mapper.f

        info(
            " @Exit step: Energy difference: {:4.2e}, (condition: {:4.2e})".format(
                np.absolute(d_u / self.fix.fixbeads.natoms), tolerances["energy"]
            ),
            verbosity.low,
        )
        info(
            " @Exit step: Maximum force component: {:4.2e}, (condition: {:4.2e})".format(
                np.amax(np.absolute(active_force)), tolerances["force"]
            ),
            verbosity.low,
        )
        info(
            " @Exit step: Maximum component step component: {:4.2e}, (condition: {:4.2e})".format(
                d_x_max, tolerances["position"]
            ),
            verbosity.low,
        )

        if (
            (np.absolute(d_u / self.mapper.sm.dbeads.natoms) <= tolerances["energy"])
            and (
                (np.amax(np.absolute(active_force)) <= tolerances["force"])
                or (
                    np.linalg.norm(
                        self.forces.f.flatten() - self.optarrays["old_f"].flatten()
                    )
                    <= 1e-08
                )
            )
            and (d_x_max <= tolerances["position"])
        ):

            print_instanton_geo(
                self.options["prefix"] + "_FINAL",
                step,
                self.beads.nbeads,
                self.beads.natoms,
                self.beads.names,
                self.beads.q,
                self.forces.f,
                self.forces.pots,
                self.cell,
                self.optarrays["energy_shift"],
                self.output_maker,
            )
            if not self.options["hessian_final"]:
                info("We are not going to compute the final hessian.", verbosity.low)
                info(
                    "Warning, The current hessian is not the real hessian is only an approximation .",
                    verbosity.low,
                )

            else:
                info("We are going to compute the final hessian", verbosity.low)
                self.optarrays["hessian"][:] = get_hessian(
                    self.mapper.gm,
                    self.beads.q.copy(),
                    self.beads.natoms,
                    self.beads.nbeads,
                    self.fixatoms,
                )

                print_instanton_hess(
                    self.options["prefix"] + "_FINAL",
                    step,
                    self.optarrays["hessian"],
                    self.output_maker,
                )

            return True
            # If we just exit here, the last step (including the last hessian) will not be in the RESTART file

        return False

    def update_pos_for(self):
        """ Update positions and forces """

        self.beads.q[:] = self.mapper.gm.dbeads.q[:]

        # This forces the update of the forces
        self.forces.transfer_forces(self.mapper.gm.dforces)

    def update_old_pos_for(self):
        """ Update 'old' positions and forces arrays """

        self.optarrays["old_x"][:] = self.beads.q
        self.optarrays["old_u"][:] = self.forces.pots
        self.optarrays["old_f"][:] = self.forces.f

    def print_geo(self, step):
        """ Small interface to call the function that prints thet instanton geometry """

        if (
            self.options["save"] > 0 and np.mod(step, self.options["save"]) == 0
        ) or self.exit:
            print_instanton_geo(
                self.options["prefix"],
                step,
                self.beads.nbeads,
                self.beads.natoms,
                self.beads.names,
                self.beads.q,
                self.forces.f,
                self.forces.pots,
                self.cell,
                self.optarrays["energy_shift"],
                self.output_maker,
            )

    def pre_step(self, step=None, adaptative=False):
        """ General tasks that have to be performed before actual step"""

        if self.exit:
            softexit.trigger("Geometry optimzation converged. Exiting simulation")

        if not self.init:
            self.initialize(step)

        if adaptative:
            softexit.trigger("Adaptative discretization is not fully implemented")
            # new_coef = <implement_here>
            # self.mapper.set_coef(coef)
            raise NotImplementedError

        self.qtime = -time.time()
        info("\n Instanton optimization STEP {}".format(step), verbosity.low)

        activearrays = self.fix.get_active_array(self.optarrays)

        return activearrays

    def step(self, step=None):
        """Dummy simulationt step which does nothing."""
        pass

    def opt_coef(self, coef):
        # func = lambda x: 2 * np.sum(x) - x[0] - x[-1]
        def func(x):
            return 2 * np.sum(x) - x[0] - x[-1]

        coef = np.absolute(coef)
        s = func(coef)
        coef *= 2 * self.sm.dbeads.nbeads / s
        # c0   = 2*self.sm.dbeads.nbeads - 2*np.sum(coef)
        # coef = np.insert(coef,0,c0)

        fphys = self.gm.dforces.f * ((coef[1:] + coef[:-1]) / 2).reshape(-1, 1)
        e, gspring = self.sm(self.sm.dbeads.q)
        return np.amax(np.absolute(-gspring + fphys))


class HessianOptimizer(DummyOptimizer):
    """ Instaton Rate calculation"""

    def bind(self, geop):
        # call bind function from DummyOptimizer
        super(HessianOptimizer, self).bind(geop)

        self.options["hessian_update"] = geop.options["hessian_update"]
        self.options["hessian_asr"] = geop.options["hessian_asr"]

        if len(self.fixatoms) > 0:
            info(" 'fixatoms' is enabled. Setting asr to None", verbosity.low)
            self.options["hessian_asr"] = "none"
        #        self.output_maker = geop.output_maker
        self.options["hessian_init"] = geop.options["hessian_init"]
        self.optarrays["initial_hessian"] = None

        if geop.optarrays["hessian"].size != (
            self.beads.natoms * 3 * self.beads.q.size
        ):
            if geop.optarrays["hessian"].size == (self.beads.natoms * 3) ** 2:
                self.optarrays["initial_hessian"] = geop.optarrays["hessian"].copy()
                geop.optarrays["hessian"] = np.zeros(
                    (self.beads.natoms * 3, self.beads.q.size), float
                )

            elif geop.optarrays["hessian"].size == 0 and geop.options["hessian_init"]:
                info(
                    " Initial hessian is not provided. We are going to compute it.",
                    verbosity.low,
                )
                geop.optarrays["hessian"] = np.zeros(
                    (self.beads.natoms * 3, self.beads.q.size)
                )

                if (
                    (self.beads.q - self.beads.q[0]) == 0
                ).all() and self.beads.nbeads > 1:
                    raise ValueError(
                        """We need a initial hessian in order to create our initial
                    instanton geometry. Please provide a (1-bead) hessian or an initial instanton geometry."""
                    )

            else:
                raise ValueError(
                    " 'Hessian_init' is false, an initial hessian (of the proper size) must be provided."
                )

        self.optarrays["hessian"] = geop.optarrays["hessian"]

    def initialize(self, step):

        if step == 0:

            info(" @GEOP: Initializing INSTANTON", verbosity.low)

            if self.beads.nbeads == 1:

                info(" @GEOP: Classical TS search", verbosity.low)

            else:
                # If the coordinates in all the imaginary time slices are the same
                if ((self.beads.q - self.beads.q[0]) == 0).all():

                    self.initial_geo()
                    self.options["hessian_init"] = True

                else:

                    info(
                        " @GEOP: Starting from the provided geometry in the extended phase space",
                        verbosity.low,
                    )
                    if not (self.optarrays["initial_hessian"] is None):
                        raise ValueError(
                            " You have to provided a hessian with size (3 x natoms)^2 but also geometry in"
                            " the extended phase space (nbeads>1). Please check the inputs\n"
                        )

        # Initialize all the mappers
        self.mapper.initialize(self.beads.q, self.forces)

        if self.options["hessian_init"]:
            self.optarrays["hessian"][:] = get_hessian(
                self.mapper.gm,
                self.beads.q.copy(),
                self.beads.natoms,
                self.beads.nbeads,
                self.fixatoms,
            )
            # active_hessian = self.mapper.fix.get_active_vector( self.optarrays["hessian"],t=2 )

        self.update_old_pos_for()

        self.init = True

    def update_hessian(self, update, active_hessian, new_x, d_x, d_g):
        """ Update hessian """

        if update == "powell":

            i = self.fix.fixbeads.natoms * 3
            for j in range(self.fix.fixbeads.nbeads):
                aux = active_hessian[:, j * i : (j + 1) * i]
                dg = d_g[j, :]
                dx = d_x[j, :]
                Powell(dx, dg, aux)

        elif update == "recompute":
            active_hessian = get_hessian(
                self.mapper.gm,
                new_x,
                self.beads.natoms,
                self.beads.nbeads,
                self.fixatoms,
            )

        self.optarrays["hessian"][:] = self.fix.get_full_vector(active_hessian, 2)

    def print_hess(self, step):
        if (
            self.options["save"] > 0 and np.mod(step, self.options["save"]) == 0
        ) or self.exit:
            print_instanton_hess(
                self.options["prefix"],
                step,
                self.optarrays["hessian"],
                self.output_maker,
            )

    def post_step(self, step, new_x, d_x, activearrays):
        """ General tasks that have to be performed after finding the new step"""

        d_x_max = np.amax(np.absolute(d_x))
        info("Current step norm = {}".format(d_x_max), verbosity.medium)

        # Get energy and forces(f) for the new position
        self.mapper(new_x, ret=False)

        # Update
        f = self.fix.get_active_vector(self.mapper.gm.f, t=1)
        d_g = np.subtract(activearrays["old_f"], f)

        self.update_hessian(
            self.options["hessian_update"], activearrays["hessian"], new_x, d_x, d_g
        )

        self.update_pos_for()

        #  Print
        self.print_geo(step)
        self.print_hess(step)

        # Check Exit and only then update old arrays
        self.exit = self.exitstep(d_x_max, step)
        self.update_old_pos_for()


class NicholsOptimizer(HessianOptimizer):
    """ Class that implements a nichols optimization. It can find first order saddle points or minimum"""

    def step(self, step=None):
        """ Does one simulation step."""

        activearrays = self.pre_step(step)

        # First construct complete hessian from reduced
        h0 = red2comp(
            activearrays["hessian"],
            self.fix.fixbeads.nbeads,
            self.fix.fixbeads.natoms,
            self.mapper.coef,
        )

        # Add spring terms to the physical hessian
        h1 = np.add(self.mapper.sm.h, h0)

        # Get eigenvalues and eigenvector.
        d, w = clean_hessian(
            h1,
            self.fix.fixbeads.q,
            self.fix.fixbeads.natoms,
            self.fix.fixbeads.nbeads,
            self.fix.fixbeads.m,
            self.fix.fixbeads.m3,
            self.options["hessian_asr"],
        )

        # d,w =np.linalg.eigh(h1) #Cartesian
        info(
            "\n@Nichols: 1st freq {} cm^-1".format(
                units.unit_to_user(
                    "frequency", "inversecm", np.sign(d[0]) * np.sqrt(np.absolute(d[0]))
                )
            ),
            verbosity.medium,
        )
        info(
            "@Nichols: 2nd freq {} cm^-1".format(
                units.unit_to_user(
                    "frequency", "inversecm", np.sign(d[1]) * np.sqrt(np.absolute(d[1]))
                )
            ),
            verbosity.medium,
        )
        info(
            "@Nichols: 3rd freq {} cm^-1".format(
                units.unit_to_user(
                    "frequency", "inversecm", np.sign(d[2]) * np.sqrt(np.absolute(d[2]))
                )
            ),
            verbosity.medium,
        )

        # Find new movement direction
        if self.options["mode"] == "rate":
            f = (
                activearrays["old_f"]
                * (self.mapper.coef[1:] + self.mapper.coef[:-1])
                / 2
            )
            d_x = nichols(
                f,
                self.mapper.sm.f,
                d,
                w,
                self.fix.fixbeads.m3,
                activearrays["big_step"],
            )
        elif self.options["mode"] == "splitting":
            d_x = nichols(
                activearrays["old_f"],
                self.mapper.sm.f,
                d,
                w,
                self.mapper.sm.dbeads.m3,
                activearrays["big_step"],
                mode=0,
            )

        # Rescale step if necessary
        if np.amax(np.absolute(d_x)) > activearrays["big_step"]:
            info(
                "Step norm, scaled down to {}".format(activearrays["big_step"]),
                verbosity.low,
            )
            d_x *= activearrays["big_step"] / np.amax(np.absolute(d_x))

        # Get the new full-position
        d_x_full = self.fix.get_full_vector(d_x, t=1)
        new_x = self.optarrays["old_x"].copy() + d_x_full

        self.post_step(step, new_x, d_x, activearrays)


class NROptimizer(HessianOptimizer):
    """ Class that implements a Newton-Raphson optsmization. It can find first order saddle points or minimum"""

    def step(self, step=None):
        """ Does one simulaion step."""
        activearrays = self.pre_step(step)

        dyn_mat = get_dynmat(
            activearrays["hessian"], self.sm.dbeads.m3, self.sm.dbeads.nbeads
        )
        h_up_band = banded_hessian(
            dyn_mat, self.sm, masses=False, shift=0.0000001
        )  # create upper band matrix

        fff = activearrays["old_f"] * (self.mapper.coef[1:] + self.mapper.coef[:-1]) / 2
        f = (fff + self.sm.f).reshape(
            self.sm.dbeads.natoms * 3 * self.sm.dbeads.nbeads, 1
        )
        f = np.multiply(f, self.sm.dbeads.m3.reshape(f.shape) ** -0.5)

        d_x = invmul_banded(h_up_band, f).reshape(self.sm.dbeads.q.shape)
        d_x = np.multiply(d_x, self.sm.dbeads.m3 ** -0.5)

        # Rescale step if necessary
        if np.amax(np.absolute(d_x)) > activearrays["big_step"]:
            info(
                "Step norm, scaled down to {}".format(activearrays["big_step"]),
                verbosity.low,
            )
            d_x *= activearrays["big_step"] / np.amax(np.absolute(d_x))

        # Get the new full-position
        d_x_full = self.fix.get_full_vector(d_x, t=1)
        new_x = self.optarrays["old_x"].copy() + d_x_full

        self.post_step(step, new_x, d_x, activearrays)


class LanczosOptimizer(HessianOptimizer):
    """Class that implements a modified Nichols algorithm based on Lanczos diagonalization to avoid constructing and diagonalizing
    the full (3*natoms*nbeads)^2 matrix"""

    def step(self, step=None):
        """ Does one simulation step."""

        activearrays = self.pre_step(step)

        f = self.mapper.f.reshape(
            self.fix.fixbeads.natoms * 3 * self.fix.fixbeads.nbeads, 1
        )

        banded = False
        banded = True
        if banded:
            # BANDED Version
            # MASS-scaled
            dyn_mat = get_dynmat(
                activearrays["hessian"], self.fix.fixbeads.m3, self.fix.fixbeads.nbeads
            )

            h_up_band = banded_hessian(
                dyn_mat, self.mapper.sm, masses=False, shift=0.000000001
            )  # create upper band matrix
            f = np.multiply(f, self.fix.fixbeads.m3.reshape(f.shape) ** -0.5)
            # CARTESIAN
            # h_up_band = banded_hessian(activearrays["hessian"], self.sm.masses=True)  # create upper band matrix

            d = diag_banded(h_up_band)
        else:
            # FULL dimensional version
            h_0 = red2comp(
                activearrays["hessian"],
                self.sm.dbeads.nbeads,
                self.sm.dbeads.natoms,
                self.mapper.coef,
            )
            h_test = np.add(self.sm.h, h_0)  # add spring terms to the physical hessian
            d, w = clean_hessian(
                h_test,
                self.sm.dbeads.q,
                self.sm.dbeads.natoms,
                self.sm.dbeads.nbeads,
                self.sm.dbeads.m,
                self.sm.dbeads.m3,
                None,
            )
            # CARTESIAN
            # d,w =np.linalg.eigh(h_test) #Cartesian
        info(
            "\n@Lanczos: 1st freq {} cm^-1".format(
                units.unit_to_user(
                    "frequency", "inversecm", np.sign(d[0]) * np.sqrt(np.absolute(d[0]))
                )
            ),
            verbosity.medium,
        )
        info(
            "@Lanczos: 2nd freq {} cm^-1".format(
                units.unit_to_user(
                    "frequency", "inversecm", np.sign(d[1]) * np.sqrt(np.absolute(d[1]))
                )
            ),
            verbosity.medium,
        )
        info(
            "@Lanczos: 3rd freq {} cm^-1\n".format(
                units.unit_to_user(
                    "frequency", "inversecm", np.sign(d[2]) * np.sqrt(np.absolute(d[2]))
                )
            ),
            verbosity.medium,
        )

        if d[0] > 0:
            if d[1] / 2 > d[0]:
                alpha = 1
                lamb = (2 * d[0] + d[1]) / 4
            else:
                alpha = (d[1] - d[0]) / d[1]
                lamb = (
                    3 * d[0] + d[1]
                ) / 4  # midpoint between b[0] and b[1]*(1-alpha/2)
        elif d[1] < 0:  # Jeremy Richardson
            if d[1] >= d[0] / 2:
                alpha = 1
                lamb = (d[0] + 2 * d[1]) / 4
            else:
                alpha = (d[0] - d[1]) / d[1]
                lamb = (d[0] + 3 * d[1]) / 4
        # elif d[1] < 0:  #Litman for Second Order Saddle point
        #    alpha = 1
        #    lamb = (d[1] + d[2]) / 4
        #    print 'WARNING: We are not using the standard Nichols'
        #    print 'd_x', d_x[0],d_x[1]

        else:  # Only d[0] <0
            alpha = 1
            lamb = (d[0] + d[1]) / 4

        if banded:
            h_up_band[-1, :] += -np.ones(h_up_band.shape[1]) * lamb
            d_x = invmul_banded(h_up_band, f)
        else:
            h_test = alpha * (h_test - np.eye(h_test.shape[0]) * lamb)
            d_x = np.linalg.solve(h_test, f)

        d_x.shape = self.fix.fixbeads.q.shape

        # MASS-scaled
        d_x = np.multiply(d_x, self.fix.fixbeads.m3 ** -0.5)

        # Rescale step if necessary
        if np.amax(np.absolute(d_x)) > activearrays["big_step"]:
            info(
                "Step norm, scaled down to {}".format(activearrays["big_step"]),
                verbosity.low,
            )
            d_x *= activearrays["big_step"] / np.amax(np.absolute(d_x))

        # Get the new full-position
        d_x_full = self.fix.get_full_vector(d_x, t=1)
        new_x = self.optarrays["old_x"].copy() + d_x_full

        self.post_step(step, new_x, d_x, activearrays)


class LBFGSOptimizer(DummyOptimizer):
    def bind(self, geop):
        # call bind function from DummyOptimizer
        super(LBFGSOptimizer, self).bind(geop)

        if geop.optarrays["hessian"].size == (self.beads.natoms * 3) ** 2:
            self.optarrays["initial_hessian"] = geop.optarrays["hessian"].copy()
            geop.optarrays["hessian"] = np.zeros(
                (self.beads.natoms * 3, self.beads.q.size)
            )

        if geop.options["hessian_final"]:
            self.options["hessian_asr"] = geop.options["hessian_asr"]
            if geop.optarrays["hessian"].size == 0:
                geop.optarrays["hessian"] = np.zeros(
                    (self.beads.natoms * 3, self.beads.q.size)
                )
            self.optarrays["hessian"] = geop.optarrays["hessian"]

        # self.sm.bind(self, self.options["discretization"])

        # Specific for LBFGS
        self.options["corrections"] = geop.options["corrections"]
        self.options["ls_options"] = geop.options["ls_options"]
        if geop.optarrays["qlist"].size != (
            self.options["corrections"] * self.beads.q.size
        ):
            if geop.optarrays["qlist"].size == 0:
                geop.optarrays["qlist"] = np.zeros(
                    (self.options["corrections"], self.beads.q.size), float
                )
            else:
                raise ValueError("qlist size does not match system size")
        if geop.optarrays["glist"].size != (
            self.options["corrections"] * self.beads.q.size
        ):
            if geop.optarrays["glist"].size == 0:
                geop.optarrays["glist"] = np.zeros(
                    (self.options["corrections"], self.beads.q.size), float
                )
            else:
                raise ValueError("qlist size does not match system size")

        self.optarrays["qlist"] = geop.optarrays["qlist"]
        self.optarrays["glist"] = geop.optarrays["glist"]

        if geop.options["scale"] not in [0, 1, 2]:
            raise ValueError("Scale option is not valid")

        self.options["scale"] = geop.options["scale"]

        if geop.optarrays["d"].size != self.beads.q.size:
            if geop.optarrays["d"].size == 0:
                geop.optarrays["d"] = np.zeros(
                    (self.beads.nbeads, 3 * self.beads.natoms), float
                )
            else:
                raise ValueError("Initial direction size does not match system size")

        self.optarrays["d"] = geop.optarrays["d"]

        self.mapper.esum = True

    def initialize(self, step):

        if step == 0:
            info(" @GEOP: Initializing instanton", verbosity.low)

            if self.beads.nbeads == 1:
                raise ValueError(
                    "We can not perform an splitting calculation with nbeads =1"
                )

            else:
                if ((self.beads.q - self.beads.q[0]) == 0).all():
                    # If the coordinates in all the imaginary time slices are the same
                    self.initial_geo()
                else:
                    info(
                        " @GEOP: Starting from the provided geometry in the extended phase space",
                        verbosity.low,
                    )

        # This must be done after the stretching and before the self.d.
        # Initialize all the mapper
        self.mapper.initialize(self.beads.q, self.forces)
        # if self.mapper.sm.f is None:
        #    self.mapper.sm(self.beads.q, ret=False)  # Init instanton mapper

        if (
            self.optarrays["old_x"]
            == np.zeros((self.beads.nbeads, 3 * self.beads.natoms), float)
        ).all():
            self.optarrays["old_x"][:] = self.beads.q

        # Specific for LBFGS
        if np.linalg.norm(self.optarrays["d"]) == 0.0:
            # f = self.forces.f + self.mapper.sm.f
            f = self.mapper.f
            self.optarrays["d"] += dstrip(f) / np.sqrt(np.dot(f.flatten(), f.flatten()))

        self.update_old_pos_for()
        self.init = True

    def post_step(self, step, activearrays):

        """ General tasks that have to be performed after the  actual step"""

        # Update
        self.optarrays["qlist"][:] = self.fix.get_full_vector(
            activearrays["qlist"], t=3
        )
        self.optarrays["glist"][:] = self.fix.get_full_vector(
            activearrays["glist"], t=3
        )
        self.optarrays["d"][:] = self.fix.get_full_vector(activearrays["d"], t=1)

        self.update_pos_for()

        self.print_geo(step)

        # Check Exit and only then update old arrays
        d_x_max = np.amax(
            np.absolute(np.subtract(self.beads.q, self.optarrays["old_x"]))
        )
        self.exit = self.exitstep(d_x_max, step)
        self.update_old_pos_for()

    def step(self, step=None):
        """ Does one simulation step."""

        activearrays = self.pre_step(step)

        e, g = self.mapper(self.beads.q)
        fdf0 = (e, g)

        # Do one step. Update the position and force inside the mapper.
        print(
            activearrays["big_step"],
            self.options["ls_options"]["tolerance"],
            self.options["tolerances"]["energy"],
            self.options["ls_options"]["iter"],
            self.options["corrections"],
            self.options["scale"],
            step,
        )

        L_BFGS(
            activearrays["old_x"],
            activearrays["d"],
            self.mapper,
            activearrays["qlist"],
            activearrays["glist"],
            fdf0,
            activearrays["big_step"],
            self.options["ls_options"]["tolerance"]
            * self.options["tolerances"]["energy"],
            self.options["ls_options"]["iter"],
            self.options["corrections"],
            self.options["scale"],
            step,
        )

        self.post_step(step, activearrays)
