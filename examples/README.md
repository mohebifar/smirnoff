# Examples for Bayesian atomtype sampler

## Manifest
* `parm@frosst/` - example illustrating attempt to recover parm@frosst atom types
* `AlkEtOH/` - example illustrating attempt to recover parm99 atom types for a small set of alkanes, ethers, and alcohols
* `SMIRFF_comparison/` - temporary example (waiting for permanent home) of cross-comparison of molecule energies from SMIRFF with same molecule energies from .prmtop and .crd.
* `SMIRFF_simulation/` - gives a simple example of simulating a molecule in the gas phase beginning from a molecular structure and the SMIRFF forcefield format.
* `forcefield_modification/` - gives example of modifying a forcefield parameter, evaluating how it changes an energy (IPython notebook).
* `chemicalEnvironments/` - contains an example and documentation of using chemical environment objects to manipulate environment being considered, generate example SMIRKS, etc. Also contains IPython notebook using the chemical environment for depiction.
* `smirff99Frosst` - contains an under-development manual conversion of amber-parm99+parm@Frosst to smirff format via an intermediate smirffishFrcmod format. The hope is to have an intial guess smirff for small molecules.
* `partial_bondorder/` - example of using partial bond orders for parameterizing molecules, and showing how the SMIRFF format extends. (See issue #53 on Smarty).
