# SMIRFF FFXML files for smarty.forcefield

## Manifest
- `Frosst_AlkEtOH.ffxml`: Original FFXML file for the AlkEthOH region of chemical space/set as hand created by Christopher Bayly
- `Frosst_AlkEtOH_parmAtFrosst.ffxml`: Same as the above but with additional "bugs" introduced (see comments in the FFXML) in order to reproduce problems introduced by atom typing in the AMBER forcefields, typically missing torsions which resulted in specialized torsions instead being given the default. In other words, this forcefield file can reproduce erroneous AMBER energies.
- `Frosst_AlkEtOH_MDL.ffxml`: Same as Frosst_AlkEtOH.ffxml but uses the MDL aromaticity model for bond perception. No functional difference for this region of chemical space, but illustrates how to invoke that model.
- `Frosst_AlkEthOH_withIDs`: Adds parameter IDs to Frosst_AlkEtOH.ffxml for use by the forcefield labeler to show which parameters are used in which places. Otherwise identical.
- `benzene_minimal.ffxml`: Minimal ffxml file for benzene constructed from smirff99Frosst 9/22/16 (dx.doi.org/10.5281/zenodo.154235), intended for use in testing impropers. 
